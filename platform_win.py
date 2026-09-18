# -*- coding: utf-8 -*-
"""Windows 专属后端（platform_win）——与 v2.1.0 行为完全一致。

从主程序 workbuddy_credit_widget.py 抽离的 Windows 专属实现，全部保留：
  · DWM 系统圆角（DWMWA_WINDOW_CORNER_PREFERENCE）+ 补 WS_THICKFRAME 子类化
    （WM_NCCALCSIZE / WM_NCPAINT / WM_NCACTIVATE / WM_NCHITTEST 拦截）
  · DWM 非客户区配色（消除失焦“白边”）+ 沉浸式深色
  · SetWindowRgn 1bit 圆角回退
  · Shell_NotifyIcon 托盘（独立线程消息循环 + q 队列）
  · GetSystemMetrics 虚拟屏多屏坐标
  · winreg 系统深浅色

统一接口（由 platform_adapter 重导出）：
    PLATFORM_NAME / ui_font / num_font / system_light_theme / virtual_screen /
    apply_window_round_corners / refresh_window_decor / set_app_user_model_id /
    create_tray / notify
"""
import os
import sys
import time
import queue
import threading
import ctypes
import ctypes.wintypes  # noqa: F401  (wintypes.MSG / wintypes.POINT)

PLATFORM_NAME = "Windows"
FONT_UI = "Microsoft YaHei UI"
FONT_NUM = "Segoe UI"


def ui_font():
    return FONT_UI


def num_font():
    return FONT_NUM


# ---------------- 系统深浅色（winreg） ----------------
def system_light_theme():
    """Win11 系统深浅色：HKCU\\...\\Themes\\Personalize\\AppsUseLightTheme。
    返回 True=浅色 / False=深色；读取失败按深色处理。"""
    try:
        import winreg
        k = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                           r"SOFTWARE\Microsoft\Windows\CurrentVersion\Themes\Personalize")
        v, _ = winreg.QueryValueEx(k, "AppsUseLightTheme")
        winreg.CloseKey(k)
        return bool(v)
    except Exception:
        return False


def set_app_user_model_id():
    """让任务栏使用 exe 自带的图标资源（替代默认 python/tk 图标）。"""
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
            "WorkBuddy.CreditWidget.Desktop")
    except Exception:
        pass


# ---------------- 圆角 / 白边 / 非客户区 ----------------
RADIUS_WIN_DWM = 8
RADIUS_WIN_RGN = 16

# Win32 常量
WS_EX_LAYERED = 0x00080000
GWL_EXSTYLE = -20
GWL_STYLE = -16
GWLP_WNDPROC = -4
WS_CAPTION = 0x00C00000
WS_THICKFRAME = 0x00040000
WM_NCCALCSIZE = 0x0083
WM_NCHITTEST = 0x0084
WM_NCPAINT = 0x0085
WM_NCACTIVATE = 0x0086
WM_NCMOUSEMOVE = 0x00A0
WM_NCLBUTTONDOWN = 0x00A1
DWMWA_BORDER_COLOR = 34
DWMWA_CAPTION_COLOR = 35
DWMWA_USE_IMMERSIVE_DARK_MODE = 20
WM_NCDESTROY = 0x0082
SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_NOZORDER = 0x0004
SWP_FRAMECHANGED = 0x0020
DWMWA_WINDOW_CORNER_PREFERENCE = 33
DWMWCP_ROUND = 2


class _MARGINS(ctypes.Structure):
    _fields_ = [("cxLeftWidth", ctypes.c_int), ("cxRightWidth", ctypes.c_int),
                ("cyTopHeight", ctypes.c_int), ("cyBottomHeight", ctypes.c_int)]


class _POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


# 回调签名（LRESULT 在 64 位是 64 位，默认 c_int 会截断）
try:
    _CWP = ctypes.windll.user32.CallWindowProcW
    _CWP.restype = ctypes.c_ssize_t
    _CWP.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint,
                     ctypes.c_size_t, ctypes.c_ssize_t]
except Exception:
    pass
_WNDPROC_KEEP = {}   # hwnd -> 回调对象，必须常驻，否则被 GC 后消息处理崩溃


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


def _get_wnd_long(hwnd, idx):
    fn = getattr(ctypes.windll.user32, "GetWindowLongPtrW", None)
    if fn is None:
        fn = ctypes.windll.user32.GetWindowLongW
    fn.argtypes = [ctypes.c_void_p, ctypes.c_int]
    fn.restype = ctypes.c_ssize_t
    return fn(ctypes.c_void_p(int(hwnd)), idx)


def _set_wnd_long(hwnd, idx, val):
    fn = getattr(ctypes.windll.user32, "SetWindowLongPtrW", None)
    if fn is None:
        fn = ctypes.windll.user32.SetWindowLongW
    fn.argtypes = [ctypes.c_void_p, ctypes.c_int,
                   ctypes.c_void_p if isinstance(val, ctypes.c_void_p) else ctypes.c_ssize_t]
    fn.restype = ctypes.c_ssize_t
    # val 可能是窗口过程地址，必须按指针传，走 Python int 会在 64 位下被截断
    return fn(ctypes.c_void_p(int(hwnd)), idx, val)


def _install_frameless_dwm(hwnd, bg=None, dark=None):
    """补回 DWM 圆角需要的边框样式，再用 WM_NCCALCSIZE 把非客户区清零。"""
    if int(hwnd) in _WNDPROC_KEEP:
        return True
    try:
        WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, ctypes.c_void_p,
                                     ctypes.c_uint, ctypes.c_size_t, ctypes.c_ssize_t)
        old = _get_wnd_long(hwnd, GWLP_WNDPROC)
        if not old:
            return False

        def _proc(h, msg, wp, lp):
            if msg == WM_NCCALCSIZE:
                _dbg("NCCALCSIZE intercepted (wp=%s)" % bool(wp))
                return 0   # 客户区 = 窗口矩形：不画标题栏、不留边框
            if msg == WM_NCPAINT:
                _dbg("NCPAINT blocked")
                return 0
            if msg == WM_NCACTIVATE:
                return 1
            if msg in (WM_NCMOUSEMOVE, WM_NCLBUTTONDOWN):
                return 0
            if msg == WM_NCDESTROY:
                _set_wnd_long(h, GWLP_WNDPROC, old)
                _WNDPROC_KEEP.pop(int(hwnd), None)
                return _CWP(ctypes.c_void_p(old), ctypes.c_void_p(h),
                            ctypes.c_uint(msg), ctypes.c_size_t(wp), ctypes.c_ssize_t(lp))
            r = _CWP(ctypes.c_void_p(old), ctypes.c_void_p(h), ctypes.c_uint(msg),
                     ctypes.c_size_t(wp), ctypes.c_ssize_t(lp))
            if msg == WM_NCHITTEST and 10 <= r <= 18:
                return 1   # HTCLIENT
            return r

        cb = WNDPROC(_proc)
        _dbg("hwnd=%s oldproc=%s cb=%s" % (hwnd, hex(int(old)), ctypes.cast(cb, ctypes.c_void_p).value))
        r1 = _set_wnd_long(hwnd, GWLP_WNDPROC, ctypes.cast(cb, ctypes.c_void_p))
        _dbg("set wndproc -> %s err=%s" % (r1, ctypes.windll.kernel32.GetLastError()))
        _WNDPROC_KEEP[int(hwnd)] = cb
        _set_wnd_long(hwnd, GWL_STYLE,
                      _get_wnd_long(hwnd, GWL_STYLE) | WS_THICKFRAME)
        _dbg("style now=%s" % hex(_get_wnd_long(hwnd, GWL_STYLE)))
        ctypes.windll.user32.SetWindowPos(
            ctypes.c_void_p(int(hwnd)), None, 0, 0, 0, 0,
            SWP_NOSIZE | SWP_NOMOVE | SWP_NOZORDER | SWP_FRAMECHANGED)
        _set_nc_colors(hwnd, bg, dark)
        if os.environ.get("WIDGET_EXTEND_FRAME") == "1":
            m = _MARGINS(-1, -1, -1, -1)
            r3 = ctypes.windll.dwmapi.DwmExtendFrameIntoClientArea(
                ctypes.c_void_p(int(hwnd)), ctypes.byref(m))
            _dbg("ExtendFrame -> %s" % r3)
        return True
    except Exception:
        return False


def _set_nc_colors(hwnd, bg=None, dark=None):
    """把 DWM 非客户区（边框/标题栏）配色设成挂件底色，消除失焦露出的 1px 白边。"""
    if bg is None:
        return False
    try:
        s = bg.lstrip("#")
        r, g, b = int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16)
        colorref = (b << 16) | (g << 8) | r
        h = ctypes.c_void_p(int(hwnd))
        for attr in (DWMWA_BORDER_COLOR, DWMWA_CAPTION_COLOR):
            v = ctypes.c_int(colorref)
            ctypes.windll.dwmapi.DwmSetWindowAttribute(
                h, ctypes.c_uint(attr), ctypes.byref(v), ctypes.sizeof(v))
        v = ctypes.c_int(1 if dark else 0)
        ctypes.windll.dwmapi.DwmSetWindowAttribute(
            h, ctypes.c_uint(DWMWA_USE_IMMERSIVE_DARK_MODE),
            ctypes.byref(v), ctypes.sizeof(v))
        return True
    except Exception:
        return False


def _is_win11_or_later():
    try:
        return os.name == "nt" and sys.getwindowsversion().build >= 22000
    except Exception:
        return False


def _set_dwm_round_corner(hwnd):
    try:
        pref = ctypes.c_int(DWMWCP_ROUND)
        hr = ctypes.windll.dwmapi.DwmSetWindowAttribute(
            ctypes.c_void_p(int(hwnd)), ctypes.c_uint(DWMWA_WINDOW_CORNER_PREFERENCE),
            ctypes.byref(pref), ctypes.sizeof(pref))
        return hr == 0
    except Exception:
        return False


def _top_hwnd(widget):
    try:
        hwnd = widget.winfo_id()
        parent = ctypes.windll.user32.GetParent(ctypes.c_void_p(int(hwnd)))
        return int(parent) if parent else int(hwnd)
    except Exception:
        return 0


def _is_layered(hwnd):
    try:
        return bool(ctypes.windll.user32.GetWindowLongPtrW(
            ctypes.c_void_p(int(hwnd)), GWL_EXSTYLE) & WS_EX_LAYERED)
    except Exception:
        try:
            return bool(ctypes.windll.user32.GetWindowLongW(
                ctypes.c_void_p(int(hwnd)), GWL_EXSTYLE) & WS_EX_LAYERED)
        except Exception:
            return False


# ---------------- 多屏虚拟屏 ----------------
def virtual_screen(root=None):
    """虚拟屏全范围（含所有显示器）：返回 [X, Y, W, H]。失败回退单屏。"""
    u = ctypes.windll.user32
    try:
        x = u.GetSystemMetrics(76)
        y = u.GetSystemMetrics(77)
        w = u.GetSystemMetrics(78)
        h = u.GetSystemMetrics(79)
    except Exception:
        x = y = 0
        w = h = 0
    if w <= 0 or h <= 0:
        try:
            w = u.GetSystemMetrics(0)
            h = u.GetSystemMetrics(1)
        except Exception:
            w, h = 1920, 1080
    return [int(x), int(y), int(w), int(h)]


# ---------------- 窗口圆角（对外接口） ----------------
def apply_window_round_corners(root, bg=None, dark=None):
    """窗口圆角：优先 Win11 DWM 系统圆角，回退 CreatePolygonRgn + SetWindowRgn。

    返回模式字符串："dwm" 或 "rgn"。尺寸变化（收缩↔展开）后必须重新调用，
    否则圆角停留在旧尺寸。任何异常静默，绝不抛出。
    """
    mode = ""
    try:
        root.update_idletasks()
        hwnd = _top_hwnd(root)
        w, h = root.winfo_width(), root.winfo_height()
        if not hwnd or w < 10 or h < 10:
            return ""
        if _is_win11_or_later() and _install_frameless_dwm(hwnd, bg, dark) \
                and _set_dwm_round_corner(hwnd):
            ctypes.windll.user32.SetWindowRgn(ctypes.c_void_p(hwnd),
                                              ctypes.c_void_p(0), True)
            mode = "dwm"
        else:
            pts = []
            _round_rect_pts(pts, w + 1, h + 1, RADIUS_WIN_RGN, steps=24)
            n = len(pts) // 2
            arr = (_POINT * n)(*[(int(round(pts[i * 2])), int(round(pts[i * 2 + 1])))
                                 for i in range(n)])
            rgn = ctypes.windll.gdi32.CreatePolygonRgn(
                ctypes.byref(arr), ctypes.c_int(n), ctypes.c_int(2))
            if rgn:
                ctypes.windll.user32.SetWindowRgn(ctypes.c_void_p(hwnd),
                                                  ctypes.c_void_p(rgn), True)
            mode = "rgn"
    except Exception:
        pass
    return mode


def _round_rect_pts(pts, w, h, r, steps=18):
    """圆角矩形密集采样点（与主程序 _round_rect_points 同算法），写入 pts 列表。"""
    import math
    r = int(max(1, min(r, (w - 2) // 2, (h - 2) // 2)))
    x1, y1, x2, y2 = 0.0, 0.0, float(w - 1), float(h - 1)
    corners = ((x2 - r, y2 - r, 0.0),
               (x1 + r, y2 - r, 90.0),
               (x1 + r, y1 + r, 180.0),
               (x2 - r, y1 + r, 270.0))
    for cx, cy, a0 in corners:
        for i in range(steps + 1):
            a = math.radians(a0 + 90.0 * i / steps)
            pts.append(cx + r * math.cos(a))
            pts.append(cy + r * math.sin(a))


def refresh_window_decor(root, bg=None, dark=None):
    """主题切换后重设 DWM 边框/标题栏配色（随新底色），防失焦白边复发。"""
    try:
        hwnd = _top_hwnd(root)
        if hwnd:
            _set_nc_colors(hwnd, bg, dark)
    except Exception:
        pass


# ---------------- 系统托盘（ctypes Shell_NotifyIcon） ----------------
TRAY_ID = 1
WM_TRAY = 0x0801
TRAY_WM_LBUTTONUP = 0x0202
TRAY_WM_RBUTTONUP = 0x0205
NIM_ADD, NIM_MODIFY, NIM_DELETE = 0, 1, 2
NIF_MESSAGE, NIF_ICON, NIF_TIP = 0x01, 0x02, 0x04
NIF_INFO = 0x10
IMAGE_ICON, LR_LOADFROMFILE, LR_DEFAULTSIZE = 1, 0x10, 0x40
WM_QUIT = 0x0012


class _NOTIFYICONDATAW(ctypes.Structure):
    _fields_ = [
        ("cbSize", ctypes.c_uint),
        ("hWnd", ctypes.c_void_p),
        ("uID", ctypes.c_uint),
        ("uFlags", ctypes.c_uint),
        ("uCallbackMessage", ctypes.c_uint),
        ("hIcon", ctypes.c_void_p),
        ("szTip", ctypes.c_wchar * 128),
        ("dwState", ctypes.c_uint),
        ("dwStateMask", ctypes.c_uint),
        ("szInfo", ctypes.c_wchar * 256),
        ("uVersion", ctypes.c_uint),
        ("szInfoTitle", ctypes.c_wchar * 64),
        ("dwInfoFlags", ctypes.c_uint),
        ("guidItem", ctypes.c_byte * 16),
        ("hBalloonIcon", ctypes.c_void_p),
    ]


class _WNDCLASSEXW(ctypes.Structure):
    _fields_ = [
        ("cbSize", ctypes.c_uint),
        ("style", ctypes.c_uint),
        ("lpfnWndProc", ctypes.c_void_p),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", ctypes.c_void_p),
        ("hIcon", ctypes.c_void_p),
        ("hCursor", ctypes.c_void_p),
        ("hbrBackground", ctypes.c_void_p),
        ("lpszMenuName", ctypes.c_wchar_p),
        ("lpszClassName", ctypes.c_wchar_p),
        ("hIconSm", ctypes.c_void_p),
    ]


class TrayIcon(object):
    """系统托盘图标（Shell_NotifyIcon），独立线程消息循环。

    回调（WM_TRAY，lParam=鼠标事件）在主线程 _tick 里每 200ms 从 q 取——
    统一事件为字符串："left"（左键）/ "right"（右键）。右键菜单坐标由主线程
    用 Tk 的 winfo_pointerx/pointery 获取，不依赖 GetCursorPos。
    """

    def __init__(self, icon_path=None):
        self.added = False
        self.hwnd = None
        self.icon = None
        self.q = queue.Queue()
        self._icon_path = icon_path
        self._thread = None
        self._ready = threading.Event()
        self._wndproc_ref = None
        self._wndcls_registered = False
        self.nid = None
        self._run_error = None

    # ---------- 消息窗口 / 线程 ----------
    def _create_message_window(self):
        u32 = ctypes.windll.user32
        u32.RegisterClassExW.argtypes = [ctypes.POINTER(_WNDCLASSEXW)]
        u32.RegisterClassExW.restype = ctypes.c_ushort
        u32.CreateWindowExW.argtypes = [
            ctypes.c_uint, ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint,
            ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
            ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
        u32.CreateWindowExW.restype = ctypes.c_void_p
        u32.LoadImageW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p,
                                   ctypes.c_uint, ctypes.c_int, ctypes.c_int,
                                   ctypes.c_uint]
        u32.LoadImageW.restype = ctypes.c_void_p
        shell32 = ctypes.windll.shell32
        shell32.Shell_NotifyIconW.argtypes = [ctypes.c_uint, ctypes.c_void_p]
        shell32.Shell_NotifyIconW.restype = ctypes.c_int
        u32.GetMessageW.argtypes = [ctypes.POINTER(ctypes.wintypes.MSG),
                                    ctypes.c_void_p, ctypes.c_uint, ctypes.c_uint]
        u32.GetMessageW.restype = ctypes.c_int
        u32.DestroyWindow.argtypes = [ctypes.c_void_p]
        u32.DestroyWindow.restype = ctypes.c_int
        u32.PostMessageW.argtypes = [ctypes.c_void_p, ctypes.c_uint,
                                     ctypes.c_size_t, ctypes.c_ssize_t]
        u32.PostMessageW.restype = ctypes.c_int
        u32.DestroyIcon.argtypes = [ctypes.c_void_p]
        u32.DestroyIcon.restype = ctypes.c_int

        WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, ctypes.c_void_p,
                                     ctypes.c_uint, ctypes.c_size_t, ctypes.c_ssize_t)

        def _proc(hw, msg, wp, lp):
            if msg == WM_TRAY:
                try:
                    ev = int(lp) & 0xFFFF
                    if ev == TRAY_WM_RBUTTONUP:
                        self.q.put("right")
                    elif ev == TRAY_WM_LBUTTONUP:
                        self.q.put("left")
                except Exception:
                    pass
                return 0
            try:
                return u32.DefWindowProcW(ctypes.c_void_p(hw),
                                          ctypes.c_uint(msg),
                                          ctypes.c_size_t(wp), ctypes.c_ssize_t(lp))
            except Exception:
                return 0

        self._wndproc_ref = WNDPROC(_proc)
        cls = "WorkBuddyCreditWidgetTrayWnd"
        hinst = ctypes.cast(ctypes.windll.kernel32.GetModuleHandleW(None),
                            ctypes.c_void_p).value
        if not self._wndcls_registered:
            wc = _WNDCLASSEXW()
            wc.cbSize = ctypes.sizeof(_WNDCLASSEXW)
            wc.style = 0
            wc.lpfnWndProc = ctypes.cast(self._wndproc_ref, ctypes.c_void_p).value
            wc.cbClsExtra = 0
            wc.cbWndExtra = 0
            wc.hInstance = hinst
            wc.hIcon = None
            wc.hCursor = None
            wc.hbrBackground = None
            wc.lpszMenuName = None
            wc.lpszClassName = cls
            wc.hIconSm = None
            try:
                if u32.RegisterClassExW(ctypes.byref(wc)):
                    self._wndcls_registered = True
            except Exception:
                pass
        if not self._wndcls_registered:
            self._wndcls_registered = True
        try:
            hwnd = u32.CreateWindowExW(0, cls, cls, 0, 0, 0, 0, 0,
                                       None, None, hinst, None)
        except Exception:
            hwnd = None
        return hwnd if hwnd else None

    def _run(self):
        try:
            u32 = ctypes.windll.user32
            hwnd = self._create_message_window()
            if not hwnd:
                self.added = False
                self._ready.set()
                return
            self.hwnd = hwnd
            self._add_icon(hwnd)
            self._ready.set()
            msg = ctypes.wintypes.MSG()
            while True:
                try:
                    r = u32.GetMessageW(ctypes.byref(msg), None, 0, 0)
                except Exception:
                    break
                if r <= 0:
                    break
                try:
                    u32.TranslateMessage(ctypes.byref(msg))
                    u32.DispatchMessageW(ctypes.byref(msg))
                except Exception:
                    break
            self._delete_icon()
            try:
                u32.DestroyWindow(ctypes.c_void_p(int(hwnd)))
            except Exception:
                pass
            self.hwnd = None
            self.added = False
        except Exception:
            try:
                import traceback
                self._run_error = traceback.format_exc()
            except Exception:
                self._run_error = "tray thread error"
            self.added = False
            self.hwnd = None
            self._ready.set()

    # ---------- 图标 ----------
    def _load_icon(self):
        path = self._icon_path
        if not path:
            return None
        try:
            h = ctypes.windll.user32.LoadImageW(
                None, path, IMAGE_ICON, 0, 0,
                LR_LOADFROMFILE | LR_DEFAULTSIZE)
            return h if h else None
        except Exception:
            return None

    def _add_icon(self, hwnd):
        if not self.icon:
            self.icon = self._load_icon()
        nid = _NOTIFYICONDATAW()
        nid.cbSize = ctypes.sizeof(_NOTIFYICONDATAW)
        nid.hWnd = ctypes.c_void_p(int(hwnd))
        nid.uID = TRAY_ID
        nid.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP
        nid.uCallbackMessage = WM_TRAY
        nid.hIcon = ctypes.c_void_p(int(self.icon) if self.icon else 0)
        nid.szTip = "WorkBuddy积分助手"
        self.nid = nid
        try:
            self.added = bool(ctypes.windll.shell32.Shell_NotifyIconW(
                NIM_ADD, ctypes.byref(nid)))
        except Exception:
            self.added = False
        return self.added

    def _delete_icon(self):
        if self.nid is not None:
            try:
                ctypes.windll.shell32.Shell_NotifyIconW(
                    NIM_DELETE, ctypes.byref(self.nid))
            except Exception:
                pass
            self.nid = None
        if self.icon:
            try:
                ctypes.windll.user32.DestroyIcon(ctypes.c_void_p(int(self.icon)))
            except Exception:
                pass
            self.icon = None

    # ---------- 添加 / 移除 ----------
    def add(self):
        if self.added:
            return True
        if self._thread is None or not self._thread.is_alive():
            self._ready = threading.Event()
            self._thread = threading.Thread(target=self._run, daemon=True)
            self._thread.start()
        self._ready.wait(timeout=4)
        return self.added

    def remove(self):
        hwnd = self.hwnd
        if hwnd:
            try:
                ctypes.windll.user32.PostMessageW(
                    ctypes.c_void_p(int(hwnd)), WM_QUIT, 0, 0)
            except Exception:
                pass
            self.hwnd = None
        self.added = False
        if self._thread is not None:
            try:
                self._thread.join(timeout=1.0)
            except Exception:
                pass
            if not self._thread.is_alive():
                self._thread = None
        if self.nid is not None:
            self._delete_icon()

    # ---------- 气泡通知（NIF_INFO，纯 Shell_NotifyIcon）----------
    def show_balloon(self, title, text):
        if not self.added or self.nid is None:
            return False
        try:
            nid = self.nid
            nid.uFlags = NIF_INFO
            nid.szInfo = (text or "")[:255]
            nid.szInfoTitle = (title or "")[:63]
            nid.dwInfoFlags = 0x01  # NIIF_INFO
            ctypes.windll.shell32.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(nid))
            nid.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP
            return True
        except Exception:
            return False


def create_tray(icon_path=None):
    """创建 Windows 托盘对象（Shell_NotifyIcon）。icon_path 传 .ico 绝对路径。"""
    return TrayIcon(icon_path=icon_path)


def notify(title, text):
    """Windows 独立系统通知：主程序统一走 tray.show_balloon（托盘气泡即会进入
    通知中心）。此函数供托盘不可用时的兜底调用，无句柄时静默返回 False。"""
    return False
