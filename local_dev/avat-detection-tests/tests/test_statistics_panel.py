from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QDate, Qt
from PySide6.QtWidgets import QLabel, QPushButton, QTableWidget, QToolButton

from investigator.form_definition import FormDefinition, new_field
from investigator.models import InvestigationCase
from investigator.schema import FieldKind
from investigator.statistics import INTERVAL_KEY, SEVERITY_KEY, StatsCondition
from investigator.widgets.statistics_charts import StatisticsBarChart
from investigator.widgets.statistics_panel import StatisticsConditionDialog, StatisticsPanel
from tests.helpers import valid_values


def accident(uid: str, *, status="completed", **values) -> InvestigationCase:
    raw = valid_values()
    raw.update(accident_classification=uid, **values)
    return InvestigationCase(uid=uid, status=status, values=raw)


def create_panel(qtbot, cases, definition=None, fps=None):
    widget = StatisticsPanel()
    qtbot.addWidget(widget)
    widget.resize(1320, 850)
    widget.set_data(cases, definition or FormDefinition.default(), fps, Path("test_project"))
    widget.show()
    return widget


def current_table(panel):
    return next(widget for widget in panel.pages.currentWidget().findChildren(QTableWidget) if not widget.isHidden())


def chart_rows(panel):
    return [chart._rows for chart in panel.pages.currentWidget().findChildren(StatisticsBarChart) if not chart.isHidden()]


def test_actual_project_counts_status_date_filters_and_no_prototype_records(qtbot):
    panel = create_panel(qtbot, [
        accident("001", incident_date="20260110"),
        accident("002", incident_date="20260210"),
        accident("003", status="draft", incident_date="20260120"),
    ])
    assert panel.target_label.text() == "대상 2건 / 전체 3건"
    assert [widget.text() for widget in panel.pages.currentWidget().findChildren(QLabel) if widget.objectName() == "statValue"][0] == "2건"
    panel.status.setCurrentIndex(panel.status.findData("all"))
    assert panel.target_label.text() == "대상 3건 / 전체 3건"
    panel.date_from.setDate(QDate(2026, 1, 1))
    panel.date_to.setDate(QDate(2026, 1, 31))
    panel.period.setChecked(True)
    assert panel.target_label.text() == "대상 2건 / 전체 3건"
    panel.status.setCurrentIndex(panel.status.findData("draft"))
    assert panel.target_label.text() == "대상 1건 / 전체 3건"
    assert all(row[2] <= 1 for row in panel._export_rows)


def test_chart_click_drills_real_case_and_pin_creates_removable_condition(qtbot):
    panel = create_panel(qtbot, [
        accident("001", weather="맑음"), accident("002", weather="강수"),
        accident("003", status="draft", weather="맑음"),
    ])
    panel.open_item("weather")
    chart = next(widget for widget in panel.pages.currentWidget().findChildren(StatisticsBarChart) if not widget.isHidden())
    index = next(index for index, row in enumerate(chart._rows) if row["label"] == "맑음")
    qtbot.mouseClick(chart, Qt.MouseButton.LeftButton, pos=chart.bucket_rect(index).center().toPoint())
    assert panel.drill_count.text() == "해당 사고 1건"
    item = panel.drill_list.topLevelItem(0)
    assert item.data(0, Qt.ItemDataRole.UserRole) == "001"
    with qtbot.waitSignal(panel.case_activated) as signal:
        qtbot.mouseClick(panel.drill_list.viewport(), Qt.MouseButton.LeftButton,
                         pos=panel.drill_list.visualItemRect(item).center())
    assert signal.args == ["001", ""]
    qtbot.mouseClick(panel.pin_condition_button, Qt.MouseButton.LeftButton)
    assert panel.conditions == [StatsCondition("weather", "맑음")]
    assert panel.target_label.text() == "대상 1건 / 전체 3건"
    chip = next(widget for widget in panel.chips_widget.findChildren(QPushButton) if widget.objectName() == "filterChip" and not widget.isHidden())
    assert "맑음" in chip.text()
    qtbot.mouseClick(chip, Qt.MouseButton.LeftButton)
    assert panel.conditions == [StatsCondition("weather", "맑음")]
    panel._condition_popup.close()
    remove = next(widget for widget in panel.chips_widget.findChildren(QToolButton)
                  if widget.objectName() == "filterChipRemove" and not widget.isHidden())
    qtbot.mouseClick(remove, Qt.MouseButton.LeftButton)
    assert panel.conditions == []
    assert panel.target_label.text() == "대상 2건 / 전체 3건"


def test_multichoice_item_table_percentages_use_accidents_with_missing(qtbot):
    panel = create_panel(qtbot, [
        accident("001", site_types=["교차로", "횡단보도"]),
        accident("002", site_types=["교차로"]),
        accident("003", site_types=[]),
    ])
    panel.open_item("site_types")
    panel.item_mode.setCurrentIndex(panel.item_mode.findData("table"))
    widget = current_table(panel)
    rows = {widget.item(index, 0).text(): (widget.item(index, 1).text(), widget.item(index, 2).text()) for index in range(widget.rowCount())}
    assert rows == {"교차로": ("2", "66.7%"), "횡단보도": ("1", "33.3%"), "미입력": ("1", "33.3%")}
    assert sum(row[2] for row in panel._export_rows) > 100
    row = next(index for index in range(widget.rowCount()) if widget.item(index, 0).text() == "미입력")
    widget.cellClicked.emit(row, 0)
    assert panel.drill_list.topLevelItem(0).data(0, Qt.ItemDataRole.UserRole) == "003"
    panel.pin_selection()
    assert panel.conditions == [StatsCondition("site_types", operator="missing")]
    assert panel.target_label.text() == "대상 1건 / 전체 3건"


def test_cross_real_counts_row_percentage_and_cell_two_condition_pin(qtbot):
    panel = create_panel(qtbot, [
        accident("001", weather="맑음", driver_sex="남성"),
        accident("002", weather="맑음", driver_sex="여성"),
        accident("003", weather="맑음", driver_sex="여성"),
        accident("004", weather="강수", driver_sex="남성"),
    ])
    panel._row_key, panel._column_key = "weather", "driver_sex"
    panel.view_tabs.setCurrentIndex(2)
    widget = current_table(panel)
    row = next(index for index in range(widget.rowCount()) if widget.verticalHeaderItem(index).text() == "맑음")
    column = next(index for index in range(widget.columnCount()) if widget.horizontalHeaderItem(index).text() == "여성")
    assert widget.item(row, column).text() == "66.7%\n2건"
    assert widget.item(row, widget.columnCount() - 1).text() == "3"
    widget.cellClicked.emit(row, column)
    assert panel.drill_count.text() == "해당 사고 2건"
    assert {panel.drill_list.topLevelItem(index).data(0, Qt.ItemDataRole.UserRole) for index in range(2)} == {"002", "003"}
    panel.pin_selection()
    assert panel.conditions == [StatsCondition("weather", "맑음"), StatsCondition("driver_sex", "여성")]
    assert panel.target_label.text() == "대상 2건 / 전체 4건"


def test_quality_ignores_status_and_jumps_to_first_missing_or_error(qtbot):
    panel = create_panel(qtbot, [
        accident("001"),
        accident("002", status="draft", driver_sex=None),
        accident("003", status="draft", driver_exp="wrong"),
    ])
    assert panel.target_label.text() == "대상 1건 / 전체 3건"
    panel.view_tabs.setCurrentIndex(4)
    assert panel.target_label.text() == "대상 3건 / 전체 3건"
    widget = current_table(panel)
    assert widget.rowCount() == 2
    assert widget.item(0, 3).text() == "운전자 성별"
    assert widget.item(1, 3).text() == "운전 경력"
    with qtbot.waitSignal(panel.case_activated) as signal:
        widget.cellClicked.emit(0, 1)
    assert signal.args == ["002", "driver_sex"]
    assert "상태 필터를 제외" in panel.filter_description()


def test_custom_field_choices_use_edited_labels_and_can_pin(qtbot):
    definition = FormDefinition.default()
    record = new_field()
    record.update(label="추가 관찰", kind=FieldKind.MULTI.value)
    record["choices"] = [{"label": "선택 표시 A", "value": "value_a", "csv_column": ""}]
    definition.tabs.append({"key": "extra", "title": "추가 조사", "short_title": "추가", "fields": [record]})
    definition.validate()
    panel = create_panel(qtbot, [accident("001", **{record["key"]: ["value_a"]})], definition)
    panel.open_item(record["key"])
    assert chart_rows(panel)[0][0]["label"] == "선택 표시 A"
    panel.add_condition(StatsCondition(record["key"], "value_a"))
    assert panel.condition_text(panel.conditions[0]) == "추가 관찰: 선택 표시 A"
    assert panel.target_label.text() == "대상 1건 / 전체 1건"


def test_video_metrics_use_real_fps_and_request_metadata_on_tab(qtbot):
    first = accident("001")
    first.videos = ["first.mp4", "second.mp4"]
    first.markers_by_video = {
        "first.mp4": {"trigger_frame": 10, "collision_frames": [70]},
        "second.mp4": {"trigger_frame": 0, "collision_frames": []},
    }
    second = accident("002")
    second.videos = ["third.mp4"]
    second.markers_by_video = {"third.mp4": {"trigger_frame": 0, "collision_frames": [30]}}
    panel = create_panel(qtbot, [first, second], fps={"first.mp4": 60})
    with qtbot.waitSignal(panel.media_requested):
        panel.view_tabs.setCurrentIndex(3)
    values = [widget.text() for widget in panel.pages.currentWidget().findChildren(QLabel) if widget.objectName() == "statValue"]
    assert values == ["2 / 3개", "1.00초", "1건", "1개"]
    assert panel.engine.video(panel.filters()).missing_fps == 1


def test_saved_project_filter_state_can_be_restored_without_filter_leak(qtbot):
    panel = create_panel(qtbot, [accident("001", weather="맑음")])
    panel.status.setCurrentIndex(panel.status.findData("all"))
    panel.add_condition(StatsCondition("weather", "맑음"))
    panel.open_item("weather")
    state = panel.capture_state()
    panel.restore_state({})
    panel.set_data([accident("002", status="draft", weather="강수")], FormDefinition.default())
    assert panel.conditions == []
    assert panel.target_label.text() == "대상 0건 / 전체 1건"
    panel.restore_state(state)
    panel.set_data([accident("001", weather="맑음")], FormDefinition.default())
    assert panel.target_label.text() == "대상 1건 / 전체 1건"
    assert panel._item_key == "weather"
    assert panel.view_tabs.currentIndex() == 1


def test_filter_change_removes_drill_cases_that_are_no_longer_in_scope(qtbot):
    panel = create_panel(qtbot, [accident("001"), accident("002", status="draft")])
    panel.status.setCurrentIndex(panel.status.findData("all"))
    panel.select_cases(("002",), (), "선택한 임시저장 사고")
    assert panel.drill_list.topLevelItemCount() == 1
    panel.status.setCurrentIndex(panel.status.findData("completed"))
    assert panel.drill_list.topLevelItemCount() == 0


def test_inverted_date_range_is_handled_as_safe_empty_or_clamped_range(qtbot):
    panel = create_panel(qtbot, [accident("001", incident_date="20260110")])
    panel.period.setChecked(True)
    panel.date_from.setDate(QDate(2026, 12, 1))
    panel.date_to.setDate(QDate(2026, 1, 1))
    # The UI must normalize the range or show an empty, recoverable result.
    assert panel.date_from.date() <= panel.date_to.date() or "0건" in panel.target_label.text()


def test_numeric_virtual_condition_equals_entered_number(qtbot):
    case = accident("001")
    case.videos = ["a.mp4"]
    case.markers_by_video = {"a.mp4": {"trigger_frame": 0, "collision_frames": [30]}}
    panel = create_panel(qtbot, [case], fps={"a.mp4": 30})
    dialog = StatisticsConditionDialog(panel.engine, panel)
    qtbot.addWidget(dialog)
    dialog.field.setCurrentIndex(dialog.field.findData(INTERVAL_KEY))
    dialog.number.setText("1")
    dialog._accept()
    assert dialog.condition is not None
    panel.add_condition(dialog.condition)
    assert panel.target_label.text() == "대상 1건 / 전체 1건"


def test_severity_item_respects_injury_count_and_unknown_data(qtbot):
    panel = create_panel(qtbot, [
        accident("001", incident_damage_unknown="확인 가능", incident_minor_injury="2"),
        accident("002", incident_damage_unknown="확인불가", incident_severe_injury="9"),
    ])
    panel.open_item(SEVERITY_KEY)
    assert [(row["label"], row["count"]) for row in chart_rows(panel)[0]] == [("경상", 1), ("확인불가", 1)]


def test_cross_oversized_text_axes_show_recoverable_warning(qtbot):
    panel = create_panel(qtbot, [accident(str(i), remarks=f"unique note {i}") for i in range(101)])
    panel._row_key, panel._column_key = "remarks", "weather"
    panel.view_tabs.setCurrentIndex(2)
    assert any("교차" in widget.text() and "100" in widget.text()
               for widget in panel.pages.currentWidget().findChildren(QLabel))
    assert not panel._export_rows
    panel.cross_row.setCurrentIndex(panel.cross_row.findData("driver_sex"))
    assert current_table(panel).rowCount() == 1
    assert panel._export_rows


def test_pdf_from_quality_is_explicit_overview_with_matching_status_and_denominators(qtbot, monkeypatch, tmp_path):
    panel = create_panel(qtbot, [accident("001"), accident("002", status="draft")])
    panel.view_tabs.setCurrentIndex(4)
    captured = {}

    def export(path, **kwargs):
        captured.update(kwargs)
        return path

    monkeypatch.setattr("investigator.statistics_export.export_overview_pdf", export)
    panel.export_to(tmp_path / "summary.pdf", "pdf")
    assert captured["title"].endswith("개요 보고서")
    assert "상태 필터를 제외" not in captured["filters"]
    assert "완료만" in captured["filters"]
    assert captured["summary"]["selected"] == 1
    assert all(headers[-1] == "분모" and all(row[-1] == 1 for row in rows)
               for _, headers, rows in captured["tables"])


def test_item_csv_identifies_field_and_exports_full_distribution(qtbot, tmp_path):
    import csv

    panel = create_panel(qtbot, [accident("001", weather="맑음"), accident("002", weather="강수")])
    panel.open_item("weather")
    path = panel.export_to(tmp_path / "statistics.csv", "csv")
    with path.open(encoding="utf-8-sig", newline="") as stream:
        rows = list(csv.DictReader(stream))
    assert len(rows) == 2
    assert {row["항목"] for row in rows} == {"기상상황"}
    assert {row["값"] for row in rows} == {"맑음", "강수"}
    assert {row["분모"] for row in rows} == {"2"}


def test_clear_selection_resets_both_drill_and_chart_highlight(qtbot):
    panel = create_panel(qtbot, [accident("001")])
    panel.open_item("weather")
    chart = panel.pages.currentWidget().findChild(StatisticsBarChart)
    chart.bucket_clicked.emit(0)
    assert chart._selected == 0
    assert panel.drill_list.topLevelItemCount() == 1
    panel.clear_selection()
    assert chart._selected is None
    assert panel.drill_list.topLevelItemCount() == 0
