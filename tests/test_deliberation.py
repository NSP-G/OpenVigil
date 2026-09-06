# -*- coding: utf-8 -*-
"""多轮复演与 MD 记忆系统测试。

存在意义：固化两条容易被后续改动破坏的原则。

原则一：**判断权在模型手里**。
曾经有一版实现建了张「类别 → 告不告警」的硬编码映射表，
模型说"聚集围观"就直接判定不告警。这是用静态规则覆盖模型对
当下具体画面的判断。正确做法是给模型更多角度和依据，让它自己决定。

原则二：**复演必须独立**。
各视角彼此看不到对方的结论，否则锚定效应会让复演退化成昂贵的复读。

运行：python -m pytest tests/test_deliberation.py -v
"""
import json
import os
import shutil
import sys
import tempfile

import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from app.deliberation import PERSPECTIVES, Deliberation, build_perspective_prompt
from app.md_memory import MEMORY_KINDS, MemoryStore


# ==================== 假客户端 ====================

class FakeClient:
    """按预设返回文本的假客户端，记录每次调用收到的提示词。"""

    def __init__(self, replies=None, chat_replies=None):
        self.replies = list(replies or [])
        self.chat_replies = list(chat_replies or [])
        self.prompts = []
        self.chat_prompts = []

    def analyze(self, image, prompt, model=None, **kw):
        self.prompts.append(prompt)
        if not self.replies:
            return "无回复"
        return self.replies.pop(0)

    def chat(self, prompt, model=None, **kw):
        self.chat_prompts.append(prompt)
        if not self.chat_replies:
            return '{"remember": false}'
        return self.chat_replies.pop(0)


def vote_json(verdict, confidence, reason="依据"):
    return json.dumps(
        {"verdict": verdict, "confidence": confidence, "reason": reason},
        ensure_ascii=False)


# ==================== 视角设计 ====================

class TestPerspectives:
    def test_four_perspectives(self):
        assert len(PERSPECTIVES) == 4

    def test_each_perspective_asks_one_thing(self):
        """每个视角只问一件事，问得杂模型会顾此失彼。"""
        for p in PERSPECTIVES:
            assert p["question"].strip()
            assert p["name"]
            assert p["brief"]

    def test_has_skeptic(self):
        """必须有一个专门唱反调的，否则复演会变成互相附和。"""
        keys = [p["key"] for p in PERSPECTIVES]
        assert "skeptic" in keys

    def test_context_perspective_uses_memory(self):
        """情境解读员是唯一读记忆的视角。

        其余视角刻意不给记忆——它们负责独立观察，
        记忆只用于解释，不用于影响事实判断。
        """
        with_memory = [p["key"] for p in PERSPECTIVES if p.get("use_memory")]
        assert with_memory == ["context"]


class TestIndependence:
    def test_prompts_do_not_contain_other_verdicts(self):
        """各视角提示词中不得出现其他视角的结论——独立复演的前提。"""
        client = FakeClient([vote_json("normal", 0.8)] * 4)
        d = Deliberation(client, "fake", memory_root=tempfile.mkdtemp())
        primary = {"observation": "学生均就座", "category_label": "正常上课"}
        d.deliberate(None, primary)

        assert len(client.prompts) == 4
        for prompt in client.prompts:
            # 各视角只应看到基础事实，不应看到别人的投票
            assert "abnormal" not in prompt.lower() or "verdict" in prompt
            assert "行为观察员认为" not in prompt
            assert "前一位" not in prompt

    def test_shared_facts_are_identical(self):
        """各视角共享同一批基础事实，保证信息对称。"""
        client = FakeClient([vote_json("normal", 0.8)] * 4)
        d = Deliberation(client, "fake", memory_root=tempfile.mkdtemp())
        primary = {"observation": "三排学生就座", "category_label": "正常上课"}
        d.deliberate(None, primary)
        # 每个提示词都应包含同一条原始观测
        for prompt in client.prompts:
            assert "三排学生就座" in prompt

    def test_no_conclusion_shared(self):
        """描述性推断（description）不共享，避免锚定。"""
        client = FakeClient([vote_json("normal", 0.8)] * 4)
        d = Deliberation(client, "fake", memory_root=tempfile.mkdtemp())
        primary = {"observation": "学生就座", "description": "这是一堂安静的自习课"}
        d.deliberate(None, primary)
        for prompt in client.prompts:
            assert "安静的自习课" not in prompt


class TestVoting:
    def _agg(self, votes, primary=None, abstained=None):
        return Deliberation._aggregate(
            votes, primary or {"abnormal": True, "confidence": 0.8},
            abstained=abstained)

    def test_all_abnormal(self):
        v = [{"name": "a", "abnormal": True, "confidence": 0.9, "reason": ""},
             {"name": "b", "abnormal": True, "confidence": 0.8, "reason": ""}]
        assert self._agg(v)["abnormal"] is True

    def test_all_normal(self):
        v = [{"name": "a", "abnormal": False, "confidence": 0.9, "reason": ""},
             {"name": "b", "abnormal": False, "confidence": 0.9, "reason": ""}]
        assert self._agg(v)["abnormal"] is False

    def test_tie_goes_to_normal(self):
        """平票判正常：误报的代价大于漏报。

        漏报只是老师晚点知道；误报会让老师不再信任这个系统。
        """
        v = [{"name": "a", "abnormal": True, "confidence": 0.8, "reason": ""},
             {"name": "b", "abnormal": False, "confidence": 0.8, "reason": ""}]
        r = self._agg(v)
        assert r["abnormal"] is False
        assert r["agreement"] == 0.0

    def test_majority_normal_overrides_initial_abnormal(self):
        """三比一的正常票应能推翻初判的异常——这正是复演的价值。"""
        v = [{"name": "a", "abnormal": False, "confidence": 0.9, "reason": ""},
             {"name": "b", "abnormal": False, "confidence": 0.9, "reason": ""},
             {"name": "c", "abnormal": False, "confidence": 0.9, "reason": ""},
             {"name": "d", "abnormal": True, "confidence": 0.6, "reason": ""}]
        r = self._agg(v)
        assert r["abnormal"] is False
        assert r["changed"] is True

    def test_no_votes_falls_back_to_initial(self):
        """全部弃权时沿用初判，不因复演失败而擅自改变结论。"""
        r = self._agg([], {"abnormal": True, "confidence": 0.7})
        assert r["abnormal"] is True
        assert "弃权" in (r.get("note") or "")

    def test_abstentions_weaken_confidence(self):
        """4 位弃权 3 位时，仅剩的 1 票不能伪装成"一致通过"。

        实测事故：4 个视角因输出截断弃权 3 个，剩下 1 票算出
        异常占比 1.0，看着是全票通过，实则一人独断。
        出席率必须参与置信度折算。
        """
        few = [{"name": "行为", "abnormal": True, "confidence": 0.9, "reason": ""}]
        r_few = self._agg(few, {"abnormal": True, "confidence": 0.8},
                          abstained=["情境", "时序", "质疑"])
        full = [{"name": n, "abnormal": True, "confidence": 0.9, "reason": ""}
                for n in ("a", "b", "c", "d")]
        r_full = self._agg(full, {"abnormal": True, "confidence": 0.8})

        assert r_few["attendance"] == 0.25
        assert r_full["attendance"] == 1.0
        # 同样的"全票异常"，出席不全时置信度必须显著更低
        assert r_few["confidence"] < r_full["confidence"] * 0.5

    def test_one_vote_does_not_trigger_alert(self):
        """一人独断的复演结果不应达到告警阈值。"""
        r = self._agg(
            [{"name": "行为", "abnormal": True, "confidence": 0.9, "reason": ""}],
            {"abnormal": True, "confidence": 0.8},
            abstained=["情境", "时序", "质疑"])
        assert r["abnormal"] is True          # 结论仍是异常
        assert r["confidence"] < 0.45         # 但证据不足，达不到阈值

    def test_abstained_names_reported(self):
        """弃权名单要对外可见，否则无法排查输出截断问题。"""
        r = self._agg(
            [{"name": "行为", "abnormal": True, "confidence": 0.9, "reason": ""}],
            {"abnormal": True, "confidence": 0.8},
            abstained=["情境", "时序"])
        assert "情境" in (r.get("note") or "")
        assert r["abstained"] == ["情境", "时序"]

    def test_low_confidence_vote_weighs_less(self):
        """把握低的票影响力小。"""
        v = [{"name": "a", "abnormal": True, "confidence": 0.3, "reason": ""},
             {"name": "b", "abnormal": False, "confidence": 0.95, "reason": ""}]
        r = self._agg(v)
        assert r["abnormal"] is False


class TestVoteParsing:
    def test_parse_valid(self):
        r = Deliberation._parse_vote(vote_json("abnormal", 0.7, "有人聚集"),
                                     PERSPECTIVES[0])
        assert r["abnormal"] is True
        assert abs(r["confidence"] - 0.7) < 1e-6

    def test_parse_strips_think_block(self):
        text = "<think>分析中…</think>" + vote_json("normal", 0.6)
        r = Deliberation._parse_vote(text, PERSPECTIVES[0])
        assert r["abnormal"] is False

    def test_parse_truncated_returns_none(self):
        """截断的投票视为弃权，不参与计票——弃权好过乱投。"""
        text = '<think>让我分析一下，首先看人数，然后'
        assert Deliberation._parse_vote(text, PERSPECTIVES[0]) is None

    def test_parse_bad_verdict_returns_none(self):
        text = json.dumps({"verdict": "maybe", "confidence": 0.5})
        assert Deliberation._parse_vote(text, PERSPECTIVES[0]) is None

    def test_confidence_clamped(self):
        r = Deliberation._parse_vote(vote_json("abnormal", 5.0), PERSPECTIVES[0])
        assert r["confidence"] == 1.0


class TestDeliberationFlow:
    def test_unparseable_votes_are_abstained(self):
        client = FakeClient(["我无法判断"] * 4)
        d = Deliberation(client, "fake", memory_root=tempfile.mkdtemp())
        r = d.deliberate(None, {"observation": "x", "abnormal": True})
        assert r["votes"] == []
        assert r["abnormal"] is True      # 沿用初判

    def test_single_perspective_failure_does_not_break(self):
        """某个视角调用失败不应中断整个复演。"""

        class Flaky(FakeClient):
            def analyze(self, image, prompt, model=None, **kw):
                self.prompts.append(prompt)
                if len(self.prompts) == 2:
                    raise RuntimeError("网络抖动")
                return vote_json("normal", 0.9)

        d = Deliberation(Flaky(), "fake", memory_root=tempfile.mkdtemp())
        r = d.deliberate(None, {"observation": "x", "abnormal": True})
        assert len(r["votes"]) == 3
        assert r["abnormal"] is False


# ==================== MD 记忆 ====================

class TestMemoryStore:
    def setup_method(self):
        self.tmp = tempfile.mkdtemp()

    def teardown_method(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _store(self, max_chars=4000):
        return MemoryStore(os.path.join(self.tmp, "mem"), max_chars=max_chars)

    def test_empty_memory_is_valid(self):
        """零标注基线：空记忆是合法状态，系统照常运行。"""
        m = self._store()
        assert m.read_all() == ""
        for kind in MEMORY_KINDS:
            assert m.read(kind) == ""

    def test_append_and_read(self):
        m = self._store()
        m.append("scene", "讲台在正前方")
        assert "讲台在正前方" in m.read("scene")

    def test_same_day_grouped(self):
        """同一天的条目归在一组，不新建重复标题。"""
        m = self._store()
        m.append("changes", "新贴分组表")
        m.append("changes", "又贴了一张")
        content = m.read("changes")
        assert content.count("###") == 1
        assert "新贴分组表" in content and "又贴了一张" in content

    def test_empty_text_ignored(self):
        m = self._store()
        assert m.append("scene", "   ") is False
        assert m.read("scene") == ""

    def test_needs_compaction(self):
        m = self._store(max_chars=50)
        m.append("scene", "一" * 60)
        assert m.needs_compaction("scene") is True

    def test_compaction_prompt_preserves_early_knowledge(self):
        """压缩=提炼，不是截断。截断等于失忆。"""
        m = self._store(max_chars=50)
        m.append("scene", "一" * 60)
        p = m.build_compaction_prompt("scene")
        assert "最早" in p or "基础认知" in p
        assert "一" * 60 in p

    def test_replace_rejects_empty(self):
        m = self._store()
        m.append("scene", "重要认知")
        ok, msg = m.replace("scene", "")
        assert ok is False
        assert "重要认知" in m.read("scene")   # 未被覆盖

    def test_replace_rejects_too_short(self):
        m = self._store()
        m.append("scene", "重要认知")
        ok, _ = m.replace("scene", "没了")
        assert ok is False
        assert "重要认知" in m.read("scene")

    def test_replace_strips_code_fence(self):
        m = self._store(max_chars=2000)
        ok, _ = m.replace(
            "scene",
            "```markdown\n本班常态为安静自习，学生按座位就坐，鲜少离座。\n```")
        assert ok is True
        assert "```" not in m.read("scene")

    def test_replace_accepts_valid(self):
        m = self._store(max_chars=2000)
        ok, _ = m.replace("scene", "本班常态为安静自习，学生按座位就坐，鲜少离座走动。")
        assert ok is True

    def test_stats(self):
        m = self._store(max_chars=100)
        m.append("scene", "x" * 40)
        st = m.stats()
        # 文件除正文外还含标题与日期行，故用下界断言
        assert st["scene"]["chars"] >= 40
        assert st["scene"]["needs_compaction"] is False
        assert st["scene"]["max"] == 100


class TestMemoryEvolution:
    def setup_method(self):
        self.tmp = tempfile.mkdtemp()

    def teardown_method(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_model_decides_to_remember(self):
        client = FakeClient(chat_replies=[
            json.dumps({"remember": True, "file": "scene",
                        "text": "本班常态为安静自习"}, ensure_ascii=False)])
        d = Deliberation(client, "fake", memory_root=os.path.join(self.tmp, "m"))
        result = {"votes": [{"name": "a", "abnormal": False,
                             "confidence": 0.9, "reason": "都在座位上"}],
                  "abnormal": False}
        assert d.evolve_memory(result) is True
        assert "安静自习" in d.memory.read("scene")

    def test_model_declines_to_remember(self):
        client = FakeClient(chat_replies=['{"remember": false}'])
        d = Deliberation(client, "fake", memory_root=os.path.join(self.tmp, "m"))
        result = {"votes": [{"name": "a", "abnormal": False,
                             "confidence": 0.9, "reason": "x"}],
                  "abnormal": False}
        assert d.evolve_memory(result) is False
        assert d.memory.read("scene") == ""

    def test_invalid_file_name_rejected(self):
        client = FakeClient(chat_replies=[
            json.dumps({"remember": True, "file": "etc/passwd",
                        "text": "恶意内容"}, ensure_ascii=False)])
        d = Deliberation(client, "fake", memory_root=os.path.join(self.tmp, "m"))
        result = {"votes": [{"name": "a", "abnormal": False,
                             "confidence": 0.9, "reason": "x"}],
                  "abnormal": False}
        assert d.evolve_memory(result) is False

    def test_no_votes_no_evolution(self):
        client = FakeClient()
        d = Deliberation(client, "fake", memory_root=os.path.join(self.tmp, "m"))
        assert d.evolve_memory({"votes": []}) is False

    def test_compact_when_overloaded(self):
        client = FakeClient(chat_replies=["本班常态：安静自习，学生按座位就坐，鲜少离座走动。"])
        d = Deliberation(client, "fake", memory_root=os.path.join(self.tmp, "m"),
                         max_chars=60)
        d.memory.append("scene", "一" * 100)
        done = d.compact_if_needed()
        assert "scene" in done
        assert d.memory.needs_compaction("scene") is False


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
