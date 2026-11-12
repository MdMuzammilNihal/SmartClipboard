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
- A row that cannot be decrypted or parsed is skipped and reported by id. It
  never raises out of load_all(), so one damaged row cannot stop the app.
- Decoding errors are turned into PayloadError outside the `except` block, so
  the original error (a JSON error keeps a copy of the text it failed on) is
  not attached to the one we raise.
- Decrypted items stay in Python memory while the app runs. Python cannot
  reliably wipe them.

Threading: a Store must be used from the thread that created it.
"""

import json
import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Self

from smartclip import dpapi, logsafe
from smartclip.labels import Label, SecretState

PAYLOAD_VERSION = 1
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


@dataclass(frozen=True)
class ItemPayload:
    """Everything about an item that is stored encrypted."""

    content: str
    label: Label
    source_app: str | None = None
    secret_state: SecretState = SecretState.NORMAL
    pinned: bool = False
    expires_at: datetime | None = None
    kind: str = KIND_TEXT

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
            "expires_at": None if self.expires_at is None else _to_utc_text(self.expires_at),
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
        if data["v"] != PAYLOAD_VERSION or data["kind"] != KIND_TEXT:
            raise ValueError
        content = data["content"]
        source_app = data["source_app"]
        pinned = data["pinned"]
        expires_text = data["expires_at"]
        if not isinstance(content, str) or not isinstance(pinned, bool):
            raise TypeError
        if source_app is not None and not isinstance(source_app, str):
            raise TypeError
        return cls(
            content=content,
            label=Label(data["label"]),
            source_app=source_app,
            secret_state=SecretState(data["secret_state"]),
            pinned=pinned,
            expires_at=None if expires_text is None else _from_utc_text(expires_text),
            kind=KIND_TEXT,
        )


@dataclass(frozen=True)
class StoredItem:
    """A decrypted row."""

    id: int
    created_at: datetime
    payload: ItemPayload


@dataclass(frozen=True)
class LoadResult:
    """The readable items, plus the ids of rows that could not be read."""

    items: list[StoredItem]
    failed_ids: list[int]


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
        for item_id, created_text, blob in rows:
            item = _decode_row(item_id, created_text, blob)
            if item is None:
                failed_ids.append(item_id)
                logsafe.log_event(
                    _log, "item_unreadable", level=logging.WARNING, item_id=item_id
                )
            else:
                items.append(item)
        logsafe.log_event(_log, "items_loaded", count=len(items), failed=len(failed_ids))
        return LoadResult(items=items, failed_ids=failed_ids)

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
