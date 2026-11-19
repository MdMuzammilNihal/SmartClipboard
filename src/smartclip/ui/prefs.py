"""Remembered window layout: whether the search bar and the filter chips show.

Owner's decision (2026-11-19): hiding the search bar or the filter chips
from the "..." menu is remembered after a restart, in a small JSON file in
the app's data folder (config.window_prefs_path()).

SECURITY REVIEW (owner): the file holds two true/false values and nothing
else. It is not encrypted because it holds no clipboard content, item ids,
or source apps. A missing, unreadable or malformed file falls back to the
defaults (both shown) and is logged by error class only.
"""

import json
import os
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import Protocol

from smartclip import logsafe

_log = logsafe.get_logger("prefs")


@dataclass(frozen=True)
class WindowPrefs:
    """What the window shows above the cards."""

    show_search: bool = True
    show_filters: bool = True


class PrefsStore(Protocol):
    """Where WindowPrefs are kept. A fake in tests, a JSON file in the app."""

    def load(self) -> WindowPrefs: ...
    def save(self, prefs: WindowPrefs) -> bool: ...


class MemoryPrefs:
    """Keeps the choice only while the app runs (the window's default)."""

    def __init__(self, prefs: WindowPrefs | None = None) -> None:
        self.prefs = prefs or WindowPrefs()

    def load(self) -> WindowPrefs:
        return self.prefs

    def save(self, prefs: WindowPrefs) -> bool:
        self.prefs = prefs
        return True


class JsonPrefs:
    """Keeps the choice in a JSON file, written whole through a temporary file."""

    def __init__(self, path: Path) -> None:
        self._path = path

    def load(self) -> WindowPrefs:
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return WindowPrefs()
        except (OSError, ValueError) as error:
            logsafe.log_error(_log, "window_prefs_unreadable", error)
            return WindowPrefs()
        if not isinstance(raw, dict):
            logsafe.log_event(_log, "window_prefs_malformed")
            return WindowPrefs()
        values = {
            field.name: raw[field.name]
            for field in fields(WindowPrefs)
            if isinstance(raw.get(field.name), bool)
        }
        return WindowPrefs(**values)

    def save(self, prefs: WindowPrefs) -> bool:
        temporary = self._path.with_suffix(".tmp")
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            temporary.write_text(json.dumps(asdict(prefs)), encoding="utf-8")
            os.replace(temporary, self._path)
        except OSError as error:
            logsafe.log_error(_log, "window_prefs_not_saved", error)
            return False
        return True
