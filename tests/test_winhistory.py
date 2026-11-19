"""Tests for removing secrets from Win+V history (option C). All values are fake.

Most tests use a fake history. The test at the end touches the real Win+V
history and clipboard: it is skipped unless SMARTCLIP_REAL_CLIPBOARD_TESTS=1,
because it REPLACES whatever is on the clipboard with fake text.
"""

import io
import logging
import os
import time
from collections.abc import Iterator

import pytest

from smartclip import logsafe
from smartclip.dedupe import KEY_LENGTH, DedupeKey
from smartclip.winhistory import HistoryCleaner, remove_matching

KEY = DedupeKey(b"w" * KEY_LENGTH)
FAKE_SECRET = "FAKE-winhistory-Zx9!q-not-real"
FAKE_NOTE = "FAKE winhistory note"


class FakeHistory:
    """Win+V history as a list of (entry id, text)."""

    def __init__(self, texts: list[str], enabled: bool = True) -> None:
        self.entries = list(enumerate(texts))
        self.enabled = enabled
        self.listed = 0

    def is_enabled(self) -> bool:
        return self.enabled

    def text_entries(self) -> list[tuple[object, str]]:
        self.listed += 1
        return list(self.entries)

    def delete(self, entry: object) -> bool:
        before = len(self.entries)
        self.entries = [e for e in self.entries if e[0] != entry]
        return len(self.entries) < before

    def texts(self) -> list[str]:
        return [text for _id, text in self.entries]


class BrokenHistory(FakeHistory):
    def text_entries(self) -> list[tuple[object, str]]:
        raise RuntimeError(FAKE_SECRET)  # a message that must never be logged


@pytest.fixture
def log_output() -> Iterator[io.StringIO]:
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    logsafe.attach_handler(handler)
    yield stream
    logging.getLogger(logsafe.ROOT_LOGGER_NAME).removeHandler(handler)


def test_matching_entries_are_deleted_and_others_kept() -> None:
    history = FakeHistory([FAKE_NOTE, FAKE_SECRET, "FAKE other", FAKE_SECRET])
    removed = remove_matching(history, KEY.digest, KEY.digest(FAKE_SECRET))
    assert removed == 2
    assert history.texts() == [FAKE_NOTE, "FAKE other"]


def test_near_matches_are_kept() -> None:
    history = FakeHistory([FAKE_SECRET + " ", FAKE_SECRET.upper()])
    assert remove_matching(history, KEY.digest, KEY.digest(FAKE_SECRET)) == 0
    assert len(history.entries) == 2


def test_history_turned_off_is_not_read() -> None:
    history = FakeHistory([FAKE_SECRET], enabled=False)
    assert remove_matching(history, KEY.digest, KEY.digest(FAKE_SECRET)) == 0
    assert history.listed == 0


def test_cleaner_runs_on_a_worker_thread_and_logs_counts_only(log_output: io.StringIO) -> None:
    history = FakeHistory([FAKE_SECRET, FAKE_NOTE])
    cleaner = HistoryCleaner(KEY.digest, api=history)
    try:
        assert cleaner.submit(KEY.digest(FAKE_SECRET)).result(timeout=3) == 1
    finally:
        cleaner.shutdown()
    text = log_output.getvalue()
    assert "win_history_removed entries=1" in text
    assert FAKE_SECRET not in text and FAKE_NOTE not in text


def test_cleaner_failure_is_logged_by_class_only(log_output: io.StringIO) -> None:
    cleaner = HistoryCleaner(KEY.digest, api=BrokenHistory([FAKE_SECRET]))
    try:
        assert cleaner.submit(KEY.digest(FAKE_SECRET)).result(timeout=3) == 0
    finally:
        cleaner.shutdown()
    text = log_output.getvalue()
    assert "win_history_remove_failed error_type=RuntimeError" in text
    assert FAKE_SECRET not in text


def test_shutdown_finishes_queued_removals() -> None:
    history = FakeHistory([FAKE_SECRET])
    cleaner = HistoryCleaner(KEY.digest, api=history)
    future = cleaner.submit(KEY.digest(FAKE_SECRET))
    cleaner.shutdown()
    assert future.result() == 1


# --- Real Win+V history (opt-in) ---


@pytest.mark.skipif(
    os.environ.get("SMARTCLIP_REAL_CLIPBOARD_TESTS") != "1",
    reason="replaces the real clipboard; set SMARTCLIP_REAL_CLIPBOARD_TESTS=1 to run",
)
def test_real_history_entry_is_removed() -> None:
    import win32clipboard
    import win32con

    from smartclip.winhistory import WinRtHistoryApi

    api = WinRtHistoryApi()
    if not api.is_enabled():
        pytest.skip("Windows clipboard history is turned off")
    fake = f"FAKE-winhistory-real-{int(time.time())}"
    win32clipboard.OpenClipboard(None)
    try:
        win32clipboard.EmptyClipboard()
        win32clipboard.SetClipboardData(win32con.CF_UNICODETEXT, fake)
    finally:
        win32clipboard.CloseClipboard()
    time.sleep(1.5)  # Windows records the entry
    assert fake in [text for _entry, text in api.text_entries()]
    assert remove_matching(api, KEY.digest, KEY.digest(fake)) == 1
    assert fake not in [text for _entry, text in api.text_entries()]
