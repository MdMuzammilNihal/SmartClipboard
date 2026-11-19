"""Light and dark colours for the window, following the Windows setting.

Owner's decision (2026-11-19): the window follows the Windows light/dark
setting, read each time it opens (Qt reports it through styleHints()).
The badge colours (view_model.py) are the same in both themes.
"""

from dataclasses import dataclass

from PySide6.QtCore import Qt
from PySide6.QtGui import QGuiApplication


@dataclass(frozen=True)
class Theme:
    """The colours one theme uses, as CSS colour strings."""

    name: str
    window: str
    card: str
    card_hover: str
    card_selected: str
    border: str
    accent: str
    accent_text: str
    text: str
    muted: str
    input: str
    banner_background: str
    banner_text: str


LIGHT = Theme(
    name="light",
    window="#f3f3f3",
    card="#ffffff",
    card_hover="#f5f8fc",
    card_selected="#e5f0fb",
    border="#e0e0e0",
    accent="#0f6cbd",
    accent_text="#ffffff",
    text="#1b1b1b",
    muted="#666666",
    input="#ffffff",
    banner_background="#fde7e9",
    banner_text="#a4262c",
)

DARK = Theme(
    name="dark",
    window="#202020",
    card="#2c2c2c",
    card_hover="#333333",
    card_selected="#2d3a49",
    border="#3b3b3b",
    accent="#4cc2ff",
    accent_text="#000000",
    text="#f3f3f3",
    muted="#a6a6a6",
    input="#2c2c2c",
    banner_background="#442726",
    banner_text="#ff99a4",
)


def theme_for(scheme: Qt.ColorScheme) -> Theme:
    """DARK for a dark Windows setting, LIGHT otherwise (including unknown)."""
    return DARK if scheme == Qt.ColorScheme.Dark else LIGHT


def current_theme() -> Theme:
    """The theme matching the Windows setting right now."""
    return theme_for(QGuiApplication.styleHints().colorScheme())


def stylesheet(theme: Theme) -> str:
    """The Qt style sheet for the window and its menus."""
    t = theme
    return f"""
        #panel {{ background: {t.window}; border: 1px solid {t.border}; border-radius: 8px; }}
        QLabel {{ color: {t.text}; background: transparent; }}
        #muted, #status {{ color: {t.muted}; }}
        #banner {{
            background: {t.banner_background}; color: {t.banner_text};
            border-radius: 6px; padding: 5px 8px; font-weight: bold;
        }}
        #search {{
            background: {t.input}; color: {t.text}; border: 1px solid {t.border};
            border-bottom: 2px solid {t.accent}; border-radius: 6px; padding: 6px 8px;
        }}
        QToolButton {{
            color: {t.text}; background: transparent; border: none;
            border-radius: 6px; padding: 4px; min-width: 28px; min-height: 28px;
        }}
        QToolButton:hover {{ background: {t.card_hover}; }}
        QToolButton::menu-indicator {{ image: none; width: 0px; }}
        QPushButton[chip="true"] {{
            color: {t.text}; background: {t.card}; border: 1px solid {t.border};
            border-radius: 12px; padding: 3px 10px;
        }}
        QPushButton[chip="true"]:hover {{ background: {t.card_hover}; }}
        QPushButton[chip="true"]:checked {{
            color: {t.accent_text}; background: {t.accent}; border-color: {t.accent};
        }}
        QListView, QScrollArea, #chips {{ background: transparent; border: none; }}
        QScrollBar:vertical {{ background: transparent; width: 8px; margin: 2px; }}
        QScrollBar::handle:vertical {{ background: {t.border}; border-radius: 3px; min-height: 24px; }}
        QScrollBar:horizontal {{ background: transparent; height: 6px; margin: 0px; }}
        QScrollBar::handle:horizontal {{ background: {t.border}; border-radius: 3px; }}
        QScrollBar::add-line, QScrollBar::sub-line {{ width: 0px; height: 0px; }}
        QMenu {{
            background: {t.card}; color: {t.text}; border: 1px solid {t.border};
            border-radius: 6px; padding: 4px;
        }}
        QMenu::item {{ padding: 6px 18px; border-radius: 4px; }}
        QMenu::item:selected {{ background: {t.card_selected}; }}
        QMenu::item:disabled {{ color: {t.muted}; }}
        QMenu::separator {{ height: 1px; background: {t.border}; margin: 4px 6px; }}
    """
