"""Tests for the remembered window layout (ui/prefs.py). No clipboard content involved."""

import json
from pathlib import Path

from smartclip.ui.prefs import JsonPrefs, WindowPrefs


def test_missing_file_gives_defaults(tmp_path: Path) -> None:
    assert JsonPrefs(tmp_path / "window.json").load() == WindowPrefs()


def test_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "sub" / "window.json"
    store = JsonPrefs(path)
    assert store.save(WindowPrefs(show_search=False, show_filters=True))
    assert JsonPrefs(path).load() == WindowPrefs(show_search=False, show_filters=True)
    assert json.loads(path.read_text(encoding="utf-8")) == {
        "show_search": False, "show_filters": True,
    }
    assert not path.with_suffix(".tmp").exists()


def test_malformed_file_gives_defaults(tmp_path: Path) -> None:
    path = tmp_path / "window.json"
    path.write_text("{not json", encoding="utf-8")
    assert JsonPrefs(path).load() == WindowPrefs()
    path.write_text("[1, 2]", encoding="utf-8")
    assert JsonPrefs(path).load() == WindowPrefs()


def test_values_that_are_not_true_or_false_are_ignored(tmp_path: Path) -> None:
    path = tmp_path / "window.json"
    path.write_text('{"show_search": "no", "show_filters": false, "extra": 1}', encoding="utf-8")
    assert JsonPrefs(path).load() == WindowPrefs(show_search=True, show_filters=False)


def test_save_failure_is_reported(tmp_path: Path) -> None:
    blocker = tmp_path / "file"
    blocker.write_text("x", encoding="utf-8")
    assert JsonPrefs(blocker / "window.json").save(WindowPrefs()) is False
