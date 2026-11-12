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
3. The read opens the clipboard, retrying a few times because another app may
   be holding it, and takes the text if there is any. Non-text content is
   ignored.
4. The result is put on a queue. The listener never touches the UI or the
   database; whoever owns the queue does that on their own thread.

SECURITY REVIEW (owner):

- The captured text travels only inside ClipboardEvent objects on the queue.
  This module logs lengths and sequence numbers, never the text.
- pywin32 prints a full traceback, message included, if a window-message
  handler raises. Every handler here therefore catches all exceptions and
  logs the exception class name only.
- At this stage the listener captures every text copy. Pause mode, the app
  exclusion list, and the Windows opt-out formats that password managers set
  are NOT implemented yet, so this must not be used for daily capture until
  the gate exists.
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

WM_CLIPBOARDUPDATE = 0x031D

# How long to wait after the last change message before reading. Long enough
# to merge the messages from one copy, short enough to feel instant.
DEBOUNCE_MS = 75
# Opening the clipboard fails while another app holds it. The waits grow:
# 20 ms, 40 ms, 60 ms, 80 ms, so about 0.2 seconds in total before giving up.
OPEN_ATTEMPTS = 5
RETRY_DELAY_SECONDS = 0.02

_TIMER_ID = 1
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


class ClipboardApi(Protocol):
    """The clipboard operations the reader needs. Tests supply a fake."""

    def open(self) -> None:
        """Open the clipboard. Raises ClipboardBusyError if another app holds it."""

    def close(self) -> None:
        """Close the clipboard."""

    def get_text(self) -> str | None:
        """Return the clipboard's text, or None if it holds no text."""

    def sequence_number(self) -> int:
        """Return Windows' counter that increases on every clipboard change."""


class Win32Clipboard:
    """The real Windows clipboard."""

    def open(self) -> None:
        try:
            win32clipboard.OpenClipboard(None)
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


class ClipboardReader:
    """Reads the clipboard once per change, with retry when it is busy."""

    def __init__(
        self,
        api: ClipboardApi,
        attempts: int = OPEN_ATTEMPTS,
        delay_seconds: float = RETRY_DELAY_SECONDS,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._api = api
        self._attempts = attempts
        self._delay_seconds = delay_seconds
        self._sleep = sleep
        self._last_sequence: int | None = None

    def read_new(self) -> ClipboardEvent | None:
        """Return an event if the clipboard holds text not yet seen, else None."""
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
            if sequence == self._last_sequence:
                return None
            text = self._api.get_text()
            self._last_sequence = sequence
        finally:
            self._api.close()

        if not text:
            logsafe.log_event(_log, "clipboard_skipped_no_text", sequence=sequence)
            return None
        logsafe.log_event(_log, "clipboard_captured", length=len(text), sequence=sequence)
        return ClipboardEvent(
            text=text, sequence_number=sequence, captured_at=datetime.now(timezone.utc)
        )

    def _open_with_retry(self) -> bool:
        """Try to open the clipboard, waiting a little longer after each failure."""
        for attempt in range(1, self._attempts + 1):
            try:
                self._api.open()
                return True
            except ClipboardBusyError:
                if attempt < self._attempts:
                    self._sleep(self._delay_seconds * attempt)
        return False


class ClipboardListener:
    """Runs the clipboard watcher on its own thread and fills a queue."""

    def __init__(
        self,
        events: "queue.Queue[ClipboardEvent]",
        api: ClipboardApi | None = None,
        debounce_ms: int = DEBOUNCE_MS,
    ) -> None:
        self._events = events
        self._reader = ClipboardReader(api or Win32Clipboard())
        self._debounce_ms = debounce_ms
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

    def _on_timer(self, hwnd: int, _message: int, _wparam: int, _lparam: int) -> int:
        """The burst is over: read the clipboard once and queue the result."""
        _user32.KillTimer(hwnd, _TIMER_ID)
        try:
            event = self._reader.read_new()
            if event is not None:
                self._events.put(event)
        except Exception as error:  # noqa: BLE001 - see SECURITY REVIEW above
            logsafe.log_error(_log, "capture_failed", error)
        return 0

    def _on_destroy(self, hwnd: int, _message: int, _wparam: int, _lparam: int) -> int:
        """The window is closing: unregister and end the message loop."""
        _user32.KillTimer(hwnd, _TIMER_ID)
        _user32.RemoveClipboardFormatListener(hwnd)
        win32gui.PostQuitMessage(0)
        return 0
