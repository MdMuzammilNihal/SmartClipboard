"""Tests for the opt-in labeled-data log (task 20). All values are fake.

These use the real Windows DPAPI and a temporary SQLite file.
"""

import io
import logging
import sqlite3
import string
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path

import pytest

from smartclip import config, datalog, dpapi, logsafe
from smartclip.datalog import LabeledLog, RecordEvent, redact
from smartclip.labels import Label, SecretState
from smartclip.storage import ItemPayload

AT = datetime(2026, 11, 19, 12, 0, tzinfo=timezone.utc)
FAKE_NOTE = "FAKE datalog note: lunch at 1pm"
FAKE_PASSWORD = "FAKE-dl-Zx9!q7Lm-not-real"
FAKE_TOKEN = "ghp_" + "Fk3" * 12  # the GitHub token shape, obviously fake


def payload(text: str, state: SecretState = SecretState.NORMAL, label: Label = Label.NOTE) -> ItemPayload:
    return ItemPayload(text, label, source_app="fakeeditor.exe", secret_state=state)


@pytest.fixture
def log_path(tmp_path: Path) -> Path:
    return tmp_path / "labeled.db"


@pytest.fixture
def labeled(log_path: Path) -> Iterator[LabeledLog]:
    with LabeledLog(log_path) as opened:
        yield opened


@pytest.fixture
def log_output() -> Iterator[io.StringIO]:
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    logsafe.attach_handler(handler)
    yield stream
    logging.getLogger(logsafe.ROOT_LOGGER_NAME).removeHandler(handler)


def char_class(char: str) -> str:
    for name, chars in (
        ("upper", string.ascii_uppercase),
        ("lower", string.ascii_lowercase),
        ("digit", string.digits),
    ):
        if char in chars:
            return name
    return "symbol" if char in string.punctuation else char


# --- Redaction ---


def test_one_word_redaction_keeps_length_and_classes() -> None:
    redacted = redact(FAKE_PASSWORD)
    assert len(redacted) == len(FAKE_PASSWORD)
    assert [char_class(c) for c in redacted] == [char_class(c) for c in FAKE_PASSWORD]
    assert redacted != FAKE_PASSWORD


def test_one_word_redaction_randomises_symbols() -> None:
    symbols = "!@#$%^&*-_.~" * 4
    redacted = redact(symbols)
    assert len(redacted) == len(symbols)
    assert all(c in string.punctuation for c in redacted)
    assert redacted != symbols  # 48 random symbols matching by chance is negligible


def test_spaces_around_one_word_are_kept() -> None:
    newline = chr(10)
    redacted = redact("  !@#$%^&*" + newline)
    assert redacted[:2] == "  " and redacted[-1] == newline
    assert all(c in string.punctuation for c in redacted[2:-1])


def test_text_with_several_words_keeps_punctuation() -> None:
    text = "FAKE pass: !@#$%^&* (old)"
    redacted = redact(text)
    assert [char_class(c) for c in redacted] == [char_class(c) for c in text]
    assert "!@#$%^&*" in redacted  # the documented limit for multi-word text


def test_redaction_keeps_spaces_and_line_breaks() -> None:
    text = "FAKE user\tpass word\nsecond line"
    redacted = redact(text)
    assert [i for i, c in enumerate(redacted) if c in " \t\n"] == [
        i for i, c in enumerate(text) if c in " \t\n"
    ]


def test_redaction_keeps_known_prefixes() -> None:
    assert redact(FAKE_TOKEN).startswith("ghp_")
    assert redact(FAKE_TOKEN)[4:] != FAKE_TOKEN[4:]
    assert redact("sk-" + "f4ke" * 6).startswith("sk-")
    assert redact("AKIA" + "FAKE" * 4).startswith("AKIA")
    assert redact("xoxb-" + "1234fake" * 2).startswith("xoxb-")
    assert redact("sk_test_" + "f4ke" * 5).startswith("sk_test_")
    assert redact("AIza" + "F" * 35).startswith("AIza")


def test_redaction_keeps_prefixes_inside_other_text() -> None:
    text = '{"token": "' + FAKE_TOKEN + '"}'
    redacted = redact(text)
    assert '": "ghp_' in redacted
    assert redacted[:2] == '{"' and redacted[-2:] == '"}'
    assert FAKE_TOKEN not in redacted


def test_redaction_keeps_jwt_part_prefixes() -> None:
    fake_jwt = "eyJ" + "a" * 10 + ".eyJ" + "b" * 10 + "." + "c" * 10
    redacted = redact(fake_jwt)
    # One word, so the dots are randomised too; both "eyJ" prefixes stay.
    assert redacted[:3] == "eyJ" and redacted[14:17] == "eyJ"
    assert len(redacted) == len(fake_jwt) and redacted != fake_jwt


def test_redaction_keeps_private_key_header_lines() -> None:
    text = "-----BEGIN RSA PRIVATE KEY-----\nFAKEfakeFAKE1234\n-----END RSA PRIVATE KEY-----"
    redacted = redact(text)
    lines = redacted.split("\n")
    assert lines[0] == "-----BEGIN RSA PRIVATE KEY-----"
    assert lines[2] == "-----END RSA PRIVATE KEY-----"
    assert lines[1] != "FAKEfakeFAKE1234"


def test_prefix_is_not_kept_in_the_middle_of_a_word() -> None:
    # "AKIA" here follows other letters, so it is not a format prefix and is
    # randomised (a random "AKIA" has a 1 in 26**4 chance).
    redacted = redact("xxAKIAFAKEFAKEFAKEFAKE")
    assert redacted[2:6] != "AKIA"
    assert len(redacted) == 22


def test_redaction_of_non_ascii_text_and_empty_text() -> None:
    assert redact("") == ""
    redacted = redact("Ünïcødé١€")
    assert len(redacted) == 9
    assert all(c in string.ascii_letters + string.digits for c in redacted[:8])
    assert redacted[8] in string.punctuation


def test_redaction_is_random_each_time() -> None:
    long_fake = "FAKE" * 20
    assert redact(long_fake) != redact(long_fake)


# --- Writing and reading ---


def test_normal_capture_is_stored_as_is(labeled: LabeledLog) -> None:
    record_id = labeled.record(RecordEvent.CAPTURED, 7, payload(FAKE_NOTE), AT)
    records, failed = labeled.load_all()
    assert failed == []
    [record] = records
    assert record.id == record_id
    assert (record.event, record.item_id, record.at) == (RecordEvent.CAPTURED, 7, AT)
    assert (record.content, record.redacted) == (FAKE_NOTE, False)
    assert (record.label, record.secret_state) == (Label.NOTE, SecretState.NORMAL)
    assert record.owner_confirmed is False
    assert record.source_app == "fakeeditor.exe"
    assert record.kind == "text"


@pytest.mark.parametrize("state", [SecretState.SECRET, SecretState.UNSURE])
def test_secret_and_unsure_items_are_redacted(labeled: LabeledLog, state: SecretState) -> None:
    labeled.record(RecordEvent.CAPTURED, 1, payload(FAKE_PASSWORD, state, Label.POSSIBLE_SECRET), AT)
    [record] = labeled.load_all()[0]
    assert record.redacted is True
    assert record.content != FAKE_PASSWORD
    assert len(record.content) == len(FAKE_PASSWORD)
    assert record.secret_state is state


def test_marking_records_are_owner_confirmed(labeled: LabeledLog) -> None:
    labeled.record(RecordEvent.MARKED_SECRET, 1, payload(FAKE_TOKEN, SecretState.SECRET), AT)
    labeled.record(RecordEvent.MARKED_NORMAL, 2, payload(FAKE_NOTE), AT)
    secret, normal = labeled.load_all()[0]
    assert secret.owner_confirmed and secret.redacted and secret.content.startswith("ghp_")
    assert secret.content != FAKE_TOKEN
    assert normal.owner_confirmed and not normal.redacted and normal.content == FAKE_NOTE


def test_only_the_id_is_plaintext_in_the_file(labeled: LabeledLog, log_path: Path) -> None:
    labeled.record(RecordEvent.CAPTURED, 1, payload(FAKE_NOTE), AT)
    labeled.record(RecordEvent.CAPTURED, 2, payload(FAKE_PASSWORD, SecretState.SECRET), AT)
    labeled.close()
    raw = log_path.read_bytes()
    for needle in (FAKE_NOTE, FAKE_PASSWORD, "fakeeditor", "2026-11-19", "captured", "note"):
        assert needle.encode() not in raw
        assert needle.encode("utf-16-le") not in raw
    with sqlite3.connect(log_path) as conn:
        columns = [row[1] for row in conn.execute("PRAGMA table_info(records)")]
    assert columns == ["id", "payload"]


def test_records_survive_reopening(log_path: Path) -> None:
    with LabeledLog(log_path) as first:
        first.record(RecordEvent.CAPTURED, 1, payload(FAKE_NOTE), AT)
    with LabeledLog(log_path) as second:
        second.record(RecordEvent.CAPTURED, 2, payload("FAKE second"), AT)
        assert [r.item_id for r in second.load_all()[0]] == [1, 2]


def test_damaged_record_is_skipped_by_id(labeled: LabeledLog, log_path: Path) -> None:
    labeled.record(RecordEvent.CAPTURED, 1, payload(FAKE_NOTE), AT)
    bad_id = labeled.record(RecordEvent.CAPTURED, 2, payload("FAKE two"), AT)
    with sqlite3.connect(log_path) as conn:
        conn.execute("UPDATE records SET payload = ? WHERE id = ?", (b"not a dpapi blob", bad_id))
    records, failed = labeled.load_all()
    assert [r.item_id for r in records] == [1]
    assert failed == [bad_id]


def test_wrong_json_inside_a_valid_blob_is_skipped(labeled: LabeledLog, log_path: Path) -> None:
    with sqlite3.connect(log_path) as conn:
        conn.execute(
            "INSERT INTO records (payload) VALUES (?)", (dpapi.protect(b'{"v": 1, "content": 5}'),)
        )
    assert labeled.load_all() == ([], [1])


def test_write_failure_returns_none_and_logs_no_content(
    labeled: LabeledLog, log_output: io.StringIO, monkeypatch: pytest.MonkeyPatch
) -> None:
    def failing_protect(_raw: bytes) -> bytes:
        raise dpapi.DpapiError("CryptProtectData failed")

    monkeypatch.setattr(datalog.dpapi, "protect", failing_protect)
    assert labeled.record(RecordEvent.CAPTURED, 1, payload(FAKE_NOTE), AT) is None
    output = log_output.getvalue()
    assert "labeled_record_failed" in output and "DpapiError" in output
    assert FAKE_NOTE not in output


def test_logs_never_hold_content(labeled: LabeledLog, log_output: io.StringIO) -> None:
    labeled.record(RecordEvent.CAPTURED, 1, payload(FAKE_NOTE), AT)
    labeled.record(RecordEvent.CAPTURED, 2, payload(FAKE_PASSWORD, SecretState.SECRET), AT)
    labeled.load_all()
    output = log_output.getvalue()
    assert "labeled_record_written" in output
    for text in (FAKE_NOTE, FAKE_PASSWORD, "fakeeditor"):
        assert text not in output


def test_labeled_log_is_off_by_default() -> None:
    assert config.LABELED_LOG_ENABLED is False
    assert config.LABELED_LOG_FILE_NAME.endswith(".db")  # covered by .gitignore's *.db
