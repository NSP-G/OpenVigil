# -*- coding: utf-8 -*-
"""架构约束守卫：判断权必须在模型手里，不在基础系统里。

==========================================================================
这个文件为什么存在
==========================================================================

曾经有一版实现，在 Monitor 里建了一张硬编码的映射表：

    _ALERT_POLICY = {
        "scuffle":     "alert",      # 打闹冲突 → 直接告警
        "gathering":   "notice",     # 聚集围观 → 只记录不告警
        "leave_seat":  "sustained",  # 离座走动 → 持续够了才告警
        ...
    }

看着合理，实际上犯了一个根本性错误：**把判断权从模型手里抢回了代码里**。

模型看到的是真实画面——它知道那群人是围过去看新贴的通知，
还是在互相推搡。基础系统看到的只是模型输出的一个字符串。
用一张静态表去覆盖模型对当下具体画面的判断，
本质上是拿二手信息压一手信息。

正确做法不是替模型下结论，而是给它更多角度和依据：
多轮复演（同一个模型戴不同帽子独立看几遍再投票）、
外部记忆（这个教室的历史上下文）、
以及让它自己质疑自己。

判断权始终在 AI 手里。基础系统只负责**安排它怎么看**。

==========================================================================
守卫什么
==========================================================================

这类"顺手加个规则表"的改动极易复活，因为它看起来又快又有效。
本文件用源码级断言把它钉死：一旦有人重新引入类别→处置的硬编码映射，
测试立即失败，并说明为什么不能这么做。

运行：python -m pytest tests/test_architecture_guard.py -v
"""
import ast
import os

import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MONITOR_PATH = os.path.join(PROJECT_ROOT, "app", "monitor.py")

# 这些标识符一旦出现，说明硬编码裁决层又回来了
FORBIDDEN_NAMES = (
    "_ALERT_POLICY",
    "_resolve_alert_policy",
    "_CATEGORY_ACTION",
    "_DISPATCH_TABLE",
)

# 类别常量名。它们出现在提示词构造、日志统计里是正常的，
# 但出现在「决定要不要告警」的分支里就不正常了。
CATEGORY_CONST_NAMES = (
    "CATEGORY_SCUFFLE", "CATEGORY_GATHERING", "CATEGORY_LEAVE_SEAT",
    "CATEGORY_HEAD_DOWN", "CATEGORY_ATTENTION_DROP", "CATEGORY_EMPTY",
    "CATEGORY_NORMAL_CLASS", "CATEGORY_ORDERLY",
)


def _source():
    with open(MONITOR_PATH, encoding="utf-8") as f:
        return f.read()


def _tree():
    return ast.parse(_source())


class TestNoHardcodedVerdict:
    def test_forbidden_identifiers_absent(self):
        """硬编码裁决层的标志性标识符不得出现。"""
        src = _source()
        for name in FORBIDDEN_NAMES:
            assert name not in src, (
                f"检测到 `{name}`——硬编码裁决层已被明确否决。\n"
                "判断权属于模型：模型看的是真实画面，基础系统只看得见它"
                "输出的字符串。要用静态规则覆盖模型判断，等于拿二手信息"
                "压一手信息。\n"
                "如需降低误判，请走多轮复演（app/deliberation.py）或"
                "给模型更多上下文，而不是替它下结论。"
            )

    def test_no_category_to_boolean_mapping(self):
        """不得存在「类别 → True/False 是否告警」的字典字面量。"""
        for node in ast.walk(_tree()):
            if not isinstance(node, ast.Dict):
                continue
            keys, values = node.keys, node.values
            if not keys or len(keys) != len(values):
                continue
            # 键是类别常量或类别字符串
            key_is_category = False
            for k in keys:
                if isinstance(k, ast.Name) and k.id in CATEGORY_CONST_NAMES:
                    key_is_category = True
                elif isinstance(k, ast.Constant) and isinstance(k.value, str):
                    if k.value in ("scuffle", "gathering", "leave_seat",
                                   "head_down", "attention_drop", "empty",
                                   "normal_class", "orderly"):
                        key_is_category = True
            if not key_is_category:
                continue
            # 值是常量（字符串或布尔）→ 就是一张静态映射表
            vals_const = all(
                isinstance(v, ast.Constant) for v in values)
            if vals_const:
                pytest.fail(
                    "发现「类别 → 固定处置」的静态映射表。\n"
                    "该设计已被否决：它用基础系统的规则覆盖了模型对当下画面的判断。\n"
                    "正确的降误判手段是多轮复演 + 外部记忆，见 app/deliberation.py。"
                )

    def test_deliberation_module_exists(self):
        """替代方案必须存在——否则守卫就只是拆了桥没修路。"""
        path = os.path.join(PROJECT_ROOT, "app", "deliberation.py")
        assert os.path.exists(path), "多轮复演模块缺失"
        path2 = os.path.join(PROJECT_ROOT, "app", "md_memory.py")
        assert os.path.exists(path2), "外部记忆模块缺失"


class TestDeliberationWiredIn:
    def test_monitor_uses_deliberation(self):
        src = _source()
        assert "deliberation" in src.lower(), "Monitor 未接入多轮复演"

    def test_deliberation_only_when_initially_abnormal(self):
        """复演只在初判异常时启动——成本分级，平时不开庭。

        用 AST 做语义检查而非字符串索引：后者会因换行、括号
        等纯格式调整而误报，守卫本身反倒成了改代码的绊脚石。
        """
        tree = ast.parse(_source())
        found = False
        for node in ast.walk(tree):
            if not isinstance(node, ast.If):
                continue
            if "self._run_deliberation(" not in ast.unparse(node.body):
                continue
            cond = ast.unparse(node.test)
            assert "final_abnormal" in cond, (
                "复演调用不在初判异常的分支内：%s" % cond)
            found = True
        assert found, "未找到复演调用点"


class TestReportingTransparency:
    """不告警时必须说明原因，不能静默丢弃。"""

    def test_deliberation_reports_votes(self):
        """复演过程要对外可见，老师才知道系统不是漏了。"""
        path = os.path.join(PROJECT_ROOT, "app", "deliberation.py")
        with open(path, encoding="utf-8") as f:
            src = f.read()
        assert "on_report" in src


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
