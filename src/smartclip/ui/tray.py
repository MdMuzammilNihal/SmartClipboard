"""The notification-area (tray) icon (UI plan M3, owner's request 2026-11-26).

What it gives the owner: proof that the app is running (it can start hidden
at login), a paused look, one click to open the window, and a small menu with
Open, Pause or Resume capture, and Quit. Quit used to be only in the settings.

Behaviour (decisions made here, to be confirmed by the owner):

- The icon stays while the app runs, whether the window is shown or hidden.
  It is removed at Quit so Windows does not keep a ghost icon.
- A left click opens the window; the menu is on the right click.
- If Windows has no notification area (rare), the app runs exactly as before.

SECURITY REVIEW (owner): everything shown here is fixed wording. The tooltip,
the menu and the one start-up message never contain clipboard content, item
ids or window titles. No notification is ever sent about a copied item.
"""

from collections.abc import Callable

from PySide6.QtCore import QObject, QRectF, Qt
from PySide6.QtGui import QAction, QColor, QFont, QIcon, QPainter, QPen, QPixmap
from PySide6.QtWidgets import QApplication, QMenu, QSystemTrayIcon

from smartclip import logsafe
from smartclip.ui import theme as themes

_log = logsafe.get_logger("tray")

ICON_SIZES = (16, 20, 24, 32, 48, 64)
_RUNNING_FILL = themes.LIGHT.accent  # the app's purple
_PAUSED_FILL = "#7a828c"  # grey: nothing is being captured

OPEN_TEXT = "Open Smart Clipboard"
PAUSE_TEXT = "Pause capture"
RESUME_TEXT = "Resume capture"
QUIT_TEXT = "Quit"
RUNNING_TIP = "Smart Clipboard"
PAUSED_TIP = "Smart Clipboard (capture paused)"
HOTKEYS_TITLE = "Smart Clipboard"
HOTKEYS_TEXT = "Some hotkeys are used by another app: "


def draw_icon(size: int, paused: bool) -> QPixmap:
    """One icon image: a clipboard on a rounded square, bars on it while paused.

    Drawn in code (no image file, no new dependency). A filled square keeps it
    visible on both a light and a dark taskbar.
    """
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    fill = QColor(_PAUSED_FILL if paused else _RUNNING_FILL)
    white = QColor("#ffffff")
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(fill)
    corner = size * 0.24
    painter.drawRoundedRect(QRectF(0, 0, size, size), corner, corner)

    # The clipboard board, with its clip across the top edge.
    board = QRectF(size * 0.26, size * 0.22, size * 0.48, size * 0.58)
    painter.setBrush(white)
    painter.drawRoundedRect(board, size * 0.07, size * 0.07)
    clip = QRectF(size * 0.38, size * 0.14, size * 0.24, size * 0.16)
    painter.setPen(QPen(fill, max(1.0, size * 0.045)))
    painter.drawRoundedRect(clip, size * 0.05, size * 0.05)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(fill)
    if paused:  # two pause bars on the board
        bar_w, bar_h = size * 0.08, size * 0.24
        top = size * 0.40
        painter.drawRoundedRect(QRectF(size * 0.37, top, bar_w, bar_h), 1, 1)
        painter.drawRoundedRect(QRectF(size * 0.55, top, bar_w, bar_h), 1, 1)
    else:  # two text lines on the board
        for y in (0.44, 0.58):
            painter.drawRoundedRect(QRectF(size * 0.34, size * y, size * 0.32, size * 0.055), 1, 1)
    painter.end()
    return pixmap


def make_icon(paused: bool) -> QIcon:
    """The tray icon in every size Windows may ask for."""
    icon = QIcon()
    for size in ICON_SIZES:
        icon.addPixmap(draw_icon(size, paused))
    return icon


class TrayIcon(QObject):
    """The icon, its tooltip and its right-click menu. UI thread only."""

    def __init__(
        self,
        is_paused: Callable[[], bool],
        open_window: Callable[[], None],
        toggle_pause: Callable[[], None],
        quit_app: Callable[[], None],
        current_theme: Callable[[], themes.Theme] = themes.current_theme,
        available: Callable[[], bool] = QSystemTrayIcon.isSystemTrayAvailable,
    ) -> None:
        super().__init__()
        self._is_paused = is_paused
        self._open_window = open_window
        self._toggle_pause = toggle_pause
        self._quit_app = quit_app
        self._current_theme = current_theme
        self._available = available

        self.icon = QSystemTrayIcon(self)
        self.menu = QMenu()
        self.open_action = QAction(OPEN_TEXT, self.menu)
        font = QFont(self.open_action.font())
        font.setBold(True)  # the default action: what a click does
        self.open_action.setFont(font)
        self.pause_action = QAction(PAUSE_TEXT, self.menu)
        self.quit_action = QAction(QUIT_TEXT, self.menu)
        self.menu.addAction(self.open_action)
        self.menu.addSeparator()
        self.menu.addAction(self.pause_action)
        self.menu.addSeparator()
        self.menu.addAction(self.quit_action)
        self.icon.setContextMenu(self.menu)

        self.open_action.triggered.connect(lambda _checked=False: self._open_window())
        self.pause_action.triggered.connect(lambda _checked=False: self._toggle_pause())
        self.quit_action.triggered.connect(lambda _checked=False: self._quit_app())
        self.icon.activated.connect(self.on_activated)
        self.menu.aboutToShow.connect(self.sync)
        self._icons = {False: make_icon(False), True: make_icon(True)}
        self.sync()

    @property
    def available(self) -> bool:
        """True if Windows can show the icon."""
        return self._available()

    def start(self) -> bool:
        """Show the icon. False (and the app carries on) if there is no tray."""
        if not self.available:
            logsafe.log_event(_log, "tray_unavailable")
            return False
        self.sync()
        self.icon.show()
        app = QApplication.instance()
        if app is not None:
            app.aboutToQuit.connect(self.stop)  # no ghost icon left behind
        logsafe.log_event(_log, "tray_started")
        return True

    def stop(self) -> None:
        self.icon.hide()

    def sync(self, *_args: object) -> None:
        """Make the icon, tooltip and menu text match the paused state."""
        paused = self._is_paused()
        self.icon.setIcon(self._icons[paused])
        self.icon.setToolTip(PAUSED_TIP if paused else RUNNING_TIP)
        self.pause_action.setText(RESUME_TEXT if paused else PAUSE_TEXT)
        theme = self._current_theme()
        self.menu.setStyleSheet(
            f"""
            QMenu {{
                background: {theme.card}; color: {theme.text};
                border: 1px solid {theme.border}; padding: 4px;
            }}
            QMenu::item {{ padding: 6px 22px; border-radius: 6px; }}
            QMenu::item:selected {{ background: {theme.card_selected}; color: {theme.text}; }}
            QMenu::separator {{ height: 1px; background: {theme.border}; margin: 4px 8px; }}
            """
        )

    def on_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        """A left click opens the window. (A double click also sends a click first.)"""
        if reason == QSystemTrayIcon.ActivationReason.Trigger:
            self._open_window()

    def show_hotkey_clash(self, names: list[str]) -> None:
        """Tell the owner which hotkeys another app already holds (fixed words and hotkey names)."""
        if names and self.icon.isVisible():
            self.icon.showMessage(
                HOTKEYS_TITLE,
                HOTKEYS_TEXT + ", ".join(names),
                QSystemTrayIcon.MessageIcon.Warning,
                8000,
            )
