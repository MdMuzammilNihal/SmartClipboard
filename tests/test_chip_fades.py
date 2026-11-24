"""Edge fades on the filter chip strip when chips are cut off (UI plan item 8). Fake data only."""

import os
from collections.abc import Iterator
from datetime import timedelta

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtGui import QColor  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from smartclip.labels import Label  # noqa: E402
from smartclip.storage import ItemPayload, StoredItem  # noqa: E402
from smartclip.ui import theme  # noqa: E402
from smartclip.ui.window import EdgeFade, SearchWindow  # noqa: E402
from qt_wait import wait_for  # noqa: E402
from test_window import START, Clock, FakeBackend  # noqa: E402


@pytest.fixture
def qapp() -> QApplication:
    return QApplication.instance() or QApplication([])


def backend_with_every_type() -> FakeBackend:
    made = FakeBackend()
    for number, label in enumerate(Label, start=20):
        made.store[number] = StoredItem(
            number, START - timedelta(minutes=number), ItemPayload(f"FAKE {label.value}", label)
        )
    return made


@pytest.fixture
def crowded(qapp: QApplication) -> Iterator[SearchWindow]:
    win = SearchWindow(backend_with_every_type(), now=Clock(), current_theme=lambda: theme.DARK)
    win.show_and_focus()
    yield win
    win.hide()
    win.deleteLater()


@pytest.fixture
def roomy(qapp: QApplication) -> Iterator[SearchWindow]:
    few = FakeBackend()
    few.store = {1: few.store[1]}  # one item: just the "All" chip and one type
    win = SearchWindow(few, now=Clock())
    win.show_and_focus()
    yield win
    win.hide()
    win.deleteLater()


def test_a_fade_shows_on_the_right_when_chips_are_cut_off(crowded: SearchWindow) -> None:
    assert wait_for(lambda: crowded.fade_right.isVisible())
    assert not crowded.fade_left.isVisible()  # nothing is hidden on the left yet


def test_scrolling_moves_the_fade_to_the_other_side(crowded: SearchWindow) -> None:
    bar = crowded.chips_scroll.horizontalScrollBar()
    assert wait_for(lambda: bar.maximum() > 0)
    bar.setValue(bar.maximum() // 2)
    assert crowded.fade_left.isVisible() and crowded.fade_right.isVisible()
    bar.setValue(bar.maximum())
    assert crowded.fade_left.isVisible() and not crowded.fade_right.isVisible()
    bar.setValue(0)
    assert not crowded.fade_left.isVisible() and crowded.fade_right.isVisible()


def test_no_fade_when_every_chip_fits(roomy: SearchWindow) -> None:
    roomy.update_chip_fades()
    assert roomy.chips_scroll.horizontalScrollBar().maximum() == 0
    assert not roomy.fade_left.isVisible() and not roomy.fade_right.isVisible()


def test_fades_use_the_window_colour_and_ignore_the_mouse(crowded: SearchWindow) -> None:
    for fade in (crowded.fade_left, crowded.fade_right):
        assert fade._colour == QColor(theme.DARK.window)
        assert fade.testAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        assert fade.width() == EdgeFade.WIDTH


def test_the_fade_sits_over_the_edge_of_the_strip(crowded: SearchWindow) -> None:
    assert wait_for(lambda: crowded.fade_right.isVisible())
    view = crowded.chips_scroll.viewport()
    assert crowded.fade_right.geometry().right() == view.width() - 1
    assert crowded.fade_left.geometry().left() == 0
    assert crowded.fade_right.geometry().height() == view.height()


def test_the_window_still_draws_with_fades(crowded: SearchWindow) -> None:
    assert wait_for(lambda: crowded.fade_right.isVisible())
    assert not crowded.grab().toImage().isNull()


# --- Scrolling the chips (owner's report, 2026-11-26) ---


def wheel(window: SearchWindow, x: int, y: int) -> None:
    from PySide6.QtCore import QPoint, QPointF
    from PySide6.QtGui import QWheelEvent

    view = window.chips_scroll.viewport()
    event = QWheelEvent(
        QPointF(10, 10), QPointF(10, 10), QPoint(0, 0), QPoint(x, y),
        Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier,
        Qt.ScrollPhase.NoScrollPhase, False,
    )
    QApplication.sendEvent(view, event)


def test_mouse_wheel_and_touchpad_swipe_both_scroll_the_chips(crowded: SearchWindow) -> None:
    bar = crowded.chips_scroll.horizontalScrollBar()
    assert bar.maximum() > 0
    wheel(crowded, 0, -120)  # a mouse wheel turned down: the strip moves right
    after_wheel = bar.value()
    assert after_wheel > 0
    wheel(crowded, -120, 0)  # a touchpad swipe sideways
    assert bar.value() > after_wheel
    wheel(crowded, 240, 0)
    assert bar.value() < after_wheel + 120


def test_dragging_the_chips_scrolls_them_without_clicking_a_chip(crowded: SearchWindow) -> None:
    from PySide6.QtCore import QPoint, QPointF
    from PySide6.QtGui import QMouseEvent

    bar = crowded.chips_scroll.horizontalScrollBar()
    chip = crowded.chips[None]  # the "All" chip is already the active filter
    clicks: list[int] = []
    crowded.chips[None].clicked.connect(lambda *_a: clicks.append(1))

    def send(kind, x: float) -> None:  # noqa: ANN001
        pos = QPointF(x, 5)
        button = Qt.MouseButton.LeftButton
        buttons = Qt.MouseButton.NoButton if kind == QMouseEvent.Type.MouseButtonRelease else button
        QApplication.sendEvent(
            chip, QMouseEvent(kind, pos, chip.mapToGlobal(pos), button, buttons, Qt.KeyboardModifier.NoModifier)
        )

    send(QMouseEvent.Type.MouseButtonPress, 30)
    send(QMouseEvent.Type.MouseMove, 10)
    send(QMouseEvent.Type.MouseMove, -60)  # dragged 90 px to the left: the strip moves right
    send(QMouseEvent.Type.MouseButtonRelease, -60)
    assert bar.value() > 0 and clicks == []


def test_a_plain_click_on_a_chip_still_filters(crowded: SearchWindow) -> None:
    link_chip = crowded.chips[Label.LINK]
    link_chip.click()
    assert crowded._filter is Label.LINK
