"""Accident and attached-video navigation panel."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QBrush, QColor
from PySide6.QtWidgets import (
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
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


class CaseListPanel(QWidget):
    case_selected = Signal(str)
    video_selected = Signal(str, int)
    new_case_requested = Signal()
    add_video_requested = Signal()
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
        self.tree.itemSelectionChanged.connect(self._selection_changed)
        root.addWidget(self.tree, 1)

        button_row = QHBoxLayout()
        self.new_button = QPushButton(material_icon("add", PRIMARY), "새 사고", self)
        self.new_button.setObjectName("newCaseButton")
        self.new_button.clicked.connect(self.new_case_requested)
        self.video_button = QPushButton(material_icon("video_file", PRIMARY), "영상 추가", self)
        self.video_button.setObjectName("addVideoButton")
        self.video_button.clicked.connect(self.add_video_requested)
        button_row.addWidget(self.new_button)
        button_row.addWidget(self.video_button)
        root.addLayout(button_row)

        self.export_button = QPushButton(
            material_icon("download", PRIMARY), "조사 결과 CSV 내보내기", self
        )
        self.export_button.setObjectName("exportCsvButton")
        self.export_button.clicked.connect(self.export_requested)
        root.addWidget(self.export_button)

    def set_cases(self, cases: list[InvestigationCase], selected_uid: str | None = None) -> None:
        self._cases = cases
        self.tree.blockSignals(True)
        self.tree.clear()
        selected_item: QTreeWidgetItem | None = None
        completed = sum(case.status == "completed" for case in cases)
        self.count_label.setText(f"{completed}/{len(cases)} 완료")
        for case in cases:
            item = QTreeWidgetItem(self.tree)
            item.setText(0, case.display_id)
            item.setData(0, UID_ROLE, case.uid)
            item.setData(0, VIDEO_INDEX_ROLE, -1)
            item.setIcon(0, material_icon("description", PRIMARY))
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
                if index == case.active_video_index:
                    child.setForeground(0, QBrush(QColor(PRIMARY)))
            item.setExpanded(True)
            if case.uid == selected_uid:
                selected_item = item
        if selected_item:
            self.tree.setCurrentItem(selected_item)
        self.tree.blockSignals(False)
        self._filter(self.search.text())

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
        if item is None:
            return
        uid = item.data(0, UID_ROLE)
        video_index = int(item.data(0, VIDEO_INDEX_ROLE) or 0)
        if item.parent() is not None and video_index >= 0:
            self.video_selected.emit(str(uid), video_index)
        else:
            self.case_selected.emit(str(uid))

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
