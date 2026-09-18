# -*- mode: python ; coding: utf-8 -*-
"""WorkBuddyCreditWidget 跨平台打包 spec（PyInstaller）。

平台后端 platform_adapter 运行时按 sys.platform 选后端（win/mac/linux），
hiddenimports 全量收集三套后端，确保任意目标平台打包后 platform_adapter
都能正确的 import 对应模块（跨平台通用库，无副作用）。

图标/资源：
  · Windows  --icon=widget_icon.ico（构建脚本传参）
  · macOS    .app 图标在打包后用 pics/xxx.icns 再处理（见 build_mac.sh 说明）
  · Linux    无窗口图标约束，直接产出可执行 ELF
"""
import os
import sys

root = os.path.dirname(os.path.abspath(SPEC))

datas = [
    (os.path.join(root, "widget_title_logo.png"), "."),
]
if os.path.exists(os.path.join(root, "widget_icon.ico")):
    datas.append((os.path.join(root, "widget_icon.ico"), "."))

hiddenimports = [
    "platform_adapter",
    "platform_win",
    "platform_mac",
    "platform_linux",
    "platform_unix",
]

a = Analysis(
    ["workbuddy_credit_widget.py"],
    pathex=[root],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["pytest", "setuptools", "IPython", "matplotlib", "numpy"],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="WorkBuddyCreditWidget",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon="widget_icon.ico" if not sys.platform.startswith("linux") and
         os.path.exists(os.path.join(root, "widget_icon.ico")) else None,
)
