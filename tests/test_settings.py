"""Settings: defaults, a missing or broken file, out-of-range values, saving.

Fake values only. The excluded-app names are made up.
"""

import json
from datetime import timedelta
from pathlib import Path

import pytest

from smartclip import config
from smartclip.settings import (
    FIELD_NAMES,
    JsonSettings,
    MemorySettings,
    Settings,
    clean_app_name,
    from_raw,
)


def test_defaults_come_from_config() -> None:
    s = Settings()
    assert s.normal_retention == config.NORMAL_RETENTION
    assert s.secret_retention == config.SECRET_RETENTION
    assert s.excluded_apps == ()
    assert s.pause_on_start is False
    assert s.font == "plex"


def test_missing_file_gives_defaults(tmp_path: Path) -> None:
    assert JsonSettings(tmp_path / "settings.json").load() == Settings()


@pytest.mark.parametrize("text", ["", "not json", "[1, 2]", "null", '"text"', "{"])
def test_malformed_file_gives_defaults(tmp_path: Path, text: str) -> None:
    path = tmp_path / "settings.json"
    path.write_text(text, encoding="utf-8")
    assert JsonSettings(path).load() == Settings()


def test_unreadable_file_gives_defaults(tmp_path: Path) -> None:
    folder = tmp_path / "settings.json"
    folder.mkdir()  # reading a folder as a file fails with OSError
    assert JsonSettings(folder).load() == Settings()


def test_round_trip(tmp_path: Path) -> None:
    store = JsonSettings(tmp_path / "settings.json")
    wanted = Settings(
        normal_retention_days=30,
        secret_retention_hours=3,
        excluded_apps=("fakemanager.exe", "fakebank.exe"),
        pause_on_start=True,
        font="system",
    )
    assert store.save(wanted) is True
    assert store.load() == wanted


@pytest.mark.parametrize(
    "field, bad",
    [
        ("normal_retention_days", 0),
        ("normal_retention_days", 99999),
        ("normal_retention_days", -5),
        ("normal_retention_days", "90"),
        ("normal_retention_days", True),
        ("normal_retention_days", 3.5),
        ("secret_retention_hours", 0),
        ("secret_retention_hours", 100),
        ("secret_retention_hours", None),
    ],
)
def test_out_of_range_number_falls_back_to_its_default(field: str, bad: object) -> None:
    loaded = from_raw({field: bad})
    assert getattr(loaded, field) == getattr(Settings(), field)


def test_one_bad_value_does_not_spoil_the_rest() -> None:
    loaded = from_raw(
        {"normal_retention_days": 0, "secret_retention_hours": 6, "pause_on_start": True}
    )
    assert loaded.normal_retention_days == Settings().normal_retention_days
    assert loaded.secret_retention_hours == 6
    assert loaded.pause_on_start is True


def test_limits_themselves_are_allowed() -> None:
    assert from_raw({"normal_retention_days": 1}).normal_retention == timedelta(days=1)
    assert from_raw({"normal_retention_days": 3650}).normal_retention_days == 3650
    assert from_raw({"secret_retention_hours": 1}).secret_retention_hours == 1
    assert from_raw({"secret_retention_hours": 24}).secret_retention_hours == 24


@pytest.mark.parametrize("minutes, hours", [(5, 1), (60, 1), (61, 2), (120, 2), (1440, 24)])
def test_an_old_file_with_minutes_is_rounded_up_to_whole_hours(minutes: int, hours: int) -> None:
    assert from_raw({"secret_retention_minutes": minutes}).secret_retention_hours == hours


def test_hours_win_over_an_old_minutes_value() -> None:
    raw = {"secret_retention_hours": 4, "secret_retention_minutes": 30}
    assert from_raw(raw).secret_retention_hours == 4


def test_bad_true_false_and_font_fall_back() -> None:
    loaded = from_raw({"pause_on_start": "yes", "font": "comic"})
    assert loaded == Settings()


def test_unknown_keys_are_ignored() -> None:
    assert from_raw({"theme_mode": "dark", "surprise": 1}) == Settings()


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("FakeManager.EXE", "fakemanager.exe"),
        ("  fakemanager.exe  ", "fakemanager.exe"),
        (r"C:\Fake\Folder\Tool.exe", "tool.exe"),
        ("my app (2).exe", "my app (2).exe"),
    ],
)
def test_clean_app_name_accepts_executable_names(raw: str, expected: str) -> None:
    assert clean_app_name(raw) == expected


@pytest.mark.parametrize(
    "raw",
    ["", "   ", "fakemanager", "fake.txt", ".exe", "a;b.exe", "x" * 80 + ".exe", "<b>.exe", "fake\n.exe"],
)
def test_clean_app_name_refuses_everything_else(raw: str) -> None:
    assert clean_app_name(raw) is None


def test_excluded_apps_are_cleaned_deduplicated_and_capped() -> None:
    loaded = from_raw(
        {"excluded_apps": ["A.exe", "a.exe", "bad", 7, None, r"C:\x\B.exe"]}
    )
    assert loaded.excluded_apps == ("a.exe", "b.exe")
    many = [f"app{i}.exe" for i in range(80)]
    assert len(from_raw({"excluded_apps": many}).excluded_apps) == 50
    assert from_raw({"excluded_apps": "a.exe"}).excluded_apps == ()


def test_saved_file_holds_settings_and_nothing_else(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"
    JsonSettings(path).save(Settings(excluded_apps=("fakemanager.exe",)))
    data = json.loads(path.read_text(encoding="utf-8"))
    assert set(data) == set(FIELD_NAMES)
    assert not path.with_suffix(".tmp").exists()


def test_save_failure_returns_false(tmp_path: Path) -> None:
    blocker = tmp_path / "blocker"
    blocker.write_text("x", encoding="utf-8")  # a file where a folder is needed
    assert JsonSettings(blocker / "settings.json").save(Settings()) is False


def test_memory_store_keeps_what_it_is_given() -> None:
    store = MemorySettings()
    assert store.load() == Settings()
    assert store.save(Settings(font="system")) is True
    assert store.load().font == "system"
