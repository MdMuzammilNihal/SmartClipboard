"""Tests for the "Start with Windows" and Start menu switches, with fakes.

The real Run key and Start menu are never touched here.
"""

import io
import logging
from collections.abc import Iterator
from pathlib import Path

import pytest

from smartclip import autostart, logsafe
from smartclip.autostart import (
    BACKGROUND_FLAG,
    RUN_VALUE_NAME,
    SHORTCUT_NAME,
    AutostartSwitch,
    ShortcutSwitch,
    launch_command,
)


class FakeRunKey:
    def __init__(self, fail: bool = False) -> None:
        self.values: dict[str, str] = {"OtherApp": '"C:\\fake\\other.exe"'}
        self.fail = fail

    def get(self, name: str) -> str | None:
        if self.fail:
            raise PermissionError("FAKE access denied")
        return self.values.get(name)

    def set(self, name: str, value: str) -> None:
        if self.fail:
            raise PermissionError("FAKE access denied")
        self.values[name] = value

    def delete(self, name: str) -> None:
        if self.fail:
            raise PermissionError("FAKE access denied")
        self.values.pop(name, None)


class FakeShortcutMaker:
    def __init__(self) -> None:
        self.made: list[tuple[Path, Path, str]] = []

    def create(self, path: Path, target: Path, arguments: str, description: str) -> None:
        path.write_text("fake shortcut")
        self.made.append((path, target, arguments))


@pytest.fixture
def log_output() -> Iterator[io.StringIO]:
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    logsafe.attach_handler(handler)
    yield stream
    logging.getLogger(logsafe.ROOT_LOGGER_NAME).removeHandler(handler)


def test_command_uses_windowless_python_and_background_flag() -> None:
    command = launch_command(background=True)
    assert command.startswith('"') and command.endswith(f" -m smartclip {BACKGROUND_FLAG}")
    assert "pythonw.exe" in command.lower() or "python" in command.lower()
    assert BACKGROUND_FLAG not in launch_command(background=False)


def test_windowless_python_is_next_to_this_python() -> None:
    path = autostart.windowless_python()
    assert path.exists()


def test_autostart_switch_adds_and_removes_only_its_value() -> None:
    key = FakeRunKey()
    switch = AutostartSwitch(key)
    assert switch.is_on() is False
    assert switch.set_on(True) is True
    assert switch.is_on() is True
    assert key.values[RUN_VALUE_NAME] == launch_command(background=True)
    assert switch.set_on(False) is True
    assert switch.is_on() is False
    assert key.values == {"OtherApp": '"C:\\fake\\other.exe"'}  # never touched


def test_turning_off_twice_is_harmless() -> None:
    switch = AutostartSwitch(FakeRunKey())
    assert switch.set_on(False) is True
    assert switch.set_on(False) is True


def test_registry_failure_is_reported_and_logged_by_type(log_output: io.StringIO) -> None:
    switch = AutostartSwitch(FakeRunKey(fail=True))
    assert switch.set_on(True) is False
    assert switch.is_on() is False
    output = log_output.getvalue()
    assert "autostart_change_failed" in output and "PermissionError" in output
    assert "access denied" not in output  # the message is never logged


def test_shortcut_switch_creates_and_deletes_the_shortcut(tmp_path: Path) -> None:
    maker = FakeShortcutMaker()
    switch = ShortcutSwitch(maker, folder=tmp_path / "Programs")
    assert switch.is_on() is False
    assert switch.set_on(True) is True
    shortcut = tmp_path / "Programs" / SHORTCUT_NAME
    assert shortcut.exists() and switch.is_on() is True
    assert maker.made == [(shortcut, autostart.windowless_python(), "-m smartclip")]
    assert switch.set_on(False) is True
    assert not shortcut.exists() and switch.is_on() is False


def test_shortcut_failure_is_reported(tmp_path: Path, log_output: io.StringIO) -> None:
    class BrokenMaker:
        def create(self, *_args: object) -> None:
            raise OSError("FAKE COM failure")

    switch = ShortcutSwitch(BrokenMaker(), folder=tmp_path)  # type: ignore[arg-type]
    assert switch.set_on(True) is False
    assert "shortcut_change_failed" in log_output.getvalue()
