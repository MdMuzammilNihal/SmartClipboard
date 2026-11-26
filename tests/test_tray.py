"""The notification-area icon (UI plan M3). Fake data only; nothing is shown on a real tray."""

import os
from collections.abc import Iterator

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtGui import QColor  # noqa: E402
from PySide6.QtWidgets import QApplication, QSystemTrayIcon  # noqa: E402

from smartclip.ui import theme, tray  # noqa: E402
from smartclip.ui.tray import TrayIcon, draw_icon, make_icon  # noqa: E402
from smartclip.ui.window import SearchWindow  # noqa: E402
from test_window import Clock, FakeBackend  # noqa: E402


@pytest.fixture
def qapp() -> QApplication:
    return QApplication.instance() or QApplication([])


class Probe:
    """Records what the tray asked the app to do."""

    def __init__(self) -> None:
        self.paused = False
        self.calls: list[str] = []

    def is_paused(self) -> bool:
        return self.paused

    def open_window(self) -> None:
        self.calls.append("open")

    def toggle_pause(self) -> None:
        self.calls.append("pause")
        self.paused = not self.paused

    def quit_app(self) -> None:
        self.calls.append("quit")


@pytest.fixture
def probe() -> Probe:
    return Probe()


@pytest.fixture
def icon(qapp: QApplication, probe: Probe) -> Iterator[TrayIcon]:
    made = TrayIcon(
        probe.is_paused, probe.open_window, probe.toggle_pause, probe.quit_app,
        current_theme=lambda: theme.LIGHT, available=lambda: True,
    )
    yield made
    made.stop()


# --- The drawn icon ---


def test_icon_has_every_size_and_is_not_empty(qapp: QApplication) -> None:
    pictures = make_icon(False)
    sizes = {s.width() for s in pictures.availableSizes()}
    assert sizes == set(tray.ICON_SIZES)
    for size in tray.ICON_SIZES:
        image = draw_icon(size, False).toImage()
        assert image.width() == size and image.pixelColor(size // 2, size // 2).alpha() == 255


def test_paused_icon_looks_different_from_the_running_one(qapp: QApplication) -> None:
    running = draw_icon(32, False).toImage()
    paused = draw_icon(32, True).toImage()
    # The square's colour (near the corner, inside the rounded edge) shows the state.
    assert running.pixelColor(3, 16) == QColor(theme.LIGHT.accent)
    assert paused.pixelColor(3, 16) == QColor(tray._PAUSED_FILL)
    assert running != paused


def test_icon_corners_are_transparent_so_it_is_a_rounded_square(qapp: QApplication) -> None:
    assert draw_icon(64, False).toImage().pixelColor(0, 0).alpha() == 0


# --- Tooltip, menu and clicks ---


def test_menu_has_open_pause_and_quit(icon: TrayIcon) -> None:
    texts = [a.text() for a in icon.menu.actions() if not a.isSeparator()]
    assert texts == [tray.OPEN_TEXT, tray.PAUSE_TEXT, tray.QUIT_TEXT]
    assert icon.open_action.font().bold()  # the action a left click performs


def test_pause_entry_and_tooltip_follow_the_paused_state(icon: TrayIcon, probe: Probe) -> None:
    assert icon.icon.toolTip() == tray.RUNNING_TIP and icon.pause_action.text() == tray.PAUSE_TEXT
    probe.paused = True
    icon.sync()
    assert icon.icon.toolTip() == tray.PAUSED_TIP and icon.pause_action.text() == tray.RESUME_TEXT


def test_menu_resyncs_every_time_it_opens(icon: TrayIcon, probe: Probe) -> None:
    probe.paused = True  # changed elsewhere (a hotkey), no sync call
    icon.menu.aboutToShow.emit()
    assert icon.pause_action.text() == tray.RESUME_TEXT


def test_menu_entries_call_the_app(icon: TrayIcon, probe: Probe) -> None:
    icon.open_action.trigger()
    icon.pause_action.trigger()
    icon.quit_action.trigger()
    assert probe.calls == ["open", "pause", "quit"]


def test_only_a_left_click_opens_the_window(icon: TrayIcon, probe: Probe) -> None:
    reasons = QSystemTrayIcon.ActivationReason
    for reason in (reasons.Context, reasons.MiddleClick, reasons.DoubleClick):
        icon.on_activated(reason)  # a double click already sent a Trigger first
    assert probe.calls == []
    icon.on_activated(reasons.Trigger)
    assert probe.calls == ["open"]


# --- Starting, stopping, no tray ---


def test_without_a_notification_area_the_icon_is_not_shown(qapp: QApplication, probe: Probe) -> None:
    made = TrayIcon(
        probe.is_paused, probe.open_window, probe.toggle_pause, probe.quit_app, available=lambda: False
    )
    assert made.start() is False
    assert not made.icon.isVisible()
    made.stop()


def test_start_returns_true_when_a_tray_exists_and_stop_hides_it(icon: TrayIcon) -> None:
    assert icon.start() is True
    icon.stop()
    assert not icon.icon.isVisible()


def test_hotkey_message_is_skipped_while_the_icon_is_hidden(icon: TrayIcon) -> None:
    icon.show_hotkey_clash(["Ctrl+Alt+V"])  # not visible: nothing is sent, nothing fails
    icon.show_hotkey_clash([])


# --- Fixed wording only ---


def test_every_tray_text_is_fixed_wording(icon: TrayIcon) -> None:
    words = " ".join(
        [tray.OPEN_TEXT, tray.PAUSE_TEXT, tray.RESUME_TEXT, tray.QUIT_TEXT, tray.RUNNING_TIP,
         tray.PAUSED_TIP, tray.HOTKEYS_TITLE, tray.HOTKEYS_TEXT]
    )
    assert "FAKE" not in words
    assert not hasattr(tray, "items") and "item" not in icon.icon.toolTip().lower()


# --- The window tells the tray when pause changes ---


def test_window_announces_pause_changes_from_any_source(qapp: QApplication) -> None:
    backend = FakeBackend()
    win = SearchWindow(backend, now=Clock())
    seen: list[bool] = []
    win.pause_changed.connect(seen.append)
    win.toggle_pause()
    win.toggle_pause()
    assert seen == [True, False]
    win.pause_button.click()  # the window's own button goes the same way
    assert seen == [True, False, True]
    win.deleteLater()
