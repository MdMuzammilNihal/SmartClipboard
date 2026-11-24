"""Retention, duplicate-merge, and pin rules (tasks 8 and 21).

These are pure functions on decrypted items. history.py applies them to the
items held in memory and to the database.

Rules (owner's decisions in CLAUDE.md sections 3 and 8, and 2026-11-18;
the choices under SECURITY REVIEW were proposed by Claude and confirmed by
the owner on 2026-11-18):

- Normal items are deleted once `NORMAL_RETENTION` (default 90 days) has
  passed since they were last copied. Copying one again starts the count
  over, since it then counts as "copied just now".
- Pinned normal items are never deleted by retention.
- Secret and unsure items are deleted at their `expires_at`, set to
  `SECRET_RETENTION` (default 2 hours) after the first copy. Copying one
  again does not move that deadline.
- Secret and unsure items cannot be pinned.

SECURITY REVIEW (owner):

- Unsure items cannot be pinned either. They carry the short secret
  retention until the owner confirms them, and pinning would let a possible
  secret stay forever. To pin one, the owner first marks it normal.
- When a copy comes in again, the item never becomes less protected: if the
  old item is secret and the new copy looks normal, it stays secret with its
  original deadline. If the new copy looks more sensitive than the stored
  item (for example the same text now comes from a login-type app), the item
  takes the stricter state and is unpinned. A normal item that becomes
  secret this way gets a deadline counted from this copy, since it was not a
  secret before; an unsure item that becomes secret keeps its deadline.
- When a duplicate is merged, the stored label and source app are kept
  (unless the state is raised as above); only the last-copied time changes.
- A secret or unsure item with no `expires_at` (which this code never
  writes) is treated as expiring `SECRET_RETENTION` after it was created,
  so a damaged deadline can only shorten how long a secret is kept.
- Normal items ignore `expires_at`: their age counts from the last copy.
"""

from dataclasses import dataclass, replace
from datetime import datetime, timedelta

from smartclip import config
from smartclip.labels import SecretState
from smartclip.storage import ItemPayload, StoredItem

# Higher means more protected. Used so a re-copy never lowers protection.
_PROTECTION = {SecretState.NORMAL: 0, SecretState.UNSURE: 1, SecretState.SECRET: 2}


@dataclass(frozen=True)
class RetentionPolicy:
    """How long items are kept. Defaults come from config."""

    normal: timedelta = config.NORMAL_RETENTION
    secret: timedelta = config.SECRET_RETENTION


class PinRefused(Exception):
    """A secret or unsure item cannot be pinned. Never carries the item."""


def is_sensitive(payload: ItemPayload) -> bool:
    """True for secret and unsure items, which get the short retention."""
    return payload.secret_state is not SecretState.NORMAL


def last_copied(item: StoredItem) -> datetime:
    """When the item was last copied. Items saved before this field existed use created_at."""
    return item.payload.last_copied_at or item.created_at


def deadline(item: StoredItem, policy: RetentionPolicy) -> datetime | None:
    """When retention deletes the item, or None if it is pinned and normal."""
    payload = item.payload
    if is_sensitive(payload):
        return payload.expires_at or item.created_at + policy.secret
    if payload.pinned:
        return None
    return last_copied(item) + policy.normal


def is_expired(item: StoredItem, now: datetime, policy: RetentionPolicy) -> bool:
    """True once the item's deadline has been reached."""
    item_deadline = deadline(item, policy)
    return item_deadline is not None and now >= item_deadline


def expired_ids(
    items: list[StoredItem], now: datetime, policy: RetentionPolicy
) -> list[int]:
    """Return the ids of items that retention should delete now."""
    return [item.id for item in items if is_expired(item, now, policy)]


def newest_first(items: list[StoredItem]) -> list[StoredItem]:
    """Sort by last copy, most recent first, so a re-copied item moves to the top."""
    return sorted(items, key=lambda item: (last_copied(item), item.id), reverse=True)


def prepare_new(
    payload: ItemPayload, dedupe_key: str, now: datetime, policy: RetentionPolicy
) -> ItemPayload:
    """Fill in the fields a newly captured item needs before it is first saved."""
    expires_at = payload.expires_at
    if is_sensitive(payload) and expires_at is None:
        expires_at = now + policy.secret
    return replace(payload, dedupe_key=dedupe_key, last_copied_at=now, expires_at=expires_at)


def merge_recopy(
    existing: ItemPayload, incoming: ItemPayload, now: datetime, policy: RetentionPolicy
) -> ItemPayload:
    """Return the stored item updated for a new copy of the same text."""
    old_state, new_state = existing.secret_state, incoming.secret_state
    if _PROTECTION[new_state] <= _PROTECTION[old_state]:
        return replace(existing, last_copied_at=now)
    if old_state is SecretState.NORMAL:
        expires_at = now + policy.secret
    else:
        expires_at = existing.expires_at
    return replace(
        existing,
        label=incoming.label,
        secret_state=new_state,
        pinned=False,
        expires_at=expires_at,
        last_copied_at=now,
        title=None,  # secrets cannot be titled or tagged (naming.py)
        tags=(),
    )


def pin(payload: ItemPayload) -> ItemPayload:
    """Return the payload pinned. Raises PinRefused for secret and unsure items."""
    if is_sensitive(payload):
        raise PinRefused("secret and unsure items cannot be pinned")
    return replace(payload, pinned=True)


def unpin(payload: ItemPayload) -> ItemPayload:
    """Return the payload unpinned."""
    return replace(payload, pinned=False)
