"""The "Copied" notice and the Paused banner with Resume (UI plan item 5). Fake data only."""

import os
from collections.abc import Iterator
from datetime import timedelta

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPoint, Qt  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from smartclip.ui.notice import Notice  # noqa: E402
from smartclip.ui.paste import PASTE_TIMEOUT, Paster  # noqa: E402
from smartclip.ui.window import COPIED_TEXT, PAUSED_TEXT, SearchWindow  # noqa: E402
from test_paste import (  # noqa: E402
    Clock as PasteClock,
    FakeApi,
    open_window_over_editor,
    writing_copy,
)
from test_window import Clock, FakeBackend, FakePaster  # noqa: E402


@pytest.fixture
def qapp() -> QApplication:
    return QApplication.instance() or QApplication([])


@pytest.fixture
def backend() -> FakeBackend:
    return FakeBackend()


@pytest.fixture
def window(qapp: QApplication, backend: FakeBackend) -> Iterator[SearchWindow]:
    win = SearchWindow(backend, now=Clock())
    win.show_and_focus()
    yield win
    win.hide()
    if win._notice is not None:
        win._notice.hide()
    win.deleteLater()


# --- Paused banner ---


def test_banner_appears_with_pause_and_goes_with_resume(window: SearchWindow) -> None:
    assert not window.pause_banner.isVisible()
    window.toggle_pause()
    assert window.pause_banner.isVisible()
    assert window.pause_label.text() == PAUSED_TEXT
    assert PAUSED_TEXT == "Paused: nothing is being saved"
    window.toggle_pause()
    assert not window.pause_banner.isVisible()


def test_resume_button_resumes_capture(window: SearchWindow, backend: FakeBackend) -> None:
    window.toggle_pause()
    assert backend.paused is True
    window.resume_button.click()
    assert backend.paused is False
    assert not window.pause_banner.isVisible()


def test_pause_button_looks_active_while_paused(window: SearchWindow) -> None:
    assert window.pause_button.property("active") is False
    window.toggle_pause()
    assert window.pause_button.property("active") is True
    window.toggle_pause()
    assert window.pause_button.property("active") is False


def test_banner_is_shown_when_the_window_opens_while_paused(
    qapp: QApplication, backend: FakeBackend
) -> None:
    backend.paused = True
    win = SearchWindow(backend, now=Clock())
    win.show_and_focus()
    assert win.pause_banner.isVisible() and win.resume_button.isVisible()
    win.hide()


def test_resume_button_is_styled_in_both_themes() -> None:
    from smartclip.ui import theme

    for t in (theme.LIGHT, theme.DARK):
        sheet = theme.stylesheet(t)
        assert "#banner QPushButton" in sheet and 'QToolButton[active="true"]' in sheet


# --- The Copied notice ---


def test_notice_is_a_focusless_click_through_window(qapp: QApplication) -> None:
    notice = Notice()
    assert notice.windowFlags() & Qt.WindowType.WindowDoesNotAcceptFocus
    assert notice.windowFlags() & Qt.WindowType.WindowStaysOnTopHint
    assert notice.testAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
    assert notice.testAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)


def test_notice_shows_text_near_the_point_then_goes(qapp: QApplication) -> None:
    notice = Notice(duration_ms=20)
    notice.show_text(COPIED_TEXT, "", QPoint(200, 200))
    assert notice.isVisible() and notice.label.text() == COPIED_TEXT
    assert not notice.isActiveWindow()  # never takes the focus
    QTest.qWait(120)
    assert not notice.isVisible()


def test_notice_stays_on_screen_at_an_edge(qapp: QApplication) -> None:
    notice = Notice()
    notice.show_text(COPIED_TEXT, "", QPoint(-5000, -5000))
    screen = notice.screen().availableGeometry()
    assert screen.contains(notice.geometry().topLeft())
    notice.hide()


def test_copy_only_menu_action_shows_the_notice(window: SearchWindow, backend: FakeBackend) -> None:
    row = window.model.rows[0]
    window.choose(row, paste=False)
    assert backend.calls[-1] == ("copy_back", row.item_id)
    assert window._notice is not None and window._notice.isVisible()
    assert window._notice.label.text() == COPIED_TEXT
    assert not window.isVisible()


def test_copy_only_mode_without_a_paster_shows_the_notice(window: SearchWindow) -> None:
    window.choose(window.model.rows[0], paste=True)  # no paster: copy only
    assert window._notice is not None and window._notice.isVisible()


def test_a_failed_copy_shows_no_notice(window: SearchWindow, backend: FakeBackend) -> None:
    backend.copy_ok = False
    window.choose(window.model.rows[0], paste=True)
    assert window._notice is None
    assert window.status_label.text() == "Could not copy"


def test_a_pasted_item_shows_no_notice(qapp: QApplication, backend: FakeBackend) -> None:
    paster = FakePaster()
    win = SearchWindow(backend, now=Clock(), paster=paster)
    win.show_and_focus()
    win.choose(win.model.rows[0], paste=True)
    assert win._notice is None  # the real Paster calls on_copy_only only if it could not paste
    win.hide()


def test_the_window_gives_the_paster_its_copy_only_callback(
    qapp: QApplication, backend: FakeBackend
) -> None:
    paster = FakePaster()
    win = SearchWindow(backend, now=Clock(), paster=paster)
    assert paster.on_copy_only == win.show_copy_notice  # type: ignore[attr-defined]


# --- The real Paster tells the window when it could not paste ---


@pytest.fixture
def paste_api() -> FakeApi:
    return FakeApi()


@pytest.fixture
def paste_clock() -> PasteClock:
    return PasteClock()


@pytest.fixture
def real_paster(qapp: QApplication, paste_api: FakeApi, paste_clock: PasteClock) -> Paster:
    return Paster(paste_api, now=paste_clock)


@pytest.fixture
def copy_only_calls(real_paster: Paster) -> list[int]:
    calls: list[int] = []
    real_paster.on_copy_only = lambda: calls.append(1)
    return calls


def test_paster_reports_copy_only_when_there_is_no_target(
    real_paster: Paster, paste_api: FakeApi, copy_only_calls: list[int]
) -> None:
    assert real_paster.paste(writing_copy(paste_api)) is True  # remember() never ran
    assert copy_only_calls == [1] and paste_api.sent == 0


def test_paster_reports_copy_only_when_the_target_never_comes_to_the_front(
    real_paster: Paster, paste_api: FakeApi, paste_clock: PasteClock, copy_only_calls: list[int]
) -> None:
    open_window_over_editor(real_paster, paste_api)
    paste_api.honour_focus = False
    real_paster.paste(writing_copy(paste_api))
    paste_clock.now += PASTE_TIMEOUT
    real_paster.step()
    assert copy_only_calls == [1] and paste_api.sent == 0


def test_paster_reports_copy_only_when_keys_stay_held(
    real_paster: Paster, paste_api: FakeApi, paste_clock: PasteClock, copy_only_calls: list[int]
) -> None:
    open_window_over_editor(real_paster, paste_api)
    paste_api.held = True
    real_paster.paste(writing_copy(paste_api))
    paste_clock.now += PASTE_TIMEOUT + timedelta(milliseconds=1)
    real_paster.step()
    assert copy_only_calls == [1]


def test_paster_stays_quiet_when_it_pastes(
    real_paster: Paster, paste_api: FakeApi, copy_only_calls: list[int]
) -> None:
    open_window_over_editor(real_paster, paste_api)
    real_paster.paste(writing_copy(paste_api))
    real_paster.step()
    assert paste_api.sent == 1 and copy_only_calls == []


def test_paster_stays_quiet_when_the_copy_failed_or_was_never_written(
    real_paster: Paster, paste_api: FakeApi, paste_clock: PasteClock, copy_only_calls: list[int]
) -> None:
    open_window_over_editor(real_paster, paste_api)
    assert real_paster.paste(writing_copy(paste_api, ok=False)) is False
    assert copy_only_calls == []
    real_paster.paste(lambda: True)  # accepted, but the clipboard never changes
    paste_clock.now += PASTE_TIMEOUT
    real_paster.step()
    assert copy_only_calls == []  # not on the clipboard, so "Copied" would be untrue
