"""Day headers and the Pinned section (UI plan item 2). Fake data only, no Qt."""

from datetime import datetime, timedelta, timezone, tzinfo

import pytest

from smartclip.labels import Label
from smartclip.storage import ItemPayload, StoredItem
from smartclip.ui.view_model import (
    GROUP_OLDER,
    GROUP_PINNED,
    GROUP_RESULTS,
    GROUP_THIS_WEEK,
    GROUP_TODAY,
    GROUP_YESTERDAY,
    Header,
    RevealState,
    Row,
    arrange,
    build_rows,
    day_group,
)

UTC = timezone.utc
IST = timezone(timedelta(hours=5, minutes=30))  # a fixed offset with a half hour


class SpringForward(tzinfo):
    """A made-up zone: UTC-5 until `change`, then UTC-4 (a one-hour clock jump)."""

    def __init__(self, change: datetime) -> None:
        self.change = change

    def utcoffset(self, dt: datetime | None) -> timedelta:
        if dt is None:
            return timedelta(hours=-5)
        naive_utc = dt.replace(tzinfo=None)
        return timedelta(hours=-5) if naive_utc < self.change.replace(tzinfo=None) else timedelta(hours=-4)

    def dst(self, dt: datetime | None) -> timedelta:
        return timedelta(0)

    def tzname(self, dt: datetime | None) -> str:
        return "FAKE"

    def fromutc(self, dt: datetime) -> datetime:
        cutoff = self.change.replace(tzinfo=None)
        offset = timedelta(hours=-5) if dt.replace(tzinfo=None) < cutoff else timedelta(hours=-4)
        return (dt + offset).replace(tzinfo=self)


def row(item_id: int, copied_at: datetime, pinned: bool = False) -> Row:
    item = StoredItem(item_id, copied_at, ItemPayload(f"FAKE {item_id}", Label.NOTE, pinned=pinned))
    [made] = build_rows([item], "", None, RevealState(), copied_at)
    return made


def names(entries: list) -> list[str]:
    return [e.text if isinstance(e, Header) else f"#{e.item_id}" for e in entries]


NOW = datetime(2026, 11, 24, 12, 0, tzinfo=UTC)


def test_rows_remember_when_they_were_last_copied() -> None:
    made = row(1, NOW - timedelta(hours=3))
    assert made.copied_at == NOW - timedelta(hours=3)


def test_groups_in_order_with_pinned_first() -> None:
    rows = [
        row(1, NOW - timedelta(minutes=5)),
        row(2, NOW - timedelta(days=1)),
        row(3, NOW - timedelta(days=3)),
        row(4, NOW - timedelta(days=30)),
        row(5, NOW - timedelta(days=60), pinned=True),
    ]
    assert names(arrange(rows, NOW, False, UTC)) == [
        GROUP_PINNED, "#5",
        GROUP_TODAY, "#1",
        GROUP_YESTERDAY, "#2",
        GROUP_THIS_WEEK, "#3",
        GROUP_OLDER, "#4",
    ]


def test_a_pinned_item_is_listed_once_only_under_pinned() -> None:
    rows = [row(1, NOW - timedelta(minutes=1), pinned=True)]
    assert names(arrange(rows, NOW, False, UTC)) == [GROUP_PINNED, "#1"]


def test_empty_groups_have_no_header() -> None:
    assert names(arrange([row(1, NOW)], NOW, False, UTC)) == [GROUP_TODAY, "#1"]
    assert arrange([], NOW, False, UTC) == []
    assert arrange([], NOW, True, UTC) == []


def test_searching_gives_one_flat_results_list() -> None:
    rows = [row(1, NOW), row(2, NOW - timedelta(days=9)), row(3, NOW, pinned=True)]
    assert names(arrange(rows, NOW, True, UTC)) == [GROUP_RESULTS, "#1", "#2", "#3"]


def test_order_inside_a_group_is_kept() -> None:
    rows = [row(i, NOW - timedelta(minutes=i)) for i in (1, 2, 3)]
    assert names(arrange(rows, NOW, False, UTC)) == [GROUP_TODAY, "#1", "#2", "#3"]


# --- Midnight boundaries ---


def test_one_second_either_side_of_local_midnight() -> None:
    now = datetime(2026, 11, 24, 10, 0, tzinfo=UTC)
    today = now.astimezone(UTC).date()
    midnight = datetime(2026, 11, 24, 0, 0, tzinfo=UTC)
    assert day_group(midnight, today, UTC) == GROUP_TODAY
    assert day_group(midnight - timedelta(seconds=1), today, UTC) == GROUP_YESTERDAY


def test_the_day_follows_local_midnight_not_utc() -> None:
    # 20:00 UTC on the 23rd is 01:30 on the 24th in IST: today, for an IST user.
    now = datetime(2026, 11, 24, 6, 0, tzinfo=UTC)  # 11:30 IST on the 24th
    copy = datetime(2026, 11, 23, 20, 0, tzinfo=UTC)
    assert day_group(copy, now.astimezone(IST).date(), IST) == GROUP_TODAY
    assert day_group(copy, now.astimezone(UTC).date(), UTC) == GROUP_YESTERDAY


def test_week_edges() -> None:
    today = NOW.date()
    base = datetime(2026, 11, 24, 12, 0, tzinfo=UTC)
    assert day_group(base - timedelta(days=2), today, UTC) == GROUP_THIS_WEEK
    assert day_group(base - timedelta(days=6), today, UTC) == GROUP_THIS_WEEK
    assert day_group(base - timedelta(days=7), today, UTC) == GROUP_OLDER


def test_a_copy_from_the_future_counts_as_today() -> None:
    assert day_group(NOW + timedelta(days=2), NOW.date(), UTC) == GROUP_TODAY


def test_clock_change_day_still_groups_by_calendar_date() -> None:
    # The day has 23 hours: the clock jumps from 02:00 to 03:00 local on the 8th.
    change = datetime(2026, 3, 8, 7, 0)  # 02:00 at UTC-5, as a naive UTC time
    zone = SpringForward(change)
    now = datetime(2026, 3, 9, 3, 0, tzinfo=UTC)  # 23:00 on the 8th, local
    assert now.astimezone(zone).date().day == 8
    just_after_midnight_on_8th = datetime(2026, 3, 8, 5, 30, tzinfo=UTC)  # 00:30 local
    late_on_7th = datetime(2026, 3, 8, 4, 30, tzinfo=UTC)  # 23:30 local on the 7th
    today = now.astimezone(zone).date()
    assert day_group(just_after_midnight_on_8th, today, zone) == GROUP_TODAY
    assert day_group(late_on_7th, today, zone) == GROUP_YESTERDAY
    # 24 hours earlier is not "yesterday" by the clock span rule; the date rule
    # puts a 22:30-on-the-7th copy (27 UTC hours before now) under yesterday.
    assert day_group(datetime(2026, 3, 8, 3, 30, tzinfo=UTC), today, zone) == GROUP_YESTERDAY


@pytest.mark.parametrize("tz", [UTC, IST])
def test_arrange_uses_the_given_zone_for_today(tz: tzinfo) -> None:
    now = datetime(2026, 11, 24, 20, 0, tzinfo=UTC)  # 01:30 on the 25th in IST
    copy = datetime(2026, 11, 24, 12, 0, tzinfo=UTC)  # 17:30 on the 24th in IST
    entries = arrange([row(1, copy)], now, False, tz)
    expected = GROUP_TODAY if tz is UTC else GROUP_YESTERDAY
    assert names(entries) == [expected, "#1"]


def test_headers_are_fixed_words_never_item_text() -> None:
    entries = arrange([row(1, NOW)], NOW, False, UTC)
    header = entries[0]
    assert isinstance(header, Header)
    assert "FAKE" not in header.text
