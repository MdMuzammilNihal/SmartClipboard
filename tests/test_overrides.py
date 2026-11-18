"""Tests for manual overrides. All content is fake."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone

from smartclip import config
from smartclip.labels import Label, SecretState
from smartclip.overrides import LastCapture, mark_normal, mark_secret
from smartclip.storage import ItemPayload

NOW = datetime(2026, 11, 18, 12, 0, tzinfo=timezone.utc)
NORMAL = ItemPayload(
    content="FAKE-override-note",
    label=Label.NOTE,
    source_app="fakeeditor.exe",
    pinned=True,
    expires_at=NOW + timedelta(days=90),
)


# --- Marking secret ---


def test_mark_secret_sets_state_short_expiry_and_unpins() -> None:
    marked = mark_secret(NORMAL, NOW)
    assert marked.secret_state is SecretState.SECRET
    assert marked.expires_at == NOW + config.SECRET_RETENTION
    assert marked.pinned is False


def test_mark_secret_keeps_everything_else() -> None:
    marked = mark_secret(NORMAL, NOW)
    assert (marked.content, marked.label, marked.source_app, marked.kind) == (
        NORMAL.content,
        NORMAL.label,
        NORMAL.source_app,
        NORMAL.kind,
    )


def test_unsure_items_become_secret() -> None:
    unsure = replace(NORMAL, secret_state=SecretState.UNSURE, pinned=False)
    assert mark_secret(unsure, NOW).secret_state is SecretState.SECRET


def test_marking_twice_never_extends_a_secret() -> None:
    once = mark_secret(NORMAL, NOW)
    twice = mark_secret(once, NOW + timedelta(hours=1))
    assert twice == once


def test_already_secret_item_is_left_alone() -> None:
    secret = replace(
        NORMAL, secret_state=SecretState.SECRET, pinned=False, expires_at=NOW
    )
    assert mark_secret(secret, NOW + timedelta(minutes=5)) is secret


def test_marked_item_round_trips_through_the_payload_format() -> None:
    marked = mark_secret(NORMAL, NOW)
    assert ItemPayload.from_bytes(marked.to_bytes()) == marked


# --- Which item is "the last item" ---


def test_nothing_recorded_means_nothing_to_mark() -> None:
    assert LastCapture().item_for(5) is None


def test_latest_item_is_found_while_the_clipboard_still_holds_it() -> None:
    last = LastCapture()
    last.record(item_id=7, sequence_number=40)
    assert last.item_for(40) == 7


def test_mark_is_refused_after_the_clipboard_changes() -> None:
    # A newer copy (for example one the gate skipped) must not let the
    # hotkey mark the older item by mistake.
    last = LastCapture()
    last.record(item_id=7, sequence_number=40)
    assert last.item_for(45) is None


def test_newer_record_replaces_the_older_one() -> None:
    last = LastCapture()
    last.record(item_id=7, sequence_number=40)
    last.record(item_id=8, sequence_number=45)
    assert last.item_for(40) is None
    assert last.item_for(45) == 8


def test_forgetting_a_deleted_item() -> None:
    last = LastCapture()
    last.record(item_id=7, sequence_number=40)
    last.forget(6)
    assert last.item_for(40) == 7
    last.forget(7)
    assert last.item_for(40) is None


# --- Marking normal (from the window) ---


def test_mark_normal_clears_state_and_expiry() -> None:
    secret = mark_secret(NORMAL, NOW)
    marked = mark_normal(secret)
    assert marked.secret_state is SecretState.NORMAL
    assert marked.expires_at is None
    assert marked.pinned is False
    assert marked.label is Label.NOTE


def test_mark_normal_relabels_possible_secret_by_content() -> None:
    unsure = ItemPayload(
        content="https://example.test/fake-override",
        label=Label.POSSIBLE_SECRET,
        secret_state=SecretState.UNSURE,
        expires_at=NOW,
    )
    assert mark_normal(unsure).label is Label.LINK


def test_mark_normal_of_normal_item_changes_nothing() -> None:
    assert mark_normal(NORMAL) is NORMAL
