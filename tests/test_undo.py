"""The undo toast, soft delete and Clear all with Undo (UI plan item 4). Fake data only."""

import os
from collections.abc import Iterator
from datetime import timedelta

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication, QWidget  # noqa: E402

from smartclip.labels import Label  # noqa: E402
from smartclip.ui.toast import Toast  # noqa: E402
from smartclip.ui.window import (  # noqa: E402
    SearchWindow,
    clear_text,
    partial_restore_text,
)
from test_window import FAKE_SECRET_TEXT, START, Clock, FakeBackend  # noqa: E402


@pytest.fixture
def qapp() -> QApplication:
    return QApplication.instance() or QApplication([])


@pytest.fixture
def backend() -> FakeBackend:
    return FakeBackend()


@pytest.fixture
def window(qapp: QApplication, backend: FakeBackend) -> Iterator[SearchWindow]:
    win = SearchWindow(backend, now=Clock(), confirm_clear=lambda _p, _n: True)
    win.show_and_focus()
    yield win
    win.hide()
    win.deleteLater()


def ids(window: SearchWindow) -> list[int]:
    return [row.item_id for row in window.model.rows]


def texts_in_store(backend: FakeBackend) -> list[str]:
    return sorted(item.payload.content for item in backend.store.values())


# --- Toast on its own ---


def test_toast_finishes_exactly_once_whichever_way_it_ends(qapp: QApplication) -> None:
    host = QWidget()
    toast = Toast(host, duration_ms=20, fade_ms=0)
    finished: list[int] = []
    toast.finished.connect(lambda: finished.append(1))

    toast.show_message("one")  # ends by itself
    QTest.qWait(80)
    assert finished == [1] and not toast.is_active

    toast.show_message("two")  # dismissed
    toast.dismiss()
    toast.dismiss()
    assert finished == [1, 1]

    toast.show_message("three", "Undo")  # action clicked
    actions: list[int] = []
    toast.action_clicked.connect(lambda: actions.append(1))
    toast.action_button.click()
    assert actions == [1] and finished == [1, 1, 1] and not toast.is_active

    toast.show_message("four")  # replaced by a new message
    toast.show_message("five")
    assert finished == [1, 1, 1, 1] and toast.is_active
    toast.dismiss()
    assert finished == [1, 1, 1, 1, 1]


def test_toast_without_an_action_hides_the_button(qapp: QApplication) -> None:
    host = QWidget()
    toast = Toast(host, fade_ms=0)
    toast.show_message("Restored")
    assert toast.action_button.isHidden()
    toast.show_message("Item deleted", "Undo")
    assert not toast.action_button.isHidden()
    assert toast.label.text() == "Item deleted"


def test_toast_fades_out_before_finishing(qapp: QApplication) -> None:
    host = QWidget()
    toast = Toast(host, duration_ms=10, fade_ms=40)
    done: list[int] = []
    toast.finished.connect(lambda: done.append(1))
    toast.show_message("hello")
    QTest.qWait(30)
    assert done == []  # still on screen in the fade
    QTest.qWait(200)
    assert done == [1]


# --- Delete and Undo ---


def test_undo_puts_the_item_back_in_its_place_with_its_pin(
    window: SearchWindow, backend: FakeBackend
) -> None:
    backend.pin(2)
    window.refresh()
    before = sorted(ids(window))
    pinned_text = backend.store[2].payload.content
    window.delete(next(r for r in window.model.rows if r.item_id == 2))
    assert 2 not in backend.store
    window.toast.action_button.click()
    assert ("restore", 1) in backend.calls
    assert len(backend.store) == len(before)
    restored = next(i for i in backend.store.values() if i.payload.content == pinned_text)
    assert restored.payload.pinned is True
    assert restored.created_at == START - timedelta(minutes=1)
    # Listed again, pinned section first.
    assert window.model.rows[0].pinned and window.model.rows[0].preview == pinned_text


def test_the_toast_names_no_item_text(window: SearchWindow) -> None:
    secret_row = next(r for r in window.model.rows if r.item_id == 3)
    window.delete(secret_row)
    assert FAKE_SECRET_TEXT not in window.toast.label.text()
    assert window.toast.label.text() == "Item deleted"


def test_the_delete_is_real_at_once(window: SearchWindow, backend: FakeBackend) -> None:
    window.delete(window.model.rows[0])
    assert ("delete", 1) in backend.calls
    assert 1 not in backend.store  # not just hidden


def test_the_kept_items_are_dropped_when_the_time_runs_out(
    qapp: QApplication, backend: FakeBackend
) -> None:
    win = SearchWindow(backend, now=Clock(), undo_ms=20)
    win.show_and_focus()
    win.delete(win.model.rows[0])
    assert len(win._undo_items) == 1
    QTest.qWait(300)
    assert win._undo_items == [] and not win.toast.is_active
    win.undo_delete()  # too late: nothing to restore
    assert not any(call[0] == "restore" for call in backend.calls)
    win.hide()


def test_hiding_the_window_ends_the_undo_offer(window: SearchWindow, backend: FakeBackend) -> None:
    window.delete(window.model.rows[0])
    window.hide()
    assert window._undo_items == [] and not window.toast.is_active
    window.undo_delete()
    assert not any(call[0] == "restore" for call in backend.calls)
    assert 1 not in backend.store


def test_a_second_delete_replaces_the_first_offer(window: SearchWindow, backend: FakeBackend) -> None:
    window.delete(next(r for r in window.model.rows if r.item_id == 1))
    window.delete(next(r for r in window.model.rows if r.item_id == 2))
    window.toast.action_button.click()
    assert backend.calls.count(("restore", 1)) == 1
    assert 1 not in backend.store  # the first one is final
    assert any(i.payload.content.startswith("https://") for i in backend.store.values())  # item 2 back


def test_undo_says_restored_and_reports_a_partial_restore(
    window: SearchWindow, backend: FakeBackend
) -> None:
    window.delete(window.model.rows[0])
    window.toast.action_button.click()
    assert window.toast.label.text() == "Restored"
    assert window.toast.action_button.isHidden()

    backend.restore = lambda items: len(items) - 1  # type: ignore[method-assign]
    window.delete(window.model.rows[0])
    window.delete(window.model.rows[0])
    window.undo_delete()
    assert window.toast.label.text() == partial_restore_text(0, 1)


def test_deleting_something_already_gone_offers_no_undo(
    window: SearchWindow, backend: FakeBackend
) -> None:
    row = window.model.rows[0]
    backend.store.pop(row.item_id)
    window.delete(row)
    assert not window.toast.is_active


def test_toast_is_placed_inside_the_window_above_the_hint_bar(window: SearchWindow) -> None:
    window.delete(window.model.rows[0])
    assert window.toast.geometry().bottom() <= window.hint_label.y()
    assert window.toast.geometry().left() >= 0
    assert window.toast.geometry().right() <= window.width()


# --- Clear all ---


def test_clear_all_toast_counts_cleared_and_pinned_kept(
    window: SearchWindow, backend: FakeBackend
) -> None:
    backend.pin(1)
    window.refresh()
    window.clear_all()
    assert window.toast.label.text() == "Cleared 3 items, 1 pinned kept"
    assert ids(window) == [1]
    assert 1 in backend.store  # pinned items are never touched


def test_clear_all_with_nothing_pinned_has_no_pinned_part(
    window: SearchWindow, backend: FakeBackend
) -> None:
    window.clear_all()
    assert window.toast.label.text() == "Cleared 4 items"
    assert ids(window) == []


def test_one_undo_restores_the_whole_batch(window: SearchWindow, backend: FakeBackend) -> None:
    backend.pin(1)
    window.refresh()
    expected = texts_in_store(backend)
    window.clear_all()
    assert len(backend.store) == 1
    window.toast.action_button.click()
    assert ("restore", 3) in backend.calls
    assert texts_in_store(backend) == expected
    assert len(ids(window)) == 4
    # The same order as before: newest first among the unpinned cards.
    unpinned = [r.item_id for r in window.model.rows if not r.pinned]
    created = [backend.store[i].created_at for i in unpinned]
    assert created == sorted(created, reverse=True)


def test_clear_all_asked_to_confirm_is_still_respected(
    qapp: QApplication, backend: FakeBackend
) -> None:
    win = SearchWindow(backend, now=Clock(), confirm_clear=lambda _p, _n: False)
    win.show_and_focus()
    win.clear_all()
    assert len(backend.store) == 4 and not win.toast.is_active
    win.hide()


def test_clear_all_only_clears_what_is_shown_and_undo_matches(
    window: SearchWindow, backend: FakeBackend
) -> None:
    window.set_filter(Label.LINK)
    window.clear_all()
    assert window.toast.label.text() == "Cleared 1 item"
    assert sorted(backend.store) == [1, 3, 4]
    window.toast.action_button.click()
    assert len(backend.store) == 4


def test_clear_text_wording() -> None:
    assert clear_text(1, 0) == "Cleared 1 item"
    assert clear_text(42, 12) == "Cleared 42 items, 12 pinned kept"
    assert partial_restore_text(3, 5).startswith("Restored 3 of 5")


def test_undo_buffer_holds_the_deleted_items_only_while_offered(
    window: SearchWindow,
) -> None:
    window.clear_all()
    assert len(window._undo_items) == 4
    window.toast.dismiss()
    assert window._undo_items == []
