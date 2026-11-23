"""The bundled IBM Plex Serif font (UI plan item 11, owner's decision 2026-11-24).

The four .ttf files in ui/fonts/ come unchanged from IBM's release
@ibm/plex-serif@2.0.0 on GitHub (ibm-plex-serif.zip, fonts/complete/ttf),
downloaded on 2026-11-24. They are licensed under the SIL Open Font License
1.1 (ui/fonts/OFL.txt). They are loaded from disk at start-up; nothing is
fetched from the internet.

SHA-256 of the files as downloaded:
    IBMPlexSerif-Regular.ttf   6c490ccd38e856c674e0d163f3f40742a75058b7b935998dbfbc1745ba54e474
    IBMPlexSerif-Medium.ttf    bab252b4f84ca9e409fc39410099fb481635b25628b5687524d657853ab07ed7
    IBMPlexSerif-SemiBold.ttf  857edeaaff0f3913f4dfaf6bc0e9c89ef73e475429cf63d6a3cb5d1022a213d5
    IBMPlexSerif-Bold.ttf      5fa66833dc80adb6252c793adb555716ec10833d2b1e5b0235b68c72cd69adf0
"""

import logging
from pathlib import Path

from PySide6.QtGui import QFont, QFontDatabase

FONT_DIR = Path(__file__).with_name("fonts")
FONT_FILES = (
    "IBMPlexSerif-Regular.ttf",
    "IBMPlexSerif-Medium.ttf",
    "IBMPlexSerif-SemiBold.ttf",
    "IBMPlexSerif-Bold.ttf",
)
FAMILY = "IBM Plex Serif"
# Used when the bundled files are missing or fail to load.
FALLBACK_FAMILIES = ["Georgia", "serif"]
# Body text in the popup: 13 px (the plan's type scale for a 380 px window).
BODY_PIXEL_SIZE = 13

_log = logging.getLogger(__name__)


def load_bundled_fonts() -> list[str]:
    """Register the bundled .ttf files with Qt; return the families loaded.

    A missing or broken file is logged by name and skipped, so the app
    still starts with the fallback fonts.
    """
    families: list[str] = []
    for name in FONT_FILES:
        font_id = QFontDatabase.addApplicationFont(str(FONT_DIR / name))
        if font_id < 0:
            _log.warning("bundled font not loaded: %s", name)
            continue
        for family in QFontDatabase.applicationFontFamilies(font_id):
            if family not in families:
                families.append(family)
    return families


def app_font() -> QFont:
    """The default font for the app's windows: Plex Serif, then the fallbacks."""
    font = QFont()
    font.setFamilies([FAMILY, *FALLBACK_FAMILIES])
    font.setPixelSize(BODY_PIXEL_SIZE)
    return font
