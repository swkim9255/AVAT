"""Lightweight strict conversion safety checks; no model downloads or torch load."""

from __future__ import annotations

import gzip
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

SCRIPT = Path(__file__).parents[1] / "tools" / "convert_sam3_native.py"
SPEC = importlib.util.spec_from_file_location("sam3_conversion_under_test", SCRIPT)
conversion = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(conversion)


def test_unwrap_supported_state_dicts():
    state = {"weight": np.zeros((2, 3))}
    assert conversion.unwrap_state_dict(state) is state
    assert conversion.unwrap_state_dict({"model": state}) is state
    assert conversion.unwrap_state_dict({"state_dict": state}) is state


@pytest.mark.parametrize("value", [None, [], {}, {1: np.zeros(1)}, {"model": {}}])
def test_reject_invalid_state_dicts(value):
    with pytest.raises(ValueError):
        conversion.unwrap_state_dict(value)


@pytest.mark.parametrize("detector,tracker", [("detector.", "tracker."), ("sam3_model.", "sam2_predictor.")])
def test_select_detector_removes_known_tracker(detector, tracker):
    tensor = np.zeros((1,))
    selected, count = conversion.select_detector_weights(
        {detector + "backbone.weight": tensor, tracker + "weight": tensor}
    )
    assert list(selected) == ["backbone.weight"]
    assert selected["backbone.weight"] is tensor
    assert count == 1


@pytest.mark.parametrize(
    "keys",
    [
        ("detector.weight", "sam3_model.weight"),
        ("detector.weight", "unknown.weight"),
        ("tracker.weight",),
    ],
)
def test_reject_unknown_or_ambiguous_namespaces(keys):
    with pytest.raises(ValueError):
        conversion.select_detector_weights(dict.fromkeys(keys, np.zeros(1)))


def test_unwrapped_detector_is_not_silently_filtered():
    selected, count = conversion.select_detector_weights({"unknown.weight": np.zeros(1)})
    assert list(selected) == ["unknown.weight"]
    assert count == 0
    with pytest.raises(ValueError, match="unexpected"):
        conversion.validate_weights(selected, {})


def test_strict_validation_allows_only_known_precomputed_rope():
    state = {
        "learned": np.zeros((2, 3)),
        "vision_encoder.backbone.layers.2.rotary_emb.rope_embeddings": np.zeros(9),
    }
    omitted = conversion.validate_weights(state, {"learned": np.empty((2, 3))})
    assert len(omitted) == 1
    assert list(state) == ["learned"]


def test_excludes_exact_point_and_sam2_heads_not_used_by_hf_detector():
    point_names = conversion._UNSUPPORTED_POINT_PARAMETERS
    neck_names = conversion._UNSUPPORTED_SAM2_NECK_PARAMETERS
    assert len(point_names) == 6
    assert len(neck_names) == 22
    state = dict.fromkeys(point_names | neck_names, np.zeros(1))
    state["learned"] = np.zeros(2)
    omitted = conversion.validate_weights(state, {"learned": np.zeros(2)})
    assert set(omitted) == point_names | neck_names
    assert set(state) == {"learned"}


@pytest.mark.parametrize(
    "unknown",
    [
        "geometry_encoder.points_direct_project.unknown",
        "geometry_encoder.boxes_direct_project.weight",
        "backbone.vision_backbone.sam2_convs.4.conv_1x1.weight",
        "backbone.vision_backbone.sam2_convs.0.new_layer.weight",
        "backbone.vision_backbone.convs.0.conv_1x1.weight",
    ],
)
def test_unsupported_branch_allowlist_is_not_a_permissive_namespace_filter(unknown):
    with pytest.raises(ValueError, match="unexpected"):
        conversion.validate_weights({unknown: np.zeros(1)}, {})


def test_unsupported_parameter_is_not_dropped_if_future_hf_model_requires_it():
    name = "geometry_encoder.points_direct_project.weight"
    state = {name: np.zeros((2, 3))}
    assert conversion.validate_weights(state, {name: np.zeros((2, 3))}) == []
    assert name in state


@pytest.mark.parametrize("reason", ["missing", "unexpected", "shape"])
def test_strict_validation_never_accepts_missing_learned_weights(reason):
    state = {"weight": np.zeros((2, 3))}
    expected = {"weight": np.zeros((2, 3))}
    if reason == "missing":
        expected["bias"] = np.zeros(2)
    elif reason == "unexpected":
        state["unused_learned.cls_token"] = np.zeros(1)
    else:
        expected["weight"] = np.zeros((3, 2))
    with pytest.raises(ValueError, match="Strict checkpoint validation failed"):
        conversion.validate_weights(state, expected)


def test_conversion_strips_cls_position_and_transposes_clip_projection():
    source_position = np.arange(577 * 4).reshape(1, 577, 4)
    source_projection = np.arange(6).reshape(2, 3)
    names = {
        "old_pos": "vision_encoder.backbone.embeddings.position_embeddings",
        "old_text": "text_encoder.text_projection.weight",
    }
    called = []

    def split(state):
        called.append(True)
        return state

    mapping = SimpleNamespace(convert_old_keys_to_new_keys=lambda keys: names, split_qkv=split)
    state = conversion.convert_detector_weights(
        {"old_pos": source_position, "old_text": source_projection}, mapping
    )
    assert called == [True]
    np.testing.assert_array_equal(state[names["old_pos"]], source_position[:, 1:, :])
    np.testing.assert_array_equal(state[names["old_text"]], source_projection.T)


def test_conversion_rejects_colliding_or_incomplete_mapping():
    mapping = SimpleNamespace(
        convert_old_keys_to_new_keys=lambda keys: {"a": "same", "b": "same"},
        split_qkv=lambda state: state,
    )
    with pytest.raises(ValueError, match="collision"):
        conversion.convert_detector_weights({"a": np.zeros(1), "b": np.zeros(1)}, mapping)
    with pytest.raises(ValueError, match="every original key"):
        conversion.convert_detector_weights({"a": np.zeros(1)}, mapping)


def test_sha256_is_deterministic(tmp_path):
    source = tmp_path / "model.bin"
    source.write_bytes(b"abc")
    assert conversion.sha256_file(source) == "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"


def test_output_is_never_overwritten_even_before_importing_torch(tmp_path):
    output = tmp_path / "existing"
    output.mkdir()
    sentinel = output / "keep.txt"
    sentinel.write_text("keep", encoding="utf-8")
    with pytest.raises(FileExistsError):
        conversion.convert(tmp_path / "no-checkpoint", output, tmp_path / "mapping", None, None)
    assert sentinel.read_text(encoding="utf-8") == "keep"


def test_bpe_reconstructs_clip_ids_without_network(tmp_path):
    source = tmp_path / "original.gz"
    merges = [f"test{number} merged" for number in range(48894)]
    with gzip.open(source, "wt", encoding="utf-8") as stream:
        stream.write("#version: 0.2\n" + "\n".join(merges) + "\n")
    vocab_path, merges_path = conversion.clip_assets_from_bpe(source, tmp_path / "tokenizer")
    vocab = json.loads(vocab_path.read_text(encoding="utf-8"))
    assert len(vocab) == 49408
    assert vocab["!"] == 0
    assert vocab["¡"] == 94
    assert vocab["Ā"] == 188
    assert vocab["<|startoftext|>"] == 49406
    assert vocab["<|endoftext|>"] == 49407
    assert len(merges_path.read_text(encoding="utf-8").splitlines()) == 48895


def test_invalid_bpe_does_not_create_tokenizer_output(tmp_path):
    source = tmp_path / "wrong.gz"
    with gzip.open(source, "wt", encoding="utf-8") as stream:
        stream.write("#version: 0.2\na b\n")
    output = tmp_path / "tokenizer"
    with pytest.raises(ValueError, match="standard CLIP"):
        conversion.clip_assets_from_bpe(source, output)
    assert not output.exists()
