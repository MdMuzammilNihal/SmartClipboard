"""The bundled IBM Plex Serif font loads from disk (UI plan item 11)."""

import hashlib
import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

from smartclip.ui import typeface  # noqa: E402

# Must match the hashes recorded in typeface.py's docstring.
EXPECTED_SHA256 = {
    "IBMPlexSerif-Regular.ttf": "6c490ccd38e856c674e0d163f3f40742a75058b7b935998dbfbc1745ba54e474",
    "IBMPlexSerif-Medium.ttf": "bab252b4f84ca9e409fc39410099fb481635b25628b5687524d657853ab07ed7",
    "IBMPlexSerif-SemiBold.ttf": "857edeaaff0f3913f4dfaf6bc0e9c89ef73e475429cf63d6a3cb5d1022a213d5",
    "IBMPlexSerif-Bold.ttf": "5fa66833dc80adb6252c793adb555716ec10833d2b1e5b0235b68c72cd69adf0",
}


@pytest.fixture(scope="module")
def qapp() -> QApplication:
    return QApplication.instance() or QApplication([])


def test_bundled_files_are_unchanged() -> None:
    assert set(typeface.FONT_FILES) == set(EXPECTED_SHA256)
    for name, expected in EXPECTED_SHA256.items():
        data = (typeface.FONT_DIR / name).read_bytes()
        assert hashlib.sha256(data).hexdigest() == expected, name


def test_licence_is_bundled() -> None:
    text = (typeface.FONT_DIR / "OFL.txt").read_text(encoding="utf-8")
    assert "SIL Open Font License" in text


def test_fonts_load_as_ibm_plex_serif(qapp: QApplication) -> None:
    assert typeface.FAMILY in typeface.load_bundled_fonts()


def test_missing_file_is_skipped_not_raised(
    qapp: QApplication, monkeypatch: pytest.MonkeyPatch, tmp_path, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(typeface, "FONT_DIR", tmp_path)
    assert typeface.load_bundled_fonts() == []
    assert "IBMPlexSerif-Regular.ttf" in caplog.text


def test_app_font_prefers_plex_with_fallbacks(qapp: QApplication) -> None:
    font = typeface.app_font()
    assert font.families() == ["IBM Plex Serif", "Georgia", "serif"]
    assert font.pixelSize() == 13
