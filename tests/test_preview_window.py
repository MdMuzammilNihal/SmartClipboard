"""Rich previews drawn in the window: sizes, "Show more", clicks (UI plan item 8). Fake data only."""

import os
from collections.abc import Iterator
from datetime import timedelta

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QRect  # noqa: E402
from PySide6.QtGui import QColor, QFont, QFontMetrics  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from smartclip.labels import Label  # noqa: E402
from smartclip.storage import ItemPayload, StoredItem  # noqa: E402
from smartclip.ui import theme  # noqa: E402
from smartclip.ui.view_model import Seg  # noqa: E402
from smartclip.ui.window import (  # noqa: E402
    EXPANDED_BODY_LINES,
    MIN_ELIDED_WIDTH,
    REGION_EXPAND,
    SearchWindow,
    body_line_count,
    card_height,
    card_rects,
    card_tooltip,
    fit_segments,
    mono_font,
    region_at,
)
from test_window import FAKE_SECRET_TEXT, START, Clock, FakeBackend  # noqa: E402

CODE = "def fake(a):\n    b = a + 1\n    c = b * 2\n    d = c - 3\n    e = d / 4\n    return e"
LONG_NOTE = "fake words " * 80
ERROR = "\n".join(f"error line {n}" for n in range(8))
JSON_TEXT = '{"name": "fake", "retries": 3, "debug": false}'


@pytest.fixture
def qapp() -> QApplication:
    return QApplication.instance() or QApplication([])


@pytest.fixture
def backend() -> FakeBackend:
    made = FakeBackend()
    extra = [
        (10, Label.CODE, CODE),
        (11, Label.NOTE, LONG_NOTE),
        (12, Label.JSON, JSON_TEXT),
        (13, Label.LINK, "https://example.test/fake/path/that/goes/on/and/on?q=1"),
        (14, Label.FILE_PATH, r"C:\Users\demo\Documents\fake\project\notes.txt"),
        (15, Label.ERROR_MESSAGE, ERROR),
        (16, Label.NOTE, "#ff8800"),
    ]
    for n, (item_id, label, text) in enumerate(extra):
        made.store[item_id] = StoredItem(
            item_id, START - timedelta(minutes=10 + n), ItemPayload(text, label)
        )
    return made


@pytest.fixture
def window(qapp: QApplication, backend: FakeBackend) -> Iterator[SearchWindow]:
    win = SearchWindow(backend, now=Clock(), current_theme=lambda: theme.LIGHT)
    win.show_and_focus()
    yield win
    win.hide()
    win.deleteLater()


def row_of(window: SearchWindow, item_id: int):  # noqa: ANN201
    return next(r for r in window.model.rows if r.item_id == item_id)


def height_of(window: SearchWindow, item_id: int) -> int:
    return card_height(row_of(window, item_id), window.font(), window.list.viewport().width())


# --- Sizes ---


def test_code_card_is_taller_than_a_one_line_note(window: SearchWindow) -> None:
    assert height_of(window, 10) > height_of(window, 2)


def test_masked_secret_card_stays_one_line_with_no_expand(window: SearchWindow) -> None:
    secret = row_of(window, 3)
    assert body_line_count(secret, window.font(), 300) == 1
    rects = card_rects(QRect(0, 0, 380, 100), secret, QFontMetrics(window.font()))
    assert rects.expand is None


def test_expandable_cards_have_a_show_more_link_inside_the_card(window: SearchWindow) -> None:
    metrics = QFontMetrics(window.font())
    for item_id in (10, 11, 12, 15):
        row = row_of(window, item_id)
        assert row.rich is not None and row.rich.expandable
        cell = QRect(0, 0, 380, card_height(row, window.font(), 380))
        rects = card_rects(cell, row, metrics)
        assert rects.expand is not None
        assert rects.card.contains(rects.expand)
        assert rects.expand.top() >= rects.body.bottom()  # under the preview, not over it
        assert region_at(rects, rects.expand.center()) == REGION_EXPAND


def test_cards_that_are_not_expandable_have_no_link(window: SearchWindow) -> None:
    metrics = QFontMetrics(window.font())
    for item_id in (13, 14, 16, 1):
        row = row_of(window, item_id)
        cell = QRect(0, 0, 380, card_height(row, window.font(), 380))
        assert card_rects(cell, row, metrics).expand is None


# --- Show more ---


def test_show_more_grows_the_card_and_show_less_shrinks_it(window: SearchWindow) -> None:
    closed = height_of(window, 10)
    window.toggle_expand(row_of(window, 10))
    assert 10 in window._expanded and row_of(window, 10).rich.expanded  # type: ignore[union-attr]
    opened = height_of(window, 10)
    assert opened > closed
    window.toggle_expand(row_of(window, 10))
    assert 10 not in window._expanded and height_of(window, 10) == closed


def test_a_long_note_expands_to_at_most_twelve_lines(window: SearchWindow) -> None:
    window.toggle_expand(row_of(window, 11))
    lines = body_line_count(row_of(window, 11), window.font(), 330)
    assert 3 < lines <= EXPANDED_BODY_LINES


def test_clicking_the_link_expands_the_card(window: SearchWindow) -> None:
    window.handle_region(row_of(window, 15), REGION_EXPAND)
    assert row_of(window, 15).rich.expanded  # type: ignore[union-attr]


def test_expanding_keeps_the_card_selected(window: SearchWindow) -> None:
    window.select_item(12)
    window.toggle_expand(row_of(window, 12))
    selected = window.selected_row()
    assert selected is not None and selected.item_id == 12


def test_toggling_a_card_that_cannot_expand_does_nothing(window: SearchWindow) -> None:
    window.toggle_expand(row_of(window, 13))
    assert window._expanded == set()


def test_hiding_the_window_closes_every_show_more(window: SearchWindow) -> None:
    window.toggle_expand(row_of(window, 10))
    window.hide()
    assert window._expanded == set()


def test_the_link_tooltip_is_a_fixed_phrase(window: SearchWindow) -> None:
    row = row_of(window, 10)
    assert card_tooltip(row, REGION_EXPAND) == "Show the whole item"
    window.toggle_expand(row)
    assert card_tooltip(row_of(window, 10), REGION_EXPAND) == "Show less"
    assert "def fake" not in card_tooltip(row, REGION_EXPAND)


# --- Drawing ---


def test_every_type_draws_without_error_in_both_themes(
    qapp: QApplication, backend: FakeBackend
) -> None:
    for t in (theme.LIGHT, theme.DARK):
        win = SearchWindow(backend, now=Clock(), current_theme=lambda t=t: t)
        win.show_and_focus()
        win.resize(win.width(), 1500)
        for item_id in (10, 11, 15):
            win.toggle_expand(row_of(win, item_id))  # draw the opened versions too
        qapp.processEvents()
        image = win.grab().toImage()
        assert not image.isNull()
        win.hide()


def test_a_colour_swatch_is_drawn_in_its_colour(qapp: QApplication, backend: FakeBackend) -> None:
    win = SearchWindow(backend, now=Clock(), current_theme=lambda: theme.LIGHT)
    win.show_and_focus()
    win.resize(win.width(), 1500)
    qapp.processEvents()
    wanted = QColor("#ff8800").rgb()
    image = win.grab().toImage()
    found = sum(
        1 for x in range(image.width()) for y in range(image.height())
        if image.pixelColor(x, y).rgb() == wanted
    )
    assert found >= 20
    win.hide()


def test_the_window_keeps_secret_text_out_of_every_preview_role(window: SearchWindow) -> None:
    for position, entry in enumerate(window.model.entries):
        for role in list(range(0, 64)):
            assert FAKE_SECRET_TEXT not in str(window.model.data(window.model.index(position), role))
    assert row_of(window, 3).rich is None and row_of(window, 4).rich is None


# --- Fitting pieces of a line ---


def test_fit_segments_keeps_natural_widths_when_there_is_room(qapp: QApplication) -> None:
    font = QFont()
    segs = (Seg("folder/", "muted", "middle"), Seg("name.txt", "bold"))
    fonts = [font, font]
    natural = [QFontMetrics(font).horizontalAdvance(s.text) + 2 for s in segs]
    assert fit_segments(segs, fonts, 10_000) == natural


def test_fit_segments_shrinks_the_folder_and_keeps_the_file_name(qapp: QApplication) -> None:
    font = QFont()
    segs = (Seg("C:\\a\\very\\long\\folder\\path\\here\\", "muted", "middle"), Seg("notes.txt", "bold"))
    natural_name = QFontMetrics(font).horizontalAdvance("notes.txt") + 2
    room = natural_name + MIN_ELIDED_WIDTH + 30  # room for the name and a short folder
    widths = fit_segments(segs, [font, font], room)
    assert widths[1] == natural_name
    assert sum(widths) <= room and widths[0] >= MIN_ELIDED_WIDTH


def test_fit_segments_cuts_the_last_piece_when_even_that_is_not_enough(qapp: QApplication) -> None:
    font = QFont()
    segs = (Seg("folder/", "muted", "middle"), Seg("a-very-long-file-name-indeed.txt", "bold"))
    widths = fit_segments(segs, [font, font], 80)
    assert sum(widths) <= 80 and all(w >= 0 for w in widths)


def test_fit_segments_handles_a_single_piece_and_no_room(qapp: QApplication) -> None:
    font = QFont()
    assert fit_segments((Seg("x" * 50),), [font], 40)[0] <= 40
    assert fit_segments((Seg("x" * 50),), [font], 0) == [0]
    assert fit_segments((), [], 100) == []


def test_mono_font_is_a_code_font_one_pixel_smaller(qapp: QApplication) -> None:
    base = QFont()
    base.setPixelSize(13)
    mono = mono_font(base)
    assert mono.pixelSize() == 12
    assert "Consolas" in mono.families()
