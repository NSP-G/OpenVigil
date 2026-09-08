# -*- coding: utf-8 -*-
"""Windows 平台特定测试（窗口枚举、抓帧、通知模块）。
仅在 Windows 上运行；非 Windows 环境自动跳过整个模块。
"""
import sys

import pytest

if sys.platform != "win32":
    pytest.skip("仅 Windows 平台运行", allow_module_level=True)

from PIL import Image

from app import window_capture
from app import notifier
from app.window_capture import WindowClosedError, WindowMinimizedError


class TestWindowExceptions:
    def test_exception_hierarchy(self):
        assert issubclass(WindowClosedError, RuntimeError)
        assert issubclass(WindowMinimizedError, RuntimeError)


class TestWindowCaptureWindows:
    """窗口枚举与抓帧模块在真实 Windows 环境下的冒烟测试。"""

    def test_list_windows_returns_list(self):
        wins = window_capture.list_windows()
        assert isinstance(wins, list)

    def test_list_windows_visible_only(self):
        wins = window_capture.list_windows(visible_only=True)
        assert isinstance(wins, list)
        for w in wins:
            assert hasattr(w, "hwnd")
            assert hasattr(w, "title")

    def test_list_windows_all(self):
        wins = window_capture.list_windows(visible_only=False)
        assert isinstance(wins, list)

    def test_capture_invalid_hwnd_raises_closed_error(self):
        """无效句柄应抛 WindowClosedError，而非其他异常。"""
        with pytest.raises(window_capture.WindowClosedError):
            window_capture.capture_window(99999999)

    def test_capture_zero_hwnd_raises(self):
        with pytest.raises(window_capture.WindowClosedError):
            window_capture.capture_window(0)

    def test_capture_any_real_window(self):
        """遍历所有窗口，至少能成功抓一帧（验证 PrintWindow 链路）。"""
        wins = window_capture.list_windows(visible_only=True)
        if not wins:
            pytest.skip("无可见窗口")
        last_error = None
        for w in wins[:20]:  # 最多试 20 个窗口
            try:
                img = window_capture.capture_window(w.hwnd)
                assert isinstance(img, Image.Image)
                assert img.size[0] >= 1 and img.size[1] >= 1
                assert img.mode in ("RGB", "RGBA")
                return  # 成功抓到一帧即通过
            except window_capture.WindowClosedError:
                continue  # 窗口在枚举后关闭了，正常
            except Exception as e:
                last_error = e
                continue
        # 所有窗口都抓失败才报错
        if last_error:
            pytest.fail(f"所有窗口抓帧均失败，最后错误：{last_error}")
        else:
            pytest.skip("无窗口可抓帧")

    def test_window_by_title_nonexistent(self):
        result = window_capture.window_by_title("不存在的窗口标题_xyz_nonexistent_12345")
        assert result is None

    def test_window_by_title_empty(self):
        result = window_capture.window_by_title("")
        assert result is None

    def test_dpi_awareness_idempotent(self):
        """重复调用 _set_dpi_awareness 不应报错。"""
        window_capture._set_dpi_awareness()
        window_capture._set_dpi_awareness()  # 第二次应直接返回
        assert window_capture._dpi_aware is True


class TestNotifierWindows:
    """通知模块在 Windows 上的冒烟测试。"""

    def test_win11toast_available(self):
        """win11toast 应能成功导入。"""
        assert notifier._HAS_TOAST is True

    def test_notify_does_not_crash(self):
        """调用 notify 不应抛异常（无头环境下 Toast 可能不显示，但不应崩溃）。"""
        # 用不存在的图片路径，验证降级逻辑
        notifier.notify("Vigil 测试通知", "这是一条来自 CI 的测试通知", image_path="nonexistent_image.jpg")

    def test_notify_without_image(self):
        notifier.notify("Vigil 测试通知", "无图片版本")


class TestConfigWindowsPaths:
    """配置模块在 Windows 路径下的兼容性。"""

    def test_writable_config_path_is_absolute(self):
        from app import config as config_mod
        path = config_mod.writable_config_path()
        assert isinstance(path, str)
        assert len(path) > 0
        # Windows 上应该是绝对路径（含盘符）
        assert ":" in path or path.startswith("\\\\")

    def test_save_and_load_with_windows_path(self, tmp_path):
        from app import config as config_mod
        path = str(tmp_path / "config.json")
        cfg = dict(config_mod.DEFAULT_CONFIG)
        cfg["api_key"] = "win-test-key"
        cfg["alert_image_dir"] = "C:\\Vigil\\alerts"
        config_mod.save_config(cfg, path)
        loaded, _ = config_mod.load_config(path)
        assert loaded["api_key"] == "win-test-key"
        assert loaded["alert_image_dir"] == "C:\\Vigil\\alerts"

