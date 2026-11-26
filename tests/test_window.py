"""Tests for the clipboard window with a fake backend. Fake data only.

Qt runs with the "offscreen" platform, so no window appears on screen.
These check what the window does with rows; the rules themselves are tested
in test_view_model.py, and pasting in test_paste.py.
"""

import os
from collections.abc import Callable, Iterator
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

pytest.importorskip("PySide6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPoint, QRect, QSize, Qt  # noqa: E402
from PySide6.QtGui import QFont, QFontMetrics  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication, QMenu, QWidget  # noqa: E402

from smartclip.labels import Label, SecretState  # noqa: E402
from smartclip.storage import ItemPayload, StoredItem  # noqa: E402
from smartclip.ui import theme  # noqa: E402
from smartclip.ui.view_model import MASK, Row  # noqa: E402
from smartclip.ui.window import (  # noqa: E402
    REGION_CARD,
    REGION_DELETE,
    REGION_LABEL,
    REGION_MARK,
    REGION_COPY,
    REGION_EDIT,
    REGION_MENU,
    REGION_PASTE,
    REGION_STRIP,
    REGION_PIN,
    card_height,
    REGION_REVEAL,
    REGION_STATE,
    REGION_TEXT,
    ROW_ROLE,
    SearchWindow,
    card_buttons,
    card_rects,
    clear_question,
    card_tooltip,
    question_box,
    place_near,
    present_filters,
    region_at,
    show_label_badge,
    wrap_lines,
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

    def restore(self, items: list[StoredItem]) -> int:
        self.calls.append(("restore", len(items)))
        for item in items:
            new_id = max(self.store, default=0) + 1
            self.store[new_id] = replace(item, id=new_id)
        return len(items)

    def mark_normal(self, item_id: int) -> bool:
        self.calls.append(("mark_normal", item_id))
        return self._set(item_id, secret_state=SecretState.NORMAL)

    def rename(self, item_id: int, title: str | None) -> bool:
        self.calls.append(("rename", item_id))
        item = self.store.get(item_id)
        if item is None or not item.payload.pinned or item.payload.secret_state is not SecretState.NORMAL:
            return False
        return self._set(item_id, title=title)

    def set_tags(self, item_id: int, tags: tuple[str, ...]) -> bool:
        self.calls.append(("set_tags", item_id))
        item = self.store.get(item_id)
        if item is None or not item.payload.pinned or item.payload.secret_state is not SecretState.NORMAL:
            return False
        return self._set(item_id, tags=tags)

    def mark_secret(self, item_id: int) -> bool:
        self.calls.append(("mark_secret", item_id))
        return self._set(
            item_id, secret_state=SecretState.SECRET, pinned=False, title=None, tags=()
        )

    def is_paused(self) -> bool:
        return self.paused

    def toggle_pause(self) -> bool:
        self.paused = not self.paused
        return self.paused

    def quit(self) -> None:
        self.calls.append(("quit", 0))


class FakePaster:
    """Stands in for ui/paste.Paster: records calls, runs the copy."""

    def __init__(self) -> None:
        self.remembered = 0
        self.pasted = 0

    def remember(self) -> None:
        self.remembered += 1

    def paste(self, copy: Callable[[], bool]) -> bool:
        self.pasted += 1
        return copy()


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
    win = SearchWindow(backend, now=clock)
    win.show_and_focus()
    yield win
    win.hide()
    win.deleteLater()


def previews(window: SearchWindow) -> list[str]:
    return [row.preview for row in window.model.rows]


def row_of(window: SearchWindow, item_id: int) -> Row:
    return next(row for row in window.model.rows if row.item_id == item_id)


def click(window: SearchWindow, item_id: int, region: str) -> None:
    window.select_item(item_id)
    window.handle_region(row_of(window, item_id), region)


def menu_action(window: SearchWindow, item_id: int, text: str):  # noqa: ANN201 (QAction)
    menu: QMenu = window.build_menu(row_of(window, item_id))
    return next(action for action in menu.actions() if action.text() == text)


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


def test_window_is_a_small_frameless_panel(window: SearchWindow) -> None:
    flags = window.windowFlags()
    assert flags & Qt.WindowType.FramelessWindowHint
    assert flags & Qt.WindowType.WindowStaysOnTopHint
    assert window.width() <= 400


def test_empty_history_shows_a_hint(qapp: QApplication, backend: FakeBackend) -> None:
    backend.store.clear()
    win = SearchWindow(backend)
    win.show_and_focus()
    assert win.empty_view.isVisible() and win.empty_view.title_label.text() == "Nothing copied yet"
    assert not win.list.isVisible()
    win.hide()


def test_no_match_shows_a_different_hint(window: SearchWindow) -> None:
    window.search.setText("nothing-matches-this")
    assert window.empty_view.title_label.text() == "No matches" and window.empty_view.isVisible()


# --- Search and filters ---


def test_typing_filters_rows_and_hides_masked_items(window: SearchWindow) -> None:
    window.search.setText("fake")
    assert [row.item_id for row in window.model.rows] == [1, 2]


def test_secret_text_does_not_find_the_secret(window: SearchWindow) -> None:
    window.search.setText("Zx9")
    assert window.model.rows == []


def test_clicking_type_badge_filters_by_type(window: SearchWindow) -> None:
    click(window, 2, REGION_LABEL)
    assert window.active_filter is Label.LINK
    assert [row.item_id for row in window.model.rows] == [2]
    assert window.chips[Label.LINK].isChecked()
    window.chips[None].click()
    assert window.active_filter is None
    assert len(window.model.rows) == 4


def test_clicking_state_badge_filters_by_state(window: SearchWindow) -> None:
    click(window, 4, REGION_STATE)
    assert window.active_filter is SecretState.UNSURE
    assert previews(window) == [MASK]


def test_state_click_on_a_normal_card_chooses_it(
    window: SearchWindow, backend: FakeBackend
) -> None:
    click(window, 1, REGION_STATE)  # a normal card has no state badge
    assert window.active_filter is None
    assert backend.calls == [("copy_back", 1)]


def test_chips_list_only_what_is_in_the_history(window: SearchWindow) -> None:
    assert list(window.chips) == [
        None, SecretState.SECRET, SecretState.UNSURE, Label.LINK, Label.NOTE,
    ]
    window.chips[Label.NOTE].click()
    assert window.active_filter is Label.NOTE


def test_active_filter_keeps_its_chip_after_its_items_go(
    window: SearchWindow, backend: FakeBackend
) -> None:
    window.set_filter(Label.LINK)
    del backend.store[2]
    window.refresh()
    assert Label.LINK in window.chips and window.chips[Label.LINK].isChecked()


def test_present_filters_order() -> None:
    items = [
        StoredItem(1, START, ItemPayload("FAKE", Label.SQL)),
        StoredItem(2, START, ItemPayload("FAKE", Label.LINK, secret_state=SecretState.UNSURE)),
    ]
    assert present_filters(items) == [SecretState.UNSURE, Label.LINK, Label.SQL]


# --- Reveal ---


def test_clicking_masked_text_reveals_for_ten_seconds(window: SearchWindow, clock: Clock) -> None:
    click(window, 3, REGION_TEXT)
    assert FAKE_SECRET_TEXT in previews(window)
    clock.now += timedelta(seconds=9)
    window._tick()
    assert FAKE_SECRET_TEXT in previews(window)
    clock.now += timedelta(seconds=1)
    window._tick()
    assert FAKE_SECRET_TEXT not in previews(window)


def test_clicking_revealed_text_masks_it(window: SearchWindow) -> None:
    click(window, 3, REGION_TEXT)
    click(window, 3, REGION_TEXT)
    assert FAKE_SECRET_TEXT not in previews(window)


def test_hiding_the_window_masks_every_reveal(window: SearchWindow) -> None:
    click(window, 3, REGION_TEXT)
    window.hide()
    assert not window.reveals.any_revealed()
    assert FAKE_SECRET_TEXT not in previews(window)


def test_masked_row_never_exposes_content_in_any_role(window: SearchWindow) -> None:
    index = window.model.index(window.model.position_of(3))
    for role in list(range(0, 64)) + [ROW_ROLE]:
        assert FAKE_SECRET_TEXT not in str(window.model.data(index, role))


def test_tooltips_never_quote_the_item(window: SearchWindow) -> None:
    window.reveals.reveal(3, START)
    window.refresh()
    row = row_of(window, 3)
    for region in (REGION_CARD, REGION_LABEL, REGION_STATE, REGION_MENU, REGION_TEXT):
        assert FAKE_SECRET_TEXT not in card_tooltip(row, region)


def test_reveal_in_menu_only_for_sensitive_rows(window: SearchWindow) -> None:
    assert not menu_action(window, 1, "Reveal").isEnabled()
    action = menu_action(window, 3, "Reveal")
    assert action.isEnabled()
    action.trigger()
    assert FAKE_SECRET_TEXT in previews(window)
    assert menu_action(window, 3, "Hide").isEnabled()


def test_ctrl_r_reveals_selected(window: SearchWindow) -> None:
    window.select_item(3)
    window.shortcuts["Ctrl+R"].activated.emit()
    assert FAKE_SECRET_TEXT in previews(window)


# --- Choosing (copy and paste) ---


def test_enter_in_search_chooses_selected_and_hides(
    window: SearchWindow, backend: FakeBackend
) -> None:
    QTest.keyClick(window.search, Qt.Key.Key_Down)
    QTest.keyClick(window.search, Qt.Key.Key_Return)
    assert backend.calls == [("copy_back", 2)]
    assert not window.isVisible()


def test_clicking_a_card_chooses_it(window: SearchWindow, backend: FakeBackend) -> None:
    click(window, 2, REGION_CARD)
    assert backend.calls == [("copy_back", 2)]
    assert not window.isVisible()


def test_choosing_a_masked_item_does_not_reveal_it(
    window: SearchWindow, backend: FakeBackend
) -> None:
    click(window, 3, REGION_CARD)
    assert backend.calls == [("copy_back", 3)]
    assert not window.reveals.any_revealed()


def test_failed_copy_keeps_window_open_with_message(
    window: SearchWindow, backend: FakeBackend
) -> None:
    backend.copy_ok = False
    window.choose_selected()
    assert window.isVisible()
    assert window.status_label.text() == "Could not copy"
    assert window.status_label.isVisible()


def test_with_a_paster_choosing_pastes_and_menu_offers_copy_only(
    qapp: QApplication, backend: FakeBackend
) -> None:
    paster = FakePaster()
    win = SearchWindow(backend, paster=paster)  # type: ignore[arg-type]
    win.show_and_focus()
    assert paster.remembered == 1
    win.show_and_focus()  # already open: the target is not replaced
    assert paster.remembered == 1
    texts = [a.text() for a in win.build_menu(row_of(win, 1)).actions()]
    assert texts[:2] == ["Paste", "Copy only"]
    win.choose_selected()
    assert paster.pasted == 1 and backend.calls == [("copy_back", 1)]
    win.show_and_focus()
    menu_action(win, 2, "Copy only").trigger()
    assert paster.pasted == 1 and backend.calls[-1] == ("copy_back", 2)
    win.hide()


def test_without_a_paster_menu_offers_copy(window: SearchWindow, backend: FakeBackend) -> None:
    menu_action(window, 1, "Copy").trigger()
    assert backend.calls == [("copy_back", 1)]


def test_escape_clears_search_then_hides(window: SearchWindow) -> None:
    window.search.setText("lunch")
    QTest.keyClick(window.search, Qt.Key.Key_Escape)
    assert window.search.text() == ""
    assert window.isVisible()
    QTest.keyClick(window.search, Qt.Key.Key_Escape)
    assert not window.isVisible()


# --- Card menu actions ---


def test_pin_is_disabled_for_secret_and_unsure(window: SearchWindow) -> None:
    for item_id in (3, 4):
        assert not menu_action(window, item_id, "Pin").isEnabled()
    assert menu_action(window, 1, "Pin").isEnabled()


def test_pin_then_unpin(window: SearchWindow, backend: FakeBackend) -> None:
    menu_action(window, 1, "Pin").trigger()
    assert row_of(window, 1).pinned is True
    menu_action(window, 1, "Unpin").trigger()
    assert backend.calls == [("pin", 1), ("unpin", 1)]


def test_ctrl_p_pins_selected(window: SearchWindow, backend: FakeBackend) -> None:
    window.select_item(2)
    window.shortcuts["Ctrl+P"].activated.emit()
    assert backend.calls == [("pin", 2)]


def test_mark_normal_only_for_sensitive_rows(window: SearchWindow, backend: FakeBackend) -> None:
    assert not menu_action(window, 1, "Mark normal").isEnabled()
    menu_action(window, 4, "Mark normal").trigger()
    assert backend.calls == [("mark_normal", 4)]
    assert row_of(window, 4).masked is False


def test_mark_secret_not_offered_for_secret_rows(
    window: SearchWindow, backend: FakeBackend
) -> None:
    assert not menu_action(window, 3, "Mark secret").isEnabled()
    menu_action(window, 1, "Mark secret").trigger()
    assert backend.calls == [("mark_secret", 1)]
    assert row_of(window, 1).preview == MASK


def test_delete_is_immediate_and_offers_undo(
    qapp: QApplication, backend: FakeBackend, clock: Clock
) -> None:
    win = SearchWindow(backend, now=clock)
    win.show_and_focus()
    menu_action(win, 1, "Delete").trigger()
    assert backend.calls == [("delete", 1)]
    assert 1 not in [row.item_id for row in win.model.rows]
    assert win.toast.is_active and win.toast.label.text() == "Item deleted"
    assert win.toast.action_button.text() == "Undo"
    win.hide()


def test_shift_del_deletes_the_selected_card(
    qapp: QApplication, backend: FakeBackend, clock: Clock
) -> None:
    win = SearchWindow(backend, now=clock)
    win.show_and_focus()
    win.shortcuts["Shift+Del"].activated.emit()
    assert backend.calls == [("delete", 1)]
    win.hide()


def test_question_box_stays_above_the_on_top_window(window: SearchWindow) -> None:
    # The window is always on top; an ordinary dialog could sit behind it.
    assert window.windowFlags() & Qt.WindowType.WindowStaysOnTopHint
    box = question_box(window, "Clear all", clear_question(2))
    assert box.windowFlags() & Qt.WindowType.WindowStaysOnTopHint
    assert box.parent() is window
    assert box.windowModality() == Qt.WindowModality.ApplicationModal
    box.deleteLater()


# --- Header: pause and quit ---


def test_pause_indicator_follows_backend(window: SearchWindow, backend: FakeBackend) -> None:
    assert not window.pause_label.isVisible()
    window.pause_button.click()
    assert backend.paused is True
    assert window.pause_label.isVisible()
    assert window.pause_button.toolTip().startswith("Resume capture")


def test_close_hides_the_window_and_does_not_quit(
    window: SearchWindow, backend: FakeBackend
) -> None:
    window.close()
    assert not window.isVisible()
    assert ("quit", 0) not in backend.calls


def test_selection_follows_the_item_after_refresh(
    window: SearchWindow, backend: FakeBackend
) -> None:
    window.select_item(2)
    backend.store[9] = StoredItem(9, START + timedelta(minutes=1), ItemPayload("FAKE newer", Label.NOTE))
    window.refresh()
    assert window.selected_row().item_id == 2


# --- Theme ---


def test_theme_follows_the_given_scheme(qapp: QApplication, backend: FakeBackend) -> None:
    win = SearchWindow(backend, current_theme=lambda: theme.DARK)
    assert win.delegate.theme is theme.DARK
    assert theme.DARK.window in win.styleSheet()
    assert theme.theme_for(Qt.ColorScheme.Dark) is theme.DARK
    assert theme.theme_for(Qt.ColorScheme.Light) is theme.LIGHT
    assert theme.theme_for(Qt.ColorScheme.Unknown) is theme.LIGHT


# --- Card layout ---


def make_row(**changes: object) -> Row:
    base = Row(
        item_id=1, label=Label.NOTE, label_badge=row_badge(Label.NOTE), state=SecretState.NORMAL,
        state_badge=None, preview="FAKE preview text", source_app="fakeeditor.exe", age="5m",
        masked=False, revealed=False, pinned=False, can_pin=True,
    )
    return replace(base, **changes)


def row_badge(label: Label):  # noqa: ANN201
    from smartclip.ui.view_model import LABEL_BADGES

    return LABEL_BADGES[label]


def test_click_regions_of_a_card(qapp: QApplication) -> None:
    from smartclip.ui.view_model import STATE_BADGES

    metrics = QFontMetrics(QFont())
    row = make_row(
        label=Label.JSON, label_badge=row_badge(Label.JSON), state=SecretState.UNSURE,
        state_badge=STATE_BADGES[SecretState.UNSURE], preview=MASK, masked=True, can_pin=False,
    )
    rects = card_rects(QRect(0, 0, 360, 100), row, metrics)
    assert rects.label is not None and rects.state is not None and rects.text is not None
    assert region_at(rects, rects.label.center()) == REGION_LABEL
    assert region_at(rects, rects.state.center()) == REGION_STATE
    assert region_at(rects, rects.menu.center()) == REGION_MENU
    assert region_at(rects, rects.text.center()) == REGION_TEXT
    assert region_at(rects, QPoint(rects.card.right() - 2, rects.card.bottom() - 2)) == REGION_CARD
    assert not rects.label.intersects(rects.state)
    assert not rects.state.intersects(rects.menu)


def test_possible_secret_label_is_not_shown_next_to_a_state_badge() -> None:
    from smartclip.ui.view_model import STATE_BADGES

    secret = make_row(
        label=Label.POSSIBLE_SECRET, label_badge=row_badge(Label.POSSIBLE_SECRET),
        state=SecretState.SECRET, state_badge=STATE_BADGES[SecretState.SECRET],
    )
    assert not show_label_badge(secret)
    assert card_rects(QRect(0, 0, 360, 100), secret, QFontMetrics(QFont())).label is None
    assert show_label_badge(make_row())


def test_long_text_wraps_to_three_lines_and_is_cut(qapp: QApplication) -> None:
    lines = wrap_lines("FAKE " * 200, QFont(), 200, 3)
    assert len(lines) == 3
    assert lines[-1].endswith("…")
    assert wrap_lines("FAKE short", QFont(), 200, 3) == ["FAKE short"]


def test_long_word_without_spaces_still_wraps(qapp: QApplication) -> None:
    assert len(wrap_lines("F" * 500, QFont(), 200, 3)) == 3


# --- Placement next to the pointer ---


SCREEN = QRect(0, 0, 1920, 1040)
SIZE = QSize(380, 520)


def test_placed_below_right_of_the_pointer_when_it_fits() -> None:
    assert place_near(QPoint(100, 100), SIZE, SCREEN) == QPoint(108, 108)


def test_flipped_left_and_up_near_the_screen_edges() -> None:
    position = place_near(QPoint(1900, 1000), SIZE, SCREEN)
    assert position == QPoint(1900 - 8 - 380, 1000 - 8 - 520)


def test_always_inside_the_screen() -> None:
    position = place_near(QPoint(5, 1030), SIZE, QRect(0, 0, 400, 1040))
    assert position.x() >= 0 and position.x() + 380 <= 400
    assert position.y() >= 0 and position.y() + 520 <= 1040


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


# --- Switches in the "..." menu (autostart.py) ---


class FakeSwitch:
    def __init__(self, label: str, works: bool = True) -> None:
        self.label = label
        self.on = False
        self.works = works

    def is_on(self) -> bool:
        return self.on

    def set_on(self, on: bool) -> bool:
        if self.works:
            self.on = on
        return self.works


# --- Card buttons (owner's decisions 2026-11-19) ---


def test_an_active_card_shows_only_pin_and_secrets_cannot_be_pinned(window: SearchWindow) -> None:
    assert card_buttons(row_of(window, 1)) == [REGION_PIN]
    assert card_buttons(row_of(window, 3)) == []  # secret: only "..." is left
    assert card_buttons(row_of(window, 4)) == []


def test_buttons_are_laid_out_only_on_an_active_card(qapp: QApplication) -> None:
    metrics = QFontMetrics(QFont())
    row = make_row(pinned=True)
    still = card_rects(QRect(0, 0, 360, 100), row, metrics)
    assert still.buttons == () and still.pin is not None
    active = card_rects(QRect(0, 0, 360, 100), row, metrics, active=True)
    assert active.pin is None  # the pin button takes the pin mark's place
    assert [region for region, _rect in active.buttons] == card_buttons(row)
    for region, rect in active.buttons:
        assert region_at(active, rect.center()) == region
        assert not rect.intersects(active.menu)
        assert not rect.intersects(active.top_text)


# --- The strip that slides in from "..." (owner's design, 2026-11-25) ---


def test_strip_icons_depend_on_the_card() -> None:
    from smartclip.ui.window import action_regions

    normal = make_row()
    secret = make_row(state=SecretState.SECRET, can_pin=False)
    pinned = make_row(pinned=True, can_name=True)
    assert action_regions(normal, paste=True) == [REGION_COPY, REGION_PASTE, REGION_MARK, REGION_DELETE]
    assert action_regions(normal, paste=False) == [REGION_COPY, REGION_MARK, REGION_DELETE]
    assert action_regions(secret, paste=True)[0] == REGION_REVEAL
    assert REGION_EDIT in action_regions(pinned, paste=True)
    assert REGION_EDIT not in action_regions(normal, paste=True) and REGION_EDIT not in action_regions(secret, True)


def test_strip_slides_in_over_the_right_of_the_card(qapp: QApplication) -> None:
    metrics = QFontMetrics(QFont())
    row = make_row()
    closed = card_rects(QRect(0, 0, 360, 100), row, metrics, active=True)
    assert closed.strip is None and closed.actions == ()
    half = card_rects(QRect(0, 0, 360, 100), row, metrics, active=True, slide=0.5)
    full = card_rects(QRect(0, 0, 360, 100), row, metrics, active=True, slide=1.0)
    assert half.strip is not None and full.strip is not None
    assert 0 < half.strip.width() < full.strip.width()
    assert full.strip.right() == full.card.right()  # anchored to the right edge
    for region, icon in full.actions:
        assert region_at(full, icon.center()) == region
        assert full.strip.contains(icon) and full.card.contains(icon)
    assert region_at(full, full.strip.topLeft() + QPoint(1, 1)) in (REGION_STRIP, full.actions[0][0])


def test_strip_opens_and_closes_from_the_menu_button(window: SearchWindow) -> None:
    window.delegate.animation_ms = 0
    window.select_item(1)
    click(window, 1, REGION_MENU)
    assert window.delegate.is_open(1) and window.delegate.slide_for(1) == 1.0
    click(window, 1, REGION_MENU)  # "..." again closes
    assert window.delegate.actions_item is None
    click(window, 1, REGION_MENU)
    click(window, 1, REGION_STRIP)  # the strip's empty area closes it
    assert window.delegate.actions_item is None


def test_strip_actions_copy_paste_mark_and_delete(window: SearchWindow, backend: FakeBackend) -> None:
    window.delegate.animation_ms = 0
    click(window, 1, REGION_MENU)
    click(window, 1, REGION_COPY)
    assert ("copy_back", 1) in backend.calls
    window.show_and_focus()
    click(window, 2, REGION_MENU)
    click(window, 2, REGION_MARK)
    assert row_of(window, 2).state is SecretState.SECRET and window.delegate.actions_item is None
    click(window, 2, REGION_MENU)
    click(window, 2, REGION_DELETE)
    assert ("delete", 2) in backend.calls


def test_escape_closes_an_open_strip_before_hiding_the_window(window: SearchWindow) -> None:
    window.delegate.animation_ms = 0
    click(window, 1, REGION_MENU)
    QTest.keyClick(window.search, Qt.Key.Key_Escape)
    assert window.delegate.actions_item is None and window.isVisible()
    QTest.keyClick(window.search, Qt.Key.Key_Escape)
    assert not window.isVisible()


def test_moving_the_selection_or_hiding_closes_the_strip(window: SearchWindow) -> None:
    window.delegate.animation_ms = 0
    click(window, 1, REGION_MENU)
    window.select_item(2)
    assert window.delegate.actions_item is None
    click(window, 2, REGION_MENU)
    window.hide()
    assert window.delegate.actions_item is None


def test_the_strip_animates_when_animation_is_on(window: SearchWindow) -> None:
    window.delegate.animation_ms = 120
    click(window, 1, REGION_MENU)
    assert window.delegate.is_open(1)
    from qt_wait import wait_for

    assert wait_for(lambda: window.delegate.slide_for(1) >= 1.0)


def test_button_clicks_act_on_their_card(window: SearchWindow, backend: FakeBackend) -> None:
    click(window, 1, REGION_PIN)
    assert row_of(window, 1).pinned
    click(window, 1, REGION_PIN)
    assert not row_of(window, 1).pinned
    click(window, 2, REGION_MARK)
    assert row_of(window, 2).state is SecretState.SECRET
    click(window, 3, REGION_REVEAL)
    assert row_of(window, 3).revealed
    click(window, 3, REGION_REVEAL)
    assert row_of(window, 3).masked
    click(window, 4, REGION_MARK)
    assert row_of(window, 4).state is SecretState.NORMAL
    click(window, 1, REGION_DELETE)
    assert backend.calls[-1] == ("delete", 1)


def test_button_delete_deletes_at_once(qapp: QApplication, backend: FakeBackend, clock: Clock) -> None:
    win = SearchWindow(backend, now=clock)
    win.show_and_focus()
    click(win, 1, REGION_DELETE)
    assert ("delete", 1) in backend.calls
    win.hide()


def test_button_tooltips_never_hold_the_text(window: SearchWindow) -> None:
    for item_id in (1, 3):
        row = row_of(window, item_id)
        for region in card_buttons(row):
            tip = card_tooltip(row, region)
            assert tip and FAKE_SECRET_TEXT not in tip and "lunch" not in tip


# --- The top bar that drops down (owner's design, 2026-11-25) ---


def folded_window(backend: FakeBackend, clock: Clock) -> SearchWindow:
    win = SearchWindow(backend, now=clock, start_open=False)
    win.drawer_animation_ms = 0
    win.show_and_focus()
    return win


def test_the_top_bar_starts_folded_when_asked_and_open_otherwise(
    qapp: QApplication, backend: FakeBackend, clock: Clock
) -> None:
    folded = folded_window(backend, clock)
    assert not folded.drawer_open
    assert not folded.search.isVisible() and not folded.filters_row.isVisible()
    assert not folded.pause_button.isVisible() and not folded.settings_button.isVisible()
    assert folded.handle.isVisible()
    folded.hide()
    opened = SearchWindow(backend, now=clock)
    opened.show_and_focus()
    assert opened.drawer_open and opened.search.isVisible() and opened.pause_button.isVisible()
    opened.hide()


def test_the_handle_drops_the_bar_down_and_folds_it_again(
    qapp: QApplication, backend: FakeBackend, clock: Clock
) -> None:
    win = folded_window(backend, clock)
    win.handle.click()
    assert win.drawer_open and win.search.isVisible() and win.filters_row.isVisible()
    assert win.pause_button.isVisible() and win.settings_button.isVisible()
    assert "Hide" in win.handle.toolTip()
    win.handle.click()
    assert not win.drawer_open and not win.search.isVisible()
    assert "Show" in win.handle.toolTip()
    win.hide()


def test_folding_the_bar_clears_the_search_and_filter(
    qapp: QApplication, backend: FakeBackend, clock: Clock
) -> None:
    win = folded_window(backend, clock)
    win.handle.click()
    win.search.setText("lunch")
    win.set_filter(Label.NOTE)
    assert len(win.model.rows) == 1
    win.handle.click()  # fold: nothing hidden may keep filtering
    assert win.search.text() == "" and win.active_filter is None
    assert len(win.model.rows) == 4
    win.hide()


def test_typing_or_ctrl_f_opens_a_folded_bar(
    qapp: QApplication, backend: FakeBackend, clock: Clock
) -> None:
    win = folded_window(backend, clock)
    QTest.keyClick(win, Qt.Key.Key_Down)
    assert win.selected_row().item_id == 2
    QTest.keyClicks(win, "l")
    assert win.drawer_open and win.search.text() == "l"
    win.hide()
    win.show_and_focus()
    assert not win.drawer_open  # a fresh opening starts folded again
    win.shortcuts["Ctrl+F"].activated.emit()
    assert win.drawer_open and win.search.text() == ""
    win.hide()


def test_choosing_a_filter_opens_a_folded_bar_so_it_can_be_cleared(
    qapp: QApplication, backend: FakeBackend, clock: Clock
) -> None:
    win = folded_window(backend, clock)
    click(win, 1, REGION_LABEL)
    assert win.active_filter is Label.NOTE and win.drawer_open and win.filters_row.isVisible()
    win.hide()


def test_the_bar_animates_when_animation_is_on(
    qapp: QApplication, backend: FakeBackend, clock: Clock
) -> None:
    from qt_wait import wait_for

    win = SearchWindow(backend, now=clock, start_open=False)
    win.drawer_animation_ms = 120
    win.show_and_focus()
    win.handle.click()
    assert win.drawer.isVisible()  # dropping down
    assert wait_for(lambda: win.drawer.maximumHeight() == 16777215)
    win.handle.click()
    assert wait_for(lambda: not win.drawer.isVisible())
    win.hide()


# --- Clear all ---


def clear_window(
    backend: FakeBackend, clock: Clock, answer: bool = True
) -> tuple[SearchWindow, list[int]]:
    asked: list[int] = []

    def confirm(_parent: QWidget, count: int) -> bool:
        asked.append(count)
        return answer

    win = SearchWindow(backend, now=clock, confirm_clear=confirm)
    win.show_and_focus()
    return win, asked


def test_clear_all_keeps_pinned_items(qapp: QApplication, backend: FakeBackend, clock: Clock) -> None:
    backend.pin(1)
    win, asked = clear_window(backend, clock)
    win.clear_button.click()
    assert asked == [3]
    assert [row.item_id for row in win.model.rows] == [1]
    assert not win.clear_button.isEnabled()
    win.hide()


def test_clear_all_only_clears_the_chip_shown(
    qapp: QApplication, backend: FakeBackend, clock: Clock
) -> None:
    win, asked = clear_window(backend, clock)
    win.set_filter(Label.LINK)
    win.clear_all()
    assert asked == [1]
    assert sorted(backend.store) == [1, 3, 4]
    win.hide()


def test_clear_all_does_nothing_when_answered_no(
    qapp: QApplication, backend: FakeBackend, clock: Clock
) -> None:
    win, asked = clear_window(backend, clock, answer=False)
    win.clear_button.click()
    assert asked == [4] and len(backend.store) == 4
    win.hide()


def test_clear_question_gives_only_a_count() -> None:
    assert clear_question(1) == "Delete this item? Pinned items are kept. This cannot be undone."
    assert "3 items" in clear_question(3)



# --- Pin under "...", 2 x 2 strip, larger buttons (owner's request, 2026-11-26) ---


def test_pin_sits_directly_under_the_more_button(qapp: QApplication) -> None:
    metrics = QFontMetrics(QFont())
    row = make_row()
    rects = card_rects(QRect(0, 0, 360, 120), row, metrics, active=True)
    [(region, pin)] = rects.buttons
    assert region == REGION_PIN
    assert pin.left() == rects.menu.left() and pin.top() > rects.menu.bottom()
    assert rects.card.contains(pin) and not pin.intersects(rects.menu)
    # A pinned card without buttons draws its pin mark in the same place.
    quiet = card_rects(QRect(0, 0, 360, 120), make_row(pinned=True), metrics, active=False)
    assert quiet.pin is not None and quiet.pin.topLeft() == pin.topLeft()
    # The body keeps clear of the pin column, hovered or not.
    assert rects.body.right() < pin.left()
    assert card_rects(QRect(0, 0, 360, 120), row, metrics).body == rects.body


def test_a_short_pinnable_card_is_tall_enough_for_the_stacked_buttons(qapp: QApplication) -> None:
    font = QFont()
    metrics = QFontMetrics(font)
    row = make_row()
    height = card_height(row, font, 360)
    rects = card_rects(QRect(0, 0, 360, height), row, metrics, active=True)
    assert rects.card.contains(rects.buttons[0][1]) and rects.card.contains(rects.menu)


def test_buttons_are_larger_than_the_badges(qapp: QApplication) -> None:
    metrics = QFontMetrics(QFont())
    rects = card_rects(QRect(0, 0, 360, 120), make_row(), metrics, active=True, slide=1.0)
    assert rects.menu.height() > metrics.height() + 4
    assert all(icon.height() > metrics.height() + 4 for _r, icon in rects.actions)


def test_strip_icons_form_two_rows(qapp: QApplication) -> None:
    metrics = QFontMetrics(QFont())
    four = card_rects(QRect(0, 0, 360, 120), make_row(), metrics, active=True, slide=1.0)
    spots = [icon.topLeft() for _r, icon in four.actions]
    assert len(four.actions) == 4 and len({p.y() for p in spots}) == 2 and len({p.x() for p in spots}) == 2
    five = card_rects(
        QRect(0, 0, 360, 120), make_row(pinned=True, can_name=True), metrics, active=True, slide=1.0
    )
    assert len(five.actions) == 5 and len({icon.top() for _r, icon in five.actions}) == 2
    for _r, icon in five.actions:
        assert five.strip is not None and five.strip.contains(icon) and five.card.contains(icon)


# --- Accessibility pass (owner's request, 2026-11-26) ---


def test_cards_have_a_spoken_description_without_secret_text(window: SearchWindow) -> None:
    from PySide6.QtCore import Qt as _Qt

    for position, entry in enumerate(window.model.entries):
        spoken = window.model.data(window.model.index(position), _Qt.ItemDataRole.AccessibleTextRole)
        assert spoken
        if getattr(entry, "masked", False):
            assert "text hidden" in spoken and FAKE_SECRET_TEXT not in spoken and "•" not in spoken
            assert "left" in spoken  # the time left is announced
    normal = next(r for r in window.model.rows if not r.masked)
    index = window.model.index(window.model.position_of(normal.item_id))
    assert normal.preview in window.model.data(index, _Qt.ItemDataRole.AccessibleTextRole)


def test_icon_controls_have_names_and_tooltips(window: SearchWindow) -> None:
    assert window.settings_button.accessibleName() == "Settings"
    assert "Ctrl+," in window.settings_button.toolTip()
    assert window.pause_button.accessibleName() in ("Pause capture", "Resume capture")
    assert window.pause_button.toolTip().startswith(window.pause_button.accessibleName())
    assert window.handle.accessibleName() and window.search.accessibleName()
    assert window.list.accessibleName() == "Clipboard items"
    assert all(chip.accessibleName() for chip in window.chips.values())


def test_every_card_action_is_reachable_without_the_mouse(window: SearchWindow) -> None:
    for keys in ("Ctrl+E", "Ctrl+,", "Ctrl+Shift+P", "Ctrl+D", "Ctrl+P", "Ctrl+R", "Shift+Del", "Ctrl+F"):
        assert keys in window.shortcuts
    window.select_item(1)
    window.shortcuts["Ctrl+E"].activated.emit()
    assert window.delegate.is_open(1)
    window.shortcuts["Ctrl+E"].activated.emit()
    assert not window.delegate.is_open(1)


# --- Review? cards can be marked secret too; the shortcuts popup (owner's request, 2026-11-26) ---


def test_a_review_card_offers_both_mark_normal_and_mark_secret() -> None:
    from smartclip.ui.window import REGION_MARK_SECRET, action_regions

    unsure = make_row(state=SecretState.UNSURE, can_pin=False)
    names = action_regions(unsure, paste=True)
    assert REGION_MARK in names and REGION_MARK_SECRET in names
    assert REGION_MARK_SECRET not in action_regions(make_row(), paste=True)
    assert REGION_MARK_SECRET not in action_regions(make_row(state=SecretState.SECRET, can_pin=False), True)
    assert card_tooltip(unsure, REGION_MARK) == "Mark normal"
    assert card_tooltip(unsure, REGION_MARK_SECRET) == "Mark secret"


def test_clicking_mark_secret_on_a_review_card_makes_it_secret(window: SearchWindow, backend: FakeBackend) -> None:
    from smartclip.ui.window import REGION_MARK_SECRET

    review = next(r for r in window.model.rows if r.state is SecretState.UNSURE)
    window.select_item(review.item_id)
    window.handle_region(review, REGION_MARK_SECRET)
    assert backend.store[review.item_id].payload.secret_state is SecretState.SECRET


def test_the_hint_bar_is_gone_and_settings_open_the_shortcuts_list(window: SearchWindow) -> None:
    assert not hasattr(window, "hint_label")


def test_shortcuts_list_covers_every_window_shortcut(window: SearchWindow) -> None:
    from smartclip import config
    from smartclip.ui.shortcuts_dialog import shortcut_groups

    listed = " | ".join(keys for _g, entries in shortcut_groups() for keys, _w in entries)
    for keys in window.shortcuts:
        if keys.startswith("Alt+"):
            continue  # the numbered ones are listed as a range
        shown = {"Menu": "Menu key"}.get(keys, keys)
        assert shown in listed, keys
    for hotkey in (config.HOTKEY_OPEN_WINDOW, config.HOTKEY_PAUSE, config.HOTKEY_MARK_SECRET, config.HOTKEY_FORCE_SAVE):
        assert hotkey in listed
    assert "Alt+1 to Alt+9" in listed and "Enter" in listed and "Esc" in listed


def test_shortcuts_dialog_shows_names_and_closes_with_escape(qapp: QApplication) -> None:
    from PySide6.QtCore import Qt as _Qt
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QLabel

    from smartclip.ui.shortcuts_dialog import ShortcutsDialog

    dialog = ShortcutsDialog(None, active_window=lambda: dialog)
    dialog.show()
    names = [label.accessibleName() for label in dialog.findChildren(QLabel) if label.accessibleName()]
    assert any(name.startswith("Ctrl+P, ") for name in names)
    QTest.keyClick(dialog, _Qt.Key.Key_Escape)
    assert not dialog.isVisible()


def test_no_possible_secret_chip_beside_the_secret_and_review_chips(window: SearchWindow) -> None:
    assert Label.POSSIBLE_SECRET not in window.chips
    assert SecretState.SECRET in window.chips and SecretState.UNSURE in window.chips


def test_a_link_marked_secret_shows_only_the_secret_badge(window: SearchWindow, backend: FakeBackend) -> None:
    window.mark_secret(next(r for r in window.model.rows if r.item_id == 2))  # the fake link
    row = next(r for r in window.model.rows if r.item_id == 2)
    assert row.label is Label.LINK and row.state is SecretState.SECRET
    assert not show_label_badge(row)
    rects = card_rects(QRect(0, 0, 360, 100), row, QFontMetrics(QFont()))
    assert rects.label is None and rects.state is not None


def test_review_cards_keep_their_type_badge() -> None:
    from smartclip.ui.view_model import STATE_BADGES

    unsure = make_row(state=SecretState.UNSURE, state_badge=STATE_BADGES[SecretState.UNSURE])
    assert show_label_badge(unsure)


def test_search_clear_button_shows_only_with_text_and_clears(window: SearchWindow) -> None:
    from PySide6.QtCore import QRect

    assert not window.search_clear.isVisible()
    window.search.setText("fake")
    assert window.search_clear.isVisible()
    box = QRect(0, 0, window.search.width(), window.search.height())
    assert box.contains(QRect(window.search_clear.pos(), window.search_clear.size()))
    # Centred up and down (within a pixel or two) in the box, and clear of the right edge.
    middle = window.search_clear.y() + window.search_clear.height() / 2
    assert abs(middle - window.search.height() / 2) <= 3
    window.search_clear.click()
    assert window.search.text() == "" and not window.search_clear.isVisible()


def test_search_clear_button_is_gone_after_the_window_reopens(window: SearchWindow) -> None:
    window.search.setText("fake")
    window.hide()
    window.show_and_focus()  # clears the text with signals blocked
    assert window.search.text() == "" and not window.search_clear.isVisible()
