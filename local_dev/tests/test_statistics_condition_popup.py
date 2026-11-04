from __future__ import annotations

import math

import pytest
from PySide6.QtCore import QPoint, Qt
from PySide6.QtGui import QInputMethodEvent
from PySide6.QtWidgets import QApplication, QLabel, QStyle, QStyleOptionComboBox

from investigator.form_definition import FormDefinition
from investigator.models import InvestigationCase
from investigator.statistics import StatisticsEngine, StatsCondition, StatsFilter
from investigator.widgets.statistics_condition_popup import StatisticsConditionPopup


def make_popup(qtbot, *, values=(), key="weather", condition=None, filters=None, ignore_status=False):
    cases = [InvestigationCase(uid=str(index), values=raw) for index, raw in enumerate(values)]
    engine = StatisticsEngine(cases, FormDefinition.default())
    popup = StatisticsConditionPopup(engine, filters or StatsFilter(status="all"), condition,
                                     ignore_status=ignore_status)
    qtbot.addWidget(popup)
    popup.field.setCurrentIndex(popup.field.findData(key))
    return popup


def item_for(popup, value):
    return next(popup.values_list.item(index) for index in range(popup.values_list.count())
                if popup.values_list.item(index).data(Qt.ItemDataRole.UserRole) == value)


def check(popup, value):
    item_for(popup, value).setCheckState(Qt.CheckState.Checked)


def test_popup_not_modal_searchable_variables_and_no_operator_dropdown(qtbot):
    popup = make_popup(qtbot)
    assert popup.windowFlags() & Qt.WindowType.Popup
    assert popup.field.isEditable()
    assert popup.field.completer().filterMode() == Qt.MatchFlag.MatchContains
    assert not hasattr(popup, "operator")
    assert popup.field.itemText(popup.field.findData("institution")) == "기관코드"


@pytest.mark.parametrize("region", ["text", "blank", "left_frame", "bottom_frame", "arrow"])
def test_single_click_anywhere_in_variable_field_opens_dropdown(qtbot, region):
    popup = make_popup(qtbot)
    popup.show()
    field = popup.field
    edit = field.lineEdit()
    if region == "text":
        target, point = edit, QPoint(5, edit.height() // 2)
    elif region == "blank":
        target, point = edit, QPoint(edit.width() - 8, edit.height() // 2)
    elif region == "left_frame":
        target, point = field, QPoint(0, field.height() // 2)
    elif region == "bottom_frame":
        target, point = field, QPoint(field.width() // 2, field.height() - 1)
    else:
        option = QStyleOptionComboBox()
        field.initStyleOption(option)
        arrow = field.style().subControlRect(
            QStyle.ComplexControl.CC_ComboBox, option,
            QStyle.SubControl.SC_ComboBoxArrow, field,
        )
        target, point = field, arrow.center()

    qtbot.mouseClick(target, Qt.MouseButton.LeftButton, pos=point)
    qtbot.waitUntil(field.view().isVisible)
    QApplication.processEvents()
    assert popup.isVisible()
    assert edit.selectedText() == field.currentText()


def test_variable_dropdown_selection_refreshes_checklist_without_closing_popup(qtbot):
    popup = make_popup(qtbot)
    check(popup, "맑음")
    popup.show()
    field = popup.field
    qtbot.mouseClick(field.lineEdit(), Qt.MouseButton.LeftButton)
    qtbot.waitUntil(field.view().isVisible)
    index = field.model().index(field.findData("driver_sex"), 0)
    field.view().scrollTo(index)
    qtbot.mouseClick(field.view().viewport(), Qt.MouseButton.LeftButton,
                     pos=field.view().visualRect(index).center())

    assert field.currentData() == "driver_sex"
    assert not field.view().isVisible()
    assert popup.isVisible()
    assert [popup.values_list.item(row).data(Qt.ItemDataRole.UserRole)
            for row in range(popup.values_list.count())] == [
                None, *(choice.value for choice in popup.engine.condition_choices("driver_sex"))]
    assert not popup.apply_button.isEnabled()


@pytest.mark.parametrize("typing_target", ["editor", "dropdown", "dropdown_window"])
def test_variable_field_remains_keyboard_searchable_after_whole_field_click(qtbot, typing_target):
    popup = make_popup(qtbot)
    field = popup.field
    target_index = field.findData("driver_sex")
    field.setItemText(target_index, "Searchable Variable Target")
    popup.show()
    qtbot.mouseClick(field.lineEdit(), Qt.MouseButton.LeftButton)
    qtbot.waitUntil(field.view().isVisible)
    target = {
        "editor": QApplication.focusWidget(),
        "dropdown": field.view(),
        "dropdown_window": field.view().window(),
    }[typing_target]
    qtbot.keyClicks(target, "V")
    qtbot.keyClicks(QApplication.focusWidget(), "ariable")

    assert field.lineEdit().text() == "Variable"
    completer = field.completer()
    qtbot.waitUntil(completer.popup().isVisible)
    assert completer.completionCount() == 1
    assert completer.currentCompletion() == "Searchable Variable Target"
    qtbot.keyClick(completer.popup(), Qt.Key.Key_Down)
    qtbot.keyClick(completer.popup(), Qt.Key.Key_Return)
    assert field.currentData() == "driver_sex"
    assert popup.isVisible()


def test_variable_dropdown_preserves_navigation_confirmation_and_escape(qtbot):
    popup = make_popup(qtbot)
    popup.show()
    field = popup.field
    next_key = field.itemData(field.currentIndex() + 1)
    qtbot.mouseClick(field.lineEdit(), Qt.MouseButton.LeftButton)
    qtbot.keyClick(field.view(), Qt.Key.Key_Down)
    qtbot.keyClick(field.view(), Qt.Key.Key_Return)
    assert field.currentData() == next_key
    assert not field.view().isVisible()
    assert popup.isVisible()

    qtbot.mouseClick(field.lineEdit(), Qt.MouseButton.LeftButton)
    qtbot.keyClick(field.view(), Qt.Key.Key_Escape)
    assert not field.view().isVisible()
    assert popup.isVisible()


def test_variable_dropdown_forwards_input_method_composition_to_search_editor(qtbot):
    popup = make_popup(qtbot)
    field = popup.field
    field.setItemText(field.findData("driver_sex"), "검색 자모합성검증 변수")
    popup.show()
    qtbot.mouseClick(field.lineEdit(), Qt.MouseButton.LeftButton)
    QApplication.sendEvent(field.view(), QInputMethodEvent("자", []))
    assert not field.view().isVisible()
    commit = QInputMethodEvent()
    commit.setCommitString("자모합성검증")
    QApplication.sendEvent(field.lineEdit(), commit)

    assert field.lineEdit().text() == "자모합성검증"
    assert field.completer().completionCount() == 1
    assert field.completer().currentCompletion() == "검색 자모합성검증 변수"
    assert popup.isVisible()


def test_value_counts_are_current_filter_counts_including_unused_and_missing(qtbot):
    popup = make_popup(qtbot, values=[
        {"weather": "맑음", "driver_sex": "남성"},
        {"weather": "강수", "driver_sex": "남성"},
        {"weather": "강수", "driver_sex": "여성"},
        {"driver_sex": "남성"},
    ], filters=StatsFilter("all", conditions=(StatsCondition("driver_sex", "남성"),)))
    assert item_for(popup, "맑음").data(Qt.ItemDataRole.UserRole + 1) == 1
    assert item_for(popup, "강수").data(Qt.ItemDataRole.UserRole + 1) == 1
    assert item_for(popup, "강설").data(Qt.ItemDataRole.UserRole + 1) == 0
    assert item_for(popup, None).data(Qt.ItemDataRole.UserRole + 1) == 1
    assert "1건" in item_for(popup, "강수").text()


def test_counting_multichoice_is_distinct_per_accident(qtbot):
    popup = make_popup(qtbot, key="collision_targets", values=[
        {"collision_targets": ["승용", "승용", "어른"]},
        {"collision_targets": ["승용"]}, {},
    ])
    assert item_for(popup, "승용").data(Qt.ItemDataRole.UserRole + 1) == 2
    assert item_for(popup, "어른").data(Qt.ItemDataRole.UserRole + 1) == 1
    assert item_for(popup, None).data(Qt.ItemDataRole.UserRole + 1) == 1


def test_checklist_applies_multiple_or_values_and_exclusion(qtbot):
    popup = make_popup(qtbot)
    check(popup, "맑음")
    check(popup, "강설")
    popup.exclude_button.setChecked(True)
    with qtbot.waitSignal(popup.applied) as signal:
        popup._apply()
    assert signal.args[0].values == ("맑음", "강설")
    assert signal.args[0].exclude
    assert popup.condition == signal.args[0]


def test_clicking_value_label_and_checkbox_each_toggle_exactly_once(qtbot):
    popup = make_popup(qtbot)
    popup.show()
    item = item_for(popup, "맑음")
    rect = popup.values_list.visualItemRect(item)
    qtbot.mouseClick(popup.values_list.viewport(), Qt.MouseButton.LeftButton,
                     pos=QPoint(rect.left() + 75, rect.center().y()))
    assert item.checkState() == Qt.CheckState.Checked
    qtbot.mouseClick(popup.values_list.viewport(), Qt.MouseButton.LeftButton,
                     pos=QPoint(rect.left() + 8, rect.center().y()))
    assert item.checkState() == Qt.CheckState.Unchecked


def test_search_and_selection_buttons_only_affect_visible_results(qtbot):
    popup = make_popup(qtbot)
    popup.search.setText("강")
    popup.select_all_button.click()
    selected = [popup.values_list.item(index).data(Qt.ItemDataRole.UserRole)
                for index in range(popup.values_list.count())
                if popup.values_list.item(index).checkState() == Qt.CheckState.Checked]
    assert selected == ["강수", "강설"]
    popup.search.clear()
    popup.clear_selection_button.click()
    assert not popup.apply_button.isEnabled()


def test_missing_has_explicit_sentinel_and_cancel_does_not_apply(qtbot):
    popup = make_popup(qtbot)
    assert item_for(popup, None).text().startswith("(미입력)")
    check(popup, None)
    with qtbot.waitSignal(popup.applied) as signal:
        popup._apply()
    assert signal.args[0].values == (None,)
    second = make_popup(qtbot)
    captured = []
    second.applied.connect(captured.append)
    second.cancel_button.click()
    assert not captured
    assert second.condition is None


def test_numeric_range_is_inclusive_lower_exclusive_upper(qtbot):
    popup = make_popup(qtbot, key="vehicle_mileage")
    assert popup.checklist_panel.isHidden()
    assert not popup.numeric_panel.isHidden()
    popup.lower.setText("0")
    popup.upper.setText("10.5")
    with qtbot.waitSignal(popup.applied) as signal:
        popup._apply()
    condition = signal.args[0]
    assert condition.operator == "range"
    assert condition.lower == 0
    assert condition.upper == 10.5
    assert not condition.exclude


@pytest.mark.parametrize("low, high", [("10", "10"), ("20", "10"), ("nan", "10"), ("0", "inf")])
def test_numeric_invalid_range_keeps_popup_open_and_shows_inline_error(qtbot, low, high):
    popup = make_popup(qtbot, key="vehicle_mileage")
    popup.lower.setText(low)
    popup.upper.setText(high)
    popup._apply()
    assert popup.condition is None
    assert not popup.error_label.isHidden()


@pytest.mark.parametrize("low, high", [("", "10"), ("0", ""), ("", "")])
def test_numeric_open_bounds(qtbot, low, high):
    popup = make_popup(qtbot, key="vehicle_mileage")
    popup.lower.setText(low)
    popup.upper.setText(high)
    popup._apply()
    assert popup.condition.operator == "range"
    assert popup.condition.lower == (float(low) if low else None)
    assert popup.condition.upper == (float(high) if high else None)


def test_numeric_missing_legacy_reopens_and_can_be_applied_unchanged(qtbot):
    condition = StatsCondition("vehicle_mileage", operator="missing")
    popup = make_popup(qtbot, key="vehicle_mileage", condition=condition)
    assert popup.numeric_missing.isChecked()
    assert not popup.lower.isEnabled()
    popup._apply()
    assert popup.condition.values == (None,)


def test_edited_ne_condition_preselects_missing_to_preserve_legacy_semantics(qtbot):
    popup = make_popup(qtbot, condition=StatsCondition("weather", "강설", "ne"))
    assert popup.exclude_button.isChecked()
    assert item_for(popup, "강설").checkState() == Qt.CheckState.Checked
    assert item_for(popup, None).checkState() == Qt.CheckState.Checked
    check(popup, "강수")
    popup._apply()
    assert popup.condition.exclude
    assert set(popup.condition.values) == {None, "강수", "강설"}


@pytest.mark.parametrize("operator, low, high", [
    ("gt", math.nextafter(10.0, math.inf), None), ("ge", 10.0, None),
    ("lt", None, 10.0), ("le", None, math.nextafter(10.0, math.inf)),
])
def test_old_numeric_comparisons_reopen_with_lossless_half_open_bounds(qtbot, operator, low, high):
    popup = make_popup(qtbot, key="vehicle_mileage",
                       condition=StatsCondition("vehicle_mileage", 10.0, operator))
    assert (float(popup.lower.text()) if popup.lower.text() else None) == low
    assert (float(popup.upper.text()) if popup.upper.text() else None) == high
    popup._apply()
    assert popup.condition.operator == operator


def test_retired_value_is_preserved_and_current_count_is_zero(qtbot):
    popup = make_popup(qtbot, condition=StatsCondition("weather", values=("옛 라벨",)))
    assert item_for(popup, "옛 라벨").checkState() == Qt.CheckState.Checked
    assert item_for(popup, "옛 라벨").data(Qt.ItemDataRole.UserRole + 1) == 0
    popup._apply()
    assert popup.condition.values == ("옛 라벨",)


def test_special_graph_condition_is_not_silently_replaced(qtbot):
    popup = make_popup(qtbot, condition=StatsCondition("weather", operator="invalid"))
    assert "형식 오류" in popup.hint.text()
    popup._apply()
    assert popup.condition.operator == "invalid"


def test_popup_is_anchored_inside_available_screen(qtbot):
    anchor = QLabel("조건")
    qtbot.addWidget(anchor)
    anchor.setGeometry(0, 0, 100, 30)
    anchor.show()
    popup = make_popup(qtbot)
    popup.open_at(anchor)
    assert popup.isVisible()
    assert anchor.screen().availableGeometry().contains(popup.frameGeometry())


def test_thousands_of_distinct_values_do_not_refilter_for_each_choice(qtbot, monkeypatch):
    engine = StatisticsEngine([
        InvestigationCase(uid=str(index), values={"vehicle_model_name": f"모델{index}"})
        for index in range(5_000)
    ], FormDefinition.default())
    calls = []
    original = engine.filtered_cases

    def record_filter(*args, **kwargs):
        calls.append(1)
        return original(*args, **kwargs)

    monkeypatch.setattr(engine, "filtered_cases", record_filter)
    popup = StatisticsConditionPopup(engine, StatsFilter("all"))
    qtbot.addWidget(popup)
    popup.field.setCurrentIndex(popup.field.findData("vehicle_model_name"))
    assert popup.values_list.count() == 5_001
    assert len(calls) == 1
    popup.select_all_button.click()
    popup._apply()
    assert len(popup.condition.values) == 5_001
