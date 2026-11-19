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
from smartclip.ui.prefs import MemoryPrefs, WindowPrefs  # noqa: E402
from smartclip.ui.view_model import MASK, Row  # noqa: E402
from smartclip.ui.window import (  # noqa: E402
    EMPTY_TEXT,
    NO_MATCH_TEXT,
    REGION_CARD,
    REGION_DELETE,
    REGION_LABEL,
    REGION_MARK,
    REGION_MENU,
    REGION_PIN,
    REGION_REVEAL,
    REGION_STATE,
    REGION_TEXT,
    ROW_ROLE,
    SearchWindow,
    card_buttons,
    card_rects,
    clear_question,
    card_tooltip,
    delete_question_box,
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
    win = SearchWindow(backend, now=clock, confirm_delete=lambda _parent: True)
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
    assert win.empty_label.isVisible() and win.empty_label.text() == EMPTY_TEXT
    assert not win.list.isVisible()
    win.hide()


def test_no_match_shows_a_different_hint(window: SearchWindow) -> None:
    window.search.setText("nothing-matches-this")
    assert window.empty_label.text() == NO_MATCH_TEXT and window.empty_label.isVisible()


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
        None, SecretState.SECRET, SecretState.UNSURE, Label.LINK, Label.NOTE, Label.POSSIBLE_SECRET,
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
    index = window.model.index(window.model.rows.index(row_of(window, 3)))
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


def test_delete_asks_first(qapp: QApplication, backend: FakeBackend, clock: Clock) -> None:
    answers: list[bool] = [False, True]
    win = SearchWindow(backend, now=clock, confirm_delete=lambda _parent: answers.pop(0))
    win.show_and_focus()
    menu_action(win, 1, "Delete").trigger()
    assert backend.calls == []
    win.shortcuts["Shift+Del"].activated.emit()
    assert backend.calls == [("delete", 1)]
    assert 1 not in [row.item_id for row in win.model.rows]
    win.hide()


def test_delete_question_stays_above_the_on_top_window(window: SearchWindow) -> None:
    # The window is always on top; an ordinary dialog could sit behind it.
    assert window.windowFlags() & Qt.WindowType.WindowStaysOnTopHint
    box = delete_question_box(window)
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
    assert window.pause_button.toolTip() == "Resume capture"
    assert window.pause_action.text() == "Resume capture"


def test_close_hides_and_quit_is_in_the_more_menu(
    window: SearchWindow, backend: FakeBackend
) -> None:
    window.close()
    assert not window.isVisible()
    assert ("quit", 0) not in backend.calls
    window.show_and_focus()
    window.quit_action.trigger()
    assert ("quit", 0) in backend.calls


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
        label=Label.JSON, label_badge=row_badge(Label.JSON), state=SecretState.SECRET,
        state_badge=STATE_BADGES[SecretState.SECRET], preview=MASK, masked=True, can_pin=False,
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


def test_switches_are_tick_boxes_that_follow_the_setting(
    qapp: QApplication, backend: FakeBackend
) -> None:
    autostart_switch = FakeSwitch("Start with Windows")
    win = SearchWindow(backend, switches=[autostart_switch])
    [(switch, action)] = win.switch_actions
    assert action.text() == "Start with Windows" and action.isCheckable()
    win.sync_switches()
    assert not action.isChecked()
    action.trigger()  # ticks it
    assert autostart_switch.on is True and action.isChecked()
    autostart_switch.on = False  # changed outside the app
    win.sync_switches()
    assert not action.isChecked()


def test_failed_switch_says_so_and_stays_unticked(
    qapp: QApplication, backend: FakeBackend
) -> None:
    win = SearchWindow(backend, switches=[FakeSwitch("Start menu shortcut", works=False)])
    win.show_and_focus()
    [(_switch, action)] = win.switch_actions
    action.trigger()
    assert not action.isChecked()
    assert win.status_label.text() == 'Could not change "Start menu shortcut"'
    win.hide()


# --- Card buttons (owner's decisions 2026-11-19) ---


def test_card_buttons_depend_on_the_state(window: SearchWindow) -> None:
    assert card_buttons(row_of(window, 1)) == [REGION_PIN, REGION_MARK, REGION_DELETE]
    assert card_buttons(row_of(window, 3)) == [REGION_REVEAL, REGION_MARK, REGION_DELETE]
    assert card_buttons(row_of(window, 4)) == [REGION_REVEAL, REGION_MARK, REGION_DELETE]


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
    assert active.top_text.width() < still.top_text.width()


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


def test_button_delete_still_asks(qapp: QApplication, backend: FakeBackend, clock: Clock) -> None:
    win = SearchWindow(backend, now=clock, confirm_delete=lambda _parent: False)
    win.show_and_focus()
    click(win, 1, REGION_DELETE)
    assert ("delete", 1) not in backend.calls
    win.hide()


def test_button_tooltips_never_hold_the_text(window: SearchWindow) -> None:
    for item_id in (1, 3):
        row = row_of(window, item_id)
        for region in card_buttons(row):
            tip = card_tooltip(row, region)
            assert tip and FAKE_SECRET_TEXT not in tip and "lunch" not in tip


# --- Search bar and filters shown or hidden ---


def test_hiding_search_and_filters_is_remembered(
    qapp: QApplication, backend: FakeBackend, clock: Clock
) -> None:
    store = MemoryPrefs()
    win = SearchWindow(backend, now=clock, prefs=store)
    win.show_and_focus()
    win.search.setText("lunch")
    win.show_search_action.trigger()
    win.show_filters_action.trigger()
    assert store.prefs == WindowPrefs(show_search=False, show_filters=False)
    assert not win.search.isVisible() and not win.filters_row.isVisible()
    assert win.search.text() == ""  # no hidden search left filtering
    win.hide()
    again = SearchWindow(backend, now=clock, prefs=store)
    again.show_and_focus()
    assert not again.search.isVisible() and not again.filters_row.isVisible()
    assert not again.show_search_action.isChecked()
    again.show_search_action.trigger()
    assert again.search.isVisible() and store.prefs.show_search
    again.hide()
    win.deleteLater()
    again.deleteLater()


def test_typing_brings_back_a_hidden_search_bar_for_now(
    qapp: QApplication, backend: FakeBackend, clock: Clock
) -> None:
    store = MemoryPrefs(WindowPrefs(show_search=False))
    win = SearchWindow(backend, now=clock, prefs=store)
    win.show_and_focus()
    assert not win.search.isVisible()
    QTest.keyClick(win, Qt.Key.Key_Down)
    assert win.selected_row().item_id == 2
    QTest.keyClicks(win, "l")
    assert win.search.isVisible() and win.search.text() == "l"
    assert store.prefs.show_search is False  # not saved
    win.hide()
    assert not win.search.isVisibleTo(win)
    win.deleteLater()


def test_badge_click_brings_back_hidden_filters_for_now(
    qapp: QApplication, backend: FakeBackend, clock: Clock
) -> None:
    win = SearchWindow(backend, now=clock, prefs=MemoryPrefs(WindowPrefs(show_filters=False)))
    win.show_and_focus()
    click(win, 1, REGION_LABEL)
    assert win.active_filter is Label.NOTE and win.filters_row.isVisible()
    win.hide()
    win.show_and_focus()
    assert not win.filters_row.isVisible()
    win.hide()
    win.deleteLater()


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
    assert not win.clear_button.isEnabled() and not win.clear_action.isEnabled()
    win.hide()


def test_clear_all_only_clears_the_chip_shown(
    qapp: QApplication, backend: FakeBackend, clock: Clock
) -> None:
    win, asked = clear_window(backend, clock)
    win.set_filter(Label.LINK)
    win.clear_action.trigger()
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

