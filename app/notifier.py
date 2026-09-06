# -*- coding: utf-8 -*-
"""告警通知与日志模块。
Copyright (c) 2026 CaspianFlow. 版权所有。
Windows 上用 win11toast 调用系统原生 Toast 通知（兼容 Win10/11），
支持内联异常截图、Reminder 模式持续显示；不可用时降级为控制台打印。
所有事件写入本地日志（logs/monitor_YYYYMMDD.log）。
"""
import logging
import os
import threading
import time

try:
    from win11toast import toast  # type: ignore
    _HAS_TOAST = True
except ImportError:  # pragma: no cover - 非 Windows / 未安装
    toast = None
    _HAS_TOAST = False

# Toast 通知来源标识（打包后通知中心显示为 Vigil 而非 Python）
APP_ID = "Vigil"

_logger = None


def init(log_dir):
    """初始化日志。返回 (logger, log_file_path)。可重复调用，旧 handler 会被正确关闭。"""
    global _logger
    os.makedirs(log_dir, exist_ok=True)
    log_file = os.path.join(log_dir, f"monitor_{time.strftime('%Y%m%d')}.log")
    _logger = logging.getLogger("vigil")
    _logger.setLevel(logging.INFO)
    # 先关闭并移除旧 handler，避免重复初始化时 Windows 文件句柄泄漏
    for handler in list(_logger.handlers):
        try:
            handler.close()
        except Exception:
            pass
        _logger.removeHandler(handler)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
    fh = logging.FileHandler(log_file, encoding="utf-8")
    fh.setFormatter(fmt)
    _logger.addHandler(fh)
    ch = logging.StreamHandler()
    ch.setFormatter(fmt)
    _logger.addHandler(ch)
    return _logger, log_file


def get_logger():
    return _logger or logging.getLogger("vigil")


def _send_toast(title, message, image_path):
    """后台线程中发送 Toast，避免阻塞巡检循环。"""
    try:
        kwargs = {
            "title": title,
            "body": message,
            "scenario": "Reminder",  # 持续显示直到用户关闭
            "app_id": APP_ID,
        }
        if image_path and os.path.exists(image_path):
            kwargs["image"] = os.path.abspath(image_path)
        toast(**kwargs)
    except Exception as e:
        get_logger().warning("Toast 通知失败，降级为控制台输出：%s", e)
        print(f"\n[通知] {title}: {message}")


def notify(title, message, image_path=None):
    """弹出 Windows 原生 Toast 通知；不可用时打印到控制台。
    Args:
        title: 通知标题（醒目大字）
        message: 通知正文（异常类型 + 描述）
        image_path: 异常截图本地路径，作为内联图片显示在通知中
    """
    if _HAS_TOAST:
        # 后台线程发送，Toast 的 Reminder 模式会等待用户交互
        t = threading.Thread(
            target=_send_toast, args=(title, message, image_path), daemon=True
        )
        t.start()
        return
    print(f"\n[通知] {title}: {message}")
