"""Private labeled-data log for the Phase 2 test set (task 20). Opt-in.

When config.LABELED_LOG_ENABLED is True (it is False by default), the app
writes one record for each new item it captures, and one for each time the
owner marks an item secret or normal. Phase 2 uses these records to measure
the rules against a trained model on real copies.

Owner's decisions (CLAUDE.md task 20, and 2026-11-19):

- Off by default. Until the settings screen exists, the owner turns it on
  by setting LABELED_LOG_ENABLED = True in config.py.
- A separate file, `labeled.db` in the app's data folder (outside the
  repository; `*.db` is gitignored), encrypted with DPAPI.
- Secret and unsure items are written only in redacted form (redact()
  below). When the owner later clicks Mark normal, that record holds the
  real text, because the owner confirmed it is not a secret.
- Redaction keeps the length, spaces, and known format prefixes (ghp_,
  sk-, AKIA, the private-key header lines, and so on), and replaces every
  letter and digit with a random one of the same kind. Symbols depend on
  the text (owner's decision 2026-11-19): in a one-word item (a lone
  password or token) each symbol becomes a random symbol, because there the
  symbols are part of the secret; in text with several words (JSON, a
  command, an env var line) punctuation is kept, because there it carries
  the structure.
- Records are kept until the owner deletes the file. Deleting an item in
  the window, or the item expiring, leaves its records in place.
- Each record holds: the content (plain or redacted), whether it was
  redacted, the label, the secret state, whether the owner confirmed it,
  the source app's name, the item id, what happened, and when. Never a
  window title.

SECURITY REVIEW (owner):

- The only plaintext column is each row's `id`. The time, and everything
  else, is inside the DPAPI-encrypted payload, so the file shows only how
  many records exist and the size of each.
- The log outlives normal retention on purpose: normal items stay in it
  after they are deleted from the history. That is the point of a test set,
  and the reason it is opt-in. Secrets are only ever in it redacted.
- Redaction leaks the shape: the length and the positions of upper case,
  lower case, digits, and symbols. In text with several words it also
  keeps the exact spaces and punctuation, so a symbol-only word inside such
  text (for example "pass: !@#$%^&*") is kept as it is. Field names inside
  a redacted JSON blob or env var line are randomised like everything else
  (owner's decision 2026-11-19: revisit if many such records appear). The
  random replacements come from the operating system's random source
  (secrets.SystemRandom), so they say nothing about the real characters.
- A Mark normal record stores the real text. If the owner marks a real
  secret normal by mistake, its text is in the log until the file is
  deleted.
- A re-copy that merges into an existing item is not logged again.
- A failure to write a record is logged by exception class only and never
  stops the capture.
- Threading: like the Store, a LabeledLog must be used from the thread that
  created it (the Qt thread).
"""

import json
import logging
import re
import secrets
import sqlite3
import string
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from typing import Self

from smartclip import dpapi, logsafe
from smartclip.labels import Label, SecretState
from smartclip.storage import KIND_TEXT, ItemPayload

RECORD_VERSION = 1

_log = logsafe.get_logger("datalog")

_CREATE_TABLE = """
    CREATE TABLE IF NOT EXISTS records (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        payload BLOB NOT NULL
    )
"""

# Parts of a secret that are kept as they are when it is redacted: the
# prefixes of the known formats in secret_rules.py, and private-key header
# and footer lines. They say which kind of key it was, not the key.
_KEPT_PREFIXES = re.compile(
    r"-----(?:BEGIN|END) (?:[A-Z0-9]+ )*PRIVATE KEY-----"
    r"|\b(?:gh[pousr]_|github_pat_|sk-|[sr]k_(?:live|test)_|xox[baprs]-|AKIA|ASIA|AIza|eyJ)"
)
_random = secrets.SystemRandom()


class RecordEvent(StrEnum):
    """What a record describes."""

    CAPTURED = "captured"
    MARKED_SECRET = "marked_secret"
    MARKED_NORMAL = "marked_normal"


@dataclass(frozen=True)
class LabeledRecord:
    """One decrypted record of the labeled log."""

    id: int
    event: RecordEvent
    item_id: int
    at: datetime
    content: str
    redacted: bool
    label: Label
    secret_state: SecretState
    owner_confirmed: bool
    source_app: str | None
    kind: str = KIND_TEXT


def _random_like(char: str, replace_symbols: bool) -> str:
    """A random character of the same kind, or the character itself."""
    if char in string.ascii_uppercase:
        return _random.choice(string.ascii_uppercase)
    if char in string.ascii_lowercase:
        return _random.choice(string.ascii_lowercase)
    if char in string.digits:
        return _random.choice(string.digits)
    if char.isupper():
        return _random.choice(string.ascii_uppercase)
    if char.isalpha():
        return _random.choice(string.ascii_lowercase)
    if char.isdigit():
        return _random.choice(string.digits)
    if replace_symbols and not char.isspace():
        return _random.choice(string.punctuation)
    return char  # spaces are kept; punctuation too in text with several words


def redact(text: str) -> str:
    """Return a same-shape copy of `text` with every letter and digit randomised.

    In a one-word text (nothing but spaces around it) symbols are randomised
    too; in text with several words they are kept. Known format prefixes and
    private-key header lines are always kept. The result has the same length
    as `text`.
    """
    one_word = len(text.split()) <= 1
    kept = [False] * len(text)
    for match in _KEPT_PREFIXES.finditer(text):
        for index in range(match.start(), match.end()):
            kept[index] = True
    return "".join(
        char if keep else _random_like(char, replace_symbols=one_word)
        for char, keep in zip(text, kept)
    )


def _needs_redaction(payload: ItemPayload) -> bool:
    return payload.secret_state is not SecretState.NORMAL


def _to_utc_text(moment: datetime) -> str:
    if moment.tzinfo is None:
        raise ValueError("datetime must be timezone-aware")
    return moment.astimezone(timezone.utc).isoformat()


class LabeledLog:
    """The encrypted labeled-data file. Use from one thread only."""

    def __init__(self, db_path: Path) -> None:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(db_path)
        self._conn.execute("PRAGMA secure_delete = ON")
        with self._conn:
            self._conn.execute(_CREATE_TABLE)

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_exc_info: object) -> None:
        self.close()

    def close(self) -> None:
        """Close the file."""
        self._conn.close()

    def record(
        self, event: RecordEvent, item_id: int, payload: ItemPayload, at: datetime
    ) -> int | None:
        """Write one record. Returns its id, or None if writing failed.

        Secret and unsure items are redacted before anything is written.
        Marking is the owner's confirmation; a capture is the rules' guess.
        """
        redacted = _needs_redaction(payload)
        data = {
            "v": RECORD_VERSION,
            "kind": payload.kind,
            "event": event.value,
            "item_id": item_id,
            "at": _to_utc_text(at),
            "content": redact(payload.content) if redacted else payload.content,
            "redacted": redacted,
            "label": payload.label.value,
            "secret_state": payload.secret_state.value,
            "owner_confirmed": event is not RecordEvent.CAPTURED,
            "source_app": payload.source_app,
        }
        raw = json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        try:
            blob = dpapi.protect(raw)
            with self._conn:
                cursor = self._conn.execute("INSERT INTO records (payload) VALUES (?)", (blob,))
        except (dpapi.DpapiError, sqlite3.Error) as error:
            logsafe.log_error(_log, "labeled_record_failed", error, item_id=item_id)
            return None
        record_id = cursor.lastrowid
        logsafe.log_event(
            _log,
            "labeled_record_written",
            record_id=record_id,
            item_id=item_id,
            redacted=redacted,
            content_type=payload.label,
        )
        return record_id

    def load_all(self) -> tuple[list[LabeledRecord], list[int]]:
        """Decrypt every record. Returns the readable ones and the ids of the rest."""
        records: list[LabeledRecord] = []
        failed: list[int] = []
        for record_id, blob in self._conn.execute(
            "SELECT id, payload FROM records ORDER BY id"
        ).fetchall():
            record = _decode(record_id, blob)
            if record is None:
                failed.append(record_id)
                logsafe.log_event(
                    _log, "labeled_record_unreadable", level=logging.WARNING, record_id=record_id
                )
            else:
                records.append(record)
        return records, failed


def _decode(record_id: int, blob: object) -> LabeledRecord | None:
    """Decrypt and parse one record. Returns None if anything about it is wrong."""
    if not isinstance(blob, bytes):
        return None
    record = None
    try:
        data = json.loads(dpapi.unprotect(blob).decode("utf-8"))
        record = _parse(record_id, data)
    except (dpapi.DpapiError, ValueError, KeyError, TypeError):
        pass  # the error may hold a copy of the text; it is dropped here
    return record


def _parse(record_id: int, data: object) -> LabeledRecord:
    if not isinstance(data, dict) or data["v"] != RECORD_VERSION:
        raise ValueError
    content, source_app = data["content"], data["source_app"]
    if not isinstance(content, str) or not (source_app is None or isinstance(source_app, str)):
        raise TypeError
    if type(data["item_id"]) is not int:
        raise TypeError
    if not isinstance(data["redacted"], bool) or not isinstance(data["owner_confirmed"], bool):
        raise TypeError
    at = datetime.fromisoformat(data["at"])
    if at.tzinfo is None:
        raise ValueError
    return LabeledRecord(
        id=record_id,
        event=RecordEvent(data["event"]),
        item_id=data["item_id"],
        at=at,
        content=content,
        redacted=data["redacted"],
        label=Label(data["label"]),
        secret_state=SecretState(data["secret_state"]),
        owner_confirmed=data["owner_confirmed"],
        source_app=source_app,
        kind=data["kind"],
    )
