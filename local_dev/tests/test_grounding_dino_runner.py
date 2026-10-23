"""Box-only DINO worker integration and backend-specific CLI configuration."""

from __future__ import annotations

import importlib
import json

import pytest
from tests.detection_fixture import video as video

from investigator.detection_worker import __main__ as cli
from investigator.detection_worker.backend import RawInstance, create_backend


class BoxBackend:
    device = "cuda"
    model_id = "fake-grounding-dino"
    fallback_reason = None
    detector_name = "grounding_dino_prompt_detector"
    supports_segmentation = False
    supports_prompt_groups = True

    def __init__(self, *, failures=True):
        self.prepared = []
        self.calls = 0
        self.failures = failures
        self.pending = []

    def prepare(self, prompts, prompt_groups=None):
        self.prepared.append((list(prompts), prompt_groups))

    @property
    def inference_metadata(self):
        return {"precision": "float16", "chunk_size": 8, "chunks": [["car", "van"], ["person"]],
                "box_threshold": .3, "text_threshold": .25}

    def predict(self, frame_rgb, prompts, threshold, mask_threshold):
        self.calls += 1
        output = {prompt: [] for prompt in prompts}
        # Different source prompts within one coarse class must retain its ID.
        output["car" if self.calls % 2 else "van"] = [
            RawInstance(None, .9, (12., 20., 52., 50.)),
        ]
        if self.calls < 3:
            output["person"] = [RawInstance(None, .8, (120., 20., 145., 70.))]
        if self.failures and self.calls == 2:
            self.pending.append({"label": "mystery object", "reason": "unmapped_label",
                                 "chunk_prompts": ["car", "van"], "score": .7,
                                 "box_xyxy": [4., 5., 20., 25.], "frame_index": -1})
        return output

    def drain_mapping_failures(self):
        failures, self.pending = self.pending, []
        return failures


def run_boxes(video, tmp_path, backend):
    path = tmp_path / "dino-result.json"
    code = cli.main([
        "--video", str(video), "--output", str(path), "--classes", "vehicle,pedestrian,building_wall",
        "--start-frame", "4", "--end-frame", "13", "--frame-step", "3",
    ], backend=backend)
    assert code == cli.EXIT_OK
    return json.loads(path.read_text(encoding="utf-8"))


def test_box_runner_receives_model_groups_without_changing_class_tracking(video, tmp_path):
    backend = BoxBackend()
    doc = run_boxes(video, tmp_path, backend)
    assert len(backend.prepared) == 1
    prompts, groups = backend.prepared[0]
    assert prompts == sorted({prompt for group in groups.values() for prompt in group})
    assert set(groups) == {"vehicle", "pedestrian", "building_wall"}
    assert {"car", "van", "bus", "truck", "excavator"} == set(groups["vehicle"])
    assert groups["pedestrian"] == ["person"]
    assert doc["sampling"]["processed_frames"] == [4, 7, 10]
    vehicles = [record for record in doc["detections"] if record["class_key"] == "vehicle"]
    assert [record["source_prompt"] for record in vehicles] == ["car", "van", "car"]
    assert len({record["object_id"] for record in vehicles}) == 1


def test_box_only_runner_does_not_call_area_a_mask_or_invent_a_polygon(video, tmp_path):
    doc = run_boxes(video, tmp_path, BoxBackend())
    vehicles = [record for record in doc["detections"] if record["class_key"] == "vehicle"]
    assert len(vehicles) == 3
    for record in vehicles:
        assert record["bbox_xywh"] == [12., 20., 40., 30.]
        assert record["metadata"]["box_area_px"] == 1200
        assert "mask_area_px" not in record["metadata"]
        assert "polygon" not in record["metadata"]


def test_diagnostics_include_actual_frame_and_summary_includes_zero_classes(video, tmp_path, capsys):
    doc = run_boxes(video, tmp_path, BoxBackend())
    assert doc["mapping_failures"] == [{
        "label": "mystery object", "reason": "unmapped_label", "chunk_prompts": ["car", "van"],
        "score": .7, "box_xyxy": [4., 5., 20., 25.], "frame_index": 7,
    }]
    assert doc["detection_summary"] == {
        "processed_frames": 3,
        "coarse_detection_counts": {"vehicle": 3, "pedestrian": 2, "building_wall": 0},
        "label_mapping_failure_count": 1,
    }
    events = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line]
    warnings = [event for event in events if event["type"] == "warning"]
    assert len(warnings) == 1
    assert warnings[0]["code"] == "label_mapping_failure"
    assert warnings[0]["frame_index"] == 7 and warnings[0]["count"] == 1


def test_no_mapping_failures_are_explicitly_saved_as_zero(video, tmp_path):
    doc = run_boxes(video, tmp_path, BoxBackend(failures=False))
    assert doc["mapping_failures"] == []
    assert doc["detection_summary"]["label_mapping_failure_count"] == 0


def test_provenance_records_actual_box_capability_precision_and_chunks(video, tmp_path):
    doc = run_boxes(video, tmp_path, BoxBackend())
    provenance = doc["provenance"]
    assert provenance["detector"] == "grounding_dino_prompt_detector"
    assert provenance["supports_segmentation"] is False
    assert provenance["precision"] == "float16"
    assert provenance["chunks"] == [["car", "van"], ["person"]]
    assert provenance["chunk_size"] == 8
    assert provenance["box_threshold"] == .3
    assert provenance["text_threshold"] == .25


@pytest.mark.parametrize(("backend_name", "flags", "half"), [
    ("grounding_dino", [], True), ("grounding_dino", ["--no-half"], False),
    ("grounding_dino", ["--half"], True), ("sam3", [], False),
    ("efficientsam3", [], False), ("yoloe", [], False),
    ("sam3", ["--half"], True), ("yoloe", ["--no-half"], False),
])
def test_cli_uses_dino_specific_precision_default_and_forwards_new_options(
    tmp_path, monkeypatch, backend_name, flags, half,
):
    from investigator.detection_worker import backend as factory_module

    clip = tmp_path / "clip.mp4"
    clip.write_bytes(b"fake-video-unused-by-mocked-run")
    calls = []

    def fake_factory(model_id, device, requested_half, backend, **kwargs):
        calls.append((model_id, device, requested_half, backend, kwargs))
        return BoxBackend()

    monkeypatch.setattr(factory_module, "create_backend", fake_factory)
    monkeypatch.setattr(cli, "run", lambda *args: {"detections": []})
    code = cli.main([
        "--video", str(clip), "--output", str(tmp_path / "out.json"),
        "--backend", backend_name, "--model", "chosen-model", "--device", "cuda",
        "--dino-chunk-size", "5", "--dino-box-threshold", ".41",
        "--dino-text-threshold", ".21", *flags,
    ])
    assert code == cli.EXIT_OK
    assert calls == [("chosen-model", "cuda", half, backend_name, {
        "chunk_size": 5, "box_threshold": .41, "text_threshold": .21,
    })]


@pytest.mark.parametrize(("backend_name", "model_id"), [
    ("grounding_dino", "IDEA-Research/grounding-dino-base"), ("sam3", "facebook/sam3"),
])
def test_cli_default_model_id_is_backend_specific(tmp_path, monkeypatch, backend_name, model_id):
    from investigator.detection_worker import backend as factory_module

    clip = tmp_path / "clip.mp4"
    clip.touch()
    models = []

    def fake_factory(model, *args, **kwargs):
        models.append(model)
        return BoxBackend()

    monkeypatch.setattr(factory_module, "create_backend", fake_factory)
    monkeypatch.setattr(cli, "run", lambda *args: {"detections": []})
    assert cli.main(["--video", str(clip), "--output", str(tmp_path / "out.json"),
                     "--backend", backend_name]) == cli.EXIT_OK
    assert models == [model_id]


def test_factory_passes_grounding_dino_only_configuration_to_new_backend(monkeypatch):
    module = importlib.import_module("investigator.detection_worker.grounding_dino_backend")
    calls = []
    sentinel = object()

    def fake_dino(*args, **kwargs):
        calls.append((args, kwargs))
        return sentinel

    monkeypatch.setattr(module, "GroundingDinoBackend", fake_dino)
    assert create_backend("tiny-model", "cuda", True, "grounding_dino", chunk_size=6,
                          box_threshold=.43, text_threshold=.27) is sentinel
    assert calls == [(("tiny-model", "cuda", True), {
        "chunk_size": 6, "box_threshold": .43, "text_threshold": .27,
    })]


@pytest.mark.parametrize(("name", "module_name", "class_name"), [
    ("sam3", "investigator.detection_worker.backend", "Sam3Backend"),
    ("efficientsam3", "investigator.detection_worker.efficient_backend", "EfficientSam3Backend"),
    ("yoloe", "investigator.detection_worker.yoloe_backend", "YoloEBackend"),
])
def test_factory_does_not_leak_dino_options_into_existing_three_argument_backends(
    monkeypatch, name, module_name, class_name,
):
    module = importlib.import_module(module_name)
    calls = []

    def legacy_constructor(model_id, device, half):
        calls.append((model_id, device, half))
        return "legacy-backend"

    monkeypatch.setattr(module, class_name, legacy_constructor)
    assert create_backend("old-model", "cpu", False, name, chunk_size=5,
                          box_threshold=.33, text_threshold=.23) == "legacy-backend"
    assert calls == [("old-model", "cpu", False)]
