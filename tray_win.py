# -*- coding: utf-8 -*-
"""托盘图标（系统托盘最小化）—— 零第三方依赖，纯 ctypes 实现。

设计约定：
- 只在 Windows 上可用（其他平台 supported() 返回 False，调用方直接跳过）。
- 所有 Win32 调用失败都静默降级：托盘只是锦上添花，绝不能因为托盘挂掉把提醒弄死。
- 菜单项由调用方传入 [(id, 文字或None, 回调)]，回调在托盘线程里执行。
- stop() 负责删除图标并结束消息循环，进程退出前必须调用，否则托盘会留一个死图标。

三个踩过的坑（都写进 tests/test_tray.py 锁住了）：
1. `Shell_NotifyIconW` / `ExtractIconExW` 在 **shell32**，不在 user32。
2. 宿主窗口必须是**能被设为前台的顶层窗口**。message-only 窗口（HWND_MESSAGE）
   不能成为前台窗口，TrackPopupMenu 因此弹不出来；弹之前 SetForegroundWindow
   又会失败或抢到前台后不还 —— 表现就是「点托盘没反应 + 别的程序输入法失灵」。
3. 弹完菜单（或弹失败）必须把前台还给原来的窗口，否则前台停在一个不可见的窗口上。
"""
import ctypes
import os
import sys
import threading
import time

WM_APP_TRAY = 0x8001          # 托盘回调消息（WM_APP+1）
WM_NULL = 0x0000
WM_DESTROY = 0x0002
WM_CLOSE = 0x0010
WM_CONTEXTMENU = 0x007B
WM_LBUTTONUP = 0x0202
WM_LBUTTONDBLCLK = 0x0203
WM_RBUTTONUP = 0x0205
WM_USER = 0x0400
NIN_SELECT = WM_USER + 0
NIN_KEYSELECT = WM_USER + 1
WS_POPUP = 0x80000000

NIM_ADD, NIM_MODIFY, NIM_DELETE = 0, 1, 2
NIF_MESSAGE, NIF_ICON, NIF_TIP, NIF_INFO = 1, 2, 4, 0x10
NIIF_INFO = 0x01
MF_STRING = 0x0000
MF_SEPARATOR = 0x0800
TPM_BOTTOMALIGN, TPM_RIGHTBUTTON, TPM_RETURNCMD = 0x0001, 0x0002, 0x0100
TPM_LEFTALIGN = 0x0000
IMAGE_ICON = 1
LR_LOADFROMFILE = 0x0010
IDI_APPLICATION = 32512
SW_HIDE = 0


class _POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


class _MSG(ctypes.Structure):
    _fields_ = [("hwnd", ctypes.c_void_p), ("message", ctypes.c_uint),
                ("wParam", ctypes.c_size_t), ("lParam", ctypes.c_ssize_t),
                ("time", ctypes.c_ulong), ("pt_x", ctypes.c_long),
                ("pt_y", ctypes.c_long), ("lPrivate", ctypes.c_ulong)]


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
        ("unionVersion", ctypes.c_uint),
        ("szInfoTitle", ctypes.c_wchar * 64),
        ("dwInfoFlags", ctypes.c_uint),
        ("guidItem", ctypes.c_ubyte * 16),
    ]


_WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, ctypes.c_void_p,
                              ctypes.c_uint, ctypes.c_size_t, ctypes.c_ssize_t)

_U32 = _SH32 = _K32 = None


def _apis():
    """集中声明 Win32 原型。

    必须显式 set argtypes / restype：
    - 64 位下句柄是 64 位，ctypes 默认按 32 位 int 处理会截断
      （GetForegroundWindow 返回值截断后比较失真，前台恢复就会判断错）；
    - use_last_error=True 才能拿到真实错误码（否则被 ctypes 内部调用污染）。
    """
    global _U32, _SH32, _K32
    if _U32 is not None:
        return _U32, _SH32, _K32
    u32 = ctypes.WinDLL("user32", use_last_error=True)
    sh32 = ctypes.WinDLL("shell32", use_last_error=True)
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)

    H = ctypes.c_void_p
    u32.CreateWindowExW.argtypes = [ctypes.c_ulong, ctypes.c_wchar_p, ctypes.c_wchar_p,
                                    ctypes.c_ulong, ctypes.c_int, ctypes.c_int,
                                    ctypes.c_int, ctypes.c_int, H, H, H, H]
    u32.CreateWindowExW.restype = H
    u32.RegisterClassW.argtypes = [H]
    u32.GetMessageW.argtypes = [H, H, ctypes.c_uint, ctypes.c_uint]
    u32.GetMessageW.restype = ctypes.c_int
    u32.DefWindowProcW.argtypes = [H, ctypes.c_uint, ctypes.c_size_t, ctypes.c_ssize_t]
    u32.DefWindowProcW.restype = ctypes.c_ssize_t
    u32.GetForegroundWindow.restype = H
    u32.SetForegroundWindow.argtypes = [H]
    u32.SetForegroundWindow.restype = ctypes.c_int
    u32.ShowWindow.argtypes = [H, ctypes.c_int]
    u32.CreatePopupMenu.restype = H
    u32.AppendMenuW.argtypes = [H, ctypes.c_uint, ctypes.c_size_t, ctypes.c_wchar_p]
    u32.TrackPopupMenu.argtypes = [H, ctypes.c_uint, ctypes.c_int, ctypes.c_int,
                                   ctypes.c_int, H, H]
    u32.TrackPopupMenu.restype = ctypes.c_int
    u32.DestroyMenu.argtypes = [H]
    u32.GetCursorPos.argtypes = [ctypes.POINTER(_POINT)]
    u32.PostMessageW.argtypes = [H, ctypes.c_uint, ctypes.c_size_t, ctypes.c_ssize_t]
    u32.DestroyWindow.argtypes = [H]
    u32.UnregisterClassW.argtypes = [ctypes.c_wchar_p, H]
    u32.LoadImageW.argtypes = [H, ctypes.c_wchar_p, ctypes.c_uint, ctypes.c_int,
                               ctypes.c_int, ctypes.c_uint]
    u32.LoadImageW.restype = H
    u32.LoadIconW.argtypes = [H, H]
    u32.LoadIconW.restype = H
    u32.IsWindowVisible.argtypes = [H]
    u32.GetWindowLongW.argtypes = [H, ctypes.c_int]
    u32.GetWindowLongW.restype = ctypes.c_long

    sh32.Shell_NotifyIconW.argtypes = [ctypes.c_ulong, H]
    sh32.Shell_NotifyIconW.restype = ctypes.c_int
    sh32.ExtractIconExW.argtypes = [ctypes.c_wchar_p, ctypes.c_int,
                                    ctypes.POINTER(H), ctypes.POINTER(H), ctypes.c_uint]
    sh32.ExtractIconExW.restype = ctypes.c_uint

    k32.GetModuleHandleW.argtypes = [ctypes.c_wchar_p]
    k32.GetModuleHandleW.restype = H
    k32.GetConsoleWindow.restype = H

    _U32, _SH32, _K32 = u32, sh32, k32
    return _U32, _SH32, _K32


def click_action(lparam):
    """把托盘回调的 lParam 翻译成动作。

    不同 Windows 版本 / 不同 shell 语义下鼠标消息并不统一：
    - 老语义：左键 WM_LBUTTONUP，右键 WM_RBUTTONUP；
    - 新语义：左键 NIN_SELECT，右键常发 WM_CONTEXTMENU 或 NIN_KEYSELECT。
    只认其中一种，表现就是「点了没反应」。
    """
    lp = lparam & 0xFFFF
    if lp in (WM_LBUTTONUP, WM_LBUTTONDBLCLK, NIN_SELECT):
        return "left"
    if lp in (WM_RBUTTONUP, WM_CONTEXTMENU, NIN_KEYSELECT):
        return "right"
    return None


def supported():
    if os.name != "nt":
        return False
    try:
        _apis()
        return True
    except Exception:
        return False


_CLS_SEQ = [0]


def _next_class_name():
    """每个实例用独立类名。

    固定类名有一个隐蔽问题：同进程内第二次注册会返回 0（ERROR_CLASS_ALREADY_EXISTS），
    于是第二个托盘直接建不出来 —— 表现是「托盘图标在，但点什么都没反应」。
    类名前缀也与其他工具（如可转债提醒）区分，避免同机多程序时互相干扰。
    """
    _CLS_SEQ[0] += 1
    return f"ASTockSentimentTrayWnd{_CLS_SEQ[0]}"


class TrayIcon:
    """托盘图标 + 左键打开看板 / 右键菜单。"""

    def __init__(self, title, tip, menu_items, on_left_click=None, menu_handler=None):
        self.title = title
        self.tip = tip[:127]
        self.menu_items = menu_items
        self.on_left_click = on_left_click     # 左键单击
        self.menu_handler = menu_handler       # 测试注入点；None = 走系统菜单
        self._hwnd = None
        self._thread = None
        self._nid = None
        self._created = threading.Event()
        self._quit = threading.Event()
        self._log = lambda *a: None
        self._last_error = None
        self._last_left = 0.0
        self._cls = None
        self._proc_ref = None

    def set_logger(self, fn):
        self._log = fn

    def _fail(self, why):
        self._last_error = why
        try:
            self._log(f"[托盘] 创建失败：{why}")
        except Exception:
            pass

    # ---- Win32 封装 ----
    def _make_icon(self):
        u32, sh32, _k32 = _apis()
        roots = []
        if getattr(sys, "frozen", False):
            roots.append(os.path.dirname(os.path.abspath(sys.executable)))   # EXE 同级
            roots.append(getattr(sys, "_MEIPASS", "") or "")                # 解包资源目录
        roots.append(os.path.dirname(os.path.abspath(__file__)))             # 源码目录
        icon_files = [os.path.join(r, "assets", "app.ico") for r in roots if r]
        for src in ((sys.executable if getattr(sys, "frozen", False) else None),
                    *icon_files):
            if not src:
                continue
            try:
                if src.lower().endswith(".ico"):
                    h = u32.LoadImageW(None, src, IMAGE_ICON, 0, 0, LR_LOADFROMFILE)
                else:
                    big = ctypes.c_void_p()
                    small = ctypes.c_void_p()
                    if sh32.ExtractIconExW(src, 0, ctypes.byref(big),
                                           ctypes.byref(small), 1) and (big.value or small.value):
                        h = big.value or small.value
                    else:
                        h = None
                if h:
                    return h
            except Exception:
                continue
        return u32.LoadIconW(None, IDI_APPLICATION)

    def _proc(self, hwnd, msg, wparam, lparam):
        u32, _sh32, _k32 = _apis()
        if msg == WM_APP_TRAY:
            act = click_action(lparam)
            if act == "left":
                self._on_left()
            elif act == "right":
                self._popup_menu()
            return 0
        if msg == WM_CLOSE:
            u32.DestroyWindow(hwnd)
            return 0
        if msg == WM_DESTROY:
            u32.PostQuitMessage(0)
            return 0
        if msg == WM_NULL:
            return 0
        return u32.DefWindowProcW(hwnd, msg, wparam, lparam)

    def _on_left(self):
        """左键：打开看板。0.6 秒去抖，避免双击把浏览器开两次。"""
        now = time.time()
        if now - self._last_left < 0.6:
            return
        self._last_left = now
        if not self.on_left_click:
            return
        try:
            self._log("[托盘] 左键：打开看板")
            self.on_left_click()
        except Exception as e:
            self._log(f"[托盘] 左键动作异常：{e}")

    def _popup_menu(self):
        """右键：弹菜单。

        关键（治「点了没反应 / 输入法像被禁用」）：
        TrackPopupMenu 要求宿主窗口是前台窗口，所以弹之前要 SetForegroundWindow；
        但**弹完或弹失败之后必须把前台还给原来的窗口** —— 否则前台停留在不可见的
        隐藏窗口上，别的程序拿不到焦点、输入法看起来就像被禁用了。
        """
        u32, _sh32, _k32 = _apis()
        prev = u32.GetForegroundWindow()
        menu = None
        try:
            if self.menu_handler is not None:          # 测试注入点
                self.menu_handler()
                return
            menu = u32.CreatePopupMenu()
            if not menu:
                return
            for cid, text, _cb in self.menu_items:
                if text is None:
                    u32.AppendMenuW(menu, MF_SEPARATOR, cid, None)
                else:
                    u32.AppendMenuW(menu, MF_STRING, cid, text)
            p = _POINT()
            u32.GetCursorPos(ctypes.byref(p))
            if self._hwnd:
                u32.SetForegroundWindow(self._hwnd)
            cmd = u32.TrackPopupMenu(
                menu,
                TPM_RIGHTBUTTON | TPM_LEFTALIGN | TPM_BOTTOMALIGN | TPM_RETURNCMD,
                p.x, p.y, 0, self._hwnd, None)
            if self._hwnd:
                u32.PostMessageW(self._hwnd, WM_NULL, 0, 0)
            if cmd:
                for cid, _t, cb in self.menu_items:
                    if cid == cmd:
                        try:
                            cb()
                        except Exception as e:
                            self._log(f"[托盘] 菜单动作异常：{e}")
                        break
        except Exception as e:
            self._log(f"[托盘] 菜单异常：{e}")
        finally:
            if menu:
                try:
                    u32.DestroyMenu(menu)
                except Exception:
                    pass
            self._restore_foreground(prev)

    def _restore_foreground(self, prev):
        """把前台还给弹菜单之前的窗口；失败也无所谓，只是焦点体验差一点。"""
        u32, _sh32, _k32 = _apis()
        try:
            cur = u32.GetForegroundWindow()
            if prev and prev != self._hwnd and (not cur or cur == self._hwnd):
                u32.SetForegroundWindow(prev)
        except Exception:
            pass

    def _run(self):
        try:
            u32, sh32, k32 = _apis()
            proc = _WNDPROC(self._proc)

            class WNDCLASSW(ctypes.Structure):
                _fields_ = [("style", ctypes.c_uint), ("lpfnWndProc", _WNDPROC),
                            ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int),
                            ("hInstance", ctypes.c_void_p), ("hIcon", ctypes.c_void_p),
                            ("hCursor", ctypes.c_void_p), ("hbrBackground", ctypes.c_void_p),
                            ("lpszMenuName", ctypes.c_wchar_p),
                            ("lpszClassName", ctypes.c_wchar_p),
                            ("hIconSm", ctypes.c_void_p)]

            hinst = k32.GetModuleHandleW(None)
            cls = _next_class_name()
            self._cls = cls
            wc = WNDCLASSW()
            wc.lpfnWndProc = proc
            wc.hInstance = hinst
            wc.lpszClassName = cls
            self._proc_ref = proc          # 必须持有：类只存裸指针，GC 掉就没人处理消息
            if not u32.RegisterClassW(ctypes.byref(wc)):
                self._fail(f"RegisterClassW 失败 err={ctypes.get_last_error()}")
                return
            # 隐藏的顶层窗口（WS_POPUP，不 ShowWindow）：不进任务栏、不可见，
            # 但**能被设为前台** —— 这是弹托盘菜单的前提。
            # 不能用 message-only 窗口：那种窗口永远成不了前台，菜单弹不出来。
            self._hwnd = u32.CreateWindowExW(0, cls, "astock-sentiment-tray", WS_POPUP,
                                             0, 0, 0, 0, None, None, hinst, None)
            if not self._hwnd:
                self._fail(f"CreateWindowExW 失败 err={ctypes.get_last_error()}")
                return
            nid = _NOTIFYICONDATAW()
            nid.cbSize = ctypes.sizeof(nid)
            nid.hWnd = self._hwnd
            nid.uID = 1
            nid.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP
            nid.uCallbackMessage = WM_APP_TRAY
            nid.hIcon = self._make_icon()
            nid.szTip = self.tip
            if not sh32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(nid)):
                self._fail(f"Shell_NotifyIconW(NIM_ADD) 失败 err={ctypes.get_last_error()} "
                           f"hIcon={nid.hIcon}")
                return
            self._nid = nid
            self._created.set()

            msg = _MSG()
            while u32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                u32.TranslateMessage(ctypes.byref(msg))
                u32.DispatchMessageW(ctypes.byref(msg))
                if self._quit.is_set():
                    u32.PostQuitMessage(0)
        except Exception as e:
            self._fail(f"托盘线程异常 {type(e).__name__}: {e}")
        finally:
            self._created.set()

    def start(self):
        if not supported():
            return False
        self._thread = threading.Thread(target=self._run, daemon=True, name="cb-tray")
        self._thread.start()
        self._created.wait(3.0)
        return bool(self._nid)

    def stop(self):
        u32, sh32, _k32 = _apis()
        if self._nid:
            try:
                sh32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(self._nid))
            except Exception:
                pass
            self._nid = None
        self._quit.set()
        try:
            if self._hwnd:
                u32.PostMessageW(self._hwnd, WM_CLOSE, 0, 0)
        except Exception:
            pass
        try:
            if self._cls:
                u32.UnregisterClassW(self._cls, _k32.GetModuleHandleW(None))
        except Exception:
            pass

    def show_balloon(self, title, text):
        """气泡提示：窗口收进托盘时告诉用户「还在运行」。"""
        if not self._nid or not self._hwnd:
            return
        _u32, sh32, _k32 = _apis()
        try:
            nid = _NOTIFYICONDATAW()
            nid.cbSize = ctypes.sizeof(nid)
            nid.hWnd = self._hwnd
            nid.uID = 1
            nid.uFlags = NIF_INFO
            nid.szInfo = (text or "")[:255]
            nid.szInfoTitle = (title or "")[:63]
            nid.dwInfoFlags = NIIF_INFO
            sh32.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(nid))
        except Exception:
            pass


def hide_console():
    """把控制台窗口藏起来。失败静默。"""
    if os.name != "nt":
        return False
    try:
        u32, _sh32, k32 = _apis()
        hwnd = k32.GetConsoleWindow()
        if hwnd:
            return bool(u32.ShowWindow(hwnd, SW_HIDE))
    except Exception:
        pass
    return False


def console_hwnd():
    if os.name != "nt":
        return None
    try:
        _u32, _sh32, k32 = _apis()
        return k32.GetConsoleWindow() or None
    except Exception:
        return None
