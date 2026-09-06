# -*- coding: utf-8 -*-
"""架构级回归测试：守住那些「代码在跑、功能已死」的缺陷。

==========================================================================
这份测试为什么单独成篇
==========================================================================

本轮全量代码审查发现了一批共性缺陷：函数还在、注释还在、
测试也可能还绿着，但**功能从未真正生效**。典型的有：

  - confirm_scene_change 依赖的 pending_hash 只由 observe_scene 写入，
    而后者无人调用 → 场景变化一条都没记进环境文件
  - EventTracker 的 present 实参恒为真 → 事件永不结束，
    生命周期追踪与其下游日志全部空转
  - age_conclusions 只有测试在调 → "防止记忆固化成偏见"从未运行
  - purge_expired 同样无人调用 → 90 天滚动保留这条合规底线形同虚设

这类缺陷的共同点是：**局部看代码毫无异样**，只有跨文件追踪调用关系
才能发现。因此这里全部用「调用链/数据流」级别的断言来守住，
一旦有人再次切断链路，测试立刻失败。

运行：python -m pytest tests/test_regression_wiring.py -v
"""
import ast
import inspect
import os

import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP_DIR = os.path.join(PROJECT_ROOT, "app")
MONITOR_PATH = os.path.join(APP_DIR, "monitor.py")


def _read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def _code_only(src):
    """剥掉注释与文档字符串，只留真正的代码行。

    源码里大量注释会解释"为什么不用 fsync""pending_hash 的历史教训"等，
    若直接对整段文本做子串断言，注释就会把测试自己绊倒。
    """
    out = []
    for line in src.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        out.append(line)
    return "\n".join(out)


MONITOR_SRC = _read(MONITOR_PATH)


def _calls_in_monitor(name):
    """Monitor 源码中是否调用了某方法（排除注释与定义行）。"""
    for line in MONITOR_SRC.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        if stripped.startswith("def " + name):
            continue
        if name + "(" in stripped:
            return True
    return False


class TestSceneChangeChain:
    """场景变化：检测器 → 模型描述 → 写入环境文件，整条链路必须闭合。"""

    def test_confirm_scene_change_receives_hash(self):
        """confirm_scene_change 必须拿到场景指纹。

        历史缺陷：它从 scene["pending_hash"] 取值，而该字段只由
        observe_scene() 写入——后者早已无人调用（Monitor 改用
        SceneChangeDetector）。结果本方法恒返回 None，
        场景变化记录一条都写不进环境文件。
        """
        src = _code_only(inspect.getsource(
            __import__("app.environment", fromlist=["x"]).Environment
            .confirm_scene_change))
        assert "scene_hash" in src, "confirm_scene_change 必须支持显式传入指纹"
        # pending_hash 不该再作为数据来源（注释里提及历史原因是可以的）
        assert "new_hash = scene.pop" not in src, (
            "不应再从永不存在的 pending_hash 取值")

    def test_monitor_passes_hash(self):
        """Monitor 调用时必须真的把指纹传进去。"""
        idx = MONITOR_SRC.index("confirm_scene_change(")
        snippet = MONITOR_SRC[idx:idx + 300]
        assert "scene_hash" in snippet, "Monitor 未传入 scene_hash"

    def test_observe_scene_not_a_dead_dependency(self):
        """observe_scene 若仍被当作数据源，就是回归。
        它自己无人调用，但只要有人再从中取值就会重蹈覆辙。"""
        idx = MONITOR_SRC.find("observe_scene")
        assert idx == -1, "Monitor 不应再依赖 observe_scene（它无人调用）"


class TestEventLifecycle:
    """事件必须能开始，也必须能结束。"""

    def test_present_is_not_always_true(self):
        """present 实参不得恒为真，否则事件永不结束。

        历史缺陷：present=verdict or bool(category)，
        而该分支下 category 必然非空 → 恒为 True。
        """
        idx = MONITOR_SRC.index("self.tracker.update(")
        snippet = MONITOR_SRC[idx:idx + 300]
        assert "bool(category)" not in snippet, (
            "present 不得用 `or bool(category)`——category 在此恒非空，"
            "会导致 present 恒真、事件永不结束")

    def test_tracker_actually_ends(self):
        from app.memory import EventTracker
        t = EventTracker()
        for _ in range(3):
            t.update(kind="gathering", region="3-2", present=True)
        ended = t.update(kind="gathering", region="3-2", present=False)
        assert ended["status"] == "ended"
        assert ended["event"] is not None

    def test_active_events_bounded(self):
        """region 写法不稳定时，活跃事件不得无限堆积。"""
        from app.memory import EventTracker
        t = EventTracker(max_active=5)
        for i in range(50):
            t.update(kind="gathering", region="r%d" % i, present=True)
        assert len(t.active_events()) <= 5, "活跃事件必须有上限"


class TestMaintenanceActuallyRuns:
    """维护类操作必须挂在生产代码上，不能只有测试在调。"""

    def test_age_conclusions_is_wired(self):
        """知识老化必须被生产代码调用。

        它只在测试里被调用过——于是"防止长期记忆固化成偏见"
        只是注释里的承诺，系统跑得越久偏见越深。
        """
        assert _calls_in_monitor("age_conclusions"), (
            "Monitor 未调用 env.age_conclusions()，知识老化从未运行")

    def test_log_purge_is_wired(self):
        """过期日志清理必须被生产代码调用。

        对着未成年人建长期画像，90 天滚动保留是合规底线。
        若无人调用，日志只增不减，这条底线等于没写。
        """
        assert _calls_in_monitor("purge_if_due") or \
            _calls_in_monitor("purge_expired"), "过期日志清理从未被调用"


class TestLocalFallbackNotOverruled:
    """本地兜底的前提是「模型判正常」，不能再让模型把它否决掉。"""

    def test_deliberation_skips_local_fallback(self):
        idx = MONITOR_SRC.index("self._run_deliberation(")
        # 回溯到最近的 if 条件
        head = MONITOR_SRC.rindex("if (", 0, idx)
        cond = MONITOR_SRC[head:idx]
        assert "local_fallback" in cond, (
            "复演必须跳过本地兜底。兜底的前提就是模型判正常，"
            "再请模型投票等于让漏判者否决最后一道防线。")


class TestNoMonkeyPatchingSharedState:
    """自检不得通过猴子补丁修改全局模块状态。"""

    def test_selftest_does_not_patch_window_capture(self):
        src = _read(os.path.join(APP_DIR, "selftest.py"))
        assert "window_capture.capture_window =" not in src, (
            "自检跑在后台线程，猴子补丁 window_capture 模块会连"
            "正在进行的真实巡检一起污染——巡检线程会抓到合成画面。")

    def test_selftest_uses_instance_injection(self):
        src = _read(os.path.join(APP_DIR, "selftest.py"))
        assert "_capture_fn" in src, "应改用实例级注入"

    def test_monitor_supports_capture_injection(self):
        assert "self._capture_fn" in MONITOR_SRC
        assert "self._capture_fn or window_capture.capture_window" in MONITOR_SRC


class TestSelftestIsolation:
    """自检产生的演练数据不得混进用户真实记录。"""

    def test_selftest_passes_isolated_root(self):
        src = _read(os.path.join(APP_DIR, "selftest.py"))
        assert "root=tmpdir" in src, (
            "Monitor 默认把 memory/ 与 environment.yml 建在项目根下。"
            "不隔离的话，自检的合成画面观测会写进用户真实的日志与模型记忆。")

    def test_selftest_shuts_down(self):
        src = _read(os.path.join(APP_DIR, "selftest.py"))
        assert "shutdown()" in src, (
            "只 close() 不停后台索引线程，每次自检都留一个空转线程")


class TestResourceRelease:
    """短生命周期实例必须能干净收尾。"""

    def test_monitor_has_shutdown(self):
        assert "def shutdown(self)" in MONITOR_SRC

    def test_gui_test_once_calls_shutdown(self):
        src = _read(os.path.join(PROJECT_ROOT, "gui", "main_gui.py"))
        assert "monitor.shutdown()" in src

    def test_cli_once_releases(self):
        src = _read(os.path.join(PROJECT_ROOT, "main.py"))
        assert "monitor.shutdown()" in src
        # 必须在 finally 或 try 之后，不能是裸 return
        assert "finally:" in src

    def test_indexer_stop_exists(self):
        from app.indexer import LogIndexer
        assert hasattr(LogIndexer, "stop_async")


class TestWindowMinimizedHandling:
    """窗口最小化时不应每 5 秒刷一条错误。"""

    def test_monitor_handles_minimized(self):
        assert "WindowMinimizedError" in MONITOR_SRC, (
            "最小化异常此前落到通用 except，导致持续报错却永不停止")
        idx = MONITOR_SRC.index("except WindowMinimizedError")
        assert "continue" in MONITOR_SRC[idx:idx + 1200]

    def test_poll_interval_configurable(self):
        assert "minimized_poll_interval" in MONITOR_SRC


class TestImageIntegrity:
    """抓图不能有隐蔽的画面损坏。"""

    def test_bmp_stride_aligned(self):
        """BMP 每行必须按 4 字节对齐。

        宽度不是 4 的倍数时每行末尾有 padding，
        若按 width*4 计算 stride，从第二行起逐行错位，
        画面呈规律斜向撕裂——图"能看"，但模型看到的是错乱内容。
        """
        src = _code_only(_read(os.path.join(APP_DIR, "window_capture.py")))
        assert "stride" in src, "必须显式计算 stride"
        assert "((bw * 32 + 31) // 32) * 4" in src or "// 32) * 4" in src


class TestConfigIntegrity:
    """配置项不能只有代码在用、配置表里却没有。"""

    def test_leave_seat_sustain_in_defaults(self):
        from app.config import DEFAULT_CONFIG, _NUMERIC_FIELDS
        assert "leave_seat_sustain" in DEFAULT_CONFIG, (
            "Monitor 在用这个配置，但 DEFAULT_CONFIG 里没有它，"
            "导致 load_config 遍历时直接忽略——写在 config.json 里也不生效")
        assert "leave_seat_sustain" in _NUMERIC_FIELDS

    def test_bool_fields_have_coercion(self):
        from app.config import _BOOL_FIELDS, _coerce_bool
        for field in ("multi_frame_confirm", "deliberation_enabled",
                      "local_guard_enabled", "keep_frames"):
            assert field in _BOOL_FIELDS
        assert _coerce_bool("true") is True
        assert _coerce_bool("否") is False
        assert _coerce_bool("not-a-bool") is None

    def test_path_fields_reject_non_string(self):
        """路径类配置写成数字会让 os.path.join 直接抛 TypeError。"""
        from app.config import _PATH_FIELDS
        assert "memory_root" in _PATH_FIELDS
        import json
        import tempfile
        from app import config as cfgmod
        with tempfile.NamedTemporaryFile("w", suffix=".json",
                                         delete=False) as f:
            json.dump({"memory_root": 12345}, f)
            path = f.name
        cfg, errors = cfgmod.load_config(path)
        os.unlink(path)
        assert isinstance(cfg["memory_root"], str), (
            "非字符串路径必须被拦下并回退默认值")
        assert any("memory_root" in e for e in errors)


class TestMemorySystemIntegrity:
    def test_no_fsync_per_write(self):
        """每帧 fsync 会把巡检循环拖慢，日志不是金融流水。"""
        src = _code_only(_read(os.path.join(APP_DIR, "memory.py")))
        assert "fsync" not in src, "不应逐条 fsync"

    def test_log_ids_unique_across_restart(self):
        """序号重启归零后同日会重号，ID 是索引与纠正引用的主键。"""
        src = _read(os.path.join(APP_DIR, "memory.py"))
        assert "getpid" in src, "ID 需混入进程标识以消除重启后的重号"

    def test_md_memory_group_match_is_exact(self):
        """分组标记必须行首精确匹配，子串匹配会被模型写的内容误触发。"""
        from app.md_memory import MemoryStore
        import tempfile
        d = tempfile.mkdtemp()
        m = MemoryStore(d, max_chars=4000)
        # 先写一条内容里含有日期标题样式的记忆
        m.append("scene", "注意 ### 2026-09-06 这个日期的说法")
        m.append("scene", "另一条正常记忆")
        content = m.read("scene")
        assert content.count("### 2026-09-06") >= 1
        # 正常记忆应被追加进去，而不是插到错乱位置
        assert "另一条正常记忆" in content

    def test_compaction_rejects_still_oversized(self):
        """放宽上限会让「压缩完仍超限」被接受，形成反复压缩的死循环。"""
        from app.md_memory import MemoryStore
        import tempfile
        m = MemoryStore(tempfile.mkdtemp(), max_chars=100)
        ok, msg = m.replace("scene", "x" * 130)
        assert ok is False
        assert "超过上限" in msg


class TestFrontendRobustness:
    def test_event_binding_is_defensive(self):
        """事件绑定缺元素时不得中断后续绑定。

        此前 20 多个监听器连续裸调，任一元素缺失就会抛错，
        导致后面全部注册不上——界面半失灵且毫无提示。
        """
        src = _read(os.path.join(PROJECT_ROOT, "gui", "web", "js", "app.js"))
        assert "function on(el, evt, handler)" in src
        # 初始化区不应再有裸绑定
        tail = src[src.index("els.btnRefresh.addEventListener") if
                   "els.btnRefresh.addEventListener" in src else 0:]
        assert "els.btnRefresh.addEventListener" not in tail


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
