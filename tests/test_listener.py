"""Tests for the clipboard listener. All values are fake.

Most tests use a fake clipboard, so the owner's real clipboard is neither read
nor changed. Some start the real listener thread and send it real window
messages, still with the fake clipboard behind it.

One test touches the real clipboard. It is skipped unless the environment
variable SMARTCLIP_REAL_CLIPBOARD_TESTS is set to 1, because it REPLACES
whatever is currently on the clipboard with fake text and then empties it.
"""

import io
import logging
import os
import queue
import time
from collections.abc import Callable, Iterator
from typing import Any

import pytest
import win32clipboard
import win32con
import win32gui

from smartclip import logsafe
from smartclip.listener import (
    OPEN_ATTEMPTS,
    WM_CLIPBOARDUPDATE,
    ClipboardBusyError,
    ClipboardEvent,
    ClipboardListener,
    ClipboardReader,
)

FAKE_TEXT = "FAKE-copied-text-for-listener-tests-777"


class FakeClipboard:
    """A scripted stand-in for the Windows clipboard."""

    def __init__(
        self,
        text: str | None = FAKE_TEXT,
        sequence: int = 1,
        busy_times: int = 0,
        new_sequence_on_open: bool = False,
        fail_get_text: bool = False,
    ) -> None:
        self.text = text
        self.sequence = sequence
        self.busy_times = busy_times
        self.new_sequence_on_open = new_sequence_on_open
        self.fail_get_text = fail_get_text
        self.open_attempts = 0
        self.opens = 0
        self.closes = 0

    def open(self, window: int | None = None) -> None:
        self.open_attempts += 1
        if self.busy_times > 0:
            self.busy_times -= 1
            raise ClipboardBusyError
        self.opens += 1
        if self.new_sequence_on_open:
            self.sequence += 1

    def close(self) -> None:
        self.closes += 1

    def get_text(self) -> str | None:
        if self.fail_get_text:
            raise RuntimeError(FAKE_TEXT)
        return self.text

    def sequence_number(self) -> int:
        return self.sequence

    # The listener's gate checks these; this fake never carries opt-out formats.
    def register_format(self, name: str) -> int:
        return 0xC000 + len(name)

    def has_format(self, _format_id: int) -> bool:
        return False

    def get_format_bytes(self, _format_id: int) -> bytes | None:
        return None


class NoWindows:
    """Window lookups that find nothing, so tests never read real windows."""

    def clipboard_owner(self) -> int | None:
        return None

    def foreground_window(self) -> int | None:
        return None

    def process_id(self, _window: int) -> int | None:
        return None

    def executable_name(self, _process_id: int) -> str | None:
        return None


def make_reader(api: FakeClipboard, **options: Any) -> ClipboardReader:
    """A reader with no gate settings and no real window lookups."""
    return ClipboardReader(api, windows=NoWindows(), **options)


def no_sleep(_seconds: float) -> None:
    """Stands in for time.sleep so retry tests run instantly."""


def wait_until(condition: Callable[[], bool], timeout: float = 3.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.01)
    return condition()


@pytest.fixture
def log_output() -> Iterator[io.StringIO]:
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    logsafe.attach_handler(handler)
    yield stream
    logging.getLogger(logsafe.ROOT_LOGGER_NAME).removeHandler(handler)


# --- Reading: text only, once per change ---


def test_text_is_captured_with_its_sequence_number() -> None:
    reader = make_reader(FakeClipboard(sequence=41))
    event = reader.read_new()
    assert event is not None
    assert event.text == FAKE_TEXT
    assert event.sequence_number == 41
    assert event.captured_at.tzinfo is not None


def test_same_change_is_not_captured_twice() -> None:
    reader = make_reader(FakeClipboard())
    assert reader.read_new() is not None
    assert reader.read_new() is None


def test_a_new_change_is_captured() -> None:
    api = FakeClipboard(sequence=1)
    reader = make_reader(api)
    reader.read_new()
    api.sequence = 2
    api.text = "FAKE second copy"
    event = reader.read_new()
    assert event is not None and event.text == "FAKE second copy"


def test_copying_the_same_text_again_is_a_new_event() -> None:
    # Merging duplicates is the storage layer's job, not the listener's.
    api = FakeClipboard(sequence=1)
    reader = make_reader(api)
    reader.read_new()
    api.sequence = 2
    assert reader.read_new() is not None


@pytest.mark.parametrize("non_text", [None, ""])
def test_non_text_content_is_ignored(non_text: str | None) -> None:
    api = FakeClipboard(text=non_text)
    reader = make_reader(api)
    assert reader.read_new() is None
    # The change is remembered, so it is not read again.
    api.text = FAKE_TEXT
    assert reader.read_new() is None


# --- Retry when the clipboard is busy ---


def test_busy_clipboard_is_retried_until_it_opens() -> None:
    waits: list[float] = []
    api = FakeClipboard(busy_times=2)
    reader = make_reader(api, delay_seconds=0.02, sleep=waits.append)
    assert reader.read_new() is not None
    assert api.open_attempts == 3
    assert waits == [0.02, 0.04]


def test_gives_up_quietly_when_clipboard_stays_busy() -> None:
    api = FakeClipboard(busy_times=100)
    reader = make_reader(api, sleep=no_sleep)
    assert reader.read_new() is None
    assert api.open_attempts == OPEN_ATTEMPTS
    assert api.closes == 0


def test_item_is_still_captured_after_a_failed_read() -> None:
    api = FakeClipboard(busy_times=OPEN_ATTEMPTS)
    reader = make_reader(api, sleep=no_sleep)
    assert reader.read_new() is None
    assert reader.read_new() is not None


def test_clipboard_is_always_closed_after_opening() -> None:
    api = FakeClipboard()
    reader = make_reader(api)
    reader.read_new()
    reader.read_new()
    assert api.opens == api.closes == 2


def test_clipboard_is_closed_even_if_reading_fails() -> None:
    api = FakeClipboard(fail_get_text=True)
    with pytest.raises(RuntimeError):
        make_reader(api).read_new()
    assert api.opens == api.closes == 1


# --- Logging ---


def test_reader_never_logs_the_text(log_output: io.StringIO) -> None:
    api = FakeClipboard(sequence=5)
    reader = make_reader(api, sleep=no_sleep)
    reader.read_new()
    api.sequence, api.text = 6, None
    reader.read_new()
    api.sequence, api.busy_times = 7, 100
    reader.read_new()

    text = log_output.getvalue()
    assert FAKE_TEXT not in text
    assert f"clipboard_captured length={len(FAKE_TEXT)} sequence=5" in text
    assert "clipboard_skipped_no_text sequence=6" in text
    assert f"clipboard_busy attempts={OPEN_ATTEMPTS}" in text
    assert "dropped_fields" not in text


# --- The real listener thread and window, with a fake clipboard behind it ---


@pytest.fixture
def events() -> "queue.Queue[ClipboardEvent]":
    return queue.Queue()


def send_update(listener: ClipboardListener) -> None:
    """Send the message Windows sends when the clipboard changes."""
    handle = listener.window_handle
    assert handle is not None
    win32gui.PostMessage(handle, WM_CLIPBOARDUPDATE, 0, 0)


def test_listener_starts_and_stops_cleanly(events: "queue.Queue[ClipboardEvent]") -> None:
    listener = ClipboardListener(events, windows=NoWindows(), api=FakeClipboard())
    listener.start()
    assert listener.is_running
    assert listener.window_handle is not None
    listener.stop()
    assert not listener.is_running
    assert listener.window_handle is None


def test_stop_without_start_is_harmless(events: "queue.Queue[ClipboardEvent]") -> None:
    ClipboardListener(events, windows=NoWindows(), api=FakeClipboard()).stop()


def test_starting_twice_is_an_error(events: "queue.Queue[ClipboardEvent]") -> None:
    listener = ClipboardListener(events, windows=NoWindows(), api=FakeClipboard())
    listener.start()
    try:
        with pytest.raises(RuntimeError):
            listener.start()
    finally:
        listener.stop()


def test_listener_can_be_restarted(events: "queue.Queue[ClipboardEvent]") -> None:
    listener = ClipboardListener(events, windows=NoWindows(), api=FakeClipboard())
    listener.start()
    listener.stop()
    listener.start()
    assert listener.is_running
    listener.stop()


def test_update_message_puts_an_event_on_the_queue(
    events: "queue.Queue[ClipboardEvent]",
) -> None:
    listener = ClipboardListener(
        events, windows=NoWindows(), api=FakeClipboard(), debounce_ms=20
    )
    listener.start()
    try:
        send_update(listener)
        event = events.get(timeout=3)
    finally:
        listener.stop()
    assert event.text == FAKE_TEXT


def test_burst_of_updates_is_merged_into_one_read(
    events: "queue.Queue[ClipboardEvent]",
) -> None:
    # Every open reports a new sequence number, so without merging each of the
    # five messages would produce its own read and its own event.
    api = FakeClipboard(new_sequence_on_open=True)
    listener = ClipboardListener(events, windows=NoWindows(), api=api, debounce_ms=100)
    listener.start()
    try:
        for _ in range(5):
            send_update(listener)
        assert wait_until(lambda: events.qsize() >= 1)
        time.sleep(0.3)
        assert api.opens == 1
        assert events.qsize() == 1

        send_update(listener)
        assert wait_until(lambda: events.qsize() >= 2)
        assert api.opens == 2
    finally:
        listener.stop()


def test_failed_read_does_not_kill_the_listener_or_leak_text(
    events: "queue.Queue[ClipboardEvent]",
    log_output: io.StringIO,
    capfd: pytest.CaptureFixture[str],
) -> None:
    api = FakeClipboard(new_sequence_on_open=True, fail_get_text=True)
    listener = ClipboardListener(events, windows=NoWindows(), api=api, debounce_ms=20)
    listener.start()
    try:
        send_update(listener)
        assert wait_until(lambda: "capture_failed" in log_output.getvalue())
        assert listener.is_running

        api.fail_get_text = False
        send_update(listener)
        assert events.get(timeout=3).text == FAKE_TEXT
    finally:
        listener.stop()

    captured = capfd.readouterr()
    assert FAKE_TEXT not in log_output.getvalue()
    assert FAKE_TEXT not in captured.out
    assert FAKE_TEXT not in captured.err
    assert "capture_failed error_type=RuntimeError" in log_output.getvalue()


class WindowRecordingClipboard(FakeClipboard):
    def __init__(self) -> None:
        super().__init__(new_sequence_on_open=True)
        self.windows: list[int | None] = []

    def open(self, window: int | None = None) -> None:
        self.windows.append(window)
        super().open(window)


def test_listener_opens_the_clipboard_for_its_own_window(
    events: "queue.Queue[ClipboardEvent]",
) -> None:
    # Opening with no window failed on a real machine after the first reads
    # (see ClipboardReader.window), so the listener must pass its window.
    api = WindowRecordingClipboard()
    listener = ClipboardListener(events, windows=NoWindows(), api=api, debounce_ms=20)
    listener.start()
    try:
        handle = listener.window_handle
        send_update(listener)
        events.get(timeout=3)
    finally:
        listener.stop()
    assert api.windows == [handle]
    assert handle is not None


def test_two_listeners_can_run_at_once(events: "queue.Queue[ClipboardEvent]") -> None:
    first = ClipboardListener(events, windows=NoWindows(), api=FakeClipboard())
    second = ClipboardListener(events, windows=NoWindows(), api=FakeClipboard())
    first.start()
    second.start()
    try:
        assert first.is_running and second.is_running
    finally:
        first.stop()
        second.stop()


# --- Opt-in: the real clipboard ---


@pytest.mark.skipif(
    os.environ.get("SMARTCLIP_REAL_CLIPBOARD_TESTS") != "1",
    reason="replaces the real clipboard; set SMARTCLIP_REAL_CLIPBOARD_TESTS=1 to run",
)
def test_real_copy_is_captured(events: "queue.Queue[ClipboardEvent]") -> None:
    listener = ClipboardListener(events)
    listener.start()
    try:
        win32clipboard.OpenClipboard(None)
        try:
            win32clipboard.EmptyClipboard()
            win32clipboard.SetClipboardText(FAKE_TEXT, win32con.CF_UNICODETEXT)
        finally:
            win32clipboard.CloseClipboard()
        event = events.get(timeout=3)
    finally:
        listener.stop()
        win32clipboard.OpenClipboard(None)
        try:
            win32clipboard.EmptyClipboard()
        finally:
            win32clipboard.CloseClipboard()
    assert event.text == FAKE_TEXT


@pytest.mark.skipif(
    os.environ.get("SMARTCLIP_REAL_CLIPBOARD_TESTS") != "1",
    reason="replaces the real clipboard; set SMARTCLIP_REAL_CLIPBOARD_TESTS=1 to run",
)
def test_real_copies_in_a_row_are_all_captured(
    events: "queue.Queue[ClipboardEvent]",
) -> None:
    # Regression: with OpenClipboard(None) only the first reads worked.
    listener = ClipboardListener(events)
    listener.start()
    try:
        for number in range(6):
            text = f"{FAKE_TEXT}-{number}"
            win32clipboard.OpenClipboard(None)
            try:
                win32clipboard.EmptyClipboard()
                win32clipboard.SetClipboardText(text, win32con.CF_UNICODETEXT)
            finally:
                win32clipboard.CloseClipboard()
            assert events.get(timeout=3).text == text
    finally:
        listener.stop()
        win32clipboard.OpenClipboard(None)
        try:
            win32clipboard.EmptyClipboard()
        finally:
            win32clipboard.CloseClipboard()


@pytest.mark.skipif(
    os.environ.get("SMARTCLIP_REAL_CLIPBOARD_TESTS") != "1",
    reason="replaces the real clipboard; set SMARTCLIP_REAL_CLIPBOARD_TESTS=1 to run",
)
@pytest.mark.parametrize(
    ("clipboard_format", "data"),
    [
        (win32con.CF_UNICODETEXT, FAKE_TEXT),
        # Only ANSI text: Windows converts it when we ask for Unicode.
        (win32con.CF_TEXT, FAKE_TEXT.encode("ascii")),
    ],
)
def test_real_read_leaves_the_sequence_number_unchanged(
    events: "queue.Queue[ClipboardEvent]", clipboard_format: int, data: str | bytes
) -> None:
    # Mark-as-secret (overrides.LastCapture) relies on this: the number on a
    # captured event must still be the clipboard's number after we read it.
    listener = ClipboardListener(events)
    listener.start()
    try:
        win32clipboard.OpenClipboard(None)
        try:
            win32clipboard.EmptyClipboard()
            win32clipboard.SetClipboardData(clipboard_format, data)
        finally:
            win32clipboard.CloseClipboard()
        event = events.get(timeout=3)
        time.sleep(0.2)
        assert event.text == FAKE_TEXT
        assert event.sequence_number == win32clipboard.GetClipboardSequenceNumber()
    finally:
        listener.stop()
        win32clipboard.OpenClipboard(None)
        try:
            win32clipboard.EmptyClipboard()
        finally:
            win32clipboard.CloseClipboard()
