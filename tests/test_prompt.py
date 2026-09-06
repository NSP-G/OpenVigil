# -*- coding: utf-8 -*-
"""提示词构造回归测试。

存在意义：真实模型测试曾暴露一个严重问题——
提示词里同时存在两套 JSON 格式（旧的 abnormal/type/detail/evidence
与新的 observation/category/description），模型遵循了先出现的旧格式，
导致新字段全部丢失、类别退化为「其他」。

这组测试把「提示词只能有一套格式」这条约束固化下来，
防止以后有人往基础提示词里加回旧格式指令而无人察觉。

运行：python -m pytest tests/test_prompt.py -v
"""
import os
import sys

import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from app import config as config_mod
from app.zhipu_client import (
    CATEGORY_GATHERING,
    CATEGORY_NORMAL_CLASS,
    build_diff_prompt,
    build_observe_prompt,
    parse_observation,
)


class TestLegacySchemaStripped:
    def test_no_legacy_schema_line(self):
        """基础提示词里的旧格式声明必须被剔除。"""
        p = build_observe_prompt(config_mod.DEFAULT_PROMPT)
        assert "请只输出一个 JSON" not in p

    def test_no_evidence_field(self):
        """旧字段 evidence 不应再出现在提示词中。"""
        p = build_observe_prompt(config_mod.DEFAULT_PROMPT)
        assert '"evidence"' not in p

    def test_no_detail_field_instruction(self):
        """旧字段 detail 的填写说明不应残留。"""
        p = build_observe_prompt(config_mod.DEFAULT_PROMPT)
        assert "detail 请写" not in p
        assert "不超过 50 字" not in p

    def test_no_format_as_follows(self):
        p = build_observe_prompt(config_mod.DEFAULT_PROMPT)
        assert "格式如下" not in p

    def test_criteria_preserved(self):
        """剔除格式的同时，业务判定标准必须原样保留。

        这些规则（哪些情形算异常）是长期积累的领域知识，
        不能为了换格式就丢掉。
        """
        p = build_observe_prompt(config_mod.DEFAULT_PROMPT)
        assert "多人离开座位" in p
        assert "打闹" in p
        assert "三人以上" in p
        assert "不要因为看不清人脸" in p


class TestNewSchemaPresent:
    def test_required_fields(self):
        p = build_observe_prompt(config_mod.DEFAULT_PROMPT)
        for field in ("observation", "category", "description",
                      "confidence", "facing_consistency", "abnormal"):
            assert '"%s"' % field in p, f"新格式缺少字段 {field}"

    def test_all_categories_listed(self):
        """类别选项必须完整列出，模型才知道有哪些可选。"""
        p = build_observe_prompt(config_mod.DEFAULT_PROMPT)
        for cat in ("normal_class", "orderly", "gathering", "scuffle",
                    "leave_seat", "head_down", "attention_drop", "empty",
                    "other"):
            assert cat in p, f"类别 {cat} 未出现在提示词中"

    def test_observation_listed_before_abnormal(self):
        """先描述、再判断——observation 必须出现在 abnormal 之前。"""
        p = build_observe_prompt(config_mod.DEFAULT_PROMPT)
        assert p.index('"observation"') < p.index('"abnormal"')


class TestMultiFrameGuidance:
    def test_multi_frame_adds_guidance(self):
        p = build_observe_prompt(config_mod.DEFAULT_PROMPT, multi_frame=True)
        assert "多帧" in p
        assert "聚" in p and "散开" in p

    def test_multi_frame_warns_against_overreading(self):
        """必须明确「小幅位置变化不算离座」。

        实测教训：没有这句时，模型把安静自习里学生轻微的位置变化
        判成了「离座走动」并告警。
        """
        p = build_observe_prompt(config_mod.DEFAULT_PROMPT, multi_frame=True)
        assert "离座" in p
        assert "正常" in p

    def test_single_frame_omits_guidance(self):
        """单帧时不应出现多帧说明，避免干扰模型。"""
        p = build_observe_prompt(config_mod.DEFAULT_PROMPT, multi_frame=False)
        assert "你会看到多帧画面" not in p


class TestContextInjection:
    def test_scene_change_context(self):
        p = build_observe_prompt(config_mod.DEFAULT_PROMPT,
                                 recent_changes="左墙新贴了一张分组表")
        assert "新贴" in p
        assert "响应" in p   # 提示模型聚集可能是响应性行为

    def test_scene_normal_context(self):
        p = build_observe_prompt(config_mod.DEFAULT_PROMPT,
                                 scene_context="本班常态：学生按座位就坐")
        assert "常态" in p

    def test_recheck_instruction(self):
        p = build_observe_prompt(config_mod.DEFAULT_PROMPT, is_recheck=True)
        assert "独立复核" in p


class TestRealModelReplyRegression:
    """把真实模型回复的形态固化为回归用例。"""

    def test_real_reply_with_think_and_answer(self):
        """真实回复带思维链，思维链里的词不得被当成结论。"""
        text = (
            "<think>判断过程中枚举了打闹、聚集、离座等可能……"
            "但仔细看学生都在座位上。</think>"
            "<answer>{\"observation\":\"学生均在座位上\","
            "\"category\":\"normal_class\",\"description\":\"正常上课\","
            "\"confidence\":0.9,\"abnormal\":false}</answer>"
        )
        r = parse_observation(text)
        assert r["category"] == CATEGORY_NORMAL_CLASS
        assert r["abnormal"] is False

    def test_real_legacy_reply_keeps_description(self):
        """真实回复若仍用旧格式，描述不得丢失。"""
        text = ('{"abnormal": true, "type": "异常聚集", '
                '"detail": "教室中间多人异常聚集", "confidence": 0.9}')
        r = parse_observation(text)
        assert r["observation"] == "教室中间多人异常聚集"
        assert r["description"] == "教室中间多人异常聚集"
        assert r["abnormal"] is True

    def test_truncated_think_block(self):
        """思维链未闭合（输出被截断）时不得崩溃。"""
        text = ('<think>分析中……枚举了打闹、聚集'
                '{"observation":"x","category":"gathering","abnormal":false}')
        r = parse_observation(text)
        assert r["abnormal"] is False

    def test_diff_prompt_mentions_no_change_option(self):
        p = build_diff_prompt()
        assert "无变化" in p
        assert "changed" in p


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
