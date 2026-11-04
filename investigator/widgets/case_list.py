"""Accident and attached-video navigation panel."""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path

from PySide6.QtCore import QItemSelectionModel, QPoint, QSize, Qt, QTimer, Signal
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
KIND_ROLE = Qt.ItemDataRole.UserRole + 2
FOLDER_KEY_ROLE = Qt.ItemDataRole.UserRole + 3
_SELECTION_UNSET = object()
SEARCH_DEBOUNCE_MS = 150


class CaseTreeWidget(QTreeWidget):
    """Folder navigation that reorders accidents without moving their files."""

    case_dropped = Signal(str, int)
    cases_dropped = Signal(list, int)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._drop_generation = 0
        self._case_order: list[str] = []

    def set_case_order(self, uids: list[str]) -> None:
        self._case_order = list(uids)

    def case_items(self) -> list[QTreeWidgetItem]:
        """Return case rows in display order, independent of folder depth."""
        result = []

        def visit(item: QTreeWidgetItem) -> None:
            if item.data(0, KIND_ROLE) == "case":
                result.append(item)
            elif item.data(0, KIND_ROLE) == "folder":
                for index in range(item.childCount()):
                    visit(item.child(index))

        for index in range(self.topLevelItemCount()):
            visit(self.topLevelItem(index))
        return result

    @staticmethod
    def item_is_visible(item: QTreeWidgetItem) -> bool:
        while item is not None:
            if item.isHidden():
                return False
            item = item.parent()
        return True

    def cancel_pending_drops(self) -> None:
        self._drop_generation += 1

    def selected_cases(self) -> list[QTreeWidgetItem]:
        selected = {
            str(item.data(0, UID_ROLE)): item
            for item in self.case_items()
            if item.isSelected() and self.item_is_visible(item)
        }
        return [selected[uid] for uid in self._case_order if uid in selected]

    def startDrag(self, supported_actions) -> None:  # type: ignore[no-untyped-def]
        """Start a custom move drag without Qt deleting the source row."""

        del supported_actions
        source = self.currentItem()
        if source is None or source.data(0, KIND_ROLE) != "case":
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
        if source is None or source.data(0, KIND_ROLE) != "case":
            event.ignore()
            return
        super().dragMoveEvent(event)

    def dropEvent(self, event: QDropEvent) -> None:
        source = self.currentItem()
        if source is None or source.data(0, KIND_ROLE) != "case" or len(self._case_order) < 2:
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
                0 if position.y() < self.visualItemRect(first).top() else len(self._case_order)
            )
        else:
            kind = hit.data(0, KIND_ROLE)
            if kind == "folder":
                children = [hit.child(index) for index in range(hit.childCount())]
                indices = [self._case_order.index(str(item.data(0, UID_ROLE))) for item in children]
                if not indices:
                    event.ignore()
                    return
                after = position.y() >= self.visualItemRect(hit).center().y()
                raw_destination = max(indices) + 1 if after else min(indices)
            else:
                target = hit.parent() if kind == "video" else hit
                target_index = self._case_order.index(str(target.data(0, UID_ROLE)))
                place_after = kind == "video" or position.y() >= self.visualItemRect(target).center().y()
                raw_destination = target_index + int(place_after)

        destination = raw_destination - sum(
            self._case_order.index(str(item.data(0, UID_ROLE))) < raw_destination for item in sources
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
    refresh_requested = Signal()
    # Kept for API compatibility with integrations that add individual files.
    add_video_requested = Signal()
    delete_case_requested = Signal(str)
    delete_video_requested = Signal(str, int)
    case_reordered = Signal(str, int)
    cases_reordered = Signal(list, int)
    sort_ascending_requested = Signal()
    export_requested = Signal()
    report_export_requested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("panel")
        self.setMinimumWidth(240)
        self.setMaximumWidth(390)
        self._cases: list[InvestigationCase] = []
        self._workspace: Path | None = None
        self._video_filter: Callable[[str], bool] | None = None
        self._video_filter_key: object = None
        self._show_empty_in_scope = False
        self._folder_items_by_key: dict[str, QTreeWidgetItem] = {}
        self._cases_by_uid: dict[str, InvestigationCase] = {}
        self._items_by_uid: dict[str, QTreeWidgetItem] = {}
        self._videos_by_uid: dict[str, tuple[str, ...]] = {}
        self._missing_counts: dict[str, int] = {}
        self._completed_uids: set[str] = set()
        self._active_uid: str | None = None
        self._active_video_item: QTreeWidgetItem | None = None
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
        # 사용자가 입력 중일 때는 글자마다 목록 전체를 훑지 않도록 잠시 모아서 적용한다.
        # 코드에서 검색어를 바꾸면(포커스 없음) 기존처럼 즉시 반영한다.
        self._search_timer = QTimer(self)
        self._search_timer.setSingleShot(True)
        self._search_timer.setInterval(SEARCH_DEBOUNCE_MS)
        self._search_timer.timeout.connect(lambda: self._filter(self.search.text()))
        self.search.textChanged.connect(self._search_text_changed)
        search_row = QHBoxLayout()
        search_row.setSpacing(6)
        search_row.addWidget(self.search, 1)
        self.sort_button = QToolButton(self)
        self.sort_button.setObjectName("sortCasesAscendingButton")
        self.sort_button.setIcon(material_icon("format_list_numbered", PRIMARY))
        self.sort_button.setIconSize(QSize(20, 20))
        self.sort_button.setFixedSize(36, 36)
        self.sort_button.setStyleSheet("QToolButton#sortCasesAscendingButton { padding: 0; }")
        self.sort_button.setToolTip("사고번호 오름차순 정렬")
        self.sort_button.setAccessibleName("사고번호 오름차순 정렬")
        self.sort_button.setEnabled(False)
        self.sort_button.clicked.connect(self.sort_ascending_requested)
        search_row.addWidget(self.sort_button)
        root.addLayout(search_row)

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
        self.refresh_button = QPushButton(material_icon("refresh", PRIMARY), "목록 새로고침", self)
        self.refresh_button.setObjectName("refreshCasesButton")
        self.refresh_button.clicked.connect(self.refresh_requested)
        button_row.addWidget(self.refresh_button)
        root.addLayout(button_row)

        self.delete_button = QPushButton(
            material_icon("delete_outline", DANGER), "선택 항목 삭제", self
        )
        self.delete_button.setObjectName("deleteSelectionButton")
        self.delete_button.setToolTip(
            "DB에서 삭제하고 영상 파일은 프로젝트의 삭제사고 폴더로 이동합니다. 다른 사고의 공유 파일은 유지합니다."
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

        self.report_export_button = QPushButton(
            material_icon("description", PRIMARY), "사고 분석 보고서 내보내기", self
        )
        self.report_export_button.setObjectName("exportReportButton")
        self.report_export_button.clicked.connect(self.report_export_requested)
        root.addWidget(self.report_export_button)

    def set_workspace(self, workspace: Path) -> None:
        """Set the root used by the next list refresh to label video folders."""
        self._workspace = Path(workspace).resolve()

    def set_video_filter(self, predicate: Callable[[str], bool] | None, *, show_empty: bool = False,
                         scope_key: object = None, rebuild: bool = True) -> None:
        """Filter display only; child indices still address original case videos.

        범위 밖 사고는 숨기지 않고 아예 항목을 만들지 않는다. 숨긴 행은 Qt 영속
        인덱스를 남겨 행 삽입·삭제마다 전체를 갱신하게 하므로 사고 수만 건에서
        목록 갱신이 제곱으로 느려졌다. ``rebuild=False``는 곧 이어질 set_cases를 위해
        범위만 바꾼다.
        """
        unchanged = predicate is not None and scope_key is not None and scope_key == self._video_filter_key
        self._video_filter = predicate
        self._video_filter_key = scope_key
        self._show_empty_in_scope = show_empty
        if not rebuild:
            return
        if unchanged:
            self._filter(self.search.text())
            self._update_count_label()
            return
        self.set_cases(self._cases, self._active_uid)

    def _case_in_scope(self, case: InvestigationCase) -> bool:
        return self._video_filter is None or (
            self._show_empty_in_scope and not case.videos
        ) or any(self._video_filter(path) for path in case.videos)

    def case_item(self, uid: str) -> QTreeWidgetItem | None:
        return self._items_by_uid.get(uid)

    def _folder_details(self, case: InvestigationCase) -> tuple[str, str, str] | None:
        if self._workspace is None or self._video_filter is not None:
            return None
        existing = [Path(path).resolve() for path in case.videos if Path(path).is_file()]
        paths = existing or [Path(path).resolve() for path in case.videos]
        if not paths:
            return "__no_video__", "영상 없음", "연결된 영상이 없는 사고"
        parents = [path.parent for path in paths]
        try:
            folder = Path(os.path.commonpath(parents))
        except ValueError:  # Files on different drives still have one accident row.
            return "__multiple_locations__", "여러 영상 폴더", "여러 드라이브에 연결된 영상"
        try:
            relative = folder.relative_to(self._workspace)
            label = relative.as_posix() if relative.parts else self._workspace.name
        except ValueError:
            label = folder.name or str(folder)
        return os.path.normcase(str(folder)), label, str(folder)

    def _parent_for_case(self, case: InvestigationCase) -> QTreeWidgetItem | None:
        details = self._folder_details(case)
        if details is None:
            return None
        key, label, tooltip = details
        item = self._folder_items_by_key.get(key)
        if item is None:
            item = QTreeWidgetItem(self.tree)
            item.setData(0, KIND_ROLE, "folder")
            item.setData(0, FOLDER_KEY_ROLE, key)
            item.setText(0, label)
            item.setToolTip(0, tooltip)
            item.setIcon(0, material_icon("folder", MUTED))
            item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsDropEnabled)
            item.setExpanded(True)
            self._folder_items_by_key[key] = item
        return item

    def _new_case_item(self, case: InvestigationCase, *, attach: bool = True) -> QTreeWidgetItem:
        """``attach=False``는 범위 필터 모드에서 모아서 한 번에 삽입할 분리 항목을 만든다."""
        parent = self._parent_for_case(case)
        if parent is not None:
            item = QTreeWidgetItem(parent)
        elif attach:
            item = QTreeWidgetItem(self.tree)
        else:
            item = QTreeWidgetItem()
        item.setData(0, KIND_ROLE, "case")
        item.setData(0, UID_ROLE, case.uid)
        item.setData(0, VIDEO_INDEX_ROLE, -1)
        item.setIcon(0, material_icon("description", PRIMARY))
        item.setFlags(item.flags() | Qt.ItemFlag.ItemIsDragEnabled | Qt.ItemFlag.ItemIsDropEnabled)
        self._items_by_uid[case.uid] = item
        return item

    def _grouped(self) -> bool:
        """범위 필터가 없을 때만 영상 폴더 묶음 항목을 사용한다."""
        return self._video_filter is None

    def _arrange_case_items(self) -> None:
        """Retain case/video item identity while regrouping and applying order."""
        if not self._grouped():
            self._arrange_flat_items()
            return
        current = self.tree.currentItem()
        selected = [
            item
            for case_item in self._items_by_uid.values()
            for item in [case_item, *(case_item.child(index) for index in range(case_item.childCount()))]
            if item.isSelected()
        ]
        expanded = {uid: item.isExpanded() for uid, item in self._items_by_uid.items()}
        expanded_folders = {key: item.isExpanded() for key, item in self._folder_items_by_key.items()}
        for case in self._cases:
            if case.uid not in self._items_by_uid:
                continue
            item = self._items_by_uid[case.uid]
            old_parent = item.parent()
            if old_parent is not None:
                old_parent.takeChild(old_parent.indexOfChild(item))
            else:
                self.tree.takeTopLevelItem(self.tree.indexOfTopLevelItem(item))
            parent = self._parent_for_case(case)
            if parent is not None:
                parent.addChild(item)
            else:
                self.tree.addTopLevelItem(item)
        for key, folder in list(self._folder_items_by_key.items()):
            if folder.childCount() == 0:
                self.tree.takeTopLevelItem(self.tree.indexOfTopLevelItem(folder))
                del self._folder_items_by_key[key]
        # Folder order follows the first accident's global display order.
        folder_order = []
        for case in self._cases:
            if case.uid not in self._items_by_uid:
                continue
            folder = self._items_by_uid[case.uid].parent()
            if folder is not None and folder not in folder_order:
                folder_order.append(folder)
        for index, folder in enumerate(folder_order):
            old_index = self.tree.indexOfTopLevelItem(folder)
            if old_index != index:
                self.tree.takeTopLevelItem(old_index)
                self.tree.insertTopLevelItem(index, folder)
        for uid, state in expanded.items():
            self._items_by_uid[uid].setExpanded(state)
        for key, state in expanded_folders.items():
            if key in self._folder_items_by_key:
                self._folder_items_by_key[key].setExpanded(state)
        if current is not None:
            self.tree.setCurrentItem(current, 0, QItemSelectionModel.SelectionFlag.NoUpdate)
        for item in selected:
            item.setSelected(True)
        self.tree.set_case_order([case.uid for case in self._cases])
        self._filter(self.search.text())

    def _top_level_uids(self) -> list[str]:
        return [
            str(self.tree.topLevelItem(index).data(0, UID_ROLE))
            for index in range(self.tree.topLevelItemCount())
        ]

    def _arrange_flat_items(self) -> None:
        """범위 필터 모드의 최상위 사고 행을 한 번에 꺼냈다가 순서대로 다시 넣는다.

        행을 하나씩 꺼내고 넣으면 Qt가 행마다 모든 영속 인덱스(펼침·선택)를 갱신해
        사고 수의 제곱으로 느려진다. 일괄 처리는 그 갱신을 두 번으로 줄인다.
        """
        desired = [case.uid for case in self._cases if case.uid in self._items_by_uid]
        if self._top_level_uids() == desired:
            self.tree.set_case_order([case.uid for case in self._cases])
            return
        current = self.tree.currentItem()
        selected = [
            item
            for case_item in self._items_by_uid.values()
            for item in [case_item, *(case_item.child(index) for index in range(case_item.childCount()))]
            if item.isSelected()
        ]
        expanded = {uid: item.isExpanded() for uid, item in self._items_by_uid.items()}
        updates = self.tree.updatesEnabled()
        self.tree.setUpdatesEnabled(False)
        try:
            self.tree.invisibleRootItem().takeChildren()
            self.tree.addTopLevelItems([self._items_by_uid[uid] for uid in desired])
            for uid, state in expanded.items():
                self._items_by_uid[uid].setExpanded(state)
            if current is not None:
                self.tree.setCurrentItem(current, 0, QItemSelectionModel.SelectionFlag.NoUpdate)
            for item in selected:
                item.setSelected(True)
        finally:
            self.tree.setUpdatesEnabled(updates)
        self.tree.set_case_order([case.uid for case in self._cases])
        self._filter(self.search.text())

    def selected_case_uids(self) -> list[str]:
        """Map selected case/video rows to distinct accidents in project order."""
        selected = {
            str(item.data(0, UID_ROLE))
            for item in self.tree.selectedItems()
            if item.data(0, KIND_ROLE) in {"case", "video"}
            and self.tree.item_is_visible(item)
        }
        return [case.uid for case in self._cases if case.uid in selected]

    def capture_view_state(self) -> dict:
        return {
            "search": self.search.text(),
            "expanded": {uid: item.isExpanded() for uid, item in self._items_by_uid.items()},
            "expanded_folders": {
                key: item.isExpanded() for key, item in self._folder_items_by_key.items()
            },
            "selected": [
                (str(item.data(0, UID_ROLE)), int(item.data(0, VIDEO_INDEX_ROLE)))
                # Qt's selectedItems() excludes hidden rows even when their
                # selected flag is retained. Preserve them through sorting a
                # filtered project, so clearing the filter restores selection.
                for parent in self._items_by_uid.values()
                for item in [parent, *(parent.child(i) for i in range(parent.childCount()))]
                if item.isSelected()
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
            for key, expanded in state.get("expanded_folders", {}).items():
                if item := self._folder_items_by_key.get(key):
                    item.setExpanded(expanded)

            def find(target):  # type: ignore[no-untyped-def]
                if not target or (item := self._items_by_uid.get(target[0])) is None:
                    return None
                if target[1] >= 0:
                    return item.child(target[1]) if target[1] < item.childCount() else None
                return item

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
            (str(item.data(0, UID_ROLE)), item.toolTip(0) if item.data(0, KIND_ROLE) == "video" else None)
            for item in self.tree.selectedItems()
        } if previous is not None and previous[0] == selected_uid else set()
        expanded = {
            uid: item.isExpanded() for uid, item in self._items_by_uid.items()
        }
        expanded_folders = {
            key: item.isExpanded() for key, item in self._folder_items_by_key.items()
        }
        if selected_video_index is _SELECTION_UNSET:
            selected_video_index = (
                previous[1]
                if previous is not None and previous[0] == selected_uid and previous[1] >= 0
                else None
            )
        self._cases = list(cases)
        self._cases_by_uid = {case.uid: case for case in cases}
        self._active_video_item = None
        self._active_uid = None
        self._items_by_uid.clear()
        self._folder_items_by_key.clear()
        self._videos_by_uid.clear()
        self._missing_counts.clear()
        self._completed_uids.clear()
        blocked = self.tree.blockSignals(True)
        updates = self.tree.updatesEnabled()
        self.tree.setUpdatesEnabled(False)
        try:
            self.tree.clear()
            grouped = self._grouped()
            # 현재 영상 폴더 범위의 사고만 항목으로 만든다(범위 밖은 숨기지 않고 생략).
            scoped = [case for case in cases if self._case_in_scope(case)]
            detached: list[QTreeWidgetItem] = []
            for case in scoped:
                item = self._new_case_item(case, attach=grouped)
                self._update_case_row(case, item)
                self._sync_video_items(case, item)
                if grouped:
                    item.setExpanded(expanded.get(case.uid, True))
                else:
                    detached.append(item)
            if detached:
                # 한 번에 삽입해야 행마다 영속 인덱스를 갱신하는 비용이 생기지 않는다.
                self.tree.addTopLevelItems(detached)
                for case in scoped:
                    self._items_by_uid[case.uid].setExpanded(expanded.get(case.uid, True))
            selected_item: QTreeWidgetItem | None = None
            selected_case_item: QTreeWidgetItem | None = None
            for case in scoped:
                item = self._items_by_uid[case.uid]
                if case.uid == selected_uid:
                    selected_case_item = item
                    if selected_video_index is None:
                        selected_item = item
                    elif (isinstance(selected_video_index, int)
                          and 0 <= selected_video_index < item.childCount()):
                        selected_item = item.child(selected_video_index)
                if selected_targets:
                    for index in range(item.childCount()):
                        child = item.child(index)
                        if (case.uid, child.toolTip(0)) in selected_targets:
                            child.setSelected(True)
                    if (case.uid, None) in selected_targets:
                        item.setSelected(True)
            for key, item in self._folder_items_by_key.items():
                item.setExpanded(expanded_folders.get(key, True))
            self.tree.set_case_order([case.uid for case in cases])
            if selected_item is None:
                selected_item = selected_case_item
            if selected_item:
                if selected_targets:
                    self.tree.setCurrentItem(selected_item, 0, QItemSelectionModel.SelectionFlag.NoUpdate)
                else:
                    self.tree.setCurrentItem(selected_item)
            self.set_active_video(selected_uid)
            self._update_count_label()
        finally:
            self.tree.setUpdatesEnabled(updates)
            self.tree.blockSignals(blocked)
        self._filter(self.search.text())
        self._update_delete_button()

    def update_case(
        self,
        case: InvestigationCase,
        active_uid: str | None | object = _SELECTION_UNSET,
    ) -> bool:
        """Refresh one existing row without rebuilding or changing selection."""

        if case.uid not in self._cases_by_uid:
            return False
        item = self._items_by_uid.get(case.uid)
        in_scope = self._case_in_scope(case)
        if item is None or not in_scope:
            if item is None and not in_scope:
                # 범위 밖 사고는 행이 없지만 탭 전환 때 쓸 최신 객체로는 바꿔 둔다.
                self._replace_case_reference(case)
                return False
            # 다른 폴더 영상 추가·삭제로 범위 소속이 바뀌면 병합으로 행을 추가·제거한다.
            self._replace_case_reference(case)
            self.merge_cases(
                self._cases, self._active_uid if active_uid is _SELECTION_UNSET else active_uid,
            )
            return case.uid in self._items_by_uid
        self._replace_case_reference(case)
        blocked = self.tree.blockSignals(True)
        try:
            self._update_case_row(case, item)
            if self._videos_by_uid.get(case.uid) != tuple(case.videos):
                self._sync_video_items(case, item)
                if self._grouped():
                    self._arrange_case_items()
                else:
                    self._filter_item(item, self.search.text().strip().casefold())
            else:
                parent = item.parent()
                needle = self.search.text().strip().casefold()
                folder_matches = parent is not None and bool(needle) and needle in parent.text(0).casefold()
                was_hidden = item.isHidden()
                self._filter_item(item, "" if folder_matches else needle)
                if parent is not None and was_hidden != item.isHidden():
                    parent.setHidden(all(parent.child(index).isHidden() for index in range(parent.childCount())))
            self.set_active_video(self._active_uid if active_uid is _SELECTION_UNSET else active_uid)
            self._update_count_label()
        finally:
            self.tree.blockSignals(blocked)
        self._update_delete_button()
        return True

    def _replace_case_reference(self, case: InvestigationCase) -> None:
        previous_case = self._cases_by_uid.get(case.uid)
        self._cases_by_uid[case.uid] = case
        if previous_case is not case:
            for index, previous in enumerate(self._cases):
                if previous.uid == case.uid:
                    self._cases[index] = case
                    break

    def _remove_case_item(self, uid: str) -> None:
        item = self._items_by_uid.pop(uid, None)
        if item is None:
            return
        if self._active_video_item is not None and self._active_video_item.parent() is item:
            self._active_video_item = None
        parent = item.parent()
        if parent is not None:
            parent.takeChild(parent.indexOfChild(item))
        else:
            self.tree.takeTopLevelItem(self.tree.indexOfTopLevelItem(item))
        self._videos_by_uid.pop(uid, None)
        self._missing_counts.pop(uid, None)
        self._completed_uids.discard(uid)

    def merge_cases(self, cases: list[InvestigationCase], active_uid: str | None) -> None:
        """Append watched accidents and refresh changed rows without a tree reset."""
        self._cases = list(cases)
        if not self._grouped():
            self._merge_flat_cases(cases, active_uid)
            return
        blocked = self.tree.blockSignals(True)
        try:
            remaining = {case.uid for case in cases}
            for uid in self._items_by_uid.keys() - remaining:
                self._remove_case_item(uid)
                self._cases_by_uid.pop(uid, None)
            for case in cases:
                if self._cases_by_uid.get(case.uid) is case and self._videos_by_uid.get(case.uid) == tuple(case.videos):
                    continue
                if case.uid in self._items_by_uid:
                    self.update_case(case)
                    continue
                item = self._new_case_item(case)
                self._cases_by_uid[case.uid] = case
                self._update_case_row(case, item)
                self._sync_video_items(case, item)
                item.setExpanded(True)
                self._filter_item(item, self.search.text().strip().casefold())
            self._arrange_case_items()
            self.set_active_video(active_uid)
            self._update_count_label()
        finally:
            self.tree.blockSignals(blocked)
        self._update_delete_button()

    def _merge_flat_cases(self, cases: list[InvestigationCase], active_uid: str | None) -> None:
        """범위 필터 모드의 증분 병합: 바뀐 행만 갱신하고 새 행은 한 번에 덧붙인다."""
        blocked = self.tree.blockSignals(True)
        try:
            remaining = {case.uid for case in cases}
            for uid in self._items_by_uid.keys() - remaining:
                self._remove_case_item(uid)
            for uid in self._cases_by_uid.keys() - remaining:
                del self._cases_by_uid[uid]
            needle = self.search.text().strip().casefold()
            new_items: list[QTreeWidgetItem] = []
            for case in cases:
                previous = self._cases_by_uid.get(case.uid)
                item = self._items_by_uid.get(case.uid)
                unchanged = previous is case and self._videos_by_uid.get(case.uid) == tuple(case.videos)
                self._cases_by_uid[case.uid] = case
                if unchanged:
                    continue
                in_scope = self._case_in_scope(case)
                if not in_scope:
                    self._remove_case_item(case.uid)
                    continue
                if item is not None:
                    self._update_case_row(case, item)
                    if self._videos_by_uid.get(case.uid) != tuple(case.videos):
                        self._sync_video_items(case, item)
                    self._filter_item(item, needle)
                    continue
                item = self._new_case_item(case, attach=False)
                self._update_case_row(case, item)
                self._sync_video_items(case, item)
                new_items.append(item)
            if new_items:
                self.tree.addTopLevelItems(new_items)
                for item in new_items:
                    item.setExpanded(True)
                    self._filter_item(item, needle)
            # 새 사고가 끝에 추가되는 일반적인 경우에는 재배치하지 않는다.
            self._arrange_flat_items()
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
        # 행은 현재 범위의 사고에만 있으므로 입력 한 번마다 전체 사고를 다시 훑지 않는다.
        total = len(self._items_by_uid)
        self.count_label.setText(f"{len(self._completed_uids)}/{total} 완료")
        self.sort_button.setEnabled(bool(total))

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
            child.setData(0, KIND_ROLE, "video")
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
        """Move one accident in global order, retaining its filesystem group."""
        source = next((index for index, case in enumerate(self._cases) if case.uid == uid), -1)
        if source < 0:
            return False
        target = max(0, min(int(destination), len(self._cases) - 1))
        if source == target:
            return False
        self.move_cases([uid], target)
        return True

    def move_cases(self, uids: list[str], destination: int) -> None:
        """Move the selected block without rebuilding accident/video objects."""
        tree = self.tree
        moving = [case for case in self._cases if case.uid in uids]
        if not moving:
            return
        remaining = [case for case in self._cases if case.uid not in uids]
        target = max(0, min(int(destination), len(remaining)))
        self._cases = remaining[:target] + moving + remaining[target:]
        blocked = tree.blockSignals(True)
        tree.setUpdatesEnabled(False)
        try:
            self._arrange_case_items()
        finally:
            tree.setUpdatesEnabled(True)
            tree.blockSignals(blocked)
        tree.viewport().update()

    def _selection_changed(self) -> None:
        item = self.tree.currentItem()
        self._update_delete_button()
        if item is None or item.data(0, KIND_ROLE) == "folder":
            return
        if QApplication.keyboardModifiers() & (
            Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.ShiftModifier
        ):
            self._navigation_deferred = True
            return
        self._navigation_deferred = False
        uid = item.data(0, UID_ROLE)
        video_index = int(item.data(0, VIDEO_INDEX_ROLE) or 0)
        if item.data(0, KIND_ROLE) == "video" and video_index >= 0:
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
        return str(uid), video_index if item.data(0, KIND_ROLE) == "video" else -1

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
        if item.data(0, KIND_ROLE) != "video":
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

    def _search_text_changed(self, text: str) -> None:
        if self.search.hasFocus():
            self._search_timer.start()
            return
        self._search_timer.stop()
        self._filter(text)

    def flush_search(self) -> None:
        """입력 대기 중인 검색어를 즉시 적용한다."""
        if self._search_timer.isActive():
            self._search_timer.stop()
            self._filter(self.search.text())

    def clear_search(self) -> None:
        """검색어를 지우고 입력 지연 없이 바로 전체 목록을 보인다."""
        self._search_timer.stop()
        blocked = self.search.blockSignals(True)
        try:
            self.search.clear()
        finally:
            self.search.blockSignals(blocked)
        self._filter("")

    def _filter(self, text: str) -> None:
        needle = text.strip().casefold()
        for item in self._items_by_uid.values():
            parent = item.parent()
            folder_matches = parent is not None and bool(needle) and needle in parent.text(0).casefold()
            self._filter_item(item, "" if folder_matches else needle)
        for folder in self._folder_items_by_key.values():
            folder.setHidden(all(folder.child(index).isHidden() for index in range(folder.childCount())))

    def _filter_item(self, item: QTreeWidgetItem, needle: str) -> None:
        matches = needle in item.text(0).casefold()
        case = self._cases_by_uid.get(str(item.data(0, UID_ROLE)))
        for child_index in range(item.childCount()):
            child = item.child(child_index)
            child_match = needle in child.text(0).casefold()
            in_scope = self._video_filter is None or self._video_filter(child.toolTip(0))
            child.setHidden(not in_scope or (bool(needle) and not child_match))
            matches = matches or (in_scope and child_match)
        in_scope = case is not None and self._case_in_scope(case)
        item.setHidden(not in_scope or (bool(needle) and not matches))


__all__ = ["CaseListPanel", "CaseTreeWidget", "KIND_ROLE"]
