"""Native Windows notification-area icon (Shell_NotifyIcon), dedicated message thread."""
from __future__ import annotations
import ctypes as C
import os
import threading
from pathlib import Path

class Tray:
    def __init__(self, icon_path: Path, dispatch):
        self.path, self.dispatch = str(icon_path), dispatch
        self.lock = threading.Lock()
        self.data = {'peers': [], 'master': None, 'live': False}
        self.ready = threading.Event()
        self.error = ''
        self.hwnd = None
        self.flyout = None
        self.thread = threading.Thread(target=self._run, name='NotificationArea', daemon=True)

    def start(self):
        self.thread.start()
        if not self.ready.wait(8) or self.error:
            raise RuntimeError(self.error or 'Notification area initialisation timed out')

    def update(self, peers, master, live, snapshot=None):
        with self.lock:
            self.data = {'peers': peers, 'master': master, 'live': live}
        if self.flyout and snapshot is not None:self.flyout.update(snapshot)

    def show(self):
        if self.hwnd and os.name == 'nt':
            u=C.WinDLL('user32');u.PostMessageW.argtypes=[C.c_void_p,C.c_uint,C.c_size_t,C.c_ssize_t]
            u.PostMessageW(self.hwnd,0x8000+72,0,0)

    def toast(self,text):
        if self.flyout:self.flyout.toast(text)

    def stop(self):
        if self.hwnd and os.name == 'nt':
            user = C.WinDLL('user32')
            user.PostMessageW.argtypes = [C.c_void_p, C.c_uint, C.c_size_t, C.c_ssize_t]
            user.PostMessageW(self.hwnd, 0x0010, 0, 0)
        self.thread.join(timeout=3)

    def _run(self):
        try:
            self._message_loop()
        except BaseException as exc:
            self.error = type(exc).__name__ + ': ' + str(exc)
            self.ready.set()
            self.dispatch('tray_failed', {'message': self.error})

    def _message_loop(self):
        if os.name != 'nt':
            raise RuntimeError('Tray requires Windows')
        from ctypes import wintypes as W
        LRESULT = C.c_ssize_t
        WNDPROC = C.WINFUNCTYPE(LRESULT, W.HWND, W.UINT, W.WPARAM, W.LPARAM)
        class WNDCLASS(C.Structure):
            _fields_ = [('style', W.UINT), ('proc', WNDPROC), ('cls', C.c_int), ('wnd', C.c_int),
                        ('instance', W.HINSTANCE), ('icon', W.HICON), ('cursor', W.HANDLE),
                        ('background', W.HANDLE), ('menu', W.LPCWSTR), ('name', W.LPCWSTR)]
        class GUID(C.Structure):
            _fields_ = [('data', C.c_ubyte*16)]
        class NOTIFY(C.Structure):
            _fields_ = [('cbSize', W.DWORD), ('hWnd', W.HWND), ('uID', W.UINT), ('uFlags', W.UINT),
                        ('uCallbackMessage', W.UINT), ('hIcon', W.HICON), ('szTip', W.WCHAR*128),
                        ('dwState', W.DWORD), ('dwStateMask', W.DWORD), ('szInfo', W.WCHAR*256),
                        ('uVersion', W.UINT), ('szInfoTitle', W.WCHAR*64), ('dwInfoFlags', W.DWORD),
                        ('guidItem', GUID), ('hBalloonIcon', W.HICON)]
        u, k, shell = C.WinDLL('user32', use_last_error=True), C.WinDLL('kernel32'), C.WinDLL('shell32')
        def proto(lib, name, result, args):
            f = getattr(lib, name); f.restype=result; f.argtypes=args; return f
        proto(k, 'GetModuleHandleW', W.HMODULE, [W.LPCWSTR])
        proto(u, 'RegisterClassW', W.ATOM, [C.POINTER(WNDCLASS)])
        proto(u, 'CreateWindowExW', W.HWND, [W.DWORD,W.LPCWSTR,W.LPCWSTR,W.DWORD,C.c_int,C.c_int,C.c_int,C.c_int,W.HWND,W.HMENU,W.HINSTANCE,C.c_void_p])
        proto(u, 'DefWindowProcW', LRESULT, [W.HWND,W.UINT,W.WPARAM,W.LPARAM])
        proto(u, 'DestroyWindow', W.BOOL, [W.HWND])
        proto(u, 'RegisterWindowMessageW', W.UINT, [W.LPCWSTR])
        proto(u, 'LoadImageW', W.HANDLE, [W.HINSTANCE,W.LPCWSTR,W.UINT,C.c_int,C.c_int,W.UINT])
        proto(u, 'LoadIconW', W.HICON, [W.HINSTANCE,C.c_void_p])
        proto(u, 'DestroyIcon', W.BOOL, [W.HICON])
        proto(u, 'CreatePopupMenu', W.HMENU, [])
        proto(u, 'AppendMenuW', W.BOOL, [W.HMENU,W.UINT,C.c_size_t,W.LPCWSTR])
        proto(u, 'TrackPopupMenu', W.UINT, [W.HMENU,W.UINT,C.c_int,C.c_int,C.c_int,W.HWND,C.c_void_p])
        proto(u, 'SetForegroundWindow', W.BOOL, [W.HWND])
        proto(u, 'DestroyMenu', W.BOOL, [W.HMENU])
        proto(u, 'GetCursorPos', W.BOOL, [C.POINTER(W.POINT)])
        proto(u, 'PostMessageW', W.BOOL, [W.HWND,W.UINT,W.WPARAM,W.LPARAM])
        proto(u, 'GetMessageW', C.c_int, [C.POINTER(W.MSG), W.HWND,W.UINT,W.UINT])
        proto(u, 'TranslateMessage', W.BOOL, [C.POINTER(W.MSG)])
        proto(u, 'DispatchMessageW', LRESULT, [C.POINTER(W.MSG)])
        proto(shell, 'Shell_NotifyIconW', W.BOOL, [W.DWORD,C.POINTER(NOTIFY)])
        instance = k.GetModuleHandleW(None)
        taskbar_created = u.RegisterWindowMessageW('TaskbarCreated')
        callback_message = 0x8000 + 71
        custom_icon = u.LoadImageW(None, self.path, 1, 32, 32, 0x10)
        icon = custom_icon or u.LoadIconW(None, C.c_void_p(32512))
        notify = NOTIFY()

        def popup(hwnd):
            with self.lock:
                state = dict(self.data)
            menu = u.CreatePopupMenu()
            handlers = {}
            def add(parent, text, command, args=None, checked=False, enabled=True):
                ident = len(handlers) + 100
                handlers[ident] = (command, args or {})
                u.AppendMenuW(parent, (8 if checked else 0) | (0 if enabled else 1), ident, text)
            add(menu, 'Настройки и выбор колонок…', 'open')
            add(menu, 'Начать передачу', 'start', enabled=not state['live'])
            add(menu, 'Остановить', 'stop', enabled=state['live'])
            add(menu, 'К живому звуку', 'resync', enabled=state['live'])
            u.AppendMenuW(menu, 0x800, 0, None)
            master = state.get('master') or {}
            volume = master.get('volume', .5)
            add(menu, 'Общая громкость +5%', 'master_volume', {'value': min(1, volume+.05)}, enabled=bool(master))
            add(menu, 'Общая громкость −5%', 'master_volume', {'value': max(0, volume-.05)}, enabled=bool(master))
            add(menu, 'Без звука (Windows)', 'master_mute', {'value': not master.get('mute', False)}, checked=master.get('mute',False), enabled=bool(master))
            for peer in state['peers']:
                sub = u.CreatePopupMenu()
                current = round(float(peer.get('volume') or 0)*10)
                for percent in range(0,101,10):
                    add(sub, str(percent)+'%', 'station_volume', {'id':peer['id'], 'value':percent/100}, checked=current==percent//10)
                name = str(peer['name']).replace('&','&&')[:70]
                u.AppendMenuW(menu, 0x10, sub, name)
            u.AppendMenuW(menu, 0x800, 0, None)
            add(menu, 'Звук Windows…', 'sound_settings')
            add(menu, 'Завершить', 'shutdown')
            point = W.POINT(); u.GetCursorPos(C.byref(point))
            u.SetForegroundWindow(hwnd)
            choice = u.TrackPopupMenu(menu, 0x100 | 0x2, point.x, point.y, 0, hwnd, None)
            u.PostMessageW(hwnd, 0, 0, 0)
            u.DestroyMenu(menu)
            if choice in handlers:
                self.dispatch(*handlers[choice])

        @WNDPROC
        def procedure(hwnd, message, wparam, lparam):
            try:
                if message == callback_message:
                    if lparam == 0x0205:  # right button up
                        self.flyout.show()
                    elif lparam == 0x0202:  # left button up
                        self.flyout.show()
                    return 0
                if message == 0x8000+72:
                    self.flyout.show()
                    return 0
                if message == taskbar_created:
                    shell.Shell_NotifyIconW(0, C.byref(notify))
                    return 0
                if message == 0x0010:
                    if self.flyout:self.flyout.destroy()
                    shell.Shell_NotifyIconW(2, C.byref(notify))
                    u.DestroyWindow(hwnd)
                    return 0
                if message == 0x0002:
                    u.PostQuitMessage(0)
                    return 0
            except Exception as exc:
                self.dispatch('tray_error', {'message':type(exc).__name__})
            return u.DefWindowProcW(hwnd, message, wparam, lparam)

        klass = WNDCLASS(0, procedure, 0, 0, instance, icon, None, None, None, 'YSA.NotificationWindow')
        if not u.RegisterClassW(C.byref(klass)):
            raise C.WinError(C.get_last_error())
        self.hwnd = u.CreateWindowExW(0, klass.name, 'Yandex Station Advanced', 0,0,0,0,0,None,None,instance,None)
        if not self.hwnd:
            raise C.WinError(C.get_last_error())
        notify.cbSize=C.sizeof(NOTIFY); notify.hWnd=self.hwnd; notify.uID=1
        notify.uFlags=1|2|4; notify.uCallbackMessage=callback_message; notify.hIcon=icon
        notify.szTip='Yandex Station Advanced — настройки и колонки'
        if not shell.Shell_NotifyIconW(0,C.byref(notify)):
            raise RuntimeError('Shell_NotifyIcon failed')
        from .flyout import NativeFlyout
        self.flyout=NativeFlyout(self.dispatch,instance)
        self.ready.set()
        msg=W.MSG()
        while True:
            status=u.GetMessageW(C.byref(msg),None,0,0)
            if status == -1:
                raise C.WinError(C.get_last_error())
            if status == 0:
                break
            u.TranslateMessage(C.byref(msg)); u.DispatchMessageW(C.byref(msg))
        self.hwnd=None
        if custom_icon:
            u.DestroyIcon(custom_icon)
