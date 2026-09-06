# -*- coding: utf-8 -*-
"""v0.3 记忆系统 · 时空逻辑 · 判定升级 专项测试。

这些用例存在的意义，是把三条设计决策固化下来：

  1. 日志记「发生了什么」，环境文件记「我们知道什么」——两者不可混同
  2. 单帧判定无法区分「聚集围观」与「打闹冲突」，区别在时间里
  3. 判断异常用「相对自身常态」，而非全局固定阈值

若哪天有人为了图省事把判据改回全局阈值或二值判定，这组测试会立刻失败。

运行：python -m pytest tests/test_memory_system.py -v
"""
import json
import os
import random
import sys
import tempfile

import pytest
from PIL import Image, ImageDraw

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from app import config as config_mod, diff_detect, notifier
from app import monitor as monitor_mod
from app.environment import Baseline, Environment
from app.indexer import LogIndexer, subject_event_rank
from app.memory import EventTracker, ObservationLog
from app.zhipu_client import (
    CATEGORY_GATHERING,
    CATEGORY_NORMAL_CLASS,
    CATEGORY_SCUFFLE,
    build_observe_prompt,
    normalize_category,
    parse_diff,
    parse_observation,
)


# ==================== 测试素材 ====================

def classroom_base():
    """整齐就座的教室画面。"""
    img = Image.new("RGB", (640, 400), (232, 228, 220))
    d = ImageDraw.Draw(img)
    d.rectangle([0, 0, 640, 60], fill=(205, 200, 190))
    for r in range(4):
        for c in range(8):
            x, y = 70 + c * 68, 120 + r * 70
            d.ellipse([x, y, x + 22, y + 22], fill=(60, 55, 50))
            d.rectangle([x + 2, y + 22, x + 20, y + 48], fill=(70, 90, 140))
    return img


def with_poster(img):
    """在左墙贴一张新表——模拟「新贴分组表」场景。"""
    img = img.copy()
    d = ImageDraw.Draw(img)
    d.rectangle([8, 90, 58, 200], fill=(250, 248, 240))
    d.rectangle([8, 90, 58, 200], outline=(90, 90, 90))
    for i in range(6):
        d.line([12, 100 + i * 16, 54, 100 + i * 16], fill=(120, 120, 120))
    return img


def normal_class(i):
    random.seed(i)
    img = classroom_base()
    d = ImageDraw.Draw(img)
    tx = 120 + (i * 22) % 380
    d.rectangle([tx, 30, tx + 38, 95], fill=(45, 45, 50))
    return img


# ==================== 场景指纹 ====================

class TestSceneHash:
    def test_same_frame_same_hash(self):
        a = classroom_base()
        assert diff_detect.scene_hash(a) == diff_detect.scene_hash(a)

    def test_poster_changes_hash(self):
        """墙上新贴一张表，指纹必须改变——这是场景变化检测的基础。"""
        base = classroom_base()
        poster = with_poster(base)
        assert diff_detect.scene_hash(base) != diff_detect.scene_hash(poster)

    def test_hamming_distance_zero_for_identical(self):
        h = diff_detect.scene_hash(classroom_base())
        assert diff_detect.hamming_distance(h, h) == 0

    def test_minor_jitter_within_tolerance(self):
        """轻微抖动（模拟压缩噪声）应落在容忍范围内，不能误报为场景变化。"""
        random.seed(1)
        base = classroom_base()
        d = ImageDraw.Draw(base)
        for _ in range(30):   # 少量椒盐噪声
            d.point((random.randint(0, 639), random.randint(0, 399)),
                    fill=(random.randint(0, 255),) * 3)
        dist = diff_detect.hamming_distance(
            diff_detect.scene_hash(classroom_base()),
            diff_detect.scene_hash(base))
        assert dist is not None and dist <= 12, f"轻微抖动距离 {dist} 过大"

    def test_invalid_hash_returns_none(self):
        assert diff_detect.hamming_distance(None, "abc") is None
        assert diff_detect.hamming_distance("abc", "xyz") is None


# ==================== 日志四层结构 ====================

class TestObservationLog:
    def test_frame_level(self, tmp_path):
        log = ObservationLog(str(tmp_path))
        fid = log.frame(entities=[{"region": "3-2", "posture": "head_down"}],
                        activity=0.4)
        assert fid.startswith("fr-")
        recs = list(log.iter_records(level="frame"))
        assert len(recs) == 1
        assert recs[0]["entities"][0]["region"] == "3-2"

    def test_four_levels_coexist(self, tmp_path):
        log = ObservationLog(str(tmp_path))
        log.frame(entities=[])
        log.event(kind="gathering", region="1-1")
        log.alert(category="scuffle", description="打闹", confidence=0.8)
        log.correction(target_id="al-1", verdict="dismissed", reason="误报")
        levels = {r["level"] for r in log.iter_records()}
        assert levels == {"frame", "event", "alert", "correction"}

    def test_append_only_never_mutates(self, tmp_path):
        """日志只增不改：写入后无法被后续操作篡改。"""
        log = ObservationLog(str(tmp_path))
        log.event(kind="gathering")
        log.event(kind="scuffle")
        assert log.count(level="event") == 2

    def test_alert_separates_observation_from_inference(self, tmp_path):
        """观察与推断必须分字段存储——这是合规与可纠错的前提。"""
        log = ObservationLog(str(tmp_path))
        log.alert(category="head_down",
                  observation="位置3-2持续低头约240秒，未见书写动作",
                  description="疑似在看与课堂无关的内容")
        rec = list(log.iter_records(level="alert"))[0]
        assert "未" in rec["observation"] and "240" in rec["observation"]
        assert "疑似" in rec["description"]
        assert rec["status"] == "pending"

    def test_correction_records_feedback(self, tmp_path):
        log = ObservationLog(str(tmp_path))
        aid = log.alert(category="scuffle", description="打闹")
        log.correction(target_id=aid, verdict="dismissed",
                       reason="在看新贴的值日表", replacement="聚集围观")
        rec = list(log.iter_records(level="correction"))[0]
        assert rec["target_id"] == aid
        assert rec["replacement"] == "聚集围观"

    def test_retention_purge(self, tmp_path):
        """保留期外的数据必须能清理——对着未成年人建长期画像是合规敏感项。"""
        log = ObservationLog(str(tmp_path), retention_days=1)
        log.frame(entities=[])
        assert len(log._list_files()) == 1
        # 把文件时间改到 3 天前
        for p in log._list_files():
            old = os.path.getmtime(p) - 3 * 86400
            os.utime(p, (old, old))
        removed = log.purge_expired()
        assert len(removed) == 1
        assert log.count() == 0

    def test_export_range(self, tmp_path):
        log = ObservationLog(str(tmp_path))
        log.frame(entities=[])
        dest = str(tmp_path / "export.jsonl")
        today = __import__("time").strftime("%Y%m%d")
        n = log.export_range(today, today, dest)
        assert n == 1


# ==================== 环境文件 ====================

class TestBaseline:
    def test_first_sample(self):
        b = Baseline()
        b.update(0.5)
        assert b.mean == 0.5 and b.samples == 1

    def test_stable_values_low_deviation(self):
        b = Baseline()
        for _ in range(30):
            b.update(0.5)
        assert abs(b.deviation(0.52)) < 0.5

    def test_spike_high_deviation(self):
        b = Baseline()
        for _ in range(30):
            b.update(0.5)
        assert b.deviation(0.05) < -2.0

    def test_insufficient_samples_returns_zero(self):
        """样本不足时一律返回 0（不判定异常），避免开局乱报。"""
        b = Baseline()
        b.update(0.5)
        assert b.deviation(5.0) == 0.0

    def test_std_floor_prevents_blowup(self):
        """完全恒定的基线：标准差为 0 时不能被放大成无穷大。"""
        b = Baseline()
        for _ in range(20):
            b.update(0.30)
        assert abs(b.deviation(0.32)) < 1.0


class TestEnvironment:
    def test_zero_annotation_works(self, tmp_path):
        """零标注基线：不填任何姓名，系统照样建立位置档案。"""
        env = Environment(str(tmp_path / "env.yml"))
        sub = env.subject("3-2")
        assert sub["name"] is None
        s = env.subject_summary("3-2")
        assert s["label"] == "3-2"

    def test_link_name_is_incremental(self, tmp_path):
        """名字关联是增量增强，不是启动前提。"""
        env = Environment(str(tmp_path / "env.yml"))
        env.subject("3-2")
        env.link_name("3-2", "李雷", confidence=0.85)
        assert env.subject_summary("3-2")["name"] == "李雷"

    def test_low_confidence_name_marked(self, tmp_path):
        """不确定时明说——不许假装确定。"""
        env = Environment(str(tmp_path / "env.yml"))
        env.link_name("3-2", "李雷", confidence=0.4)
        assert "推测" in env.subject_summary("3-2")["label"]

    def test_individual_baseline(self, tmp_path):
        """个体差异基线：相对自己的常态判异常。"""
        env = Environment(str(tmp_path / "env.yml"))
        # 某人平时就常低头（注意力 0.2）
        for _ in range(25):
            env.observe_posture("3-2", "head_down", attention=0.2, is_normal=True)
        # 今天还是低头 → 与平时无异
        assert abs(env.observe_posture("3-2", "head_down", attention=0.2)) < 1.0
        # 某人平时专注，突然趴桌 → 明显异常
        for _ in range(25):
            env.observe_posture("1-1", "head_up", attention=1.0, is_normal=True)
        dev = env.observe_posture("1-1", "lying", attention=0.0)
        assert dev < -1.5, f"突然趴桌应显著偏离常态，实际 {dev}"

    def test_chaos_samples_do_not_pollute_baseline(self, tmp_path):
        """混乱期的观测绝不进基线，否则打闹久了会被学成常态。"""
        env = Environment(str(tmp_path / "env.yml"))
        for _ in range(25):
            env.observe_posture("3-2", "head_up", attention=1.0, is_normal=True)
        before = env.subject("3-2")["attention_baseline"]["mean"]
        for _ in range(10):
            env.observe_posture("3-2", "lying", attention=0.0, is_normal=False)
        after = env.subject("3-2")["attention_baseline"]["mean"]
        assert after == before, "混乱期样本污染了基线"

    def test_scene_change_recorded(self, tmp_path):
        env = Environment(str(tmp_path / "env.yml"))
        # pending_hash 只由 observe_scene() 写入，而该方法早已无人调用，
        # 曾导致 confirm_scene_change 恒返回 None、场景变化一条都记不下。
        # 现在改为显式传入 scene_hash。
        rec = env.confirm_scene_change("左墙新贴了一张分组表",
                                       confidence=0.8, scene_hash="abc123")
        assert rec["description"] == "左墙新贴了一张分组表"
        assert env.recent_scene_changes()[0]["description"] == "左墙新贴了一张分组表"

    def test_aging_retires_stale_conclusions(self, tmp_path):
        """知识必须可老化，否则长期记忆会固化成偏见。"""
        env = Environment(str(tmp_path / "env.yml"))
        env.note_behavior("3-2", "数学课常低头", confidence=0.9)
        note = env.subject("3-2")["behavior_notes"][0]
        # 把最后观察时间改到 60 天前
        note["last_seen"] = "2026-01-01T08:00:00"
        import time as _t
        retired = env.age_conclusions(now=_t.time())
        assert retired == 1
        assert note["superseded_by"] == "aged_out"

    def test_supersede_keeps_history(self, tmp_path):
        """旧结论不删除，只标记 superseded——完整修正时间线是宝贵数据。"""
        env = Environment(str(tmp_path / "env.yml"))
        env.note_behavior("3-2", "常低头")
        env.supersede_note("3-2", "常低头", "已改善，连续一周专注", confidence=0.7)
        notes = env.subject("3-2")["behavior_notes"]
        old = [n for n in notes if n["note"] == "常低头"][0]
        assert old["superseded_by"] == "已改善，连续一周专注"
        assert any(n["note"] == "已改善，连续一周专注" for n in notes)

    def test_timetable_lookup(self, tmp_path):
        env = Environment(str(tmp_path / "env.yml"))
        env.set_timetable([
            {"weekday": 3, "start": "00:00", "end": "23:59",
             "course": "数学", "teacher": "王老师"},
        ])
        import time as _t
        ts = _t.strptime("2026-09-02 10:00", "%Y-%m-%d %H:%M")  # 周三
        lesson = env.current_lesson(ts)
        assert lesson is not None and lesson["course"] == "数学"

    def test_no_timetable_returns_none(self, tmp_path):
        """零标注时课程表为空，退化不影响功能。"""
        env = Environment(str(tmp_path / "env.yml"))
        assert env.current_lesson() is None

    def test_save_and_reload(self, tmp_path):
        p = str(tmp_path / "env.yml")
        env = Environment(p)
        env.subject("3-2")
        env.link_name("3-2", "李雷", confidence=0.9)
        env.note_behavior("3-2", "数学课持续书写")
        env.save()
        env2 = Environment(p)
        assert env2.subject("3-2")["name"] == "李雷"
        assert env2.subject("3-2")["behavior_notes"][0]["note"] == "数学课持续书写"

    def test_corrupted_file_falls_back(self, tmp_path):
        """环境文件损坏时回退默认结构，绝不因读档失败拖垮巡检。"""
        p = tmp_path / "env.yml"
        p.write_text("::: not valid yaml :::\n\t- broken", encoding="utf-8")
        env = Environment(str(p))
        assert env.data["version"] == 1


# ==================== 索引 ====================

class TestIndexer:
    def _build(self, tmp_path):
        log = ObservationLog(str(tmp_path))
        idx = LogIndexer()
        return log, idx

    def test_time_range_query(self, tmp_path):
        log, idx = self._build(tmp_path)
        log.frame(entities=[])
        for rec in log.iter_records():
            idx.index_record(rec)
        assert len(idx.time.range("2000-01-01T00:00:00", "2099-12-31T23:59:59")) == 1

    def test_space_query(self, tmp_path):
        log, idx = self._build(tmp_path)
        log.event(kind="gathering", region="3-2")
        log.event(kind="scuffle", region="1-1")
        for rec in log.iter_records():
            idx.index_record(rec)
        assert len(idx.space.query(["3-2"])) == 1
        assert idx.space.query(["3-2"])[0]["kind"] == "gathering"

    def test_semantic_query(self, tmp_path):
        log, idx = self._build(tmp_path)
        log.event(kind="scuffle", detail="后排学生打闹推搡")
        log.event(kind="gathering", detail="学生围在左墙看值日表")
        for rec in log.iter_records():
            idx.index_record(rec)
        hits = idx.sem.query("打闹")
        assert any("打闹" in (r.get("detail") or "") for r in hits)

    def test_combined_query(self, tmp_path):
        log, idx = self._build(tmp_path)
        log.event(kind="gathering", region="3-2", detail="聚集")
        for rec in log.iter_records():
            idx.index_record(rec)
        res = idx.query(start="2000-01-01T00:00:00", end="2099-12-31T23:59:59",
                        regions=["3-2"], level="event")
        assert len(res) == 1

    def test_rank_by_event(self, tmp_path):
        """「谁说话最多」这类排名的底层能力。"""
        log, idx = self._build(tmp_path)
        for _ in range(5):
            log.event(kind="gathering", involved=["3-2"])
        for _ in range(2):
            log.event(kind="gathering", involved=["1-1"])
        for rec in log.iter_records():
            idx.index_record(rec)
        rank = subject_event_rank(
            idx, "2000-01-01T00:00:00", "2099-12-31T23:59:59", kind="gathering")
        assert rank[0] == ("3-2", 5)
        assert rank[1] == ("1-1", 2)

    def test_rebuild_async(self, tmp_path):
        log, idx = self._build(tmp_path)
        log.frame(entities=[])
        log.event(kind="gathering", region="1-1")
        idx.rebuild_async(log)
        idx.wait_for_rebuild(timeout=10)
        assert idx.stats()["records"] == 2

    def test_duplicate_indexing_is_idempotent(self, tmp_path):
        log, idx = self._build(tmp_path)
        log.frame(entities=[])
        rec = list(log.iter_records())[0]
        assert idx.index_record(rec) is True
        assert idx.index_record(rec) is False
        assert idx.stats()["records"] == 1


# ==================== 事件生命周期 ====================

class TestEventTracker:
    def test_forming_then_ongoing(self):
        t = EventTracker()
        assert t.update("gathering", "1-1", True)["status"] == "forming"
        assert t.update("gathering", "1-1", True)["status"] == "ongoing"

    def test_ended_with_duration(self):
        """事件消失时记录完整生命周期——持续时长本身就是判据。"""
        import time as _t
        t = EventTracker()
        t.update("gathering", "1-1", True)
        _t.sleep(1.1)
        r = t.update("gathering", "1-1", False)
        assert r["status"] == "ended"
        assert r["event"]["duration_sec"] >= 1

    def test_absent_returns_none(self):
        t = EventTracker()
        assert t.update("gathering", "1-1", False)["status"] == "none"

    def test_peak_confidence_tracked(self):
        t = EventTracker()
        t.update("gathering", "1-1", True, confidence=0.6)
        t.update("gathering", "1-1", True, confidence=0.9)
        assert t.update("gathering", "1-1", False)["event"]["peak_confidence"] == 0.9


# ==================== 判定升级 ====================

class TestCategoryParsing:
    def test_normalize_aliases(self):
        assert normalize_category("gathering") == CATEGORY_GATHERING
        assert normalize_category("聚集围观") == CATEGORY_GATHERING
        assert normalize_category("打闹冲突") == CATEGORY_SCUFFLE
        assert normalize_category("正常上课") == CATEGORY_NORMAL_CLASS

    def test_unknown_defaults_to_other(self):
        assert normalize_category("完全没见过的类别") == "other"

    def test_observation_and_inference_separated(self):
        text = ('{"observation":"六名学生站在左墙前","category":"gathering",'
                '"description":"在看新贴的值日表","confidence":0.85,'
                '"facing_consistency":0.92,"abnormal":false}')
        r = parse_observation(text)
        assert r["observation"] == "六名学生站在左墙前"
        assert r["description"] == "在看新贴的值日表"
        assert r["abnormal"] is False

    def test_normal_category_overrides_abnormal_true(self):
        """类别判为正常时，不允许 abnormal 为真——消除自相矛盾的输出。"""
        r = parse_observation('{"observation":"学生都在座位上",'
                              '"category":"normal_class","abnormal":true}')
        assert r["abnormal"] is False

    def test_legacy_format_not_losing_description(self):
        """模型不按新格式回时，描述绝不能丢失（曾导致告警正文为空）。"""
        r = parse_observation('{"abnormal": true, "type": "多人离座走动", '
                              '"detail": "多名学生离开座位走动", "confidence": 0.7}')
        assert r["observation"] == "多名学生离开座位走动"
        assert r["description"] == "多名学生离开座位走动"
        assert r["abnormal"] is True

    def test_prompt_contains_observation_before_judgement(self):
        """提示词必须强制「先描述、再判断」。"""
        p = build_observe_prompt("观察这个教室")
        i_obs = p.find("observation")
        i_abn = p.find("abnormal")
        assert i_obs > 0 and i_abn > i_obs

    def test_prompt_injects_scene_change_context(self):
        """已确认的场景变化必须注入提示词，让模型理解聚集是响应性的。"""
        p = build_observe_prompt("观察", recent_changes="左墙新贴了一张分组表")
        assert "新贴" in p
        assert "响应" in p


class TestTruncationSafety:
    """截断安全：模型话没说完时，绝不能当成"发现异常"。

    这组用例对应一次真实事故：多帧输入让思考型模型的推理链暴涨，
    输出被 token 上限截断在半句话上，解析不到 JSON，
    于是落到"无法判断时保守判异常"的兜底逻辑——
    结果一间安静的教室凭空产生了告警。
    """

    def test_truncated_answer_is_unreliable_not_abnormal(self):
        """截断的回复必须标记为不可靠，且不得判异常。"""
        text = ('<think>让我分析这两帧的差异。首先第一帧有三排学生，'
                '第二帧看起来第二排最后一个位置有变化，我需要判断这是'
                '否属于离座走动。先看姿态，再数人数，然后考虑'
                'facing_consistency：如果大部分')
        r = parse_observation(text)
        assert r.get("unreliable") is True, "截断回复必须标记为不可靠"
        assert r["abnormal"] is False, "截断回复绝不能判异常"
        assert r["category"] is None

    def test_truncated_json_is_unreliable(self):
        """JSON 未闭合（括号不平衡）同样算截断。"""
        text = '{"observation":"后排有几名学生聚集在一起","category":"gathering","confidence":0.8'
        r = parse_observation(text)
        assert r.get("unreliable") is True

    def test_unclosed_answer_tag_is_unreliable(self):
        text = '<think>分析中…</think><answer>{"observation":"一切正常","category":"normal_class"'
        r = parse_observation(text)
        assert r.get("unreliable") is True

    def test_complete_reply_not_flagged(self):
        """正常完整的回复不能被误判为截断。"""
        text = ('{"observation":"学生均在座位上","category":"normal_class",'
                '"description":"正常上课","confidence":0.9,"abnormal":false}')
        r = parse_observation(text)
        assert r.get("unreliable") is False
        assert r["category"] == CATEGORY_NORMAL_CLASS

    def test_thinking_inside_field_is_stripped(self):
        """模型把思维链写进字段值时，必须清洗掉。

        日志里的 observation 是"纯观测事实"栏，
        塞满推理过程既违反观察与推断分离，也让存储白白膨胀。
        """
        text = ('{"observation":"<think>我需要先数人数，三排各九个，'
                '然后比较两帧的差异</think>学生均在座位上",'
                '"category":"normal_class","description":"正常上课",'
                '"confidence":0.9,"abnormal":false}')
        r = parse_observation(text)
        assert "<think>" not in r["observation"]
        assert "先数人数" not in r["observation"]
        assert r["observation"] == "学生均在座位上"


class TestDiffParsing:
    def test_changed(self):
        changed, changes, conf = parse_diff(
            '{"changed": true, "changes": ["左墙新增一张表格"], "confidence": 0.9}')
        assert changed is True
        assert changes == ["左墙新增一张表格"]

    def test_no_change(self):
        changed, changes, _ = parse_diff(
            '{"changed": false, "changes": [], "confidence": 0.9}')
        assert changed is False

    def test_plain_text_fallback(self):
        changed, _, _ = parse_diff("两张图无变化")
        assert changed is False


# ==================== 端到端：核心场景 ====================

def _make_monitor(tmp_path, script, cfg_over=None):
    """构造一个带假模型的 Monitor。"""
    root = str(tmp_path)
    notifier.init(os.path.join(root, "logs"))
    cfg = dict(config_mod.DEFAULT_CONFIG)
    cfg["api_key"] = "test-key"
    cfg["alert_image_dir"] = os.path.join(root, "alerts")
    if cfg_over:
        cfg.update(cfg_over)
    config_mod.save_config(cfg, os.path.join(root, "config.json"))

    class FakeClient:
        def __init__(self):
            self.n = 0
        def analyze(self, image, prompt, model=None):
            return self._reply()
        def analyze_multi(self, frames, prompt, model=None):
            return self._reply()
        def analyze_diff(self, reference, current, prompt, model=None):
            return self._reply()
        def _reply(self):
            i = self.n
            self.n += 1
            if isinstance(script, list):
                return script[min(i, len(script) - 1)]
            return script
        def close(self):
            pass

    monitor_mod.ZhipuVisionClient = lambda **kw: FakeClient()
    win = type("W", (), {"hwnd": 0, "title": "测试窗口"})()
    # root 必须显式指定：否则会写到真实项目目录，
    # 既污染仓库，又会让各用例读到彼此的残留数据。
    return monitor_mod.Monitor(cfg, win, verbose=False, alert_notify=False,
                               root=root)


class TestEndToEnd:
    def test_gathering_with_consistent_facing_not_alerted(self, tmp_path):
        """核心场景：新贴分组表导致聚集 + 朝向一致 → 不得误判为打闹。

        这是用户提出的真实痛点：「墙边粘贴了一个新的分组表，
        人都可能聚集，怎么保证不误判」。
        """
        script = ('{"observation":"六名学生站在左墙前朝同一方向",'
                  '"category":"gathering","description":"在看新贴的分组表",'
                  '"confidence":0.85,"facing_consistency":0.95,'
                  '"postures":[{"region":"2-1","posture":"standing","facing":"front"}],'
                  '"abnormal":false}')
        mon = _make_monitor(tmp_path, script)
        frames = [normal_class(i) for i in range(20)]
        orig = monitor_mod.window_capture.capture_window
        cur = {"i": 0}
        def cap(h):
            f = frames[min(cur["i"], len(frames) - 1)]
            cur["i"] += 1
            return f
        monitor_mod.window_capture.capture_window = cap
        try:
            for i in range(20):
                mon._tick(on_status=None, force_analyze=(i == 0))
            assert mon.stats["alerts"] == 0, \
                "朝向一致的响应性聚集不应告警"
        finally:
            mon.close()
            monitor_mod.window_capture.capture_window = orig

    def test_scuffle_with_chaotic_facing_is_alerted(self, tmp_path):
        """对照组：打闹 + 朝向混乱 → 必须告警。"""
        script = ('{"observation":"后排多名学生互相推搡，朝向混乱",'
                  '"category":"scuffle","description":"后排发生打闹",'
                  '"confidence":0.85,"facing_consistency":0.15,'
                  '"postures":[{"region":"4-5","posture":"standing","facing":"other"}],'
                  '"abnormal":true}')
        mon = _make_monitor(tmp_path, script)
        frames = [normal_class(i) for i in range(12)]
        orig = monitor_mod.window_capture.capture_window
        cur = {"i": 0}
        def cap(h):
            f = frames[min(cur["i"], len(frames) - 1)]
            cur["i"] += 1
            return f
        monitor_mod.window_capture.capture_window = cap
        try:
            for i in range(12):
                mon._tick(on_status=None, force_analyze=(i == 0))
            assert mon.stats["alerts"] >= 1, \
                "朝向混乱的打闹必须告警"
        finally:
            mon.close()
            monitor_mod.window_capture.capture_window = orig

    def test_memory_written_during_patrol(self, tmp_path):
        """巡检过程中必须持续沉淀记忆：日志 + 环境文件。"""
        script = ('{"observation":"学生均在座位上","category":"normal_class",'
                  '"description":"正常上课","confidence":0.9,'
                  '"facing_consistency":0.8,'
                  '"postures":[{"region":"3-2","posture":"head_up","facing":"front"}],'
                  '"abnormal":false}')
        mon = _make_monitor(tmp_path, script)
        frames = [normal_class(i) for i in range(8)]
        orig = monitor_mod.window_capture.capture_window
        cur = {"i": 0}
        def cap(h):
            f = frames[min(cur["i"], len(frames) - 1)]
            cur["i"] += 1
            return f
        monitor_mod.window_capture.capture_window = cap
        try:
            for i in range(8):
                mon._tick(on_status=None, force_analyze=(i == 0))
            assert mon.log.count(level="frame") >= 1, "未写入帧级日志"
            assert mon.env.subject("3-2") is not None, "未建立位置档案"
            assert mon.env.data["stats"]["frames"] >= 1
        finally:
            mon.close()
            monitor_mod.window_capture.capture_window = orig

    def test_alert_written_to_memory_with_evidence(self, tmp_path):
        """告警必须写入日志，且 observation 与 description 分开。"""
        script = ('{"observation":"后排学生互相推搡","category":"scuffle",'
                  '"description":"后排打闹，建议介入","confidence":0.9,'
                  '"facing_consistency":0.1,'
                  '"postures":[{"region":"4-5","posture":"standing","facing":"other"}],'
                  '"abnormal":true}')
        mon = _make_monitor(tmp_path, script)
        frames = [normal_class(i) for i in range(10)]
        orig = monitor_mod.window_capture.capture_window
        cur = {"i": 0}
        def cap(h):
            f = frames[min(cur["i"], len(frames) - 1)]
            cur["i"] += 1
            return f
        monitor_mod.window_capture.capture_window = cap
        try:
            for i in range(10):
                mon._tick(on_status=None, force_analyze=(i == 0))
            alerts = list(mon.log.iter_records(level="alert"))
            assert len(alerts) >= 1, "告警未写入日志"
            a = alerts[0]
            assert "推搡" in (a["observation"] or ""), "缺少原始观测"
            assert "介入" in (a["description"] or ""), "缺少解读"
            assert a["status"] == "pending"
        finally:
            mon.close()
            monitor_mod.window_capture.capture_window = orig

    def test_zero_annotation_patrol_works(self, tmp_path):
        """零标注巡检：不关联任何姓名，全流程照常。"""
        script = ('{"observation":"学生均在座位上","category":"normal_class",'
                  '"description":"正常","confidence":0.9,"abnormal":false}')
        mon = _make_monitor(tmp_path, script)
        frames = [normal_class(i) for i in range(6)]
        orig = monitor_mod.window_capture.capture_window
        cur = {"i": 0}
        def cap(h):
            f = frames[min(cur["i"], len(frames) - 1)]
            cur["i"] += 1
            return f
        monitor_mod.window_capture.capture_window = cap
        try:
            for i in range(6):
                mon._tick(on_status=None, force_analyze=(i == 0))
            s = mon.env.summary()
            assert s["subjects_named"] == 0, "零标注下不应有姓名"
        finally:
            mon.close()
            monitor_mod.window_capture.capture_window = orig


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
