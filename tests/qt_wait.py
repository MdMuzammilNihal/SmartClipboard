"""A small helper for tests that wait for a Qt timer or animation to do something."""

import time
from collections.abc import Callable

from PySide6.QtTest import QTest


def wait_for(condition: Callable[[], bool], timeout_ms: int = 3000) -> bool:
    """Run the Qt event loop until `condition()` is true; False if the time runs out.

    Waits for the condition, not for a fixed time, so a slow or busy machine
    cannot make a timing test fail.
    """
    deadline = time.monotonic() + timeout_ms / 1000
    while time.monotonic() < deadline:
        if condition():
            return True
        QTest.qWait(10)
    return condition()
