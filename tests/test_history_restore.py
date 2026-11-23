"""History.restore and AppBackend.restore: the data side of Undo. Fake data only.

These use the real Windows DPAPI and a temporary SQLite file.
"""

import io
import logging
import sqlite3
from collections.abc import Iterator
from contextlib import closing
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from smartclip import logsafe
from smartclip.dedupe import KEY_LENGTH, DedupeKey
from smartclip.history import History
from smartclip.labels import Label, SecretState
from smartclip.retention import RetentionPolicy
from smartclip.storage import ItemPayload, Store

NOW = datetime(2026, 11, 24, 12, 0, tzinfo=timezone.utc)
POLICY = RetentionPolicy(normal=timedelta(days=90), secret=timedelta(hours=2))
KEY = DedupeKey(b"r" * KEY_LENGTH)
FAKE_SECRET_TEXT = "FAKE-restore-Zx9!q-not-real"


def note(text: str, **changes: object) -> ItemPayload:
    return replace(ItemPayload(content=text, label=Label.NOTE), **changes)  # type: ignore[arg-type]


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


def rows_in_file(db_path: Path) -> int:
    with closing(sqlite3.connect(db_path)) as conn:
        return conn.execute("SELECT COUNT(*) FROM items").fetchone()[0]


def texts(history: History) -> list[str]:
    return [item.payload.content for item in history.items()]


def test_a_deleted_item_comes_back_with_its_time_and_pin(history: History, db_path: Path) -> None:
    first = history.capture(note("FAKE one"), NOW - timedelta(hours=3)).item_id
    history.pin(first)
    kept = history.get(first)
    assert kept is not None
    history.delete(first)
    assert history.get(first) is None and rows_in_file(db_path) == 0
    [new_id] = history.restore([kept], NOW)
    back = history.get(new_id)
    assert back is not None
    assert back.created_at == kept.created_at
    assert back.payload.pinned is True
    assert back.payload.content == "FAKE one"
    assert rows_in_file(db_path) == 1


def test_a_batch_comes_back_in_its_old_order(history: History) -> None:
    ids = [history.capture(note(f"FAKE {n}"), NOW - timedelta(minutes=60 - n)).item_id for n in range(5)]
    before = texts(history)
    kept = [item for item in history.items()]
    for item_id in ids:
        history.delete(item_id)
    assert texts(history) == []
    history.restore(kept, NOW)
    assert texts(history) == before


def test_restore_does_not_depend_on_the_order_it_is_given(history: History) -> None:
    for n in range(4):
        history.capture(note(f"FAKE {n}"), NOW - timedelta(minutes=10 - n))
    before = texts(history)
    kept = list(reversed(history.items()))
    for item in kept:
        history.delete(item.id)
    history.restore(kept, NOW)
    assert texts(history) == before


def test_an_expired_secret_is_not_restored(history: History, db_path: Path) -> None:
    secret = note(FAKE_SECRET_TEXT, secret_state=SecretState.SECRET)
    item_id = history.capture(secret, NOW).item_id
    kept = history.get(item_id)
    assert kept is not None
    history.delete(item_id)
    assert history.restore([kept], NOW + timedelta(hours=2, seconds=1)) == []
    assert history.items() == [] and rows_in_file(db_path) == 0


def test_a_secret_inside_its_time_is_restored_and_keeps_its_deadline(history: History) -> None:
    item_id = history.capture(note(FAKE_SECRET_TEXT, secret_state=SecretState.SECRET), NOW).item_id
    kept = history.get(item_id)
    assert kept is not None
    history.delete(item_id)
    [new_id] = history.restore([kept], NOW + timedelta(minutes=1))
    back = history.get(new_id)
    assert back is not None
    assert back.payload.expires_at == kept.payload.expires_at  # undo does not extend it


def test_an_item_copied_again_meanwhile_is_not_duplicated(history: History) -> None:
    item_id = history.capture(note("FAKE same text"), NOW - timedelta(hours=1)).item_id
    kept = history.get(item_id)
    assert kept is not None
    history.delete(item_id)
    history.capture(note("FAKE same text"), NOW)  # the user copied it again
    assert history.restore([kept], NOW) == []
    assert texts(history) == ["FAKE same text"]


def test_a_restored_item_still_merges_with_a_new_copy(history: History) -> None:
    item_id = history.capture(note("FAKE merge me"), NOW - timedelta(hours=1)).item_id
    kept = history.get(item_id)
    assert kept is not None
    history.delete(item_id)
    [new_id] = history.restore([kept], NOW)
    result = history.capture(note("FAKE merge me"), NOW)
    assert result.merged is True and result.item_id == new_id
    assert len(history.items()) == 1


def test_restore_of_nothing_does_nothing(history: History) -> None:
    assert history.restore([], NOW) == []


def test_restored_rows_survive_a_reload(store: Store) -> None:
    first = History(store, KEY, POLICY)
    first.load()
    item_id = first.capture(note("FAKE persists", pinned=True), NOW).item_id
    kept = first.get(item_id)
    assert kept is not None
    first.delete(item_id)
    first.restore([kept], NOW)
    second = History(store, KEY, POLICY)
    second.load()
    assert texts(second) == ["FAKE persists"]
    assert second.items()[0].payload.pinned is True


def test_restore_logs_counts_only(history: History) -> None:
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    logsafe.attach_handler(handler)
    try:
        item_id = history.capture(note(FAKE_SECRET_TEXT), NOW).item_id
        kept = history.get(item_id)
        assert kept is not None
        history.delete(item_id)
        history.restore([kept], NOW)
    finally:
        logging.getLogger(logsafe.ROOT_LOGGER_NAME).removeHandler(handler)
    assert FAKE_SECRET_TEXT not in stream.getvalue()
    assert "items_restored" in stream.getvalue()
