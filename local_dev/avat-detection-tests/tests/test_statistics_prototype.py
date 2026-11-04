"""The updated reference design wired to real investigation data, not mock records."""

from __future__ import annotations

import csv

import pytest
from PySide6.QtWidgets import QApplication, QLabel, QProgressBar, QTreeWidget

from investigator.form_definition import FormDefinition
from investigator.speed_estimation import SpeedPoint
from investigator.statistics import INTERVAL_KEY, TRIGGER_SPEED_KEY, StatsCondition
from investigator.theme import apply_theme
from investigator.widgets.statistics_charts import (
    BoxPlotWidget,
    StatisticsBarChart,
    StatisticsDonutChart,
    StatisticsScatterPlot,
    StatisticsStackedChart,
)
from tests.test_statistics_panel import accident, create_panel


def visible_chart(panel, kind):
    QApplication.processEvents()
    return next(chart for chart in panel.pages.currentWidget().findChildren(kind) if chart.isVisible())


def numeric_cases():
    return [
        accident('001', weather='맑음', vehicle_mileage='10000', collision_dist_value='4'),
        accident('002', weather='맑음', vehicle_mileage='20000', collision_dist_value='8'),
        accident('003', weather='강수', vehicle_mileage='30000', collision_dist_value='9'),
        accident('004', weather='강수', vehicle_mileage=None, collision_dist_value='12'),
    ]


def test_nominal_pie_is_clickable_and_includes_missing(qtbot):
    panel = create_panel(qtbot, [accident('001', weather='맑음'), accident('002', weather=None)])
    panel.open_item('weather')
    panel.item_mode.setCurrentIndex(panel.item_mode.findData('pie'))
    chart = visible_chart(panel, StatisticsDonutChart)
    assert [row['count'] for row in chart._rows] == [1, 1]
    chart.category_clicked.emit(1)
    assert panel._selection[0] == ('002',)
    assert panel._selection[1] == (StatsCondition('weather', operator='missing'),)
    panel.clear_selection()
    assert chart._selected is None
    assert panel.drill_list.topLevelItemCount() == 0


@pytest.mark.parametrize('key', ['driver_age', 'site_types', 'vehicle_mileage', 'remarks'])
def test_pie_not_offered_for_ordered_multi_numeric_or_free_text(qtbot, key):
    panel = create_panel(qtbot, [accident('001')])
    panel.open_item('weather')
    panel.item_mode.setCurrentIndex(panel.item_mode.findData('pie'))
    panel.open_item(key)
    assert panel.item_mode.findData('pie') == -1
    assert panel._item_mode == 'count'


def test_direction_and_cross_view_survive_state_roundtrip(qtbot):
    panel = create_panel(qtbot, numeric_cases())
    panel.open_item('vehicle_mileage')
    assert visible_chart(panel, StatisticsBarChart)._vertical
    panel.item_orientation.setCurrentIndex(panel.item_orientation.findData('h'))
    assert not visible_chart(panel, StatisticsBarChart)._vertical
    panel.view_tabs.setCurrentIndex(2)
    panel.cross_view.setCurrentIndex(panel.cross_view.findData('stack'))
    saved = panel.capture_state()
    panel.restore_state({})
    panel.set_data(numeric_cases(), FormDefinition.default())
    panel.restore_state(saved)
    panel.set_data(numeric_cases(), FormDefinition.default())
    assert panel._cross_view == 'stack'
    assert visible_chart(panel, StatisticsStackedChart)
    panel.view_tabs.setCurrentIndex(1)
    assert not visible_chart(panel, StatisticsBarChart)._vertical


def test_stacked_bar_drills_the_same_two_conditions_as_heatmap(qtbot):
    panel = create_panel(qtbot, [
        accident('001', weather='맑음', driver_sex='남성'),
        accident('002', weather='맑음', driver_sex='여성'),
        accident('003', weather='강수', driver_sex='여성'),
    ])
    panel._row_key, panel._column_key = 'weather', 'driver_sex'
    panel.view_tabs.setCurrentIndex(2)
    panel.cross_view.setCurrentIndex(panel.cross_view.findData('stack'))
    chart = visible_chart(panel, StatisticsStackedChart)
    cross = panel.engine.cross_table('weather', 'driver_sex', panel.filters())
    row = next(i for i, category in enumerate(cross.rows) if category.label == '맑음')
    column = next(i for i, category in enumerate(cross.columns) if category.label == '여성')
    chart.cell_clicked.emit(row, column)
    assert panel._selection[0] == ('002',)
    panel.pin_selection()
    assert panel.conditions == [StatsCondition('weather', '맑음'), StatsCondition('driver_sex', '여성')]


def test_multi_columns_do_not_normalize_accidents_into_false_100_percent(qtbot):
    panel = create_panel(qtbot, [accident('001', collision_targets=['승용', '화물'])])
    panel._row_key, panel._column_key = 'weather', 'collision_targets'
    panel._cross_view = 'stack'
    panel.view_tabs.setCurrentIndex(2)
    assert panel._cross_view == 'heat'
    assert not panel.cross_view._buttons.button(1).isEnabled()
    assert not panel.pages.currentWidget().findChildren(StatisticsStackedChart)


def test_same_axis_is_recoverable_and_clears_previous_export(qtbot):
    panel = create_panel(qtbot, numeric_cases())
    panel._row_key = panel._column_key = 'vehicle_mileage'
    panel.view_tabs.setCurrentIndex(2)
    assert panel._cross_kind == 'same'
    assert panel._export_rows == []
    assert any('서로 다른 두 항목' in text.text() for text in panel.pages.currentWidget().findChildren(QLabel))
    panel.cross_column.setCurrentIndex(panel.cross_column.findData('collision_dist_value'))
    assert panel._cross_kind == 'nn'
    assert len(panel._export_rows) == 3


def test_category_numeric_uses_raw_values_and_only_valid_group_cases(qtbot, tmp_path):
    panel = create_panel(qtbot, numeric_cases())
    panel._row_key, panel._column_key = 'weather', 'vehicle_mileage'
    panel.view_tabs.setCurrentIndex(2)
    assert panel._cross_kind == 'cn'
    chart = visible_chart(panel, BoxPlotWidget)
    report = panel.engine.numeric_groups('weather', 'vehicle_mileage', panel.filters())
    index = next(i for i, group in enumerate(report.groups) if group.label == '맑음')
    chart.group_clicked.emit(index)
    assert set(panel._selection[0]) == {'001', '002'}
    assert panel._selection[1] == ()
    assert not panel.pin_condition_button.isEnabled()
    path = panel.export_to(tmp_path / 'groups.csv', 'csv')
    with path.open(encoding='utf-8-sig', newline='') as stream:
        rows = list(csv.DictReader(stream))
    row = next(row for row in rows if row['기상상황'] == '맑음')
    assert row['중앙값'] == '15000.0'
    assert row['단위'] == 'km'
    assert row['유효 사고 수'] == '3'
    panel._swap_cross()
    assert panel._cross_kind == 'cn'
    assert next(row for row in panel._export_rows if row[0] == '맑음')[4] == 15000


def test_scatter_click_selects_exact_accident_and_exports_numeric_pairs(qtbot, tmp_path):
    panel = create_panel(qtbot, numeric_cases())
    panel._row_key, panel._column_key = 'vehicle_mileage', 'collision_dist_value'
    panel.view_tabs.setCurrentIndex(2)
    chart = visible_chart(panel, StatisticsScatterPlot)
    chart.point_clicked.emit('002')
    assert panel._selection[0] == ('002',)
    assert not panel.pin_condition_button.isEnabled()
    panel.clear_selection()
    assert panel._selection is None
    path = panel.export_to(tmp_path / 'pairs.csv', 'csv')
    with path.open(encoding='utf-8-sig', newline='') as stream:
        rows = list(csv.DictReader(stream))
    assert {row['사고 UID'] for row in rows} == {'001', '002', '003'}
    assert {row['유효 쌍'] for row in rows} == {'3'}
    assert any('추세선 기울기' in widget.text() and 'm / km' in widget.text() for widget in panel.pages.currentWidget().findChildren(QLabel))


def test_derived_speed_requires_measurements_and_real_fps(qtbot):
    case = accident('001', trigger_vehicle_speed='고속(60kph이상)')
    case.videos = ['measured.mp4']
    case._store_speed_points([SpeedPoint(0), SpeedPoint(30, 10)])
    case.set_trigger(15)
    case.set_collision(45)
    case.status = 'completed'
    panel = create_panel(qtbot, [case], fps={'measured.mp4': 30})
    panel._row_key, panel._column_key = TRIGGER_SPEED_KEY, INTERVAL_KEY
    panel.view_tabs.setCurrentIndex(2)
    assert panel._export_rows[0][2:4] == [36, 1]
    panel.set_data([case], FormDefinition.default(), {})
    assert panel._export_rows == []
    assert panel.engine.case_value(case, TRIGGER_SPEED_KEY) is None


def test_quality_rates_are_field_specific_and_exclude_optional_notes(qtbot):
    panel = create_panel(qtbot, [accident('001'), accident('002', status='draft', driver_sex=None)])
    panel.view_tabs.setCurrentIndex(4)
    report = panel.engine.quality(panel.filters())
    rate = next(rate for rate in report.field_rates if rate.field_key == 'driver_sex')
    assert rate.eligible == 2 and rate.answered == 1 and rate.rate == 50
    assert all(rate.field_key != 'remarks' for rate in report.field_rates)
    assert any('운전자 성별 입력률 50.0%' in bar.accessibleName() for bar in panel.pages.currentWidget().findChildren(QProgressBar))
    assert any('#D9822B' in bar.styleSheet() for bar in panel.pages.currentWidget().findChildren(QProgressBar))


def test_field_navigation_keeps_rate_column_compact(qtbot):
    panel = create_panel(qtbot, numeric_cases())
    panel.open_item('weather')
    QApplication.processEvents()
    tree = panel.findChild(QTreeWidget, 'statisticsFieldTree')
    assert tree.header().sectionSize(1) == 64
    assert tree.header().sectionSize(0) > 100


def test_deleted_custom_fields_in_saved_view_state_fall_back_safely(qtbot):
    panel = create_panel(qtbot, [accident('001')])
    panel.restore_state({'item': 'removed_custom', 'row': 'removed_custom', 'view': 2})
    panel.set_data([accident('001')], FormDefinition.default())
    assert panel._cross_kind in {'cc', 'cn', 'nn', 'same'}
    panel.view_tabs.setCurrentIndex(1)
    assert panel._item_key != 'removed_custom'


@pytest.mark.parametrize('width', [1180, 1660])
def test_new_chart_modes_fit_without_horizontal_panel_clipping(qtbot, qapp, width):
    apply_theme(qapp)
    panel = create_panel(qtbot, numeric_cases())
    panel.resize(width, 760)
    for x, y in [('weather', 'driver_sex'), ('weather', 'vehicle_mileage'), ('vehicle_mileage', 'collision_dist_value')]:
        panel._row_key, panel._column_key = x, y
        panel.view_tabs.setCurrentIndex(2)
        panel.refresh()
        if panel._cross_kind == 'cc':
            panel.cross_view.setCurrentIndex(panel.cross_view.findData('stack'))
        qtbot.wait(10)
        assert panel.width() == width
        assert panel.main_scroll.horizontalScrollBar().maximum() == 0
    panel.open_item('weather')
    panel.item_mode.setCurrentIndex(panel.item_mode.findData('pie'))
    qtbot.wait(10)
    assert panel.main_scroll.horizontalScrollBar().maximum() == 0
    assert panel.width() == width
