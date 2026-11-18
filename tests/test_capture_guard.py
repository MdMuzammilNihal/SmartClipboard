"""Tests for keeping the window out of screen capture.

These create a real (hidden) Win32 window of this process with pywin32 and
read its display affinity back. Nothing is shown and no screen is captured.
Whether capture tools really leave the window out is a manual check.
"""

import pytest
import win32api
import win32con
import win32gui

from smartclip.ui import capture_guard


@pytest.fixture
def hwnd():
    window_class = win32gui.WNDCLASS()
    window_class.lpfnWndProc = {}
    window_class.lpszClassName = "SmartClipCaptureGuardTest"
    window_class.hInstance = win32api.GetModuleHandle(None)
    atom = win32gui.RegisterClass(window_class)
    handle = win32gui.CreateWindow(
        atom, "FAKE capture test", win32con.WS_OVERLAPPEDWINDOW,
        0, 0, 100, 100, 0, 0, window_class.hInstance, None,
    )
    yield handle
    win32gui.DestroyWindow(handle)
    win32gui.UnregisterClass(atom, window_class.hInstance)


def test_window_is_excluded_from_capture(hwnd: int) -> None:
    assert capture_guard.current_affinity(hwnd) == capture_guard.WDA_NONE
    applied = capture_guard.exclude_from_capture(hwnd)
    assert applied in (capture_guard.WDA_EXCLUDEFROMCAPTURE, capture_guard.WDA_MONITOR)
    assert capture_guard.current_affinity(hwnd) == applied


def test_invalid_window_fails_cleanly() -> None:
    assert capture_guard.exclude_from_capture(0) == capture_guard.WDA_NONE


# --- CaptureGuard: every top-level Qt window is marked as it is shown ---
# The off-screen Qt platform used in tests has no real window handles, so the
# Windows calls are replaced with a fake that records which handles were
# marked. The real calls are covered by the tests above.


@pytest.fixture
def fake_affinity(monkeypatch: pytest.MonkeyPatch) -> dict[int, int]:
    marks: dict[int, int] = {}

    def fake_exclude(hwnd: int) -> int:
        marks[hwnd] = capture_guard.WDA_EXCLUDEFROMCAPTURE
        return capture_guard.WDA_EXCLUDEFROMCAPTURE

    monkeypatch.setattr(capture_guard, "exclude_from_capture", fake_exclude)
    monkeypatch.setattr(capture_guard, "current_affinity", lambda hwnd: marks.get(hwnd, 0))
    return marks


@pytest.fixture
def guarded_app():
    import os

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    guard = capture_guard.CaptureGuard()
    app.installEventFilter(guard)
    yield guard
    app.removeEventFilter(guard)


def test_main_window_is_marked_when_shown(guarded_app, fake_affinity) -> None:
    from PySide6.QtWidgets import QWidget

    window = QWidget()
    window.show()
    assert int(window.winId()) in fake_affinity
    window.hide()


def test_tooltip_window_is_marked_when_shown(guarded_app, fake_affinity) -> None:
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QLabel

    # A tooltip is its own top-level window (Qt.ToolTip), like the one the
    # owner saw in a screen recording.
    tip = QLabel("FAKE tooltip", None, Qt.WindowType.ToolTip)
    tip.show()
    assert int(tip.winId()) in fake_affinity
    tip.hide()


def test_dialog_is_marked_when_shown(guarded_app, fake_affinity) -> None:
    from PySide6.QtWidgets import QMessageBox

    box = QMessageBox()
    box.setText("FAKE question")
    box.show()
    assert int(box.winId()) in fake_affinity
    box.hide()


def test_child_widgets_are_left_alone(guarded_app, fake_affinity) -> None:
    from PySide6.QtWidgets import QPushButton, QVBoxLayout, QWidget

    window = QWidget()
    button = QPushButton("FAKE", window)
    QVBoxLayout(window).addWidget(button)
    window.show()
    assert list(fake_affinity) == [int(window.winId())]
    window.hide()


def test_window_shown_twice_is_marked_once(guarded_app, fake_affinity) -> None:
    from PySide6.QtWidgets import QWidget

    window = QWidget()
    window.show()
    window.hide()
    window.show()
    assert guarded_app.marked == 1
    window.hide()
