"""Coarse detection, de-duplication and temporal identity contracts."""

from __future__ import annotations

import json

import numpy as np
import pytest

from investigator.detection_taxonomy import taxonomy_hash, taxonomy_version
from investigator.detection_worker import __main__ as cli
from investigator.detection_worker.backend import RawInstance
from investigator.detection_worker.classes import load_config
from investigator.detection_worker.postprocess import build_detections
from investigator.detection_worker.tracking import SimpleTracker
from local_dev.tests.detection_fixture import video as video


def mask(x=0):
    out = np.zeros((240, 320), bool)
    out[20:60, x:x + 40] = True
    return out


def test_defaults_are_fixed_coarse_taxonomy_without_old_prompts():
    config = load_config(None)
    prompts = {prompt for cls in config.classes for prompt in cls.prompts}
    assert len(config.classes) == 21 and len(prompts) == 28
    assert {"vehicle", "two_wheeler", "pedestrian", "bicycle"} <= {c.key for c in config.classes}
    assert {"child", "adult person", "tow truck", "fire truck", "ambulance", "electric kick scooter"}.isdisjoint(prompts)
    assert config.notes == {"taxonomy_version": taxonomy_version(), "taxonomy_hash": taxonomy_hash()}


def test_vehicle_cross_prompt_nms_retains_winner_prompt_and_separate_vehicle():
    config = load_config(None)
    raw = {
        "van": [RawInstance(mask(), 0.8)],
        "bus": [RawInstance(mask(), 0.9)],
        "truck": [RawInstance(mask(), 0.7), RawInstance(mask(180), 0.85)],
    }
    out = build_detections(raw, config.enabled(["vehicle"]), config, (240, 320))
    assert len(out) == 2
    assert [(det.class_key, det.prompt) for det in out] == [("vehicle", "bus"), ("vehicle", "truck")]


def test_vehicle_preserves_point_six_cross_prompt_suppression():
    config = load_config(None)
    # 34/46 ~= .739 and 32/48 ~= .667. Use .667 to distinguish .6 from .7.
    raw = {"car": [RawInstance(mask(0), 0.9)], "truck": [RawInstance(mask(8), 0.8)]}
    out = build_detections(raw, config.enabled(["vehicle"]), config, (240, 320))
    assert len(out) == 1 and out[0].prompt == "car"


def test_separate_coarse_classes_do_not_suppress_one_another():
    config = load_config(None)
    raw = {"car": [RawInstance(mask(), 0.9)], "person": [RawInstance(mask(), 0.8)]}
    out = build_detections(raw, config.enabled(["vehicle", "pedestrian"]), config, (240, 320))
    assert {det.class_key for det in out} == {"vehicle", "pedestrian"}


def test_tracker_retains_geometry_rules_and_coarse_identity():
    tracker = SimpleTracker()
    ids = [tracker.update(frame, [("vehicle", (20 + frame, 40, 60, 40))])[0] for frame in range(3)]
    assert len(set(ids)) == 1
    assert tracker.update(3, [("pedestrian", (23, 40, 60, 40))])[0] != ids[0]


def test_old_explicit_config_supported_without_taxonomy(tmp_path):
    path = tmp_path / "legacy.json"
    path.write_text(json.dumps({"classes": [{"key": "passenger", "prompts": ["car"]}]}), encoding="utf-8")
    config = load_config(path)
    out = build_detections({"car": [RawInstance(mask(), 0.9)]}, config.classes, config, (240, 320))
    assert out[0].class_key == "passenger"
    assert not config.notes


def test_run_prepare_once_raw_prompts_and_recommendation_per_observation(video, tmp_path):
    class Backend:
        device, model_id = "cpu", "test/coarse"

        def __init__(self):
            self.prepare_calls, self.calls = [], 0

        def prepare(self, prompts):
            self.prepare_calls.append(list(prompts))

        def predict(self, _frame, _prompts, _threshold, _mask_threshold):
            prompt = ["van", "bus", "truck"][self.calls]
            self.calls += 1
            return {prompt: [RawInstance(mask(20 + self.calls), 0.9)],
                    "person": [RawInstance(mask(200), 0.8)]}

    backend = Backend()
    output = tmp_path / "result.json"
    assert cli.main(["--video", str(video), "--output", str(output), "--frame-step", "1",
                     "--end-frame", "3", "--classes", "vehicle,pedestrian"], backend=backend) == cli.EXIT_OK
    document = json.loads(output.read_text(encoding="utf-8"))
    assert document["schema_version"] == 2
    assert len(backend.prepare_calls) == 1 and backend.calls == 3
    assert document["provenance"]["taxonomy_hash"] == taxonomy_hash()
    cars = [record for record in document["detections"] if record["model_class_key"] == "vehicle"]
    assert len({record["object_id"] for record in cars}) == 1
    assert [record["source_prompt"] for record in cars] == ["van", "bus", "truck"]
    assert [record["suggested_subtype_key"] for record in cars] == ["van", "van", "truck"]
    people = [record for record in document["detections"] if record["model_class_key"] == "pedestrian"]
    assert all(record["suggested_subtype_key"] is None and record["reviewed"] is False for record in people)


@pytest.mark.parametrize("value", [-1, 1.01, float("nan")])
def test_class_nms_iou_validation(tmp_path, value):
    path = tmp_path / "bad.json"
    path.write_text(json.dumps({"classes": [{"key": "vehicle", "prompts": ["car"], "nms_iou": value}]}), encoding="utf-8")
    with pytest.raises(ValueError):
        load_config(path)
