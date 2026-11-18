"""Global hotkeys through Windows' RegisterHotKey.

How it works:

1. A background thread creates a hidden, message-only window and registers
   each hotkey for it with RegisterHotKey. Windows then sends that window a
   WM_HOTKEY message when the keys are pressed, whichever app is in front.
2. The handler looks up the hotkey's callback and runs it on this thread.
   Callbacks must be quick and must not touch the UI or the database
   directly: they hand the work to the thread that owns it (for example by
   putting it on a queue or posting a message to the listener).

RegisterHotKey is used instead of a keyboard hook (the `keyboard` package):
it only hears the registered combinations, not every key press, and
antivirus tools are less likely to flag it (CLAUDE.md section 11, pitfall 8).

SECURITY REVIEW (owner):

- This module never sees any other key presses. Windows delivers only the
  combinations registered here.
- A combination another app already holds cannot be registered. That is
  reported (start() returns the names that failed, and they are logged by
  number), never retried or forced.
- pywin32 prints a full traceback if a window-message handler raises, so the
  handler catches all exceptions and logs the class name only.

Not yet verified: real key presses. Tests register real hotkeys on unusual
combinations and send the WM_HOTKEY message themselves.
"""

import logging
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass

import pywintypes
import win32api
import win32con
import win32gui

from smartclip import logsafe

WM_HOTKEY = 0x0312
# Stops Windows sending the message again and again while the keys are held.
MOD_NOREPEAT = 0x4000

_MODIFIERS = {
    "ctrl": win32con.MOD_CONTROL,
    "alt": win32con.MOD_ALT,
    "shift": win32con.MOD_SHIFT,
    "win": win32con.MOD_WIN,
}
_START_TIMEOUT_SECONDS = 5.0
_STOP_TIMEOUT_SECONDS = 5.0

_log = logsafe.get_logger("hotkeys")


class HotkeyError(Exception):
    """A hotkey text could not be understood, or the thread could not start."""


@dataclass(frozen=True)
class Hotkey:
    """A key combination as Windows wants it."""

    modifiers: int
    virtual_key: int


def parse_hotkey(text: str) -> Hotkey:
    """Turn text such as "Ctrl+Alt+S" into a Hotkey.

    Accepts the modifiers Ctrl, Alt, Shift, Win (any case) and one key: a
    letter, a digit, or F1 to F24. At least one modifier is required, so a
    plain key is never taken from every app.
    """
    parts = [part.strip().lower() for part in text.split("+")]
    if len(parts) < 2 or any(not part for part in parts):
        raise HotkeyError("a hotkey needs at least one modifier and one key")
    *modifier_names, key_name = parts
    modifiers = 0
    for name in modifier_names:
        if name not in _MODIFIERS:
            raise HotkeyError("unknown modifier")
        if modifiers & _MODIFIERS[name]:
            raise HotkeyError("modifier given twice")
        modifiers |= _MODIFIERS[name]
    return Hotkey(modifiers=modifiers, virtual_key=_virtual_key(key_name))


def _virtual_key(name: str) -> int:
    """Return the Windows virtual-key code for a letter, digit, or F-key."""
    if len(name) == 1 and (name.isascii() and name.isalnum()):
        # For letters and digits the code is the upper-case character's code.
        return ord(name.upper())
    if name.startswith("f") and name[1:].isdigit():
        number = int(name[1:])
        if 1 <= number <= 24:
            return win32con.VK_F1 + number - 1
    raise HotkeyError("unsupported key")


class HotkeyListener:
    """Registers hotkeys on its own thread and runs their callbacks there."""

    def __init__(self, bindings: Mapping[str, tuple[Hotkey, Callable[[], None]]]) -> None:
        """`bindings` maps an action name to its hotkey and callback."""
        self._names = list(bindings)
        self._bindings = dict(bindings)
        self._thread: threading.Thread | None = None
        self._hwnd: int | None = None
        self._ready = threading.Event()
        self._start_failed = False
        self._failed: list[str] = []

    @property
    def window_handle(self) -> int | None:
        """The hidden window's handle while running, else None."""
        return self._hwnd

    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def hotkey_id(self, name: str) -> int:
        """The number Windows uses for an action's hotkey (its WM_HOTKEY wparam)."""
        return self._names.index(name) + 1

    def start(self) -> list[str]:
        """Start the thread and register the hotkeys.

        Returns the action names whose hotkey could not be registered, usually
        because another app already uses that combination.
        """
        if self._thread is not None:
            raise RuntimeError("hotkey listener already started")
        self._ready.clear()
        self._start_failed = False
        self._failed = []
        self._thread = threading.Thread(
            target=self._run, name="smartclip-hotkeys", daemon=True
        )
        self._thread.start()
        started = self._ready.wait(_START_TIMEOUT_SECONDS)
        if not started or self._start_failed:
            self._thread = None
            raise HotkeyError("hotkey listener could not start")
        return list(self._failed)

    def stop(self) -> None:
        """Unregister the hotkeys and wait for the thread to finish."""
        thread = self._thread
        if thread is None:
            return
        hwnd = self._hwnd
        if hwnd is not None and thread.is_alive():
            try:
                win32gui.PostMessage(hwnd, win32con.WM_CLOSE, 0, 0)
            except pywintypes.error:
                pass
        thread.join(_STOP_TIMEOUT_SECONDS)
        self._thread = None

    # --- Everything below runs on the hotkey thread ---

    def _run(self) -> None:
        class_name = f"SmartClipHotkeys-{id(self):x}"
        instance = win32api.GetModuleHandle(None)
        try:
            self._hwnd = self._create_window(class_name, instance)
        except pywintypes.error as error:
            self._start_failed = True
            logsafe.log_error(_log, "hotkeys_start_failed", error)
            self._ready.set()
            return

        self._register_all()
        self._ready.set()
        try:
            win32gui.PumpMessages()
        finally:
            self._hwnd = None
            try:
                win32gui.UnregisterClass(class_name, instance)
            except pywintypes.error:
                pass
            logsafe.log_event(_log, "hotkeys_stopped")

    def _create_window(self, class_name: str, instance: int) -> int:
        window_class = win32gui.WNDCLASS()
        window_class.lpszClassName = class_name
        window_class.hInstance = instance
        window_class.lpfnWndProc = {
            WM_HOTKEY: self._on_hotkey,
            win32con.WM_DESTROY: self._on_destroy,
        }
        atom = win32gui.RegisterClass(window_class)
        return win32gui.CreateWindow(
            atom, "", 0, 0, 0, 0, 0, win32con.HWND_MESSAGE, 0, instance, None
        )

    def _register_all(self) -> None:
        for name in self._names:
            hotkey, _callback = self._bindings[name]
            hotkey_id = self.hotkey_id(name)
            try:
                win32gui.RegisterHotKey(
                    self._hwnd,
                    hotkey_id,
                    hotkey.modifiers | MOD_NOREPEAT,
                    hotkey.virtual_key,
                )
            except pywintypes.error:
                self._failed.append(name)
                logsafe.log_event(
                    _log, "hotkey_register_failed", level=logging.WARNING, hotkey_id=hotkey_id
                )
            else:
                logsafe.log_event(_log, "hotkey_registered", hotkey_id=hotkey_id)

    def _on_hotkey(self, _hwnd: int, _message: int, wparam: int, _lparam: int) -> int:
        """A registered combination was pressed: run its callback."""
        index = wparam - 1
        if not 0 <= index < len(self._names):
            return 0
        name = self._names[index]
        _hotkey, callback = self._bindings[name]
        logsafe.log_event(_log, "hotkey_pressed", hotkey_id=wparam)
        try:
            callback()
        except Exception as error:  # noqa: BLE001 - see SECURITY REVIEW above
            logsafe.log_error(_log, "hotkey_callback_failed", error, hotkey_id=wparam)
        return 0

    def _on_destroy(self, hwnd: int, _message: int, _wparam: int, _lparam: int) -> int:
        for name in self._names:
            if name not in self._failed:
                try:
                    win32gui.UnregisterHotKey(hwnd, self.hotkey_id(name))
                except pywintypes.error:
                    pass
        win32gui.PostQuitMessage(0)
        return 0
