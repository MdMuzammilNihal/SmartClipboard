"""The privacy gate: decides whether a clipboard change may be captured.

A change is skipped, and its text is never read, when any of these hold:

1. Capture is paused (pause / private mode).
2. The change is our own write (copy-back), recognised by the clipboard
   owner window belonging to this app's own process.
3. The clipboard carries a Windows opt-out format that password managers
   set: `ExcludeClipboardContentFromMonitorProcessing` (any data), or
   `CanIncludeInClipboardHistory` with the DWORD value 0.
4. The source app is on the owner's exclusion list.

The listener (listener.ClipboardReader) asks the gate in two steps. Pause is
checked before the clipboard is opened, so a paused app does not touch the
clipboard at all. The other checks run while the clipboard is open, in the
same open/close window as the sequence number, the owner lookup, and the
text, so all of them describe the same change.

SECURITY REVIEW (owner):

- Fail closed on the opt-out formats. If `CanIncludeInClipboardHistory` is
  present but its value cannot be read, or is shorter than a 4-byte DWORD,
  the change is skipped. A missing capture is better than capturing
  something a password manager asked us not to see.
- The opt-out formats are cooperative. An app that does not set them is
  captured unless the owner adds it to the exclusion list, and the default
  exclusion list is empty (owner's decision, CLAUDE.md section 16).
- The exclusion list matches the source app's executable name, and that name
  is a best guess (see sourceapp.py). If the source cannot be found, the
  exclusion list cannot match and the change is captured.
- Own writes are recognised by the clipboard owner, not by sequence number.
  Windows makes a window the owner when it empties the clipboard while the
  clipboard is open for that window, and any later write by another app
  replaces the owner. So there is no moment in which another app's copy can
  be taken for ours. (Sequence numbers were tried first and do not work: on
  the owner's machine, closing the clipboard after a write raised the number
  by 3 more, so the number read while writing is never the one the listener
  sees. Measured 2026-11-18.)
- Consequences of that choice:
  - Copy-back (task 12) must open the clipboard for one of this app's
    windows, never with no window. A write with no window has no owner and
    would be captured as a new item.
  - Copying inside this app's own window (Ctrl+C in the search window) is
    also skipped. That item is already stored, so nothing is lost.
  - If the owner lookup fails, the write is not recognised as ours and is
    captured; duplicate merging (task 8) would then fold it into the
    existing item.
- Logs record why a change was skipped, never the text, the app name, or a
  window title.

Not yet verified on a real machine: that real password managers set these
formats. The own-write check is covered by an opt-in real-clipboard test.
Default tests use a fake clipboard.
"""

import os
import threading
from collections.abc import Iterable
from enum import StrEnum
from typing import Protocol

from smartclip import config
from smartclip.sourceapp import SourceApp, exe_name

# Windows' documented names for the opt-out formats. Their numeric IDs differ
# per session, so they are looked up with RegisterClipboardFormat.
EXCLUDE_FROM_MONITOR_FORMAT = "ExcludeClipboardContentFromMonitorProcessing"
CAN_INCLUDE_IN_HISTORY_FORMAT = "CanIncludeInClipboardHistory"

_DWORD_BYTES = 4


class SkipReason(StrEnum):
    """Why the gate refused a change. Values are used in log event names."""

    PAUSED = "paused"
    OWN_WRITE = "own_write"
    OPT_OUT_MONITOR = "opt_out_monitor"
    OPT_OUT_HISTORY = "opt_out_history"
    EXCLUDED_APP = "excluded_app"


class FormatReader(Protocol):
    """The clipboard format operations the gate needs. Tests supply a fake.

    has_format and get_format_bytes are only called while the clipboard is open.
    """

    def register_format(self, name: str) -> int:
        """Return the ID Windows uses for a named clipboard format."""

    def has_format(self, format_id: int) -> bool:
        """Return True if the clipboard currently holds this format."""

    def get_format_bytes(self, format_id: int) -> bytes | None:
        """Return the raw data of a format, or None if it cannot be read."""


class PauseSwitch:
    """Pause / private mode. Safe to flip from any thread.

    The hotkey and tray toggle that flip it come later, with the UI.
    """

    def __init__(self, paused: bool = False) -> None:
        self._paused = threading.Event()
        self._toggle_lock = threading.Lock()
        if paused:
            self._paused.set()

    @property
    def is_paused(self) -> bool:
        return self._paused.is_set()

    def pause(self) -> None:
        self._paused.set()

    def resume(self) -> None:
        self._paused.clear()

    def toggle(self) -> bool:
        """Flip the state and return True if capture is now paused."""
        # Not atomic on its own, so guard against two toggles racing.
        with self._toggle_lock:
            if self._paused.is_set():
                self._paused.clear()
            else:
                self._paused.set()
            return self._paused.is_set()


def normalise_app_names(names: Iterable[str]) -> frozenset[str]:
    """Turn exclusion-list entries into lower-case executable names.

    Entries are compared with the source app's executable name, for example
    "fakemanager.exe". A full path is cut to its file name; blanks are dropped.
    """
    cleaned = (exe_name(name) for name in names)
    return frozenset(name for name in cleaned if name is not None)


class OptOutFormats:
    """The registered IDs of the Windows opt-out formats."""

    def __init__(self, clipboard: FormatReader) -> None:
        self.exclude_from_monitor = clipboard.register_format(EXCLUDE_FROM_MONITOR_FORMAT)
        self.can_include_in_history = clipboard.register_format(
            CAN_INCLUDE_IN_HISTORY_FORMAT
        )

    def skip_reason(self, clipboard: FormatReader) -> SkipReason | None:
        """Return why the current content opts out, or None. Clipboard must be open."""
        if clipboard.has_format(self.exclude_from_monitor):
            # Its presence is the signal; the data is not read.
            return SkipReason.OPT_OUT_MONITOR
        if clipboard.has_format(self.can_include_in_history):
            data = clipboard.get_format_bytes(self.can_include_in_history)
            if data is None or len(data) < _DWORD_BYTES:
                return SkipReason.OPT_OUT_HISTORY  # unreadable: fail closed
            # Windows may hand back a memory block larger than the 4 bytes
            # written, so only the first 4 bytes are the value.
            if int.from_bytes(data[:_DWORD_BYTES], "little") == 0:
                return SkipReason.OPT_OUT_HISTORY
        return None


class Gate:
    """Holds the pause switch, this app's process ID, and the exclusion list."""

    def __init__(
        self,
        pause: PauseSwitch | None = None,
        excluded_apps: Iterable[str] = config.EXCLUDED_APPS,
        own_process_id: int | None = None,
    ) -> None:
        self.pause = pause or PauseSwitch()
        self.own_process_id = own_process_id or os.getpid()
        self._excluded_apps = normalise_app_names(excluded_apps)

    @property
    def excluded_apps(self) -> frozenset[str]:
        return self._excluded_apps

    def set_excluded_apps(self, names: Iterable[str]) -> None:
        """Replace the exclusion list, for example from the settings screen."""
        # Replacing one reference with a new frozenset is atomic in CPython,
        # so the listener thread sees either the old list or the new one.
        self._excluded_apps = normalise_app_names(names)

    def skip_before_open(self) -> SkipReason | None:
        """Checks that need no clipboard access: pause only."""
        return SkipReason.PAUSED if self.pause.is_paused else None

    def skip_while_open(
        self,
        clipboard: FormatReader,
        formats: OptOutFormats,
        source: SourceApp,
    ) -> SkipReason | None:
        """Checks that must describe the same change as the text.

        Call with the clipboard open, after looking up the source app, and
        before reading the text.
        """
        if source.owner_process_id == self.own_process_id:
            return SkipReason.OWN_WRITE
        opt_out = formats.skip_reason(clipboard)
        if opt_out is not None:
            return opt_out
        if source.name is not None and exe_name(source.name) in self._excluded_apps:
            return SkipReason.EXCLUDED_APP
        return None
