"""Contrast checks for the theme colours (UI plan item 11).

WCAG 2 asks for at least 4.5:1 for normal-size text. These numbers are
calculated, not measured on a screen.
"""

import pytest

from smartclip.ui import theme
from smartclip.ui.view_model import LABEL_BADGES, STATE_BADGES


def _luminance(colour: str) -> float:
    value = colour.lstrip("#")
    channels = [int(value[i : i + 2], 16) / 255 for i in (0, 2, 4)]
    linear = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
    return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]


def contrast(a: str, b: str) -> float:
    """WCAG contrast ratio between two #rrggbb colours."""
    high, low = sorted((_luminance(a), _luminance(b)), reverse=True)
    return (high + 0.05) / (low + 0.05)


def test_contrast_helper_matches_known_values() -> None:
    assert contrast("#000000", "#ffffff") == pytest.approx(21.0)
    assert contrast("#ffffff", "#ffffff") == pytest.approx(1.0)


@pytest.mark.parametrize("t", [theme.LIGHT, theme.DARK], ids=["light", "dark"])
def test_text_colours_pass_4_5_to_1(t: theme.Theme) -> None:
    for background in (t.card, t.window, t.card_hover, t.card_selected):
        assert contrast(t.text, background) >= 4.5
    assert contrast(t.muted, t.card) >= 4.5
    assert contrast(t.accent, t.card) >= 4.5
    assert contrast(t.accent_text, t.accent) >= 4.5
    assert contrast(t.banner_text, t.banner_background) >= 4.5


def test_white_on_dark_accent_would_fail() -> None:
    """Why the dark theme uses dark text on its accent."""
    assert contrast("#ffffff", theme.DARK.accent) < 4.5


def test_code_badge_is_not_the_accent_purple() -> None:
    code = next(b for b in LABEL_BADGES.values() if b.text == "Code")
    assert code.background.lower() not in {theme.LIGHT.accent, theme.DARK.accent}


def test_state_badges_are_readable() -> None:
    for badge in STATE_BADGES.values():
        assert contrast(badge.foreground, badge.background) >= 4.5
