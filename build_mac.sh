#!/usr/bin/env bash
# =====================================================================
#  WorkBuddyCreditWidget - macOS 打包脚本（PyInstaller .app）
#  在 macOS 上于 crossplatform\ 目录执行：bash build_mac.sh
#  产物：
#     - macOS 通用版（在 Apple Silicon M1/M2/M3 上运行）：
#       PyInstaller 若在 ARM 机器打包默认出 ARM 架构；若要兼容 Intel（x86_64），
#       见下方 --target-arch 说明。
#
#  前置：
#     1) 安装 Python 3.9+：建议用 pyenv 或官方安装包
#     2) pip install pyinstaller「可选：pystray pillow」，用于菜单栏托盘
#     3) 运行本脚本
#
#  架构说明（--target-arch）：
#     · 在 Apple Silicon（ARM）上打包默认产出 arm64 版；
#     · 需要同时兼容 Intel 时，用带 x86_64 的 Python 打包，或在两个架构
#       分别打包后用 lipo -create -output 合并为通用二进制；
#     · PyInstaller 的 --target-arch universal2 需配合合适工具链，
#       实践中更稳的是：分别打 arm64 与 x86_64 两枚 .app 再 lipo 合成内核二进制。
# =====================================================================
set -e
cd "$(dirname "$0")"

PYTHON="${PYTHON:-python3}"
"$PYTHON" -m PyInstaller --version >/dev/null 2>&1 || {
    echo "[错误] 未安装 PyInstaller，请先执行: $PYTHON -m pip install pyinstaller"
    exit 1
}

echo "[1/2] 执行 PyInstaller 打包（windowed .app）..."
"$PYTHON" -m PyInstaller --clean --noconfirm --windowed \
    --icon=widget_icon.icns WorkBuddyCreditWidget_cross.spec || \
    "$PYTHON" -m PyInstaller --clean --noconfirm --windowed \
             WorkBuddyCreditWidget_cross.spec

APP="dist/WorkBuddyCreditWidget.app"
if [ ! -d "$APP" ]; then
    echo "[失败] 打包未产出 .app"
    exit 1
fi

echo ""
echo "[完成] 产物：$PWD/$APP"

# ---------------- 签名与公证（可选但推荐，若要在别的 Mac 上分发） ----------------
# 下面的命令在本地开发者没有 Apple Developer ID 时请跳过（跳过仅影响 Gatekeeper）。
# 需要时取消注释并替换 {YOUR_DEVELOPER_ID}：
#
#   # 1) 对 .app 做 ad-hoc 或 Developer ID 签名（含嵌套二进制）
#   codesign --force --deep --sign "Developer ID Application: {YOUR_DEVELOPER_ID}" \
#            "$APP"
#   # 2) 生成 .dmg（用 hdiutil 或 create-dmg）
#   hdiutil create -volname WorkBuddyCreditWidget -srcfolder "$APP" \
#            -ov dist/WorkBuddyCreditWidget.dmg
#   # 3) 公证 dmg
#   xcrun notarytool submit dist/WorkBuddyCreditWidget.dmg \
#            --apple-id "YOU@EXAMPLE.COM" --team-id "TEAMID" \
#            --password "APP_SPECIFIC_PASSWORD" --wait
#   # 4) 公证通过后 staple
#   xcrun stapler staple dist/WorkBuddyCreditWidget.dmg
# =====================================================================
# 若上面的 codesign 命令被跳过，请至少在本地执行 ad-hoc 签名，避免部分
# 场景下 Gatekeeper 拦截：
#   codesign --force --deep --sign - "$APP"
