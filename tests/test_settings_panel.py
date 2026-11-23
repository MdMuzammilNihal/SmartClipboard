"""The settings panel and how changed settings reach the app. Fake data only."""

import os
from datetime import datetime, timedelta, timezone

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication, QWidget  # noqa: E402

from smartclip import config, overrides  # noqa: E402
from smartclip.app import apply_settings, retention_policy  # noqa: E402
from smartclip.gate import Gate  # noqa: E402
from smartclip.labels import Label, SecretState  # noqa: E402
from smartclip.settings import MemorySettings, Settings  # noqa: E402
from smartclip.storage import ItemPayload  # noqa: E402
from smartclip.ui.settings_dialog import SettingsPanel  # noqa: E402
from smartclip.ui.window import SearchWindow  # noqa: E402
from test_window import FakeBackend  # noqa: E402


class FakeSwitch:
    def __init__(self, label: str, works: bool = True) -> None:
        self.label = label
        self.on = False
        self.works = works

    def is_on(self) -> bool:
        return self.on

    def set_on(self, on: bool) -> bool:
        if self.works:
            self.on = on
        return self.works


class FailingStore(MemorySettings):
    def save(self, settings: Settings) -> bool:
        return False


class FakeHistory:
    def __init__(self) -> None:
        self.policies: list[object] = []

    def set_policy(self, policy: object) -> None:
        self.policies.append(policy)


@pytest.fixture
def qapp() -> QApplication:
    return QApplication.instance() or QApplication([])


@pytest.fixture
def applied() -> list[Settings]:
    return []


@pytest.fixture
def store() -> MemorySettings:
    return MemorySettings()


@pytest.fixture
def panel(qapp: QApplication, store: MemorySettings, applied: list[Settings]) -> SettingsPanel:
    return SettingsPanel(store, applied.append, switches=[FakeSwitch("Start with Windows")])


def test_panel_shows_the_saved_settings(qapp: QApplication) -> None:
    saved = Settings(
        normal_retention_days=30, secret_retention_minutes=45,
        excluded_apps=("fakemanager.exe",), pause_on_start=True, font="system", show_hints=False,
    )
    shown = SettingsPanel(MemorySettings(saved), lambda _s: None)
    assert shown.normal_days.value() == 30
    assert shown.secret_minutes.value() == 45
    assert shown.excluded_list.count() == 1
    assert shown.pause_on_start.isChecked()
    assert shown.font_box.currentData() == "system"
    assert not shown.show_hints.isChecked()


def test_showing_the_panel_saves_nothing(
    panel: SettingsPanel, store: MemorySettings, applied: list[Settings]
) -> None:
    panel.load_from_settings()
    assert applied == []
    assert store.load() == Settings()


def test_changing_retention_saves_and_applies(
    panel: SettingsPanel, store: MemorySettings, applied: list[Settings]
) -> None:
    panel.normal_days.setValue(30)
    panel.secret_minutes.setValue(60)
    assert store.load().normal_retention_days == 30
    assert store.load().secret_retention_minutes == 60
    assert applied[-1] == store.load()


def test_font_choice_saves_and_applies(
    panel: SettingsPanel, store: MemorySettings, applied: list[Settings]
) -> None:
    panel.font_box.setCurrentIndex(panel.font_box.findData("system"))
    assert store.load().font == "system"
    assert applied[-1].font == "system"


def test_pause_on_start_and_hints_save(panel: SettingsPanel, store: MemorySettings) -> None:
    panel.pause_on_start.setChecked(True)
    panel.show_hints.setChecked(False)
    assert store.load().pause_on_start is True
    assert store.load().show_hints is False


def test_add_excluded_app_is_cleaned_saved_and_applied(
    panel: SettingsPanel, store: MemorySettings, applied: list[Settings]
) -> None:
    panel.excluded_input.setText(r"C:\Fake\FakeManager.EXE")
    panel.add_excluded()
    assert store.load().excluded_apps == ("fakemanager.exe",)
    assert applied[-1].excluded_apps == ("fakemanager.exe",)
    assert panel.excluded_list.item(0).text() == "fakemanager.exe"
    assert panel.excluded_input.text() == ""


def test_bad_or_repeated_app_names_are_refused_with_a_message(
    panel: SettingsPanel, store: MemorySettings
) -> None:
    for bad in ("", "notanexe", "bad;name.exe"):
        panel.excluded_input.setText(bad)
        panel.add_excluded()
        assert "executable name" in panel.excluded_message.text()
    assert store.load().excluded_apps == ()
    panel.excluded_input.setText("fakebank.exe")
    panel.add_excluded()
    panel.excluded_input.setText("FakeBank.exe")
    panel.add_excluded()
    assert panel.excluded_message.text() == "Already on the list"
    assert store.load().excluded_apps == ("fakebank.exe",)


def test_remove_excluded_app(panel: SettingsPanel, store: MemorySettings) -> None:
    for name in ("one.exe", "two.exe"):
        panel.excluded_input.setText(name)
        panel.add_excluded()
    panel.remove_excluded()
    assert "Choose" in panel.excluded_message.text()
    panel.excluded_list.setCurrentRow(0)
    panel.remove_excluded()
    assert store.load().excluded_apps == ("two.exe",)
    assert panel.excluded_list.count() == 1


def test_save_failure_says_so_and_changes_nothing(
    qapp: QApplication, applied: list[Settings]
) -> None:
    broken = SettingsPanel(FailingStore(), applied.append)
    broken.normal_days.setValue(10)
    assert "Could not save" in broken.status_label.text()
    assert applied == []
    assert broken.settings == Settings()


def test_switches_follow_the_real_setting_and_report_failure(qapp: QApplication) -> None:
    good, bad = FakeSwitch("Start with Windows"), FakeSwitch("Start menu shortcut", works=False)
    shown = SettingsPanel(MemorySettings(), lambda _s: None, switches=[good, bad])
    (_g, good_box), (_b, bad_box) = shown.switch_boxes
    good_box.click()
    assert good.on and good_box.isChecked()
    bad_box.click()
    assert not bad_box.isChecked()
    assert "Start menu shortcut" in shown.status_label.text()
    good.on = False  # changed outside the app
    shown.sync_switches()
    assert not good_box.isChecked()


def test_data_folder_button_calls_the_opener(qapp: QApplication) -> None:
    calls: list[int] = []
    shown = SettingsPanel(MemorySettings(), lambda _s: None, show_data_folder=lambda: calls.append(1))
    shown.folder_button.click()
    assert calls == [1]
    assert not SettingsPanel(MemorySettings(), lambda _s: None).folder_button.isEnabled()


def test_escape_closes_the_panel(panel: SettingsPanel) -> None:
    panel.show()
    QTest.keyClick(panel, Qt.Key.Key_Escape)
    assert not panel.isVisible()


# --- How changed settings reach the app ---


def test_retention_policy_follows_the_settings() -> None:
    policy = retention_policy(Settings(normal_retention_days=7, secret_retention_minutes=30))
    assert policy.normal == timedelta(days=7)
    assert policy.secret == timedelta(minutes=30)


def test_apply_settings_updates_policy_exclusions_and_font_without_restart() -> None:
    history, gate, fonts = FakeHistory(), Gate(), []
    apply_settings(
        Settings(normal_retention_days=7, excluded_apps=("fakebank.exe",), font="system"),
        history, gate, fonts.append,  # type: ignore[arg-type]
    )
    assert history.policies[-1].normal == timedelta(days=7)  # type: ignore[attr-defined]
    assert gate.excluded_apps == frozenset({"fakebank.exe"})
    assert fonts == ["system"]


def test_mark_secret_uses_the_chosen_secret_length() -> None:
    now = datetime(2026, 11, 24, 12, 0, tzinfo=timezone.utc)
    payload = ItemPayload("FAKE-token-123", Label.NOTE)
    default = overrides.mark_secret(payload, now)
    short = overrides.mark_secret(payload, now, timedelta(minutes=30))
    assert default.expires_at == now + config.SECRET_RETENTION
    assert short.expires_at == now + timedelta(minutes=30)
    assert short.secret_state is SecretState.SECRET


# --- In the window ---


@pytest.fixture
def window(qapp: QApplication, store: MemorySettings, applied: list[Settings]) -> SearchWindow:
    def factory(parent: QWidget) -> SettingsPanel:
        return SettingsPanel(store, applied.append, parent=parent)

    win = SearchWindow(FakeBackend(), settings_panel_factory=factory)
    win.show_and_focus()
    return win


def test_menu_has_a_settings_entry_that_opens_the_panel(window: SearchWindow) -> None:
    assert window.settings_action.isEnabled()
    window.settings_action.trigger()
    assert window.settings_panel is not None and window.settings_panel.isVisible()
    assert window.isVisible()


def test_settings_entry_is_disabled_without_a_panel(qapp: QApplication) -> None:
    win = SearchWindow(FakeBackend())
    assert not win.settings_action.isEnabled()
    win.open_settings()  # does nothing, does not fail


def test_hiding_the_window_hides_the_panel(window: SearchWindow) -> None:
    window.open_settings()
    window.hide()
    assert window.settings_panel is not None and not window.settings_panel.isVisible()


def test_panel_uses_the_windows_style_sheet(window: SearchWindow) -> None:
    assert window.settings_panel is not None
    assert window.settings_panel.parent() is window
    assert window.styleSheet() != ""


# --- Layout: nothing overlaps or is clipped ---


def test_tick_boxes_have_room_for_their_square(panel: SettingsPanel) -> None:
    from smartclip.ui import theme

    panel.parent() or panel.setStyleSheet(theme.stylesheet(theme.LIGHT))
    panel.show()
    for box in (panel.pause_on_start, panel.show_hints):
        assert box.height() >= theme.INDICATOR_SIZE + 8  # not clipped at the top or bottom


def test_the_secret_note_does_not_overlap_the_tick_box_below_it(panel: SettingsPanel) -> None:
    from PySide6.QtWidgets import QLabel

    panel.setStyleSheet("")
    panel.show()
    note = next(label for label in panel.findChildren(QLabel) if label.text().startswith("Applies"))
    note_bottom = note.mapTo(panel, note.rect().bottomLeft()).y()
    box_top = panel.pause_on_start.mapTo(panel, panel.pause_on_start.rect().topLeft()).y()
    assert note_bottom < box_top


def test_check_icons_exist_for_both_themes() -> None:
    from pathlib import Path

    from smartclip.ui import theme

    for t in (theme.LIGHT, theme.DARK):
        assert Path(theme.check_icon(t)).is_file()
        assert f"url({theme.check_icon(t)})" in theme.stylesheet(t)
