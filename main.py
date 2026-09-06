#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Vigil - 命令行入口。

Copyright (c) 2026 CaspianFlow. 版权所有。

用法：
    python main.py                 # 列出窗口并选择，随后持续巡检
    python main.py --list          # 只列出当前窗口
    python main.py --once          # 选窗口后立即抓帧分析一次（联调用）
    python main.py --title 关键字   # 按标题关键字自动选窗口并持续巡检
"""
import argparse
import sys

from app import config as config_mod
from app import notifier, window_capture
from app.monitor import Monitor


def _pick_window(console=True):
    """枚举窗口并让用户选择，返回 WindowInfo。"""
    windows = window_capture.list_windows()
    if not windows:
        print("未发现任何可见窗口。")
        sys.exit(1)

    print(f"共发现 {len(windows)} 个窗口，请输入序号选择要盯的监控窗口：")
    for i, w in enumerate(windows):
        print(f"  [{i + 1:>3}] {w.title}  (hwnd={w.hwnd})")

    while True:
        try:
            raw = input("\n序号 > ").strip()
            idx = int(raw) - 1
            if 0 <= idx < len(windows):
                return windows[idx]
            print("序号超出范围，请重试。")
        except (ValueError, EOFError):
            print("输入无效，请重试。")


def main():
    parser = argparse.ArgumentParser(description="Vigil：把已打开窗口的画面交给视觉大模型巡检")
    parser.add_argument("--list", action="store_true", help="只列出当前窗口")
    parser.add_argument("--once", action="store_true", help="选窗口后立即抓帧分析一次后退出")
    parser.add_argument("--title", type=str, default=None, help="按标题关键字自动选窗口")
    parser.add_argument("--config", type=str, default=None, help="配置文件路径")
    args = parser.parse_args()

    cfg, errors = config_mod.load_config(args.config)
    for e in errors:
        print(f"[提示] {e}")

    # 非 Windows 环境直接给出明确提示
    if sys.platform != "win32":
        print("错误：本工具仅支持 Windows（需要 pywin32 调用系统窗口 API）。")
        sys.exit(1)

    if args.list:
        for i, w in enumerate(window_capture.list_windows()):
            print(f"  [{i + 1:>3}] {w.title}")
        return

    # 选择窗口
    if args.title:
        win = window_capture.window_by_title(args.title)
        if win is None:
            print(f"未找到标题含「{args.title}」的窗口，请改用交互选择。")
            sys.exit(1)
        print(f"已按标题匹配窗口：{win.title}")
    else:
        win = _pick_window()

    if not cfg.get("api_key"):
        print("错误：config.json 中未填写 api_key，请先配置。")
        sys.exit(1)

    log_dir = config_mod.ensure_dirs(cfg)
    notifier.init(log_dir)
    print(f"日志目录：{log_dir}")

    monitor = Monitor(cfg, win)

    if args.once:
        print("正在抓帧并分析一次…")
        result = monitor.analyze_once()
        print("结果：", result)
        return

    try:
        monitor.run_forever()
    except KeyboardInterrupt:
        print("\n收到退出信号，正在停止…")
        monitor.stop()


if __name__ == "__main__":
    main()
