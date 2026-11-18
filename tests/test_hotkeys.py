"""Tests for global hotkeys.

Parsing tests need no Windows calls. The thread tests register real hotkeys,
but only on unusual combinations (Ctrl+Alt+Shift with F21 to F23) that no
keyboard normally has, and they are unregistered when each test ends. Key
presses are simulated by sending the WM_HOTKEY message to the hidden window.
"""

import io
import logging
import threading
from collections.abc import Iterator

import pytest
import win32con
import win32gui

from smartclip import config, logsafe
from smartclip.hotkeys import (
    WM_HOTKEY,
    Hotkey,
    HotkeyError,
    HotkeyListener,
    parse_hotkey,
)

CTRL_ALT = win32con.MOD_CONTROL | win32con.MOD_ALT
UNUSUAL_A = "Ctrl+Alt+Shift+F23"
UNUSUAL_B = "Ctrl+Alt+Shift+F22"


@pytest.fixture
def log_output() -> Iterator[io.StringIO]:
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    logsafe.attach_handler(handler)
    yield stream
    logging.getLogger(logsafe.ROOT_LOGGER_NAME).removeHandler(handler)


# --- Parsing ---


def test_default_hotkeys_parse() -> None:
    assert parse_hotkey(config.HOTKEY_MARK_SECRET) == Hotkey(CTRL_ALT, ord("S"))
    assert parse_hotkey(config.HOTKEY_FORCE_SAVE) == Hotkey(CTRL_ALT, ord("F"))


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("ctrl+alt+s", Hotkey(CTRL_ALT, ord("S"))),
        (" Ctrl + Alt + 7 ", Hotkey(CTRL_ALT, ord("7"))),
        ("Shift+Win+F12", Hotkey(win32con.MOD_SHIFT | win32con.MOD_WIN, win32con.VK_F12)),
        ("Alt+F1", Hotkey(win32con.MOD_ALT, win32con.VK_F1)),
        ("Ctrl+F24", Hotkey(win32con.MOD_CONTROL, win32con.VK_F1 + 23)),
    ],
)
def test_hotkeys_parse(text: str, expected: Hotkey) -> None:
    assert parse_hotkey(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "S",
        "",
        "Ctrl+",
        "Ctrl+Alt",
        "Ctrl+Ctrl+S",
        "Hyper+S",
        "Ctrl+F25",
        "Ctrl+F0",
        "Ctrl+Space",
        "Ctrl+é",
    ],
)
def test_bad_hotkeys_are_refused(text: str) -> None:
    with pytest.raises(HotkeyError):
        parse_hotkey(text)


# --- The real hotkey thread ---


def press(listener: HotkeyListener, name: str) -> None:
    """Send the message Windows sends when a registered hotkey is pressed."""
    handle = listener.window_handle
    assert handle is not None
    win32gui.PostMessage(handle, WM_HOTKEY, listener.hotkey_id(name), 0)


def test_hotkey_runs_its_callback() -> None:
    pressed_a, pressed_b = threading.Event(), threading.Event()
    listener = HotkeyListener(
        {
            "a": (parse_hotkey(UNUSUAL_A), pressed_a.set),
            "b": (parse_hotkey(UNUSUAL_B), pressed_b.set),
        }
    )
    assert listener.start() == []
    try:
        press(listener, "b")
        assert pressed_b.wait(3)
        assert not pressed_a.is_set()
        press(listener, "a")
        assert pressed_a.wait(3)
    finally:
        listener.stop()
    assert not listener.is_running


def test_combination_already_taken_is_reported() -> None:
    first = HotkeyListener({"a": (parse_hotkey(UNUSUAL_A), lambda: None)})
    second = HotkeyListener({"a": (parse_hotkey(UNUSUAL_A), lambda: None)})
    assert first.start() == []
    try:
        assert second.start() == ["a"]
        second.stop()
    finally:
        first.stop()


def test_hotkey_is_released_on_stop() -> None:
    first = HotkeyListener({"a": (parse_hotkey(UNUSUAL_A), lambda: None)})
    first.start()
    first.stop()
    again = HotkeyListener({"a": (parse_hotkey(UNUSUAL_A), lambda: None)})
    try:
        assert again.start() == []
    finally:
        again.stop()


def test_failing_callback_does_not_stop_the_thread_or_leak(
    log_output: io.StringIO, capfd: pytest.CaptureFixture[str]
) -> None:
    secret_message = "FAKE-hotkey-error-text"
    worked = threading.Event()

    def broken() -> None:
        raise RuntimeError(secret_message)

    listener = HotkeyListener(
        {
            "broken": (parse_hotkey(UNUSUAL_A), broken),
            "fine": (parse_hotkey(UNUSUAL_B), worked.set),
        }
    )
    listener.start()
    try:
        press(listener, "broken")
        press(listener, "fine")
        assert worked.wait(3)
    finally:
        listener.stop()
    captured = capfd.readouterr()
    text = log_output.getvalue()
    assert "hotkey_callback_failed error_type=RuntimeError hotkey_id=1" in text
    assert secret_message not in text + captured.out + captured.err


def test_unknown_hotkey_id_is_ignored() -> None:
    pressed = threading.Event()
    listener = HotkeyListener({"a": (parse_hotkey(UNUSUAL_A), pressed.set)})
    listener.start()
    try:
        handle = listener.window_handle
        assert handle is not None
        win32gui.PostMessage(handle, WM_HOTKEY, 99, 0)
        press(listener, "a")
        assert pressed.wait(3)
        assert listener.is_running
    finally:
        listener.stop()


def test_starting_twice_is_an_error() -> None:
    listener = HotkeyListener({"a": (parse_hotkey(UNUSUAL_A), lambda: None)})
    listener.start()
    try:
        with pytest.raises(RuntimeError):
            listener.start()
    finally:
        listener.stop()
