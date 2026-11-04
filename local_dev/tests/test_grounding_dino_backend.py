"""Grounding DINO grouping, grounded-label diagnostics, and box-only API."""

from __future__ import annotations

import json
import sys
from contextlib import nullcontext
from types import SimpleNamespace

import numpy as np
import pytest

import investigator.detection_worker.grounding_dino_backend as backend
from investigator.detection_taxonomy import taxonomy_records
from investigator.detection_worker.grounding_dino_backend import (
    GroundingDinoBackend,
    map_prompt,
    prompt_chunks,
)


@pytest.mark.parametrize(("label", "prompts", "expected"), [
    ("TRUCK.", ["tow truck", "truck"], "truck"),
    ("tow", ["tow truck", "truck"], "tow truck"),
    ("a tow truck", ["tow truck", "truck"], "tow truck"),
    ("lamp", ["utility pole", "street lamp"], "street lamp"),
    ("a traffic sign", ["traffic sign", "wall"], "traffic sign"),
    ("wall", ["building wall", "wall"], "wall"),
    ("utility-pole", ["Utility Pole", "wall"], "Utility Pole"),
])
def test_mapping_prefers_exact_then_specific_token_phrase(label, prompts, expected):
    assert map_prompt(label, prompts) == (expected, None)


@pytest.mark.parametrize(("label", "prompts", "reason"), [
    ("cargo", ["car"], "unmapped_label"),
    ("car truck", ["car", "truck"], "ambiguous_label"),
    ("truck", ["tow truck", "fire truck"], "ambiguous_label"),
    ("unknown", ["car", "truck"], "unmapped_label"),
    (" . ", ["car"], "empty_label"),
    (3, ["car"], "empty_label"),
])
def test_mapping_does_not_guess_ambiguous_or_unknown_labels(label, prompts, reason):
    assert map_prompt(label, prompts) == (None, reason)


def test_custom_atomic_groups_are_not_split_and_unknowns_stay_singletons():
    prompts = ["bus", "wall", "car", "tree", "truck", "person", "custom"]
    chunks = prompt_chunks(prompts, 4, {"vehicle": ["car", "bus", "truck"]})
    assert chunks == (("bus", "car", "truck", "wall"), ("tree", "person", "custom"))
    assert sorted(prompt for chunk in chunks for prompt in chunk) == sorted(prompts)


def test_fixed_taxonomy_groups_stay_together_even_for_sorted_worker_vocabulary():
    records = taxonomy_records()
    prompts = sorted(prompt for record in records for prompt in record["prompts"])
    chunks = prompt_chunks(prompts)
    assert all(len(chunk) <= 8 for chunk in chunks)
    assert sorted(prompt for chunk in chunks for prompt in chunk) == prompts
    for record in records:
        assert sum(bool(set(record["prompts"]) & set(chunk)) for chunk in chunks) == 1


def test_group_larger_than_limit_is_indivisible():
    assert prompt_chunks(["a", "b", "c", "z"], 2, [["a", "b", "c"]]) == (
        ("a", "b", "c"), ("z",),
    )


@pytest.mark.parametrize("prompts", [[], "car", [""], ["car", "CAR"], ["car", "car."], [1]])
def test_invalid_vocabulary_is_rejected(prompts):
    with pytest.raises(ValueError):
        prompt_chunks(prompts)


@pytest.mark.parametrize("size", [0, -1, True, 1.5])
def test_invalid_chunk_size_is_rejected(size):
    with pytest.raises(ValueError):
        prompt_chunks(["car"], size)


def test_groups_cannot_assign_one_prompt_to_two_classes():
    with pytest.raises(ValueError, match="중복"):
        prompt_chunks(["car"], prompt_groups=[["car"], ["CAR"]])


class FakeTensor:
    def __init__(self, name):
        self.name = name
        self.moves = []

    def to(self, value):
        self.moves.append(value)
        return self


class FakeInputs(dict):
    def __init__(self):
        super().__init__(pixel_values=FakeTensor("pixels"), input_ids=FakeTensor("tokens"),
                         pixel_mask=FakeTensor("mask"))
        self.devices = []

    def to(self, device):
        self.devices.append(device)
        return self


class FakeProcessor:
    def __init__(self):
        self.calls = []
        self.post_calls = []
        self.inputs = []

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        self.inputs.append(FakeInputs())
        return self.inputs[-1]

    def post_process_grounded_object_detection(
        self, outputs, input_ids, threshold=.25, text_threshold=.25, target_sizes=None,
    ):
        self.post_calls.append((outputs, input_ids, threshold, text_threshold, target_sizes))
        label = self.calls[-1]["text"].split(".")[0]
        return [{"boxes": np.array([[-2, -3, 30, 40]], dtype=np.float32),
                 "scores": np.array([.8]), "text_labels": [label]}]


@pytest.fixture
def fake_modules(monkeypatch):
    processor = FakeProcessor()
    calls = {"processor_load": [], "model_load": [], "model_inputs": [], "model_to": [],
             "autocast": []}

    class FakeModel:
        def to(self, device):
            calls["model_to"].append(device)
            return self

        def eval(self):
            return self

        def __call__(self, **kwargs):
            calls["model_inputs"].append(kwargs)
            return "model-output"

    model = FakeModel()

    def load_processor(*args, **kwargs):
        calls["processor_load"].append((args, kwargs))
        return processor

    def load_model(*args, **kwargs):
        calls["model_load"].append((args, kwargs))
        return model

    def autocast(**kwargs):
        calls["autocast"].append(kwargs)
        return nullcontext()

    torch = SimpleNamespace(float16="float16", float32="float32", inference_mode=nullcontext,
                            autocast=autocast)
    transformers = SimpleNamespace(
        AutoProcessor=SimpleNamespace(from_pretrained=load_processor),
        AutoModelForZeroShotObjectDetection=SimpleNamespace(from_pretrained=load_model),
    )
    monkeypatch.setitem(sys.modules, "torch", torch)
    monkeypatch.setitem(sys.modules, "transformers", transformers)
    monkeypatch.setattr(backend, "_pick_device", lambda torch, requested: (requested, None))
    return processor, calls


def test_predict_calls_model_once_per_chunk_and_keeps_separate_thresholds(fake_modules):
    processor, calls = fake_modules
    instance = GroundingDinoBackend(device="cuda", chunk_size=1, box_threshold=.31, text_threshold=.22)
    frame = np.zeros((12, 20, 3), np.uint8)
    result = instance.predict(frame, ["Car", "person"], threshold=.99, mask_threshold=.7)
    assert instance.supports_segmentation is False
    assert instance.supports_prompt_groups is True
    assert len(calls["model_inputs"]) == 2
    assert [call["text"] for call in processor.calls] == ["car.", "person."]
    assert all(call["images"] is frame for call in processor.calls)
    assert all(call[2:] == (.31, .22, [(12, 20)]) for call in processor.post_calls)
    assert all(inputs["pixel_values"].moves == ["float16"] for inputs in processor.inputs)
    assert all(inputs["input_ids"].moves == [] for inputs in processor.inputs)
    assert calls["model_load"][0][1] == {"torch_dtype": "float16"}
    assert calls["autocast"] == [{"device_type": "cuda", "dtype": "float16"}]
    assert list(result) == ["Car", "person"]
    assert result["Car"][0].mask is None
    assert result["Car"][0].box_xyxy == (0., 0., 20., 12.)
    assert result["person"][0].score == .8
    assert instance.drain_mapping_failures() == []
    assert instance.inference_metadata == {
        "chunk_size": 1, "chunks": [["Car"], ["person"]], "box_threshold": .31,
        "text_threshold": .22, "precision": "float16", "supports_segmentation": False,
    }


def test_local_tiny_folder_stays_offline_and_cpu_uses_fp32(fake_modules, tmp_path):
    _, calls = fake_modules
    path = tmp_path / "grounding-dino-tiny"
    path.mkdir()
    (path / "config.json").write_text("{}")
    instance = GroundingDinoBackend(str(path), "cpu", half=True)
    assert instance._half is False
    assert calls["processor_load"] == [((str(path.resolve()),), {"local_files_only": True})]
    assert calls["model_load"][0][1] == {"torch_dtype": "float32", "local_files_only": True}


def test_prepare_is_idempotent_but_rejects_runtime_vocabulary_changes(fake_modules):
    instance = GroundingDinoBackend(device="cpu")
    instance.prepare(["car", "bus"])
    instance.prepare(["car", "bus"])
    assert instance.chunks == (("car", "bus"),)
    with pytest.raises(ValueError, match="변경"):
        instance.prepare(["bus", "car"])


def test_failures_preserve_unmapped_boxes_and_are_drained_once(fake_modules, caplog):
    instance = GroundingDinoBackend(device="cpu")
    output = {"car": [], "truck": []}
    instance._convert_result({
        "boxes": np.array([[1, 2, 3, 4], [2, 3, 4, 5], [1, 1, 4, 4]]),
        "scores": np.array([.7, .8, .9]),
        "text_labels": ["unknown", "car truck", "car"],
    }, ["car", "truck"], (10, 10), output)
    assert len(output["car"]) == 1
    assert output["truck"] == []
    records = instance.drain_mapping_failures()
    assert [record["reason"] for record in records] == ["unmapped_label", "ambiguous_label"]
    assert records[0] == {"label": "unknown", "chunk_prompts": ["car", "truck"],
                          "reason": "unmapped_label", "score": .7, "box_xyxy": [1., 2., 3., 4.]}
    json.dumps(records, allow_nan=False)
    assert "unknown" in caplog.text and "car truck" in caplog.text
    assert instance.drain_mapping_failures() == []


@pytest.mark.parametrize("post", [
    {"boxes": np.array([[0, 0, 4, 4]]), "scores": np.array([.7]), "text_labels": []},
    {"boxes": np.array([[0, 0, np.nan, 4]]), "scores": np.array([.7]), "text_labels": ["car"]},
    {"boxes": np.array([[4, 0, 0, 4]]), "scores": np.array([.7]), "text_labels": ["car"]},
    {"boxes": np.array([[0, 0, 4, 4]]), "scores": np.array([1.2]), "text_labels": ["car"]},
])
def test_invalid_model_geometry_or_scores_are_not_silently_used(fake_modules, post):
    instance = GroundingDinoBackend(device="cpu")
    with pytest.raises(ValueError):
        instance._convert_result(post, ["car"], (10, 10), {"car": []})


def test_empty_boxes_are_valid_without_masks(fake_modules):
    instance = GroundingDinoBackend(device="cpu")
    output = {"car": []}
    instance._convert_result({"boxes": np.empty((0, 4)), "scores": np.empty(0),
                              "text_labels": []}, ["car"], (10, 10), output)
    assert output == {"car": []}


def test_transformers_empty_decode_quirk_is_not_a_phantom_detection(fake_modules):
    instance = GroundingDinoBackend(device="cpu")
    output = {"car": []}
    instance._convert_result({"boxes": np.empty((0, 4)), "scores": np.empty(0),
                              "text_labels": [""]}, ["car"], (10, 10), output)
    assert output == {"car": []}
    assert instance.drain_mapping_failures() == []


def test_empty_boxes_with_a_real_label_still_fail_as_mismatched_output(fake_modules):
    instance = GroundingDinoBackend(device="cpu")
    with pytest.raises(ValueError, match="일치"):
        instance._convert_result({"boxes": np.empty((0, 4)), "scores": np.empty(0),
                                  "text_labels": ["car"]}, ["car"], (10, 10), {"car": []})


@pytest.mark.parametrize(("kwargs", "message"), [
    ({"chunk_size": 0}, "청크"), ({"box_threshold": float("nan")}, "박스"),
    ({"text_threshold": 1.1}, "텍스트"), ({"model_id": ""}, "모델"),
])
def test_invalid_options_fail_before_loading_libraries(kwargs, message):
    with pytest.raises(ValueError, match=message):
        GroundingDinoBackend(**kwargs)


def test_legacy_postprocessor_threshold_signature_is_supported(fake_modules):
    processor, _ = fake_modules
    old_method = processor.post_process_grounded_object_detection

    def legacy(outputs, input_ids, box_threshold=.25, text_threshold=.25, target_sizes=None):
        return old_method(outputs, input_ids, box_threshold, text_threshold, target_sizes)

    processor.post_process_grounded_object_detection = legacy
    instance = GroundingDinoBackend(device="cpu", box_threshold=.42)
    instance.predict(np.zeros((10, 10, 3), np.uint8), ["car"], .4, .5)
    assert processor.post_calls[0][2] == .42
