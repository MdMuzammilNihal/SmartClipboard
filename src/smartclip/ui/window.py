"""The clipboard window (tasks 9 and 10), drawn with PySide6 in a Win+V style.

Owner's decisions (2026-11-19): a narrow panel that opens next to the mouse
pointer, one card per item, choosing an item pastes it into the app that
was in front (ui/paste.py; copy only if no Paster is given), item actions
in a card menu and on right-click, and light or dark colours following
Windows (ui/theme.py).

Layout, top to bottom: the search box (focused whenever the window opens)
with the pause and "more" buttons, a "Capture paused" banner while paused,
a row of filter chips (All, then each badge present in the history) with a
"Clear all" button, then the cards. Each card shows the type badge, the
state badge ("Secret" / "Review?"), the source app, the age, a pin mark, a
"..." menu button, and up to three lines of the item's text.

Owner's decisions (2026-11-19, second round):
- The card under the mouse, and the selected card, show buttons next to
  "...": reveal/hide, mark normal and delete on Secret and Review? cards;
  pin/unpin, mark secret and delete on normal cards.
- (Replaced 2026-11-25) A slim handle across the top drops down the search
  box, the filter chips, pause and settings with a short animation. In the
  app it starts folded; typing, Ctrl+F or choosing a filter opens it, and
  folding it clears any search or filter so nothing hidden keeps filtering.
- "Clear all" deletes the cards shown right now (the current chip and
  search), except pinned ones, after one confirmation that gives the count.

Mouse and keys:
- Click a card (or press Enter) to paste it. Up and Down move the
  selection, Esc clears the search or hides the window.
- Click a card's type or state badge, or a chip, to filter by it.
- Click a masked text to reveal it for 10 seconds (Ctrl+R does the same for
  the selected card); click it again to mask it.
- The buttons on the card under the mouse do the same as their menu items.
- The "..." button, a right-click, or Shift+F10 opens the card menu: paste,
  copy only, reveal, pin, mark normal, mark secret, delete. Ctrl+P pins or
  unpins, Shift+Del deletes (after a confirmation).

The window never touches the database or the clipboard itself. It calls a
WindowBackend (app.py), which runs on the same (UI) thread.

SECURITY REVIEW (owner):

- What is shown, masked, and searchable is decided in view_model.py; see its
  docstring. This file only draws those rows. The list model hands out the
  row's preview only, which is the bullet mask for masked items.
- Every reveal is masked again when the window hides (hideEvent), and a
  one-second timer masks reveals whose 10 seconds are up while it is open.
- Choosing a masked item pastes it without revealing it. app.py sends
  secret and unsure items through the safe copy-back (opt-out formats,
  cleared after 30 seconds). The paste itself has its own checks (paste.py).
- After a paste or copy the window hides, so a revealed secret is not left
  on screen.
- Deleting asks for confirmation; the question never quotes the item.
  Clear all asks once, giving only the number of items.
- The window hides as soon as another app takes the focus (as Win+V does).
  Its own dialogs and menus do not count as leaving.
- Tooltips are fixed hints; they never show an item's text.
- Known limit: while an item is revealed, its text is drawn on screen and
  is exposed to accessibility tools like any other window text. Masked
  items expose only the bullets.
- Not checked on the real machine yet: that the window comes to the front
  when the hotkey is pressed while another app is in front (Windows limits
  which apps may take focus), and how it looks there.
"""

from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
import time
from typing import Protocol

from PySide6.QtCore import (
    QAbstractListModel,
    QEasingCurve,
    QEvent,
    QModelIndex,
    QObject,
    QPersistentModelIndex,
    QPropertyAnimation,
    QPoint,
    QRect,
    QSize,
    Qt,
    QTimer,
    QVariantAnimation,
    Signal,
)
from PySide6.QtGui import (
    QColor,
    QCursor,
    QFont,
    QFontMetrics,
    QGuiApplication,
    QKeySequence,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPen,
    QShortcut,
    QTextLayout,
    QTextOption,
)
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QButtonGroup,
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QInputDialog,
    QLineEdit,
    QListView,
    QMenu,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QStackedWidget,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QToolButton,
    QToolTip,
    QVBoxLayout,
    QWidget,
)

from smartclip import naming
from smartclip.labels import Label, SecretState
from smartclip.storage import StoredItem
from smartclip.ui import theme as themes
from smartclip.ui.confirm import ask_confirm
from smartclip.ui.empty_view import EmptyView
from smartclip.ui.geometry import fit_size, keep_on_screen, restore_position

try:  # Insights is a local-only extra (gitignored): the app runs fine without the file.
    from smartclip.ui.insights import InsightsView, compute_insights
except ImportError:  # pragma: no cover - depends on the checkout
    InsightsView = None  # type: ignore[assignment,misc]
    compute_insights = None  # type: ignore[assignment]
from smartclip.ui.notice import Notice
from smartclip.ui.paste import Paster
from smartclip.ui.settings_dialog import SettingsPanel
from smartclip.ui.toast import Toast
from smartclip.ui.view_model import (
    LABEL_BADGES,
    QUICK_PASTE_COUNT,
    STATE_BADGES,
    ACTION_CLEAR_FILTER,
    ACTION_CLEAR_SEARCH,
    ACTION_RESUME,
    EMPTY_PAUSED,
    Badge,
    Filter,
    Header,
    Preview,
    RevealState,
    Row,
    Seg,
    TagFilter,
    arrange,
    build_rows,
    countdown_level,
    countdown_text,
    empty_state,
    filter_name,
)

WINDOW_SIZE = QSize(380, 520)
# The role under which the model hands the whole Row to the delegate.
ROW_ROLE = Qt.ItemDataRole.UserRole + 1
# The number shown on the first nine cards (Alt+1 to Alt+9 paste them).
NUMBER_ROLE = Qt.ItemDataRole.UserRole + 2
PAGE_HISTORY, PAGE_INSIGHTS = "history", "insights"
HEADER_HEIGHT = 30  # a day header: room above, then one line of small text
# Ages are redrawn this often (in seconds) while the window is open.
_AGE_REFRESH_TICKS = 30
MAX_BODY_LINES = 3
MIN_ELIDED_WIDTH = 48  # the long part of a link or path never shrinks below this
EXPANDED_BODY_LINES = 12  # a long note after "Show more"
UNDO_TEXT = "Undo"
PAUSED_TEXT = "Paused: nothing is being saved"
COPIED_TEXT = "Copied. Press Ctrl+V to paste."
CLEAR_TOOLTIP = "Delete the items shown (pinned items are kept)"
REVEAL_HINT = "Click to reveal"
HIDE_HINT = "Click to hide"

# Card geometry, in pixels.
# Cards are about 15% taller than the first version (owner's request, 2026-11-25).
_OUTER_X, _OUTER_Y, _PAD, _GAP, _BADGE_PAD_X = 8, 6, 12, 8, 7

# Icon glyphs from the Windows icon fonts (Windows 11, then Windows 10).
_ICON_FONTS = ["Segoe Fluent Icons", "Segoe MDL2 Assets"]
_GLYPH_PAUSE, _GLYPH_PLAY, _GLYPH_MORE, _GLYPH_PIN = "", "", "", ""
_GLYPH_VIEW, _GLYPH_HIDE, _GLYPH_DELETE = "", "", ""
_GLYPH_CLEAR = ""  # the x in the search box
_GLYPH_SETTINGS = ""  # the gear
_GLYPH_DOWN, _GLYPH_UP = "", ""  # the handle's chevrons
_NO_LIMIT = 16777215  # Qt's "no maximum height"
_GLYPH_COPY, _GLYPH_PASTE, _GLYPH_EDIT = "\ue8c8", "\ue77f", "\ue70f"
_GLYPH_LOCK, _GLYPH_UNLOCK = "", ""

# Regions of a card that react to a click.
REGION_CARD, REGION_LABEL, REGION_STATE, REGION_MENU, REGION_TEXT = (
    "card", "label", "state", "menu", "text",
)
# The pin button shown on the card under the mouse or selected.
REGION_REVEAL, REGION_PIN, REGION_MARK, REGION_DELETE = "reveal", "pin", "mark", "delete"
REGION_MARK_SECRET = "mark_secret"  # Review? cards only: confirm it IS a secret (REGION_MARK = normal)
# More icons of the strip that slides in when "..." is pressed.
REGION_COPY, REGION_PASTE, REGION_EDIT = "copy", "paste", "edit"
REGION_STRIP = "strip"  # the strip's own background: a click there does nothing
# The "Show more" / "Show less" link under an expandable preview.
REGION_EXPAND = "expand"
# A click on a tag chip on a card; the region text is TAG_PREFIX plus the tag.
TAG_PREFIX = "tag:"
MAX_TAG_CHIP = 110  # a tag chip is never wider than this many pixels
SHOW_MORE, SHOW_LESS = "Show more", "Show less"
MONO_FAMILIES = ["Cascadia Mono", "Consolas", "Courier New"]


class WindowBackend(Protocol):
    """What the window needs from the app. Called on the UI thread only."""

    def items(self) -> list[StoredItem]: ...
    def copy_back(self, item_id: int) -> bool: ...
    def pin(self, item_id: int) -> bool: ...
    def unpin(self, item_id: int) -> bool: ...
    def delete(self, item_id: int) -> bool: ...
    def rename(self, item_id: int, title: str | None) -> bool: ...
    def set_tags(self, item_id: int, tags: tuple[str, ...]) -> bool: ...
    def restore(self, items: list[StoredItem]) -> int: ...
    def mark_normal(self, item_id: int) -> bool: ...
    def mark_secret(self, item_id: int) -> bool: ...
    def is_paused(self) -> bool: ...
    def toggle_pause(self) -> bool: ...
    def quit(self) -> None: ...


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def clear_text(count: int, pinned_kept: int) -> str:
    """The toast after Clear all: how many went, and how many pinned stayed."""
    text = f"Cleared {count} item" + ("" if count == 1 else "s")
    return f"{text}, {pinned_kept} pinned kept" if pinned_kept else text


def partial_restore_text(restored: int, asked: int) -> str:
    """Undo could not bring everything back: some had expired or were copied again."""
    return f"Restored {restored} of {asked} (the rest expired or were copied again)"


def clear_question(count: int) -> str:
    """The Clear all question. Gives the number of items only, never their text."""
    items = "this item" if count == 1 else f"these {count} items"
    return f"Delete {items}? Pinned items are kept. This cannot be undone."


def question_box(parent: QWidget, title: str, text: str) -> QMessageBox:
    """A Yes/No question kept above the always-on-top window.

    The main window uses WindowStaysOnTopHint, so an ordinary dialog can end
    up behind it (seen on the owner's machine). The question gets the same
    hint and is raised when shown.
    """
    box = QMessageBox(
        QMessageBox.Icon.Question,
        title,
        text,
        QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        parent,
    )
    # Modality first: setting it afterwards resets the window flags (measured).
    box.setWindowModality(Qt.WindowModality.ApplicationModal)
    box.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, True)
    return box


def _ask(box: QMessageBox) -> bool:
    # Raise once the dialog is on screen, in case Windows placed it lower.
    QTimer.singleShot(0, box.raise_)
    QTimer.singleShot(0, box.activateWindow)
    return box.exec() == QMessageBox.StandardButton.Yes


def _ask_clear(parent: QWidget, count: int) -> bool:
    return ask_confirm(parent, "Clear all?", clear_question(count), "Delete")


NAMING_NOTE_SECRET = "Names and tags are not allowed on secrets"
NAMING_NOTE_SECRET_TIP = "A title could reveal what the secret is, so secrets cannot have one."
NAMING_NOTE_PIN = "Pin this item to add a name or tag"


def _ask_text(parent: QWidget, title: str, label: str, initial: str, max_length: int) -> str | None:
    """A one-line text question kept above the always-on-top window. None if cancelled."""
    dialog = QInputDialog(parent)
    dialog.setWindowTitle(title)
    dialog.setLabelText(label)
    dialog.setTextValue(initial)
    dialog.setWindowModality(Qt.WindowModality.ApplicationModal)
    dialog.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, True)
    line = dialog.findChild(QLineEdit)
    if line is not None:
        line.setMaxLength(max_length)
    QTimer.singleShot(0, dialog.raise_)
    return dialog.textValue() if dialog.exec() == QDialog.DialogCode.Accepted else None


def place_near(pointer: QPoint, size: QSize, area: QRect, offset: int = 8) -> QPoint:
    """Top-left corner for a window of `size` next to `pointer`, kept inside `area`.

    Below and to the right of the pointer when it fits, otherwise flipped
    above or to the left, and always clamped to the screen area.
    """
    x = pointer.x() + offset
    y = pointer.y() + offset
    if x + size.width() > area.right() + 1:
        x = pointer.x() - offset - size.width()
    if y + size.height() > area.bottom() + 1:
        y = pointer.y() - offset - size.height()
    x = max(area.left(), min(x, area.right() + 1 - size.width()))
    y = max(area.top(), min(y, area.bottom() + 1 - size.height()))
    return QPoint(x, y)


def wrap_lines(text: str, font: QFont, width: int, max_lines: int) -> list[str]:
    """Break `text` into at most `max_lines` lines of `width`; elide the last if cut."""
    if not text or width <= 0:
        return [text]
    metrics = QFontMetrics(font)
    layout = QTextLayout(text, font)
    option = QTextOption()
    option.setWrapMode(QTextOption.WrapMode.WrapAtWordBoundaryOrAnywhere)
    layout.setTextOption(option)
    lines: list[str] = []
    layout.beginLayout()
    while True:
        line = layout.createLine()
        if not line.isValid():
            break
        line.setLineWidth(width)
        start = line.textStart()
        if len(lines) == max_lines - 1:
            rest = text[start:]
            lines.append(metrics.elidedText(rest, Qt.TextElideMode.ElideRight, width))
            break
        lines.append(text[start : start + line.textLength()].rstrip())
    layout.endLayout()
    return lines or [text]


@dataclass(frozen=True)
class CardRects:
    """Where each part of one card is drawn, in the list's coordinates."""

    card: QRect
    label: QRect | None  # None when the state badge already says it all
    state: QRect | None
    menu: QRect
    pin: QRect | None  # the pin mark on a pinned card without buttons
    top_text: QRect  # source app and age, between the badges and the icons
    body: QRect
    text: QRect | None  # the clickable masked or revealed text
    buttons: tuple[tuple[str, QRect], ...] = ()  # (region, rect), left to right
    expand: QRect | None = None  # the "Show more" link of an expandable preview
    meta: QRect | None = None  # the line with the owner's title and tag chips
    tags: tuple[tuple[str, QRect], ...] = ()  # (tag, chip), left to right
    strip: QRect | None = None  # the sliding action strip, while it is (partly) open
    actions: tuple[tuple[str, QRect], ...] = ()  # (region, rect) of its icons, left to right


def card_buttons(row: Row) -> list[str]:
    """The buttons an active card shows before the "..." button: only Pin.

    Secret and Review? cards cannot be pinned, so they show "..." alone. The
    other actions are in the strip that slides in from "...".
    """
    return [REGION_PIN] if row.can_pin else []


def action_regions(row: Row, paste: bool) -> list[str]:
    """The icons of the sliding strip, left to right (owner's design, 2026-11-25).

    Reveal only for secret and Review? items; Edit (name and tags) only for
    pinned normal items; Paste only when pasting is available.
    """
    names: list[str] = []
    if row.state is not SecretState.NORMAL:
        names.append(REGION_REVEAL)
    names.append(REGION_COPY)
    if paste:
        names.append(REGION_PASTE)
    names.append(REGION_MARK)
    if row.state is SecretState.UNSURE:
        names.append(REGION_MARK_SECRET)  # a Review? card can be settled either way
    if row.can_name:
        names.append(REGION_EDIT)
    names.append(REGION_DELETE)
    return names


ACTION_GAP, ACTION_PAD = 4, 6
STRIP_ROWS = 2  # the icons sit in a grid of two rows (owner's design, 2026-11-26)


def strip_columns(count: int) -> int:
    """How many columns the grid of `count` icons needs: 2 icons per column."""
    return max(1, -(-count // STRIP_ROWS))


def strip_width(columns: int, size: int) -> int:
    """The width of the fully open strip for `columns` columns of `size`-pixel icons."""
    return ACTION_PAD + columns * (size + ACTION_GAP) + ACTION_PAD - ACTION_GAP


def button_size(metrics: QFontMetrics) -> int:
    """The side of the "..." and Pin buttons (a little larger than the badges, 2026-11-26)."""
    return metrics.height() + 8


def pin_column(row: Row, metrics: QFontMetrics) -> int:
    """Width kept free at the right of the body for the Pin button under "...".

    Kept for every card that can be pinned or is pinned, hovered or not, so the
    text does not re-wrap when the mouse moves over a card.
    """
    return button_size(metrics) + 6 if (row.can_pin or row.pinned) else 0


MIN_SOURCE_WIDTH = 36  # the top row keeps at least this much for the source app


def top_row_extras(
    row: Row, number: object, active: bool, metrics: QFontMetrics, room: int
) -> tuple[str | None, bool]:
    """What fits in the top row besides the source app: (pill text, show key-cap).

    The countdown pill matters more than the quick-paste key-cap. When space
    is short the pill loses its word "left", then the key-cap goes. An active
    card shows buttons instead of the key-cap but keeps its pill, so a secret
    just marked (still selected) visibly starts its countdown. `room` is the
    width left after the age.
    """
    if active:
        number = None
    cap = metrics.height() + 8 if number is not None else 0  # same as the key-cap drawn below

    def fits(pill: str | None, key_cap: int) -> bool:
        pill_room = metrics.horizontalAdvance(pill) + 14 + 6 if pill else 0
        return room - pill_room - key_cap >= MIN_SOURCE_WIDTH

    if row.remaining is None:
        return None, number is not None and fits(None, cap)
    full = countdown_text(row.remaining)
    short = full.removesuffix(" left")
    for pill, key_cap in ((full, cap), (short, cap), (short, 0)):
        if fits(pill, key_cap):
            return pill, key_cap > 0
    return short, False


def _badge_width(badge: Badge, metrics: QFontMetrics) -> int:
    return metrics.horizontalAdvance(badge.text) + 2 * _BADGE_PAD_X


def has_meta(row: Row) -> bool:
    """True if the card shows a line for the owner's title and tags."""
    return not row.masked and bool(row.title or row.tags)


def tag_chip_rects(row: Row, metrics: QFontMetrics, meta: QRect) -> tuple[tuple[str, QRect], ...]:
    """Where each tag chip goes: right-aligned on the meta line, the title keeps room to its left."""
    chips: list[tuple[str, int]] = []
    for tag in row.tags:
        chips.append((tag, min(MAX_TAG_CHIP, metrics.horizontalAdvance(tag) + 16)))
    min_title = 60 if row.title else 0
    while chips and meta.width() - sum(w + 6 for _t, w in chips) < min_title:
        chips.pop()  # too many chips for this width: drop the last ones
    x = meta.right() + 1 - sum(w + 6 for _t, w in chips) + 6
    placed: list[tuple[str, QRect]] = []
    for tag, width in chips:
        placed.append((tag, QRect(x, meta.top(), width, meta.height())))
        x += width + 6
    return tuple(placed)


def card_rects(
    rect: QRect, row: Row, metrics: QFontMetrics, active: bool = False,
    slide: float = 0.0, paste: bool = True,
) -> CardRects:
    """Lay out one card inside the list cell `rect`.

    `active` (the card under the mouse, or the selected one) adds the Pin
    button; the source app and age then get less room. `slide` (0 to 1) is
    how far the action strip has slid in from the right over the card.
    """
    card = rect.adjusted(_OUTER_X, _OUTER_Y, -_OUTER_X, -_OUTER_Y)
    inner = card.adjusted(_PAD, _PAD, -_PAD, -_PAD)
    badge_height = metrics.height() + 4
    label = None
    right_of_badges = inner.left() - 5
    if show_label_badge(row):
        label = QRect(inner.left(), inner.top(), _badge_width(row.label_badge, metrics), badge_height)
        right_of_badges = label.right()
    state = None
    if row.state_badge is not None:
        state = QRect(
            right_of_badges + 5, inner.top(), _badge_width(row.state_badge, metrics), badge_height
        )
        right_of_badges = state.right()
    side = button_size(metrics)
    # "..." is centred on the badge row; Pin sits directly below it.
    menu = QRect(inner.right() - side + 1, inner.top() - (side - badge_height) // 2, side, side)
    pin = None
    buttons: list[tuple[str, QRect]] = []
    below = QRect(menu.left(), menu.bottom() + 3, side, side)
    if active:
        for region in card_buttons(row):
            buttons.append((region, QRect(below)))
            below.translate(0, side + 2)
    elif row.pinned:
        pin = below
    right_edge = menu.left() - 2
    top_text = QRect(
        right_of_badges + 8, inner.top(), max(0, right_edge - right_of_badges - 8), badge_height
    )
    body = QRect(
        inner.left(),
        inner.top() + badge_height + _GAP,
        inner.width(),
        max(0, inner.bottom() - inner.top() - badge_height - _GAP + 1),
    ).adjusted(0, 0, -pin_column(row, metrics), 0)
    meta = None
    tag_rects: tuple[tuple[str, QRect], ...] = ()
    if has_meta(row):
        # The owner's title and tag chips take a line of their own above the body.
        line = metrics.lineSpacing()
        meta = QRect(body.left(), body.top(), body.width(), line)
        tag_rects = tag_chip_rects(row, metrics, meta)
        body = QRect(body.left(), body.top() + line + _GAP, body.width(), max(0, body.height() - line - _GAP))
    text = None
    expand = None
    if row.masked:
        hint_width = metrics.horizontalAdvance(f"{row.preview}   {REVEAL_HINT}")
        text = QRect(body.left(), body.top(), min(hint_width, body.width()), metrics.lineSpacing())
    elif row.revealed:
        text = body
    elif row.rich is not None and row.rich.expandable:
        # The toggle takes the last line of the body, under the preview.
        line = metrics.lineSpacing()
        link = SHOW_LESS if row.rich.expanded else SHOW_MORE
        expand = QRect(
            body.left(), body.bottom() - line + 1, metrics.horizontalAdvance(link) + 4, line
        )
        body = QRect(body.left(), body.top(), body.width(), max(0, body.height() - line))
    strip = None
    icons: list[tuple[str, QRect]] = []
    if slide > 0:
        size = side + 2
        regions = action_regions(row, paste)
        columns = strip_columns(len(regions))
        width = round(strip_width(columns, size) * min(1.0, slide))
        strip = QRect(card.right() + 1 - width, card.top(), width, card.height())
        grid_height = STRIP_ROWS * size + (STRIP_ROWS - 1) * ACTION_GAP
        top = card.top() + (card.height() - grid_height) // 2
        for index, region in enumerate(regions):
            line, column = divmod(index, columns)
            x = strip.left() + ACTION_PAD + column * (size + ACTION_GAP)
            y = top + line * (size + ACTION_GAP)
            icons.append((region, QRect(x, y, size, size)))
    return CardRects(
        card, label, state, menu, pin, top_text, body, text, tuple(buttons), expand, meta,
        tag_rects, strip, tuple(icons),
    )


def show_label_badge(row: Row) -> bool:
    """False when the state badge already says it all.

    A Secret card shows only "Secret", whatever the item was before (a link
    marked secret is no longer shown as "Link": owner's report, 2026-11-26).
    A "possible secret" label next to Review? is the same news too. A Review?
    card keeps its type badge, which helps to judge it.
    """
    if row.state is SecretState.SECRET:
        return False
    return not (row.label is Label.POSSIBLE_SECRET and row.state_badge is not None)


def region_at(rects: CardRects, point: QPoint) -> str:
    """Which part of a card a click at `point` landed on."""
    if rects.strip is not None and rects.strip.contains(point):
        for region, icon in rects.actions:
            if icon.contains(point):
                return region
        return REGION_STRIP  # covers whatever lies under the strip
    if rects.menu.contains(point):
        return REGION_MENU
    for region, button in rects.buttons:
        if button.contains(point):
            return region
    for tag, chip in rects.tags:
        if chip.contains(point):
            return TAG_PREFIX + tag
    if rects.expand is not None and rects.expand.contains(point):
        return REGION_EXPAND
    if rects.label is not None and rects.label.contains(point):
        return REGION_LABEL
    if rects.state is not None and rects.state.contains(point):
        return REGION_STATE
    if rects.text is not None and rects.text.contains(point):
        return REGION_TEXT
    return REGION_CARD


def fit_segments(segments: tuple[Seg, ...], fonts: list[QFont], room: int) -> list[int]:
    """The width each piece of a line gets, so that the line fits in `room` pixels.

    Pieces keep their natural width when everything fits. Otherwise the
    first piece marked "middle" (the folder of a path, the tail of a link)
    shrinks first, down to a small minimum; whatever is still too wide is
    taken from the last piece. A bold file name therefore survives a long folder.
    """
    natural = [QFontMetrics(font).horizontalAdvance(seg.text) + 2 for seg, font in zip(segments, fonts)]
    excess = sum(natural) - room
    widths = list(natural)
    if excess > 0:
        for index, seg in enumerate(segments):
            if seg.elide == "middle":
                shrunk = max(MIN_ELIDED_WIDTH, widths[index] - excess)
                excess -= widths[index] - shrunk
                widths[index] = shrunk
                break
    if excess > 0 and widths:
        widths[-1] = max(0, widths[-1] - excess)
    return widths


def mono_font(base: QFont) -> QFont:
    """The code font for previews, one pixel smaller than the text font."""
    font = QFont(base)
    font.setFamilies(MONO_FAMILIES)
    font.setStyleHint(QFont.StyleHint.Monospace)
    if base.pixelSize() > 0:
        font.setPixelSize(max(8, base.pixelSize() - 1))
    return font


def body_line_count(row: Row, font: QFont, width: int) -> int:
    """How many lines the body of this card takes at this width (not counting "Show more")."""
    if row.masked:
        return 1
    rich = row.rich
    if rich is None:
        return len(wrap_lines(row.preview, font, width, MAX_BODY_LINES))
    if rich.wrap:
        limit = EXPANDED_BODY_LINES if rich.expanded else MAX_BODY_LINES
        return len(wrap_lines(rich.lines[0][0].text, font, width, limit))
    return len(rich.lines)


def card_height(row: Row, font: QFont, width: int) -> int:
    """The height of one list cell holding this row's card."""
    metrics = QFontMetrics(font)
    body_width = width - 2 * _OUTER_X - 2 * _PAD - pin_column(row, metrics)
    lines = body_line_count(row, font, body_width)
    if not row.masked and row.rich is not None and row.rich.expandable:
        lines += 1  # the "Show more" link
    badge_height = metrics.height() + 4
    meta = metrics.lineSpacing() + _GAP if has_meta(row) else 0
    height = 2 * _OUTER_Y + 2 * _PAD + badge_height + _GAP + meta + lines * metrics.lineSpacing()
    if pin_column(row, metrics):  # room for "..." with Pin stacked under it
        height = max(height, 2 * _OUTER_Y + 2 * _PAD + 2 * button_size(metrics) + 2)
    return height


class EdgeFade(QWidget):
    """A soft fade over one edge of the filter chips, shown when chips are cut off there.

    It is drawn in the window colour, ignores the mouse, and is only shown
    while there is more to scroll to on its side (UI plan item 8, chip row).
    """

    WIDTH = 28

    def __init__(self, parent: QWidget, right: bool) -> None:
        super().__init__(parent)
        self._right = right
        self._colour = QColor("#ffffff")
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.setFixedWidth(self.WIDTH)
        self.hide()

    def set_colour(self, colour: str) -> None:
        self._colour = QColor(colour)
        self.update()

    def paintEvent(self, event: QEvent) -> None:  # noqa: N802 (Qt name)
        painter = QPainter(self)
        solid, clear = QColor(self._colour), QColor(self._colour)
        clear.setAlpha(0)
        gradient = QLinearGradient(0, 0, self.width(), 0)
        gradient.setColorAt(0.0, clear if self._right else solid)
        gradient.setColorAt(1.0, solid if self._right else clear)
        painter.fillRect(self.rect(), gradient)


class CardsModel(QAbstractListModel):
    """Holds what the list view shows: cards (Row) and day headers (Header).

    `rows` is only the cards, in the order shown. Qt positions (`index(n)`,
    `row_at(n)`) count the headers too; use `position_of` to turn an item id
    into one. A header cannot be selected, so the arrow keys and Enter never
    land on it.
    """

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._entries: list[Row | Header] = []
        self._rows: list[Row] = []
        self._numbers: dict[int, int] = {}

    @property
    def rows(self) -> list[Row]:
        return self._rows

    @property
    def entries(self) -> list[Row | Header]:
        return self._entries

    def set_rows(self, entries: list[Row | Header]) -> None:
        self.beginResetModel()
        self._entries = entries
        self._rows = [entry for entry in entries if isinstance(entry, Row)]
        # Quick-paste numbers go to the first nine cards; headers are not counted.
        self._numbers = {
            position: number
            for number, position in enumerate(self.card_positions()[:QUICK_PASTE_COUNT], start=1)
        }
        self.endResetModel()

    def row_at(self, index: int) -> Row | None:
        """The card at a Qt position, or None for a header or a bad position."""
        entry = self._entries[index] if 0 <= index < len(self._entries) else None
        return entry if isinstance(entry, Row) else None

    def position_of(self, item_id: int) -> int | None:
        """The Qt position of the card for `item_id`, or None."""
        for position, entry in enumerate(self._entries):
            if isinstance(entry, Row) and entry.item_id == item_id:
                return position
        return None

    def card_positions(self) -> list[int]:
        """Qt positions of the cards only (the places the selection may go)."""
        return [i for i, entry in enumerate(self._entries) if isinstance(entry, Row)]

    def rowCount(self, parent: QModelIndex | QPersistentModelIndex = QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self._entries)

    def flags(self, index: QModelIndex | QPersistentModelIndex) -> Qt.ItemFlag:  # noqa: N802
        entry = self._entries[index.row()] if 0 <= index.row() < len(self._entries) else None
        if isinstance(entry, Header):
            return Qt.ItemFlag.NoItemFlags  # drawn, but cannot be selected or made current
        return super().flags(index)

    def data(
        self, index: QModelIndex | QPersistentModelIndex, role: int = Qt.ItemDataRole.DisplayRole
    ) -> object:
        entry = self._entries[index.row()] if 0 <= index.row() < len(self._entries) else None
        if entry is None:
            return None
        if role == ROW_ROLE:
            return entry
        if role == NUMBER_ROLE:
            return self._numbers.get(index.row())
        if role == Qt.ItemDataRole.AccessibleTextRole:
            return entry.text if isinstance(entry, Header) else card_accessible_text(entry)
        if role == Qt.ItemDataRole.DisplayRole:
            # Headers show their fixed word. For cards: the bullet mask for
            # masked rows; the text itself never leaves here.
            return entry.text if isinstance(entry, Header) else entry.preview
        return None


class CardDelegate(QStyledItemDelegate):
    """Draws a Row as a card and reports which part of it was clicked."""

    region_clicked = Signal(int, str, QPoint)  # list row, region, global position

    def __init__(self, view: QListView) -> None:
        super().__init__(view)
        self._view = view
        self.theme = themes.LIGHT
        self.icon_font = QFont()
        self.icon_font.setFamilies(_ICON_FONTS)
        self.icon_font.setPixelSize(16)  # a little larger than before (2026-11-26)
        self.paste_enabled = True  # the window turns it off when it cannot paste
        self.animation_ms = 170  # tests set 0 so the strip jumps instead of sliding
        self._slides: dict[int, float] = {}
        self._actions_item: int | None = None
        self._opening = False
        self._animation = QVariantAnimation(self)
        self._animation.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._animation.valueChanged.connect(self._on_slide)
        self._animation.finished.connect(self._on_slide_finished)

    # --- The sliding action strip (one card at a time) ---

    @property
    def actions_item(self) -> int | None:
        """The item whose strip is open or sliding, or None."""
        return self._actions_item

    def is_open(self, item_id: int) -> bool:
        return self._actions_item == item_id and self._opening

    def slide_for(self, item_id: int) -> float:
        return self._slides.get(item_id, 0.0)

    def open_actions(self, item_id: int) -> None:
        if self._actions_item not in (None, item_id):
            self._slides.pop(self._actions_item, None)
        self._actions_item, self._opening = item_id, True
        self._run(self.slide_for(item_id), 1.0)

    def close_actions(self, animate: bool = True) -> None:
        item = self._actions_item
        if item is None:
            return
        self._opening = False
        if animate and self.animation_ms > 0:
            self._run(self.slide_for(item), 0.0)
        else:
            self._animation.stop()
            self._slides.pop(item, None)
            self._actions_item = None
            self._view.viewport().update()

    def _run(self, start: float, end: float) -> None:
        self._animation.stop()
        if self.animation_ms <= 0:
            self._set_slide(end)
            self._on_slide_finished()
            return
        self._animation.setDuration(self.animation_ms)
        self._animation.setStartValue(float(start))
        self._animation.setEndValue(float(end))
        self._animation.start()

    def _set_slide(self, value: object) -> None:
        if self._actions_item is not None:
            self._slides[self._actions_item] = float(value)  # type: ignore[arg-type]
            self._view.viewport().update()

    def _on_slide(self, value: object) -> None:
        self._set_slide(value)

    def _on_slide_finished(self) -> None:
        item = self._actions_item
        if item is not None and not self._opening and self.slide_for(item) <= 0.0:
            self._slides.pop(item, None)
            self._actions_item = None
        self._view.viewport().update()

    def sizeHint(  # noqa: N802 (Qt name)
        self, option: QStyleOptionViewItem, index: QModelIndex | QPersistentModelIndex
    ) -> QSize:
        row = index.data(ROW_ROLE)
        width = self._view.viewport().width()
        if isinstance(row, Header):
            return QSize(width, HEADER_HEIGHT)
        if not isinstance(row, Row):
            return QSize(width, 0)
        return QSize(width, card_height(row, option.font, width))

    def paint(
        self,
        painter: QPainter,
        option: QStyleOptionViewItem,
        index: QModelIndex | QPersistentModelIndex,
    ) -> None:
        row = index.data(ROW_ROLE)
        if isinstance(row, Header):
            self._paint_header(painter, option, row)
            return
        if not isinstance(row, Row):
            return
        t = self.theme
        metrics = option.fontMetrics
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        hovered = bool(option.state & QStyle.StateFlag.State_MouseOver)
        slide = self.slide_for(row.item_id)
        rects = card_rects(
            option.rect, row, metrics, active=selected or hovered, slide=slide, paste=self.paste_enabled
        )
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        background = t.card_selected if selected else t.card_hover if hovered else t.card
        painter.setBrush(QColor(background))
        painter.setPen(QPen(QColor(t.accent if selected else t.border), 1))
        radius = themes.CARD_RADIUS
        painter.drawRoundedRect(rects.card.adjusted(0, 0, -1, -1), radius, radius)

        if rects.label is not None:
            self._badge(painter, rects.label, row.label_badge)
        if rects.state is not None and row.state_badge is not None:
            self._badge(painter, rects.state, row.state_badge)

        painter.setPen(QColor(t.muted))
        age_width = metrics.horizontalAdvance(row.age) + 4
        number = index.data(NUMBER_ROLE)
        keycap_size = rects.top_text.height() - 4
        pill_text, numbered = top_row_extras(
            row, number, bool(rects.buttons), metrics, rects.top_text.width() - age_width
        )
        number_width = keycap_size + 8 if numbered else 0
        pill_width = metrics.horizontalAdvance(pill_text) + 14 if pill_text else 0
        pill_gap = 6 if pill_text else 0
        source_rect = rects.top_text.adjusted(
            0, 0, -(age_width + number_width + pill_width + pill_gap), 0
        )
        source = metrics.elidedText(row.source_app, Qt.TextElideMode.ElideRight, source_rect.width())
        align = Qt.AlignmentFlag.AlignVCenter
        painter.drawText(source_rect, align | Qt.AlignmentFlag.AlignLeft, source)
        painter.drawText(rects.top_text, align | Qt.AlignmentFlag.AlignRight, row.age)
        if pill_text and row.remaining is not None:
            pill = QRect(
                rects.top_text.right() - age_width - number_width - pill_width,
                rects.top_text.top() + 2,
                pill_width,
                keycap_size,
            )
            fill, ink = themes.pill_colours(t, countdown_level(row.remaining))
            painter.setPen(QPen(QColor(fill or t.border), 1))
            painter.setBrush(QColor(fill) if fill else Qt.BrushStyle.NoBrush)
            painter.drawRoundedRect(pill, pill.height() / 2, pill.height() / 2)
            painter.setPen(QColor(ink))
            painter.drawText(pill, Qt.AlignmentFlag.AlignCenter, pill_text)
            painter.setPen(QColor(t.muted))
        if numbered:
            keycap = QRect(
                rects.top_text.right() - age_width - keycap_size - 4,
                rects.top_text.top() + 2,
                keycap_size,
                keycap_size,
            )
            painter.setOpacity(0.7)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setPen(QPen(QColor(t.border), 1))
            painter.drawRoundedRect(keycap, 4, 4)
            painter.setPen(QColor(t.muted))
            painter.drawText(keycap, Qt.AlignmentFlag.AlignCenter, str(number))
            painter.setOpacity(1.0)
            painter.setPen(QColor(t.muted))

        painter.setFont(self.icon_font)
        center = Qt.AlignmentFlag.AlignCenter
        if rects.pin is not None:
            painter.setPen(QColor(t.accent))
            painter.drawText(rects.pin, center, _GLYPH_PIN)
        for region, button in rects.buttons:
            glyph, on = button_glyph(row, region)
            painter.setPen(QColor(t.accent if on else t.muted))
            painter.drawText(button, center, glyph)
        painter.setPen(QColor(t.muted))
        painter.drawText(rects.menu, center, _GLYPH_MORE)
        painter.setFont(option.font)

        if rects.meta is not None:
            self._paint_meta(painter, option, row, rects)
        top_left = Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop
        if row.masked:
            painter.setPen(QColor(t.text))
            painter.drawText(rects.body, top_left, row.preview)
            painter.setPen(QColor(t.muted))
            hint_x = metrics.horizontalAdvance(row.preview + "   ")
            painter.drawText(rects.body.adjusted(hint_x, 0, 0, 0), top_left, REVEAL_HINT)
        elif row.rich is not None:
            self._paint_preview(painter, option, row, rects)
        else:
            painter.setPen(QColor(t.text))
            lines = wrap_lines(row.preview, option.font, rects.body.width(), MAX_BODY_LINES)
            y = rects.body.top()
            for line in lines:
                line_rect = QRect(rects.body.left(), y, rects.body.width(), metrics.lineSpacing())
                painter.drawText(line_rect, top_left, line)
                y += metrics.lineSpacing()
        if rects.strip is not None:
            self._paint_strip(painter, row, rects, slide, selected)
        painter.restore()

    def _paint_strip(
        self, painter: QPainter, row: Row, rects: CardRects, slide: float, selected: bool
    ) -> None:
        """The action icons sliding in over the right side of the card."""
        t = self.theme
        radius = themes.CARD_RADIUS
        path = QPainterPath()
        path.addRoundedRect(rects.card.adjusted(0, 0, -1, -1), radius, radius)
        painter.setClipPath(path)
        strip = rects.strip
        if strip is None:
            return
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(t.card_selected if selected else t.card_hover))
        painter.drawRect(strip.adjusted(0, -1, 2, 1))
        painter.setPen(QPen(QColor(t.border), 1))  # a thin divider on its left edge only
        painter.drawLine(strip.left(), strip.top() + 6, strip.left(), strip.bottom() - 6)
        painter.setFont(self.icon_font)
        painter.setOpacity(min(1.0, slide * 1.6))
        for region, icon in rects.actions:
            glyph, on = button_glyph(row, region)
            ink = t.danger if region == REGION_DELETE else t.accent if on else t.text
            painter.setPen(QColor(ink))
            painter.drawText(icon, Qt.AlignmentFlag.AlignCenter, glyph)
        painter.setOpacity(1.0)

    def _paint_meta(
        self, painter: QPainter, option: QStyleOptionViewItem, row: Row, rects: CardRects
    ) -> None:
        """The owner's title (bold) and tag chips above the text (UI plan item 9)."""
        t = self.theme
        meta = rects.meta
        if meta is None:
            return
        if row.title:
            title_font = QFont(option.font)
            title_font.setWeight(QFont.Weight.DemiBold)
            right = rects.tags[0][1].left() - 8 if rects.tags else meta.right()
            room = max(0, right - meta.left() + 1)
            painter.setFont(title_font)
            painter.setPen(QColor(t.text))
            shown = QFontMetrics(title_font).elidedText(row.title, Qt.TextElideMode.ElideRight, room)
            painter.drawText(
                QRect(meta.left(), meta.top(), room, meta.height()),
                Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                shown,
            )
            painter.setFont(option.font)
        # Chips are card-coloured with an outline, so they show on a selected card too,
        # and the accent text on them keeps at least 4.5:1 contrast (tests/test_theme.py).
        for tag, chip in rects.tags:
            painter.setBrush(QColor(t.card))
            painter.setPen(QPen(QColor(t.border), 1))
            painter.drawRoundedRect(chip, chip.height() / 2, chip.height() / 2)
            painter.setPen(QColor(t.accent))
            text = QFontMetrics(option.font).elidedText(tag, Qt.TextElideMode.ElideRight, chip.width() - 12)
            painter.drawText(chip, Qt.AlignmentFlag.AlignCenter, text)
        painter.setPen(QColor(t.muted))

    def _paint_preview(
        self, painter: QPainter, option: QStyleOptionViewItem, row: Row, rects: CardRects
    ) -> None:
        """Draw the body of a normal card by its Preview (UI plan item 8)."""
        t = self.theme
        rich = row.rich
        if rich is None:
            return
        metrics = option.fontMetrics
        line_height = metrics.lineSpacing()
        body = rects.body
        top_left = Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop
        if rich.wrap:
            limit = EXPANDED_BODY_LINES if rich.expanded else MAX_BODY_LINES
            painter.setPen(QColor(t.text))
            y = body.top()
            for line in wrap_lines(rich.lines[0][0].text, option.font, body.width(), limit):
                painter.drawText(QRect(body.left(), y, body.width(), line_height), top_left, line)
                y += line_height
        else:
            y = body.top()
            for segments in rich.lines:
                self._paint_line(
                    painter, option.font, segments, rich, QRect(body.left(), y, body.width(), line_height)
                )
                y += line_height
        if rects.expand is not None:
            painter.setFont(option.font)
            painter.setPen(QColor(t.accent))
            link = SHOW_LESS if rich.expanded else SHOW_MORE
            painter.drawText(rects.expand, Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, link)

    def _paint_line(
        self, painter: QPainter, base: QFont, segments: tuple[Seg, ...], rich: Preview, rect: QRect
    ) -> None:
        """One preview line: an optional swatch, its pieces cut to fit, an optional chip."""
        t = self.theme
        left, right = rect.left(), rect.right()
        if rich.swatch:
            size = min(rect.height() - 4, 14)
            swatch = QRect(left, rect.top() + (rect.height() - size) // 2, size, size)
            painter.setPen(QPen(QColor(t.border), 1))
            painter.setBrush(QColor(rich.swatch))
            painter.drawRoundedRect(swatch, 3, 3)
            left = swatch.right() + 8
        chip_rect = None
        if rich.chip:
            chip_font = QFont(base)
            chip_font.setPixelSize(11)
            chip_width = QFontMetrics(chip_font).horizontalAdvance(rich.chip) + 12
            chip_rect = QRect(right - chip_width + 1, rect.top() + 2, chip_width, rect.height() - 4)
            right = chip_rect.left() - 6
        fonts = [self._segment_font(base, seg) for seg in segments]
        widths = fit_segments(segments, fonts, max(0, right - left + 1))
        x = left
        for seg, font, width in zip(segments, fonts, widths):
            painter.setFont(font)
            painter.setPen(QColor(t.muted if seg.style == "muted" else t.text))
            mode = Qt.TextElideMode.ElideMiddle if seg.elide == "middle" else Qt.TextElideMode.ElideRight
            text = QFontMetrics(font).elidedText(seg.text, mode, width)
            painter.drawText(
                QRect(x, rect.top(), width, rect.height()),
                Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                text,
            )
            x += width
        if chip_rect is not None and rich.chip:
            chip_font = QFont(base)
            chip_font.setPixelSize(11)
            painter.setFont(chip_font)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setPen(QPen(QColor(t.border), 1))
            painter.drawRoundedRect(chip_rect, chip_rect.height() / 2, chip_rect.height() / 2)
            painter.setPen(QColor(t.muted))
            painter.drawText(chip_rect, Qt.AlignmentFlag.AlignCenter, rich.chip)
        painter.setFont(base)

    @staticmethod
    def _segment_font(base: QFont, seg: Seg) -> QFont:
        if seg.style == "mono":
            return mono_font(base)
        font = QFont(base)
        if seg.style == "bold":
            font.setWeight(QFont.Weight.DemiBold)
        return font

    def _paint_header(self, painter: QPainter, option: QStyleOptionViewItem, header: Header) -> None:
        """A small muted capitals label, like the dashboard's "MENU" (UI plan item 2)."""
        painter.save()
        font = QFont(option.font)
        font.setPixelSize(11)
        font.setWeight(QFont.Weight.DemiBold)
        font.setLetterSpacing(QFont.SpacingType.PercentageSpacing, 110)
        painter.setFont(font)
        painter.setPen(QColor(self.theme.muted))
        rect = option.rect.adjusted(6, 8, 0, 0)  # room above, aligned with the card text
        painter.drawText(rect, Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, header.text)
        painter.restore()

    def _badge(self, painter: QPainter, rect: QRect, badge: Badge) -> None:
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(badge.background))
        pill = rect.height() / 2  # fully round ends (UI plan item 11)
        painter.drawRoundedRect(rect, pill, pill)
        painter.setPen(QColor(badge.foreground))
        painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, badge.text)

    def editorEvent(  # noqa: N802 (Qt name)
        self,
        event: QEvent,
        model: QAbstractListModel,
        option: QStyleOptionViewItem,
        index: QModelIndex | QPersistentModelIndex,
    ) -> bool:
        if (
            event.type() == QEvent.Type.MouseButtonRelease
            and event.button() == Qt.MouseButton.LeftButton  # type: ignore[attr-defined]
        ):
            row = index.data(ROW_ROLE)
            if isinstance(row, Row):
                point = event.position().toPoint()  # type: ignore[attr-defined]
                # A click lands on the card under the mouse, which shows its buttons.
                rects = card_rects(
                    option.rect, row, option.fontMetrics, active=True,
                    slide=self.slide_for(row.item_id), paste=self.paste_enabled,
                )
                region = region_at(rects, point)
                self.region_clicked.emit(
                    index.row(), region, self._view.viewport().mapToGlobal(point)
                )
                return True
        return super().editorEvent(event, model, option, index)

    def helpEvent(  # noqa: N802 (Qt name)
        self, event: QEvent, view: QAbstractItemView, option: QStyleOptionViewItem,
        index: QModelIndex | QPersistentModelIndex,
    ) -> bool:
        row = index.data(ROW_ROLE)
        if not isinstance(row, Row):
            return False
        point = event.pos()  # type: ignore[attr-defined]
        rects = card_rects(
            option.rect, row, option.fontMetrics, active=True,
            slide=self.slide_for(row.item_id), paste=self.paste_enabled,
        )
        tip = card_tooltip(row, region_at(rects, point))
        if tip:
            QToolTip.showText(event.globalPos(), tip, view)  # type: ignore[attr-defined]
        else:
            QToolTip.hideText()
        return True


def card_accessible_text(row: Row) -> str:
    """What a screen reader says for one card (owner's accessibility pass, 2026-11-26).

    Type, state, the text, source app, age, and the time left for a secret.
    A masked secret is announced as "hidden", never as its text or its mask;
    a revealed one reads out the text the window is showing at that moment.
    """
    parts = [row.label_badge.text if show_label_badge(row) else ""]
    if row.state_badge is not None:
        parts.append(row.state_badge.text)
    if row.title and not row.masked:
        parts.append(row.title)
    parts.append("text hidden" if row.masked else row.preview[:200])
    if row.pinned:
        parts.append("pinned")
    parts += [f"from {row.source_app}" if row.source_app else "", row.age]
    if row.remaining is not None:
        parts.append(countdown_text(row.remaining))
    return ", ".join(part for part in parts if part)


def card_tooltip(row: Row, region: str) -> str:
    """The fixed hint for a part of a card. Never the item's text."""
    if region == REGION_LABEL:
        return f"Show only {row.label_badge.text} items"
    if region == REGION_STATE and row.state_badge is not None:
        return f"Show only {row.state_badge.text} items"
    if region == REGION_MENU:
        return "More actions"
    if region == REGION_TEXT:
        return HIDE_HINT if row.revealed else "Click to reveal for 10 seconds"
    if region.startswith(TAG_PREFIX):
        return f"Show only items tagged {region[len(TAG_PREFIX):]}"
    if region == REGION_EXPAND:
        return "Show less" if row.rich is not None and row.rich.expanded else "Show the whole item"
    if region == REGION_REVEAL:
        return "Hide" if row.revealed else "Reveal for 10 seconds"
    if region == REGION_COPY:
        return "Copy only"
    if region == REGION_PASTE:
        return "Paste into the previous app"
    if region == REGION_EDIT:
        return "Name and tags"
    if region == REGION_PIN:
        return "Unpin" if row.pinned else "Pin (kept until unpinned)"
    if region == REGION_MARK:
        return "Mark secret" if row.state is SecretState.NORMAL else "Mark normal"
    if region == REGION_MARK_SECRET:
        return "Mark secret"
    if region == REGION_DELETE:
        return "Delete"
    return ""


def button_glyph(row: Row, region: str) -> tuple[str, bool]:
    """The icon for one card button, and whether it is drawn as "on" (accent)."""
    if region == REGION_REVEAL:
        return (_GLYPH_HIDE, True) if row.revealed else (_GLYPH_VIEW, False)
    if region == REGION_PIN:
        return _GLYPH_PIN, row.pinned
    if region == REGION_MARK_SECRET:
        return _GLYPH_LOCK, False
    if region == REGION_MARK:
        # The lock marks secret; the open lock marks a Secret/Review? item normal.
        return (_GLYPH_LOCK, False) if row.state is SecretState.NORMAL else (_GLYPH_UNLOCK, False)
    if region == REGION_COPY:
        return _GLYPH_COPY, False
    if region == REGION_PASTE:
        return _GLYPH_PASTE, False
    if region == REGION_EDIT:
        return _GLYPH_EDIT, False
    return _GLYPH_DELETE, False


def present_filters(items: list[StoredItem]) -> list[Filter]:
    """The chips to offer: secret states, then content types, as found in `items`."""
    states = {item.payload.secret_state for item in items}
    labels = {item.payload.label for item in items}
    found: list[Filter] = [state for state in STATE_BADGES if state in states]
    # No chip for the "possible secret" type: every such item has a Secret or
    # Review? state, and those chips already list them (owner's report, 2026-11-26).
    found += [label for label in LABEL_BADGES if label in labels and label is not Label.POSSIBLE_SECRET]
    seen: dict[str, str] = {}
    for item in items:
        for tag in item.payload.tags:
            seen.setdefault(naming.tag_key(tag), tag)
    found += [TagFilter(seen[key]) for key in sorted(seen)]  # tag chips come after the type chips
    return found


class SearchWindow(QWidget):
    """The window opened by Ctrl+Alt+V."""

    # Emitted after capture is paused or resumed from the window, a hotkey or the tray.
    pause_changed = Signal(bool)

    def __init__(
        self,
        backend: WindowBackend,
        now: Callable[[], datetime] = _utc_now,
        reveals: RevealState | None = None,
        active_window: Callable[[], QWidget | None] = QApplication.activeWindow,
        paster: Paster | None = None,
        current_theme: Callable[[], themes.Theme] = themes.current_theme,
        start_open: bool = True,
        confirm_clear: Callable[[QWidget, int], bool] = _ask_clear,
        settings_panel_factory: Callable[[QWidget], SettingsPanel] | None = None,
        undo_ms: int = 5000,
        ask_text: Callable[[QWidget, str, str, str, int], str | None] = _ask_text,
    ) -> None:
        super().__init__()
        self._built = False  # event filters ignore events until the window is fully built
        self._backend = backend
        # The top bar (search, filters, pause, settings): folded or open when the
        # window opens. The app passes False; tests default to open.
        self._start_open = start_open
        self._drawer_open = start_open
        self.drawer_animation_ms = 180  # tests set 0 so the bar jumps
        self._drawer_anim: QPropertyAnimation | None = None
        self._confirm_clear = confirm_clear
        self._ask_text = ask_text
        self._notice: Notice | None = None
        # Built with this window as its parent, so it uses the same style sheet.
        self.settings_panel = settings_panel_factory(self) if settings_panel_factory else None
        if self.settings_panel is not None:
            # The panel hides itself when another app takes the focus; so do we.
            self.settings_panel.focus_left.connect(self.hide_if_focus_left)
        self._now = now
        self.reveals = reveals or RevealState()
        self._active_window = active_window
        self._paster = paster
        if paster is not None:
            paster.on_copy_only = self.show_copy_notice
        self._current_theme = current_theme
        self._filter: Filter | None = None
        self._chip_filters: list[Filter] = []
        self._ticks = 0
        # Countdown bookkeeping: the earliest secret expiry on screen, and the
        # smallest time-left shown so far for each item (it never goes up).
        self._next_expiry: datetime | None = None
        self._remaining_seen: dict[int, timedelta] = {}
        # Items whose "Show more" is open; forgotten when the window hides.
        self._expanded: set[int] = set()
        self._corners_rounded = False
        self._panel_opened_at = float("-inf")
        # Where the window can go: both are replaced by fake ones in tests.
        self.screen_areas: Callable[[], list[QRect]] = lambda: [
            screen.availableGeometry()
            for screen in sorted(QGuiApplication.screens(), key=lambda s: s is not QGuiApplication.primaryScreen())
        ]  # the main screen first
        self.pointer_pos: Callable[[], QPoint] = QCursor.pos
        # A drag of the top handle in progress: (pointer at start, window at start, panel at start).
        self._window_drag: tuple[QPoint, QPoint, QPoint | None] | None = None
        self._window_dragged = False

        self.setWindowTitle("Smart Clipboard")
        self.setWindowFlags(
            Qt.WindowType.Tool
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
        )
        self.resize(WINDOW_SIZE)
        # Takes the keys while the search bar is hidden.
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

        self.search = QLineEdit()
        self.search.setObjectName("search")
        self.search.setPlaceholderText("Search")
        self.search.setAccessibleName("Search clipboard history")
        # Qt's own clear button sat too low with our rounded box and thick bottom
        # line (seen 2026-11-26), so the x is our own button, placed by hand below.
        self.search.textChanged.connect(self.refresh)
        self.search.textChanged.connect(lambda _text: self._back_to_history())
        self.search.installEventFilter(self)

        icon_font = QFont()
        icon_font.setFamilies(_ICON_FONTS)
        self.pause_button = QToolButton()
        self.pause_button.setFont(icon_font)
        self.pause_button.clicked.connect(self.toggle_pause)
        # The gear opens the settings panel, which also holds what the old
        # "..." menu had (search bar, filters, Clear all, Start with Windows, Quit).
        self.settings_button = QToolButton()
        self.settings_button.setFont(icon_font)
        self.settings_button.setText(_GLYPH_SETTINGS)
        self.settings_button.setToolTip("Settings (Ctrl+,)")
        self.settings_button.setAccessibleName("Settings")
        self.settings_button.setEnabled(self.settings_panel is not None)
        self.settings_button.clicked.connect(self.toggle_settings)
        if self.settings_panel is not None:
            self.settings_panel.clear_all_requested.connect(self.clear_all)
            self.settings_panel.quit_requested.connect(self._backend.quit)

        # Shown while paused: what it means, and a way back (UI plan item 5).
        self.pause_banner = QFrame()
        self.pause_banner.setObjectName("banner")
        self.pause_label = QLabel(PAUSED_TEXT)
        self.resume_button = QPushButton("Resume")
        self.resume_button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.resume_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.resume_button.clicked.connect(self.toggle_pause)
        banner_row = QHBoxLayout(self.pause_banner)
        banner_row.setContentsMargins(10, 4, 6, 4)
        banner_row.addWidget(self.pause_label, 1)
        banner_row.addWidget(self.resume_button)

        self.chips_bar = QWidget()
        self.chips_bar.setObjectName("chips")
        self.chips_layout = QHBoxLayout(self.chips_bar)
        self.chips_layout.setContentsMargins(0, 0, 0, 0)
        self.chips_layout.setSpacing(6)
        self.chips: dict[Filter | None, QPushButton] = {}
        chips_scroll = QScrollArea()
        chips_scroll.setWidget(self.chips_bar)
        chips_scroll.setWidgetResizable(True)
        chips_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        # No scroll bar: the mouse wheel scrolls the chips sideways instead.
        chips_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        chips_scroll.setFixedHeight(30)
        chips_scroll.viewport().installEventFilter(self)
        self.chips_bar.installEventFilter(self)
        self._drag_start: tuple[float, int] | None = None
        self._dragging = False
        self.chips_scroll = chips_scroll
        self.search_clear = QToolButton(self.search)
        self.search_clear.setObjectName("searchClear")
        self.search_clear.setFont(icon_font)
        self.search_clear.setText(_GLYPH_CLEAR)
        self.search_clear.setToolTip("Clear search")
        self.search_clear.setAccessibleName("Clear search")
        self.search_clear.setCursor(Qt.CursorShape.ArrowCursor)
        self.search_clear.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.search_clear.clicked.connect(self._clear_search_clicked)
        self.search_clear.hide()
        chips_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.fade_left = EdgeFade(chips_scroll, right=False)
        self.fade_right = EdgeFade(chips_scroll, right=True)
        bar = chips_scroll.horizontalScrollBar()
        bar.valueChanged.connect(lambda _v: self.update_chip_fades())
        bar.rangeChanged.connect(lambda _a, _b: self.update_chip_fades())
        self.clear_button = QPushButton("Clear all")
        self.clear_button.setProperty("chip", True)
        self.clear_button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.clear_button.setToolTip(CLEAR_TOOLTIP)
        self.clear_button.setAccessibleName("Clear all")
        self.clear_button.clicked.connect(self.clear_all)
        self.filters_row = QWidget()
        filters_layout = QHBoxLayout(self.filters_row)
        filters_layout.setContentsMargins(0, 0, 0, 0)
        filters_layout.setSpacing(6)
        filters_layout.addWidget(chips_scroll, 1)
        filters_layout.addWidget(self.clear_button)

        self.model = CardsModel(self)
        self.list = QListView()
        self.list.setModel(self.model)
        self.delegate = CardDelegate(self.list)
        self.delegate.paste_enabled = paster is not None
        self.list.selectionModel().currentChanged.connect(lambda *_args: self._close_strip_if_moved())
        self.list.setItemDelegate(self.delegate)
        self.list.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.list.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.list.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.list.setResizeMode(QListView.ResizeMode.Adjust)
        self.list.setMouseTracking(True)
        self.list.setFocusPolicy(Qt.FocusPolicy.NoFocus)  # keys go to the search box
        self.list.setAccessibleName("Clipboard items")
        self.list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.list.customContextMenuRequested.connect(self._on_context_menu)
        self.delegate.region_clicked.connect(self._on_region_clicked)

        # The designed empty screens (no history, no match, paused, secrets only).
        self.empty_view = EmptyView()
        self.empty_view.action_triggered.connect(self._on_empty_action)
        self.status_label = QLabel()
        self.status_label.setObjectName("status")
        self.status_label.setWordWrap(True)
        # Floats over the bottom of the window (not in the layout).
        self._undo_items: list[StoredItem] = []
        self.toast = Toast(self, duration_ms=undo_ms)
        self.toast.action_clicked.connect(self.undo_delete)
        self.toast.finished.connect(self._drop_undo_items)

        panel = QFrame()
        panel.setObjectName("panel")
        top = QHBoxLayout()
        top.setSpacing(4)
        top.addWidget(self.search, 1)
        top.addStretch(0)  # keeps the buttons at the right while the search bar is hidden
        top.addWidget(self.pause_button)
        top.addWidget(self.settings_button)
        # Two pages (UI plan item 6): History, which is everything above, and Insights.
        self._page = PAGE_HISTORY
        self.history_page = QWidget()
        history_layout = QVBoxLayout(self.history_page)
        history_layout.setContentsMargins(0, 0, 0, 0)
        history_layout.setSpacing(6)
        history_layout.addWidget(self.pause_banner)
        history_layout.addWidget(self.list, 1)
        history_layout.addWidget(self.empty_view, 1)
        self.insights_view = InsightsView() if InsightsView is not None else None
        self.pages = QStackedWidget()
        self.pages.addWidget(self.history_page)
        if self.insights_view is not None:
            self.pages.addWidget(self.insights_view)
        self.segment = QFrame()
        self.segment.setObjectName("segment")
        self.tab_history = QPushButton("History")
        self.tab_insights = QPushButton("Insights")
        segment_row = QHBoxLayout(self.segment)
        segment_row.setContentsMargins(3, 3, 3, 3)
        segment_row.setSpacing(2)
        group = QButtonGroup(self.segment)
        for name, button in ((PAGE_HISTORY, self.tab_history), (PAGE_INSIGHTS, self.tab_insights)):
            button.setCheckable(True)
            button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            group.addButton(button)
            segment_row.addWidget(button)
            button.clicked.connect(lambda _checked=False, page=name: self.set_page(page))
        self.tab_history.setChecked(True)
        self.segment.setVisible(self.insights_view is not None)  # no tabs without Insights
        inner = QVBoxLayout(panel)
        inner.setContentsMargins(10, 10, 10, 8)
        inner.setSpacing(6)
        # The handle across the top and the bar it drops down (search, pause,
        # settings, History | Insights, filter chips).
        self.handle = QPushButton()
        self.handle.setObjectName("handle")
        self.handle.setFont(icon_font)
        self.handle.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.handle.setCursor(Qt.CursorShape.OpenHandCursor)  # a click folds; a drag moves the window
        self.handle.setFixedHeight(18)
        self.handle.clicked.connect(self.toggle_drawer)
        self.handle.installEventFilter(self)
        self.drawer = QWidget()
        drawer_layout = QVBoxLayout(self.drawer)
        drawer_layout.setContentsMargins(0, 0, 0, 0)
        drawer_layout.setSpacing(6)
        drawer_layout.addLayout(top)
        drawer_layout.addWidget(self.segment)
        drawer_layout.addWidget(self.filters_row)
        inner.addWidget(self.handle)
        inner.addWidget(self.drawer)
        inner.addWidget(self.pages, 1)
        inner.addWidget(self.status_label)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(panel)

        self.shortcuts: dict[str, QShortcut] = {}
        for keys, action in (
            ("Ctrl+R", self.toggle_reveal_selected),
            ("Ctrl+P", self.toggle_pin_selected),
            ("Shift+Del", self.delete_selected),
            ("Ctrl+F", lambda: self.show_search("")),
            ("Ctrl+E", self.toggle_actions_selected),
            ("Ctrl+,", self.toggle_settings),
            ("Ctrl+Shift+P", self.toggle_pause),
            ("Ctrl+D", self.toggle_drawer),
            ("Shift+F10", self.open_menu_for_selected),
            ("Menu", self.open_menu_for_selected),
        ):
            self.shortcuts[keys] = QShortcut(QKeySequence(keys), self, activated=action)
        # Alt, not bare digits, so typing numbers in the search box still works.
        for number in range(1, QUICK_PASTE_COUNT + 1):
            keys = f"Alt+{number}"
            self.shortcuts[keys] = QShortcut(
                QKeySequence(keys), self, activated=lambda n=number: self.quick_paste(n)
            )

        self._timer = QTimer(self)
        self._timer.setInterval(1000)
        self._timer.timeout.connect(self._tick)

        self.apply_theme()
        self.set_drawer(self._start_open, animate=False)
        self.refresh()
        self._built = True

    # --- Showing and hiding ---

    def show_and_focus(self) -> None:
        """Open the window fresh next to the pointer: empty search, no filter."""
        if self._paster is not None and not self.isVisible():
            self._paster.remember()
        self.search.blockSignals(True)
        self.search.clear()
        self.search.blockSignals(False)
        self._filter = None
        self.reveals.hide_all()
        self._page = PAGE_HISTORY  # a fresh opening starts on History
        self.tab_history.setChecked(True)
        self.filters_row.setVisible(True)
        self.pages.setCurrentWidget(self.history_page)
        self.apply_theme()
        self.set_drawer(self._start_open, animate=False)
        self.refresh()
        self.place_on_open()
        self.show()
        self.raise_()
        self.activateWindow()
        self.focus_keys()

    def focus_keys(self) -> None:
        """Keys go to the search box, or to the window while the box is hidden."""
        if self.search.isVisibleTo(self):
            self.search.setFocus()
        else:
            self.setFocus()

    # --- The top bar that drops down (search, filters, pause, settings) ---

    @property
    def drawer_open(self) -> bool:
        return self._drawer_open

    def toggle_drawer(self) -> None:
        self.set_drawer(not self._drawer_open)
        self.focus_keys()

    def set_drawer(self, open_: bool, animate: bool = True) -> None:
        """Drop the top bar down or fold it away.

        Folding it clears the search and the filter: a hidden search or chip
        must never keep filtering the cards without being seen.
        """
        cleared = False
        if not open_ and (self.search.text() or self._filter is not None):
            self.search.blockSignals(True)
            self.search.clear()
            self.search.blockSignals(False)
            self._filter = None
            cleared = True
        changed = open_ != self._drawer_open
        self._drawer_open = open_
        self.handle.setText(_GLYPH_UP if open_ else _GLYPH_DOWN)
        label = "Hide search, filters and settings" if open_ else "Show search, filters and settings"
        self.handle.setToolTip(f"{label}. Drag to move the window.")
        self.handle.setAccessibleName(label)
        self._place_drawer(animate and changed)
        if cleared:
            self.refresh()

    def _place_drawer(self, animate: bool) -> None:
        """Show or hide the bar, sliding its height when `animate` and the window is on screen."""
        if self._drawer_anim is not None:
            self._drawer_anim.stop()
        open_ = self._drawer_open
        if not animate or self.drawer_animation_ms <= 0 or not self.isVisible():
            self.drawer.setMaximumHeight(_NO_LIMIT)
            self.drawer.setVisible(open_)
            return
        full = self.drawer.layout().sizeHint().height()
        start = self.drawer.height() if self.drawer.isVisible() else 0
        if open_:
            self.drawer.setMaximumHeight(max(0, start))
            self.drawer.setVisible(True)
        animation = QPropertyAnimation(self.drawer, b"maximumHeight", self)
        animation.setDuration(self.drawer_animation_ms)
        animation.setEasingCurve(QEasingCurve.Type.OutCubic)
        animation.setStartValue(max(0, start))
        animation.setEndValue(full if open_ else 0)
        animation.finished.connect(self._drawer_settled)
        self._drawer_anim = animation
        animation.start()

    def _drawer_settled(self) -> None:
        self.drawer.setMaximumHeight(_NO_LIMIT)
        self.drawer.setVisible(self._drawer_open)

    def show_search(self, text: str) -> None:
        """Open the bar (if folded), focus the search box and add `text` to it."""
        self.set_drawer(True)
        self.search.setFocus()
        if text:
            self.search.insert(text)

    def apply_theme(self) -> None:
        """Use the light or dark colours that match Windows right now."""
        theme = self._current_theme()
        self.delegate.theme = theme
        self.setStyleSheet(themes.stylesheet(theme))
        self.fade_left.set_colour(theme.window)
        self.fade_right.set_colour(theme.window)
        self.list.viewport().update()

    def update_chip_fades(self) -> None:
        """Show a fade on each side of the chips that has more chips beyond it."""
        bar = self.chips_scroll.horizontalScrollBar()
        view = self.chips_scroll.viewport()
        height = view.height()
        self.fade_left.setGeometry(0, 0, EdgeFade.WIDTH, height)
        self.fade_right.setGeometry(view.width() - EdgeFade.WIDTH, 0, EdgeFade.WIDTH, height)
        self.fade_left.setVisible(bar.value() > bar.minimum())
        self.fade_right.setVisible(bar.value() < bar.maximum())
        self.fade_left.raise_()
        self.fade_right.raise_()

    def remembers_position(self) -> bool:
        """True if the owner chose "Where I left it" in the settings."""
        return self.settings_panel is not None and self.settings_panel.settings.window_position == "remembered"

    def place_on_open(self) -> None:
        """Put the window where the owner's setting says: where it was left, or by the pointer.

        "Where I left it" uses the two saved numbers if they are on a screen
        that is connected now (clamped to it); with none saved, or after a
        monitor was unplugged, it opens next to the pointer like the default.
        """
        if self.remembers_position():
            spot = self.settings_panel.settings.window_spot
            found = restore_position(QPoint(*spot) if spot else None, WINDOW_SIZE, self.screen_areas())
            if found is not None:
                point, area = found
                self.resize(fit_size(WINDOW_SIZE, area))
                self.move(keep_on_screen(point, self.size(), area))
                return
        self.move_near_pointer()

    def move_near_pointer(self) -> None:
        pointer = self.pointer_pos()
        areas = self.screen_areas()
        area = next((a for a in areas if a.contains(pointer)), areas[0] if areas else None)
        if area is not None:
            # A short screen (200% scaling on 1080p is 540 logical pixels high)
            # gets a shorter window instead of one that runs off the screen.
            self.resize(fit_size(WINDOW_SIZE, area))
            self.move(place_near(pointer, self.size(), area))

    def _drag_window(self, event: QEvent) -> bool:
        """Move the window by dragging the top handle. True if `event` was used up.

        A press and release without movement stays a click (it folds or opens
        the top bar). The window is kept inside the screen under the pointer,
        and the settings panel, if open, moves with it. When the owner chose
        "Where I left it", the final spot is saved (two numbers).
        """
        kind = event.type()
        button = getattr(event, "button", lambda: None)()
        if kind == QEvent.Type.MouseButtonPress and button == Qt.MouseButton.LeftButton:
            panel = self.settings_panel
            panel_at = panel.pos() if panel is not None and panel.isVisible() else None
            self._window_drag = (event.globalPosition().toPoint(), self.pos(), panel_at)  # type: ignore[attr-defined]
            self._window_dragged = False
            return False
        if self._window_drag is None:
            return False
        start, window_at, panel_at = self._window_drag
        if kind == QEvent.Type.MouseMove:
            here = event.globalPosition().toPoint()  # type: ignore[attr-defined]
            delta = here - start
            if not self._window_dragged and delta.manhattanLength() < 5:
                return False
            if not self._window_dragged:
                self._window_dragged = True
                self.handle.setCursor(Qt.CursorShape.ClosedHandCursor)
            areas = self.screen_areas()
            # The screen under the pointer; if it is in a gap between screens, the
            # screen the window is on now (never "no screen": it must stay reachable).
            area = next(
                (a for a in areas if a.contains(here)),
                next((a for a in areas if a.contains(self.geometry().center())), areas[0] if areas else None),
            )
            target = window_at + delta
            self.move(keep_on_screen(target, self.size(), area) if area else target)
            if panel_at is not None and self.settings_panel is not None:
                self.settings_panel.move(panel_at + (self.pos() - window_at))
            return True
        if kind == QEvent.Type.MouseButtonRelease:
            dragged, self._window_drag, self._window_dragged = self._window_dragged, None, False
            self.handle.setCursor(Qt.CursorShape.OpenHandCursor)
            if not dragged:
                return False  # a plain click: the handle folds or opens the bar
            self.handle.setDown(False)
            if self.remembers_position():
                self.settings_panel.remember_position(self.x(), self.y())
            return True
        return False

    def showEvent(self, event: QEvent) -> None:  # noqa: N802 (Qt name)
        self._ticks = 0
        self._timer.start()
        if not self._corners_rounded:
            self._corners_rounded = True
            _round_corners(self)
        super().showEvent(event)

    def open_settings(self) -> None:
        """Show the settings panel beside this window."""
        if self.settings_panel is not None:
            self._panel_opened_at = time.monotonic()
            self.settings_panel.open_beside(self)
            self.sync_settings_panel()

    def toggle_settings(self) -> None:
        """The gear: open the panel, or close it if it is already open."""
        if self.settings_panel is not None and self.settings_panel.isVisible():
            self.settings_panel.hide()
        else:
            self.open_settings()

    def sync_settings_panel(self) -> None:
        """Show the window's own choices in the panel's Window group."""
        if self.settings_panel is not None and hasattr(self, "clear_button"):
            self.settings_panel.set_window_state(bool(self.clearable_rows()))

    def hideEvent(self, event: QEvent) -> None:  # noqa: N802 (Qt name)
        # A pending Undo ends with the window: the deletes are already final.
        self.toast.dismiss()
        self.close_actions(animate=False)
        self._expanded.clear()
        if self.settings_panel is not None:
            self.settings_panel.hide()
        # Nothing stays revealed behind a hidden window.
        self._timer.stop()
        self.reveals.hide_all()
        self.refresh()
        super().hideEvent(event)

    def changeEvent(self, event: QEvent) -> None:  # noqa: N802 (Qt name)
        if event.type() == QEvent.Type.ActivationChange and not self.isActiveWindow():
            # Checked once Qt has settled which window is active, so moving to
            # our own delete question does not hide the window.
            QTimer.singleShot(0, self.hide_if_focus_left)
        super().changeEvent(event)

    def hide_if_focus_left(self) -> None:
        """Hide when no window of this app has the focus any more."""
        if time.monotonic() - self._panel_opened_at < 0.4:
            return  # focus is still moving to the panel just opened
        # With the panel open this also runs when you click back on this window
        # and then outside: hiding the window hides the panel too (hideEvent).
        if self.isVisible() and self._active_window() is None and QApplication.activePopupWidget() is None:
            self.hide()

    def closeEvent(self, event: QEvent) -> None:  # noqa: N802 (Qt name)
        # Closing only hides; the app keeps running. Quit stops it.
        event.ignore()
        self.hide()

    # --- Rows ---

    @property
    def active_filter(self) -> Filter | None:
        return self._filter

    def set_filter(self, active: Filter | None) -> None:
        self._filter = active
        if active is not None and not self._drawer_open:
            self.set_drawer(True)  # so the filter can be seen and cleared
        self.refresh()

    @property
    def page(self) -> str:
        """"history" or "insights": which tab is showing."""
        return self._page

    def set_page(self, page: str) -> None:
        """Switch between the History list and the Insights summary."""
        wanted = page == PAGE_INSIGHTS and self.insights_view is not None
        self._page = PAGE_INSIGHTS if wanted else PAGE_HISTORY
        on_insights = self._page == PAGE_INSIGHTS
        self.tab_insights.setChecked(on_insights)
        self.tab_history.setChecked(not on_insights)
        self.filters_row.setVisible(not on_insights)
        self.pages.setCurrentWidget(self.insights_view if on_insights else self.history_page)
        if on_insights:
            self.show_insights()
        else:
            self.refresh()

    def _back_to_history(self) -> None:
        """Typing a search shows the History list again."""
        if self._page != PAGE_HISTORY:
            self.set_page(PAGE_HISTORY)

    def show_insights(self) -> None:
        """Work out the numbers now (they are not kept up to date while open)."""
        self.insights_view.show_insights(
            compute_insights(self._backend.items(), self._now()), self._current_theme()
        )

    def refresh(self) -> None:
        """Rebuild the cards from the backend, keeping the selected item selected."""
        selected = self.selected_row()
        items = self._backend.items()
        now = self._now()
        rows = self._keep_countdowns_falling(
            build_rows(
                items, self.search.text(), self._filter, self.reveals, now,
                expanded=frozenset(self._expanded),
            )
        )
        self._next_expiry = min(
            (now + row.remaining for row in rows if row.remaining is not None), default=None
        )
        entries = arrange(rows, now, bool(self.search.text().strip()))
        self.model.set_rows(entries)
        open_item = self.delegate.actions_item
        if open_item is not None and self.model.position_of(open_item) is None:
            self.close_actions(animate=False)  # that card is gone
        cards = self.model.card_positions()
        target = cards[0] if cards else None
        if selected is not None:
            kept = self.model.position_of(selected.item_id)
            if kept is not None:
                target = kept
        if target is not None:
            self.list.setCurrentIndex(self.model.index(target))
        self.list.setVisible(bool(rows))
        paused = self._backend.is_paused()
        self.empty_view.setVisible(not rows)
        empty = None
        if not rows:
            empty = empty_state(items, self.search.text(), self._filter, paused)
            self.empty_view.show_state(empty, self.delegate.theme)
        # Shown only while there is text (checked here, because the search box is
        # sometimes cleared with its signals blocked, e.g. when the window opens).
        self.search_clear.setVisible(bool(self.search.text()))
        self._update_chips(items)
        # A paused empty screen carries its own Resume button, so no banner above it.
        self.pause_banner.setVisible(paused and (empty is None or empty.kind != EMPTY_PAUSED))
        self.pause_button.setProperty("active", paused)  # drawn highlighted while paused
        self.pause_button.style().unpolish(self.pause_button)
        self.pause_button.style().polish(self.pause_button)
        self.pause_button.setText(_GLYPH_PLAY if paused else _GLYPH_PAUSE)
        pause_text = "Resume capture" if paused else "Pause capture"
        self.pause_button.setToolTip(f"{pause_text} (Ctrl+Shift+P)")
        self.pause_button.setAccessibleName(pause_text)
        self.status_label.setVisible(bool(self.status_label.text()))
        clearable = bool(self.clearable_rows())
        self.clear_button.setEnabled(clearable)
        self.sync_settings_panel()

    def _clear_search_clicked(self) -> None:
        self.search.clear()
        self.search.setFocus()

    def _place_search_clear(self) -> None:
        """Put the x at the right end of the search box, centred above its bottom line."""
        size = self.search_clear.sizeHint()
        box = self.search.rect()
        y = (box.height() - size.height()) // 2 + 1  # the glyph sits a little high in its box
        self.search_clear.move(box.width() - size.width() - 6, max(0, y))

    def _on_empty_action(self, action: str) -> None:
        """A button on an empty screen: resume capture, or clear the search or the filter."""
        if action == ACTION_RESUME:
            if self._backend.is_paused():
                self.toggle_pause()
        elif action == ACTION_CLEAR_SEARCH:
            self.search.clear()  # the text change refreshes the list
            self.focus_keys()
        elif action == ACTION_CLEAR_FILTER:
            self.set_filter(None)
            self.focus_keys()

    def clearable_rows(self) -> list[Row]:
        """What Clear all would delete: the cards shown now, except pinned ones."""
        return [row for row in self.model.rows if not row.pinned]

    def _update_chips(self, items: list[StoredItem]) -> None:
        wanted = present_filters(items)
        if self._filter is not None and self._filter not in wanted:
            wanted.append(self._filter)  # so an active filter can always be cleared
        if wanted != self._chip_filters:
            self._chip_filters = wanted
            # Take out everything, the old stretch spacer too: a leftover spacer
            # sat in front of the new chips and pushed them to the right.
            while (entry := self.chips_layout.takeAt(0)) is not None:
                widget = entry.widget()
                if widget is not None:
                    widget.hide()  # gone from the screen now, deleted when Qt gets to it
                    widget.deleteLater()
            self.chips = {}
            for active in [None, *wanted]:
                chip = QPushButton("All" if active is None else filter_name(active))
                chip.setProperty("chip", True)
                chip.setCheckable(True)
                chip.setFocusPolicy(Qt.FocusPolicy.NoFocus)
                chip.installEventFilter(self)  # so a drag that starts on a chip scrolls
                chip.setAccessibleName(
                    "Show all items" if active is None else f"Show only {filter_name(active)}"
                )
                chip.clicked.connect(lambda _checked=False, f=active: self.set_filter(f))
                self.chips_layout.addWidget(chip)
                self.chips[active] = chip
            self.chips_layout.addStretch(1)
        for active, chip in self.chips.items():
            chip.setChecked(active == self._filter)

    def _close_strip_if_moved(self) -> None:
        """Moving the selection to another card slides the open strip shut."""
        open_item = self.delegate.actions_item
        row = self.selected_row()
        if open_item is not None and (row is None or row.item_id != open_item):
            self.close_actions()

    def selected_row(self) -> Row | None:
        if self._page != PAGE_HISTORY:
            return None  # the hidden list must never act on keys pressed over Insights
        index = self.list.currentIndex()
        return self.model.row_at(index.row()) if index.isValid() else None

    def select_item(self, item_id: int) -> bool:
        position = self.model.position_of(item_id)
        if position is None:
            return False
        self.list.setCurrentIndex(self.model.index(position))
        return True

    def _move_selection(self, step: int) -> None:
        cards = self.model.card_positions()  # headers are skipped
        if not cards:
            return
        current = self.list.currentIndex().row()
        if current in cards:
            place = max(0, min(len(cards) - 1, cards.index(current) + step))
        else:
            place = 0 if step > 0 else len(cards) - 1
        self.list.setCurrentIndex(self.model.index(cards[place]))

    def _keep_countdowns_falling(self, rows: list[Row]) -> list[Row]:
        """Never show more time left than before, even if the clock was set back.

        The real deadline (retention) uses the clock; this only keeps the
        pill from counting up. Items no longer shown are forgotten.
        """
        seen = self._remaining_seen
        kept: list[Row] = []
        for row in rows:
            if row.remaining is not None:
                lowest = min(row.remaining, seen.get(row.item_id, row.remaining))
                seen[row.item_id] = lowest
                if lowest != row.remaining:
                    row = replace(row, remaining=lowest)
            kept.append(row)
        shown = {row.item_id for row in rows}
        for item_id in [i for i in seen if i not in shown]:
            del seen[item_id]
        return kept

    def _tick(self) -> None:
        self._ticks += 1
        now = self._now()
        expired = self.reveals.expire(now)
        # A secret reaching its deadline leaves the list right then, not up to
        # 30 seconds later; the pills themselves update every 30 seconds.
        due = self._next_expiry is not None and now >= self._next_expiry
        if expired or due or self._ticks % _AGE_REFRESH_TICKS == 0:
            self.refresh()

    # --- Clicks and the card menu ---

    def _on_region_clicked(self, position: int, region: str, global_pos: QPoint) -> None:
        row = self.model.row_at(position)
        if row is not None:
            self.list.setCurrentIndex(self.model.index(position))
            self.handle_region(row, region, global_pos)

    def handle_region(self, row: Row, region: str, global_pos: QPoint | None = None) -> None:
        """Act on a click on one part of a card."""
        if region == REGION_LABEL:
            self.set_filter(row.label)
        elif region == REGION_STATE and row.state_badge is not None:
            self.set_filter(row.state)
        elif region == REGION_TEXT and (row.masked or row.revealed):
            self.toggle_reveal(row)
        elif region.startswith(TAG_PREFIX):
            self.set_filter(TagFilter(region[len(TAG_PREFIX):]))
        elif region == REGION_EXPAND:
            self.toggle_expand(row)
        elif region == REGION_MENU:
            self.toggle_actions(row)
        elif region == REGION_STRIP:  # the strip's empty area closes it
            self.close_actions()
        elif region == REGION_COPY:
            self.close_actions(animate=False)
            self.choose(row, paste=False)
        elif region == REGION_PASTE:
            self.close_actions(animate=False)
            self.choose(row, paste=True)
        elif region == REGION_EDIT:
            self.edit_names(row, global_pos or QCursor.pos())
        elif region == REGION_REVEAL:
            self.toggle_reveal(row)
        elif region == REGION_PIN:
            self.toggle_pin(row)
        elif region == REGION_MARK:
            self.close_actions(animate=False)
            if row.state is SecretState.NORMAL:
                self.mark_secret(row)
            else:
                self.mark_normal(row)
        elif region == REGION_MARK_SECRET:
            self.close_actions(animate=False)
            self.mark_secret(row)
        elif region == REGION_DELETE:
            self.close_actions(animate=False)
            self.delete(row)
        else:
            self.choose_selected()

    def build_menu(self, row: Row) -> QMenu:
        """The card menu for one row. Item actions apply to that row."""
        menu = QMenu(self)
        sensitive = row.state is not SecretState.NORMAL
        if self._paster is not None:
            menu.addAction("Paste", lambda: self.choose(row, paste=True))
            menu.addAction("Copy only", lambda: self.choose(row, paste=False))
        else:
            menu.addAction("Copy", lambda: self.choose(row, paste=False))
        menu.addSeparator()
        reveal = menu.addAction("Hide" if row.revealed else "Reveal", lambda: self.toggle_reveal(row))
        reveal.setEnabled(sensitive)
        pin = menu.addAction("Unpin" if row.pinned else "Pin", lambda: self.toggle_pin(row))
        pin.setEnabled(row.can_pin)
        normal = menu.addAction("Mark normal", lambda: self.mark_normal(row))
        normal.setEnabled(sensitive)
        secret = menu.addAction("Mark secret", lambda: self.mark_secret(row))
        secret.setEnabled(row.state is not SecretState.SECRET)
        menu.addSeparator()
        self._add_naming_actions(menu, row, sensitive)
        menu.addSeparator()
        menu.addAction("Delete", lambda: self.delete(row))
        return menu

    def _add_naming_actions(self, menu: QMenu, row: Row, sensitive: bool) -> None:
        """Rename and tags for a pinned item; otherwise a line that says why not."""
        if row.can_name:
            menu.addAction("Rename..." if row.title else "Add a name...", lambda: self.rename(row))
            if row.title:
                menu.addAction("Remove name", lambda: self.remove_name(row))
            add = menu.addAction("Add tag...", lambda: self.add_tag(row))
            add.setEnabled(len(row.tags) < naming.MAX_TAGS)
            if row.tags:
                remove = menu.addMenu("Remove tag")
                for tag in row.tags:
                    remove.addAction(tag, lambda _checked=False, t=tag: self.remove_tag(row, t))
            return
        menu.setToolTipsVisible(True)
        note = menu.addAction(NAMING_NOTE_SECRET if sensitive else NAMING_NOTE_PIN)
        note.setEnabled(False)
        if sensitive:
            note.setToolTip(NAMING_NOTE_SECRET_TIP)

    # --- The sliding action strip ---

    def toggle_actions(self, row: Row) -> None:
        """"...": slide the action icons in over the card, or slide them away again."""
        if self.delegate.is_open(row.item_id):
            self.close_actions()
        else:
            self.delegate.open_actions(row.item_id)
        self.list.viewport().update()

    def toggle_actions_selected(self) -> None:
        """Ctrl+E: the keyboard way to press "..." on the selected card."""
        row = self.selected_row()
        if row is not None:
            self.toggle_actions(row)

    def close_actions(self, animate: bool = True) -> None:
        self.delegate.close_actions(animate)

    def edit_names(self, row: Row, global_pos: QPoint) -> None:
        """The pencil icon: name and tag choices for a pinned item, as a small menu."""
        menu = QMenu(self)
        self._add_naming_actions(menu, row, row.state is not SecretState.NORMAL)
        menu.exec(global_pos)

    def open_menu(self, row: Row, global_pos: QPoint) -> None:
        self.build_menu(row).exec(global_pos)

    def open_menu_for_selected(self) -> None:
        row = self.selected_row()
        if row is None:
            return
        rect = self.list.visualRect(self.list.currentIndex())
        self.open_menu(row, self.list.viewport().mapToGlobal(rect.center()))

    def _on_context_menu(self, point: QPoint) -> None:
        index = self.list.indexAt(point)
        row = self.model.row_at(index.row()) if index.isValid() else None
        if row is not None:
            self.list.setCurrentIndex(index)
            self.open_menu(row, self.list.viewport().mapToGlobal(point))

    # --- Actions ---

    def rename(self, row: Row) -> None:
        """Ask for a name for a pinned item. Empty removes it; cancel changes nothing."""
        if not row.can_name:
            return
        text = self._ask_text(self, "Name this item", "Name:", row.title or "", naming.MAX_TITLE_CHARS)
        if text is None:
            return
        self._backend.rename(row.item_id, naming.clean_title(text))
        self.refresh()
        self.select_item(row.item_id)

    def remove_name(self, row: Row) -> None:
        if row.can_name:
            self._backend.rename(row.item_id, None)
            self.refresh()

    def add_tag(self, row: Row) -> None:
        """Ask for one more tag (at most three, no repeats)."""
        if not row.can_name or len(row.tags) >= naming.MAX_TAGS:
            return
        text = self._ask_text(self, "Add a tag", "Tag:", "", naming.MAX_TAG_CHARS)
        if text is None:
            return
        tags = naming.add_tag(row.tags, text)
        if tags != row.tags:
            self._backend.set_tags(row.item_id, tags)
        self.refresh()
        self.select_item(row.item_id)

    def remove_tag(self, row: Row, tag: str) -> None:
        if row.can_name:
            self._backend.set_tags(row.item_id, naming.remove_tag(row.tags, tag))
            self.refresh()

    def toggle_expand(self, row: Row) -> None:
        """Open or close "Show more" on one card, in place."""
        if row.rich is None or not row.rich.expandable:
            return
        self._expanded.symmetric_difference_update({row.item_id})
        self.refresh()
        self.select_item(row.item_id)

    def toggle_reveal(self, row: Row) -> None:
        if row.revealed:
            self.reveals.hide(row.item_id)
        elif row.masked:
            self.reveals.reveal(row.item_id, self._now())
        self.refresh()

    def toggle_reveal_selected(self) -> None:
        row = self.selected_row()
        if row is not None:
            self.toggle_reveal(row)

    def quick_paste(self, number: int) -> bool:
        """Paste the `number`-th card shown (headers are not counted), as Enter would.

        Goes through choose(), so a secret takes the same safe copy-back path.
        False if there is no such card.
        """
        rows = self.model.rows
        if self._page != PAGE_HISTORY or not 1 <= number <= min(QUICK_PASTE_COUNT, len(rows)):
            return False
        self.choose(rows[number - 1], paste=True)
        return True

    def show_copy_notice(self) -> None:
        """Tell the owner the item is on the clipboard but was not pasted for them."""
        if self._notice is None:
            self._notice = Notice()
        self._notice.show_text(COPIED_TEXT, self.styleSheet(), QCursor.pos())

    def choose_selected(self) -> None:
        row = self.selected_row()
        if row is not None:
            self.choose(row, paste=True)

    def choose(self, row: Row, paste: bool) -> None:
        """Copy the item back, and paste it into the previous app if asked and set up."""
        def copy() -> bool:
            return self._backend.copy_back(row.item_id)

        if paste and self._paster is not None:
            ok = self._paster.paste(copy)
        else:
            ok = copy()
        if ok:
            self.status_label.setText("")
            if not paste or self._paster is None:
                self.show_copy_notice()  # copied, and nothing was pasted for the owner
            self.hide()
        else:
            self.status_label.setText("Could not copy")
            self.status_label.setVisible(True)

    def toggle_pin(self, row: Row) -> None:
        if not row.can_pin:
            return
        if row.pinned:
            self._backend.unpin(row.item_id)
        else:
            self._backend.pin(row.item_id)
        self.refresh()

    def toggle_pin_selected(self) -> None:
        row = self.selected_row()
        if row is not None:
            self.toggle_pin(row)

    def mark_normal(self, row: Row) -> None:
        if row.state is not SecretState.NORMAL:
            self._backend.mark_normal(row.item_id)
            self.reveals.hide(row.item_id)
            self.refresh()

    def mark_secret(self, row: Row) -> None:
        if row.state is not SecretState.SECRET:
            self._backend.mark_secret(row.item_id)
            self.reveals.hide(row.item_id)
            self.refresh()

    def delete(self, row: Row) -> None:
        """Delete one card at once; the toast offers Undo for a few seconds."""
        self._delete_with_undo([row.item_id], "Item deleted")

    def clear_all(self) -> None:
        """Delete every card shown (current chip and search), keeping pinned ones."""
        rows = self.clearable_rows()
        if not rows or not self._confirm_clear(self, len(rows)):
            return
        kept = sum(1 for row in self.model.rows if row.pinned)
        self._delete_with_undo([row.item_id for row in rows], clear_text(len(rows), kept))

    def _delete_with_undo(self, item_ids: list[int], message: str) -> None:
        """Delete the items now and keep them in memory until Undo or the toast ends.

        The delete is real at once (the database row is gone). Undo puts the
        kept items back in their old order with their pins; the kept copy is
        dropped when the toast ends, the window hides, or another delete
        starts. Nothing about the items is logged or shown in the message.
        """
        self.toast.dismiss()  # finishes an earlier Undo offer and drops its items
        wanted = set(item_ids)
        kept = [item for item in self._backend.items() if item.id in wanted]
        deleted = [item for item in kept if self._backend.delete(item.id)]
        for item_id in item_ids:
            self.reveals.hide(item_id)
        self._undo_items = deleted
        self.refresh()
        if deleted:
            self._show_toast(message, UNDO_TEXT)

    def undo_delete(self) -> None:
        """Put back what the last delete or Clear all removed."""
        items, self._undo_items = self._undo_items, []
        if not items:
            return
        restored = self._backend.restore(items)
        self.refresh()
        if restored < len(items):
            self._show_toast(partial_restore_text(restored, len(items)))
        else:
            self._show_toast("Restored")

    def _drop_undo_items(self) -> None:
        self._undo_items = []

    def _show_toast(self, text: str, action: str | None = None) -> None:
        self.toast.show_message(text, action)
        self._place_toast()

    def _place_toast(self) -> None:
        """Float the toast along the bottom of the window."""
        margin = 12
        self.toast.setFixedWidth(max(0, self.width() - 2 * margin))
        self.toast.adjustSize()
        floor = self.height() - 8
        self.toast.move(margin, max(0, floor - self.toast.height() - 6))

    def resizeEvent(self, event: QEvent) -> None:  # noqa: N802 (Qt name)
        super().resizeEvent(event)  # type: ignore[arg-type]
        if self.toast.is_active:
            self._place_toast()

    def delete_selected(self) -> None:
        row = self.selected_row()
        if row is not None:
            self.delete(row)

    def toggle_pause(self) -> None:
        paused = self._backend.toggle_pause()
        self.refresh()
        self.pause_changed.emit(bool(paused))

    # --- Keys ---

    def _quick_key(self, event: QEvent) -> bool:
        """Alt+1 to Alt+9 paste the numbered card. True if `event` was one of them."""
        key = event.key()  # type: ignore[attr-defined]
        held = event.modifiers()  # type: ignore[attr-defined]
        if held != Qt.KeyboardModifier.AltModifier:
            return False
        number = key - Qt.Key.Key_0.value
        if not 1 <= number <= QUICK_PASTE_COUNT:
            return False
        self.quick_paste(number)
        return True

    def _navigate(self, key: int) -> bool:
        """Up, Down and Enter act on the cards. True if `key` was one of them."""
        if key == Qt.Key.Key_Down:
            self._move_selection(1)
        elif key == Qt.Key.Key_Up:
            self._move_selection(-1)
        elif key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self.choose_selected()
        else:
            return False
        return True

    def keyPressEvent(self, event: QEvent) -> None:  # noqa: N802 (Qt name)
        key = event.key()  # type: ignore[attr-defined]
        # Esc pressed anywhere else in the window: close an open strip first.
        if key == Qt.Key.Key_Escape:
            if self.delegate.actions_item is not None and self.delegate.is_open(self.delegate.actions_item):
                self.close_actions()
            else:
                self.hide()
            return
        if not self.search.isVisibleTo(self):
            if self._quick_key(event) or self._navigate(key):
                return
            text = event.text()  # type: ignore[attr-defined]
            held = event.modifiers()  # type: ignore[attr-defined]
            control = Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.AltModifier
            if text and text.isprintable() and not held & control:
                self.show_search(text)
                return
        super().keyPressEvent(event)  # type: ignore[arg-type]

    def _drag_scroll(self, watched: QObject, event: QEvent) -> bool:
        """Drag the chip strip sideways with the left button (touch screens, no wheel).

        True if the event was used up: moves while dragging, and the release
        that ends a drag (so the chip under the pointer is not also clicked).
        """
        sources = (self.chips_scroll.viewport(), self.chips_bar, *self.chips.values())
        if watched not in sources:
            return False
        kind = event.type()
        bar = self.chips_scroll.horizontalScrollBar()
        if kind == QEvent.Type.MouseButtonPress and event.button() == Qt.MouseButton.LeftButton:  # type: ignore[attr-defined]
            self._drag_start = (event.globalPosition().x(), bar.value())  # type: ignore[attr-defined]
            self._dragging = False
        elif kind == QEvent.Type.MouseMove and self._drag_start is not None:
            moved = event.globalPosition().x() - self._drag_start[0]  # type: ignore[attr-defined]
            if self._dragging or abs(moved) > 6:
                self._dragging = True
                bar.setValue(round(self._drag_start[1] - moved))
                return True
        elif kind == QEvent.Type.MouseButtonRelease:
            was_dragging = self._dragging
            self._drag_start, self._dragging = None, False
            return was_dragging
        return False

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:  # noqa: N802 (Qt name)
        if not self._built:
            return False  # building the window sends events to parts that are not made yet
        if watched is self.chips_scroll.viewport() and event.type() == QEvent.Type.Resize:
            self.update_chip_fades()
        if watched is self.chips_scroll.viewport() and event.type() == QEvent.Type.Wheel:
            # A mouse wheel turns vertically, a touchpad swipe sideways: use whichever is larger.
            delta = event.angleDelta()  # type: ignore[attr-defined]
            step = delta.x() if abs(delta.x()) > abs(delta.y()) else delta.y()
            bar = self.chips_scroll.horizontalScrollBar()
            bar.setValue(bar.value() - step)
            return True
        if watched is self.handle and self._drag_window(event):
            return True
        if self._drag_scroll(watched, event):
            return True
        if watched is self.search and event.type() in (QEvent.Type.Resize, QEvent.Type.Show):
            self._place_search_clear()
        if watched is self.search and event.type() == QEvent.Type.KeyPress:
            key = event.key()  # type: ignore[attr-defined]
            if self._quick_key(event) or self._navigate(key):
                return True
            if key == Qt.Key.Key_Escape:
                if self.delegate.actions_item is not None and self.delegate.is_open(self.delegate.actions_item):
                    self.close_actions()
                elif self.search.text():
                    self.search.clear()
                else:
                    self.hide()
                return True
        return super().eventFilter(watched, event)


def _round_corners(window: QWidget) -> None:
    """Ask Windows 11 to round the frameless window's corners. Ignored elsewhere."""
    if QGuiApplication.platformName() != "windows":
        return
    try:
        import ctypes

        preference = ctypes.c_int(2)  # DWMWCP_ROUND
        ctypes.windll.dwmapi.DwmSetWindowAttribute(  # type: ignore[attr-defined]
            int(window.winId()), 33, ctypes.byref(preference), ctypes.sizeof(preference)
        )  # 33 = DWMWA_WINDOW_CORNER_PREFERENCE; fails harmlessly before Windows 11
    except (OSError, AttributeError):
        pass
