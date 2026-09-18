# -*- coding: utf-8 -*-
"""macOS / Linux 共享底座（platform_unix）——被 platform_mac / platform_linux 复用。

覆盖三大类平台差异的替代实现：
  1. 托盘：pystray（可选依赖）。未安装时自动降级为 NoopTray，挂件正常启动，
     只是没有托盘图标，功能（窗口置顶、刷新、签到、派猫）完全不受影响。
  2. 系统通知：macOS 用 osascript（系统自带）；Linux 用 notify-send。
  3. 窗口圆角：两平台 Tk 均无系统级窗口裁剪，采用「自绘圆角卡片 + 圆角描边」近似，
     不崩、不依赖外部 WM 工具。

事件模型：托盘与挂件主线程通过 queue.Queue 通信，事件为字符串：
    "left" / "right" / "restore" / "refresh" / "toggle" / "quit"
（"right" 在 mac/linux 语义=弹出菜单由 pystray 原生处理；挂件只消费其余菜单动作。）
"""
import os
import sys
import subprocess
import threading
import queue

PLATFORM_NAME = "Unix"


# ---------------- 通知 ----------------
def _sh_escape(s):
    return (s or "").replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")


def notify(title, text):
    """系统通知（尽力而为）。成功返回 True，失败 / 无工具返回 False，绝不抛出。"""
    try:
        if sys.platform == "darwin":
            script = 'display notification "%s" with title "%s"' % (
                _sh_escape(text), _sh_escape(title))
            r = subprocess.run(["osascript", "-e", script],
                               capture_output=True, timeout=8)
            return r.returncode == 0
        r = subprocess.run(["notify-send", _sh_escape(title), _sh_escape(text)],
                           capture_output=True, timeout=8)
        return r.returncode == 0
    except Exception:
        return False


# ---------------- 托盘（pystray，可选） ----------------
class PtyTray(object):
    """pystray 托盘：菜单栏（mac）/ 状态栏（linux）图标。

    add() 返回 bool 表示是否真正启用；pystray / Pillow 缺失时静默降级为 False，
    挂件其余功能正常。事件经 q 队列转发给主线程，线程安全。
    """

    def __init__(self, icon_path=None):
        self.q = queue.Queue()
        self.added = False
        self._icon = None
        self._thread = None
        self._icon_path = icon_path

    def _emit(self, kind):
        def cb(icon, item):
            try:
                self.q.put(kind)
            except Exception:
                pass
        return cb

    def add(self):
        if self.added:
            return True
        try:
            import pystray  # noqa: F401
            from PIL import Image
        except Exception:
            return False   # 可选依赖缺失：无托盘，功能不受影响
        try:
            img = self._build_icon_image(Image)
            menu = pystray.Menu(
                pystray.MenuItem("显示主窗口", self._emit("restore"), default=True),
                pystray.MenuItem("立即刷新", self._emit("refresh")),
                pystray.MenuItem("展开 / 收缩", self._emit("toggle")),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("退出", self._emit("quit")),
            )
            self._icon = pystray.Icon(
                "workbuddy-credit-widget", img, "WorkBuddy积分助手", menu=menu)
            self._thread = threading.Thread(target=self._icon.run, daemon=True)
            self._thread.start()
            self.added = True
            return True
        except Exception:
            self.added = False
            return False

    def _build_icon_image(self, Image):
        path = self._icon_path
        if path and os.path.isfile(path):
            try:
                return Image.open(path).convert("RGBA")
            except Exception:
                pass
        try:
            im = Image.new("RGBA", (64, 64), (37, 99, 235, 255))
            return im
        except Exception:
            return None

    def remove(self):
        icon = self._icon
        if icon is not None:
            try:
                icon.stop()
            except Exception:
                pass
            self._icon = None
        self.added = False

    def show_balloon(self, title, text):
        # pystray 无系统原生气泡 API：转系统通知
        try:
            return notify(title, text)
        except Exception:
            return False


def create_pty_tray(icon_path=None):
    return PtyTray(icon_path=icon_path)


# ---------------- 圆角 / 多屏 / 深浅色（通用降级） ----------------
def selfdraw_round_corners(root, bg=None, dark=None):
    """mac/linux：无系统级窗口圆角。返回 "selfdraw"，由主程序自绘圆角描边近似。
    仅执行必要的 idletasks 以对齐尺寸，不做任何平台特定调用，绝不抛出。"""
    try:
        root.update_idletasks()
    except Exception:
        pass
    return "selfdraw"


def tk_virtual_screen(root):
    """多屏边界：Tk 主屏尺寸回退。mac/linux 上 Tk 的 winfo_screenwidth/height
    只反映“主显示器”，多显示器时无法覆盖副屏——属于已知限制（见文档），
    挂件仍可拖动到副屏，只是边界夹紧以主屏为准。"""
    try:
        w, h = root.winfo_screenwidth(), root.winfo_screenheight()
        return [0, 0, int(w), int(h)]
    except Exception:
        return [0, 0, 1920, 1080]
