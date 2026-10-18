"""Pinned identity fields and nine schema-driven investigation tabs."""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import QDate, QLocale, QRegularExpression, QSize, Qt, Signal
from PySide6.QtGui import QColor, QPalette, QRegularExpressionValidator, QTextCharFormat
from PySide6.QtWidgets import (
    QCalendarWidget,
    QCheckBox,
    QComboBox,
    QCompleter,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMenu,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QTabWidget,
    QTextEdit,
    QToolButton,
    QVBoxLayout,
    QWidget,
    QWidgetAction,
)

from investigator.form_definition import FormDefinition
from investigator.schema import (
    INJURY_GRADE_RULES,
    FieldKind,
    FieldSpec,
    incident_day_for_date,
    injury_grade_summary,
)
from investigator.theme import BORDER, MUTED, PRIMARY, SURFACE, TEXT, material_icon
from investigator.widgets.responsive_choices import ResponsiveChoiceGroup, WrappingCheckBox
from investigator.widgets.scroll_safe_combo import ScrollSafeComboBox


class FieldEditor(QWidget):
    value_changed = Signal(str, object)

    def __init__(self, spec: FieldSpec, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.spec = spec
        policy = QSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        policy.setHeightForWidth(True)
        self.setSizePolicy(policy)
        self.setMinimumWidth(0)
        self._loading = False
        self.checkboxes: list[tuple[QCheckBox, str]] = []

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 8, 0, 10)
        root.setSpacing(6)
        root.setAlignment(Qt.AlignmentFlag.AlignTop)
        label_row = QHBoxLayout()
        label_row.setSpacing(0)
        label = QLabel(spec.label, self)
        label.setWordWrap(True)
        label.setTextFormat(Qt.TextFormat.PlainText)
        label_policy = QSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        label_policy.setHeightForWidth(True)
        label.setSizePolicy(label_policy)
        label.setStyleSheet("font-weight: 650;")
        label_row.addWidget(label, 1)
        self.required_mark = QLabel("*", self)
        self.required_mark.setObjectName("requiredMark")
        self.required_mark.setVisible(False)
        label_row.addWidget(self.required_mark)
        root.addLayout(label_row)

        self.control = self._make_control()
        control_policy = self.control.sizePolicy()
        control_policy.setHorizontalPolicy(QSizePolicy.Policy.Ignored)
        self.control.setSizePolicy(control_policy)
        self.control.setMinimumWidth(0)
        root.addWidget(self.control)

    def _make_control(self) -> QWidget:
        spec = self.spec
        if spec.key == "incident_day":
            display = QLineEdit(self)
            display.setObjectName("field_incident_day")
            display.setReadOnly(True)
            display.setPlaceholderText("사고일자 선택 시 자동 계산")
            display.setToolTip("사고일자를 선택하거나 입력하면 자동으로 계산됩니다.")
            display.setAccessibleDescription("사고일자에서 자동으로 계산되는 읽기 전용 값")
            display.setStyleSheet(
                "QLineEdit#field_incident_day {"
                " background: #F1F5F9; color: #475569; border-color: #CBD5E1;"
                "}"
                "QLineEdit#field_incident_day:focus { border: 1px solid #CBD5E1; }"
                "QLineEdit#field_incident_day[invalid=true] {"
                " background: #FEF2F2; border: 1px solid #DC2626;"
                "}"
            )
            self.line_edit = display
            return display

        representative_keys = {rule[3] for rule in INJURY_GRADE_RULES.values()}
        if spec.key in representative_keys:
            display = QLineEdit(self)
            object_name = f"field_{spec.key}"
            display.setObjectName(object_name)
            display.setReadOnly(True)
            display.setPlaceholderText("")
            display.setToolTip("")
            display.setAccessibleDescription("부상급수에서 자동 계산되는 읽기 전용 값")
            display.setStyleSheet(
                f"QLineEdit#{object_name} {{"
                " background: #F1F5F9; color: #475569; border-color: #CBD5E1;"
                "}"
                f"QLineEdit#{object_name}:focus {{"
                " border: 1px solid #CBD5E1;"
                "}"
                f"QLineEdit#{object_name}[invalid=true] {{"
                " background: #FEF2F2; border: 1px solid #DC2626;"
                "}"
            )
            self.line_edit = display
            return display

        if spec.kind is FieldKind.SINGLE:
            combo = ScrollSafeComboBox(self)
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
            container = ResponsiveChoiceGroup(self)
            container.setObjectName("multiChoiceContainer")
            container.setStyleSheet(
                "QFrame#multiChoiceContainer { background: #F8FAFC; border: 1px solid #E2E8F0; border-radius: 8px; }"
                "QFrame#multiChoiceContainer[invalid=true] { background: #FEF2F2; border: 1px solid #DC2626; }"
            )
            for index, choice in enumerate(spec.choices):
                checkbox = WrappingCheckBox(choice.label, container)
                checkbox.setObjectName(f"choice_{spec.key}_{index}")
                checkbox.setToolTip(choice.label)
                checkbox.toggled.connect(self._multi_changed)
                self.checkboxes.append((checkbox, choice.value))
                container.add_choice(checkbox)
            return container

        if spec.kind is FieldKind.LONG_TEXT:
            text_edit = QTextEdit(self)
            text_edit.setObjectName(f"field_{spec.key}")
            text_edit.setPlaceholderText(spec.placeholder)
            text_edit.setAcceptRichText(False)
            text_edit.setMinimumHeight(240)
            text_edit.textChanged.connect(lambda: self._emit(text_edit.toPlainText().strip()))
            return text_edit

        row = QWidget(self)
        layout = QHBoxLayout(row)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        edit = QLineEdit(row)
        edit.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        edit.setMinimumWidth(0)
        edit.setObjectName(f"field_{spec.key}")
        edit.setPlaceholderText(spec.placeholder)
        if spec.max_length:
            edit.setMaxLength(spec.max_length)
        if spec.kind is FieldKind.DIGITS:
            digit_pattern = rf"\d{{0,{spec.max_length}}}" if spec.max_length is not None else r"\d*"
            edit.setValidator(QRegularExpressionValidator(QRegularExpression(digit_pattern), edit))
            edit.setInputMethodHints(Qt.InputMethodHint.ImhDigitsOnly)
        elif spec.kind is FieldKind.DECIMAL:
            edit.setValidator(
                QRegularExpressionValidator(QRegularExpression(r"\d{0,8}(?:\.\d{0,3})?"), edit)
            )
            edit.setInputMethodHints(Qt.InputMethodHint.ImhFormattedNumbersOnly)
        edit.textEdited.connect(lambda text: self._emit(text.strip()))
        if spec.key in INJURY_GRADE_RULES:
            edit.editingFinished.connect(self._finish_injury_grades)
        self.line_edit = edit
        layout.addWidget(edit, 1)
        if spec.key == "incident_date":
            self.calendar_button = QToolButton(row)
            self.calendar_button.setObjectName("incidentDateCalendarButton")
            self.calendar_button.setIcon(material_icon("calendar_month", PRIMARY))
            self.calendar_button.setIconSize(QSize(20, 20))
            self.calendar_button.setFixedSize(36, 36)
            self.calendar_button.setToolTip("사고일자 선택")
            self.calendar_button.setAccessibleName("사고일자 선택")
            self.calendar_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)

            self.calendar_menu = QMenu(self.calendar_button)
            self.calendar_widget = QCalendarWidget(self.calendar_menu)
            self.calendar_widget.setObjectName("incidentDateCalendar")
            self.calendar_widget.setLocale(
                QLocale(QLocale.Language.Korean, QLocale.Country.SouthKorea)
            )
            self.calendar_widget.setGridVisible(True)
            self.calendar_widget.setFirstDayOfWeek(Qt.DayOfWeek.Sunday)
            self.calendar_widget.setVerticalHeaderFormat(
                QCalendarWidget.VerticalHeaderFormat.NoVerticalHeader
            )
            calendar_palette = self.calendar_widget.palette()
            calendar_palette.setColor(QPalette.ColorRole.Highlight, QColor(BORDER))
            calendar_palette.setColor(QPalette.ColorRole.HighlightedText, QColor(TEXT))
            self.calendar_widget.setPalette(calendar_palette)
            header_format = QTextCharFormat()
            header_format.setBackground(QColor("#F8FAFC"))
            header_format.setForeground(QColor(MUTED))
            self.calendar_widget.setHeaderTextFormat(header_format)
            self.calendar_widget.setStyleSheet(
                f"""
                QCalendarWidget QWidget#qt_calendar_navigationbar {{
                    background: #F8FAFC;
                    border-bottom: 1px solid {BORDER};
                }}
                QCalendarWidget QToolButton {{
                    color: {TEXT};
                    background: transparent;
                    border: 0;
                    border-radius: 4px;
                    padding: 4px;
                }}
                QCalendarWidget QToolButton:hover {{ background: {BORDER}; }}
                QCalendarWidget QToolButton::menu-indicator {{ image: none; }}
                QCalendarWidget QAbstractItemView:enabled {{
                    color: {TEXT};
                    background: {SURFACE};
                    selection-background-color: {BORDER};
                    selection-color: {TEXT};
                }}
                """
            )
            calendar_action = QWidgetAction(self.calendar_menu)
            calendar_action.setDefaultWidget(self.calendar_widget)
            self.calendar_menu.addAction(calendar_action)
            self.calendar_button.setMenu(self.calendar_menu)
            self.calendar_menu.aboutToShow.connect(self._prepare_incident_calendar)
            self.calendar_widget.clicked.connect(self._select_incident_date)
            self.calendar_widget.activated.connect(self._select_incident_date)
            layout.addWidget(self.calendar_button)
        if spec.unit:
            unit = QLabel(spec.unit, row)
            unit.setStyleSheet(f"color: {MUTED};")
            layout.addWidget(unit)
        return row

    def _prepare_incident_calendar(self) -> None:
        selected = QDate.fromString(self.line_edit.text().strip(), "yyyyMMdd")
        if not selected.isValid():
            selected = QDate.currentDate()
        self.calendar_widget.setSelectedDate(selected)
        self.calendar_widget.setCurrentPage(selected.year(), selected.month())

    def _select_incident_date(self, selected: QDate) -> None:
        if not selected.isValid():
            return
        text = selected.toString("yyyyMMdd")
        self.line_edit.setText(text)
        self._emit(text)
        self.calendar_menu.close()
        self.line_edit.setFocus(Qt.FocusReason.OtherFocusReason)

    def _combo_changed(self, text: str) -> None:
        if self._loading:
            return
        combo = self.control
        assert isinstance(combo, QComboBox)
        data = combo.currentData()
        if combo.currentIndex() == 0 and (not combo.isEditable() or text == combo.itemText(0)):
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

    def _finish_injury_grades(self) -> None:
        minimum, maximum, _, _ = INJURY_GRADE_RULES[self.spec.key]
        summary = injury_grade_summary(self.line_edit.text(), minimum=minimum, maximum=maximum)
        if summary is None:
            return
        normalized = summary[0]
        if self.line_edit.text() != normalized:
            self.line_edit.setText(normalized)
        self._emit(normalized)

    def _emit(self, value: Any) -> None:
        if not self._loading:
            self.set_invalid(None)
            self.value_changed.emit(self.spec.key, value)

    def value(self) -> Any:
        if self.spec.key == "incident_day":
            return self.line_edit.text().strip() or None
        if self.spec.kind is FieldKind.SINGLE:
            combo = self.control
            assert isinstance(combo, QComboBox)
            data = combo.currentData()
            if combo.currentIndex() == 0 and (
                not combo.isEditable() or combo.currentText() == combo.itemText(0)
            ):
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
        if self.spec.kind is FieldKind.LONG_TEXT:
            text_edit = self.control
            assert isinstance(text_edit, QTextEdit)
            return text_edit.toPlainText().strip()
        return self.line_edit.text().strip()

    def set_value(self, value: Any) -> None:
        self._loading = True
        try:
            if self.spec.key == "incident_day":
                self.line_edit.setText("" if value is None else str(value))
            elif self.spec.kind is FieldKind.SINGLE:
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
            elif self.spec.kind is FieldKind.LONG_TEXT:
                text_edit = self.control
                assert isinstance(text_edit, QTextEdit)
                text_edit.setPlainText("" if value is None else str(value))
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
        if self.spec.key == "incident_day" and not message:
            target.setToolTip("사고일자를 선택하거나 입력하면 자동으로 계산됩니다.")
        else:
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
    edit_form_requested = Signal()

    def __init__(
        self, parent: QWidget | None = None, *, definition: FormDefinition | None = None
    ) -> None:
        super().__init__(parent)
        self.setObjectName("panel")
        self.setMinimumWidth(448)
        self.editors: dict[str, FieldEditor] = {}
        self.section_field_grids: dict[str, QGridLayout] = {}
        self._loading = False
        definition = definition or FormDefinition.default()
        self.definition = definition
        configured_sections = definition.specs()
        tab_layouts = {tab["key"]: tab for tab in definition.tabs}
        self.identity_fields = configured_sections[0].fields
        self.sections = configured_sections[1:]

        root = QVBoxLayout(self)
        root.setContentsMargins(18, 16, 18, 12)
        root.setSpacing(12)

        title_row = QHBoxLayout()
        title = QLabel("사고 조사 입력", self)
        title.setObjectName("sectionTitle")
        title_row.addWidget(title)
        title_row.addStretch(1)
        self.edit_form_button = QToolButton(self)
        self.edit_form_button.setIcon(material_icon("edit_note", PRIMARY))
        self.edit_form_button.setToolTip("조사 항목 편집")
        self.edit_form_button.setAccessibleName("조사 항목 편집")
        self.edit_form_button.clicked.connect(self.edit_form_requested)
        title_row.addWidget(self.edit_form_button)
        self.completion_badge = QLabel("미입력", self)
        self.completion_badge.setObjectName("missingBadge")
        title_row.addWidget(self.completion_badge)
        root.addLayout(title_row)

        self.case_title = QLabel("미지정 사고", self)
        self.case_title.setObjectName("caseTitle")
        root.addWidget(self.case_title)

        identity_card = QFrame(self)
        identity_card.setObjectName("card")
        identity_layout = QGridLayout(identity_card)
        identity_layout.setContentsMargins(12, 6, 12, 6)
        identity_layout.setHorizontalSpacing(12)
        identity_layout.setVerticalSpacing(0)
        self.section_field_grids["identity"] = identity_layout
        identity_settings = tab_layouts["identity"]
        identity_columns = identity_settings.get("columns", 2)
        identity_full_width = {
            f["key"] for f in identity_settings["fields"] if f.get("full_width", False)
        }
        for column in range(identity_columns):
            identity_layout.setColumnStretch(column, 1)
        row, column = 0, 0
        for spec in self.identity_fields:
            editor = self._add_editor(spec, identity_card)
            span = identity_columns if spec.key in identity_full_width else 1
            if column and span == identity_columns:
                row, column = row + 1, 0
            identity_layout.addWidget(editor, row, column, 1, span)
            column += span
            if column >= identity_columns:
                row, column = row + 1, 0
        root.addWidget(identity_card)

        self.tabs = QTabWidget(self)
        self.tabs.setObjectName("investigationTabs")
        self.tabs.setDocumentMode(True)
        self.tabs.tabBar().setExpanding(True)
        self.tabs.tabBar().setUsesScrollButtons(len(self.sections) > 9)
        self.tabs.tabBar().setElideMode(Qt.TextElideMode.ElideRight)
        for section in self.sections:
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
            section_label.setWordWrap(True)
            section_label.setMinimumWidth(0)
            section_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
            section_label.setStyleSheet("font-size: 15px; font-weight: 700; padding: 8px 0;")
            page_layout.addWidget(section_label)
            section_fields = list(section.fields)
            tab_layout = tab_layouts[section.key]
            if tab_layout.get("columns", 1) == 2:
                field_grid = QGridLayout()
                field_grid.setObjectName(f"{section.key}FieldsGrid")
                field_grid.setContentsMargins(0, 0, 0, 0)
                field_grid.setHorizontalSpacing(12)
                field_grid.setVerticalSpacing(0)
                field_grid.setColumnStretch(0, 1)
                field_grid.setColumnStretch(1, 1)
                full_width_keys = {
                    f["key"] for f in tab_layout["fields"] if f.get("full_width", False)
                }
                row, column = 0, 0
                for spec in section_fields:
                    span = 2 if spec.key in full_width_keys else 1
                    if span == 2 and column:
                        row, column = row + 1, 0
                    field_grid.addWidget(self._add_editor(spec, page), row, column, 1, span)
                    column += span
                    if column >= 2:
                        row, column = row + 1, 0
                self.section_field_grids[section.key] = field_grid
                page_layout.addLayout(field_grid)
            else:
                for spec in section_fields:
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
        value = self.definition.canonical_values({key: value}).get(key)
        if key == "obstacle_exists" and value == "무":
            for editor in self.editors.values():
                if editor.spec.group == "obstacle_detail":
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
        if key in INJURY_GRADE_RULES:
            self._apply_injury_grades(key, value)
        if key == "incident_date":
            self._apply_incident_day(str(value or ""))

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
        target = self.definition.stored_value(target_key, target)
        editor = self.editors[target_key]
        if editor.value() != target:
            editor.set_value(target)
            self.value_changed.emit(target_key, target)

    def _apply_incident_day(self, text: str) -> None:
        day = incident_day_for_date(text)
        day = self.definition.stored_value("incident_day", day)
        editor = self.editors["incident_day"]
        if editor.value() != day:
            editor.set_value(day)
            self.value_changed.emit("incident_day", day)

    def _apply_injury_grades(self, grade_key: str, value: Any) -> None:
        minimum, maximum, count_key, representative_key = INJURY_GRADE_RULES[grade_key]
        summary = injury_grade_summary(value, minimum=minimum, maximum=maximum)
        if summary is None:
            return
        normalized, count, representative = summary
        derived = {
            count_key: count,
            representative_key: representative,
        }
        for key, derived_value in derived.items():
            editor = self.editors[key]
            if editor.value() != derived_value:
                editor.set_value(derived_value)
                self.value_changed.emit(key, derived_value)
        self._update_injury_count_state(count_key, bool(normalized))

    def _update_injury_count_state(self, count_key: str, calculated: bool) -> None:
        editor = self.editors[count_key]
        editor.line_edit.setReadOnly(calculated)
        editor.line_edit.setProperty("calculated", calculated)
        editor.line_edit.style().unpolish(editor.line_edit)
        editor.line_edit.style().polish(editor.line_edit)

    def _update_conditional_states(self, values: dict[str, Any]) -> None:
        values = self.definition.canonical_values(values)
        obstacle_enabled = values.get("obstacle_exists") == "유"
        damage_enabled = values.get("incident_damage_unknown") == "확인 가능"
        for editor in self.editors.values():
            if editor.spec.group == "obstacle_detail":
                editor.setEnabled(obstacle_enabled)
            elif editor.spec.group == "obstacle_distance":
                editor.setEnabled(True)
            elif editor.spec.group == "damage_counts":
                editor.setEnabled(damage_enabled)
        for grade_key, (minimum, maximum, count_key, _) in INJURY_GRADE_RULES.items():
            injury_summary = injury_grade_summary(
                values.get(grade_key), minimum=minimum, maximum=maximum
            )
            self._update_injury_count_state(count_key, bool(injury_summary and injury_summary[0]))

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
        for index, section in enumerate(self.sections):
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
        elif editor.spec.kind in {FieldKind.SINGLE, FieldKind.LONG_TEXT}:
            focus_target = editor.control
        else:
            focus_target = editor.line_edit
        focus_target.setFocus(Qt.FocusReason.OtherFocusReason)


__all__ = ["FieldEditor", "InvestigationPanel"]
