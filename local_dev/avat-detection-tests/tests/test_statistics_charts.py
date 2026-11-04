import math

import pytest
from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QColor
from PySide6.QtTest import QSignalSpy
from PySide6.QtWidgets import QScrollArea, QTableWidget, QTableWidgetItem

from investigator.widgets.statistics_charts import (
    BoxPlotWidget,
    StatisticsBarChart,
    StatisticsDonutChart,
    StatisticsHeatmapDelegate,
    StatisticsScatterPlot,
    StatisticsStackedChart,
)


def test_bar_chart_click_keyboard_and_hover_use_supplied_bucket_indices(qtbot):
    chart = StatisticsBarChart()
    qtbot.addWidget(chart)
    chart.resize(500, 150)
    chart.set_data(
        [
            {"label": "승용", "count": 3, "uids": ["a", "b", "c"]},
            {"label": "미입력", "count": 1, "missing": True},
        ],
        4,
    )
    chart.show()
    spy = QSignalSpy(chart.bucket_clicked)
    point = chart.bucket_rect(1).center().toPoint()
    qtbot.mouseMove(chart, point)
    assert chart.toolTip() == "미입력 · 1건 · 25.0%"
    qtbot.mouseClick(chart, Qt.MouseButton.LeftButton, pos=point)
    assert spy.at(0) == [1]
    assert chart._selected == 1
    qtbot.keyClick(chart, Qt.Key.Key_Home)
    qtbot.keyClick(chart, Qt.Key.Key_Return)
    assert spy.at(1) == [0]
    qtbot.keyClick(chart, Qt.Key.Key_End)
    qtbot.keyClick(chart, Qt.Key.Key_Space)
    assert spy.at(2) == [1]
    chart.set_selection(None)
    assert chart._selected is None
    assert not chart.grab().isNull()


def test_long_wrapping_category_chart_stays_scrollable_in_narrow_panel(qtbot):
    scroll = QScrollArea()
    qtbot.addWidget(scroll)
    scroll.setWidgetResizable(True)
    chart = StatisticsBarChart()
    chart.set_data(
        [
            {"label": f"아주 긴 도로시설물 분류와 충돌 대상 이름 {index}", "count": index + 1}
            for index in range(20)
        ],
        20,
        mode="percent",
    )
    scroll.setWidget(chart)
    scroll.resize(300, 260)
    scroll.show()
    qtbot.wait(20)
    assert scroll.horizontalScrollBar().maximum() == 0
    assert scroll.verticalScrollBar().maximum() > 0
    assert chart.bucket_rect(0).height() > 34
    assert chart.bucket_rect(19).bottom() <= chart.height()
    scroll.verticalScrollBar().setValue(scroll.verticalScrollBar().maximum())
    assert chart._value(chart._rows[-1]) == "100.0%"
    assert not chart.grab().isNull()


def test_vertical_zero_data_percent_and_empty_chart_are_safe(qtbot):
    chart = StatisticsBarChart()
    qtbot.addWidget(chart)
    chart.resize(240, 300)
    chart.set_data(
        [
            {"label": "0–0.5초", "count": 0},
            {"label": "0.5–1초", "count": 0},
        ],
        0,
        mode="percent",
        vertical=True,
    )
    chart.show()
    assert chart._value(chart._rows[0]) == "0.0%"
    assert chart.bucket_rect(0).right() <= chart.bucket_rect(1).left()
    assert not chart.grab().isNull()
    chart.set_data([], 0, vertical=True)
    qtbot.mouseMove(chart, chart.rect().center())
    qtbot.keyClick(chart, Qt.Key.Key_Return)
    assert chart._index_at(chart.rect().center()) is None
    assert "표시할 데이터가 없습니다" in chart.accessibleDescription()
    assert not chart.grab().isNull()


def test_box_plot_real_summary_and_equal_values_render(qtbot):
    chart = BoxPlotWidget()
    qtbot.addWidget(chart)
    chart.resize(320, 160)
    chart.set_data(
        [
            {
                "label": "충돌 대상이 긴 그룹 이름",
                "min": 0,
                "q1": 0.5,
                "median": 1,
                "q3": 1.5,
                "max": 3,
                "n": 4,
            },
            {"label": "모두 같은 시간", "min": 0, "q1": 0, "median": 0, "q3": 0, "max": 0, "n": 1},
        ]
    )
    chart.show()
    qtbot.mouseMove(chart, chart.rect().topLeft() + chart.rect().center() / 4)
    assert "중앙값 1.00초" in chart.accessibleDescription()
    assert "n=4" in chart.accessibleDescription()
    assert not chart.grab().isNull()
    chart.set_data([])
    assert "유효한 영상 지표가 없습니다" in chart.accessibleDescription()
    assert not chart.grab().isNull()


@pytest.mark.parametrize(
    "values",
    [
        [0, 1, 0.5, 2, 3],
        [0, 1, 2, 3, float("inf")],
        [-1, 0, 1, 2, 3],
    ],
)
def test_box_plot_rejects_invalid_summary(qtbot, values):
    chart = BoxPlotWidget()
    qtbot.addWidget(chart)
    with pytest.raises(ValueError):
        chart.set_data(
            [
                {
                    "label": "잘못된 요약",
                    "n": 2,
                    **dict(zip(("min", "q1", "median", "q3", "max"), values, strict=True)),
                }
            ]
        )


def test_heatmap_colors_survive_global_item_styles_and_cells_remain_clickable(qtbot):
    table = QTableWidget(1, 2)
    qtbot.addWidget(table)
    table.setStyleSheet(
        "QTableView::item { background: white; color: white; }"
        "QTableView::item:selected { background: white; color: white; }"
    )
    table.horizontalHeader().hide()
    table.verticalHeader().hide()
    table.setShowGrid(False)
    table.setItemDelegate(StatisticsHeatmapDelegate(table))
    table.setRowHeight(0, 64)
    for column, alpha in enumerate((0.05, 0.65)):
        item = QTableWidgetItem(f"{column * 60}%\n{column * 6}건")
        color = QColor("#1F5FBF")
        color.setAlphaF(alpha)
        item.setBackground(color)
        table.setItem(0, column, item)
        table.setColumnWidth(column, 120)
    table.resize(270, 90)
    table.show()
    qtbot.wait(10)
    image = table.viewport().grab().toImage()
    ratio = image.devicePixelRatio()
    colors = []
    for column in range(2):
        rect = table.visualItemRect(table.item(0, column))
        colors.append(
            image.pixelColor(int((rect.left() + 10) * ratio), int((rect.top() + 10) * ratio))
        )
    assert colors[1].blue() - colors[1].red() > 50
    assert colors[1].blue() - colors[1].red() > colors[0].blue() - colors[0].red()
    spy = QSignalSpy(table.cellClicked)
    qtbot.mouseClick(
        table.viewport(),
        Qt.MouseButton.LeftButton,
        pos=table.visualItemRect(table.item(0, 1)).center(),
    )
    assert spy.at(0) == [0, 1]
    assert table.item(0, 1).isSelected()
    # The selected cell retains its heat intensity instead of reverting to QSS white.
    image = table.viewport().grab().toImage()
    rect = table.visualItemRect(table.item(0, 1))
    selected = image.pixelColor(int((rect.left() + 10) * ratio), int((rect.top() + 10) * ratio))
    assert selected.blue() - selected.red() > 50


def test_donut_slice_legend_keyboard_and_hole_interactions(qtbot):
    chart = StatisticsDonutChart()
    qtbot.addWidget(chart)
    chart.resize(650, 280)
    chart.set_data(
        [
            {"label": "승용", "count": 3},
            {"label": "미입력", "count": 1, "missing": True},
        ],
        denominator=4,
    )
    chart.show()
    spy = QSignalSpy(chart.category_clicked)
    point = chart.category_rect(1).center().toPoint()
    qtbot.mouseMove(chart, point)
    assert chart.toolTip() == "미입력 · 1건 · 25.0%"
    assert chart.cursor().shape() == Qt.CursorShape.PointingHandCursor
    qtbot.mouseClick(chart, Qt.MouseButton.LeftButton, pos=point)
    assert spy.at(0) == [1]
    circle = chart._layout_geometry(chart.width())[0]
    assert chart._index_at(circle.center()) is None
    # A point inside the upper-left missing slice must select the same bucket.
    point = circle.center() + QPointF(-70, -70)
    assert chart.slice_path(1).contains(point)
    qtbot.mouseClick(chart, Qt.MouseButton.LeftButton, pos=point.toPoint())
    assert spy.at(1) == [1]
    qtbot.keyClick(chart, Qt.Key.Key_Home)
    qtbot.keyClick(chart, Qt.Key.Key_Return)
    assert spy.at(2) == [0]
    assert chart._selected == 0
    chart.set_selection(None)
    assert chart._selected is None
    assert chart._color(1).name() == "#c3c9d2"
    assert not chart.grab().isNull()


def test_donut_wrapping_legend_fits_narrow_scroll_area(qtbot):
    scroll = QScrollArea()
    qtbot.addWidget(scroll)
    scroll.setWidgetResizable(True)
    chart = StatisticsDonutChart()
    chart.set_data(
        [
            {"label": f"여러 줄로 표시되어야 하는 긴 충돌 대상 이름 {i}", "count": i + 1}
            for i in range(8)
        ],
        denominator=36,
    )
    scroll.setWidget(chart)
    scroll.resize(230, 260)
    scroll.show()
    qtbot.wait(10)
    assert scroll.horizontalScrollBar().maximum() == 0
    assert scroll.verticalScrollBar().maximum() > 0
    for index in range(8):
        rect = chart.category_rect(index)
        assert rect.left() >= 0 and rect.right() <= chart.width()
        assert rect.bottom() <= chart.height()
    chart.setFocus()
    qtbot.keyClick(chart, Qt.Key.Key_End)
    qtbot.keyClick(chart, Qt.Key.Key_Space)
    assert chart._selected == 7
    assert scroll.verticalScrollBar().value() > 0
    assert not chart.grab().isNull()


def test_zero_donut_denominator_and_empty_slices_are_safe(qtbot):
    chart = StatisticsDonutChart()
    qtbot.addWidget(chart)
    chart.resize(150, 200)
    chart.set_data([{"label": "건수 없음", "count": 0}], denominator=0)
    chart.show()
    assert chart.slice_path(0).isEmpty()
    assert "0.0%" in chart.accessibleDescription()
    assert not chart.grab().isNull()
    chart.set_data([], denominator=0)
    qtbot.keyClick(chart, Qt.Key.Key_Return)
    assert "표시할 데이터가 없습니다" in chart.accessibleDescription()
    assert not chart.grab().isNull()


def test_stacked_segments_preserve_original_indices_and_row_percentages(qtbot):
    chart = StatisticsStackedChart()
    qtbot.addWidget(chart)
    chart.resize(600, 240)
    chart.set_data(
        [
            {"label": "주간", "counts": [2, 6, 0], "total": 8},
            {"label": "야간", "counts": [0, 0, 3], "total": 3},
            {"label": "없음", "counts": [0, 0, 0], "total": 0},
        ],
        ["물피", "경상", "미입력"],
        ordered=True,
    )
    chart.show()
    spy = QSignalSpy(chart.cell_clicked)
    assert chart.cell_rect(0, 1).width() == pytest.approx(chart.cell_rect(0, 0).width() * 3)
    assert chart.cell_rect(0, 2).isEmpty()
    point = chart.cell_rect(0, 1).center().toPoint()
    qtbot.mouseMove(chart, point)
    assert "75.0%" in chart.toolTip()
    qtbot.mouseClick(chart, Qt.MouseButton.LeftButton, pos=point)
    assert spy.at(0) == [0, 1]
    qtbot.keyClick(chart, Qt.Key.Key_End)
    qtbot.keyClick(chart, Qt.Key.Key_Return)
    assert spy.at(1) == [1, 2]
    qtbot.keyClick(chart, Qt.Key.Key_Home)
    qtbot.keyClick(chart, Qt.Key.Key_Space)
    assert spy.at(2) == [0, 0]
    chart.set_selection(0, 1)
    assert chart._selected == 1
    chart.set_selection(None)
    assert chart._selected is None
    assert chart.column_color(0).lightnessF() > chart.column_color(1).lightnessF()
    assert chart.column_color(2).name() == "#c3c9d2"
    assert not chart.grab().isNull()


def test_stacked_long_legends_narrow_zero_rows_and_bad_dimensions(qtbot):
    chart = StatisticsStackedChart()
    qtbot.addWidget(chart)
    chart.resize(190, 180)
    columns = [f"길게 작성한 열 항목 {i}" for i in range(6)]
    chart.set_data([{"label": "아주 긴 행 이름", "counts": [0] * 6, "total": 0}], columns)
    chart.show()
    assert not chart._indices()
    for rect in chart._legend_rects(chart.width()):
        assert rect.left() >= 0 and rect.right() <= chart.width()
        assert rect.bottom() <= chart.height()
    qtbot.keyClick(chart, Qt.Key.Key_Return)
    assert not chart.grab().isNull()
    with pytest.raises(ValueError):
        chart.set_data([{"label": "bad", "counts": [1]}], columns)
    chart.set_data([], [])
    assert not chart.grab().isNull()


def test_scatter_click_keyboard_select_actual_case_uid_and_finite_fit(qtbot):
    chart = StatisticsScatterPlot()
    qtbot.addWidget(chart)
    chart.resize(600, 380)
    chart.set_data(
        [
            {"x": 0, "y": 1, "uid": "case-a", "label": "SAMSUNG-001"},
            {"x": 5, "y": 11, "uid": "case-b", "label": "SAMSUNG-002"},
            {"x": 10, "y": 21, "uid": "case-c", "label": "SAMSUNG-003"},
        ],
        x_label="속도(km/h)",
        y_label="시간(초)",
        slope=2,
        intercept=1,
    )
    chart.show()
    spy = QSignalSpy(chart.point_clicked)
    point = chart.point_rect(1).center().toPoint()
    qtbot.mouseMove(chart, point)
    assert "SAMSUNG-002" in chart.toolTip() and "시간(초) 11" in chart.toolTip()
    qtbot.mouseClick(chart, Qt.MouseButton.LeftButton, pos=point)
    assert spy.at(0) == ["case-b"]
    qtbot.keyClick(chart, Qt.Key.Key_End)
    qtbot.keyClick(chart, Qt.Key.Key_Return)
    assert spy.at(1) == ["case-c"]
    qtbot.keyClick(chart, Qt.Key.Key_Home)
    qtbot.keyClick(chart, Qt.Key.Key_Space)
    assert spy.at(2) == ["case-a"]
    chart.set_selection("case-b")
    assert chart._selected == 1
    chart.set_selection(None)
    assert chart._selected is None
    line = chart.trend_line()
    assert line and all(chart.plot_rect().contains(point) for point in line)
    assert not chart.grab().isNull()


@pytest.mark.parametrize("coordinate", [0.0, -4.5, 1e308])
def test_scatter_constant_axes_invalid_pairs_and_small_width_are_safe(qtbot, coordinate):
    chart = StatisticsScatterPlot()
    qtbot.addWidget(chart)
    chart.resize(180, 300)
    chart.set_data(
        [
            {"x": coordinate, "y": coordinate, "uid": "valid"},
            {"x": float("nan"), "y": 1, "uid": "nan"},
            {"x": 1, "y": float("inf"), "uid": "infinite"},
            {"x": "bad", "y": 1, "uid": "bad"},
        ],
        x_label="긴 가로축 수치 항목명",
        y_label="긴 세로축 수치 항목명",
        slope=float("inf"),
        intercept=0,
    )
    chart.show()
    assert len(chart._points) == 1
    point = chart.point_rect(0).center()
    assert math.isfinite(point.x()) and math.isfinite(point.y())
    assert chart.plot_rect().contains(point)
    assert chart.trend_line() is None
    assert not chart.grab().isNull()
    chart.set_data([], x_label="x", y_label="y")
    qtbot.keyClick(chart, Qt.Key.Key_Return)
    assert "표시할 데이터가 없습니다" in chart.accessibleDescription()
    assert not chart.grab().isNull()


def test_box_plot_generic_unit_points_low_samples_and_group_activation(qtbot):
    chart = BoxPlotWidget()
    qtbot.addWidget(chart)
    chart.resize(450, 220)
    chart.set_data(
        [
            {
                "label": "세 그룹 표본",
                "min": -10,
                "q1": -5,
                "median": 0,
                "q3": 5,
                "max": 10,
                "n": 3,
                "values": [-10, 0, 10],
            },
            {"label": "빈 그룹", "n": 0},
        ],
        unit="km/h",
        axis_label="속도",
        allow_negative=True,
    )
    chart.show()
    spy = QSignalSpy(chart.group_clicked)
    assert "중앙값 0.00km/h" in chart.accessibleDescription()
    assert "표본 적음" in chart.accessibleDescription()
    assert len(chart._point_positions(0)) == 3
    xs = [point.x() for point in chart._point_positions(0)]
    assert xs == sorted(xs)
    point = chart.group_rect(0).center().toPoint()
    qtbot.mouseMove(chart, point)
    assert "n=3" in chart.toolTip()
    qtbot.mouseClick(chart, Qt.MouseButton.LeftButton, pos=point)
    assert spy.at(0) == [0]
    qtbot.keyClick(chart, Qt.Key.Key_End)
    qtbot.keyClick(chart, Qt.Key.Key_Return)
    assert spy.at(1) == [1]
    chart.set_selection(None)
    assert chart._selected is None
    assert not chart.grab().isNull()


def test_box_plot_summary_unit_fits_and_narrow_plot_uses_full_width(qtbot):
    chart = BoxPlotWidget()
    qtbot.addWidget(chart)
    chart.set_data(
        [
            {
                "label": "속도",
                "min": 10,
                "q1": 20,
                "median": 29.52,
                "q3": 40,
                "max": 60,
                "n": 20,
                "values": [10, 20, 29.52, 40, 60],
            },
        ],
        unit="km/h",
        axis_label="속도",
    )
    chart.resize(700, 180)
    chart.show()
    assert chart._summary_width(chart.width()) - 4 >= chart.fontMetrics().horizontalAdvance(
        "중앙 29.52km/h"
    )
    chart.resize(230, chart.heightForWidth(230))
    qtbot.wait(5)
    plot_x, plot_width, suffix_width = chart._plot_dimensions()
    assert suffix_width == 0
    assert plot_x + plot_width <= chart.width()
    assert plot_width >= chart.width() * 0.85
    for point in chart._point_positions(0):
        assert plot_x <= point.x() <= plot_x + plot_width
        assert chart.group_rect(0).contains(point)
    assert not chart.grab().isNull()
