# -*- coding: utf-8 -*-
"""WorkBuddy 积分挂件 · 平台抽象层（唯一入口）

把 v2.1.0 中散落在主程序里的 Windows 专属实现抽成统一接口，运行时按
sys.platform 选择后端模块，主程序只 import platform_adapter：

    Windows  → platform_win.py   完整保留 v2.1.0 Win32 行为
    macOS    → platform_mac.py   pystray 菜单栏托盘 + osascript 通知 + 自绘圆角
    Linux    → platform_linux.py pystray 托盘 + notify-send + 自绘圆角

统一接口（所有平台同名，返回语义一致）：
    PLATFORM_NAME / ui_font / num_font / system_light_theme / virtual_screen /
    apply_window_round_corners / refresh_window_decor / set_app_user_model_id /
    create_tray / notify / config_dir / round_corners_label

设计约束：任何平台能力的缺失都必须“失败静默降级”，绝不能让挂件因缺少某平台能力
而崩溃。托盘依赖 pystray+Pillow（可选），未安装时 add() 返回 False，主功能无损。
"""
import os
import sys
import queue

if sys.platform.startswith("win"):
    PLATFORM = "win"
    from platform_win import (PLATFORM_NAME, system_light_theme, virtual_screen,
                              apply_window_round_corners, refresh_window_decor,
                              set_app_user_model_id, ui_font, num_font,
                              create_tray, notify)
elif sys.platform == "darwin":
    PLATFORM = "mac"
    from platform_mac import (PLATFORM_NAME, system_light_theme, virtual_screen,
                              apply_window_round_corners, refresh_window_decor,
                              set_app_user_model_id, ui_font, num_font,
                              create_tray, notify)
else:
    PLATFORM = "linux"
    from platform_linux import (PLATFORM_NAME, system_light_theme, virtual_screen,
                                apply_window_round_corners, refresh_window_decor,
                                set_app_user_model_id, ui_font, num_font,
                                create_tray, notify)


class NoopTray(object):
    """占位托盘：pystray 缺失 / 平台不支持时的零副作用实现。

    add() 只返回 False，绝不抛异常；show_balloon 仍尝试系统通知兜底，
    保证「托盘不可用」不影响挂件其余任何功能。
    """

    def __init__(self):
        self.q = queue.Queue()
        self.added = False

    def add(self):
        return False

    def remove(self):
        pass

    def show_balloon(self, title, text):
        try:
            return notify(title, text)
        except Exception:
            return False


def config_dir():
    """跨平台配置目录（每个平台一个稳定位置，可由 v2.1.0 原地升级覆盖）。"""
    if PLATFORM == "win":
        return os.path.join(os.environ.get("APPDATA")
                            or os.path.expanduser("~"), "WorkBuddyCreditWidget")
    if PLATFORM == "mac":
        return os.path.join(os.path.expanduser("~"), "Library",
                            "Application Support", "WorkBuddyCreditWidget")
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(
        os.path.expanduser("~"), ".config")
    return os.path.join(base, "WorkBuddyCreditWidget")


def ensure_role_markers():
    """托盘对象统一属性：q / added，便于主程序无差别访问。Noop 之外都不需要额外处理。"""
    return None


def round_corners_label():
    """当前平台圆角方案的一句话说明（文档 / 调试用）。"""
    return {
        "win": "DWM 系统圆角（Win11）或 SetWindowRgn 1bit 回退",
        "mac": "无系统级圆角，自绘圆角卡片 + 圆角描边近似",
        "linux": "无 WM 无关圆角，自绘圆角卡片 + 圆角描边近似",
    }.get(PLATFORM, "自绘近似")
