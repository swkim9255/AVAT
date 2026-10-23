"""Modeless, value-aware checklist filters for the statistics workspace."""

from __future__ import annotations

import math
import re
from collections import Counter
from typing import Any

from PySide6.QtCore import QEvent, QLocale, QObject, QPoint, Qt, Signal
from PySide6.QtGui import QDoubleValidator, QInputMethodEvent, QKeyEvent, QMouseEvent
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QCompleter,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from investigator.schema import is_blank
from investigator.statistics import (
    StatisticsEngine,
    StatsCondition,
    StatsFilter,
    normalize_condition,
)
from investigator.widgets.statistics_condition_dialog import _ConditionComboBox


def _field_label(label: str) -> str:
    """Keep meaningful qualifiers, hiding only the old internal Index suffix."""
    return re.sub(r"\s*\(\s*Index\s*\)", "", label, flags=re.IGNORECASE).strip()


def _blank(value: Any) -> bool:
    return is_blank(value) or isinstance(value, str) and not value.strip()


class _ValueChecklist(QListWidget):
    """Like a checkbox label, let the whole value row toggle its check."""

    def mousePressEvent(self, event: QMouseEvent) -> None:
        self._pressed = None
        item = self.itemAt(event.position().toPoint())
        if event.button() == Qt.MouseButton.LeftButton and item is not None:
            self._pressed = (item, item.checkState(), event.position().toPoint())
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        super().mouseReleaseEvent(event)
        pressed = getattr(self, "_pressed", None)
        self._pressed = None
        if event.button() != Qt.MouseButton.LeftButton or pressed is None:
            return
        item, state, point = pressed
        release = event.position().toPoint()
        if (self.itemAt(release) is item and item.checkState() == state
                and (release - point).manhattanLength() < QApplication.startDragDistance()):
            # Native checkbox-indicator clicks have already toggled in super.
            item.setCheckState(Qt.CheckState.Unchecked if state == Qt.CheckState.Checked
                               else Qt.CheckState.Checked)


class _StatisticsFieldComboBox(_ConditionComboBox):
    """Open the searchable variable selector from its entire editable field."""

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self._open_on_release = False
        self._dropdown_targets: tuple[QWidget, ...] = ()
        self.setEditable(True)
        self._dropdown_targets = (self.view(), self.view().window())
        self.lineEdit().installEventFilter(self)
        for target in self._dropdown_targets:
            target.installEventFilter(self)
        # Once typing starts, let QCompleter own the filtered popup instead of
        # leaving the full, unfiltered combo popup competing for key events.
        self.lineEdit().textEdited.connect(self.hidePopup)

    def _field_press(self, event: QMouseEvent) -> None:
        self._open_on_release = not self.view().isVisible()
        if not self._open_on_release:
            self.hidePopup()
        event.accept()

    def _field_release(self, event: QMouseEvent, target: QWidget) -> None:
        open_popup = self._open_on_release and target.rect().contains(event.position().toPoint())
        self._open_on_release = False
        if open_popup:
            self.setFocus(Qt.FocusReason.MouseFocusReason)
            # Opening on release keeps that same click from dismissing the list
            # (and its containing Qt.Popup) immediately after it is shown.
            self.showPopup()
            self.lineEdit().setFocus(Qt.FocusReason.PopupFocusReason)
            self.lineEdit().selectAll()
        event.accept()

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if (watched in self._dropdown_targets and self.view().isVisible()
                and isinstance(event, (QKeyEvent, QInputMethodEvent))):
            # Native popup keyboard grabs can still deliver the first typed key
            # to the list, even after restoring the editor's focus. Leave list
            # navigation/confirmation alone, but transfer editing back to the
            # line edit so its existing completer and IME keep working.
            navigation_keys = {
                Qt.Key.Key_Up, Qt.Key.Key_Down, Qt.Key.Key_PageUp, Qt.Key.Key_PageDown,
                Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Escape,
                Qt.Key.Key_Tab, Qt.Key.Key_Backtab,
            }
            editing_keys = {
                Qt.Key.Key_Backspace, Qt.Key.Key_Delete, Qt.Key.Key_Home,
                Qt.Key.Key_End, Qt.Key.Key_Left, Qt.Key.Key_Right,
            }
            editing = (isinstance(event, QKeyEvent) and event.type() == QEvent.Type.KeyPress
                       and event.key() not in navigation_keys
                       and (event.key() in editing_keys or bool(event.text()))
                       and not event.modifiers() & Qt.KeyboardModifier.AltModifier)
            if editing or isinstance(event, QInputMethodEvent):
                self.hidePopup()
                self.lineEdit().setFocus(Qt.FocusReason.OtherFocusReason)
                QApplication.sendEvent(self.lineEdit(), event)
                return True
        if (watched is self.lineEdit() and isinstance(event, QMouseEvent)
                and event.button() == Qt.MouseButton.LeftButton):
            if event.type() == QEvent.Type.MouseButtonPress:
                self._field_press(event)
                return True
            if event.type() == QEvent.Type.MouseButtonRelease:
                self._field_release(event, self.lineEdit())
                return True
        return super().eventFilter(watched, event)

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._field_press(event)
        else:
            super().mousePressEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._field_release(event, self)
        else:
            super().mouseReleaseEvent(event)


class StatisticsConditionPopup(QWidget):
    """An anchored Qt.Popup which applies only on explicit confirmation.

    Counts use the supplied current filter, including a condition being edited.
    Multi-choice responses contribute at most once per value per accident.
    The report-export modal intentionally remains a separate legacy editor.
    """

    applied = Signal(object)

    def __init__(
        self,
        engine: StatisticsEngine,
        filters: StatsFilter,
        condition: StatsCondition | None = None,
        parent: QWidget | None = None,
        *,
        ignore_status: bool = False,
    ) -> None:
        super().__init__(parent, Qt.WindowType.Popup)
        self.engine = engine
        self.filters = filters
        self.condition: StatsCondition | None = None
        self._original = normalize_condition(condition) if condition is not None else None
        self._dirty = False
        self._loading = True
        self._counts: dict[str, Counter] = {}
        self._scoped_cases = engine.filtered_cases(filters, ignore_status=ignore_status)
        self.setObjectName("statisticsConditionPopup")
        self.setWindowTitle("통계 조건")
        self.setAutoFillBackground(True)
        self.setMinimumWidth(360)
        self.resize(480, 530)
        self.setStyleSheet(
            "QWidget#statisticsConditionPopup { border: 1px solid #cbd5e1; "
            "background: #ffffff; }"
            "QPushButton#statisticsFilterMode:checked { background: #1b1f24; "
            "border-color: #1b1f24; color: white; }"
        )

        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(9)
        title = QLabel("조건 수정" if condition is not None else "조건 추가", self)
        title.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(title)

        self.field = _StatisticsFieldComboBox(self)
        self.field.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.field.setMinimumContentsLength(24)
        self.field.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.field.setMaxVisibleItems(14)
        self.field.setAccessibleName("변수 검색 및 선택")
        self.field.lineEdit().setPlaceholderText("변수 검색")
        for spec in engine.fields:
            self.field.addItem(_field_label(spec.label), spec.key)
        completer = self.field.completer()
        completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        completer.setFilterMode(Qt.MatchFlag.MatchContains)
        completer.setCompletionMode(QCompleter.CompletionMode.PopupCompletion)
        layout.addWidget(self.field)

        mode_row = QHBoxLayout()
        self.include_button = QPushButton("포함", self)
        self.exclude_button = QPushButton("제외", self)
        self.mode_group = QButtonGroup(self)
        self.mode_group.setExclusive(True)
        for button in (self.include_button, self.exclude_button):
            button.setObjectName("statisticsFilterMode")
            button.setCheckable(True)
            self.mode_group.addButton(button)
            mode_row.addWidget(button)
            button.toggled.connect(self._changed)
        self.include_button.setChecked(True)
        mode_row.addStretch(1)
        layout.addLayout(mode_row)

        self.checklist_panel = QWidget(self)
        checklist_layout = QVBoxLayout(self.checklist_panel)
        checklist_layout.setContentsMargins(0, 0, 0, 0)
        checklist_layout.setSpacing(7)
        self.search = QLineEdit(self.checklist_panel)
        self.search.setPlaceholderText("값 검색")
        self.search.setClearButtonEnabled(True)
        self.search.setAccessibleName("조건 값 검색")
        checklist_layout.addWidget(self.search)
        selection_row = QHBoxLayout()
        self.select_all_button = QPushButton("전체선택", self.checklist_panel)
        self.clear_selection_button = QPushButton("선택해제", self.checklist_panel)
        self.select_all_button.setToolTip("현재 검색 결과의 값을 모두 선택합니다.")
        self.clear_selection_button.setToolTip("현재 검색 결과의 선택을 해제합니다.")
        selection_row.addWidget(self.select_all_button)
        selection_row.addWidget(self.clear_selection_button)
        selection_row.addStretch(1)
        checklist_layout.addLayout(selection_row)
        self.values_list = _ValueChecklist(self.checklist_panel)
        self.values_list.setMinimumHeight(150)
        self.values_list.setUniformItemSizes(True)
        self.values_list.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.values_list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.values_list.setTextElideMode(Qt.TextElideMode.ElideRight)
        self.values_list.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Expanding)
        self.values_list.setAccessibleName("값과 현재 필터 기준 사고 건수")
        checklist_layout.addWidget(self.values_list, 1)
        layout.addWidget(self.checklist_panel, 1)

        self.numeric_panel = QWidget(self)
        numeric_layout = QVBoxLayout(self.numeric_panel)
        numeric_layout.setContentsMargins(0, 0, 0, 0)
        range_row = QHBoxLayout()
        self.lower = self._number_edit("최소 (이상)")
        self.upper = self._number_edit("최대 (미만)")
        range_row.addWidget(self.lower, 1)
        range_row.addWidget(QLabel("~", self.numeric_panel))
        range_row.addWidget(self.upper, 1)
        self.unit = QLabel(self.numeric_panel)
        self.unit.setTextFormat(Qt.TextFormat.PlainText)
        range_row.addWidget(self.unit)
        numeric_layout.addLayout(range_row)
        self.numeric_missing = QCheckBox("(미입력)만", self.numeric_panel)
        self.numeric_missing.setToolTip("수치가 입력되지 않은 사고를 선택합니다.")
        numeric_layout.addWidget(self.numeric_missing)
        layout.addWidget(self.numeric_panel)

        self.hint = QLabel(self)
        self.hint.setTextFormat(Qt.TextFormat.PlainText)
        self.hint.setWordWrap(True)
        self.hint.setObjectName("secondaryText")
        layout.addWidget(self.hint)
        self.error_label = QLabel(self)
        self.error_label.setTextFormat(Qt.TextFormat.PlainText)
        self.error_label.setWordWrap(True)
        self.error_label.setStyleSheet("color: #b45309;")
        self.error_label.hide()
        layout.addWidget(self.error_label)
        button_row = QHBoxLayout()
        button_row.addStretch(1)
        self.apply_button = QPushButton("적용", self)
        self.cancel_button = QPushButton("취소", self)
        button_row.addWidget(self.apply_button)
        button_row.addWidget(self.cancel_button)
        layout.addLayout(button_row)

        self.search.textChanged.connect(self._search_values)
        self.values_list.itemChanged.connect(self._changed)
        self.select_all_button.clicked.connect(lambda: self._select_visible(True))
        self.clear_selection_button.clicked.connect(lambda: self._select_visible(False))
        self.lower.textChanged.connect(self._changed)
        self.upper.textChanged.connect(self._changed)
        self.numeric_missing.toggled.connect(self._changed)
        self.apply_button.clicked.connect(self._apply)
        self.cancel_button.clicked.connect(self.close)
        self.field.currentIndexChanged.connect(self._field_changed)
        self.field.lineEdit().editingFinished.connect(self._restore_field_text)

        if self._original is not None:
            index = self.field.findData(self._original.field_key)
            if index >= 0:
                self.field.setCurrentIndex(index)
        self._field_changed()
        self._loading = False
        self._dirty = False
        self._update_controls()

    def _number_edit(self, placeholder: str) -> QLineEdit:
        edit = QLineEdit(self)
        edit.setMinimumWidth(0)
        edit.setPlaceholderText(placeholder)
        edit.setAccessibleName(placeholder)
        validator = QDoubleValidator(edit)
        validator.setNotation(QDoubleValidator.Notation.ScientificNotation)
        locale = QLocale.c()
        locale.setNumberOptions(QLocale.NumberOption.RejectGroupSeparator)
        validator.setLocale(locale)
        edit.setValidator(validator)
        return edit

    def _restore_field_text(self) -> None:
        # Free typing searches the available variables; it never creates a key.
        if self.field.currentIndex() >= 0:
            self.field.lineEdit().setText(self.field.itemText(self.field.currentIndex()))

    def _value_counts(self, key: str) -> Counter:
        if key not in self._counts:
            counts: Counter = Counter()
            for case in self._scoped_cases:
                if not self.engine.is_applicable(case, key):
                    continue
                raw = self.engine.case_value(case, key)
                values = raw if isinstance(raw, list) else [raw]
                present = {
                    value for value in values
                    if isinstance(value, (str, int, float, bool)) and not _blank(value)
                    and not (isinstance(value, float) and not math.isfinite(value))
                }
                if present:
                    counts.update(present)
                elif _blank(raw) or all(_blank(value) for value in values):
                    counts[None] += 1
            self._counts[key] = counts
        return self._counts[key]

    def _field_changed(self) -> None:
        previous_loading = self._loading
        self._loading = True
        key = self.field.currentData()
        self.search.clear()
        self.lower.clear()
        self.upper.clear()
        self.numeric_missing.setChecked(False)
        self.include_button.setChecked(True)
        self.values_list.blockSignals(True)
        self.values_list.clear()
        if key is not None:
            numeric = self.engine.is_numeric(key)
            self.checklist_panel.setVisible(not numeric)
            self.numeric_panel.setVisible(numeric)
            original = self._original if self._original and self._original.field_key == key else None
            if original is not None:
                excluded = original.exclude or original.operator == "ne"
                self.exclude_button.setChecked(excluded)
                self.include_button.setChecked(not excluded)
            if numeric:
                self.unit.setText(self.engine.field(key).unit)
                if original is not None:
                    self._restore_numeric(original)
            else:
                counts = self._value_counts(key)
                selected = original.values if original is not None else ()
                if original is not None and original.operator == "ne":
                    # Old inequality excluded unanswered records as well.
                    selected = (None, *selected)
                selected_set = set(selected)
                choices = [(choice.value, choice.label) for choice in self.engine.condition_choices(key)]
                known = {value for value, _ in choices}
                # A legacy value may no longer occur in a record or form choice.
                for value in selected:
                    if value is not None and value not in known:
                        choices.append((value, str(value)))
                        known.add(value)
                for value, label in [(None, "(미입력)"), *choices]:
                    item = QListWidgetItem(f"{label} ({counts[value]:,}건)", self.values_list)
                    item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                    item.setData(Qt.ItemDataRole.UserRole, value)
                    item.setData(Qt.ItemDataRole.UserRole + 1, counts[value])
                    item.setData(Qt.ItemDataRole.UserRole + 2, label)
                    item.setToolTip(f"{label}\n현재 필터 기준 {counts[value]:,}건")
                    item.setCheckState(Qt.CheckState.Checked if value in selected_set else Qt.CheckState.Unchecked)
        self.values_list.blockSignals(False)
        self._loading = previous_loading
        if not previous_loading:
            self._dirty = True
        self._update_controls()

    def _restore_numeric(self, condition: StatsCondition) -> None:
        if condition.operator == "values" and condition.values == (None,):
            self.numeric_missing.setChecked(True)
            return
        low, high = condition.lower, condition.upper
        if condition.operator == "values" and len(condition.values) == 1:
            try:
                value = float(condition.values[0])
            except (TypeError, ValueError):
                return
            low, high = value, math.nextafter(value, math.inf)
        elif condition.operator in {"eq", "ne", "gt", "ge", "lt", "le"}:
            try:
                value = float(condition.value)
            except (TypeError, ValueError):
                return
            if condition.operator in {"eq", "ne"}:
                low, high = value, math.nextafter(value, math.inf)
                self.exclude_button.setChecked(condition.operator == "ne")
            elif condition.operator in {"gt", "ge"}:
                low = math.nextafter(value, math.inf) if condition.operator == "gt" else value
            else:
                high = math.nextafter(value, math.inf) if condition.operator == "le" else value
        elif condition.operator == "between" and high is not None:
            high = math.nextafter(high, math.inf)
        for edit, value in ((self.lower, low), (self.upper, high)):
            if value is not None:
                edit.setText(str(value))

    def _search_values(self, query: str) -> None:
        query = query.strip().casefold()
        for index in range(self.values_list.count()):
            item = self.values_list.item(index)
            item.setHidden(query not in str(item.data(Qt.ItemDataRole.UserRole + 2)).casefold())

    def _select_visible(self, checked: bool) -> None:
        self.values_list.blockSignals(True)
        for index in range(self.values_list.count()):
            item = self.values_list.item(index)
            if not item.isHidden():
                item.setCheckState(Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked)
        self.values_list.blockSignals(False)
        self._changed()

    def _changed(self, *_args) -> None:
        if not self._loading:
            self._dirty = True
            self.error_label.hide()
        self._update_controls()

    def _update_controls(self) -> None:
        if not hasattr(self, "apply_button"):
            return
        key = self.field.currentData()
        numeric = key is not None and self.engine.is_numeric(key)
        self.lower.setEnabled(not self.numeric_missing.isChecked())
        self.upper.setEnabled(not self.numeric_missing.isChecked())
        selected = any(
            self.values_list.item(index).checkState() == Qt.CheckState.Checked
            for index in range(self.values_list.count())
        )
        self.apply_button.setEnabled(key is not None and (numeric or selected or bool(self._original and not self._dirty)))
        if numeric:
            hint = "최소값은 포함하고 최대값은 포함하지 않습니다. 빈 칸은 제한하지 않습니다."
        elif self.exclude_button.isChecked():
            hint = "선택한 값 어느 것에도 해당하지 않는 사고만 포함합니다."
        else:
            hint = "선택한 값 중 하나라도 해당하는 사고를 포함합니다."
        if self._original is not None and not self._dirty:
            if self._original.operator in {"invalid", "not_applicable"}:
                state = "형식 오류" if self._original.operator == "invalid" else "해당 없음"
                hint = f"기존 ‘{state}’ 조건을 유지합니다. 값을 바꾸면 새 조건으로 적용됩니다."
            elif numeric and self._original.operator == "ne":
                hint = "기존 같지 않음 조건을 유지합니다. 범위를 바꾸면 새 포함·제외 규칙으로 적용됩니다."
        self.hint.setText(hint + " 건수는 현재 필터 기준입니다.")

    @staticmethod
    def _number(edit: QLineEdit) -> float | None:
        if not edit.text().strip():
            return None
        value = float(edit.text().strip())
        if not math.isfinite(value) or not edit.hasAcceptableInput():
            raise ValueError("형식이 잘못되었습니다")
        return value

    def _apply(self) -> None:
        key = self.field.currentData()
        if key is None:
            return
        try:
            if self._original is not None and not self._dirty:
                condition = self._original
            elif self.engine.is_numeric(key) and not self.numeric_missing.isChecked():
                low, high = self._number(self.lower), self._number(self.upper)
                if low is not None and high is not None and low >= high:
                    raise ValueError("최대값은 최소값보다 커야 합니다.")
                condition = StatsCondition(key, values=(), exclude=self.exclude_button.isChecked(),
                                           operator="range", lower=low, upper=high)
            else:
                values = (None,) if self.engine.is_numeric(key) else tuple(
                    self.values_list.item(index).data(Qt.ItemDataRole.UserRole)
                    for index in range(self.values_list.count())
                    if self.values_list.item(index).checkState() == Qt.CheckState.Checked
                )
                if not values:
                    raise ValueError("값을 선택해 주세요.")
                condition = StatsCondition(key, values=values, exclude=self.exclude_button.isChecked())
            self.engine.validate_condition(condition)
        except (TypeError, ValueError, KeyError) as error:
            self.error_label.setText(str(error) or "형식이 잘못되었습니다")
            self.error_label.show()
            return
        self.condition = condition
        self.applied.emit(condition)
        self.close()

    def open_at(self, anchor: QWidget) -> None:
        """Place the nonmodal popup near its chip/button, within its screen."""
        self.ensurePolished()
        self.adjustSize()
        if not self.numeric_panel.isHidden():
            self.resize(480, self.minimumSizeHint().height())
        else:
            self.resize(480, 530)
        available = anchor.screen().availableGeometry()
        self.resize(min(self.width(), available.width()), min(self.height(), available.height()))
        below = anchor.mapToGlobal(QPoint(0, anchor.height() + 4))
        above = anchor.mapToGlobal(QPoint(0, -self.height() - 4))
        y = below.y() if below.y() + self.height() <= available.bottom() + 1 else above.y()
        x = min(max(below.x(), available.left()), available.right() - self.width() + 1)
        y = min(max(y, available.top()), available.bottom() - self.height() + 1)
        self.move(x, y)
        self.show()
        self.field.setFocus(Qt.FocusReason.PopupFocusReason)
        self.field.lineEdit().selectAll()
