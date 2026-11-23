"""The decrypted item history held in memory, kept in step with the database.

Search, filtering, and retention all run on these in-memory items (CLAUDE.md
section 8); nothing is queried from plaintext columns. This class applies
the rules in retention.py: duplicate merging on capture, cleanup of expired
items, pinning, and a periodic VACUUM.

Threading: like Store, a History must be used from the thread that created
its Store. The app (app.py, not built yet) will run it on that thread.

SECURITY REVIEW (owner):

- Duplicates are found by the keyed hash in dedupe.py, through an in-memory
  map from hash to item id. The map is rebuilt from decrypted payloads at
  load and is never written anywhere.
- "Moves to the top" is done by writing a new `last_copied_at` inside the
  encrypted payload. The plaintext `created_at` keeps the first-copy time
  and is never rewritten, so the file does not show when an item was copied
  again (it does show that some row changed, as any update would).
- A duplicate of an item that has already expired is not merged into it:
  the old item is deleted and the copy is saved as a new item, so an
  expired secret cannot be revived with its old deadline and vanish at
  once.
- Rows that could not be decrypted at load (owner's decision, 2026-11-18):
  such a row will never be readable again, so cleanup deletes it once its
  plaintext `created_at` is older than the normal retention (default 90
  days). Its real state cannot be known, so a damaged pinned item is
  deleted too, and a damaged secret is kept up to 90 days rather than 2
  hours (it stays encrypted all that time). This only happens when at least
  one other row in the same load decrypted fine: if every row fails, the
  likely cause is the Windows account (a profile reset, for example), not
  damage, and deleting would wipe the database for nothing. A row whose
  `created_at` is itself unreadable is never deleted, since its age is
  unknown. Note: after a profile reset, old rows become deletable once rows
  saved under the new account load; they were unrecoverable anyway.
- VACUUM rewrites the file to drop the free pages left by deletes
  (`secure_delete` already zeroes them). It runs at most once per
  `VACUUM_INTERVAL`, and only after something was deleted.
"""

from dataclasses import dataclass, replace
from datetime import datetime, timedelta

from smartclip import config, logsafe, retention
from smartclip.dedupe import DedupeKey
from smartclip.retention import RetentionPolicy
from smartclip.storage import ItemPayload, Store, StoredItem

_log = logsafe.get_logger("history")


@dataclass(frozen=True)
class CaptureResult:
    """Which item holds a capture, and whether it was merged into an existing one."""

    item_id: int
    merged: bool


class History:
    """All readable items, newest first, with retention and dedupe applied."""

    def __init__(
        self,
        store: Store,
        key: DedupeKey,
        policy: RetentionPolicy | None = None,
        vacuum_interval: timedelta = config.VACUUM_INTERVAL,
    ) -> None:
        self._store = store
        self._key = key
        self._policy = policy or RetentionPolicy()
        self._vacuum_interval = vacuum_interval
        self._items: dict[int, StoredItem] = {}
        self._by_key: dict[str, int] = {}
        self._last_vacuum: datetime | None = None
        self._deleted_since_vacuum = 0
        self._unreadable_created: dict[int, datetime] = {}

    @property
    def policy(self) -> RetentionPolicy:
        return self._policy

    def set_policy(self, policy: RetentionPolicy) -> None:
        """Use new retention lengths from now on (the settings panel).

        The normal length applies to every normal item at the next cleanup.
        A secret's deadline is fixed when it is first saved, so a new secret
        length only applies to secrets saved after the change.
        """
        self._policy = policy

    def load(self) -> list[int]:
        """Decrypt every row into memory. Returns the ids of unreadable rows."""
        result = self._store.load_all()
        self._items = {}
        self._by_key = {}
        for item in result.items:
            self._remember(item)
        # Only age out unreadable rows when DPAPI works for this account.
        self._unreadable_created = dict(result.failed_created) if result.items else {}
        return result.failed_ids

    def items(self) -> list[StoredItem]:
        """Every readable item, most recently copied first."""
        return retention.newest_first(list(self._items.values()))

    def get(self, item_id: int) -> StoredItem | None:
        """Return one item, or None if there is no such item."""
        return self._items.get(item_id)

    def capture(self, payload: ItemPayload, now: datetime) -> CaptureResult:
        """Save a new copy, merging it into an existing item with the same text."""
        key = self._key.digest(payload.content)
        existing_id = self._by_key.get(key)
        if existing_id is not None:
            existing = self._items[existing_id]
            if retention.is_expired(existing, now, self._policy):
                self._delete(existing_id)
            else:
                merged = retention.merge_recopy(existing.payload, payload, now, self._policy)
                merged = replace(merged, dedupe_key=key)
                if self._store.update(existing_id, merged):
                    self._remember(replace(existing, payload=merged))
                    logsafe.log_event(_log, "item_merged", item_id=existing_id)
                    return CaptureResult(item_id=existing_id, merged=True)
                # The row vanished from the file; drop it and save the copy as new.
                self._forget(existing_id)
        new_payload = retention.prepare_new(payload, key, now, self._policy)
        item_id = self._store.add(new_payload, created_at=now)
        self._remember(StoredItem(id=item_id, created_at=now, payload=new_payload))
        return CaptureResult(item_id=item_id, merged=False)

    def cleanup(self, now: datetime) -> list[int]:
        """Delete expired items and old unreadable rows. Returns the deleted ids."""
        deleted = retention.expired_ids(list(self._items.values()), now, self._policy)
        for item_id in deleted:
            self._delete(item_id)
        unreadable = [
            item_id
            for item_id, created_at in self._unreadable_created.items()
            if now - created_at >= self._policy.normal
        ]
        for item_id in unreadable:
            self._store.delete(item_id)
            del self._unreadable_created[item_id]
            self._deleted_since_vacuum += 1
        logsafe.log_event(
            _log, "retention_cleanup", deleted=len(deleted), unreadable_deleted=len(unreadable)
        )
        return deleted + unreadable

    def vacuum_if_due(self, now: datetime) -> bool:
        """Run VACUUM if something was deleted and the interval has passed."""
        if self._deleted_since_vacuum == 0:
            return False
        if self._last_vacuum is not None and now - self._last_vacuum < self._vacuum_interval:
            return False
        self._store.vacuum()
        self._last_vacuum = now
        self._deleted_since_vacuum = 0
        return True

    def pin(self, item_id: int) -> bool:
        """Pin a normal item. Returns False if it does not exist or is secret or unsure."""
        item = self._items.get(item_id)
        if item is None:
            return False
        try:
            pinned = retention.pin(item.payload)
        except retention.PinRefused:
            logsafe.log_event(_log, "pin_refused", item_id=item_id)
            return False
        return self.update(item_id, pinned)

    def unpin(self, item_id: int) -> bool:
        """Unpin an item. Returns False if it does not exist."""
        item = self._items.get(item_id)
        if item is None:
            return False
        return self.update(item_id, retention.unpin(item.payload))

    def update(self, item_id: int, payload: ItemPayload) -> bool:
        """Replace an item's payload, for example after mark-as-secret."""
        item = self._items.get(item_id)
        if item is None or not self._store.update(item_id, payload):
            return False
        self._remember(replace(item, payload=payload))
        return True

    def delete(self, item_id: int) -> bool:
        """Delete an item. Returns False if it does not exist."""
        if item_id not in self._items:
            return False
        self._delete(item_id)
        return True

    def restore(self, items: list[StoredItem], now: datetime) -> list[int]:
        """Put deleted items back (the window's Undo). Returns the new ids.

        Each item keeps its first-copied time and its payload, so it lands in
        its old place in the order and keeps its pin. The id is new, because
        the old row is gone. Two kinds are skipped on purpose:
        - an item whose retention deadline has passed (a secret's expiry
          wins over Undo), and
        - an item whose text is in the history again (copied again meanwhile),
          so Undo never creates a duplicate.
        """
        restored: list[int] = []
        for item in sorted(items, key=lambda each: each.created_at):
            if retention.is_expired(item, now, self._policy):
                continue
            key = item.payload.dedupe_key or self._key.digest(item.payload.content)
            if key in self._by_key:
                continue
            payload = replace(item.payload, dedupe_key=key)
            item_id = self._store.add(payload, created_at=item.created_at)
            self._remember(StoredItem(id=item_id, created_at=item.created_at, payload=payload))
            restored.append(item_id)
        logsafe.log_event(_log, "items_restored", requested=len(items), restored=len(restored))
        return restored

    def _remember(self, item: StoredItem) -> None:
        self._items[item.id] = item
        key = item.payload.dedupe_key or self._key.digest(item.payload.content)
        current = self._by_key.get(key)
        # If old rows hold the same text twice, new copies merge into the newest.
        if current is None or current <= item.id:
            self._by_key[key] = item.id

    def _forget(self, item_id: int) -> None:
        item = self._items.pop(item_id, None)
        if item is None:
            return
        key = item.payload.dedupe_key or self._key.digest(item.payload.content)
        if self._by_key.get(key) == item_id:
            del self._by_key[key]
            for other in self._items.values():
                other_key = other.payload.dedupe_key or self._key.digest(other.payload.content)
                if other_key == key:
                    self._remember(other)

    def _delete(self, item_id: int) -> None:
        self._store.delete(item_id)
        self._forget(item_id)
        self._deleted_since_vacuum += 1
