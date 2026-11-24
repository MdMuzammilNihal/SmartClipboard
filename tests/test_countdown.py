"""The secret expiry countdown pill (UI plan item 7). Fake data only."""

import os
from collections.abc import Iterator
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtGui import QColor  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from smartclip.labels import Label, SecretState  # noqa: E402
from smartclip.retention import RetentionPolicy  # noqa: E402
from smartclip.storage import ItemPayload, StoredItem  # noqa: E402
from smartclip.ui import theme  # noqa: E402
from smartclip.ui.view_model import (  # noqa: E402
    COUNTDOWN_DANGER,
    COUNTDOWN_WARNING,
    RevealState,
    build_rows,
    countdown_level,
    countdown_text,
)
from test_theme import contrast  # noqa: E402
from test_window import FAKE_SECRET_TEXT, START, Clock, FakeBackend  # noqa: E402
from smartclip.ui.window import SearchWindow  # noqa: E402

NOW = datetime(2026, 11, 25, 12, 0, tzinfo=timezone.utc)
M = timedelta(minutes=1)


def secret(item_id: int, minutes_left: float | None, state: SecretState = SecretState.SECRET) -> StoredItem:
    expires = None if minutes_left is None else NOW + timedelta(minutes=minutes_left)
    payload = ItemPayload(
        f"FAKE-countdown-{item_id}", Label.POSSIBLE_SECRET, secret_state=state, expires_at=expires
    )
    return StoredItem(item_id, NOW - timedelta(minutes=10), payload)


def rows_for(items: list[StoredItem], policy: RetentionPolicy | None = None):  # noqa: ANN201
    return build_rows(items, "", None, RevealState(), NOW, policy)


# --- Text and colours ---


@pytest.mark.parametrize(
    "remaining, expected",
    [
        (timedelta(hours=2), "2h 0m left"),
        (timedelta(hours=1, minutes=42, seconds=30), "1h 42m left"),
        (timedelta(minutes=59, seconds=59), "59m left"),
        (timedelta(minutes=1), "1m left"),
        (timedelta(seconds=59), "under 1 min left"),
        (timedelta(seconds=1), "under 1 min left"),
        (timedelta(0), "under 1 min left"),
        (timedelta(seconds=-5), "under 1 min left"),
    ],
)
def test_countdown_text(remaining: timedelta, expected: str) -> None:
    assert countdown_text(remaining) == expected


def test_countdown_level_thresholds() -> None:
    assert countdown_level(timedelta(hours=1)) == "normal"
    assert countdown_level(COUNTDOWN_WARNING) == "normal"
    assert countdown_level(COUNTDOWN_WARNING - timedelta(seconds=1)) == "warning"
    assert countdown_level(COUNTDOWN_DANGER) == "warning"
    assert countdown_level(COUNTDOWN_DANGER - timedelta(seconds=1)) == "danger"
    assert COUNTDOWN_WARNING == timedelta(minutes=15) and COUNTDOWN_DANGER == timedelta(minutes=2)


@pytest.mark.parametrize("t", [theme.LIGHT, theme.DARK], ids=["light", "dark"])
def test_pill_text_is_readable_on_its_background(t: theme.Theme) -> None:
    for level, background in (("warning", t.tint3), ("danger", t.banner_background), ("normal", t.card)):
        fill, ink = theme.pill_colours(t, level)
        assert (fill or t.card) == background
        assert contrast(ink, background) >= 4.5


def test_normal_pill_has_no_fill_and_levels_differ() -> None:
    assert theme.pill_colours(theme.LIGHT, "normal")[0] is None
    colours = {theme.pill_colours(theme.LIGHT, level) for level in ("normal", "warning", "danger")}
    assert len(colours) == 3


# --- Rows ---


def test_secret_and_unsure_rows_carry_time_left_and_normal_rows_do_not() -> None:
    normal = StoredItem(9, NOW, ItemPayload("FAKE normal", Label.NOTE))
    rows = rows_for([secret(1, 90), secret(2, 30, SecretState.UNSURE), normal])
    assert [r.remaining for r in rows] == [timedelta(minutes=90), timedelta(minutes=30), None]


def test_an_expired_secret_is_not_listed() -> None:
    rows = rows_for([secret(1, 0), secret(2, -1), secret(3, 1)])
    assert [r.item_id for r in rows] == [3]


def test_old_rows_without_a_deadline_use_the_retention_length() -> None:
    [row] = rows_for([secret(1, None)])  # created 10 minutes ago, default 2 hours
    assert row.remaining == timedelta(hours=1, minutes=50)
    [short] = rows_for([secret(1, None)], RetentionPolicy(secret=timedelta(minutes=30)))
    assert short.remaining == timedelta(minutes=20)


def test_the_pill_text_never_holds_item_text() -> None:
    [row] = rows_for([secret(1, 42)])
    assert row.remaining is not None
    text = countdown_text(row.remaining)
    assert "FAKE" not in text and text == "42m left"


# --- In the window ---


@pytest.fixture
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


def set_expiry(backend: FakeBackend, item_id: int, minutes_left: float) -> None:
    item = backend.store[item_id]
    backend.store[item_id] = replace(
        item, payload=replace(item.payload, expires_at=START + timedelta(minutes=minutes_left))
    )


def test_secret_rows_in_the_window_show_time_left(window: SearchWindow) -> None:
    by_id = {row.item_id: row for row in window.model.rows}
    assert by_id[3].remaining is not None and by_id[4].remaining is not None
    assert by_id[1].remaining is None and by_id[2].remaining is None


def test_next_expiry_is_the_earliest_secret_on_screen(
    window: SearchWindow, backend: FakeBackend
) -> None:
    set_expiry(backend, 3, 40)
    set_expiry(backend, 4, 25)
    window.refresh()
    assert window._next_expiry == START + timedelta(minutes=25)


def test_there_is_nothing_to_watch_when_no_secret_is_shown(window: SearchWindow) -> None:
    window.search.setText("lunch")  # secrets are left out while searching
    assert window._next_expiry is None
    window.search.setText("")
    assert window._next_expiry is not None


def test_a_secret_leaves_the_list_when_its_time_is_up(
    window: SearchWindow, backend: FakeBackend, clock: Clock
) -> None:
    set_expiry(backend, 3, 5)
    window.refresh()
    assert 3 in [r.item_id for r in window.model.rows]
    clock.now = START + timedelta(minutes=5)  # not a 30-second boundary: _tick alone must notice
    window._tick()
    assert 3 not in [r.item_id for r in window.model.rows]


def test_pills_follow_the_clock_every_30_seconds(
    window: SearchWindow, backend: FakeBackend, clock: Clock
) -> None:
    set_expiry(backend, 3, 60)
    window.refresh()
    before = next(r.remaining for r in window.model.rows if r.item_id == 3)
    clock.now += timedelta(minutes=10)
    for _ in range(30):
        window._tick()
    after = next(r.remaining for r in window.model.rows if r.item_id == 3)
    assert before is not None and after == before - timedelta(minutes=10)


def test_the_countdown_never_goes_up_if_the_clock_is_set_back(
    window: SearchWindow, backend: FakeBackend, clock: Clock
) -> None:
    set_expiry(backend, 3, 60)
    window.refresh()
    first = next(r.remaining for r in window.model.rows if r.item_id == 3)
    clock.now -= timedelta(hours=3)  # the clock jumps back
    window.refresh()
    again = next(r.remaining for r in window.model.rows if r.item_id == 3)
    assert first is not None and again is not None and again <= first


def test_seen_times_are_forgotten_for_items_no_longer_shown(
    window: SearchWindow, backend: FakeBackend
) -> None:
    window.set_filter(Label.LINK)
    assert window._remaining_seen == {}


def test_a_warning_pill_is_drawn_in_amber_and_stays_on_a_selected_card(
    qapp: QApplication, backend: FakeBackend, clock: Clock
) -> None:
    set_expiry(backend, 3, 10)  # under 15 minutes: amber
    win = SearchWindow(backend, now=clock, current_theme=lambda: theme.LIGHT)
    win.show_and_focus()
    amber = QColor(theme.LIGHT.tint3).rgb()

    def amber_pixels() -> int:
        image = win.grab().toImage()
        return sum(
            1 for x in range(image.width()) for y in range(image.height())
            if image.pixelColor(x, y).rgb() == amber
        )

    assert amber_pixels() > 20  # the pill is on screen
    win.select_item(3)  # a selected card shows buttons AND keeps its pill
    qapp.processEvents()
    assert amber_pixels() > 20
    win.hide()


def test_the_window_never_draws_secret_text_in_the_pill(window: SearchWindow) -> None:
    for row in window.model.rows:
        assert FAKE_SECRET_TEXT not in str(row.remaining)


# --- What fits in the top row ---


def make_row(minutes_left: float | None):  # noqa: ANN201
    [row] = rows_for([secret(1, minutes_left)]) if minutes_left is not None else rows_for(
        [StoredItem(9, NOW, ItemPayload("FAKE normal", Label.NOTE))]
    )
    return row


def test_top_row_keeps_everything_when_there_is_room(qapp: QApplication) -> None:
    from PySide6.QtGui import QFontMetrics

    from smartclip.ui.window import top_row_extras

    metrics = QFontMetrics(qapp.font())
    assert top_row_extras(make_row(90), 2, False, metrics, 400) == ("1h 30m left", True)


def test_top_row_shortens_the_pill_then_drops_the_key_cap(qapp: QApplication) -> None:
    from PySide6.QtGui import QFontMetrics

    from smartclip.ui.window import MIN_SOURCE_WIDTH, top_row_extras

    metrics = QFontMetrics(qapp.font())
    cap = metrics.height() + 8
    short_room = MIN_SOURCE_WIDTH + metrics.horizontalAdvance("9m") + 20 + cap  # just enough
    assert top_row_extras(make_row(9), 3, False, metrics, short_room) == ("9m", True)
    assert top_row_extras(make_row(9), 3, False, metrics, short_room + 200) == ("9m left", True)
    tight = MIN_SOURCE_WIDTH + metrics.horizontalAdvance("9m") + 20 + 1
    assert top_row_extras(make_row(9), 3, False, metrics, tight) == ("9m", False)
    assert top_row_extras(make_row(9), 3, False, metrics, 10) == ("9m", False)  # the pill still wins


def test_an_active_card_keeps_its_pill_but_loses_the_key_cap(qapp: QApplication) -> None:
    from PySide6.QtGui import QFontMetrics

    from smartclip.ui.window import top_row_extras

    metrics = QFontMetrics(qapp.font())
    assert top_row_extras(make_row(9), 3, True, metrics, 400) == ("9m left", False)


def test_a_normal_card_gets_a_key_cap_only_when_numbered_and_roomy(qapp: QApplication) -> None:
    from PySide6.QtGui import QFontMetrics

    from smartclip.ui.window import top_row_extras

    metrics = QFontMetrics(qapp.font())
    assert top_row_extras(make_row(None), 4, False, metrics, 400) == (None, True)
    assert top_row_extras(make_row(None), None, False, metrics, 400) == (None, False)
    assert top_row_extras(make_row(None), 4, False, metrics, 20) == (None, False)
