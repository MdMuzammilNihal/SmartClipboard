"""Light and dark colours for the window, following the Windows setting.

Owner's decision (2026-11-19): the window follows the Windows light/dark
setting, read each time it opens (Qt reports it through styleHints()).
The badge colours (view_model.py) are the same in both themes.

Owner's decision (2026-11-24, UI plan milestone M0): the palette is the
purple design system shared with the owner's Dataset Factory dashboard.
Blended colours (hover, selected, banner) are worked out ahead of time as
solid colours, because Qt style sheets handle transparency less reliably.
"""

from dataclasses import dataclass
from pathlib import Path

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
    # A little stronger than `muted`, for small text on the tinted cards and the tab bar.
    muted_strong: str
    input: str
    banner_background: str
    banner_text: str
    # Status colours, for later items (countdown pill, toasts).
    success: str
    warning: str
    danger: str
    # Amber text readable on the amber tint (the plain warning colour is too light on it).
    warning_text: str
    # Tinted card backgrounds (countdown pill, banners).
    tint1: str
    tint2: str
    tint3: str
    tint4: str
    # End colour of the accent gradient used for bars.
    accent_gradient_end: str


LIGHT = Theme(
    name="light",
    window="#f1f2f8",
    card="#ffffff",
    card_hover="#f3f2fd",
    card_selected="#eae8fc",
    border="#e7e8f1",
    accent="#6c5ce7",
    accent_text="#ffffff",
    text="#1b1d2e",
    muted="#6b7089",
    muted_strong="#575c75",
    input="#ffffff",
    # Danger at 10% over white. The plan's #ebd9dd gave only 4.0:1 with the
    # banner text (calculated in tests/test_theme.py), below 4.5:1.
    banner_background="#f9ebea",
    banner_text="#c2352b",
    success="#14894f",
    warning="#b7791f",
    danger="#c2352b",
    warning_text="#8f5d0e",
    tint1="#efeafe",
    tint2="#e3f6ee",
    tint3="#fdf0dc",
    tint4="#e4eefe",
    accent_gradient_end="#a99dff",
)

DARK = Theme(
    name="dark",
    window="#0e1017",
    card="#171a25",
    card_hover="#25273f",
    card_selected="#2d2d4c",
    border="#272b3b",
    accent="#8f83ff",
    # White on #8f83ff is about 3.1:1 and fails; dark text passes.
    accent_text="#0e1017",
    text="#e8eaf2",
    muted="#8e93aa",
    muted_strong="#9a9fb5",
    input="#171a25",
    banner_background="#392327",
    banner_text="#ff7a6e",
    success="#3ccf82",
    warning="#e0b04a",
    danger="#ff7a6e",
    warning_text="#e0b04a",
    tint1="#241f45",
    tint2="#14301f",
    tint3="#392c14",
    tint4="#16294a",
    accent_gradient_end="#a99dff",
)

# Size of a tick box's square; the box row is taller so the square is never clipped.
INDICATOR_SIZE = 16
ICON_DIR = Path(__file__).with_name("icons")

# Corner radii in pixels (UI plan item 11). Chips and badges are pills
# (badges are drawn with half their height as the radius, in window.py).
SEARCH_BOTTOM_LINE = 2  # px: the purple line under the search box
WINDOW_RADIUS = 14
CARD_RADIUS = 12
BUTTON_RADIUS = 10


def theme_for(scheme: Qt.ColorScheme) -> Theme:
    """DARK for a dark Windows setting, LIGHT otherwise (including unknown)."""
    return DARK if scheme == Qt.ColorScheme.Dark else LIGHT


def current_theme() -> Theme:
    """The theme matching the Windows setting right now."""
    return theme_for(QGuiApplication.styleHints().colorScheme())


def pill_colours(theme: Theme, level: str) -> tuple[str | None, str]:
    """(background, text) colours for the secret countdown pill (UI plan item 7).

    `level` is "normal", "warning" (amber) or "danger" (red); see
    view_model.countdown_level. The normal pill has no fill, only an outline.
    """
    if level == "danger":
        return theme.banner_background, theme.banner_text
    if level == "warning":
        return theme.tint3, theme.warning_text
    return None, theme.muted


def check_icon(theme: Theme) -> str:
    """The tick mark picture for this theme, as a style-sheet url (forward slashes)."""
    name = "check-dark.svg" if theme.name == "dark" else "check-light.svg"
    return (ICON_DIR / name).as_posix()


def stylesheet(theme: Theme) -> str:
    """The Qt style sheet for the window and its menus."""
    t = theme
    return f"""
        #panel {{
            background: {t.window}; border: 1px solid {t.border};
            border-radius: {WINDOW_RADIUS}px;
        }}
        QLabel {{ color: {t.text}; background: transparent; }}
        #muted, #status, #hint {{ color: {t.muted}; }}
        #hint {{ font-size: 11px; }}
        #banner {{
            background: {t.banner_background}; border-radius: {BUTTON_RADIUS}px;
        }}
        #banner QLabel {{ color: {t.banner_text}; font-weight: 600; }}
        #banner QPushButton {{
            color: {t.banner_text}; background: transparent;
            border: 1px solid {t.banner_text}; border-radius: 8px;
            padding: 2px 10px; font-weight: 600;
        }}
        #banner QPushButton:hover {{ background: {t.card}; }}
        QToolButton[active="true"] {{
            background: {t.banner_background}; color: {t.banner_text};
        }}
        #search {{
            background: {t.input}; color: {t.text}; border: 1px solid {t.border};
            border-bottom: {SEARCH_BOTTOM_LINE}px solid {t.accent}; border-radius: {BUTTON_RADIUS}px;
            padding: 6px 30px 6px 8px;  /* room on the right for the x */
        }}
        #searchClear {{
            min-width: 22px; min-height: 22px; max-width: 22px; max-height: 22px;
            padding: 0px; border-radius: 11px; color: {t.muted_strong};
        }}
        #searchClear:hover {{ background: {t.card_hover}; color: {t.text}; }}
        QToolButton {{
            color: {t.text}; background: transparent; border: none;
            border-radius: {BUTTON_RADIUS}px; padding: 4px; min-width: 28px; min-height: 28px;
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
        #toast {{
            background: {t.card}; border: 1px solid {t.accent};
            border-radius: {CARD_RADIUS}px;
        }}
        #toast QLabel {{ color: {t.text}; }}
        #toast QPushButton {{
            color: {t.accent}; background: transparent; border: none;
            font-weight: 600; padding: 4px 10px;
        }}
        #toast QPushButton:hover {{ background: {t.card_hover}; }}
        QPushButton#handle {{
            color: {t.muted}; background: transparent; border: none;
            border-radius: 8px; padding: 0px;
        }}
        QPushButton#handle:hover {{ background: {t.card_hover}; color: {t.text}; }}
        #segment {{ background: {t.border}; border-radius: 12px; }}
        #segment QPushButton {{
            background: transparent; color: {t.muted_strong};
            border: 1px solid transparent; border-radius: 9px; padding: 4px 18px;
            font-weight: 600;
        }}
        /* Qt drops a radius larger than half the button's height, so it stays small. */
        #segment QPushButton:checked {{
            background: {t.card}; color: {t.text}; border: 1px solid {t.card}; border-radius: 9px;
        }}
        #emptyTitle {{ color: {t.text}; font-size: 16px; font-weight: 700; }}
        #emptyBody {{ color: {t.muted_strong}; }}
        QPushButton#emptyAction {{
            color: {t.accent_text}; background: {t.accent}; border: 1px solid {t.accent};
            border-radius: 12px; padding: 5px 16px; font-weight: 600;
        }}
        QPushButton#emptyAction:hover {{ background: {t.accent_gradient_end}; border-color: {t.accent_gradient_end}; }}
        QPushButton#emptyActionPlain {{ border-radius: 12px; padding: 5px 16px; }}
        #tint1 {{ background: {t.tint1}; border-radius: {CARD_RADIUS}px; }}
        #tint2 {{ background: {t.tint2}; border-radius: {CARD_RADIUS}px; }}
        #tint3 {{ background: {t.tint3}; border-radius: {CARD_RADIUS}px; }}
        #tint4 {{ background: {t.tint4}; border-radius: {CARD_RADIUS}px; }}
        #cardTitle {{ color: {t.muted_strong}; font-size: 11px; font-weight: 600; }}
        #cardValue {{ color: {t.text}; font-size: 20px; font-weight: 700; }}
        #cardNote {{ color: {t.muted_strong}; font-size: 11px; }}
        #insightsBody {{ background: transparent; }}
        #settingsBody {{ background: transparent; }}
        QComboBox::drop-down {{ border: none; width: 22px; }}
        QSpinBox::up-button, QSpinBox::down-button {{ width: 0px; border: none; }}
        QSpinBox, QComboBox, QLineEdit {{
            background: {t.input}; color: {t.text}; border: 1px solid {t.border};
            border-radius: {BUTTON_RADIUS}px; padding: 4px 8px; min-height: 22px;
        }}
        QSpinBox:focus, QComboBox:focus, QLineEdit:focus {{ border-color: {t.accent}; }}
        QPushButton:focus, QCheckBox:focus {{ border: 1px solid {t.accent}; border-radius: {BUTTON_RADIUS}px; }}
        QComboBox QAbstractItemView {{
            background: {t.card}; color: {t.text}; border: 1px solid {t.border};
            selection-background-color: {t.card_selected}; selection-color: {t.text};
        }}
        QListWidget {{
            background: {t.input}; color: {t.text}; border: 1px solid {t.border};
            border-radius: {BUTTON_RADIUS}px; padding: 2px;
        }}
        QListWidget::item {{ padding: 3px 6px; border-radius: 6px; }}
        QListWidget::item:selected {{ background: {t.card_selected}; color: {t.text}; }}
        QCheckBox {{ color: {t.text}; spacing: 8px; min-height: 26px; }}
        QCheckBox::indicator {{
            width: {INDICATOR_SIZE}px; height: {INDICATOR_SIZE}px;
            border: 1px solid {t.muted}; border-radius: 4px; background: {t.input};
        }}
        QCheckBox::indicator:hover {{ border-color: {t.accent}; }}
        QCheckBox::indicator:checked {{
            background: {t.accent}; border-color: {t.accent}; image: url({check_icon(t)});
        }}
        QPushButton {{
            color: {t.text}; background: {t.card}; border: 1px solid {t.border};
            border-radius: {BUTTON_RADIUS}px; padding: 5px 12px;
        }}
        QPushButton:hover {{ background: {t.card_hover}; }}
        QPushButton:disabled {{ color: {t.muted}; }}
        QListView, QScrollArea, #chips {{ background: transparent; border: none; }}
        QScrollBar:vertical {{ background: transparent; width: 8px; margin: 2px; }}
        QScrollBar::handle:vertical {{ background: {t.border}; border-radius: 3px; min-height: 24px; }}
        QScrollBar:horizontal {{ background: transparent; height: 6px; margin: 0px; }}
        QScrollBar::handle:horizontal {{ background: {t.border}; border-radius: 3px; }}
        QScrollBar::add-line, QScrollBar::sub-line {{ width: 0px; height: 0px; }}
        /* Without this the track around the handle is drawn as a grey checkerboard
           on the real Windows platform (seen at 125% to 200%, 2026-11-26). */
        QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}
        QMenu {{
            background: {t.card}; color: {t.text}; border: 1px solid {t.border};
            border-radius: {BUTTON_RADIUS}px; padding: 4px;
        }}
        QMenu::item {{ padding: 6px 18px; border-radius: 6px; }}
        QMenu::item:selected {{ background: {t.card_selected}; }}
        QMenu::item:disabled {{ color: {t.muted}; }}
        QMenu::separator {{ height: 1px; background: {t.border}; margin: 4px 6px; }}
    """
