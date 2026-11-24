"""Names and tags for pinned items: rules, storage format, migration (UI plan item 9). Fake data only.

These use the real Windows DPAPI and a temporary SQLite file.
"""

import json
import sqlite3
from collections.abc import Iterator
from contextlib import closing
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from smartclip import dpapi, naming, overrides, retention
from smartclip.labels import Label, SecretState
from smartclip.naming import MAX_TAG_CHARS, MAX_TAGS, MAX_TITLE_CHARS
from smartclip.storage import ItemPayload, PayloadError, Store

NOW = datetime(2026, 11, 25, 12, 0, tzinfo=timezone.utc)
FAKE_TITLE = "FAKE-title-Standup-link"
FAKE_TAG = "FAKE-tag-work"


def named(**changes: object) -> ItemPayload:
    base = ItemPayload("FAKE named text", Label.NOTE, pinned=True, title=FAKE_TITLE, tags=(FAKE_TAG,))
    return replace(base, **changes)  # type: ignore[arg-type]


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "items.db"


@pytest.fixture
def store(db_path: Path) -> Iterator[Store]:
    with Store(db_path) as opened:
        yield opened


def raw_blob(db_path: Path, item_id: int) -> bytes:
    with closing(sqlite3.connect(db_path)) as conn:
        return conn.execute("SELECT payload FROM items WHERE id = ?", (item_id,)).fetchone()[0]


def write_blob(db_path: Path, item_id: int, plain: bytes) -> None:
    with closing(sqlite3.connect(db_path)) as conn, conn:
        conn.execute("UPDATE items SET payload = ? WHERE id = ?", (dpapi.protect(plain), item_id))


# --- Tidying ---


def test_titles_are_tidied_and_cut() -> None:
    assert naming.clean_title("  Daily \n  standup   ") == "Daily standup"
    assert naming.clean_title("   ") is None and naming.clean_title("") is None
    assert len(naming.clean_title("x" * 200) or "") == MAX_TITLE_CHARS


def test_tags_are_tidied_and_commas_do_not_survive() -> None:
    assert naming.clean_tag("  work ,  home ") == "work home"
    assert naming.clean_tag(",,,") is None
    assert len(naming.clean_tag("y" * 99) or "") == MAX_TAG_CHARS


def test_add_tag_respects_the_limits() -> None:
    tags: tuple[str, ...] = ()
    for text in ("one", "ONE", "two", "", "three", "four"):
        tags = naming.add_tag(tags, text)
    assert tags == ("one", "two", "three") and len(tags) == MAX_TAGS
    assert naming.remove_tag(tags, "TWO") == ("one", "three")


def test_validity_checks_match_the_tidying() -> None:
    assert naming.title_is_valid(None) and naming.title_is_valid("Good title")
    assert not naming.title_is_valid("  padded ") and not naming.title_is_valid("x" * 61)
    assert naming.tags_are_valid(("a", "b")) and naming.tags_are_valid(())
    assert not naming.tags_are_valid(("a", "A")) and not naming.tags_are_valid(("a", "b", "c", "d"))
    assert not naming.tags_are_valid((" a",)) and not naming.tags_are_valid(("",))


# --- The payload ---


def test_a_named_payload_round_trips_through_bytes() -> None:
    again = ItemPayload.from_bytes(named().to_bytes())
    assert again.title == FAKE_TITLE and again.tags == (FAKE_TAG,) and again == named()


def test_an_unnamed_payload_is_still_written_as_version_2() -> None:
    data = json.loads(ItemPayload("FAKE plain", Label.NOTE, pinned=True).to_bytes())
    assert data["v"] == 2 and "title" not in data and "tags" not in data


def test_a_named_payload_is_written_as_version_3() -> None:
    for payload in (named(), named(title=None), named(tags=())):
        data = json.loads(payload.to_bytes())
        assert data["v"] == 3 and set(("title", "tags")) <= set(data)


@pytest.mark.parametrize("state", [SecretState.SECRET, SecretState.UNSURE])
def test_a_secret_or_unsure_payload_cannot_have_a_title_or_tags(state: SecretState) -> None:
    for changes in ({"title": "Why not"}, {"tags": ("tag",)}):
        with pytest.raises(ValueError):
            ItemPayload("FAKE", Label.NOTE, secret_state=state, **changes)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "changes",
    [{"title": "x" * 61}, {"title": "  padded"}, {"tags": ("a", "b", "c", "d")}, {"tags": ("A", "a")}, {"tags": ("",)}],
)
def test_untidy_or_oversized_names_are_refused(changes: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        ItemPayload("FAKE", Label.NOTE, pinned=True, **changes)  # type: ignore[arg-type]


# --- The file ---


def test_names_survive_a_restart_and_are_not_in_the_file_as_plain_text(store: Store, db_path: Path) -> None:
    item_id = store.add(named(), created_at=NOW)
    loaded = store.load_all().items[0]
    assert loaded.payload.title == FAKE_TITLE and loaded.payload.tags == (FAKE_TAG,)
    blob = raw_blob(db_path, item_id)
    assert FAKE_TITLE.encode() not in blob and FAKE_TAG.encode() not in blob
    store.close()
    assert FAKE_TITLE.encode() not in db_path.read_bytes() and FAKE_TAG.encode() not in db_path.read_bytes()
    with Store(db_path) as again:
        assert again.load_all().items[0].payload.title == FAKE_TITLE


def test_old_version_1_and_2_rows_open_unchanged_with_no_names(store: Store, db_path: Path) -> None:
    first = store.add(ItemPayload("FAKE old one", Label.NOTE, pinned=True), created_at=NOW)
    second = store.add(ItemPayload("FAKE old two", Label.LINK), created_at=NOW)
    assert b'"v":2' in dpapi.unprotect(raw_blob(db_path, second))
    v1 = dpapi.unprotect(raw_blob(db_path, first)).replace(b'"v":2', b'"v":1').replace(b',"dedupe_key":null', b"").replace(b',"last_copied_at":null', b"")
    write_blob(db_path, first, v1)
    items = {item.id: item for item in store.load_all().items}
    assert store.load_all().failed_ids == []
    for item in items.values():
        assert item.payload.title is None and item.payload.tags == ()
    assert items[first].payload.pinned is True and items[first].payload.content == "FAKE old one"
    assert items[second].payload.content == "FAKE old two"


def test_an_old_row_is_not_rewritten_in_a_new_format_by_loading(store: Store, db_path: Path) -> None:
    item_id = store.add(ItemPayload("FAKE untouched", Label.NOTE), created_at=NOW)
    before = dpapi.unprotect(raw_blob(db_path, item_id))
    store.load_all()
    assert dpapi.unprotect(raw_blob(db_path, item_id)) == before and b'"v":2' in before


def test_a_row_that_titles_a_secret_is_unreadable_not_loaded(store: Store, db_path: Path) -> None:
    item_id = store.add(ItemPayload("FAKE s", Label.POSSIBLE_SECRET), created_at=NOW)
    tampered = json.loads(dpapi.unprotect(raw_blob(db_path, item_id)))
    tampered.update({"v": 3, "secret_state": "secret", "title": "reveals it", "tags": []})
    write_blob(db_path, item_id, json.dumps(tampered).encode())
    result = store.load_all()
    assert result.items == [] and result.failed_ids == [item_id]


@pytest.mark.parametrize(
    "extra",
    [{"title": 5, "tags": []}, {"title": "ok", "tags": "nope"}, {"title": "ok", "tags": [1]}, {"tags": []}],
)
def test_malformed_name_fields_fail_cleanly(store: Store, db_path: Path, extra: dict[str, object]) -> None:
    item_id = store.add(ItemPayload("FAKE", Label.NOTE, pinned=True), created_at=NOW)
    data = json.loads(dpapi.unprotect(raw_blob(db_path, item_id)))
    data["v"] = 3
    data.update(extra)
    if "title" not in extra:
        data.pop("title", None)
    write_blob(db_path, item_id, json.dumps(data).encode())
    assert store.load_all().failed_ids == [item_id]
    with pytest.raises(PayloadError):
        ItemPayload.from_bytes(json.dumps(data).encode())


# --- Marking secret clears names; merging keeps them ---


def test_marking_an_item_secret_clears_its_names() -> None:
    marked = overrides.mark_secret(named(), NOW)
    assert marked.secret_state is SecretState.SECRET and marked.pinned is False
    assert marked.title is None and marked.tags == ()


def test_a_recopy_that_looks_more_sensitive_clears_names_instead_of_crashing() -> None:
    policy = retention.RetentionPolicy()
    incoming = ItemPayload("FAKE named text", Label.POSSIBLE_SECRET, secret_state=SecretState.SECRET)
    merged = retention.merge_recopy(named(), incoming, NOW, policy)
    assert merged.secret_state is SecretState.SECRET and merged.title is None and merged.tags == ()


def test_a_plain_recopy_keeps_the_names() -> None:
    incoming = ItemPayload("FAKE named text", Label.NOTE)
    merged = retention.merge_recopy(named(), incoming, NOW + timedelta(days=1), retention.RetentionPolicy())
    assert merged.title == FAKE_TITLE and merged.tags == (FAKE_TAG,) and merged.pinned is True


def test_unpinning_keeps_the_names_and_pinning_is_still_refused_for_secrets() -> None:
    assert retention.unpin(named()).title == FAKE_TITLE
    with pytest.raises(retention.PinRefused):
        retention.pin(ItemPayload("FAKE", Label.NOTE, secret_state=SecretState.UNSURE))


# --- Other places that handle payloads ---


def test_the_labeled_log_never_records_titles_or_tags(tmp_path: Path) -> None:
    from smartclip.datalog import LabeledLog, RecordEvent

    with LabeledLog(tmp_path / "labeled.db") as log:
        log.record(RecordEvent.MARKED_NORMAL, 1, named(), NOW)
        records, failed = log.load_all()
    assert failed == [] and len(records) == 1
    assert FAKE_TITLE not in repr(records) and FAKE_TAG not in repr(records)


def test_undo_brings_back_a_named_item_with_its_names(tmp_path: Path) -> None:
    from smartclip.dedupe import KEY_LENGTH, DedupeKey
    from smartclip.history import History

    with Store(tmp_path / "items.db") as opened:
        history = History(opened, DedupeKey(b"n" * KEY_LENGTH), retention.RetentionPolicy())
        history.load()
        item_id = history.capture(named(), NOW).item_id
        kept = history.get(item_id)
        assert kept is not None
        history.delete(item_id)
        [new_id] = history.restore([kept], NOW)
        back = history.get(new_id)
    assert back is not None and back.payload.title == FAKE_TITLE and back.payload.tags == (FAKE_TAG,)
