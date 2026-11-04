"""Mirrored real-case cohorts, common scales and pointer/drilldown integration."""

from __future__ import annotations

import csv

import pytest
from PySide6.QtCore import Qt
from PySide6.QtTest import QSignalSpy
from PySide6.QtWidgets import QApplication, QLabel, QToolTip

from investigator.butterfly import build_butterfly, butterfly_groups
from investigator.form_definition import FormDefinition
from investigator.statistics import (
    CrossTableLimitError,
    StatisticsEngine,
    StatsCondition,
    StatsFilter,
)
from investigator.theme import apply_theme
from investigator.widgets.statistics_charts import (
    StatisticsBarChart,
    StatisticsButterflyChart,
    StatisticsStackedChart,
)
from tests.test_statistics_panel import accident, create_panel


def cases():
    return [
        accident("001", driver_sex="남성", weather="맑음", driver_age="20-24세"),
        accident("002", driver_sex="남성", weather="맑음", driver_age="25-29세"),
        accident("003", driver_sex="남성", weather="강수", driver_age="25-29세"),
        accident("004", driver_sex="여성", weather="맑음", driver_age="30-34세"),
        accident("005", driver_sex="여성", weather=None, driver_age=None),
        accident("006", driver_sex=None, weather="맑음"),
        accident("007", driver_sex="잘못된 값", weather="맑음"),
        accident("008", status="draft", driver_sex="여성", weather="강수"),
    ]


def report_for(data=None, **kwargs):
    engine = StatisticsEngine(data or cases(), FormDefinition.default())
    return build_butterfly(engine, "weather", "driver_sex", StatsFilter(),
                           left_value="남성", right_value="여성", **kwargs)


def open_butterfly(qtbot, data=None, axis="weather", group="driver_sex"):
    panel = create_panel(qtbot, data or cases())
    panel._row_key, panel._column_key, panel._cross_view = axis, group, "butterfly"
    panel.view_tabs.setCurrentIndex(2)
    QApplication.processEvents()
    chart = next(w for w in panel.pages.currentWidget().findChildren(StatisticsButterflyChart) if w.isVisible())
    return panel, chart


def test_actual_cohorts_exclude_missing_invalid_and_unselected_status_without_mutating():
    data = cases()
    before = [case.to_dict() for case in data]
    report = report_for(data)
    assert (report.total, report.left_total, report.right_total, report.excluded) == (7, 3, 2, 2)
    rows = {row.label: row for row in report.rows}
    assert rows["맑음"].left_uids == ("001", "002")
    assert rows["맑음"].right_uids == ("004",)
    assert rows["미입력"].right_uids == ("005",)
    assert any("20건 미만" in warning for warning in report.warnings)
    assert [case.to_dict() for case in data] == before


def test_one_vs_rest_is_membership_complement_not_union_of_other_multi_values():
    data = [
        accident("001", collision_targets=["vehicle.passenger", "vehicle.truck"]),
        accident("002", collision_targets=["vehicle.passenger"]),
        accident("003", collision_targets=["vehicle.truck", "vehicle.bicycle"]),
        accident("004", collision_targets=[]),
    ]
    engine = StatisticsEngine(data, FormDefinition.default())
    report = build_butterfly(engine, "weather", "collision_targets", StatsFilter(),
                             left_value="vehicle.passenger", mode="rest")
    assert (report.left_total, report.right_total, report.excluded, report.overlap) == (2, 1, 1, 0)
    assert {uid for row in report.rows for uid in row.right_uids} == {"003"}
    two = build_butterfly(engine, "weather", "collision_targets", StatsFilter(),
                          left_value="vehicle.passenger", right_value="vehicle.truck")
    assert two.overlap == 1
    assert any("좌우 두 그룹" in warning for warning in two.warnings)


def test_global_conditions_and_status_filter_apply_to_both_groups():
    engine = StatisticsEngine(cases(), FormDefinition.default())
    filters = StatsFilter("all", conditions=(StatsCondition("weather", "강수"),))
    report = build_butterfly(engine, "driver_age", "driver_sex", filters,
                             left_value="남성", right_value="여성")
    assert (report.total, report.left_total, report.right_total) == (2, 1, 1)


def test_ordered_axis_keeps_configured_order_and_empty_five_year_intervals():
    engine = StatisticsEngine(cases(), FormDefinition.default())
    report = build_butterfly(engine, "driver_age", "driver_sex", StatsFilter(),
                             left_value="남성", right_value="여성")
    choices = engine.field("driver_age").choices
    assert [row.label for row in report.rows[:len(choices)]] == [choice.label for choice in choices]
    assert len(choices) == 16
    assert report.rows[-1].label == "미입력"
    assert report.rows[-1].right_uids == ("005",)


def test_numeric_axis_uses_shared_bins_not_independently_scaled_group_intervals():
    data = [accident("001", driver_sex="남성", vehicle_mileage="10"),
            accident("002", driver_sex="여성", vehicle_mileage="100000"),
            accident("003", driver_sex="여성", vehicle_mileage="bad")]
    engine = StatisticsEngine(data, FormDefinition.default())
    report = build_butterfly(engine, "vehicle_mileage", "driver_sex", StatsFilter(),
                             left_value="남성", right_value="여성")
    buckets = engine.distribution("vehicle_mileage").buckets
    assert [row.label for row in report.rows] == [bucket.label for bucket in buckets]
    assert [set(row.left_uids) | set(row.right_uids) for row in report.rows] == [set(bucket.case_uids) for bucket in buckets]
    assert sum(row.left_count + row.right_count for row in report.rows) == 3


def test_configured_zero_groups_are_available_and_invalid_requests_are_recoverable():
    engine = StatisticsEngine([accident("001", driver_sex="남성")], FormDefinition.default())
    groups = butterfly_groups(engine, "driver_sex", StatsFilter())
    assert [(group.label, group.count) for group in groups] == [("남성", 1), ("여성", 0), ("불명", 0)]
    for axis, group, kwargs in [
        ("weather", "driver_sex", dict(left_value="남성", right_value="남성")),
        ("weather", "vehicle_mileage", dict(left_value=1, right_value=2)),
        ("weather", "weather", dict(left_value="맑음", right_value="강수")),
        ("weather", "driver_sex", dict(left_value="남성", mode="bad")),
    ]:
        with pytest.raises(ValueError):
            build_butterfly(engine, axis, group, StatsFilter(), **kwargs)


def test_high_cardinality_axis_is_not_silently_truncated():
    engine = StatisticsEngine([accident(str(i), remarks=f"note{i}") for i in range(101)], FormDefinition.default())
    with pytest.raises(CrossTableLimitError):
        build_butterfly(engine, "remarks", "driver_sex", StatsFilter(), left_value="남성", right_value="여성")


def test_mirrored_scale_ratio_counts_and_immediate_visible_tooltip(qtbot):
    chart = StatisticsButterflyChart()
    qtbot.addWidget(chart)
    chart.resize(700, 220)
    chart.set_data([dict(label="맑음", left_count=2, right_count=1)],
                   left_label="남성", right_label="여성", left_total=4, right_total=2)
    chart.show()
    assert chart.bar_rect(0, 0).width() == chart.bar_rect(0, 1).width()
    point = chart.bar_rect(0, 0).center().toPoint()
    qtbot.mouseMove(chart, point)
    assert "남성 2건" in chart.toolTip()
    assert "50.0%" in QToolTip.text()
    assert "분모 4건" in QToolTip.text()
    assert QToolTip.isVisible()
    spy = QSignalSpy(chart.bucket_clicked)
    qtbot.mouseClick(chart, Qt.MouseButton.LeftButton, pos=point)
    assert spy.at(0) == [0, 0]
    qtbot.mouseClick(chart, Qt.MouseButton.LeftButton, pos=chart.cell_rect(0, 2).center().toPoint())
    assert spy.at(1) == [0, 2]
    chart.set_data(chart._rows, left_label="남성", right_label="여성", left_total=4, right_total=2, unit="count")
    assert chart.bar_rect(0, 0).width() == chart.bar_rect(0, 1).width() * 2
    qtbot.keyClick(chart, Qt.Key.Key_End)
    qtbot.keyClick(chart, Qt.Key.Key_Return)
    assert spy.at(2) == [0, 1]
    chart.set_selection(None)
    assert chart._selected is None


@pytest.mark.parametrize("kind", [StatisticsBarChart, StatisticsStackedChart])
def test_existing_bar_types_show_actual_counts_without_native_tooltip_delay(qtbot, kind):
    chart = kind()
    qtbot.addWidget(chart)
    chart.resize(600, 250)
    if kind is StatisticsBarChart:
        chart.set_data([dict(label="맑음", count=3)], 5)
        point = chart.bucket_rect(0).center().toPoint()
    else:
        chart.set_data([dict(label="맑음", counts=[3, 2], total=5)], ["남성", "여성"])
        point = chart.cell_rect(0, 0).center().toPoint()
    chart.show()
    qtbot.mouseMove(chart, point)
    assert "3건" in QToolTip.text()
    assert QToolTip.isVisible()


def test_panel_exact_side_and_combined_case_drilldowns_and_export(qtbot, tmp_path):
    panel, chart = open_butterfly(qtbot)
    row = next(i for i, row in enumerate(chart._rows) if row["label"] == "맑음")
    assert chart._totals == (3, 2)
    chart.bucket_clicked.emit(row, 0)
    assert panel._selection[0] == ("001", "002")
    assert panel.pin_condition_button.isEnabled()
    chart.bucket_clicked.emit(row, 1)
    assert panel._selection[0] == ("004",)
    chart.bucket_clicked.emit(row, 2)
    assert panel._selection[0] == ("001", "002", "004")
    assert "66.7%" in panel.butterfly_readout.text()
    assert "50.0%" in panel.butterfly_readout.text()
    assert "+16.7%p" in panel.butterfly_readout.text()
    path = tmp_path / "butterfly.csv"
    panel.export_to(path, "csv")
    with path.open(encoding="utf-8-sig", newline="") as stream:
        exported = list(csv.reader(stream))
    assert any("왼쪽 분모" in line for line in exported)
    record = next(line for line in exported if len(line) > 1 and line[1] == "맑음")
    assert record[4:7] == ["2", "3", "66.67"]
    panel.clear_selection()
    assert chart._selected is None
    assert panel.drill_list.topLevelItemCount() == 0


def test_panel_rest_mode_distinct_groups_unit_and_state_roundtrip(qtbot):
    panel, chart = open_butterfly(qtbot)
    panel._butterfly_left_values = ("여성",)
    panel.refresh()
    assert "여성" not in panel._butterfly_right_values
    panel.butterfly_mode.setCurrentIndex(panel.butterfly_mode.findData("rest"))
    assert not panel.butterfly_right.isEnabled()
    panel.butterfly_unit.setCurrentIndex(panel.butterfly_unit.findData("count"))
    saved = panel.capture_state()
    panel.restore_state({})
    panel.set_data(cases(), FormDefinition.default())
    panel.restore_state(saved)
    panel.set_data(cases(), FormDefinition.default())
    assert panel._cross_view == "butterfly"
    assert panel._butterfly_left == "여성"
    assert panel._butterfly_mode == "rest"
    assert panel._butterfly_unit == "count"
    panel.butterfly_mode.setCurrentIndex(panel.butterfly_mode.findData("two"))
    assert panel.butterfly_right.isEnabled()
    assert not set(panel._butterfly_left_values) & set(panel._butterfly_right_values)


@pytest.mark.parametrize("axis,group", [("vehicle_mileage", "driver_sex"), ("driver_sex", "vehicle_mileage"), ("weather", "collision_targets")])
def test_numeric_and_multiselect_butterfly_view_is_not_forced_to_heat(qtbot, axis, group):
    panel, chart = open_butterfly(qtbot, axis=axis, group=group)
    assert panel._cross_view == "butterfly"
    assert chart._rows
    assert panel.cross_mode.isHidden()


def test_zero_group_n_and_missing_axis_do_not_crash_or_claim_a_comparison(qtbot):
    panel, chart = open_butterfly(qtbot, [accident("001", weather=None, driver_sex="남성")])
    assert chart._totals == (1, 0)
    assert chart._rows[0]["label"] == "미입력"
    assert chart.bar_rect(0, 1).width() == 0
    assert any("0건이어서" in w.text() for w in panel.pages.currentWidget().findChildren(QLabel))
    assert not chart.grab().isNull()


@pytest.mark.parametrize("width", [1180, 1660])
def test_butterfly_controls_and_chart_fit_sidebar_with_no_horizontal_clipping(qtbot, qapp, width):
    apply_theme(qapp)
    panel, chart = open_butterfly(qtbot, axis="driver_age")
    panel.resize(width, 760)
    qtbot.wait(20)
    assert panel.width() == width
    assert panel.main_scroll.horizontalScrollBar().maximum() == 0
    assert chart.cell_rect(0, 1).right() <= chart.width()
    assert not panel.grab().isNull()


@pytest.mark.parametrize("width", [230, 400, 800])
def test_chart_wraps_long_axis_and_group_labels_at_narrow_widths(qtbot, width):
    chart = StatisticsButterflyChart()
    qtbot.addWidget(chart)
    chart.resize(width, 150)
    chart.set_data([dict(label="도로시설물과 건물 조경 중 아주 긴 범주 이름", left_count=0, right_count=1)],
                   left_label="아주 긴 좌측 비교 그룹 이름", right_label="다른 모든 그룹을 합한 나머지 사고",
                   left_total=0, right_total=1, axis_label="가운데 축 항목 이름")
    chart.show()
    assert chart.row_rect(0).bottom() < chart.height()
    assert chart.cell_rect(0, 0).right() < chart.cell_rect(0, 2).left()
    assert chart.cell_rect(0, 2).right() < chart.cell_rect(0, 1).left()
    assert not chart.grab().isNull()
