"""Tests for the retention, merge, and pin rules. All content is fake."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from smartclip import retention
from smartclip.labels import Label, SecretState
from smartclip.retention import PinRefused, RetentionPolicy
from smartclip.storage import ItemPayload, StoredItem

NOW = datetime(2026, 11, 18, 12, 0, tzinfo=timezone.utc)
POLICY = RetentionPolicy(normal=timedelta(days=90), secret=timedelta(hours=2))

NOTE = ItemPayload(content="FAKE retention note", label=Label.NOTE, last_copied_at=NOW)
SECRET = ItemPayload(
    content="FAKE-ret-Zx9!q-not-real",
    label=Label.POSSIBLE_SECRET,
    secret_state=SecretState.SECRET,
    expires_at=NOW + timedelta(hours=2),
    last_copied_at=NOW,
)
UNSURE = replace(SECRET, secret_state=SecretState.UNSURE, label=Label.NOTE)


def stored(payload: ItemPayload, item_id: int = 1, created_at: datetime = NOW) -> StoredItem:
    return StoredItem(id=item_id, created_at=created_at, payload=payload)


# --- Defaults ---


def test_default_policy_matches_the_owners_decisions() -> None:
    policy = RetentionPolicy()
    assert policy.normal == timedelta(days=90)
    assert policy.secret == timedelta(hours=2)


# --- Normal expiry ---


def test_normal_item_is_kept_until_90_days_after_last_copy() -> None:
    item = stored(NOTE)
    assert not retention.is_expired(item, NOW + timedelta(days=90) - timedelta(seconds=1), POLICY)
    assert retention.is_expired(item, NOW + timedelta(days=90), POLICY)


def test_normal_age_counts_from_the_last_copy_not_the_first() -> None:
    recopied = replace(NOTE, last_copied_at=NOW + timedelta(days=60))
    item = stored(recopied, created_at=NOW)
    assert not retention.is_expired(item, NOW + timedelta(days=100), POLICY)


def test_old_rows_without_last_copy_use_created_at() -> None:
    item = stored(replace(NOTE, last_copied_at=None), created_at=NOW)
    assert retention.last_copied(item) == NOW
    assert retention.is_expired(item, NOW + timedelta(days=90), POLICY)


def test_normal_items_ignore_expires_at() -> None:
    item = stored(replace(NOTE, expires_at=NOW + timedelta(minutes=1)))
    assert not retention.is_expired(item, NOW + timedelta(days=1), POLICY)


def test_policy_lengths_are_configurable() -> None:
    short = RetentionPolicy(normal=timedelta(days=1), secret=timedelta(minutes=5))
    assert retention.is_expired(stored(NOTE), NOW + timedelta(days=1), short)


# --- Pinned exemption ---


def test_pinned_normal_item_never_expires() -> None:
    item = stored(replace(NOTE, pinned=True))
    assert retention.deadline(item, POLICY) is None
    assert not retention.is_expired(item, NOW + timedelta(days=10_000), POLICY)


# --- Secret and unsure expiry ---


@pytest.mark.parametrize("payload", [SECRET, UNSURE], ids=["secret", "unsure"])
def test_sensitive_items_expire_at_their_deadline(payload: ItemPayload) -> None:
    item = stored(payload)
    assert not retention.is_expired(item, NOW + timedelta(hours=2) - timedelta(seconds=1), POLICY)
    assert retention.is_expired(item, NOW + timedelta(hours=2), POLICY)


def test_secret_without_deadline_expires_from_creation() -> None:
    item = stored(replace(SECRET, expires_at=None), created_at=NOW)
    assert retention.deadline(item, POLICY) == NOW + timedelta(hours=2)


def test_secret_deadline_ignores_later_copies() -> None:
    item = stored(replace(SECRET, last_copied_at=NOW + timedelta(hours=1, minutes=59)))
    assert retention.is_expired(item, NOW + timedelta(hours=2), POLICY)


def test_expired_ids_lists_only_expired_items() -> None:
    items = [stored(NOTE, 1), stored(SECRET, 2), stored(replace(NOTE, pinned=True), 3)]
    assert retention.expired_ids(items, NOW + timedelta(hours=3), POLICY) == [2]
    assert retention.expired_ids(items, NOW + timedelta(days=91), POLICY) == [1, 2]


# --- New items ---


def test_new_secret_gets_a_deadline_from_its_first_copy() -> None:
    fresh = replace(SECRET, expires_at=None, last_copied_at=None)
    prepared = retention.prepare_new(fresh, "k" * 64, NOW, POLICY)
    assert prepared.expires_at == NOW + timedelta(hours=2)
    assert prepared.last_copied_at == NOW
    assert prepared.dedupe_key == "k" * 64


def test_new_normal_item_gets_no_deadline() -> None:
    prepared = retention.prepare_new(replace(NOTE, last_copied_at=None), "k" * 64, NOW, POLICY)
    assert prepared.expires_at is None


# --- Re-copy merge ---


def test_recopy_of_normal_item_only_moves_last_copy() -> None:
    existing = replace(NOTE, source_app="fakeeditor.exe", pinned=True)
    incoming = replace(NOTE, source_app="fakebrowser.exe", last_copied_at=None)
    later = NOW + timedelta(days=3)
    merged = retention.merge_recopy(existing, incoming, later, POLICY)
    assert merged == replace(existing, last_copied_at=later)


@pytest.mark.parametrize("incoming_state", list(SecretState))
def test_recopy_never_extends_a_secret(incoming_state: SecretState) -> None:
    incoming = replace(NOTE, secret_state=incoming_state)
    if incoming_state is not SecretState.NORMAL:
        incoming = replace(incoming, expires_at=NOW + timedelta(days=1))
    later = NOW + timedelta(hours=1)
    merged = retention.merge_recopy(SECRET, incoming, later, POLICY)
    assert merged.secret_state is SecretState.SECRET
    assert merged.expires_at == SECRET.expires_at
    assert merged.last_copied_at == later


def test_recopy_never_extends_an_unsure_item() -> None:
    later = NOW + timedelta(hours=1)
    merged = retention.merge_recopy(UNSURE, NOTE, later, POLICY)
    assert merged.secret_state is SecretState.UNSURE
    assert merged.expires_at == UNSURE.expires_at


def test_unsure_item_copied_as_secret_becomes_secret_with_same_deadline() -> None:
    merged = retention.merge_recopy(UNSURE, SECRET, NOW + timedelta(hours=1), POLICY)
    assert merged.secret_state is SecretState.SECRET
    assert merged.expires_at == UNSURE.expires_at


def test_normal_item_copied_as_secret_becomes_secret_and_unpinned() -> None:
    existing = replace(NOTE, pinned=True)
    later = NOW + timedelta(days=5)
    merged = retention.merge_recopy(existing, SECRET, later, POLICY)
    assert merged.secret_state is SecretState.SECRET
    assert merged.label is Label.POSSIBLE_SECRET
    assert merged.pinned is False
    assert merged.expires_at == later + timedelta(hours=2)


# --- Ordering ---


def test_newest_first_orders_by_last_copy() -> None:
    first = stored(replace(NOTE, last_copied_at=NOW), 1)
    second = stored(replace(NOTE, last_copied_at=NOW + timedelta(minutes=1)), 2)
    recopied_first = stored(replace(NOTE, last_copied_at=NOW + timedelta(minutes=2)), 1)
    assert [i.id for i in retention.newest_first([first, second])] == [2, 1]
    assert [i.id for i in retention.newest_first([recopied_first, second])] == [1, 2]


def test_equal_times_put_the_higher_id_first() -> None:
    items = [stored(NOTE, 1), stored(NOTE, 2)]
    assert [i.id for i in retention.newest_first(items)] == [2, 1]


# --- Pin rules ---


def test_normal_item_can_be_pinned_and_unpinned() -> None:
    pinned = retention.pin(NOTE)
    assert pinned.pinned is True
    assert retention.unpin(pinned).pinned is False


@pytest.mark.parametrize("payload", [SECRET, UNSURE], ids=["secret", "unsure"])
def test_sensitive_items_cannot_be_pinned(payload: ItemPayload) -> None:
    with pytest.raises(PinRefused) as caught:
        retention.pin(payload)
    assert payload.content not in str(caught.value)


@pytest.mark.parametrize("state", [SecretState.SECRET, SecretState.UNSURE])
def test_pinned_sensitive_payload_cannot_be_built(state: SecretState) -> None:
    with pytest.raises(ValueError):
        ItemPayload(content="FAKE pinned", label=Label.NOTE, secret_state=state, pinned=True)
