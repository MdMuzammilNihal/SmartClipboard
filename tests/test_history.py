"""Tests for the in-memory history: dedupe, retention, pins, VACUUM. Fake data only.

These use the real Windows DPAPI and a temporary SQLite file.
"""

import hashlib
import io
import logging
import sqlite3
from collections.abc import Iterator
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from smartclip import dpapi, logsafe
from smartclip.dedupe import KEY_LENGTH, DedupeKey
from smartclip.history import History
from smartclip.labels import Label, SecretState
from smartclip.retention import RetentionPolicy
from smartclip.storage import ItemPayload, Store

NOW = datetime(2026, 11, 18, 12, 0, tzinfo=timezone.utc)
POLICY = RetentionPolicy(normal=timedelta(days=90), secret=timedelta(hours=2))
KEY = DedupeKey(b"k" * KEY_LENGTH)

FAKE_SECRET_TEXT = "FAKE-hist-Zx9!q-not-real"
FAKE_SOURCE_APP = "fakehistapp.exe"
NOTE = ItemPayload(content="FAKE history note: lunch at 1pm", label=Label.NOTE)
OTHER_NOTE = ItemPayload(content="FAKE history note: call at 4pm", label=Label.NOTE)
SECRET = ItemPayload(
    content=FAKE_SECRET_TEXT,
    label=Label.POSSIBLE_SECRET,
    source_app=FAKE_SOURCE_APP,
    secret_state=SecretState.SECRET,
)
UNSURE = ItemPayload(
    content="FAKE-unsure-Qw3rty7", label=Label.NOTE, secret_state=SecretState.UNSURE
)


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "items.db"


@pytest.fixture
def store(db_path: Path) -> Iterator[Store]:
    with Store(db_path) as opened:
        yield opened


@pytest.fixture
def history(store: Store) -> History:
    loaded = History(store, KEY, POLICY)
    loaded.load()
    return loaded


def row_count(db_path: Path) -> int:
    with closing(sqlite3.connect(db_path)) as conn:
        return conn.execute("SELECT COUNT(*) FROM items").fetchone()[0]


def plaintext_created(db_path: Path, item_id: int) -> str:
    with closing(sqlite3.connect(db_path)) as conn:
        return conn.execute("SELECT created_at FROM items WHERE id = ?", (item_id,)).fetchone()[0]


# --- Dedupe ---


def test_new_text_is_saved_with_key_and_copy_time(history: History) -> None:
    result = history.capture(NOTE, NOW)
    assert result.merged is False
    item = history.get(result.item_id)
    assert item is not None
    assert item.created_at == NOW
    assert item.payload.last_copied_at == NOW
    assert item.payload.dedupe_key == KEY.digest(NOTE.content)


def test_duplicate_is_merged_not_stored_twice(history: History, db_path: Path) -> None:
    first = history.capture(NOTE, NOW)
    again = history.capture(NOTE, NOW + timedelta(minutes=5))
    assert again == type(again)(item_id=first.item_id, merged=True)
    assert row_count(db_path) == 1
    assert len(history.items()) == 1


def test_different_text_is_not_merged(history: History, db_path: Path) -> None:
    history.capture(NOTE, NOW)
    history.capture(OTHER_NOTE, NOW)
    assert row_count(db_path) == 2


def test_recopy_moves_the_item_to_the_top(history: History, db_path: Path) -> None:
    first = history.capture(NOTE, NOW).item_id
    second = history.capture(OTHER_NOTE, NOW + timedelta(minutes=1)).item_id
    assert [i.id for i in history.items()] == [second, first]
    history.capture(NOTE, NOW + timedelta(minutes=2))
    assert [i.id for i in history.items()] == [first, second]
    # The first-copy time in the plaintext column is never rewritten.
    assert plaintext_created(db_path, first) == NOW.isoformat()


def test_merge_survives_reopening(db_path: Path) -> None:
    with Store(db_path) as first_run:
        history = History(first_run, KEY, POLICY)
        history.load()
        item_id = history.capture(NOTE, NOW).item_id
        history.capture(OTHER_NOTE, NOW + timedelta(minutes=1))
        history.capture(NOTE, NOW + timedelta(minutes=2))
    with Store(db_path) as second_run:
        reopened = History(second_run, KEY, POLICY)
        assert reopened.load() == []
        assert reopened.items()[0].id == item_id
        assert reopened.capture(NOTE, NOW + timedelta(minutes=3)).merged is True
    assert row_count(db_path) == 2


def test_version_1_rows_are_still_found_as_duplicates(store: Store, db_path: Path) -> None:
    old_id = store.add(NOTE, created_at=NOW)
    version_1 = (
        NOTE.to_bytes()
        .replace(b'"v":2', b'"v":1')
        .replace(b',"dedupe_key":null,"last_copied_at":null', b"")
    )
    with closing(sqlite3.connect(db_path)) as conn, conn:
        conn.execute("UPDATE items SET payload = ? WHERE id = ?", (dpapi.protect(version_1), old_id))
    history = History(store, KEY, POLICY)
    assert history.load() == []
    assert history.capture(NOTE, NOW + timedelta(days=1)).item_id == old_id
    assert row_count(db_path) == 1


# --- Secret timer on re-copy ---


def test_secret_recopy_keeps_the_first_deadline(history: History) -> None:
    item_id = history.capture(SECRET, NOW).item_id
    history.capture(SECRET, NOW + timedelta(hours=1, minutes=30))
    item = history.get(item_id)
    assert item is not None
    assert item.payload.expires_at == NOW + timedelta(hours=2)
    history.cleanup(NOW + timedelta(hours=2))
    assert history.get(item_id) is None


def test_unsure_recopy_keeps_the_first_deadline(history: History) -> None:
    item_id = history.capture(UNSURE, NOW).item_id
    history.capture(UNSURE, NOW + timedelta(hours=1))
    assert history.cleanup(NOW + timedelta(hours=2)) == [item_id]


def test_copy_of_an_expired_secret_starts_a_new_item(history: History, db_path: Path) -> None:
    old_id = history.capture(SECRET, NOW).item_id
    result = history.capture(SECRET, NOW + timedelta(hours=3))
    assert result.merged is False
    assert result.item_id != old_id
    new = history.get(result.item_id)
    assert new is not None and new.payload.expires_at == NOW + timedelta(hours=5)
    assert row_count(db_path) == 1


# --- Retention cleanup ---


def test_cleanup_deletes_expired_items_from_memory_and_file(
    history: History, db_path: Path
) -> None:
    note_id = history.capture(NOTE, NOW).item_id
    secret_id = history.capture(SECRET, NOW).item_id
    assert history.cleanup(NOW + timedelta(hours=1)) == []
    assert history.cleanup(NOW + timedelta(hours=2)) == [secret_id]
    assert [i.id for i in history.items()] == [note_id]
    assert history.cleanup(NOW + timedelta(days=90)) == [note_id]
    assert row_count(db_path) == 0


def test_recopy_restarts_normal_retention(history: History) -> None:
    item_id = history.capture(NOTE, NOW).item_id
    history.capture(NOTE, NOW + timedelta(days=80))
    assert history.cleanup(NOW + timedelta(days=100)) == []
    assert history.cleanup(NOW + timedelta(days=170)) == [item_id]


def test_pinned_normal_item_survives_cleanup(history: History) -> None:
    item_id = history.capture(NOTE, NOW).item_id
    assert history.pin(item_id) is True
    assert history.cleanup(NOW + timedelta(days=1000)) == []
    assert history.unpin(item_id) is True
    assert history.cleanup(NOW + timedelta(days=1000)) == [item_id]


def test_pin_persists_in_the_file(db_path: Path) -> None:
    with Store(db_path) as first_run:
        history = History(first_run, KEY, POLICY)
        history.load()
        item_id = history.capture(NOTE, NOW).item_id
        history.pin(item_id)
    with Store(db_path) as second_run:
        assert second_run.load_all().items[0].payload.pinned is True


def damage(db_path: Path, item_id: int, column: str = "payload", value: object = b"garbage") -> None:
    with closing(sqlite3.connect(db_path)) as conn, conn:
        conn.execute(f"UPDATE items SET {column} = ? WHERE id = ?", (value, item_id))


def test_corrupt_row_does_not_stop_cleanup(store: Store, db_path: Path) -> None:
    store.add(NOTE, created_at=NOW)
    bad_id = store.add(SECRET, created_at=NOW)
    damage(db_path, bad_id)
    history = History(store, KEY, POLICY)
    assert history.load() == [bad_id]
    assert history.cleanup(NOW + timedelta(hours=3)) == []
    assert row_count(db_path) == 2


def test_unreadable_row_is_deleted_after_normal_retention(store: Store, db_path: Path) -> None:
    good_id = store.add(OTHER_NOTE, created_at=NOW + timedelta(days=50))
    bad_id = store.add(SECRET, created_at=NOW)
    damage(db_path, bad_id)
    history = History(store, KEY, POLICY)
    assert history.load() == [bad_id]
    assert history.cleanup(NOW + timedelta(days=90) - timedelta(seconds=1)) == []
    assert history.cleanup(NOW + timedelta(days=90)) == [bad_id]
    assert [item.id for item in history.items()] == [good_id]
    assert row_count(db_path) == 1
    assert history.vacuum_if_due(NOW + timedelta(days=90)) is True


def test_unreadable_rows_are_kept_when_nothing_decrypts(store: Store, db_path: Path) -> None:
    first = store.add(NOTE, created_at=NOW)
    second = store.add(SECRET, created_at=NOW)
    damage(db_path, first)
    damage(db_path, second)
    history = History(store, KEY, POLICY)
    assert history.load() == [first, second]
    # New copies this run do not prove the old rows are damaged rather than locked.
    history.capture(OTHER_NOTE, NOW + timedelta(days=100))
    assert history.cleanup(NOW + timedelta(days=100)) == []
    assert row_count(db_path) == 3


def test_unreadable_row_with_bad_created_at_is_never_deleted(
    store: Store, db_path: Path
) -> None:
    store.add(OTHER_NOTE, created_at=NOW)
    bad_id = store.add(SECRET, created_at=NOW)
    damage(db_path, bad_id)
    damage(db_path, bad_id, column="created_at", value="not a time")
    history = History(store, KEY, POLICY)
    history.load()
    history.cleanup(NOW + timedelta(days=1000))
    # The readable note expired; only the row of unknown age is left.
    assert row_count(db_path) == 1
    with closing(sqlite3.connect(db_path)) as conn:
        assert conn.execute("SELECT COUNT(*) FROM items WHERE id = ?", (bad_id,)).fetchone()[0] == 1


def test_unreadable_row_cleanup_never_logs_content(store: Store, db_path: Path) -> None:
    store.add(NOTE, created_at=NOW)
    bad_id = store.add(SECRET, created_at=NOW)
    damage(db_path, bad_id, value=dpapi.protect(FAKE_SECRET_TEXT.encode("utf-8")))
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    logsafe.attach_handler(handler)
    try:
        history = History(store, KEY, POLICY)
        history.load()
        history.cleanup(NOW + timedelta(days=91))
    finally:
        logging.getLogger(logsafe.ROOT_LOGGER_NAME).removeHandler(handler)
    text = stream.getvalue()
    assert FAKE_SECRET_TEXT not in text
    assert "retention_cleanup deleted=1 unreadable_deleted=1" in text


# --- Pin rules ---


@pytest.mark.parametrize("payload", [SECRET, UNSURE], ids=["secret", "unsure"])
def test_secret_and_unsure_items_cannot_be_pinned(history: History, payload: ItemPayload) -> None:
    item_id = history.capture(payload, NOW).item_id
    assert history.pin(item_id) is False
    item = history.get(item_id)
    assert item is not None and item.payload.pinned is False


def test_pinned_item_copied_as_secret_is_unpinned(history: History) -> None:
    item_id = history.capture(NOTE, NOW).item_id
    history.pin(item_id)
    as_secret = ItemPayload(
        content=NOTE.content, label=Label.POSSIBLE_SECRET, secret_state=SecretState.SECRET
    )
    history.capture(as_secret, NOW + timedelta(days=1))
    item = history.get(item_id)
    assert item is not None
    assert item.payload.pinned is False
    assert item.payload.secret_state is SecretState.SECRET
    assert history.cleanup(NOW + timedelta(days=1, hours=2)) == [item_id]


def test_pin_of_missing_item_returns_false(history: History) -> None:
    assert history.pin(999) is False
    assert history.unpin(999) is False
    assert history.delete(999) is False


# --- Delete and VACUUM ---


def test_delete_removes_and_allows_a_fresh_copy(history: History, db_path: Path) -> None:
    item_id = history.capture(NOTE, NOW).item_id
    assert history.delete(item_id) is True
    assert history.capture(NOTE, NOW).merged is False
    assert row_count(db_path) == 1


def test_vacuum_runs_only_after_deletes_and_at_most_once_per_interval(store: Store) -> None:
    history = History(store, KEY, POLICY, vacuum_interval=timedelta(days=1))
    history.load()
    assert history.vacuum_if_due(NOW) is False
    history.delete(history.capture(NOTE, NOW).item_id)
    assert history.vacuum_if_due(NOW) is True
    history.delete(history.capture(OTHER_NOTE, NOW).item_id)
    assert history.vacuum_if_due(NOW + timedelta(hours=23)) is False
    assert history.vacuum_if_due(NOW + timedelta(days=1)) is True


def test_vacuum_keeps_remaining_items(history: History) -> None:
    keep_id = history.capture(NOTE, NOW).item_id
    history.capture(SECRET, NOW)
    history.cleanup(NOW + timedelta(hours=2))
    assert history.vacuum_if_due(NOW + timedelta(hours=2)) is True
    assert [i.id for i in history.items()] == [keep_id]


# --- What is in the file ---


def test_raw_file_has_no_content_and_no_hash(db_path: Path) -> None:
    with Store(db_path) as opened:
        history = History(opened, KEY, POLICY)
        history.load()
        history.capture(SECRET, NOW)
        history.capture(SECRET, NOW + timedelta(minutes=1))
    raw = db_path.read_bytes()
    keyed = KEY.digest(FAKE_SECRET_TEXT)
    plain = hashlib.sha256(FAKE_SECRET_TEXT.encode("utf-8"))
    forbidden = [
        FAKE_SECRET_TEXT.encode("utf-8"),
        FAKE_SECRET_TEXT.encode("utf-16-le"),
        FAKE_SOURCE_APP.encode("utf-8"),
        keyed.encode("ascii"),
        bytes.fromhex(keyed),
        plain.hexdigest().encode("ascii"),
        plain.digest(),
        b"dedupe_key",
        b"last_copied_at",
        (NOW + timedelta(minutes=1)).isoformat().encode("ascii"),
    ]
    for text in forbidden:
        assert text not in raw
    # Only the first-copy time is readable.
    assert NOW.isoformat().encode("ascii") in raw


# --- Logs ---


def test_history_flow_never_logs_content(store: Store) -> None:
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    logsafe.attach_handler(handler)
    try:
        history = History(store, KEY, POLICY)
        history.load()
        note_id = history.capture(NOTE, NOW).item_id
        secret_id = history.capture(SECRET, NOW).item_id
        history.capture(SECRET, NOW + timedelta(minutes=1))
        history.pin(secret_id)
        history.pin(note_id)
        history.cleanup(NOW + timedelta(hours=3))
        history.vacuum_if_due(NOW + timedelta(hours=3))
    finally:
        logging.getLogger(logsafe.ROOT_LOGGER_NAME).removeHandler(handler)
    text = stream.getvalue()
    for forbidden in (FAKE_SECRET_TEXT, NOTE.content, FAKE_SOURCE_APP, KEY.digest(NOTE.content)):
        assert forbidden not in text
    assert f"item_merged item_id={secret_id}" in text
    assert f"pin_refused item_id={secret_id}" in text
    assert "retention_cleanup deleted=1 unreadable_deleted=0" in text
    assert logsafe.DROPPED_RECORD_TEXT not in text
    assert "dropped_fields" not in text
