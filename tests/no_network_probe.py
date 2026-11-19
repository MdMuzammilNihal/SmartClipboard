"""Runtime no-network probe, run in a fresh Python process by test_no_network.py.

It installs an audit hook before anything else is imported, then imports
every app module and runs the app's flows with fake data:

- the real clipboard listener started and stopped with capture paused (so
  it never opens the clipboard),
- logging to a file in a temporary folder, as main() does,
- the real DPAPI-protected dedupe key and encrypted SQLite store in that folder,
- AppBackend: capture of fake text (a note, a duplicate, a fake token),
  copy-back requests to a fake target, mark secret, mark normal, pin,
  delete, cleanup,
- the search window in an off-screen Qt app, showing and refreshing rows,
- Win+V removal through the real HistoryCleaner and the real asyncio path
  of WinRtHistoryApi, with the history listing replaced so the owner's
  real Win+V history is never read. is_enabled() is the real call.

It prints one JSON object: every network-related audit event (with its
address, never any text) and the networking modules found in sys.modules.
No clipboard content is printed: the fake text stays inside this process.

Limits: audit hooks only see Python-level calls. Network use from C or C++
code that does not go through Python's socket module (Qt, WinRT, pywin32,
Windows itself) is invisible here. That is why there is a manual check too.
"""

import sys

EVENT_PREFIXES = (
    "socket.",
    "urllib.",
    "http.client.",
    "ftplib.",
    "smtplib.",
    "poplib.",
    "imaplib.",
    "telnetlib.",
    "webbrowser.",
)
_events: list[tuple[str, str]] = []


def _audit(event: str, args: tuple[object, ...]) -> None:
    if not event.startswith(EVENT_PREFIXES):
        return
    if event == "socket.__new__":
        # args: (socket, family, type, proto). The repr of the socket object
        # is not needed; family and type tell what was opened.
        detail = f"family={args[1]} type={args[2]}"
    elif event in {"socket.bind", "socket.connect", "socket.sendto"}:
        detail = repr(args[-1])  # the address
    else:
        detail = repr(args[:2])
    _events.append((event, detail))


sys.addaudithook(_audit)

# Everything below runs with the hook installed.
import importlib  # noqa: E402
import json  # noqa: E402
import os  # noqa: E402
import pkgutil  # noqa: E402
import queue  # noqa: E402
import tempfile  # noqa: E402
from concurrent.futures import Future  # noqa: E402
from datetime import datetime, timezone  # noqa: E402
from pathlib import Path  # noqa: E402

os.environ["QT_QPA_PLATFORM"] = "offscreen"

import smartclip  # noqa: E402

NETWORK_MODULE_ROOTS = frozenset(
    {
        "socket", "_socket", "ssl", "_ssl", "select", "selectors", "asyncio",
        "http", "urllib", "urllib3", "requests", "httpx", "aiohttp", "websockets",
        "ftplib", "smtplib", "poplib", "imaplib", "telnetlib", "xmlrpc",
        "socketserver", "webbrowser",
    }
)
FAKE_NOTE = "FAKE probe note: lunch at 1pm"
FAKE_TOKEN = "ghp_" + "P" * 36  # the known GitHub token shape, obviously fake


class FakeCopier:
    """Stands in for the listener's copy-back, so the clipboard is never written."""

    def request_copy_back(self, text: str, sensitive: bool) -> "Future[bool]":
        future: "Future[bool]" = Future()
        future.set_result(True)
        return future

    def request_capture_clear(self, sequence: int | None) -> bool:
        return True


def import_every_app_module() -> None:
    for module in pkgutil.walk_packages(smartclip.__path__, "smartclip."):
        if not module.name.endswith("__main__"):
            importlib.import_module(module.name)


def run_flows(folder: Path) -> None:
    from PySide6.QtWidgets import QApplication

    from smartclip import logsafe
    from smartclip.app import AppBackend
    from smartclip.dedupe import DedupeKey
    from smartclip.gate import Gate, PauseSwitch
    from smartclip.history import History
    from smartclip.listener import ClipboardEvent, ClipboardListener
    from smartclip.storage import Store
    from smartclip.ui.window import SearchWindow
    from smartclip.winhistory import HistoryCleaner, WinRtHistoryApi

    class NoListingHistoryApi(WinRtHistoryApi):
        """The real WinRT class, except that it never lists the real history."""

        async def _text_entries(self) -> list[tuple[object, str]]:
            return []

    logsafe.setup_logging(folder / "logs")

    # Paused: the listener receives change messages but never opens the clipboard.
    events: "queue.Queue[ClipboardEvent]" = queue.Queue()
    listener = ClipboardListener(events, gate=Gate(pause=PauseSwitch(paused=True)))
    listener.start()
    listener.stop()

    api = NoListingHistoryApi()
    api.is_enabled()
    api.text_entries()  # the real asyncio.run path, with an empty listing

    qt_app = QApplication.instance() or QApplication([])
    with Store(folder / "items.db") as store:
        key = DedupeKey.load_or_create(folder / "dedupe.key")
        history = History(store, key)
        history.load()
        cleaner = HistoryCleaner(key.digest, api=api)
        now = datetime.now(timezone.utc)
        backend = AppBackend(
            history, FakeCopier(), PauseSwitch(), sequence_number=lambda: 1,
            win_history=cleaner,
        )
        note_id = backend.handle_event(ClipboardEvent(FAKE_NOTE, 1, now, "fakeeditor.exe"))
        backend.handle_event(ClipboardEvent(FAKE_NOTE, 2, now, "fakeeditor.exe"))
        token_id = backend.handle_event(ClipboardEvent(FAKE_TOKEN, 3, now, "fakeeditor.exe"))
        window = SearchWindow(backend)
        window.show_and_focus()
        window.refresh()
        qt_app.processEvents()
        backend.copy_back(note_id)
        backend.copy_back(token_id)
        backend.pin(note_id)
        backend.unpin(note_id)
        backend.mark_secret(note_id)
        backend.mark_normal(note_id)
        backend.mark_secret(note_id)  # an older item: removed from Win+V at once
        backend.run_due_history_removals(everything=True)
        cleaner.shutdown()
        backend.delete(token_id)
        backend.cleanup()
        window.refresh()
        qt_app.processEvents()
        window.hide()
        window.deleteLater()
        qt_app.processEvents()


def network_modules_loaded() -> list[str]:
    """Networking modules in sys.modules, as top-level names (Qt ones in full)."""
    found = set()
    for name in sys.modules:
        root = name.split(".")[0]
        if root in NETWORK_MODULE_ROOTS:
            found.add(root)
        elif name.startswith(("PySide6.QtNetwork", "PySide6.QtWebEngine", "PySide6.QtWebSockets")):
            found.add(name)
    return sorted(found)


def main() -> None:
    import_every_app_module()
    with tempfile.TemporaryDirectory(prefix="smartclip-probe-") as folder:
        run_flows(Path(folder))
        import logging

        logging.shutdown()  # releases the log file so the folder can be removed
    print(json.dumps({"events": _events, "modules": network_modules_loaded()}))


if __name__ == "__main__":
    main()
