"""Worker flow with a fake backend (no GPU or model)."""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest
from fake_detection_worker import FakeBackend

from investigator.detection_worker import __main__ as cli
from investigator.detection_worker.classes import load_config
from investigator.detection_worker.postprocess import build_detections, mask_to_bbox
from investigator.detection_worker.runner import RunOptions, plan_frames
from investigator.detection_worker.tracking import SimpleTracker
from investigator.object_detection import DETECTION_CLASSES


def _events(capsys) -> list[dict]:  # type: ignore[no-untyped-def]
    return [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.strip()]


def test_default_config_is_the_union_class_set():
    config = load_config(None)
    assert [item.key for item in config.enabled()] == [item.key for item in DETECTION_CLASSES]


def test_plan_frames_with_frame_step():
    opts = RunOptions(Path("x"), Path("y"), start_frame=3, end_frame=12, frame_step=4)
    assert plan_frames(30, 10.0, opts) == [3, 7, 11]
    single = RunOptions(Path("x"), Path("y"), start_frame=5, end_frame=6, frame_step=1)
    assert plan_frames(30, 10.0, single) == [5]


def test_mask_bbox():
    mask = np.zeros((240, 320), bool)
    mask[20:60, 10:40] = True
    assert mask_to_bbox(mask) == (10.0, 20.0, 30.0, 40.0)


def test_suppression_and_threshold():
    config = load_config(None)
    raw = FakeBackend().predict(
        np.zeros((240, 320, 3), np.uint8),
        ["car", "tree", "building wall", "wall", "guardrail"],
        0.3,
        0.5,
    )
    detections = build_detections(raw, config.enabled(), config, (240, 320))
    assert sorted(d.class_key for d in detections) == ["building_wall", "passenger", "streettree"]


def test_tracker_keeps_id_for_moving_box():
    tracker = SimpleTracker()
    a = tracker.update(0, [("passenger", (20, 40, 60, 40))])
    b = tracker.update(1, [("passenger", (24, 40, 60, 40))])
    assert a == b


def test_range_run(video, tmp_path, capsys):  # type: ignore[no-untyped-def]
    out = tmp_path / "result.json"
    code = cli.main(
        [
            "--video",
            str(video),
            "--output",
            str(out),
            "--classes",
            "passenger,streettree",
            "--start-frame",
            "4",
            "--end-frame",
            "13",
            "--frame-step",
            "3",
        ],
        backend=FakeBackend(),
    )
    assert code == cli.EXIT_OK
    events = _events(capsys)
    assert events[-1]["type"] == "completed"
    document = json.loads(out.read_text(encoding="utf-8"))
    assert document["sampling"]["processed_frames"] == [4, 7, 10]
    cars = [d for d in document["detections"] if d["class_key"] == "passenger"]
    assert len(cars) == 3 and len({d["object_id"] for d in cars}) == 1


def test_unknown_class_is_usage_error(video, tmp_path, capsys):  # type: ignore[no-untyped-def]
    code = cli.main(
        ["--video", str(video), "--output", str(tmp_path / "r.json"), "--classes", "rocket"]
    )
    assert code == cli.EXIT_USAGE
    assert _events(capsys)[-1]["type"] == "error"


def test_cancel_leaves_no_result(video, tmp_path, capsys):  # type: ignore[no-untyped-def]
    out = tmp_path / "result.json"
    flag = tmp_path / "cancel.flag"
    backend = FakeBackend(on_call=lambda n: n == 2 and flag.touch())
    code = cli.main(
        [
            "--video",
            str(video),
            "--output",
            str(out),
            "--frame-step",
            "1",
            "--cancel-file",
            str(flag),
        ],
        backend=backend,
    )
    assert code == cli.EXIT_CANCELLED
    assert not out.exists()


@pytest.mark.parametrize("bad", ["0", "-2"])
def test_invalid_frame_step(video, tmp_path, bad):  # type: ignore[no-untyped-def]
    code = cli.main(
        ["--video", str(video), "--output", str(tmp_path / "r.json"), "--frame-step", bad]
    )
    assert code in {cli.EXIT_USAGE, cli.EXIT_ERROR}
