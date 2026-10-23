"""Checklist OR values, explicit OR groups, and old-state migration."""

from dataclasses import asdict

import pytest

from investigator.form_definition import FormDefinition
from investigator.models import InvestigationCase
from investigator.statistics import (
    AnyOf,
    StatisticsEngine,
    StatsCondition,
    StatsFilter,
    normalize_condition,
    normalize_filter_items,
)
from investigator.statistics_conditions import clean_field_label, condition_text


def case(uid, *, status="completed", **values):
    return InvestigationCase(uid=uid, values=values, status=status)


def engine(*cases):
    return StatisticsEngine(cases, FormDefinition.default())


def selected(stats, *items, **kwargs):
    return tuple(case.uid for case in stats.filtered_cases(StatsFilter(conditions=items, **kwargs)))


def test_values_within_one_checklist_are_or_and_distinct_accidents():
    stats = engine(
        case("front", collision_directions=["전면"]),
        case("rear", collision_directions=["후면"]),
        case("both", collision_directions=["전면", "후면", "후면"]),
        case("left", collision_directions=["좌측"]), case("missing"),
    )
    condition = StatsCondition("collision_directions", values=("전면", "후면"))
    assert selected(stats, condition) == ("front", "rear", "both")
    assert stats.overview(StatsFilter(conditions=(condition,)))["selected"] == 3


def test_exclude_means_none_of_checked_values_and_missing_is_explicit():
    stats = engine(
        case("sun", weather="맑음"), case("rain", weather="강수"),
        case("snow", weather="강설"), case("missing"), case("blank", weather=" "),
    )
    assert selected(stats, StatsCondition("weather", values=("강수", "강설"), exclude=True)) == (
        "sun", "missing", "blank",
    )
    assert selected(stats, StatsCondition("weather", values=("강수", "강설", None), exclude=True)) == (
        "sun",
    )
    assert selected(stats, StatsCondition("weather", values=(None,))) == ("missing", "blank")
    assert selected(stats, StatsCondition("weather", values=(None,), exclude=True)) == (
        "sun", "rain", "snow",
    )
    assert selected(stats, StatsCondition("weather", values=("맑음", None))) == (
        "sun", "missing", "blank",
    )


def test_missing_list_items_are_not_separate_missing_answers():
    stats = engine(
        case("partly", collision_directions=["전면", None]),
        case("empty", collision_directions=[None, " "]), case("missing"),
    )
    assert selected(stats, StatsCondition("collision_directions", values=(None,))) == (
        "empty", "missing",
    )


def test_no_answer_and_exclusion_never_include_inapplicable_fields():
    stats = engine(
        case("empty", obstacle_exists="유"),
        case("adult", obstacle_exists="유", obstacle_pedestrians=["어른"]),
        case("hidden", obstacle_exists="무"),
    )
    assert selected(stats, StatsCondition("obstacle_pedestrians", values=(None,))) == ("empty",)
    assert selected(stats, StatsCondition("obstacle_pedestrians", values=("어른",), exclude=True)) == (
        "empty",
    )


def test_or_group_is_intersected_with_outer_conditions_status_and_dates():
    stats = engine(
        case("weather", weather="강수", driver_sex="남성", driver_exp=5, incident_date="20260110"),
        case("driver", weather="맑음", driver_sex="여성", driver_exp=5, incident_date="20260110"),
        case("both", weather="강수", driver_sex="여성", driver_exp=5, incident_date="20260110"),
        case("wrong_outer", weather="강수", driver_sex="여성", driver_exp=2, incident_date="20260110"),
        case("old", weather="강수", driver_exp=5, incident_date="20250110"),
        case("draft", status="draft", weather="강수", driver_exp=5, incident_date="20260110"),
    )
    group = AnyOf((StatsCondition("weather", values=("강수",)),
                   StatsCondition("driver_sex", values=("여성",))))
    assert selected(stats, group, StatsCondition("driver_exp", lower=5), date_from="20260101") == (
        "weather", "driver", "both",
    )
    assert selected(stats, group, StatsCondition("driver_exp", lower=5), date_from="20260101") == (
        "weather", "driver", "both",
    )


def test_separate_groups_remain_outer_and_and_have_independent_cache_keys():
    stats = engine(
        case("both", weather="맑음", driver_sex="여성"),
        case("sun", weather="맑음", driver_sex="남성"),
        case("woman", weather="강수", driver_sex="여성"),
    )
    sun = StatsCondition("weather", values=("맑음",))
    woman = StatsCondition("driver_sex", values=("여성",))
    assert selected(stats, sun, woman) == ("both",)
    assert selected(stats, AnyOf((sun, woman))) == ("both", "sun", "woman")
    assert selected(stats, StatsCondition("weather", values=("맑음",), exclude=True)) == ("woman",)
    assert selected(stats, StatsCondition("weather", values=("강수",))) == ("woman",)
    assert selected(stats, sun, woman) == ("both",)


def test_numeric_new_range_is_inclusive_lower_exclusive_upper_and_cache_tracks_exclusion():
    stats = engine(*(case(str(n), driver_exp=str(n)) for n in (0, 5, 10)), case("missing"))
    condition = StatsCondition("driver_exp", lower=0, upper=10)
    assert condition.operator == "range"
    assert selected(stats, condition) == ("0", "5")
    assert selected(stats, StatsCondition("driver_exp", values=(), lower=0, upper=10)) == ("0", "5")
    assert selected(stats, StatsCondition("driver_exp", operator="range", lower=0, upper=10,
                                          exclude=True)) == ("10", "missing")
    assert selected(stats, StatsCondition("driver_exp", values=(None,))) == ("missing",)
    assert selected(stats, StatsCondition("driver_exp", values=(5, "10"))) == ("5", "10")


def test_empty_selected_set_has_defined_membership_semantics():
    stats = engine(case("sun", weather="맑음"), case("missing"))
    assert selected(stats, StatsCondition("weather", values=())) == ()
    assert selected(stats, StatsCondition("weather", values=(), exclude=True)) == ("sun", "missing")
    with pytest.raises(ValueError):
        StatsCondition("weather", values="맑음")


def test_anyof_rejects_nested_or_empty_and_validates_every_operand_before_matching():
    good = StatsCondition("weather", values=("맑음",))
    with pytest.raises(ValueError):
        AnyOf(())
    with pytest.raises(ValueError):
        AnyOf((AnyOf((good,)), good))
    stats = engine(case("sun", weather="맑음"))
    with pytest.raises(KeyError):
        selected(stats, AnyOf((good, StatsCondition("deleted", values=("x",)))))
    with pytest.raises(ValueError):
        selected(stats, AnyOf((good, StatsCondition("driver_exp", values=("bad",)))))


@pytest.mark.parametrize("legacy, expected", [
    ({"field_key": "weather", "operator": "eq", "value": "맑음"},
     StatsCondition("weather", values=("맑음",))),
    ({"field_key": "weather", "operator": "missing"}, StatsCondition("weather", values=(None,))),
    ({"field_key": "weather", "operator": "not_missing"},
     StatsCondition("weather", values=(None,), exclude=True)),
    ({"field_key": "driver_exp", "operator": "range", "lower": 5, "upper": 10},
     StatsCondition("driver_exp", operator="range", lower=5, upper=10)),
])
def test_normalizes_old_saved_dictionary_and_object_conditions(legacy, expected):
    assert normalize_condition(legacy) == expected
    old = StatsCondition(**legacy)
    assert normalize_condition(old) == expected
    assert normalize_condition(asdict(expected)) == expected


def test_migration_of_old_global_or_preserves_required_outer_and():
    sun = StatsCondition("weather", "맑음")
    woman = StatsCondition("driver_sex", "여성")
    required = StatsCondition("driver_exp", 5, "ge")
    migrated = normalize_filter_items((sun, woman), "any", (required,))
    assert isinstance(migrated[0], AnyOf)
    assert migrated[1] == required
    assert normalize_filter_items((), "any", (required,)) == (required,)
    stats = engine(
        case("yes", weather="맑음", driver_exp=6),
        case("no", driver_sex="여성", driver_exp=1),
    )
    assert selected(stats, *migrated) == ("yes",)
    group_dict = {"conditions": [asdict(sun), asdict(woman)]}
    assert normalize_filter_items((group_dict, required)) == migrated
    with pytest.raises(ValueError):
        normalize_filter_items(({"conditions": [group_dict]},))


def test_shared_text_names_values_exclusion_missing_and_single_level_groups():
    stats = engine()
    assert clean_field_label("기관코드 (Index)") == "기관코드"
    assert clean_field_label("거리(m)") == "거리(m)"
    assert condition_text(stats, StatsCondition("institution", values=("samsung", "katri"))) == (
        "기관코드: samsung, katri"
    )
    assert condition_text(stats, StatsCondition("weather", values=("강수",), exclude=True)) == "기상상황: 강수 제외"
    assert condition_text(stats, StatsCondition("weather", values=(None,))) == "기상상황: (미입력)"
    assert condition_text(stats, AnyOf((StatsCondition("weather", values=("강수",)),
                                      StatsCondition("driver_exp", lower=5, upper=10)))) == (
        "(기상상황: 강수 OR 운전 경력: 5 이상 ~ 10 미만)"
    )


def test_legacy_ne_retains_report_behavior_and_other_comparisons_are_unchanged():
    stats = engine(case("sun", weather="맑음", driver_exp=5),
                   case("rain", weather="강수", driver_exp=10), case("missing"))
    assert selected(stats, StatsCondition("weather", "맑음", "ne")) == ("rain",)
    assert selected(stats, normalize_condition(StatsCondition("driver_exp", 5, "gt"))) == ("rain",)
    assert selected(stats, StatsCondition("driver_exp", operator="between", lower=5, upper=10)) == (
        "sun", "rain",
    )
