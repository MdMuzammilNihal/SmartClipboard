"""The designed empty screens of the clipboard window (UI plan M3).

One small calm layout for four cases (what to say is decided in
view_model.empty_state): a round icon drawn in code, a short title, a few
lines of text, and one or two buttons. Colours follow the purple theme in
both light and dark; the paused screen uses the same red as the pause banner.

SECURITY REVIEW (owner): fixed wording only. Nothing here receives clipboard
content: not an item, not the search text. Icons are painted with
QPainter, so no font or image file is needed.
"""

from PySide6.QtCore import QPointF, QRectF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

from smartclip.ui import theme as themes
from smartclip.ui.view_model import (
    ACTION_LABELS,
    EMPTY_NO_HISTORY,
    EMPTY_NO_MATCH,
    EMPTY_PAUSED,
    EMPTY_SECRETS_ONLY,
    EmptyState,
)

ICON_SIZE = 64  # logical pixels: the round background; the drawing is about half of it
TEXT_WIDTH = 290  # the title and text wrap at this width (the window is 380 wide)


def icon_colours(theme: themes.Theme, kind: str) -> tuple[QColor, QColor]:
    """(background, drawing) colours for an icon: the pause banner's red when paused."""
    if kind == EMPTY_PAUSED:
        return QColor(theme.banner_background), QColor(theme.banner_text)
    return QColor(theme.tint1), QColor(theme.accent)


def paint_icon(painter: QPainter, kind: str, box: QRectF, ink: QColor) -> None:
    """Draw the icon for `kind` inside `box` (about 30 px square), outline style."""
    painter.save()
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    pen = QPen(ink, max(1.8, box.width() / 14))
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    painter.setPen(pen)
    painter.setBrush(Qt.BrushStyle.NoBrush)
    x, y, w, h = box.x(), box.y(), box.width(), box.height()
    if kind == EMPTY_NO_MATCH:  # a magnifier
        radius = w * 0.34
        centre = QPointF(x + w * 0.42, y + h * 0.42)
        painter.drawEllipse(centre, radius, radius)
        painter.drawLine(
            QPointF(centre.x() + radius * 0.72, centre.y() + radius * 0.72),
            QPointF(x + w * 0.92, y + h * 0.92),
        )
    elif kind == EMPTY_PAUSED:  # two bars
        bar_w = w * 0.2
        for left in (x + w * 0.2, x + w * 0.6):
            painter.drawRoundedRect(QRectF(left, y + h * 0.12, bar_w, h * 0.76), bar_w / 2, bar_w / 2)
    elif kind == EMPTY_SECRETS_ONLY:  # a closed lock
        body = QRectF(x + w * 0.14, y + h * 0.46, w * 0.72, h * 0.46)
        painter.drawRoundedRect(body, w * 0.1, w * 0.1)
        shackle = QPainterPath()
        shackle.moveTo(x + w * 0.28, y + h * 0.46)
        shackle.lineTo(x + w * 0.28, y + h * 0.34)
        shackle.arcTo(QRectF(x + w * 0.28, y + h * 0.06, w * 0.44, h * 0.56), 180, -180)
        shackle.lineTo(x + w * 0.72, y + h * 0.46)
        painter.drawPath(shackle)
    else:  # a clipboard (also the fallback)
        board = QRectF(x + w * 0.14, y + h * 0.14, w * 0.72, h * 0.8)
        painter.drawRoundedRect(board, w * 0.1, w * 0.1)
        painter.setBrush(ink)
        clip = QRectF(x + w * 0.34, y + h * 0.04, w * 0.32, h * 0.18)
        painter.drawRoundedRect(clip, w * 0.06, w * 0.06)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        for fraction in (0.46, 0.64):
            painter.drawLine(
                QPointF(x + w * 0.3, y + h * fraction), QPointF(x + w * 0.7, y + h * fraction)
            )
    painter.restore()


class EmptyIcon(QWidget):
    """The round icon at the top of an empty screen."""

    def __init__(self) -> None:
        super().__init__()
        self.kind = EMPTY_NO_HISTORY
        self.theme = themes.LIGHT
        self.setFixedSize(ICON_SIZE, ICON_SIZE)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)

    def set_look(self, kind: str, theme: themes.Theme) -> None:
        self.kind, self.theme = kind, theme
        self.update()

    def sizeHint(self) -> QSize:  # noqa: N802 (Qt name)
        return QSize(ICON_SIZE, ICON_SIZE)

    def paintEvent(self, event: object) -> None:  # noqa: N802 (Qt name)
        background, ink = icon_colours(self.theme, self.kind)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(background)
        painter.drawEllipse(self.rect())
        inset = ICON_SIZE * 0.29
        paint_icon(painter, self.kind, QRectF(inset, inset, ICON_SIZE - 2 * inset, ICON_SIZE - 2 * inset), ink)


class EmptyView(QWidget):
    """Shows one EmptyState, centred. Buttons report their ACTION_* id."""

    action_triggered = Signal(str)

    def __init__(self) -> None:
        super().__init__()
        self.state: EmptyState | None = None
        self.icon = EmptyIcon()
        self.title_label = QLabel()
        self.title_label.setObjectName("emptyTitle")
        self.title_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.title_label.setWordWrap(True)
        self.body_label = QLabel()
        self.body_label.setObjectName("emptyBody")
        self.body_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.body_label.setWordWrap(True)
        for label in (self.title_label, self.body_label):
            label.setFixedWidth(TEXT_WIDTH)  # a fixed width keeps wrapped text from being cut
        self.buttons_row = QHBoxLayout()
        self.buttons_row.setSpacing(8)
        self.buttons: dict[str, QPushButton] = {}

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 8, 16, 8)
        layout.setSpacing(10)
        layout.addStretch(2)
        layout.addWidget(self.icon, 0, Qt.AlignmentFlag.AlignHCenter)
        layout.addSpacing(4)
        layout.addWidget(self.title_label, 0, Qt.AlignmentFlag.AlignHCenter)
        layout.addWidget(self.body_label, 0, Qt.AlignmentFlag.AlignHCenter)
        layout.addSpacing(6)
        layout.addLayout(self.buttons_row)
        layout.addStretch(3)

    def show_state(self, state: EmptyState, theme: themes.Theme) -> None:
        """Show `state`; rebuilds the buttons only when the set of actions changes."""
        if state != self.state:
            self.state = state
            self.title_label.setText(state.title)
            self.body_label.setText(state.body)
            self._set_actions(state.actions)
            self.setAccessibleName(state.title)
        self.icon.set_look(state.kind, theme)

    def _set_actions(self, actions: tuple[str, ...]) -> None:
        while (entry := self.buttons_row.takeAt(0)) is not None:
            widget = entry.widget()
            if widget is not None:
                widget.hide()
                widget.deleteLater()
        self.buttons = {}
        self.buttons_row.addStretch(1)
        for index, action in enumerate(actions):
            button = QPushButton(ACTION_LABELS[action])
            # The first button is the main one (filled); a second one is plain.
            button.setObjectName("emptyAction" if index == 0 else "emptyActionPlain")
            button.setFocusPolicy(Qt.FocusPolicy.NoFocus)  # the search box keeps the keys
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.setAccessibleName(ACTION_LABELS[action])
            button.clicked.connect(lambda _checked=False, a=action: self.action_triggered.emit(a))
            self.buttons_row.addWidget(button)
            self.buttons[action] = button
        self.buttons_row.addStretch(1)
