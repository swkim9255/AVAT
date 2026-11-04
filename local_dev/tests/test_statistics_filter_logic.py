"""Statistics conditions retain exact values and have explicit AND/OR scope."""

from __future__ import annotations

import math

import pytest

from investigator.form_definition import FormDefinition
from investigator.models import InvestigationCase
from investigator.schema import Choice
from investigator.statistics import StatisticsEngine, StatsCondition, StatsFilter


def case(uid: str, *, status: str = "completed", **values) -> InvestigationCase:
    return InvestigationCase(uid=uid, values=values, status=status)


def stats(cases=(), definition=None) -> StatisticsEngine:
    return StatisticsEngine(cases, definition or FormDefinition.default())


def selected(engine: StatisticsEngine, *conditions, mode="all", **kwargs) -> tuple[str, ...]:
    filters = StatsFilter(conditions=conditions, condition_mode=mode, **kwargs)
    return tuple(item.uid for item in engine.filtered_cases(filters))


def test_default_intersection_and_union_are_distinct_cached_groups():
    engine = stats([
        case("both", weather="맑음", driver_sex="여성"),
        case("weather", weather="맑음", driver_sex="남성"),
        case("driver", weather="강수", driver_sex="여성"),
        case("neither", weather="강수", driver_sex="남성"),
    ])
    conditions = (StatsCondition("weather", "맑음"), StatsCondition("driver_sex", "여성"))
    assert selected(engine, *conditions) == ("both",)
    assert selected(engine, *conditions, mode="any") == ("both", "weather", "driver")
    assert selected(engine, *conditions) == ("both",)
    # Matching both operands or a repeated operand never duplicates a case.
    assert selected(engine, *conditions, conditions[0], mode="any") == (
        "both", "weather", "driver",
    )
    assert engine.overview(StatsFilter(conditions=conditions, condition_mode="any"))["selected"] == 3


@pytest.mark.parametrize("mode", ["all", "any"])
def test_empty_condition_group_keeps_status_and_date_limits(mode):
    engine = stats([
        case("in", incident_date="20260110"),
        case("draft", status="draft", incident_date="20260110"),
        case("old", incident_date="20251231"),
        case("missing"),
    ])
    assert selected(engine, mode=mode) == ("in", "old", "missing")
    assert selected(engine, mode=mode, date_from="20260101", date_to="20260131") == ("in",)


def test_union_never_bypasses_outer_status_date_or_required_graph_conditions():
    engine = stats([
        case("hit", weather="맑음", driver_sex="남성", incident_date="20260110", driver_exp="5"),
        case("hit2", weather="강수", driver_sex="여성", incident_date="20260110", driver_exp="5"),
        case("both", weather="맑음", driver_sex="여성", incident_date="20260110", driver_exp="5"),
        case("wrong_pin", weather="맑음", driver_sex="여성", incident_date="20260110", driver_exp="3"),
        case("old", weather="맑음", driver_sex="여성", incident_date="20251231", driver_exp="5"),
        case("draft", status="draft", weather="맑음", driver_sex="여성",
             incident_date="20260110", driver_exp="5"),
    ])
    conditions = (StatsCondition("weather", "맑음"), StatsCondition("driver_sex", "여성"))
    required = (
        StatsCondition("driver_exp", 4, "ge"),
        StatsCondition("driver_sex", "여성"),
    )
    assert selected(
        engine, *conditions, mode="any", date_from="20260101", required_conditions=required,
    ) == ("hit2", "both")
    # A different graph selection must have a different cache key.
    assert selected(
        engine, *conditions, mode="any", date_from="20260101",
        required_conditions=(StatsCondition("driver_sex", "남성"),),
    ) == ("hit",)
    assert selected(engine, mode="any", required_conditions=required) == (
        "hit2", "both", "old",
    )


@pytest.mark.parametrize(("operator", "value", "expected"), [
    ("eq", 10, ("ten",)),
    ("ne", 10, ("zero", "twenty")),
    ("gt", 10, ("twenty",)),
    ("ge", 10, ("ten", "twenty")),
    ("lt", 10, ("zero",)),
    ("le", 10, ("zero", "ten")),
])
def test_numeric_comparisons_ignore_missing_invalid_and_accept_numeric_strings(operator, value, expected):
    engine = stats([
        case("zero", vehicle_mileage="0"), case("ten", vehicle_mileage="0010.0"),
        case("twenty", vehicle_mileage=20), case("blank", vehicle_mileage=""),
        case("missing"), case("invalid", vehicle_mileage="bad"),
        case("nan", vehicle_mileage="nan"), case("inf", vehicle_mileage="inf"),
    ])
    assert selected(engine, StatsCondition("vehicle_mileage", value, operator)) == expected


def test_between_is_inclusive_but_existing_chart_range_remains_half_open():
    engine = stats([case(str(value), vehicle_mileage=str(value)) for value in (0, 10, 20, 30)])
    assert selected(engine, StatsCondition("vehicle_mileage", operator="between", lower=10, upper=20)) == (
        "10", "20",
    )
    assert selected(engine, StatsCondition("vehicle_mileage", operator="between", lower=10, upper=10)) == ("10",)
    assert selected(engine, StatsCondition("vehicle_mileage", operator="range", lower=10, upper=20)) == ("10",)
    assert selected(engine, StatsCondition("vehicle_mileage", operator="range", lower=20)) == ("20", "30")
    assert selected(engine, StatsCondition("vehicle_mileage", operator="range", upper=10)) == ("0",)


def test_category_not_equal_means_not_selected_and_never_matches_blank():
    engine = stats([
        case("included", collision_directions=["전면", "후면"]),
        case("other", collision_directions=["좌측"]),
        case("empty", collision_directions=[]), case("blank", collision_directions=[""]),
        case("missing"),
    ])
    assert selected(engine, StatsCondition("collision_directions", "전면")) == ("included",)
    assert selected(engine, StatsCondition("collision_directions", "전면", "ne")) == ("other",)
    scalar = stats([
        case("sun", weather="맑음"), case("rain", weather="강수"),
        case("space", weather=" "), case("empty", weather=""), case("missing"),
    ])
    assert selected(scalar, StatsCondition("weather", "맑음", "ne")) == ("rain",)


def test_missing_and_not_missing_exclude_non_applicable_answers():
    engine = stats([
        case("selected", obstacle_exists="유", obstacle_pedestrians=["어른"]),
        case("empty", obstacle_exists="유"),
        case("not_applicable", obstacle_exists="무", obstacle_pedestrians=["어른"]),
    ])
    assert selected(engine, StatsCondition("obstacle_pedestrians", operator="missing")) == ("empty",)
    assert selected(engine, StatsCondition("obstacle_pedestrians", operator="not_missing")) == ("selected",)
    assert selected(engine, StatsCondition("obstacle_pedestrians", "어린이", "ne")) == ("selected",)


@pytest.mark.parametrize("condition", [
    StatsCondition("vehicle_mileage", math.nan),
    StatsCondition("vehicle_mileage", math.inf, "gt"),
    StatsCondition("vehicle_mileage", "bad", "ne"),
    StatsCondition("vehicle_mileage", None, "le"),
    StatsCondition("vehicle_mileage", True),
    StatsCondition("vehicle_mileage", operator="between", lower=1),
    StatsCondition("vehicle_mileage", operator="between", lower=2, upper=1),
    StatsCondition("vehicle_mileage", operator="between", lower=0, upper=math.inf),
    StatsCondition("vehicle_mileage", operator="range", lower=math.nan),
    StatsCondition("weather", "맑음", "gt"),
    StatsCondition("weather", operator="between", lower=0, upper=10),
    StatsCondition("weather", "맑음", "unknown"),
])
def test_malformed_conditions_rejected_even_on_empty_data_or_required_group(condition):
    engine = stats()
    with pytest.raises(ValueError):
        engine.filtered_cases(StatsFilter(conditions=(condition,), condition_mode="any"))
    with pytest.raises(ValueError):
        engine.filtered_cases(StatsFilter(required_conditions=(condition,)))


def test_invalid_mode_and_hidden_short_circuited_or_condition_are_rejected():
    engine = stats([case("sun", weather="맑음")])
    with pytest.raises(ValueError):
        selected(engine, mode="xor")
    with pytest.raises(ValueError):
        selected(engine, StatsCondition("weather", "맑음"), StatsCondition("driver_exp", "bad"), mode="any")
    with pytest.raises(KeyError):
        selected(stats(), StatsCondition("deleted_field", "value"))


def test_condition_choices_include_unused_options_then_distinct_observed_values():
    engine = stats([
        case("one", weather="새 라벨"), case("two", weather="맑음"),
        case("duplicate", weather="새 라벨"), case("blank", weather=" "), case("missing"),
    ])
    configured = engine.field("weather").choices
    choices = engine.condition_choices("weather")
    assert choices == (*configured, Choice("새 라벨", "새 라벨"))
    assert "강설" in [choice.label for choice in choices]  # Unused but available.
    assert selected(engine, StatsCondition("weather", choices[-1].value)) == ("one", "duplicate")
    assert engine.condition_choices("vehicle_mileage") == ()


def test_condition_choices_keep_current_labels_and_stored_values_not_codes_or_canonical_ids():
    definition = FormDefinition.default()
    record = next(record for record in definition.records() if record["key"] == "weather")
    first = record["choices"][0]
    first.update(label="쾌청한 날", value="clear_current")
    engine = stats([case("clear", weather="clear_current")], definition)
    choice = engine.condition_choices("weather")[0]
    assert choice == Choice("쾌청한 날", "clear_current")
    assert selected(engine, StatsCondition("weather", choice.value)) == ("clear",)
    assert all(option.value not in {"1", "2", "3", "4"} for option in engine.condition_choices("weather"))


def test_condition_choices_for_multiselect_text_identifiers_and_snapshot():
    values = [
        case("objects", obstacle_exists="유", obstacle_vehicle_types=["승용", "레거시", "레거시"]),
        case("hidden", obstacle_exists="무", obstacle_vehicle_types=["숨긴 값"]),
        case("text", remarks="메모", accident_classification="0000000000001"),
        case("more", remarks="다른 메모"),
    ]
    engine = stats(values)
    values[2].values["remarks"] = "나중 변경"
    choices = engine.condition_choices("obstacle_vehicle_types")
    assert choices[-1] == Choice("레거시", "레거시")
    assert "숨긴 값" not in [choice.value for choice in choices]
    assert [choice.value for choice in engine.condition_choices("remarks")] == ["메모", "다른 메모"]
    assert engine.condition_choices("accident_classification") == (Choice("0000000000001", "0000000000001"),)


def test_condition_choices_preserve_scalar_item_data_and_ignore_blank_list_items():
    engine = stats([
        case("number", remarks=10), case("list", remarks=[10, "text", None, " "]),
    ])
    choices = engine.condition_choices("remarks")
    assert [(choice.label, choice.value) for choice in choices] == [("10", 10), ("text", "text")]
    assert selected(engine, StatsCondition("remarks", choices[0].value)) == ("number", "list")


def test_condition_choices_keep_all_5000_unique_observed_values_in_order():
    # A large project must not truncate labels or sort away first-seen order.
    # No hard wall-clock threshold: shared CI machines can be intermittently busy.
    count = 5_000
    engine = stats([
        case(str(index), remarks=f"메모 {index:05d}") for index in range(count)
    ])
    choices = engine.condition_choices("remarks")
    assert len(choices) == count
    assert [choice.value for choice in choices] == [f"메모 {index:05d}" for index in range(count)]
    assert engine.condition_choices("remarks") is choices


def test_condition_choices_scalar_deduplication_keeps_python_numeric_equality():
    engine = stats([
        case("integer", remarks=1), case("float", remarks=1.0),
        case("boolean", remarks=True), case("text", remarks="1"),
    ])
    assert [(choice.label, choice.value) for choice in engine.condition_choices("remarks")] == [
        ("1", 1), ("1", "1"),
    ]
