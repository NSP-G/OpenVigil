# -*- coding: utf-8 -*-
"""自检与告警历史功能的单元测试。

运行：python -m pytest tests/test_features.py -v
"""
import json
import os
import sys
import tempfile
import threading

import pytest
from PIL import Image

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from app import config as config_mod, notifier
from app import selftest as selftest_mod
from app.alert_history import AlertHistory
from app.selftest import SelfTest, make_classroom_frame


# ==================== 告警历史测试 ====================

class TestAlertHistory:
    def test_add_and_list(self):
        with tempfile.TemporaryDirectory() as td:
            h = AlertHistory(os.path.join(td, "h.json"))
            h.add("打闹", "后排两名学生打闹", 0.82, image_path="/tmp/a.jpg",
                  window_title="高一3班")
            recs = h.list()
            assert len(recs) == 1
            r = recs[0]
            assert r["type"] == "打闹"
            assert r["detail"] == "后排两名学生打闹"
            assert abs(r["confidence"] - 0.82) < 1e-9
            assert r["image"] == "/tmp/a.jpg"
            assert r["window"] == "高一3班"
            assert r["local_fallback"] is False
            assert r["time"]  # 时间字符串非空

    def test_newest_first(self):
        with tempfile.TemporaryDirectory() as td:
            h = AlertHistory(os.path.join(td, "h.json"))
            h.add("先", "第一条", 0.5)
            h.add("后", "第二条", 0.6)
            recs = h.list()
            assert recs[0]["type"] == "后"
            assert recs[1]["type"] == "先"

    def test_persistence_across_instances(self):
        """历史必须能跨进程/重启保留，否则「告警历史」栏目没有意义。"""
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "h.json")
            h = AlertHistory(path)
            h.add("聚集", "三人聚集", 0.7)
            h.add("离座", "多人离座", 0.55)

            h2 = AlertHistory(path)
            assert h2.count() == 2, "重新打开程序后历史应仍在"
            assert h2.list()[0]["type"] == "离座"

    def test_max_records_cap(self):
        with tempfile.TemporaryDirectory() as td:
            h = AlertHistory(os.path.join(td, "h.json"), max_records=3)
            for i in range(10):
                h.add("类型%d" % i, "detail", 0.5)
            assert h.count() == 3
            assert h.list()[0]["type"] == "类型9"

    def test_clear(self):
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "h.json")
            h = AlertHistory(path)
            h.add("x", "y", 0.5)
            h.clear()
            assert h.count() == 0
            # 清空后新建实例不应把旧数据读回来（惰性加载标志的回归测试）
            assert AlertHistory(path).count() == 0

    def test_corrupt_file_does_not_crash(self):
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "h.json")
            with open(path, "w", encoding="utf-8") as f:
                f.write("{ 这不是合法 json ]")
            h = AlertHistory(path)
            assert h.count() == 0
            assert h.load_error, "应记录加载失败原因供界面提示"

    def test_non_list_json_ignored(self):
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "h.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump({"unexpected": "shape"}, f)
            assert AlertHistory(path).count() == 0

    def test_confidence_normalized(self):
        with tempfile.TemporaryDirectory() as td:
            h = AlertHistory(os.path.join(td, "h.json"))
            h.add("a", "百分制", 80)      # 80 → 0.8
            h.add("b", "字符串", "0.65")
            h.add("c", "非法", "很高")
            recs = h.list()
            by_type = {r["type"]: r["confidence"] for r in recs}
            assert abs(by_type["a"] - 0.8) < 1e-9
            assert abs(by_type["b"] - 0.65) < 1e-9
            assert by_type["c"] == 0.0

    def test_missing_file_ok(self):
        with tempfile.TemporaryDirectory() as td:
            h = AlertHistory(os.path.join(td, "不存在.json"))
            assert h.count() == 0
            assert h.load_error is None

    def test_thread_safety(self):
        """巡检线程写、界面线程读，不应崩溃或丢记录。"""
        with tempfile.TemporaryDirectory() as td:
            h = AlertHistory(os.path.join(td, "h.json"), max_records=1000)
            errors = []

            def writer(tag):
                try:
                    for i in range(50):
                        h.add("t%s" % tag, "d%d" % i, 0.5)
                except Exception as e:  # pragma: no cover
                    errors.append(e)

            def reader():
                try:
                    for _ in range(50):
                        h.list(limit=10)
                        h.count()
                except Exception as e:  # pragma: no cover
                    errors.append(e)

            threads = [threading.Thread(target=writer, args=(i,)) for i in range(3)]
            threads += [threading.Thread(target=reader) for _ in range(2)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            assert not errors, f"并发访问出错: {errors}"
            assert h.count() == 150, "并发写入不应丢记录"

    def test_local_fallback_flag(self):
        with tempfile.TemporaryDirectory() as td:
            h = AlertHistory(os.path.join(td, "h.json"))
            h.add("画面持续剧烈活动", "本地兜底", 0.7, local_fallback=True)
            assert h.list()[0]["local_fallback"] is True


# ==================== 合成画面测试 ====================

class TestSyntheticFrame:
    def test_frame_size_and_mode(self):
        img = make_classroom_frame()
        assert isinstance(img, Image.Image)
        assert img.size == (640, 400)
        assert img.mode == "RGB"

    def test_deterministic(self):
        """同一参数生成的画面必须一致，否则自检结果不可复现。"""
        a = make_classroom_frame(frame_idx=3, chaos=0.7, seed=5)
        b = make_classroom_frame(frame_idx=3, chaos=0.7, seed=5)
        assert list(a.getdata()) == list(b.getdata())

    def test_chaos_changes_frame(self):
        quiet = make_classroom_frame(chaos=0.0)
        chaotic = make_classroom_frame(frame_idx=2, chaos=0.9, seed=3)
        assert list(quiet.getdata()) != list(chaotic.getdata())


# ==================== 自检测试 ====================

class FakeClient:
    """用于自检测试的假 API 客户端。"""

    def __init__(self, replies=None, raise_error=None):
        # replies: 按顺序回放；用完后循环最后一条
        self.replies = list(replies or [])
        self.raise_error = raise_error
        self.calls = []

    def analyze(self, image, prompt, model=None):
        return self._reply(prompt)

    def analyze_multi(self, frames, prompt, model=None):
        """多帧分析：与单帧共用回放脚本。"""
        return self._reply(prompt)

    def analyze_diff(self, reference, current, prompt, model=None):
        """场景差异描述：默认「无变化」。"""
        self.calls.append(prompt)
        return '{"changed": false, "changes": [], "confidence": 0.9}'

    def _reply(self, prompt):
        self.calls.append(prompt)
        if self.raise_error is not None:
            raise self.raise_error
        if not self.replies:
            return "正常"
        idx = min(len(self.calls) - 1, len(self.replies) - 1)
        return self.replies[idx]

    def close(self):
        pass


def _base_cfg(**overrides):
    cfg = dict(config_mod.DEFAULT_CONFIG)
    cfg["api_key"] = "test-key"
    cfg.update(overrides)
    return cfg


@pytest.fixture
def selftest_env(tmp_path, monkeypatch):
    """准备自检环境：日志目录 + 可注入假客户端的工厂。"""
    notifier.init(str(tmp_path / "logs"))
    holder = {}

    def install(replies=None, raise_error=None):
        client = FakeClient(replies, raise_error)
        holder["client"] = client
        monkeypatch.setattr(
            selftest_mod, "ZhipuVisionClient",
            lambda **kw: client,
        )
        return client

    return install


def _find(steps, key):
    for s in steps:
        if s["key"] == key:
            return s
    return None


class TestSelfTest:
    def test_missing_api_key_fails_config_step(self, selftest_env):
        cfg = _base_cfg(api_key="")
        report = SelfTest(cfg, full=False).run()
        step = _find(report["steps"], "config")
        assert step["status"] == "fail"
        assert "API Key" in step["hint"]
        assert report["overall"] == "fail"

    def test_too_high_threshold_flagged(self, selftest_env):
        """告警阈值过高是「从不报警」的常见原因，自检必须能抓出来。"""
        cfg = _base_cfg(alert_confidence=0.95)
        report = SelfTest(cfg, full=False).run()
        step = _find(report["steps"], "config")
        assert step["status"] == "fail"
        assert "告警阈值" in step["hint"]

    def test_too_high_activity_trigger_flagged(self, selftest_env):
        cfg = _base_cfg(activity_trigger=0.8)
        report = SelfTest(cfg, full=False).run()
        step = _find(report["steps"], "config")
        assert step["status"] == "fail"
        assert "活动度门控" in step["hint"]

    def test_connectivity_failure_reported(self, selftest_env):
        from app.zhipu_client import ZhipuError
        selftest_env(raise_error=ZhipuError("401 鉴权失败", status_code=401))
        report = SelfTest(_base_cfg(), full=False).run()
        step = _find(report["steps"], "connectivity")
        assert step["status"] == "fail"
        assert "API Key" in step["hint"], "401 应给出重新生成 Key 的建议"

    def test_rate_limit_hint(self, selftest_env):
        from app.zhipu_client import ZhipuError
        selftest_env(raise_error=ZhipuError("限流", status_code=429))
        report = SelfTest(_base_cfg(), full=False).run()
        step = _find(report["steps"], "connectivity")
        assert "限流" in step["hint"]

    def test_all_pass_scenario(self, selftest_env):
        """模型对混乱图报异常、对安静图报正常 → 六项全过。"""
        selftest_env(replies=[
            "正常",                                                    # 连通性探针
            '{"abnormal": true, "type": "多人离座", "detail": "多名学生离开座位走动", "confidence": 0.8}',  # 混乱图
            '{"abnormal": false, "detail": "学生均在座位上自习", "confidence": 0.9}',                      # 安静图
            '{"abnormal": true, "type": "多人离座", "detail": "多人离座", "confidence": 0.75}',            # e2e 初判
            '{"abnormal": true, "type": "多人离座", "detail": "确认", "confidence": 0.75}',                # e2e 复核
        ])
        report = SelfTest(_base_cfg(), full=True).run()
        keys = [s["key"] for s in report["steps"]]
        assert "config" in keys and "connectivity" in keys
        assert _find(report["steps"], "model_chaos")["status"] == "pass"
        assert _find(report["steps"], "model_calm")["status"] == "pass"
        assert _find(report["steps"], "local_activity")["status"] == "pass"
        assert _find(report["steps"], "e2e")["status"] == "pass"
        assert report["overall"] == "pass"

    def test_blind_model_caught_by_step3(self, selftest_env):
        """模型对明显混乱的画面判正常 → 第③项必须报失败并给出建议。"""
        selftest_env(replies=[
            "正常",
            '{"abnormal": false, "detail": "画面不清晰，无法判断"}',   # 混乱图被判正常
            '{"abnormal": false, "detail": "正常"}',
            '{"abnormal": false, "detail": "无法判断"}',
        ])
        report = SelfTest(_base_cfg(), full=True).run()
        step = _find(report["steps"], "model_chaos")
        assert step["status"] == "fail"
        assert "模型没能识别" in step["hint"] or "换用" in step["hint"]

    def test_blind_model_still_alerts_via_local_fallback(self, selftest_env):
        """关键回归：模型完全看不见混乱时，本地兜底必须让端到端链路仍然告警。"""
        selftest_env(replies=[
            "正常",
            '{"abnormal": false, "detail": "无法判断"}',
            '{"abnormal": false, "detail": "正常"}',
            '{"abnormal": false, "detail": "未发现异常"}',
        ])
        report = SelfTest(_base_cfg(), full=True).run()
        e2e = _find(report["steps"], "e2e")
        assert e2e["status"] == "pass", \
            f"模型失效时本地兜底应兜住告警，实际：{e2e['detail']}"

    def test_false_positive_tendency_warns(self, selftest_env):
        """模型对安静画面也报异常 → 应给出误报提示（warn 而非 fail）。"""
        selftest_env(replies=[
            "正常",
            '{"abnormal": true, "type": "离座", "detail": "疑似", "confidence": 0.7}',
            '{"abnormal": true, "type": "离座", "detail": "疑似", "confidence": 0.7}',  # 安静图误报
            '{"abnormal": true, "type": "离座", "detail": "疑似", "confidence": 0.7}',
        ])
        report = SelfTest(_base_cfg(), full=True).run()
        step = _find(report["steps"], "model_calm")
        assert step["status"] == "warn"
        assert "误报" in step["hint"]

    def test_quick_mode_skips_expensive_steps(self, selftest_env):
        """快速自检只做配置与连通性，不跑昂贵的模型判定与端到端演练。"""
        selftest_env(replies=["正常"])
        report = SelfTest(_base_cfg(), full=False).run()
        keys = [s["key"] for s in report["steps"]]
        assert "model_chaos" not in keys
        assert "config" in keys and "connectivity" in keys
        e2e = _find(report["steps"], "e2e")
        assert e2e["status"] == "warn", "快速自检的 e2e 应标记为已跳过"

    def test_progress_callback_invoked(self, selftest_env):
        selftest_env(replies=["正常", "正常", "正常", "正常"])
        seen = []
        SelfTest(_base_cfg(), progress_cb=seen.append, full=True).run()
        assert len(seen) >= 4, "每个检查项都应通过回调上报进度"
        assert all("key" in s and "status" in s for s in seen)

    def test_report_structure(self, selftest_env):
        selftest_env(replies=["正常", "正常", "正常", "正常"])
        report = SelfTest(_base_cfg(), full=True).run()
        assert report["ok"] is True
        assert report["overall"] in ("pass", "warn", "fail")
        assert isinstance(report["steps"], list)
        assert isinstance(report["summary"], str) and report["summary"]
        assert isinstance(report["elapsed"], float)
        for step in report["steps"]:
            assert set(["key", "title", "status", "detail", "hint"]).issubset(step)

    def test_local_activity_step_always_runs(self, selftest_env):
        """本地活动度检测不依赖 API，即使连通性失败也要执行。"""
        from app.zhipu_client import ZhipuError
        selftest_env(raise_error=ZhipuError("网络不可达"))
        report = SelfTest(_base_cfg(), full=False).run()
        assert _find(report["steps"], "local_activity") is not None
        assert _find(report["steps"], "local_activity")["status"] == "pass"

    def test_selftest_does_not_pollute_user_alert_history(self, selftest_env, tmp_path, monkeypatch):
        """回归：自检是演练，绝不能把假告警写进用户的告警历史栏目。"""
        from gui import main_gui
        monkeypatch.setattr(main_gui.config_mod, "_project_root", lambda: str(tmp_path))
        history = main_gui.Bridge(lambda: None)._get_history()
        assert history.count() == 0

        selftest_env(replies=[
            "正常",
            '{"abnormal": true, "type": "多人离座", "detail": "多人离座", "confidence": 0.8}',
            '{"abnormal": false, "detail": "正常"}',
            '{"abnormal": true, "type": "多人离座", "detail": "多人离座", "confidence": 0.75}',
        ])
        SelfTest(_base_cfg(), full=True).run()

        # 用户历史文件必须仍然干净
        assert history.count() == 0, \
            f"自检污染了用户告警历史，写入了 {history.count()} 条假记录"

    def test_selftest_does_not_fire_desktop_notifications(self, selftest_env, monkeypatch):
        """回归：一次自检会连续判定多帧异常，绝不能连弹多条系统通知。"""
        fired = []
        monkeypatch.setattr(
            selftest_mod.notifier, "notify",
            lambda *a, **kw: fired.append(a),
        )
        selftest_env(replies=[
            "正常",
            '{"abnormal": true, "type": "多人离座", "detail": "多人离座", "confidence": 0.8}',
            '{"abnormal": false, "detail": "正常"}',
            '{"abnormal": true, "type": "多人离座", "detail": "多人离座", "confidence": 0.75}',
        ])
        report = SelfTest(_base_cfg(), full=True).run()
        e2e = _find(report["steps"], "e2e")
        assert e2e["status"] == "pass", "本场景应能触发告警"
        assert not fired, f"自检期间不应弹系统通知，实际弹了 {len(fired)} 次"

    def test_no_temp_files_left_behind(self, selftest_env, tmp_path):
        """自检产生的临时截图必须清理干净，不能污染用户磁盘。"""
        selftest_env(replies=[
            "正常",
            '{"abnormal": true, "type": "打闹", "detail": "打闹", "confidence": 0.9}',
            '{"abnormal": false, "detail": "正常"}',
            '{"abnormal": true, "type": "打闹", "detail": "打闹", "confidence": 0.9}',
        ])
        SelfTest(_base_cfg(), full=True).run()
        leftovers = [p for p in os.listdir(tempfile.gettempdir())
                     if p.startswith("vigil_selftest_")]
        assert not leftovers, f"自检临时目录未清理: {leftovers}"


# ==================== 界面桥接层接口测试 ====================

class TestBridgeAPI:
    def test_bridge_exposes_new_methods(self):
        from gui.main_gui import Bridge
        for name in ("run_selftest", "get_alert_history",
                     "clear_alert_history", "open_alert_image"):
            assert hasattr(Bridge, name), f"Bridge 缺少界面所需的方法: {name}"

    def test_bridge_methods_are_public(self):
        """名字不能以下划线开头，否则 pywebview 不会暴露给 JS。"""
        from gui.main_gui import Bridge
        for name in ("run_selftest", "get_alert_history",
                     "clear_alert_history", "open_alert_image"):
            assert not name.startswith("_"), name

    def test_history_methods_end_to_end(self, tmp_path, monkeypatch):
        """验证 Bridge 的历史接口能真正读写文件。"""
        from gui import main_gui
        monkeypatch.setattr(
            main_gui.config_mod, "_project_root", lambda: str(tmp_path)
        )
        bridge = main_gui.Bridge(lambda: None)
        res = bridge.get_alert_history(10)
        assert res["ok"] is True
        assert res["records"] == []
        # 直接操作历史对象写入一条，再读回
        bridge._get_history().add("打闹", "测试", 0.8)
        res2 = bridge.get_alert_history(10)
        assert res2["total"] == 1
        assert res2["records"][0]["type"] == "打闹"
        cleared = bridge.clear_alert_history()
        assert cleared["ok"] is True
        assert bridge.get_alert_history(10)["total"] == 0

    def test_open_missing_image_returns_error(self, tmp_path):
        from gui import main_gui
        bridge = main_gui.Bridge(lambda: None)
        res = bridge.open_alert_image(str(tmp_path / "不存在.jpg"))
        assert res["ok"] is False
        assert "不存在" in res["error"]


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
