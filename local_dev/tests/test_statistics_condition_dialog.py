from __future__ import annotations

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QDialog, QMessageBox, QStyle, QStyleOptionComboBox

from investigator.form_definition import FormDefinition, new_field
from investigator.models import InvestigationCase
from investigator.schema import FieldKind
from investigator.statistics import StatisticsEngine, StatsCondition
from investigator.widgets.statistics_condition_dialog import StatisticsConditionDialog


def make_dialog(qtbot, key="weather", *, values=(), definition=None, field_keys=None):
    cases = [InvestigationCase(uid=str(index), values=raw) for index, raw in enumerate(values)]
    engine = StatisticsEngine(cases, definition or FormDefinition.default())
    dialog = StatisticsConditionDialog(engine, field_keys=field_keys)
    qtbot.addWidget(dialog)
    if key is not None:
        dialog.field.setCurrentIndex(dialog.field.findData(key))
    return dialog


def choose_operator(dialog, operator):
    index = dialog.operator.findData(operator)
    assert index >= 0
    dialog.operator.setCurrentIndex(index)


def test_categories_are_noneditable_and_offer_all_configured_labels_even_without_cases(qtbot):
    dialog = make_dialog(qtbot)
    assert not dialog.value.isEditable()
    assert [dialog.value.itemText(index) for index in range(dialog.value.count())] == [
        "맑음", "강수", "강설", "안개",
    ]
    assert dialog.operator.findData("between") == -1
    dialog.value.setCurrentIndex(dialog.value.findData("강설"))
    dialog._accept()
    assert dialog.condition == StatsCondition("weather", "강설")
    assert dialog.result() == QDialog.DialogCode.Accepted


def test_current_custom_labels_are_visible_and_original_stored_values_are_used(qtbot):
    definition = FormDefinition.default()
    custom = new_field()
    custom.update(
        label="새 변수", kind=FieldKind.MULTI.value,
        choices=[
            {"id": "a", "label": "사용자가 변경한 라벨", "value": "hidden.internal.code", "csv_column": ""},
            {"id": "b", "label": "아직 선택되지 않은 값", "value": "unused", "csv_column": ""},
        ],
    )
    definition.tabs[-1]["fields"].append(custom)
    definition.validate()
    dialog = make_dialog(qtbot, custom["key"], definition=definition,
                         values=[{custom["key"]: ["hidden.internal.code"]}])
    assert dialog.value.currentText() == "사용자가 변경한 라벨"
    assert dialog.value.findData("unused") >= 0
    assert dialog.operator.currentText() == "포함"
    assert dialog.operator.itemText(dialog.operator.findData("ne")) == "포함하지 않음"
    dialog._accept()
    assert dialog.condition == StatsCondition(custom["key"], "hidden.internal.code")


def test_free_text_fields_show_existing_distinct_values_without_requiring_typing(qtbot):
    dialog = make_dialog(qtbot, "vehicle_model_name", values=[
        {"vehicle_model_name": "아반떼"}, {"vehicle_model_name": "쏘나타"},
        {"vehicle_model_name": "아반떼"}, {},
    ])
    assert not dialog.value.isEditable()
    assert dialog.value.count() == 2
    assert {dialog.value.itemText(index) for index in range(2)} == {"아반떼", "쏘나타"}
    dialog.value.setCurrentIndex(dialog.value.findData("쏘나타"))
    dialog._accept()
    assert dialog.condition == StatsCondition("vehicle_model_name", "쏘나타")


def test_long_saved_text_does_not_force_the_dialog_beyond_a_reasonable_width(qtbot):
    dialog = make_dialog(qtbot, "vehicle_model_name", values=[{"vehicle_model_name": "긴 모델명" * 1000}])
    dialog.show()
    dialog.adjustSize()
    assert dialog.width() < 700
    assert dialog.value.currentData() == "긴 모델명" * 1000


def test_no_values_has_clear_hint_and_disables_value_operators_but_allows_missing(qtbot):
    dialog = make_dialog(qtbot, "vehicle_model_name")
    assert dialog.operator.currentData() == "missing"
    assert not dialog.operator.model().item(dialog.operator.findData("eq")).isEnabled()
    assert not dialog.operator.model().item(dialog.operator.findData("ne")).isEnabled()
    assert "선택할 값이 없습니다" in dialog.hint.text()
    assert dialog.value.isHidden()
    dialog._accept()
    assert dialog.condition == StatsCondition("vehicle_model_name", operator="missing")


@pytest.mark.parametrize("operator", ["eq", "ne", "gt", "ge", "lt", "le"])
@pytest.mark.parametrize("text, value", [("0", 0.0), ("12.75", 12.75), ("-3.5", -3.5), ("1e3", 1000.0)])
def test_numeric_comparisons_accept_zero_decimal_negative_and_scientific_values(qtbot, operator, text, value):
    dialog = make_dialog(qtbot, "vehicle_mileage")
    choose_operator(dialog, operator)
    assert not dialog.number_row.isHidden()
    assert dialog.value.isHidden()
    assert dialog.unit.text() == "km"
    dialog.number.setText(text)
    dialog._accept()
    assert dialog.condition == StatsCondition("vehicle_mileage", value, operator)
    assert dialog.result() == QDialog.DialogCode.Accepted


@pytest.mark.parametrize("low, high", [("0", "10.5"), ("10", "10"), ("-12.5", "-1")])
def test_between_includes_both_bounds_and_permits_equal_bounds(qtbot, low, high):
    dialog = make_dialog(qtbot, "vehicle_mileage")
    choose_operator(dialog, "between")
    dialog.lower.setText(low)
    dialog.upper.setText(high)
    assert not dialog.range_row.isHidden()
    assert dialog.number_row.isHidden()
    dialog._accept()
    assert dialog.condition == StatsCondition(
        "vehicle_mileage", operator="between", lower=float(low), upper=float(high),
    )


@pytest.mark.parametrize("text", ["", "abc", "nan", "inf", "-inf", "1,000", "1e9999"])
def test_invalid_or_missing_numbers_cannot_create_condition(qtbot, monkeypatch, text):
    warnings = []
    monkeypatch.setattr(QMessageBox, "warning", lambda *args: warnings.append(args[2]))
    dialog = make_dialog(qtbot, "vehicle_mileage")
    dialog.number.setText(text)
    dialog._accept()
    assert dialog.condition is None
    assert dialog.result() == QDialog.DialogCode.Rejected
    assert warnings == ["올바른 숫자를 입력해 주세요."]


@pytest.mark.parametrize("low, high", [("", "10"), ("0", ""), ("20", "10"), ("nan", "10")])
def test_invalid_between_requires_both_finite_bounds_in_order(qtbot, monkeypatch, low, high):
    warnings = []
    monkeypatch.setattr(QMessageBox, "warning", lambda *args: warnings.append(args[2]))
    dialog = make_dialog(qtbot, "vehicle_mileage")
    choose_operator(dialog, "between")
    dialog.lower.setText(low)
    dialog.upper.setText(high)
    dialog._accept()
    assert dialog.condition is None
    assert len(warnings) == 1


@pytest.mark.parametrize("operator", ["missing", "not_missing"])
def test_presence_conditions_need_no_input_even_for_numeric_fields(qtbot, operator):
    dialog = make_dialog(qtbot, "vehicle_mileage")
    choose_operator(dialog, operator)
    assert dialog.number_row.isHidden()
    assert dialog.range_row.isHidden()
    assert dialog.value.isHidden()
    assert dialog.value_label.isHidden()
    dialog._accept()
    assert dialog.condition == StatsCondition("vehicle_mileage", operator=operator)


def test_switching_fields_resets_inputs_operators_and_values(qtbot):
    dialog = make_dialog(qtbot, "vehicle_mileage")
    choose_operator(dialog, "between")
    dialog.lower.setText("10")
    dialog.upper.setText("20")
    dialog.number.setText("99")
    dialog.field.setCurrentIndex(dialog.field.findData("weather"))
    assert dialog.operator.currentData() == "eq"
    assert dialog.operator.findData("between") == -1
    assert dialog.lower.text() == dialog.upper.text() == dialog.number.text() == ""
    assert not dialog.value.isHidden()
    assert dialog.number_row.isHidden()
    dialog.field.setCurrentIndex(dialog.field.findData("vehicle_mileage"))
    assert dialog.operator.currentData() == "eq"
    assert dialog.value.count() == 0
    assert not dialog.number_row.isHidden()


def test_restricted_field_keys_and_title_remain_compatible(qtbot):
    dialog = make_dialog(qtbot, field_keys={"weather", "driver_sex"})
    dialog.setWindowTitle("보고서 선택 조건 추가")
    assert dialog.field.count() == 2
    assert dialog.field.findData("vehicle_mileage") < 0
    assert dialog.windowTitle() == "보고서 선택 조건 추가"


def test_no_fields_is_safe_and_cannot_create_condition(qtbot):
    dialog = make_dialog(qtbot, key=None, field_keys=set())
    dialog._accept()
    assert dialog.condition is None
    assert "변수가 없습니다" in dialog.hint.text()


@pytest.mark.parametrize("key", ["weather", "obstacle_pedestrians", "vehicle_mileage"])
def test_not_equal_explains_that_missing_values_are_excluded(qtbot, key):
    dialog = make_dialog(qtbot, key)
    choose_operator(dialog, "ne")
    assert "미입력은 제외합니다" in dialog.hint.text()
    assert "OR" in dialog.hint.text()


def test_all_dropdowns_draw_visible_chevrons_and_arrow_click_still_opens_choices(qtbot):
    dialog = make_dialog(qtbot)
    dialog.show()
    for combo in (dialog.field, dialog.operator, dialog.value):
        combo.setStyleSheet("QComboBox { background: white; color: black; border: 0; }")
        option = QStyleOptionComboBox()
        combo.initStyleOption(option)
        arrow = combo.style().subControlRect(
            QStyle.ComplexControl.CC_ComboBox, option,
            QStyle.SubControl.SC_ComboBoxArrow, combo,
        )
        image = combo.grab().toImage()
        center = arrow.center()
        dark_pixels = sum(
            image.pixelColor(x, y).lightness() < 160
            for x in range(center.x() - 5, center.x() + 6)
            for y in range(center.y() - 3, center.y() + 4)
        )
        assert dark_pixels >= 6
        qtbot.mouseClick(combo, Qt.MouseButton.LeftButton, pos=center)
        assert combo.view().isVisible()
        combo.hidePopup()
