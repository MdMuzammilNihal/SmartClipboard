"""The keyboard shortcuts popup, opened from "Keyboard shortcuts" in the settings.

Replaces the hint bar that used to sit under the list (owner's request,
2026-11-26): the bar could not hold every shortcut. This list is fixed
wording plus the hotkeys from config.py; it never shows clipboard content.

Same behaviour as the Clear all question (ui/confirm.py): themed, modal for
this app, above the always-on-top windows, closes when another app takes the
focus, and Esc or Close shuts it.
"""

from collections.abc import Callable

from PySide6.QtCore import QEvent, QSize, Qt, QTimer
from PySide6.QtGui import QKeyEvent
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from smartclip import config
from smartclip.ui import theme as themes
from smartclip.ui.geometry import centre_over, fit_size
from smartclip.ui.view_model import QUICK_PASTE_COUNT

SHORTCUTS_SIZE = QSize(360, 480)  # logical pixels; shortened on a short screen


def shortcut_groups() -> list[tuple[str, list[tuple[str, str]]]]:
    """Every shortcut as (group title, [(keys, what it does)]). Keep in step with window.py."""
    return [
        (
            "Anywhere in Windows",
            [
                (config.HOTKEY_OPEN_WINDOW, "Open the clipboard window"),
                (config.HOTKEY_PAUSE, "Pause or resume capture"),
                (config.HOTKEY_MARK_SECRET, "Mark the last copied item as secret"),
                (config.HOTKEY_FORCE_SAVE, "Save what is on the clipboard now"),
            ],
        ),
        (
            "Choosing an item",
            [
                ("Up / Down", "Move between items"),
                ("Enter", "Paste the selected item"),
                (f"Alt+1 to Alt+{QUICK_PASTE_COUNT}", "Paste the numbered item"),
                ("Esc", "Close the strip, clear the search, then close the window"),
            ],
        ),
        (
            "Actions on the selected item",
            [
                ("Ctrl+E", 'Open or close the "..." actions'),
                ("Ctrl+P", "Pin or unpin"),
                ("Ctrl+R", "Reveal or hide a secret"),
                ("Shift+Del", "Delete (Undo shows for 5 seconds)"),
                ("Shift+F10 or Menu key", "Open the item's menu"),
            ],
        ),
        (
            "In the window",
            [
                ("Ctrl+F", "Search (opens the top bar)"),
                ("Ctrl+D", "Open or fold the top bar"),
                ("Ctrl+Shift+P", "Pause or resume capture"),
                ("Ctrl+,", "Open or close the settings"),
            ],
        ),
    ]


class ShortcutsDialog(QDialog):
    """A scrolling list of every shortcut, grouped."""

    def __init__(
        self,
        parent: QWidget | None,
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
        self.setWindowModality(Qt.WindowModality.ApplicationModal)  # before anything else
        self.setWindowTitle("Keyboard shortcuts")

        t = current_theme()
        self.setStyleSheet(
            themes.stylesheet(t)
            + f"""
            #shortcutsCard {{
                background: {t.window}; border: 1px solid {t.border};
                border-radius: {themes.WINDOW_RADIUS}px;
            }}
            #shortcutsTitle {{ font-size: 15px; font-weight: 700; color: {t.text}; }}
            #shortcutsGroup {{ color: {t.muted_strong}; font-size: 11px; font-weight: 600; }}
            #shortcutsKeys {{
                color: {t.text}; background: {t.card}; border: 1px solid {t.border};
                border-radius: 5px; padding: 1px 6px;
            }}
            #shortcutsBody {{ background: transparent; }}
            """
        )

        card = QFrame()
        card.setObjectName("shortcutsCard")
        title = QLabel("Keyboard shortcuts")
        title.setObjectName("shortcutsTitle")
        grid = QGridLayout()
        grid.setContentsMargins(0, 0, 8, 0)
        grid.setHorizontalSpacing(12)
        grid.setVerticalSpacing(6)
        grid.setColumnStretch(1, 1)
        row = 0
        for group, entries in shortcut_groups():
            heading = QLabel(group.upper())
            heading.setObjectName("shortcutsGroup")
            grid.addWidget(heading, row, 0, 1, 2)
            grid.setRowMinimumHeight(row, 26)
            row += 1
            for keys, what in entries:
                key_label = QLabel(keys)
                key_label.setObjectName("shortcutsKeys")
                key_label.setWordWrap(True)
                key_label.setMaximumWidth(130)
                key_label.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Preferred)
                description = QLabel(what)
                description.setWordWrap(True)
                # Read as one sentence by a screen reader: "Ctrl+P, Pin or unpin".
                key_label.setAccessibleName(f"{keys}, {what}")
                grid.addWidget(key_label, row, 0, Qt.AlignmentFlag.AlignTop)
                grid.addWidget(description, row, 1)
                row += 1
        body = QWidget()
        body.setObjectName("shortcutsBody")
        body.setLayout(grid)
        scroll = QScrollArea()
        scroll.setWidget(body)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.viewport().setAutoFillBackground(False)
        self.close_button = QPushButton("Close")
        self.close_button.setDefault(True)
        self.close_button.clicked.connect(self.accept)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        buttons.addWidget(self.close_button)
        inner = QVBoxLayout(card)
        inner.setContentsMargins(18, 16, 10, 14)
        inner.setSpacing(8)
        inner.addWidget(title)
        inner.addWidget(scroll, 1)
        inner.addLayout(buttons)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(card)
        self.setFixedSize(SHORTCUTS_SIZE)

    def showEvent(self, event: QEvent) -> None:  # noqa: N802 (Qt name)
        super().showEvent(event)
        parent = self.parentWidget()
        screen = (parent.screen() if parent is not None else None) or self.screen()
        area = screen.availableGeometry() if screen else None
        if area is not None:  # a short screen (200% scaling on 1080p) gets a shorter list
            self.setFixedHeight(fit_size(QSize(SHORTCUTS_SIZE.width(), SHORTCUTS_SIZE.height()), area).height())
        if parent is not None:  # centred over the settings panel, kept on its screen
            self.move(centre_over(parent.geometry(), self.size(), area or parent.geometry()))
        QTimer.singleShot(0, self.raise_)
        QTimer.singleShot(0, self.activateWindow)

    def keyPressEvent(self, event: QKeyEvent) -> None:  # noqa: N802 (Qt name)
        if event.key() == Qt.Key.Key_Escape:
            self.accept()
            return
        super().keyPressEvent(event)

    def changeEvent(self, event: QEvent) -> None:  # noqa: N802 (Qt name)
        if event.type() == QEvent.Type.ActivationChange and not self.isActiveWindow():
            QTimer.singleShot(0, self._close_if_focus_left)
        super().changeEvent(event)

    def _close_if_focus_left(self) -> None:
        if self.isVisible() and self._active_window() is None:
            self.accept()


def show_shortcuts(
    parent: QWidget, current_theme: Callable[[], themes.Theme] = themes.current_theme
) -> None:
    """Show the list and wait until it is closed."""
    ShortcutsDialog(parent, current_theme).exec()
