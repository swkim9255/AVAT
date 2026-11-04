"""Responsive statistics page checks with generated, test-only cases."""

from __future__ import annotations

import pytest
from PySide6.QtWidgets import QLabel

from investigator.form_definition import FormDefinition
from investigator.models import InvestigationCase
from investigator.theme import apply_theme
from investigator.widgets.statistics_charts import StatisticsBarChart
from investigator.widgets.statistics_panel import StatisticsPanel
from tests.helpers import valid_values
from tests.test_statistics_panel import accident, create_panel


@pytest.mark.parametrize("width,height", [(1660, 850), (1180, 650)])
def test_statistics_pages_fit_supported_window_sizes(qtbot, qapp, width, height):
    apply_theme(qapp)
    definition = FormDefinition.default()
    cases = []
    for index in range(24):
        values = valid_values()
        values.update(
            accident_classification=f"{index:013d}",
            driver_age="20-24세" if index % 2 else "95-99세",
            weather="맑음" if index % 3 else "강수",
            collision_targets=["승용", "화물"],
            incident_date="20260101",
        )
        cases.append(InvestigationCase(uid=f"layout-{index}", values=values))
    panel = StatisticsPanel()
    qtbot.addWidget(panel)
    panel.set_data(cases, definition)
    panel.status.setCurrentIndex(panel.status.findData("all"))
    panel.resize(width, height)
    panel.show()
    for index in range(panel.view_tabs.count()):
        panel.view_tabs.setCurrentIndex(index)
        qtbot.wait(10)
        assert panel.width() == width
        assert panel.main_scroll.horizontalScrollBar().maximum() == 0
        assert panel.export_button.mapTo(panel, panel.export_button.rect().topRight()).x() < width
        assert panel.target_label.width() >= panel.target_label.sizeHint().width()
        for text in ("필터", "~"):
            labels = [widget for widget in panel.findChildren(QLabel) if widget.text() == text]
            assert labels and labels[0].width() >= labels[0].sizeHint().width()
        for chart in panel.findChildren(StatisticsBarChart):
            if chart.isVisible():
                assert chart.geometry().left() >= 0
                assert chart.geometry().right() < chart.parentWidget().width()
                assert chart.geometry().bottom() < chart.parentWidget().height()
        if index == 4:
            assert not panel.drill.isVisible()


def test_switching_from_tall_overview_does_not_stretch_short_cross_report(qtbot, qapp):
    apply_theme(qapp)
    panel = StatisticsPanel()
    qtbot.addWidget(panel)
    panel.set_data([InvestigationCase(values=valid_values(), status="completed")], FormDefinition.default())
    panel.resize(1320, 750)
    panel.show()
    qtbot.wait(10)
    overview_height = panel.pages.height()
    panel.view_tabs.setCurrentIndex(2)
    qtbot.wait(10)
    assert panel.pages.height() < overview_height
    assert panel.pages.height() <= panel.pages.currentWidget().sizeHint().height() + 5


def assert_cross_units_below_view(panel):
    from PySide6.QtCore import QPoint

    view = panel.cross_view
    units = panel.cross_mode
    view_position = view.mapTo(panel, QPoint(0, 0))
    units_position = units.mapTo(panel, QPoint(0, 0))
    assert units.isVisible()
    assert units_position.y() >= view_position.y() + view.height()
    assert units_position.x() + units.width() == view_position.x() + view.width()


@pytest.mark.parametrize("width", [1180, 1660])
def test_heatmap_units_are_right_aligned_below_view_and_preserve_selection(qtbot, qapp, width):
    apply_theme(qapp)
    panel = create_panel(qtbot, [
        accident("001", weather="맑음", driver_sex="남성"),
        accident("002", weather="강수", driver_sex="여성"),
    ])
    panel.resize(width, 850)
    panel._row_key, panel._column_key = "weather", "driver_sex"
    panel.view_tabs.setCurrentIndex(2)
    qtbot.wait(10)
    assert_cross_units_below_view(panel)

    panel.cross_mode.setCurrentIndex(panel.cross_mode.findData("count"))
    qtbot.wait(10)
    assert panel._cross_mode == "count"
    assert_cross_units_below_view(panel)

    panel.cross_view.setCurrentIndex(panel.cross_view.findData("stack"))
    qtbot.wait(10)
    assert panel.cross_mode.isHidden()
    panel.cross_view.setCurrentIndex(panel.cross_view.findData("heat"))
    qtbot.wait(10)
    assert panel.cross_mode.currentData() == "count"
    assert_cross_units_below_view(panel)


def test_multi_column_heatmap_fallback_shows_units_on_lower_row(qtbot, qapp):
    apply_theme(qapp)
    panel = create_panel(qtbot, [accident("001", collision_targets=["승용", "화물"])])
    panel._row_key, panel._column_key = "weather", "collision_targets"
    panel._cross_view = "stack"
    panel.view_tabs.setCurrentIndex(2)
    qtbot.wait(10)
    assert panel._cross_view == "heat"
    assert_cross_units_below_view(panel)
