# -*- coding: utf-8 -*-
"""macOS 专属后端（platform_mac）。

能力映射（相对 Windows）：
  · 圆角：Tk 在 macOS 上对 overrideredirect 无窗口级裁剪（无 DWMWA 等价物，
    -transparentcolor 在 macOS 被 Tk 忽略），采用自绘圆角卡片 + 圆角描边近似。
  · 托盘：菜单栏状态图标，pystray（可选依赖，缺失自动降级，不影响主功能）。
  · 通知：osascript `display notification`（系统自带，无需额外依赖）。
  · 多屏：Tk 主屏尺寸（winfo_screenwidth/height），副屏边界夹紧受限。
  · 深浅色：`defaults read -g AppleInterfaceStyle`。
"""
import sys
import subprocess

import platform_unix as _ux

PLATFORM_NAME = "macOS"
FONT_UI = "PingFang SC"
FONT_NUM = "Helvetica Neue"


def ui_font():
    return FONT_UI


def num_font():
    return FONT_NUM


def system_light_theme():
    """AppleInterfaceStyle == Dark 时返回 False（深色）。读取失败按深色处理。"""
    try:
        r = subprocess.run(["defaults", "read", "-g", "AppleInterfaceStyle"],
                           capture_output=True, text=True, timeout=5)
        if r.returncode == 0:
            style = (r.stdout or "").strip()
            if style.lower() == "dark":
                return False
            return True
        # 未设置该键（=浅色）时 defaults 返回非 0
        return True
    except Exception:
        return False


def virtual_screen(root=None):
    return _ux.tk_virtual_screen(root)


def apply_window_round_corners(root, bg=None, dark=None):
    return _ux.selfdraw_round_corners(root, bg, dark)


def refresh_window_decor(root, bg=None, dark=None):
    return None   # 无 DWM 非客户区，无需重设


def set_app_user_model_id():
    return None   # 无关概念


def create_tray(icon_path=None):
    return _ux.create_pty_tray(icon_path=icon_path)


def notify(title, text):
    return _ux.notify(title, text)
