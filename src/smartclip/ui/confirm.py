"""The themed confirmation question (used by Clear all).

Replaces the plain Windows message box so the question looks like the rest
of the window: same colours, rounded corners, a red Delete button.

Behaviour (owner's request, 2026-11-25):

- It is modal for this app and stays above the always-on-top window.
- It cannot stay open behind your back: when another app takes the focus it
  closes as "Cancel", exactly as the main window hides itself. Nothing is
  deleted by a question that was left open.
- Cancel is the default button; Enter does not delete.

The text it shows is fixed wording plus a count, never an item's text.
"""

from collections.abc import Callable

from PySide6.QtCore import QEvent, Qt, QTimer
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from smartclip.ui import theme as themes


class ConfirmDialog(QDialog):
    """A two-button question: Cancel (default) and a red confirm button."""

    def __init__(
        self,
        parent: QWidget | None,
        title: str,
        text: str,
        confirm_text: str,
        current_theme: Callable[[], themes.Theme] = themes.current_theme,
        active_window: Callable[[], QWidget | None] = QApplication.activeWindow,
    ) -> None:
        super().__init__(parent)
        self._active_window = active_window
        self.setWindowFlags(
            Qt.WindowType.Dialog
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        # Modality before anything else: setting it later resets the window flags.
        self.setWindowModality(Qt.WindowModality.ApplicationModal)
        self.setWindowTitle(title)

        t = current_theme()
        # Dark text on the light red of the dark theme, white on the deep red of the light one.
        yes_ink = "#ffffff" if t.name == "light" else t.window
        # The sheet is ours alone: the main window's would not reach a top-level dialog.
        self.setStyleSheet(
            themes.stylesheet(t)
            + f"""
            #confirmCard {{
                background: {t.window}; border: 1px solid {t.border};
                border-radius: {themes.WINDOW_RADIUS}px;
            }}
            #confirmTitle {{ font-size: 15px; font-weight: 700; color: {t.text}; }}
            #confirmBody {{ color: {t.muted_strong}; }}
            QPushButton#confirmYes {{
                background: {t.danger}; color: {yes_ink}; border: 1px solid {t.danger};
                font-weight: 600;
            }}
            QPushButton#confirmYes:hover {{ background: {t.banner_text}; }}
            """
        )

        card = QFrame()
        card.setObjectName("confirmCard")
        title_label = QLabel(title)
        title_label.setObjectName("confirmTitle")
        body = QLabel(text)
        body.setObjectName("confirmBody")
        body.setWordWrap(True)
        self.cancel_button = QPushButton("Cancel")
        self.cancel_button.setDefault(True)
        self.confirm_button = QPushButton(confirm_text)
        self.confirm_button.setObjectName("confirmYes")
        self.confirm_button.setAutoDefault(False)
        self.cancel_button.clicked.connect(self.reject)
        self.confirm_button.clicked.connect(self.accept)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        buttons.addWidget(self.cancel_button)
        buttons.addWidget(self.confirm_button)
        inner = QVBoxLayout(card)
        inner.setContentsMargins(18, 16, 18, 14)
        inner.setSpacing(8)
        inner.addWidget(title_label)
        inner.addWidget(body)
        inner.addSpacing(6)
        inner.addLayout(buttons)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(card)
        self.setFixedWidth(340)

    def showEvent(self, event: QEvent) -> None:  # noqa: N802 (Qt name)
        super().showEvent(event)
        parent = self.parentWidget()
        if parent is not None:  # centred over the main window
            centre = parent.geometry().center()
            self.move(centre.x() - self.width() // 2, centre.y() - self.height() // 2)
        QTimer.singleShot(0, self.raise_)
        QTimer.singleShot(0, self.activateWindow)

    def changeEvent(self, event: QEvent) -> None:  # noqa: N802 (Qt name)
        if event.type() == QEvent.Type.ActivationChange and not self.isActiveWindow():
            # Checked once Qt has settled which window is active: moving to
            # another window of this app is fine, moving to another app is not.
            QTimer.singleShot(0, self._cancel_if_focus_left)
        super().changeEvent(event)

    def _cancel_if_focus_left(self) -> None:
        if self.isVisible() and self._active_window() is None:
            self.reject()


def ask_confirm(
    parent: QWidget,
    title: str,
    text: str,
    confirm_text: str,
    current_theme: Callable[[], themes.Theme] = themes.current_theme,
) -> bool:
    """Show the question and return True only if the confirm button was pressed."""
    dialog = ConfirmDialog(parent, title, text, confirm_text, current_theme)
    answer = dialog.exec() == QDialog.DialogCode.Accepted
    # If the question closed because the focus went to another app, the main
    # window follows it and hides (it checks the focus itself).
    hide = getattr(parent, "hide_if_focus_left", None)
    if hide is not None:
        QTimer.singleShot(0, hide)
    return answer
