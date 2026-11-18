"""Copy-back (task 12): put a stored item back on the clipboard.

A normal item is written as plain text. A secret or unsure item (the caller
decides, see retention.is_sensitive) is written safely:

1. The text is written together with the three Windows opt-out formats,
   each set to the DWORD 0: `ExcludeClipboardContentFromMonitorProcessing`,
   `CanIncludeInClipboardHistory`, and `CanUploadToCloudClipboard`. Windows
   then keeps it out of clipboard history (Win+V) and cloud sync, and
   cooperating clipboard managers skip it.
2. The clipboard is opened for this app's listener window, so the listener's
   gate sees our own window as the clipboard owner and does not capture the
   write again (task 15).
3. After `COPYBACK_CLEAR_AFTER` (default 30 seconds) the clipboard is
   emptied, but only if it still holds our write.

The listener thread runs all of this (listener.request_copy_back), so every
clipboard open by this app happens on one thread, for the window whose
thread pumps messages. Windows sends clipboard messages to the owner
window; an owner on a thread that does not pump messages could make other
apps hang while they copy.

SECURITY REVIEW (owner):

- "Still holds our write" is checked by the clipboard owner, not by the
  sequence number CLAUDE.md section 9 first named (owner's decision,
  2026-11-18). The gate found on the
  owner's machine that closing the clipboard raises the sequence number by
  more than one, so the number we could record while writing is never the
  one we would read later (see gate.py). Any other app that copies must
  empty the clipboard, which makes its own window (or no window) the owner.
  So the check is: open the clipboard, and only if the owner is still the
  listener window, empty it. While we hold it open no other app can change
  it, so the check and the clear see the same content.
- Known gap: an app that adds data without emptying the clipboard first
  (unusual, and against Windows' rules for writers) would leave us as the
  owner, and its data would be cleared with ours.
- Only the latest safe copy-back is cleared. A plain copy-back after a safe
  one cancels the pending clear, since the clipboard then holds the plain
  item. A second safe copy-back restarts the 30 seconds.
- When the listener stops (app exit) a pending clear runs at once, so a
  secret is not left on the clipboard after the app is gone.
- If the clipboard is busy when the clear is due, it is retried every
  second, a few times; if it still fails the secret stays on the clipboard
  and the failure is logged (without content).
- Unsure items get the safe copy-back too (owner's decision, 2026-11-18):
  they may be secrets and already get the short secret retention.
- The opt-out formats are cooperative: a clipboard manager that ignores
  them can still record the secret during the 30 seconds.
- The text is held in memory only while it is being written and while it
  waits in the request queue. Logs record lengths and outcomes only.

Tested with a fake clipboard, and with opt-in tests that write fake text to
the real clipboard (passing on the owner's Windows 11 machine, 2026-11-18).
Not yet checked that Win+V history
and cloud sync really leave the item out, or how other clipboard managers
treat it.
"""

import time
from collections.abc import Callable
from datetime import timedelta
from enum import StrEnum
from typing import Protocol

import pywintypes
import win32clipboard
import win32con

from smartclip import config, logsafe
from smartclip.gate import CAN_INCLUDE_IN_HISTORY_FORMAT, EXCLUDE_FROM_MONITOR_FORMAT

CAN_UPLOAD_TO_CLOUD_FORMAT = "CanUploadToCloudClipboard"
OPT_OUT_FORMAT_NAMES = (
    EXCLUDE_FROM_MONITOR_FORMAT,
    CAN_INCLUDE_IN_HISTORY_FORMAT,
    CAN_UPLOAD_TO_CLOUD_FORMAT,
)
_DWORD_ZERO = (0).to_bytes(4, "little")

# Same retry pattern as the listener's reads: about 0.2 seconds in total.
OPEN_ATTEMPTS = 5
RETRY_DELAY_SECONDS = 0.02

_log = logsafe.get_logger("copyback")


class ClearOutcome(StrEnum):
    """What happened when a pending clear was due. Values appear in logs."""

    CLEARED = "cleared"
    REPLACED = "replaced"  # the clipboard holds something newer; left alone
    NOTHING_PENDING = "nothing_pending"
    BUSY = "busy"


class ClipboardWriter(Protocol):
    """The clipboard operations copy-back needs. Tests supply a fake."""

    def open(self, window: int) -> bool:
        """Open the clipboard for a window. Returns False if another app holds it."""

    def close(self) -> None:
        """Close the clipboard."""

    def empty(self) -> None:
        """Empty the clipboard, making the open window its owner."""

    def set_text(self, text: str) -> None:
        """Put Unicode text on the clipboard."""

    def set_format_bytes(self, format_id: int, data: bytes) -> None:
        """Put raw data in a registered format."""

    def register_format(self, name: str) -> int:
        """Return the ID Windows uses for a named clipboard format."""

    def owner(self) -> int | None:
        """Return the window that owns the clipboard, or None."""


class Win32ClipboardWriter:
    """The real Windows clipboard."""

    def open(self, window: int) -> bool:
        try:
            win32clipboard.OpenClipboard(window)
        except pywintypes.error:
            return False
        return True

    def close(self) -> None:
        win32clipboard.CloseClipboard()

    def empty(self) -> None:
        win32clipboard.EmptyClipboard()

    def set_text(self, text: str) -> None:
        win32clipboard.SetClipboardData(win32con.CF_UNICODETEXT, text)

    def set_format_bytes(self, format_id: int, data: bytes) -> None:
        win32clipboard.SetClipboardData(format_id, data)

    def register_format(self, name: str) -> int:
        return win32clipboard.RegisterClipboardFormat(name)

    def owner(self) -> int | None:
        try:
            return win32clipboard.GetClipboardOwner() or None
        except pywintypes.error:
            return None


class CopyBack:
    """Writes items back and clears safe copies later. Use from one thread only."""

    def __init__(
        self,
        api: ClipboardWriter | None = None,
        clear_after: timedelta = config.COPYBACK_CLEAR_AFTER,
        attempts: int = OPEN_ATTEMPTS,
        delay_seconds: float = RETRY_DELAY_SECONDS,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._api = api or Win32ClipboardWriter()
        self.clear_after = clear_after
        self._attempts = attempts
        self._delay_seconds = delay_seconds
        self._sleep = sleep
        self._opt_out_ids = [self._api.register_format(name) for name in OPT_OUT_FORMAT_NAMES]
        self._clear_pending = False

    @property
    def clear_pending(self) -> bool:
        """True while a safe copy-back waits to be cleared."""
        return self._clear_pending

    def write(self, window: int, text: str, sensitive: bool) -> bool:
        """Put text on the clipboard as `window`. Returns False if the clipboard stayed busy.

        With sensitive=True the opt-out formats are added and a clear becomes
        pending; the caller schedules clear_if_ours() after `clear_after`.
        """
        if not self._open_with_retry(window):
            logsafe.log_event(_log, "copy_back_busy", attempts=self._attempts)
            return False
        try:
            self._api.empty()
            self._api.set_text(text)
            if sensitive:
                for format_id in self._opt_out_ids:
                    self._api.set_format_bytes(format_id, _DWORD_ZERO)
        finally:
            self._api.close()
        self._clear_pending = sensitive
        logsafe.log_event(_log, "copy_back_written", length=len(text), sensitive=sensitive)
        return True

    def clear_if_ours(self, window: int) -> ClearOutcome:
        """Empty the clipboard if a safe copy-back is pending and still on it."""
        if not self._clear_pending:
            return ClearOutcome.NOTHING_PENDING  # not logged: normal on every stop
        if not self._open_with_retry(window):
            outcome = ClearOutcome.BUSY
        else:
            try:
                if self._api.owner() == window:
                    self._api.empty()
                    outcome = ClearOutcome.CLEARED
                else:
                    outcome = ClearOutcome.REPLACED
            finally:
                self._api.close()
        if outcome is not ClearOutcome.BUSY:
            self._clear_pending = False
        logsafe.log_event(_log, f"copy_back_clear_{outcome.value}")
        return outcome

    def give_up_clear(self) -> None:
        """Stop trying to clear after repeated busy failures."""
        if self._clear_pending:
            self._clear_pending = False
            logsafe.log_event(_log, "copy_back_clear_gave_up")

    def _open_with_retry(self, window: int) -> bool:
        for attempt in range(1, self._attempts + 1):
            if self._api.open(window):
                return True
            if attempt < self._attempts:
                self._sleep(self._delay_seconds * attempt)
        return False
