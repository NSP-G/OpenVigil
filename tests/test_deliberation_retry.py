# -*- coding: utf-8 -*-
"""复演截断重试测试。

存在意义：读记忆的视角最容易被截断，而它偏偏最关键。

真实测试中出现过这样一幕——情境解读员弃权，而它是**唯一读取记忆**的视角，
掌握着"墙上刚贴了新表"这类解释性证据。它一弃权，
剩下三位只能就画面本身投票，于是把"围观新通知"判成了异常。

根因：该视角提示词最长（多了记忆段），推理链也最长，
在相同 token 预算下最容易被截断。

这组测试固化两条约束：
  1. 读记忆的视角必须有更大的输出预算
  2. 解析失败要先重试（用精简提示要求跳过推理），重试不成才弃权

运行：python -m pytest tests/test_deliberation_retry.py -v
"""
import os
import sys

import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from app import deliberation as delib_mod
from app.deliberation import (PERSPECTIVES, Deliberation,
                              _VOTE_MAX_TOKENS,
                              _VOTE_MAX_TOKENS_WITH_MEMORY,
                              _VOTE_RETRY_TOKENS)


class RecordingClient:
    """记录每次调用的预算与提示词，可按脚本返回内容。"""

    def __init__(self, reply_fn):
        self.reply_fn = reply_fn
        self.calls = []          # [(persp_key, max_tokens, prompt)]

    def analyze(self, frame, prompt, model=None, max_tokens=None, **kw):
        # 从提示词里反推是哪个视角
        key = None
        for p in PERSPECTIVES:
            if p["brief"] in prompt:
                key = p["key"]
                break
        self.calls.append((key, max_tokens, prompt))
        return self.reply_fn(len(self.calls), key, prompt, max_tokens)


def _delib(reply_fn, memory_text=""):
    d = Deliberation(RecordingClient(reply_fn), "fake-model")
    d.memory.read_all = lambda: memory_text
    return d


class TestPerspectiveBudgets:
    def test_memory_perspective_gets_more_budget(self):
        """读记忆的视角提示词最长，必须给更大预算。"""
        assert _VOTE_MAX_TOKENS_WITH_MEMORY > _VOTE_MAX_TOKENS

    def test_budget_actually_applied(self):
        """预算要真的传下去，不能只是定义了个常量。"""
        seen = {}

        def reply(n, key, prompt, mt):
            seen[key] = mt
            return '{"verdict":"normal","confidence":0.8,"reason":"ok"}'

        d = _delib(reply, memory_text="左墙新贴分组表")
        d.deliberate(None, {"observation": "x", "abnormal": True})

        assert seen.get("context") == _VOTE_MAX_TOKENS_WITH_MEMORY, (
            "情境解读员未拿到读记忆专用预算")
        assert seen.get("behavior") == _VOTE_MAX_TOKENS

    def test_only_context_reads_memory(self):
        """只有情境解读员拿到记忆，其余视角保持独立观察。"""
        seen = {}

        def reply(n, key, prompt, mt):
            seen[key] = prompt
            return '{"verdict":"normal","confidence":0.8,"reason":"ok"}'

        d = _delib(reply, memory_text="左墙新贴了分组表")
        d.deliberate(None, {"observation": "x", "abnormal": True})

        assert "左墙新贴了分组表" in seen["context"]
        assert "左墙新贴了分组表" not in seen["behavior"]
        assert "左墙新贴了分组表" not in seen["temporal"]
        assert "左墙新贴了分组表" not in seen["skeptic"]


class TestTruncationRetry:
    def test_retry_on_unparseable_output(self):
        """首次截断后应重试，而不是直接弃权。"""

        def reply(n, key, prompt, mt):
            if "【重要】" in prompt:
                return '{"verdict":"normal","confidence":0.9,"reason":"重试成功"}'
            return "<think>让我仔细分析这个场景，首先"

        d = _delib(reply)
        r = d.deliberate(None, {"observation": "x", "abnormal": True})

        assert r["abstained"] == [], "有重试机会就不该弃权"
        assert len(r["votes"]) == len(PERSPECTIVES)

    def test_retry_prompt_asks_to_skip_reasoning(self):
        """重试提示必须明确要求跳过推理，否则会再次截断。"""

        def reply(n, key, prompt, mt):
            return '{"verdict":"normal","confidence":0.8,"reason":"ok"}'

        d = _delib(reply)
        d.deliberate(None, {"observation": "x", "abnormal": True})
        # 强制触发一次重试路径
        prompts = [p for _, _, p in d.client.calls]
        # 首次调用不应带重试标记
        assert not any("【重要】" in p for p in prompts[:len(PERSPECTIVES)])

    def test_retry_uses_smaller_budget(self):
        """重试只要输出 JSON，不需要推理空间。"""
        budgets = []

        def reply(n, key, prompt, mt):
            budgets.append(mt)
            if "【重要】" in prompt:
                return '{"verdict":"normal","confidence":0.8,"reason":"ok"}'
            return "<think>被截断"

        d = _delib(reply)
        d.deliberate(None, {"observation": "x", "abnormal": True})
        assert _VOTE_RETRY_TOKENS in budgets

    def test_abstain_after_retry_fails(self):
        """重试也失败才真正弃权。"""

        def reply(n, key, prompt, mt):
            return "始终无法解析的内容"

        d = _delib(reply)
        r = d.deliberate(None, {"observation": "x", "abnormal": True})
        assert len(r["abstained"]) == len(PERSPECTIVES)
        assert r["votes"] == []


class TestContextPerspectiveCritical:
    """情境解读员掌握解释性证据，它弃权会显著削弱复演。"""

    def test_context_abstention_lowers_confidence(self):
        """情境解读员缺席时，结论不该像全员到齐那样有分量。"""

        def reply(n, key, prompt, mt):
            if key == "context":
                return "<think>被截断"      # 首次与重试都失败
            return '{"verdict":"abnormal","confidence":0.9,"reason":"看到聚集"}'

        d = _delib(reply, memory_text="左墙新贴分组表")
        r = d.deliberate(None, {"observation": "x", "abnormal": True})

        assert "情境解读员" in r["abstained"]
        # 缺席一人，置信度必须打折
        assert r["attendance"] < 1.0
        assert r["confidence"] < 0.9

    def test_single_perspective_cannot_dictate(self):
        """一个人不能压过其余三人——复演是投票，不是独裁。

        哪怕情境解读员把握再高，只要另外三人都判异常，
        结论仍是异常。若允许单视角翻盘，等于把复演退化成
        "某一个视角说了算"，各视角独立观察就失去意义了。
        """

        def reply(n, key, prompt, mt):
            if key == "context":
                return ('{"verdict":"normal","confidence":0.95,'
                        '"reason":"左墙新贴分组表，围观属正常响应"}')
            return '{"verdict":"abnormal","confidence":0.7,"reason":"看到聚集"}'

        d = _delib(reply, memory_text="左墙新贴分组表")
        r = d.deliberate(None, {"observation": "x", "abnormal": True})

        assert len(r["votes"]) == len(PERSPECTIVES)
        assert r["abnormal"] is True

    def test_context_breaks_the_tie(self):
        """真实场景：行为观察员看到聚集，情境解读员与质疑员认为可解释。

        2:2 僵持时，情境解读员因掌握记忆而把握更高，
        其权重应能把天平推向"不打扰"。这正是复演的价值所在——
        不是让它一个人说了算，而是让**有依据的那一票更重**。
        """

        def reply(n, key, prompt, mt):
            if key == "context":
                return ('{"verdict":"normal","confidence":0.95,'
                        '"reason":"左墙新贴分组表，围观属正常响应"}')
            if key == "skeptic":
                return '{"verdict":"normal","confidence":0.60,"reason":"可能是正常活动"}'
            return '{"verdict":"abnormal","confidence":0.70,"reason":"看到聚集"}'

        d = _delib(reply, memory_text="左墙新贴分组表")
        r = d.deliberate(None, {"observation": "x", "abnormal": True})

        assert len(r["votes"]) == len(PERSPECTIVES)
        assert r["abnormal"] is False
        assert r["changed"] is True


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
