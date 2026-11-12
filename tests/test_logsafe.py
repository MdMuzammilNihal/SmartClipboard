"""Tests for safe logging. All values are fake."""

import io
import logging
import sys
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest

from smartclip import logsafe
from smartclip.labels import Label

# Stands in for clipboard content. It must never appear in any log output.
FAKE_CONTENT = "FAKE-clipboard-text-do-not-log-12345"


@pytest.fixture
def log_output() -> Iterator[io.StringIO]:
    """Capture everything the smartclip loggers write."""
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    logsafe.attach_handler(handler)
    yield stream
    logging.getLogger(logsafe.ROOT_LOGGER_NAME).removeHandler(handler)


def test_event_with_safe_fields_is_written(log_output: io.StringIO) -> None:
    logger = logsafe.get_logger("test")
    logsafe.log_event(logger, "item_saved", item_id=7, length=36, content_type=Label.LINK)
    text = log_output.getvalue()
    assert "INFO smartclip.test item_saved item_id=7 length=36 content_type=link" in text


def test_booleans_and_none_are_written(log_output: io.StringIO) -> None:
    logsafe.log_event(logsafe.get_logger("test"), "checked", found=True, extra=None)
    assert "checked found=true extra=none" in log_output.getvalue()


def test_string_field_is_dropped(log_output: io.StringIO) -> None:
    logsafe.log_event(logsafe.get_logger("test"), "item_saved", preview=FAKE_CONTENT)
    text = log_output.getvalue()
    assert FAKE_CONTENT not in text
    assert "item_saved dropped_fields=1" in text


def test_content_type_must_be_a_label(log_output: io.StringIO) -> None:
    logsafe.log_event(logsafe.get_logger("test"), "item_saved", content_type=FAKE_CONTENT)
    text = log_output.getvalue()
    assert FAKE_CONTENT not in text
    assert "dropped_fields=1" in text


def test_label_under_another_field_name_is_dropped(log_output: io.StringIO) -> None:
    logsafe.log_event(logsafe.get_logger("test"), "item_saved", other=Label.NOTE)
    assert "dropped_fields=1" in log_output.getvalue()


def test_content_passed_as_event_name_is_replaced(log_output: io.StringIO) -> None:
    logsafe.log_event(logsafe.get_logger("test"), FAKE_CONTENT, item_id=1)
    text = log_output.getvalue()
    assert FAKE_CONTENT not in text
    assert f"{logsafe.INVALID_EVENT_TEXT} item_id=1" in text


def test_ordinary_logging_call_is_discarded(log_output: io.StringIO) -> None:
    logsafe.get_logger("test").info("copied %s", FAKE_CONTENT)
    text = log_output.getvalue()
    assert FAKE_CONTENT not in text
    assert logsafe.DROPPED_RECORD_TEXT in text


def test_logger_exception_does_not_write_message_or_traceback(
    log_output: io.StringIO,
) -> None:
    try:
        raise ValueError(FAKE_CONTENT)
    except ValueError:
        logsafe.get_logger("test").exception("failed on %s", FAKE_CONTENT)
    text = log_output.getvalue()
    assert FAKE_CONTENT not in text
    assert "Traceback" not in text


def test_log_error_writes_class_name_only(log_output: io.StringIO) -> None:
    logsafe.log_error(
        logsafe.get_logger("test"), "save_failed", ValueError(FAKE_CONTENT), item_id=3
    )
    text = log_output.getvalue()
    assert FAKE_CONTENT not in text
    assert "ERROR smartclip.test save_failed error_type=ValueError item_id=3" in text


def test_crash_handler_logs_type_and_location_only(
    log_output: io.StringIO, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Registering the current hooks with monkeypatch restores them afterwards.
    monkeypatch.setattr(sys, "excepthook", sys.excepthook)
    monkeypatch.setattr(threading, "excepthook", threading.excepthook)
    logsafe.install_crash_handlers()

    def fake_work(local_value: str) -> None:
        raise RuntimeError(local_value)

    try:
        fake_work(FAKE_CONTENT)
    except RuntimeError:
        sys.excepthook(*sys.exc_info())

    text = log_output.getvalue()
    assert FAKE_CONTENT not in text
    assert "CRITICAL smartclip.crash unhandled_exception error_type=RuntimeError" in text
    assert "test_logsafe.py:" in text
    assert "fake_work" in text


def test_crash_in_another_thread_is_logged_without_content(
    log_output: io.StringIO, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "excepthook", sys.excepthook)
    monkeypatch.setattr(threading, "excepthook", threading.excepthook)
    logsafe.install_crash_handlers()

    def fake_thread_work() -> None:
        raise RuntimeError(FAKE_CONTENT)

    thread = threading.Thread(target=fake_thread_work)
    thread.start()
    thread.join()

    text = log_output.getvalue()
    assert FAKE_CONTENT not in text
    assert "unhandled_exception error_type=RuntimeError" in text
    assert "fake_thread_work" in text


def test_setup_logging_writes_safe_lines_to_a_file(tmp_path: Path) -> None:
    handler = logsafe.setup_logging(tmp_path / "logs")
    try:
        logger = logsafe.get_logger("test")
        logsafe.log_event(logger, "item_saved", item_id=1, preview=FAKE_CONTENT)
        logger.warning("copied %s", FAKE_CONTENT)
    finally:
        logging.getLogger(logsafe.ROOT_LOGGER_NAME).removeHandler(handler)
        handler.close()

    text = (tmp_path / "logs" / logsafe.LOG_FILE_NAME).read_text(encoding="utf-8")
    assert FAKE_CONTENT not in text
    assert "item_saved item_id=1 dropped_fields=1" in text
    assert logsafe.DROPPED_RECORD_TEXT in text
