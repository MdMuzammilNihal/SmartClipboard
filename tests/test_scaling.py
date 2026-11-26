"""Layout rules that must hold at any display scaling (125%, 150%, 200%).

Qt sizes are in logical pixels, so the display scaling makes everything bigger
together; what changes is the font metrics (rounding) and how much of the
screen is left. These tests check the rules with fonts of several sizes and
with short screens, using fake data. Real renders at each scale are in the
session notes; real screens and the real Windows platform are not covered.
"""

import os
from datetime import datetime, timedelta, timezone

import pytest

pytest.importorskip("PySide6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPoint, QRect, QSize  # noqa: E402
from PySide6.QtGui import QFont, QFontMetrics  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from smartclip.labels import Label, SecretState  # noqa: E402
from smartclip.storage import ItemPayload, StoredItem  # noqa: E402
from smartclip.ui.geometry import centre_over, fit_size, keep_on_screen  # noqa: E402
from smartclip.ui.window import (  # noqa: E402
    WINDOW_SIZE,
    SearchWindow,
    card_height,
    card_rects,
)
from test_window import START, Clock, FakeBackend  # noqa: E402

# Font pixel sizes standing for the 13 px body text drawn at 100%, 125%, 150% and 200%.
FONT_PIXELS = [13, 16, 20, 26]


@pytest.fixture(scope="module")
def qapp() -> QApplication:
    return QApplication.instance() or QApplication([])


def _rows(qapp: QApplication):
    backend = FakeBackend()
    backend.store[5] = StoredItem(
        5, START - timedelta(minutes=5),
        ItemPayload("C:/Users/fake/folder/report.txt", Label.FILE_PATH, "fake.exe",
                    pinned=True, title="Fake report", tags=("work", "fake")),
    )
    win = SearchWindow(backend, now=Clock())
    win.show_and_focus()
    rows = list(win.model.rows)
    win.hide()
    win.deleteLater()
    return rows


def _inside(inner: QRect, outer: QRect) -> bool:
    return outer.contains(inner)


def _disjoint(a: QRect, b: QRect) -> bool:
    return not a.intersects(b)


@pytest.mark.parametrize("pixels", FONT_PIXELS)
@pytest.mark.parametrize("slide", [0.0, 1.0])
def test_card_parts_stay_inside_and_apart(qapp: QApplication, pixels: int, slide: float) -> None:
    font = QFont()
    font.setPixelSize(pixels)
    metrics = QFontMetrics(font)
    for row in _rows(qapp):
        height = card_height(row, font, WINDOW_SIZE.width() - 20)
        cell = QRect(0, 0, WINDOW_SIZE.width() - 20, height)
        for active in (False, True):
            rects = card_rects(cell, row, metrics, active=active, slide=slide)
            assert _inside(rects.card, cell)
            assert _inside(rects.menu, rects.card), "the ... button must not leave the card"
            for part in (rects.label, rects.state, rects.pin, rects.top_text):
                if part is not None and part.width() > 0:
                    assert _inside(part, rects.card)
            # Pin (or the buttons) sit under "..." and never over it.
            for part in ([rects.pin] if rects.pin else []) + [r for _n, r in rects.buttons]:
                assert _inside(part, rects.card), "the Pin button must stay in the card"
                assert _disjoint(part, rects.menu)
                assert _disjoint(part, rects.body), "the text must keep clear of Pin"
            assert _disjoint(rects.top_text, rects.menu)
            if rects.label and rects.state:
                assert _disjoint(rects.label, rects.state)
            if slide > 0:
                assert rects.strip is not None and _inside(rects.strip, rects.card)
                icons = [r for _n, r in rects.actions]
                for icon in icons:
                    assert _inside(icon, rects.strip), "strip icons must fit the strip"
                for i, a in enumerate(icons):
                    for b in icons[i + 1:]:
                        assert _disjoint(a, b)


def test_fit_size_shrinks_only_when_needed() -> None:
    tall = QRect(0, 0, 1920, 1000)
    assert fit_size(WINDOW_SIZE, tall) == WINDOW_SIZE
    short = QRect(0, 0, 960, 516)  # 1080p at 200% minus the taskbar, in logical pixels
    fitted = fit_size(WINDOW_SIZE, short)
    assert fitted.height() <= short.height() - 16
    assert fitted.width() == WINDOW_SIZE.width()


def test_keep_on_screen_and_centre_over() -> None:
    area = QRect(0, 0, 800, 500)
    assert keep_on_screen(QPoint(-50, 700), QSize(380, 400), area) == QPoint(0, 100)
    # A dialog centred over a window near the right edge is pulled back inside.
    parent = QRect(500, 0, 380, 520)
    corner = centre_over(parent, QSize(340, 200), area)
    assert QRect(corner, QSize(340, 200)).right() <= area.right()


def test_window_height_follows_a_short_screen(qapp: QApplication) -> None:
    backend = FakeBackend()
    win = SearchWindow(backend, now=Clock())
    win.resize(fit_size(WINDOW_SIZE, QRect(0, 0, 960, 500)))
    assert win.height() <= 500 - 16
    win.deleteLater()


def test_scroll_bar_track_is_transparent_in_the_style_sheet() -> None:
    """Without it the real Windows platform draws a checkerboard track (seen 2026-11-26)."""
    from smartclip.ui import theme

    for t in (theme.LIGHT, theme.DARK):
        assert "QScrollBar::add-page" in theme.stylesheet(t)


def test_notice_has_a_transparent_window_so_its_corners_are_round(qapp: QApplication) -> None:
    from PySide6.QtCore import Qt

    from smartclip.ui.notice import Notice

    notice = Notice()
    assert notice.testAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
    notice.deleteLater()
