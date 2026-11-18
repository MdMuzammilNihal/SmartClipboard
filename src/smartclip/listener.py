"""Clipboard listener: receives change events from Windows and reads text.

How it works:

1. A background thread creates a hidden, message-only window and registers it
   with AddClipboardFormatListener. Windows then sends that window a
   WM_CLIPBOARDUPDATE message every time the clipboard changes. Nothing is
   polled.
2. One copy can produce several of those messages (an app may write its
   formats in more than one step). Each message restarts a short timer, and
   the clipboard is read only when the timer fires, so a burst of messages
   becomes one read.
3. Before reading, the privacy gate (gate.py) is asked. If capture is paused
   the clipboard is not opened at all. Otherwise the read opens the
   clipboard, retrying a few times because another app may be holding it,
   and while it is open: reads the sequence number, guesses the source app
   (sourceapp.py), and lets the gate check for our own writes, the Windows
   opt-out formats, and the exclusion list. Only if the gate allows it is
   the text taken. Non-text content is ignored.
4. Force-save (task 11): request_force_save() asks the listener thread to
   read what is on the clipboard now, overriding pause and the exclusion
   list but not the opt-out formats or our own writes (gate.skip_forced).
5. The result is put on a queue. The listener never touches the UI or the
   database; whoever owns the queue does that on their own thread.
6. Copy-back (task 12): request_copy_back() asks the listener thread to
   write an item back to the clipboard for the listener's own window, and
   for a secret, to clear it again later (copyback.py).
7. Captured secrets (owner's decision 2026-11-19, option B):
   request_capture_clear() asks the listener thread to empty the
   clipboard 30 seconds after a clearly secret item was captured, if
   nothing was copied since (copyback.py).

SECURITY REVIEW (owner):

- The captured text travels only inside ClipboardEvent objects on the queue.
  This module logs lengths, sequence numbers, and skip reasons, never the
  text or the source app name.
- A change the gate refuses is never read: get_text() is not called, so the
  text does not even pass through memory.
- pywin32 prints a full traceback, message included, if a window-message
  handler raises. Every handler here therefore catches all exceptions and
  logs the exception class name only.
- Pause is checked when the debounce timer fires, not at the moment of the
  copy. A copy made just before pausing can therefore be skipped; one made
  while paused is never captured.
- There is no size limit on captured text yet.

Not yet verified: the logic is tested with a fake clipboard and with real
window messages. Capturing real copies from other apps is not covered by the
default tests; the owner checks it with tools/watch_clipboard.py or the
opt-in test in tests/test_listener.py.
"""

import ctypes
import logging
import queue
import threading
import time
from collections.abc import Callable
from concurrent.futures import Future
from ctypes import wintypes
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Protocol

import pywintypes
import win32api
import win32clipboard
import win32con
import win32gui

from smartclip import logsafe
from smartclip.copyback import ClearOutcome, CopyBack
from smartclip.gate import FormatReader, Gate, OptOutFormats, SkipReason
from smartclip.sourceapp import (
    UNKNOWN_SOURCE,
    SourceApp,
    SourceMethod,
    Win32WindowApi,
    WindowApi,
    find_source_app,
)

WM_CLIPBOARDUPDATE = 0x031D
# Our own message asking the listener thread for a force-save read.
WM_FORCE_SAVE = win32con.WM_APP + 1
# Our own message asking the listener thread to run queued copy-backs.
WM_COPY_BACK = win32con.WM_APP + 2
# Our own message asking the listener thread to (re)schedule or cancel the
# clear of a captured secret.
WM_CAPTURE_CLEAR = win32con.WM_APP + 3

# How long to wait after the last change message before reading. Long enough
# to merge the messages from one copy, short enough to feel instant.
DEBOUNCE_MS = 75
# Opening the clipboard fails while another app holds it. The waits grow:
# 20 ms, 40 ms, 60 ms, 80 ms, so about 0.2 seconds in total before giving up.
OPEN_ATTEMPTS = 5
RETRY_DELAY_SECONDS = 0.02

_TIMER_ID = 1
# Timer for clearing a secret after copy-back.
_CLEAR_TIMER_ID = 2
# Timer for clearing a secret captured from another app.
_CAPTURE_CLEAR_TIMER_ID = 3
# If the clipboard is busy when the clear is due, try again this often, at
# most this many times, before giving up.
CLEAR_RETRY_MS = 1000
CLEAR_RETRY_LIMIT = 5
_START_TIMEOUT_SECONDS = 5.0
_STOP_TIMEOUT_SECONDS = 5.0

_log = logsafe.get_logger("listener")

# pywin32 does not expose these functions, so they are called through ctypes.
_user32 = ctypes.WinDLL("user32", use_last_error=True)
_user32.AddClipboardFormatListener.argtypes = [wintypes.HWND]
_user32.AddClipboardFormatListener.restype = wintypes.BOOL
_user32.RemoveClipboardFormatListener.argtypes = [wintypes.HWND]
_user32.RemoveClipboardFormatListener.restype = wintypes.BOOL
_user32.SetTimer.argtypes = [wintypes.HWND, ctypes.c_size_t, wintypes.UINT, ctypes.c_void_p]
_user32.SetTimer.restype = ctypes.c_size_t
_user32.KillTimer.argtypes = [wintypes.HWND, ctypes.c_size_t]
_user32.KillTimer.restype = wintypes.BOOL


class ClipboardBusyError(Exception):
    """Another app is holding the clipboard open."""


class ListenerError(Exception):
    """The listener thread could not start."""


@dataclass(frozen=True)
class ClipboardEvent:
    """One captured text copy."""

    text: str
    sequence_number: int
    captured_at: datetime
    # Lower-case executable name, a best guess (see sourceapp.py).
    source_app: str | None = None
    source_method: SourceMethod = SourceMethod.UNKNOWN
    # True when the owner force-saved it with the hotkey.
    forced: bool = False


class ClipboardApi(FormatReader, Protocol):
    """The clipboard operations the reader needs. Tests supply a fake.

    Includes the format checks the gate needs (FormatReader).
    """

    def open(self, window: int | None) -> None:
        """Open the clipboard for a window (or None for no window).

        Raises ClipboardBusyError if another app holds it.
        """

    def close(self) -> None:
        """Close the clipboard."""

    def get_text(self) -> str | None:
        """Return the clipboard's text, or None if it holds no text."""

    def sequence_number(self) -> int:
        """Return Windows' counter that increases on every clipboard change."""


class Win32Clipboard:
    """The real Windows clipboard."""

    def open(self, window: int | None) -> None:
        try:
            win32clipboard.OpenClipboard(window)
        except pywintypes.error:
            raise ClipboardBusyError from None

    def close(self) -> None:
        win32clipboard.CloseClipboard()

    def get_text(self) -> str | None:
        if not win32clipboard.IsClipboardFormatAvailable(win32con.CF_UNICODETEXT):
            return None
        try:
            data = win32clipboard.GetClipboardData(win32con.CF_UNICODETEXT)
        except (pywintypes.error, TypeError):
            # The text was there a moment ago but could not be fetched, for
            # example because the source app failed to supply it.
            return None
        return data if isinstance(data, str) else None

    def sequence_number(self) -> int:
        return win32clipboard.GetClipboardSequenceNumber()

    def register_format(self, name: str) -> int:
        return win32clipboard.RegisterClipboardFormat(name)

    def has_format(self, format_id: int) -> bool:
        return bool(win32clipboard.IsClipboardFormatAvailable(format_id))

    def get_format_bytes(self, format_id: int) -> bytes | None:
        try:
            data = win32clipboard.GetClipboardData(format_id)
        except (pywintypes.error, TypeError):
            return None
        return data if isinstance(data, bytes) else None


class ClipboardReader:
    """Reads the clipboard once per change, with retry when it is busy."""

    def __init__(
        self,
        api: ClipboardApi,
        gate: Gate | None = None,
        windows: WindowApi | None = None,
        attempts: int = OPEN_ATTEMPTS,
        delay_seconds: float = RETRY_DELAY_SECONDS,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._api = api
        self._gate = gate or Gate()
        self._windows = windows or Win32WindowApi()
        self._formats = OptOutFormats(api)
        self._attempts = attempts
        self._delay_seconds = delay_seconds
        self._sleep = sleep
        self._last_sequence: int | None = None
        # The latest change that was actually captured, so force-save does not
        # store the same copy twice.
        self._last_captured_sequence: int | None = None
        # The window the clipboard is opened for. The listener sets this to
        # its hidden window. Seen on Windows 11 (owner's machine, 2026-11-18):
        # with no window (None), OpenClipboard reported success on the
        # listener thread after the first reads, yet the thread did not hold
        # the clipboard, so reads failed and CloseClipboard raised error 1418.
        # Opening for the listener's own window fixed it. The exact Windows
        # reason was not confirmed.
        self.window: int | None = None

    def read_new(self) -> ClipboardEvent | None:
        """Return an event if the clipboard holds text not yet seen, else None."""
        skip = self._gate.skip_before_open()
        if skip is not None:
            _log_skip(skip, sequence=None)
            return None
        return self._read(forced=False)

    def read_forced(self) -> ClipboardEvent | None:
        """Force-save: capture what the clipboard holds now, if allowed.

        Overrides pause and the exclusion list. Still refuses our own writes,
        the opt-out formats, and a change that was already captured.
        """
        return self._read(forced=True)

    def _read(self, forced: bool) -> ClipboardEvent | None:
        """Open the clipboard, run the gate, and take the text if allowed."""
        if not self._open_with_retry():
            logsafe.log_event(
                _log, "clipboard_busy", level=logging.WARNING, attempts=self._attempts
            )
            return None
        try:
            # Read the counter while the clipboard is open: no other app can
            # change the clipboard until we close it, so the counter and the
            # text are guaranteed to belong together.
            sequence = self._api.sequence_number()
            if forced and sequence == self._last_captured_sequence:
                logsafe.log_event(_log, "force_save_already_captured", sequence=sequence)
                return None
            if not forced and sequence == self._last_sequence:
                return None
            source = self._find_source()
            if forced:
                skip = self._gate.skip_forced(self._api, self._formats, source)
            else:
                skip = self._gate.skip_while_open(self._api, self._formats, source)
            # A refused change is never read.
            text = self._api.get_text() if skip is None else None
            self._last_sequence = sequence
        finally:
            self._api.close()

        if skip is not None:
            _log_skip(skip, sequence=sequence, forced=forced)
            return None
        if not text:
            logsafe.log_event(_log, "clipboard_skipped_no_text", sequence=sequence)
            return None
        self._last_captured_sequence = sequence
        logsafe.log_event(
            _log,
            "clipboard_captured",
            length=len(text),
            sequence=sequence,
            source_known=source.name is not None,
            source_from_owner=source.method is SourceMethod.CLIPBOARD_OWNER,
            forced=forced,
        )
        return ClipboardEvent(
            text=text,
            sequence_number=sequence,
            captured_at=datetime.now(timezone.utc),
            source_app=source.name,
            source_method=source.method,
            forced=forced,
        )

    def _find_source(self) -> SourceApp:
        """Guess the source app; a failed lookup must not stop the gate."""
        try:
            return find_source_app(self._windows)
        except Exception as error:  # noqa: BLE001 - a hint is not worth a lost copy
            logsafe.log_error(_log, "source_app_lookup_failed", error)
            return UNKNOWN_SOURCE

    def _open_with_retry(self) -> bool:
        """Try to open the clipboard, waiting a little longer after each failure."""
        for attempt in range(1, self._attempts + 1):
            try:
                self._api.open(self.window)
                return True
            except ClipboardBusyError:
                if attempt < self._attempts:
                    self._sleep(self._delay_seconds * attempt)
        return False


def _log_skip(reason: SkipReason, sequence: int | None, forced: bool = False) -> None:
    """Log why a change was refused. The event name comes from the enum."""
    prefix = "force_save_refused" if forced else "clipboard_skipped"
    logsafe.log_event(_log, f"{prefix}_{reason.value}", sequence=sequence)


class ClipboardListener:
    """Runs the clipboard watcher on its own thread and fills a queue."""

    def __init__(
        self,
        events: "queue.Queue[ClipboardEvent]",
        api: ClipboardApi | None = None,
        gate: Gate | None = None,
        windows: WindowApi | None = None,
        debounce_ms: int = DEBOUNCE_MS,
        copy_back: CopyBack | None = None,
    ) -> None:
        self._events = events
        self.gate = gate or Gate()
        self._reader = ClipboardReader(api or Win32Clipboard(), self.gate, windows)
        self._debounce_ms = debounce_ms
        self._copy_back = copy_back or CopyBack()
        self._copy_requests: "queue.Queue[tuple[str, bool, Future[bool]]]" = queue.Queue()
        self._clear_retries = 0
        self._capture_clear_retries = 0
        # The latest capture-clear request: a sequence number, or None to cancel.
        self._capture_clear_lock = threading.Lock()
        self._capture_clear_request: tuple[int | None] | None = None
        self._thread: threading.Thread | None = None
        self._hwnd: int | None = None
        self._ready = threading.Event()
        self._start_failed = False

    @property
    def window_handle(self) -> int | None:
        """The hidden window's handle while the listener runs, else None."""
        return self._hwnd

    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        """Start the listener thread and wait until it is receiving events."""
        if self._thread is not None:
            raise RuntimeError("listener already started")
        self._ready.clear()
        self._start_failed = False
        # A daemon thread does not keep the process alive if the app exits.
        self._thread = threading.Thread(
            target=self._run, name="smartclip-listener", daemon=True
        )
        self._thread.start()
        started = self._ready.wait(_START_TIMEOUT_SECONDS)
        if not started or self._start_failed:
            self._thread = None
            raise ListenerError("clipboard listener could not start")

    def request_force_save(self) -> bool:
        """Ask the listener thread to force-save the current clipboard.

        Safe to call from any thread (for example the hotkey thread). The
        read happens on the listener thread and its result, if any, goes on
        the queue with forced=True. Returns False if the listener is not
        running.
        """
        hwnd = self._hwnd
        if hwnd is None:
            return False
        try:
            win32gui.PostMessage(hwnd, WM_FORCE_SAVE, 0, 0)
        except pywintypes.error:
            return False
        return True

    def request_copy_back(self, text: str, sensitive: bool) -> "Future[bool] | None":
        """Ask the listener thread to put text back on the clipboard.

        Safe to call from any thread. With sensitive=True (secret and unsure
        items) the write carries the opt-out formats and is cleared after
        `config.COPYBACK_CLEAR_AFTER`. Never waits: the returned future
        becomes True once written, or False if the clipboard stayed busy.
        Returns None if the listener is not running.
        """
        hwnd = self._hwnd
        if hwnd is None:
            return None
        future: Future[bool] = Future()
        self._copy_requests.put((text, sensitive, future))
        try:
            win32gui.PostMessage(hwnd, WM_COPY_BACK, 0, 0)
        except pywintypes.error:
            # The request stays queued but is skipped when next seen, so a
            # stale copy-back can never happen later by surprise.
            future.cancel()
            return None
        return future

    def request_capture_clear(self, sequence: int | None) -> bool:
        """Ask the listener thread to clear a captured secret later, or cancel that.

        Safe to call from any thread. `sequence` is the clipboard sequence
        number the secret was read at; None cancels a pending clear. Only the
        latest request counts. Returns False if the listener is not running.
        """
        hwnd = self._hwnd
        if hwnd is None:
            return False
        with self._capture_clear_lock:
            self._capture_clear_request = (sequence,)
        try:
            win32gui.PostMessage(hwnd, WM_CAPTURE_CLEAR, 0, 0)
        except pywintypes.error:
            return False
        return True

    def stop(self) -> None:
        """Ask the listener thread to finish and wait for it."""
        thread = self._thread
        if thread is None:
            return
        hwnd = self._hwnd
        if hwnd is not None and thread.is_alive():
            try:
                # A window must be destroyed by the thread that created it, so
                # ask it to close instead of destroying it from here.
                win32gui.PostMessage(hwnd, win32con.WM_CLOSE, 0, 0)
            except pywintypes.error:
                pass
        thread.join(_STOP_TIMEOUT_SECONDS)
        self._thread = None

    # --- Everything below runs on the listener thread ---

    def _run(self) -> None:
        # The class name must be unique per listener in this process.
        class_name = f"SmartClipListener-{id(self):x}"
        instance = win32api.GetModuleHandle(None)
        try:
            self._hwnd = self._create_window(class_name, instance)
            self._reader.window = self._hwnd
        except (pywintypes.error, OSError) as error:
            self._start_failed = True
            logsafe.log_error(_log, "listener_start_failed", error)
            self._ready.set()
            return

        logsafe.log_event(_log, "listener_started")
        self._ready.set()
        try:
            # Blocks, handing each window message to the handlers below, until
            # _on_destroy posts the quit message.
            win32gui.PumpMessages()
        finally:
            self._hwnd = None
            self._reader.window = None
            try:
                win32gui.UnregisterClass(class_name, instance)
            except pywintypes.error:
                pass
            logsafe.log_event(_log, "listener_stopped")

    def _create_window(self, class_name: str, instance: int) -> int:
        """Create the hidden window and register it for clipboard updates."""
        window_class = win32gui.WNDCLASS()
        window_class.lpszClassName = class_name
        window_class.hInstance = instance
        window_class.lpfnWndProc = {
            WM_CLIPBOARDUPDATE: self._on_update,
            win32con.WM_TIMER: self._on_timer,
            WM_FORCE_SAVE: self._on_force_save,
            WM_COPY_BACK: self._on_copy_back,
            WM_CAPTURE_CLEAR: self._on_capture_clear,
            win32con.WM_DESTROY: self._on_destroy,
        }
        atom = win32gui.RegisterClass(window_class)
        # HWND_MESSAGE makes a message-only window: never visible, not in the
        # taskbar, and it does not receive broadcast messages.
        hwnd = win32gui.CreateWindow(
            atom, "", 0, 0, 0, 0, 0, win32con.HWND_MESSAGE, 0, instance, None
        )
        if not _user32.AddClipboardFormatListener(hwnd):
            win32gui.DestroyWindow(hwnd)
            raise OSError("AddClipboardFormatListener failed")
        return hwnd

    def _on_update(self, hwnd: int, _message: int, _wparam: int, _lparam: int) -> int:
        """The clipboard changed: (re)start the timer instead of reading now."""
        # Setting a timer that already exists restarts it, which is what
        # merges a burst of change messages into one read.
        _user32.SetTimer(hwnd, _TIMER_ID, self._debounce_ms, None)
        return 0

    def _on_timer(self, hwnd: int, _message: int, wparam: int, _lparam: int) -> int:
        """A timer fired: the copy-back clear, or the end of a burst of changes."""
        if wparam == _CLEAR_TIMER_ID:
            return self._on_clear_timer(hwnd)
        if wparam == _CAPTURE_CLEAR_TIMER_ID:
            return self._on_capture_clear_timer(hwnd)
        # The burst is over: read the clipboard once and queue the result.
        _user32.KillTimer(hwnd, _TIMER_ID)
        try:
            event = self._reader.read_new()
            if event is not None:
                self._events.put(event)
        except Exception as error:  # noqa: BLE001 - see SECURITY REVIEW above
            logsafe.log_error(_log, "capture_failed", error)
        return 0

    def _on_force_save(self, _hwnd: int, _message: int, _wparam: int, _lparam: int) -> int:
        """The force-save hotkey was pressed: read once, overriding pause."""
        try:
            event = self._reader.read_forced()
            if event is not None:
                self._events.put(event)
        except Exception as error:  # noqa: BLE001 - see SECURITY REVIEW above
            logsafe.log_error(_log, "force_save_failed", error)
        return 0

    def _on_copy_back(self, hwnd: int, _message: int, _wparam: int, _lparam: int) -> int:
        """Run every queued copy-back request, oldest first."""
        while True:
            try:
                text, sensitive, future = self._copy_requests.get_nowait()
            except queue.Empty:
                return 0
            if not future.set_running_or_notify_cancel():
                continue  # cancelled because the request could not be posted
            written = False
            try:
                written = self._copy_back.write(hwnd, text, sensitive)
                if written and sensitive:
                    self._clear_retries = 0
                    clear_ms = int(self._copy_back.clear_after.total_seconds() * 1000)
                    _user32.SetTimer(hwnd, _CLEAR_TIMER_ID, clear_ms, None)
                elif written:
                    _user32.KillTimer(hwnd, _CLEAR_TIMER_ID)
            except Exception as error:  # noqa: BLE001 - see SECURITY REVIEW above
                logsafe.log_error(_log, "copy_back_failed", error)
            finally:
                future.set_result(written)

    def _on_clear_timer(self, hwnd: int) -> int:
        """The copy-back time is up: clear the secret if it is still ours."""
        _user32.KillTimer(hwnd, _CLEAR_TIMER_ID)
        try:
            outcome = self._copy_back.clear_if_ours(hwnd)
            if outcome is ClearOutcome.BUSY:
                self._clear_retries += 1
                if self._clear_retries < CLEAR_RETRY_LIMIT:
                    _user32.SetTimer(hwnd, _CLEAR_TIMER_ID, CLEAR_RETRY_MS, None)
                else:
                    self._copy_back.give_up_clear()
        except Exception as error:  # noqa: BLE001 - see SECURITY REVIEW above
            logsafe.log_error(_log, "copy_back_clear_failed", error)
        return 0

    def _on_capture_clear(self, hwnd: int, _message: int, _wparam: int, _lparam: int) -> int:
        """Schedule (or cancel) the clear of a captured secret."""
        with self._capture_clear_lock:
            request, self._capture_clear_request = self._capture_clear_request, None
        if request is None:
            return 0
        (sequence,) = request
        try:
            if sequence is None:
                _user32.KillTimer(hwnd, _CAPTURE_CLEAR_TIMER_ID)
                self._copy_back.cancel_capture_clear()
            else:
                self._capture_clear_retries = 0
                self._copy_back.schedule_capture_clear(sequence)
                clear_ms = int(self._copy_back.capture_clear_after.total_seconds() * 1000)
                _user32.SetTimer(hwnd, _CAPTURE_CLEAR_TIMER_ID, clear_ms, None)
        except Exception as error:  # noqa: BLE001 - see SECURITY REVIEW above
            logsafe.log_error(_log, "capture_clear_schedule_failed", error)
        return 0

    def _on_capture_clear_timer(self, hwnd: int) -> int:
        """The time is up: clear the captured secret if nothing was copied since."""
        _user32.KillTimer(hwnd, _CAPTURE_CLEAR_TIMER_ID)
        try:
            outcome = self._copy_back.clear_capture_if_unchanged(hwnd)
            if outcome is ClearOutcome.BUSY:
                self._capture_clear_retries += 1
                if self._capture_clear_retries < CLEAR_RETRY_LIMIT:
                    _user32.SetTimer(hwnd, _CAPTURE_CLEAR_TIMER_ID, CLEAR_RETRY_MS, None)
                else:
                    self._copy_back.give_up_capture_clear()
        except Exception as error:  # noqa: BLE001 - see SECURITY REVIEW above
            logsafe.log_error(_log, "capture_clear_failed", error)
        return 0

    def _on_destroy(self, hwnd: int, _message: int, _wparam: int, _lparam: int) -> int:
        """The window is closing: clear a pending secret, unregister, end the loop."""
        _user32.KillTimer(hwnd, _TIMER_ID)
        _user32.KillTimer(hwnd, _CLEAR_TIMER_ID)
        _user32.KillTimer(hwnd, _CAPTURE_CLEAR_TIMER_ID)
        try:
            # Do not leave a secret on the clipboard after the app is gone.
            self._copy_back.clear_if_ours(hwnd)
            self._copy_back.clear_capture_if_unchanged(hwnd)
        except Exception as error:  # noqa: BLE001 - see SECURITY REVIEW above
            logsafe.log_error(_log, "copy_back_clear_failed", error)
        _user32.RemoveClipboardFormatListener(hwnd)
        win32gui.PostQuitMessage(0)
        return 0
