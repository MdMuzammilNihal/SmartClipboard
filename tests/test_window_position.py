"""Window position memory: settings, clamping to the screens, and dragging the top handle.

Fake screens and a fake pointer only; nothing is read from the real desktop
or the clipboard. Real multi-monitor behaviour is not covered here.
"""

import json
import os
from pathlib import Path

import pytest

pytest.importorskip("PySide6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEvent, QPoint, QPointF, QRect, QSize, Qt  # noqa: E402
from PySide6.QtGui import QMouseEvent  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication, QWidget  # noqa: E402

from smartclip.settings import FIELD_NAMES, JsonSettings, MemorySettings, Settings, from_raw  # noqa: E402
from smartclip.ui.geometry import restore_position  # noqa: E402
from smartclip.ui.settings_dialog import SettingsPanel  # noqa: E402
from smartclip.ui.window import WINDOW_SIZE, SearchWindow  # noqa: E402
from test_window import Clock, FakeBackend  # noqa: E402

PRIMARY = QRect(0, 0, 1536, 816)  # 1080p at 125%, minus the taskbar (logical pixels)
LEFT = QRect(-1280, 0, 1280, 984)  # a second monitor to the left of the primary one
SCREENS = [PRIMARY, LEFT]


# --- Settings ---


def test_defaults_open_next_to_the_pointer_with_no_spot() -> None:
    s = Settings()
    assert s.window_position == "pointer" and s.window_spot is None


def test_valid_position_settings_load() -> None:
    s = from_raw({"window_position": "remembered", "window_x": -1200, "window_y": 40})
    assert s.window_position == "remembered" and s.window_spot == (-1200, 40)


@pytest.mark.parametrize(
    "raw",
    [
        {"window_x": 10},  # half a position
        {"window_x": 10, "window_y": "5"},
        {"window_x": True, "window_y": 5},
        {"window_x": 1e3, "window_y": 5},
        {"window_x": 10**9, "window_y": 5},  # nonsense far off any desktop
    ],
)
def test_bad_spots_are_ignored(raw: dict) -> None:
    assert from_raw(raw).window_spot is None


def test_bad_choice_falls_back_to_pointer() -> None:
    assert from_raw({"window_position": "centre"}).window_position == "pointer"


def test_the_file_holds_only_numbers_and_the_choice(tmp_path: Path) -> None:
    store = JsonSettings(tmp_path / "settings.json")
    assert store.save(Settings(window_position="remembered", window_x=30, window_y=-20))
    data = json.loads((tmp_path / "settings.json").read_text(encoding="utf-8"))
    assert data["window_x"] == 30 and data["window_y"] == -20
    assert store.load().window_spot == (30, -20)
    assert set(data) == set(FIELD_NAMES)  # nothing but the known settings


# --- Clamping with fake screens ---


def test_a_spot_on_a_screen_is_kept() -> None:
    point, area = restore_position(QPoint(300, 100), WINDOW_SIZE, SCREENS)
    assert point == QPoint(300, 100) and area == PRIMARY


def test_a_spot_on_the_second_monitor_stays_there() -> None:
    point, area = restore_position(QPoint(-900, 200), WINDOW_SIZE, SCREENS)
    assert area == LEFT and point == QPoint(-900, 200)


def test_a_window_hanging_over_the_edge_is_pulled_in() -> None:
    point, area = restore_position(QPoint(1300, 400), WINDOW_SIZE, SCREENS)
    assert area == PRIMARY
    assert QRect(point, WINDOW_SIZE).right() <= PRIMARY.right()
    assert QRect(point, WINDOW_SIZE).bottom() <= PRIMARY.bottom()


def test_an_unplugged_monitor_gives_no_spot() -> None:
    assert restore_position(QPoint(-900, 200), WINDOW_SIZE, [PRIMARY]) is None
    assert restore_position(None, WINDOW_SIZE, SCREENS) is None


def test_a_smaller_screen_pulls_the_window_inside() -> None:
    small = QRect(0, 0, 800, 500)
    point, _ = restore_position(QPoint(500, 100), QSize(380, 480), [small])
    assert QRect(point, QSize(380, 480)).right() <= small.right()


# --- In the window ---


@pytest.fixture(scope="module")
def qapp() -> QApplication:
    return QApplication.instance() or QApplication([])


def make_window(settings: Settings) -> tuple[SearchWindow, MemorySettings]:
    store = MemorySettings(settings)

    def factory(parent: QWidget) -> SettingsPanel:
        return SettingsPanel(store, lambda _s: None, parent=parent)

    win = SearchWindow(FakeBackend(), now=Clock(), settings_panel_factory=factory)
    win.screen_areas = lambda: SCREENS
    win.pointer_pos = lambda: QPoint(500, 100)
    win.drawer_animation_ms = 0
    return win, store


@pytest.fixture
def opened(qapp: QApplication):
    made: list[SearchWindow] = []

    def build(settings: Settings) -> tuple[SearchWindow, MemorySettings]:
        win, store = make_window(settings)
        made.append(win)
        return win, store

    yield build
    for win in made:
        win.hide()
        win.deleteLater()


def test_pointer_mode_ignores_a_saved_spot(opened) -> None:
    win, _ = opened(Settings(window_position="pointer", window_x=900, window_y=100))
    win.show_and_focus()
    assert win.pos() == QPoint(508, 108)


def test_remembered_mode_without_a_spot_opens_by_the_pointer(opened) -> None:
    win, _ = opened(Settings(window_position="remembered"))
    win.show_and_focus()
    assert win.pos() == QPoint(508, 108)


def test_remembered_mode_opens_at_the_saved_spot(opened) -> None:
    win, _ = opened(Settings(window_position="remembered", window_x=700, window_y=60))
    win.show_and_focus()
    assert win.pos() == QPoint(700, 60)


def test_remembered_spot_on_another_monitor(opened) -> None:
    win, _ = opened(Settings(window_position="remembered", window_x=-1000, window_y=100))
    win.show_and_focus()
    assert win.pos() == QPoint(-1000, 100)


def test_unplugged_monitor_falls_back_to_the_pointer(opened) -> None:
    win, _ = opened(Settings(window_position="remembered", window_x=-1000, window_y=100))
    win.screen_areas = lambda: [PRIMARY]
    win.show_and_focus()
    assert win.pos() == QPoint(508, 108)


def test_remembered_spot_is_clamped_to_the_screen(opened) -> None:
    win, _ = opened(Settings(window_position="remembered", window_x=1500, window_y=790))
    win.show_and_focus()
    rect = QRect(win.pos(), win.size())
    assert PRIMARY.contains(rect)


def mouse(kind: QEvent.Type, at: QPoint) -> QMouseEvent:
    """A left-button mouse event with an explicit desktop position (the pointer does not move with the window)."""
    buttons = Qt.MouseButton.NoButton if kind == QEvent.Type.MouseButtonRelease else Qt.MouseButton.LeftButton
    return QMouseEvent(
        kind, QPointF(5, 5), QPointF(at), Qt.MouseButton.LeftButton, buttons, Qt.KeyboardModifier.NoModifier
    )


def drag(win: SearchWindow, dx: int, dy: int) -> bool:
    """Drag the top handle by (dx, dy) on the desktop. Returns whether the release was used up."""
    start = QPoint(600, 20)
    win.eventFilter(win.handle, mouse(QEvent.Type.MouseButtonPress, start))
    win.eventFilter(win.handle, mouse(QEvent.Type.MouseMove, start + QPoint(dx // 2, dy // 2)))
    win.eventFilter(win.handle, mouse(QEvent.Type.MouseMove, start + QPoint(dx, dy)))
    return win.eventFilter(win.handle, mouse(QEvent.Type.MouseButtonRelease, start + QPoint(dx, dy)))


def test_dragging_the_handle_moves_the_window_and_saves_the_spot(opened) -> None:
    win, store = opened(Settings(window_position="remembered", window_x=700, window_y=60))
    win.show_and_focus()
    before = win.pos()
    open_before = win.drawer_open
    assert drag(win, -120, 40), "the release of a drag must not count as a click"
    assert win.pos() == before + QPoint(-120, 40)
    assert win.drawer_open == open_before, "a drag must not fold or open the top bar"
    assert store.settings.window_spot == (win.x(), win.y())


def test_pointer_mode_drag_moves_but_saves_nothing(opened) -> None:
    win, store = opened(Settings(window_position="pointer"))
    win.show_and_focus()
    before = win.pos()
    drag(win, 50, 30)
    assert win.pos() == before + QPoint(50, 30)
    assert store.settings.window_spot is None


def test_dragging_cannot_leave_the_screen(opened) -> None:
    win, _ = opened(Settings(window_position="remembered", window_x=700, window_y=60))
    win.show_and_focus()
    drag(win, 5000, 5000)
    assert PRIMARY.contains(QRect(win.pos(), win.size()))


def test_a_plain_click_on_the_handle_still_folds_the_bar(opened) -> None:
    win, store = opened(Settings(window_position="remembered", window_x=700, window_y=60))
    win.show_and_focus()
    was_open = win.drawer_open
    QTest.mouseClick(win.handle, Qt.MouseButton.LeftButton)
    assert win.drawer_open != was_open
    assert store.settings.window_spot == (700, 60), "a click must not save a new spot"


def test_the_open_settings_panel_moves_with_the_window(opened) -> None:
    win, _ = opened(Settings(window_position="remembered", window_x=300, window_y=100))
    win.show_and_focus()
    win.open_settings()
    gap = win.settings_panel.pos() - win.pos()
    drag(win, 30, 20)
    assert win.settings_panel.pos() - win.pos() == gap


# --- The settings panel ---


def test_panel_choice_and_spot_are_saved(qapp: QApplication) -> None:
    store = MemorySettings()
    panel = SettingsPanel(store, lambda _s: None)
    panel.position_box.setCurrentIndex(panel.position_box.findData("remembered"))
    assert store.settings.window_position == "remembered"
    assert panel.remember_position(12, -34)
    assert store.settings.window_spot == (12, -34)
    panel.deleteLater()
