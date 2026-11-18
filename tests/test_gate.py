"""Tests for the privacy gate. All text and app names are fake.

Most tests use a fake clipboard and fake window lookups, so the owner's real
clipboard is neither read nor changed. Some start the real listener thread
with the fake clipboard behind it.

The tests at the end touch the real clipboard. They are skipped unless the
environment variable SMARTCLIP_REAL_CLIPBOARD_TESTS is set to 1, because they
REPLACE whatever is currently on the clipboard with fake text and then empty
it.
"""

import io
import logging
import os
import queue
import threading
import time
from collections.abc import Iterator

import pytest
import win32clipboard
import win32con
import win32gui

from smartclip import logsafe
from smartclip.gate import (
    CAN_INCLUDE_IN_HISTORY_FORMAT,
    EXCLUDE_FROM_MONITOR_FORMAT,
    Gate,
    OptOutFormats,
    PauseSwitch,
    SkipReason,
    normalise_app_names,
)
from smartclip.listener import (
    WM_CLIPBOARDUPDATE,
    ClipboardEvent,
    ClipboardListener,
    ClipboardReader,
)
from smartclip.sourceapp import SourceApp, SourceMethod

FAKE_TEXT = "FAKE-gate-test-text-4242"
FAKE_APP = "fakeeditor.exe"
FAKE_MANAGER = "FakeManager.exe"
OWNER_WINDOW = 11
FOREGROUND_WINDOW = 22
# Real Windows process IDs are multiples of 4, so these odd values never
# collide with the test process's own ID.
FAKE_OWNER_PID = 1_000_001
FAKE_FOREGROUND_PID = 1_000_003

DWORD_ZERO = (0).to_bytes(4, "little")
DWORD_ONE = (1).to_bytes(4, "little")


class FakeClipboard:
    """A scripted clipboard that can carry registered formats."""

    def __init__(self, text: str | None = FAKE_TEXT, sequence: int = 1) -> None:
        self.text = text
        self.sequence = sequence
        self.format_ids: dict[str, int] = {}
        # Format ID -> its data (None means present but unreadable).
        self.formats: dict[int, bytes | None] = {}
        self.opens = 0
        self.closes = 0
        self.text_reads = 0

    def set_format(self, name: str, data: bytes | None) -> None:
        self.formats[self.register_format(name)] = data

    def open(self, window: int | None = None) -> None:
        self.opens += 1

    def close(self) -> None:
        self.closes += 1

    def get_text(self) -> str | None:
        self.text_reads += 1
        return self.text

    def sequence_number(self) -> int:
        return self.sequence

    def register_format(self, name: str) -> int:
        return self.format_ids.setdefault(name, 0xC100 + len(self.format_ids))

    def has_format(self, format_id: int) -> bool:
        return format_id in self.formats

    def get_format_bytes(self, format_id: int) -> bytes | None:
        return self.formats.get(format_id)


class FakeWindows:
    """Scripted window lookups."""

    def __init__(
        self,
        owner_name: str | None = FAKE_APP,
        foreground_name: str | None = None,
        owner_pid: int = FAKE_OWNER_PID,
    ) -> None:
        self.pids = {OWNER_WINDOW: owner_pid, FOREGROUND_WINDOW: FAKE_FOREGROUND_PID}
        self.names = {owner_pid: owner_name, FAKE_FOREGROUND_PID: foreground_name}

    def clipboard_owner(self) -> int | None:
        return OWNER_WINDOW

    def foreground_window(self) -> int | None:
        return FOREGROUND_WINDOW

    def process_id(self, window: int) -> int | None:
        return self.pids.get(window)

    def executable_name(self, process_id: int) -> str | None:
        return self.names.get(process_id)


class BrokenWindows(FakeWindows):
    def clipboard_owner(self) -> int | None:
        raise RuntimeError(FAKE_APP)


def make_reader(
    api: FakeClipboard, gate: Gate | None = None, windows: FakeWindows | None = None
) -> ClipboardReader:
    return ClipboardReader(api, gate=gate or Gate(), windows=windows or FakeWindows())


@pytest.fixture
def log_output() -> Iterator[io.StringIO]:
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    logsafe.attach_handler(handler)
    yield stream
    logging.getLogger(logsafe.ROOT_LOGGER_NAME).removeHandler(handler)


# --- Pause switch ---


def test_pause_switch_starts_running_by_default() -> None:
    assert not PauseSwitch().is_paused
    assert PauseSwitch(paused=True).is_paused


def test_pause_and_resume() -> None:
    switch = PauseSwitch()
    switch.pause()
    assert switch.is_paused
    switch.resume()
    assert not switch.is_paused


def test_toggle_returns_the_new_state() -> None:
    switch = PauseSwitch()
    assert switch.toggle() is True
    assert switch.toggle() is False


def test_toggles_from_many_threads_do_not_get_lost() -> None:
    switch = PauseSwitch()
    start = threading.Barrier(8)

    def toggle_many() -> None:
        start.wait()
        for _ in range(1000):
            switch.toggle()

    threads = [threading.Thread(target=toggle_many) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    # 8000 toggles, an even number, so it ends where it started.
    assert not switch.is_paused


def test_paused_reader_does_not_open_the_clipboard() -> None:
    api = FakeClipboard()
    gate = Gate(pause=PauseSwitch(paused=True))
    assert make_reader(api, gate).read_new() is None
    assert api.opens == 0
    assert api.text_reads == 0


def test_capture_resumes_after_pause() -> None:
    api = FakeClipboard(sequence=1)
    gate = Gate()
    reader = make_reader(api, gate)
    gate.pause.pause()
    assert reader.read_new() is None
    gate.pause.resume()
    api.sequence = 2
    event = reader.read_new()
    assert event is not None and event.text == FAKE_TEXT


# --- Own writes ---


def test_gate_uses_this_process_id_by_default() -> None:
    assert Gate().own_process_id == os.getpid()


def test_own_write_is_skipped_and_text_not_read() -> None:
    api = FakeClipboard()
    gate = Gate(own_process_id=FAKE_OWNER_PID)
    assert make_reader(api, gate).read_new() is None
    assert api.text_reads == 0
    assert api.opens == api.closes == 1


def test_own_write_is_skipped_even_if_our_name_is_unreadable() -> None:
    api = FakeClipboard()
    gate = Gate(own_process_id=FAKE_OWNER_PID)
    windows = FakeWindows(owner_name=None, foreground_name="fakebrowser.exe")
    assert make_reader(api, gate, windows).read_new() is None


def test_a_later_copy_by_another_app_is_captured() -> None:
    api = FakeClipboard(sequence=70)
    gate = Gate(own_process_id=FAKE_OWNER_PID)
    windows = FakeWindows()
    reader = make_reader(api, gate, windows)
    assert reader.read_new() is None
    # Another app writes: it becomes the owner.
    windows.pids[OWNER_WINDOW] = FAKE_FOREGROUND_PID
    api.sequence = 71
    assert reader.read_new() is not None


def test_our_app_in_the_foreground_does_not_count_as_our_write() -> None:
    # Only the clipboard owner proves we wrote it; the foreground window does not.
    api = FakeClipboard()
    gate = Gate(own_process_id=FAKE_FOREGROUND_PID)
    windows = FakeWindows(owner_name=None, foreground_name="fakeeditor.exe")
    assert make_reader(api, gate, windows).read_new() is not None


# --- Windows opt-out formats ---


def test_formats_are_looked_up_by_their_documented_names() -> None:
    api = FakeClipboard()
    OptOutFormats(api)
    assert set(api.format_ids) == {
        "ExcludeClipboardContentFromMonitorProcessing",
        "CanIncludeInClipboardHistory",
    }


@pytest.mark.parametrize("data", [b"", b"\x01", None, DWORD_ONE])
def test_exclude_from_monitor_skips_whatever_its_data(data: bytes | None) -> None:
    api = FakeClipboard()
    api.set_format(EXCLUDE_FROM_MONITOR_FORMAT, data)
    gate = Gate()
    reader = make_reader(api, gate)
    assert reader.read_new() is None
    assert api.text_reads == 0


def test_history_flag_zero_is_skipped() -> None:
    api = FakeClipboard()
    api.set_format(CAN_INCLUDE_IN_HISTORY_FORMAT, DWORD_ZERO)
    assert make_reader(api).read_new() is None
    assert api.text_reads == 0


def test_history_flag_one_is_captured() -> None:
    api = FakeClipboard()
    api.set_format(CAN_INCLUDE_IN_HISTORY_FORMAT, DWORD_ONE)
    assert make_reader(api).read_new() is not None


def test_history_flag_in_a_larger_memory_block_is_read_from_its_first_bytes() -> None:
    api = FakeClipboard()
    api.set_format(CAN_INCLUDE_IN_HISTORY_FORMAT, DWORD_ZERO + b"\xff" * 12)
    assert make_reader(api).read_new() is None
    api.sequence = 2
    api.set_format(CAN_INCLUDE_IN_HISTORY_FORMAT, DWORD_ONE + b"\x00" * 12)
    assert make_reader(api).read_new() is not None


@pytest.mark.parametrize("unreadable", [None, b"", b"\x00\x00"])
def test_unreadable_history_flag_fails_closed(unreadable: bytes | None) -> None:
    api = FakeClipboard()
    api.set_format(CAN_INCLUDE_IN_HISTORY_FORMAT, unreadable)
    assert make_reader(api).read_new() is None
    assert api.text_reads == 0


@pytest.mark.parametrize(
    ("name", "data", "reason"),
    [
        (EXCLUDE_FROM_MONITOR_FORMAT, b"\x00", SkipReason.OPT_OUT_MONITOR),
        (CAN_INCLUDE_IN_HISTORY_FORMAT, DWORD_ZERO, SkipReason.OPT_OUT_HISTORY),
    ],
)
def test_gate_reports_which_format_opted_out(
    name: str, data: bytes, reason: SkipReason
) -> None:
    api = FakeClipboard()
    api.set_format(name, data)
    formats = OptOutFormats(api)
    source = SourceApp(FAKE_APP, SourceMethod.CLIPBOARD_OWNER, FAKE_OWNER_PID)
    assert Gate().skip_while_open(api, formats, source) is reason


def test_opted_out_change_is_not_read_again() -> None:
    api = FakeClipboard()
    api.set_format(EXCLUDE_FROM_MONITOR_FORMAT, b"\x00")
    reader = make_reader(api)
    reader.read_new()
    api.formats.clear()
    assert reader.read_new() is None
    assert api.text_reads == 0


# --- App exclusion list ---


def test_default_exclusion_list_is_empty() -> None:
    assert Gate().excluded_apps == frozenset()


def test_excluded_app_is_skipped_case_insensitively() -> None:
    api = FakeClipboard()
    gate = Gate(excluded_apps=[FAKE_MANAGER])
    windows = FakeWindows(owner_name="fakemanager.EXE")
    assert make_reader(api, gate, windows).read_new() is None
    assert api.text_reads == 0


def test_other_apps_are_still_captured() -> None:
    api = FakeClipboard()
    gate = Gate(excluded_apps=[FAKE_MANAGER])
    event = make_reader(api, gate, FakeWindows(owner_name=FAKE_APP)).read_new()
    assert event is not None


def test_exclusion_matches_the_foreground_fallback_too() -> None:
    api = FakeClipboard()
    gate = Gate(excluded_apps=[FAKE_MANAGER])
    windows = FakeWindows(owner_name=None, foreground_name=FAKE_MANAGER)
    assert make_reader(api, gate, windows).read_new() is None


def test_unknown_source_cannot_match_the_exclusion_list() -> None:
    # Documented limit: if the app cannot be found, the change is captured.
    api = FakeClipboard()
    gate = Gate(excluded_apps=[FAKE_MANAGER])
    windows = FakeWindows(owner_name=None, foreground_name=None)
    assert make_reader(api, gate, windows).read_new() is not None


def test_exclusion_list_can_be_replaced_while_running() -> None:
    api = FakeClipboard(sequence=1)
    gate = Gate()
    reader = make_reader(api, gate, FakeWindows(owner_name=FAKE_APP))
    assert reader.read_new() is not None
    gate.set_excluded_apps([FAKE_APP.upper()])
    api.sequence = 2
    assert reader.read_new() is None


@pytest.mark.parametrize(
    ("entries", "expected"),
    [
        (["FakeManager.exe"], {"fakemanager.exe"}),
        ([r"C:\Program Files\Fake\FakeManager.exe"], {"fakemanager.exe"}),
        (["  fakemanager.exe ", "", "   "], {"fakemanager.exe"}),
        ([], set()),
    ],
)
def test_exclusion_entries_are_normalised(entries: list[str], expected: set[str]) -> None:
    assert normalise_app_names(entries) == expected


# --- Source app on captured events ---


def test_event_carries_the_owner_app() -> None:
    event = make_reader(FakeClipboard()).read_new()
    assert event is not None
    assert event.source_app == FAKE_APP
    assert event.source_method is SourceMethod.CLIPBOARD_OWNER


def test_event_carries_the_foreground_fallback() -> None:
    windows = FakeWindows(owner_name=None, foreground_name="FakeBrowser.exe")
    event = make_reader(FakeClipboard(), windows=windows).read_new()
    assert event is not None
    assert event.source_app == "fakebrowser.exe"
    assert event.source_method is SourceMethod.FOREGROUND


def test_failed_source_lookup_still_captures_without_a_source(
    log_output: io.StringIO,
) -> None:
    event = make_reader(FakeClipboard(), windows=BrokenWindows()).read_new()
    assert event is not None
    assert event.source_app is None
    assert event.source_method is SourceMethod.UNKNOWN
    assert "source_app_lookup_failed error_type=RuntimeError" in log_output.getvalue()
    assert FAKE_APP not in log_output.getvalue()


def test_failed_source_lookup_does_not_bypass_opt_out_formats() -> None:
    api = FakeClipboard()
    api.set_format(EXCLUDE_FROM_MONITOR_FORMAT, b"\x00")
    assert make_reader(api, windows=BrokenWindows()).read_new() is None


# --- Logging: reasons only, never text or app names ---


def test_skips_are_logged_by_reason_only(log_output: io.StringIO) -> None:
    api = FakeClipboard(sequence=1)
    gate = Gate(excluded_apps=[FAKE_APP])
    reader = make_reader(api, gate)

    reader.read_new()  # excluded app
    api.sequence = 2
    gate.set_excluded_apps([])
    api.set_format(EXCLUDE_FROM_MONITOR_FORMAT, b"\x00")
    reader.read_new()
    api.sequence = 3
    api.formats.clear()
    api.set_format(CAN_INCLUDE_IN_HISTORY_FORMAT, DWORD_ZERO)
    reader.read_new()
    api.sequence = 4
    api.formats.clear()
    gate.own_process_id = FAKE_OWNER_PID
    reader.read_new()
    gate.own_process_id = os.getpid()
    gate.pause.pause()
    reader.read_new()
    gate.pause.resume()
    api.sequence = 5
    reader.read_new()  # captured

    text = log_output.getvalue()
    assert "clipboard_skipped_excluded_app sequence=1" in text
    assert "clipboard_skipped_opt_out_monitor sequence=2" in text
    assert "clipboard_skipped_opt_out_history sequence=3" in text
    assert "clipboard_skipped_own_write sequence=4" in text
    assert "clipboard_skipped_paused sequence=none" in text
    assert "clipboard_captured" in text and "source_from_owner=true" in text
    for private in (FAKE_TEXT, FAKE_APP, "fakeeditor", "\\"):
        assert private not in text
    assert "dropped_fields" not in text


# --- Force-save: overrides pause and exclusions, nothing else ---


def test_force_save_overrides_pause() -> None:
    api = FakeClipboard()
    gate = Gate(pause=PauseSwitch(paused=True))
    event = make_reader(api, gate).read_forced()
    assert event is not None
    assert event.text == FAKE_TEXT and event.forced is True


def test_force_save_overrides_the_exclusion_list() -> None:
    api = FakeClipboard()
    gate = Gate(excluded_apps=[FAKE_APP])
    reader = make_reader(api, gate)
    assert reader.read_new() is None  # skipped as an excluded app
    event = reader.read_forced()
    assert event is not None and event.forced is True


@pytest.mark.parametrize(
    ("name", "data"),
    [(EXCLUDE_FROM_MONITOR_FORMAT, b"\x00"), (CAN_INCLUDE_IN_HISTORY_FORMAT, DWORD_ZERO)],
)
def test_force_save_still_refuses_opt_out_formats(name: str, data: bytes) -> None:
    api = FakeClipboard()
    api.set_format(name, data)
    gate = Gate(pause=PauseSwitch(paused=True), excluded_apps=[FAKE_APP])
    assert make_reader(api, gate).read_forced() is None
    assert api.text_reads == 0


def test_force_save_still_refuses_our_own_writes() -> None:
    api = FakeClipboard()
    gate = Gate(own_process_id=FAKE_OWNER_PID)
    assert make_reader(api, gate).read_forced() is None
    assert api.text_reads == 0


def test_force_save_does_not_store_a_copy_twice() -> None:
    api = FakeClipboard(sequence=9)
    reader = make_reader(api)
    assert reader.read_new() is not None
    assert reader.read_forced() is None
    assert api.text_reads == 1


def test_normal_read_does_not_repeat_a_force_saved_copy() -> None:
    api = FakeClipboard(sequence=9)
    gate = Gate(pause=PauseSwitch(paused=True))
    reader = make_reader(api, gate)
    assert reader.read_forced() is not None
    gate.pause.resume()
    assert reader.read_new() is None


def test_force_save_works_again_for_a_new_copy() -> None:
    api = FakeClipboard(sequence=9)
    gate = Gate(pause=PauseSwitch(paused=True))
    reader = make_reader(api, gate)
    assert reader.read_forced() is not None
    api.sequence = 10
    assert reader.read_forced() is not None


def test_force_save_logs_reasons_only(log_output: io.StringIO) -> None:
    api = FakeClipboard(sequence=3)
    api.set_format(EXCLUDE_FROM_MONITOR_FORMAT, b"\x00")
    gate = Gate(pause=PauseSwitch(paused=True), excluded_apps=[FAKE_APP])
    reader = make_reader(api, gate)
    reader.read_forced()
    api.formats.clear()
    reader.read_forced()
    reader.read_forced()
    text = log_output.getvalue()
    assert "force_save_refused_opt_out_monitor sequence=3" in text
    assert "clipboard_captured" in text and "forced=true" in text
    assert "force_save_already_captured sequence=3" in text
    assert FAKE_TEXT not in text and FAKE_APP not in text


def test_listener_force_save_request_while_paused() -> None:
    events: "queue.Queue[ClipboardEvent]" = queue.Queue()
    api = FakeClipboard()
    listener = ClipboardListener(events, api=api, windows=FakeWindows(), debounce_ms=20)
    listener.gate.pause.pause()
    assert listener.request_force_save() is False  # not running yet
    listener.start()
    try:
        assert listener.request_force_save() is True
        event = events.get(timeout=3)
    finally:
        listener.stop()
    assert event.forced is True and event.text == FAKE_TEXT


# --- The real listener thread, fake clipboard behind it ---


def test_listener_thread_respects_pause() -> None:
    events: "queue.Queue[ClipboardEvent]" = queue.Queue()
    api = FakeClipboard()
    listener = ClipboardListener(events, api=api, windows=FakeWindows(), debounce_ms=20)
    listener.gate.pause.pause()
    listener.start()
    try:
        handle = listener.window_handle
        assert handle is not None
        win32gui.PostMessage(handle, WM_CLIPBOARDUPDATE, 0, 0)
        time.sleep(0.2)
        assert events.empty()
        assert api.opens == 0

        listener.gate.pause.resume()
        win32gui.PostMessage(handle, WM_CLIPBOARDUPDATE, 0, 0)
        assert events.get(timeout=3).text == FAKE_TEXT
    finally:
        listener.stop()


def test_listener_uses_the_gate_it_was_given() -> None:
    gate = Gate()
    listener = ClipboardListener(queue.Queue(), api=FakeClipboard(), gate=gate)
    assert listener.gate is gate


# --- Opt-in: the real clipboard ---

real_clipboard = pytest.mark.skipif(
    os.environ.get("SMARTCLIP_REAL_CLIPBOARD_TESTS") != "1",
    reason="replaces the real clipboard; set SMARTCLIP_REAL_CLIPBOARD_TESTS=1 to run",
)


def _write_real(
    text: str, extra: dict[str, bytes] | None = None, window: int | None = None
) -> None:
    """Write fake text and formats, opening the clipboard for `window`.

    With a window, emptying the clipboard makes that window the owner, which
    is how copy-back will write. With None the clipboard has no owner.
    """
    win32clipboard.OpenClipboard(window)
    try:
        win32clipboard.EmptyClipboard()
        win32clipboard.SetClipboardText(text, win32con.CF_UNICODETEXT)
        for name, data in (extra or {}).items():
            fmt = win32clipboard.RegisterClipboardFormat(name)
            win32clipboard.SetClipboardData(fmt, data)
    finally:
        win32clipboard.CloseClipboard()


def _empty_real() -> None:
    win32clipboard.OpenClipboard(None)
    try:
        win32clipboard.EmptyClipboard()
    finally:
        win32clipboard.CloseClipboard()


@real_clipboard
def test_real_own_write_is_skipped_by_its_owner() -> None:
    events: "queue.Queue[ClipboardEvent]" = queue.Queue()
    listener = ClipboardListener(events)
    # A window of this process, standing in for the one copy-back will use.
    our_window = win32gui.CreateWindow("STATIC", "", 0, 0, 0, 0, 0, 0, 0, 0, None)
    listener.start()
    try:
        _write_real(FAKE_TEXT, window=our_window)
        time.sleep(0.5)
        assert events.empty(), "our own write was captured"

        # A write with no owner is not ours, so it is captured.
        _write_real(FAKE_TEXT + "-second")
        assert events.get(timeout=3).text == FAKE_TEXT + "-second"
    finally:
        listener.stop()
        win32gui.DestroyWindow(our_window)
        _empty_real()


@real_clipboard
@pytest.mark.parametrize(
    "extra",
    [
        {EXCLUDE_FROM_MONITOR_FORMAT: b"\x00"},
        {CAN_INCLUDE_IN_HISTORY_FORMAT: DWORD_ZERO},
    ],
)
def test_real_opt_out_formats_are_honoured(extra: dict[str, bytes]) -> None:
    events: "queue.Queue[ClipboardEvent]" = queue.Queue()
    listener = ClipboardListener(events)
    listener.start()
    try:
        _write_real(FAKE_TEXT, extra)
        time.sleep(0.5)
        assert events.empty()

        _write_real(FAKE_TEXT, {CAN_INCLUDE_IN_HISTORY_FORMAT: DWORD_ONE})
        assert events.get(timeout=3).text == FAKE_TEXT
    finally:
        listener.stop()
        _empty_real()


@real_clipboard
def test_real_force_save_while_paused() -> None:
    events: "queue.Queue[ClipboardEvent]" = queue.Queue()
    listener = ClipboardListener(events)
    listener.gate.pause.pause()
    listener.start()
    try:
        _write_real(FAKE_TEXT)
        time.sleep(0.5)
        assert events.empty()
        assert listener.request_force_save()
        event = events.get(timeout=3)
    finally:
        listener.stop()
        _empty_real()
    assert event.text == FAKE_TEXT and event.forced is True
