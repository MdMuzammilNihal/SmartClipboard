"""Tests for pasting into the previous app (ui/paste.py), with a fake Windows API.

No real keys are sent and no real window is touched.
"""

import io
import logging
import os
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone

import pytest

pytest.importorskip("PySide6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

from smartclip import logsafe  # noqa: E402
from smartclip.ui.paste import PASTE_TIMEOUT, Paster  # noqa: E402

START = datetime(2026, 11, 19, 12, 0, tzinfo=timezone.utc)
EDITOR, OUR_WINDOW, CHAT = 101, 202, 303


class FakeApi:
    def __init__(self) -> None:
        self.front = EDITOR
        self.own = {OUR_WINDOW}
        self.existing = {EDITOR, OUR_WINDOW, CHAT}
        self.sequence = 10
        self.held = False
        self.sent = 0
        self.focus_requests: list[int] = []
        self.honour_focus = True

    def foreground_window(self) -> int:
        return self.front

    def is_own_window(self, window: int) -> bool:
        return window in self.own

    def window_exists(self, window: int) -> bool:
        return window in self.existing

    def set_foreground(self, window: int) -> None:
        self.focus_requests.append(window)
        if self.honour_focus:
            self.front = window

    def sequence_number(self) -> int:
        return self.sequence

    def modifier_held(self) -> bool:
        return self.held

    def send_ctrl_v(self) -> None:
        self.sent += 1


class Clock:
    def __init__(self) -> None:
        self.now = START

    def __call__(self) -> datetime:
        return self.now


@pytest.fixture(scope="module")
def qapp() -> QApplication:
    return QApplication.instance() or QApplication([])


@pytest.fixture
def api() -> FakeApi:
    return FakeApi()


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def paster(qapp: QApplication, api: FakeApi, clock: Clock) -> Paster:
    return Paster(api, now=clock)


@pytest.fixture
def log_output() -> Iterator[io.StringIO]:
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    logsafe.attach_handler(handler)
    yield stream
    logging.getLogger(logsafe.ROOT_LOGGER_NAME).removeHandler(handler)


def open_window_over_editor(paster: Paster, api: FakeApi) -> None:
    api.front = EDITOR
    paster.remember()
    api.front = OUR_WINDOW  # the clipboard window is now in front


def writing_copy(api: FakeApi, ok: bool = True):  # noqa: ANN201
    def copy() -> bool:
        if ok:
            api.sequence += 1  # the copy-back changed the clipboard
        return ok

    return copy


def test_pastes_into_the_remembered_window(paster: Paster, api: FakeApi, log_output) -> None:
    open_window_over_editor(paster, api)
    assert paster.paste(writing_copy(api)) is True
    assert api.focus_requests == [EDITOR]
    paster.step()
    assert api.sent == 1 and not paster.pending
    assert "paste_sent" in log_output.getvalue()


def test_waits_for_the_copy_back_to_be_written(paster: Paster, api: FakeApi) -> None:
    open_window_over_editor(paster, api)
    paster.paste(lambda: True)  # accepted, but the listener has not written yet
    paster.step()
    assert api.sent == 0 and paster.pending
    api.sequence += 1
    paster.step()
    assert api.sent == 1


def test_never_pastes_into_another_window(
    paster: Paster, api: FakeApi, clock: Clock, log_output
) -> None:
    open_window_over_editor(paster, api)
    api.honour_focus = False
    paster.paste(writing_copy(api))
    api.front = CHAT  # something else came to the front
    paster.step()
    assert api.sent == 0
    clock.now += PASTE_TIMEOUT
    paster.step()
    assert api.sent == 0 and not paster.pending
    assert "paste_skipped_target_not_in_front" in log_output.getvalue()


def test_waits_while_a_modifier_key_is_held(paster: Paster, api: FakeApi) -> None:
    open_window_over_editor(paster, api)
    api.held = True
    paster.paste(writing_copy(api))
    paster.step()
    assert api.sent == 0
    api.held = False
    paster.step()
    assert api.sent == 1


def test_gives_up_if_keys_stay_held(paster: Paster, api: FakeApi, clock: Clock, log_output) -> None:
    open_window_over_editor(paster, api)
    api.held = True
    paster.paste(writing_copy(api))
    clock.now += PASTE_TIMEOUT + timedelta(milliseconds=1)
    paster.step()
    assert api.sent == 0 and not paster.pending
    assert "paste_skipped_keys_held" in log_output.getvalue()


def test_gives_up_if_the_clipboard_never_changes(
    paster: Paster, api: FakeApi, clock: Clock, log_output
) -> None:
    open_window_over_editor(paster, api)
    paster.paste(lambda: True)
    clock.now += PASTE_TIMEOUT
    paster.step()
    assert api.sent == 0
    assert "paste_skipped_not_copied" in log_output.getvalue()


def test_failed_copy_does_nothing_else(paster: Paster, api: FakeApi) -> None:
    open_window_over_editor(paster, api)
    assert paster.paste(writing_copy(api, ok=False)) is False
    assert api.focus_requests == [] and not paster.pending


def test_own_window_is_never_the_target(paster: Paster, api: FakeApi, log_output) -> None:
    api.front = OUR_WINDOW
    paster.remember()
    assert paster.paste(writing_copy(api)) is True  # copied only
    assert api.focus_requests == [] and not paster.pending
    assert "paste_skipped_no_target" in log_output.getvalue()


def test_reopening_over_our_own_window_keeps_the_target(paster: Paster, api: FakeApi) -> None:
    open_window_over_editor(paster, api)
    paster.remember()  # our window is in front now: ignored
    paster.paste(writing_copy(api))
    assert api.focus_requests == [EDITOR]


def test_closed_target_is_not_used(paster: Paster, api: FakeApi) -> None:
    open_window_over_editor(paster, api)
    api.existing.discard(EDITOR)
    assert paster.paste(writing_copy(api)) is True
    assert api.focus_requests == [] and api.sent == 0


def test_step_without_a_paste_does_nothing(paster: Paster, api: FakeApi) -> None:
    paster.step()
    assert api.sent == 0
