"""Keep the app's windows out of screenshots, screen recording, and screen sharing.

Windows lets a program mark one of its windows so that capture tools get a
blank area instead of its pixels (SetWindowDisplayAffinity with
WDA_EXCLUDEFROMCAPTURE, Windows 10 version 2004 and later). This matters
when the owner shares the screen in a call: opening the window would
otherwise show the clipboard history to everyone watching.

The mark is per window, and Qt opens more windows than the main one:
tooltips, right-click menus, and dialogs (the delete question) are each a
separate top-level window. Marking only the main window let tooltips show
in captures (found by the owner, 2026-11-19). CaptureGuard is therefore
installed on the whole Qt application and marks every top-level window just
before it is first shown.

SECURITY REVIEW (owner):

- This only covers capture through Windows' own capture paths, which
  screenshot tools, screen recorders, and meeting apps use. It does nothing
  against a phone camera, and a program running under the same account
  could undo it.
- It also hides the windows from the owner's own screenshots (Snipping Tool
  shows a black or empty area). Set config.EXCLUDE_FROM_SCREEN_CAPTURE to
  False to take screenshots of the window, for example for the README.
- On a Windows version without WDA_EXCLUDEFROMCAPTURE the call fails, and
  WDA_MONITOR is tried instead: the window then shows as black in captures.
- Qt's Show event arrives just before a window appears, so the mark is set
  before the first frame is drawn. Not proven for every kind of window
  Windows itself draws (for example the native file dialogs, which this app
  does not use).
"""

import ctypes
from ctypes import wintypes

from PySide6.QtCore import QEvent, QObject
from PySide6.QtWidgets import QWidget

from smartclip import logsafe

WDA_NONE = 0x00
WDA_MONITOR = 0x01
WDA_EXCLUDEFROMCAPTURE = 0x11

_log = logsafe.get_logger("capture_guard")

_user32 = ctypes.WinDLL("user32", use_last_error=True)
_user32.SetWindowDisplayAffinity.argtypes = [wintypes.HWND, wintypes.DWORD]
_user32.SetWindowDisplayAffinity.restype = wintypes.BOOL
_user32.GetWindowDisplayAffinity.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
_user32.GetWindowDisplayAffinity.restype = wintypes.BOOL


def exclude_from_capture(hwnd: int) -> int:
    """Mark a top-level window of this process. Returns the affinity set (WDA_NONE on failure)."""
    for affinity in (WDA_EXCLUDEFROMCAPTURE, WDA_MONITOR):
        if _user32.SetWindowDisplayAffinity(hwnd, affinity):
            return affinity
    logsafe.log_event(_log, "capture_exclusion_failed", win_error=ctypes.get_last_error())
    return WDA_NONE


def current_affinity(hwnd: int) -> int | None:
    """Read a window's affinity back, or None if Windows refuses."""
    value = wintypes.DWORD()
    if not _user32.GetWindowDisplayAffinity(hwnd, ctypes.byref(value)):
        return None
    return value.value


class CaptureGuard(QObject):
    """Application-wide event filter that marks every top-level window as it is shown.

    Install with `QApplication.instance().installEventFilter(guard)` before
    any window is shown. It never stops an event; it only looks at them.
    """

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.marked = 0

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:  # noqa: N802 (Qt name)
        if (
            event.type() == QEvent.Type.Show
            and isinstance(watched, QWidget)
            and watched.isWindow()
        ):
            self.guard(watched)
        return False

    def guard(self, window: QWidget) -> int:
        """Mark one window unless it already is. Returns its affinity afterwards."""
        hwnd = int(window.winId())
        current = current_affinity(hwnd)
        if current not in (None, WDA_NONE):
            return current
        applied = exclude_from_capture(hwnd)
        if applied != WDA_NONE:
            self.marked += 1
            logsafe.log_event(
                _log, "capture_exclusion_set", affinity=applied, windows_marked=self.marked
            )
        return applied
