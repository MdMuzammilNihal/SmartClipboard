"""The search-first window (tasks 9 and 10), drawn with PySide6.

Layout: a search box on top (focused whenever the window opens), the active
badge filter and the pause indicator under it, then one table row per item:
type badge, state badge ("Secret" / "Review?"), preview, source app, age,
and a star for pinned items. Buttons at the bottom act on the selected row.

Mouse and keys:
- Type in the search box to search. Up and Down move the selection, Enter
  copies the selected item back, Esc clears the search or hides the window.
- Click a type or state badge to filter by it; click the filter's "x" to
  clear it.
- Click a masked preview to reveal it for 10 seconds (Ctrl+R does the same
  for the selected row); click again to mask it.
- Double-click a row to copy it back. Ctrl+P pins or unpins, Shift+Del
  deletes (after a confirmation).

The window never touches the database or the clipboard itself. It calls a
WindowBackend (app.py), which runs on the same (UI) thread.

SECURITY REVIEW (owner):

- What is shown, masked, and searchable is decided in view_model.py; see its
  docstring. This file only draws those rows.
- Every reveal is masked again when the window hides (hideEvent), and a
  one-second timer masks reveals whose 10 seconds are up while it is open.
- Copying a masked item back does not reveal it. app.py sends secret and
  unsure items through the safe copy-back (opt-out formats, cleared after 30
  seconds).
- After a copy the window hides, so a revealed secret is not left on screen.
- Deleting asks for confirmation; the question never quotes the item.
- The window hides as soon as another app takes the focus (as Win+V does),
  so the list, and any revealed item, is not left on top of other work.
  Its own dialogs (the delete question) do not count as leaving.
- Known limit: while an item is revealed, its preview is ordinary widget
  text, readable by screen readers and UI automation tools like any other
  window text. Masked items expose only the bullets.
- Not checked on the real machine yet: that the window comes to the front
  when the hotkey is pressed while another app is in front (Windows limits
  which apps may take focus).
"""

from collections.abc import Callable
from datetime import datetime, timezone
from typing import Protocol

from PySide6.QtCore import (
    QAbstractTableModel,
    QEvent,
    QModelIndex,
    QObject,
    QPersistentModelIndex,
    QRectF,
    Qt,
    QTimer,
)
from PySide6.QtGui import QColor, QKeySequence, QPainter, QShortcut
from PySide6.QtWidgets import (
    QApplication,
    QAbstractItemView,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from smartclip.labels import SecretState
from smartclip.storage import StoredItem
from smartclip.ui.view_model import (
    Badge,
    Filter,
    RevealState,
    Row,
    build_rows,
    filter_name,
)

COL_TYPE, COL_STATE, COL_PREVIEW, COL_SOURCE, COL_AGE, COL_PIN = range(6)
_HEADERS = ("Type", "", "Item", "Source", "Age", "")
# The role under which badge columns hand their Badge to the delegate.
BADGE_ROLE = Qt.ItemDataRole.UserRole + 1
# Ages are redrawn this often while the window is open.
_AGE_REFRESH_TICKS = 30
DELETE_QUESTION = "Delete this item? This cannot be undone."


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


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _ask_delete(parent: QWidget) -> bool:
    answer = QMessageBox.question(parent, "Delete item", DELETE_QUESTION)
    return answer == QMessageBox.StandardButton.Yes


class RowsModel(QAbstractTableModel):
    """Holds the current rows for the table view."""

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

    def columnCount(self, parent: QModelIndex | QPersistentModelIndex = QModelIndex()) -> int:
        return 0 if parent.isValid() else len(_HEADERS)

    def headerData(
        self, section: int, orientation: Qt.Orientation, role: int = Qt.ItemDataRole.DisplayRole
    ) -> object:
        if orientation == Qt.Orientation.Horizontal and role == Qt.ItemDataRole.DisplayRole:
            return _HEADERS[section]
        return None

    def data(
        self, index: QModelIndex | QPersistentModelIndex, role: int = Qt.ItemDataRole.DisplayRole
    ) -> object:
        row = self.row_at(index.row())
        if row is None:
            return None
        column = index.column()
        if role == BADGE_ROLE:
            if column == COL_TYPE:
                return row.label_badge
            if column == COL_STATE:
                return row.state_badge
            return None
        if role == Qt.ItemDataRole.DisplayRole:
            if column == COL_TYPE:
                return row.label_badge.text
            if column == COL_STATE:
                return row.state_badge.text if row.state_badge else ""
            if column == COL_PREVIEW:
                return row.preview
            if column == COL_SOURCE:
                return row.source_app
            if column == COL_AGE:
                return row.age
            if column == COL_PIN:
                return "★" if row.pinned else ""
        if role == Qt.ItemDataRole.ToolTipRole:
            # Fixed hints only; never the item's text.
            if column in (COL_TYPE, COL_STATE) and self.data(index, BADGE_ROLE):
                return "Click to filter by this badge"
            if column == COL_PREVIEW and (row.masked or row.revealed):
                return "Click to hide" if row.revealed else "Click to reveal for 10 seconds"
        return None


class BadgeDelegate(QStyledItemDelegate):
    """Paints a Badge as a rounded coloured tag."""

    def paint(
        self,
        painter: QPainter,
        option: QStyleOptionViewItem,
        index: QModelIndex | QPersistentModelIndex,
    ) -> None:
        badge = index.data(BADGE_ROLE)
        if not isinstance(badge, Badge):
            super().paint(painter, option, index)
            return
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        metrics = option.fontMetrics
        width = metrics.horizontalAdvance(badge.text) + 14
        height = metrics.height() + 4
        rect = QRectF(
            option.rect.x() + 4,
            option.rect.y() + (option.rect.height() - height) / 2,
            min(width, option.rect.width() - 8),
            height,
        )
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(badge.background))
        painter.drawRoundedRect(rect, 6, 6)
        painter.setPen(QColor(badge.foreground))
        painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, badge.text)
        painter.restore()


class SearchWindow(QWidget):
    """The window opened by Ctrl+Alt+V."""

    def __init__(
        self,
        backend: WindowBackend,
        now: Callable[[], datetime] = _utc_now,
        reveals: RevealState | None = None,
        confirm_delete: Callable[[QWidget], bool] = _ask_delete,
        active_window: Callable[[], QWidget | None] = QApplication.activeWindow,
    ) -> None:
        super().__init__()
        self._backend = backend
        self._now = now
        self.reveals = reveals or RevealState()
        self._confirm_delete = confirm_delete
        self._active_window = active_window
        self._filter: Filter | None = None
        self._ticks = 0

        self.setWindowTitle("Smart Clipboard")
        self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint)
        self.resize(760, 480)

        self.search = QLineEdit()
        self.search.setPlaceholderText("Search")
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(self.refresh)
        self.search.installEventFilter(self)

        self.filter_label = QLabel()
        self.clear_filter_button = QPushButton("×")
        self.clear_filter_button.setFixedWidth(24)
        self.clear_filter_button.setToolTip("Clear the filter")
        self.clear_filter_button.clicked.connect(lambda: self.set_filter(None))
        self.pause_label = QLabel("Capture paused")
        self.pause_label.setStyleSheet("color: #c92a2a; font-weight: bold;")
        self.pause_button = QPushButton()
        self.pause_button.clicked.connect(self.toggle_pause)
        self.status_label = QLabel()

        self.model = RowsModel(self)
        self.table = QTableView()
        self.table.setModel(self.model)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setShowGrid(False)
        self.table.setWordWrap(False)
        self.table.verticalHeader().hide()
        self.table.setItemDelegateForColumn(COL_TYPE, BadgeDelegate(self.table))
        self.table.setItemDelegateForColumn(COL_STATE, BadgeDelegate(self.table))
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(COL_PREVIEW, QHeaderView.ResizeMode.Stretch)
        for column, width in ((COL_TYPE, 80), (COL_STATE, 76), (COL_SOURCE, 120), (COL_AGE, 44)):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.Fixed)
            self.table.setColumnWidth(column, width)
        header.setSectionResizeMode(COL_PIN, QHeaderView.ResizeMode.Fixed)
        self.table.setColumnWidth(COL_PIN, 24)
        self.table.clicked.connect(self._on_clicked)
        # Enter or a double-click on a row (Windows style) copies it back.
        self.table.activated.connect(lambda _index: self.copy_selected())
        self.table.selectionModel().selectionChanged.connect(self._update_buttons)

        self.copy_button = QPushButton("Copy")
        self.copy_button.clicked.connect(self.copy_selected)
        self.reveal_button = QPushButton("Reveal")
        self.reveal_button.clicked.connect(self.toggle_reveal_selected)
        self.pin_button = QPushButton("Pin")
        self.pin_button.clicked.connect(self.toggle_pin_selected)
        self.mark_normal_button = QPushButton("Mark normal")
        self.mark_normal_button.clicked.connect(self.mark_normal_selected)
        self.mark_secret_button = QPushButton("Mark secret")
        self.mark_secret_button.clicked.connect(self.mark_secret_selected)
        self.delete_button = QPushButton("Delete")
        self.delete_button.clicked.connect(self.delete_selected)
        self.quit_button = QPushButton("Quit")
        self.quit_button.setToolTip("Stop Smart Clipboard")
        self.quit_button.clicked.connect(self._backend.quit)

        top = QHBoxLayout()
        top.addWidget(self.search, 1)
        top.addWidget(self.pause_label)
        top.addWidget(self.pause_button)
        filters = QHBoxLayout()
        filters.addWidget(self.filter_label)
        filters.addWidget(self.clear_filter_button)
        filters.addStretch(1)
        filters.addWidget(self.status_label)
        buttons = QHBoxLayout()
        for button in (
            self.copy_button,
            self.reveal_button,
            self.pin_button,
            self.mark_normal_button,
            self.mark_secret_button,
            self.delete_button,
        ):
            buttons.addWidget(button)
        buttons.addStretch(1)
        buttons.addWidget(self.quit_button)
        layout = QVBoxLayout(self)
        layout.addLayout(top)
        layout.addLayout(filters)
        layout.addWidget(self.table, 1)
        layout.addLayout(buttons)

        for keys, action in (
            ("Ctrl+R", self.toggle_reveal_selected),
            ("Ctrl+P", self.toggle_pin_selected),
            ("Shift+Del", self.delete_selected),
        ):
            QShortcut(QKeySequence(keys), self, activated=action)

        self._timer = QTimer(self)
        self._timer.setInterval(1000)
        self._timer.timeout.connect(self._tick)

        self.refresh()

    # --- Showing and hiding ---

    def show_and_focus(self) -> None:
        """Open the window fresh: empty search, no filter, search box focused."""
        self.search.blockSignals(True)
        self.search.clear()
        self.search.blockSignals(False)
        self._filter = None
        self.reveals.hide_all()
        self.refresh()
        self.show()
        self.raise_()
        self.activateWindow()
        self.search.setFocus()

    def showEvent(self, event: QEvent) -> None:  # noqa: N802 (Qt name)
        self._ticks = 0
        self._timer.start()
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
        if self.isVisible() and self._active_window() is None:
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
        """Rebuild the rows from the backend, keeping the selected item selected."""
        selected = self.selected_row()
        rows = build_rows(
            self._backend.items(), self.search.text(), self._filter, self.reveals, self._now()
        )
        self.model.set_rows(rows)
        target = 0
        if selected is not None:
            for position, row in enumerate(rows):
                if row.item_id == selected.item_id:
                    target = position
                    break
        if rows:
            self.table.selectRow(target)
        if self._filter is None:
            self.filter_label.setText("All items")
            self.clear_filter_button.hide()
        else:
            self.filter_label.setText(f"Filter: {filter_name(self._filter)}")
            self.clear_filter_button.show()
        paused = self._backend.is_paused()
        self.pause_label.setVisible(paused)
        self.pause_button.setText("Resume" if paused else "Pause")
        self._update_buttons()

    def selected_row(self) -> Row | None:
        indexes = self.table.selectionModel().selectedRows()
        return self.model.row_at(indexes[0].row()) if indexes else None

    def _move_selection(self, step: int) -> None:
        count = self.model.rowCount()
        if count == 0:
            return
        current = self.table.currentIndex().row()
        self.table.selectRow(max(0, min(count - 1, current + step)))

    def _update_buttons(self) -> None:
        row = self.selected_row()
        has_row = row is not None
        self.copy_button.setEnabled(has_row)
        self.delete_button.setEnabled(has_row)
        sensitive = has_row and row.state is not SecretState.NORMAL
        self.reveal_button.setEnabled(sensitive)
        self.reveal_button.setText("Hide" if has_row and row.revealed else "Reveal")
        self.pin_button.setEnabled(has_row and row.can_pin)
        self.pin_button.setText("Unpin" if has_row and row.pinned else "Pin")
        self.mark_normal_button.setEnabled(sensitive)
        self.mark_secret_button.setEnabled(has_row and row.state is not SecretState.SECRET)

    def _tick(self) -> None:
        self._ticks += 1
        expired = self.reveals.expire(self._now())
        if expired or self._ticks % _AGE_REFRESH_TICKS == 0:
            self.refresh()

    # --- Actions ---

    def _on_clicked(self, index: QModelIndex) -> None:
        row = self.model.row_at(index.row())
        if row is None:
            return
        if index.column() == COL_TYPE:
            self.set_filter(row.label)
        elif index.column() == COL_STATE and row.state_badge is not None:
            self.set_filter(row.state)
        elif index.column() == COL_PREVIEW and (row.masked or row.revealed):
            self.toggle_reveal(row)

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

    def copy_selected(self) -> None:
        row = self.selected_row()
        if row is None:
            return
        if self._backend.copy_back(row.item_id):
            self.status_label.setText("")
            self.hide()
        else:
            self.status_label.setText("Could not copy")

    def toggle_pin_selected(self) -> None:
        row = self.selected_row()
        if row is None or not row.can_pin:
            return
        if row.pinned:
            self._backend.unpin(row.item_id)
        else:
            self._backend.pin(row.item_id)
        self.refresh()

    def mark_normal_selected(self) -> None:
        row = self.selected_row()
        if row is not None and row.state is not SecretState.NORMAL:
            self._backend.mark_normal(row.item_id)
            self.reveals.hide(row.item_id)
            self.refresh()

    def mark_secret_selected(self) -> None:
        row = self.selected_row()
        if row is not None and row.state is not SecretState.SECRET:
            self._backend.mark_secret(row.item_id)
            self.reveals.hide(row.item_id)
            self.refresh()

    def delete_selected(self) -> None:
        row = self.selected_row()
        if row is None or not self._confirm_delete(self):
            return
        self._backend.delete(row.item_id)
        self.reveals.hide(row.item_id)
        self.refresh()

    def toggle_pause(self) -> None:
        self._backend.toggle_pause()
        self.refresh()

    # --- Keys ---

    def keyPressEvent(self, event: QEvent) -> None:  # noqa: N802 (Qt name)
        # Esc pressed anywhere else in the window (for example the table).
        if event.key() == Qt.Key.Key_Escape:  # type: ignore[attr-defined]
            self.hide()
            return
        super().keyPressEvent(event)  # type: ignore[arg-type]

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:  # noqa: N802 (Qt name)
        if watched is self.search and event.type() == QEvent.Type.KeyPress:
            key = event.key()  # type: ignore[attr-defined]
            if key == Qt.Key.Key_Down:
                self._move_selection(1)
                return True
            if key == Qt.Key.Key_Up:
                self._move_selection(-1)
                return True
            if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
                self.copy_selected()
                return True
            if key == Qt.Key.Key_Escape:
                if self.search.text():
                    self.search.clear()
                else:
                    self.hide()
                return True
        return super().eventFilter(watched, event)
