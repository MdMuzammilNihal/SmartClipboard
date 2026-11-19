"""Wires the pieces together: listener, labeling, storage, hotkeys, window.

This is the smallest wiring that lets the window run on real copies (owner's
decision, 2026-11-18). There is no tray icon and no settings screen yet.

Threads:
- The listener thread watches the clipboard and puts ClipboardEvents on a
  queue (listener.py).
- The hotkey thread hears the global hotkeys (hotkeys.py). Its callbacks only
  emit Qt signals or post to the listener; they touch nothing else.
- The Qt (main) thread owns the Store and History, drains the event queue
  every EVENT_POLL_MS, runs retention cleanup every `CLEANUP_INTERVAL`, and
  draws the window. So the database is only ever used on one thread.

Capture path: event -> classify() -> secret_rules.assess() -> ItemPayload ->
History.capture() (merges duplicates) -> LastCapture.record() (for the
mark-as-secret hotkey).

SECURITY REVIEW (owner):

- Label for secrets: when secret detection says secret or unsure and the
  content rules said Note (a lone token, for example), the label becomes
  "possible secret". Any other label is kept (a JSON blob holding a token
  stays JSON, but is still masked and kept 2 hours because of its state).
- Copy-back from the window passes `sensitive=retention.is_sensitive(...)`,
  so secret and unsure items always get the safe copy-back. The window does
  not wait for the write; a failed write is logged by the listener.
- If the dedupe key file cannot be read, the app does not start and does not
  replace the key (owner's decision: an unreadable key raises and is never
  replaced). It shows key_error_message(): which file, the likely cause, and
  that renaming the file by hand lets the app start with a new key. The
  app never renames or deletes it itself. The path is shown on screen only,
  never logged (it holds the Windows user name).
- Only one copy runs at a time (a named mutex, config.SINGLE_INSTANCE_NAME),
  so two copies never capture into the same database. A second copy shows
  a message and exits before it opens anything.
- Choosing an item in the window pastes it into the app that was in front
  (owner's decision 2026-11-19, config.PASTE_ON_CHOOSE; checks in
  ui/paste.py): only if that same window is back in front, the copy-back
  was written, and no modifier key is held.
- Every window the app shows (the search window and its tooltips, menus,
  and dialogs) is kept out of screen capture (ui/capture_guard.py) unless
  config.EXCLUDE_FROM_SCREEN_CAPTURE is False.
- Captured secrets are cleared from the clipboard (owner's decision
  2026-11-19, option B): when a capture ends up clearly secret, or the item
  on the clipboard right now is marked secret (button or Ctrl+Alt+S), the
  listener is asked to empty the clipboard 30 seconds later if nothing was
  copied since. Unsure items are not cleared. Marking the item on the
  clipboard normal cancels the clear. Deleting the item does not: the secret
  is still on the clipboard.
- Confirmed secrets are also removed from Win+V history (owner's decision
  2026-11-19, option C; winhistory.py). Same triggers as above, plus
  marking an older item secret. The item on the clipboard now waits
  `WIN_HISTORY_REMOVE_AFTER` (32 seconds), because deleting its history
  entry also empties the clipboard; an older item is removed at once. It
  still runs if something newer was copied meanwhile, and if the item was
  deleted from this app. Marking the item normal cancels it; unsure items
  are never removed. Pending removals run at once when the app quits.
- The opt-in labeled-data log (task 20, datalog.py) is written only when
  config.LABELED_LOG_ENABLED is True: one record per new capture (not for a
  merged re-copy) and one per mark secret or mark normal that changed the
  item. Secret and unsure items are written redacted; a Mark normal record
  holds the real text (owner's decision 2026-11-19).
- Logs record item ids, content types, lengths, and counts only.
- Known limit: the pause state is only visible in the window, since there
  is no tray icon yet.
"""

import queue
import sys
from collections.abc import Callable
from concurrent.futures import Future
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Protocol

from smartclip import classify, config, logsafe, overrides, retention, secret_rules
from smartclip.datalog import LabeledLog, RecordEvent
from smartclip.dedupe import DedupeKey, DedupeKeyError
from smartclip.gate import Gate, PauseSwitch
from smartclip.history import History
from smartclip.labels import Label, SecretState
from smartclip.listener import ClipboardEvent
from smartclip.storage import ItemPayload, StoredItem

EVENT_POLL_MS = 100

_log = logsafe.get_logger("app")


class CopyBackTarget(Protocol):
    """The part of ClipboardListener the backend uses."""

    def request_copy_back(self, text: str, sensitive: bool) -> "Future[bool] | None": ...

    def request_capture_clear(self, sequence: int | None) -> bool: ...


class HistoryRemover(Protocol):
    """The part of winhistory.HistoryCleaner the backend uses."""

    def submit(self, digest: str) -> object: ...


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


ALREADY_RUNNING_MESSAGE = (
    "Smart Clipboard is already running. Press Ctrl+Alt+V to open its window."
)


def key_error_message(key_path: Path) -> str:
    """What the owner sees when the dedupe key file cannot be read."""
    return "\n\n".join(
        [
            "The duplicate-detection key file cannot be read, so Smart Clipboard "
            "will not start. The file was left as it is.",
            f"File: {key_path}",
            "This usually means the Windows account changed (for example after a "
            "profile reset). Saved items are then most likely unreadable too.",
            "To start with a new key, rename or move that file yourself and start "
            "Smart Clipboard again. Items saved before will then not be merged "
            "with new copies of the same text.",
        ]
    )


def build_payload(event: ClipboardEvent) -> ItemPayload:
    """Label one captured copy and decide its secret state."""
    label = classify.classify(event.text).label
    assessment = secret_rules.assess(event.text, source_app=event.source_app, label=label)
    if assessment.state is not SecretState.NORMAL and label is Label.NOTE:
        label = Label.POSSIBLE_SECRET
    return ItemPayload(
        content=event.text,
        label=label,
        source_app=event.source_app,
        secret_state=assessment.state,
    )


class AppBackend:
    """Everything the window and the hotkeys do to the history. UI thread only."""

    def __init__(
        self,
        history: History,
        copier: CopyBackTarget,
        pause: PauseSwitch,
        sequence_number: Callable[[], int],
        now: Callable[[], datetime] = _utc_now,
        on_quit: Callable[[], None] = lambda: None,
        win_history: HistoryRemover | None = None,
        labeled_log: LabeledLog | None = None,
    ) -> None:
        self._history = history
        self._copier = copier
        self._pause = pause
        self._sequence_number = sequence_number
        self._now = now
        self._on_quit = on_quit
        self.last_capture = overrides.LastCapture()
        self._win_history = win_history
        self._labeled_log = labeled_log
        # Item id -> (keyed hash, when to remove it from Win+V history).
        self._history_due: dict[int, tuple[str, datetime]] = {}

    # --- Capture and retention ---

    def handle_event(self, event: ClipboardEvent) -> int:
        """Store one captured copy. Returns the id of the item that holds it."""
        payload = build_payload(event)
        result = self._history.capture(payload, event.captured_at)
        self.last_capture.record(result.item_id, event.sequence_number)
        stored = self._history.get(result.item_id)
        if stored is not None and not result.merged:
            self._write_labeled(RecordEvent.CAPTURED, stored, event.captured_at)
        # The stored state counts: a re-copy merged into a secret stays secret.
        if stored is not None and stored.payload.secret_state is SecretState.SECRET:
            self._copier.request_capture_clear(event.sequence_number)
            self._schedule_history_removal(stored, config.WIN_HISTORY_REMOVE_AFTER)
        logsafe.log_event(
            _log,
            "item_captured",
            item_id=result.item_id,
            merged=result.merged,
            content_type=payload.label,
            length=len(event.text),
            forced=event.forced,
        )
        return result.item_id

    def drain(self, events: "queue.Queue[ClipboardEvent]") -> int:
        """Store every event waiting on the queue. Returns how many were stored."""
        count = 0
        while True:
            try:
                event = events.get_nowait()
            except queue.Empty:
                return count
            self.handle_event(event)
            count += 1

    def cleanup(self) -> list[int]:
        """Delete expired items, then VACUUM if due."""
        now = self._now()
        deleted = self._history.cleanup(now)
        for item_id in deleted:
            self.last_capture.forget(item_id)
        self._history.vacuum_if_due(now)
        return deleted

    def run_due_history_removals(self, everything: bool = False) -> int:
        """Hand due Win+V removals to the worker. everything=True runs all (at exit)."""
        if self._win_history is None:
            return 0
        now = self._now()
        due = [
            item_id
            for item_id, (_digest, when) in self._history_due.items()
            if everything or now >= when
        ]
        for item_id in due:
            digest, _when = self._history_due.pop(item_id)
            self._win_history.submit(digest)
            logsafe.log_event(_log, "win_history_removal_started", item_id=item_id)
        return len(due)

    def mark_last_secret(self) -> bool:
        """The Ctrl+Alt+S action: mark the item on the clipboard now as secret."""
        item_id = self.last_capture.item_for(self._sequence_number())
        if item_id is None:
            logsafe.log_event(_log, "mark_secret_refused_no_current_item")
            return False
        return self.mark_secret(item_id)

    # --- WindowBackend ---

    def items(self) -> list[StoredItem]:
        return self._history.items()

    def copy_back(self, item_id: int) -> bool:
        item = self._history.get(item_id)
        if item is None:
            return False
        sensitive = retention.is_sensitive(item.payload)
        future = self._copier.request_copy_back(item.payload.content, sensitive=sensitive)
        logsafe.log_event(
            _log, "copy_back_requested", item_id=item_id, sensitive=sensitive,
            accepted=future is not None,
        )
        return future is not None

    def pin(self, item_id: int) -> bool:
        return self._history.pin(item_id)

    def unpin(self, item_id: int) -> bool:
        return self._history.unpin(item_id)

    def delete(self, item_id: int) -> bool:
        self.last_capture.forget(item_id)
        deleted = self._history.delete(item_id)
        logsafe.log_event(_log, "item_deleted", item_id=item_id, deleted=deleted)
        return deleted

    def mark_normal(self, item_id: int) -> bool:
        item = self._history.get(item_id)
        if item is None:
            return False
        updated = overrides.mark_normal(item.payload)
        changed = self._history.update(item_id, updated)
        marked = self._history.get(item_id)
        if changed and marked is not None and updated != item.payload:
            self._write_labeled(RecordEvent.MARKED_NORMAL, marked, self._now())
        if changed and self._on_clipboard_now(item_id) is not None:
            self._copier.request_capture_clear(None)
        if changed and self._history_due.pop(item_id, None) is not None:
            logsafe.log_event(_log, "win_history_removal_cancelled", item_id=item_id)
        logsafe.log_event(_log, "item_marked_normal", item_id=item_id, changed=changed)
        return changed

    def mark_secret(self, item_id: int) -> bool:
        item = self._history.get(item_id)
        if item is None:
            return False
        updated = overrides.mark_secret(item.payload, self._now())
        changed = self._history.update(item_id, updated)
        sequence = self._on_clipboard_now(item_id)
        if changed and sequence is not None:
            self._copier.request_capture_clear(sequence)
        marked = self._history.get(item_id)
        if changed and marked is not None:
            if updated != item.payload:  # only a real change is a new label
                self._write_labeled(RecordEvent.MARKED_SECRET, marked, self._now())
            # Still on the clipboard: wait for the paste window. Older: now.
            delay = config.WIN_HISTORY_REMOVE_AFTER if sequence is not None else timedelta(0)
            self._schedule_history_removal(marked, delay)
        logsafe.log_event(_log, "item_marked_secret", item_id=item_id, changed=changed)
        return changed

    def _write_labeled(self, event: RecordEvent, item: StoredItem, at: datetime) -> None:
        """Add a record to the opt-in labeled log, if it is turned on."""
        if self._labeled_log is not None:
            self._labeled_log.record(event, item.id, item.payload, at)

    def _schedule_history_removal(self, item: StoredItem, delay: timedelta) -> None:
        """Plan the removal of a secret's Win+V entries after `delay`."""
        if self._win_history is None:
            return
        digest = item.payload.dedupe_key
        if digest is None:
            # Only rows saved before payload version 2 lack it.
            logsafe.log_event(_log, "win_history_no_key", item_id=item.id)
            return
        self._history_due[item.id] = (digest, self._now() + delay)
        if delay <= timedelta(0):
            self.run_due_history_removals()

    def _on_clipboard_now(self, item_id: int) -> int | None:
        """The clipboard sequence number if this item is what the clipboard holds now."""
        sequence = self._sequence_number()
        return sequence if self.last_capture.item_for(sequence) == item_id else None

    def is_paused(self) -> bool:
        return self._pause.is_paused

    def toggle_pause(self) -> bool:
        paused = self._pause.toggle()
        logsafe.log_event(_log, "capture_paused" if paused else "capture_resumed")
        return paused

    def quit(self) -> None:
        self._on_quit()


def main() -> int:
    """Run the app until Quit is pressed. Returns the process exit code."""
    # Qt and the Windows-only modules are imported here so the rest of this
    # file (build_payload, AppBackend) can be tested without them.
    import win32api
    import win32clipboard
    import win32event
    import winerror
    from PySide6.QtCore import QObject, QTimer, Signal
    from PySide6.QtWidgets import QApplication, QMessageBox

    from smartclip.autostart import BACKGROUND_FLAG, AutostartSwitch, ShortcutSwitch
    from smartclip.hotkeys import HotkeyError, HotkeyListener, parse_hotkey
    from smartclip.listener import ClipboardListener, ListenerError
    from smartclip.storage import Store
    from smartclip.ui import capture_guard
    from smartclip.ui.paste import Paster
    from smartclip.ui.prefs import JsonPrefs
    from smartclip.ui.window import SearchWindow
    from smartclip.winhistory import HistoryCleaner

    logsafe.setup_logging(config.log_dir())
    logsafe.install_crash_handlers()
    qt_app = QApplication(sys.argv)
    qt_app.setQuitOnLastWindowClosed(False)
    if config.EXCLUDE_FROM_SCREEN_CAPTURE:
        # Installed first, so every window this app shows is covered: the
        # search window, its tooltips, menus, and dialogs, and the start-up
        # messages below.
        guard = capture_guard.CaptureGuard(qt_app)
        qt_app.installEventFilter(guard)

    # Held until the process ends; Windows releases it if the app crashes.
    instance_lock = win32event.CreateMutex(None, False, config.SINGLE_INSTANCE_NAME)
    if win32api.GetLastError() == winerror.ERROR_ALREADY_EXISTS:
        logsafe.log_event(_log, "already_running")
        QMessageBox.information(None, "Smart Clipboard", ALREADY_RUNNING_MESSAGE)
        return 1

    store = Store(config.database_path())
    try:
        key = DedupeKey.load_or_create(config.dedupe_key_path())
    except DedupeKeyError:
        QMessageBox.critical(None, "Smart Clipboard", key_error_message(config.dedupe_key_path()))
        store.close()
        return 1
    history = History(store, key)
    unreadable = history.load()
    logsafe.log_event(
        _log, "history_loaded", items=len(history.items()), unreadable=len(unreadable)
    )

    events: "queue.Queue[ClipboardEvent]" = queue.Queue()
    pause = PauseSwitch()
    listener = ClipboardListener(events, gate=Gate(pause=pause))
    try:
        listener.start()
    except ListenerError:
        QMessageBox.critical(None, "Smart Clipboard", "The clipboard listener could not start.")
        store.close()
        return 1

    cleaner = HistoryCleaner(key.digest) if config.REMOVE_SECRETS_FROM_WIN_HISTORY else None
    labeled_log = LabeledLog(config.labeled_log_path()) if config.LABELED_LOG_ENABLED else None
    backend = AppBackend(
        history,
        listener,
        pause,
        sequence_number=win32clipboard.GetClipboardSequenceNumber,
        on_quit=qt_app.quit,
        win_history=cleaner,
        labeled_log=labeled_log,
    )
    backend.cleanup()
    window = SearchWindow(
        backend,
        paster=Paster() if config.PASTE_ON_CHOOSE else None,
        switches=[AutostartSwitch(), ShortcutSwitch()],
        prefs=JsonPrefs(config.window_prefs_path()),
    )

    class HotkeyBridge(QObject):
        """Carries hotkey presses from the hotkey thread to the Qt thread."""

        open_window = Signal()
        toggle_pause = Signal()
        mark_secret = Signal()

    bridge = HotkeyBridge()
    bridge.open_window.connect(window.show_and_focus)
    bridge.toggle_pause.connect(window.toggle_pause)

    def on_mark_secret() -> None:
        backend.mark_last_secret()
        window.refresh()

    bridge.mark_secret.connect(on_mark_secret)

    hotkeys = HotkeyListener(
        {
            "open_window": (parse_hotkey(config.HOTKEY_OPEN_WINDOW), bridge.open_window.emit),
            "pause": (parse_hotkey(config.HOTKEY_PAUSE), bridge.toggle_pause.emit),
            "mark_secret": (parse_hotkey(config.HOTKEY_MARK_SECRET), bridge.mark_secret.emit),
            # Safe from the hotkey thread: it only posts to the listener.
            "force_save": (parse_hotkey(config.HOTKEY_FORCE_SAVE), listener.request_force_save),
        }
    )
    try:
        taken = hotkeys.start()
    except HotkeyError:
        taken = ["all hotkeys"]
    if taken:
        window.status_label.setText("Hotkeys in use by another app: " + ", ".join(taken))

    def poll_events() -> None:
        if backend.drain(events) and window.isVisible():
            window.refresh()
        backend.run_due_history_removals()

    def run_cleanup() -> None:
        if backend.cleanup() and window.isVisible():
            window.refresh()

    event_timer = QTimer()
    event_timer.timeout.connect(poll_events)
    event_timer.start(EVENT_POLL_MS)
    cleanup_timer = QTimer()
    cleanup_timer.timeout.connect(run_cleanup)
    cleanup_timer.start(int(config.CLEANUP_INTERVAL.total_seconds() * 1000))

    # Started by hand, the window opens so it is clear the app runs. Started
    # at login (autostart.py passes --background), it stays hidden until
    # Ctrl+Alt+V. With no tray icon yet, a hotkey clash is then only shown
    # when the window is opened.
    background = BACKGROUND_FLAG in sys.argv[1:]
    if not background:
        window.show_and_focus()
    logsafe.log_event(_log, "app_started", hotkeys_taken=len(taken), background=background)
    try:
        return qt_app.exec()
    finally:
        event_timer.stop()
        cleanup_timer.stop()
        hotkeys.stop()
        # Stopping the listener also clears a pending secret copy-back.
        listener.stop()
        backend.drain(events)
        # Pending Win+V removals run now, so a secret does not outlive the app there.
        backend.run_due_history_removals(everything=True)
        if cleaner is not None:
            cleaner.shutdown()
        if labeled_log is not None:
            labeled_log.close()
        store.close()
        logsafe.log_event(_log, "app_stopped")
        del instance_lock
