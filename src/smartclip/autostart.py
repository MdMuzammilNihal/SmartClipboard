"""Start with Windows, and a Start menu shortcut (owner's decisions 2026-11-12 and 2026-11-19).

Both are switches in the window's "..." menu, off until the owner turns them
on. Both run the app with the virtual environment's pythonw.exe, the
Python that runs without a console window, so no terminal is needed.

- "Start with Windows" adds a value named SmartClipboard under the current
  user's Run key (HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Run).
  Windows runs it at login. It passes --background, so the app starts
  hidden; Ctrl+Alt+V opens the window.
- "Start menu shortcut" creates "Smart Clipboard.lnk" in the current user's
  Start menu folder, which starts the app with its window showing.

SECURITY REVIEW (owner):

- Per-user only: no admin rights, no machine-wide settings, nothing for
  other accounts. Turning a switch off deletes exactly the value or file
  this module created, and nothing else in the Run key or Start menu.
- The command is the full path of this install's pythonw.exe plus
  "-m smartclip". If the project or its .venv folder moves, the entry
  points at the old path and does nothing; turning the switch off and on
  again writes the new path.
- Anything that can write to your user's Run key (any program running as
  you) could already start programs at login; this adds no new power.
- Failures are logged by exception class only and reported to the window.
- Not checked on the real machine yet: a login with the switch on.
"""

import os
import sys
from pathlib import Path
from typing import Protocol

from smartclip import logsafe

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_VALUE_NAME = "SmartClipboard"
SHORTCUT_NAME = "Smart Clipboard.lnk"
BACKGROUND_FLAG = "--background"

_log = logsafe.get_logger("autostart")


def windowless_python() -> Path:
    """This install's pythonw.exe, or the current Python if there is none."""
    candidate = Path(sys.executable).with_name("pythonw.exe")
    return candidate if candidate.exists() else Path(sys.executable)


def launch_command(background: bool) -> str:
    """The command line that starts the app, quoted for Windows."""
    command = f'"{windowless_python()}" -m smartclip'
    return f"{command} {BACKGROUND_FLAG}" if background else command


class RunKey(Protocol):
    """The current user's Run key. Tests supply a fake."""

    def get(self, name: str) -> str | None: ...
    def set(self, name: str, value: str) -> None: ...
    def delete(self, name: str) -> None: ...


class WinRunKey:
    """The real Run key, through the standard library's winreg."""

    def __init__(self) -> None:
        import winreg

        self._winreg = winreg

    def get(self, name: str) -> str | None:
        reg = self._winreg
        try:
            with reg.OpenKey(reg.HKEY_CURRENT_USER, RUN_KEY) as key:
                value, _kind = reg.QueryValueEx(key, name)
        except FileNotFoundError:
            return None
        return value if isinstance(value, str) else None

    def set(self, name: str, value: str) -> None:
        reg = self._winreg
        with reg.CreateKey(reg.HKEY_CURRENT_USER, RUN_KEY) as key:
            reg.SetValueEx(key, name, 0, reg.REG_SZ, value)

    def delete(self, name: str) -> None:
        reg = self._winreg
        try:
            with reg.OpenKey(reg.HKEY_CURRENT_USER, RUN_KEY, 0, reg.KEY_SET_VALUE) as key:
                reg.DeleteValue(key, name)
        except FileNotFoundError:
            pass


class ShortcutMaker(Protocol):
    """Creates .lnk files. Tests supply a fake."""

    def create(self, path: Path, target: Path, arguments: str, description: str) -> None: ...


class WinShortcutMaker:
    """Creates shortcuts through the Windows Script Host COM object (pywin32)."""

    def create(self, path: Path, target: Path, arguments: str, description: str) -> None:
        import win32com.client

        shortcut = win32com.client.Dispatch("WScript.Shell").CreateShortCut(str(path))
        shortcut.TargetPath = str(target)
        shortcut.Arguments = arguments
        shortcut.WorkingDirectory = str(target.parent)
        shortcut.Description = description
        shortcut.Save()


def start_menu_folder() -> Path:
    """The current user's Start menu Programs folder."""
    appdata = os.environ.get("APPDATA")
    if not appdata:
        raise RuntimeError("APPDATA is not set; this app runs on Windows only")
    return Path(appdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs"


class AutostartSwitch:
    """The "Start with Windows" switch."""

    label = "Start with Windows"

    def __init__(self, run_key: RunKey | None = None) -> None:
        self._run_key = run_key

    def _key(self) -> RunKey:
        if self._run_key is None:
            self._run_key = WinRunKey()
        return self._run_key

    def is_on(self) -> bool:
        try:
            return self._key().get(RUN_VALUE_NAME) is not None
        except OSError as error:
            logsafe.log_error(_log, "autostart_read_failed", error)
            return False

    def set_on(self, on: bool) -> bool:
        """Add or remove the Run value. Returns False if Windows refused."""
        try:
            if on:
                self._key().set(RUN_VALUE_NAME, launch_command(background=True))
            else:
                self._key().delete(RUN_VALUE_NAME)
        except OSError as error:
            logsafe.log_error(_log, "autostart_change_failed", error, on=on)
            return False
        logsafe.log_event(_log, "autostart_changed", on=on)
        return True


class ShortcutSwitch:
    """The "Start menu shortcut" switch."""

    label = "Start menu shortcut"

    def __init__(self, maker: ShortcutMaker | None = None, folder: Path | None = None) -> None:
        self._maker = maker
        self._folder = folder

    def _path(self) -> Path:
        return (self._folder or start_menu_folder()) / SHORTCUT_NAME

    def is_on(self) -> bool:
        try:
            return self._path().exists()
        except (OSError, RuntimeError) as error:
            logsafe.log_error(_log, "shortcut_read_failed", error)
            return False

    def set_on(self, on: bool) -> bool:
        """Create or delete the shortcut. Returns False if it failed."""
        try:
            path = self._path()
            if on:
                maker = self._maker or WinShortcutMaker()
                path.parent.mkdir(parents=True, exist_ok=True)
                maker.create(path, windowless_python(), "-m smartclip", "Smart Clipboard")
            else:
                path.unlink(missing_ok=True)
        except Exception as error:  # noqa: BLE001 - COM errors have their own types
            logsafe.log_error(_log, "shortcut_change_failed", error, on=on)
            return False
        logsafe.log_event(_log, "shortcut_changed", on=on)
        return True
