"""Alt+1 to Alt+9 quick paste and the card numbers (UI plan item 3). Fake data only."""

import os
from collections.abc import Iterator
from dataclasses import replace
from datetime import timedelta

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from smartclip.labels import Label  # noqa: E402
from smartclip.storage import ItemPayload, StoredItem  # noqa: E402
from smartclip.ui.view_model import Header  # noqa: E402
from smartclip.ui.window import NUMBER_ROLE, SearchWindow  # noqa: E402
from test_view_model import LINK, NOW, SECRET  # noqa: E402
from test_window import FAKE_SECRET_TEXT, START, Clock, FakeBackend, FakePaster  # noqa: E402


@pytest.fixture
def qapp() -> QApplication:
    return QApplication.instance() or QApplication([])


@pytest.fixture
def backend() -> FakeBackend:
    made = FakeBackend()
    for item_id in range(10, 18):  # 8 more cards: 12 in all
        made.store[item_id] = StoredItem(
            item_id,
            START - timedelta(minutes=10 + item_id),
            ItemPayload(f"FAKE filler {item_id}", Label.NOTE),
        )
    return made


@pytest.fixture
def window(qapp: QApplication, backend: FakeBackend) -> Iterator[SearchWindow]:
    win = SearchWindow(backend, now=Clock())
    win.show_and_focus()
    yield win
    win.hide()
    win.deleteLater()


def number_of(window: SearchWindow, item_id: int) -> object:
    position = window.model.position_of(item_id)
    assert position is not None
    return window.model.data(window.model.index(position), NUMBER_ROLE)


# --- Numbers on the cards ---


def test_first_nine_cards_are_numbered_and_headers_are_not_counted(window: SearchWindow) -> None:
    numbers = [number_of(window, row.item_id) for row in window.model.rows]
    assert numbers[:9] == [1, 2, 3, 4, 5, 6, 7, 8, 9]
    assert all(n is None for n in numbers[9:])
    assert any(isinstance(e, Header) for e in window.model.entries)
    first_header = window.model.index(0)
    assert window.model.data(first_header, NUMBER_ROLE) is None


def test_numbers_follow_the_order_after_pinning(window: SearchWindow, backend: FakeBackend) -> None:
    backend.pin(2)
    window.refresh()
    assert window.model.rows[0].item_id == 2
    assert number_of(window, 2) == 1


# --- Quick paste ---


def test_alt_3_pastes_the_third_visible_card(window: SearchWindow, backend: FakeBackend) -> None:
    third = window.model.rows[2].item_id
    assert window.quick_paste(3) is True
    assert backend.calls[-1] == ("copy_back", third)
    assert not window.isVisible()


def test_quick_paste_skips_headers_when_pinned_items_add_one(
    window: SearchWindow, backend: FakeBackend
) -> None:
    backend.pin(2)
    window.refresh()
    expected = window.model.rows[2].item_id
    window.quick_paste(3)
    assert backend.calls[-1] == ("copy_back", expected)


def test_quick_paste_beyond_the_cards_does_nothing(qapp: QApplication) -> None:
    win = SearchWindow(FakeBackend(), now=Clock())
    win.show_and_focus()
    assert win.quick_paste(9) is False
    assert win.quick_paste(0) is False
    assert win.isVisible()


def test_quick_paste_goes_through_the_same_paste_path_as_enter(
    qapp: QApplication, backend: FakeBackend
) -> None:
    paster = FakePaster()
    win = SearchWindow(backend, now=Clock(), paster=paster)
    win.show_and_focus()
    win.quick_paste(1)
    assert paster.pasted == 1
    assert backend.calls[-1][0] == "copy_back"


def test_a_secret_pasted_by_shortcut_uses_the_backend_copy_back(
    window: SearchWindow, backend: FakeBackend
) -> None:
    number = next(
        i for i, row in enumerate(window.model.rows, start=1) if row.item_id == 3
    )
    window.quick_paste(number)
    # The backend decides what is safe (AppBackend passes sensitive=True for
    # secret and unsure items); the window only ever asks it by id.
    assert backend.calls[-1] == ("copy_back", 3)


def test_alt_digit_keys_are_registered_for_the_window(
    window: SearchWindow, backend: FakeBackend
) -> None:
    assert [f"Alt+{n}" for n in range(1, 10)] == [k for k in window.shortcuts if k.startswith("Alt+")]
    second = window.model.rows[1].item_id
    window.shortcuts["Alt+2"].activated.emit()
    assert backend.calls[-1] == ("copy_back", second)


def test_typing_digits_in_the_search_box_still_works(window: SearchWindow) -> None:
    QTest.keyClicks(window.search, "123")
    assert window.search.text() == "123"
    assert window.isVisible()


def test_alt_modified_digit_is_not_typed_into_search(window: SearchWindow, backend: FakeBackend) -> None:
    first = window.model.rows[0].item_id
    QTest.keyClick(window.search, Qt.Key.Key_1, Qt.KeyboardModifier.AltModifier)
    assert window.search.text() == ""
    assert backend.calls[-1] == ("copy_back", first)
