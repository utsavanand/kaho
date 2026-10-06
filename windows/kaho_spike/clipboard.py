"""Paste through the clipboard, then hand the user's clipboard back.

Same contract as the Mac app: put the transcript on the clipboard, send
Ctrl+V, and after a delay restore what was there before — but only if
nothing else has written to the clipboard since, judged by the clipboard
sequence number (the Windows twin of NSPasteboard.changeCount).
"""

import threading
import time

RESTORE_AFTER_S = 1.5


def should_restore(seq_after_our_write, seq_now):
    """Restore only if the clipboard is still exactly what we put there."""
    return seq_now == seq_after_our_write


class WinClipboard:
    """CF_UNICODETEXT clipboard and SendInput via ctypes (Windows only)."""

    CF_UNICODETEXT = 13
    GMEM_MOVEABLE = 0x0002

    def __init__(self):
        import ctypes
        from ctypes import wintypes

        self.ctypes, self.wintypes = ctypes, wintypes
        self.user32 = ctypes.WinDLL("user32", use_last_error=True)
        self.kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        u, k = self.user32, self.kernel32
        u.GetClipboardData.restype = wintypes.HANDLE
        u.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
        u.SetClipboardData.restype = wintypes.HANDLE
        u.GetClipboardSequenceNumber.restype = wintypes.DWORD
        u.RegisterClipboardFormatW.argtypes = [wintypes.LPCWSTR]
        u.RegisterClipboardFormatW.restype = wintypes.UINT
        k.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
        k.GlobalAlloc.restype = wintypes.HGLOBAL
        k.GlobalLock.argtypes = [wintypes.HGLOBAL]
        k.GlobalLock.restype = ctypes.c_void_p
        k.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
        # Clipboard history (Win+V) and cloud clipboard skip entries carrying
        # these formats, so dictations don't pile up there
        self.no_history = [u.RegisterClipboardFormatW(n) for n in
                           ("ExcludeClipboardContentFromMonitorProcessing", "CanIncludeInClipboardHistory",
                            "CanUploadToCloudClipboard")]

    def _open(self):
        for _ in range(10):  # another app may hold it for a moment
            if self.user32.OpenClipboard(None):
                return
            time.sleep(0.02)
        raise OSError("clipboard is busy")

    def _global(self, data):
        h = self.kernel32.GlobalAlloc(self.GMEM_MOVEABLE, len(data))
        p = self.kernel32.GlobalLock(h)
        self.ctypes.memmove(p, data, len(data))
        self.kernel32.GlobalUnlock(h)
        return h

    def get_text(self):
        self._open()
        try:
            h = self.user32.GetClipboardData(self.CF_UNICODETEXT)
            if not h:
                return None
            p = self.kernel32.GlobalLock(h)
            try:
                return self.ctypes.wstring_at(p)
            finally:
                self.kernel32.GlobalUnlock(h)
        finally:
            self.user32.CloseClipboard()

    def set_text(self, text, transient=False):
        self._open()
        try:
            self.user32.EmptyClipboard()
            self.user32.SetClipboardData(self.CF_UNICODETEXT, self._global((text + "\0").encode("utf-16-le")))
            if transient:
                zero = (0).to_bytes(4, "little")
                for fmt in self.no_history:
                    self.user32.SetClipboardData(fmt, self._global(zero))
        finally:
            self.user32.CloseClipboard()
        return self.user32.GetClipboardSequenceNumber()

    def sequence(self):
        return self.user32.GetClipboardSequenceNumber()

    def send_ctrl_v(self):
        ctypes, wintypes = self.ctypes, self.wintypes
        INPUT_KEYBOARD, KEYEVENTF_KEYUP, VK_CONTROL, VK_V = 1, 0x0002, 0x11, 0x56

        class KEYBDINPUT(ctypes.Structure):
            _fields_ = (("wVk", wintypes.WORD), ("wScan", wintypes.WORD), ("dwFlags", wintypes.DWORD),
                        ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t))

        class _U(ctypes.Union):
            # MOUSEINPUT is the largest member; pad so sizeof(INPUT) matches Win32
            _fields_ = (("ki", KEYBDINPUT), ("pad", ctypes.c_byte * 32))

        class INPUT(ctypes.Structure):
            _fields_ = (("type", wintypes.DWORD), ("u", _U))

        seq = [(VK_CONTROL, 0), (VK_V, 0), (VK_V, KEYEVENTF_KEYUP), (VK_CONTROL, KEYEVENTF_KEYUP)]
        arr = (INPUT * len(seq))(*[INPUT(INPUT_KEYBOARD, _U(ki=KEYBDINPUT(vk, 0, fl, 0, 0))) for vk, fl in seq])
        sent = self.user32.SendInput(len(seq), arr, ctypes.sizeof(INPUT))
        if sent != len(seq):
            # UIPI blocks input into windows running as administrator
            raise OSError("Windows blocked the paste (is the target app running as administrator?)")


def paste(clip, text, log=print):
    """Paste text at the cursor and restore the previous clipboard if untouched."""
    previous = clip.get_text()
    ours = clip.set_text(text, transient=True)
    clip.send_ctrl_v()

    def restore():
        time.sleep(RESTORE_AFTER_S)
        if previous is not None and should_restore(ours, clip.sequence()):
            clip.set_text(previous)
        elif previous is not None:
            log("clipboard changed since the paste; leaving it alone")

    threading.Thread(target=restore, daemon=True).start()
