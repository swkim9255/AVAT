"""Value-aware condition editor shared by statistics and report selection."""

from __future__ import annotations

import math

from PySide6.QtCore import QLocale, QPointF, Qt
from PySide6.QtGui import QDoubleValidator, QPainter, QPaintEvent, QPalette, QPen
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QStyle,
    QStyleOptionComboBox,
    QStylePainter,
    QVBoxLayout,
    QWidget,
)

from investigator.schema import FieldKind
from investigator.statistics import StatisticsEngine, StatsCondition
from investigator.widgets.scroll_safe_combo import ScrollSafeComboBox


def _label(text: str, parent: QWidget) -> QLabel:
    result = QLabel(text, parent)
    result.setTextFormat(Qt.TextFormat.PlainText)
    result.setWordWrap(True)
    result.setObjectName("secondaryText")
    return result


class _ConditionComboBox(ScrollSafeComboBox):
    """Keep a visible dropdown affordance with every native/theme style."""

    def paintEvent(self, event: QPaintEvent) -> None:
        option = QStyleOptionComboBox()
        self.initStyleOption(option)
        arrow_rect = self.style().subControlRect(
            QStyle.ComplexControl.CC_ComboBox,
            option,
            QStyle.SubControl.SC_ComboBoxArrow,
            self,
        )
        # Replace only the arrow drawing; retain native frame, focus and label.
        option.subControls &= ~QStyle.SubControl.SC_ComboBoxArrow
        painter = QStylePainter(self)
        painter.drawComplexControl(QStyle.ComplexControl.CC_ComboBox, option)
        painter.drawControl(QStyle.ControlElement.CE_ComboBoxLabel, option)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        group = QPalette.ColorGroup.Active if self.isEnabled() else QPalette.ColorGroup.Disabled
        painter.setPen(QPen(
            self.palette().color(group, QPalette.ColorRole.Text),
            1.5,
            Qt.PenStyle.SolidLine,
            Qt.PenCapStyle.RoundCap,
            Qt.PenJoinStyle.RoundJoin,
        ))
        center = QPointF(arrow_rect.center())
        painter.drawLine(center + QPointF(-4, -2), center + QPointF(0, 2))
        painter.drawLine(center + QPointF(0, 2), center + QPointF(4, -2))


class StatisticsConditionDialog(QDialog):
    """Choose known labels, or enter validated numeric comparisons.

    Choice item data is always the stored value; its visible label may have
    been renamed by the project form editor. No free-text guessing is needed.
    """

    def __init__(
        self,
        engine: StatisticsEngine,
        parent: QWidget | None = None,
        *,
        field_keys: set[str] | None = None,
        title: str = "통계 조건 추가",
    ) -> None:
        super().__init__(parent)
        self.engine = engine
        self.condition: StatsCondition | None = None
        self.setWindowTitle(title)
        self.setMinimumWidth(440)
        layout = QVBoxLayout(self)
        layout.setSpacing(10)

        layout.addWidget(_label("변수", self))
        self.field = _ConditionComboBox(self)
        for spec in engine.fields:
            if field_keys is None or spec.key in field_keys:
                self.field.addItem(spec.label, spec.key)
        layout.addWidget(self.field)

        layout.addWidget(_label("조건", self))
        self.operator = _ConditionComboBox(self)
        layout.addWidget(self.operator)

        self.value_label = _label("값", self)
        layout.addWidget(self.value_label)
        # Retain the public `value` combo used by report dialogs and callers.
        self.value = _ConditionComboBox(self)
        self.value.setEditable(False)
        # A long stored model name/note must not stretch the whole dialog.
        self.value.setMinimumContentsLength(26)
        self.value.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.value.setMaxVisibleItems(16)
        layout.addWidget(self.value)

        self.number_row = QWidget(self)
        number_layout = QHBoxLayout(self.number_row)
        number_layout.setContentsMargins(0, 0, 0, 0)
        self.number = self._number_edit("숫자를 입력하세요")
        number_layout.addWidget(self.number)
        self.unit = _label("", self.number_row)
        number_layout.addWidget(self.unit)
        layout.addWidget(self.number_row)

        self.range_row = QWidget(self)
        range_layout = QHBoxLayout(self.range_row)
        range_layout.setContentsMargins(0, 0, 0, 0)
        self.lower = self._number_edit("최솟값 (이상)")
        self.upper = self._number_edit("최댓값 (이하)")
        range_layout.addWidget(self.lower, 1)
        range_layout.addWidget(_label("~", self.range_row))
        range_layout.addWidget(self.upper, 1)
        self.range_unit = _label("", self.range_row)
        range_layout.addWidget(self.range_unit)
        layout.addWidget(self.range_row)

        self.hint = _label("", self)
        layout.addWidget(self.hint)
        self.buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel,
            self,
        )
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setText("조건 추가")
        self.buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("취소")
        self.buttons.accepted.connect(self._accept)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)
        self.field.currentIndexChanged.connect(self._field_changed)
        self.operator.currentIndexChanged.connect(self._operator_changed)
        self._field_changed()

    def _number_edit(self, placeholder: str) -> QLineEdit:
        edit = QLineEdit(self)
        edit.setMinimumWidth(0)
        edit.setPlaceholderText(placeholder)
        validator = QDoubleValidator(edit)
        validator.setNotation(QDoubleValidator.Notation.ScientificNotation)
        locale = QLocale.c()
        locale.setNumberOptions(QLocale.NumberOption.RejectGroupSeparator)
        validator.setLocale(locale)
        edit.setValidator(validator)
        return edit

    def _field_changed(self) -> None:
        key = self.field.currentData()
        self.value.clear()
        for edit in (self.number, self.lower, self.upper):
            edit.clear()
        self.operator.blockSignals(True)
        self.operator.clear()
        if key is None:
            self.operator.blockSignals(False)
            self.buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(False)
            self._operator_changed()
            return
        spec = self.engine.field(key)
        numeric = self.engine.is_numeric(key)
        if numeric:
            for text, operator in (
                ("같음 (=)", "eq"), ("같지 않음 (≠)", "ne"),
                ("초과 (>)", "gt"), ("이상 (≥)", "ge"),
                ("미만 (<)", "lt"), ("이하 (≤)", "le"),
                ("사이 (이상 ~ 이하)", "between"),
            ):
                self.operator.addItem(text, operator)
        else:
            for choice in self.engine.condition_choices(key):
                self.value.addItem(choice.label, choice.value)
            multi = spec.kind == FieldKind.MULTI
            self.operator.addItem("포함" if multi else "같음", "eq")
            self.operator.addItem("포함하지 않음" if multi else "같지 않음", "ne")
            if not self.value.count():
                for index in range(2):
                    self.operator.model().item(index).setEnabled(False)
        self.operator.addItem("미입력", "missing")
        self.operator.addItem("입력됨", "not_missing")
        if not numeric and not self.value.count():
            self.operator.setCurrentIndex(self.operator.findData("missing"))
        self.operator.blockSignals(False)
        self.unit.setText(spec.unit)
        self.range_unit.setText(spec.unit)
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(True)
        self._operator_changed()

    def _operator_changed(self) -> None:
        key = self.field.currentData()
        operator = self.operator.currentData()
        numeric = key is not None and self.engine.is_numeric(key)
        value_operator = operator not in {None, "missing", "not_missing"}
        self.value_label.setVisible(value_operator)
        self.value.setVisible(value_operator and not numeric)
        self.number_row.setVisible(value_operator and numeric and operator != "between")
        self.range_row.setVisible(value_operator and numeric and operator == "between")
        if key is None:
            hint = "조건을 설정할 수 있는 변수가 없습니다."
        elif not numeric and not self.value.count():
            hint = "선택할 값이 없습니다. 미입력 여부로 조건을 설정할 수 있습니다."
        elif operator == "between":
            hint = "최솟값과 최댓값을 모두 포함합니다."
        elif operator == "ne":
            hint = "미입력은 제외합니다. 미입력도 포함하려면 미입력 조건을 OR로 추가하세요."
        elif value_operator and not numeric:
            hint = "선택 가능한 라벨과 현재 저장된 값에서 고르세요."
        else:
            hint = ""
        self.hint.setText(hint)
        self.hint.setVisible(bool(hint))

    @staticmethod
    def _finite_number(edit: QLineEdit) -> float:
        value = float(edit.text().strip())
        if not math.isfinite(value) or not edit.hasAcceptableInput():
            raise ValueError
        return value

    def _accept(self) -> None:
        key = self.field.currentData()
        operator = self.operator.currentData()
        if key is None or operator is None:
            return
        if operator in {"missing", "not_missing"}:
            condition = StatsCondition(key, operator=operator)
        elif self.engine.is_numeric(key):
            try:
                if operator == "between":
                    low, high = self._finite_number(self.lower), self._finite_number(self.upper)
                    if low > high:
                        raise ValueError
                    condition = StatsCondition(key, operator=operator, lower=low, upper=high)
                else:
                    condition = StatsCondition(key, self._finite_number(self.number), operator)
            except ValueError:
                message = (
                    "최솟값과 최댓값을 올바르게 입력해 주세요. 최솟값은 최댓값 이하여야 합니다."
                    if operator == "between" else "올바른 숫자를 입력해 주세요."
                )
                QMessageBox.warning(self, "조건 확인", message)
                return
        else:
            if self.value.currentIndex() < 0:
                QMessageBox.warning(self, "조건 확인", "비교할 값을 선택해 주세요.")
                return
            condition = StatsCondition(key, self.value.currentData(), operator)
        self.condition = condition
        self.accept()
