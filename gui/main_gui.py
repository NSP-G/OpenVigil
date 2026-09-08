# -*- coding: utf-8 -*-
"""Vigil · 图形界面入口（pywebview + 本地 Web 前端）。
Copyright (c) 2026 CaspianFlow. 版权所有。
用法：
    python gui/main_gui.py
说明：
    - 前端为本地 HTML/CSS/JS（gui/web/），动画由本地 GSAP 提供，无任何外链。
    - Python 通过 pywebview 的 js_api 向 JS 暴露接口；
      Monitor 的状态通过 evaluate_js 推送回前端。
    - 仅支持 Windows（窗口抓取依赖 pywin32）。
"""
import base64
import io
import json
import os
import sys
import threading
import time

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from app import config as config_mod
from app import notifier, window_capture
from app.alert_history import AlertHistory
from app.monitor import Monitor
from app.selftest import SelfTest


def _resource_path(rel):
    """定位打包资源：PyInstaller 冻结时用 _MEIPASS，源码运行用脚本目录。"""
    base = getattr(sys, "_MEIPASS", None)
    if base is None:
        base = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base, rel)


def _frame_to_data_url(frame, max_side=640, quality=70):
    """把 PIL 帧转成小尺寸 JPEG data URL，用于前端预览（低流量）。"""
    img = frame.convert("RGB")
    w, h = img.size
    scale = min(1.0, max_side / max(w, h))
    if scale < 1.0:
        img = img.resize((int(w * scale), int(h * scale)))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=quality)
    b64 = base64.b64encode(buf.getvalue()).decode("ascii")
    return f"data:image/jpeg;base64,{b64}"


class Bridge:
    """暴露给前端 JS 的 API 对象。"""
    def __init__(self, window_getter):
        self._window_getter = window_getter  # 返回 webview.Window
        self._monitor = None
        self._stop_event = None
        self._thread = None
        self._lock = threading.Lock()
        self._selftest_thread = None
        self._selftest_lock = threading.Lock()
        # 告警历史独立于巡检实例，未巡检时也能查看既往记录
        self._history = None
        self._history_path = None
        # 配置缓存：历史轮询每 2 秒一次，若每次都重新读盘解析 config.json，
        # 等于给本就频繁的 IO 再叠一层。配置变更走 save_config 主动失效。
        self._cfg_cache = None
        self._last_preview_ts = 0.0

    # ---- JS 可调用的接口 ----
    def list_windows(self):
        """返回窗口列表，按 Z 序。非 Windows 时返回空列表。

        【修复】以前只返回 `{hwnd, title}`，且内部会因为"没有标题"
        直接丢弃整个窗口——监控客户端普遍无标准标题栏，于是在列表里
        永远找不到它。现在无标题窗口也会返回，并用类名/EXE 兜底显示，
        保证老师在界面上"看得见、选得到"。
        """
        try:
            wins = window_capture.list_windows()
        except Exception as e:
            self._push_status("error", f"窗口枚举不可用：{e}")
            return []
        out = []
        for w in wins:
            out.append({
                "hwnd": w.hwnd,
                "title": w.title,
                "cls": w.cls_name,
                "exe": w.exe,
                "pid": w.pid,
                "display": window_capture.display_name(w),
            })
        return out

    def list_monitors(self):
        """返回显示器列表，供屏幕区域捕获选择。"""
        try:
            return window_capture.list_monitors()
        except Exception as e:
            return [{"error": str(e)}]

    def capture_screen_preview(self, monitor_index=0, max_side=960):
        """抓一整屏缩略图，供前端拖拽框选巡检区域。

        返回 data URL。老师可以据此直接框出监控画面所在的矩形，
        不必手输坐标。
        """
        try:
            mons = window_capture.list_monitors()
            if not mons:
                return {"ok": False, "error": "未检测到显示器"}
            idx = 0
            try:
                idx = max(0, min(int(monitor_index), len(mons) - 1))
            except (TypeError, ValueError):
                idx = 0
            m = mons[idx]
            img = window_capture.capture_screen_region(
                m["left"], m["top"], m["width"], m["height"])
            scale = min(1.0, float(max_side) / max(img.width, img.height))
            if scale < 1.0:
                img = img.resize((int(img.width * scale),
                                  int(img.height * scale)))
            import io as _io
            import base64 as _b64
            buf = _io.BytesIO()
            img.save(buf, format="JPEG", quality=70)
            return {
                "ok": True,
                "dataUrl": "data:image/jpeg;base64," +
                           _b64.b64encode(buf.getvalue()).decode("ascii"),
                "screen": m,
                "preview_size": [img.width, img.height],
            }
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}

    def run_capture_diagnose(self):
        """采集窗口枚举/抓屏诊断信息并落盘，供排查"列表找不到窗口"。

        返回报告文件路径，老师把它发回即可定位原因，
        不必反复试各种操作。
        """
        try:
            report = window_capture.diagnose()
            import json as _json
            import os as _os
            log_dir = _os.path.join("memory")
            _os.makedirs(log_dir, exist_ok=True)
            path = _os.path.abspath(_os.path.join(log_dir,
                                                  "capture_diagnose.json"))
            with open(path, "w", encoding="utf-8") as f:
                _json.dump(report, f, ensure_ascii=False, indent=2)
            return {"ok": True, "path": path, "report": report}
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}

    def start_monitor(self, hwnd, region=None):
        """开始巡检。

        支持两种捕获源：
          - 窗口：传 hwnd（region 为 None）
          - 屏幕区域：hwnd 传 0，region 传 [left, top, width, height]

        屏幕区域是**窗口枚举的兜底通道**：监控软件若在窗口列表里找不到
        （无标题、工具窗口、被过滤），可以直接框选它所在的屏幕区域，
        不再依赖枚举结果。
        """
        with self._lock:
            if self._monitor is not None:
                return {"ok": False, "error": "已在巡检中"}
            cfg = self._get_config()
            if not cfg.get("api_key"):
                return {"ok": False, "error": "config.json 未填写 API Key，请先在设置中填写。"}

            if region:
                try:
                    rect = [int(v) for v in region][:4]
                    if len(rect) != 4 or rect[2] <= 0 or rect[3] <= 0:
                        return {"ok": False, "error": "区域参数无效"}
                except (TypeError, ValueError):
                    return {"ok": False, "error": "区域参数无效"}
                win = window_capture.WindowInfo(
                    0, f"屏幕区域 {rect[2]}x{rect[3]}", "ScreenRegion")
                source_desc = f"屏幕区域 {rect}"
            else:
                win = self._find_window(hwnd)
                if win is None:
                    return {"ok": False, "error": "窗口不存在或已关闭"}
                source_desc = f"窗口：{win.title}"

            log_dir = config_mod.ensure_dirs(cfg)
            notifier.init(log_dir)
            monitor = Monitor(
                cfg,
                win,
                verbose=False,
                preview_cb=self._push_frame,
            )
            if region:
                # 注入抓帧函数：忽略 hwnd，直接按区域抓屏
                monitor._capture_fn = (
                    lambda _h, _r=tuple(rect):
                    window_capture.capture_screen_region(*_r)
                )
            self._source_desc = source_desc
            self._stop_event = threading.Event()
            self._thread = threading.Thread(
                target=monitor.run_forever,
                kwargs={"stop_event": self._stop_event, "on_status": self._push_status},
                daemon=True,
            )
            self._monitor = monitor
            self._thread.start()
            return {"ok": True}

    def stop_monitor(self):
        """停止巡检。"""
        with self._lock:
            if self._monitor is None:
                return {"ok": False, "error": "未在巡检"}
            monitor, thread, stop_event = self._monitor, self._thread, self._stop_event
            stop_event.set()
            monitor.stop()
            thread.join(timeout=5)
            if thread.is_alive():
                # 可能正在等待一次 API 返回；不阻塞界面，线程为 daemon 会自行结束
                notifier.get_logger().warning("巡检线程未在 5 秒内退出，将在当前分析结束后自行停止。")
            try:
                monitor.shutdown()
            except Exception:
                pass
            monitor.close()
            self._monitor = None
            self._stop_event = None
            self._thread = None
            return {"ok": True}

    def test_once(self, hwnd):
        """选窗口后立即抓帧分析一次，返回判定结果。任何异常都转为结构化错误，不抛给 JS。"""
        monitor = None
        try:
            cfg = self._get_config()
            if not cfg.get("api_key"):
                return {"ok": False, "error": "config.json 未填写 API Key，请先在设置中填写。"}
            win = self._find_window(hwnd)
            if win is None:
                return {"ok": False, "error": "窗口不存在或已关闭"}
            log_dir = config_mod.ensure_dirs(cfg)
            notifier.init(log_dir)
            # alert_notify=False：单次测试只是为了看判定效果，
            # 若沿用默认 True，一旦判异常就会弹出**真实的系统告警通知**，
            # 用户点一下"测试"收到一条告警，会以为是真出事了。
            monitor = Monitor(cfg, win, verbose=False,
                              preview_cb=self._push_frame,
                              alert_notify=False)
            result = monitor.analyze_once()
            result["ok"] = True
            return result
        except Exception as e:
            notifier.get_logger().exception("单次测试失败")
            return {"ok": False, "error": f"测试失败：{e}"}
        finally:
            if monitor is not None:
                # Monitor 构造时会启动后台索引线程（rebuild_async），
                # 它是 daemon 但不会因 close() 而停止。
                # 每点一次"测试一次"就留下一个线程，反复点击会不断堆积。
                # 这里显式通知索引器停止，再关闭连接。
                try:
                    monitor.shutdown()
                except Exception:
                    pass
                monitor.close()

    def get_config(self):
        """返回当前配置（api_key 脱敏）。"""
        cfg, errors = config_mod.load_config()
        key = cfg.get("api_key", "")
        masked = ("****" + key[-4:]) if len(key) > 4 else ("****" if key else "")
        return {
            "ok": True,
            # 回传真实提示词，否则设置面板里这项永远显示为空，
            # 用户既看不到当前值，也不知道自己改没改成功。
            "config": {**cfg, "api_key": masked},
            "config_path": config_mod.writable_config_path(),
            "warnings": errors,
        }

    def save_config(self, data):
        """保存前端传来的配置。api_key 含 '*' 说明未修改，不覆盖。"""
        if not isinstance(data, dict):
            return {"ok": False, "error": "配置数据格式错误"}
        cfg, _ = config_mod.load_config()
        # 前端可能回传 prompt。空字符串表示"未修改/不关心"——
        # 此时保留现有值；有内容则以用户填写为准。
        # 此前无条件 pop 掉，导致提示词这项配置永远改不动。
        incoming_prompt = data.pop("prompt", None)
        if incoming_prompt and str(incoming_prompt).strip():
            cfg["prompt"] = str(incoming_prompt).strip()
        for k, v in data.items():
            if k not in cfg:
                continue
            if k == "api_key":
                if not v or "*" in v:
                    continue  # 脱敏值或空值不覆盖
            cfg[k] = v
        try:
            path = config_mod.save_config(cfg)
        except Exception as e:
            return {"ok": False, "error": f"保存失败：{e}（程序可能放在了只读目录，请移动到可写位置）"}
        # 配置已变更，缓存必须失效，否则界面读到的仍是旧值
        self._cfg_cache = dict(cfg)
        key_ok = bool(cfg.get("api_key"))
        return {
            "ok": True,
            "path": path,
            "api_key_set": key_ok,
            "message": "配置已保存" + ("" if key_ok else "（尚未填写 API Key）"),
        }

    # ---- 告警历史 ----
    def _get_config(self):
        """读取配置（带缓存）。保存设置时由 save_config 主动失效。"""
        if self._cfg_cache is None:
            cfg, _ = config_mod.load_config()
            self._cfg_cache = cfg
        return self._cfg_cache

    def _get_history(self):
        """按当前配置惰性创建/复用告警历史实例。"""
        cfg = self._get_config()
        path = cfg.get("alert_history_file") or "alert_history.json"
        if not os.path.isabs(path):
            path = os.path.join(config_mod._project_root(), path)
        max_records = int(cfg.get("alert_history_max") or 500)
        if self._history is None or self._history_path != path:
            self._history = AlertHistory(path, max_records=max_records)
            self._history_path = path
        return self._history

    def get_alert_history(self, limit=200):
        """返回告警历史（最新在前），供界面「告警历史」栏目展示。"""
        try:
            history = self._get_history()
            records = history.list(limit=limit)
            return {
                "ok": True,
                "records": records,
                "total": history.count(),
                "path": history.path,
                "warning": history.load_error or "",
            }
        except Exception as e:
            notifier.get_logger().exception("读取告警历史失败")
            return {"ok": False, "error": f"读取告警历史失败：{e}", "records": []}

    def clear_alert_history(self):
        """清空告警历史（不影响已保存的异常截图文件）。"""
        try:
            self._get_history().clear()
            return {"ok": True, "message": "告警历史已清空（异常截图文件保留）。"}
        except Exception as e:
            return {"ok": False, "error": f"清空失败：{e}"}

    def open_alert_image(self, path):
        """用系统默认程序打开异常截图（便于放大查看）。"""
        try:
            if not path or not os.path.exists(path):
                return {"ok": False, "error": "截图文件不存在，可能已被清理或移动。"}
            if sys.platform == "win32":
                os.startfile(path)  # noqa: S606 - 打开用户自己的截图文件
            elif sys.platform == "darwin":
                import subprocess
                subprocess.Popen(["open", path])
            else:
                import subprocess
                subprocess.Popen(["xdg-open", path])
            return {"ok": True}
        except Exception as e:
            notifier.get_logger().warning("打开截图失败：%s", e)
            return {"ok": False, "error": f"打开失败：{e}"}

    # ---- 自检 ----
    def run_selftest(self, full=True):
        """后台执行自检，进度与结论通过状态回调推给前端。

        自检会调用 API（完整自检约 4~6 次），耗时可能达数十秒，
        因此放在后台线程，避免界面卡死。
        """
        with self._selftest_lock:
            if self._selftest_thread is not None and self._selftest_thread.is_alive():
                return {"ok": False, "error": "自检正在运行中，请等待完成。"}
            cfg = self._get_config()
            tester = SelfTest(cfg, progress_cb=self._push_selftest_step, full=bool(full))

            def worker():
                try:
                    report = tester.run()
                except Exception as e:
                    notifier.get_logger().exception("自检线程异常")
                    report = {
                        "ok": False,
                        "overall": "fail",
                        "elapsed": 0,
                        "steps": [],
                        "summary": f"自检异常终止：{e}",
                    }
                self._push_selftest_done(report)

            self._selftest_thread = threading.Thread(target=worker, daemon=True)
            self._selftest_thread.start()
        return {"ok": True, "message": "自检已开始，结果将逐项显示。"}

    def _push_selftest_step(self, step):
        self._report_raw("selftest_step", step)

    def _push_selftest_done(self, report):
        self._report_raw("selftest_done", report)

    # ---- 内部 ----
    def _find_window(self, hwnd):
        try:
            target = int(hwnd)
        except (TypeError, ValueError):
            return None
        for w in window_capture.list_windows():
            if w.hwnd == target:
                return w
        return None

    def _push_status(self, status, detail):
        """把 Monitor 状态推给前端。"""
        payload = {"status": status, "detail": detail}
        if status == "alert":
            payload["detail"] = f"告警：{detail}"
        self._eval(
            "window.monitorBridge && window.monitorBridge.onStatus("
            + json.dumps(payload, ensure_ascii=False) + ")"
        )

    # 预览帧最小间隔（秒）。抓帧间隔可低至 0.5s，若每帧都把整张图
    # base64 编码后经 evaluate_js 推给 WebView（单帧可达数十 KB 字符串），
    # 界面会被持续拖慢。预览只是"看个大意"，不必跟上抓帧频率。
    _PREVIEW_MIN_INTERVAL = 1.0

    def _push_frame(self, frame, stats):
        """把预览帧推给前端（带统计数据），并按最小间隔节流。"""
        now = time.monotonic()
        if now - self._last_preview_ts < self._PREVIEW_MIN_INTERVAL:
            return
        self._last_preview_ts = now
        try:
            data_url = _frame_to_data_url(frame)
        except Exception:
            return
        payload = {"status": "frame", "detail": {"dataUrl": data_url, **stats}}
        self._eval(
            "window.monitorBridge && window.monitorBridge.onStatus("
            + json.dumps(payload, ensure_ascii=False) + ")"
        )

    def _report_raw(self, status, detail):
        """向前端推送任意结构化状态（自检步骤/结论等）。"""
        payload = {"status": status, "detail": detail}
        self._eval(
            "window.monitorBridge && window.monitorBridge.onStatus("
            + json.dumps(payload, ensure_ascii=False) + ")"
        )

    def _eval(self, js):
        try:
            win = self._window_getter()
            if win is not None:
                win.evaluate_js(js)
        except Exception:
            pass


def detect_system_theme():
    """探测操作系统当前偏好深色还是浅色，返回 "dark" / "light"。

    窗口创建时就要定下底色，否则用户选了浅色、启动时却先闪一下深色背景。
    Python 侧拿不到浏览器的 prefers-color-scheme，只能直接问系统：
      - Windows：读注册表 AppsUseLightTheme（0=深色，1=浅色）
      - macOS   ：读 NSUserDefaults 的 AppleInterfaceStyle
      - Linux   ：看 GTK 的 gtk-application-prefer-dark-theme
    探测失败一律回退深色（与默认主题一致）。
    """
    if sys.platform == "win32":
        try:
            import winreg
            key = winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize",
            )
            value, _ = winreg.QueryValueEx(key, "AppsUseLightTheme")
            winreg.CloseKey(key)
            return "light" if int(value) else "dark"
        except Exception:
            return "dark"
    if sys.platform == "darwin":
        try:
            import subprocess
            out = subprocess.run(
                ["defaults", "read", "-g", "AppleInterfaceStyle"],
                capture_output=True, text=True, timeout=5,
            )
            return "dark" if "dark" in (out.stdout or "").lower() else "light"
        except Exception:
            return "dark"
    # Linux / 其他：优先读 GTK 的深色偏好开关，再退而看主题名。
    # 此前只读 gtk-theme，可很多深色主题名里并不带 "dark" 字样，
    # 于是跟随系统这一档在 Linux 上基本是失效的。
    try:
        import subprocess
        out = subprocess.run(
            ["gsettings", "get", "org.gnome.desktop.interface",
             "gtk-application-prefer-dark-theme"],
            capture_output=True, text=True, timeout=5,
        )
        if (out.stdout or "").strip().lower() == "true":
            return "dark"
        out2 = subprocess.run(
            ["gsettings", "get", "org.gnome.desktop.interface", "gtk-theme"],
            capture_output=True, text=True, timeout=5,
        )
        return "dark" if "dark" in (out2.stdout or "").lower() else "light"
    except Exception:
        return "dark"


def resolve_theme(cfg):
    """把配置里的主题值解析成实际生效的 dark / light。"""
    mode = (cfg.get("theme") or config_mod.DEFAULT_THEME)
    if mode == "auto":
        return detect_system_theme()
    return mode if mode in ("light", "dark") else "dark"


# 与 web/css/style.css 中两套主题的 --bg 保持一致，用于窗口创建时的底色
_WINDOW_BG = {"dark": "#0F1418", "light": "#F4F6F8"}


def main():
    if sys.platform != "win32":
        print("[警告] 当前不是 Windows，窗口抓取功能不可用；GUI 仍可启动用于界面测试。")
    try:
        import webview
    except ImportError:
        print("缺少依赖：请先执行 pip install pywebview")
        sys.exit(1)
    cfg, errors = config_mod.load_config()
    for e in errors:
        print(f"[提示] {e}")
    index_html = _resource_path(os.path.join("web", "index.html"))
    # 先定主题再建窗口：窗口底色用主题对应的 --bg，避免启动时闪一下反色
    theme = resolve_theme(cfg)

    def get_window():
        try:
            return webview.windows[0]
        except Exception:
            return None

    bridge = Bridge(get_window)
    window = webview.create_window(
        "Vigil",
        url=index_html,
        js_api=bridge,
        width=1180,
        height=780,
        min_size=(960, 640),
        background_color=_WINDOW_BG[theme],
    )

    # 页面就绪后立刻套用主题：此时 CSS 已加载，
    # 由前端按配置（含 auto）决定最终配色，Python 侧只负责不闪屏。
    def _apply_saved_theme():
        try:
            bridge._report_raw("apply_theme", {"theme": cfg.get("theme") or "auto"})
        except Exception:
            pass

    window.events.loaded += _apply_saved_theme
    webview.start()


if __name__ == "__main__":
    main()
