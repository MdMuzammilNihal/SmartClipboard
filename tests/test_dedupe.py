"""Tests for the duplicate keyed hash. All content is fake.

These use the real Windows DPAPI, so they only run on Windows.
"""

import hashlib
import io
import logging
from pathlib import Path

import pytest

from smartclip import config, dpapi, logsafe
from smartclip.dedupe import KEY_LENGTH, DedupeKey, DedupeKeyError

FAKE_TEXT = "FAKE-dedupe-Zx9!q-not-real"


def test_key_is_created_once_and_reused(tmp_path: Path) -> None:
    path = tmp_path / "app" / "dedupe.key"
    first = DedupeKey.load_or_create(path)
    second = DedupeKey.load_or_create(path)
    assert path.exists()
    assert first.digest(FAKE_TEXT) == second.digest(FAKE_TEXT)


def test_key_file_is_dpapi_protected(tmp_path: Path) -> None:
    path = tmp_path / "dedupe.key"
    DedupeKey.load_or_create(path)
    raw = path.read_bytes()
    assert len(raw) > KEY_LENGTH
    assert len(dpapi.unprotect(raw)) == KEY_LENGTH


def test_no_temporary_file_is_left(tmp_path: Path) -> None:
    DedupeKey.load_or_create(tmp_path / "dedupe.key")
    assert [p.name for p in tmp_path.iterdir()] == ["dedupe.key"]


def test_digest_is_keyed_not_a_plain_hash() -> None:
    key_a = DedupeKey(b"a" * KEY_LENGTH)
    key_b = DedupeKey(b"b" * KEY_LENGTH)
    plain = hashlib.sha256(FAKE_TEXT.encode("utf-8")).hexdigest()
    assert key_a.digest(FAKE_TEXT) != key_b.digest(FAKE_TEXT)
    assert key_a.digest(FAKE_TEXT) != plain
    assert len(key_a.digest(FAKE_TEXT)) == 64


def test_digest_tells_different_text_apart() -> None:
    key = DedupeKey(b"a" * KEY_LENGTH)
    assert key.digest(FAKE_TEXT) == key.digest(FAKE_TEXT)
    assert key.digest(FAKE_TEXT) != key.digest(FAKE_TEXT + " ")


@pytest.mark.parametrize("content", [b"garbage, not a DPAPI blob", b""])
def test_unreadable_key_file_raises_and_is_not_replaced(tmp_path: Path, content: bytes) -> None:
    path = tmp_path / "dedupe.key"
    path.write_bytes(content)
    with pytest.raises(DedupeKeyError) as caught:
        DedupeKey.load_or_create(path)
    assert caught.value.__context__ is None
    assert path.read_bytes() == content


def test_key_of_wrong_length_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "dedupe.key"
    path.write_bytes(dpapi.protect(b"short"))
    with pytest.raises(DedupeKeyError):
        DedupeKey.load_or_create(path)


def test_repr_hides_the_key() -> None:
    key_bytes = b"Q" * KEY_LENGTH
    assert repr(key_bytes) not in repr(DedupeKey(key_bytes))
    assert "QQQQ" not in repr(DedupeKey(key_bytes))


def test_key_path_is_under_local_app_data(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    assert config.dedupe_key_path() == tmp_path / "SmartClipboard" / "dedupe.key"


def test_key_handling_never_logs_the_key(tmp_path: Path) -> None:
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    logsafe.attach_handler(handler)
    try:
        key = DedupeKey.load_or_create(tmp_path / "dedupe.key")
        key.digest(FAKE_TEXT)
        (tmp_path / "bad.key").write_bytes(b"garbage")
        with pytest.raises(DedupeKeyError):
            DedupeKey.load_or_create(tmp_path / "bad.key")
    finally:
        logging.getLogger(logsafe.ROOT_LOGGER_NAME).removeHandler(handler)
    text = stream.getvalue()
    assert "dedupe_key_created" in text
    assert "dedupe_key_unreadable" in text
    assert FAKE_TEXT not in text
    assert key.digest(FAKE_TEXT) not in text
