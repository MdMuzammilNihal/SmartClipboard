"""Shared test setup."""

import pytest


@pytest.fixture(autouse=True)
def flush_qt_deletions():  # noqa: ANN201
    """Finish every window deletion at the end of the test that asked for it.

    Tests call deleteLater() on their windows. Left alone, those deletions
    happen later, inside whichever test next runs the Qt event loop, where
    half-destroyed windows raise errors and starve that test's timers.
    """
    yield
    try:
        from PySide6.QtCore import QEvent
        from PySide6.QtWidgets import QApplication
    except ImportError:  # Qt is not installed: nothing to flush
        return
    app = QApplication.instance()
    if app is not None:
        app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        app.processEvents()
