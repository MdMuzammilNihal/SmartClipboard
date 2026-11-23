"""Paste a chosen item into the app the owner was using, as Win+V does.

Owner's decision (2026-11-19): choosing an item in the window (click or
Enter) copies it back and then pastes it into the app that was in front
when the window opened. config.PASTE_ON_CHOOSE turns this off (copy only).

How it works:

1. remember() runs just before the window opens and records the window in
   front (if it belongs to another process).
2. paste(copy) records the clipboard sequence number, runs the copy-back,
   and, while this app still has the focus (Windows lets only the app in
   front hand the focus on), brings the remembered window back to the front.
3. A timer then waits until (a) the clipboard has changed, so the copy-back
   was written, (b) the remembered window really is in front, and (c) no
   Ctrl, Alt, Shift, or Windows key is held down. Then it sends Ctrl+V.
   If these are not all true within PASTE_TIMEOUT, it does not paste: the
   item is still on the clipboard for a manual Ctrl+V.

SECURITY REVIEW (owner):

- The risk is pasting into the wrong window, for example a secret into a
  chat. Ctrl+V is sent only when the window in front is exactly the one
  that was in front when the window opened. If anything else is in front,
  nothing is pasted.
- Ctrl+V is not sent while a modifier key is held: held Alt plus our Ctrl+V
  would become Ctrl+Alt+V (the open-window hotkey), and other combinations
  could trigger other apps' shortcuts.
- Secrets and unsure items still go through the safe copy-back (kept out of
  Win+V history where honored, cleared after 30 seconds). The paste does
  not change that.
- Windows blocks simulated keys into apps running as administrator when
  this app is not (UIPI). Then nothing is pasted, silently; the item is
  still on the clipboard.
- Logs record only that a paste was sent or why it was skipped.
- Not checked on the real machine yet: focus hand-back and pasting into
  real apps.
"""

import os
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from typing import Protocol

from PySide6.QtCore import QObject, QTimer

from smartclip import logsafe

PASTE_TIMEOUT = timedelta(milliseconds=1500)
STEP_MS = 20

_log = logsafe.get_logger("paste")


class PasteApi(Protocol):
    """The Windows calls a paste needs. Tests supply a fake."""

    def foreground_window(self) -> int: ...
    def is_own_window(self, window: int) -> bool: ...
    def window_exists(self, window: int) -> bool: ...
    def set_foreground(self, window: int) -> None: ...
    def sequence_number(self) -> int: ...
    def modifier_held(self) -> bool: ...
    def send_ctrl_v(self) -> None: ...


class WinPasteApi:
    """The real Windows calls, through pywin32."""

    _MODIFIERS = (0x10, 0x11, 0x12, 0x5B, 0x5C)  # Shift, Ctrl, Alt, left and right Windows
    _VK_CONTROL = 0x11
    _VK_V = 0x56
    _KEYUP = 0x0002

    def __init__(self) -> None:
        import win32api
        import win32clipboard
        import win32gui
        import win32process

        self._api = win32api
        self._clipboard = win32clipboard
        self._gui = win32gui
        self._process = win32process

    def foreground_window(self) -> int:
        return int(self._gui.GetForegroundWindow() or 0)

    def is_own_window(self, window: int) -> bool:
        _thread, pid = self._process.GetWindowThreadProcessId(window)
        return pid == os.getpid()

    def window_exists(self, window: int) -> bool:
        return bool(self._gui.IsWindow(window))

    def set_foreground(self, window: int) -> None:
        try:
            self._gui.SetForegroundWindow(window)
        except self._gui.error:
            pass  # checked again before pasting

    def sequence_number(self) -> int:
        return int(self._clipboard.GetClipboardSequenceNumber())

    def modifier_held(self) -> bool:
        return any(self._api.GetAsyncKeyState(key) & 0x8000 for key in self._MODIFIERS)

    def send_ctrl_v(self) -> None:
        keybd = self._api.keybd_event
        keybd(self._VK_CONTROL, 0, 0, 0)
        keybd(self._VK_V, 0, 0, 0)
        keybd(self._VK_V, 0, self._KEYUP, 0)
        keybd(self._VK_CONTROL, 0, self._KEYUP, 0)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class Paster(QObject):
    """Remembers the app in front and pastes into it after a copy-back. UI thread only."""

    def __init__(
        self,
        api: PasteApi | None = None,
        now: Callable[[], datetime] = _utc_now,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._api = api or WinPasteApi()
        self._now = now
        self._target: int | None = None
        self._pending: tuple[int, int, datetime] | None = None  # target, sequence, deadline
        # Called when the item was copied but not pasted, so the owner can be
        # told to press Ctrl+V themselves (UI plan item 5). Set by the window.
        self.on_copy_only: Callable[[], None] | None = None
        self._timer = QTimer(self)
        self._timer.setInterval(STEP_MS)
        self._timer.timeout.connect(self.step)

    @property
    def pending(self) -> bool:
        return self._pending is not None

    def remember(self) -> None:
        """Record the window in front, unless it is one of this app's own."""
        window = self._api.foreground_window()
        if window and not self._api.is_own_window(window):
            self._target = window

    def paste(self, copy: Callable[[], bool]) -> bool:
        """Run `copy`, then paste into the remembered window. Returns copy's result."""
        before = self._api.sequence_number()
        if not copy():
            return False
        target = self._target
        if target is None or not self._api.window_exists(target):
            logsafe.log_event(_log, "paste_skipped_no_target")
            self._copy_only()
            return True
        # While this app is still in front it may hand the focus back.
        self._api.set_foreground(target)
        self._pending = (target, before, self._now() + PASTE_TIMEOUT)
        self._timer.start()
        return True

    def step(self) -> None:
        """One check of the conditions; sends Ctrl+V once they all hold."""
        if self._pending is None:
            self._timer.stop()
            return
        target, before, deadline = self._pending
        written = self._api.sequence_number() != before
        in_front = self._api.foreground_window() == target
        keys_free = not self._api.modifier_held()
        if written and in_front and keys_free:
            self._finish()
            self._api.send_ctrl_v()
            logsafe.log_event(_log, "paste_sent")
        elif self._now() >= deadline:
            self._finish()
            if not written:
                reason = "paste_skipped_not_copied"
            elif not in_front:
                reason = "paste_skipped_target_not_in_front"
            else:
                reason = "paste_skipped_keys_held"
            logsafe.log_event(_log, reason)
            if written:
                self._copy_only()  # on the clipboard, not pasted

    def _copy_only(self) -> None:
        if self.on_copy_only is not None:
            self.on_copy_only()

    def _finish(self) -> None:
        self._pending = None
        self._timer.stop()
