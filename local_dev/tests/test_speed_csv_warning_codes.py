"""Companion warnings are compact; missing-distance indices stay numeric."""

from __future__ import annotations

import csv
import io
import json
from copy import deepcopy

import pytest

import investigator.speed_csv as speed_csv
from investigator.models import InvestigationCase
from investigator.speed_csv import SPEED_CSV_HEADERS, iter_speed_rows, write_speed_csv
from investigator.speed_estimation import SpeedEstimate, estimate_speed


def case_with_points(points, *, trigger=15, collision=90):
    video = "warnings.mp4"
    case = InvestigationCase(
        values={"institution": "samsung", "accident_classification": "2020011302572"},
        videos=[video],
        markers_by_video={video: {
            "trigger_frame": trigger,
            "collision_frames": [] if collision is None else [collision],
            "speed_points": [{"frame": frame, "distance_m": distance} for frame, distance in points],
        }},
    )
    return case, video


def rows_for(case, video, *, fps=30):
    return list(iter_speed_rows([case], fps_by_video={} if fps is None else {video: fps}))


def test_missing_points_are_one_based_numeric_and_exclude_baseline():
    case, video = case_with_points([(0, None), (30, None), (60, None)])
    before = deepcopy(case.to_dict())
    rows = rows_for(case, video)
    trigger = next(row for row in rows if row["event_type"] == "trigger")
    assert trigger["estimate_warnings"] == "거리 미입력 | 계산 지점 부족"
    assert trigger["missing_distance_points"] == [2, 3]
    assert all(type(index) is int for index in trigger["missing_distance_points"])
    points = [row for row in rows if row["event_type"] == "speed_point"]
    assert points[0]["estimate_warnings"] is None
    assert points[0]["missing_distance_points"] is None
    assert [row["missing_distance_points"] for row in points[1:]] == [[2], [3]]
    assert all(row["estimate_warnings"] == "거리 미입력" for row in points[1:])
    collision = next(row for row in rows if row["event_type"] == "collision")
    assert collision["estimate_warnings"] is None
    assert collision["missing_distance_points"] is None
    assert case.to_dict() == before


def test_trigger_missing_indices_cover_full_video_not_only_nearest_fit_chain():
    case, video = case_with_points(
        [(0, None), (30, 10), (60, None), (90, 10), (120, None)], trigger=15,
    )
    rows = rows_for(case, video)
    trigger = next(row for row in rows if row["event_type"] == "trigger")
    assert trigger["trigger_speed_estimate_kph"] == 36
    assert trigger["missing_distance_points"] == [3, 5]
    assert trigger["estimate_warnings"] == "거리 미입력"
    assert next(row for row in rows if row["event_type"] == "speed_point" and row["point_index"] == 3)[
        "missing_distance_points"
    ] == [3]


def test_missing_fps_is_short_and_does_not_hide_missing_distance_indices():
    case, video = case_with_points([(0, None), (30, None), (60, 10)])
    rows = rows_for(case, video, fps=None)
    trigger = next(row for row in rows if row["event_type"] == "trigger")
    assert trigger["estimate_warnings"] == "FPS 미확인 | 거리 미입력"
    assert trigger["missing_distance_points"] == [2]
    points = [row for row in rows if row["event_type"] == "speed_point"]
    assert [row["estimate_warnings"] for row in points] == [
        "FPS 미확인", "FPS 미확인 | 거리 미입력", "FPS 미확인",
    ]
    collision = next(row for row in rows if row["event_type"] == "collision")
    assert collision["estimate_warnings"] == "FPS 미확인"
    assert collision["missing_distance_points"] is None


@pytest.mark.parametrize(
    ("points", "trigger", "expected"),
    [
        ([(0, None)], 0, "계산 지점 부족"),
        ([(0, None), (1, 1)], 0, "짧은 구간"),
        ([(100, None), (130, 8)], 10, "범위 밖 추정"),
        ([(0, None), (30, 1), (36, 20)], 33, "속도 변화 큼"),
        ([(100, None), (130, 2), (160, 4)], 0, "범위 밖 추정 | 속도 계산 불가"),
    ],
)
def test_real_estimation_warning_categories_are_compact(points, trigger, expected):
    case, video = case_with_points(points, trigger=trigger, collision=None)
    engine_before = estimate_speed(case.speed_points, 30, trigger).warnings
    assert engine_before
    row = next(row for row in rows_for(case, video) if row["event_type"] == "trigger")
    assert row["estimate_warnings"] == expected
    assert "번" not in row["estimate_warnings"]
    assert "\n" not in row["estimate_warnings"]
    assert estimate_speed(case.speed_points, 30, trigger).warnings == engine_before
    assert all(len(category) <= 9 for category in row["estimate_warnings"].split(" | "))


def test_short_interval_speed_row_uses_same_short_category():
    case, video = case_with_points([(0, None), (1, 1)], trigger=None, collision=None)
    rows = rows_for(case, video)
    assert rows[0]["estimate_warnings"] is None
    assert rows[1]["estimate_warnings"] == "짧은 구간"
    assert rows[1]["segment_speed_kph"] == 108
    assert rows[1]["missing_distance_points"] is None


def test_unknown_engine_warnings_remain_visible_without_exporting_full_prose(monkeypatch):
    case, video = case_with_points([(0, None), (30, 10)], collision=None)
    messages = [
        "미래의 계산 엔진에서 생성한 긴 경고입니다. 2번, 9번 지점과 입력을 확인하세요.",
        "또 다른 알 수 없는 장문의 경고입니다.",
        "지점 시간이 너무 가까워 계산할 수 없습니다.",
    ]

    def fake_estimate(_points, _fps, frame):
        return SpeedEstimate(trigger_frame=frame, warnings=messages)

    monkeypatch.setattr(speed_csv, "estimate_speed", fake_estimate)
    trigger = next(row for row in rows_for(case, video) if row["event_type"] == "trigger")
    assert trigger["estimate_warnings"] == "속도 계산 불가 | 기타 계산 경고"
    assert trigger["missing_distance_points"] is None
    assert messages[0] not in trigger["estimate_warnings"]
    assert "9번" not in trigger["estimate_warnings"]


def test_warning_categories_are_deduplicated_with_fixed_order(monkeypatch):
    case, video = case_with_points([(0, None), (30, None)])
    messages = [
        "T가 지점 범위 밖이라 추정값이 외삽이에요.",
        "거리가 비어 있는 지점이 있어요: 2번",
        "T가 지점 범위에서 멀리 떨어져 있어요.",
        "거리가 입력된 구간이 2개 지점 이상 이어져야 T 속도를 계산해요.",
    ]
    monkeypatch.setattr(speed_csv, "estimate_speed", lambda _points, _fps, frame: SpeedEstimate(
        trigger_frame=frame, warnings=messages,
    ))
    trigger = next(row for row in rows_for(case, video) if row["event_type"] == "trigger")
    assert trigger["estimate_warnings"] == "거리 미입력 | 계산 지점 부족 | 범위 밖 추정"
    assert trigger["missing_distance_points"] == [2]


def test_csv_appends_twentieth_column_with_json_integer_arrays_and_nulls(monkeypatch):
    case, video = case_with_points([(0, None), (30, None), (60, None)])
    monkeypatch.setattr(speed_csv, "probe_video_fps", lambda *_: {video: (100, 1, 30)})
    output = io.StringIO(newline="")
    write_speed_csv(output, [case])
    reader = csv.DictReader(io.StringIO(output.getvalue()))
    rows = list(reader)
    assert len(SPEED_CSV_HEADERS) == 20
    assert SPEED_CSV_HEADERS[-1] == "missing_distance_points"
    assert reader.fieldnames == list(SPEED_CSV_HEADERS)
    trigger = next(row for row in rows if row["event_type"] == "trigger")
    assert trigger["missing_distance_points"] == "[2,3]"
    assert json.loads(trigger["missing_distance_points"]) == [2, 3]
    assert trigger["estimate_warnings"] == "거리 미입력 | 계산 지점 부족"
    points = [row for row in rows if row["event_type"] == "speed_point"]
    assert [row["missing_distance_points"] for row in points] == ["null", "[2]", "[3]"]
    collision = next(row for row in rows if row["event_type"] == "collision")
    assert collision["missing_distance_points"] == "null"
    assert collision["estimate_warnings"] == "null"
    assert speed_csv._csv_text([]) == "null"
    assert all("번" not in row["missing_distance_points"] for row in rows)


def test_no_warning_and_no_missing_distance_stay_null_after_csv_serialization(monkeypatch):
    case, video = case_with_points([(0, None), (30, 10), (60, 10)], trigger=30)
    monkeypatch.setattr(speed_csv, "probe_video_fps", lambda *_: {video: (100, 1, 30)})
    output = io.StringIO(newline="")
    write_speed_csv(output, [case])
    rows = list(csv.DictReader(io.StringIO(output.getvalue())))
    assert all(row["estimate_warnings"] == "null" for row in rows)
    assert all(row["missing_distance_points"] == "null" for row in rows)


def test_missing_indices_are_local_to_each_video_and_independent_of_fps():
    case, first = case_with_points([(0, None), (30, None), (60, 10)])
    second = "second.mp4"
    case.videos.append(second)
    case.markers_by_video[second] = {
        "trigger_frame": 0,
        "speed_points": [{"frame": 0}, {"frame": 30, "distance_m": 10}, {"frame": 60}],
    }
    triggers = [row for row in iter_speed_rows([case], fps_by_video={first: 30})
                if row["event_type"] == "trigger"]
    assert [row["missing_distance_points"] for row in triggers] == [[2], [3]]
    assert triggers[1]["estimate_warnings"] == "FPS 미확인 | 거리 미입력"
