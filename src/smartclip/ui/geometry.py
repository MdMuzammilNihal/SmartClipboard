"""Small pure helpers for sizes and positions on screen (no widgets, easy to test).

Sizes in this app are in Qt's logical pixels, so they grow with the Windows
display scaling: a 380 x 520 window is 760 x 1040 real pixels at 200%. On a
1080p screen at 200% the screen is only 960 x 540 logical pixels, which is
barely taller than the window, so windows are shrunk to fit when needed.
"""

from PySide6.QtCore import QPoint, QRect, QSize

SCREEN_MARGIN = 8  # logical pixels kept free around a window that had to shrink


def fit_size(size: QSize, area: QRect, margin: int = SCREEN_MARGIN) -> QSize:
    """`size`, made smaller if needed so it fits inside `area` with `margin` to spare."""
    return QSize(
        min(size.width(), max(1, area.width() - 2 * margin)),
        min(size.height(), max(1, area.height() - 2 * margin)),
    )


def keep_on_screen(top_left: QPoint, size: QSize, area: QRect) -> QPoint:
    """Move a window of `size` at `top_left` the least needed so it lies inside `area`."""
    x = max(area.left(), min(top_left.x(), area.right() + 1 - size.width()))
    y = max(area.top(), min(top_left.y(), area.bottom() + 1 - size.height()))
    return QPoint(x, y)


def centre_over(parent: QRect, size: QSize, area: QRect) -> QPoint:
    """Top-left corner that centres a window of `size` over `parent`, kept inside `area`."""
    centre = parent.center()
    top_left = QPoint(centre.x() - size.width() // 2, centre.y() - size.height() // 2)
    return keep_on_screen(top_left, size, area)


def restore_position(
    spot: QPoint | None, size: QSize, areas: list[QRect]
) -> tuple[QPoint, QRect] | None:
    """Where a remembered window goes now, and the screen area it is on.

    `areas` are the usable areas of the screens connected right now. The
    screen that holds the centre of the window at its remembered spot is the
    one used, and the window is moved the least needed to lie fully inside it
    (the screen may be smaller, the taskbar may have moved). If no connected
    screen holds that centre (a monitor was unplugged) the answer is None and
    the caller falls back to opening next to the pointer.
    """
    if spot is None:
        return None
    centre = QPoint(spot.x() + size.width() // 2, spot.y() + size.height() // 2)
    for area in areas:
        if area.contains(centre):
            return keep_on_screen(spot, size, area), area
    return None
