"""A tiny message that floats near the pointer and never takes the focus (UI plan item 5).

The window has already closed when a paste turns out to be copy-only, so the
"Copied" message cannot live inside it. This is a separate always-on-top
window that ignores the mouse and does not accept the focus, so the owner's
Ctrl+V still goes to the app they were using. The text is a fixed phrase,
never clipboard content.
"""

from PySide6.QtCore import QPoint, Qt, QTimer
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QWidget

NOTICE_MS = 2500


class Notice(QWidget):
    """One line of text near a point, gone after a moment."""

    def __init__(self, duration_ms: int = NOTICE_MS) -> None:
        super().__init__()
        self.setWindowTitle("Smart Clipboard notice")
        self.setWindowFlags(
            Qt.WindowType.Tool
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.WindowDoesNotAcceptFocus
        )
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.label = QLabel()
        frame = QFrame()
        frame.setObjectName("toast")  # the same look as the in-window toast
        inner = QHBoxLayout(frame)
        inner.setContentsMargins(12, 8, 12, 8)
        inner.addWidget(self.label)
        outer = QHBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(frame)
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(duration_ms)
        self._timer.timeout.connect(self.hide)

    def show_text(self, text: str, style_sheet: str, near: QPoint) -> None:
        """Show `text` just below `near`, kept on the screen that holds it."""
        self.setStyleSheet(style_sheet)
        self.label.setText(text)
        self.adjustSize()
        screen = QGuiApplication.screenAt(near) or QGuiApplication.primaryScreen()
        area = screen.availableGeometry()
        x = max(area.left(), min(near.x() - self.width() // 2, area.right() - self.width()))
        y = max(area.top(), min(near.y() + 24, area.bottom() - self.height()))
        self.move(x, y)
        self.show()
        self.raise_()
        self._timer.start()
