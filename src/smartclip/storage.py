"""Encrypted local storage: one DPAPI-protected JSON payload per SQLite row.

SECURITY REVIEW (owner): what is and is not readable in the database file.

Readable by anyone who has the file:
- Each row's `id` and `created_at`. So the number of items and when each was
  saved are visible.
- The size of each encrypted payload, which roughly reveals how long the
  copied text was. Payloads are not padded to hide this.
- SQLite's own bookkeeping, including the highest id ever used (kept so that
  ids are never reused after a delete).

Not readable without this Windows account:
- Everything else: the content, label, source app, secret state, pinned flag,
  and expiry all live inside the encrypted payload.

Other choices:
- Filtering, search, and retention run in memory after load_all() decrypts
  the rows. Nothing about an item is stored in plaintext to make queries
  faster.
- `PRAGMA secure_delete` makes SQLite overwrite deleted rows with zeros
  instead of leaving them in free pages. It does not control copies the disk
  or file system may keep (SSD wear levelling, backups, shadow copies).
- A row that cannot be decrypted or parsed is skipped and reported by id,
  with its plaintext creation time when that is valid. It never raises out
  of load_all(), so one damaged row cannot stop the app. history.py decides
  when such rows are deleted.
- Decoding errors are turned into PayloadError outside the `except` block, so
  the original error (a JSON error keeps a copy of the text it failed on) is
  not attached to the one we raise.
- Decrypted items stay in Python memory while the app runs. Python cannot
  reliably wipe them.
- Version 2 payloads add `dedupe_key` (the keyed hash used to merge
  duplicates, see dedupe.py) and `last_copied_at` (when the item was last
  copied). Both live inside the encrypted payload, so the file shows no hash
  of the content in any form, and re-copying an item rewrites only the
  encrypted payload, never the plaintext `created_at`. Version 1 rows still
  load, with both fields empty.
- A payload that is pinned while secret or unsure is refused (ValueError when
  built, unreadable when loaded), because secrets cannot be pinned.

Threading: a Store must be used from the thread that created it.
"""

import json
import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Self

from smartclip import dpapi, logsafe
from smartclip.labels import Label, SecretState

PAYLOAD_VERSION = 2
# Versions load_all() can read. Version 1 has no dedupe_key or last_copied_at.
_READABLE_VERSIONS = frozenset({1, 2})
# The only kind in this version. The field exists so that other kinds can be
# added later without migrating existing rows.
KIND_TEXT = "text"

_log = logsafe.get_logger("storage")

_CREATE_TABLE = """
    CREATE TABLE IF NOT EXISTS items (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        created_at TEXT NOT NULL,
        payload BLOB NOT NULL
    )
"""


class PayloadError(Exception):
    """A decrypted payload is not a valid item. Never carries the payload."""


def _to_utc_text(moment: datetime) -> str:
    """Format a timezone-aware datetime as ISO 8601 text in UTC."""
    if moment.tzinfo is None:
        raise ValueError("datetime must be timezone-aware")
    return moment.astimezone(timezone.utc).isoformat()


def _from_utc_text(text: str) -> datetime:
    """Parse text written by _to_utc_text()."""
    moment = datetime.fromisoformat(text)
    if moment.tzinfo is None:
        raise ValueError("stored datetime has no timezone")
    return moment


def _optional_time_text(moment: datetime | None) -> str | None:
    return None if moment is None else _to_utc_text(moment)


@dataclass(frozen=True)
class ItemPayload:
    """Everything about an item that is stored encrypted.

    `expires_at` is the fixed deadline of a secret or unsure item. Normal
    items expire by the time since `last_copied_at` instead (retention.py).
    `last_copied_at` is None for items saved before version 2; their
    creation time stands in for it.
    """

    content: str
    label: Label
    source_app: str | None = None
    secret_state: SecretState = SecretState.NORMAL
    pinned: bool = False
    expires_at: datetime | None = None
    kind: str = KIND_TEXT
    dedupe_key: str | None = None
    last_copied_at: datetime | None = None

    def __post_init__(self) -> None:
        if self.pinned and self.secret_state is not SecretState.NORMAL:
            raise ValueError("secret and unsure items cannot be pinned")

    def to_bytes(self) -> bytes:
        """Serialize to JSON bytes, ready to be encrypted."""
        data = {
            "v": PAYLOAD_VERSION,
            "kind": self.kind,
            "content": self.content,
            "label": self.label.value,
            "source_app": self.source_app,
            "secret_state": self.secret_state.value,
            "pinned": self.pinned,
            "expires_at": _optional_time_text(self.expires_at),
            "dedupe_key": self.dedupe_key,
            "last_copied_at": _optional_time_text(self.last_copied_at),
        }
        return json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8")

    @classmethod
    def from_bytes(cls, raw: bytes) -> Self:
        """Parse decrypted JSON bytes. Raises PayloadError if they are not an item."""
        payload = None
        try:
            payload = cls._parse(json.loads(raw.decode("utf-8")))
        except (ValueError, KeyError, TypeError):
            pass
        if payload is None:
            raise PayloadError("payload is not a valid item")
        return payload

    @classmethod
    def _parse(cls, data: object) -> Self:
        """Build a payload from parsed JSON, raising on anything unexpected."""
        if not isinstance(data, dict):
            raise TypeError
        version = data["v"]
        if type(version) is not int or version not in _READABLE_VERSIONS:
            raise ValueError
        if data["kind"] != KIND_TEXT:
            raise ValueError
        content = data["content"]
        source_app = data["source_app"]
        pinned = data["pinned"]
        expires_text = data["expires_at"]
        dedupe_key = data["dedupe_key"] if version >= 2 else None
        last_copied_text = data["last_copied_at"] if version >= 2 else None
        if not isinstance(content, str) or not isinstance(pinned, bool):
            raise TypeError
        for optional_text in (source_app, dedupe_key, expires_text, last_copied_text):
            if optional_text is not None and not isinstance(optional_text, str):
                raise TypeError
        return cls(
            content=content,
            label=Label(data["label"]),
            source_app=source_app,
            secret_state=SecretState(data["secret_state"]),
            pinned=pinned,
            expires_at=None if expires_text is None else _from_utc_text(expires_text),
            kind=KIND_TEXT,
            dedupe_key=dedupe_key,
            last_copied_at=None if last_copied_text is None else _from_utc_text(last_copied_text),
        )


@dataclass(frozen=True)
class StoredItem:
    """A decrypted row."""

    id: int
    created_at: datetime
    payload: ItemPayload


@dataclass(frozen=True)
class LoadResult:
    """The readable items, plus the ids of rows that could not be read.

    `failed_created` gives the plaintext creation time of each unreadable row
    whose `created_at` is itself still valid, so retention can age them out.
    """

    items: list[StoredItem]
    failed_ids: list[int]
    failed_created: dict[int, datetime] = field(default_factory=dict)


class Store:
    """The encrypted item database."""

    def __init__(self, db_path: Path) -> None:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(db_path)
        # secure_delete is a per-connection setting, so it is set on every open.
        self._conn.execute("PRAGMA secure_delete = ON")
        with self._conn:
            self._conn.execute(_CREATE_TABLE)

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_exc_info: object) -> None:
        self.close()

    def close(self) -> None:
        """Close the database file."""
        self._conn.close()

    def add(self, payload: ItemPayload, created_at: datetime | None = None) -> int:
        """Encrypt and save an item. Returns its id."""
        created_text = _to_utc_text(created_at or datetime.now(timezone.utc))
        blob = dpapi.protect(payload.to_bytes())
        with self._conn:
            cursor = self._conn.execute(
                "INSERT INTO items (created_at, payload) VALUES (?, ?)", (created_text, blob)
            )
        item_id = cursor.lastrowid
        assert item_id is not None
        logsafe.log_event(
            _log,
            "item_saved",
            item_id=item_id,
            length=len(payload.content),
            content_type=payload.label,
        )
        return item_id

    def update(self, item_id: int, payload: ItemPayload) -> bool:
        """Replace an item's payload. Returns False if the id does not exist."""
        blob = dpapi.protect(payload.to_bytes())
        with self._conn:
            cursor = self._conn.execute(
                "UPDATE items SET payload = ? WHERE id = ?", (blob, item_id)
            )
        found = cursor.rowcount > 0
        logsafe.log_event(_log, "item_updated", item_id=item_id, found=found)
        return found

    def delete(self, item_id: int) -> bool:
        """Delete an item. Returns False if the id does not exist."""
        with self._conn:
            cursor = self._conn.execute("DELETE FROM items WHERE id = ?", (item_id,))
        found = cursor.rowcount > 0
        logsafe.log_event(_log, "item_deleted", item_id=item_id, found=found)
        return found

    def load_all(self) -> LoadResult:
        """Decrypt every row. Unreadable rows are skipped and reported by id."""
        rows = self._conn.execute(
            "SELECT id, created_at, payload FROM items ORDER BY id"
        ).fetchall()
        items: list[StoredItem] = []
        failed_ids: list[int] = []
        failed_created: dict[int, datetime] = {}
        for item_id, created_text, blob in rows:
            item = _decode_row(item_id, created_text, blob)
            if item is None:
                failed_ids.append(item_id)
                created_at = _created_or_none(created_text)
                if created_at is not None:
                    failed_created[item_id] = created_at
                logsafe.log_event(
                    _log, "item_unreadable", level=logging.WARNING, item_id=item_id
                )
            else:
                items.append(item)
        logsafe.log_event(_log, "items_loaded", count=len(items), failed=len(failed_ids))
        return LoadResult(items=items, failed_ids=failed_ids, failed_created=failed_created)

    def vacuum(self) -> None:
        """Rebuild the file so space freed by deletes is returned to the disk."""
        self._conn.execute("VACUUM")
        logsafe.log_event(_log, "database_vacuumed")


def _decode_row(item_id: int, created_text: object, blob: object) -> StoredItem | None:
    """Decrypt and parse one row. Returns None if anything about it is wrong."""
    if not isinstance(blob, bytes) or not isinstance(created_text, str):
        return None
    try:
        payload = ItemPayload.from_bytes(dpapi.unprotect(blob))
        created_at = _from_utc_text(created_text)
    except (dpapi.DpapiError, PayloadError, ValueError):
        return None
    return StoredItem(id=item_id, created_at=created_at, payload=payload)


def _created_or_none(created_text: object) -> datetime | None:
    """Parse a row's plaintext creation time, or return None if it is not valid."""
    if not isinstance(created_text, str):
        return None
    try:
        return _from_utc_text(created_text)
    except ValueError:
        return None
