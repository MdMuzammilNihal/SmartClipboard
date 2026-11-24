"""Fixes from the owner's first M1/M2 hand check (2026-11-25). Fake data only."""

import os
from datetime import timedelta

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QCoreApplication, QEvent  # noqa: E402
from PySide6.QtWidgets import QApplication, QSpacerItem  # noqa: E402

from smartclip import overrides  # noqa: E402
from smartclip.labels import Label, SecretState  # noqa: E402
from smartclip.storage import ItemPayload  # noqa: E402
from smartclip.ui import theme  # noqa: E402
from smartclip.ui.confirm import ConfirmDialog  # noqa: E402
from smartclip.ui.view_model import RevealState, build_rows  # noqa: E402
from smartclip.ui.window import SearchWindow  # noqa: E402
from test_window import START, Clock, FakeBackend, backend, clock, qapp  # noqa: E402,F401


def test_marking_a_normal_item_secret_starts_the_full_timer() -> None:
    payload = ItemPayload("FAKE note", Label.NOTE)
    marked = overrides.mark_secret(payload, START, timedelta(hours=2))
    assert marked.expires_at == START + timedelta(hours=2)
    assert marked.secret_state is SecretState.SECRET


def test_a_secret_just_marked_shows_its_countdown_while_still_selected(
    qapp: QApplication, backend: FakeBackend, clock: Clock
) -> None:
    win = SearchWindow(backend, now=clock)
    win.show_and_focus()
    win.select_item(1)
    # The fake backend only flips the state, so give it the deadline the real one sets.
    backend._set(1, secret_state=SecretState.SECRET, expires_at=START + timedelta(hours=2))
    win.refresh()
    row = next(row for row in win.model.rows if row.item_id == 1)
    assert row.remaining == timedelta(hours=2)
    assert win.selected_row().item_id == 1  # still selected, so its buttons are showing
    win.hide()


def test_chips_have_no_leftover_spacer_after_the_last_secret_goes(
    qapp: QApplication, backend: FakeBackend, clock: Clock
) -> None:
    win = SearchWindow(backend, now=clock)
    win.show_and_focus()
    for item_id in (3, 4):  # the fake secret and the unsure item
        backend.delete(item_id)
        win.refresh()
    layout = win.chips_layout
    spacers = [layout.itemAt(i) for i in range(layout.count()) if isinstance(layout.itemAt(i), QSpacerItem)]
    assert len(spacers) == 1  # one stretch, at the right end
    assert layout.itemAt(0).widget() is win.chips[None]  # "All" is the first thing, at the left
    assert Label.NOTE in win.chips and SecretState.SECRET not in win.chips
    win.hide()


def test_the_window_works_when_insights_is_missing(
    qapp: QApplication, backend: FakeBackend, clock: Clock, monkeypatch
) -> None:
    import smartclip.ui.window as window_module

    monkeypatch.setattr(window_module, "InsightsView", None)
    monkeypatch.setattr(window_module, "compute_insights", None)
    win = SearchWindow(backend, now=clock)
    win.show_and_focus()
    assert win.insights_view is None
    assert not win.segment.isVisibleTo(win)  # no History | Insights tabs
    win.set_page("insights")
    assert win.page == "history"
    assert len(win.model.rows) == 4
    win.hide()


def test_confirm_dialog_cancel_is_the_default_and_confirm_accepts(qapp: QApplication) -> None:
    dialog = ConfirmDialog(None, "Clear all?", "Delete these 3 items?", "Delete")
    assert dialog.cancel_button.isDefault() and not dialog.confirm_button.isDefault()
    assert dialog.confirm_button.text() == "Delete"
    dialog.confirm_button.click()
    assert dialog.result() == dialog.DialogCode.Accepted
    other = ConfirmDialog(None, "Clear all?", "Delete these 3 items?", "Delete")
    other.cancel_button.click()
    assert other.result() == other.DialogCode.Rejected


def test_confirm_dialog_cancels_when_the_focus_goes_to_another_app(qapp: QApplication) -> None:
    dialog = ConfirmDialog(None, "Clear all?", "Delete?", "Delete", active_window=lambda: None)
    dialog.show()
    QCoreApplication.sendEvent(dialog, QEvent(QEvent.Type.ActivationChange))
    for _ in range(5):
        qapp.processEvents()
    # Offscreen the dialog may count as active; only the "left for another app" path matters.
    if not dialog.isActiveWindow():
        assert dialog.result() == dialog.DialogCode.Rejected
        assert not dialog.isVisible()
    dialog.close()


def test_confirm_dialog_is_modal_on_top_and_themed(qapp: QApplication) -> None:
    from PySide6.QtCore import Qt

    dialog = ConfirmDialog(None, "Clear all?", "Delete?", "Delete", current_theme=lambda: theme.DARK)
    assert dialog.windowModality() == Qt.WindowModality.ApplicationModal
    assert dialog.windowFlags() & Qt.WindowType.WindowStaysOnTopHint
    assert theme.DARK.danger in dialog.styleSheet()
