# -*- coding: utf-8 -*-
"""窗口枚举与窗口抓帧模块（仅 Windows）。
Copyright (c) 2026 CaspianFlow. 版权所有。
使用 pywin32 的 PrintWindow 抓取指定窗口内容，即使窗口被其他窗口遮挡也能抓全。
"""
import ctypes

from PIL import Image

try:
    import win32gui
    import win32ui
except ImportError:  # pragma: no cover - 非 Windows 环境
    win32gui = win32ui = None


class WindowInfo:
    """一个已打开窗口的元信息。"""
    def __init__(self, hwnd, title, cls_name):
        self.hwnd = hwnd
        self.title = title or "(无标题)"
        self.cls_name = cls_name or ""

    def __repr__(self):
        return f"<Window hwnd={self.hwnd} title={self.title!r}>"


class WindowClosedError(RuntimeError):
    """目标窗口已关闭或句柄失效。"""


class WindowMinimizedError(RuntimeError):
    """目标窗口处于最小化状态，PrintWindow 只能抓到黑屏。"""


_dpi_aware = False


def _set_dpi_awareness():
    """按进程设置 DPI 感知（仅第一次调用生效），避免高分屏下抓图尺寸与实际不符。"""
    global _dpi_aware
    if _dpi_aware:
        return
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)  # PER_MONITOR_DPI_AWARE
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass
    _dpi_aware = True


def list_windows(visible_only=True):
    """枚举当前所有打开的顶层窗口。
    返回 [WindowInfo]，顺序为 Windows 返回的窗口 Z 序（前台到后台）。仅在 Windows 上可用。
    """
    if win32gui is None:
        raise RuntimeError("pywin32 未安装或当前不是 Windows 环境，无法枚举窗口。")
    results = []

    def _enum_cb(hwnd, _):
        if visible_only and not win32gui.IsWindowVisible(hwnd):
            return True
        title = win32gui.GetWindowText(hwnd)
        if not title:
            return True
        cls = win32gui.GetClassName(hwnd)
        results.append(WindowInfo(hwnd, title, cls))
        return True

    win32gui.EnumWindows(_enum_cb, None)
    return results


def _get_window_rect(hwnd):
    """获取窗口矩形（含边框与标题栏），用于抓帧尺寸计算。"""
    left, top, right, bottom = win32gui.GetWindowRect(hwnd)
    return left, top, right, bottom


def capture_window(hwnd, dpi_aware=True):
    """抓取指定窗口的画面，返回 PIL.Image。
    优先用 PrintWindow(PW_RENDERFULLCONTENT) 抓取，失败时回退到
    PrintWindow(0)（经典模式）。返回的图片即为窗口当前画面。
    """
    if win32gui is None:
        raise RuntimeError("pywin32 未安装或当前不是 Windows 环境，无法抓取窗口。")
    # 句柄失效（窗口已关闭）时 IsWindow 返回 False
    if not win32gui.IsWindow(hwnd):
        raise WindowClosedError(f"目标窗口已关闭（hwnd={hwnd}）")
    # 最小化窗口 PrintWindow 只会得到黑屏，直接给出明确错误，避免把黑屏发给 API
    if win32gui.IsIconic(hwnd):
        raise WindowMinimizedError("目标窗口已最小化，请恢复窗口显示后再巡检。")
    if dpi_aware:
        _set_dpi_awareness()
    left, top, right, bottom = _get_window_rect(hwnd)
    width = max(right - left, 1)
    height = max(bottom - top, 1)
    # 方案一：PrintWindow + PW_RENDERFULLCONTENT
    img = _print_window(hwnd, left, top, width, height, use_full_content=True)
    if img is not None:
        return img
    # 方案二：经典 PrintWindow
    img = _print_window(hwnd, left, top, width, height, use_full_content=False)
    if img is not None:
        return img
    raise WindowClosedError(
        f"抓取窗口失败（hwnd={hwnd}）：PrintWindow 返回空。该窗口可能已关闭。"
    )


def _print_window(hwnd, left, top, width, height, use_full_content=True):
    """执行一次 PrintWindow 抓取，失败返回 None。所有 GDI 资源在 finally 中安全释放。"""
    hwnd_dc = None
    mfc_dc = None
    save_dc = None
    bitmap = None
    try:
        hwnd_dc = win32gui.GetWindowDC(hwnd)
        if not hwnd_dc:
            return None
        mfc_dc = win32ui.CreateDCFromHandle(hwnd_dc)
        save_dc = mfc_dc.CreateCompatibleDC()
        bitmap = win32ui.CreateBitmap()
        bitmap.CreateCompatibleBitmap(mfc_dc, width, height)
        save_dc.SelectObject(bitmap)
        flag = 2 if use_full_content else 0  # PW_RENDERFULLCONTENT=2
        ret = ctypes.windll.user32.PrintWindow(hwnd, save_dc.GetSafeHdc(), flag)
        if ret == 0:
            return None
        bmpinfo = bitmap.GetInfo()
        bmpstr = bitmap.GetBitmapBits(True)
        bw, bh = bmpinfo["bmWidth"], bmpinfo["bmHeight"]
        # BMP 每行字节数必须按 4 字节对齐。窗口宽度不是 4 的倍数时，
        # 每行末尾会补 padding，而 frombuffer 传 stride=0 会按 width*4 计算，
        # 于是从第二行起逐行错位——画面呈现为规律的斜向撕裂。
        # 这种损坏很隐蔽：图看起来"能看"，但模型看到的是错乱内容。
        stride = ((bw * 32 + 31) // 32) * 4
        image = Image.frombuffer(
            "RGB", (bw, bh), bmpstr, "raw", "BGRX", stride, 1,
        )
        return image.copy()
    finally:
        if bitmap is not None:
            try:
                win32gui.DeleteObject(bitmap.GetHandle())
            except Exception:
                pass
        if save_dc is not None:
            try:
                save_dc.DeleteDC()
            except Exception:
                pass
        if mfc_dc is not None:
            try:
                mfc_dc.DeleteDC()
            except Exception:
                pass
        if hwnd_dc is not None:
            try:
                win32gui.ReleaseDC(hwnd, hwnd_dc)
            except Exception:
                pass


def window_by_title(fragment):
    """按标题片段模糊查找窗口，返回第一个匹配的 WindowInfo；找不到返回 None。"""
    for w in list_windows():
        if fragment and fragment.lower() in w.title.lower():
            return w
    return None
