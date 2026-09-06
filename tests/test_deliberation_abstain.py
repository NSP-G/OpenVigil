# -*- coding: utf-8 -*-
"""复演弃权透明度测试。

存在意义：真实测试中出现过这样一幕——

    复演结论：正常（置信度 0.00）｜投票数 2｜一致度 1.00

4 位评委里只有 2 位投了票（其余解析失败而弃权），
报告却显示"一致度 1.00"，看上去像是全员一致通过。
把弃权藏起来，等于把一次残缺的复演包装成确凿结论。

本文件固化两条约束：
  1. 弃权必须对外可见，不能悄无声息地少几票
  2. 出席率不足时，一致度与置信度要相应打折，
     不能让"两人一致"冒充"全员一致"

运行：python -m pytest tests/test_deliberation_abstain.py -v
"""
import os
import sys

import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from app.deliberation import PERSPECTIVES, Deliberation


def vote(abnormal, confidence):
    return {"abnormal": abnormal, "confidence": confidence, "reason": "测试"}


PRIMARY = {"abnormal": True, "confidence": 0.8}

ALL_NAMES = [p["name"] for p in PERSPECTIVES]


class TestAbstainVisibility:
    def test_aggregate_records_abstained(self):
        r = Deliberation._aggregate(
            [vote(False, 0.9)], PRIMARY, abstained=["行为观察员"])
        assert r["abstained"] == ["行为观察员"]

    def test_aggregate_records_attendance(self):
        r = Deliberation._aggregate(
            [vote(False, 0.9), vote(False, 0.8)], PRIMARY,
            abstained=ALL_NAMES[2:])
        assert r["attendance"] == pytest.approx(0.5)

    def test_full_attendance(self):
        votes = [vote(False, 0.8)] * len(PERSPECTIVES)
        r = Deliberation._aggregate(votes, PRIMARY)
        assert r["attendance"] == pytest.approx(1.0)
        assert r["abstained"] == []


class TestAbstainWeakensAgreement:
    def test_half_absent_lowers_agreement(self):
        """同样的投票分布，出席率越低，一致度越不该虚高。"""
        full = Deliberation._aggregate([vote(False, 0.9)] * len(PERSPECTIVES),
                                       PRIMARY)
        half = Deliberation._aggregate([vote(False, 0.9)] * 2, PRIMARY,
                                       abstained=ALL_NAMES[2:])
        assert half["agreement"] < full["agreement"], (
            "出席率不足时一致度必须打折，否则『两人一致』会冒充『全员一致』")

    def test_agreement_scales_with_attendance(self):
        r = Deliberation._aggregate([vote(False, 0.9)] * 2, PRIMARY,
                                    abstained=ALL_NAMES[2:])
        # 原始一致度为 1.0，出席率 0.5 → 0.5
        assert r["agreement"] == pytest.approx(0.5, abs=0.01)

    def test_direction_preserved_despite_discount(self):
        """打折只削弱分量，不改变结论方向。"""
        r = Deliberation._aggregate([vote(False, 0.9)] * 2, PRIMARY,
                                    abstained=ALL_NAMES[2:])
        assert r["abnormal"] is False


class TestAllAbstain:
    def test_all_abstain_falls_back_to_primary(self):
        r = Deliberation._aggregate([], PRIMARY, abstained=ALL_NAMES)
        assert r["abnormal"] is True          # 沿用初判
        assert r["attendance"] == 0.0
        assert r["note"], "全员弃权必须留痕，不能静默沿用"

    def test_all_abstain_reports_who_abstained(self):
        r = Deliberation._aggregate([], PRIMARY, abstained=ALL_NAMES)
        assert r["abstained"] == ALL_NAMES


class TestAbstainReportedToUser:
    def test_deliberate_reports_abstentions(self):
        """弃权要通过 on_report 告知界面，不能只写日志。"""

        class FakeClient:
            def __init__(self):
                self.calls = 0

            def analyze(self, frame, prompt, model=None, **kw):
                self.calls += 1
                # 前两位正常作答，后两位返回无法解析的内容
                if self.calls <= 2:
                    return '{"verdict":"normal","confidence":0.9,"reason":"可被解释"}'
                return "抱歉，我无法判断"

        class FakeMemory:
            def read_all(self):
                return ""

        d = Deliberation(FakeClient(), "fake-model")
        d.memory = FakeMemory()
        reports = []
        r = d.deliberate(None, PRIMARY, on_report=lambda lvl, msg: reports.append(msg))

        joined = "\n".join(reports)
        assert "弃权" in joined, "弃权必须对外可见"
        assert r["abstained"], "结果中必须记录弃权名单"
        assert r["attendance"] == pytest.approx(0.5)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
