"""Names and tags in the window: menu, prompts, cards, tag chips, search (UI plan item 9). Fake data only."""

import os
from collections.abc import Iterator
from dataclasses import replace
from datetime import timedelta

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QRect  # noqa: E402
from PySide6.QtGui import QFontMetrics  # noqa: E402
from PySide6.QtWidgets import QApplication, QMenu  # noqa: E402

from smartclip import naming  # noqa: E402
from smartclip.labels import Label, SecretState  # noqa: E402
from smartclip.storage import ItemPayload, StoredItem  # noqa: E402
from smartclip.ui import theme  # noqa: E402
from smartclip.ui.view_model import RevealState, TagFilter, build_rows, filter_name  # noqa: E402
from smartclip.ui.window import (  # noqa: E402
    _GAP,
    NAMING_NOTE_PIN,
    NAMING_NOTE_SECRET,
    TAG_PREFIX,
    SearchWindow,
    card_height,
    card_rects,
    card_tooltip,
    present_filters,
    region_at,
    tag_chip_rects,
)
from test_window import FAKE_SECRET_TEXT, START, Clock, FakeBackend  # noqa: E402

TITLE = "FAKE-Standup-link"


class Answers:
    """Stands in for the text question: hands back queued answers and remembers the questions."""

    def __init__(self, *answers: str | None) -> None:
        self.queue = list(answers)
        self.asked: list[tuple[str, str, str, int]] = []

    def __call__(self, _parent: object, title: str, label: str, initial: str, max_length: int) -> str | None:
        self.asked.append((title, label, initial, max_length))
        return self.queue.pop(0) if self.queue else None


@pytest.fixture
def qapp() -> QApplication:
    return QApplication.instance() or QApplication([])


@pytest.fixture
def backend() -> FakeBackend:
    made = FakeBackend()
    made.pin(1)  # a pinned normal note
    made.pin(2)  # a pinned link
    return made


def make_window(backend: FakeBackend, answers: Answers) -> SearchWindow:
    win = SearchWindow(backend, now=Clock(), ask_text=answers, current_theme=lambda: theme.LIGHT)
    win.show_and_focus()
    return win


@pytest.fixture
def window(qapp: QApplication, backend: FakeBackend) -> Iterator[SearchWindow]:
    win = make_window(backend, Answers())
    yield win
    win.hide()
    win.deleteLater()


def row_of(window: SearchWindow, item_id: int):  # noqa: ANN201
    return next(r for r in window.model.rows if r.item_id == item_id)


def menu_texts(menu: QMenu) -> list[str]:
    return [a.text() for a in menu.actions() if a.text()]


# --- Rows and the menu ---


def test_only_pinned_normal_items_can_be_named(window: SearchWindow) -> None:
    assert row_of(window, 1).can_name and row_of(window, 2).can_name
    assert not row_of(window, 3).can_name  # a secret
    assert not row_of(window, 4).can_name  # a Review? item


def test_a_pinned_card_offers_a_name_and_tags(window: SearchWindow) -> None:
    texts = menu_texts(window.build_menu(row_of(window, 1)))
    assert "Add a name..." in texts and "Add tag..." in texts
    assert "Remove name" not in texts and "Remove tag" not in texts


def test_a_named_card_offers_rename_remove_and_remove_tag(window: SearchWindow, backend: FakeBackend) -> None:
    backend.rename(1, TITLE)
    backend.set_tags(1, ("work", "daily"))
    window.refresh()
    menu = window.build_menu(row_of(window, 1))
    assert {"Rename...", "Remove name", "Add tag...", "Remove tag"} <= set(menu_texts(menu))
    submenu = next(a.menu() for a in menu.actions() if a.text() == "Remove tag")
    assert menu_texts(submenu) == ["work", "daily"]


def test_adding_tags_stops_at_three(window: SearchWindow, backend: FakeBackend) -> None:
    backend.set_tags(1, ("a", "b", "c"))
    window.refresh()
    add = next(a for a in window.build_menu(row_of(window, 1)).actions() if a.text() == "Add tag...")
    assert not add.isEnabled()


def test_secret_cards_explain_why_they_cannot_be_named(window: SearchWindow) -> None:
    for item_id in (3, 4):
        menu = window.build_menu(row_of(window, item_id))
        note = next(a for a in menu.actions() if a.text() == NAMING_NOTE_SECRET)
        assert not note.isEnabled() and "reveal" in note.toolTip()
        assert "Add a name..." not in menu_texts(menu)


def test_unpinned_cards_say_to_pin_first(qapp: QApplication) -> None:
    plain = FakeBackend()  # nothing pinned
    win = make_window(plain, Answers())
    menu = win.build_menu(row_of(win, 1))
    note = next(a for a in menu.actions() if a.text() == NAMING_NOTE_PIN)
    assert not note.isEnabled() and "Add a name..." not in menu_texts(menu)
    win.hide()


# --- Prompts and saving ---


def test_naming_an_item_saves_and_shows_it(qapp: QApplication, backend: FakeBackend) -> None:
    answers = Answers(f"  {TITLE}  ")
    win = make_window(backend, answers)
    win.rename(row_of(win, 1))
    assert backend.store[1].payload.title == TITLE
    assert row_of(win, 1).title == TITLE
    assert answers.asked == [("Name this item", "Name:", "", naming.MAX_TITLE_CHARS)]
    win.hide()


def test_cancelling_the_question_changes_nothing(qapp: QApplication, backend: FakeBackend) -> None:
    win = make_window(backend, Answers(None))
    win.rename(row_of(win, 1))
    win.add_tag(row_of(win, 1))
    assert not any(call[0] in ("rename", "set_tags") for call in backend.calls)
    win.hide()


def test_renaming_starts_from_the_current_name_and_empty_removes_it(
    qapp: QApplication, backend: FakeBackend
) -> None:
    backend.rename(1, TITLE)
    answers = Answers("   ")
    win = make_window(backend, answers)
    win.rename(row_of(win, 1))
    assert answers.asked[0][2] == TITLE
    assert backend.store[1].payload.title is None
    win.hide()


def test_remove_name_and_remove_tag(qapp: QApplication, backend: FakeBackend) -> None:
    backend.rename(1, TITLE)
    backend.set_tags(1, ("one", "two"))
    win = make_window(backend, Answers())
    win.remove_name(row_of(win, 1))
    win.remove_tag(row_of(win, 1), "ONE")
    assert backend.store[1].payload.title is None and backend.store[1].payload.tags == ("two",)
    win.hide()


def test_tags_are_added_one_at_a_time_without_repeats(qapp: QApplication, backend: FakeBackend) -> None:
    answers = Answers("Work", "work", "  ", "home")
    win = make_window(backend, answers)
    for _ in range(4):
        win.add_tag(row_of(win, 1))
    assert backend.store[1].payload.tags == ("Work", "home")
    assert answers.asked[0][3] == naming.MAX_TAG_CHARS
    win.hide()


def test_naming_is_refused_for_items_that_cannot_have_names(qapp: QApplication, backend: FakeBackend) -> None:
    answers = Answers(TITLE, TITLE)
    win = make_window(backend, answers)
    win.rename(row_of(win, 3))  # a secret
    win.add_tag(row_of(win, 3))
    assert answers.asked == [] and not any(c[0] in ("rename", "set_tags") for c in backend.calls)
    win.hide()


def test_marking_a_named_item_secret_removes_its_names(qapp: QApplication, backend: FakeBackend) -> None:
    backend.rename(1, TITLE)
    backend.set_tags(1, ("work",))
    win = make_window(backend, Answers())
    win.mark_secret(row_of(win, 1))
    payload = backend.store[1].payload
    assert payload.secret_state is SecretState.SECRET and payload.title is None and payload.tags == ()
    win.hide()


# --- Tag chips and search ---


def test_tag_chips_come_after_the_type_chips(qapp: QApplication, backend: FakeBackend) -> None:
    backend.set_tags(1, ("work",))
    backend.set_tags(2, ("Work", "daily"))
    filters = present_filters(list(backend.store.values()))
    tags = [f for f in filters if isinstance(f, TagFilter)]
    assert tags == [TagFilter("daily"), TagFilter("work")]  # one chip per tag, ignoring case
    first_tag = filters.index(tags[0])
    assert all(isinstance(f, Label) or isinstance(f, SecretState) for f in filters[:first_tag])
    assert filter_name(TagFilter("work")) == "#work"


def test_the_window_shows_a_chip_for_each_tag_and_filters_by_it(window: SearchWindow, backend: FakeBackend) -> None:
    backend.set_tags(1, ("work",))
    window.refresh()
    assert TagFilter("work") in window.chips
    window.set_filter(TagFilter("work"))
    assert [r.item_id for r in window.model.rows] == [1]
    window.set_filter(None)
    assert len(window.model.rows) > 1


def test_clicking_a_tag_on_a_card_filters_by_it(window: SearchWindow, backend: FakeBackend) -> None:
    backend.set_tags(2, ("Links",))
    window.refresh()
    window.handle_region(row_of(window, 2), TAG_PREFIX + "Links")
    assert window.active_filter == TagFilter("Links")
    assert [r.item_id for r in window.model.rows] == [2]


def test_tag_filter_ignores_case() -> None:
    assert TagFilter("Work") == TagFilter("wORK") and hash(TagFilter("Work")) == hash(TagFilter("work"))
    assert TagFilter("a") != TagFilter("b") and TagFilter("a") != "a"


def test_search_finds_items_by_title_and_tag() -> None:
    now = START
    named = StoredItem(
        1, now, ItemPayload("FAKE plain body", Label.NOTE, pinned=True, title="Weekly Standup", tags=("Team",))
    )
    other = StoredItem(2, now - timedelta(minutes=1), ItemPayload("FAKE other body", Label.NOTE))
    for query, expected in (("standup", [1]), ("team", [1]), ("weekly team", [1]), ("body", [1, 2]), ("nothing", [])):
        rows = build_rows([named, other], query, None, RevealState(), now)
        assert [r.item_id for r in rows] == expected, query


def test_secrets_are_still_never_found_by_search() -> None:
    secret = StoredItem(
        3, START,
        ItemPayload(FAKE_SECRET_TEXT, Label.POSSIBLE_SECRET, secret_state=SecretState.SECRET,
                    expires_at=START + timedelta(hours=1)),
    )
    assert build_rows([secret], "fake", None, RevealState(), START) == []
    assert build_rows([secret], "", None, RevealState(), START)[0].title is None


# --- Drawing ---


def test_a_named_card_is_taller_and_its_chips_sit_inside_the_card(window: SearchWindow, backend: FakeBackend) -> None:
    plain = card_height(row_of(window, 1), window.font(), 380)
    backend.rename(1, TITLE)
    backend.set_tags(1, ("work", "daily", "x"))
    window.refresh()
    row = row_of(window, 1)
    metrics = QFontMetrics(window.font())
    # (a short card is held at the height "..." with Pin under it needs, so only taller is certain)
    assert card_height(row, window.font(), 380) > plain
    rects = card_rects(QRect(0, 0, 380, card_height(row, window.font(), 380)), row, metrics)
    assert rects.meta is not None and [t for t, _r in rects.tags] == ["work", "daily", "x"]
    for tag, chip in rects.tags:
        assert rects.card.contains(chip) and rects.meta.contains(chip)
        assert region_at(rects, chip.center()) == TAG_PREFIX + tag
        assert card_tooltip(row, TAG_PREFIX + tag) == f"Show only items tagged {tag}"
    assert rects.body.top() > rects.meta.bottom()  # the text starts under the title line


def test_cards_without_names_have_no_meta_line(window: SearchWindow) -> None:
    rects = card_rects(QRect(0, 0, 380, 120), row_of(window, 1), QFontMetrics(window.font()))
    assert rects.meta is None and rects.tags == ()


def test_chips_make_way_for_a_long_title(qapp: QApplication) -> None:
    row = build_rows(
        [StoredItem(1, START, ItemPayload("FAKE", Label.NOTE, pinned=True, title="t" * 60,
                                          tags=("aaaaaaaaaaaaaaaaaaaaaaaa", "bbbbbbbbbbbbbbbbbbbbbbbb", "c")))],
        "", None, RevealState(), START,
    )[0]
    metrics = QFontMetrics(QApplication.font())
    meta = QRect(0, 0, 200, metrics.lineSpacing())
    chips = tag_chip_rects(row, metrics, meta)
    assert len(chips) < 3 and all(meta.contains(c) for _t, c in chips)
    assert tag_chip_rects(replace(row, title=None), metrics, QRect(0, 0, 2000, 20))  # room: all kept


def test_named_cards_draw_in_both_themes(qapp: QApplication, backend: FakeBackend) -> None:
    backend.rename(1, naming.clean_title("A rather long friendly name that will need to be cut short to fit"))
    backend.set_tags(1, ("one", "two", "three"))
    backend.set_tags(2, ("links",))
    for t in (theme.LIGHT, theme.DARK):
        win = SearchWindow(backend, now=Clock(), current_theme=lambda t=t: t)
        win.show_and_focus()
        qapp.processEvents()
        assert not win.grab().toImage().isNull()
        win.hide()
