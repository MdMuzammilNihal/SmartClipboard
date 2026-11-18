"""Tests for source-app lookup. All app names and paths are fake.

Default tests use a fake window API, so no real window or process is read.
Two tests call the real lookups on this test process only (no clipboard
change and no other app involved).
"""

import os
import sys

import pytest
import win32gui

from smartclip.sourceapp import (
    UNKNOWN_SOURCE,
    SourceApp,
    SourceMethod,
    Win32WindowApi,
    exe_name,
    find_source_app,
)

OWNER_WINDOW = 101
FOREGROUND_WINDOW = 202


class FakeWindows:
    """A scripted stand-in for the window and process lookups."""

    def __init__(
        self,
        owner: int | None = OWNER_WINDOW,
        foreground: int | None = FOREGROUND_WINDOW,
        names: dict[int, str | None] | None = None,
    ) -> None:
        self.owner = owner
        self.foreground = foreground
        self.names = (
            names
            if names is not None
            else {OWNER_WINDOW: "fakeeditor.exe", FOREGROUND_WINDOW: "fakebrowser.exe"}
        )
        self.lookups: list[int] = []

    def clipboard_owner(self) -> int | None:
        return self.owner

    def foreground_window(self) -> int | None:
        return self.foreground

    def process_id(self, window: int) -> int | None:
        # In this fake a window's process ID equals the window value.
        return window

    def executable_name(self, process_id: int) -> str | None:
        self.lookups.append(process_id)
        return self.names.get(process_id)


# --- Owner first, foreground as fallback ---


def test_clipboard_owner_is_used_first() -> None:
    api = FakeWindows()
    assert find_source_app(api) == SourceApp(
        "fakeeditor.exe", SourceMethod.CLIPBOARD_OWNER, OWNER_WINDOW
    )
    assert api.lookups == [OWNER_WINDOW]


@pytest.mark.parametrize("no_owner", [None, 0])
def test_foreground_is_used_when_there_is_no_owner(no_owner: int | None) -> None:
    api = FakeWindows(owner=no_owner)
    assert find_source_app(api) == SourceApp("fakebrowser.exe", SourceMethod.FOREGROUND)
    assert find_source_app(api).owner_process_id is None


def test_foreground_is_used_when_owner_process_is_unreadable() -> None:
    api = FakeWindows(names={OWNER_WINDOW: None, FOREGROUND_WINDOW: "fakebrowser.exe"})
    # The owner's process ID is still reported: the gate needs it even when
    # the name came from the foreground window.
    assert find_source_app(api) == SourceApp(
        "fakebrowser.exe", SourceMethod.FOREGROUND, OWNER_WINDOW
    )
    assert api.lookups == [OWNER_WINDOW, FOREGROUND_WINDOW]


def test_unknown_when_nothing_can_be_read() -> None:
    api = FakeWindows(names={})
    assert find_source_app(api) == SourceApp(None, SourceMethod.UNKNOWN, OWNER_WINDOW)


def test_unknown_when_there_are_no_windows() -> None:
    api = FakeWindows(owner=None, foreground=None)
    assert find_source_app(api) == UNKNOWN_SOURCE
    assert api.lookups == []


# --- Names only: lower case, never a path ---


def test_name_is_lower_cased() -> None:
    api = FakeWindows(names={OWNER_WINDOW: "FakeEditor.EXE"})
    assert find_source_app(api).name == "fakeeditor.exe"


@pytest.mark.parametrize(
    "raw",
    [
        r"C:\Users\fakeuser\AppData\Local\FakeApp\FakeApp.exe",
        "C:/Program Files/FakeApp/fakeapp.exe",
        r"\\?\C:\Fake\fakeapp.exe",
        "  fakeapp.exe  ",
    ],
)
def test_paths_are_cut_to_the_file_name(raw: str) -> None:
    assert exe_name(raw) == "fakeapp.exe"


@pytest.mark.parametrize("raw", [None, "", "   ", "C:\\Fake\\"])
def test_empty_names_become_none(raw: str | None) -> None:
    assert exe_name(raw) is None


def test_a_path_from_the_lookup_never_reaches_the_result() -> None:
    api = FakeWindows(names={OWNER_WINDOW: r"C:\Users\fakeuser\FakeApp\fakeapp.exe"})
    result = find_source_app(api)
    assert result.name == "fakeapp.exe"
    assert "fakeuser" not in repr(result)


# --- Real lookups, on this test process only ---


def test_real_lookup_handles_a_window_that_does_not_exist() -> None:
    # A handle value no real window uses: the lookup must return None, not raise.
    assert Win32WindowApi().process_id(0x7FFFFFF0) is None


def test_real_lookup_handles_a_process_that_does_not_exist() -> None:
    # Odd values are never real process IDs on Windows.
    assert Win32WindowApi().executable_name(1_000_001) is None


def test_real_lookup_names_our_own_process() -> None:
    hwnd = win32gui.CreateWindow("STATIC", "", 0, 0, 0, 0, 0, 0, 0, 0, None)
    try:
        api = Win32WindowApi()
        process_id = api.process_id(hwnd)
        assert process_id == os.getpid()
        name = api.executable_name(process_id)
    finally:
        win32gui.DestroyWindow(hwnd)
    assert name is not None
    assert name.endswith(".exe")
    assert "\\" not in name and "/" not in name
    assert name == os.path.basename(sys.executable).lower()
