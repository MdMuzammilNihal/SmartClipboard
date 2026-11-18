"""Tests for the window's rules: search, badge filters, masking, reveal. Fake data only.

No Qt is needed: these test view_model.py directly.
"""

from datetime import datetime, timedelta, timezone

import pytest

from smartclip.labels import Label, SecretState
from smartclip.storage import ItemPayload, StoredItem
from smartclip.ui.view_model import (
    LABEL_BADGES,
    MASK,
    PREVIEW_CHARS,
    STATE_BADGES,
    RevealState,
    age_text,
    build_rows,
    filter_name,
    preview_text,
)

NOW = datetime(2026, 11, 18, 12, 0, tzinfo=timezone.utc)
FAKE_SECRET_TEXT = "FAKE-vm-Zx9!q7Lm-not-real"
FAKE_UNSURE_TEXT = "FAKE-vm-unsure-Qw3rty7"


def item(item_id: int, payload: ItemPayload, minutes_ago: int = 0) -> StoredItem:
    copied = NOW - timedelta(minutes=minutes_ago)
    return StoredItem(id=item_id, created_at=copied, payload=payload)


LINK = item(1, ItemPayload("https://example.test/fake-page", Label.LINK, "fakebrowser.exe"))
NOTE = item(2, ItemPayload("FAKE note: lunch at 1pm", Label.NOTE, "fakeeditor.exe"), 5)
SECRET = item(
    3,
    ItemPayload(
        FAKE_SECRET_TEXT,
        Label.POSSIBLE_SECRET,
        "fakebrowser.exe",
        secret_state=SecretState.SECRET,
        expires_at=NOW + timedelta(hours=2),
    ),
    10,
)
UNSURE = item(
    4,
    ItemPayload(
        FAKE_UNSURE_TEXT,
        Label.NOTE,
        "fakeeditor.exe",
        secret_state=SecretState.UNSURE,
        expires_at=NOW + timedelta(hours=2),
    ),
    15,
)
PINNED = item(5, ItemPayload("FAKE pinned SELECT 1", Label.SQL, pinned=True), 60 * 24 * 3)
ALL = [LINK, NOTE, SECRET, UNSURE, PINNED]


def rows(query: str = "", active=None, reveals: RevealState | None = None, now=NOW):
    return build_rows(ALL, query, active, reveals or RevealState(), now)


def ids(result) -> list[int]:
    return [row.item_id for row in result]


# --- Masking ---


def test_secret_and_unsure_items_are_masked_by_default() -> None:
    by_id = {row.item_id: row for row in rows()}
    for item_id in (SECRET.id, UNSURE.id):
        assert by_id[item_id].masked is True
        assert by_id[item_id].preview == MASK
    assert by_id[NOTE.id].masked is False
    assert by_id[NOTE.id].preview == NOTE.payload.content


def test_mask_is_the_same_length_for_every_secret() -> None:
    long_secret = item(
        9,
        ItemPayload("FAKE" * 20, Label.POSSIBLE_SECRET, secret_state=SecretState.SECRET),
    )
    result = build_rows([SECRET, long_secret], "", None, RevealState(), NOW)
    assert {row.preview for row in result} == {MASK}


def test_masked_rows_never_carry_the_content() -> None:
    for row in rows():
        if row.masked:
            assert FAKE_SECRET_TEXT not in repr(row)
            assert FAKE_UNSURE_TEXT not in repr(row)


def test_masked_rows_show_label_state_and_source() -> None:
    row = next(r for r in rows() if r.item_id == SECRET.id)
    assert row.label_badge == LABEL_BADGES[Label.POSSIBLE_SECRET]
    assert row.state_badge == STATE_BADGES[SecretState.SECRET]
    assert row.source_app == "fakebrowser.exe"
    unsure = next(r for r in rows() if r.item_id == UNSURE.id)
    assert unsure.state_badge is not None and unsure.state_badge.text == "Review?"


def test_normal_rows_have_no_state_badge() -> None:
    row = next(r for r in rows() if r.item_id == NOTE.id)
    assert row.state_badge is None


# --- Search ---


def test_empty_search_lists_everything_newest_first_as_given() -> None:
    assert ids(rows()) == [item.id for item in ALL]


def test_search_is_case_insensitive_and_matches_all_words() -> None:
    assert ids(rows("LUNCH")) == [NOTE.id]
    assert ids(rows("fake lunch")) == [NOTE.id]
    assert ids(rows("lunch dinner")) == []


def test_search_matches_source_app_of_normal_items() -> None:
    assert ids(rows("fakebrowser")) == [LINK.id]


def test_masked_items_are_never_matched_by_their_content() -> None:
    assert ids(rows(FAKE_SECRET_TEXT)) == []
    assert ids(rows("Zx9")) == []
    assert ids(rows(FAKE_UNSURE_TEXT)) == []


def test_masked_items_are_left_out_of_any_search() -> None:
    # Not even their source app or a word every item contains matches them.
    assert SECRET.id not in ids(rows("fakebrowser"))
    assert ids(rows("fake")) == [LINK.id, NOTE.id, PINNED.id]


def test_revealed_item_is_searched_like_a_normal_one() -> None:
    reveals = RevealState()
    reveals.reveal(SECRET.id, NOW)
    result = rows("Zx9", reveals=reveals)
    assert ids(result) == [SECRET.id]
    assert result[0].preview == FAKE_SECRET_TEXT
    assert result[0].revealed is True


# --- Badge filters ---


def test_label_filter_shows_only_that_type() -> None:
    assert ids(rows(active=Label.LINK)) == [LINK.id]
    assert ids(rows(active=Label.NOTE)) == [NOTE.id, UNSURE.id]


def test_state_filter_shows_masked_items_still_masked() -> None:
    result = rows(active=SecretState.UNSURE)
    assert ids(result) == [UNSURE.id]
    assert result[0].preview == MASK


def test_filter_and_search_combine() -> None:
    assert ids(rows("lunch", active=Label.LINK)) == []
    assert ids(rows("lunch", active=Label.NOTE)) == [NOTE.id]


def test_filter_names() -> None:
    assert filter_name(Label.LINK) == "Link"
    assert filter_name(SecretState.UNSURE) == "Review?"


def test_every_label_has_a_badge() -> None:
    assert set(LABEL_BADGES) == set(Label)


# --- Reveal timing ---


def test_reveal_lasts_ten_seconds_by_default() -> None:
    reveals = RevealState()
    reveals.reveal(SECRET.id, NOW)
    assert reveals.is_revealed(SECRET.id, NOW + timedelta(seconds=9))
    assert not reveals.is_revealed(SECRET.id, NOW + timedelta(seconds=10))
    later = rows(reveals=reveals, now=NOW + timedelta(seconds=10))
    assert next(r for r in later if r.item_id == SECRET.id).preview == MASK


def test_expire_forgets_finished_reveals_only() -> None:
    reveals = RevealState(duration=timedelta(seconds=10))
    reveals.reveal(SECRET.id, NOW)
    reveals.reveal(UNSURE.id, NOW + timedelta(seconds=5))
    assert reveals.expire(NOW + timedelta(seconds=10)) == [SECRET.id]
    assert reveals.is_revealed(UNSURE.id, NOW + timedelta(seconds=10))


def test_hide_and_hide_all() -> None:
    reveals = RevealState()
    reveals.reveal(SECRET.id, NOW)
    reveals.reveal(UNSURE.id, NOW)
    reveals.hide(SECRET.id)
    assert not reveals.is_revealed(SECRET.id, NOW)
    reveals.hide_all()
    assert not reveals.any_revealed()


def test_revealing_a_normal_item_changes_nothing() -> None:
    reveals = RevealState()
    reveals.reveal(NOTE.id, NOW)
    row = next(r for r in rows(reveals=reveals) if r.item_id == NOTE.id)
    assert row.revealed is False and row.masked is False


# --- Pins, previews, ages ---


def test_only_normal_items_can_be_pinned() -> None:
    can_pin = {row.item_id: row.can_pin for row in rows()}
    assert can_pin == {LINK.id: True, NOTE.id: True, SECRET.id: False, UNSURE.id: False, PINNED.id: True}
    assert next(r for r in rows() if r.item_id == PINNED.id).pinned is True


def test_preview_is_one_line_and_cut() -> None:
    assert preview_text("FAKE line one\n\tline   two\r\n") == "FAKE line one line two"
    cut = preview_text("x" * (PREVIEW_CHARS + 50))
    assert len(cut) == PREVIEW_CHARS and cut.endswith("…")


@pytest.mark.parametrize(
    ("age", "text"),
    [
        (timedelta(seconds=-5), "now"),
        (timedelta(seconds=59), "now"),
        (timedelta(minutes=5), "5m"),
        (timedelta(hours=3, minutes=59), "3h"),
        (timedelta(days=2, hours=1), "2d"),
    ],
)
def test_age_text(age: timedelta, text: str) -> None:
    assert age_text(age) == text


def test_age_counts_from_last_copy() -> None:
    recopied = StoredItem(
        id=7,
        created_at=NOW - timedelta(days=10),
        payload=ItemPayload("FAKE recopied", Label.NOTE, last_copied_at=NOW - timedelta(minutes=2)),
    )
    assert build_rows([recopied], "", None, RevealState(), NOW)[0].age == "2m"
