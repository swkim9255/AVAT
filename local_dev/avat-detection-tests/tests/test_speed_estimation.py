import json
import math

import pytest

from investigator.models import InvestigationCase
from investigator.speed_estimation import (
    SpeedPoint,
    estimate_speed,
    points_from_json,
    speed_label,
)


def test_constant_speed_between_points():
    # 10 m/s = 36 km/h; a 5 m reference every 15 frames at 30 fps.
    points = [SpeedPoint(0)] + [SpeedPoint(15 * i, 5.0) for i in range(1, 5)]
    result = estimate_speed(points, 30.0, trigger_frame=40)
    assert [round(s.kph, 6) for s in result.segments] == [36.0] * 4
    assert result.kph == pytest.approx(36.0)
    assert result.kph_low < 36.0 < result.kph_high
    assert result.label == "중속(30-60kph)"
    assert result.method == "구간 평균 속도"
    assert result.points_used == (30, 45)
    assert result.velocity_line is None
    assert not result.warnings


def test_accelerating_vehicle_uses_only_the_segment_containing_t():
    # 가속 중이어도 근사하지 않고 T가 들어 있는 구간(55→70)의 평균 속도만 쓴다.
    fps, v0, accel = 30.0, 5.0, 2.0  # m/s, m/s²
    frames = [0, 20, 38, 55, 70]
    pos = [v0 * (f / fps) + 0.5 * accel * (f / fps) ** 2 for f in frames]
    points = [SpeedPoint(frames[0])] + [
        SpeedPoint(f, pos[i] - pos[i - 1]) for i, f in enumerate(frames) if i
    ]
    result = estimate_speed(points, fps, 62)
    expected = (pos[4] - pos[3]) / ((70 - 55) / fps) * 3.6
    assert result.kph == pytest.approx(expected, rel=1e-9)
    assert result.points_used == (55, 70)
    assert result.trigger_segment == (55, 70)


def test_t_outside_points_is_not_extrapolated():
    points = [SpeedPoint(100), SpeedPoint(130, 8.0)]
    result = estimate_speed(points, 30.0, trigger_frame=10)
    assert result.kph is None and result.label is None
    assert result.trigger_segment is None
    assert result.method == "" and result.points_used == ()
    assert not result.warnings


SPEC_POINTS = [SpeedPoint(100), SpeedPoint(130, 3.0), SpeedPoint(160, 5.0)]


@pytest.mark.parametrize(
    ("trigger", "kph", "segment"),
    [
        (100, 10.8, (100, 130)),  # T가 첫 지점이면 첫 구간
        (130, 10.8, (100, 130)),  # T가 지점과 같으면 그 지점에서 끝나는 앞 구간
        (145, 18.0, (130, 160)),
        (160, 18.0, (130, 160)),
        (99, None, None),
        (161, None, None),
    ],
)
def test_trigger_speed_is_the_average_of_the_segment_containing_t(trigger, kph, segment):
    result = estimate_speed(SPEC_POINTS, 30.0, trigger)
    assert result.trigger_segment == segment
    if kph is None:
        assert result.kph is None and result.points_used == ()
    else:
        assert result.kph == pytest.approx(kph)
        assert result.points_used == segment
        assert result.method == "구간 평균 속도"


def test_trigger_range_is_the_segment_plus_minus_one_frame():
    result = estimate_speed(SPEC_POINTS, 30.0, 100)
    segment = result.segments[0]
    assert (result.kph_low, result.kph_high) == (segment.kph_low, segment.kph_high)
    assert round(result.kph_low, 2) == 10.45
    assert round(result.kph_high, 2) == 11.17


def test_missing_distance_of_t_segment_keeps_segment_but_no_speed():
    points = [SpeedPoint(100), SpeedPoint(130, 3.0), SpeedPoint(160)]
    result = estimate_speed(points, 30.0, 145)
    assert result.kph is None and result.label is None
    assert result.trigger_segment == (130, 160)
    assert any("3번" in w for w in result.warnings)


def test_many_points_still_use_only_two_points_for_t():
    points = [SpeedPoint(0)] + [SpeedPoint(30 * i, 5.0 + i) for i in range(1, 5)]
    result = estimate_speed(points, 30.0, 70)
    assert len(points) == 5
    assert result.points_used == (60, 90)
    assert result.kph == pytest.approx(8.0 * 3.6)


def test_unknown_fps_still_reports_t_segment():
    result = estimate_speed(SPEC_POINTS, 0, 145)
    assert result.kph is None
    assert result.trigger_segment == (130, 160)
    assert any("FPS" in w for w in result.warnings)


def test_missing_distance_breaks_chain_and_is_reported():
    points = [SpeedPoint(0), SpeedPoint(30, 10.0), SpeedPoint(60), SpeedPoint(90, 10.0)]
    result = estimate_speed(points, 30.0, trigger_frame=75)
    assert len(result.segments) == 2
    assert result.points_used == (60, 90)
    assert any("3번" in w for w in result.warnings)


def test_no_trigger_still_gives_segments():
    result = estimate_speed([SpeedPoint(0), SpeedPoint(30, 3.0)], 30.0, None)
    assert result.kph is None and len(result.segments) == 1


@pytest.mark.parametrize(
    ("kph", "label"),
    [
        (0.0, "정차(0kph)"),
        (0.9, "크립(0-10kph)"),
        (1.0, "크립(0-10kph)"),
        (9.99, "크립(0-10kph)"),
        (10.0, "저속(10-30kph)"),
        (30.0, "중속(30-60kph)"),
        (59.9, "중속(30-60kph)"),
        (60.0, "고속(60kph이상)"),
    ],
)
def test_speed_labels(kph, label):
    assert speed_label(kph) == label


def test_label_uncertain_near_boundary():
    # 30 km/h with very short segments: the ±0.5 frame range crosses 30.
    points = [SpeedPoint(0), SpeedPoint(3, 0.8333333), SpeedPoint(6, 0.8333333)]
    result = estimate_speed(points, 30.0, trigger_frame=4)
    assert result.label_uncertain


def test_large_speed_change_does_not_switch_to_another_calculation():
    # 예전의 1g 초과 대체 계산은 없앴다. T 구간(30→36)의 평균 속도를 그대로 쓴다.
    points = [SpeedPoint(0), SpeedPoint(30, 1.0), SpeedPoint(36, 20.0)]
    result = estimate_speed(points, 30.0, trigger_frame=33)
    assert result.method == "구간 평균 속도"
    assert result.kph == pytest.approx(20.0 / (6 / 30.0) * 3.6)
    assert not any("g)" in w or "속도 변화" in w for w in result.warnings)


def test_points_from_json_skips_bad_values():
    raw = [{"frame": "5", "distance_m": None}, {"frame": 2, "distance_m": -1}, "x", {"frame": 9, "distance_m": "4.5"}]
    assert points_from_json(raw) == [SpeedPoint(2, None), SpeedPoint(5, None), SpeedPoint(9, 4.5)]


def test_case_speed_points_insert_remove_and_roundtrip(tmp_path):
    video = tmp_path / "1234567890123.mp4"
    video.write_bytes(b"")
    case = InvestigationCase(videos=[str(video)])
    assert case.toggle_speed_point(10)
    assert case.toggle_speed_point(40)
    case.set_speed_distance(40, 8.0)
    assert case.toggle_speed_point(70)  # inherits 8 m
    assert [p.distance_m for p in case.speed_points] == [None, 8.0, 8.0]
    # Inserting between 10 and 40 clears 40's distance (it was 10→40).
    assert case.toggle_speed_point(25)
    assert [p.distance_m for p in case.speed_points] == [None, None, None, 8.0]
    case.set_speed_distance(25, 8.0)
    case.set_speed_distance(40, 3.0)
    # Removing 25 merges 8 + 3 back into 40.
    assert not case.toggle_speed_point(25)
    assert [(p.frame, p.distance_m) for p in case.speed_points] == [(10, None), (40, 11.0), (70, 8.0)]
    # Removing the first point leaves the new first without a distance.
    case.toggle_speed_point(10)
    assert case.speed_points[0].distance_m is None

    restored = InvestigationCase.from_dict(json.loads(json.dumps(case.to_dict())))
    assert restored.speed_points == case.speed_points
    case.set_trigger(55)
    assert case.speed_points == restored.speed_points  # T edits keep speed points
    case.clear_speed_points()
    assert "speed_points" not in case.markers_by_video[str(video.resolve())]


def test_old_files_without_speed_points_are_unchanged():
    data = {"markers_by_video": {"a.mp4": {"trigger_frame": 3, "collision_frames": [9]}}, "videos": ["a.mp4"]}
    case = InvestigationCase.from_dict(data)
    assert case.to_dict()["markers_by_video"] == {"a.mp4": {"trigger_frame": 3, "collision_frames": [9]}}
    assert math.isclose(0, len(case.speed_points))


def test_high_frame_rates_and_large_frame_offsets_remain_stable():
    for fps, base in ((1000.0, 0), (30.0, 1_000_000_000)):
        step = 1 if fps == 1000 else 15
        distance = step / fps * 10
        points = [SpeedPoint(base)] + [SpeedPoint(base + step * i, distance) for i in range(1, 5)]
        result = estimate_speed(points, fps, base + step * 2)
        assert result.kph == pytest.approx(36.0)


def test_single_frame_interval_has_unbounded_upper_sensitivity():
    result = estimate_speed([SpeedPoint(0), SpeedPoint(1, 1)], 30, 0)
    assert result.kph == pytest.approx(108)
    assert math.isinf(result.kph_high)
    assert result.label_uncertain
    assert result.warnings


def test_t_before_first_point_is_not_reported_as_stationary():
    result = estimate_speed([SpeedPoint(100), SpeedPoint(130, 2), SpeedPoint(160, 4)], 30, 0)
    assert result.kph is None
    assert result.label is None
    assert result.trigger_segment is None


@pytest.mark.parametrize("distance", [0, -1, float('nan'), float('inf')])
def test_invalid_distance_does_not_mutate_case(distance):
    case = InvestigationCase()
    case.toggle_speed_point(0)
    case.toggle_speed_point(30)
    before = case.to_dict()
    with pytest.raises(ValueError):
        case.set_speed_distance(30, distance)
    assert case.to_dict() == before


def test_malformed_persisted_frames_are_skipped_without_losing_valid_points():
    raw = [{'frame': frame, 'distance_m': 1} for frame in (float('inf'), float('nan'), True, 1.5, 'x')]
    raw += [{'frame': 30}, {'frame': 60, 'distance_m': 8}]
    assert points_from_json(raw) == [SpeedPoint(30), SpeedPoint(60, 8)]
