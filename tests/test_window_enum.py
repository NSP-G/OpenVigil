# -*- coding: utf-8 -*-
"""窗口枚举与屏幕区域捕获的跨平台测试。

刻意**不做** Windows 平台限制：
本文件测的是源码级约束与纯逻辑（显示名兜底、常量、不抛异常），
这些在 Linux 上同样成立。若放进 test_windows.py，
非 Windows 环境会被 `pytest.skip` 整体跳过——
而 CI 恰好跑在 Linux，等于这些用例从未真正执行过。

"本地能跑但 CI 从没跑过"正是这次事故的同类问题。
"""
import inspect

from app import window_capture


class TestUntitledWindowRegression:
    """无标题窗口被丢弃——"监控软件在列表里找不到"的直接原因。

    旧版 list_windows 有一行 `if not title: return True`，
    把所有无标题窗口直接丢掉。监控客户端普遍无边框+自绘标题栏，
    于是老师看得见画面、Vigil 列表里却没有它。
    """

    def test_legacy_filter_is_removed(self):
        """不得再出现"无标题即无条件跳过"的分支。

        用 AST 精确匹配而非字符串搜索：
        文档字符串里为了说明这次事故会引用 `if not title: return True`，
        纯文本匹配会把注释当成真代码，产生假阳性。
        这里只找真正的、无附加条件的 `if not title: return`。
        """
        import ast
        src = inspect.getsource(window_capture.list_windows)
        tree = ast.parse(src)
        offenders = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.If):
                continue
            test = node.test
            # 只看 `if not title:` —— 带 and 的条件（如
            # `if not title and cls in NOISE`）是合理的收窄，不算问题
            if not (isinstance(test, ast.UnaryOp)
                    and isinstance(test.op, ast.Not)
                    and isinstance(test.operand, ast.Name)
                    and test.operand.id == "title"):
                continue
            # 该分支是否直接 return（跳过该窗口）
            for sub in node.body:
                if isinstance(sub, ast.Return):
                    offenders.append(node.lineno)
        assert not offenders, (
            "list_windows 出现无条件的 `if not title: return`（行 %s）——"
            "这会让无标题的监控软件窗口从列表里消失" % offenders)

    def test_untitled_param_defaults_to_true(self):
        sig = inspect.signature(window_capture.list_windows)
        assert sig.parameters["include_untitled"].default is True

    def test_children_param_exists(self):
        """子窗口枚举：画面可能渲染在子窗口而非顶层壳里。"""
        sig = inspect.signature(window_capture.list_windows)
        assert "include_children" in sig.parameters


class TestDisplayNameFallback:
    """无标题时必须有可识别的兜底显示名。"""

    def test_falls_back_to_exe(self):
        w = window_capture.WindowInfo(1, "", "Qt5152QWindowIcon",
                                      exe=r"C:\Program Files\NVR\Client.exe")
        name = window_capture.display_name(w)
        assert "Client.exe" in name
        assert name != "(无标题)"

    def test_falls_back_to_class(self):
        w = window_capture.WindowInfo(2, "", "SomeRenderWindow")
        assert "SomeRenderWindow" in window_capture.display_name(w)

    def test_prefers_real_title(self):
        w = window_capture.WindowInfo(3, "监控画面", "X", exe="Y.exe")
        assert window_capture.display_name(w) == "监控画面"

    def test_placeholder_title_not_treated_as_name(self):
        """WindowInfo 已把空标题填成"(无标题)"，不能当作真实名字。"""
        w = window_capture.WindowInfo(4, "", "Cls", exe="A.exe")
        assert "A.exe" in window_capture.display_name(w)

    def test_noise_classes_defined(self):
        assert "Shell_TrayWnd" in window_capture._NOISE_CLASSES

    def test_style_constants(self):
        assert window_capture.WS_EX_TOOLWINDOW == 0x00000080
        assert window_capture.WS_CAPTION == 0x00C00000


class TestScreenRegionFallback:
    """屏幕区域捕获——窗口枚举的兜底通道。"""

    def test_functions_exist(self):
        assert callable(window_capture.capture_screen_region)
        assert callable(window_capture.list_monitors)
        assert callable(window_capture.virtual_screen_rect)
        assert callable(window_capture.diagnose)

    def test_virtual_screen_rect_shape(self):
        r = window_capture.virtual_screen_rect()
        assert isinstance(r, tuple) and len(r) == 4

    def test_list_monitors_returns_list(self):
        assert isinstance(window_capture.list_monitors(), list)

    def test_diagnose_never_raises(self):
        """诊断跑在老师机器上，任何异常都要被吞掉并记进报告。"""
        rep = window_capture.diagnose()
        assert isinstance(rep, dict)
        if not rep.get("pywin32"):
            assert "error" in rep

    def test_diagnose_has_missed_key_on_windows(self):
        """Windows 上必须报告"被旧逻辑漏掉的窗口"——排查的核心依据。"""
        rep = window_capture.diagnose()
        if rep.get("pywin32"):
            assert "missed_by_legacy" in rep


class TestBridgeWindowFields:
    """GUI 层返回给前端的窗口字段必须完整。

    前端 renderWindows 依赖 `w.display` 显示名称、
    `w.flags` 标出可疑特征。后端少返回一个字段，
    列表里就退化成一串无法区分的"(无标题)"——
    老师看着像"监控软件不在列表里"，其实枚举早就抓到了它。

    这类断链不会报错，只是信息不见了，因此必须在行为层测，
    而不是只测 window_capture 本身。
    """
    def _bridge(self, monkeypatch, windows):
        from gui import main_gui

        monkeypatch.setattr(main_gui.window_capture, "list_windows",
                            lambda **kw: windows)
        return main_gui.Bridge(lambda: None)

    def _fake(self, hwnd=1, title="", cls="Chrome_WidgetWin_1",
              exe=r"C:\Program Files\NVR\client.exe", pid=4242,
              visible=True, styles=None):
        from app.window_capture import WindowInfo
        return WindowInfo(hwnd, title, cls, pid=pid, exe=exe,
                          visible=visible, styles=styles or {})

    def test_display_name_present_for_untitled_window(self, monkeypatch):
        """无标题窗口必须带上程序名，否则老师认不出哪个是监控画面。"""
        b = self._bridge(monkeypatch, [self._fake()])
        rows = b.list_windows()
        assert len(rows) == 1
        assert "display" in rows[0]
        # 无标题时回退到 EXE 名，不能是空的或纯占位符
        assert "client.exe" in rows[0]["display"]
        assert rows[0]["display"] != "(无标题)"

    def test_all_display_fields_returned(self, monkeypatch):
        b = self._bridge(monkeypatch, [self._fake(title="监控画面")])
        row = b.list_windows()[0]
        for key in ("hwnd", "title", "cls", "exe", "pid",
                    "display", "visible", "flags"):
            assert key in row, f"返回字段缺少 {key}"

    def test_flags_expose_why_window_is_hidden(self, monkeypatch):
        """可疑特征要传给前端，帮老师在几十个条目里认出目标。"""
        b = self._bridge(monkeypatch, [self._fake(
            styles={"toolwindow": True, "has_caption": False})])
        row = b.list_windows()[0]
        assert "toolwindow" in row["flags"]

    def test_children_enumerated_by_default(self, monkeypatch):
        """默认枚举子窗口：监控画面常渲染在子窗口里，顶层只是空壳。"""
        from app import window_capture
        seen = {}

        def fake(**kw):
            seen.update(kw)
            return []

        monkeypatch.setattr(window_capture, "list_windows", fake)
        from gui import main_gui
        monkeypatch.setattr(main_gui.window_capture, "list_windows", fake)
        main_gui.Bridge(lambda: None).list_windows()
        assert seen.get("include_children") is True
        assert seen.get("include_untitled") is True

    def test_include_hidden_toggles_visible_only(self, monkeypatch):
        """勾选"显示隐藏窗口"后必须真正放开可见性过滤。"""
        from gui import main_gui
        seen = {}

        def fake(**kw):
            seen.update(kw)
            return []

        monkeypatch.setattr(main_gui.window_capture, "list_windows", fake)
        b = main_gui.Bridge(lambda: None)
        b.list_windows()
        assert seen.get("visible_only") is True
        b.list_windows(include_hidden=True)
        assert seen.get("visible_only") is False

    def test_visible_flag_propagates(self, monkeypatch):
        b = self._bridge(monkeypatch, [self._fake(visible=False)])
        assert b.list_windows(include_hidden=True)[0]["visible"] is False
