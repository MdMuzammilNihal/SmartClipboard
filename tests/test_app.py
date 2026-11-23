"""Tests for the app wiring (capture path and window actions). Fake data only.

These use the real Windows DPAPI and a temporary SQLite file, with a fake
copy-back target instead of the clipboard listener. main() itself (Qt,
hotkeys, the real clipboard) is not run here; it is checked by hand.
"""

import io
import logging
from collections.abc import Iterator
from concurrent.futures import Future
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from smartclip import logsafe
from smartclip.app import AppBackend, build_payload, key_error_message
from smartclip.datalog import LabeledLog, RecordEvent
from smartclip.dedupe import KEY_LENGTH, DedupeKey
from smartclip.gate import PauseSwitch
from smartclip.history import History
from smartclip.labels import Label, SecretState
from smartclip.listener import ClipboardEvent
from smartclip.retention import RetentionPolicy
from smartclip.storage import Store

NOW = datetime(2026, 11, 18, 12, 0, tzinfo=timezone.utc)
KEY = DedupeKey(b"a" * KEY_LENGTH)
POLICY = RetentionPolicy(normal=timedelta(days=90), secret=timedelta(hours=2))
FAKE_NOTE = "FAKE app note: lunch at 1pm"
FAKE_UNSURE = "FAKE-app-Zx9!q7Lm"
FAKE_TOKEN = "ghp_" + "F" * 36


def event(text: str, sequence: int = 1, source: str | None = "fakeeditor.exe", **kw) -> ClipboardEvent:
    return ClipboardEvent(
        text=text, sequence_number=sequence, captured_at=kw.pop("at", NOW), source_app=source, **kw
    )


class FakeCopier:
    def __init__(self, running: bool = True) -> None:
        self.running = running
        self.requests: list[tuple[str, bool]] = []
        self.clear_requests: list[int | None] = []

    def request_copy_back(self, text: str, sensitive: bool) -> "Future[bool] | None":
        if not self.running:
            return None
        self.requests.append((text, sensitive))
        return Future()

    def request_capture_clear(self, sequence: int | None) -> bool:
        self.clear_requests.append(sequence)
        return self.running


class Clock:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self) -> datetime:
        return self.now


@pytest.fixture
def store(tmp_path: Path) -> Iterator[Store]:
    with Store(tmp_path / "items.db") as opened:
        yield opened


@pytest.fixture
def history(store: Store) -> History:
    loaded = History(store, KEY, POLICY)
    loaded.load()
    return loaded


@pytest.fixture
def copier() -> FakeCopier:
    return FakeCopier()


@pytest.fixture
def sequence() -> list[int]:
    return [1]


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def backend(history: History, copier: FakeCopier, sequence: list[int], clock: Clock) -> AppBackend:
    return AppBackend(history, copier, PauseSwitch(), lambda: sequence[0], now=clock)


# --- build_payload ---


def test_normal_text_keeps_its_label() -> None:
    payload = build_payload(event("https://example.test/fake"))
    assert (payload.label, payload.secret_state) == (Label.LINK, SecretState.NORMAL)
    assert payload.source_app == "fakeeditor.exe"


def test_lone_unsure_token_becomes_possible_secret() -> None:
    payload = build_payload(event(FAKE_UNSURE))
    assert (payload.label, payload.secret_state) == (Label.POSSIBLE_SECRET, SecretState.UNSURE)


def test_known_token_format_is_secret() -> None:
    payload = build_payload(event(FAKE_TOKEN))
    assert payload.secret_state is SecretState.SECRET
    assert payload.label is Label.POSSIBLE_SECRET


def test_secret_inside_other_label_keeps_the_label() -> None:
    payload = build_payload(event('{"token": "' + FAKE_TOKEN + '"}'))
    assert (payload.label, payload.secret_state) == (Label.JSON, SecretState.SECRET)


# --- Capture ---


def test_event_is_stored_and_duplicates_merge(backend: AppBackend, history: History) -> None:
    first = backend.handle_event(event(FAKE_NOTE, 1))
    second = backend.handle_event(event(FAKE_NOTE, 2, at=NOW + timedelta(minutes=1)))
    assert first == second
    assert len(history.items()) == 1


def test_sensitive_capture_gets_short_expiry(backend: AppBackend, history: History) -> None:
    item_id = backend.handle_event(event(FAKE_TOKEN))
    item = history.get(item_id)
    assert item is not None and item.payload.expires_at == NOW + timedelta(hours=2)


def test_drain_stores_every_waiting_event(backend: AppBackend, history: History) -> None:
    import queue

    events: "queue.Queue[ClipboardEvent]" = queue.Queue()
    events.put(event(FAKE_NOTE, 1))
    events.put(event("FAKE second note", 2))
    assert backend.drain(events) == 2
    assert len(history.items()) == 2
    assert backend.drain(events) == 0


def test_cleanup_deletes_expired_secret(
    backend: AppBackend, history: History, clock: Clock, sequence: list[int]
) -> None:
    item_id = backend.handle_event(event(FAKE_TOKEN, 1))
    clock.now = NOW + timedelta(hours=2)
    assert backend.cleanup() == [item_id]
    assert history.get(item_id) is None
    # The mark-secret hotkey no longer points at it.
    assert backend.mark_last_secret() is False


# --- Window actions ---


def test_copy_back_of_normal_item_is_plain(backend: AppBackend, copier: FakeCopier) -> None:
    item_id = backend.handle_event(event(FAKE_NOTE))
    assert backend.copy_back(item_id) is True
    assert copier.requests == [(FAKE_NOTE, False)]


@pytest.mark.parametrize("text", [FAKE_TOKEN, FAKE_UNSURE], ids=["secret", "unsure"])
def test_copy_back_of_secret_and_unsure_is_safe(
    backend: AppBackend, copier: FakeCopier, text: str
) -> None:
    item_id = backend.handle_event(event(text))
    backend.copy_back(item_id)
    assert copier.requests == [(text, True)]


def test_copy_back_fails_when_listener_stopped(history: History) -> None:
    backend = AppBackend(history, FakeCopier(running=False), PauseSwitch(), lambda: 1)
    item_id = backend.handle_event(event(FAKE_NOTE))
    assert backend.copy_back(item_id) is False
    assert backend.copy_back(999) is False


def test_mark_last_secret_needs_the_clipboard_unchanged(
    backend: AppBackend, history: History, sequence: list[int]
) -> None:
    item_id = backend.handle_event(event(FAKE_NOTE, 5))
    sequence[0] = 6
    assert backend.mark_last_secret() is False
    sequence[0] = 5
    assert backend.mark_last_secret() is True
    item = history.get(item_id)
    assert item is not None and item.payload.secret_state is SecretState.SECRET


def test_mark_normal_then_pin(backend: AppBackend, history: History) -> None:
    item_id = backend.handle_event(event(FAKE_UNSURE))
    assert backend.pin(item_id) is False
    assert backend.mark_normal(item_id) is True
    item = history.get(item_id)
    assert item is not None
    assert item.payload.secret_state is SecretState.NORMAL
    assert item.payload.expires_at is None
    assert item.payload.label is not Label.POSSIBLE_SECRET
    assert backend.pin(item_id) is True
    assert backend.unpin(item_id) is True


def test_mark_secret_unpins(backend: AppBackend, history: History) -> None:
    item_id = backend.handle_event(event(FAKE_NOTE))
    backend.pin(item_id)
    assert backend.mark_secret(item_id) is True
    item = history.get(item_id)
    assert item is not None and item.payload.pinned is False


def test_delete_forgets_last_capture(backend: AppBackend, history: History) -> None:
    item_id = backend.handle_event(event(FAKE_NOTE, 1))
    assert backend.delete(item_id) is True
    assert history.get(item_id) is None
    assert backend.mark_last_secret() is False
    assert backend.delete(item_id) is False


def test_restore_puts_deleted_items_back_with_their_pins(
    backend: AppBackend, history: History
) -> None:
    first = backend.handle_event(event(FAKE_NOTE, 1))
    backend.pin(first)
    kept = history.get(first)
    assert kept is not None
    backend.delete(first)
    assert backend.restore([kept]) == 1
    [back] = history.items()
    assert back.payload.content == FAKE_NOTE and back.payload.pinned is True
    assert backend.restore([]) == 0


def test_restore_skips_an_expired_secret(
    backend: AppBackend, history: History, clock: Clock
) -> None:
    item_id = backend.handle_event(event(FAKE_TOKEN, 1))
    kept = history.get(item_id)
    assert kept is not None and kept.payload.secret_state is SecretState.SECRET
    backend.delete(item_id)
    clock.now = NOW + timedelta(hours=2, seconds=1)
    assert backend.restore([kept]) == 0
    assert history.items() == []


def test_actions_on_missing_items_return_false(backend: AppBackend) -> None:
    assert backend.mark_normal(42) is False
    assert backend.mark_secret(42) is False


def test_toggle_pause(backend: AppBackend) -> None:
    assert backend.is_paused() is False
    assert backend.toggle_pause() is True
    assert backend.is_paused() is True


def test_quit_calls_back() -> None:
    called: list[bool] = []
    backend = AppBackend(None, FakeCopier(), PauseSwitch(), lambda: 1, on_quit=lambda: called.append(True))  # type: ignore[arg-type]
    backend.quit()
    assert called == [True]


# --- No content in logs ---


def test_whole_flow_never_logs_content(backend: AppBackend, clock: Clock) -> None:
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    logsafe.attach_handler(handler)
    try:
        note_id = backend.handle_event(event(FAKE_NOTE, 1))
        secret_id = backend.handle_event(event(FAKE_TOKEN, 2))
        unsure_id = backend.handle_event(event(FAKE_UNSURE, 3))
        backend.copy_back(note_id)
        backend.copy_back(secret_id)
        backend.mark_normal(unsure_id)
        backend.mark_secret(note_id)
        backend.toggle_pause()
        backend.delete(note_id)
        clock.now = NOW + timedelta(hours=3)
        backend.cleanup()
    finally:
        logging.getLogger(logsafe.ROOT_LOGGER_NAME).removeHandler(handler)
    text = stream.getvalue()
    for secret in (FAKE_NOTE, FAKE_TOKEN, FAKE_UNSURE, "fakeeditor"):
        assert secret not in text
    assert "item_captured" in text and "content_type=possible_secret" in text


# --- Start-up messages ---


def test_key_error_message_says_what_to_do(tmp_path: Path) -> None:
    key_path = tmp_path / "dedupe.key"
    message = key_error_message(key_path)
    assert str(key_path) in message
    assert "left as it is" in message
    assert "rename or move that file yourself" in message
    assert "not be merged" in message


def test_single_instance_lock_refuses_a_second_holder() -> None:
    import win32api
    import win32event
    import winerror

    name = r"Local\SmartClipboard-test-instance-lock"
    first = win32event.CreateMutex(None, False, name)
    assert win32api.GetLastError() != winerror.ERROR_ALREADY_EXISTS
    second = win32event.CreateMutex(None, False, name)
    assert win32api.GetLastError() == winerror.ERROR_ALREADY_EXISTS
    del second, first


# --- Clearing captured secrets from the clipboard (option B) ---


def test_secret_capture_asks_for_a_clear(backend: AppBackend, copier: FakeCopier) -> None:
    backend.handle_event(event(FAKE_TOKEN, 7))
    assert copier.clear_requests == [7]


@pytest.mark.parametrize("text", [FAKE_NOTE, FAKE_UNSURE], ids=["normal", "unsure"])
def test_normal_and_unsure_captures_are_not_cleared(
    backend: AppBackend, copier: FakeCopier, text: str
) -> None:
    backend.handle_event(event(text, 7))
    assert copier.clear_requests == []


def test_recopy_of_a_secret_item_is_cleared_again(backend: AppBackend, copier: FakeCopier) -> None:
    backend.handle_event(event(FAKE_TOKEN, 7))
    backend.handle_event(event(FAKE_TOKEN, 9, at=NOW + timedelta(minutes=1)))
    assert copier.clear_requests == [7, 9]


def test_marking_the_current_item_secret_asks_for_a_clear(
    backend: AppBackend, copier: FakeCopier, sequence: list[int]
) -> None:
    item_id = backend.handle_event(event(FAKE_UNSURE, 5))
    sequence[0] = 5
    backend.mark_secret(item_id)
    assert copier.clear_requests == [5]


def test_hotkey_mark_secret_asks_for_a_clear(
    backend: AppBackend, copier: FakeCopier, sequence: list[int]
) -> None:
    backend.handle_event(event(FAKE_NOTE, 5))
    sequence[0] = 5
    assert backend.mark_last_secret() is True
    assert copier.clear_requests == [5]


def test_marking_an_older_item_secret_does_not_clear(
    backend: AppBackend, copier: FakeCopier, sequence: list[int]
) -> None:
    old_id = backend.handle_event(event(FAKE_NOTE, 5))
    backend.handle_event(event("FAKE newer note", 6))
    sequence[0] = 6
    backend.mark_secret(old_id)
    assert copier.clear_requests == []


def test_marking_the_current_secret_normal_cancels_the_clear(
    backend: AppBackend, copier: FakeCopier, sequence: list[int]
) -> None:
    item_id = backend.handle_event(event(FAKE_TOKEN, 5))
    sequence[0] = 5
    backend.mark_normal(item_id)
    assert copier.clear_requests == [5, None]


def test_marking_an_older_item_normal_does_not_cancel(
    backend: AppBackend, copier: FakeCopier, sequence: list[int]
) -> None:
    old_id = backend.handle_event(event(FAKE_TOKEN, 5))
    backend.handle_event(event(FAKE_NOTE, 6))
    sequence[0] = 6
    backend.mark_normal(old_id)
    assert copier.clear_requests == [5]


# --- Removing secrets from Win+V history (option C) ---


class FakeRemover:
    def __init__(self) -> None:
        self.digests: list[str] = []

    def submit(self, digest: str) -> None:
        self.digests.append(digest)


@pytest.fixture
def remover() -> FakeRemover:
    return FakeRemover()


@pytest.fixture
def c_backend(
    history: History, copier: FakeCopier, sequence: list[int], clock: Clock, remover: FakeRemover
) -> AppBackend:
    return AppBackend(
        history, copier, PauseSwitch(), lambda: sequence[0], now=clock, win_history=remover
    )


def test_captured_secret_is_removed_after_the_paste_window(
    c_backend: AppBackend, remover: FakeRemover, clock: Clock
) -> None:
    c_backend.handle_event(event(FAKE_TOKEN, 7))
    assert c_backend.run_due_history_removals() == 0
    clock.now = NOW + timedelta(seconds=31)
    assert c_backend.run_due_history_removals() == 0  # the clipboard clear goes first
    clock.now = NOW + timedelta(seconds=32)
    assert c_backend.run_due_history_removals() == 1
    assert remover.digests == [KEY.digest(FAKE_TOKEN)]
    assert c_backend.run_due_history_removals() == 0  # only once


@pytest.mark.parametrize("text", [FAKE_NOTE, FAKE_UNSURE], ids=["normal", "unsure"])
def test_normal_and_unsure_are_never_removed(
    c_backend: AppBackend, remover: FakeRemover, clock: Clock, text: str
) -> None:
    c_backend.handle_event(event(text, 7))
    clock.now = NOW + timedelta(minutes=5)
    c_backend.run_due_history_removals()
    assert remover.digests == []


def test_review_item_marked_secret_while_on_clipboard_waits(
    c_backend: AppBackend, remover: FakeRemover, clock: Clock, sequence: list[int]
) -> None:
    item_id = c_backend.handle_event(event(FAKE_UNSURE, 5))
    sequence[0] = 5
    c_backend.mark_secret(item_id)
    assert remover.digests == []
    clock.now = NOW + timedelta(seconds=32)
    c_backend.run_due_history_removals()
    assert remover.digests == [KEY.digest(FAKE_UNSURE)]


def test_older_item_marked_secret_is_removed_at_once(
    c_backend: AppBackend, remover: FakeRemover, sequence: list[int]
) -> None:
    old_id = c_backend.handle_event(event(FAKE_UNSURE, 5))
    c_backend.handle_event(event(FAKE_NOTE, 6))
    sequence[0] = 6
    c_backend.mark_secret(old_id)
    assert remover.digests == [KEY.digest(FAKE_UNSURE)]


def test_mark_normal_cancels_the_removal(
    c_backend: AppBackend, remover: FakeRemover, clock: Clock, sequence: list[int]
) -> None:
    item_id = c_backend.handle_event(event(FAKE_TOKEN, 5))
    sequence[0] = 5
    c_backend.mark_normal(item_id)
    clock.now = NOW + timedelta(minutes=5)
    assert c_backend.run_due_history_removals() == 0
    assert remover.digests == []


def test_removal_still_runs_after_newer_copy_and_delete(
    c_backend: AppBackend, remover: FakeRemover, clock: Clock, sequence: list[int]
) -> None:
    item_id = c_backend.handle_event(event(FAKE_TOKEN, 5))
    c_backend.handle_event(event(FAKE_NOTE, 6))
    c_backend.delete(item_id)
    clock.now = NOW + timedelta(seconds=32)
    c_backend.run_due_history_removals()
    assert remover.digests == [KEY.digest(FAKE_TOKEN)]


def test_quit_runs_every_pending_removal(c_backend: AppBackend, remover: FakeRemover) -> None:
    c_backend.handle_event(event(FAKE_TOKEN, 5))
    assert c_backend.run_due_history_removals(everything=True) == 1
    assert remover.digests == [KEY.digest(FAKE_TOKEN)]


def test_without_a_remover_nothing_is_scheduled(backend: AppBackend, clock: Clock) -> None:
    backend.handle_event(event(FAKE_TOKEN, 5))
    clock.now = NOW + timedelta(minutes=1)
    assert backend.run_due_history_removals() == 0


# --- Labeled-data log (task 20) ---


@pytest.fixture
def labeled_log(tmp_path: Path) -> Iterator[LabeledLog]:
    with LabeledLog(tmp_path / "labeled.db") as opened:
        yield opened


@pytest.fixture
def logged_backend(
    history: History, copier: FakeCopier, sequence: list[int], clock: Clock, labeled_log: LabeledLog
) -> AppBackend:
    return AppBackend(
        history, copier, PauseSwitch(), lambda: sequence[0], now=clock, labeled_log=labeled_log
    )


def test_new_capture_is_logged_once_and_merges_are_not(
    logged_backend: AppBackend, labeled_log: LabeledLog
) -> None:
    item_id = logged_backend.handle_event(event(FAKE_NOTE, 1))
    logged_backend.handle_event(event(FAKE_NOTE, 2, at=NOW + timedelta(minutes=1)))
    [record] = labeled_log.load_all()[0]
    assert (record.event, record.item_id, record.content) == (RecordEvent.CAPTURED, item_id, FAKE_NOTE)
    assert record.owner_confirmed is False


def test_secret_capture_is_logged_redacted(
    logged_backend: AppBackend, labeled_log: LabeledLog
) -> None:
    logged_backend.handle_event(event(FAKE_TOKEN))
    [record] = labeled_log.load_all()[0]
    assert record.redacted and record.content != FAKE_TOKEN
    assert record.content.startswith("ghp_") and len(record.content) == len(FAKE_TOKEN)
    assert record.secret_state is SecretState.SECRET


def test_marking_writes_confirmed_records(
    logged_backend: AppBackend, labeled_log: LabeledLog, clock: Clock
) -> None:
    unsure_id = logged_backend.handle_event(event(FAKE_UNSURE))
    clock.now = NOW + timedelta(minutes=5)
    logged_backend.mark_normal(unsure_id)
    logged_backend.mark_normal(unsure_id)  # no change, no record
    logged_backend.mark_secret(unsure_id)
    captured, normal, secret = labeled_log.load_all()[0]
    assert captured.redacted and captured.content != FAKE_UNSURE
    assert (normal.event, normal.owner_confirmed, normal.at) == (
        RecordEvent.MARKED_NORMAL, True, NOW + timedelta(minutes=5)
    )
    assert normal.content == FAKE_UNSURE and not normal.redacted  # owner's decision
    assert secret.event is RecordEvent.MARKED_SECRET
    assert secret.redacted and secret.content != FAKE_UNSURE


def test_deleting_an_item_keeps_its_records(
    logged_backend: AppBackend, labeled_log: LabeledLog
) -> None:
    item_id = logged_backend.handle_event(event(FAKE_NOTE))
    logged_backend.delete(item_id)
    assert len(labeled_log.load_all()[0]) == 1


def test_no_log_is_written_when_turned_off(backend: AppBackend, tmp_path: Path) -> None:
    backend.handle_event(event(FAKE_NOTE))
    assert not (tmp_path / "labeled.db").exists()
