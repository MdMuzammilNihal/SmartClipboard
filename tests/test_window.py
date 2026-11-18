"""Tests for the search window with a fake backend. Fake data only.

Qt runs with the "offscreen" platform, so no window appears on screen.
These check what the window does with rows; the rules themselves are tested
in test_view_model.py.
"""

import os
from collections.abc import Iterator
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

pytest.importorskip("PySide6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication, QWidget  # noqa: E402

from smartclip.labels import Label, SecretState  # noqa: E402
from smartclip.storage import ItemPayload, StoredItem  # noqa: E402
from smartclip.ui.view_model import MASK  # noqa: E402
from smartclip.ui.window import (  # noqa: E402
    BADGE_ROLE,
    COL_PREVIEW,
    COL_STATE,
    COL_TYPE,
    SearchWindow,
)

START = datetime(2026, 11, 18, 12, 0, tzinfo=timezone.utc)
FAKE_SECRET_TEXT = "FAKE-win-Zx9!q7Lm-not-real"


class Clock:
    def __init__(self) -> None:
        self.now = START

    def __call__(self) -> datetime:
        return self.now


class FakeBackend:
    def __init__(self) -> None:
        self.store = {
            1: StoredItem(1, START, ItemPayload("FAKE note: lunch at 1pm", Label.NOTE)),
            2: StoredItem(
                2, START - timedelta(minutes=1),
                ItemPayload("https://example.test/fake", Label.LINK, "fakebrowser.exe"),
            ),
            3: StoredItem(
                3,
                START - timedelta(minutes=2),
                ItemPayload(
                    FAKE_SECRET_TEXT, Label.POSSIBLE_SECRET, secret_state=SecretState.SECRET
                ),
            ),
            4: StoredItem(
                4,
                START - timedelta(minutes=3),
                ItemPayload("FAKE-win-unsure-Qw3rty7", Label.NOTE, secret_state=SecretState.UNSURE),
            ),
        }
        self.calls: list[tuple[str, int]] = []
        self.paused = False
        self.copy_ok = True

    def items(self) -> list[StoredItem]:
        return sorted(self.store.values(), key=lambda i: i.created_at, reverse=True)

    def copy_back(self, item_id: int) -> bool:
        self.calls.append(("copy_back", item_id))
        return self.copy_ok

    def _set(self, item_id: int, **changes: object) -> bool:
        item = self.store[item_id]
        self.store[item_id] = replace(item, payload=replace(item.payload, **changes))
        return True

    def pin(self, item_id: int) -> bool:
        self.calls.append(("pin", item_id))
        return self._set(item_id, pinned=True)

    def unpin(self, item_id: int) -> bool:
        self.calls.append(("unpin", item_id))
        return self._set(item_id, pinned=False)

    def delete(self, item_id: int) -> bool:
        self.calls.append(("delete", item_id))
        return self.store.pop(item_id, None) is not None

    def mark_normal(self, item_id: int) -> bool:
        self.calls.append(("mark_normal", item_id))
        return self._set(item_id, secret_state=SecretState.NORMAL)

    def mark_secret(self, item_id: int) -> bool:
        self.calls.append(("mark_secret", item_id))
        return self._set(item_id, secret_state=SecretState.SECRET, pinned=False)

    def is_paused(self) -> bool:
        return self.paused

    def toggle_pause(self) -> bool:
        self.paused = not self.paused
        return self.paused

    def quit(self) -> None:
        self.calls.append(("quit", 0))


@pytest.fixture(scope="module")
def qapp() -> QApplication:
    return QApplication.instance() or QApplication([])


@pytest.fixture
def backend() -> FakeBackend:
    return FakeBackend()


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def window(qapp: QApplication, backend: FakeBackend, clock: Clock) -> Iterator[SearchWindow]:
    win = SearchWindow(backend, now=clock, confirm_delete=lambda _parent: True)
    win.show_and_focus()
    yield win
    win.hide()
    win.deleteLater()


def previews(window: SearchWindow) -> list[str]:
    return [row.preview for row in window.model.rows]


def row_position(window: SearchWindow, item_id: int) -> int:
    return next(i for i, row in enumerate(window.model.rows) if row.item_id == item_id)


def click(window: SearchWindow, item_id: int, column: int) -> None:
    window._on_clicked(window.model.index(row_position(window, item_id), column))


def select(window: SearchWindow, item_id: int) -> None:
    window.table.selectRow(row_position(window, item_id))


# --- Opening ---


def test_opens_with_everything_listed_and_secrets_masked(window: SearchWindow) -> None:
    assert previews(window) == [
        "FAKE note: lunch at 1pm", "https://example.test/fake", MASK, MASK,
    ]
    assert window.selected_row() is not None and window.selected_row().item_id == 1


def test_open_resets_search_filter_and_reveals(window: SearchWindow, clock: Clock) -> None:
    window.search.setText("lunch")
    window.set_filter(Label.NOTE)
    window.reveals.reveal(3, clock.now)
    window.hide()
    window.show_and_focus()
    assert window.search.text() == ""
    assert window.active_filter is None
    assert not window.reveals.any_revealed()


# --- Search and filters ---


def test_typing_filters_rows_and_hides_masked_items(window: SearchWindow) -> None:
    window.search.setText("fake")
    assert [row.item_id for row in window.model.rows] == [1, 2]


def test_secret_text_does_not_find_the_secret(window: SearchWindow) -> None:
    window.search.setText("Zx9")
    assert window.model.rows == []


def test_clicking_type_badge_filters_by_type(window: SearchWindow) -> None:
    click(window, 2, COL_TYPE)
    assert window.active_filter is Label.LINK
    assert [row.item_id for row in window.model.rows] == [2]
    assert window.filter_label.text() == "Filter: Link"
    window.clear_filter_button.click()
    assert window.active_filter is None
    assert len(window.model.rows) == 4


def test_clicking_state_badge_filters_by_state(window: SearchWindow) -> None:
    click(window, 4, COL_STATE)
    assert window.active_filter is SecretState.UNSURE
    assert previews(window) == [MASK]


def test_clicking_empty_state_cell_does_nothing(window: SearchWindow) -> None:
    click(window, 1, COL_STATE)
    assert window.active_filter is None


# --- Reveal ---


def test_clicking_masked_preview_reveals_for_ten_seconds(
    window: SearchWindow, clock: Clock
) -> None:
    click(window, 3, COL_PREVIEW)
    assert FAKE_SECRET_TEXT in previews(window)
    clock.now += timedelta(seconds=9)
    window._tick()
    assert FAKE_SECRET_TEXT in previews(window)
    clock.now += timedelta(seconds=1)
    window._tick()
    assert FAKE_SECRET_TEXT not in previews(window)


def test_clicking_revealed_preview_masks_it(window: SearchWindow) -> None:
    click(window, 3, COL_PREVIEW)
    click(window, 3, COL_PREVIEW)
    assert FAKE_SECRET_TEXT not in previews(window)


def test_hiding_the_window_masks_every_reveal(window: SearchWindow) -> None:
    click(window, 3, COL_PREVIEW)
    window.hide()
    assert not window.reveals.any_revealed()
    assert FAKE_SECRET_TEXT not in previews(window)


def test_masked_row_never_exposes_content_in_any_role(window: SearchWindow) -> None:
    position = row_position(window, 3)
    for column in range(window.model.columnCount()):
        index = window.model.index(position, column)
        for role in list(range(0, 64)) + [BADGE_ROLE]:
            assert FAKE_SECRET_TEXT not in str(window.model.data(index, role))


def test_reveal_button_only_for_sensitive_rows(window: SearchWindow) -> None:
    select(window, 1)
    assert not window.reveal_button.isEnabled()
    select(window, 3)
    assert window.reveal_button.isEnabled()
    window.reveal_button.click()
    assert FAKE_SECRET_TEXT in previews(window)
    assert window.reveal_button.text() == "Hide"


# --- Actions ---


def test_enter_in_search_copies_selected_and_hides(
    window: SearchWindow, backend: FakeBackend
) -> None:
    QTest.keyClick(window.search, Qt.Key.Key_Down)
    QTest.keyClick(window.search, Qt.Key.Key_Return)
    assert backend.calls == [("copy_back", 2)]
    assert not window.isVisible()


def test_copying_a_masked_item_does_not_reveal_it(
    window: SearchWindow, backend: FakeBackend
) -> None:
    select(window, 3)
    window.copy_button.click()
    assert backend.calls == [("copy_back", 3)]
    assert not window.reveals.any_revealed()


def test_failed_copy_keeps_window_open_with_message(
    window: SearchWindow, backend: FakeBackend
) -> None:
    backend.copy_ok = False
    window.copy_button.click()
    assert window.isVisible()
    assert window.status_label.text() == "Could not copy"


def test_escape_clears_search_then_hides(window: SearchWindow) -> None:
    window.search.setText("lunch")
    QTest.keyClick(window.search, Qt.Key.Key_Escape)
    assert window.search.text() == ""
    assert window.isVisible()
    QTest.keyClick(window.search, Qt.Key.Key_Escape)
    assert not window.isVisible()


def test_pin_is_disabled_for_secret_and_unsure(window: SearchWindow) -> None:
    for item_id in (3, 4):
        select(window, item_id)
        assert not window.pin_button.isEnabled()
    select(window, 1)
    assert window.pin_button.isEnabled()


def test_pin_then_unpin(window: SearchWindow, backend: FakeBackend) -> None:
    select(window, 1)
    window.pin_button.click()
    assert window.selected_row().pinned is True
    assert window.pin_button.text() == "Unpin"
    window.pin_button.click()
    assert backend.calls == [("pin", 1), ("unpin", 1)]


def test_mark_normal_only_for_sensitive_rows(window: SearchWindow, backend: FakeBackend) -> None:
    select(window, 1)
    assert not window.mark_normal_button.isEnabled()
    select(window, 4)
    window.mark_normal_button.click()
    assert backend.calls == [("mark_normal", 4)]
    assert window.selected_row().masked is False


def test_mark_secret_not_offered_for_secret_rows(
    window: SearchWindow, backend: FakeBackend
) -> None:
    select(window, 3)
    assert not window.mark_secret_button.isEnabled()
    select(window, 1)
    window.mark_secret_button.click()
    assert backend.calls == [("mark_secret", 1)]
    assert window.selected_row().preview == MASK


def test_delete_asks_first(qapp: QApplication, backend: FakeBackend, clock: Clock) -> None:
    answers: list[bool] = [False, True]
    win = SearchWindow(backend, now=clock, confirm_delete=lambda _parent: answers.pop(0))
    win.show_and_focus()
    win.delete_button.click()
    assert backend.calls == []
    win.delete_button.click()
    assert backend.calls == [("delete", 1)]
    assert 1 not in [row.item_id for row in win.model.rows]
    win.hide()


def test_pause_indicator_follows_backend(window: SearchWindow, backend: FakeBackend) -> None:
    assert not window.pause_label.isVisible()
    window.pause_button.click()
    assert backend.paused is True
    assert window.pause_label.isVisible()
    assert window.pause_button.text() == "Resume"


def test_close_hides_instead_of_quitting(window: SearchWindow, backend: FakeBackend) -> None:
    window.close()
    assert not window.isVisible()
    assert ("quit", 0) not in backend.calls
    window.show_and_focus()
    window.quit_button.click()
    assert ("quit", 0) in backend.calls


def test_selection_follows_the_item_after_refresh(
    window: SearchWindow, backend: FakeBackend
) -> None:
    select(window, 2)
    backend.store[9] = StoredItem(9, START + timedelta(minutes=1), ItemPayload("FAKE newer", Label.NOTE))
    window.refresh()
    assert window.selected_row().item_id == 2


# --- Hiding when focus moves to another app ---


def test_hides_when_another_app_takes_focus(qapp: QApplication, backend: FakeBackend) -> None:
    win = SearchWindow(backend, active_window=lambda: None)
    win.show_and_focus()
    win.reveals.reveal(3, datetime.now(timezone.utc))
    win.hide_if_focus_left()
    assert not win.isVisible()
    assert not win.reveals.any_revealed()


def test_stays_open_while_own_dialog_has_focus(qapp: QApplication, backend: FakeBackend) -> None:
    dialog = QWidget()
    win = SearchWindow(backend, active_window=lambda: dialog)
    win.show_and_focus()
    win.hide_if_focus_left()
    assert win.isVisible()
    win.hide()


def test_focus_check_on_hidden_window_does_nothing(qapp: QApplication, backend: FakeBackend) -> None:
    win = SearchWindow(backend, active_window=lambda: None)
    win.hide_if_focus_left()
    assert not win.isVisible()
