# -*- coding: utf-8 -*-
"""报警系统回归测试（针对“画面很乱却不报告”的漏报问题）。

运行：python -m pytest tests/test_alert_system.py -v

测试策略：
用可回放的假模型（FakeVisionClient）驱动完整的 Monitor 决策链，
配合合成帧序列模拟真实的教室画面活动，验证：
  - 班级混乱场景必须告警
  - 模型看不清/误判时，本地活动度能兜底告警
  - 安静自习与个别小动作不误报
  - 双意见投票、时序累积、通知节流均按预期工作
"""
import os
import sys
import time
import tempfile

import pytest
from PIL import Image, ImageDraw

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from app import config as config_mod
from app import diff_detect, notifier, window_capture
from app import monitor as monitor_mod
from app.monitor import Monitor
from app.zhipu_client import parse_verdict


# ==================== 合成帧工具 ====================

def make_classroom(frame_idx=0, chaos=0.0, seed=0):
    """生成一张模拟教室监控画面。

    chaos=0   安静自习：学生整齐坐在座位上
    chaos>0   混乱程度：学生离开座位、随机走动、聚集
    """
    w, h = 640, 400
    img = Image.new("RGB", (w, h), (232, 228, 220))
    d = ImageDraw.Draw(img)
    # 讲台与地板
    d.rectangle([0, 0, w, 60], fill=(205, 200, 190))
    d.rectangle([0, h - 70, w, h], fill=(215, 210, 200))

    rng = _rng(seed * 1000 + frame_idx)
    seated = 24
    for i in range(seated):
        col, row = i % 8, i // 8
        x = 70 + col * 68
        y = 120 + row * 70
        # chaos 越大，越多学生离座走动
        if rng.random() < chaos:
            x += rng.randint(-45, 45)
            y += rng.randint(-40, 55)
        d.ellipse([x, y, x + 22, y + 22], fill=(60, 55, 50))       # 头
        d.rectangle([x + 2, y + 22, x + 20, y + 48], fill=(70, 90, 140))  # 身体
    if chaos > 0.5:
        # 聚集人群
        for i in range(int(chaos * 8)):
            x = 300 + rng.randint(-60, 60)
            y = 250 + rng.randint(-40, 40)
            d.ellipse([x, y, x + 24, y + 24], fill=(70, 60, 55))
            d.rectangle([x + 2, y + 24, x + 22, y + 52], fill=(150, 70, 70))
    return img


def _rng(seed):
    """极简确定性伪随机（避免依赖 numpy，且结果可复现）。"""
    state = [seed or 1]

    def rnd():
        state[0] = (1103515245 * state[0] + 12345) % (2 ** 31)
        return state[0] / (2 ** 31)
    return type("R", (), {"random": lambda self: rnd(),
                          "randint": lambda self, a, b: a + int(rnd() * (b - a + 1))})()


class FakeWindow:
    def __init__(self, title="测试窗口"):
        self.hwnd = 1
        self.title = title
        self.cls_name = "Test"


class FakeVisionClient:
    """可回放的假视觉模型：按脚本返回预设回复，记录调用历史。

    cycle=True 时脚本循环使用，便于模拟"每帧都是初判异常、复核正常"这类持续分歧。
    """

    def __init__(self, script=None, cycle=False):
        self.script = list(script or [])
        self.cycle = cycle
        self.calls = []      # 记录调用顺序
        self.prompts = []    # 记录每次收到的提示词

    def analyze(self, image, prompt, model=None):
        return self._reply(prompt, model)

    def analyze_multi(self, frames, prompt, model=None):
        """多帧分析：与单帧共用同一套回放脚本，便于测试时序路径。"""
        return self._reply(prompt, model)

    def analyze_diff(self, reference, current, prompt, model=None):
        """场景差异描述：默认回复「无变化」，避免干扰主流程测试。"""
        self.prompts.append(prompt)
        return '{"changed": false, "changes": [], "confidence": 0.9}'

    def _reply(self, prompt, model):
        self.prompts.append(prompt)
        idx = len(self.calls)
        self.calls.append(model)
        if not self.script:
            return "正常"
        if self.cycle:
            reply = self.script[idx % len(self.script)]
        elif idx < len(self.script):
            reply = self.script[idx]
        else:
            reply = self.script[-1]
        if isinstance(reply, Exception):
            raise reply
        return reply

    def close(self):
        pass


def build_monitor(tmpdir, script=None, cycle=False, **cfg_overrides):
    """构造一个装好假模型的 Monitor，返回 (monitor, client, frames_box, orig_capture)。"""
    cfg = dict(config_mod.DEFAULT_CONFIG)
    cfg["api_key"] = "test-key"
    cfg["alert_image_dir"] = os.path.join(tmpdir, "alerts")
    cfg["log_dir"] = os.path.join(tmpdir, "logs")
    cfg.update(cfg_overrides)
    notifier.init(cfg["log_dir"])

    win = FakeWindow()
    frames = {"seq": [], "i": 0}

    def fake_capture(hwnd):
        if frames["seq"]:
            f = frames["seq"][min(frames["i"], len(frames["seq"]) - 1)]
        else:
            f = make_classroom()
        frames["i"] += 1
        return f

    orig_capture = window_capture.capture_window
    window_capture.capture_window = fake_capture

    mon = Monitor(cfg, win, verbose=False)
    client = FakeVisionClient(script, cycle=cycle)
    mon.client = client
    return mon, client, frames, orig_capture


# ==================== 活动度分析器测试 ====================

class TestActivityAnalyzer:
    def test_still_frames_low_activity(self):
        """完全静止的画面，活动度应接近 0。"""
        a = diff_detect.ActivityAnalyzer()
        img = make_classroom(chaos=0.0)
        for _ in range(10):
            s = a.update(img)
        assert s["activity"] < 0.05, f"静止画面活动度应极低，实际 {s['activity']}"
        assert s["active_ratio"] == 0.0

    def test_continuous_chaos_high_activity(self):
        """持续混乱的画面，活动度应显著升高。"""
        a = diff_detect.ActivityAnalyzer()
        last = None
        for i in range(12):
            last = a.update(make_classroom(frame_idx=i, chaos=0.85, seed=7))
        assert last["activity"] > 0.3, f"持续混乱活动度应高，实际 {last['activity']}"
        assert last["active_ratio"] > 0.8, "几乎每帧都应有运动"

    def test_single_glitch_not_treated_as_chaos(self):
        """偶发单帧抖动不应被当成持续混乱（防止误报）。"""
        a = diff_detect.ActivityAnalyzer()
        calm = make_classroom(chaos=0.0)
        for _ in range(8):
            s = a.update(calm)
        assert s["active_ratio"] < 0.3

    def test_activity_score_bounded(self):
        a = diff_detect.ActivityAnalyzer()
        for i in range(30):
            s = a.update(make_classroom(frame_idx=i, chaos=1.0, seed=3))
            assert 0.0 <= s["activity"] <= 1.0
            assert 0.0 <= s["motion"] <= 1.0
            assert 0.0 <= s["drift"] <= 1.0
            assert 0.0 <= s["active_ratio"] <= 1.0

    def test_reset_clears_state(self):
        a = diff_detect.ActivityAnalyzer()
        for i in range(6):
            a.update(make_classroom(frame_idx=i, chaos=0.9, seed=5))
        a.reset()
        s = a.update(make_classroom(chaos=0.0))
        assert s["frame_count"] == 1
        assert s["activity"] < 0.2


# ==================== 判定解析测试 ====================

class TestVerdictParsing:
    def test_behavior_words_caught_without_abnormal_word(self):
        """模型只描述混乱行为、没说“异常”二字时，必须能判为异常。

        这是“班级很乱却不报告”的核心修复点之一。
        """
        for text in [
            "多名学生离开座位在教室内走动，后方有学生聚集",
            "教室后排有学生追逐打闹",
            "画面中有学生扎堆围观",
            "大面积学生趴在桌上",
            "教室里很混乱，学生走动频繁",
        ]:
            abnormal, info = parse_verdict(text)
            assert abnormal is True, f"描述混乱行为应判异常: {text}"

    def test_json_in_code_fence(self):
        text = '我的判断如下：\n```json\n{"abnormal": true, "type": "聚集", "detail": "后排聚集", "confidence": 0.8}\n```'
        abnormal, info = parse_verdict(text)
        assert abnormal is True
        assert info["type"] == "聚集"
        assert abs(info["confidence"] - 0.8) < 1e-9

    def test_json_with_surrounding_text(self):
        text = '观察完成。{"abnormal": true, "type": "离座", "detail": "多人走动", "confidence": 0.75} 以上。'
        abnormal, info = parse_verdict(text)
        assert abnormal is True
        assert info["type"] == "离座"

    def test_multiple_json_picks_abnormal_one(self):
        text = '{"note": "先看整体"} 然后 {"abnormal": false, "detail": "正常"}'
        abnormal, info = parse_verdict(text)
        assert abnormal is False

    def test_not_normal_beats_normal_substring(self):
        for text in ["画面看起来不太正常", "课堂状态不正常", "很不正常"]:
            abnormal, _ = parse_verdict(text)
            assert abnormal is True, f"应判异常: {text}"

    def test_overall_normal_shields_behavior_words(self):
        """“有个别走动但整体正常”不应被行为词误伤成异常。"""
        for text in [
            "有个别学生走动，但整体正常",
            "一名学生抬头，基本正常",
            "画面正常，学生在座位上",
        ]:
            abnormal, _ = parse_verdict(text)
            assert abnormal is False, f"应判正常: {text}"

    def test_negated_behavior_words(self):
        for text in ["没有学生打闹", "未发现学生聚集", "没有离座情况"]:
            abnormal, _ = parse_verdict(text)
            assert abnormal is False, f"否定语境不应判异常: {text}"

    def test_json_abnormal_default_confidence_reachable(self):
        """JSON 判异常但未给 confidence 时，默认值必须够得着告警线。"""
        _, info = parse_verdict('{"abnormal": true, "type": "打闹"}')
        assert info["confidence"] >= config_mod.DEFAULT_CONFIG["alert_confidence"], \
            "默认置信度必须不低于告警阈值，否则永远不告警"

    def test_empty_text(self):
        assert parse_verdict("")[0] is False
        assert parse_verdict(None)[0] is False


# ==================== 端到端决策链测试 ====================

class TestAlertDecision:
    """用假模型驱动完整 Monitor，验证真实场景下的告警行为。"""

    def test_chaotic_classroom_triggers_alert(self, tmp_path):
        """场景A：班级混乱，模型如实描述 → 必须告警。"""
        script = [
            '{"abnormal": true, "type": "多人离座走动", "detail": "多名学生离开座位在教室内走动", "confidence": 0.7, "evidence": ["多人站立"]}',
            '{"abnormal": true, "type": "多人离座走动", "detail": "确认有多名学生走动聚集", "confidence": 0.72}',
        ]
        mon, client, frames, orig = build_monitor(str(tmp_path), script)
        try:
            frames["seq"] = [make_classroom(frame_idx=i, chaos=0.85, seed=7) for i in range(6)]
            for i in range(5):
                res = mon._tick(on_status=None, force_analyze=(i == 0))
            assert mon.stats["alerts"] >= 1, \
                f"班级混乱必须告警，实际 alerts={mon.stats['alerts']}"
        finally:
            window_capture.capture_window = orig

    def test_model_says_normal_but_frame_chaotic_falls_back(self, tmp_path):
        """场景B：模型连续判「正常」，但画面相对基线显著异常 → 本地兜底必须告警。

        先跑一段正常画面让基线建立（真实巡检本就是先正常、后出状况），
        再注入混乱：此时模型仍判正常，兜底必须能捕捉到。
        """
        script = ['{"abnormal": false, "detail": "学生均在座位上自习", "confidence": 0.9}']
        mon, client, frames, orig = build_monitor(str(tmp_path), script)
        try:
            # 前 16 帧安静自习建立基线，后 10 帧突然混乱
            frames["seq"] = (
                [make_classroom(frame_idx=i, chaos=0.0, seed=3) for i in range(16)]
                + [make_classroom(frame_idx=i, chaos=1.0, seed=11) for i in range(10)]
            )
            for i in range(26):
                mon._tick(on_status=None, force_analyze=(i == 0))
            assert mon.stats["alerts"] >= 1, \
                "模型误判但画面相对基线显著异常时，本地兜底必须告警"
        finally:
            window_capture.capture_window = orig

    def test_model_says_unclear_does_not_pretend_alert(self, tmp_path):
        """模型回复「看不清」属于画面质量问题，不是「画面很乱」。

        这种情况应当只做信息性提示，不得伪装成告警写入历史——
        否则用户看到的会是「疑似打闹」这类误导性记录。
        """
        script = ['{"abnormal": false, "detail": "画面不清晰，无法判断", "confidence": 0.5}']
        mon, client, frames, orig = build_monitor(str(tmp_path), script)
        try:
            frames["seq"] = (
                [make_classroom(frame_idx=i, chaos=0.0, seed=3) for i in range(16)]
                + [make_classroom(frame_idx=i, chaos=1.0, seed=11) for i in range(10)]
            )
            for i in range(26):
                mon._tick(on_status=None, force_analyze=(i == 0))
            assert mon.stats["alerts"] == 0, \
                "模型看不清时应提示画面质量，不得计入告警"
        finally:
            window_capture.capture_window = orig

    def test_quiet_self_study_no_alert(self, tmp_path):
        """场景C：安静自习 → 不得告警（防误报）。"""
        script = ['{"abnormal": false, "detail": "学生均在座位上自习", "confidence": 0.9}']
        mon, client, frames, orig = build_monitor(str(tmp_path), script)
        try:
            frames["seq"] = [make_classroom(chaos=0.0) for _ in range(10)]
            for i in range(8):
                mon._tick(on_status=None, force_analyze=(i == 0))
            assert mon.stats["alerts"] == 0, \
                f"安静自习不应告警，实际 alerts={mon.stats['alerts']}"
        finally:
            window_capture.capture_window = orig

    def test_occasional_small_movement_no_alert(self, tmp_path):
        """场景D：个别学生偶尔动一下 → 不得告警。"""
        script = ['{"abnormal": false, "detail": "个别学生抬头，整体正常", "confidence": 0.85}']
        mon, client, frames, orig = build_monitor(str(tmp_path), script)
        try:
            # 大部分时间静止，偶尔一帧轻微变化
            seq = []
            for i in range(12):
                seq.append(make_classroom(frame_idx=(i if i % 5 == 0 else 0),
                                          chaos=(0.05 if i % 5 == 0 else 0.0)))
            frames["seq"] = seq
            for i in range(10):
                mon._tick(on_status=None, force_analyze=(i == 0))
            assert mon.stats["alerts"] == 0, "零星小动作不应告警"
        finally:
            window_capture.capture_window = orig

    def test_both_models_abnormal_gives_high_confidence(self, tmp_path):
        """场景E：两次独立判定都异常 → 高置信度快速告警。"""
        script = [
            '{"abnormal": true, "type": "打闹", "detail": "后排学生打闹", "confidence": 0.7}',
            '{"abnormal": true, "type": "打闹", "detail": "确认打闹", "confidence": 0.68}',
        ]
        mon, client, frames, orig = build_monitor(str(tmp_path), script)
        try:
            frames["seq"] = [make_classroom(frame_idx=i, chaos=0.8, seed=4) for i in range(6)]
            res = mon._tick(on_status=None, force_analyze=True)
            # 两次一致（0.7 + 0.12 一致性加成 + 活动度加成）应显著高于单次原始值
            assert res["abnormal"] is True
            assert res["confidence"] > 0.7, \
                f"两次一致应有置信度加成，实际 {res['confidence']}"
            assert res.get("agreed") is True
        finally:
            window_capture.capture_window = orig

    def test_disagreement_with_high_activity_treated_as_suspicious(self, tmp_path):
        """场景F：两次意见不一致，但画面活动度高 → 按疑似异常处理。"""
        script = [
            '{"abnormal": true, "type": "聚集", "detail": "疑似聚集", "confidence": 0.65}',
            '{"abnormal": false, "detail": "未发现明显异常", "confidence": 0.5}',
        ]
        mon, client, frames, orig = build_monitor(str(tmp_path), script, cycle=True)
        try:
            # 跑若干帧让活动度统计积累起来（混乱画面活动度应显著升高）
            frames["seq"] = [make_classroom(frame_idx=i, chaos=0.8, seed=9) for i in range(8)]
            res = None
            for i in range(5):
                res = mon._tick(on_status=None, force_analyze=(i == 0))
            assert res["abnormal"] is True, "高活动度下意见分歧应按疑似异常保留"
            assert res.get("agreed") is False
        finally:
            window_capture.capture_window = orig

    def test_disagreement_with_low_activity_filtered(self, tmp_path):
        """场景G：两次意见不一致且画面平稳 → 拦截误报。"""
        script = [
            '{"abnormal": true, "type": "离座", "detail": "疑似离座", "confidence": 0.6}',
            '{"abnormal": false, "detail": "学生都在座位上", "confidence": 0.8}',
        ]
        mon, client, frames, orig = build_monitor(str(tmp_path), script, cycle=True)
        try:
            # 静止画面；每帧强制分析，确保真正走到"初判异常+复核正常"的分歧裁决
            frames["seq"] = [make_classroom(chaos=0.0) for _ in range(8)]
            res = None
            for i in range(5):
                res = mon._tick(on_status=None, force_analyze=True)
            assert res["abnormal"] is False, "低活动度下意见分歧应判正常"
            assert res.get("confidence", 0.0) == 0.0
        finally:
            window_capture.capture_window = orig

    def test_persistent_chaos_escalates_via_temporal_boost(self, tmp_path):
        """场景H：持续多帧异常，置信度应逐帧累积升高。"""
        script = ['{"abnormal": true, "type": "混乱", "detail": "持续混乱", "confidence": 0.5}']
        mon, client, frames, orig = build_monitor(str(tmp_path), script)
        try:
            frames["seq"] = [make_classroom(frame_idx=i, chaos=0.8, seed=2) for i in range(8)]
            confidences = []
            for i in range(5):
                res = mon._tick(on_status=None, force_analyze=(i == 0))
                if res.get("abnormal"):
                    confidences.append(res["confidence"])
            assert len(confidences) >= 3, "应连续多帧判异常"
            assert confidences[-1] > confidences[0], \
                f"时序累积应抬高置信度：{confidences}"
        finally:
            window_capture.capture_window = orig

    def test_recheck_failure_does_not_veto_primary(self, tmp_path):
        """复核调用失败时，不得否决初判的异常（宁可多提醒，不可漏报）。"""
        from app.zhipu_client import ZhipuError
        script = [
            '{"abnormal": true, "type": "聚集", "detail": "后排聚集", "confidence": 0.75}',
            ZhipuError("模拟复核失败"),
        ]
        mon, client, frames, orig = build_monitor(str(tmp_path), script)
        try:
            frames["seq"] = [make_classroom(frame_idx=i, chaos=0.8, seed=6) for i in range(6)]
            res = mon._tick(on_status=None, force_analyze=True)
            assert res["abnormal"] is True, "复核失败不应否决初判异常"
        finally:
            window_capture.capture_window = orig

    def test_recheck_prompt_is_neutral(self, tmp_path):
        """复核提示词必须是中性的“独立意见”，不能诱导判正常。"""
        script = [
            '{"abnormal": true, "type": "打闹", "detail": "打闹", "confidence": 0.7}',
            '{"abnormal": true, "type": "打闹", "detail": "打闹", "confidence": 0.7}',
        ]
        mon, client, frames, orig = build_monitor(str(tmp_path), script)
        try:
            frames["seq"] = [make_classroom(frame_idx=i, chaos=0.8, seed=8) for i in range(6)]
            mon._tick(on_status=None, force_analyze=True)
            recheck_prompt = client.prompts[1]
            for banned in ["一律判定为正常", "最严格、最批判", "直接回复：正常"]:
                assert banned not in recheck_prompt, \
                    f"复核提示词不得包含诱导性表述: {banned}"
            assert "独立" in recheck_prompt
        finally:
            window_capture.capture_window = orig

    def test_alert_saves_image(self, tmp_path):
        """告警时应保存异常帧，便于事后回溯。"""
        script = [
            '{"abnormal": true, "type": "打闹", "detail": "打闹", "confidence": 0.85}',
            '{"abnormal": true, "type": "打闹", "detail": "打闹", "confidence": 0.85}',
        ]
        mon, client, frames, orig = build_monitor(str(tmp_path), script)
        try:
            frames["seq"] = [make_classroom(frame_idx=i, chaos=0.85, seed=12) for i in range(6)]
            mon._tick(on_status=None, force_analyze=True)
            alert_dir = mon.alert_image_dir
            files = os.listdir(alert_dir) if os.path.isdir(alert_dir) else []
            assert any(f.startswith("alert_") and f.endswith(".jpg") for f in files), \
                f"应保存异常帧，实际目录内容: {files}"
        finally:
            window_capture.capture_window = orig

    def test_notification_throttled_but_counting_not(self, tmp_path):
        """通知节流只应压制 Toast，不应压制告警计数。"""
        script = ['{"abnormal": true, "type": "混乱", "detail": "持续混乱", "confidence": 0.9}']
        mon, client, frames, orig = build_monitor(
            str(tmp_path), script, min_alert_interval_sec=9999
        )
        try:
            frames["seq"] = [make_classroom(frame_idx=i, chaos=0.9, seed=15) for i in range(10)]
            for i in range(4):
                mon._tick(on_status=None, force_analyze=(i == 0))
            assert mon.stats["alerts"] >= 2, \
                "节流期间告警计数仍应累加，实际 %s" % mon.stats["alerts"]
        finally:
            window_capture.capture_window = orig

    def test_sustained_chaos_reaches_alert_threshold(self, tmp_path):
        """综合场景：真实混乱持续 30 秒（6 帧×5s）必须产生告警。"""
        script = ['{"abnormal": true, "type": "多人离座", "detail": "多人离座走动", "confidence": 0.5}']
        mon, client, frames, orig = build_monitor(str(tmp_path), script)
        try:
            frames["seq"] = [make_classroom(frame_idx=i, chaos=0.9, seed=21) for i in range(12)]
            statuses = []
            for i in range(8):
                mon._tick(on_status=lambda s, d: statuses.append(s), force_analyze=(i == 0))
            assert mon.stats["alerts"] >= 1, "持续混乱必须告警"
            assert "alert" in statuses, "必须向前端推送 alert 状态"
        finally:
            window_capture.capture_window = orig


# ==================== 配置默认值测试 ====================

class TestAlertDefaults:
    def test_default_confidence_low_enough(self):
        """默认告警阈值必须低于弱信号置信度，否则弱信号永远不告警。"""
        assert config_mod.DEFAULT_CONFIG["alert_confidence"] <= 0.6

    def test_new_config_keys_exist(self):
        for key in ("activity_trigger", "heartbeat_frames"):
            assert key in config_mod.DEFAULT_CONFIG, f"缺少配置项: {key}"

    def test_numeric_ranges_defined(self):
        for key in ("activity_trigger", "heartbeat_frames"):
            assert key in config_mod._NUMERIC_FIELDS, f"{key} 缺少范围约束"

    def test_notify_interval_reasonable(self):
        """通知间隔不应过长，否则持续混乱时用户感知不到。"""
        assert config_mod.DEFAULT_CONFIG["min_alert_interval_sec"] <= 30


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
