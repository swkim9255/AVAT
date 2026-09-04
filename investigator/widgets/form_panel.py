"""Pinned identity fields and eight schema-driven investigation tabs."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from PySide6.QtCore import QRegularExpression, Qt, Signal
from PySide6.QtGui import QRegularExpressionValidator
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QCompleter,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from investigator.schema import IDENTITY_FIELDS, SECTIONS, FieldKind, FieldSpec
from investigator.theme import MUTED, PRIMARY, material_icon


class FieldEditor(QWidget):
    value_changed = Signal(str, object)

    def __init__(self, spec: FieldSpec, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.spec = spec
        self._loading = False
        self.checkboxes: list[tuple[QCheckBox, str]] = []

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 8, 0, 10)
        root.setSpacing(6)
        label_row = QHBoxLayout()
        label_row.setSpacing(0)
        label = QLabel(spec.label, self)
        label.setStyleSheet("font-weight: 650;")
        label_row.addWidget(label)
        self.required_mark = QLabel("*", self)
        self.required_mark.setObjectName("requiredMark")
        self.required_mark.setVisible(False)
        label_row.addWidget(self.required_mark)
        label_row.addStretch(1)
        root.addLayout(label_row)

        self.control = self._make_control()
        root.addWidget(self.control)

    def _make_control(self) -> QWidget:
        spec = self.spec
        if spec.kind is FieldKind.SINGLE:
            combo = QComboBox(self)
            combo.setObjectName(f"field_{spec.key}")
            combo.setEditable(spec.editable)
            combo.addItem(spec.placeholder or "선택", None)
            for choice in spec.choices:
                combo.addItem(choice.label, choice.value)
            if spec.editable and spec.choices:
                combo.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
                completer = QCompleter([choice.label for choice in spec.choices], combo)
                completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
                completer.setFilterMode(Qt.MatchFlag.MatchContains)
                completer.setCompletionMode(QCompleter.CompletionMode.PopupCompletion)
                combo.setCompleter(completer)
            combo.currentTextChanged.connect(self._combo_changed)
            return combo

        if spec.kind is FieldKind.MULTI:
            container = QFrame(self)
            container.setObjectName("multiChoiceContainer")
            container.setStyleSheet(
                "QFrame#multiChoiceContainer { background: #F8FAFC; border: 1px solid #E2E8F0; border-radius: 8px; }"
                "QFrame#multiChoiceContainer[invalid=true] { background: #FEF2F2; border: 1px solid #DC2626; }"
            )
            grid = QGridLayout(container)
            grid.setContentsMargins(10, 8, 10, 8)
            grid.setHorizontalSpacing(12)
            grid.setVerticalSpacing(8)
            columns = 2 if len(spec.choices) > 3 else 1
            for index, choice in enumerate(spec.choices):
                checkbox = QCheckBox(choice.label, container)
                checkbox.setObjectName(f"choice_{spec.key}_{index}")
                checkbox.setToolTip(choice.label)
                checkbox.toggled.connect(self._multi_changed)
                self.checkboxes.append((checkbox, choice.value))
                grid.addWidget(checkbox, index // columns, index % columns)
            return container

        row = QWidget(self)
        layout = QHBoxLayout(row)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        edit = QLineEdit(row)
        edit.setObjectName(f"field_{spec.key}")
        edit.setPlaceholderText(spec.placeholder)
        if spec.max_length:
            edit.setMaxLength(spec.max_length)
        if spec.kind is FieldKind.DIGITS:
            maximum = spec.max_length or 12
            edit.setValidator(
                QRegularExpressionValidator(QRegularExpression(rf"\d{{0,{maximum}}}"), edit)
            )
            edit.setInputMethodHints(Qt.InputMethodHint.ImhDigitsOnly)
        elif spec.kind is FieldKind.DECIMAL:
            edit.setValidator(
                QRegularExpressionValidator(QRegularExpression(r"\d{0,8}(?:\.\d{0,3})?"), edit)
            )
            edit.setInputMethodHints(Qt.InputMethodHint.ImhFormattedNumbersOnly)
        edit.textEdited.connect(lambda text: self._emit(text.strip()))
        self.line_edit = edit
        layout.addWidget(edit, 1)
        if spec.unit:
            unit = QLabel(spec.unit, row)
            unit.setStyleSheet(f"color: {MUTED};")
            layout.addWidget(unit)
        return row

    def _combo_changed(self, text: str) -> None:
        if self._loading:
            return
        combo = self.control
        assert isinstance(combo, QComboBox)
        data = combo.currentData()
        if combo.currentIndex() == 0:
            value: Any = None
        elif (
            combo.currentIndex() > 0
            and data is not None
            and text == combo.itemText(combo.currentIndex())
        ):
            value = data
        elif combo.isEditable():
            value = text.strip()
        else:
            value = None
        if self.spec.key == "institution" and isinstance(value, str):
            value = value.casefold()
            if combo.isEditable() and combo.currentText() != value:
                line_edit = combo.lineEdit()
                cursor_position = line_edit.cursorPosition()
                signals_were_blocked = combo.blockSignals(True)
                combo.setEditText(value)
                combo.blockSignals(signals_were_blocked)
                line_edit.setCursorPosition(min(cursor_position, len(value)))
        self._emit(value)

    def _multi_changed(self) -> None:
        if self._loading:
            return
        selected = [value for checkbox, value in self.checkboxes if checkbox.isChecked()]
        self._emit(selected)

    def _emit(self, value: Any) -> None:
        if not self._loading:
            self.set_invalid(None)
            self.value_changed.emit(self.spec.key, value)

    def value(self) -> Any:
        if self.spec.kind is FieldKind.SINGLE:
            combo = self.control
            assert isinstance(combo, QComboBox)
            data = combo.currentData()
            if combo.currentIndex() == 0:
                return None
            if (
                combo.currentIndex() > 0
                and data is not None
                and combo.currentText() == combo.itemText(combo.currentIndex())
            ):
                return data
            value = combo.currentText().strip() if combo.isEditable() else None
            if self.spec.key == "institution" and isinstance(value, str):
                return value.casefold()
            return value
        if self.spec.kind is FieldKind.MULTI:
            return [value for checkbox, value in self.checkboxes if checkbox.isChecked()]
        return self.line_edit.text().strip()

    def set_value(self, value: Any) -> None:
        self._loading = True
        try:
            if self.spec.kind is FieldKind.SINGLE:
                combo = self.control
                assert isinstance(combo, QComboBox)
                if self.spec.key == "institution" and isinstance(value, str):
                    value = value.casefold()
                if value in (None, ""):
                    combo.setCurrentIndex(0)
                    if combo.isEditable():
                        combo.setEditText("")
                else:
                    index = combo.findData(value)
                    if index >= 0:
                        combo.setCurrentIndex(index)
                    elif combo.isEditable():
                        combo.setEditText(str(value))
                    else:
                        combo.setCurrentIndex(0)
            elif self.spec.kind is FieldKind.MULTI:
                selected = set(value if isinstance(value, list) else [])
                for checkbox, choice_value in self.checkboxes:
                    checkbox.setChecked(choice_value in selected)
            else:
                self.line_edit.setText("" if value is None else str(value))
        finally:
            self._loading = False

    def set_invalid(self, message: str | None) -> None:
        target = (
            self.control
            if self.spec.kind is FieldKind.SINGLE
            else getattr(self, "line_edit", self.control)
        )
        target.setProperty("invalid", bool(message))
        target.setToolTip(message or "")
        target.style().unpolish(target)
        target.style().polish(target)

    def set_missing(self, missing: bool) -> None:
        self.required_mark.setVisible(missing)


class InvestigationPanel(QWidget):
    value_changed = Signal(str, object)
    save_requested = Signal()
    complete_requested = Signal()
    next_requested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("panel")
        self.setMinimumWidth(448)
        self.editors: dict[str, FieldEditor] = {}
        self._loading = False

        root = QVBoxLayout(self)
        root.setContentsMargins(18, 16, 18, 12)
        root.setSpacing(12)

        title_row = QHBoxLayout()
        title = QLabel("사고 조사 입력", self)
        title.setObjectName("sectionTitle")
        title_row.addWidget(title)
        title_row.addStretch(1)
        self.completion_badge = QLabel("미입력", self)
        self.completion_badge.setObjectName("missingBadge")
        title_row.addWidget(self.completion_badge)
        root.addLayout(title_row)

        self.case_title = QLabel("미지정 사고", self)
        self.case_title.setObjectName("caseTitle")
        root.addWidget(self.case_title)

        identity_card = QFrame(self)
        identity_card.setObjectName("card")
        identity_layout = QVBoxLayout(identity_card)
        identity_layout.setContentsMargins(12, 6, 12, 6)
        identity_layout.setSpacing(0)
        for spec in IDENTITY_FIELDS:
            editor = self._add_editor(spec, identity_card)
            identity_layout.addWidget(editor)
        root.addWidget(identity_card)

        self.tabs = QTabWidget(self)
        self.tabs.setObjectName("investigationTabs")
        self.tabs.setDocumentMode(True)
        self.tabs.tabBar().setExpanding(True)
        self.tabs.tabBar().setUsesScrollButtons(False)
        self.tabs.tabBar().setElideMode(Qt.TextElideMode.ElideRight)
        for section in SECTIONS:
            scroll = QScrollArea(self.tabs)
            scroll.setObjectName("formScroll")
            scroll.setWidgetResizable(True)
            scroll.setFrameShape(QFrame.Shape.NoFrame)
            page = QWidget(scroll)
            page.setObjectName(f"tab_{section.key}")
            page_layout = QVBoxLayout(page)
            page_layout.setContentsMargins(4, 8, 8, 16)
            page_layout.setSpacing(0)
            section_label = QLabel(section.title, page)
            section_label.setStyleSheet("font-size: 15px; font-weight: 700; padding: 8px 0;")
            page_layout.addWidget(section_label)
            for spec in section.fields:
                page_layout.addWidget(self._add_editor(spec, page))
            page_layout.addStretch(1)
            scroll.setWidget(page)
            self.tabs.addTab(scroll, section.short_title)
            self.tabs.setTabToolTip(self.tabs.count() - 1, section.title)
        root.addWidget(self.tabs, 1)

        action_row = QHBoxLayout()
        action_row.setSpacing(8)
        self.save_button = QPushButton(material_icon("save", PRIMARY), "임시저장", self)
        self.save_button.setObjectName("saveDraftButton")
        self.complete_button = QPushButton(material_icon("task_alt", PRIMARY), "완료", self)
        self.complete_button.setObjectName("completeButton")
        for button in (self.save_button, self.complete_button):
            button.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            action_row.addWidget(button)
        root.addLayout(action_row)

        self.next_button = QPushButton(material_icon("arrow_forward", PRIMARY), "다음", self)
        self.next_button.setObjectName("nextCaseButton")
        self.next_button.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        root.addWidget(self.next_button)

        self.save_button.clicked.connect(self.save_requested)
        self.complete_button.clicked.connect(self.complete_requested)
        self.next_button.clicked.connect(self.next_requested)

    def _add_editor(self, spec: FieldSpec, parent: QWidget) -> FieldEditor:
        editor = FieldEditor(spec, parent)
        editor.value_changed.connect(self._editor_changed)
        self.editors[spec.key] = editor
        return editor

    def load_values(self, values: dict[str, Any]) -> None:
        self._loading = True
        try:
            for key, editor in self.editors.items():
                editor.set_value(values.get(key))
                editor.set_invalid(None)
                editor.set_missing(False)
            self._update_conditional_states(values)
        finally:
            self._loading = False

    def _editor_changed(self, key: str, value: Any) -> None:
        if self._loading:
            return
        self.value_changed.emit(key, value)
        if key == "obstacle_exists" and value == "무":
            for editor in self.editors.values():
                if editor.spec.group in {"obstacle_detail", "obstacle_distance"}:
                    editor.set_value(None)
        if key == "incident_damage_unknown" and value == "확인불가":
            for editor in self.editors.values():
                if editor.spec.group == "damage_counts":
                    editor.set_value(None)
        if key == "obstacle_exists" or key == "incident_damage_unknown":
            values = {name: editor.value() for name, editor in self.editors.items()}
            self._update_conditional_states(values)
        if key in {"collision_dist_value", "obstacle_dist_value"} and value not in (None, ""):
            self._apply_distance_range(key, str(value))
        if key == "incident_date" and isinstance(value, str) and len(value) == 8:
            self._apply_incident_day(value)

    def _apply_distance_range(self, key: str, text: str) -> None:
        try:
            number = float(text)
        except ValueError:
            return
        if key == "collision_dist_value":
            target_key = "collision_dist_range"
            target = "10m 이내" if number <= 10 else ("10m~50m" if number < 50 else "50m 이상")
        else:
            target_key = "obstacle_dist_range"
            target = "1.5m 이내" if number <= 1.5 else ("1.5m~10m" if number < 10 else "10m 이상")
        editor = self.editors[target_key]
        if editor.value() != target:
            editor.set_value(target)
            self.value_changed.emit(target_key, target)

    def _apply_incident_day(self, text: str) -> None:
        try:
            parsed = datetime.strptime(text, "%Y%m%d")
        except ValueError:
            return
        day = ("월", "화", "수", "목", "금", "토", "일")[parsed.weekday()]
        editor = self.editors["incident_day"]
        if editor.value() != day:
            editor.set_value(day)
            self.value_changed.emit("incident_day", day)

    def _update_conditional_states(self, values: dict[str, Any]) -> None:
        obstacle_enabled = values.get("obstacle_exists") == "유"
        damage_enabled = values.get("incident_damage_unknown") == "확인 가능"
        for editor in self.editors.values():
            if editor.spec.group == "obstacle_detail":
                editor.setEnabled(obstacle_enabled)
            elif editor.spec.group == "obstacle_distance":
                editor.setEnabled(values.get("obstacle_exists") != "무")
            elif editor.spec.group == "damage_counts":
                editor.setEnabled(damage_enabled)

    def show_case(self, display_id: str, missing: list[str], completed: bool) -> None:
        self.case_title.setText(display_id)
        missing_editor_keys = {
            {
                "site_location": "site_types",
                "obstacle_object_types": "obstacle_vehicle_types",
            }.get(key, key)
            for key in missing
        }
        for key, editor in self.editors.items():
            editor.set_missing(key in missing_editor_keys)
        if completed:
            self.completion_badge.setObjectName("completeBadge")
            self.completion_badge.setText("완료")
        else:
            self.completion_badge.setObjectName("missingBadge")
            self.completion_badge.setText(f"미입력 {len(missing)}")
        self.completion_badge.style().unpolish(self.completion_badge)
        self.completion_badge.style().polish(self.completion_badge)

    def show_validation(self, errors: dict[str, str]) -> None:
        for key, editor in self.editors.items():
            editor.set_invalid(errors.get(key))

    def focus_field(self, key: str) -> None:
        key = {
            "obstacle_object_types": "obstacle_vehicle_types",
            "site_location": "site_types",
        }.get(key, key)
        editor = self.editors.get(key)
        if editor is None:
            return
        scroll: QScrollArea | None = None
        for index, section in enumerate(SECTIONS):
            if any(field.key == key for field in section.fields):
                self.tabs.setCurrentIndex(index)
                candidate = self.tabs.widget(index)
                if isinstance(candidate, QScrollArea):
                    scroll = candidate
                break
        if scroll is not None:
            scroll.ensureWidgetVisible(editor, 0, 16)
        if editor.spec.kind is FieldKind.MULTI and editor.checkboxes:
            focus_target = editor.checkboxes[0][0]
        elif editor.spec.kind is FieldKind.SINGLE:
            focus_target = editor.control
        else:
            focus_target = editor.line_edit
        focus_target.setFocus(Qt.FocusReason.OtherFocusReason)


__all__ = ["FieldEditor", "InvestigationPanel"]
