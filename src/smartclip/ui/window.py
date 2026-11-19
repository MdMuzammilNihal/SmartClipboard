"""The clipboard window (tasks 9 and 10), drawn with PySide6 in a Win+V style.

Owner's decisions (2026-11-19): a narrow panel that opens next to the mouse
pointer, one card per item, choosing an item pastes it into the app that
was in front (ui/paste.py; copy only if no Paster is given), item actions
in a card menu and on right-click, and light or dark colours following
Windows (ui/theme.py).

Layout, top to bottom: the search box (focused whenever the window opens)
with the pause and "more" buttons, a "Capture paused" banner while paused,
a row of filter chips (All, then each badge present in the history), then
the cards. Each card shows the type badge, the state badge ("Secret" /
"Review?"), the source app, the age, a pin mark, a "..." menu button, and
up to three lines of the item's text.

Mouse and keys:
- Click a card (or press Enter) to paste it. Up and Down move the
  selection, Esc clears the search or hides the window.
- Click a card's type or state badge, or a chip, to filter by it.
- Click a masked text to reveal it for 10 seconds (Ctrl+R does the same for
  the selected card); click it again to mask it.
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
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Protocol

from PySide6.QtCore import (
    QAbstractListModel,
    QEvent,
    QModelIndex,
    QObject,
    QPersistentModelIndex,
    QPoint,
    QRect,
    QSize,
    Qt,
    QTimer,
    Signal,
)
from PySide6.QtGui import (
    QAction,
    QColor,
    QCursor,
    QFont,
    QFontMetrics,
    QGuiApplication,
    QKeySequence,
    QPainter,
    QPen,
    QShortcut,
    QTextLayout,
    QTextOption,
)
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListView,
    QMenu,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QToolButton,
    QToolTip,
    QVBoxLayout,
    QWidget,
)

from smartclip.labels import Label, SecretState
from smartclip.storage import StoredItem
from smartclip.ui import theme as themes
from smartclip.ui.paste import Paster
from smartclip.ui.view_model import (
    LABEL_BADGES,
    STATE_BADGES,
    Badge,
    Filter,
    RevealState,
    Row,
    build_rows,
    filter_name,
)

WINDOW_SIZE = QSize(380, 520)
# The role under which the model hands the whole Row to the delegate.
ROW_ROLE = Qt.ItemDataRole.UserRole + 1
# Ages are redrawn this often (in seconds) while the window is open.
_AGE_REFRESH_TICKS = 30
MAX_BODY_LINES = 3
DELETE_QUESTION = "Delete this item? This cannot be undone."
EMPTY_TEXT = "Nothing here yet. Copy some text and it will show up here."
NO_MATCH_TEXT = "No matching items."
REVEAL_HINT = "Click to reveal"
HIDE_HINT = "Click to hide"

# Card geometry, in pixels.
_OUTER_X, _OUTER_Y, _PAD, _GAP, _BADGE_PAD_X = 8, 4, 10, 6, 7

# Icon glyphs from the Windows icon fonts (Windows 11, then Windows 10).
_ICON_FONTS = ["Segoe Fluent Icons", "Segoe MDL2 Assets"]
_GLYPH_PAUSE, _GLYPH_PLAY, _GLYPH_MORE, _GLYPH_PIN = "", "", "", ""

# Regions of a card that react to a click.
REGION_CARD, REGION_LABEL, REGION_STATE, REGION_MENU, REGION_TEXT = (
    "card", "label", "state", "menu", "text",
)


class WindowBackend(Protocol):
    """What the window needs from the app. Called on the UI thread only."""

    def items(self) -> list[StoredItem]: ...
    def copy_back(self, item_id: int) -> bool: ...
    def pin(self, item_id: int) -> bool: ...
    def unpin(self, item_id: int) -> bool: ...
    def delete(self, item_id: int) -> bool: ...
    def mark_normal(self, item_id: int) -> bool: ...
    def mark_secret(self, item_id: int) -> bool: ...
    def is_paused(self) -> bool: ...
    def toggle_pause(self) -> bool: ...
    def quit(self) -> None: ...


class Switch(Protocol):
    """An on/off setting shown as a tick-box item in the "..." menu (autostart.py)."""

    label: str

    def is_on(self) -> bool: ...
    def set_on(self, on: bool) -> bool: ...


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _ask_delete(parent: QWidget) -> bool:
    answer = QMessageBox.question(parent, "Delete item", DELETE_QUESTION)
    return answer == QMessageBox.StandardButton.Yes


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
    pin: QRect | None
    top_text: QRect  # source app and age, between the badges and the icons
    body: QRect
    text: QRect | None  # the clickable masked or revealed text


def _badge_width(badge: Badge, metrics: QFontMetrics) -> int:
    return metrics.horizontalAdvance(badge.text) + 2 * _BADGE_PAD_X


def card_rects(rect: QRect, row: Row, metrics: QFontMetrics) -> CardRects:
    """Lay out one card inside the list cell `rect`."""
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
    menu = QRect(inner.right() - badge_height + 1, inner.top(), badge_height, badge_height)
    pin = None
    right_edge = menu.left() - 2
    if row.pinned:
        pin = QRect(menu.left() - badge_height - 2, inner.top(), badge_height, badge_height)
        right_edge = pin.left() - 2
    top_text = QRect(
        right_of_badges + 8, inner.top(), max(0, right_edge - right_of_badges - 8), badge_height
    )
    body = QRect(
        inner.left(),
        inner.top() + badge_height + _GAP,
        inner.width(),
        max(0, inner.bottom() - inner.top() - badge_height - _GAP + 1),
    )
    text = None
    if row.masked:
        hint_width = metrics.horizontalAdvance(f"{row.preview}   {REVEAL_HINT}")
        text = QRect(body.left(), body.top(), min(hint_width, body.width()), metrics.lineSpacing())
    elif row.revealed:
        text = body
    return CardRects(card, label, state, menu, pin, top_text, body, text)


def show_label_badge(row: Row) -> bool:
    """False for a "possible secret" label next to a Secret or Review? badge (same news)."""
    return not (row.label is Label.POSSIBLE_SECRET and row.state_badge is not None)


def region_at(rects: CardRects, point: QPoint) -> str:
    """Which part of a card a click at `point` landed on."""
    if rects.menu.contains(point):
        return REGION_MENU
    if rects.label is not None and rects.label.contains(point):
        return REGION_LABEL
    if rects.state is not None and rects.state.contains(point):
        return REGION_STATE
    if rects.text is not None and rects.text.contains(point):
        return REGION_TEXT
    return REGION_CARD


def card_height(row: Row, font: QFont, width: int) -> int:
    """The height of one list cell holding this row's card."""
    metrics = QFontMetrics(font)
    body_width = width - 2 * _OUTER_X - 2 * _PAD
    lines = 1 if row.masked else len(wrap_lines(row.preview, font, body_width, MAX_BODY_LINES))
    badge_height = metrics.height() + 4
    return 2 * _OUTER_Y + 2 * _PAD + badge_height + _GAP + lines * metrics.lineSpacing()


class CardsModel(QAbstractListModel):
    """Holds the current rows for the list view."""

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._rows: list[Row] = []

    @property
    def rows(self) -> list[Row]:
        return self._rows

    def set_rows(self, rows: list[Row]) -> None:
        self.beginResetModel()
        self._rows = rows
        self.endResetModel()

    def row_at(self, index: int) -> Row | None:
        return self._rows[index] if 0 <= index < len(self._rows) else None

    def rowCount(self, parent: QModelIndex | QPersistentModelIndex = QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self._rows)

    def data(
        self, index: QModelIndex | QPersistentModelIndex, role: int = Qt.ItemDataRole.DisplayRole
    ) -> object:
        row = self.row_at(index.row())
        if row is None:
            return None
        if role == ROW_ROLE:
            return row
        if role in (Qt.ItemDataRole.DisplayRole, Qt.ItemDataRole.AccessibleTextRole):
            # The bullet mask for masked rows; the text itself never leaves here.
            return row.preview
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

    def sizeHint(  # noqa: N802 (Qt name)
        self, option: QStyleOptionViewItem, index: QModelIndex | QPersistentModelIndex
    ) -> QSize:
        row = index.data(ROW_ROLE)
        width = self._view.viewport().width()
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
        if not isinstance(row, Row):
            return
        t = self.theme
        metrics = option.fontMetrics
        rects = card_rects(option.rect, row, metrics)
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        hovered = bool(option.state & QStyle.StateFlag.State_MouseOver)
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        background = t.card_selected if selected else t.card_hover if hovered else t.card
        painter.setBrush(QColor(background))
        painter.setPen(QPen(QColor(t.accent if selected else t.border), 1))
        painter.drawRoundedRect(rects.card.adjusted(0, 0, -1, -1), 8, 8)

        if rects.label is not None:
            self._badge(painter, rects.label, row.label_badge)
        if rects.state is not None and row.state_badge is not None:
            self._badge(painter, rects.state, row.state_badge)

        painter.setPen(QColor(t.muted))
        age_width = metrics.horizontalAdvance(row.age) + 4
        source_rect = rects.top_text.adjusted(0, 0, -age_width, 0)
        source = metrics.elidedText(row.source_app, Qt.TextElideMode.ElideRight, source_rect.width())
        align = Qt.AlignmentFlag.AlignVCenter
        painter.drawText(source_rect, align | Qt.AlignmentFlag.AlignLeft, source)
        painter.drawText(rects.top_text, align | Qt.AlignmentFlag.AlignRight, row.age)

        painter.setFont(self.icon_font)
        center = Qt.AlignmentFlag.AlignCenter
        if rects.pin is not None:
            painter.setPen(QColor(t.accent))
            painter.drawText(rects.pin, center, _GLYPH_PIN)
        painter.setPen(QColor(t.muted))
        painter.drawText(rects.menu, center, _GLYPH_MORE)
        painter.setFont(option.font)

        top_left = Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop
        if row.masked:
            painter.setPen(QColor(t.text))
            painter.drawText(rects.body, top_left, row.preview)
            painter.setPen(QColor(t.muted))
            hint_x = metrics.horizontalAdvance(row.preview + "   ")
            painter.drawText(rects.body.adjusted(hint_x, 0, 0, 0), top_left, REVEAL_HINT)
        else:
            painter.setPen(QColor(t.text))
            lines = wrap_lines(row.preview, option.font, rects.body.width(), MAX_BODY_LINES)
            y = rects.body.top()
            for line in lines:
                line_rect = QRect(rects.body.left(), y, rects.body.width(), metrics.lineSpacing())
                painter.drawText(line_rect, top_left, line)
                y += metrics.lineSpacing()
        painter.restore()

    def _badge(self, painter: QPainter, rect: QRect, badge: Badge) -> None:
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(badge.background))
        painter.drawRoundedRect(rect, 6, 6)
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
                region = region_at(card_rects(option.rect, row, option.fontMetrics), point)
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
        tip = card_tooltip(row, region_at(card_rects(option.rect, row, option.fontMetrics), point))
        if tip:
            QToolTip.showText(event.globalPos(), tip, view)  # type: ignore[attr-defined]
        else:
            QToolTip.hideText()
        return True


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
    return ""


def present_filters(items: list[StoredItem]) -> list[Filter]:
    """The chips to offer: secret states, then content types, as found in `items`."""
    states = {item.payload.secret_state for item in items}
    labels = {item.payload.label for item in items}
    found: list[Filter] = [state for state in STATE_BADGES if state in states]
    found += [label for label in LABEL_BADGES if label in labels]
    return found


class SearchWindow(QWidget):
    """The window opened by Ctrl+Alt+V."""

    def __init__(
        self,
        backend: WindowBackend,
        now: Callable[[], datetime] = _utc_now,
        reveals: RevealState | None = None,
        confirm_delete: Callable[[QWidget], bool] = _ask_delete,
        active_window: Callable[[], QWidget | None] = QApplication.activeWindow,
        paster: Paster | None = None,
        current_theme: Callable[[], themes.Theme] = themes.current_theme,
        switches: list[Switch] | None = None,
    ) -> None:
        super().__init__()
        self._backend = backend
        self._now = now
        self.reveals = reveals or RevealState()
        self._confirm_delete = confirm_delete
        self._active_window = active_window
        self._paster = paster
        self._current_theme = current_theme
        self._filter: Filter | None = None
        self._chip_filters: list[Filter] = []
        self._ticks = 0
        self._corners_rounded = False

        self.setWindowTitle("Smart Clipboard")
        self.setWindowFlags(
            Qt.WindowType.Tool
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
        )
        self.resize(WINDOW_SIZE)

        self.search = QLineEdit()
        self.search.setObjectName("search")
        self.search.setPlaceholderText("Search")
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(self.refresh)
        self.search.installEventFilter(self)

        icon_font = QFont()
        icon_font.setFamilies(_ICON_FONTS)
        self.pause_button = QToolButton()
        self.pause_button.setFont(icon_font)
        self.pause_button.clicked.connect(self.toggle_pause)
        self.more_button = QToolButton()
        self.more_button.setFont(icon_font)
        self.more_button.setText(_GLYPH_MORE)
        self.more_button.setToolTip("More")
        self.more_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        more_menu = QMenu(self)
        self.pause_action = more_menu.addAction("Pause capture", self.toggle_pause)
        self.switch_actions = []
        if switches:
            more_menu.addSeparator()
        for switch in switches or []:
            action = more_menu.addAction(switch.label)
            action.setCheckable(True)
            action.triggered.connect(
                lambda checked, sw=switch, act=action: self._set_switch(sw, act, checked)
            )
            self.switch_actions.append((switch, action))
        more_menu.aboutToShow.connect(self.sync_switches)
        more_menu.addSeparator()
        self.quit_action = more_menu.addAction("Quit Smart Clipboard", self._backend.quit)
        self.more_button.setMenu(more_menu)

        self.pause_label = QLabel("Capture paused: new copies are not saved")
        self.pause_label.setObjectName("banner")

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
        self.chips_scroll = chips_scroll
        chips_scroll.setFrameShape(QFrame.Shape.NoFrame)

        self.model = CardsModel(self)
        self.list = QListView()
        self.list.setModel(self.model)
        self.delegate = CardDelegate(self.list)
        self.list.setItemDelegate(self.delegate)
        self.list.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.list.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.list.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.list.setResizeMode(QListView.ResizeMode.Adjust)
        self.list.setMouseTracking(True)
        self.list.setFocusPolicy(Qt.FocusPolicy.NoFocus)  # keys go to the search box
        self.list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.list.customContextMenuRequested.connect(self._on_context_menu)
        self.delegate.region_clicked.connect(self._on_region_clicked)

        self.empty_label = QLabel(EMPTY_TEXT)
        self.empty_label.setObjectName("muted")
        self.empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty_label.setWordWrap(True)
        self.status_label = QLabel()
        self.status_label.setObjectName("status")
        self.status_label.setWordWrap(True)

        panel = QFrame()
        panel.setObjectName("panel")
        top = QHBoxLayout()
        top.setSpacing(4)
        top.addWidget(self.search, 1)
        top.addWidget(self.pause_button)
        top.addWidget(self.more_button)
        inner = QVBoxLayout(panel)
        inner.setContentsMargins(10, 10, 10, 8)
        inner.setSpacing(6)
        inner.addLayout(top)
        inner.addWidget(self.pause_label)
        inner.addWidget(chips_scroll)
        inner.addWidget(self.list, 1)
        inner.addWidget(self.empty_label, 1)
        inner.addWidget(self.status_label)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(panel)

        self.shortcuts: dict[str, QShortcut] = {}
        for keys, action in (
            ("Ctrl+R", self.toggle_reveal_selected),
            ("Ctrl+P", self.toggle_pin_selected),
            ("Shift+Del", self.delete_selected),
            ("Shift+F10", self.open_menu_for_selected),
            ("Menu", self.open_menu_for_selected),
        ):
            self.shortcuts[keys] = QShortcut(QKeySequence(keys), self, activated=action)

        self._timer = QTimer(self)
        self._timer.setInterval(1000)
        self._timer.timeout.connect(self._tick)

        self.apply_theme()
        self.refresh()

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
        self.apply_theme()
        self.refresh()
        self.move_near_pointer()
        self.show()
        self.raise_()
        self.activateWindow()
        self.search.setFocus()

    def apply_theme(self) -> None:
        """Use the light or dark colours that match Windows right now."""
        theme = self._current_theme()
        self.delegate.theme = theme
        self.setStyleSheet(themes.stylesheet(theme))
        self.list.viewport().update()

    def move_near_pointer(self) -> None:
        pointer = QCursor.pos()
        screen = QGuiApplication.screenAt(pointer) or QGuiApplication.primaryScreen()
        if screen is not None:
            self.move(place_near(pointer, self.size(), screen.availableGeometry()))

    def showEvent(self, event: QEvent) -> None:  # noqa: N802 (Qt name)
        self._ticks = 0
        self._timer.start()
        if not self._corners_rounded:
            self._corners_rounded = True
            _round_corners(self)
        super().showEvent(event)

    def hideEvent(self, event: QEvent) -> None:  # noqa: N802 (Qt name)
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
        self.refresh()

    def refresh(self) -> None:
        """Rebuild the cards from the backend, keeping the selected item selected."""
        selected = self.selected_row()
        items = self._backend.items()
        rows = build_rows(items, self.search.text(), self._filter, self.reveals, self._now())
        self.model.set_rows(rows)
        target = 0
        if selected is not None:
            for position, row in enumerate(rows):
                if row.item_id == selected.item_id:
                    target = position
                    break
        if rows:
            self.list.setCurrentIndex(self.model.index(target))
        self.list.setVisible(bool(rows))
        self.empty_label.setVisible(not rows)
        self.empty_label.setText(EMPTY_TEXT if not items else NO_MATCH_TEXT)
        self._update_chips(items)
        paused = self._backend.is_paused()
        self.pause_label.setVisible(paused)
        self.pause_button.setText(_GLYPH_PLAY if paused else _GLYPH_PAUSE)
        self.pause_button.setToolTip("Resume capture" if paused else "Pause capture")
        self.pause_action.setText("Resume capture" if paused else "Pause capture")
        self.status_label.setVisible(bool(self.status_label.text()))

    def _update_chips(self, items: list[StoredItem]) -> None:
        wanted = present_filters(items)
        if self._filter is not None and self._filter not in wanted:
            wanted.append(self._filter)  # so an active filter can always be cleared
        if wanted != self._chip_filters:
            self._chip_filters = wanted
            for chip in self.chips.values():
                self.chips_layout.removeWidget(chip)
                chip.deleteLater()
            self.chips = {}
            for active in [None, *wanted]:
                chip = QPushButton("All" if active is None else filter_name(active))
                chip.setProperty("chip", True)
                chip.setCheckable(True)
                chip.setFocusPolicy(Qt.FocusPolicy.NoFocus)
                chip.clicked.connect(lambda _checked=False, f=active: self.set_filter(f))
                self.chips_layout.addWidget(chip)
                self.chips[active] = chip
            self.chips_layout.addStretch(1)
        for active, chip in self.chips.items():
            chip.setChecked(active == self._filter)

    def selected_row(self) -> Row | None:
        index = self.list.currentIndex()
        return self.model.row_at(index.row()) if index.isValid() else None

    def select_item(self, item_id: int) -> bool:
        for position, row in enumerate(self.model.rows):
            if row.item_id == item_id:
                self.list.setCurrentIndex(self.model.index(position))
                return True
        return False

    def _move_selection(self, step: int) -> None:
        count = self.model.rowCount()
        if count == 0:
            return
        current = self.list.currentIndex().row()
        position = max(0, min(count - 1, current + step))
        self.list.setCurrentIndex(self.model.index(position))

    def _tick(self) -> None:
        self._ticks += 1
        expired = self.reveals.expire(self._now())
        if expired or self._ticks % _AGE_REFRESH_TICKS == 0:
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
        elif region == REGION_MENU:
            self.open_menu(row, global_pos or QCursor.pos())
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
        menu.addAction("Delete", lambda: self.delete(row))
        return menu

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
        if not self._confirm_delete(self):
            return
        self._backend.delete(row.item_id)
        self.reveals.hide(row.item_id)
        self.refresh()

    def delete_selected(self) -> None:
        row = self.selected_row()
        if row is not None:
            self.delete(row)

    def toggle_pause(self) -> None:
        self._backend.toggle_pause()
        self.refresh()

    def sync_switches(self) -> None:
        """Tick each switch item to match the real setting (read when the menu opens)."""
        for switch, action in self.switch_actions:
            action.setChecked(switch.is_on())

    def _set_switch(self, switch: Switch, action: QAction, on: bool) -> None:
        if not switch.set_on(on):
            self.status_label.setText(f"Could not change \"{switch.label}\"")
            self.status_label.setVisible(True)
        action.setChecked(switch.is_on())

    # --- Keys ---

    def keyPressEvent(self, event: QEvent) -> None:  # noqa: N802 (Qt name)
        # Esc pressed anywhere else in the window.
        if event.key() == Qt.Key.Key_Escape:  # type: ignore[attr-defined]
            self.hide()
            return
        super().keyPressEvent(event)  # type: ignore[arg-type]

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:  # noqa: N802 (Qt name)
        if watched is self.chips_scroll.viewport() and event.type() == QEvent.Type.Wheel:
            bar = self.chips_scroll.horizontalScrollBar()
            bar.setValue(bar.value() - event.angleDelta().y())  # type: ignore[attr-defined]
            return True
        if watched is self.search and event.type() == QEvent.Type.KeyPress:
            key = event.key()  # type: ignore[attr-defined]
            if key == Qt.Key.Key_Down:
                self._move_selection(1)
                return True
            if key == Qt.Key.Key_Up:
                self._move_selection(-1)
                return True
            if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
                self.choose_selected()
                return True
            if key == Qt.Key.Key_Escape:
                if self.search.text():
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
