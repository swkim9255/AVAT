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
    QSizePolicy,
    QToolButton,
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

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._drop_generation = 0

    def cancel_pending_drops(self) -> None:
        self._drop_generation += 1

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
            generation = self._drop_generation

            def emit_drop() -> None:
                # A project switch/list rebuild invalidates the old drag,
                # including when two copied projects share accident UIDs.
                if generation != self._drop_generation:
                    return
                if len(dropped_uids) == 1:
                    self.case_dropped.emit(dropped_uids[0], destination)
                else:
                    self.cases_dropped.emit(dropped_uids, destination)

            QTimer.singleShot(0, emit_drop)


class CaseListPanel(QWidget):
    case_selected = Signal(str)
    video_selected = Signal(str, int)
    new_case_requested = Signal()
    # Kept for API compatibility with integrations that add individual files.
    add_video_requested = Signal()
    delete_case_requested = Signal(str)
    delete_video_requested = Signal(str, int)
    case_reordered = Signal(str, int)
    cases_reordered = Signal(list, int)
    export_requested = Signal()
    open_project_requested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("panel")
        self.setMinimumWidth(240)
        self.setMaximumWidth(390)
        self._cases: list[InvestigationCase] = []
        self._cases_by_uid: dict[str, InvestigationCase] = {}
        self._items_by_uid: dict[str, QTreeWidgetItem] = {}
        self._videos_by_uid: dict[str, tuple[str, ...]] = {}
        self._missing_counts: dict[str, int] = {}
        self._completed_uids: set[str] = set()
        self._active_uid: str | None = None
        self._active_video_item: QTreeWidgetItem | None = None
        self._navigation_deferred = False
        self._project_name = ""

        root = QVBoxLayout(self)
        root.setContentsMargins(14, 16, 14, 12)
        root.setSpacing(10)
        caption = QLabel("프로젝트", self)
        caption.setObjectName("muted")
        root.addWidget(caption)
        project_row = QHBoxLayout()
        self.project_label = QLabel(self)
        self.project_label.setObjectName("projectFolderName")
        self.project_label.setTextFormat(Qt.TextFormat.PlainText)
        self.project_label.setMinimumWidth(0)
        self.project_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.project_label.setStyleSheet("font-size: 15px; font-weight: 700;")
        project_row.addWidget(self.project_label, 1)
        self.project_open_button = QToolButton(self)
        self.project_open_button.setIcon(material_icon("folder_open", PRIMARY))
        self.project_open_button.setAccessibleName("프로젝트 폴더 열기")
        self.project_open_button.setToolTip("프로젝트 폴더 열기 · 현재 탭 / 새 탭")
        self.project_open_button.setFixedSize(34, 34)
        self.project_open_button.setStyleSheet("padding: 0;")
        self.project_open_button.clicked.connect(self.open_project_requested)
        project_row.addWidget(self.project_open_button)
        root.addLayout(project_row)
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
        button_row.addWidget(self.new_button)
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

    def set_project_folder(self, folder: str | Path) -> None:
        path = Path(folder)
        self._project_name = path.name or str(path)
        self.project_label.setToolTip(str(path))
        self.project_label.setAccessibleName(f"현재 프로젝트: {self._project_name}")
        self._elide_project_name()

    def _elide_project_name(self) -> None:
        self.project_label.setText(self.project_label.fontMetrics().elidedText(
            self._project_name, Qt.TextElideMode.ElideRight, max(1, self.project_label.width())
        ))

    def resizeEvent(self, event) -> None:  # type: ignore[no-untyped-def]
        super().resizeEvent(event)
        self._elide_project_name()

    def capture_view_state(self) -> dict:
        return {
            "search": self.search.text(),
            "expanded": {uid: item.isExpanded() for uid, item in self._items_by_uid.items()},
            "selected": [
                (str(item.data(0, UID_ROLE)), int(item.data(0, VIDEO_INDEX_ROLE)))
                for item in self.tree.selectedItems()
            ],
            "current": self._selected_target(),
            "scroll": self.tree.verticalScrollBar().value(),
        }

    def restore_view_state(self, state: dict) -> None:
        blocked = self.tree.blockSignals(True)
        try:
            self.search.setText(state.get("search", ""))
            self.tree.clearSelection()
            for uid, expanded in state.get("expanded", {}).items():
                if item := self._items_by_uid.get(uid):
                    item.setExpanded(expanded)

            def find(target):  # type: ignore[no-untyped-def]
                if not target or (item := self._items_by_uid.get(target[0])) is None:
                    return None
                return item.child(target[1]) if target[1] >= 0 else item

            if current := find(state.get("current")):
                self.tree.setCurrentItem(current, 0, QItemSelectionModel.SelectionFlag.NoUpdate)
            for target in state.get("selected", []):
                if item := find(target):
                    item.setSelected(True)
            self.tree.verticalScrollBar().setValue(state.get("scroll", 0))
        finally:
            self.tree.blockSignals(blocked)
        self._update_delete_button()

    def set_cases(
        self,
        cases: list[InvestigationCase],
        selected_uid: str | None = None,
        selected_video_index: int | None | object = _SELECTION_UNSET,
    ) -> None:
        self.tree.cancel_pending_drops()
        previous = self._selected_target()
        selected_targets = {
            (str(item.data(0, UID_ROLE)), item.toolTip(0) if item.parent() else None)
            for item in self.tree.selectedItems()
        } if previous is not None and previous[0] == selected_uid else set()
        expanded = {
            self.tree.topLevelItem(index).data(0, UID_ROLE):
            self.tree.topLevelItem(index).isExpanded()
            for index in range(self.tree.topLevelItemCount())
        }
        if selected_video_index is _SELECTION_UNSET:
            selected_video_index = (
                previous[1]
                if previous is not None and previous[0] == selected_uid and previous[1] >= 0
                else None
            )
        self._cases = cases
        self._cases_by_uid = {case.uid: case for case in cases}
        self._active_video_item = None
        self._active_uid = None
        self._items_by_uid.clear()
        self._videos_by_uid.clear()
        self._missing_counts.clear()
        self._completed_uids.clear()
        blocked = self.tree.blockSignals(True)
        self.tree.clear()
        selected_item: QTreeWidgetItem | None = None
        selected_case_item: QTreeWidgetItem | None = None
        for case in cases:
            item = QTreeWidgetItem(self.tree)
            self._items_by_uid[case.uid] = item
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
            self._update_case_row(case, item)
            self._sync_video_items(case, item)
            for index in range(item.childCount()):
                child = item.child(index)
                if case.uid == selected_uid and index == selected_video_index:
                    selected_item = child
                if (case.uid, child.toolTip(0)) in selected_targets:
                    child.setSelected(True)
            if (case.uid, None) in selected_targets:
                item.setSelected(True)
            item.setExpanded(expanded.get(case.uid, True))
        if selected_item is None:
            selected_item = selected_case_item
        if selected_item:
            if selected_targets:
                self.tree.setCurrentItem(selected_item, 0, QItemSelectionModel.SelectionFlag.NoUpdate)
            else:
                self.tree.setCurrentItem(selected_item)
        self.set_active_video(selected_uid)
        self._update_count_label()
        self.tree.blockSignals(blocked)
        self._filter(self.search.text())
        self._update_delete_button()

    def update_case(
        self,
        case: InvestigationCase,
        active_uid: str | None | object = _SELECTION_UNSET,
    ) -> bool:
        """Refresh one existing row without rebuilding or changing selection."""

        item = self._items_by_uid.get(case.uid)
        if item is None:
            return False
        self._cases_by_uid[case.uid] = case
        blocked = self.tree.blockSignals(True)
        try:
            self._update_case_row(case, item)
            if self._videos_by_uid.get(case.uid) != tuple(case.videos):
                self._sync_video_items(case, item)
            self._filter_item(item, self.search.text().strip().casefold())
            self.set_active_video(self._active_uid if active_uid is _SELECTION_UNSET else active_uid)
            self._update_count_label()
        finally:
            self.tree.blockSignals(blocked)
        self._update_delete_button()
        return True

    def merge_cases(self, cases: list[InvestigationCase], active_uid: str | None) -> None:
        """Append watched accidents and refresh changed rows without a tree reset."""
        self._cases = cases
        blocked = self.tree.blockSignals(True)
        try:
            remaining = {case.uid for case in cases}
            for uid in self._items_by_uid.keys() - remaining:
                item = self._items_by_uid.pop(uid)
                if self._active_video_item is not None and self._active_video_item.parent() is item:
                    self._active_video_item = None
                self.tree.takeTopLevelItem(self.tree.indexOfTopLevelItem(item))
                self._cases_by_uid.pop(uid, None)
                self._videos_by_uid.pop(uid, None)
                self._missing_counts.pop(uid, None)
                self._completed_uids.discard(uid)
            for case in cases:
                if self._cases_by_uid.get(case.uid) is case:
                    continue
                if case.uid in self._items_by_uid:
                    self.update_case(case)
                    continue
                item = QTreeWidgetItem(self.tree)
                item.setData(0, UID_ROLE, case.uid)
                item.setData(0, VIDEO_INDEX_ROLE, -1)
                item.setIcon(0, material_icon("description", PRIMARY))
                item.setFlags(item.flags() | Qt.ItemFlag.ItemIsDragEnabled | Qt.ItemFlag.ItemIsDropEnabled)
                self._items_by_uid[case.uid] = item
                self._cases_by_uid[case.uid] = case
                self._update_case_row(case, item)
                self._sync_video_items(case, item)
                item.setExpanded(True)
                self._filter_item(item, self.search.text().strip().casefold())
            self.set_active_video(active_uid)
            self._update_count_label()
        finally:
            self.tree.blockSignals(blocked)
        self._update_delete_button()

    def _update_case_row(self, case: InvestigationCase, item: QTreeWidgetItem) -> None:
        item.setText(0, case.display_id)
        if case.status == "completed":
            self._completed_uids.add(case.uid)
            self._missing_counts[case.uid] = 0
            item.setText(1, "✓")
            item.setForeground(1, QBrush(QColor(SUCCESS)))
            item.setToolTip(1, "조사 완료")
        else:
            self._completed_uids.discard(case.uid)
            missing = len(case.missing)
            self._missing_counts[case.uid] = missing
            item.setText(1, str(missing))
            item.setForeground(1, QBrush(QColor(DANGER)))
            item.setToolTip(1, f"미입력 {missing}개")

    def _update_count_label(self) -> None:
        self.count_label.setText(f"{len(self._completed_uids)}/{len(self._items_by_uid)} 완료")

    def _sync_video_items(self, case: InvestigationCase, item: QTreeWidgetItem) -> None:
        """Reuse video rows by path, retaining selections through attachment edits."""

        current = self.tree.currentItem()
        current_path = current.toolTip(0) if current is not None and current.parent() is item else None
        selected_paths = {
            item.child(index).toolTip(0)
            for index in range(item.childCount())
            if item.child(index).isSelected()
        }
        expanded = item.isExpanded()
        if self._active_video_item is not None and self._active_video_item.parent() is item:
            self._active_video_item.setForeground(0, QBrush())
            self._active_video_item = None
        children = {child.toolTip(0): child for child in item.takeChildren()}
        current_replacement = item
        for index, path in enumerate(case.videos):
            child = children.pop(path, None)
            if child is None:
                child = QTreeWidgetItem()
                child.setText(0, Path(path).name)
                child.setData(0, UID_ROLE, case.uid)
                child.setIcon(0, material_icon("movie", MUTED))
                child.setFlags(
                    child.flags() & ~Qt.ItemFlag.ItemIsDragEnabled & ~Qt.ItemFlag.ItemIsDropEnabled
                )
                child.setToolTip(0, path)
            child.setData(0, VIDEO_INDEX_ROLE, index)
            item.addChild(child)
            child.setSelected(path in selected_paths)
            if path == current_path:
                current_replacement = child
        if current_path is not None:
            self.tree.setCurrentItem(
                current_replacement, 0, QItemSelectionModel.SelectionFlag.NoUpdate
            )
            if current_replacement is item and current_path in selected_paths:
                item.setSelected(True)
        item.setExpanded(expanded)
        self._videos_by_uid[case.uid] = tuple(case.videos)

    def set_active_video(self, uid: str | None) -> None:
        """Highlight only the playing child; leave navigation selection untouched."""

        case = self._cases_by_uid.get(uid) if uid is not None else None
        item = self._items_by_uid.get(uid) if uid is not None else None
        child = (
            item.child(case.active_video_index)
            if item is not None and case is not None and 0 <= case.active_video_index < item.childCount()
            else None
        )
        if child is not self._active_video_item:
            if self._active_video_item is not None:
                self._active_video_item.setForeground(0, QBrush())
            if child is not None:
                child.setForeground(0, QBrush(QColor(PRIMARY)))
            self._active_video_item = child
        self._active_uid = uid

    def select_case(self, uid: str) -> None:
        item = self._items_by_uid.get(uid)
        if item is not None:
            self.tree.setCurrentItem(item)
            self.tree.scrollToItem(item)

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
        for item in self._items_by_uid.values():
            self._filter_item(item, needle)

    @staticmethod
    def _filter_item(item: QTreeWidgetItem, needle: str) -> None:
        matches = needle in item.text(0).casefold()
        for child_index in range(item.childCount()):
            child = item.child(child_index)
            child_match = needle in child.text(0).casefold()
            child.setHidden(bool(needle) and not child_match)
            matches = matches or child_match
        item.setHidden(bool(needle) and not matches)


__all__ = ["CaseListPanel", "CaseTreeWidget"]
