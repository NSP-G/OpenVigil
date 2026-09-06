#!/usr/bin/env bash
# Vigil · Linux 一键启动脚本
# Copyright (c) 2026 CaspianFlow. 版权所有。
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

echo "=== Vigil (Linux) ==="

command -v python3 >/dev/null || { echo "错误：未找到 python3，请先安装 Python 3.8+"; exit 1; }

MISSING=""
for pkg in python3-gi gir1.2-webkit2-4.0 libgtk-3-0; do
    dpkg -s "$pkg" >/dev/null 2>&1 || MISSING="$MISSING $pkg"
done

if [ -n "$MISSING" ]; then
    echo "缺少系统依赖：$MISSING"
    echo "请执行：sudo apt-get install -y python3-pip python3-venv$MISSING"
    read -p "是否现在自动安装？(y/N) " -n 1 -r; echo
    if [[ $REPLY =~ ^[Yy]$ ]]; then
        sudo apt-get update
        sudo apt-get install -y python3-pip python3-venv$MISSING
    else
        echo "已取消。安装依赖后重新运行本脚本。"
        exit 1
    fi
fi

if [ ! -d venv ]; then
    echo "创建虚拟环境…"
    python3 -m venv --system-site-packages venv
fi

# shellcheck disable=SC1091
source venv/bin/activate

pip install -q --upgrade pip
pip install -q -r requirements.txt

echo "启动 Vigil…"
exec python gui/main_gui.py
