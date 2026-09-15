"""Accident and attached-video navigation panel."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QItemSelectionModel, QPoint, Qt, QTimer, Signal
from PySide6.QtGui import QBrush, QColor, QDrag, QDragMoveEvent, QDropEvent
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMenu,
    QPushButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from investigator.models import InvestigationCase
from investigator.theme import DANGER, MUTED, PRIMARY, SUCCESS, material_icon

UID_ROLE = Qt.ItemDataRole.UserRole
VIDEO_INDEX_ROLE = Qt.ItemDataRole.UserRole + 1
_SELECTION_UNSET = object()


class CaseTreeWidget(QTreeWidget):
    """Tree that only reorders top-level accident items."""

    case_dropped = Signal(str, int)
    cases_dropped = Signal(list, int)

    def selected_cases(self) -> list[QTreeWidgetItem]:
        return [
            self.topLevelItem(index)
            for index in range(self.topLevelItemCount())
            if self.topLevelItem(index).isSelected()
            and not self.topLevelItem(index).isHidden()
        ]

    def startDrag(self, supported_actions) -> None:  # type: ignore[no-untyped-def]
        """Start a custom move drag without Qt deleting the source row."""

        del supported_actions
        source = self.currentItem()
        if source is None or source.parent() is not None:
            return
        mime_data = self.mimeData(self.selected_cases())
        if mime_data is None:
            return
        drag = QDrag(self)
        drag.setMimeData(mime_data)
        source_rect = self.visualItemRect(source)
        if source_rect.isValid():
            drag.setPixmap(self.viewport().grab(source_rect))
            drag.setHotSpot(source_rect.center() - source_rect.topLeft())
        drag.exec(Qt.DropAction.MoveAction)

    def dragMoveEvent(self, event: QDragMoveEvent) -> None:
        source = self.currentItem()
        if source is None or source.parent() is not None:
            event.ignore()
            return
        super().dragMoveEvent(event)

    def dropEvent(self, event: QDropEvent) -> None:
        source = self.currentItem()
        if source is None or source.parent() is not None or self.topLevelItemCount() < 2:
            event.ignore()
            return
        sources = self.selected_cases()
        if not sources:
            event.ignore()
            return

        position = event.position().toPoint()
        hit = self.itemAt(position)
        if hit is None:
            first = self.topLevelItem(0)
            raw_destination = (
                0 if position.y() < self.visualItemRect(first).top() else self.topLevelItemCount()
            )
        else:
            hit_is_video = hit.parent() is not None
            target = hit.parent() if hit_is_video else hit
            target_index = self.indexOfTopLevelItem(target)
            place_after = hit_is_video or position.y() >= self.visualItemRect(target).center().y()
            raw_destination = target_index + int(place_after)

        destination = raw_destination - sum(
            self.indexOfTopLevelItem(item) < raw_destination for item in sources
        )
        event.setDropAction(Qt.DropAction.MoveAction)
        event.accept()
        if sources:
            # Rebuilding or moving items while Qt is still finalizing the drag
            # can leave the dragged row temporarily hidden.  Only retain plain
            # values here and apply the move on the next event-loop turn.
            dropped_uids = [str(item.data(0, UID_ROLE)) for item in sources]
            QTimer.singleShot(
                0,
                lambda uids=dropped_uids, index=destination: (
                    self.case_dropped.emit(uids[0], index)
                    if len(uids) == 1 else self.cases_dropped.emit(uids, index)
                ),
            )


class CaseListPanel(QWidget):
    case_selected = Signal(str)
    video_selected = Signal(str, int)
    new_case_requested = Signal()
    video_folder_requested = Signal()
    # Kept for API compatibility with integrations that add individual files.
    add_video_requested = Signal()
    delete_case_requested = Signal(str)
    delete_video_requested = Signal(str, int)
    case_reordered = Signal(str, int)
    cases_reordered = Signal(list, int)
    export_requested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("panel")
        self.setMinimumWidth(240)
        self.setMaximumWidth(390)
        self._cases: list[InvestigationCase] = []
        self._navigation_deferred = False

        root = QVBoxLayout(self)
        root.setContentsMargins(14, 16, 14, 12)
        root.setSpacing(10)
        title_row = QHBoxLayout()
        title = QLabel("사고 / 영상 목록", self)
        title.setObjectName("sectionTitle")
        title_row.addWidget(title)
        title_row.addStretch(1)
        self.count_label = QLabel("0건", self)
        self.count_label.setStyleSheet(f"color: {MUTED};")
        title_row.addWidget(self.count_label)
        root.addLayout(title_row)

        self.search = QLineEdit(self)
        self.search.setObjectName("caseSearch")
        self.search.setStyleSheet(
            "QLineEdit#caseSearch QToolButton {"
            " min-width: 20px; max-width: 20px; min-height: 20px; max-height: 20px;"
            " padding: 0; margin: 0; border: 0; background: transparent;"
            "}"
        )
        self.search.setPlaceholderText("사고번호 또는 영상 검색")
        self.search.addAction(
            material_icon("search", MUTED), QLineEdit.ActionPosition.LeadingPosition
        )
        self.search.textChanged.connect(self._filter)
        root.addWidget(self.search)

        self.tree = CaseTreeWidget(self)
        self.tree.setObjectName("caseTree")
        self.tree.setHeaderHidden(True)
        self.tree.setColumnCount(2)
        self.tree.header().setStretchLastSection(False)
        self.tree.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.tree.header().setSectionResizeMode(1, QHeaderView.ResizeMode.Fixed)
        self.tree.setColumnWidth(1, 34)
        self.tree.setIndentation(18)
        self.tree.setAnimated(True)
        self.tree.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.tree.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        self.tree.setDefaultDropAction(Qt.DropAction.MoveAction)
        self.tree.setDragDropOverwriteMode(False)
        self.tree.setDropIndicatorShown(True)
        self.tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.tree.currentItemChanged.connect(self._selection_changed)
        self.tree.itemSelectionChanged.connect(self._update_delete_button)
        self.tree.itemClicked.connect(self._activate_deferred_selection)
        self.tree.customContextMenuRequested.connect(self._show_context_menu)
        self.tree.case_dropped.connect(self.case_reordered)
        self.tree.cases_dropped.connect(self.cases_reordered)
        root.addWidget(self.tree, 1)

        button_row = QHBoxLayout()
        self.new_button = QPushButton(material_icon("add", PRIMARY), "새 사고", self)
        self.new_button.setObjectName("newCaseButton")
        self.new_button.clicked.connect(self.new_case_requested)
        self.video_button = QPushButton(material_icon("folder_open", PRIMARY), "영상 폴더", self)
        self.video_button.setObjectName("importVideoFolderButton")
        self.video_button.clicked.connect(self.video_folder_requested)
        button_row.addWidget(self.new_button)
        button_row.addWidget(self.video_button)
        root.addLayout(button_row)

        self.delete_button = QPushButton(
            material_icon("delete_outline", DANGER), "선택 항목 삭제", self
        )
        self.delete_button.setObjectName("deleteSelectionButton")
        self.delete_button.setToolTip(
            "목록과 조사 데이터에서만 삭제하며 원본 영상 파일은 유지합니다."
        )
        self.delete_button.clicked.connect(self._request_selected_delete)
        self.delete_button.setEnabled(False)
        root.addWidget(self.delete_button)

        self.export_button = QPushButton(
            material_icon("download", PRIMARY), "조사 결과 CSV 내보내기", self
        )
        self.export_button.setObjectName("exportCsvButton")
        self.export_button.clicked.connect(self.export_requested)
        root.addWidget(self.export_button)

    def set_cases(
        self,
        cases: list[InvestigationCase],
        selected_uid: str | None = None,
        selected_video_index: int | None | object = _SELECTION_UNSET,
    ) -> None:
        expanded = {
            self.tree.topLevelItem(index).data(0, UID_ROLE):
            self.tree.topLevelItem(index).isExpanded()
            for index in range(self.tree.topLevelItemCount())
        }
        if selected_video_index is _SELECTION_UNSET:
            previous = self._selected_target()
            selected_video_index = (
                previous[1]
                if previous is not None and previous[0] == selected_uid and previous[1] >= 0
                else None
            )
        self._cases = cases
        self.tree.blockSignals(True)
        self.tree.clear()
        selected_item: QTreeWidgetItem | None = None
        selected_case_item: QTreeWidgetItem | None = None
        completed = sum(case.status == "completed" for case in cases)
        self.count_label.setText(f"{completed}/{len(cases)} 완료")
        for case in cases:
            item = QTreeWidgetItem(self.tree)
            item.setText(0, case.display_id)
            item.setData(0, UID_ROLE, case.uid)
            item.setData(0, VIDEO_INDEX_ROLE, -1)
            item.setIcon(0, material_icon("description", PRIMARY))
            item.setFlags(
                item.flags() | Qt.ItemFlag.ItemIsDragEnabled | Qt.ItemFlag.ItemIsDropEnabled
            )
            if case.uid == selected_uid:
                selected_case_item = item
                if selected_video_index is None:
                    selected_item = item
            if case.status == "completed":
                item.setText(1, "✓")
                item.setForeground(1, QBrush(QColor(SUCCESS)))
                item.setToolTip(1, "조사 완료")
            else:
                item.setText(1, str(len(case.missing)))
                item.setForeground(1, QBrush(QColor(DANGER)))
                item.setToolTip(1, f"미입력 {len(case.missing)}개")
            for index, path in enumerate(case.videos):
                child = QTreeWidgetItem(item)
                child.setText(0, Path(path).name)
                child.setData(0, UID_ROLE, case.uid)
                child.setData(0, VIDEO_INDEX_ROLE, index)
                child.setIcon(0, material_icon("movie", MUTED))
                child.setFlags(
                    child.flags() & ~Qt.ItemFlag.ItemIsDragEnabled & ~Qt.ItemFlag.ItemIsDropEnabled
                )
                child.setToolTip(0, path)
                if case.uid == selected_uid and index == case.active_video_index:
                    child.setForeground(0, QBrush(QColor(PRIMARY)))
                if case.uid == selected_uid and index == selected_video_index:
                    selected_item = child
            item.setExpanded(expanded.get(case.uid, True))
        if selected_item is None:
            selected_item = selected_case_item
        if selected_item:
            self.tree.setCurrentItem(selected_item)
        self.tree.blockSignals(False)
        self._filter(self.search.text())
        self._update_delete_button()

    def select_case(self, uid: str) -> None:
        iterator = self.tree.invisibleRootItem()
        for index in range(iterator.childCount()):
            item = iterator.child(index)
            if item.data(0, UID_ROLE) == uid:
                self.tree.setCurrentItem(item)
                self.tree.scrollToItem(item)
                return

    def move_case(self, uid: str, destination: int) -> bool:
        """Move one existing top-level item without rebuilding the tree."""

        source = next(
            (
                index
                for index in range(self.tree.topLevelItemCount())
                if self.tree.topLevelItem(index).data(0, UID_ROLE) == uid
            ),
            -1,
        )
        if source < 0:
            return False
        target = max(0, min(int(destination), self.tree.topLevelItemCount() - 1))
        if source == target:
            return False
        selected_item = self.tree.currentItem()
        signals_were_blocked = self.tree.blockSignals(True)
        was_expanded = self.tree.topLevelItem(source).isExpanded()
        item = self.tree.takeTopLevelItem(source)
        if item is None:
            self.tree.blockSignals(signals_were_blocked)
            return False
        self.tree.insertTopLevelItem(target, item)
        item.setExpanded(was_expanded)
        if selected_item is not None:
            self.tree.setCurrentItem(selected_item)
        self.tree.blockSignals(signals_were_blocked)
        self.tree.viewport().update()
        return True

    def move_cases(self, uids: list[str], destination: int) -> None:
        """Move the selected block, preserving item identity and UI state."""
        tree = self.tree
        items = [tree.topLevelItem(i) for i in range(tree.topLevelItemCount())]
        moving = [item for item in items if item.data(0, UID_ROLE) in uids]
        current = tree.currentItem()
        selected = tree.selectedItems()
        expanded = [(item, item.isExpanded()) for item in moving]
        blocked = tree.blockSignals(True)
        tree.setUpdatesEnabled(False)
        try:
            for item in moving:
                tree.takeTopLevelItem(tree.indexOfTopLevelItem(item))
            target = max(0, min(destination, tree.topLevelItemCount()))
            for offset, item in enumerate(moving):
                tree.insertTopLevelItem(target + offset, item)
            for item, state in expanded:
                item.setExpanded(state)
            if current is not None:
                tree.setCurrentItem(current, 0, QItemSelectionModel.SelectionFlag.NoUpdate)
            for item in selected:
                item.setSelected(True)
        finally:
            tree.setUpdatesEnabled(True)
            tree.blockSignals(blocked)
        tree.viewport().update()

    def _selection_changed(self) -> None:
        item = self.tree.currentItem()
        self._update_delete_button()
        if item is None:
            return
        if QApplication.keyboardModifiers() & (
            Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.ShiftModifier
        ):
            self._navigation_deferred = True
            return
        self._navigation_deferred = False
        uid = item.data(0, UID_ROLE)
        video_index = int(item.data(0, VIDEO_INDEX_ROLE) or 0)
        if item.parent() is not None and video_index >= 0:
            self.video_selected.emit(str(uid), video_index)
        else:
            self.case_selected.emit(str(uid))

    def _activate_deferred_selection(self) -> None:
        # A plain click on the current Ctrl/Shift-selected row does not emit
        # currentItemChanged again, but should still open that accident/video.
        if self._navigation_deferred:
            self._selection_changed()

    def _selected_target(self) -> tuple[str, int] | None:
        item = self.tree.currentItem()
        if item is None:
            return None
        uid = item.data(0, UID_ROLE)
        if not uid:
            return None
        video_index = int(item.data(0, VIDEO_INDEX_ROLE) or 0)
        return str(uid), video_index if item.parent() is not None else -1

    def _update_delete_button(self) -> None:
        target = self._selected_target()
        self.delete_button.setEnabled(target is not None)
        if target is None:
            self.delete_button.setText("선택 항목 삭제")
        elif target[1] >= 0:
            self.delete_button.setText("영상 삭제")
        else:
            self.delete_button.setText("사고 삭제")

    def _request_selected_delete(self) -> None:
        target = self._selected_target()
        if target is None:
            return
        uid, video_index = target
        if video_index >= 0:
            self.delete_video_requested.emit(uid, video_index)
        else:
            self.delete_case_requested.emit(uid)

    def _show_context_menu(self, position: QPoint) -> None:
        item = self.tree.itemAt(position)
        if item is None:
            return
        # Capture the clicked target before changing selection. Selecting a
        # missing or unreadable video can rebuild the tree and roll playback
        # back, but its context-menu delete action must still target that video
        # rather than the parent accident.
        raw_uid = item.data(0, UID_ROLE)
        if not raw_uid:
            return
        uid = str(raw_uid)
        video_index = int(item.data(0, VIDEO_INDEX_ROLE) or 0)
        if item.parent() is None:
            video_index = -1
        self.tree.setCurrentItem(item)
        menu = QMenu(self.tree)
        if video_index >= 0:
            action = menu.addAction(material_icon("delete_outline", DANGER), "영상 삭제")
            action.triggered.connect(
                lambda checked=False: self.delete_video_requested.emit(uid, video_index)
            )
        else:
            action = menu.addAction(material_icon("delete_outline", DANGER), "사고 삭제")
            action.triggered.connect(lambda checked=False: self.delete_case_requested.emit(uid))
        menu.exec(self.tree.viewport().mapToGlobal(position))

    def _filter(self, text: str) -> None:
        needle = text.strip().casefold()
        root = self.tree.invisibleRootItem()
        for index in range(root.childCount()):
            item = root.child(index)
            matches = needle in item.text(0).casefold()
            for child_index in range(item.childCount()):
                child = item.child(child_index)
                child_match = needle in child.text(0).casefold()
                child.setHidden(bool(needle) and not child_match)
                matches = matches or child_match
            item.setHidden(bool(needle) and not matches)


__all__ = ["CaseListPanel", "CaseTreeWidget"]
