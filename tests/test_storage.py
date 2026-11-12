"""Tests for encrypted storage. All values are fake.

These use the real Windows DPAPI, so they only run on Windows.
"""

import io
import logging
import sqlite3
from collections.abc import Iterator
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from smartclip import dpapi, logsafe
from smartclip.labels import Label, SecretState
from smartclip.storage import ItemPayload, PayloadError, Store

FAKE_CONTENT = "FAKE-secret-value-Zx9!q-not-real"
FAKE_SOURCE_APP = "fakebrowser.exe"
FAKE_CREATED = datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)

FAKE_SECRET = ItemPayload(
    content=FAKE_CONTENT,
    label=Label.POSSIBLE_SECRET,
    source_app=FAKE_SOURCE_APP,
    secret_state=SecretState.SECRET,
    expires_at=FAKE_CREATED + timedelta(hours=2),
)
FAKE_NOTE = ItemPayload(content="FAKE note: meeting at 5pm", label=Label.NOTE)


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "data" / "items.db"


@pytest.fixture
def store(db_path: Path) -> Iterator[Store]:
    with Store(db_path) as opened:
        yield opened


def overwrite_payload(db_path: Path, item_id: int, value: object) -> None:
    """Damage a row directly, the way corruption or tampering would."""
    with closing(sqlite3.connect(db_path)) as conn, conn:
        conn.execute("UPDATE items SET payload = ? WHERE id = ?", (value, item_id))


def read_blob(db_path: Path, item_id: int) -> bytes:
    with closing(sqlite3.connect(db_path)) as conn:
        return conn.execute("SELECT payload FROM items WHERE id = ?", (item_id,)).fetchone()[0]


# --- Round trip ---


def test_item_round_trips_with_every_field(store: Store) -> None:
    item_id = store.add(FAKE_SECRET, created_at=FAKE_CREATED)
    result = store.load_all()
    assert result.failed_ids == []
    assert len(result.items) == 1
    item = result.items[0]
    assert item.id == item_id
    assert item.created_at == FAKE_CREATED
    assert item.payload == FAKE_SECRET
    assert item.payload.kind == "text"


def test_unicode_and_multiline_content_round_trips(store: Store) -> None:
    payload = ItemPayload(content="FAKE línea 1\nFAKE 行 2\t\U0001f600", label=Label.NOTE)
    store.add(payload)
    assert store.load_all().items[0].payload == payload


def test_items_survive_closing_and_reopening(db_path: Path) -> None:
    with Store(db_path) as first:
        first.add(FAKE_NOTE)
        first.add(FAKE_SECRET)
    with Store(db_path) as second:
        payloads = [item.payload for item in second.load_all().items]
    assert payloads == [FAKE_NOTE, FAKE_SECRET]


def test_created_at_defaults_to_now_in_utc(store: Store) -> None:
    before = datetime.now(timezone.utc)
    store.add(FAKE_NOTE)
    created = store.load_all().items[0].created_at
    assert before <= created <= datetime.now(timezone.utc)


def test_naive_datetime_is_rejected(store: Store) -> None:
    with pytest.raises(ValueError):
        store.add(FAKE_NOTE, created_at=datetime(2026, 1, 2, 3, 4, 5))


# --- What is plaintext in the file ---


def test_table_has_only_id_created_at_and_payload(store: Store, db_path: Path) -> None:
    store.add(FAKE_SECRET)
    with closing(sqlite3.connect(db_path)) as conn:
        columns = [row[1] for row in conn.execute("PRAGMA table_info(items)")]
        tables = [row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")]
    assert columns == ["id", "created_at", "payload"]
    # sqlite_sequence is SQLite's own counter for AUTOINCREMENT ids.
    assert sorted(tables) == ["items", "sqlite_sequence"]


def test_raw_file_contains_nothing_sensitive(db_path: Path) -> None:
    with Store(db_path) as opened:
        opened.add(FAKE_SECRET, created_at=FAKE_CREATED)
    raw = db_path.read_bytes()
    sensitive = [
        FAKE_CONTENT,
        FAKE_SOURCE_APP,
        Label.POSSIBLE_SECRET.value,
        "secret_state",
        "source_app",
        "expires_at",
    ]
    for text in sensitive:
        assert text.encode("utf-8") not in raw
        assert text.encode("utf-16-le") not in raw
    # The creation time is expected to be readable.
    assert b"2026-01-02T03:04:05+00:00" in raw


# --- Damaged rows ---


def test_garbage_blob_is_skipped_and_reported(store: Store, db_path: Path) -> None:
    good_id = store.add(FAKE_NOTE)
    bad_id = store.add(FAKE_SECRET)
    overwrite_payload(db_path, bad_id, b"garbage, not a DPAPI blob")
    result = store.load_all()
    assert result.failed_ids == [bad_id]
    assert [item.id for item in result.items] == [good_id]


def test_tampered_blob_is_skipped_and_reported(store: Store, db_path: Path) -> None:
    item_id = store.add(FAKE_SECRET)
    blob = bytearray(read_blob(db_path, item_id))
    blob[len(blob) // 2] ^= 0xFF
    overwrite_payload(db_path, item_id, bytes(blob))
    result = store.load_all()
    assert result.failed_ids == [item_id]
    assert result.items == []


def test_valid_encryption_of_invalid_json_is_skipped(store: Store, db_path: Path) -> None:
    item_id = store.add(FAKE_NOTE)
    overwrite_payload(db_path, item_id, dpapi.protect(b"FAKE not json"))
    assert store.load_all().failed_ids == [item_id]


def test_unknown_kind_is_skipped(store: Store, db_path: Path) -> None:
    item_id = store.add(FAKE_NOTE)
    unknown_kind = FAKE_NOTE.to_bytes().replace(b'"kind":"text"', b'"kind":"image"')
    overwrite_payload(db_path, item_id, dpapi.protect(unknown_kind))
    assert store.load_all().failed_ids == [item_id]


def test_payload_stored_as_text_is_skipped(store: Store, db_path: Path) -> None:
    item_id = store.add(FAKE_NOTE)
    overwrite_payload(db_path, item_id, "FAKE text where a blob should be")
    assert store.load_all().failed_ids == [item_id]


def test_payload_error_does_not_carry_the_payload() -> None:
    with pytest.raises(PayloadError) as caught:
        ItemPayload.from_bytes(FAKE_CONTENT.encode("utf-8"))
    assert FAKE_CONTENT not in str(caught.value)
    # The JSON error, which keeps the text it failed on, must not be attached.
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None


# --- Update and delete ---


def test_update_replaces_the_payload(store: Store) -> None:
    item_id = store.add(FAKE_NOTE)
    pinned = ItemPayload(content=FAKE_NOTE.content, label=Label.NOTE, pinned=True)
    assert store.update(item_id, pinned) is True
    assert store.load_all().items[0].payload == pinned


def test_update_of_missing_id_returns_false(store: Store) -> None:
    assert store.update(999, FAKE_NOTE) is False


def test_delete_removes_the_item(store: Store) -> None:
    keep_id = store.add(FAKE_NOTE)
    drop_id = store.add(FAKE_SECRET)
    assert store.delete(drop_id) is True
    assert [item.id for item in store.load_all().items] == [keep_id]


def test_delete_of_missing_id_returns_false(store: Store) -> None:
    assert store.delete(999) is False


def test_ids_are_not_reused_after_delete(store: Store) -> None:
    first_id = store.add(FAKE_NOTE)
    store.delete(first_id)
    assert store.add(FAKE_NOTE) > first_id


def test_deleted_row_is_overwritten_in_the_file(db_path: Path) -> None:
    with Store(db_path) as opened:
        opened.add(FAKE_NOTE)
        item_id = opened.add(FAKE_SECRET)
        blob = read_blob(db_path, item_id)
        assert blob in db_path.read_bytes()
        opened.delete(item_id)
    # secure_delete: even the encrypted bytes must be gone, not left in a free page.
    assert blob not in db_path.read_bytes()


def test_vacuum_keeps_remaining_items(store: Store) -> None:
    keep_id = store.add(FAKE_NOTE)
    store.delete(store.add(FAKE_SECRET))
    store.vacuum()
    assert [item.id for item in store.load_all().items] == [keep_id]


# --- No content in logs ---


def test_storage_flow_never_logs_content(store: Store, db_path: Path) -> None:
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    logsafe.attach_handler(handler)
    try:
        note_id = store.add(FAKE_NOTE)
        secret_id = store.add(FAKE_SECRET)
        store.update(note_id, FAKE_NOTE)
        overwrite_payload(db_path, secret_id, dpapi.protect(FAKE_CONTENT.encode("utf-8")))
        store.load_all()
        store.delete(note_id)
        store.vacuum()
    finally:
        logging.getLogger(logsafe.ROOT_LOGGER_NAME).removeHandler(handler)

    text = stream.getvalue()
    for forbidden in (FAKE_CONTENT, FAKE_NOTE.content, FAKE_SOURCE_APP):
        assert forbidden not in text
    assert f"item_saved item_id={secret_id} length={len(FAKE_CONTENT)}" in text
    assert f"item_unreadable item_id={secret_id}" in text
    assert logsafe.DROPPED_RECORD_TEXT not in text
    assert "dropped_fields" not in text
