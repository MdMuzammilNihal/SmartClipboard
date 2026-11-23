"""The settings the owner can change in the app (UI plan item 1, 2026-11-24).

Kept in a small JSON file in the app's data folder (config.settings_path()).
Every value has a default and a minimum and maximum. A value that is
missing, has the wrong type or is out of range falls back to its default on
its own; the rest of the file is still used. Defaults for retention come from
config.py.

Owner's decisions (2026-11-24): the window keeps following the Windows light
or dark setting, so there is no theme choice here.

SECURITY REVIEW (owner): the file holds numbers, true/false values, a font
choice and the exclusion list, which is executable names such as
"keepass.exe" and nothing else. It never holds clipboard content, item ids or
window titles. The names are configuration, not clipboard content, but they
say which apps you use, so the file is not meant to be shared. It is not
encrypted. A missing, unreadable or malformed file gives the defaults and is
logged by error class only, never with an error dialog.
"""

import json
import os
import re
from dataclasses import asdict, dataclass, fields
from datetime import timedelta
from pathlib import Path
from typing import Any, Protocol

from smartclip import config, logsafe
from smartclip.sourceapp import exe_name

_log = logsafe.get_logger("settings")

NORMAL_DAYS_RANGE = (1, 3650)
SECRET_MINUTES_RANGE = (5, 1440)
MAX_EXCLUDED_APPS = 50
FONT_CHOICES = ("plex", "system")
# A plain Windows executable name: letters, digits, spaces and . _ - ( ) only.
_EXE_PATTERN = re.compile(r"^[a-z0-9][a-z0-9 ._()\-]{0,62}\.exe$")


def clean_app_name(raw: str) -> str | None:
    """The lower-case executable name for `raw`, or None if it is not one.

    A full path is cut to its file name, so "C:\\Fake\\Tool.EXE" becomes
    "tool.exe". Names that do not end in ".exe" or hold odd characters are
    refused, so the list can only ever hold names the gate can match.
    """
    name = exe_name(raw)
    if name is None or not _EXE_PATTERN.match(name):
        return None
    return name


@dataclass(frozen=True)
class Settings:
    """Everything the settings panel edits."""

    normal_retention_days: int = config.NORMAL_RETENTION.days
    secret_retention_minutes: int = int(config.SECRET_RETENTION.total_seconds() // 60)
    excluded_apps: tuple[str, ...] = ()
    pause_on_start: bool = False
    font: str = "plex"
    show_hints: bool = True

    @property
    def normal_retention(self) -> timedelta:
        return timedelta(days=self.normal_retention_days)

    @property
    def secret_retention(self) -> timedelta:
        return timedelta(minutes=self.secret_retention_minutes)


def _in_range(value: object, low_high: tuple[int, int]) -> bool:
    # bool is an int in Python; a true/false is not a number of days.
    return (
        isinstance(value, int)
        and not isinstance(value, bool)
        and low_high[0] <= value <= low_high[1]
    )


def _clean_apps(value: object) -> tuple[str, ...] | None:
    if not isinstance(value, list):
        return None
    names: list[str] = []
    for entry in value:
        cleaned = clean_app_name(entry) if isinstance(entry, str) else None
        if cleaned is not None and cleaned not in names:
            names.append(cleaned)
    return tuple(names[:MAX_EXCLUDED_APPS])


def from_raw(raw: object) -> Settings:
    """Build Settings from decoded JSON, field by field, defaults for bad ones."""
    if not isinstance(raw, dict):
        return Settings()
    values: dict[str, Any] = {}
    if _in_range(raw.get("normal_retention_days"), NORMAL_DAYS_RANGE):
        values["normal_retention_days"] = raw["normal_retention_days"]
    if _in_range(raw.get("secret_retention_minutes"), SECRET_MINUTES_RANGE):
        values["secret_retention_minutes"] = raw["secret_retention_minutes"]
    apps = _clean_apps(raw.get("excluded_apps"))
    if apps is not None:
        values["excluded_apps"] = apps
    for name in ("pause_on_start", "show_hints"):
        if isinstance(raw.get(name), bool):
            values[name] = raw[name]
    if raw.get("font") in FONT_CHOICES:
        values["font"] = raw["font"]
    return Settings(**values)


class SettingsStore(Protocol):
    """Where Settings are kept. A fake in tests, a JSON file in the app."""

    def load(self) -> Settings: ...
    def save(self, settings: Settings) -> bool: ...


class MemorySettings:
    """Keeps the settings only while the app runs (the default in tests)."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or Settings()

    def load(self) -> Settings:
        return self.settings

    def save(self, settings: Settings) -> bool:
        self.settings = settings
        return True


class JsonSettings:
    """Keeps the settings in a JSON file, written whole through a temporary file."""

    def __init__(self, path: Path) -> None:
        self._path = path

    def load(self) -> Settings:
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return Settings()
        except (OSError, ValueError) as error:
            logsafe.log_error(_log, "settings_unreadable", error)
            return Settings()
        if not isinstance(raw, dict):
            logsafe.log_event(_log, "settings_malformed")
        return from_raw(raw)

    def save(self, settings: Settings) -> bool:
        data = asdict(settings)
        data["excluded_apps"] = list(settings.excluded_apps)
        temporary = self._path.with_suffix(".tmp")
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            temporary.write_text(json.dumps(data, indent=2), encoding="utf-8")
            os.replace(temporary, self._path)
        except OSError as error:
            logsafe.log_error(_log, "settings_not_saved", error)
            return False
        return True


# Fields, for tests that check every one has a default.
FIELD_NAMES = tuple(field.name for field in fields(Settings))
