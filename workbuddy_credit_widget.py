# -*- coding: utf-8 -*-
"""WorkBuddy 积分桌面挂件（支持收缩 / 展开）

两个积分口径并列展示，互不混淆：
  · 累计领取 —— 签到活动累计领到的积分（整数，如 300），来自 checkin-activity-status
  · 积分余额 —— App 首屏额度（带两位小数，如 6,434.52），来自 get-user-resource-summary

收缩态：标题栏 + 「累计领取」大数字与「积分余额」并排 + 连续天数与今日记录。
展开态：积分余额卡（余额 / 总额度 / 已用额度）、三栏数据卡（累计领取 / 连续登录 /
        今日记录）、活跃地图（本月）、本周进度（7 格可视化）、本期活动与周期、
        活动剩余与全勤预计积分。

操作：
  · 左键拖动移动；双击任一数值复制该数值
  · 标题栏 ▾ / ▴ 收缩展开；↻ 立即刷新；× 退出
  · 右键菜单：立即刷新 / 始终置顶 / 透明度 / 退出
  · 默认每 10 分钟自动刷新；位置、透明度、展开状态记在 widget_config.json

数据：同目录 acp_credit_client（只读）
      query_credits_detail() → 累计领取；query_balance() → 积分余额。
      两个接口在后台线程串行请求，主线程只负责渲染；任一失败只影响自己那块区域。
启动：Windows 双击 start_widget.bat（pythonw 无黑窗）；
macOS/Linux 见文档（pythonw workbuddy_credit_widget.py）。
"""
import os
import sys
import json
import time
import queue
import shutil
import math
import threading
import tkinter as tk
from datetime import datetime

import platform_adapter as platform  # noqa: E402 平台抽象层

# 平台字体族（Windows=微软雅黑/Segoe UI；macOS=PingFang SC/Helvetica Neue；
# Linux=Noto Sans CJK SC/DejaVu Sans）。Tk 找不到指定族时回退系统默认字体，不崩。
FONT_UI = platform.ui_font()
FONT_NUM = platform.num_font()


def _app_base_dir():
    """程序所在目录（frozen 时取 exe 目录，源码运行时取脚本目录）。"""
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


BASE_DIR = _app_base_dir()
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from acp_credit_client import (query_credits_detail, query_balance,  # noqa: E402
                               load_credentials, find_auth_file, _post_json)

# 派猫猫旅行客户端：接口来自 buddy_station.py（旅行走 www.workbuddy.cn 域、无 /v2 前缀、
# 仅 Bearer Token）。写接口（depart/claim）受调度与状态机管控。
import buddy_travel_client as btc  # noqa: E402

# 配置写到平台稳定目录（Windows=%APPDATA% / macOS=~/Library/Application Support/ / 
# Linux=~/.config），由 platform_adapter.config_dir() 决定；onefile 模式下 
# sys._MEIPASS 是每次都变的临时目录，不能用来存放需要持久化的配置。
CONFIG_DIR = platform.config_dir()
CONFIG_NAME = "widget_config.json"
CONFIG_PATH = os.path.join(CONFIG_DIR, CONFIG_NAME)
LEGACY_CONFIG_PATH = os.path.join(BASE_DIR, CONFIG_NAME)


def _prepare_config_path():
    """确保配置目录存在；若旧版配置在程序目录则迁移到平台配置目录（旧文件保留）。"""
    try:
        os.makedirs(CONFIG_DIR, exist_ok=True)
    except Exception:
        pass
    if not os.path.isfile(CONFIG_PATH) and os.path.isfile(LEGACY_CONFIG_PATH):
        try:
            shutil.copy2(LEGACY_CONFIG_PATH, CONFIG_PATH)
        except Exception:
            pass
    return CONFIG_PATH


_prepare_config_path()

AUTO_REFRESH_MS = 10 * 60 * 1000

# ---------------- 主题（auto / light / dark） ----------------
# 三套调色板；模块级颜色变量（BG/BG_CARD/...）始终指向“当前生效主题”的取值，
# 所有控件创建与渲染都引用这些变量，切主题时把变量换成新调色板再重建 UI 即可。
THEMES_DARK = {
    "BG": "#1B1F27", "BG_CARD": "#232834", "BG_SOFT": "#2A3140",
    "FG": "#EAF0F9", "FG_DIM": "#B9C0D6", "FG_FAINT": "#8895AD",
    "ACCENT": "#6FE3D6", "ACCENT_DIM": "#2C4A47",
    "WARN": "#F0B27A", "ERR": "#FC8181", "LINE": "#333B4B",
    "MENU_ACTIVE": "#2F3746", "ACCENT_TXT": "#10221F", "DARK": True,
}
THEMES_LIGHT = {
    "BG": "#F4F6FB", "BG_CARD": "#FFFFFF", "BG_SOFT": "#E8EBF2",
    "FG": "#1D2330", "FG_DIM": "#4A5268", "FG_FAINT": "#7C879C",
    "ACCENT": "#0E9C90", "ACCENT_DIM": "#D5F1EC",
    "WARN": "#C07007", "ERR": "#D64545", "LINE": "#D6DBE6",
    "MENU_ACTIVE": "#E4E9F4", "ACCENT_TXT": "#FFFFFF", "DARK": False,
}
THEMES = {"dark": THEMES_DARK, "light": THEMES_LIGHT}


def _system_light_theme():
    """系统深浅色（平台抽象层接管：Windows=winreg / macOS=defaults / Linux=gsettings）。
    返回 True=浅色 / False=深色；读取失败按深色处理。"""
    try:
        return platform.system_light_theme()
    except Exception:
        return False


def _resolve_theme(theme):
    """theme=auto 时转成实际 light / dark。"""
    if theme == "light":
        return "light"
    if theme == "dark":
        return "dark"
    return "light" if _system_light_theme() else "dark"


def _apply_palette(theme):
    """把模块级颜色变量切到指定主题实际调色板，返回调色板 dict。"""
    pal = THEMES[_resolve_theme(theme)]
    for k, v in pal.items():
        if k == "DARK":
            continue
        globals()[k] = v
    global NC_DARK
    NC_DARK = bool(pal.get("DARK", True))
    return pal


# 模块加载时默认调色板：跟随系统（读不到按深色）。后续 __init__ 会按配置再设一次。
NC_DARK = True
_apply_palette("auto")

# 窗口宽度固定；高度不再写死，改为按内容实际请求高度计算（见 _fit_window），
# 这两个值只作为下限保护，防止异常布局把窗口压成一条缝。
W_COL, W_EXP = 268, 306
H_COL_MIN, H_EXP_MIN = 150, 380
# 底部安全留白：保证「更新于 …」整行完整可见，不被窗口下边界裁掉
SAFE_PAD = 12
# 版本号：修改只需改这一处（展开态底部信息行最右侧展示）
VERSION = "v2.1.0"

DEFAULT_CFG = {"x": None, "y": None, "alpha": 0.92, "topmost": True,
               "expanded": False, "theme": "auto", "auto_checkin": False,
               "auto_dispatch": False}

WEEK_LABELS = ["一", "二", "三", "四", "五", "六", "日"]


def load_cfg():
    cfg = dict(DEFAULT_CFG)
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg.update(json.load(f))
    except Exception:
        pass
    return cfg


def save_cfg(cfg):
    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


ICON_NAME = "widget_icon.ico"


def _resolve_icon_path():
    """定位随包的 ico：frozen 时从 sys._MEIPASS 取，源码运行时从脚本目录取。"""
    try:
        if getattr(sys, "frozen", False):
            base = getattr(sys, "_MEIPASS", None) or BASE_DIR
            p = os.path.join(base, ICON_NAME)
            if os.path.isfile(p):
                return p
        p = os.path.join(BASE_DIR, ICON_NAME)
        if os.path.isfile(p):
            return p
    except Exception:
        pass
    return None


TITLE_LOGO_NAME = "widget_title_logo.png"


def _resolve_title_logo_path():
    """定位标题栏 logo 的 PNG：frozen 时从 sys._MEIPASS 取，源码运行时从脚本目录取。"""
    try:
        if getattr(sys, "frozen", False):
            base = getattr(sys, "_MEIPASS", None) or BASE_DIR
            p = os.path.join(base, TITLE_LOGO_NAME)
            if os.path.isfile(p):
                return p
        p = os.path.join(BASE_DIR, TITLE_LOGO_NAME)
        if os.path.isfile(p):
            return p
    except Exception:
        pass
    return None


def _apply_window_icon(root):
    """设置窗口图标（影响任务栏 / Alt-Tab / 标题栏）。失败静默跳过，不影响主功能。"""
    try:
        platform.set_app_user_model_id()   # Windows 设 AppUserModelID；其它平台 no-op
    except Exception:
        pass
    ico = _resolve_icon_path()
    if not ico:
        return None
    try:
        root.iconbitmap(default=ico)
    except Exception:
        try:
            root.iconbitmap(ico)
        except Exception:
            pass
    try:
        root.wm_iconbitmap(default=ico)
    except Exception:
        pass
    return ico


# 圆角：窗口外框半径与卡片半径
#   RADIUS_WIN_DWM —— Win11 系统级圆角（DWM 裁剪，边缘带抗锯齿），半径跟随系统 8px
#   RADIUS_WIN_RGN —— 回退方案（SetWindowRgn，1bit 无抗锯齿）用更大半径弱化锯齿
RADIUS_WIN_DWM = 8
RADIUS_WIN_RGN = 16
RADIUS_WIN = RADIUS_WIN_RGN
RADIUS_CARD = 10



def _dbg(msg):
    """调试日志：仅当设了 WIDGET_DEBUG_DIR 时写文件（默认完全关闭）。"""
    d = os.environ.get("WIDGET_DEBUG_DIR")
    if not d:
        return
    try:
        with open(os.path.join(d, "corner_debug.log"), "a", encoding="utf-8") as f:
            f.write("[%s] %s\n" % (time.strftime("%H:%M:%S"), msg))
    except Exception:
        pass



# 三栏卡片统一字体（数值 / 标签各一个常量，三栏共用，保证严格一致）
STAT_VAL_FONT = (FONT_NUM, 14, "bold")
STAT_TITLE_FONT = (FONT_UI, 8)


def _round_rect_points(w, h, r, steps=18):
    """圆角矩形的密集采样点（每角 steps 段折线逼近圆弧）。

    Tk Canvas 底层是 GDI，arc/矩形只有 1bit 硬边缘，圆角处是明显阶梯。用多段
    折线逼近圆弧后，边缘变成细密斜线，锯齿在视觉上被大幅削弱（steps 越大越平滑）。
    """
    r = int(max(1, min(r, (w - 2) // 2, (h - 2) // 2)))
    x1, y1, x2, y2 = 0.0, 0.0, float(w - 1), float(h - 1)
    # (圆心x, 圆心y, 起始角)：右下 → 左下 → 左上 → 右上，屏幕坐标 y 向下
    corners = ((x2 - r, y2 - r, 0.0),
               (x1 + r, y2 - r, 90.0),
               (x1 + r, y1 + r, 180.0),
               (x2 - r, y1 + r, 270.0))
    pts = []
    for cx, cy, a0 in corners:
        for i in range(steps + 1):
            a = math.radians(a0 + 90.0 * i / steps)
            pts.append(cx + r * math.cos(a))
            pts.append(cy + r * math.sin(a))
    return pts


def _round_rect(canvas, w, h, r, fill, outline, steps=18):
    """在 Canvas 上绘制圆角矩形（填充 + 描边），四角用密集折线抗锯齿。"""
    canvas.delete("rr")
    if w < 4 or h < 4:
        return
    pts = _round_rect_points(w, h, r, steps)
    if fill:
        canvas.create_polygon(pts, fill=fill, outline="", smooth=0, tags="rr")
    if outline:
        canvas.create_polygon(pts, fill="", outline=outline, width=1,
                              smooth=0, joinstyle="round", tags="rr")


class RoundCard(tk.Frame):
    """圆角卡片：底层 Canvas 画圆角矩形（背景 + 描边），内容放在内缩后的 Frame 里。

    内容区左右各内缩 radius、上下内缩 1px，正好避开四角的圆弧，不会盖住描边。
    """

    def __init__(self, master, height=None, fill=None, outline=None,
                 radius=None, pad=None, pady=None):
        # height=None → 卡片高度由内容撑开（自适应，保证文字不被裁切）
        # 颜色默认参数不在此处绑定：BG_CARD/LINE 是模块级主题变量，切主题时会被
        # _apply_palette 换成新值。若在 def 行绑定旧值，切主题重建 UI 后卡片仍用
        # 旧调色板（深色主题下残留浅色块）。统一落到 None，构造时取“当前生效”变量。
        if fill is None:
            fill = BG_CARD
        if outline is None:
            outline = LINE
        if radius is None:
            radius = RADIUS_CARD
        tk.Frame.__init__(self, master, height=height, bg=fill)
        self._fill, self._outline, self._r = fill, outline, radius
        self.pad = radius if pad is None else pad
        self._pady = (radius // 2 + 2) if pady is None else pady
        self._cv = tk.Canvas(self, bg=BG, bd=0, highlightthickness=0)
        self._cv.place(x=0, y=0, relwidth=1, relheight=1)
        self.body = tk.Frame(self, bg=fill)
        if height:
            # 固定高度（分隔线这类无内容装饰条）：place 不参与尺寸传播
            self.body.place(x=self.pad, y=1)
        else:
            # 自适应：body 用 pack，卡片高度 = 内容高度 + 上下内缩
            self.body.pack(fill="both", expand=True, padx=self.pad, pady=self._pady)
        self.bind("<Configure>", self._on_cfg)

    def _on_cfg(self, _e=None):
        w, h = self.winfo_width(), self.winfo_height()
        if w < 2 or h < 2:
            return
        _round_rect(self._cv, w, h, self._r, self._fill, self._outline)
        if self.body.winfo_manager() == "place":
            self.body.place_configure(width=max(1, w - 2 * self.pad),
                                      height=max(1, h - 2))


def fmt_int(n):
    try:
        return "{:,}".format(int(n))
    except Exception:
        return "—"


class Widget(object):

    def __init__(self, root):
        self.root = root
        self.cfg = load_cfg()
        self.busy = False
        self.last = None              # 累计领取（签到积分）
        self.last_balance = None      # 积分余额（额度）
        self._result_q = queue.Queue()  # 后台线程 → 主线程的结果通道（数据）
        self._signin_q = queue.Queue()  # 后台线程 → 主线程的结果通道（签到）
        # ------- 派猫猫旅行状态（v1.2.0 新增；buddy 卡已移除，不再抓取 buddy 信息） -------
        self._travel_q = queue.Queue()      # 只读查询（status/config）→ 主线程
        self._travel_cmd_q = queue.Queue()  # 写接口 / 自动流程结果 → 主线程
        self._travel_fetching = False        # 只读查询是否进行中（防并发）
        self._travel_fetch_started = 0.0     # 最近一次只读查询发起时刻（看门狗防锁死）
        self._travel_cmd_busy = False        # 写接口是否进行中（防重入）
        # 历史遗留：buddy 缓存状态（保留初始化，缓存文件不主动删；已无抓取与展示调用）
        self._buddy_loaded_ok = False        # 不再使用
        self._buddy_photo = None             # 不再使用
        # 旅行状态机：state=idle(未派遣) / traveling(倒计时) / arrived(可领取)；限流置位
        self.travel = {"state": None, "arrive_at": None, "server_now": None,
                       "daily_limit_reached": False, "location": None,
                       "reward_credit": None, "last_error": None,
                       "buddy_id": None, "record_id": None}
        # 历史遗留：buddy 缓存数据（不再读取）
        self.buddy = {"name": None, "thumbnail_url": None, "error": None}
        self._travel_recv_ts = 0.0     # 收到 traveling 状态时的时间.time() 基准
        self._travel_server_now = 0.0  # 收到 traveling 状态时服务端 server_now
        self._travel_arrive_at = 0.0   # 收到 traveling 状态时的 arrive_at
        # ---- 演示模式（仅验证截图用，正式无影响）：WIDGET_TRAVEL_DEMO=ready|traveling|arrived|done ----
        self._travel_demo = os.environ.get("WIDGET_TRAVEL_DEMO", "").strip()
        self.anim_i = 0
        self._anim_on = False
        self._signin_busy = False     # 签到请求是否进行中（防重入）
        self._retry_scheduled = False # 本轮自动刷新失败后是否已安排 3 秒重试（防多重重试）
        self.expanded = bool(self.cfg.get("expanded", False))
        self.auto_checkin = bool(self.cfg.get("auto_checkin", False))
        self.auto_dispatch = bool(self.cfg.get("auto_dispatch", False))  # 自动派遣开关（持久化）
        # 系统托盘（平台抽象层：Windows=Shell_NotifyIcon；macOS/Linux=pystray 可选）
        self.tray = platform.create_tray(
            icon_path=_resolve_icon_path() or _resolve_title_logo_path())
        self._corner_mode = ""   # "dwm"=系统圆角 / "rgn"=1bit region / "selfdraw"=自绘圆角

        # 主题：读配置并切调色板（auto 会解析成系统当前深浅色）
        self.theme = self.cfg.get("theme", "auto")
        if self.theme not in ("auto", "light", "dark"):
            self.theme = "auto"
        _apply_palette(self.theme)

        root.overrideredirect(True)
        root.attributes("-topmost", bool(self.cfg.get("topmost", True)))
        # 100% 不透明时不设 -alpha：Tk 一旦设过就给窗口加 WS_EX_LAYERED，而 DWM
        # 不给 layered 窗口裁系统圆角，只能退回 1bit region（边缘有锯齿）
        _alpha = float(self.cfg.get("alpha", 0.92))
        if _alpha < 0.999:
            root.attributes("-alpha", _alpha)
        root.configure(bg=BG)
        _apply_window_icon(root)

        self._build_ui()
        self._bind_events()
        self._apply_mode(initial=True)
        self.refresh_async()
        self._tick()
        # 无边框自绘窗口仍保留标题，供外部脚本按窗口名精确查找（截图像素/窗口定位用）
        try:
            root.title("WorkBuddyCreditWidget")
        except Exception:
            pass
        # 启动即拉取旅行状态（只读，不触发任何写接口）
        self.root.after(400, lambda: self._start_travel_fetch(force=True))

    # ---------------- UI 构建 ----------------
    def _build_ui(self):
        r = self.root
        # 窗口圆角描边：与 SetWindowRgn 圆角同半径；先创建，内容后创建自然盖在其上
        self.border = tk.Canvas(r, bg=BG, bd=0, highlightthickness=0)
        self.border.place(x=0, y=0, relwidth=1, relheight=1)
        self.border.bind("<Configure>", self._draw_win_border)

        outer = tk.Frame(r, bg=BG)
        outer.pack(fill="both", expand=True)

        # 标题栏
        bar = tk.Frame(outer, bg=BG_CARD)
        bar.pack(fill="x")
        # 标题栏左侧 logo（无边框自绘窗口，窗口图标不会自动出现，需自己画）
        self.title_logo_img = None   # 必须保持强引用，否则被 GC 后图标不显示
        logo_path = _resolve_title_logo_path()
        if logo_path:
            try:
                img = tk.PhotoImage(file=logo_path)
                try:
                    # 32 → 16，避免把标题栏撑高、挤压固定高度的主体区域
                    img = img.subsample(2, 2)
                except Exception:
                    pass
                self.title_logo_img = img   # 强引用，防止被 GC 后图标不显示
                tk.Label(bar, image=self.title_logo_img, bg=BG_CARD).pack(
                    side="left", padx=(8, 5), pady=5)
            except Exception:
                self.title_logo_img = None

        tk.Label(bar, text="WorkBuddy积分助手", bg=BG_CARD, fg=FG_DIM,
                 font=(FONT_UI, 9), anchor="w").pack(side="left", padx=(0, 0), pady=5)

        self.close_btn = tk.Label(bar, text="×", bg=BG_CARD, fg=FG_DIM,
                                  font=(FONT_UI, 11), cursor="hand2")
        self.close_btn.pack(side="right", padx=(0, 8))
        self.close_btn.bind("<Button-1>", lambda e: self.quit())
        self._hover(self.close_btn, ERR)

        # 最小化到托盘按钮（─）：隐藏窗口并从任务栏移除，显示系统托盘图标
        self.min_btn = tk.Label(bar, text="─", bg=BG_CARD, fg=FG_DIM,
                                font=(FONT_UI, 11), cursor="hand2")
        self.min_btn.pack(side="right", padx=(0, 2))
        self.min_btn.bind("<Button-1>", lambda e: self._minimize_to_tray())
        self._hover(self.min_btn, ACCENT)

        self.refresh_btn = tk.Label(bar, text="↻", bg=BG_CARD, fg=FG_DIM,
                                    font=(FONT_UI, 11), cursor="hand2")
        self.refresh_btn.pack(side="right", padx=(0, 6))
        self.refresh_btn.bind("<Button-1>", lambda e: self.refresh_async())
        self._hover(self.refresh_btn, ACCENT)

        self.toggle_btn = tk.Label(bar, text="▾", bg=BG_CARD, fg=FG_DIM,
                                   font=(FONT_UI, 10), cursor="hand2")
        self.toggle_btn.pack(side="right", padx=(0, 6))
        self.toggle_btn.bind("<Button-1>", lambda e: self.toggle_mode())
        self._hover(self.toggle_btn, ACCENT)

        # 主题切换按钮：◐=跟随系统 / ☀=浅色 / ☾=深色，点击循环切换
        self.theme_btn = tk.Label(bar, text=self._theme_glyph(self.theme),
                                  bg=BG_CARD, fg=FG_DIM,
                                  font=(FONT_UI, 10), cursor="hand2")
        self.theme_btn.pack(side="right", padx=(0, 6))
        self.theme_btn.bind("<Button-1>", lambda e: self._cycle_theme())
        self.theme_btn.bind("<Enter>", lambda e: self._theme_hover(True))
        self.theme_btn.bind("<Leave>", lambda e: self._theme_hover(False))

        # 主体（收缩 / 展开两套）
        self.body = tk.Frame(outer, bg=BG)
        self.body.pack(fill="both", expand=True, padx=12, pady=(6, 4))
        self._build_collapsed()
        self._build_expanded()

        # 底部状态行
        self.foot_lbl = tk.Label(outer, text="正在读取…", bg=BG, fg=FG_FAINT,
                                 font=(FONT_UI, 8), anchor="w")
        self.foot_lbl.pack(fill="x", padx=12, pady=(2, 8))

        self._build_menu()

    def _draw_win_border(self, _e=None):
        """窗口外框的圆角描边（填充透明，只画边框），半径跟随当前圆角方案。"""
        try:
            w, h = self.root.winfo_width(), self.root.winfo_height()
            r = RADIUS_WIN_DWM if getattr(self, "_corner_mode", "") == "dwm" \
                else RADIUS_WIN_RGN
            _round_rect(self.border, w, h, r, "", LINE, steps=24)
        except Exception:
            pass

    def _apply_round_corners(self):
        """窗口圆角：交给平台抽象层。

        Windows：Win11 DWM 系统圆角（合成阶段裁剪、抗锯齿），回退 SetWindowRgn；
        macOS/Linux：Tk 无系统级窗口裁剪，返回 selfdraw，由 _draw_win_border 自绘
        圆角描边近似。尺寸变化（收缩 ↔ 展开）/ 主题切换后必须重新调用。异常静默。
        """
        try:
            self.root.update_idletasks()
            mode = platform.apply_window_round_corners(self.root, bg=BG, dark=NC_DARK)
            self._corner_mode = mode
            self._draw_win_border()
        except Exception:
            pass

    def _hover(self, w, color):
        w.bind("<Enter>", lambda e: w.configure(fg=color))
        w.bind("<Leave>", lambda e: w.configure(fg=FG_DIM))

    def _build_collapsed(self):
        self.f_col = tk.Frame(self.body, bg=BG)

        # 上排：累计领取（左）与积分余额（右）并列
        row = tk.Frame(self.f_col, bg=BG)
        row.pack(fill="x")

        left = tk.Frame(row, bg=BG)
        left.pack(side="left", anchor="n")
        tk.Label(left, text="累计领取", bg=BG, fg=FG_DIM,
                 font=(FONT_UI, 8), anchor="w").pack(fill="x")
        self.c_balance = tk.Label(left, text="—", bg=BG, fg=ACCENT,
                                  font=(FONT_NUM, 26, "bold"), anchor="w")
        self.c_balance.pack(fill="x")

        right = tk.Frame(row, bg=BG)
        right.pack(side="right", anchor="n")
        tk.Label(right, text="积分余额", bg=BG, fg=FG_DIM,
                 font=(FONT_UI, 8), anchor="e").pack(fill="x")
        self.c_bal_val = tk.Label(right, text="—", bg=BG, fg=FG,
                                  font=(FONT_NUM, 15, "bold"), anchor="e")
        self.c_bal_val.pack(fill="x")

        # 下排：连续天数 / 今日记录（仅签到口径）
        self.c_sub = tk.Label(self.f_col, text="正在读取…", bg=BG, fg=FG_DIM,
                              font=(FONT_UI, 9), anchor="w")
        self.c_sub.pack(fill="x")

    def _build_expanded(self):
        f = tk.Frame(self.body, bg=BG)
        self.f_exp = f

        # 头部信息行（仅展开态可见，随 f_exp 一起 pack/forget）：
        # 公众号文案贴左、版本号贴右，位于标题栏下方、积分余额卡上方。
        f_info = tk.Frame(f, bg=BG, height=17)
        f_info.pack(fill="x", pady=(0, 6))
        f_info.pack_propagate(False)
        tk.Label(f_info, text="公众号：龙猫科技说", bg=BG, fg=FG_FAINT,
                 font=(FONT_UI, 8), anchor="w").pack(side="left")
        tk.Label(f_info, text=VERSION, bg=BG, fg=FG_FAINT,
                 font=(FONT_UI, 8), anchor="e").pack(side="right")

        # 积分余额卡（额度口径，与下方签到口径区分）→ 纯积分信息（100% 宽，无 buddy 区）
        bal_card = RoundCard(f)
        bal_card.pack(fill="x", pady=(0, 8))
        bc = bal_card.body
        bc_balrow = tk.Frame(bc, bg=BG_CARD)
        bc_balrow.pack(fill="x")
        tk.Label(bc_balrow, text="积分余额（额度）", bg=BG_CARD, fg=FG_DIM,
                 font=(FONT_UI, 8), anchor="w").pack(fill="x")
        self.e_bal_val = tk.Label(bc_balrow, text="—", bg=BG_CARD, fg=ACCENT,
                                  font=(FONT_NUM, 20, "bold"), anchor="w")
        self.e_bal_val.pack(fill="x")
        self.e_bal_sub = tk.Label(bc_balrow, text="等待数据…", bg=BG_CARD, fg=FG_DIM,
                                  font=(FONT_UI, 8), anchor="w")
        self.e_bal_sub.pack(fill="x")

        # 三栏数据卡（签到口径）：grid + weight/uniform 保证三列严格等宽
        cards = tk.Frame(f, bg=BG)
        cards.pack(fill="x")
        for i in range(3):
            cards.grid_columnconfigure(i, weight=1, uniform="stat3", minsize=0)
        self.e_balance, _ = self._stat_card(cards, "累计领取", 0)
        self.e_streak, _ = self._stat_card(cards, "连续登录(天)", 1)
        self.e_today, self.e_today_lbl = self._stat_card(cards, "今日记录", 2)

        self._sep(f)

        # 活跃地图 + 本周进度（同一张圆角卡内）
        g1 = RoundCard(f)
        g1.pack(fill="x", pady=(0, 8))
        g = g1.body
        self._row_title(g, "活跃地图 (本月)", bg=BG_CARD)
        self.e_month = self._row_value(g, "—", bg=BG_CARD)
        self._row_title(g, "本周进度", bg=BG_CARD)
        self.e_week = self._row_value(g, "—", bg=BG_CARD)
        week_bar = tk.Frame(g, bg=BG_CARD)
        week_bar.pack(fill="x", pady=(2, 0))
        self.week_cells = []
        for i, name in enumerate(WEEK_LABELS):
            cell = tk.Frame(week_bar, bg=BG_CARD)
            cell.pack(side="left", expand=True)
            box = tk.Label(cell, text=name, bg=BG_SOFT, fg=FG_DIM, width=3,
                           font=(FONT_UI, 8))
            box.pack()
            self.week_cells.append(box)

        self._sep(f)

        # 本期活动
        g2 = RoundCard(f)
        g2.pack(fill="x")
        g = g2.body
        self._row_title(g, "本期活动", bg=BG_CARD)
        self.e_activity = self._row_value(g, "—", color=ACCENT, bg=BG_CARD)
        self.e_period = tk.Label(g, text="", bg=BG_CARD, fg=FG_FAINT,
                                 font=(FONT_UI, 8), anchor="w")
        self.e_period.pack(fill="x")
        self.e_remain = self._row_value(g, "—", color=WARN, bg=BG_CARD)

        # 派猫猫旅行卡：手动派遣状态机按钮 + 自动派遣开关（展开态，本期活动下方）
        self._sep(f)
        g4 = RoundCard(f)
        g4.pack(fill="x", pady=(0, 8))
        g = g4.body
        # 标题行：左「派猫猫旅行」，右「自动派遣」开关（样式随主题、状态持久化）
        trow = tk.Frame(g, bg=BG_CARD)
        trow.pack(fill="x")
        tk.Label(trow, text="派猫猫旅行", bg=BG_CARD, fg=FG_DIM,
                 font=(FONT_UI, 8, "bold"), anchor="w").pack(side="left")
        self.auto_dispatch_lbl = tk.Label(trow, text="自动派遣", bg=BG_CARD,
                                          fg=FG_DIM, font=(FONT_UI, 8))
        self.auto_dispatch_lbl.pack(side="right", padx=(10, 0))
        self.auto_dispatch_switch = tk.Label(trow, text="OFF", bg=BG_SOFT, fg=FG_DIM,
                                             font=(FONT_UI, 8, "bold"),
                                             cursor="hand2", padx=8, pady=2)
        self.auto_dispatch_switch.pack(side="right")
        self.auto_dispatch_switch.bind("<Button-1>",
                                       lambda e: self._toggle_auto_dispatch())
        # 派遣按钮行：未派遣=「立即派遣」可点；旅行中=倒计时（实时跳动）；到达=「立即领取」；结束=禁用
        brow = tk.Frame(g, bg=BG_CARD)
        brow.pack(fill="x", pady=(4, 0))
        self.dispatch_btn = tk.Label(brow, text="…", bg=BG_SOFT, fg=FG_DIM,
                                     font=(FONT_UI, 9, "bold"),
                                     cursor="hand2", padx=10, pady=3)
        self.dispatch_btn.pack(side="left")
        self.dispatch_btn.bind("<Button-1>", lambda e: self._on_dispatch_btn())
        self.travel_info = tk.Label(brow, text="读取中…", bg=BG_CARD, fg=FG_FAINT,
                                    font=(FONT_UI, 8), anchor="w")
        self.travel_info.pack(side="left", padx=(8, 0))
        self._update_dispatch_ui()

        # 签到卡：今日签到按钮 + 自动签到开关（展开态底部，排版对齐派猫猫旅行卡：
        # 标签右上角 / 按钮左侧 / 说明文字右侧同位置，字号间距配色一致）
        self._sep(f)
        g3 = RoundCard(f)
        g3.pack(fill="x")
        g = g3.body
        # 标题行：左「每日签到」，右「自动签到」开关（样式随主题、状态持久化）
        trow = tk.Frame(g, bg=BG_CARD)
        trow.pack(fill="x")
        tk.Label(trow, text="每日签到", bg=BG_CARD, fg=FG_DIM,
                 font=(FONT_UI, 8, "bold"), anchor="w").pack(side="left")
        self.auto_lbl = tk.Label(trow, text="自动签到", bg=BG_CARD,
                                 fg=FG_DIM, font=(FONT_UI, 8))
        self.auto_lbl.pack(side="right", padx=(10, 0))
        self.auto_switch = tk.Label(trow, text="OFF", bg=BG_SOFT, fg=FG_DIM,
                                    font=(FONT_UI, 8, "bold"),
                                    cursor="hand2", padx=8, pady=2)
        self.auto_switch.pack(side="right")
        self.auto_switch.bind("<Button-1>", lambda e: self._toggle_auto_checkin())
        # 按钮行：左「立即签到」可点 /「已签到」禁用；右侧说明文字（与派猫卡同位置）
        brow = tk.Frame(g, bg=BG_CARD)
        brow.pack(fill="x", pady=(4, 0))
        self.signin_btn = tk.Label(brow, text="…", bg=BG_SOFT, fg=FG_DIM,
                                   font=(FONT_UI, 9, "bold"),
                                   cursor="hand2", padx=10, pady=3)
        self.signin_btn.pack(side="left")
        self.signin_btn.bind("<Button-1>", lambda e: self._on_signin_click())
        self.signin_info = tk.Label(brow, text="今日已签到并完成领取，明日再来",
                                    bg=BG_CARD, fg=FG_FAINT,
                                    font=(FONT_UI, 8), anchor="w")
        self.signin_info.pack(side="left", padx=(8, 0))
        self._update_checkin_ui()

    def _stat_card(self, parent, title, col=0):
        """三栏小卡片：同一高度、同一字体常量、grid 等分列宽，保证严格一致。"""
        card = RoundCard(parent, pad=4)
        card.grid(row=0, column=col, sticky="nsew", padx=2)
        val = tk.Label(card.body, text="—", bg=BG_CARD, fg=FG, font=STAT_VAL_FONT)
        val.pack(pady=(6, 0))
        tk.Label(card.body, text=title, bg=BG_CARD, fg=FG_DIM, font=STAT_TITLE_FONT).pack()
        return val, card

    def _sep(self, parent):
        """圆角分隔线：用高 2px 的圆角矩形代替原来的直角 Frame。"""
        RoundCard(parent, height=2, fill=LINE, outline="", radius=1, pad=0).pack(
            fill="x", pady=7)

    def _row_title(self, parent, text, bg=None):
        if bg is None:
            bg = BG
        tk.Label(parent, text=text, bg=bg, fg=FG_DIM,
                 font=(FONT_UI, 8, "bold"), anchor="w").pack(fill="x")

    def _row_value(self, parent, text, color=None, bg=None):
        if color is None:
            color = FG
        if bg is None:
            bg = BG
        lbl = tk.Label(parent, text=text, bg=bg, fg=color,
                       font=(FONT_UI, 9), anchor="w", justify="left")
        lbl.pack(fill="x")
        return lbl

    def _build_menu(self):
        m = tk.Menu(self.root, tearoff=0, bg=BG_CARD, fg=FG, bd=0,
                    activebackground=MENU_ACTIVE, activeforeground=FG, relief="flat")
        m.add_command(label="立即刷新", command=self.refresh_async)
        m.add_command(label="展开 / 收缩", command=self.toggle_mode)
        m.add_separator()
        self.top_var = tk.BooleanVar(value=bool(self.cfg.get("topmost", True)))
        m.add_checkbutton(label="始终置顶", variable=self.top_var, command=self._toggle_top)
        am = tk.Menu(m, tearoff=0, bg=BG_CARD, fg=FG, bd=0,
                     activebackground=MENU_ACTIVE, activeforeground=FG)
        for a in (1.0, 0.92, 0.85, 0.75, 0.6):
            am.add_command(label="%d%%" % int(a * 100), command=lambda v=a: self._set_alpha(v))
        m.add_cascade(label="透明度", menu=am)
        # 主题子菜单：与标题栏按钮同源，即时生效并持久化
        tm = tk.Menu(m, tearoff=0, bg=BG_CARD, fg=FG, bd=0,
                     activebackground=MENU_ACTIVE, activeforeground=FG)
        self.theme_var = tk.StringVar(value=self.theme)
        for k in ("auto", "light", "dark"):
            tm.add_radiobutton(
                label={"auto": "跟随系统", "light": "浅色", "dark": "深色"}[k],
                value=k, variable=self.theme_var,
                command=lambda v=k: self.set_theme(v))
        m.add_cascade(label="主题", menu=tm)
        m.add_separator()
        m.add_command(label="退出", command=self.quit)
        self.menu = m

        # 托盘右键专用菜单：含「显示主窗口」与「退出」，配色随主题
        tm2 = tk.Menu(self.root, tearoff=0, bg=BG_CARD, fg=FG, bd=0,
                      activebackground=MENU_ACTIVE, activeforeground=FG, relief="flat")
        tm2.add_command(label="显示主窗口", command=self._restore_from_tray)
        tm2.add_command(label="立即刷新", command=self.refresh_async)
        tm2.add_command(label="展开 / 收缩", command=self.toggle_mode)
        tm2.add_separator()
        tm2.add_command(label="退出", command=self.quit)
        self.tray_menu = tm2

    # ---------------- 主题 ----------------
    @staticmethod
    def _theme_glyph(t):
        """标题栏按钮图标：auto=◐ / light=☀ / dark=☾（Tk 内置字符，无外部图片依赖）。"""
        return {"auto": "◐", "light": "☀", "dark": "☾"}.get(t, "◐")

    @staticmethod
    def _theme_label(t):
        return {"auto": "跟随系统", "light": "浅色", "dark": "深色"}.get(t, t)

    def _theme_hover(self, entering):
        self.theme_btn.configure(fg=ACCENT if entering else FG_DIM)

    def _cycle_theme(self):
        """标题栏按钮循环：默认(auto) → 浅色 → 深色 → 默认…"""
        order = ("auto", "light", "dark")
        nxt = order[(order.index(self.theme) + 1) % len(order)]
        self.set_theme(nxt)

    def set_theme(self, theme):
        if theme not in ("auto", "light", "dark"):
            theme = "auto"
        self.theme = theme
        self.cfg["theme"] = theme
        save_cfg(self.cfg)
        self._apply_theme_ui()

    def _apply_theme_ui(self):
        """整体切换主题：重设调色板 → 重建全部 UI → 重设窗口装饰（含 DWM 边框
        色，防失焦白边复发）→ 重放已取得的数据，保证不残留任何旧色。"""
        try:
            _apply_palette(self.theme)
        except Exception:
            pass
        self.root.configure(bg=BG)
        self._teardown_ui()
        self._build_ui()
        self._bind_events()
        self.root.update_idletasks()
        self._apply_mode(initial=True)
        # Windows：子类化已注册时不会重设 DWM 配色 → 显式重设；其它平台幂等
        self.root.after_idle(self._refresh_window_decor)
        self.root.after(120, self._refresh_window_decor)
        # 重放数据（保留上次口径，立即刷新为当前语义）
        if (self.last is not None) or (self.last_balance is not None):
            try:
                now = datetime.now().strftime("%H:%M:%S")
                self._render(self.last, self.last_balance)
            except Exception:
                pass
        # 重放派猫猫旅行按钮态 / 自动派遣开关（重建后新控件需重新配色）
        # 注：buddy 卡已移除（v1.2.0 调整），不再重放 buddy UI 或重查 buddy 信息
        try:
            self._update_dispatch_ui()
        except Exception:
            pass
        self.foot_lbl.configure(text="主题：%s" % self._theme_label(self.theme), fg=FG_FAINT)

    def _refresh_window_decor(self):
        """重设窗口装饰（平台抽象层）：Windows 重设 DWM 边框配色 + 圆角；
        其它平台仅重设圆角。"""
        try:
            platform.refresh_window_decor(self.root, bg=BG, dark=NC_DARK)
            self._apply_round_corners()
        except Exception:
            pass

    def _teardown_ui(self):
        """销毁当前全部子控件（保留 root），供重建用。"""
        try:
            for w in list(self.root.winfo_children()):
                try:
                    w.destroy()
                except Exception:
                    pass
        except Exception:
            pass
        self.menu = None

    def _auto_check_theme(self):
        """仅 auto 模式下：检测系统深浅色变化并自动跟随。"""
        if self.theme != "auto":
            return
        try:
            if _resolve_theme("auto") != _resolve_theme(self.theme):
                self._apply_theme_ui()
        except Exception:
            pass

    # ---------------- 模式切换 ----------------
    def toggle_mode(self):
        self.expanded = not self.expanded
        self.cfg["expanded"] = self.expanded
        save_cfg(self.cfg)
        self._apply_mode()

    def _apply_mode(self, initial=False):
        w = W_EXP if self.expanded else W_COL
        sw, sh = self.root.winfo_screenwidth(), self.root.winfo_screenheight()

        if initial:
            x = self.cfg.get("x")
            y = self.cfg.get("y")
            if x is None or y is None:
                x, y = sw - w - 24, 96
        else:
            # 保持左上角不变，避免跳动；超出屏幕则回收
            x = self.root.winfo_x()
            y = self.root.winfo_y()
        vs = self._virtual_screen()
        # 按虚拟屏全范围夹紧：允许拖到副屏，且不会把窗口夹死在主屏内
        x = max(vs[0], min(int(x), vs[0] + max(0, vs[2] - w)))
        y = max(vs[1], min(int(y), vs[1] + max(0, vs[3] - 60)))

        if self.expanded:
            self.f_col.pack_forget()
            self.f_exp.pack(fill="both", expand=True)
            self.toggle_btn.configure(text="▴")
        else:
            self.f_exp.pack_forget()
            self.f_col.pack(fill="both", expand=True)
            self.toggle_btn.configure(text="▾")

        # 先按目标宽度落位，再按内容实际高度贴合（见 _fit_window）
        self.root.geometry("%dx%d+%d+%d" % (w, max(1, self.root.winfo_height()), x, y))
        self._fit_window(x, y)
        # 尺寸变了，圆角要按新尺寸重设（否则停留在旧尺寸）
        self.root.after_idle(self._apply_round_corners)
        self.root.after(120, self._apply_round_corners)

    def _fit_window(self, x=None, y=None):
        """按内容实际请求高度设置窗口高度（外加底部安全留白）。

        窗口高度不再写死：Tk 的 winfo_reqheight() 是所有控件自然高度之和，
        取它 + SAFE_PAD 作为窗口高度，任何文字行都不会被下边界裁掉。
        内容变化（刷新后文案变长/变短）后也会被调用重新贴合。
        """
        try:
            w = W_EXP if self.expanded else W_COL
            hmin = H_EXP_MIN if self.expanded else H_COL_MIN
            # 两次：第一轮让 RoundCard 的 <Configure> 完成重绘与几何传播
            self.root.update_idletasks()
            self.root.update_idletasks()
            need = max(int(self.root.winfo_reqheight()), hmin) + SAFE_PAD
            sw, sh = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
            if x is None:
                x = self.root.winfo_x()
            if y is None:
                y = self.root.winfo_y()
            vs = self._virtual_screen()
            # 虚拟屏全范围夹紧：副屏（可能为负坐标/主屏右侧）也能容纳并持久化
            x = max(vs[0], min(int(x), vs[0] + max(0, vs[2] - w)))
            y = max(vs[1], min(int(y), vs[1] + max(0, vs[3] - need)))
            if self.root.winfo_width() != w or self.root.winfo_height() != need:
                self.root.geometry("%dx%d+%d+%d" % (w, need, x, y))
                self.root.update_idletasks()
                # 高度变了，region 圆角要按新尺寸重设（DWM 圆角由系统跟随，无影响）
                self.root.after_idle(self._apply_round_corners)
        except Exception:
            pass

    # ---------------- 事件 ----------------
    def _bind_events(self):
        # 重建 UI 后需重绑；先清掉上一次的 bind_all，避免多主题切换叠加回调
        for seq in ("<ButtonPress-1>", "<B1-Motion>", "<ButtonRelease-1>"):
            self.root.unbind_all(seq)
        self.root.bind("<Button-3>", self._show_menu)
        self.root.bind_all("<ButtonPress-1>", self._on_press, add="+")
        self.root.bind_all("<B1-Motion>", self._on_drag, add="+")
        self.root.bind_all("<ButtonRelease-1>", self._on_release, add="+")
        self.c_balance.bind("<Double-Button-1>", self._copy_value)
        self.c_bal_val.bind("<Double-Button-1>", self._copy_balance)
        self.e_balance.bind("<Double-Button-1>", self._copy_value)
        self.e_bal_val.bind("<Double-Button-1>", self._copy_balance)

    def _clip(self, text):
        try:
            self.root.clipboard_clear()
            self.root.clipboard_append(str(text))
            self.foot_lbl.configure(text="已复制 %s 到剪贴板" % text, fg=FG_FAINT)
        except Exception:
            pass

    def _copy_value(self, _e=None):
        """复制「累计领取」（签到积分）。"""
        if self.last and self.last.get("balance") is not None:
            self._clip(self.last["balance"])

    def _copy_balance(self, _e=None):
        """复制「积分余额」（额度，带两位小数）。"""
        if self.last_balance and self.last_balance.get("balance") is not None:
            self._clip(self.last_balance.get("display")
                       or ("%.2f" % self.last_balance["balance"]))

    def _on_press(self, event):
        self._dx = event.x_root - self.root.winfo_x()
        self._dy = event.y_root - self.root.winfo_y()
        self._moved = False

    def _on_drag(self, event):
        try:
            x = event.x_root - self._dx
            y = event.y_root - self._dy
        except Exception:
            return
        vs = self._virtual_screen()
        sw, sh = vs[2], vs[3]           # 虚拟屏总范围（含所有显示器）
        w, h = self.root.winfo_width(), self.root.winfo_height()
        # 按虚拟屏夹紧：左侧可到虚拟屏左缘（负坐标），右侧到虚拟屏右缘 → 副屏可拖
        x = max(vs[0] - w + 60, min(x, vs[0] + sw - 60))
        y = max(vs[1], min(y, vs[1] + sh - 24))
        self.root.geometry("+%d+%d" % (x, y))
        self._moved = True

    def _on_release(self, _e):
        if getattr(self, "_moved", False):
            self.cfg["x"] = self.root.winfo_x()
            self.cfg["y"] = self.root.winfo_y()
            save_cfg(self.cfg)

    def _show_menu(self, event):
        try:
            self.menu.tk_popup(event.x_root, event.y_root)
        finally:
            self.menu.grab_release()

    def _toggle_top(self):
        v = bool(self.top_var.get())
        self.root.attributes("-topmost", v)
        self.cfg["topmost"] = v
        save_cfg(self.cfg)

    def _set_alpha(self, v):
        self.root.attributes("-alpha", 1.0 if v >= 0.999 else v)
        self.cfg["alpha"] = v
        save_cfg(self.cfg)
        self.foot_lbl.configure(text="已设为 %d%% 不透明度" % int(v * 100), fg=FG_FAINT)

    # ---------------- 多屏 / 托盘 / 签到 ----------------

    def _virtual_screen(self):
        """虚拟屏全范围（含所有显示器）：返回 [X, Y, W, H]。

        Windows：SM_XVIRTUALSCREEN（76~79）取所有显示器的包围盒（副屏可为负坐标）；
        macOS/Linux：Tk 主屏尺寸回退（winfo_screenwidth/height），副屏边界夹紧受限。
        拖动 / 夹紧全部改用该范围，保证窗口能自由拖到第二屏幕。
        """
        return platform.virtual_screen(self.root)

    # ---------- 托盘（平台抽象层：Windows=Shell_NotifyIcon / mac·linux=pystray）----------
    def _minimize_to_tray(self):
        """最小化按钮：隐藏窗口 + 显示系统托盘图标（数据刷新不停）。

        无托盘可用（Windows 外未装 pystray 等）时 tray.add() 返回 False，
        保持窗口可见，避免把窗口藏起来后用户无法找回。
        """
        try:
            ok = self.tray.add()
        except Exception:
            ok = False
        if not ok:
            return
        try:
            self.root.withdraw()
        except Exception:
            pass

    def _restore_from_tray(self):
        """托盘左键 / 菜单「显示主窗口」：恢复窗口并置顶。"""
        try:
            self.root.update_idletasks()
            self.root.deiconify()
            if self.cfg.get("topmost", False):
                self.root.attributes("-topmost", True)
            self.root.lift()
            self.root.focus_force()
        except Exception:
            pass

    def _show_tray_menu(self, x, y):
        try:
            if getattr(self, "tray_menu", None) is None:
                self._build_menu()
            # 「显示/隐藏主窗口」按当前窗口实际状态动态切换：
            # 窗口隐藏/最小化 → 「显示主窗口」（点击恢复）；窗口已展示 → 「隐藏主窗口」（点击缩到托盘）。
            # winfo_viewable() 在窗口 withdrawn / 未映射时返回 0，可准确反映窗口是否可见。
            try:
                visible = bool(self.root.winfo_viewable())
            except Exception:
                visible = bool(self.root.state() != "withdrawn")
            label = "显示主窗口" if not visible else "隐藏主窗口"
            cmd = self._restore_from_tray if not visible else self._minimize_to_tray
            self.tray_menu.entryconfig(0, label=label, command=cmd)
            try:
                self.tray_menu.tk_popup(x, y)
            finally:
                self.tray_menu.grab_release()
        except Exception:
            pass

    def _on_tray_event(self, evt):
        """托盘回调事件（平台抽象层统一为字符串事件）。

        Windows（Shell_NotifyIcon）：右键抬起→弹菜单；左键抬起→恢复窗口。
        macOS/Linux（pystray 菜单栏）：菜单项回传 restore/refresh/toggle/quit。
        未知事件一律忽略，不执行任何展示逻辑。
        """
        if evt == "right":
            # 右键菜单坐标用 Tk 全局指针（跨平台），不依赖 GetCursorPos
            try:
                x = self.root.winfo_pointerx()
                y = self.root.winfo_pointery()
                self.root.after(0, self._show_tray_menu, int(x), int(y))
            except Exception:
                pass
        elif evt in ("left", "restore"):
            self.root.after(0, self._restore_from_tray)
        elif evt == "refresh":
            self.root.after(0, self.refresh_async)
        elif evt == "toggle":
            self.root.after(0, self.toggle_mode)
        elif evt == "quit":
            self.root.after(0, self.quit)

    # ---------- 签到（手动按钮 / 自动开关）----------
    def _check_auth_once(self):
        """启动时检测登录态：读取不到 accessToken 则发一次系统通知提醒登录。

        用 self._auth_notified 保证只通知一次，避免后续每轮刷新重复轰炸。
        """
        if getattr(self, "_auth_notified", False):
            return
        self._auth_notified = True
        try:
            tok, _dom = load_credentials()
        except Exception:
            tok = None
        if not tok:
            try:
                self.tray.show_balloon("WorkBuddy 登录提醒", "未检测到登录态，请先登录 WorkBuddy")
            except Exception:
                pass

    def _update_checkin_ui(self):
        """按今日签到状态刷新签到卡（未签=ACCENT 可点，已签=灰禁用）与自动开关。

        无登录态（auth 文件缺失/解析失败，load_credentials 返回 (None, None)）时
        对齐派猫猫旅行卡未登录态写法：按钮灰禁用显「无法签到」，说明文字显
        「未获取登录凭据（请先登录WorkBuddy）」（同派猫卡 last_error 的 ERR 样式）。
        """
        if getattr(self, "signin_btn", None) is None:
            return
        on = self.auto_checkin
        # 演示模式（仅验证截图用，正式无影响）：WIDGET_SIGNIN_DEMO=nologin|checked
        sign_demo = os.environ.get("WIDGET_SIGNIN_DEMO", "").strip()
        # 无登录态优先判定：避免触发 _checked_today() 的网络查询与误判已签。
        try:
            if sign_demo == "nologin":
                tok = None
            else:
                tok, _dom = load_credentials()
        except Exception:
            tok = None
        if not tok:
            # 对齐派猫卡无登录态：按钮灰色禁用「无法签到」，说明文字红色（ERR）
            try:
                self.signin_btn.configure(text="无法签到", bg=BG_SOFT, fg=FG_DIM,
                                          cursor="arrow")
                self.signin_info.configure(text="未获取登录凭据（请先登录WorkBuddy）",
                                           fg=ERR)
            except Exception:
                pass
            try:
                self.auto_switch.configure(text="ON" if on else "OFF",
                                           fg=ACCENT if on else FG_DIM)
            except Exception:
                pass
            return
        checked = True if sign_demo == "checked" else self._checked_today()
        try:
            if checked:
                self.signin_btn.configure(text="已签到", bg=BG_SOFT, fg=FG_DIM,
                                          cursor="arrow")
            else:
                self.signin_btn.configure(text="立即签到", bg=ACCENT, fg="#04332F",
                                          cursor="hand2")
            # 恢复说明文字默认占位，避免无登录态文案残留到正常态
            self.signin_info.configure(text="今日已签到并完成领取，明日再来", fg=FG_FAINT)
        except Exception:
            pass
        try:
            self.auto_switch.configure(text="ON" if on else "OFF",
                                       fg=ACCENT if on else FG_DIM)
        except Exception:
            pass

    def _checked_today(self):
        """今日是否已签到：优先最近一次 detail 缓存，否则只读查询一次。"""
        d = self.last
        if isinstance(d, dict):
            v = d.get("checked_in")
            if v is not None:
                return bool(v)
        try:
            return bool(query_credits_detail(timeout=8).get("checked_in"))
        except Exception:
            return False

    def _toggle_auto_checkin(self):
        """自动签到开关：持久化到 config；开启立即触发一次检查。"""
        self.auto_checkin = not self.auto_checkin
        self.cfg["auto_checkin"] = self.auto_checkin
        try:
            save_cfg(self.cfg)
        except Exception:
            pass
        self._update_checkin_ui()
        if self.auto_checkin:
            self._checkin_if_needed(indent="自动签到已开启，开始检查…")

    # ================= 派猫猫旅行 / Buddy 卡片（v1.2.0） =================

    # ---------- 自动派遣开关 ----------
    def _toggle_auto_dispatch(self):
        """自动派遣开关：持久化到 config；开启立即触发一次自动流程（未派遣则派）。"""
        self.auto_dispatch = not self.auto_dispatch
        self.cfg["auto_dispatch"] = self.auto_dispatch
        try:
            save_cfg(self.cfg)
        except Exception:
            pass
        self._update_dispatch_ui()
        if self.auto_dispatch:
            self.foot_lbl.configure(text="自动派遣已开启，开始检查…", fg=FG_FAINT)
            # 延迟一帧再跑，先让 UI 正确渲染 ON 态，再进后台流程
            self.root.after(300, self._auto_dispatch_once)

    # ---------- 派遣按钮点击（手动） ----------
    def _on_dispatch_btn(self):
        """手动派遣按钮：按状态机分发真正的写接口动作（防重入）。"""
        if self._travel_cmd_busy:
            return
        st = self.travel.get("state")
        limit = self.travel.get("daily_limit_reached")
        if st == "idle" and not limit:
            self._travel_action("dispatch")
        elif st == "arrived":
            self._travel_action("claim")
        elif st == "traveling":
            # 本地倒计时已归零 → 先刷新状态确认 arrived 再领取（防止误写接口）
            if self._travel_remaining() is not None and self._travel_remaining() <= 0:
                self._travel_action("claim_recheck")

    def _travel_remaining(self):
        """当前旅行剩余秒数（traveling 态才有）；可能为负，主线程每秒调用。"""
        if self.travel.get("state") != "traveling":
            return None
        if not self._travel_arrive_at or not self._travel_recv_ts:
            return None
        elapsed = time.time() - self._travel_recv_ts
        return self._travel_arrive_at - (self._travel_server_now + elapsed)

    # ---------- 派遣区 UI 渲染（按钮三态 / 开关 / 信息行） ----------
    def _update_dispatch_ui(self):
        """按旅行状态机刷新派遣按钮+信息行+自动派遣开关（按钮用主题色）。"""
        if getattr(self, "dispatch_btn", None) is None:
            return
        try:
            self.auto_dispatch_switch.configure(text="ON" if self.auto_dispatch else "OFF",
                                                fg=ACCENT if self.auto_dispatch else FG_DIM)
        except Exception:
            pass
        try:
            st = self.travel.get("state")
            limit = self.travel.get("daily_limit_reached")
            loc = self.travel.get("location")
            reward = self.travel.get("reward_credit")
            if st == "idle" and not limit:
                self.dispatch_btn.configure(text="立即派遣", bg=ACCENT, fg=ACCENT_TXT,
                                            cursor="hand2")
                self.travel_info.configure(
                    text="未派遣 · 随机地点" + (" · 奖励 %s 积分" % reward if reward else ""),
                    fg=FG_FAINT)
            elif st == "traveling":
                self.dispatch_btn.configure(text="旅行中…", bg=BG_SOFT, fg=FG_DIM,
                                            cursor="arrow")
                self.travel_info.configure(
                    text="%s · 奖励 %s 积分" % (loc or "Buddy 旅行中", reward if reward else "?"),
                    fg=FG_FAINT)
            elif st == "arrived":
                self.dispatch_btn.configure(text="立即领取", bg=ACCENT, fg=ACCENT_TXT,
                                            cursor="hand2")
                self.travel_info.configure(
                    text="已到达 · 可领 %s 积分" % (reward if reward else "?"), fg=FG_FAINT)
            elif st == "idle" and limit:
                # 「今日已达派遣上限」是服务端给出的唯一权威结束标志：
                # 已派遣并完成领取后，服务端会把旅行记录字段清零（buddy_id=0 /
                # record_id=0 / arrive_at=0 / location=null），仅保留
                # daily_limit_reached=true（实测：GET status 返 {"state":"idle",
                # "buddy_id":0,"record_id":0,"arrive_at":0,"location":null,
                # "daily_limit_reached":true,...}）。因此 idle+limit=true 即
                # 「今日已派完」，不得再依赖记录字段佐证——否则会把已派完误降级
                # 成「立即派遣」（即此前「未派遣 · 随机地点 + 可点绿钮」的错误表现）。
                self.dispatch_btn.configure(text="派遣结束", bg=BG_SOFT, fg=FG_DIM,
                                            cursor="arrow")
                self.travel_info.configure(text="今天已派遣并完成领取，明日再来", fg=FG_FAINT)
            else:
                self.dispatch_btn.configure(text="查询失败", bg=BG_SOFT, fg=FG_DIM,
                                            cursor="arrow")
                self.travel_info.configure(text=self.travel.get("last_error") or "查询失败",
                                           fg=ERR)
        except Exception:
            pass
        try:
            self.root.update_idletasks()
            self._fit_window()
        except Exception:
            pass

    # ---------- 倒计时（每秒跳动，副线程只读结果驱动） ----------
    def _update_travel_countdown(self):
        """traveling 态每秒刷新按钮上的倒计时文本（本地时钟换算，不重新发请求）。"""
        if getattr(self, "dispatch_btn", None) is None:
            return
        if self.travel.get("state") != "traveling":
            return
        rem = self._travel_remaining()
        if rem is None:
            return
        if rem <= 0:
            # 本地到点：本地标记 arrived（时钟换算归根结底以服务端为准，点领取时
            # 会先刷新状态复核，已到达才真正调用写接口，杜绝误写）
            self.travel["state"] = "arrived"
            try:
                self.dispatch_btn.configure(text="立即领取", bg=ACCENT, fg=ACCENT_TXT,
                                            cursor="hand2")
                self.travel_info.configure(text="已到达 · 可领取旅行奖励", fg=ACCENT)
            except Exception:
                pass
        else:
            try:
                h = int(rem // 3600)
                m = int((rem % 3600) // 60)
                s = int(rem % 60)
                self.dispatch_btn.configure(text="旅行中 %d:%02d:%02d" % (h, m, s),
                                            bg=BG_SOFT, fg=FG_DIM, cursor="arrow")
                self.travel_info.configure(text="%s · 奖励 %s 积分"
                                           % (self.travel.get("location") or "Buddy 旅行中",
                                              self.travel.get("reward_credit") or "?"),
                                           fg=FG_FAINT)
            except Exception:
                pass

    # ---------- 只读查询（status/config） ----------
    def _start_travel_fetch(self, force=False):
        """后台线程拉取旅行状态。只读，不触发写接口。
        注：buddy 信息（GET /v2/activity/growth/buddy/info）已不再抓取（v1.2.0 调整）。
        看门狗：若上次请求异常超时（>30s）导致 _travel_fetching 未复位，强制复位以免
        后续所有刷新（含手动刷新）被永久跳过——这正是此前「刷新后状态不变」的根因之一。"""
        if self._travel_fetching:
            if force and self._travel_fetch_started and \
                    time.time() - self._travel_fetch_started > 30:
                self._travel_fetching = False  # 强制复位锁死的只读查询
            else:
                return
        self._travel_fetching = True
        self._travel_fetch_started = time.time()

        def work():
            box = {"travel": None}
            try:
                # 演示模式（验证截图用）：旅行状态走模拟数据
                if self._travel_demo:
                    box["travel"] = self._demo_travel(self._travel_demo)
                else:
                    tv = btc.travel_status()
                    box["travel"] = tv
            except Exception as exc:
                box["travel"] = {"ok": False, "data": None,
                                 "error": "%s: %s" % (type(exc).__name__, str(exc)[:120])}
            try:
                self._travel_q.put(box)
            except Exception:
                pass

        threading.Thread(target=work, daemon=True).start()

    @staticmethod
    def _flag_true(v):
        """把 daily_limit_reached 等开关字段解析为严格 bool。

        服务端布尔传递存在类型不稳定风险（曾出现 true/false 以字符串返回的情况），
        bool("false")==True 会把「未达上限」误判成「已达上限」，直接导致未派遣却
        显示「派遣结束」。此处显式归一化：布尔/数字按字面，字符串仅 true/1/yes 为真。"""
        if isinstance(v, bool):
            return v
        if v is None:
            return False
        if isinstance(v, (int, float)):
            return bool(v)
        return str(v).strip().lower() in ("true", "1", "yes")

    @staticmethod
    def _demo_travel(demo):
        """演示模式：按 WIDGET_TRAVEL_DEMO 生成模拟旅行状态（配合三态截图验证）。"""
        now = int(time.time())
        d = {"demo": demo}
        if demo == "ready":
            d.update({"state": "idle", "daily_limit_reached": False,
                      "server_now": now, "location": None, "reward_credit": 7})
        elif demo == "traveling":
            d.update({"state": "traveling", "daily_limit_reached": False,
                      "server_now": now, "arrive_at": now + 3666,
                      "location": {"id": 2, "name": "咖啡馆"},
                      "reward_credit": 7})
        elif demo == "arrived":
            d.update({"state": "arrived", "daily_limit_reached": False,
                      "server_now": now, "location": {"id": 2, "name": "古镇客栈"},
                      "reward_credit": 9})
        else:  # done
            d.update({"state": "idle", "daily_limit_reached": True,
                      "server_now": now, "location": None, "reward_credit": 8})
        return {"ok": True, "data": d, "error": None}

    # ---------- 主线程处理只读结果 ----------
    def _handle_travel_result(self, box):
        """渲染旅行状态（主线程）。注：buddy 卡已移除，不再处理 buddy 结果。"""
        self._travel_fetching = False
        tv = box.get("travel") or {}
        if tv.get("ok") and isinstance(tv.get("data"), dict):
            d = tv["data"]
            loc = d.get("location")
            self.travel.update({
                "state": d.get("state"),
                "arrive_at": d.get("arrive_at"),
                "server_now": d.get("server_now"),
                # 严格解析：杜绝字符串 "false" 被 bool() 误判为 True
                "daily_limit_reached": self._flag_true(d.get("daily_limit_reached")),
                "location": (loc or {}).get("name") if isinstance(loc, dict) else None,
                "reward_credit": d.get("reward_credit"),
                "buddy_id": d.get("buddy_id"),
                "record_id": d.get("record_id"),
                "last_error": None,
            })
            if self.travel["state"] == "traveling":
                self._travel_recv_ts = time.time()
                self._travel_server_now = float(d.get("server_now") or 0)
                self._travel_arrive_at = float(d.get("arrive_at") or 0)
            else:
                self._travel_recv_ts = 0.0
                self._travel_server_now = 0.0
                self._travel_arrive_at = 0.0
        else:
            if self.travel.get("state") is None or tv.get("error"):
                self.travel["last_error"] = tv.get("error") or "旅行状态查询失败"
            # 未拿到新状态时保留上一份状态，避免 UI 闪烁
        self._update_dispatch_ui()
        # 自动派遣流程轮询由 _auto() / 开关触发，这里只更新 UI

    # ---------- 写接口（派遣 / 领取 / 自动流程），受状态机与防重入管控 ----------
    def _travel_action(self, action):
        """后台执行写接口或自动流程。dispatch/claim 前先只读复核状态，杜绝误写。"""
        if self._travel_cmd_busy:
            return
        self._travel_cmd_busy = True

        def work():
            res = {"kind": "cmd", "action": action, "ok": False, "msg": ""}
            try:
                tok, _ = load_credentials()
                if not tok:
                    res["msg"] = "未获取到登录凭据（请先登录 WorkBuddy）"
                    return
                if action == "dispatch":
                    # 复核：未达上限且 idle 才允许派出
                    sv = btc.travel_status(token=tok)
                    if not sv.get("ok"):
                        res["msg"] = "状态复核失败：%s" % (sv.get("error") or "?")
                        return
                    d = sv["data"]
                    reached = self._flag_true(d.get("daily_limit_reached"))
                    if d.get("state") != "idle" or reached:
                        res["msg"] = "当前不可派遣（已派遣或旅行中/已结束），已跳过"
                        return
                    cfg = btc.travel_config(token=tok)
                    dep = btc.travel_depart(token=tok, config=cfg.get("locations") or [])
                    res["ok"] = bool(dep.get("success"))
                    res["msg"] = dep.get("message") or "派遣失败"
                    res["cmd_detail"] = dep
                elif action == "claim":
                    # 复核：arrived 才允许领取
                    sv = btc.travel_status(token=tok)
                    if not sv.get("ok"):
                        res["msg"] = "状态复核失败：%s" % (sv.get("error") or "?")
                        return
                    if sv["data"].get("state") != "arrived":
                        res["msg"] = "Buddy 尚未到达，自动领取失败（等待倒计时结束重试）"
                        return
                    cl = btc.travel_claim(token=tok)
                    res["ok"] = bool(cl.get("success"))
                    res["msg"] = cl.get("message") or "领取失败"
                    res["cmd_detail"] = cl
                elif action == "claim_recheck":
                    # 本地倒计时到点先刷新状态：已 arrived → 领取；仍 traveling → 仅刷新
                    sv = btc.travel_status(token=tok)
                    if not sv.get("ok"):
                        res["msg"] = "状态刷新失败：%s" % (sv.get("error") or "?")
                        return
                    if sv["data"].get("state") == "arrived":
                        cl = btc.travel_claim(token=tok)
                        res["ok"] = bool(cl.get("success"))
                        res["msg"] = cl.get("message") or "领取失败"
                        res["cmd_detail"] = cl
                    else:
                        res["msg"] = "旅行尚未到达，已刷新倒计时"
                        res["need_refresh"] = True
                elif action == "auto":
                    # 自动流程（先领后派，语义对齐脚本 travel_auto）：仅在本开关开启时动作
                    sv = btc.travel_status(token=tok)
                    if not sv.get("ok"):
                        res["msg"] = "查询旅行状态失败：%s" % (sv.get("error") or "?")
                        return
                    if not self.auto_dispatch:
                        res["msg"] = "自动派遣已关闭，未执行任何动作"
                        return
                    d = sv["data"]
                    st = d.get("state")
                    log = []
                    if st == "arrived":
                        cl = btc.travel_claim(token=tok)
                        if cl.get("success"):
                            log.append(cl.get("message"))
                        elif not (cl.get("message") or "").startswith("已领取"):
                            log.append(cl.get("message") or "领取失败")
                    if st == "traveling":
                        log.append("Buddy 旅行中，等待到达后自动领取")
                    elif st == "idle":
                        if self._flag_true(d.get("daily_limit_reached")):
                            log.append("今日派遣次数已用完")
                        else:
                            cfg = btc.travel_config(token=tok)
                            dep = btc.travel_depart(token=tok, config=cfg.get("locations") or [])
                            log.append(dep.get("message") or "派遣失败")
                    res["ok"] = True
                    res["msg"] = "；".join(log) if log else "自动派遣：今日无待办"
                else:
                    res["msg"] = "未知动作：%s" % action
            except Exception as exc:
                res["msg"] = "%s: %s" % (type(exc).__name__, str(exc)[:120])
            finally:
                try:
                    self._travel_cmd_q.put(res)
                except Exception:
                    pass

        threading.Thread(target=work, daemon=True).start()

    def _handle_travel_cmd(self, res):
        """主线程处理写接口 / 自动流程结果：通知 + 刷新旅行状态。"""
        self._travel_cmd_busy = False
        act = res.get("action")
        msg = res.get("msg") or ""
        ok = bool(res.get("ok"))
        try:
            if act == "dispatch":
                if ok:
                    self.tray.show_balloon("派猫猫旅行", msg)
                    self.foot_lbl.configure(text=msg, fg=ACCENT)
                else:
                    self.foot_lbl.configure(text="派遣未执行：%s" % msg, fg=ERR)
            elif act in ("claim", "claim_recheck"):
                if ok:
                    self.tray.show_balloon("旅行奖励", msg)
                    self.foot_lbl.configure(text=msg, fg=ACCENT)
                else:
                    self.foot_lbl.configure(text="领取未执行：%s" % msg, fg=ERR)
            elif act == "auto":
                self.foot_lbl.configure(text="自动派遣：%s" % msg, fg=FG_FAINT)
            else:
                self.foot_lbl.configure(text=msg, fg=FG_FAINT)
        except Exception:
            pass
        # 写接口成功后同步刷新余额/累计数据（旅行奖励会入账；失败则跳过）
        if ok and act in ("dispatch", "claim", "claim_recheck"):
            try:
                self.refresh_async()
            except Exception:
                pass
        # 动作后重拉旅行状态（含自动领取后的状态推进、余额随全局刷新更新）
        self._start_travel_fetch(force=False)

    # ---------- 自动派遣入口（开关开启时触发 / 每轮 10 分钟刷新调用） ----------
    def _auto_dispatch_once(self):
        """自动派遣每轮调度：开启且无写接口进行中时才动作。领取也可纳入自动流程。"""
        if not self.auto_dispatch:
            return
        if self._travel_cmd_busy:
            return
        if self._travel_demo:
            # 演示模式不触发真实写接口，仅刷新状态展示
            self._start_travel_fetch(force=False)
            return
        self._travel_action("auto")

    def _checkin_if_needed(self, indent="正在检查签到状态…"):
        """仅自动开启时调用：未签到则自动签到，已签到不动作。"""
        if not self.auto_checkin:
            return
        try:
            st = query_credits_detail(timeout=8)
        except Exception as e:
            self.foot_lbl.configure(text="签到检查失败: %s" % e, fg=WARN)
            return
        if not isinstance(st, dict) or not st.get("ok"):
            self.foot_lbl.configure(text="签到检查失败", fg=WARN)
            return
        if st.get("checked_in"):
            if isinstance(self.last, dict):
                self.last["checked_in"] = True
            self._update_checkin_ui()
            self.foot_lbl.configure(text="今日已签到，无需重复", fg=FG_FAINT)
            return
        self.foot_lbl.configure(text=indent, fg=ACCENT)
        self._do_signin_async()

    def _on_signin_click(self):
        if self._signin_busy:
            return
        if self._checked_today():
            self.foot_lbl.configure(text="今日已签到", fg=FG_FAINT)
            return
        self._do_signin_async()

    @staticmethod
    def _read_uid():
        """从登录态文件读取 account.uid（X-User-Id 请求头用）。"""
        try:
            p = find_auth_file()
            if not p:
                return None
            with open(p, "r", encoding="utf-8") as f:
                d = json.load(f)
            uid = (d.get("account") or {}).get("uid")
            return str(uid) if uid else None
        except Exception:
            return None

    def _do_signin_async(self):
        """后台线程执行签到写请求；结果放 _signin_q，由 _tick 取回渲染。"""
        if self._signin_busy:
            return
        self._signin_busy = True
        self.foot_lbl.configure(text="签到中…", fg=ACCENT)

        def work():
            try:
                tok, dom = load_credentials()
                if not tok:
                    self._signin_q.put({"ok": False, "error": "未获取到登录凭据（请先登录 WorkBuddy）"})
                    return
                headers = {
                    "Authorization": "Bearer " + tok,
                    "Origin": "https://" + dom,
                    "Referer": "https://" + dom + "/",
                    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                                   "Chrome/124.0.0.0 Safari/537.36"),
                    "Content-Type": "application/json",
                }
                uid = self._read_uid()
                if uid:
                    headers["X-User-Id"] = uid
                # 真实写接口（逆向自 WorkBuddy resources/app.asar 的
                # AuthService.claimDailyCheckin / DesktopAccountRepo.checkin）：
                # POST {endpoint}/v2/billing/meter/daily-checkin，body 为空对象 {}
                url = "https://%s/v2/billing/meter/daily-checkin" % dom
                body = b"{}"
                _st, raw = _post_json(url, body, headers, timeout=12)
                j = None
                try:
                    j = json.loads(raw)
                except Exception:
                    j = None
                # 服务端判重：今日已签返回 HTTP 400 + code=10001 "今天已签到"
                # 该情况等同于签到成功态，不视为错误
                already = (isinstance(j, dict) and j.get("code") in (10001,)) or \
                          ("已签到" in (j.get("msg") or "") if isinstance(j, dict) else False)
                if _st < 400 and isinstance(j, dict) and not j.get("code"):
                    ok = True
                elif already:
                    ok = True
                else:
                    ok = False
                self._signin_q.put({"ok": ok,
                                    "already": already,
                                    "http": _st,
                                    "raw": raw[:800],
                                    "json": j})
            except Exception as e:
                self._signin_q.put({"ok": False,
                                    "error": "%s: %s" % (type(e).__name__, str(e)[:150])})

        threading.Thread(target=work, daemon=True).start()

    def _handle_signin_result(self, r):
        """_tick 取回签到结果：成功 → 刷新数据与按钮态 + 系统通知；失败 → 提示。"""
        self._signin_busy = False
        if not isinstance(r, dict):
            self.foot_lbl.configure(text="签到无响应", fg=WARN)
            return
        if r.get("ok"):
            if isinstance(self.last, dict):
                self.last["checked_in"] = True
            self._update_checkin_ui()
            # 本次确实执行了签到动作并成功 → 发系统气泡通知；
            # already=True（今日已是已签到态、未执行签到）→ 不发通知。
            if not r.get("already"):
                try:
                    self.tray.show_balloon("WorkBuddy 签到", "签到成功，已领取今日积分")
                except Exception:
                    pass
                self.foot_lbl.configure(text="签到成功", fg=ACCENT)
            else:
                self.foot_lbl.configure(text="今日已签到", fg=FG_FAINT)
            self.refresh_async()
        else:
            msg = r.get("error") or ("HTTP %s" % r.get("http"))
            self.foot_lbl.configure(text="签到失败: %s" % msg, fg=ERR)

    # ---------------- 数据 ----------------
    def refresh_async(self, auto=False):
        """发起一次刷新。auto=True 表示由 10 分钟自动周期（或其补偿重试）触发——
        失败时由 _render 据此安排 3 秒后的补偿刷新；手动刷新（工具栏/菜单）传默认
        False，不影响自动周期重试状态，也不会被误判为自动轮次。
        """
        if self.busy:
            return
        self.busy = True
        self._anim_on = True
        self.foot_lbl.configure(text="刷新中…")
        # 手动/周期刷新必须一并刷新派猫猫旅行状态（此前「立即刷新」只刷签到余额区，
        # 导致派遣区一直停留旧状态，是用户「刷新后仍显示派遣结束」的直接原因之一）。
        # force=True：即使只读查询上次异常未复位，看门狗也会强制重启本轮查询。
        try:
            self._start_travel_fetch(force=True)
        except Exception:
            pass

        def work():
            # 两个接口并行请求，互不阻塞；任一失败只影响对应区域
            box = {}

            def run_detail():
                box["detail"] = self._safe_detail()

            def run_balance():
                box["balance"] = self._safe_balance()

            t1 = threading.Thread(target=run_detail, daemon=True)
            t2 = threading.Thread(target=run_balance, daemon=True)
            t1.start()
            t2.start()
            t1.join()
            t2.join()
            # 结果投递给主线程（_tick 中统一渲染），避免在子线程直接操作 Tk
            self._result_q.put((box.get("detail"), box.get("balance"), auto))

        threading.Thread(target=work, daemon=True).start()

    @staticmethod
    def _safe_detail():
        """后台线程：取「累计领取」（签到积分）。永不抛异常。"""
        try:
            return query_credits_detail(timeout=8)
        except Exception as e:
            return {"ok": False, "error": "签到查询异常: %s" % e}

    @staticmethod
    def _safe_balance():
        """后台线程：取「积分余额」（额度）。永不抛异常。"""
        try:
            return query_balance()
        except Exception as e:
            return {"ok": False, "error": "余额查询异常: %s" % e}

    def _render(self, res, bal_res=None, auto=False):
        self.busy = False
        self._anim_on = False
        now = datetime.now().strftime("%H:%M:%S")
        bal_res = bal_res if isinstance(bal_res, dict) else {"ok": False, "error": None}

        # 两个口径各自渲染，互不影响；底部状态按整体结果给出
        self._render_balance(bal_res)
        self._render_detail(res, now)
        d_ok = bool(isinstance(res, dict) and res.get("ok"))
        b_ok = bool(bal_res.get("ok"))

        # 刷新失败 3 秒重试（仅自动周期轮次）：
        #   · 自动轮任一口径失败且本周期尚未重试 → 3 秒后再主动刷新一次；
        #   · 重试仍失败 → 保持当前数据，等下一轮 10 分钟周期（_auto 会重置标记）；
        #   · 自动轮成功 → 复位标记，恢复正常周期；
        #   · 手动刷新（auto=False）不参与重试判定，避免与自动周期串扰。
        if auto:
            if not (d_ok and b_ok):
                if not self._retry_scheduled:
                    self._retry_scheduled = True
                    self.foot_lbl.configure(text="获取失败，3 秒后自动重试…", fg=WARN)
                    self.root.after(3000, lambda: self.refresh_async(auto=True))
            else:
                self._retry_scheduled = False

        if d_ok and b_ok:
            self.foot_lbl.configure(text="更新于 %s · 每 10 分钟自动刷新" % now, fg=FG_FAINT)
        elif d_ok or b_ok:
            miss = "积分余额" if d_ok else "累计领取"
            self.foot_lbl.configure(text="更新于 %s · %s 获取失败" % (now, miss), fg=WARN)
        else:
            self.foot_lbl.configure(text="读取失败，点 ↻ 重试 · %s" % now, fg=ERR)
        # 文案变化可能改变所需高度（如错误提示变长），渲染后重新贴合窗口
        self.root.after_idle(self._fit_window)
        # 签到状态可能已变化（手动/自动签到成功等），同步按钮态
        self._update_checkin_ui()

    def _render_balance(self, b):
        """渲染「积分余额」（额度口径）。失败只影响本区域。"""
        ok = bool(b.get("ok"))
        val = b.get("balance")
        if ok and val is not None:
            text = b.get("display") or ("%.2f" % val)
            self.c_bal_val.configure(text=text, fg=FG)
            self.e_bal_val.configure(text=text, fg=ACCENT)
            tc, uc = b.get("total_capacity"), b.get("used_capacity")
            if tc is not None and uc is not None:
                sub = "总额度 %.2f · 已用 %.2f" % (tc, uc)
            elif tc is not None:
                sub = "总额度 %.2f" % tc
            else:
                sub = "额度剩余"
            self.e_bal_sub.configure(text=sub, fg=FG_DIM)
        else:
            err = (b.get("error") or "暂不可用")[:18]
            self.c_bal_val.configure(text="—", fg=ERR)
            self.e_bal_val.configure(text="—", fg=ERR)
            self.e_bal_sub.configure(text="余额获取失败：%s" % err, fg=ERR)
        self.last_balance = b

    def _render_detail(self, res, now):
        """渲染「累计领取」（签到积分口径）。失败只影响本区域。"""
        res = res if isinstance(res, dict) else {"ok": False, "error": None}
        if not res.get("ok"):
            err = (res.get("error") or "未知错误")[:30]
            self.c_balance.configure(text="—", fg=ERR)
            self.c_sub.configure(text="获取失败：%s" % err, fg=ERR)
            self.e_balance.configure(text="—", fg=ERR)
            self.e_streak.configure(text="—", fg=ERR)
            self.e_today.configure(text="—", fg=ERR)
            self.e_month.configure(text="—")
            self.e_week.configure(text="—")
            self.e_activity.configure(text="—")
            self.e_period.configure(text="")
            self.e_remain.configure(text="")
            for c in self.week_cells:
                c.configure(bg=BG_SOFT, fg=FG_DIM)
            self.last = res
            return

        self.last = res
        bal = res.get("balance")
        streak = res.get("streak")
        checked = res.get("checked_in")
        month_days = res.get("month_days")
        total_days = res.get("total_days")
        wk, wt = res.get("week_days"), res.get("week_total") or 7
        prog = res.get("week_progress") or []
        an = res.get("activity_name") or "积分活动"
        tn = res.get("theme_name") or ""
        season = res.get("season")
        active = res.get("active")
        st = (res.get("start_time") or "")[:10]
        et = (res.get("end_time") or "")[:10]
        remain = res.get("remain_days")
        forecast = res.get("forecast_credit")

        # 收缩态
        self.c_balance.configure(text=fmt_int(bal), fg=ACCENT)
        parts = []
        if streak is not None:
            parts.append("连续 %d 天" % streak)
        parts.append("今日已记录" if checked else "今日未记录")
        self.c_sub.configure(text=" · ".join(parts), fg=FG_DIM)

        # 展开态 - 三栏
        self.e_balance.configure(text=fmt_int(bal), fg=ACCENT)
        self.e_streak.configure(text=str(streak if streak is not None else "—"), fg=ACCENT)
        self.e_today.configure(text="已记录" if checked else "未记录",
                               fg=ACCENT if checked else WARN)

        # 活跃地图
        self.e_month.configure(text="本月已记录 %s 天，累计 %s 天"
                                    % (month_days if month_days is not None else "—",
                                       total_days if total_days is not None else "—"))

        # 本周进度
        left = max(0, wt - (wk or 0))
        self.e_week.configure(text="已完成 %s / %d 天（还差 %d 天满勤）"
                                   % (wk if wk is not None else "—", wt, left))
        for i, cell in enumerate(self.week_cells):
            done = bool(prog[i]) if i < len(prog) else False
            cell.configure(bg=ACCENT if done else BG_SOFT,
                           fg=ACCENT_TXT if done else FG_DIM)

        # 本期活动
        tag = " [进行中]" if active else ""
        season_txt = " / 第%s期" % season if season else ""
        theme_txt = "%s%s" % (tn, season_txt) if tn else ""
        self.e_activity.configure(text="%s（%s）%s" % (an, theme_txt, tag) if theme_txt
                                  else "%s%s" % (an, tag))
        self.e_period.configure(text="周期：%s ~ %s" % (st, et) if st or et else "")
        seg = []
        if remain is not None:
            seg.append("活动剩余 %d 天" % remain)
        if forecast:
            seg.append("若全勤预计再得 %s 分" % fmt_int(forecast))
        self.e_remain.configure(text=" · ".join(seg))

    def _tick(self):
        if self._anim_on:
            self.anim_i = (self.anim_i + 1) % 4
            self.refresh_btn.configure(text="|/-\\"[self.anim_i])
        else:
            self.refresh_btn.configure(text="↻")
        # auto 主题下定期检测系统深浅色变化（约每 60s 一次），变化则自动跟随
        self._tick_n = getattr(self, "_tick_n", 0) + 1
        if self._tick_n % 300 == 0:
            self._auto_check_theme()
        # 取回后台线程的查询结果，在主线程渲染
        try:
            while True:
                detail, balance, _auto = self._result_q.get_nowait()
                try:
                    self._render(detail, balance, _auto)
                except Exception:
                    pass
        except queue.Empty:
            pass
        # 取回签到写请求的结果（手动 / 自动签到），在主线程更新按钮态
        try:
            while True:
                sign_res = self._signin_q.get_nowait()
                try:
                    self._handle_signin_result(sign_res)
                except Exception:
                    self._signin_busy = False
        except queue.Empty:
            pass
        # 取回派猫猫旅行只读查询结果（status/config），渲染状态机
        try:
            while True:
                travel_box = self._travel_q.get_nowait()
                try:
                    self._handle_travel_result(travel_box)
                except Exception:
                    pass
        except queue.Empty:
            pass
        # 取回旅行写接口 / 自动流程结果（派遣/领取/auto），更新通知与状态
        try:
            while True:
                travel_cmd = self._travel_cmd_q.get_nowait()
                try:
                    self._handle_travel_cmd(travel_cmd)
                except Exception:
                    self._travel_cmd_busy = False
        except queue.Empty:
            pass
        # 每秒刷新旅行倒计时文本（本地换算，不重新发请求）
        if self._tick_n % 5 == 0:
            try:
                self._update_travel_countdown()
            except Exception:
                pass
        # 取回托盘图标回调事件（左键→恢复，右键→弹菜单），在主线程分发
        try:
            while True:
                tray_evt = self.tray.q.get_nowait()
                try:
                    self._on_tray_event(tray_evt)
                except Exception:
                    pass
        except queue.Empty:
            pass
        self.root.after(200, self._tick)

    def _auto(self):
        # 新一轮 10 分钟周期开始：复位重试标记，确保本轮失败仍有一次 3 秒补偿重试
        self._retry_scheduled = False
        self.refresh_async(auto=True)
        # 自动签到：每轮（10 分钟）未签到则自动签，已签到不动作
        self._checkin_if_needed()
        # 自动派遣：每轮判断一次（未派遣→派；进行中→等领取；到达→领；已结束→今日不再动作）
        try:
            self._auto_dispatch_once()
        except Exception:
            pass
        self.root.after(AUTO_REFRESH_MS, self._auto)

    def quit(self):
        self.cfg["x"] = self.root.winfo_x()
        self.cfg["y"] = self.root.winfo_y()
        self.cfg["expanded"] = self.expanded
        save_cfg(self.cfg)
        try:
            self.tray.remove()          # 退托盘图标，进程零残留
        except Exception:
            pass
        self.root.destroy()


def main():
    root = tk.Tk()
    app = Widget(root)
    # 托盘常驻：启动即创建托盘图标，最小化/最大化只影响窗口本身，不影响托盘。
    try:
        app.tray.add()
    except Exception:
        pass
    root.after(AUTO_REFRESH_MS, app._auto)
    # 自动签到：若上次开启过，启动后稍候检查一次（未签到则自动签）
    if app.auto_checkin:
        root.after(1500, app._checkin_if_needed)
    # 自动派遣：若上次开启过，启动后稍候触发一轮自动流程（未派遣则自动派）
    if app.auto_dispatch:
        root.after(2500, app._auto_dispatch_once)
    # 登录态缺失提醒：启动时检测一次，无 accessToken 则发一次系统通知，不重复轰炸
    root.after(2500, app._check_auth_once)
    root.mainloop()


if __name__ == "__main__":
    main()
