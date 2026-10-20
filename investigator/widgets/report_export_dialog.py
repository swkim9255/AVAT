"""Choose the current project's accidents to export as individual Word reports."""

from __future__ import annotations

from collections.abc import Iterable

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QDialogButtonBox,
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
from investigator.theme import MUTED, material_icon


class ReportExportDialog(QDialog):
    """Keep export selection independent from navigation and completion status."""

    def __init__(
        self,
        cases: list[InvestigationCase],
        selected_uids: Iterable[str] = (),
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("사고 분석 보고서 내보내기")
        self.setMinimumSize(580, 420)
        self.resize(680, 540)
        self._cases = list(cases)
        self._items: dict[str, QTreeWidgetItem] = {}

        layout = QVBoxLayout(self)
        description = QLabel(
            "보고서로 만들 사고를 선택해 주세요. 사고별로 Word 파일(.docx)을 생성합니다.\n"
            "현재 입력된 내용을 사용하며, 미입력 항목은 -로 표시합니다. 완료 여부는 변경하지 않습니다.",
            self,
        )
        description.setWordWrap(True)
        layout.addWidget(description)

        self.search = QLineEdit(self)
        self.search.setObjectName("reportCaseSearch")
        self.search.setStyleSheet(
            "QLineEdit#reportCaseSearch QToolButton {"
            " min-width: 20px; max-width: 20px; min-height: 20px; max-height: 20px;"
            " padding: 0; margin: 0; border: 0; background: transparent;"
            "}"
        )
        self.search.setPlaceholderText("사고번호 또는 영상 검색")
        self.search.addAction(
            material_icon("search", MUTED), QLineEdit.ActionPosition.LeadingPosition
        )
        self.search.textChanged.connect(self._filter)
        layout.addWidget(self.search)

        selection_row = QHBoxLayout()
        self.select_all_button = QPushButton("전체 선택", self)
        self.select_all_button.clicked.connect(lambda: self._set_all_checked(True))
        selection_row.addWidget(self.select_all_button)
        self.clear_button = QPushButton("전체 해제", self)
        self.clear_button.clicked.connect(lambda: self._set_all_checked(False))
        selection_row.addWidget(self.clear_button)
        selection_row.addStretch(1)
        self.count_label = QLabel(self)
        selection_row.addWidget(self.count_label)
        layout.addLayout(selection_row)

        self.tree = QTreeWidget(self)
        self.tree.setObjectName("reportCaseTree")
        self.tree.setColumnCount(3)
        self.tree.setHeaderLabels(["사고번호", "상태", "영상 수"])
        self.tree.setRootIsDecorated(False)
        self.tree.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.tree.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.tree.header().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self.tree.header().setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        checked = set(selected_uids)
        for case in self._cases:
            item = QTreeWidgetItem([
                case.display_id,
                "완료" if case.status == "completed" else "작성 중",
                str(len(case.videos)),
            ])
            item.setData(0, Qt.ItemDataRole.UserRole, case.uid)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(
                0, Qt.CheckState.Checked if case.uid in checked else Qt.CheckState.Unchecked
            )
            item.setToolTip(0, "\n".join([case.display_id, *case.videos]))
            self._items[case.uid] = item
            self.tree.addTopLevelItem(item)
        self.tree.itemChanged.connect(self._update_selection)
        layout.addWidget(self.tree, 1)

        note = QLabel("동일한 이름의 보고서가 있으면 번호를 붙여 기존 파일을 유지합니다.", self)
        note.setWordWrap(True)
        note.setStyleSheet(f"color: {MUTED};")
        layout.addWidget(note)

        self.buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel,
            parent=self,
        )
        self.export_button = self.buttons.button(QDialogButtonBox.StandardButton.Ok)
        self.export_button.setText("저장 폴더 선택…")
        self.buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("취소")
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)
        self._update_selection()

    def selected_uids(self) -> list[str]:
        """Return checked accidents in project order, including filtered rows."""
        return [
            case.uid for case in self._cases
            if self._items[case.uid].checkState(0) == Qt.CheckState.Checked
        ]

    def accept(self) -> None:
        if self.selected_uids():
            super().accept()

    def _update_selection(self) -> None:
        count = len(self.selected_uids())
        self.count_label.setText(f"{len(self._cases)}건 중 {count}건 선택")
        self.export_button.setEnabled(count > 0)

    def _set_all_checked(self, checked: bool) -> None:
        blocked = self.tree.blockSignals(True)
        try:
            state = Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked
            for item in self._items.values():
                item.setCheckState(0, state)
        finally:
            self.tree.blockSignals(blocked)
        self._update_selection()

    def _filter(self, text: str) -> None:
        needle = text.strip().casefold()
        for case in self._cases:
            haystack = "\n".join([case.display_id, *case.videos]).casefold()
            self._items[case.uid].setHidden(bool(needle) and needle not in haystack)
