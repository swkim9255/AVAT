from __future__ import annotations

from copy import deepcopy

from PySide6.QtCore import QDate, Qt
from PySide6.QtWidgets import QDialog

from investigator.case_numbering import build_case_number_map
from investigator.form_definition import FormDefinition, new_field
from investigator.main_window import MainWindow
from investigator.models import InvestigationCase
from investigator.schema import FieldKind
from investigator.statistics import INTERVAL_KEY, SEVERITY_KEY, TRIGGER_SPEED_KEY, StatsCondition
from investigator.theme import apply_theme
from investigator.widgets.report_export_dialog import ReportConditionDialog, ReportExportDialog


def accident(uid, *, status="draft", **values):
    return InvestigationCase(
        uid=uid, status=status,
        values={"institution": "samsung", "accident_classification": uid, **values},
    )


def picker(qtbot, cases, selected=(), definition=None):
    dialog = ReportExportDialog(cases, selected, definition=definition)
    qtbot.addWidget(dialog)
    return dialog


def test_status_changes_automatically_replace_selection_with_all_matching_cases(qtbot):
    cases = [accident("1", status="completed"), accident("2"), accident("3", status="completed")]
    dialog = picker(qtbot, cases, ["2"])
    assert dialog.selected_uids() == ["2"]
    dialog.status.setCurrentIndex(dialog.status.findData("completed"))
    assert dialog.selected_uids() == ["1", "3"]
    assert dialog._items["2"].isHidden()
    assert "조건 일치 2건" in dialog.count_label.text()
    dialog.status.setCurrentIndex(dialog.status.findData("draft"))
    assert dialog.selected_uids() == ["2"]
    dialog.clear_conditions()
    assert dialog.selected_uids() == ["1", "2", "3"]
    assert all(not item.isHidden() for item in dialog._items.values())


def test_all_and_any_conditions_match_multichoice_membership_without_duplicates(qtbot):
    cases = [
        accident("1", collision_directions=["전면"]),
        accident("2", collision_directions=["전면", "후면"]),
        accident("3", collision_directions=["후면"]),
        accident("4", collision_directions=["좌측"]),
    ]
    dialog = picker(qtbot, cases, ["4"])
    dialog.add_condition(StatsCondition("collision_directions", "전면"))
    assert dialog.selected_uids() == ["1", "2"]
    dialog.add_condition(StatsCondition("collision_directions", "후면"))
    assert dialog.selected_uids() == ["2"]
    dialog.match_mode.setCurrentIndex(dialog.match_mode.findData("any"))
    assert dialog.selected_uids() == ["1", "2", "3"]
    dialog.add_condition(StatsCondition("collision_directions", "후면"))
    assert len(dialog.conditions) == 2


def test_status_and_inclusive_period_remain_required_in_or_mode(qtbot):
    cases = [
        accident("1", status="completed", incident_date="20260101", weather="맑음"),
        accident("2", status="completed", incident_date="20260131", weather="강수"),
        accident("3", status="completed", incident_date="20260201", weather="맑음"),
        accident("4", status="draft", incident_date="20260120", weather="강수"),
        accident("5", status="completed", weather="맑음"),
    ]
    dialog = picker(qtbot, cases)
    dialog.status.setCurrentIndex(dialog.status.findData("completed"))
    dialog.date_from.setDate(QDate(2026, 1, 1))
    dialog.date_to.setDate(QDate(2026, 1, 31))
    dialog.period.setChecked(True)
    dialog.add_condition(StatsCondition("weather", "맑음"))
    dialog.add_condition(StatsCondition("weather", "강수"))
    dialog.match_mode.setCurrentIndex(dialog.match_mode.findData("any"))
    assert dialog.selected_uids() == ["1", "2"]


def test_numeric_range_is_lower_inclusive_upper_exclusive_and_keeps_zero(qtbot):
    dialog = picker(qtbot, [
        accident("1", vehicle_mileage="0"), accident("2", vehicle_mileage="99"),
        accident("3", vehicle_mileage="100"), accident("4"),
    ])
    dialog.add_condition(StatsCondition("vehicle_mileage", operator="range", lower=0, upper=100))
    assert dialog.selected_uids() == ["1", "2"]
    assert "0 이상 ~ 100 미만" in dialog.condition_list.item(0).text()


def test_period_initial_dates_are_not_clamped_to_qt_default_minimum(qtbot):
    dialog = picker(qtbot, [accident("1", incident_date="00010101"),
                            accident("2", incident_date="20261020")])
    assert dialog.date_from.date() == QDate(1, 1, 1)
    dialog.period.setChecked(True)
    assert dialog.selected_uids() == ["1", "2"]


def test_missing_obstacle_condition_excludes_not_applicable_stale_values(qtbot):
    cases = [
        accident("1", obstacle_exists="유"),
        accident("2", obstacle_exists="무"),
        accident("3", obstacle_exists="유", obstacle_pedestrians=["어른"]),
        accident("4", obstacle_exists="무", obstacle_pedestrians=["어른"]),
    ]
    dialog = picker(qtbot, cases)
    dialog.add_condition(StatsCondition("obstacle_pedestrians", operator="missing"))
    assert dialog.selected_uids() == ["1"]
    dialog.clear_conditions()
    dialog.add_condition(StatsCondition("obstacle_pedestrians", "어른"))
    assert dialog.selected_uids() == ["3"]


def test_zero_matches_and_invalid_period_disable_export_without_losing_prior_checks(qtbot):
    dialog = picker(qtbot, [accident("1", incident_date="20260115", weather="맑음")])
    dialog.add_condition(StatsCondition("weather", "강설"))
    assert dialog.selected_uids() == []
    assert not dialog.export_button.isEnabled()
    dialog.accept()
    assert dialog.result() == QDialog.DialogCode.Rejected
    dialog.clear_conditions()
    dialog.date_from.setDate(QDate(2026, 2, 1))
    dialog.date_to.setDate(QDate(2026, 1, 1))
    dialog.period.setChecked(True)
    assert dialog._filter_error
    assert "조건 확인 필요" in dialog.count_label.text()
    assert not dialog.export_button.isEnabled()
    assert not dialog.select_all_button.isEnabled()
    dialog.accept()
    assert dialog.result() == QDialog.DialogCode.Rejected
    dialog.date_to.setDate(QDate(2026, 2, 28))
    assert dialog._filter_error is None
    assert dialog.selected_uids() == []
    dialog.period.setChecked(False)
    assert dialog.selected_uids() == ["1"]
    assert dialog.export_button.isEnabled()


def test_search_is_display_only_and_select_all_stays_within_conditions(qtbot):
    dialog = picker(qtbot, [
        accident("1", weather="맑음"), accident("2", weather="맑음"),
        accident("3", weather="강수"),
    ], ["3"])
    dialog.add_condition(StatsCondition("weather", "맑음"))
    dialog.search.setText("SAMSUNG-1")
    assert dialog.selected_uids() == ["1", "2"]
    assert dialog._items["2"].isHidden()
    assert "숨겨진 선택 사고 1건" in dialog.count_label.toolTip()
    dialog._items["1"].setCheckState(0, Qt.CheckState.Unchecked)
    assert dialog.selected_uids() == ["2"]
    dialog.select_all_button.click()
    assert dialog.selected_uids() == ["1", "2"]
    dialog.clear_button.click()
    assert dialog.selected_uids() == []


def test_removing_condition_reselects_new_matches_and_reset_preserves_case_data(qtbot):
    cases = [accident("1", weather="맑음", driver_sex="남성"),
             accident("2", weather="맑음", driver_sex="여성"), accident("3", weather="강수")]
    before = deepcopy([case.to_dict() for case in cases])
    dialog = picker(qtbot, cases)
    dialog.add_condition(StatsCondition("weather", "맑음"))
    dialog.add_condition(StatsCondition("driver_sex", "여성"))
    assert dialog.selected_uids() == ["2"]
    dialog.remove_condition_button.click()
    assert dialog.selected_uids() == ["1", "2"]
    dialog.clear_conditions()
    assert dialog.selected_uids() == ["1", "2", "3"]
    assert not dialog.conditions
    assert [case.to_dict() for case in cases] == before


def test_condition_dialog_uses_current_custom_values_and_excludes_fps_metrics(qtbot, monkeypatch):
    definition = FormDefinition.default()
    custom = new_field()
    custom.update(label="추가 분류", kind=FieldKind.MULTI.value,
                  choices=[{"id": "a", "label": "사용자 선택지", "value": "renamed_a", "csv_column": ""}])
    definition.tabs[-1]["fields"].append(custom)
    definition.validate()
    cases = [accident("1", **{custom["key"]: ["renamed_a"]}), accident("2")]
    dialog = picker(qtbot, cases, definition=definition)

    def choose(condition_dialog):
        assert condition_dialog.windowTitle() == "보고서 선택 조건 추가"
        assert condition_dialog.field.findData(INTERVAL_KEY) == -1
        assert condition_dialog.field.findData(TRIGGER_SPEED_KEY) == -1
        assert condition_dialog.field.findData(SEVERITY_KEY) >= 0
        condition_dialog.field.setCurrentIndex(condition_dialog.field.findData(custom["key"]))
        assert condition_dialog.value.currentText() == "사용자 선택지"
        condition_dialog._accept()
        return QDialog.DialogCode.Accepted

    monkeypatch.setattr(ReportConditionDialog, "exec", choose)
    dialog.add_condition_button.click()
    assert dialog.selected_uids() == ["1"]
    assert dialog.condition_list.item(0).text() == "추가 분류: 사용자 선택지"


def test_cancelling_condition_dialog_preserves_manual_selection(qtbot, monkeypatch):
    dialog = picker(qtbot, [accident("1"), accident("2")], ["2"])
    monkeypatch.setattr(ReportConditionDialog, "exec", lambda _: QDialog.DialogCode.Rejected)
    dialog.add_condition_button.click()
    assert dialog.conditions == []
    assert dialog.selected_uids() == ["2"]


def test_main_window_exports_only_condition_matches_using_project_form(qtbot, tmp_path, monkeypatch):
    window = MainWindow(tmp_path)
    qtbot.addWidget(window)
    for _ in range(3):
        window.create_case()
    cases = window.cases
    for case, weather in zip(cases, ["맑음", "강수", "맑음"], strict=True):
        case.values["weather"] = weather
    for case, number in zip(cases, ["2020011300003", "2020011300001", "2020011300002"], strict=True):
        case.values["accident_classification"] = number
    captured = []

    def choose(dialog):
        assert dialog.engine.definition is window.repository.form_definition
        dialog.add_condition(StatsCondition("weather", "맑음"))
        return QDialog.DialogCode.Accepted

    def export(snapshots, folder, *, form_definition, report_number_by_uid):
        assert report_number_by_uid == build_case_number_map(cases)
        assert report_number_by_uid[cases[0].uid] == "20200113_003"
        assert report_number_by_uid[cases[2].uid] == "20200113_002"
        captured.extend(case.uid for case in snapshots)
        return [folder / f"{case.uid}.docx" for case in snapshots]

    monkeypatch.setattr(ReportExportDialog, "exec", choose)
    monkeypatch.setattr("investigator.main_window.export_case_reports", export)
    monkeypatch.setattr("investigator.main_window.QFileDialog.getExistingDirectory",
                        lambda *_: str(tmp_path / "reports"))
    monkeypatch.setattr("investigator.main_window.QMessageBox.information", lambda *_: None)
    window.export_reports()
    assert captured == [cases[0].uid, cases[2].uid]
    assert all(case.status == "draft" for case in cases)


def test_many_accidents_are_all_selected_once_in_original_order(qtbot):
    cases = [accident(str(index), weather="맑음" if index % 2 else "강수") for index in range(1000)]
    dialog = picker(qtbot, cases)
    dialog.add_condition(StatsCondition("weather", "맑음"))
    assert dialog.selected_uids() == [str(index) for index in range(1, 1000, 2)]


def test_condition_controls_fit_compact_dialog(qtbot, qapp):
    apply_theme(qapp)
    dialog = picker(qtbot, [accident("1", weather="맑음")])
    dialog.add_condition(StatsCondition("weather", "맑음"))
    dialog.resize(640, 700)
    dialog.show()
    qtbot.wait(20)
    for control in [dialog.status, dialog.match_mode, dialog.date_from, dialog.date_to,
                    dialog.condition_list, dialog.tree, dialog.export_button]:
        assert control.width() > 0
        left = control.mapTo(dialog, control.rect().topLeft())
        right = control.mapTo(dialog, control.rect().bottomRight())
        assert 0 <= left.x() <= right.x() < dialog.width()
        assert 0 <= left.y() <= right.y() < dialog.height()
