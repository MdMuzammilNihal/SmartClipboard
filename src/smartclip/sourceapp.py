"""Which app put the text on the clipboard: a best guess, recorded as a hint.

How it works:

1. Ask Windows for the clipboard owner (GetClipboardOwner): the window that
   last wrote to the clipboard. Map that window to its process and take the
   executable's file name, for example "notepad.exe".
2. If there is no owner (an app may write without one) or its process cannot
   be read, fall back to the foreground window, which is usually the app the
   user pressed Ctrl+C in.
3. Return the lower-case executable name and how it was found, so the owner
   can measure how often each method is right (task 17).
4. Also return the owner window's process ID. The gate compares it with this
   app's own process ID to recognise our own writes (task 15).

SECURITY REVIEW (owner):

- Only the executable's file name leaves this module. The full path is cut
  down to its last part inside Win32WindowApi, because a path such as
  C:\\Users\\<name>\\AppData\\... contains the Windows user name. Window
  titles are never read: they often hold document names, page titles, or
  email subjects.
- The app name is a hint, not a fact. Another app can be the clipboard owner
  (a helper process, a clipboard tool, or an app that writes without a
  window), and the foreground window can change between the copy and our
  read. Do not use it for anything stronger than labeling and the exclusion
  list. The owner process ID is different: when the owner window is one of
  ours, we know we wrote the content, which is what the gate relies on.
- Fallback choice: if the owner window exists but its process cannot be read
  (for example an app running as administrator), this falls back to the
  foreground window. That is usually the same app, but it can be wrong. The
  result records which method was used so this can be measured.
- The name is not logged. Logs record only which method found it.

Not yet verified: the real lookups (Win32WindowApi) have not been checked
against copies from real apps. Default tests use a fake.
"""

import ctypes
import ntpath
from ctypes import wintypes
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

import pywintypes
import win32gui
import win32process

# Lets us read a process's image name without the stronger rights that
# reading its memory would need. Works for most processes, including many
# running as administrator, but not for protected system processes.
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_MAX_PATH_CHARS = 32_768

_user32 = ctypes.WinDLL("user32", use_last_error=True)
_user32.GetClipboardOwner.argtypes = []
_user32.GetClipboardOwner.restype = wintypes.HWND

_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
_kernel32.OpenProcess.restype = wintypes.HANDLE
_kernel32.QueryFullProcessImageNameW.argtypes = [
    wintypes.HANDLE,
    wintypes.DWORD,
    wintypes.LPWSTR,
    ctypes.POINTER(wintypes.DWORD),
]
_kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
_kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
_kernel32.CloseHandle.restype = wintypes.BOOL


class SourceMethod(StrEnum):
    """How the source app was found."""

    CLIPBOARD_OWNER = "clipboard_owner"
    FOREGROUND = "foreground"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class SourceApp:
    """The guessed source app: a lower-case executable name, or None."""

    name: str | None
    method: SourceMethod
    # Process that owns the clipboard owner window, if there is one. Not the
    # same as the process `name` belongs to when the foreground fallback ran.
    owner_process_id: int | None = None


UNKNOWN_SOURCE = SourceApp(name=None, method=SourceMethod.UNKNOWN)


class WindowApi(Protocol):
    """The window and process lookups this module needs. Tests supply a fake."""

    def clipboard_owner(self) -> int | None:
        """Return the clipboard owner window, or None if there is none."""

    def foreground_window(self) -> int | None:
        """Return the foreground window, or None if there is none."""

    def process_id(self, window: int) -> int | None:
        """Return the ID of the process that owns a window, or None."""

    def executable_name(self, process_id: int) -> str | None:
        """Return the file name of a process's executable, or None."""


def exe_name(raw: str | None) -> str | None:
    """Reduce a process name or path to a lower-case file name, or None.

    Used on lookup results and on exclusion-list entries, so both compare the
    same way. Keeps only the part after the last path separator.
    """
    if not raw:
        return None
    name = ntpath.basename(raw.strip()).strip().lower()
    return name or None


def find_source_app(api: WindowApi) -> SourceApp:
    """Return the app that most likely made the current clipboard content.

    Call this while the clipboard is open, right after reading the sequence
    number, so the owner belongs to the same change as the text.
    """
    owner = api.clipboard_owner()
    owner_pid = api.process_id(owner) if owner else None
    if owner_pid:
        name = exe_name(api.executable_name(owner_pid))
        if name is not None:
            return SourceApp(name, SourceMethod.CLIPBOARD_OWNER, owner_pid)
    foreground = api.foreground_window()
    foreground_pid = api.process_id(foreground) if foreground else None
    if foreground_pid:
        name = exe_name(api.executable_name(foreground_pid))
        if name is not None:
            return SourceApp(name, SourceMethod.FOREGROUND, owner_pid)
    return SourceApp(None, SourceMethod.UNKNOWN, owner_pid)


class Win32WindowApi:
    """The real Windows lookups."""

    def clipboard_owner(self) -> int | None:
        return _user32.GetClipboardOwner() or None

    def foreground_window(self) -> int | None:
        return win32gui.GetForegroundWindow() or None

    def process_id(self, window: int) -> int | None:
        try:
            thread_id, process_id = win32process.GetWindowThreadProcessId(window)
        except pywintypes.error:
            return None
        # For a window that does not exist, Windows returns thread ID 0 and
        # pywin32 then hands back an uninitialised process ID (seen as a
        # random negative number). Trust the process ID only with a real
        # thread ID, since the gate compares it with our own.
        if not thread_id or process_id <= 0:
            return None
        return process_id

    def executable_name(self, process_id: int) -> str | None:
        path = _process_image_path(process_id)
        # Cut the path down here, so the full path (which can contain the
        # Windows user name) never leaves this method.
        return exe_name(path)


def _process_image_path(process_id: int) -> str | None:
    """Return the full path of a process's executable, or None if unreadable."""
    handle = _kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, process_id)
    if not handle:
        return None
    try:
        buffer = ctypes.create_unicode_buffer(_MAX_PATH_CHARS)
        size = wintypes.DWORD(_MAX_PATH_CHARS)
        if not _kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
            return None
        return buffer.value or None
    finally:
        _kernel32.CloseHandle(handle)
