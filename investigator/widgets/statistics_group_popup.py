"""Fixed-variable, multi-value cohort selector for butterfly comparisons."""

from __future__ import annotations

from PySide6.QtCore import QPoint, Qt, Signal
from PySide6.QtGui import QMouseEvent
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidgetItem,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from investigator.butterfly import group_value_key, normalize_group_values
from investigator.statistics import Category
from investigator.widgets.statistics_condition_popup import _ValueChecklist


class _GroupChecklist(_ValueChecklist):
    """Retain full-row toggling, respecting disabled opposite-side values."""

    def mousePressEvent(self, event: QMouseEvent) -> None:
        item = self.itemAt(event.position().toPoint())
        if item is not None and not item.flags() & Qt.ItemFlag.ItemIsEnabled:
            self._pressed = None
            event.accept()
            return
        super().mousePressEvent(event)


class StatisticsGroupPopup(QWidget):
    applied = Signal(object)

    def __init__(self, groups: tuple[Category, ...], selected: tuple, *,
                 title: str, blocked: tuple = (), parent: QWidget | None = None) -> None:
        super().__init__(parent, Qt.WindowType.Popup)
        self.setObjectName("statisticsGroupPopup")
        self.setWindowTitle(title)
        self.setMinimumWidth(320)
        self.setStyleSheet("QWidget#statisticsGroupPopup { border:1px solid #cbd5e1; background:white; }")
        self.values: tuple | None = None
        selected_keys = {group_value_key(value) for value in normalize_group_values(selected)}
        blocked_keys = {group_value_key(value) for value in normalize_group_values(blocked)}
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 12, 14, 12)
        heading = QLabel(title, self)
        heading.setTextFormat(Qt.TextFormat.PlainText)
        heading.setWordWrap(True)
        heading.setMinimumWidth(0)
        layout.addWidget(heading)
        self.search = QLineEdit(self)
        self.search.setPlaceholderText("값 검색")
        self.search.setClearButtonEnabled(True)
        layout.addWidget(self.search)
        selection = QHBoxLayout()
        self.select_all_button = QPushButton("전체선택", self)
        self.clear_selection_button = QPushButton("선택해제", self)
        for button in (self.select_all_button, self.clear_selection_button):
            selection.addWidget(button)
        selection.addStretch(1)
        layout.addLayout(selection)
        self.values_list = _GroupChecklist(self)
        self.values_list.setMinimumHeight(180)
        self.values_list.setUniformItemSizes(True)
        self.values_list.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.values_list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.values_list.setTextElideMode(Qt.TextElideMode.ElideRight)
        self.values_list.setAccessibleName("그룹 값과 현재 필터 기준 사고 건수")
        for group in groups:
            token = group_value_key(group.value)
            blocked_here = token in blocked_keys
            item = QListWidgetItem(f"{group.label} ({group.count:,}건)", self.values_list)
            item.setData(Qt.ItemDataRole.UserRole, group.value)
            item.setData(Qt.ItemDataRole.UserRole + 1, group.count)
            item.setData(Qt.ItemDataRole.UserRole + 2, group.label)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            if blocked_here:
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEnabled)
            item.setCheckState(Qt.CheckState.Checked if token in selected_keys and not blocked_here
                               else Qt.CheckState.Unchecked)
            item.setToolTip(f"{group.label}\n현재 필터 기준 {group.count:,}건" +
                            ("\n반대쪽 그룹에서 선택한 값입니다." if blocked_here else ""))
        layout.addWidget(self.values_list, 1)
        self.hint = QLabel("선택한 값 중 하나라도 해당하는 사고를 합칩니다. 건수는 현재 필터 기준입니다.", self)
        self.hint.setWordWrap(True)
        self.hint.setObjectName("secondaryText")
        layout.addWidget(self.hint)
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        self.apply_button = QPushButton("적용", self)
        self.cancel_button = QPushButton("취소", self)
        buttons.addWidget(self.apply_button)
        buttons.addWidget(self.cancel_button)
        layout.addLayout(buttons)
        self.search.textChanged.connect(self._search)
        self.values_list.itemChanged.connect(self._update_apply)
        self.select_all_button.clicked.connect(lambda: self._select_visible(True))
        self.clear_selection_button.clicked.connect(lambda: self._select_visible(False))
        self.apply_button.clicked.connect(self._apply)
        self.cancel_button.clicked.connect(self.close)
        self._update_apply()

    def _selected(self) -> tuple:
        return tuple(self.values_list.item(i).data(Qt.ItemDataRole.UserRole)
                     for i in range(self.values_list.count())
                     if self.values_list.item(i).flags() & Qt.ItemFlag.ItemIsEnabled
                     and self.values_list.item(i).checkState() == Qt.CheckState.Checked)

    def _update_apply(self, *_args) -> None:
        self.apply_button.setEnabled(bool(self._selected()))

    def _search(self, text: str) -> None:
        query = text.strip().casefold()
        for i in range(self.values_list.count()):
            item = self.values_list.item(i)
            item.setHidden(query not in str(item.data(Qt.ItemDataRole.UserRole + 2)).casefold())

    def _select_visible(self, checked: bool) -> None:
        self.values_list.blockSignals(True)
        for i in range(self.values_list.count()):
            item = self.values_list.item(i)
            if not item.isHidden() and item.flags() & Qt.ItemFlag.ItemIsEnabled:
                item.setCheckState(Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked)
        self.values_list.blockSignals(False)
        self._update_apply()

    def _apply(self) -> None:
        selected = self._selected()
        if not selected:
            return
        self.values = selected
        self.applied.emit(selected)
        self.close()

    def open_at(self, anchor: QWidget) -> None:
        self.resize(440, 480)
        available = anchor.screen().availableGeometry()
        self.resize(min(self.width(), available.width()), min(self.height(), available.height()))
        below = anchor.mapToGlobal(QPoint(0, anchor.height() + 4))
        above = anchor.mapToGlobal(QPoint(0, -self.height() - 4))
        y = below.y() if below.y() + self.height() <= available.bottom() + 1 else above.y()
        x = min(max(below.x(), available.left()), available.right() - self.width() + 1)
        self.move(x, min(max(y, available.top()), available.bottom() - self.height() + 1))
        self.show()
        self.search.setFocus(Qt.FocusReason.PopupFocusReason)
