"""Tests for copy-back (task 12). All values are fake.

Most tests use a fake clipboard writer. Some run the real listener thread
with real window messages, still with fake clipboards behind it.

The tests at the end touch the real clipboard. They are skipped unless the
environment variable SMARTCLIP_REAL_CLIPBOARD_TESTS is set to 1, because
they REPLACE whatever is on the clipboard with fake text.
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

from smartclip import logsafe
from smartclip.copyback import (
    OPT_OUT_FORMAT_NAMES,
    ClearOutcome,
    CopyBack,
    Win32ClipboardWriter,
)
from smartclip.gate import Gate
from smartclip.listener import CLEAR_RETRY_LIMIT, ClipboardEvent, ClipboardListener

FAKE_SECRET = "FAKE-copyback-Zx9!q-not-real"
FAKE_NOTE = "FAKE copy-back note"
OUR_WINDOW = 0x1234
OTHER_WINDOW = 0x5678
DWORD_ZERO = b"\x00\x00\x00\x00"


class FakeWriter:
    """A scripted clipboard that records what was written."""

    def __init__(self, busy_times: int = 0) -> None:
        self.busy_times = busy_times
        self.opened_for: list[int] = []
        self.closes = 0
        self.current_owner: int | None = None
        self.text: str | None = None
        self.formats: dict[int, bytes] = {}
        self._open_window: int | None = None

    def open(self, window: int) -> bool:
        if self.busy_times > 0:
            self.busy_times -= 1
            return False
        self.opened_for.append(window)
        self._open_window = window
        return True

    def close(self) -> None:
        self.closes += 1
        self._open_window = None

    def empty(self) -> None:
        self.text = None
        self.formats = {}
        self.current_owner = self._open_window

    def set_text(self, text: str) -> None:
        self.text = text

    def set_format_bytes(self, format_id: int, data: bytes) -> None:
        self.formats[format_id] = data

    def register_format(self, name: str) -> int:
        return 0xC000 + OPT_OUT_FORMAT_NAMES.index(name)

    def owner(self) -> int | None:
        return self.current_owner

    def another_app_copies(self) -> None:
        self.current_owner = OTHER_WINDOW
        self.text = "FAKE newer copy from another app"
        self.formats = {}


def no_sleep(_seconds: float) -> None:
    """Stands in for time.sleep so retry tests run instantly."""


def make_copy_back(writer: FakeWriter, **options: object) -> CopyBack:
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


# --- Writing ---


def test_secret_is_written_with_all_three_opt_out_formats() -> None:
    writer = FakeWriter()
    copy_back = make_copy_back(writer)
    assert copy_back.write(OUR_WINDOW, FAKE_SECRET, sensitive=True) is True
    assert writer.text == FAKE_SECRET
    assert writer.formats == {0xC000: DWORD_ZERO, 0xC001: DWORD_ZERO, 0xC002: DWORD_ZERO}
    assert copy_back.clear_pending is True


def test_opt_out_format_names_are_the_documented_ones() -> None:
    assert OPT_OUT_FORMAT_NAMES == (
        "ExcludeClipboardContentFromMonitorProcessing",
        "CanIncludeInClipboardHistory",
        "CanUploadToCloudClipboard",
    )


def test_normal_item_is_written_as_plain_text() -> None:
    writer = FakeWriter()
    copy_back = make_copy_back(writer)
    assert copy_back.write(OUR_WINDOW, FAKE_NOTE, sensitive=False) is True
    assert writer.text == FAKE_NOTE
    assert writer.formats == {}
    assert copy_back.clear_pending is False


def test_write_opens_for_our_window_and_makes_it_the_owner() -> None:
    writer = FakeWriter()
    make_copy_back(writer).write(OUR_WINDOW, FAKE_SECRET, sensitive=True)
    assert writer.opened_for == [OUR_WINDOW]
    assert writer.current_owner == OUR_WINDOW
    assert writer.closes == 1


def test_busy_clipboard_is_retried() -> None:
    writer = FakeWriter(busy_times=2)
    assert make_copy_back(writer).write(OUR_WINDOW, FAKE_NOTE, sensitive=False) is True
    assert writer.text == FAKE_NOTE


def test_write_gives_up_when_clipboard_stays_busy() -> None:
    writer = FakeWriter(busy_times=99)
    copy_back = make_copy_back(writer)
    assert copy_back.write(OUR_WINDOW, FAKE_SECRET, sensitive=True) is False
    assert copy_back.clear_pending is False


def test_clipboard_is_closed_even_if_writing_fails() -> None:
    writer = FakeWriter()

    def broken(_text: str) -> None:
        raise RuntimeError("fake failure")

    writer.set_text = broken  # type: ignore[method-assign]
    with pytest.raises(RuntimeError):
        make_copy_back(writer).write(OUR_WINDOW, FAKE_NOTE, sensitive=False)
    assert writer.closes == 1


# --- Clearing ---


def test_secret_still_ours_is_cleared() -> None:
    writer = FakeWriter()
    copy_back = make_copy_back(writer)
    copy_back.write(OUR_WINDOW, FAKE_SECRET, sensitive=True)
    assert copy_back.clear_if_ours(OUR_WINDOW) is ClearOutcome.CLEARED
    assert writer.text is None
    assert writer.formats == {}
    assert copy_back.clear_pending is False


def test_newer_copy_from_another_app_is_never_wiped() -> None:
    writer = FakeWriter()
    copy_back = make_copy_back(writer)
    copy_back.write(OUR_WINDOW, FAKE_SECRET, sensitive=True)
    writer.another_app_copies()
    assert copy_back.clear_if_ours(OUR_WINDOW) is ClearOutcome.REPLACED
    assert writer.text == "FAKE newer copy from another app"
    assert copy_back.clear_pending is False


def test_plain_copy_back_cancels_a_pending_clear() -> None:
    writer = FakeWriter()
    copy_back = make_copy_back(writer)
    copy_back.write(OUR_WINDOW, FAKE_SECRET, sensitive=True)
    copy_back.write(OUR_WINDOW, FAKE_NOTE, sensitive=False)
    assert copy_back.clear_if_ours(OUR_WINDOW) is ClearOutcome.NOTHING_PENDING
    assert writer.text == FAKE_NOTE


def test_nothing_is_cleared_without_a_safe_copy_back() -> None:
    writer = FakeWriter()
    writer.text = "FAKE something the owner copied"
    assert make_copy_back(writer).clear_if_ours(OUR_WINDOW) is ClearOutcome.NOTHING_PENDING
    assert writer.opened_for == []


def test_busy_clear_stays_pending_until_given_up() -> None:
    writer = FakeWriter()
    copy_back = make_copy_back(writer)
    copy_back.write(OUR_WINDOW, FAKE_SECRET, sensitive=True)
    writer.busy_times = 99
    assert copy_back.clear_if_ours(OUR_WINDOW) is ClearOutcome.BUSY
    assert copy_back.clear_pending is True
    copy_back.give_up_clear()
    assert copy_back.clear_pending is False


def test_default_clear_time_is_30_seconds() -> None:
    assert make_copy_back(FakeWriter()).clear_after == timedelta(seconds=30)


def test_copy_back_never_logs_content(log_output: io.StringIO) -> None:
    writer = FakeWriter()
    copy_back = make_copy_back(writer)
    copy_back.write(OUR_WINDOW, FAKE_SECRET, sensitive=True)
    copy_back.clear_if_ours(OUR_WINDOW)
    copy_back.write(OUR_WINDOW, FAKE_SECRET, sensitive=True)
    writer.another_app_copies()
    copy_back.clear_if_ours(OUR_WINDOW)
    text = log_output.getvalue()
    assert FAKE_SECRET not in text
    assert f"copy_back_written length={len(FAKE_SECRET)} sensitive=true" in text
    assert "copy_back_clear_cleared" in text
    assert "copy_back_clear_replaced" in text
    assert logsafe.DROPPED_RECORD_TEXT not in text


# --- Through the listener thread (real window messages, fake clipboards) ---


class NoReads:
    """A read clipboard that is never expected to be used."""

    def open(self, window: int | None) -> None:
        raise AssertionError("copy-back tests do not read")

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


@pytest.fixture
def writer() -> FakeWriter:
    return FakeWriter()


@pytest.fixture
def listener(writer: FakeWriter) -> Iterator[ClipboardListener]:
    copy_back = make_copy_back(writer, clear_after=timedelta(milliseconds=100))
    running = ClipboardListener(
        queue.Queue(), api=NoReads(), copy_back=copy_back  # type: ignore[arg-type]
    )
    running.start()
    yield running
    running.stop()


def test_copy_back_runs_on_the_listener_window(
    listener: ClipboardListener, writer: FakeWriter
) -> None:
    future = listener.request_copy_back(FAKE_NOTE, sensitive=False)
    assert future is not None
    assert future.result(timeout=3) is True
    assert writer.opened_for == [listener.window_handle]
    assert writer.text == FAKE_NOTE


def test_secret_is_cleared_after_the_delay(
    listener: ClipboardListener, writer: FakeWriter
) -> None:
    future = listener.request_copy_back(FAKE_SECRET, sensitive=True)
    assert future is not None and future.result(timeout=3) is True
    assert writer.text == FAKE_SECRET
    assert wait_until(lambda: writer.text is None)


def test_plain_item_is_not_cleared(listener: ClipboardListener, writer: FakeWriter) -> None:
    future = listener.request_copy_back(FAKE_NOTE, sensitive=False)
    assert future is not None and future.result(timeout=3) is True
    time.sleep(0.3)
    assert writer.text == FAKE_NOTE


def test_listener_never_wipes_a_newer_copy(
    listener: ClipboardListener, writer: FakeWriter
) -> None:
    future = listener.request_copy_back(FAKE_SECRET, sensitive=True)
    assert future is not None and future.result(timeout=3) is True
    writer.another_app_copies()
    time.sleep(0.3)
    assert writer.text == "FAKE newer copy from another app"


def test_busy_clear_is_retried_then_given_up(writer: FakeWriter) -> None:
    copy_back = make_copy_back(writer, clear_after=timedelta(milliseconds=50))
    running = ClipboardListener(
        queue.Queue(), api=NoReads(), copy_back=copy_back  # type: ignore[arg-type]
    )
    running.start()
    try:
        future = running.request_copy_back(FAKE_SECRET, sensitive=True)
        assert future is not None and future.result(timeout=3) is True
        # Every clear attempt finds the clipboard busy (5 opens per attempt).
        writer.busy_times = 5 * CLEAR_RETRY_LIMIT
        assert wait_until(lambda: not copy_back.clear_pending, timeout=CLEAR_RETRY_LIMIT + 3)
        assert writer.text == FAKE_SECRET
    finally:
        running.stop()


def test_stopping_clears_a_pending_secret(writer: FakeWriter) -> None:
    copy_back = make_copy_back(writer, clear_after=timedelta(minutes=5))
    running = ClipboardListener(
        queue.Queue(), api=NoReads(), copy_back=copy_back  # type: ignore[arg-type]
    )
    running.start()
    future = running.request_copy_back(FAKE_SECRET, sensitive=True)
    assert future is not None and future.result(timeout=3) is True
    running.stop()
    assert writer.text is None


def test_request_when_not_running_returns_none() -> None:
    stopped = ClipboardListener(
        queue.Queue(), api=NoReads(), copy_back=make_copy_back(FakeWriter())  # type: ignore[arg-type]
    )
    assert stopped.request_copy_back(FAKE_SECRET, sensitive=True) is None


def test_listener_copy_back_never_logs_content(
    log_output: io.StringIO, listener: ClipboardListener, writer: FakeWriter
) -> None:
    future = listener.request_copy_back(FAKE_SECRET, sensitive=True)
    assert future is not None and future.result(timeout=3) is True
    assert wait_until(lambda: writer.text is None)
    text = log_output.getvalue()
    assert FAKE_SECRET not in text
    assert "copy_back_clear_cleared" in text


# --- Real clipboard (opt-in) ---

real_clipboard = pytest.mark.skipif(
    os.environ.get("SMARTCLIP_REAL_CLIPBOARD_TESTS") != "1",
    reason="replaces the real clipboard; set SMARTCLIP_REAL_CLIPBOARD_TESTS=1 to run",
)


def read_real_clipboard() -> tuple[str | None, dict[str, bytes | None]]:
    """Read the real clipboard's text and opt-out formats (for checks only)."""
    for _ in range(20):
        try:
            win32clipboard.OpenClipboard(None)
            break
        except Exception:  # noqa: BLE001 - another app may hold it briefly
            time.sleep(0.05)
    else:
        raise AssertionError("clipboard stayed busy")
    try:
        text = None
        if win32clipboard.IsClipboardFormatAvailable(win32con.CF_UNICODETEXT):
            text = win32clipboard.GetClipboardData(win32con.CF_UNICODETEXT)
        formats: dict[str, bytes | None] = {}
        for name in OPT_OUT_FORMAT_NAMES:
            format_id = win32clipboard.RegisterClipboardFormat(name)
            if win32clipboard.IsClipboardFormatAvailable(format_id):
                formats[name] = win32clipboard.GetClipboardData(format_id)[:4]
            else:
                formats[name] = None
        return text, formats
    finally:
        win32clipboard.CloseClipboard()


def real_listener(clear_after: timedelta) -> tuple[ClipboardListener, "queue.Queue[ClipboardEvent]"]:
    events: "queue.Queue[ClipboardEvent]" = queue.Queue()
    running = ClipboardListener(events, gate=Gate(), copy_back=CopyBack(clear_after=clear_after))
    running.start()
    return running, events


@real_clipboard
def test_real_secret_copy_back_sets_formats_is_not_captured_and_is_cleared() -> None:
    running, events = real_listener(timedelta(milliseconds=500))
    try:
        future = running.request_copy_back(FAKE_SECRET, sensitive=True)
        assert future is not None and future.result(timeout=3) is True
        text, formats = read_real_clipboard()
        assert text == FAKE_SECRET
        assert formats == {name: DWORD_ZERO for name in OPT_OUT_FORMAT_NAMES}
        assert win32clipboard.GetClipboardOwner() == running.window_handle
        time.sleep(0.3)
        assert events.empty()  # our own write is not captured
        assert wait_until(lambda: read_real_clipboard()[0] is None)
        assert events.empty()  # nor is our clear
    finally:
        running.stop()


@real_clipboard
def test_real_newer_copy_is_not_cleared() -> None:
    running, _events = real_listener(timedelta(milliseconds=500))
    other_class = f"SmartClipTestOther-{os.getpid()}"
    instance = win32api.GetModuleHandle(None)
    window_class = win32gui.WNDCLASS()
    window_class.lpszClassName = other_class
    window_class.hInstance = instance
    window_class.lpfnWndProc = {}
    atom = win32gui.RegisterClass(window_class)
    other = win32gui.CreateWindow(atom, "", 0, 0, 0, 0, 0, win32con.HWND_MESSAGE, 0, instance, None)
    newer = "FAKE newer copy made during the wait"
    try:
        future = running.request_copy_back(FAKE_SECRET, sensitive=True)
        assert future is not None and future.result(timeout=3) is True
        # Another window copies before the clear is due.
        win32clipboard.OpenClipboard(other)
        try:
            win32clipboard.EmptyClipboard()
            win32clipboard.SetClipboardData(win32con.CF_UNICODETEXT, newer)
        finally:
            win32clipboard.CloseClipboard()
        time.sleep(1.0)
        assert read_real_clipboard()[0] == newer
    finally:
        running.stop()
        win32clipboard.OpenClipboard(other)
        try:
            win32clipboard.EmptyClipboard()
        finally:
            win32clipboard.CloseClipboard()
        win32gui.DestroyWindow(other)
        win32gui.UnregisterClass(other_class, instance)


def test_real_writer_matches_the_protocol() -> None:
    # Constructing the real writer registers format names only; it does not
    # touch the clipboard's content, so this runs by default.
    copy_back = CopyBack(Win32ClipboardWriter())
    assert copy_back.clear_pending is False
