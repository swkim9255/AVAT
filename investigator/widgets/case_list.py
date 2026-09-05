"""Accident and attached-video navigation panel."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QPoint, Qt, Signal
from PySide6.QtGui import QBrush, QColor
from PySide6.QtWidgets import (
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


class CaseListPanel(QWidget):
    case_selected = Signal(str)
    video_selected = Signal(str, int)
    new_case_requested = Signal()
    video_folder_requested = Signal()
    # Kept for API compatibility with integrations that add individual files.
    add_video_requested = Signal()
    delete_case_requested = Signal(str)
    delete_video_requested = Signal(str, int)
    export_requested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("panel")
        self.setMinimumWidth(240)
        self.setMaximumWidth(390)
        self._cases: list[InvestigationCase] = []

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

        self.tree = QTreeWidget(self)
        self.tree.setObjectName("caseTree")
        self.tree.setHeaderHidden(True)
        self.tree.setColumnCount(2)
        self.tree.header().setStretchLastSection(False)
        self.tree.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.tree.header().setSectionResizeMode(1, QHeaderView.ResizeMode.Fixed)
        self.tree.setColumnWidth(1, 34)
        self.tree.setIndentation(18)
        self.tree.setAnimated(True)
        self.tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.tree.itemSelectionChanged.connect(self._selection_changed)
        self.tree.customContextMenuRequested.connect(self._show_context_menu)
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
                child.setToolTip(0, path)
                if case.uid == selected_uid and index == case.active_video_index:
                    child.setForeground(0, QBrush(QColor(PRIMARY)))
                if case.uid == selected_uid and index == selected_video_index:
                    selected_item = child
            item.setExpanded(True)
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

    def _selection_changed(self) -> None:
        item = self.tree.currentItem()
        self._update_delete_button()
        if item is None:
            return
        uid = item.data(0, UID_ROLE)
        video_index = int(item.data(0, VIDEO_INDEX_ROLE) or 0)
        if item.parent() is not None and video_index >= 0:
            self.video_selected.emit(str(uid), video_index)
        else:
            self.case_selected.emit(str(uid))

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


__all__ = ["CaseListPanel"]
