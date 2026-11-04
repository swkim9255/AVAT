"""Multi-value butterfly groups use accident-level unions and explicit state."""

import csv

import pytest
from PySide6.QtCore import QPoint, Qt
from PySide6.QtWidgets import QApplication, QLabel

from investigator.butterfly import build_butterfly, butterfly_groups, normalize_group_values
from investigator.form_definition import FormDefinition
from investigator.models import InvestigationCase
from investigator.statistics import StatisticsEngine, StatsCondition, StatsFilter
from investigator.widgets.statistics_charts import StatisticsButterflyChart
from investigator.widgets.statistics_group_popup import StatisticsGroupPopup
from investigator.widgets.statistics_panel import StatisticsPanel


def case(uid, **values):
    return InvestigationCase(uid=uid, status="completed", values=values)


def cases():
    return [case("sun", weather="맑음", driver_sex="남성"),
            case("rain", weather="강수", driver_sex="남성"),
            case("snow", weather="강설", driver_sex="여성"),
            case("fog", weather="안개", driver_sex="여성"), case("missing", driver_sex="여성")]


def engine(data=None):
    return StatisticsEngine(data if data is not None else cases(), FormDefinition.default())


def report(stats=None, **kwargs):
    return build_butterfly(stats or engine(), "driver_sex", "weather", StatsFilter(), **kwargs)


def test_selected_values_are_accident_unions_and_others_are_excluded():
    result = report(left_values=("맑음", "강수"), right_values=("강설", "안개"))
    assert (result.left_total, result.right_total, result.excluded) == (2, 2, 1)
    assert result.left_label == "맑음, 강수" and result.right_label == "강설, 안개"
    # 어느 쪽에도 고르지 않은 값의 사고는 오른쪽에 자동으로 들어가지 않고 제외된다.
    partial = report(left_values=("맑음",), right_values=("강수",))
    assert (partial.left_total, partial.right_total, partial.excluded) == (1, 1, 3)
    assert any("20건 미만인 그룹" in warning and "맑음(n=1)" in warning for warning in partial.warnings)


def test_comparison_mode_argument_was_removed():
    with pytest.raises(TypeError):
        report(left_values=("맑음",), mode="rest")


def test_multiselect_groups_deduplicate_accidents_but_allow_cohort_overlap():
    stats = engine([case("both_left", collision_targets=["vehicle.passenger", "vehicle.truck"], weather="맑음"),
                    case("overlap", collision_targets=["vehicle.passenger", "pedestrian.adult"], weather="맑음"),
                    case("right", collision_targets=["pedestrian.adult"], weather="맑음"), case("blank")])
    result = build_butterfly(stats, "weather", "collision_targets", StatsFilter(),
                             left_values=("vehicle.passenger", "vehicle.truck", "vehicle.passenger"),
                             right_values=("pedestrian.adult",))
    assert (result.left_total, result.right_total, result.overlap) == (2, 2, 1)
    assert any("좌우 두 그룹에 모두" in warning for warning in result.warnings)


@pytest.mark.parametrize("kwargs", [
    {"left_values": (), "right_values": ("강설",)},
    {"left_values": ("맑음",), "right_values": ()},
    {"left_values": ("맑음", "강수"), "right_values": ("강수",)},
    {"left_values": "맑음", "right_values": ("강수",)},
    {"left_values": ("없는 값",), "right_values": ("강수",)},
])
def test_invalid_empty_or_shared_options_are_rejected(kwargs):
    with pytest.raises(ValueError):
        report(**kwargs)


def test_legacy_scalar_api_and_typed_value_sequences_remain_supported():
    old = report(left_value="맑음", right_value="강수")
    new = report(left_values=("맑음",), right_values=("강수",))
    assert old == new
    assert normalize_group_values((1, "1", 1)) == (1, "1")


def test_text_group_options_survive_current_filter_with_zero_counts():
    stats = engine([case("one", remarks="그룹 A", weather="맑음"),
                    case("two", remarks="그룹 B", weather="강수")])
    filters = StatsFilter(conditions=(StatsCondition("weather", "맑음"),))
    groups = butterfly_groups(stats, "remarks", filters)
    assert [(group.value, group.count) for group in groups] == [("그룹 A", 1), ("그룹 B", 0)]
    result = build_butterfly(stats, "weather", "remarks", filters,
                             left_values=("그룹 A",), right_values=("그룹 B",))
    assert (result.left_total, result.right_total) == (1, 0)


def popup(qtbot, selected=("맑음",), blocked=("강설",)):
    widget = StatisticsGroupPopup(butterfly_groups(engine(), "weather", StatsFilter()), selected,
                                  title="왼쪽 그룹 · 기상상황", blocked=blocked)
    qtbot.addWidget(widget)
    widget.resize(440, 480)
    widget.show()
    return widget


def item(widget, value):
    return next(widget.values_list.item(i) for i in range(widget.values_list.count())
                if widget.values_list.item(i).data(Qt.ItemDataRole.UserRole) == value)


def test_popup_full_row_hitbox_search_counts_and_blocked_opposite_side(qtbot):
    widget = popup(qtbot)
    assert widget.windowFlags() & Qt.WindowType.Popup
    rain = item(widget, "강수")
    assert rain.data(Qt.ItemDataRole.UserRole + 1) == 1
    rect = widget.values_list.visualItemRect(rain)
    qtbot.mouseClick(widget.values_list.viewport(), Qt.MouseButton.LeftButton,
                    pos=QPoint(rect.right() - 20, rect.center().y()))
    assert rain.checkState() == Qt.CheckState.Checked
    blocked = item(widget, "강설")
    rect = widget.values_list.visualItemRect(blocked)
    qtbot.mouseClick(widget.values_list.viewport(), Qt.MouseButton.LeftButton, pos=rect.center())
    assert blocked.checkState() == Qt.CheckState.Unchecked
    widget.search.setText("강")
    widget.select_all_button.click()
    assert widget._selected() == ("맑음", "강수")
    widget.search.clear()
    widget.clear_selection_button.click()
    assert not widget.apply_button.isEnabled()
    captured = []
    widget.applied.connect(captured.append)
    widget._apply()
    widget.cancel_button.click()
    assert captured == [] and widget.values is None


def panel(qtbot, group="weather", axis="driver_sex", data=None):
    widget = StatisticsPanel()
    qtbot.addWidget(widget)
    widget.resize(1180, 760)
    widget._row_key, widget._column_key, widget._cross_view = axis, group, "butterfly"
    widget.set_data(data if data is not None else cases(), FormDefinition.default())
    widget.view_tabs.setCurrentIndex(2)
    widget.show()
    QApplication.processEvents()
    return widget


def visible_chart(widget):
    QApplication.processEvents()
    return next(chart for chart in widget.pages.currentWidget().findChildren(StatisticsButterflyChart)
                if chart.isVisible())


def test_panel_popup_applies_multi_values_with_cancel_preserving_selection(qtbot):
    widget = panel(qtbot)
    # Initial right='강수': change right to snow before expanding left.
    widget.butterfly_right.click()
    right_popup = widget._group_popup
    right_popup.clear_selection_button.click()
    item(right_popup, "강설").setCheckState(Qt.CheckState.Checked)
    right_popup.apply_button.click()
    widget.butterfly_left.click()
    left_popup = widget._group_popup
    item(left_popup, "강수").setCheckState(Qt.CheckState.Checked)
    left_popup.apply_button.click()
    assert widget._butterfly_left_values == ("맑음", "강수")
    assert visible_chart(widget)._totals == (2, 1)
    widget.butterfly_left.click()
    widget._group_popup.clear_selection_button.click()
    widget._group_popup.cancel_button.click()
    assert widget._butterfly_left_values == ("맑음", "강수")


def test_multi_state_roundtrip_old_scalar_migration_and_filter_selection_preservation(qtbot):
    widget = panel(qtbot, group="remarks", axis="weather", data=[
        case("a", remarks="A", weather="맑음"), case("b", remarks="B", weather="강수"),
        case("c", remarks="C", weather="강설")])
    widget._butterfly_left_values, widget._butterfly_right_values = ("A", "B"), ("C",)
    widget.refresh()
    saved = widget.capture_state()
    widget.add_condition(StatsCondition("weather", "맑음"))
    assert widget._butterfly_left_values == ("A", "B")
    assert widget._butterfly_right_values == ("C",)
    widget.restore_state(saved)
    widget.refresh()
    assert widget._butterfly_left_values == ("A", "B")
    old = dict(saved)
    old.pop("butterfly_left_values")
    old.pop("butterfly_right_values")
    old.update(butterfly_left="B", butterfly_right="C")
    widget.restore_state(old)
    widget.refresh()
    assert widget._butterfly_left_values == ("B",) and widget._butterfly_right_values == ("C",)


def test_multi_side_drill_pin_and_export_reproduce_or_selection(qtbot, tmp_path):
    widget = panel(qtbot)
    widget._butterfly_left_values, widget._butterfly_right_values = ("맑음", "강수"), ("강설", "안개")
    widget.refresh()
    chart = visible_chart(widget)
    chart.bucket_clicked.emit(next(i for i, row in enumerate(chart._rows) if row["label"] == "남성"), 0)
    assert widget._selection[0] == ("sun", "rain")
    assert widget.pin_condition_button.isEnabled()
    path = tmp_path / "multi.csv"
    widget.export_to(path, "csv")
    with path.open(encoding="utf-8-sig", newline="") as stream:
        rows = list(csv.reader(stream))
    assert any("맑음, 강수" in row and "강설, 안개" in row for row in rows)
    widget.pin_selection()
    assert {case.uid for case in widget.engine.filtered_cases(widget.filters())} == {"sun", "rain"}
    assert any(condition.values == ("맑음", "강수") for condition in widget.conditions)


def toggle_texts(toggle):
    return [toggle._buttons.button(i).text() for i in range(len(toggle._values))]


def test_view_names_and_no_comparison_mode_toggle(qtbot):
    widget = panel(qtbot)
    assert toggle_texts(widget.cross_view) == ["히트맵", "100% 누적 막대", "양방향 막대"]
    assert all(widget.cross_view._buttons.button(i).toolTip() for i in range(3))
    assert toggle_texts(widget.butterfly_unit) == ["그룹 내 비율", "건수"]
    assert not hasattr(widget, "butterfly_mode")
    page_text = " ".join(child.text() for child in widget.pages.currentWidget().findChildren(QLabel))
    assert "가운데 항목" in page_text and "좌우 그룹 항목" in page_text
    assert "나머지" not in page_text and "나비형" not in page_text
    assert "butterfly_mode" not in widget.capture_state()
    # 오른쪽 그룹은 늘 직접 고르므로, 왼쪽에서 고른 값은 오른쪽 목록에서 막힌다.
    widget.butterfly_right.click()
    assert not item(widget._group_popup, "맑음").flags() & Qt.ItemFlag.ItemIsEnabled
    widget._group_popup.close()


def test_legacy_rest_state_reopens_as_two_selected_groups(qtbot):
    widget = panel(qtbot)
    groups = butterfly_groups(widget.engine, "weather", widget.filters())
    saved = widget.capture_state()
    # 이전 버전이 저장한 '한 그룹 vs 나머지' 상태: 왼쪽이 모든 값, 오른쪽은 비어 있음.
    saved.update(butterfly_mode="rest", butterfly_group_key="weather",
                 butterfly_left_values=tuple(group.value for group in groups), butterfly_right_values=())
    widget.restore_state(saved)
    widget.refresh()
    assert widget._butterfly_left_values == (groups[0].value,)
    assert widget._butterfly_right_values == (groups[1].value,)
    assert visible_chart(widget)._totals == (groups[0].count, groups[1].count)


def test_cross_item_labels_follow_view(qtbot):
    widget = panel(qtbot)

    def labels():
        return {child.text() for child in widget.pages.currentWidget().findChildren(QLabel)}

    widget.cross_view.setCurrentIndex(widget.cross_view.findData("heat"))
    QApplication.processEvents()
    assert {"행 항목", "열 항목"} <= labels()
    widget._row_key, widget._column_key = "driver_exp", "weather"
    widget.refresh()
    assert {"수치 항목", "범주 항목"} <= labels()
