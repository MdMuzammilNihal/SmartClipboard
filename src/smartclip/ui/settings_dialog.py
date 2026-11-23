"""The settings panel opened from the window's "..." menu (UI plan item 1).

A small frameless panel in the same style as the window, with three groups:
Appearance (font), Privacy (how long items are kept, excluded apps, pause on
start) and General (the start-with-Windows switches, the data folder, the
version). Every change is saved at once and applied without a restart
through the `apply` callback the app supplies.

The theme is not a setting: owner's decision (2026-11-24) that the window
keeps following the Windows light or dark setting.

SECURITY REVIEW (owner): the panel handles settings only (settings.py). It
never shows or touches clipboard content. The excluded-apps list takes
executable names only; anything else is refused (settings.clean_app_name).
"""

import os
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Protocol

from PySide6.QtCore import QEvent, Qt, QTimer, Signal
from PySide6.QtGui import QFont, QKeyEvent
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from smartclip import settings as settings_module
from smartclip.settings import Settings, SettingsStore

PANEL_WIDTH = 360
PANEL_HEIGHT = 700
FONT_LABELS = {"plex": "IBM Plex Serif", "system": "Windows default (Segoe UI)"}


class SwitchLike(Protocol):
    """An on/off setting kept outside the settings file (autostart.py)."""

    label: str

    def is_on(self) -> bool: ...
    def set_on(self, on: bool) -> bool: ...


def open_folder(path: Path) -> None:
    """Show `path` in Windows Explorer, creating it first if it is missing."""
    path.mkdir(parents=True, exist_ok=True)
    os.startfile(path)  # type: ignore[attr-defined]  # Windows only


class SettingsPanel(QWidget):
    """Edits Settings. Saves and applies each change at once."""

    # Emitted when the panel hid because another app took the focus.
    focus_left = Signal()

    def __init__(
        self,
        store: SettingsStore,
        apply: Callable[[Settings], None],
        switches: list[SwitchLike] | None = None,
        show_data_folder: Callable[[], None] | None = None,
        version: str = "",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._store = store
        self._apply = apply
        self._show_data_folder = show_data_folder
        self.settings = store.load()
        self.switch_boxes: list[tuple[SwitchLike, QCheckBox]] = []

        self.setWindowTitle("Smart Clipboard settings")
        self.setWindowFlags(
            Qt.WindowType.Tool
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
        )
        self.resize(PANEL_WIDTH, PANEL_HEIGHT)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

        # --- Appearance ---
        self.font_box = QComboBox()
        for key in settings_module.FONT_CHOICES:
            self.font_box.addItem(FONT_LABELS[key], key)

        # --- Privacy ---
        self.normal_days = QSpinBox()
        self.normal_days.setRange(*settings_module.NORMAL_DAYS_RANGE)
        self.normal_days.setSuffix(" days")
        self.normal_days.setKeyboardTracking(False)
        self.secret_minutes = QSpinBox()
        self.secret_minutes.setRange(*settings_module.SECRET_MINUTES_RANGE)
        self.secret_minutes.setSuffix(" minutes")
        self.secret_minutes.setKeyboardTracking(False)
        self.pause_on_start = QCheckBox("Start with capture paused")
        self.show_hints = QCheckBox("Show keyboard hints")

        self.excluded_list = QListWidget()
        self.excluded_list.setFixedHeight(72)
        self.excluded_input = QLineEdit()
        self.excluded_input.setPlaceholderText("keepass.exe")
        self.add_button = QPushButton("Add")
        self.remove_button = QPushButton("Remove")
        self.excluded_message = QLabel("")
        self.excluded_message.setObjectName("muted")

        # --- General ---
        self.status_label = QLabel("")
        self.status_label.setObjectName("status")
        self.close_button = QPushButton("Done")

        body = QVBoxLayout()
        body.setContentsMargins(16, 12, 16, 12)
        body.setSpacing(8)
        title = QLabel("Settings")
        title_font = QFont(title.font())
        title_font.setPixelSize(16)
        title_font.setWeight(QFont.Weight.DemiBold)
        title.setFont(title_font)
        body.addWidget(title)

        body.addWidget(self._section("APPEARANCE"))
        body.addLayout(self._row("Font", self.font_box))

        body.addWidget(self._section("PRIVACY"))
        body.addLayout(self._row("Keep normal items", self.normal_days))
        body.addLayout(self._row("Keep secrets", self.secret_minutes))
        # One short line, not wrapped: a wrapped label here was measured too
        # short by the layout and overlapped the tick box below it.
        note = QLabel("Applies to secrets saved from now on.")
        note.setObjectName("hint")
        body.addWidget(note)
        body.addSpacing(4)
        body.addWidget(self.pause_on_start)
        body.addWidget(QLabel("Apps that are never captured"))
        body.addWidget(self.excluded_list)
        add_row = QHBoxLayout()
        add_row.addWidget(self.excluded_input, 1)
        add_row.addWidget(self.add_button)
        add_row.addWidget(self.remove_button)
        body.addLayout(add_row)
        body.addWidget(self.excluded_message)

        body.addWidget(self._section("GENERAL"))
        body.addWidget(self.show_hints)
        for switch in switches or []:
            box = QCheckBox(switch.label)
            box.clicked.connect(lambda on, sw=switch, bx=box: self._set_switch(sw, bx, on))
            body.addWidget(box)
            self.switch_boxes.append((switch, box))
        self.folder_button = QPushButton("Open data folder")
        self.folder_button.setEnabled(show_data_folder is not None)
        body.addWidget(self.folder_button)
        self.version_label = QLabel(f"Version {version}" if version else "")
        self.version_label.setObjectName("muted")
        body.addWidget(self.version_label)
        body.addStretch(1)

        content = QWidget()
        content.setObjectName("settingsBody")
        content.setLayout(body)
        scroll = QScrollArea()
        scroll.setWidget(content)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        panel = QFrame()
        panel.setObjectName("panel")
        inner = QVBoxLayout(panel)
        inner.setContentsMargins(0, 0, 0, 0)
        inner.addWidget(scroll, 1)
        # Outside the scroll area, so Done and any message are always in view.
        footer = QHBoxLayout()
        footer.setContentsMargins(16, 0, 16, 12)
        footer.addWidget(self.status_label, 1)
        footer.addWidget(self.close_button)
        inner.addLayout(footer)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(panel)

        self.load_from_settings()
        self.font_box.currentIndexChanged.connect(
            lambda _i: self._commit(font=self.font_box.currentData())
        )
        self.normal_days.valueChanged.connect(
            lambda value: self._commit(normal_retention_days=value)
        )
        self.secret_minutes.valueChanged.connect(
            lambda value: self._commit(secret_retention_minutes=value)
        )
        self.pause_on_start.toggled.connect(lambda on: self._commit(pause_on_start=on))
        self.show_hints.toggled.connect(lambda on: self._commit(show_hints=on))
        self.add_button.clicked.connect(self.add_excluded)
        self.excluded_input.returnPressed.connect(self.add_excluded)
        self.remove_button.clicked.connect(self.remove_excluded)
        self.folder_button.clicked.connect(self._open_folder)
        self.close_button.clicked.connect(self.hide)

    @staticmethod
    def _section(text: str) -> QLabel:
        """A small muted capitals label that starts a group."""
        label = QLabel(text)
        label.setObjectName("muted")
        font = QFont(label.font())
        font.setPixelSize(11)
        font.setWeight(QFont.Weight.DemiBold)
        font.setLetterSpacing(QFont.SpacingType.PercentageSpacing, 110)
        label.setFont(font)
        return label

    @staticmethod
    def _row(text: str, widget: QWidget) -> QHBoxLayout:
        row = QHBoxLayout()
        row.addWidget(QLabel(text), 1)
        row.addWidget(widget)
        return row

    # --- Reading and saving ---

    def load_from_settings(self) -> None:
        """Show the current settings without saving anything."""
        s = self.settings
        for widget in (
            self.font_box, self.normal_days, self.secret_minutes,
            self.pause_on_start, self.show_hints,
        ):
            widget.blockSignals(True)
        self.font_box.setCurrentIndex(max(0, self.font_box.findData(s.font)))
        self.normal_days.setValue(s.normal_retention_days)
        self.secret_minutes.setValue(s.secret_retention_minutes)
        self.pause_on_start.setChecked(s.pause_on_start)
        self.show_hints.setChecked(s.show_hints)
        for widget in (
            self.font_box, self.normal_days, self.secret_minutes,
            self.pause_on_start, self.show_hints,
        ):
            widget.blockSignals(False)
        self.excluded_list.clear()
        self.excluded_list.addItems(list(s.excluded_apps))
        self.sync_switches()

    def sync_switches(self) -> None:
        """Tick each switch to match the real setting (read when the panel opens)."""
        for switch, box in self.switch_boxes:
            box.setChecked(switch.is_on())

    def _commit(self, **changes: object) -> bool:
        """Save a changed setting and apply it. False (and a message) if it cannot be saved."""
        new = replace(self.settings, **changes)  # type: ignore[arg-type]
        if new == self.settings:
            return True
        if not self._store.save(new):
            self.status_label.setText("Could not save the settings file")
            return False
        self.status_label.setText("")
        self.settings = new
        self._apply(new)
        return True

    def _set_switch(self, switch: SwitchLike, box: QCheckBox, on: bool) -> None:
        if not switch.set_on(on):
            self.status_label.setText(f"Could not change \"{switch.label}\"")
        box.setChecked(switch.is_on())

    def _open_folder(self) -> None:
        if self._show_data_folder is not None:
            self._show_data_folder()

    # --- Excluded apps ---

    def add_excluded(self) -> None:
        name = settings_module.clean_app_name(self.excluded_input.text())
        if name is None:
            self.excluded_message.setText("Enter an executable name such as keepass.exe")
            return
        current = self.settings.excluded_apps
        if name in current:
            self.excluded_message.setText("Already on the list")
            return
        if len(current) >= settings_module.MAX_EXCLUDED_APPS:
            self.excluded_message.setText(f"At most {settings_module.MAX_EXCLUDED_APPS} apps")
            return
        if self._commit(excluded_apps=(*current, name)):
            self.excluded_list.addItem(name)
            self.excluded_input.clear()
            self.excluded_message.setText("")

    def remove_excluded(self) -> None:
        item = self.excluded_list.currentItem()
        if item is None:
            self.excluded_message.setText("Choose an app in the list first")
            return
        name = item.text()
        remaining = tuple(app for app in self.settings.excluded_apps if app != name)
        if self._commit(excluded_apps=remaining):
            self.excluded_list.takeItem(self.excluded_list.row(item))
            self.excluded_message.setText("")

    # --- Showing and hiding ---

    def open_beside(self, anchor: QWidget) -> None:
        """Show the panel next to `anchor`, on the side with more room."""
        self.load_from_settings()
        self.status_label.setText("")
        self.excluded_message.setText("")
        screen = anchor.screen().availableGeometry() if anchor.screen() else None
        x = anchor.x() + anchor.width() + 8
        if screen is not None and x + self.width() > screen.right():
            x = anchor.x() - self.width() - 8
        y = anchor.y()
        if screen is not None:
            x = max(screen.left(), x)
            y = max(screen.top(), min(y, screen.bottom() - self.height()))
        self.move(x, y)
        self.show()
        self.raise_()
        self.activateWindow()

    def showEvent(self, event: QEvent) -> None:  # noqa: N802 (Qt name)
        super().showEvent(event)
        from smartclip.ui.window import _round_corners

        _round_corners(self)

    def changeEvent(self, event: QEvent) -> None:  # noqa: N802 (Qt name)
        if event.type() == QEvent.Type.ActivationChange and not self.isActiveWindow():
            # Checked once Qt has settled which window is active.
            QTimer.singleShot(0, self._hide_if_focus_left)
        super().changeEvent(event)

    def _hide_if_focus_left(self) -> None:
        if self.isVisible() and QApplication.activeWindow() is None:
            self.hide()
            self.focus_left.emit()

    def keyPressEvent(self, event: QKeyEvent) -> None:  # noqa: N802 (Qt name)
        if event.key() == Qt.Key.Key_Escape:
            self.hide()
            return
        super().keyPressEvent(event)
