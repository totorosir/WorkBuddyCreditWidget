#!/usr/bin/env bash
# =====================================================================
#  WorkBuddyCreditWidget - Linux 打包脚本（PyInstaller onefile）
#  用法：bash build_linux.sh
#  产物：dist/WorkBuddyCreditWidget（ELF 可执行）
#
#  前置依赖（按发行版安装）：
#     Debian/Ubuntu:
#       sudo apt install -y python3 python3-tk python3-pip
#       python3 -m pip install pyinstaller     # 可选：pystray pillow（托盘）
#     Fedora:
#       sudo dnf install -y python3-tkinter python3-pip
#     Arch:
#       sudo pacman -S tk python-pip
#
#  说明：
#     · 无托盘（未装 pystray/Pillow）时程序正常启动，仅不显示状态栏图标；
#     · 圆角为自绘近似（X11/Wayland 下 Tk 无系统级圆角接口）；
#     · 通知依赖 notify-send（多数桌面环境自带）；缺失时静默降级。
# =====================================================================
set -e
cd "$(dirname "$0")"

PYTHON="${PYTHON:-python3}"
"$PYTHON" -c "import tkinter" 2>/dev/null || {
    echo "[错误] 缺少 tkinter，请先安装（Debian/Ubuntu: sudo apt install python3-tk）"
    exit 1
}
"$PYTHON" -m PyInstaller --version >/dev/null 2>&1 || {
    echo "[错误] 未安装 PyInstaller，请先执行: $PYTHON -m pip install pyinstaller"
    exit 1
}

echo "[1/2] 执行 PyInstaller 打包（windowed onefile）..."
"$PYTHON" -m PyInstaller --clean --noconfirm --windowed \
    WorkBuddyCreditWidget_cross.spec

BIN="dist/WorkBuddyCreditWidget"
[ -x "$BIN" ] || { echo "[失败] 打包未产出可执行文件"; exit 1; }
echo ""
echo "[完成] 产物：$PWD/$BIN"
echo "[提示] 直接运行: $PWD/$BIN"
