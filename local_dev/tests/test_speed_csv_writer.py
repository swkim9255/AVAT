"""The actual companion writer preserves nulls and reads real media metadata."""

from __future__ import annotations

import csv
import io
from copy import deepcopy
from hashlib import sha256
from pathlib import Path

import cv2
import numpy as np
import pytest

import investigator.speed_csv as speed_csv
from investigator.models import InvestigationCase
from investigator.speed_csv import SPEED_CSV_HEADERS, speed_csv_path, write_speed_csv
from investigator.storage import CaseRepository


def test_writer_probes_only_marked_videos_and_preserves_literal_null_and_zero(monkeypatch):
    video = str(Path("marked.mp4"))
    untouched = str(Path("unmarked.mp4"))
    case = InvestigationCase(
        values={"accident_classification": "2020011302572"},
        videos=[video, untouched], active_video_index=1,
        markers_by_video={
            video: {"trigger_frame": 0, "speed_points": [
                {"frame": 0}, {"frame": 30, "distance_m": 10},
            ]},
            untouched: {"trigger_frame": None, "collision_frames": []},
        },
    )
    before = deepcopy(case.to_dict())
    probes = []

    def probe(paths, cache):
        probes.append((paths, cache))
        return {str(Path(video).resolve()): (100, 1, 30.0)}

    monkeypatch.setattr(speed_csv, "probe_video_fps", probe)
    result = io.StringIO(newline="")
    write_speed_csv(result, [case], report_numbers={case.uid: "20200113_007"})
    reader = csv.DictReader(io.StringIO(result.getvalue()))
    rows = list(reader)
    assert reader.fieldnames == list(SPEED_CSV_HEADERS)
    assert probes == [([video], {})]
    assert len(rows) == 3
    assert rows[0]["frame_index"] == "0"
    assert rows[0]["timecode"] == "00:00:00.000"
    assert rows[0]["trigger_speed_estimate_kph"] == "36.0"
    assert rows[1]["segment_speed_kph"] == "null"
    assert rows[2]["previous_point_frame_index"] == "0"
    assert rows[2]["segment_speed_kph"] == "36.0"
    assert all(row["report_accident_number"] == "20200113_007" for row in rows)
    assert all(value != "" for row in rows for value in row.values())
    assert case.to_dict() == before


def test_missing_video_retains_distances_and_frames_but_no_fallback_timing(tmp_path):
    path = str(tmp_path / "missing.mp4")
    case = InvestigationCase(videos=[path], markers_by_video={path: {
        "trigger_frame": 10, "collision_frame": 40,
        "speed_points": [{"frame": 0}, {"frame": 30, "distance_m": 10}],
    }})
    result = io.StringIO(newline="")
    write_speed_csv(result, [case])
    rows = list(csv.DictReader(io.StringIO(result.getvalue())))
    assert len(rows) == 4
    for row in rows:
        for key in ("fps", "time_seconds", "timecode", "segment_speed_kph",
                    "trigger_speed_estimate_kph"):
            assert row[key] == "null"
        assert "FPS" in row["estimate_warnings"]
    assert next(row for row in rows if row["frame_index"] == "30")[
        "distance_from_previous_m"
    ] == "10.0"


def test_writer_escapes_formula_like_free_text(monkeypatch):
    path = str(Path("+formula.mp4"))
    case = InvestigationCase(
        values={"accident_classification": "=SUM(1,2)"},
        videos=[path], markers_by_video={path: {"trigger_frame": 0}},
    )
    monkeypatch.setattr(speed_csv, "probe_video_fps", lambda *_: {})
    result = io.StringIO(newline="")
    write_speed_csv(result, [case], report_numbers={case.uid: "  =unsafe"})
    row = next(csv.DictReader(io.StringIO(result.getvalue())))
    assert row["accident_classification"] == "'=SUM(1,2)"
    assert row["video_name"] == "'+formula.mp4"
    assert row["report_accident_number"] == "'  =unsafe"


def test_repository_companion_uses_real_fps_and_leaves_video_unchanged(tmp_path):
    video = tmp_path / "2020011302572.avi"
    writer = cv2.VideoWriter(str(video), cv2.VideoWriter_fourcc(*"MJPG"), 10, (32, 32))
    assert writer.isOpened()
    try:
        for _ in range(21):
            writer.write(np.zeros((32, 32, 3), dtype=np.uint8))
    finally:
        writer.release()
    original_hash = sha256(video.read_bytes()).hexdigest()
    case = InvestigationCase(
        values={"accident_classification": "2020011302572"}, videos=[str(video)],
        markers_by_video={str(video): {
            "trigger_frame": 5, "collision_frame": 15,
            "speed_points": [{"frame": 0}, {"frame": 10, "distance_m": 10},
                             {"frame": 20, "distance_m": 10}],
        }},
    )
    repository = CaseRepository(tmp_path / "project")
    destination = repository.export_csv(tmp_path / "export.csv", [case])
    with speed_csv_path(destination).open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    assert len(rows) == 5
    trigger = next(row for row in rows if row["event_type"] == "trigger")
    collision = next(row for row in rows if row["event_type"] == "collision")
    assert float(trigger["fps"]) == pytest.approx(10)
    assert float(trigger["time_seconds"]) == pytest.approx(.5)
    assert float(trigger["trigger_speed_estimate_kph"]) == pytest.approx(36)
    assert float(collision["time_seconds"]) == pytest.approx(1.5)
    assert collision["trigger_speed_estimate_kph"] == "null"
    assert sha256(video.read_bytes()).hexdigest() == original_hash
    assert speed_csv_path(destination).read_bytes().startswith(b"\xef\xbb\xbf")
