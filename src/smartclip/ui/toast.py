"""A small message bar at the bottom of the window (UI plan items 4 and 5).

It shows one short line and, optionally, one action button such as "Undo".
It goes away by itself after a few seconds, with a quick fade. The text is
always a fixed phrase or a count, never clipboard content.

Exactly one `finished` signal is sent for every message: when the time runs
out, when it is dismissed, when the action is clicked, or when a new message
replaces it. The window uses that to drop what it kept for Undo.
"""

from PySide6.QtCore import QEasingCurve, QPropertyAnimation, Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QFrame,
    QGraphicsOpacityEffect,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QWidget,
)

DEFAULT_DURATION_MS = 5000
FADE_MS = 120


class Toast(QFrame):
    """One message with an optional action button, hidden until needed."""

    action_clicked = Signal()
    finished = Signal()

    def __init__(
        self,
        parent: QWidget,
        duration_ms: int = DEFAULT_DURATION_MS,
        fade_ms: int = FADE_MS,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("toast")
        self._duration_ms = duration_ms
        self._fade_ms = fade_ms
        self._active = False
        self._fading_out = False

        self.label = QLabel()
        self.label.setWordWrap(True)
        self.action_button = QPushButton()
        self.action_button.setObjectName("toastAction")
        self.action_button.setFlat(True)
        self.action_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.action_button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.action_button.clicked.connect(self._on_action)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 6, 8, 6)
        layout.addWidget(self.label, 1)
        layout.addWidget(self.action_button)

        self._effect = QGraphicsOpacityEffect(self)
        self._effect.setOpacity(1.0)
        self.setGraphicsEffect(self._effect)
        self._fade = QPropertyAnimation(self._effect, b"opacity", self)
        self._fade.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._fade.finished.connect(self._on_fade_done)
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self.expire_now)
        self.hide()

    @property
    def is_active(self) -> bool:
        """True from show_message until the message has finished."""
        return self._active

    def show_message(self, text: str, action_text: str | None = None) -> None:
        """Show `text` (with an action button if `action_text` is given).

        A message still showing is finished first.
        """
        self.dismiss()
        self.label.setText(text)
        self.action_button.setText(action_text or "")
        self.action_button.setVisible(action_text is not None)
        self._active = True
        self._fading_out = False
        self._fade.stop()
        self._effect.setOpacity(0.0 if self._fade_ms else 1.0)
        self.show()
        self.raise_()
        if self._fade_ms:
            self._fade.setDuration(self._fade_ms)
            self._fade.setStartValue(0.0)
            self._fade.setEndValue(1.0)
            self._fade.start()
        self._timer.start(self._duration_ms)

    def expire_now(self) -> None:
        """The time is up: fade out, then finish."""
        if not self._active:
            return
        self._timer.stop()
        if not self._fade_ms:
            self._finish()
            return
        self._fade.stop()
        self._fade.setDuration(self._fade_ms)
        self._fade.setStartValue(self._effect.opacity())
        self._fade.setEndValue(0.0)
        self._fading_out = True
        self._fade.start()

    def dismiss(self) -> None:
        """Hide at once and finish. Does nothing if nothing is showing."""
        if not self._active:
            return
        self._timer.stop()
        self._fade.stop()
        self._finish()

    def _on_action(self) -> None:
        if not self._active:
            return
        self._timer.stop()
        self._fade.stop()
        self._active = False
        self.hide()
        # The action runs first, while whatever it needs is still kept; then
        # this message is finished like any other.
        self.action_clicked.emit()
        self.finished.emit()

    def _on_fade_done(self) -> None:
        # Only the fade-out ends the message; the fade-in finishing does not.
        if self._fading_out:
            self._finish()

    def _finish(self) -> None:
        self._fading_out = False
        if not self._active:
            return
        self._active = False
        self.hide()
        self.finished.emit()
