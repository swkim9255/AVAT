"""Pure timeline CSV rows preserve UI semantics without changing live cases."""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path

import pytest

from investigator.models import InvestigationCase
from investigator.speed_csv import SPEED_CSV_HEADERS, iter_speed_rows


def make_case(videos: list[str] | None = None, *, number: str = "2020011302572"):
    return InvestigationCase(
        values={"institution": "samsung", "accident_classification": number},
        videos=videos or [],
    )


def marker(*, trigger=None, collision=None, points=()):
    return {
        "trigger_frame": trigger,
        "collision_frames": [] if collision is None else [collision],
        "speed_points": [
            {"frame": frame, "distance_m": distance} for frame, distance in points
        ],
    }


def test_constant_speed_rows_match_preceding_segments_and_trigger_estimate():
    video = "constant.mp4"
    case = make_case([video])
    case.markers_by_video[video] = marker(
        trigger=40, collision=60,
        points=[(0, None), (15, 5), (30, 5), (45, 5), (60, 5)],
    )
    rows = list(iter_speed_rows([case], fps_by_video={video: 30.0}))
    assert len(rows) == 7
    assert all(tuple(row) == SPEED_CSV_HEADERS for row in rows)
    points = [row for row in rows if row["event_type"] == "speed_point"]
    assert [row["point_index"] for row in points] == [1, 2, 3, 4, 5]
    assert [row["segment_speed_kph"] for row in points] == [None, 36, 36, 36, 36]
    assert [row["distance_from_previous_m"] for row in points] == [None, 5, 5, 5, 5]
    assert [row["previous_point_frame_index"] for row in points] == [None, 0, 15, 30, 45]
    assert [row["previous_point_time_seconds"] for row in points] == [None, 0, 0.5, 1, 1.5]
    trigger = next(row for row in rows if row["event_type"] == "trigger")
    assert trigger["trigger_speed_estimate_kph"] == pytest.approx(36)
    assert trigger["segment_speed_kph"] is None
    assert trigger["time_seconds"] == pytest.approx(40 / 30, abs=1e-6)
    assert trigger["timecode"] == "00:00:01.333"
    assert trigger["estimate_method"] == "등가속도 근사"
    collision = next(row for row in rows if row["event_type"] == "collision")
    assert collision["time_seconds"] == 2
    assert collision["timecode"] == "00:00:02.000"
    assert collision["trigger_speed_estimate_kph"] is None
    assert collision["segment_speed_kph"] is None
    assert collision["point_index"] is None


def test_acceleration_distinguishes_point_average_from_instantaneous_trigger_speed():
    video = "acceleration.mp4"
    case = make_case([video])
    case.markers_by_video[video] = marker(
        trigger=30, points=[(0, None), (30, 6), (60, 8)],
    )
    rows = list(iter_speed_rows([case], fps_by_video={video: 30}))
    points = [row for row in rows if row["event_type"] == "speed_point"]
    assert [row["segment_speed_kph"] for row in points] == [None, 21.6, 28.8]
    assert all(row["trigger_speed_estimate_kph"] is None for row in points)
    trigger = next(row for row in rows if row["event_type"] == "trigger")
    assert trigger["trigger_speed_estimate_kph"] == pytest.approx(25.2)
    assert trigger["distance_from_previous_m"] is None


@pytest.mark.parametrize("fps", [None, 0, -30, float("nan"), float("inf"), "bad"])
def test_missing_or_invalid_fps_keeps_frames_distances_but_does_not_assume_30(fps):
    video = "unknown-fps.mp4"
    case = make_case([video])
    case.markers_by_video[video] = marker(
        trigger=0, collision=30, points=[(0, None), (30, 10)],
    )
    metadata = {} if fps is None else {video: fps}
    rows = list(iter_speed_rows([case], fps_by_video=metadata))
    assert len(rows) == 4
    assert {row["frame_index"] for row in rows} == {0, 30}
    for row in rows:
        assert row["display_frame"] == row["frame_index"] + 1
        for column in (
            "fps", "time_seconds", "timecode", "previous_point_time_seconds",
            "segment_speed_kph", "trigger_speed_estimate_kph",
        ):
            assert row[column] is None
        assert "FPS" in row["estimate_warnings"]
    assert rows[-1]["previous_point_frame_index"] == 0
    assert rows[-1]["distance_from_previous_m"] == 10


def test_missing_distance_does_not_discard_other_known_segment_speeds():
    video = "missing-distance.mp4"
    case = make_case([video])
    case.markers_by_video[video] = marker(
        points=[(0, None), (30, 10), (60, None), (90, 10)],
    )
    rows = list(iter_speed_rows([case], fps_by_video={video: 30}))
    assert [row["segment_speed_kph"] for row in rows] == [None, 36, None, 36]
    assert rows[2]["previous_point_frame_index"] == 30
    assert rows[2]["distance_from_previous_m"] is None
    assert "거리" in rows[2]["estimate_warnings"]
    assert rows[3]["previous_point_frame_index"] == 60


def test_every_video_is_exported_without_changing_active_video_or_case_payload():
    videos = ["first.mp4", "second.mp4"]
    case = make_case(videos)
    case.active_video_index = 1
    case.markers_by_video[videos[0]] = marker(trigger=24, points=[(0, None), (24, 5)])
    case.markers_by_video[videos[1]] = marker(collision=30, points=[(0, None), (30, 10)])
    before = deepcopy(case.to_dict())
    rows = list(iter_speed_rows([case], fps_by_video={videos[0]: 24, videos[1]: 30}))
    assert {row["video_file"] for row in rows} == set(videos)
    assert [row["video_name"] for row in rows] == [
        "first.mp4", "first.mp4", "first.mp4",
        "second.mp4", "second.mp4", "second.mp4",
    ]
    assert [row["fps"] for row in rows] == [24] * 3 + [30] * 3
    assert case.to_dict() == before
    assert case.active_video_index == 1


def test_zero_frames_are_preserved_and_same_frame_events_have_stable_role_priority():
    case = make_case(["zero.mp4"])
    case.markers_by_video["zero.mp4"] = marker(
        trigger=0, collision=0, points=[(30, 5), (0, None)],
    )
    rows = list(iter_speed_rows([case], fps_by_video={"zero.mp4": 30}))
    assert [(row["frame_index"], row["event_type"]) for row in rows] == [
        (0, "trigger"), (0, "collision"), (0, "speed_point"), (30, "speed_point"),
    ]
    assert all(row["display_frame"] == 1 for row in rows[:3])
    assert all(row["time_seconds"] == 0 for row in rows[:3])
    assert all(row["timecode"] == "00:00:00.000" for row in rows[:3])


def test_no_video_case_markers_preserve_frames_without_using_fake_case_fps():
    case = make_case()
    case.markers_by_video["__case__"] = marker(
        trigger=0, collision=10, points=[(0, None), (30, 5)],
    )
    rows = list(iter_speed_rows([case], fps_by_video={"__case__": 30}))
    assert len(rows) == 4
    assert all(row["video_file"] is None and row["video_name"] is None for row in rows)
    assert all(row["fps"] is None and row["time_seconds"] is None for row in rows)
    assert rows[-1]["distance_from_previous_m"] == 5
    assert rows[-1]["segment_speed_kph"] is None


def test_no_markers_produces_no_synthetic_rows_and_removed_videos_are_ignored():
    empty = make_case(["empty.mp4"])
    no_video = make_case()
    stale = make_case(["remaining.mp4"])
    stale.markers_by_video["removed.mp4"] = marker(trigger=5, collision=8)
    assert list(iter_speed_rows([empty, no_video, stale], fps_by_video={})) == []


def test_custom_report_mapping_is_used_unchanged_and_source_number_remains_original():
    first = make_case(["first.mp4"], number="2020011302572")
    second = make_case(["second.mp4"], number="2020011300001")
    first.markers_by_video["first.mp4"] = marker(trigger=0)
    second.markers_by_video["second.mp4"] = marker(trigger=0)
    mapping = {first.uid: "custom_099", second.uid: "custom_001"}
    before = dict(mapping)
    rows = list(iter_speed_rows([first, second], fps_by_video={}, report_numbers=mapping))
    assert [row["report_accident_number"] for row in rows] == ["custom_099", "custom_001"]
    assert [row["accident_classification"] for row in rows] == ["2020011302572", "2020011300001"]
    assert [row["case_id"] for row in rows] == [first.display_id, second.display_id]
    assert mapping == before
    assert list(iter_speed_rows([first], fps_by_video={}, report_numbers={}))[0][
        "report_accident_number"
    ] is None


def test_default_report_mapping_uses_whole_input_project_not_iteration_order():
    high = make_case(["high.mp4"], number="2020011302572")
    low = make_case(["low.mp4"], number="2020011300001")
    high.markers_by_video["high.mp4"] = marker(trigger=0)
    low.markers_by_video["low.mp4"] = marker(trigger=0)
    rows = list(iter_speed_rows(iter([high, low]), fps_by_video={}))
    assert [row["report_accident_number"] for row in rows] == ["20200113_002", "20200113_001"]


def test_legacy_collision_list_keeps_earliest_single_valid_frame():
    case = make_case(["legacy.mp4"])
    case.markers_by_video["legacy.mp4"] = {
        "collision_frames": [70, "bad", 0, False, -1, 20, None, 1.5],
    }
    before = deepcopy(case.markers_by_video)
    rows = list(iter_speed_rows([case], fps_by_video={"legacy.mp4": 30}))
    assert len(rows) == 1
    assert rows[0]["event_type"] == "collision"
    assert rows[0]["frame_index"] == 0
    assert case.markers_by_video == before


def test_explicit_single_collision_takes_precedence_over_legacy_plural_values():
    case = make_case(["single.mp4"])
    case.markers_by_video["single.mp4"] = {"collision_frame": 30, "collision_frames": [0, 10]}
    rows = list(iter_speed_rows([case], fps_by_video={"single.mp4": 30}))
    assert len(rows) == 1
    assert rows[0]["frame_index"] == 30


def test_resolved_video_metadata_key_and_millisecond_rollover(tmp_path: Path):
    video = str(tmp_path / "folder" / ".." / "timing.mp4")
    case = make_case([video])
    key = case.videos[0]
    case.markers_by_video[key] = marker(trigger=10799999)
    rows = list(iter_speed_rows([case], fps_by_video={str(Path(key).resolve()): 3000}))
    assert rows[0]["time_seconds"] == pytest.approx(3599.999667)
    assert rows[0]["timecode"] == "01:00:00.000"


def test_raw_row_values_do_not_invent_text_null_sentinels():
    case = make_case(["frames-only.mp4"], number="")
    case.markers_by_video["frames-only.mp4"] = marker(trigger=0)
    row = next(iter_speed_rows([case], fps_by_video={}))
    assert row["accident_classification"] is None
    assert row["report_accident_number"] is None
    assert row["time_seconds"] is None
    assert "null" not in row.values()
