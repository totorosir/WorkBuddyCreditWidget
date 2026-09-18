# -*- coding: utf-8 -*-
"""Linux 专属后端（platform_linux）。

能力映射（相对 Windows）：
  · 圆角：X11/Wayland 下 Tk 均无 WM 无关的窗口级圆角接口；-transparentcolor
    在部分 X11 合成器下可用但方式激进（按色值全局挖空），默认关闭。
    采用自绘圆角卡片 + 圆角描边近似，功能完全可用、绝不因圆角而崩。
  · 托盘：系统状态栏图标，pystray（可选依赖，缺失自动降级，不影响主功能）。
  · 通知：notify-send（多数桌面环境自带；缺失时静默降级）。
  · 多屏：Tk 主屏尺寸回退，副屏边界夹紧受限。
  · 深浅色：GNOME gsettings color-scheme；无 gsettings 时回退 False（深色）。
"""
import sys
import subprocess

import platform_unix as _ux

PLATFORM_NAME = "Linux"
FONT_UI = "Noto Sans CJK SC"
FONT_NUM = "DejaVu Sans"


def ui_font():
    return FONT_UI


def num_font():
    return FONT_NUM


def system_light_theme():
    try:
        r = subprocess.run(["gsettings", "get", "org.gnome.desktop.interface",
                            "color-scheme"], capture_output=True, text=True, timeout=5)
        if r.returncode == 0:
            return "dark" not in (r.stdout or "").lower()
    except Exception:
        pass
    try:
        r = subprocess.run(["gsettings", "get", "org.gnome.desktop.interface",
                            "gtk-theme"], capture_output=True, text=True, timeout=5)
        if r.returncode == 0:
            return "dark" not in (r.stdout or "").lower()
    except Exception:
        pass
    return False   # 无法探测时按深色处理


def virtual_screen(root=None):
    return _ux.tk_virtual_screen(root)


def apply_window_round_corners(root, bg=None, dark=None):
    return _ux.selfdraw_round_corners(root, bg, dark)


def refresh_window_decor(root, bg=None, dark=None):
    return None


def set_app_user_model_id():
    return None


def create_tray(icon_path=None):
    return _ux.create_pty_tray(icon_path=icon_path)


def notify(title, text):
    return _ux.notify(title, text)
