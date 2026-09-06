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

    # ---- JS 可调用的接口 ----
    def list_windows(self):
        """返回 [{hwnd, title}]，按窗口 Z 序。非 Windows 时返回空列表。"""
        try:
            wins = window_capture.list_windows()
        except Exception as e:
            self._push_status("error", f"窗口枚举不可用：{e}")
            return []
        return [{"hwnd": w.hwnd, "title": w.title} for w in wins]

    def start_monitor(self, hwnd):
        """开始巡检指定窗口。hwnd 由前端从 list_windows 传入。"""
        with self._lock:
            if self._monitor is not None:
                return {"ok": False, "error": "已在巡检中"}
            cfg, _errors = config_mod.load_config()
            if not cfg.get("api_key"):
                return {"ok": False, "error": "config.json 未填写 API Key，请先在设置中填写。"}
            win = self._find_window(hwnd)
            if win is None:
                return {"ok": False, "error": "窗口不存在或已关闭"}
            log_dir = config_mod.ensure_dirs(cfg)
            notifier.init(log_dir)
            monitor = Monitor(
                cfg,
                win,
                verbose=False,
                preview_cb=self._push_frame,
            )
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
            monitor.close()
            self._monitor = None
            self._stop_event = None
            self._thread = None
            return {"ok": True}

    def test_once(self, hwnd):
        """选窗口后立即抓帧分析一次，返回判定结果。任何异常都转为结构化错误，不抛给 JS。"""
        monitor = None
        try:
            cfg, _errors = config_mod.load_config()
            if not cfg.get("api_key"):
                return {"ok": False, "error": "config.json 未填写 API Key，请先在设置中填写。"}
            win = self._find_window(hwnd)
            if win is None:
                return {"ok": False, "error": "窗口不存在或已关闭"}
            log_dir = config_mod.ensure_dirs(cfg)
            notifier.init(log_dir)
            monitor = Monitor(cfg, win, verbose=False, preview_cb=self._push_frame)
            result = monitor.analyze_once()
            result["ok"] = True
            return result
        except Exception as e:
            notifier.get_logger().exception("单次测试失败")
            return {"ok": False, "error": f"测试失败：{e}"}
        finally:
            if monitor is not None:
                monitor.close()

    def get_config(self):
        """返回当前配置（api_key 脱敏）。"""
        cfg, errors = config_mod.load_config()
        key = cfg.get("api_key", "")
        masked = ("****" + key[-4:]) if len(key) > 4 else ("****" if key else "")
        return {
            "ok": True,
            "config": {**cfg, "api_key": masked, "prompt": ""},  # 不把完整提示词回传前端
            "config_path": config_mod.writable_config_path(),
            "warnings": errors,
        }

    def save_config(self, data):
        """保存前端传来的配置。api_key 含 '*' 说明未修改，不覆盖。"""
        if not isinstance(data, dict):
            return {"ok": False, "error": "配置数据格式错误"}
        cfg, _ = config_mod.load_config()
        # 前端不应回传 prompt；保留现有值
        data.pop("prompt", None)
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
        key_ok = bool(cfg.get("api_key"))
        return {
            "ok": True,
            "path": path,
            "api_key_set": key_ok,
            "message": "配置已保存" + ("" if key_ok else "（尚未填写 API Key）"),
        }

    # ---- 告警历史 ----
    def _get_history(self):
        """按当前配置惰性创建/复用告警历史实例。"""
        cfg, _ = config_mod.load_config()
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
            cfg, _errors = config_mod.load_config()
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

    def _push_frame(self, frame, stats):
        """把预览帧推给前端（带统计数据）。"""
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
    # Linux / 其他：GTK 的深色偏好设置，读取失败则跟随环境变量兜底
    try:
        import subprocess
        out = subprocess.run(
            ["gsettings", "get", "org.gnome.desktop.interface", "gtk-theme"],
            capture_output=True, text=True, timeout=5,
        )
        return "dark" if "dark" in (out.stdout or "").lower() else "light"
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
