"""The designed empty screens: which one is chosen, what it says, what its buttons do.

Fake data only. The wording never repeats the search text or any item text.
"""

import os
from collections.abc import Iterator
from datetime import timedelta

import pytest

pytest.importorskip("PySide6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

from smartclip.labels import Label, SecretState  # noqa: E402
from smartclip.storage import ItemPayload, StoredItem  # noqa: E402
from smartclip.ui import theme  # noqa: E402
from smartclip.ui.view_model import (  # noqa: E402
    ACTION_CLEAR_FILTER,
    ACTION_CLEAR_SEARCH,
    ACTION_RESUME,
    EMPTY_NO_HISTORY,
    EMPTY_NO_MATCH,
    EMPTY_PAUSED,
    EMPTY_SECRETS_ONLY,
    TagFilter,
    empty_state,
)
from smartclip.ui.window import SearchWindow  # noqa: E402
from test_window import START, Clock, FakeBackend  # noqa: E402

FAKE_SEARCH = "FAKE-typed-query-9x"


def _note(item_id: int = 1) -> StoredItem:
    return StoredItem(item_id, START, ItemPayload("FAKE note", Label.NOTE))


def _secret(item_id: int = 2, state: SecretState = SecretState.SECRET) -> StoredItem:
    return StoredItem(
        item_id, START, ItemPayload("FAKE-secret-Zx9", Label.POSSIBLE_SECRET, secret_state=state)
    )


# --- Which screen ---


def test_first_run_names_the_hotkey_and_the_tray() -> None:
    state = empty_state([], "", None, paused=False, open_hotkey="Ctrl+Alt+V")
    assert state.kind == EMPTY_NO_HISTORY and not state.actions
    assert "Ctrl+Alt+V" in state.body and "tray" in state.body


def test_paused_offers_resume() -> None:
    state = empty_state([], "", None, paused=True)
    assert state.kind == EMPTY_PAUSED and state.actions == (ACTION_RESUME,)
    assert "paused" in state.title.lower()


def test_search_without_a_match_says_search_and_offers_to_clear_it() -> None:
    state = empty_state([_note()], FAKE_SEARCH, None, paused=False)
    assert state.kind == EMPTY_NO_MATCH and state.actions == (ACTION_CLEAR_SEARCH,)
    assert "search" in state.body


def test_filter_without_a_match_says_filter_and_offers_show_all() -> None:
    state = empty_state([_note()], "", Label.LINK, paused=False)
    assert state.kind == EMPTY_NO_MATCH and state.actions == (ACTION_CLEAR_FILTER,)
    assert "Link" in state.title


def test_search_inside_a_filter_offers_both() -> None:
    state = empty_state([_note()], FAKE_SEARCH, Label.LINK, paused=False)
    assert state.actions == (ACTION_CLEAR_SEARCH, ACTION_CLEAR_FILTER)
    assert "Link" in state.body


def test_a_tag_filter_uses_its_chip_name() -> None:
    state = empty_state([_note()], "", TagFilter("work"), paused=False)
    assert "#work" in state.title


@pytest.mark.parametrize("state_kind", [SecretState.SECRET, SecretState.UNSURE])
def test_only_secrets_while_searching_says_they_are_left_out(state_kind: SecretState) -> None:
    state = empty_state([_secret(state=state_kind)], FAKE_SEARCH, None, paused=False)
    assert state.kind == EMPTY_SECRETS_ONLY and state.actions == (ACTION_CLEAR_SEARCH,)
    assert "on purpose" in state.body


def test_secret_chip_with_a_search_says_the_same() -> None:
    state = empty_state([_note(), _secret()], FAKE_SEARCH, SecretState.SECRET, paused=False)
    assert state.kind == EMPTY_SECRETS_ONLY


def test_mixed_history_with_a_search_is_an_ordinary_no_match() -> None:
    state = empty_state([_note(), _secret()], FAKE_SEARCH, None, paused=False)
    assert state.kind == EMPTY_NO_MATCH


def test_secrets_without_a_search_do_not_get_the_secrets_screen() -> None:
    # Only the Secret chip, nothing typed, and nothing to show (expired): a plain filter message.
    state = empty_state([_secret()], "", SecretState.SECRET, paused=False)
    assert state.kind == EMPTY_NO_MATCH


def test_the_words_never_repeat_the_search_text() -> None:
    for items, active in (([_note()], None), ([_secret()], None), ([_note()], Label.LINK)):
        state = empty_state(items, FAKE_SEARCH, active, paused=True)
        assert FAKE_SEARCH not in state.title + state.body


# --- In the window ---


@pytest.fixture(scope="module")
def qapp() -> QApplication:
    return QApplication.instance() or QApplication([])


def make_window(backend: FakeBackend, dark: bool = False) -> SearchWindow:
    th = theme.DARK if dark else theme.LIGHT
    win = SearchWindow(backend, now=Clock(), current_theme=lambda: th)
    win.show_and_focus()
    return win


@pytest.fixture
def empty_backend() -> FakeBackend:
    backend = FakeBackend()
    backend.store.clear()
    return backend


@pytest.fixture
def windows() -> Iterator[list[SearchWindow]]:
    made: list[SearchWindow] = []
    yield made
    for win in made:
        win.hide()
        win.deleteLater()


def test_the_window_shows_the_first_run_screen(
    qapp: QApplication, empty_backend: FakeBackend, windows: list[SearchWindow]
) -> None:
    win = make_window(empty_backend)
    windows.append(win)
    assert win.empty_view.isVisible() and not win.list.isVisible()
    assert win.empty_view.state is not None and win.empty_view.state.kind == EMPTY_NO_HISTORY
    assert not win.empty_view.buttons


def test_paused_empty_screen_replaces_the_banner_and_resume_works(
    qapp: QApplication, empty_backend: FakeBackend, windows: list[SearchWindow]
) -> None:
    empty_backend.paused = True
    win = make_window(empty_backend)
    windows.append(win)
    assert win.empty_view.state.kind == EMPTY_PAUSED
    assert not win.pause_banner.isVisible()  # no second Resume button
    win.empty_view.buttons[ACTION_RESUME].click()
    assert empty_backend.paused is False
    assert win.empty_view.state.kind == EMPTY_NO_HISTORY


def test_paused_with_history_keeps_the_banner(qapp: QApplication, windows: list[SearchWindow]) -> None:
    backend = FakeBackend()
    backend.paused = True
    win = make_window(backend)
    windows.append(win)
    assert win.list.isVisible() and win.pause_banner.isVisible()


def test_no_match_buttons_clear_the_search_and_the_filter(
    qapp: QApplication, windows: list[SearchWindow]
) -> None:
    win = make_window(FakeBackend())
    windows.append(win)
    win.set_filter(Label.LINK)
    win.search.setText(FAKE_SEARCH)
    assert win.empty_view.state.actions == (ACTION_CLEAR_SEARCH, ACTION_CLEAR_FILTER)
    win.empty_view.buttons[ACTION_CLEAR_SEARCH].click()
    assert win.search.text() == "" and win.active_filter == Label.LINK and win.list.isVisible()
    win.search.setText(FAKE_SEARCH)
    win.empty_view.buttons[ACTION_CLEAR_FILTER].click()
    assert win.active_filter is None and win.search.text() == FAKE_SEARCH


def test_secrets_only_screen_in_the_window(
    qapp: QApplication, empty_backend: FakeBackend, windows: list[SearchWindow]
) -> None:
    empty_backend.store[1] = StoredItem(
        1, START - timedelta(minutes=1),
        ItemPayload("FAKE-secret-Zx9", Label.POSSIBLE_SECRET, secret_state=SecretState.SECRET),
    )
    win = make_window(empty_backend)
    windows.append(win)
    win.search.setText(FAKE_SEARCH)
    assert win.empty_view.state.kind == EMPTY_SECRETS_ONLY
    shown = win.empty_view.title_label.text() + win.empty_view.body_label.text()
    assert "FAKE-secret-Zx9" not in shown and FAKE_SEARCH not in shown


@pytest.mark.parametrize("dark", [False, True])
@pytest.mark.parametrize(
    "kind",
    [EMPTY_NO_HISTORY, EMPTY_NO_MATCH, EMPTY_PAUSED, EMPTY_SECRETS_ONLY],
)
def test_every_screen_fits_inside_the_window_and_draws(
    qapp: QApplication, empty_backend: FakeBackend, windows: list[SearchWindow], kind: str, dark: bool
) -> None:
    from PySide6.QtCore import QRect

    if kind == EMPTY_SECRETS_ONLY:
        empty_backend.store[1] = _secret()
    elif kind == EMPTY_NO_MATCH:
        empty_backend.store[1] = _note()
    empty_backend.paused = kind == EMPTY_PAUSED
    win = make_window(empty_backend, dark)
    windows.append(win)
    if kind in (EMPTY_SECRETS_ONLY, EMPTY_NO_MATCH):
        win.search.setText(FAKE_SEARCH)
    view = win.empty_view
    assert view.state.kind == kind
    qapp.processEvents()
    inside = QRect(0, 0, view.width(), view.height())
    for part in (view.icon, view.title_label, view.body_label, *view.buttons.values()):
        rect = QRect(part.mapTo(view, part.rect().topLeft()), part.size())
        assert inside.contains(rect), f"{part.objectName() or part} sticks out of the empty screen"
    # The wrapped text is tall enough for what it says (not cut by the layout).
    body = view.body_label
    assert body.height() >= body.heightForWidth(body.width())
    assert not win.grab().toImage().isNull()
