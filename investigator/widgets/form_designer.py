"""A staged, workspace-local investigation form editor."""

from __future__ import annotations

from copy import deepcopy
from uuid import uuid4

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from investigator.form_definition import FormDefinition, new_field, required_locked, type_locked
from investigator.schema import FIELD_BY_KEY, FieldKind
from investigator.widgets.scroll_safe_combo import ScrollSafeComboBox

INPUT_TYPES = (
    ("드롭다운 (선택형)", FieldKind.SINGLE),
    ("숫자 입력 (정수)", FieldKind.DIGITS),
    ("숫자 입력 (소수)", FieldKind.DECIMAL),
    ("텍스트 입력", FieldKind.TEXT),
    ("텍스트 입력 (여러 줄)", FieldKind.LONG_TEXT),
    ("체크박스 / 다중 선택", FieldKind.MULTI),
)


class FormDesigner(QDialog):
    def __init__(
        self,
        definition: FormDefinition,
        parent: QWidget | None = None,
        *,
        initial_definition: FormDefinition | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("조사 항목 편집")
        self.resize(1000, 740)
        self.definition = FormDefinition.from_dict(definition.to_dict())
        self.initial_definition = FormDefinition.from_dict(
            (initial_definition or FormDefinition.default()).to_dict()
        )
        # The immutable initial snapshot can itself contain the previous
        # automatic expansion, even after being saved in the current format.
        self.initial_definition._restore_automatic_adult_expansion()
        self._original_columns = {f["key"]: f["csv_column"] for f in definition.records()}
        self._selected: tuple[int, int | None] | None = None
        self._loading = False
        root = QVBoxLayout(self)
        help_text = QLabel(
            "현재 작업 폴더의 조사 양식을 편집합니다. 적용하면 설정과 조사 데이터가 함께 저장됩니다.\n"
            "선택지 이름·저장값·개별 CSV 칼럼을 수정할 수 있습니다. 저장값 변경 시 기존 선택도 함께 옮깁니다.\n"
            "기본값은 이후 새로 생성·가져오는 사고에만 적용됩니다. 기존 입력값은 유지됩니다.",
            self,
        )
        help_text.setWordWrap(True)
        root.addWidget(help_text)
        body = QHBoxLayout()
        left = QVBoxLayout()
        self.tree = QTreeWidget(self)
        self.tree.setHeaderLabel("탭 / 입력 항목")
        self.tree.setMinimumWidth(240)
        left.addWidget(self.tree)
        buttons = QHBoxLayout()
        self.add_tab_button = QPushButton("탭 추가", self)
        self.add_field_button = QPushButton("항목 추가", self)
        buttons.addWidget(self.add_tab_button)
        buttons.addWidget(self.add_field_button)
        left.addLayout(buttons)
        move_buttons = QHBoxLayout()
        self.move_up_button = QPushButton("▲", self)
        self.move_down_button = QPushButton("▼", self)
        self.move_up_button.setToolTip("선택한 탭 또는 항목을 위로 이동")
        self.move_down_button.setToolTip("선택한 탭 또는 항목을 아래로 이동")
        self.move_up_button.setAccessibleName("선택한 탭 또는 항목 위로 이동")
        self.move_down_button.setAccessibleName("선택한 탭 또는 항목 아래로 이동")
        move_buttons.addWidget(self.move_up_button)
        move_buttons.addWidget(self.move_down_button)
        left.addLayout(move_buttons)
        body.addLayout(left, 1)
        self.pages = QStackedWidget(self)
        tab_page = QWidget(self)
        tab_form = QFormLayout(tab_page)
        self.tab_title = QLineEdit(self)
        self.tab_short_title = QLineEdit(self)
        self.tab_columns = ScrollSafeComboBox(self)
        self.tab_columns.addItem("1열 배치", 1)
        self.tab_columns.addItem("2열 배치", 2)
        tab_form.addRow("탭 제목", self.tab_title)
        tab_form.addRow("탭 라벨", self.tab_short_title)
        tab_form.addRow("항목 배치", self.tab_columns)
        layout_hint = QLabel(
            "2열에서는 목록 순서대로 왼쪽 → 오른쪽에 배치됩니다.\n항목의 ‘1열 배치’을 선택하면 해당 항목만 한 행을 차지합니다.",
            self,
        )
        layout_hint.setWordWrap(True)
        tab_form.addRow(layout_hint)
        self.pages.addWidget(tab_page)

        field_page = QWidget(self)
        field_layout = QVBoxLayout(field_page)
        form = QFormLayout()
        self.field_label = QLineEdit(self)
        self.column_name = QLineEdit(self)
        self.kind = ScrollSafeComboBox(self)
        for label, kind in INPUT_TYPES:
            self.kind.addItem(label, kind.value)
        self.required = QCheckBox("필수 입력", self)
        self.editable = QCheckBox("선택지 외 직접 입력 허용", self)
        self.full_width = QCheckBox("1열 배치 (2열 배치에서)", self)
        self.default_value = QTextEdit(self)
        self.default_value.setAcceptRichText(False)
        self.default_value.setMaximumHeight(95)
        self.default_value.setPlaceholderText(
            "기본값 없음 · 다중 선택은 저장값을 한 줄에 하나씩 입력"
        )
        form.addRow("항목 이름", self.field_label)
        form.addRow("CSV 칼럼명", self.column_name)
        form.addRow("입력 방식", self.kind)
        form.addRow("", self.required)
        form.addRow("", self.editable)
        form.addRow("배치", self.full_width)
        form.addRow("기본값", self.default_value)
        field_layout.addLayout(form)
        self.rule_note = QLabel(self)
        self.rule_note.setWordWrap(True)
        field_layout.addWidget(self.rule_note)
        self.choices = QTableWidget(0, 3, self)
        self.choices.setHorizontalHeaderLabels(["선택지 이름", "저장값", "개별 CSV 칼럼"])
        self.choices.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        field_layout.addWidget(self.choices, 1)
        choice_buttons = QHBoxLayout()
        self.add_choice_button = QPushButton("선택지 추가", self)
        self.remove_choice_button = QPushButton("선택지 삭제", self)
        choice_buttons.addWidget(self.add_choice_button)
        choice_buttons.addWidget(self.remove_choice_button)
        field_layout.addLayout(choice_buttons)
        self.pages.addWidget(field_page)
        body.addWidget(self.pages, 2)
        root.addLayout(body, 1)
        self.buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel, self
        )
        self.buttons.button(QDialogButtonBox.StandardButton.Save).setText("양식 적용 및 저장")
        self.buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("취소")
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        self.restore_button = self.buttons.addButton(
            "초기 설정 복원", QDialogButtonBox.ButtonRole.ResetRole
        )
        self.restore_button.setToolTip(
            "기본 탭·항목의 이름, 입력 방식, 기본값, 순서와 배치를 초기값으로 복원합니다. 추가한 항목과 조사 값은 보존합니다."
        )
        self.restore_button.clicked.connect(self.restore_initial)
        root.addWidget(self.buttons)
        self.tree.currentItemChanged.connect(self._selection_changed)
        self.add_tab_button.clicked.connect(self.add_tab)
        self.add_field_button.clicked.connect(self.add_field)
        self.move_up_button.clicked.connect(lambda: self.move_selected(-1))
        self.move_down_button.clicked.connect(lambda: self.move_selected(1))
        self.add_choice_button.clicked.connect(self.add_choice)
        self.remove_choice_button.clicked.connect(self.remove_choice)
        self.kind.currentIndexChanged.connect(self._update_type_controls)
        self._refresh_tree((0, None))

    def _refresh_tree(self, selected: tuple[int, int | None]) -> None:
        self.tree.blockSignals(True)
        self.tree.clear()
        target = None
        for ti, tab in enumerate(self.definition.tabs):
            item = QTreeWidgetItem(self.tree, [tab["title"]])
            item.setData(0, Qt.ItemDataRole.UserRole, (ti, None))
            if selected == (ti, None):
                target = item
            for fi, field in enumerate(tab["fields"]):
                child = QTreeWidgetItem(item, [field["label"]])
                child.setData(0, Qt.ItemDataRole.UserRole, (ti, fi))
                if selected == (ti, fi):
                    target = child
            item.setExpanded(True)
        self.tree.setCurrentItem(target)
        self.tree.blockSignals(False)
        self._selected = None
        if target:
            self._selection_changed(target, None)
            self.tree.scrollToItem(target)

    def _store_editor(self) -> None:
        if self._selected is None or self._loading:
            return
        ti, fi = self._selected
        tab = self.definition.tabs[ti]
        if fi is None:
            tab["title"] = self.tab_title.text().strip()
            tab["short_title"] = self.tab_short_title.text().strip()
            tab["columns"] = self.tab_columns.currentData()
        else:
            record = tab["fields"][fi]
            column = self.column_name.text().strip()
            old_column = self._original_columns.get(record["key"], "")
            if old_column and old_column != column:
                aliases = record.setdefault("aliases", [])
                if old_column not in aliases:
                    aliases.append(old_column)
            record["aliases"] = [a for a in record.get("aliases", []) if a != column]
            record.update(
                label=self.field_label.text().strip(),
                csv_column=column,
                kind=self.kind.currentData(),
                required=self.required.isChecked(),
                editable=self.editable.isChecked(),
                full_width=self.full_width.isChecked(),
            )
            choices = []
            for row in range(self.choices.rowCount()):
                original = self.choices.item(row, 0).data(Qt.ItemDataRole.UserRole) or {}
                choice = deepcopy(original)
                choice.update(zip(
                    ("label", "value", "csv_column"),
                    (self.choices.item(row, col).text().strip() for col in range(3)),
                    strict=True,
                ))
                choice.setdefault("id", original.get("value", choice["value"]))
                for current_key, alias_key in (
                    ("value", "value_aliases"), ("csv_column", "csv_aliases")
                ):
                    aliases = list(choice.get(alias_key, []))
                    old = original.get(current_key)
                    if old and old != choice[current_key] and old not in aliases:
                        aliases.append(old)
                    choice[alias_key] = [a for a in aliases if a != choice[current_key]]
                choices.append(choice)
                self.choices.item(row, 0).setData(Qt.ItemDataRole.UserRole, deepcopy(choice))
            record["choices"] = choices
            default = self.default_value.toPlainText().strip()
            record["default"] = (
                [s.strip() for s in default.splitlines() if s.strip()]
                if record["kind"] == FieldKind.MULTI
                else default or None
            )
            record["default"] = self.definition._migrate_value(record, record["default"])

    def _selection_changed(self, current, previous) -> None:  # type: ignore[no-untyped-def]
        self._store_editor()
        if previous is not None and self._selected is not None:
            ti, fi = self._selected
            tab = self.definition.tabs[ti]
            previous.setText(0, tab["title"] if fi is None else tab["fields"][fi]["label"])
        if current is None:
            return
        self._selected = tuple(current.data(0, Qt.ItemDataRole.UserRole))
        ti, fi = self._selected
        tab = self.definition.tabs[ti]
        self._loading = True
        self.add_field_button.setEnabled(ti != 0)
        if fi is None:
            self.pages.setCurrentIndex(0)
            self.tab_title.setText(tab["title"])
            self.tab_short_title.setText(tab["short_title"])
            self.tab_columns.setCurrentIndex(self.tab_columns.findData(tab.get("columns", 1)))
            self.tab_title.setEnabled(ti != 0)
            self.tab_short_title.setEnabled(ti != 0)
            self.tab_columns.setEnabled(True)
        else:
            self.pages.setCurrentIndex(1)
            record = tab["fields"][fi]
            self.field_label.setText(record["label"])
            self.column_name.setText(record["csv_column"])
            self.column_name.setPlaceholderText(
                "개별 CSV 칼럼을 사용 중 · 추가 집계 칼럼명 입력 가능"
            )
            self.kind.setCurrentIndex(self.kind.findData(record["kind"]))
            locked = type_locked(record["key"])
            self.kind.setEnabled(not locked)
            self.required.setChecked(record["required"])
            self.required.setEnabled(not required_locked(record["key"]))
            self.editable.setChecked(record["editable"])
            self.full_width.setChecked(record.get("full_width", False))
            self.full_width.setEnabled(True)
            self.editable.setEnabled(not locked)
            self.rule_note.setText(
                "입력 방식은 유지합니다. 선택지 이름·저장값·개별 CSV 칼럼은 변경할 수 있습니다."
                if locked
                else "저장값·개별 CSV 칼럼을 변경하면 기존 선택을 유지하며, 모든 사고의 다음 CSV 출력에 반영합니다."
            )
            default = record.get("default")
            self.default_value.setPlainText(
                "\n".join(default)
                if isinstance(default, list)
                else ("" if default is None else str(default))
            )
            calculated = (
                record["key"] == "incident_day" or "representative_injury_grade" in record["key"]
            )
            self.default_value.setEnabled(not calculated)
            self.choices.setRowCount(0)
            for choice in record["choices"]:
                self._add_choice_row(choice, existing=True)
        self._loading = False
        self._update_type_controls()
        self._update_move_buttons()

    def _update_move_buttons(self) -> None:
        if self._selected is None:
            self.move_up_button.setEnabled(False)
            self.move_down_button.setEnabled(False)
            return
        ti, fi = self._selected
        if fi is None:
            self.move_up_button.setEnabled(ti > 1)
            self.move_down_button.setEnabled(0 < ti < len(self.definition.tabs) - 1)
        else:
            self.move_up_button.setEnabled(fi > 0)
            self.move_down_button.setEnabled(fi < len(self.definition.tabs[ti]["fields"]) - 1)

    def move_selected(self, delta: int) -> None:
        if self._selected is None or delta not in {-1, 1}:
            return
        self._store_editor()
        ti, fi = self._selected
        if fi is None:
            target = ti + delta
            if ti == 0 or not 1 <= target < len(self.definition.tabs):
                return
            self.definition.tabs.insert(target, self.definition.tabs.pop(ti))
            self._refresh_tree((target, None))
        else:
            fields = self.definition.tabs[ti]["fields"]
            target = fi + delta
            if not 0 <= target < len(fields):
                return
            fields.insert(target, fields.pop(fi))
            self._refresh_tree((ti, target))

    def _update_type_controls(self) -> None:
        selectable = self.kind.currentData() in (FieldKind.SINGLE, FieldKind.MULTI)
        self.choices.setEnabled(selectable)
        self.add_choice_button.setEnabled(selectable)
        self.remove_choice_button.setEnabled(selectable)
        if self._selected and self._selected[1] is not None:
            ti, fi = self._selected
            self.editable.setEnabled(
                self.kind.currentData() == FieldKind.SINGLE
                and not type_locked(self.definition.tabs[ti]["fields"][fi]["key"])
            )

    def _add_choice_row(self, choice: dict, *, existing: bool) -> None:
        row = self.choices.rowCount()
        self.choices.insertRow(row)
        for col, key in enumerate(("label", "value", "csv_column")):
            item = QTableWidgetItem(choice.get(key, ""))
            if col == 0:
                item.setData(Qt.ItemDataRole.UserRole, deepcopy(choice))
            self.choices.setItem(row, col, item)

    def add_choice(self) -> None:
        choice = {"label": "새 선택지", "value": "option_" + uuid4().hex[:8]}
        if self._selected and self._selected[1] is not None:
            ti, fi = self._selected
            base = FIELD_BY_KEY.get(self.definition.tabs[ti]["fields"][fi]["key"])
            if base and any(c.csv_column for c in base.choices):
                choice["csv_column"] = "custom_choice_" + uuid4().hex[:8]
        self._add_choice_row(choice, existing=False)

    def remove_choice(self) -> None:
        row = self.choices.currentRow()
        if row < 0 or not self._selected:
            return
        ti, fi = self._selected
        if fi is None:
            return
        base = FIELD_BY_KEY.get(self.definition.tabs[ti]["fields"][fi]["key"])
        original = self.choices.item(row, 0).data(Qt.ItemDataRole.UserRole) or {}
        value = original.get("id", original.get("value"))
        if base and value in {c.value for c in base.choices}:
            QMessageBox.warning(
                self,
                "선택지 유지",
                "기존 CSV와 저장 데이터에 연결된 기본 선택지는 삭제할 수 없습니다.",
            )
            return
        self.choices.removeRow(row)

    def add_tab(self) -> None:
        self._store_editor()
        self.definition.tabs.append(
            {
                "key": "tab_" + uuid4().hex,
                "title": "새 탭",
                "short_title": "새 탭",
                "columns": 1,
                "fields": [],
            }
        )
        self._refresh_tree((len(self.definition.tabs) - 1, None))

    def add_field(self) -> None:
        if not self._selected or self._selected[0] == 0:
            return
        self._store_editor()
        ti = self._selected[0]
        fields = self.definition.tabs[ti]["fields"]
        fields.append(new_field())
        self._refresh_tree((ti, len(fields) - 1))

    def restore_initial(self) -> None:
        answer = QMessageBox.question(
            self,
            "초기 설정 복원 확인",
            "조사 양식을 초기 설정으로 복원하시겠습니까?\n"
            "기본 항목의 이름·저장값·CSV 칼럼과 배치가 초기값으로 돌아갑니다.\n"
            "복원 결과는 ‘양식 적용 및 저장’을 눌러야 저장됩니다.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self._store_editor()
        try:
            restored = self.definition.restored(self.initial_definition)
        except (ValueError, TypeError, KeyError) as exc:
            QMessageBox.warning(self, "초기 설정 복원 오류", str(exc))
            return
        self.definition = restored
        self._refresh_tree((0, None))

    def accept(self) -> None:
        self._store_editor()
        try:
            self.definition.validate()
        except (ValueError, TypeError, KeyError) as exc:
            QMessageBox.warning(self, "양식 설정 확인", str(exc))
            return
        super().accept()
