"""The settings panel opened from the window's gear button (UI plan item 1).

A small frameless panel in the same style as the window, with four groups:
Window (Clear all), Appearance (font),
Privacy (how long items are kept, pause on start; the excluded-apps list
has no screen in this version, the setting itself still works from the file) and General
(the keyboard shortcuts list, the start-with-Windows switches, Quit, the version). The Window group
and Quit used to be the "..." menu (owner's change, 2026-11-25). Every change is saved at once and applied without a restart
through the `apply` callback the app supplies.

The theme is not a setting: owner's decision (2026-11-24) that the window
keeps following the Windows light or dark setting.

SECURITY REVIEW (owner): the panel handles settings only (settings.py). It
never shows or touches clipboard content. The excluded-apps list takes
executable names only; anything else is refused (settings.clean_app_name).
"""

from collections.abc import Callable
from dataclasses import replace
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
    QPushButton,
    QScrollArea,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from smartclip import settings as settings_module
from smartclip.settings import Settings, SettingsStore

# Same size as the main window (window.WINDOW_SIZE; a test keeps them equal).
PANEL_WIDTH = 380
PANEL_HEIGHT = 520
FONT_LABELS = {"plex": "IBM Plex Serif", "system": "Windows default (Segoe UI)"}


class SwitchLike(Protocol):
    """An on/off setting kept outside the settings file (autostart.py)."""

    label: str

    def is_on(self) -> bool: ...
    def set_on(self, on: bool) -> bool: ...


class SettingsPanel(QWidget):
    """Edits Settings. Saves and applies each change at once."""

    # Emitted when the panel hid because another app took the focus.
    focus_left = Signal()
    # Controls that belong to the window; the window connects to these.
    clear_all_requested = Signal()
    quit_requested = Signal()

    def __init__(
        self,
        store: SettingsStore,
        apply: Callable[[Settings], None],
        switches: list[SwitchLike] | None = None,
        version: str = "",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._store = store
        self._apply = apply
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

        # --- Window (what the old "..." menu held) ---
        self.clear_button = QPushButton("Clear all")
        self.clear_button.setToolTip("Delete the items shown, except pinned ones")
        self.quit_button = QPushButton("Quit Smart Clipboard")

        # --- Appearance ---
        self.font_box = QComboBox()
        for key in settings_module.FONT_CHOICES:
            self.font_box.addItem(FONT_LABELS[key], key)

        # --- Privacy ---
        self.normal_days = QSpinBox()
        self.normal_days.setRange(*settings_module.NORMAL_DAYS_RANGE)
        self.normal_days.setAlignment(Qt.AlignmentFlag.AlignRight)
        self.normal_days.setFixedWidth(72)
        self.normal_days.setKeyboardTracking(False)
        self.secret_hours = QSpinBox()
        self.secret_hours.setRange(*settings_module.SECRET_HOURS_RANGE)
        self.secret_hours.setAlignment(Qt.AlignmentFlag.AlignRight)
        self.secret_hours.setFixedWidth(72)
        self.secret_hours.setKeyboardTracking(False)
        self.pause_on_start = QCheckBox("Start with capture paused")
        self.shortcuts_button = QPushButton("Keyboard shortcuts")

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

        body.addWidget(self._section("WINDOW"))
        body.addWidget(self.clear_button)

        body.addWidget(self._section("APPEARANCE"))
        body.addLayout(self._row("Font", self.font_box))

        body.addWidget(self._section("PRIVACY"))
        # The unit is a plain label beside the box: only the number can be edited.
        body.addLayout(self._row("Keep normal items", self.normal_days, "days"))
        body.addLayout(self._row("Keep secrets", self.secret_hours, "hours"))
        # One short line, not wrapped: a wrapped label here was measured too
        # short by the layout and overlapped the tick box below it.
        note = QLabel("Applies to secrets saved from now on.")
        note.setObjectName("hint")
        body.addWidget(note)
        body.addSpacing(4)
        body.addWidget(self.pause_on_start)

        body.addWidget(self._section("GENERAL"))
        body.addWidget(self.shortcuts_button)
        for switch in switches or []:
            box = QCheckBox(switch.label)
            box.clicked.connect(lambda on, sw=switch, bx=box: self._set_switch(sw, bx, on))
            body.addWidget(box)
            self.switch_boxes.append((switch, box))
        body.addWidget(self.quit_button)
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
        self.secret_hours.valueChanged.connect(
            lambda value: self._commit(secret_retention_hours=value)
        )
        self.pause_on_start.toggled.connect(lambda on: self._commit(pause_on_start=on))
        self.shortcuts_button.clicked.connect(self.show_shortcuts)
        self.clear_button.clicked.connect(self._clear_clicked)
        self.quit_button.clicked.connect(self.quit_requested)
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
    def _row(text: str, widget: QWidget, unit: str = "") -> QHBoxLayout:
        row = QHBoxLayout()
        row.addWidget(QLabel(text), 1)
        row.addWidget(widget)
        if unit:
            unit_label = QLabel(unit)
            unit_label.setObjectName("muted")
            unit_label.setMinimumWidth(40)
            row.addWidget(unit_label)
        return row

    # --- Reading and saving ---

    def load_from_settings(self) -> None:
        """Show the current settings without saving anything."""
        s = self.settings
        for widget in (
            self.font_box, self.normal_days, self.secret_hours,
            self.pause_on_start,
        ):
            widget.blockSignals(True)
        self.font_box.setCurrentIndex(max(0, self.font_box.findData(s.font)))
        self.normal_days.setValue(s.normal_retention_days)
        self.secret_hours.setValue(s.secret_retention_hours)
        self.pause_on_start.setChecked(s.pause_on_start)
        for widget in (
            self.font_box, self.normal_days, self.secret_hours,
            self.pause_on_start,
        ):
            widget.blockSignals(False)
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

    def show_shortcuts(self) -> None:
        """The popup with every keyboard shortcut (replaces the old hint bar)."""
        from smartclip.ui.shortcuts_dialog import show_shortcuts

        show_shortcuts(self)

    def _clear_clicked(self) -> None:
        self.hide()  # the question belongs to the window, not to this panel
        self.clear_all_requested.emit()

    def set_window_state(self, can_clear: bool) -> None:
        """Match the Clear all button to the window (called by the window)."""
        self.clear_button.setEnabled(can_clear)

    # --- Showing and hiding ---

    def open_beside(self, anchor: QWidget) -> None:
        """Show the panel next to `anchor`, on the side with more room."""
        self.load_from_settings()
        self.status_label.setText("")
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
