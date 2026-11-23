"""Day headers and the Pinned section in the window (UI plan item 2). Fake data only."""

import os
from datetime import timedelta
from typing import Iterator

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QRect, QSize, Qt  # noqa: E402
from PySide6.QtGui import QImage, QPainter  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication, QStyleOptionViewItem  # noqa: E402

from smartclip.labels import Label  # noqa: E402
from smartclip.storage import ItemPayload, StoredItem  # noqa: E402
from smartclip.ui.view_model import MASK, Header  # noqa: E402
from smartclip.ui.window import HEADER_HEIGHT, ROW_ROLE, SearchWindow  # noqa: E402
from test_window import FAKE_SECRET_TEXT, START, Clock, FakeBackend  # noqa: E402


@pytest.fixture
def qapp() -> QApplication:
    return QApplication.instance() or QApplication([])


@pytest.fixture
def backend() -> FakeBackend:
    made = FakeBackend()
    made.store[6] = StoredItem(
        6, START - timedelta(days=1, hours=1), ItemPayload("FAKE older note", Label.NOTE)
    )
    made.store[7] = StoredItem(
        7, START - timedelta(days=40), ItemPayload("FAKE very old note", Label.NOTE)
    )
    return made


@pytest.fixture
def window(qapp: QApplication, backend: FakeBackend) -> Iterator[SearchWindow]:
    win = SearchWindow(backend, now=Clock())
    win.show_and_focus()
    yield win
    win.hide()
    win.deleteLater()


def shown(window: SearchWindow) -> list[str]:
    return [
        entry.text if isinstance(entry, Header) else f"#{entry.item_id}"
        for entry in window.model.entries
    ]


def test_history_reads_today_yesterday_older(window: SearchWindow) -> None:
    entries = shown(window)
    assert [e for e in entries if not e.startswith("#")] == ["TODAY", "YESTERDAY", "OLDER"]
    assert entries.index("TODAY") < entries.index("#1") < entries.index("YESTERDAY")
    assert entries.index("YESTERDAY") < entries.index("#6") < entries.index("OLDER")


def test_pinned_items_move_under_a_pinned_header(window: SearchWindow, backend: FakeBackend) -> None:
    backend.pin(2)
    window.refresh()
    entries = shown(window)
    assert entries[:2] == ["PINNED", "#2"]
    assert entries.count("#2") == 1
    assert "TODAY" in entries


def test_headers_cannot_be_selected(window: SearchWindow) -> None:
    for position, entry in enumerate(window.model.entries):
        flags = window.model.flags(window.model.index(position))
        if isinstance(entry, Header):
            assert not flags & Qt.ItemFlag.ItemIsSelectable
        else:
            assert flags & Qt.ItemFlag.ItemIsSelectable


def test_the_first_card_is_selected_not_the_header(window: SearchWindow) -> None:
    assert window.selected_row() is not None
    assert window.selected_row().item_id == 1  # type: ignore[union-attr]


def test_arrow_keys_skip_headers(window: SearchWindow) -> None:
    visited = [window.selected_row().item_id]  # type: ignore[union-attr]
    for _ in range(len(window.model.rows) + 2):
        QTest.keyClick(window.search, Qt.Key.Key_Down)
        row = window.selected_row()
        assert row is not None  # never lands on a header
        visited.append(row.item_id)
    # Passed over the YESTERDAY and OLDER headers and stopped at the last card.
    assert visited[: len(window.model.rows)] == [row.item_id for row in window.model.rows]
    assert visited[-1] == window.model.rows[-1].item_id
    for _ in range(len(window.model.rows) + 2):
        QTest.keyClick(window.search, Qt.Key.Key_Up)
        assert window.selected_row() is not None
    assert window.selected_row().item_id == window.model.rows[0].item_id  # type: ignore[union-attr]


def test_enter_acts_on_a_card_never_a_header(window: SearchWindow, backend: FakeBackend) -> None:
    QTest.keyClick(window.search, Qt.Key.Key_Return)
    assert backend.calls and backend.calls[-1][0] == "copy_back"


def test_searching_gives_one_flat_results_list(window: SearchWindow) -> None:
    window.search.setText("fake")
    entries = shown(window)
    assert entries[0] == "RESULTS"
    assert sum(1 for e in entries if not e.startswith("#")) == 1
    window.search.setText("")
    assert "TODAY" in shown(window) and "RESULTS" not in shown(window)


def test_a_header_with_no_cards_is_not_shown(window: SearchWindow) -> None:
    window.set_filter(Label.LINK)
    assert shown(window) == ["TODAY", "#2"]


def test_empty_history_shows_no_headers(qapp: QApplication) -> None:
    backend = FakeBackend()
    backend.store.clear()
    win = SearchWindow(backend, now=Clock())
    win.show_and_focus()
    assert win.model.entries == []


def test_clear_all_still_counts_only_cards_and_keeps_pinned(
    window: SearchWindow, backend: FakeBackend
) -> None:
    backend.pin(2)
    window.refresh()
    ids = [row.item_id for row in window.clearable_rows()]
    assert 2 not in ids and 1 in ids
    assert all(not isinstance(row, Header) for row in window.clearable_rows())


def test_a_masked_secret_still_exposes_no_text_with_headers_around_it(window: SearchWindow) -> None:
    position = window.model.position_of(3)
    assert position is not None
    index = window.model.index(position)
    for role in list(range(0, 64)) + [ROW_ROLE]:
        assert FAKE_SECRET_TEXT not in str(window.model.data(index, role))
    assert MASK in str(window.model.data(index, ROW_ROLE))


def test_header_role_data_is_only_its_fixed_word(window: SearchWindow) -> None:
    header_index = window.model.index(0)
    assert window.model.data(header_index, Qt.ItemDataRole.DisplayRole) == "TODAY"


def test_header_has_its_own_height_and_paints(window: SearchWindow) -> None:
    header_index = window.model.index(0)
    option = QStyleOptionViewItem()
    option.rect = QRect(0, 0, 340, HEADER_HEIGHT)
    option.font = window.font()
    assert window.delegate.sizeHint(option, header_index) == QSize(
        window.list.viewport().width(), HEADER_HEIGHT
    )
    image = QImage(340, HEADER_HEIGHT, QImage.Format.Format_ARGB32)
    image.fill(0)
    painter = QPainter(image)
    window.delegate.paint(painter, option, header_index)
    painter.end()
    assert any(image.pixelColor(x, y).alpha() for x in range(340) for y in range(HEADER_HEIGHT))


def test_clicking_a_header_does_nothing(window: SearchWindow, backend: FakeBackend) -> None:
    before = list(backend.calls)
    header_rect = window.list.visualRect(window.model.index(0))
    QTest.mouseClick(window.list.viewport(), Qt.MouseButton.LeftButton, pos=header_rect.center())
    assert backend.calls == before
    assert window.selected_row() is not None
