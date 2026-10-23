"""Choose the current project's accidents to export as individual Word reports."""

from __future__ import annotations

from collections.abc import Iterable

from PySide6.QtCore import QDate, Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCalendarWidget,
    QCheckBox,
    QDateEdit,
    QDialog,
    QDialogButtonBox,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QPushButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from investigator.form_definition import FormDefinition
from investigator.models import InvestigationCase
from investigator.statistics import SEVERITY_KEY, StatisticsEngine, StatsCondition, StatsFilter
from investigator.statistics_conditions import condition_text
from investigator.theme import MUTED, material_icon
from investigator.widgets.scroll_safe_combo import ScrollSafeComboBox
from investigator.widgets.statistics_panel import StatisticsConditionDialog as ReportConditionDialog


class ReportExportDialog(QDialog):
    """Keep export selection independent from navigation and completion status."""

    def __init__(
        self,
        cases: list[InvestigationCase],
        selected_uids: Iterable[str] = (),
        parent: QWidget | None = None,
        *,
        definition: FormDefinition | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("사고 분석 보고서 내보내기")
        self.setMinimumSize(640, 600)
        available = self.screen().availableGeometry()
        self.resize(min(760, max(640, available.width() - 48)),
                    min(760, max(600, available.height() - 64)))
        self._cases = list(cases)
        self._items: dict[str, QTreeWidgetItem] = {}
        definition = definition or next(
            (case.form_definition for case in cases if case.form_definition), None
        ) or FormDefinition.default()
        self.engine = StatisticsEngine(self._cases, definition)
        self.conditions: list[StatsCondition] = []
        self._matching_uids = {case.uid for case in self._cases}
        self._filter_error: str | None = None
        self._updating_conditions = False

        layout = QVBoxLayout(self)
        description = QLabel(
            "보고서로 만들 사고를 선택해 주세요. 사고별로 Word 파일(.docx)을 생성합니다.\n"
            "현재 입력된 내용을 사용하며, 미입력 항목은 -로 표시합니다. 완료 여부는 변경하지 않습니다.",
            self,
        )
        description.setWordWrap(True)
        layout.addWidget(description)

        self._build_conditions(layout)

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
        self.select_all_button = QPushButton("조건 일치 전체 선택", self)
        self.select_all_button.setToolTip("검색과 무관하게 현재 조건에 맞는 사고를 모두 선택합니다.")
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
        self.tree.setMinimumHeight(140)
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
        if not self._filter_error and self.selected_uids():
            super().accept()

    def _update_selection(self) -> None:
        count = len(self.selected_uids())
        if self._filter_error:
            self.count_label.setText(f"조건 확인 필요 · {count}건 선택")
        elif self._has_conditions():
            self.count_label.setText(
                f"전체 {len(self._cases)}건 · 조건 일치 {len(self._matching_uids)}건 · {count}건 선택"
            )
        else:
            self.count_label.setText(f"{len(self._cases)}건 중 {count}건 선택")
        hidden = sum(self._items[uid].isHidden() for uid in self.selected_uids())
        self.count_label.setToolTip(
            f"검색에서 숨겨진 선택 사고 {hidden}건도 보고서에 포함됩니다."
            if hidden else "체크된 사고만 보고서로 생성합니다."
        )
        self.export_button.setEnabled(count > 0 and not self._filter_error)

    def _set_all_checked(self, checked: bool) -> None:
        if checked and self._filter_error:
            return
        blocked = self.tree.blockSignals(True)
        self.tree.setUpdatesEnabled(False)
        try:
            for uid, item in self._items.items():
                state = (Qt.CheckState.Checked if checked and uid in self._matching_uids
                         else Qt.CheckState.Unchecked)
                item.setCheckState(0, state)
        finally:
            self.tree.blockSignals(blocked)
            self.tree.setUpdatesEnabled(True)
        self._update_selection()

    def _filter(self, text: str) -> None:
        needle = text.strip().casefold()
        for case in self._cases:
            haystack = "\n".join([case.display_id, *case.videos]).casefold()
            self._items[case.uid].setHidden(
                case.uid not in self._matching_uids or bool(needle) and needle not in haystack
            )
        self._update_selection()

    def _build_conditions(self, root: QVBoxLayout) -> None:
        frame = QFrame(self)
        frame.setObjectName("card")
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(8)
        title = QLabel("조건으로 사고 선택", frame)
        title.setObjectName("statTitle")
        layout.addWidget(title)
        row = QHBoxLayout()
        row.addWidget(QLabel("상태", frame))
        self.status = ScrollSafeComboBox(frame)
        self.status.setObjectName("reportStatusFilter")
        for text, value in (("전체", "all"), ("완료", "completed"), ("작성 중", "draft")):
            self.status.addItem(text, value)
        row.addWidget(self.status, 1)
        row.addWidget(QLabel("추가 조건", frame))
        self.match_mode = ScrollSafeComboBox(frame)
        self.match_mode.setObjectName("reportConditionMode")
        self.match_mode.addItem("모두 충족 (AND)", "all")
        self.match_mode.addItem("하나라도 충족 (OR)", "any")
        row.addWidget(self.match_mode, 1)
        layout.addLayout(row)
        period_row = QHBoxLayout()
        self.period = QCheckBox("사고일자", frame)
        period_row.addWidget(self.period)
        dates = [QDate.fromString(str(case.values.get("incident_date", "")), "yyyyMMdd")
                 for case in self._cases]
        dates = [date for date in dates if date.isValid()]
        initial_dates = (min(dates), max(dates)) if dates else (QDate.currentDate(),) * 2
        self.date_from = QDateEdit(frame)
        self.date_to = QDateEdit(frame)
        for edit, initial in zip((self.date_from, self.date_to), initial_dates, strict=True):
            edit.setDateRange(QDate(1, 1, 1), QDate(9999, 12, 31))
            edit.setDate(initial)
            edit.setDisplayFormat("yyyy-MM-dd")
            edit.setCalendarPopup(True)
            edit.calendarWidget().setFirstDayOfWeek(Qt.DayOfWeek.Sunday)
            edit.calendarWidget().setVerticalHeaderFormat(
                QCalendarWidget.VerticalHeaderFormat.NoVerticalHeader
            )
            edit.setEnabled(False)
        period_row.addWidget(self.date_from, 1)
        period_row.addWidget(QLabel("~", frame))
        period_row.addWidget(self.date_to, 1)
        layout.addLayout(period_row)
        action_row = QHBoxLayout()
        self.add_condition_button = QPushButton("+ 조사 항목 조건", frame)
        self.add_condition_button.clicked.connect(self.add_condition_dialog)
        action_row.addWidget(self.add_condition_button)
        self.remove_condition_button = QPushButton("선택 조건 삭제", frame)
        self.remove_condition_button.setEnabled(False)
        self.remove_condition_button.clicked.connect(self.remove_condition)
        action_row.addWidget(self.remove_condition_button)
        self.reset_conditions_button = QPushButton("조건 초기화", frame)
        self.reset_conditions_button.clicked.connect(self.clear_conditions)
        action_row.addWidget(self.reset_conditions_button)
        action_row.addStretch(1)
        layout.addLayout(action_row)
        self.condition_list = QListWidget(frame)
        self.condition_list.setObjectName("reportConditions")
        self.condition_list.setMinimumHeight(70)
        self.condition_list.setMaximumHeight(100)
        self.condition_list.setVisible(False)
        self.condition_list.currentRowChanged.connect(
            lambda row: self.remove_condition_button.setEnabled(row >= 0)
        )
        layout.addWidget(self.condition_list)
        help_text = QLabel(
            "조건을 바꾸면 일치하는 사고만 자동 체크됩니다. 체크 후 개별 해제할 수 있습니다.\n"
            "상태·기간은 항상 함께 적용하며, 검색은 목록 표시만 변경합니다.", frame,
        )
        help_text.setWordWrap(True)
        help_text.setStyleSheet(f"color: {MUTED}; font-size: 12px;")
        layout.addWidget(help_text)
        self.condition_error = QLabel(frame)
        self.condition_error.setObjectName("warningNote")
        self.condition_error.setWordWrap(True)
        self.condition_error.hide()
        layout.addWidget(self.condition_error)
        root.addWidget(frame)
        self.status.currentIndexChanged.connect(self._conditions_changed)
        self.match_mode.currentIndexChanged.connect(self._conditions_changed)
        self.period.toggled.connect(self._conditions_changed)
        self.date_from.dateChanged.connect(self._conditions_changed)
        self.date_to.dateChanged.connect(self._conditions_changed)

    def _has_conditions(self) -> bool:
        return bool(self.conditions or self.period.isChecked() or self.status.currentData() != "all")

    def add_condition_dialog(self) -> None:
        # FPS-dependent derived values are intentionally excluded: reports do
        # not need to open/decode every video merely to select investigations.
        fields = {spec.key for spec in self.engine.definition.fields} | {SEVERITY_KEY}
        dialog = ReportConditionDialog(
            self.engine, self, field_keys=fields, title="보고서 선택 조건 추가"
        )
        if dialog.exec() == QDialog.DialogCode.Accepted and dialog.condition:
            self.add_condition(dialog.condition)

    def add_condition(self, condition: StatsCondition) -> None:
        if condition not in self.conditions:
            self.conditions.append(condition)
            self._conditions_changed()

    def remove_condition(self) -> None:
        row = self.condition_list.currentRow()
        if 0 <= row < len(self.conditions):
            self.conditions.pop(row)
            self._conditions_changed()

    def clear_conditions(self) -> None:
        self._updating_conditions = True
        try:
            self.conditions.clear()
            self.status.setCurrentIndex(self.status.findData("all"))
            self.match_mode.setCurrentIndex(self.match_mode.findData("all"))
            self.period.setChecked(False)
        finally:
            self._updating_conditions = False
        self._conditions_changed()

    def _condition_text(self, condition: StatsCondition) -> str:
        return condition_text(self.engine, condition)

    def _conditions_changed(self, *_args) -> None:  # type: ignore[no-untyped-def]
        if self._updating_conditions:
            return
        self.date_from.setEnabled(self.period.isChecked())
        self.date_to.setEnabled(self.period.isChecked())
        self.condition_list.clear()
        self.condition_list.addItems([self._condition_text(c) for c in self.conditions])
        self.condition_list.setVisible(bool(self.conditions))
        if self.conditions:
            self.condition_list.setCurrentRow(len(self.conditions) - 1)
            for index in range(self.condition_list.count()):
                item = self.condition_list.item(index)
                item.setToolTip(item.text())
        try:
            candidates = self.engine.filtered_cases(StatsFilter(
                status=self.status.currentData(),
                date_from=self.date_from.date().toString("yyyyMMdd") if self.period.isChecked() else "",
                date_to=self.date_to.date().toString("yyyyMMdd") if self.period.isChecked() else "",
                conditions=tuple(self.conditions),
                condition_mode=self.match_mode.currentData(),
            ))
            self._matching_uids = {case.uid for case in candidates}
            self._filter_error = None
        except ValueError as exc:
            self._filter_error = str(exc)
        self.condition_error.setText(self._filter_error or "")
        self.condition_error.setVisible(bool(self._filter_error))
        self.select_all_button.setEnabled(not self._filter_error)
        self._filter(self.search.text())
        if not self._filter_error:
            self._set_all_checked(True)
