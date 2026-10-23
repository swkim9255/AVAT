from __future__ import annotations

from copy import deepcopy

import pytest

from investigator.form_definition import FormDefinition, new_field
from investigator.models import InvestigationCase
from investigator.schema import FieldKind
from investigator.statistics import (
    INTERVAL_KEY,
    MAX_CROSS_CELLS,
    SEVERITY_KEY,
    CrossTableLimitError,
    StatisticsEngine,
    StatsCondition,
    StatsFilter,
)
from tests.helpers import valid_values


def accident(uid: str, *, status="completed", **values) -> InvestigationCase:
    raw = valid_values()
    raw.update(values)
    for key, value in tuple(raw.items()):
        if value is None:
            raw.pop(key)
    return InvestigationCase(uid=uid, values=raw, status=status)


def engine(cases, definition=None, fps=None):
    return StatisticsEngine(cases, definition or FormDefinition.default(), fps)


def test_default_filter_completed_and_explicit_status_and_date():
    cases = [
        accident("a", incident_date="20260101"),
        accident("b", status="draft", incident_date="20260131"),
        accident("c", incident_date="20260201"),
        accident("d", incident_date=None),
    ]
    stats = engine(cases)
    assert [case.uid for case in stats.filtered_cases()] == ["a", "c", "d"]
    filters = StatsFilter(status="all", date_from="2026-01-01", date_to="20260131")
    assert [case.uid for case in stats.filtered_cases(filters)] == ["a", "b"]
    assert [case.uid for case in stats.filtered_cases(StatsFilter(status="draft"))] == ["b"]


@pytest.mark.parametrize("filters", [
    StatsFilter(status="invalid"), StatsFilter(date_from="20260230"),
    StatsFilter(date_from="20260102", date_to="20260101"),
])
def test_bad_filters_rejected(filters):
    with pytest.raises(ValueError):
        engine([]).filtered_cases(filters)


def test_multiselect_distinct_counts_membership_and_filters_are_and():
    cases = [
        accident("a", site_types=["교차로", "횡단보도", "교차로"], weather="맑음"),
        accident("b", site_types=["교차로"], weather="강수"),
        accident("c", site_types=None, weather="맑음"),
    ]
    stats = engine(cases)
    result = stats.distribution("site_types")
    assert result.denominator == 3
    assert result.answered == 2
    assert result.missing == 1
    assert result.multi
    assert [(bucket.label, bucket.count) for bucket in result.buckets] == [
        ("교차로", 2), ("횡단보도", 1), ("미입력", 1),
    ]
    assert result.buckets[0].percent(3) == pytest.approx(200 / 3)
    filters = StatsFilter(conditions=(StatsCondition("site_types", "교차로"), StatsCondition("weather", "맑음")))
    assert [case.uid for case in stats.filtered_cases(filters)] == ["a"]
    missing = StatsFilter(conditions=(StatsCondition("site_types", operator="missing"),))
    assert [case.uid for case in stats.filtered_cases(missing)] == ["c"]


def test_applicability_excludes_object_details_but_not_obstacle_distance():
    cases = [
        accident("a", obstacle_exists="무", obstacle_vehicle_types=["승용"], obstacle_dist_value="4"),
        accident("b", obstacle_exists="유", obstacle_vehicle_types=["화물"], obstacle_dist_value="7"),
        accident("c", obstacle_exists="유", obstacle_vehicle_types=None, obstacle_dist_value=None),
    ]
    stats = engine(cases)
    vehicles = stats.distribution("obstacle_vehicle_types")
    assert (vehicles.total, vehicles.denominator, vehicles.not_applicable, vehicles.missing) == (3, 2, 1, 1)
    assert [category.label for category in vehicles.buckets] == ["화물", "미입력"]
    distance = stats.distribution("obstacle_dist_value")
    assert distance.denominator == 3
    assert distance.summary["count"] == 2
    filters = StatsFilter(conditions=(StatsCondition("obstacle_vehicle_types", operator="not_applicable"),))
    assert [case.uid for case in stats.filtered_cases(filters)] == ["a"]


def test_ordered_age_and_weekday_use_schema_order_instead_of_frequency():
    cases = [
        accident("a", driver_age="95-99세", incident_date="20260830"),
        accident("b", driver_age="95-99세", incident_date="20260830"),
        accident("c", driver_age="20-24세", incident_date="20260831"),
    ]
    stats = engine(cases)
    assert [bucket.label for bucket in stats.distribution("driver_age").buckets] == ["20-24세", "95-99세"]
    assert [bucket.label for bucket in stats.distribution("incident_day").buckets] == ["월", "일"]


def test_nominal_count_order_and_missing_last():
    stats = engine([
        accident("a", driver_sex="여성"), accident("b", driver_sex="여성"),
        accident("c", driver_sex=None), accident("d", driver_sex=None),
        accident("e", driver_sex=None), accident("f", driver_sex="남성"),
    ])
    assert [(bucket.label, bucket.count) for bucket in stats.distribution("driver_sex").buckets] == [
        ("여성", 2), ("남성", 1), ("미입력", 3),
    ]


def test_numeric_mileage_fixed_bins_and_boundary_filter_drilldown():
    cases = [accident(str(index), vehicle_mileage=str(value)) for index, value in enumerate([
        0, 19999, 20000, 199999, 200000, 800000,
    ])]
    stats = engine(cases)
    result = stats.distribution("vehicle_mileage")
    assert result.kind == "numeric"
    assert result.summary["count"] == 6
    assert result.summary["min"] == 0
    assert result.summary["max"] == 800000
    assert result.buckets[0].count == 2
    assert result.buckets[1].count == 1
    assert result.buckets[-1].count == 2
    for bucket in result.buckets:
        assert tuple(case.uid for case in stats.filtered_cases(StatsFilter(conditions=(bucket.condition,)))) == bucket.case_uids


def test_numeric_invalid_missing_negative_and_single_value_are_visible():
    stats = engine([
        accident("a", collision_dist_value="bad"), accident("b", collision_dist_value=None),
        accident("c", collision_dist_value="-3"), accident("d", collision_dist_value="nan"),
    ])
    result = stats.distribution("collision_dist_value")
    assert result.summary["count"] == 1
    assert result.summary["median"] == -3
    assert result.answered == 3
    assert sum(bucket.count for bucket in result.buckets) == 4
    invalid = next(bucket for bucket in result.buckets if bucket.label == "형식 오류")
    assert invalid.case_uids == ("a", "d")
    assert [case.uid for case in stats.filtered_cases(StatsFilter(conditions=(invalid.condition,)))] == ["a", "d"]


def test_identifiers_and_model_year_remain_categories():
    stats = engine([accident("a", accident_classification="0000000000001", vehicle_model_year="2023")])
    assert stats.distribution("accident_classification").buckets[0].label == "0000000000001"
    assert stats.distribution("vehicle_model_year").kind == "digits"
    assert stats.distribution("vehicle_model_year").summary == {}


def test_cross_multiselect_cell_uses_accident_membership_not_exploded_rows():
    stats = engine([
        accident("a", site_types=["교차로", "횡단보도"], available_data=["EDR", "CCTV"]),
        accident("b", site_types=["교차로"], available_data=["EDR"]),
    ])
    table = stats.cross_table("site_types", "available_data")
    assert (table.total, table.denominator) == (2, 2)
    assert table.row_multi and table.column_multi
    assert table.cells[0][0].count == 2
    assert table.cells[0][0].case_uids == ("a", "b")
    assert sum(cell.count for row in table.cells for cell in row) == 5


def test_cross_excludes_nonapplicable_and_retains_missing_axis():
    stats = engine([
        accident("a", obstacle_exists="무"),
        accident("b", obstacle_exists="유", obstacle_vehicle_types=["승용"], driver_sex=None),
    ])
    table = stats.cross_table("obstacle_vehicle_types", "driver_sex")
    assert table.denominator == 1
    assert table.columns[0].missing
    assert table.cells[0][0].case_uids == ("b",)


def test_dynamic_choices_custom_names_and_canonical_conditional_rules():
    definition = FormDefinition.default()
    obstacle = next(record for record in definition.records() if record["key"] == "obstacle_exists")
    obstacle["choices"][1]["value"] = "NO_OBJECT"
    obstacle["choices"][1]["label"] = "장애물 없음"
    custom = new_field()
    custom.update(label="추가 위험", kind=FieldKind.MULTI.value, required=False)
    custom["choices"] = [
        {"label": "급가속", "value": "sudden", "csv_column": ""},
        {"label": "신호위반", "value": "signal", "csv_column": ""},
    ]
    definition.tabs.append({"key": "custom", "title": "추가", "short_title": "추가", "fields": [custom]})
    definition.validate()
    case = accident("a", obstacle_exists="NO_OBJECT", **{custom["key"]: ["sudden", "signal"]})
    stats = engine([case], definition)
    assert stats.distribution("obstacle_exists").buckets[0].label == "장애물 없음"
    assert stats.distribution("obstacle_vehicle_types").not_applicable == 1
    assert {bucket.label for bucket in stats.distribution(custom["key"]).buckets} == {"급가속", "신호위반"}
    assert stats.quality().missing_cases == 0


def test_severity_highest_known_damage_unknown_and_no_unsupported_zero_assumption():
    stats = engine([
        accident("fatal", incident_damage_unknown="확인 가능", incident_fatality="1", incident_severe_injury="2"),
        accident("severe", incident_damage_unknown="확인 가능", incident_severe_injury="2"),
        accident("minor", incident_damage_unknown="확인 가능", incident_minor_injury="3"),
        accident("property", incident_damage_unknown="확인 가능", incident_property_damage="1"),
        accident("unknown", incident_damage_unknown="확인불가", incident_fatality="9"),
        accident("none", incident_damage_unknown="확인 가능", incident_fatality="0", incident_severe_injury="0", incident_minor_injury="0", incident_property_damage="0"),
        accident("missing", incident_damage_unknown="확인 가능", incident_fatality="0"),
    ])
    assert [(category.value, category.count) for category in stats.distribution(SEVERITY_KEY).buckets] == [
        ("fatal", 1), ("severe", 1), ("minor", 1), ("property", 1), ("none", 1), ("unknown", 1), (None, 1),
    ]


def video_case(uid, paths, markers, active=0):
    return InvestigationCase(uid=uid, values=valid_values(), status="completed", videos=paths, active_video_index=active, markers_by_video=markers)


def test_video_seconds_uses_actual_fps_only_and_markers_counts_all_attached_videos():
    cases = [
        video_case("a", ["a.mp4", "a2.mp4"], {
            "a.mp4": {"trigger_frame": 10, "collision_frames": [70]},
            "a2.mp4": {"trigger_frame": 0, "collision_frames": []},
        }),
        video_case("b", ["b.mp4"], {"b.mp4": {"trigger_frame": 0, "collision_frames": [60]}}),
        video_case("invalid", ["c.mp4"], {"c.mp4": {"trigger_frame": 90, "collision_frames": [20]}}),
        video_case("none", [], {}),
    ]
    stats = engine(cases, fps={"a.mp4": 60, "c.mp4": 30})
    report = stats.video()
    assert (report.total_videos, report.with_trigger, report.with_collision, report.with_both) == (4, 4, 3, 3)
    assert (report.missing_fps, report.invalid_order, report.no_active_video) == (1, 1, 1)
    assert report.valid_intervals == 1
    assert report.median_seconds == 1
    assert report.intervals.summary["mean"] == 1
    assert report.intervals.missing == 3
    assert stats.case_value(cases[0], INTERVAL_KEY) == 1


@pytest.mark.parametrize("fps", [0, -1, float("nan"), float("inf")])
def test_invalid_fps_never_gets_assumed_rate(fps):
    case = video_case("a", ["a.mp4"], {"a.mp4": {"trigger_frame": 0, "collision_frames": [30]}})
    report = engine([case], fps={"a.mp4": fps}).video()
    assert report.valid_intervals == 0
    assert report.missing_fps == 1
    assert report.median_seconds is None


def test_quality_honors_date_and_conditions_but_not_workflow_status():
    cases = [
        accident("a", status="completed", weather="맑음"),
        accident("b", status="draft", weather="맑음", driver_sex=None),
        accident("c", status="draft", weather="강수", driver_exp="wrong"),
    ]
    stats = engine(cases)
    report = stats.quality(StatsFilter(conditions=(StatsCondition("weather", "맑음"),)))
    assert (report.total, report.completed, report.draft) == (2, 1, 1)
    assert report.completion_rate == 50
    assert report.missing_cases == 1
    assert report.invalid_cases == 0
    assert report.complete_valid == 1
    assert report.issues[0].case_uid == "b"
    assert report.field_missing[0].value == "driver_sex"
    assert report.field_missing[0].case_uids == ("b",)


def test_quality_optional_fields_not_penalized_and_damage_na_ignored():
    case = accident("a", remarks=None, obstacle_dist_source=None, obstacle_dist_range=None,
                    obstacle_dist_value=None, incident_minor_injury=None)
    report = engine([case]).quality()
    assert report.missing_cases == 0
    assert report.invalid_cases == 0
    assert report.required_answer_rate == 100
    assert report.complete_valid == 1


def test_quality_includes_field_and_marker_errors_without_changes():
    case = accident("a", driver_exp="wrong", driver_sex=None)
    case.videos = ["a.mp4"]
    case.markers_by_video = {"a.mp4": {"trigger_frame": 90, "collision_frames": [20]}}
    before = deepcopy(case.to_dict())
    stats = engine([case])
    report = stats.quality()
    assert report.issues[0].missing == ("driver_sex",)
    assert set(report.issues[0].errors) == {"driver_exp", "trigger_frame"}
    stats.distribution("driver_sex")
    stats.cross_table("driver_sex", "weather")
    stats.overview()
    stats.video()
    assert case.to_dict() == before


def test_empty_workspace_is_safe_for_every_report():
    stats = engine([])
    assert stats.distribution("driver_sex").denominator == 0
    assert stats.distribution("vehicle_mileage").buckets == ()
    assert stats.cross_table("driver_sex", "weather").cells == ()
    assert stats.quality().required_answer_rate == 0
    assert stats.video().valid_intervals == 0
    assert stats.overview()["selected"] == 0


def test_snapshot_stays_consistent_when_live_case_changes_during_statistics_work():
    case = video_case("a", ["a.mp4"], {"a.mp4": {"trigger_frame": 0, "collision_frames": [60]}})
    case.values["site_types"] = ["교차로"]
    stats = engine([case], fps={"a.mp4": 30})
    case.values["site_types"].append("횡단보도")
    case.status = "draft"
    case.markers_by_video["a.mp4"]["collision_frames"] = [120]
    case.videos.append("b.mp4")
    assert stats.overview()["selected"] == 1
    assert stats.video().total_videos == 1
    assert stats.video().median_seconds == 2
    assert [bucket.label for bucket in stats.distribution("site_types").buckets] == ["교차로"]


def test_vehicle_facing_state_is_not_applicable_to_pedestrian_only_obstacle():
    stats = engine([
        accident("pedestrian", obstacle_exists="유", obstacle_vehicle_types=[],
                 obstacle_pedestrians=["어른"], obstacle_vehicle_state="후면", obstacle_state="동적"),
        accident("vehicle", obstacle_exists="유", obstacle_vehicle_types=["승용"],
                 obstacle_vehicle_state=None, obstacle_state="정적"),
    ])
    distribution = stats.distribution("obstacle_vehicle_state")
    assert distribution.denominator == 1
    assert distribution.not_applicable == 1
    assert distribution.missing == 1
    assert distribution.buckets[0].case_uids == ("vehicle",)
    filters = StatsFilter(conditions=(StatsCondition("obstacle_vehicle_state", operator="not_applicable"),))
    assert [case.uid for case in stats.filtered_cases(filters)] == ["pedestrian"]
    assert all("obstacle_vehicle_state" not in issue.errors for issue in stats.quality().issues)


@pytest.mark.parametrize("marker_key", ["__case__", "orphaned_video.mp4"])
def test_quality_matches_completion_errors_for_legacy_orphaned_markers(marker_key):
    case = accident("legacy")
    case.markers_by_video[marker_key] = {"trigger_frame": 90, "collision_frames": [20]}
    assert "trigger_frame" in case.errors
    report = engine([case]).quality()
    assert report.invalid_cases == 1
    assert report.issues[0].errors == ("trigger_frame",)


def test_numeric_equality_converts_numbers_but_identifier_equality_keeps_leading_zeroes():
    stats = engine([accident("a", vehicle_mileage="0010", accident_classification="000001")])
    numeric = StatsFilter(conditions=(StatsCondition("vehicle_mileage", 10.0),))
    assert len(stats.filtered_cases(numeric)) == 1
    identifier = StatsFilter(conditions=(StatsCondition("accident_classification", "1"),))
    assert stats.filtered_cases(identifier) == ()


def test_cross_table_rejects_many_free_text_categories_without_silent_top_n():
    stats = engine([
        accident(str(index), remarks=f"개별 비고 {index}") for index in range(101)
    ])
    with pytest.raises(CrossTableLimitError) as error:
        stats.cross_table("remarks", "weather")
    assert error.value.row_count == 101
    assert error.value.column_count == 1
    assert error.value.max_cells == MAX_CROSS_CELLS
    # Ordinary distributions remain available and preserve every actual value.
    assert len(stats.distribution("remarks").buckets) == 101
    filtered = StatsFilter(conditions=(StatsCondition("remarks", "개별 비고 1"),))
    assert stats.cross_table("remarks", "weather", filtered).cells[0][0].count == 1


def test_cross_table_rejects_excessive_product_even_when_both_axes_are_small():
    stats = engine([
        accident(str(index), vehicle_model_name=f"모델 {index % 51}", remarks=f"메모 {index % 50}")
        for index in range(51)
    ])
    with pytest.raises(CrossTableLimitError) as error:
        stats.cross_table("vehicle_model_name", "remarks")
    assert (error.value.row_count, error.value.column_count) == (51, 50)


def test_cross_table_allows_exact_safe_cell_limit():
    stats = engine([
        accident(str(index), vehicle_model_name=f"모델 {index}", remarks=f"메모 {index}")
        for index in range(50)
    ])
    cross = stats.cross_table("vehicle_model_name", "remarks")
    assert sum(len(row) for row in cross.cells) == MAX_CROSS_CELLS
    assert sum(cell.count for row in cross.cells for cell in row) == 50
