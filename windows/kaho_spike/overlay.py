"""The recording pill: a small always-on-top window that must never take focus.

If it took focus, Ctrl+V would paste into the pill instead of the app the
user is dictating into. Qt's flags ask for that; WS_EX_NOACTIVATE makes
Windows enforce it.
"""

from PySide6 import QtCore, QtGui, QtWidgets

COLORS = {"Recording": "#ff453a", "Transcribing…": "#e9b65c", "Pasted": "#6fdc9a", "Cancelled": "#b1a99c"}


class Pill(QtWidgets.QWidget):
    show_state = QtCore.Signal(str)  # emitted from worker threads

    def __init__(self):
        super().__init__(None, QtCore.Qt.Tool | QtCore.Qt.FramelessWindowHint | QtCore.Qt.WindowStaysOnTopHint
                         | QtCore.Qt.WindowDoesNotAcceptFocus)
        self.setAttribute(QtCore.Qt.WA_ShowWithoutActivating)
        self.setAttribute(QtCore.Qt.WA_TranslucentBackground)
        self.label = QtWidgets.QLabel(self)
        self.label.setAlignment(QtCore.Qt.AlignCenter)
        self.label.setFont(QtGui.QFont("Segoe UI", 11, QtGui.QFont.DemiBold))
        self.resize(220, 44)
        self.label.resize(self.size())
        self.hide_timer = QtCore.QTimer(self, singleShot=True, timeout=self.hide)
        self.show_state.connect(self._set_state)

    def _apply_no_activate(self):
        import ctypes

        GWL_EXSTYLE, WS_EX_NOACTIVATE, WS_EX_TOOLWINDOW, WS_EX_TOPMOST = -20, 0x08000000, 0x00000080, 0x00000008
        u = ctypes.windll.user32
        hwnd = int(self.winId())
        u.SetWindowLongPtrW.restype = ctypes.c_ssize_t
        style = u.GetWindowLongPtrW(hwnd, GWL_EXSTYLE)
        u.SetWindowLongPtrW(hwnd, GWL_EXSTYLE, style | WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW | WS_EX_TOPMOST)

    def _set_state(self, state):
        color = COLORS.get(state, "#ff453a")  # errors keep the red border
        self.label.setText(state)
        self.label.setStyleSheet(
            f"color: #f2ede4; background: rgba(21,19,15,235); border: 1px solid {color}; border-radius: 22px;"
        )
        screen = QtGui.QGuiApplication.primaryScreen().availableGeometry()
        self.move(screen.center().x() - self.width() // 2, screen.bottom() - self.height() - 40)
        if not self.isVisible():
            self.show()
            self._apply_no_activate()
        if state == "Recording":
            self.hide_timer.stop()
        else:
            self.hide_timer.start(1200 if state in ("Pasted", "Cancelled") else 8000)
