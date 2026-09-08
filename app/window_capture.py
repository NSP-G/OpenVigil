# -*- coding: utf-8 -*-
"""窗口枚举与窗口抓帧模块（仅 Windows）。
Copyright (c) 2026 CaspianFlow. 版权所有。
使用 pywin32 的 PrintWindow 抓取指定窗口内容，即使窗口被其他窗口遮挡也能抓全。
"""
import ctypes
import sys

from PIL import Image

try:
    import win32gui
    import win32ui
    import win32process
    import win32con
except ImportError:  # pragma: no cover - 非 Windows 环境
    win32gui = win32ui = win32process = win32con = None


class WindowInfo:
    """一个已打开窗口的元信息。

    新增 pid / exe / styles 等字段是**排查刚需**：
    老师报告"监控软件在列表里找不到"时，光有标题无从判断——
    很多监控客户端窗口没有标题（无边框、自绘标题栏），
    只能靠类名和 EXE 路径认出来。
    """
    def __init__(self, hwnd, title, cls_name, pid=0, exe="",
                 visible=True, styles=None):
        self.hwnd = hwnd
        self.title = title or "(无标题)"
        self.cls_name = cls_name or ""
        self.pid = pid
        self.exe = exe or ""
        self.visible = visible
        # 标注"为什么这个窗口以前会被过滤掉"，供诊断报告使用
        self.styles = styles or {}

    def __repr__(self):
        return f"<Window hwnd={self.hwnd} title={self.title!r} cls={self.cls_name!r}>"


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


def display_name(w):
    """窗口在列表里的显示名。

    监控软件常无标准标题栏，若只显示标题就会变成一串"(无标题)"，
    几十个挤在一起根本认不出哪个是监控画面。
    因此无标题时退回到"类名"或"EXE 文件名"，让老师能凭程序名认出来。

    放在 window_capture 而非 GUI 层，是为了让它可被单元测试直接覆盖——
    GUI 模块依赖 pywebview，非 Windows 的 CI 上 import 不了，
    逻辑放那儿就等于没测试。
    """
    title = (getattr(w, "title", "") or "").strip()
    if title and title != "(无标题)":
        return title
    cls = (getattr(w, "cls_name", "") or "").strip()
    exe = (getattr(w, "exe", "") or "").strip()
    if exe:
        base = exe.replace("\\", "/").rsplit("/", 1)[-1]
        return f"[无标题] {base}" + (f" · {cls}" if cls else "")
    if cls:
        return f"[无标题] {cls}"
    return "(无标题)"


def _window_styles(hwnd):
    """读取窗口样式，标注"为什么它可能被其他工具过滤掉"。"""
    out = {}
    try:
        ex = win32gui.GetWindowLong(hwnd, GWL_EXSTYLE)
        out["toolwindow"] = bool(ex & WS_EX_TOOLWINDOW)
        out["noactivate"] = bool(ex & WS_EX_NOACTIVATE)
        out["layered"] = bool(ex & WS_EX_LAYERED)
    except Exception:
        pass
    try:
        st = win32gui.GetWindowLong(hwnd, GWL_STYLE)
        out["has_caption"] = bool(st & WS_CAPTION)
        out["child"] = bool(st & WS_CHILD)
    except Exception:
        pass
    return out


def _pid_exe(hwnd):
    """取窗口所属进程 PID 与 EXE 路径。失败返回 (0, "")。"""
    try:
        pid = win32process.GetWindowThreadProcessId(hwnd)[1]
    except Exception:
        return 0, ""
    exe = ""
    try:
        # PROCESS_QUERY_LIMITED_INFORMATION 对高权限进程也能用，
        # 比 PROCESS_ALL_ACCESS 更容易成功
        h = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
        if h:
            try:
                buf = ctypes.create_unicode_buffer(1024)
                size = ctypes.wintypes.DWORD(1024)
                if ctypes.windll.kernel32.QueryFullProcessImageNameW(
                        h, 0, buf, ctypes.byref(size)):
                    exe = buf.value
            finally:
                ctypes.windll.kernel32.CloseHandle(h)
    except Exception:
        pass
    return pid, exe


def list_windows(visible_only=True, include_untitled=True,
                 include_children=False):
    """枚举当前所有打开的顶层窗口，返回 [WindowInfo]（Z 序，前台到后台）。

    【重要修复】旧版有一行 `if not title: return True`，
    把**所有无标题窗口直接丢弃**。

    监控客户端普遍采用无边框 + 自绘标题栏的设计（画面区没有标准标题栏），
    于是"老师能看见画面，但 Vigil 列表里就是没有它"——
    功能看起来完全正常，唯独找不到目标窗口。

    这类问题极难自查：列表里其他窗口都在，用户只会以为
    "这个软件不支持"，不会想到是枚举把人家跳过了。

    因此现在**默认保留无标题窗口**，并用类名/EXE 作为可识别的显示名。
    """
    if win32gui is None:
        raise RuntimeError("pywin32 未安装或当前不是 Windows 环境，无法枚举窗口。")
    results = []

    def _enum_cb(hwnd, _):
        try:
            vis = bool(win32gui.IsWindowVisible(hwnd))
            if visible_only and not vis:
                return True
            title = win32gui.GetWindowText(hwnd) or ""
            if not title and not include_untitled:
                return True
            cls = win32gui.GetClassName(hwnd) or ""
            # 无标题时丢弃纯系统辅助窗口，避免列表被几百个无意义条目淹没；
            # 但**不能**因为没标题就全丢——那正是监控软件消失的原因。
            if not title and cls in _NOISE_CLASSES:
                return True
            pid, exe = _pid_exe(hwnd)
            results.append(WindowInfo(hwnd, title, cls, pid=pid, exe=exe,
                                      visible=vis,
                                      styles=_window_styles(hwnd)))
        except Exception:
            # 单个窗口读取失败不能中断整体枚举——
            # 某个高权限进程的窗口读不到，不代表别的窗口也不需要
            pass
        return True

    win32gui.EnumWindows(_enum_cb, None)

    if include_children:
        # 有些监控系统把画面渲染在子窗口里（顶层只是个壳），
        # 顶层能枚举但抓出来是空白，真正的画面在子窗口。
        tops = list(results)
        seen = {w.hwnd for w in tops}
        for top in tops:
            kids = []

            def _child_cb(h, _):
                if h in seen:
                    return True
                seen.add(h)
                try:
                    if visible_only and not win32gui.IsWindowVisible(h):
                        return True
                    t = win32gui.GetWindowText(h) or ""
                    c = win32gui.GetClassName(h) or ""
                    if not t and c in _NOISE_CLASSES:
                        return True
                    pid, exe = _pid_exe(h)
                    kids.append(WindowInfo(h, t, c, pid=pid, exe=exe,
                                           visible=True,
                                           styles=_window_styles(h)))
                except Exception:
                    pass
                return True

            try:
                win32gui.EnumChildWindows(top.hwnd, _child_cb, None)
            except Exception:
                pass
            results.extend(kids)

    return results


# 无标题且属于系统噪音的窗口类（丢弃它们不影响可用性，
# 但绝不能把"无标题"本身当作丢弃条件）
_NOISE_CLASSES = frozenset({
    "Shell_TrayWnd", "Shell_SecondaryTrayWnd", "Progman",
    "WorkerW", "DV2ControlHost", "MsgrIMEWindow", "IME",
    "Default IME", "Microsoft-Windows-TabletPC-Platform-Input-Context",
    "Windows.UI.Core.CoreWindow", "ApplicationFrameInputSinkWindow",
    "ApplicationManager_ImmersiveShellWindow",
})

# 窗口样式常量（ctypes 兜底，pywin32 未导出时也能用）
GWL_STYLE = -16
GWL_EXSTYLE = -20
WS_CAPTION = 0x00C00000
WS_CHILD = 0x40000000
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_NOACTIVATE = 0x08000000
WS_EX_LAYERED = 0x00080000


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


# ---------------------------------------------------------------------------
# 屏幕区域捕获（窗口枚举的兜底通道）
# ---------------------------------------------------------------------------

def virtual_screen_rect():
    """虚拟屏矩形 (left, top, width, height)。

    多显示器时，各屏坐标系可能不连续（副屏在左上时坐标为负）。
    用虚拟屏坐标才能正确定位跨屏区域——
    直接按主屏 (0,0) 起算会在副屏上抓错位置。
    """
    try:
        user32 = ctypes.windll.user32
        # 先设置 DPI 感知，否则高分屏下 GetSystemMetrics 返回的是缩放后的假值
        _set_dpi_awareness()
        left = user32.GetSystemMetrics(76)   # SM_XVIRTUALSCREEN
        top = user32.GetSystemMetrics(77)    # SM_YVIRTUALSCREEN
        width = user32.GetSystemMetrics(78)  # SM_CXVIRTUALSCREEN
        height = user32.GetSystemMetrics(79) # SM_CYVIRTUALSCREEN
        if width > 0 and height > 0:
            return left, top, width, height
    except Exception:
        pass
    return 0, 0, 0, 0


def list_monitors():
    """枚举显示器，返回 [{index, left, top, width, height, primary}]。"""
    monitors = []

    try:
        user32 = ctypes.windll.user32

        # pywin32 的 EnumDisplayMonitors 需要回调签名，用 ctypes 更直接
        MONITORENUMPROC = ctypes.WINFUNCTYPE(
            ctypes.c_int, ctypes.c_ulong, ctypes.c_ulong,
            ctypes.POINTER(RECT), ctypes.c_double)

        class RECT(ctypes.Structure):
            _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                        ("right", ctypes.c_long), ("bottom", ctypes.c_long)]

        def _cb(hmon, hdc, lprc, data):
            r = lprc.contents
            monitors.append({
                "index": len(monitors),
                "left": r.left, "top": r.top,
                "width": r.right - r.left, "height": r.bottom - r.top,
                "primary": len(monitors) == 0,
            })
            return 1

        user32.EnumDisplayMonitors(0, 0, MONITORENUMPROC(_cb), 0)
    except Exception:
        pass

    if not monitors:
        l, t, w, h = virtual_screen_rect()
        if w > 0:
            monitors = [{"index": 0, "left": l, "top": t,
                         "width": w, "height": h, "primary": True}]
    return monitors


def capture_screen_region(left, top, width, height):
    """抓取屏幕指定区域，返回 PIL.Image。

    这是**窗口枚举的兜底通道**，也是老师报告"列表里找不到监控软件"时
    最可靠的解法：它直接对屏幕像素抓图，完全不经过窗口枚举，
    因此：
      - 目标窗口有没有标题、是不是工具窗口、有没有被过滤 —— 都无所谓
      - 窗口最小化/隐藏到托盘 —— 只要画面还在屏幕上就能抓

    唯一抓不到的是 `SetWindowDisplayAffinity(WDA_EXCLUDEFROMCAPTURE)`
    这类**画面级保护**（整屏截屏该区域也是黑的），
    那种情况只能走 RTSP 直连摄像头，属于另一条路。

    刻意用纯 pywin32 GDI 实现而不引入 mss 等新依赖：
    0.3 是已发布的正式版，新增依赖会牵动打包配置与 hidden-import 守卫，
    补丁版应尽量缩小改动面。
    """
    if win32gui is None:
        raise RuntimeError("pywin32 未安装或当前不是 Windows 环境，无法抓屏。")
    _set_dpi_awareness()

    width = max(int(width), 1)
    height = max(int(height), 1)

    # GetDC(0) 得到的是覆盖虚拟屏的设备上下文，
    # 因此可以直接用虚拟屏坐标（可能为负）定位
    hdc = win32gui.GetDC(0)
    if not hdc:
        raise RuntimeError("无法获取屏幕设备上下文（GetDC 失败）")
    mfc_dc = None
    save_dc = None
    bitmap = None
    try:
        mfc_dc = win32ui.CreateDCFromHandle(hdc)
        save_dc = mfc_dc.CreateCompatibleDC()
        bitmap = win32ui.CreateBitmap()
        bitmap.CreateCompatibleBitmap(mfc_dc, width, height)
        save_dc.SelectObject(bitmap)
        # SRCCOPY | CAPTUREBLT：后者用于正确捕获分层窗口与透明效果
        save_dc.BitBlt((0, 0), (width, height), mfc_dc,
                       (int(left), int(top)), 0x00CC0020 | 0x40000000)
        bmpinfo = bitmap.GetInfo()
        bmpstr = bitmap.GetBitmapBits(True)
        bw, bh = bmpinfo["bmWidth"], bmpinfo["bmHeight"]
        # 与窗口抓帧同款坑：BMP 每行 4 字节对齐，
        # stride 算错会导致画面规律斜向撕裂（图能看但内容错乱）
        stride = ((bw * 32 + 31) // 32) * 4
        image = Image.frombuffer(
            "RGB", (bw, bh), bmpstr, "raw", "BGRX", stride, 1,
        )
        return image.copy()
    finally:
        # 逐个独立 try：某个资源释放失败不能影响后续释放，
        # 否则 GDI 句柄会持续泄漏，长时间巡检后系统资源耗尽
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
        try:
            win32gui.ReleaseDC(0, hdc)
        except Exception:
            pass


def diagnose():
    """采集窗口枚举与抓屏的诊断信息，用于"列表里找不到目标窗口"的排查。

    三层对比，一次跑完即可定位到具体原因：
      A. 旧版枚举逻辑（复现问题）
      B. 新版枚举（含无标题窗口）
      C. 屏幕区域抓帧（验证兜底通道是否可用）
    """
    report = {
        "platform": sys.platform,
        "pywin32": win32gui is not None,
        "monitors": list_monitors(),
        "virtual_screen": virtual_screen_rect(),
    }
    if win32gui is None:
        report["error"] = "非 Windows 环境或未安装 pywin32"
        return report

    # A：旧逻辑——只收"有标题且可见"的窗口（复现老师看到的现象）
    legacy = []

    def _legacy_cb(hwnd, _):
        try:
            if not win32gui.IsWindowVisible(hwnd):
                return True
            t = win32gui.GetWindowText(hwnd)
            if not t:
                return True
            legacy.append({"hwnd": hwnd, "title": t,
                           "cls": win32gui.GetClassName(hwnd)})
        except Exception:
            pass
        return True

    try:
        win32gui.EnumWindows(_legacy_cb, None)
    except Exception as e:
        report["legacy_error"] = str(e)

    # B：新版枚举
    try:
        modern = list_windows(visible_only=True, include_untitled=True)
    except Exception as e:
        modern = []
        report["modern_error"] = str(e)

    def _brief(w):
        return {
            "hwnd": w.hwnd,
            "title": w.title,
            "cls": w.cls_name,
            "pid": w.pid,
            "exe": w.exe,
            "visible": w.visible,
            "styles": w.styles,
        }

    report["legacy_count"] = len(legacy)
    report["modern_count"] = len(modern)
    report["legacy_windows"] = legacy[:200]
    report["modern_windows"] = [_brief(w) for w in modern[:200]]

    # 差异即"被旧逻辑吞掉的窗口"——多半就是老师找不到的那个
    legacy_hwnds = {w["hwnd"] for w in legacy}
    report["missed_by_legacy"] = [
        _brief(w) for w in modern if w.hwnd not in legacy_hwnds
    ][:100]

    # C：屏幕区域抓帧验证
    try:
        l, t, w, h = virtual_screen_rect()
        probe_w, probe_h = min(320, w or 320), min(240, h or 240)
        img = capture_screen_region(l, t, probe_w, probe_h)
        # 全黑/单色说明屏幕捕获通道异常（如 WDA 保护或远程会话）
        colors = img.convert("RGB").getcolors(maxcolors=1_000_000)
        report["screen_probe"] = {
            "ok": True,
            "size": [img.width, img.height],
            "unique_colors": len(colors) if colors else -1,
            "likely_blank": bool(colors and len(colors) <= 1),
        }
    except Exception as e:
        report["screen_probe"] = {"ok": False, "error": f"{type(e).__name__}: {e}"}

    return report
