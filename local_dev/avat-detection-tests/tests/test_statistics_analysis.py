from __future__ import annotations

import math
from copy import deepcopy

import pytest

from investigator.form_definition import FormDefinition
from investigator.models import InvestigationCase
from investigator.statistics import (
    TRIGGER_SPEED_KEY,
    StatisticsEngine,
    StatsCondition,
    StatsFilter,
)
from investigator.statistics_analysis import (
    beta_cdf,
    chi_square,
    gamma_survival,
    kruskal_wallis,
    pearson,
    percentile,
)
from tests.helpers import valid_values


def accident(uid, **values):
    return InvestigationCase(uid=str(uid), values={**valid_values(), **values}, status="completed")


def engine(cases, fps=None):
    return StatisticsEngine(cases, FormDefinition.default(), fps)


def speed_case(uid="speed", *, fps_marker=True):
    case = accident(uid)
    case.videos = ["measured.mp4", "other.mp4"]
    case.markers_by_video = {
        "measured.mp4": {"trigger_frame": 15 if fps_marker else None, "collision_frames": [45],
                         "speed_points": [{"frame": 0}, {"frame": 30, "distance_m": 10}]},
        "other.mp4": {"trigger_frame": 30, "collision_frames": [60]},
    }
    return case


@pytest.mark.parametrize("shape,value,expected", [
    (1, 3, math.exp(-3)), (1, 0, 1),
    (0.5, 2, math.erfc(math.sqrt(2))),
    (2, 3, 4 * math.exp(-3)), (3, 3, 8.5 * math.exp(-3)),
    (1, math.inf, 0), (0.5, 1000, 0),
])
def test_gamma_upper_probability_matches_analytic_distributions(shape, value, expected):
    assert gamma_survival(shape, value) == pytest.approx(expected, rel=2e-12, abs=2e-15)


@pytest.mark.parametrize("a,b,x,expected", [
    (1, 1, 0.3, 0.3), (1, 1, 0.8, 0.8),
    (0.5, 0.5, 0.25, 1 / 3), (2, 2, 0.25, 0.15625),
    (2, 3, 0.5, 0.6875), (3, 2, 0.5, 0.3125),
    (2, 4, 0, 0), (2, 4, 1, 1),
])
def test_regularized_beta_matches_closed_form_values(a, b, x, expected):
    assert beta_cdf(a, b, x) == pytest.approx(expected, rel=2e-12, abs=2e-15)


def test_pearson_matches_scipy_official_example_and_actual_student_t_probability():
    # https://docs.scipy.org/doc/scipy/reference/generated/scipy.stats.pearsonr.html
    r, slope, intercept, test = pearson((1, 2, 3, 4, 5, 6, 7), (10, 9, 2.5, 6, 4, 3, 2))
    assert r == pytest.approx(-0.828503883588428, rel=2e-13)
    assert test.p_value == pytest.approx(0.021280260007523286, rel=2e-12)
    assert test.valid and test.df == 5 and test.n == 7
    assert slope == pytest.approx(-1.2321428571428572)
    assert intercept == pytest.approx(10.142857142857142)
    # n=3, r=0.5 has df=1 and exact two-sided p=2/3. The mockup's
    # chi-square approximation for t² would not produce this probability.
    xs = (-1.0, 0.0, 1.0)
    ys = (-1 / math.sqrt(2) + 1 / math.sqrt(2), -math.sqrt(2), 1 / math.sqrt(2) + 1 / math.sqrt(2))
    result = pearson(xs, ys)[3]
    assert result.statistic == pytest.approx(0.5)
    assert result.p_value == pytest.approx(2 / 3)


@pytest.mark.parametrize("xs,ys", [
    ((1, 2), (2, 3)), ((), ()), ((1, 1, 1), (1, 2, 3)),
    ((1, 2, 3), (0, 0, 0)),
])
def test_pearson_missing_sample_or_constant_has_explicit_reason(xs, ys):
    r, slope, intercept, test = pearson(xs, ys)
    assert r is None and slope is None and intercept is None
    assert not test.valid and test.reason and test.p_value is None


def test_pearson_is_stable_under_very_large_and_small_unit_changes():
    xs, ys = (1, 2, 4, 5), (3, 4, 4, 8)
    expected = pearson(xs, ys)
    large = pearson(tuple(value * 1e300 for value in xs), tuple(value * 1e300 for value in ys))
    small = pearson(tuple(value * 1e-300 for value in xs), tuple(value * 1e-300 for value in ys))
    assert large[0] == pytest.approx(expected[0])
    assert small[0] == pytest.approx(expected[0])
    assert large[3].p_value == pytest.approx(expected[3].p_value)
    assert small[3].p_value == pytest.approx(expected[3].p_value)


def test_chi_square_matches_scipy_documented_example_without_yates():
    # https://docs.scipy.org/doc/scipy/reference/generated/scipy.stats.chi2_contingency.html
    result = chi_square([[10, 10, 20], [20, 20, 20]])
    assert result.valid
    assert result.statistic == pytest.approx(2.7777777777777777)
    assert result.p_value == pytest.approx(0.24935220877729619, rel=2e-12)
    assert result.effect_size == pytest.approx(1 / 6)
    assert result.df == 2


def test_chi_square_small_sample_zero_margins_and_sparse_expected_cells():
    assert not chi_square([[3, 1], [2, 4]]).valid
    assert chi_square([[10, 0], [10, 0]]).reason
    base = chi_square([[10, 10], [20, 20]])
    zero_margin = chi_square([[10, 10, 0], [20, 20, 0], [0, 0, 0]])
    assert base.p_value == zero_margin.p_value == 1
    assert zero_margin.df == 1
    sparse = chi_square([[1, 0], [0, 30]])
    assert sparse.valid
    assert sparse.warnings
    assert 0 <= sparse.p_value <= 1


def test_kruskal_matches_official_rank_example_and_documented_tie_correction():
    # https://docs.scipy.org/doc/scipy/reference/generated/scipy.stats.kruskal.html
    result = kruskal_wallis([(1, 3, 5, 7, 9), (2, 4, 6, 8, 10)])
    assert result.statistic == pytest.approx(0.2727272727272734)
    assert result.p_value == pytest.approx(0.6015081344405895)
    tied = kruskal_wallis([(1, 1, 1), (2, 2, 2), (2, 2)], minimum_group=1)
    assert tied.statistic == pytest.approx(7)
    assert tied.p_value == pytest.approx(0.0301973834223185)


def test_kruskal_excludes_small_groups_and_identical_values_have_no_test():
    result = kruskal_wallis([(1, 3, 5, 7, 9), (2, 4, 6, 8, 10), (500,)])
    assert result.valid and result.n == 10 and result.warnings
    assert not kruskal_wallis([(1, 2), (3, 4)]).valid
    identical = kruskal_wallis([(1,) * 5, (1,) * 5])
    assert not identical.valid and "같아" in identical.reason


def test_trigger_speed_is_derived_from_current_video_points_actual_fps_and_trigger():
    case = speed_case()
    stats = engine([case], {"measured.mp4": 30})
    assert stats.case_value(case, TRIGGER_SPEED_KEY) == pytest.approx(36)
    assert stats.field(TRIGGER_SPEED_KEY).unit == "km/h"
    assert stats.is_numeric(TRIGGER_SPEED_KEY)
    assert not stats.is_numeric("trigger_vehicle_speed")
    assert stats.case_value(case, "trigger_vehicle_speed") == valid_values()["trigger_vehicle_speed"]
    distribution = stats.distribution(TRIGGER_SPEED_KEY)
    assert distribution.summary["mean"] == pytest.approx(36)
    assert distribution.summary["q1"] == pytest.approx(36)
    assert stats.filtered_cases(StatsFilter(conditions=(StatsCondition(TRIGGER_SPEED_KEY, 36),))) == (case,)


def test_trigger_speed_never_converts_category_midpoints_or_assumes_fps():
    case = speed_case()
    assert engine([case]).case_value(case, TRIGGER_SPEED_KEY) is None
    assert engine([speed_case(fps_marker=False)], {"measured.mp4": 30}).distribution(TRIGGER_SPEED_KEY).missing == 1
    case.active_video_index = 1
    assert engine([case], {"measured.mp4": 30, "other.mp4": 30}).case_value(case, TRIGGER_SPEED_KEY) is None
    no_points = accident("none", trigger_vehicle_speed="고속(60kph이상)")
    assert engine([no_points]).case_value(no_points, TRIGGER_SPEED_KEY) is None


def test_association_uses_real_complete_cases_and_keeps_existing_descriptive_table():
    cases = []
    counts = [[10, 10, 20], [20, 20, 20]]
    for sex, row in zip(("남성", "여성"), counts, strict=True):
        for vehicle, count in zip(("승용", "승합", "화물"), row, strict=True):
            first_uid = len(cases)
            cases.extend(accident(first_uid + index, driver_sex=sex, vehicle_type=vehicle) for index in range(count))
    cases.extend([accident("missing", driver_sex=None), accident("bad", driver_sex="삭제된 선택지")])
    stats = engine(cases)
    result = stats.association("driver_sex", "vehicle_type")
    assert result.n == 100
    assert result.p_value == pytest.approx(0.24935220877729619)
    assert stats.cross_table("driver_sex", "vehicle_type").total == 102
    assert stats.association("driver_sex", "vehicle_type") is result


def test_association_and_group_tests_skip_overlapping_multiselect_memberships():
    cases = [accident(index, site_types=["교차로", "횡단보도"], vehicle_mileage=str(index + 1)) for index in range(10)]
    stats = engine(cases)
    association = stats.association("site_types", "weather")
    assert not association.valid and "중복" in association.reason
    groups = stats.numeric_groups("site_types", "vehicle_mileage")
    assert groups.valid_count == 10
    assert len(groups.groups) == 2
    assert sum(group.n for group in groups.groups) == 20
    assert groups.multi
    assert not groups.test.valid and "중복" in groups.test.reason


def test_numeric_groups_use_complete_valid_values_and_inclusive_quartiles():
    cases = [
        accident(index, driver_sex="남성" if index < 5 else "여성", driver_exp=str(index + 1))
        for index in range(10)
    ]
    cases.extend([
        accident("empty", driver_sex=None), accident("bad", driver_exp="bad"),
        accident("negative", driver_exp="-4"),
    ])
    stats = engine(cases)
    report = stats.numeric_groups("driver_sex", "driver_exp")
    assert (report.total, report.valid_count, report.excluded_count) == (13, 10, 3)
    male = next(group for group in report.groups if group.label == "남성")
    assert (male.min, male.q1, male.median, male.q3, male.max) == (1, 2, 3, 4, 5)
    assert male.values == (1, 2, 3, 4, 5)
    assert male.n == 5
    assert set(male.case_uids) == {str(index) for index in range(5)}
    assert male.condition == StatsCondition("driver_sex", "남성")
    assert report.test.valid and report.test.n == 10


def test_scatter_keeps_case_ids_pairs_filters_and_missing_exclusions():
    cases = [accident(index, driver_exp=str(index + 1), collision_dist_value=str((index + 1) * 2)) for index in range(6)]
    cases.extend([
        accident("missing", driver_exp=None), accident("bad", collision_dist_value="NaN"),
        accident("negative", collision_dist_value="-4"),
    ])
    stats = engine(cases)
    report = stats.scatter("driver_exp", "collision_dist_value")
    assert (report.total, report.n, report.excluded) == (9, 6, 3)
    assert report.r == pytest.approx(1)
    assert report.slope == pytest.approx(2)
    assert report.intercept == pytest.approx(0, abs=1e-13)
    assert report.test.p_value == pytest.approx(0, abs=1e-13)
    assert report.points[0].case_uid == "0"
    assert (report.points[0].x, report.points[0].y) == (1, 2)
    assert report.x_unit == "년" and report.y_unit == "m"
    assert stats.scatter("driver_exp", "collision_dist_value") is report
    filtered = StatsFilter(conditions=(StatsCondition("driver_exp", operator="range", lower=4),))
    assert stats.scatter("driver_exp", "collision_dist_value", filtered).n == 3


def test_inference_missing_same_field_and_wrong_types_are_explicit():
    stats = engine([])
    assert stats.numeric_groups("driver_sex", "driver_exp").test.reason
    assert stats.scatter("driver_exp", "collision_dist_value").test.reason
    assert stats.association("driver_sex", "weather").reason
    assert stats.association("driver_sex", "driver_sex").reason
    assert stats.scatter("driver_exp", "driver_exp").test.reason
    with pytest.raises(ValueError):
        stats.numeric_groups("driver_exp", "driver_sex")
    with pytest.raises(ValueError):
        stats.scatter("driver_sex", "weather")


def test_field_rates_use_applicable_required_cases_and_do_not_mix_unknown_damage():
    cases = [
        accident("a", obstacle_exists="무", driver_sex=None),
        accident("b", obstacle_exists="유", obstacle_vehicle_types=["승용"], obstacle_vehicle_state=None,
                 obstacle_state="정적", driver_sex="삭제된 선택지"),
        accident("c", obstacle_exists="유", obstacle_pedestrians=["어른"], obstacle_state="동적",
                 incident_damage_unknown="확인 가능", incident_minor_injury="0", incident_severe_injury="0",
                 incident_fatality="0", incident_property_damage="0"),
    ]
    report = engine(cases).quality()
    rates = {rate.field_key: rate for rate in report.field_rates}
    sex = rates["driver_sex"]
    assert (sex.eligible, sex.answered, sex.missing, sex.invalid) == (3, 1, 1, 1)
    assert sex.rate == pytest.approx(100 / 3)
    assert rates["obstacle_vehicle_state"].eligible == 1
    assert rates["incident_minor_injury"].eligible == 1
    assert rates["incident_minor_injury"].rate == 100
    assert "remarks" not in rates
    assert "incident_day" not in rates
    assert all(rate.eligible == rate.answered + rate.missing + rate.invalid for rate in rates.values())


def test_new_analysis_is_read_only_snapshot_and_summary_quartiles_match():
    case = speed_case()
    before = deepcopy(case.to_dict())
    stats = engine([case], {"measured.mp4": 30})
    stats.numeric_groups("driver_sex", TRIGGER_SPEED_KEY)
    stats.scatter(TRIGGER_SPEED_KEY, "driver_exp")
    stats.association("driver_sex", "weather")
    stats.quality()
    assert case.to_dict() == before
    assert stats.is_ordered("driver_age")
    assert not stats.is_ordered("driver_sex")
    assert percentile((1, 2, 3, 4), 0.25) == 1.75
    assert percentile((1, 2, 3, 4), 0.75) == 3.25
