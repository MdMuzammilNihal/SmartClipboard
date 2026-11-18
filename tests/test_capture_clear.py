"""Tests for clearing a captured secret from the clipboard (option B). All values are fake.

Most tests use a fake clipboard with a sequence number. Some run the real
listener thread with real window messages, still with a fake clipboard.

The test at the end touches the real clipboard. It is skipped unless the
environment variable SMARTCLIP_REAL_CLIPBOARD_TESTS is set to 1, because it
REPLACES whatever is on the clipboard with fake text.
"""

import io
import logging
import os
import queue
import time
from collections.abc import Callable, Iterator
from datetime import timedelta

import pytest
import win32api
import win32clipboard
import win32con
import win32gui

from smartclip import config, logsafe
from smartclip.copyback import OPT_OUT_FORMAT_NAMES, ClearOutcome, CopyBack
from smartclip.gate import Gate
from smartclip.listener import CLEAR_RETRY_LIMIT, ClipboardEvent, ClipboardListener

FAKE_SECRET = "FAKE-capture-clear-Zx9!q-not-real"
OUR_WINDOW = 0x1234

real_clipboard = pytest.mark.skipif(
    os.environ.get("SMARTCLIP_REAL_CLIPBOARD_TESTS") != "1",
    reason="replaces the real clipboard; set SMARTCLIP_REAL_CLIPBOARD_TESTS=1 to run",
)


class SequenceWriter:
    """A fake clipboard holding text from another app, with a change counter."""

    def __init__(self, busy_times: int = 0) -> None:
        self.busy_times = busy_times
        self.sequence = 41
        self.text: str | None = FAKE_SECRET
        self.opens = 0
        self.closes = 0
        self._open_window: int | None = None
        self.current_owner: int | None = 0x9999

    def open(self, window: int) -> bool:
        if self.busy_times > 0:
            self.busy_times -= 1
            return False
        self.opens += 1
        self._open_window = window
        return True

    def close(self) -> None:
        self.closes += 1
        self._open_window = None

    def empty(self) -> None:
        self.text = None
        self.sequence += 1
        self.current_owner = self._open_window

    def set_text(self, text: str) -> None:
        self.text = text
        self.sequence += 1

    def set_format_bytes(self, _format_id: int, _data: bytes) -> None:
        pass

    def register_format(self, name: str) -> int:
        return 0xC000 + OPT_OUT_FORMAT_NAMES.index(name)

    def owner(self) -> int | None:
        return self.current_owner

    def sequence_number(self) -> int:
        return self.sequence

    def another_app_copies(self) -> None:
        self.text = "FAKE newer copy from another app"
        self.sequence += 1


class NoReads:
    """A read clipboard that is never expected to be used."""

    def open(self, window: int | None) -> None:
        raise AssertionError("these tests do not read")

    def close(self) -> None:
        pass

    def get_text(self) -> str | None:
        return None

    def sequence_number(self) -> int:
        return 0

    def register_format(self, name: str) -> int:
        return 0xC100 + len(name)

    def has_format(self, _format_id: int) -> bool:
        return False

    def get_format_bytes(self, _format_id: int) -> bytes | None:
        return None


def no_sleep(_seconds: float) -> None:
    pass


def make_copy_back(writer: SequenceWriter, **options: object) -> CopyBack:
    return CopyBack(writer, sleep=no_sleep, **options)  # type: ignore[arg-type]


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


# --- CopyBack rules ---


def test_unchanged_secret_is_cleared() -> None:
    writer = SequenceWriter()
    copy_back = make_copy_back(writer)
    copy_back.schedule_capture_clear(writer.sequence)
    assert copy_back.clear_capture_if_unchanged(OUR_WINDOW) is ClearOutcome.CLEARED
    assert writer.text is None
    assert not copy_back.capture_clear_pending


def test_newer_copy_is_never_wiped() -> None:
    writer = SequenceWriter()
    copy_back = make_copy_back(writer)
    copy_back.schedule_capture_clear(writer.sequence)
    writer.another_app_copies()
    assert copy_back.clear_capture_if_unchanged(OUR_WINDOW) is ClearOutcome.REPLACED
    assert writer.text == "FAKE newer copy from another app"
    assert not copy_back.capture_clear_pending


def test_nothing_pending_does_not_open_the_clipboard() -> None:
    writer = SequenceWriter()
    copy_back = make_copy_back(writer)
    assert copy_back.clear_capture_if_unchanged(OUR_WINDOW) is ClearOutcome.NOTHING_PENDING
    assert writer.opens == 0


def test_cancel_keeps_the_text() -> None:
    writer = SequenceWriter()
    copy_back = make_copy_back(writer)
    copy_back.schedule_capture_clear(writer.sequence)
    copy_back.cancel_capture_clear()
    assert copy_back.clear_capture_if_unchanged(OUR_WINDOW) is ClearOutcome.NOTHING_PENDING
    assert writer.text == FAKE_SECRET


def test_newer_schedule_replaces_older() -> None:
    writer = SequenceWriter()
    copy_back = make_copy_back(writer)
    copy_back.schedule_capture_clear(writer.sequence)
    writer.another_app_copies()
    copy_back.schedule_capture_clear(writer.sequence)
    assert copy_back.clear_capture_if_unchanged(OUR_WINDOW) is ClearOutcome.CLEARED


def test_busy_stays_pending_until_given_up() -> None:
    writer = SequenceWriter(busy_times=5)
    copy_back = make_copy_back(writer)
    copy_back.schedule_capture_clear(writer.sequence)
    assert copy_back.clear_capture_if_unchanged(OUR_WINDOW) is ClearOutcome.BUSY
    assert copy_back.capture_clear_pending
    copy_back.give_up_capture_clear()
    assert not copy_back.capture_clear_pending
    assert writer.text == FAKE_SECRET


def test_clipboard_is_closed_after_check() -> None:
    writer = SequenceWriter()
    copy_back = make_copy_back(writer)
    copy_back.schedule_capture_clear(writer.sequence)
    copy_back.clear_capture_if_unchanged(OUR_WINDOW)
    assert writer.opens == writer.closes == 1


def test_capture_clear_and_copy_back_clear_are_independent() -> None:
    writer = SequenceWriter()
    copy_back = make_copy_back(writer)
    copy_back.schedule_capture_clear(writer.sequence)
    assert copy_back.clear_if_ours(OUR_WINDOW) is ClearOutcome.NOTHING_PENDING
    assert copy_back.capture_clear_pending


def test_default_capture_clear_time_is_30_seconds() -> None:
    assert config.CAPTURED_SECRET_CLEAR_AFTER == timedelta(seconds=30)
    assert make_copy_back(SequenceWriter()).capture_clear_after == timedelta(seconds=30)


def test_capture_clear_logs_no_content(log_output: io.StringIO) -> None:
    writer = SequenceWriter()
    copy_back = make_copy_back(writer)
    copy_back.schedule_capture_clear(writer.sequence)
    copy_back.clear_capture_if_unchanged(OUR_WINDOW)
    text = log_output.getvalue()
    assert FAKE_SECRET not in text
    assert "capture_clear_scheduled sequence=41" in text
    assert "capture_clear_cleared sequence=41" in text


# --- Through the listener thread (real window messages, fake clipboard) ---


def start_listener(writer: SequenceWriter, after: timedelta) -> tuple[ClipboardListener, CopyBack]:
    copy_back = make_copy_back(writer, capture_clear_after=after)
    running = ClipboardListener(
        queue.Queue(), api=NoReads(), copy_back=copy_back  # type: ignore[arg-type]
    )
    running.start()
    return running, copy_back


def test_listener_clears_after_the_delay() -> None:
    writer = SequenceWriter()
    running, _ = start_listener(writer, timedelta(milliseconds=100))
    try:
        assert running.request_capture_clear(writer.sequence) is True
        assert wait_until(lambda: writer.text is None)
        assert writer.current_owner == running.window_handle
    finally:
        running.stop()


def test_listener_cancel_before_due_keeps_text() -> None:
    writer = SequenceWriter()
    running, copy_back = start_listener(writer, timedelta(milliseconds=200))
    try:
        running.request_capture_clear(writer.sequence)
        assert wait_until(lambda: copy_back.capture_clear_pending)
        running.request_capture_clear(None)
        time.sleep(0.4)
        assert writer.text == FAKE_SECRET
    finally:
        running.stop()


def test_listener_never_wipes_a_newer_copy() -> None:
    writer = SequenceWriter()
    running, _ = start_listener(writer, timedelta(milliseconds=100))
    try:
        running.request_capture_clear(writer.sequence)
        writer.another_app_copies()
        time.sleep(0.4)
        assert writer.text == "FAKE newer copy from another app"
    finally:
        running.stop()


def test_listener_busy_clear_is_retried_then_given_up() -> None:
    writer = SequenceWriter()
    running, copy_back = start_listener(writer, timedelta(milliseconds=50))
    try:
        writer.busy_times = 5 * CLEAR_RETRY_LIMIT
        running.request_capture_clear(writer.sequence)
        assert wait_until(lambda: copy_back.capture_clear_pending)
        assert wait_until(lambda: not copy_back.capture_clear_pending, timeout=CLEAR_RETRY_LIMIT + 3)
        assert writer.text == FAKE_SECRET
    finally:
        running.stop()


def test_stopping_clears_a_pending_unchanged_secret() -> None:
    writer = SequenceWriter()
    running, copy_back = start_listener(writer, timedelta(minutes=5))
    running.request_capture_clear(writer.sequence)
    assert wait_until(lambda: copy_back.capture_clear_pending)
    running.stop()
    assert writer.text is None


def test_request_when_not_running_returns_false() -> None:
    stopped = ClipboardListener(
        queue.Queue(), api=NoReads(), copy_back=make_copy_back(SequenceWriter())  # type: ignore[arg-type]
    )
    assert stopped.request_capture_clear(1) is False


# --- Real clipboard (opt-in) ---


def _real_text() -> str | None:
    for _ in range(20):
        try:
            win32clipboard.OpenClipboard(None)
            break
        except Exception:  # noqa: BLE001 - another app may hold it briefly
            time.sleep(0.05)
    else:
        raise AssertionError("clipboard stayed busy")
    try:
        if win32clipboard.IsClipboardFormatAvailable(win32con.CF_UNICODETEXT):
            return win32clipboard.GetClipboardData(win32con.CF_UNICODETEXT)
        return None
    finally:
        win32clipboard.CloseClipboard()


@real_clipboard
def test_real_captured_secret_is_cleared_and_newer_copy_is_kept() -> None:
    events: "queue.Queue[ClipboardEvent]" = queue.Queue()
    running = ClipboardListener(
        events, gate=Gate(), copy_back=CopyBack(capture_clear_after=timedelta(milliseconds=500))
    )
    running.start()
    other_class = f"SmartClipTestCapture-{os.getpid()}"
    instance = win32api.GetModuleHandle(None)
    window_class = win32gui.WNDCLASS()
    window_class.lpszClassName = other_class
    window_class.hInstance = instance
    window_class.lpfnWndProc = {}
    atom = win32gui.RegisterClass(window_class)
    other = win32gui.CreateWindow(atom, "", 0, 0, 0, 0, 0, win32con.HWND_MESSAGE, 0, instance, None)

    def other_app_copies(text: str) -> None:
        win32clipboard.OpenClipboard(other)
        try:
            win32clipboard.EmptyClipboard()
            win32clipboard.SetClipboardData(win32con.CF_UNICODETEXT, text)
        finally:
            win32clipboard.CloseClipboard()

    try:
        # 1. A secret copied by "another app" is captured, then cleared.
        other_app_copies(FAKE_SECRET)
        event = events.get(timeout=3)
        assert running.request_capture_clear(event.sequence_number)
        assert wait_until(lambda: _real_text() is None)
        time.sleep(0.3)
        assert events.empty()  # our clear is not captured

        # 2. A newer copy during the wait is left alone.
        other_app_copies(FAKE_SECRET)
        event = events.get(timeout=3)
        running.request_capture_clear(event.sequence_number)
        newer = "FAKE newer copy made during the wait"
        other_app_copies(newer)
        time.sleep(1.0)
        assert _real_text() == newer
    finally:
        running.stop()
        win32clipboard.OpenClipboard(other)
        try:
            win32clipboard.EmptyClipboard()
        finally:
            win32clipboard.CloseClipboard()
        win32gui.DestroyWindow(other)
        win32gui.UnregisterClass(other_class, instance)
