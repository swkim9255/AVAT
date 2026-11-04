"""Explicit one-level OR groups, editable chips, and project state migration."""

from __future__ import annotations

from pathlib import Path

import pytest
from PySide6.QtCore import QDate, QPoint, Qt
from PySide6.QtWidgets import QCheckBox, QMenu, QPushButton, QToolButton

from investigator.form_definition import FormDefinition
from investigator.models import InvestigationCase
from investigator.statistics import AnyOf, StatsCondition
from investigator.theme import apply_theme
from investigator.widgets.statistics_panel import StatisticsPanel


def _case(uid: str, *, status: str = "completed", **values) -> InvestigationCase:
    return InvestigationCase(
        uid=uid, status=status,
        values={"institution": "samsung", "accident_classification": uid, **values},
    )


def _panel(qtbot, cases, *, width=1400) -> StatisticsPanel:
    panel = StatisticsPanel()
    qtbot.addWidget(panel)
    panel.resize(width, 900)
    panel.set_data(cases, FormDefinition.default(), workspace=Path("test_project"))
    panel.show()
    return panel


def _uids(panel: StatisticsPanel) -> list[str]:
    return [case.uid for case in panel.engine.filtered_cases(panel.filters())]


def _group(panel: StatisticsPanel, *indices: int) -> None:
    panel._selected_conditions = set(indices)
    panel.group_selected_conditions()


def _chip(panel: StatisticsPanel, index: int, cls, name: str):
    # Retired chips stay QObject children until deleteLater is processed; the
    # live layout is authoritative even before pending show events are handled.
    frame = next(item.widget() for i in range(panel.chips_layout.count())
                 if (item := panel.chips_layout.itemAt(i)).widget() is not None
                 and item.widget().property("conditionIndex") == index)
    return frame.findChild(cls, name)


def test_conditions_are_and_until_selected_chips_are_explicitly_grouped(qtbot):
    panel = _panel(qtbot, [
        _case("a", weather="맑음", driver_sex="남성"),
        _case("b", weather="강수", driver_sex="여성"),
        _case("overlap", weather="맑음", driver_sex="여성"),
        _case("neither", weather="강수", driver_sex="남성"),
    ])
    panel.add_condition(StatsCondition("weather", "맑음"))
    panel.add_condition(StatsCondition("driver_sex", "여성"))
    assert _uids(panel) == ["overlap"]
    _group(panel, 0, 1)
    assert len(panel.conditions) == 1 and isinstance(panel.conditions[0], AnyOf)
    assert _uids(panel) == ["a", "b", "overlap"]
    assert panel.target_label.text() == "대상 3건 / 전체 4건"
    assert not panel._selected_conditions


def test_group_checkbox_selection_enables_group_action_only_for_two_leaves(qtbot):
    panel = _panel(qtbot, [_case("a", weather="맑음", driver_sex="여성")])
    panel.add_condition(StatsCondition("weather", "맑음"))
    panel.add_condition(StatsCondition("driver_sex", "여성"))
    action = next(button for button in panel.findChildren(QPushButton)
                  if button.text() == "OR로 묶기" and not button.isHidden())
    assert not action.isEnabled()
    _chip(panel, 0, QCheckBox, "filterChipSelect").setChecked(True)
    assert panel._selected_conditions == {0} and not action.isEnabled()
    _chip(panel, 1, QCheckBox, "filterChipSelect").setChecked(True)
    assert panel._selected_conditions == {0, 1} and action.isEnabled()
    qtbot.mouseClick(action, Qt.MouseButton.LeftButton)
    assert len(panel.conditions) == 1 and isinstance(panel.conditions[0], AnyOf)


def test_status_dates_and_ungrouped_conditions_remain_required_outside_union(qtbot):
    panel = _panel(qtbot, [
        _case("a", weather="맑음", vehicle_manufacturer="kia", incident_date="20260115"),
        _case("b", driver_sex="여성", vehicle_manufacturer="kia", incident_date="20260120"),
        _case("other_make", weather="맑음", vehicle_manufacturer="bmw", incident_date="20260115"),
        _case("draft", status="draft", weather="맑음", vehicle_manufacturer="kia", incident_date="20260115"),
        _case("outside", weather="맑음", vehicle_manufacturer="kia", incident_date="20260215"),
        _case("no_date", driver_sex="여성", vehicle_manufacturer="kia"),
    ])
    panel.add_condition(StatsCondition("weather", "맑음"))
    panel.add_condition(StatsCondition("driver_sex", "여성"))
    _group(panel, 0, 1)
    panel.add_condition(StatsCondition("vehicle_manufacturer", "kia"))
    panel.date_from.setDate(QDate(2026, 1, 1))
    panel.date_to.setDate(QDate(2026, 1, 31))
    panel.period.setChecked(True)
    assert _uids(panel) == ["a", "b"]
    panel.status.setCurrentIndex(panel.status.findData("all"))
    assert _uids(panel) == ["a", "b", "draft"]


def test_empty_conditions_keep_base_population(qtbot):
    panel = _panel(qtbot, [_case("a"), _case("b"), _case("draft", status="draft")])
    assert panel.conditions == []
    assert _uids(panel) == ["a", "b"]


def test_chip_body_edits_with_popup_without_removing_condition(qtbot):
    panel = _panel(qtbot, [_case("a", weather="맑음"), _case("b", weather="강수")])
    panel.add_condition(StatsCondition("weather", "맑음"))
    before = tuple(panel.conditions)
    body = _chip(panel, 0, QPushButton, "filterChip")
    assert "맑음" in body.text() and "×" not in body.text()
    qtbot.mouseClick(body, Qt.MouseButton.LeftButton)
    popup = panel._condition_popup
    assert popup is not None and popup.isVisible()
    assert popup.windowFlags() & Qt.WindowType.Popup
    assert tuple(panel.conditions) == before and _uids(panel) == ["a"]
    popup.close()
    assert tuple(panel.conditions) == before


def test_edit_popup_apply_replaces_instead_of_appending(qtbot):
    panel = _panel(qtbot, [_case("a", weather="맑음"), _case("b", weather="강수")])
    panel.add_condition(StatsCondition("weather", "맑음"))
    panel.edit_condition(0)
    popup = panel._condition_popup
    popup.applied.emit(StatsCondition("weather", values=("강수",)))
    assert len(panel.conditions) == 1 and _uids(panel) == ["b"]
    popup.close()


def test_only_separate_toolbutton_removes_chip(qtbot):
    panel = _panel(qtbot, [_case("a", weather="맑음"), _case("b", weather="강수")])
    panel.add_condition(StatsCondition("weather", "맑음"))
    remove = _chip(panel, 0, QToolButton, "filterChipRemove")
    assert remove.text() == "×"
    qtbot.mouseClick(remove, Qt.MouseButton.LeftButton)
    assert panel.conditions == [] and _uids(panel) == ["a", "b"]


def test_ungroup_restores_leaf_order_and_and_semantics(qtbot):
    panel = _panel(qtbot, [
        _case("a", weather="맑음", driver_sex="남성"),
        _case("both", weather="맑음", driver_sex="여성"),
        _case("b", weather="강수", driver_sex="여성"),
    ])
    panel.add_condition(StatsCondition("weather", "맑음"))
    panel.add_condition(StatsCondition("driver_sex", "여성"))
    _group(panel, 0, 1)
    panel.ungroup_condition(0)
    assert all(isinstance(item, StatsCondition) for item in panel.conditions)
    assert [item.field_key for item in panel.conditions] == ["weather", "driver_sex"]
    assert _uids(panel) == ["both"]


def test_group_selection_cannot_create_nested_groups(qtbot):
    panel = _panel(qtbot, [_case("a", weather="맑음", driver_sex="여성", vehicle_manufacturer="kia")])
    for condition in (StatsCondition("weather", "맑음"), StatsCondition("driver_sex", "여성"),
                      StatsCondition("vehicle_manufacturer", "kia")):
        panel.add_condition(condition)
    _group(panel, 0, 1)
    before = tuple(panel.conditions)
    _group(panel, 0, 1)
    assert tuple(panel.conditions) == before
    assert all(isinstance(child, StatsCondition)
               for group in panel.conditions if isinstance(group, AnyOf)
               for child in group.conditions)


def test_group_chip_body_member_edit_preserves_other_member(qtbot):
    panel = _panel(qtbot, [_case("a", weather="맑음"), _case("b", weather="강수"),
                           _case("c", driver_sex="여성")])
    panel.add_condition(StatsCondition("weather", "맑음"))
    panel.add_condition(StatsCondition("driver_sex", "여성"))
    _group(panel, 0, 1)
    original_other = panel.conditions[0].conditions[1]
    assert not _chip(panel, 0, QCheckBox, "filterChipSelect").isEnabled()
    qtbot.mouseClick(_chip(panel, 0, QPushButton, "filterChip"), Qt.MouseButton.LeftButton)
    menu = panel._condition_menu
    assert menu.isVisible()
    assert len(menu.actions()) == 3  # Instruction plus two member conditions.
    menu.actions()[1].trigger()
    menu.close()
    qtbot.waitUntil(lambda: panel._condition_popup is not None and panel._condition_popup.isVisible())
    panel._condition_popup.applied.emit(StatsCondition("weather", values=("강수",)))
    panel._condition_popup.close()
    assert isinstance(panel.conditions[0], AnyOf)
    assert panel.conditions[0].conditions[1] == original_other
    assert _uids(panel) == ["b", "c"]


def test_group_context_menu_exposes_ungroup_action(qtbot):
    panel = _panel(qtbot, [_case("a", weather="맑음", driver_sex="여성")])
    panel.add_condition(StatsCondition("weather", "맑음"))
    panel.add_condition(StatsCondition("driver_sex", "여성"))
    _group(panel, 0, 1)
    body = _chip(panel, 0, QPushButton, "filterChip")
    body.customContextMenuRequested.emit(body.rect().center())
    menu = next(menu for menu in panel.findChildren(QMenu)
                if menu.isVisible() and any(action.text() == "묶음 풀기" for action in menu.actions()))
    menu.actions()[0].trigger()
    menu.close()
    assert len(panel.conditions) == 2
    assert all(isinstance(item, StatsCondition) for item in panel.conditions)


def test_grouped_state_roundtrips_and_does_not_leak_into_other_project(qtbot):
    cases = [_case("a", weather="맑음", driver_sex="여성"), _case("b", weather="강수")]
    panel = _panel(qtbot, cases)
    panel.add_condition(StatsCondition("weather", values=("맑음", "강설")))
    panel.add_condition(StatsCondition("driver_sex", "여성"))
    _group(panel, 0, 1)
    saved = panel.capture_state()
    panel.restore_state({})
    panel.set_data([_case("other", weather="강수")], FormDefinition.default())
    assert panel.conditions == [] and _uids(panel) == ["other"]
    panel.restore_state(saved)
    panel.set_data(cases, FormDefinition.default())
    assert len(panel.conditions) == 1 and isinstance(panel.conditions[0], AnyOf)
    assert _uids(panel) == ["a"]


def test_legacy_global_any_and_graph_constraints_restore_as_outer_and(qtbot):
    cases = [
        _case("a", weather="맑음", vehicle_manufacturer="kia"),
        _case("b", driver_sex="여성", vehicle_manufacturer="kia"),
        _case("other_make", weather="맑음", vehicle_manufacturer="bmw"),
    ]
    panel = _panel(qtbot, cases)
    panel.restore_state({
        "conditions": (StatsCondition("weather", "맑음"), StatsCondition("driver_sex", "여성")),
        "condition_mode": "any",
        "required_conditions": (StatsCondition("vehicle_manufacturer", "kia"),),
    })
    panel.set_data(cases, FormDefinition.default())
    assert len(panel.conditions) == 2 and isinstance(panel.conditions[0], AnyOf)
    assert panel.conditions[1].field_key == "vehicle_manufacturer"
    assert _uids(panel) == ["a", "b"]


@pytest.mark.parametrize(("record", "expected"), [
    ({"field_key": "weather", "operator": "eq", "value": "맑음"}, ["a"]),
    ({"field_key": "weather", "operator": "missing"}, ["missing"]),
    ({"field_key": "vehicle_mileage", "operator": "range", "lower": 0, "upper": 100}, ["a"]),
])
def test_legacy_condition_dict_states_are_converted(qtbot, record, expected):
    cases = [_case("a", weather="맑음", vehicle_mileage="0"),
             _case("b", weather="강수", vehicle_mileage="100"), _case("missing")]
    panel = _panel(qtbot, cases)
    panel.restore_state({"conditions": (record,)})
    panel.set_data(cases, FormDefinition.default())
    assert len(panel.conditions) == 1 and _uids(panel) == expected


def test_cross_cell_pin_appends_two_and_conditions_after_existing_group(qtbot):
    panel = _panel(qtbot, [
        _case("a", weather="맑음", driver_sex="남성", vehicle_manufacturer="kia"),
        _case("b", weather="강수", driver_sex="여성", vehicle_manufacturer="kia"),
        _case("outside_union", weather="강수", driver_sex="남성", vehicle_manufacturer="kia"),
        _case("other_make", weather="강수", driver_sex="여성", vehicle_manufacturer="bmw"),
    ])
    panel.add_condition(StatsCondition("weather", "맑음"))
    panel.add_condition(StatsCondition("driver_sex", "여성"))
    _group(panel, 0, 1)
    axes = (StatsCondition("weather", "강수"), StatsCondition("vehicle_manufacturer", "kia"))
    panel.select_cases(("b",), axes, "강수 / kia")
    panel.pin_selection()
    assert isinstance(panel.conditions[0], AnyOf)
    assert [condition.field_key for condition in panel.conditions[1:]] == ["weather", "vehicle_manufacturer"]
    assert _uids(panel) == ["b"]


def test_clear_removes_groups_pins_and_chip_selection(qtbot):
    panel = _panel(qtbot, [_case("a", weather="맑음"), _case("b", weather="강수")])
    panel.add_condition(StatsCondition("weather", "맑음"))
    panel.add_condition(StatsCondition("driver_sex", "여성"))
    _group(panel, 0, 1)
    panel.select_cases(("a",), (StatsCondition("weather", "맑음"),), "맑음")
    panel.pin_selection()
    panel._selected_conditions = {1}
    panel.clear_conditions()
    assert panel.conditions == [] and not panel._selected_conditions
    assert _uids(panel) == ["a", "b"]


def test_export_metadata_distinguishes_parenthesized_or_and_outer_and(qtbot, monkeypatch, tmp_path):
    panel = _panel(qtbot, [_case("a", weather="맑음", driver_sex="여성", vehicle_manufacturer="kia")])
    panel.add_condition(StatsCondition("weather", "맑음"))
    panel.add_condition(StatsCondition("driver_sex", "여성"))
    _group(panel, 0, 1)
    panel.add_condition(StatsCondition("vehicle_manufacturer", "kia"))
    calls = []

    def capture(path, headers, rows, **kwargs):
        calls.append(kwargs)
        return path

    monkeypatch.setattr("investigator.statistics_export.export_statistics_csv", capture)
    panel.export_to(tmp_path / "grouped.csv", "csv")
    description = calls[0]["filters"]
    assert " OR " in description and " AND " in description
    assert "기상상황" in description and "운전자 성별" in description and "제작사" in description


def test_grouping_clears_stale_drill_selection(qtbot):
    panel = _panel(qtbot, [_case("a", weather="맑음"), _case("b", weather="강수")])
    panel.add_condition(StatsCondition("weather", "맑음"))
    panel.add_condition(StatsCondition("driver_sex", "여성"))
    panel.select_cases(("a",), (StatsCondition("weather", "맑음"),), "맑음")
    assert panel.drill_list.topLevelItemCount() == 1
    _group(panel, 0, 1)
    assert panel.drill_list.topLevelItemCount() == 0


def test_refresh_prunes_deleted_group_member_without_losing_valid_member(qtbot):
    panel = _panel(qtbot, [_case("a", weather="맑음"), _case("b", weather="강수")])
    panel.restore_state({"conditions": (AnyOf((StatsCondition("weather", "맑음"),
                                                StatsCondition("deleted_custom_field", "old"))),)})
    panel.set_data([_case("a", weather="맑음"), _case("b", weather="강수")], FormDefinition.default())
    assert _uids(panel) == ["a"]
    for item in panel.conditions:
        for condition in item.conditions if isinstance(item, AnyOf) else (item,):
            panel.engine.validate_condition(condition)


def test_form_type_change_removes_incompatible_number_condition(qtbot):
    panel = _panel(qtbot, [_case("a", vehicle_mileage="100")])
    panel.add_condition(StatsCondition("vehicle_mileage", 50, "ge"))
    definition = FormDefinition.default()
    record = next(record for record in definition.records() if record["key"] == "vehicle_mileage")
    record["kind"] = "text"
    definition.validate()
    panel.set_data([_case("a", vehicle_mileage="100")], definition)
    assert panel.conditions == [] and _uids(panel) == ["a"]


@pytest.mark.parametrize("width", [1180, 1660])
def test_target_count_is_on_chip_row_and_does_not_get_clipped(qtbot, width):
    panel = _panel(qtbot, [_case("a", weather="맑음")], width=width)
    panel.add_condition(StatsCondition("weather", "맑음"))
    qtbot.wait(10)
    target = panel.target_label
    chip_top = panel.chips_scroll.mapTo(panel, QPoint(0, 0)).y()
    target_center = target.mapTo(panel, target.rect().center()).y()
    assert chip_top <= target_center <= chip_top + panel.chips_scroll.height()
    assert target.width() >= target.sizeHint().width()
    assert target.mapTo(panel, target.rect().topRight()).x() < panel.width()


def test_condition_chips_use_compact_height_without_empty_vertical_padding(qtbot, qapp):
    apply_theme(qapp)
    panel = _panel(qtbot, [_case("a", weather="맑음")])
    panel.add_condition(StatsCondition("institution", values=("katri", "ajou")))
    qtbot.wait(10)
    body = _chip(panel, 0, QPushButton, "filterChip")
    chip = body.parentWidget()
    assert chip.height() == panel.group_conditions_button.height()
    assert panel.chips_scroll.height() == chip.height()
    assert panel.condition_hint.text() == "기본은 AND, 선택한 조건끼리 OR로 묶습니다."
    assert chip.width() <= body.fontMetrics().horizontalAdvance(body.text()) + 72
    for child in (body, chip.findChild(QToolButton, "filterChipRemove"),
                  chip.findChild(QCheckBox, "filterChipSelect")):
        assert chip.rect().contains(child.geometry())
    assert panel.chips_scroll.verticalScrollBar().maximum() == 0


def test_compact_chip_strip_adds_scrollbar_space_only_when_overflowing(qtbot, qapp):
    apply_theme(qapp)
    panel = _panel(qtbot, [_case("a")], width=1180)
    for index in range(8):
        panel.add_condition(StatsCondition("vehicle_model_name", values=(f"{index}_" + "x" * 90,)))
    bar = panel.chips_scroll.horizontalScrollBar()
    qtbot.waitUntil(lambda: bar.maximum() > 0)
    height = panel.group_conditions_button.height()
    assert panel.chips_scroll.height() == height + bar.sizeHint().height()
    remove = _chip(panel, 7, QToolButton, "filterChipRemove")
    assert remove.parentWidget().height() == height
    bar.setValue(bar.maximum())
    qtbot.wait(10)
    assert panel.chips_scroll.viewport().rect().contains(
        remove.mapTo(panel.chips_scroll.viewport(), remove.rect().center()),
    )
    qtbot.mouseClick(remove, Qt.MouseButton.LeftButton)
    assert len(panel.conditions) == 7
    panel.clear_conditions()
    qtbot.waitUntil(lambda: bar.maximum() == 0)
    assert panel.chips_scroll.height() == height
    assert panel.condition_hint.text() == "기본은 AND, 선택한 조건끼리 OR로 묶습니다."
