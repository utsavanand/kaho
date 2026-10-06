"""Hold-to-talk on Right Ctrl.

`step()` is the whole decision logic, pure and testable anywhere. The hook
(Windows only) does nothing but enqueue key events: Windows silently removes
a low-level hook whose callback runs past LowLevelHooksTimeout (~300 ms), so
no decision is made on the hook thread.
"""

import queue
import threading

VK_RCONTROL = 0xA3

IDLE, HOLDING = "idle", "holding"
START, STOP, CANCEL = "start", "stop", "cancel"


def step(state, vk, is_down, hotkey=VK_RCONTROL):
    """(new_state, action or None) for one physical key event.

    - hotkey down while idle starts recording; auto-repeat downs are ignored
    - hotkey up stops it
    - any other key pressed while holding means the user is typing a
      shortcut (Right Ctrl + C), so the recording is cancelled, not pasted
    """
    if vk == hotkey:
        if is_down:
            return (HOLDING, START) if state == IDLE else (state, None)
        return (IDLE, STOP) if state == HOLDING else (IDLE, None)
    if is_down and state == HOLDING:
        return IDLE, CANCEL
    return state, None


class HotkeyHook:
    """WH_KEYBOARD_LL hook on its own thread with a message pump (Windows only).

    Calls on_action(action) from a separate dispatcher thread, never from
    the hook callback itself.
    """

    def __init__(self, on_action, hotkey=VK_RCONTROL):
        self.on_action = on_action
        self.hotkey = hotkey
        self.events = queue.SimpleQueue()
        self.state = IDLE
        self.ready = threading.Event()  # set once the hook is installed, or has failed
        self.error = None

    def start(self):
        threading.Thread(target=self._pump, name="hotkey-hook", daemon=True).start()
        threading.Thread(target=self._dispatch, name="hotkey-dispatch", daemon=True).start()

    def _dispatch(self):
        while True:
            vk, is_down = self.events.get()
            self.state, action = step(self.state, vk, is_down, self.hotkey)
            if action:
                self.on_action(action)

    def _pump(self):
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.WinDLL("user32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        WH_KEYBOARD_LL, WM_KEYDOWN, WM_SYSKEYDOWN = 13, 0x0100, 0x0104
        LLKHF_INJECTED = 0x10

        class KBDLLHOOKSTRUCT(ctypes.Structure):
            _fields_ = (("vkCode", wintypes.DWORD), ("scanCode", wintypes.DWORD), ("flags", wintypes.DWORD),
                        ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t))

        LRESULT = ctypes.c_ssize_t
        HOOKPROC = ctypes.WINFUNCTYPE(LRESULT, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)
        user32.CallNextHookEx.argtypes = [wintypes.HHOOK, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM]
        user32.CallNextHookEx.restype = LRESULT
        user32.SetWindowsHookExW.argtypes = [ctypes.c_int, HOOKPROC, wintypes.HINSTANCE, wintypes.DWORD]
        user32.SetWindowsHookExW.restype = wintypes.HHOOK
        kernel32.GetModuleHandleW.restype = wintypes.HMODULE

        def callback(code, wparam, lparam):
            if code == 0:
                kb = ctypes.cast(lparam, ctypes.POINTER(KBDLLHOOKSTRUCT)).contents
                # Our own SendInput Ctrl+V comes back through the hook; ignore it
                if not kb.flags & LLKHF_INJECTED:
                    self.events.put((kb.vkCode, wparam in (WM_KEYDOWN, WM_SYSKEYDOWN)))
            return user32.CallNextHookEx(None, code, wparam, lparam)

        self._proc = HOOKPROC(callback)  # keep a reference or the hook crashes
        hook = user32.SetWindowsHookExW(WH_KEYBOARD_LL, self._proc, kernel32.GetModuleHandleW(None), 0)
        if not hook:
            self.error = ctypes.WinError(ctypes.get_last_error())
            self.ready.set()
            return
        self.ready.set()
        msg = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))
