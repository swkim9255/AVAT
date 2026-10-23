"""YOLOE contracts without model downloads, CUDA, torch or Ultralytics."""

from __future__ import annotations

import subprocess
import sys
from contextlib import nullcontext
from types import SimpleNamespace

import numpy as np
import pytest

from investigator.detection_worker import yoloe_backend as module
from investigator.detection_worker.yoloe_backend import (
    YoloEBackend,
    checkpoint_sha256,
    local_checkpoint,
    read_embeddings,
)


class Tensor:
    def __init__(self, values):
        self.values = np.asarray(values)

    @property
    def shape(self):
        return self.values.shape

    @property
    def ndim(self):
        return self.values.ndim

    def detach(self):
        return self

    def cpu(self):
        return self

    def float(self):
        return self

    def to(self, *_):
        return self

    def numpy(self):
        return self.values


class Boxes:
    def __init__(self, indices, scores=None):
        self.cls = Tensor(indices)
        self.conf = Tensor(scores if scores is not None else [0.8] * len(indices))
        self.xyxy = Tensor([[0, 0, 4, 3]] * len(indices))

    def __len__(self):
        return len(self.cls.values)


def result(indices=(1, 0), shape=(3, 4)):
    masks = np.zeros((len(indices), *shape), dtype=np.uint8)
    masks[:, 1:, 1:3] = 1
    return SimpleNamespace(names={0: "person", 1: "car"}, orig_shape=shape,
                           boxes=Boxes(indices), masks=SimpleNamespace(data=Tensor(masks)))


def backend(tmp_path, monkeypatch):
    checkpoint = tmp_path / "yoloe-11l-seg.pt"
    checkpoint.write_bytes(b"fake-test-weights")
    (tmp_path / "mobileclip_blt.ts").write_bytes(b"fake-test-text-encoder")
    prepare_calls, predict_calls, class_calls = [], [], []

    def get_text_pe(prompts, **kwargs):
        prepare_calls.append((list(prompts), kwargs))
        return Tensor(np.zeros((1, len(prompts), 8), dtype=np.float32))

    model = SimpleNamespace(
        model=SimpleNamespace(get_text_pe=get_text_pe),
        set_classes=lambda *args: class_calls.append(args),
        predict=lambda **kwargs: predict_calls.append(kwargs) or [result()],
    )
    instance = object.__new__(YoloEBackend)
    instance._checkpoint = checkpoint
    instance._runs_dir = tmp_path / "ultralytics" / "runs"
    instance.device = "cpu"
    instance._half = False
    instance._prompts = None
    instance.model = model
    instance._torch = SimpleNamespace(
        inference_mode=nullcontext,
        from_numpy=Tensor,
        isfinite=lambda features: np.isfinite(features.values),
    )
    monkeypatch.setattr(module, "_local_text_encoder", lambda *args: object())
    return instance, prepare_calls, predict_calls, class_calls


def write_cache(path, digest, prompts=("car", "person"), embeddings=None, **changes):
    data = {
        "format_version": np.asarray(1, dtype=np.int64),
        "model_sha256": np.asarray(digest),
        "prompts": np.asarray(prompts, dtype=np.str_),
        "embeddings": np.asarray(embeddings) if embeddings is not None else
        np.arange(len(prompts) * 8, dtype=np.float32).reshape(1, len(prompts), 8),
    }
    data.update(changes)
    np.savez(path, **data)


def test_local_checkpoint_missing_never_downloads(tmp_path):
    with pytest.raises(ValueError, match="자동 다운로드"):
        local_checkpoint(tmp_path / "yoloe-11l-seg.pt")
    checkpoint = tmp_path / "checkpoint.pt"
    checkpoint.write_bytes(b"version https://git-lfs.github.com/spec/v1")
    with pytest.raises(ValueError, match="Git LFS"):
        local_checkpoint(checkpoint)


def test_backend_module_import_is_lightweight():
    child = subprocess.run([sys.executable, "-c", "import sys; "
                            "import investigator.detection_worker.yoloe_backend; "
                            "assert not {'torch', 'ultralytics', 'clip', 'PySide6'} & sys.modules.keys()"],
                           capture_output=True, text=True, check=False)
    assert child.returncode == 0, child.stderr


def test_prepare_once_single_inference_each_frame_rgb_to_bgr(tmp_path, monkeypatch):
    instance, prepared, predicted, classes = backend(tmp_path, monkeypatch)
    rgb = np.full((3, 4, 3), [10, 20, 30], dtype=np.uint8)
    instance.prepare(["person", "car"])
    for _ in range(3):
        out = instance.predict(rgb, ["person", "car"], 0.35, 0.5)
    assert len(prepared) == len(classes) == 1
    assert prepared[0] == (["person", "car"], {"cache_clip_model": True})
    assert len(predicted) == 3
    assert predicted[0]["retina_masks"] is True
    assert predicted[0]["agnostic_nms"] is False and predicted[0]["iou"] == 1.0
    assert predicted[0]["conf"] == 0.35
    # 빈 예측 폴더는 앱 폴더가 아니라 이 프로세스의 임시 설정 폴더 아래 한 곳만 쓴다.
    assert all(call["project"] == str(tmp_path / "ultralytics" / "runs") for call in predicted)
    assert all(call["name"] == "predict" and call["exist_ok"] is True for call in predicted)
    np.testing.assert_array_equal(predicted[0]["source"][0, 0], [30, 20, 10])
    assert rgb[0, 0].tolist() == [10, 20, 30]
    assert out["car"][0].mask.shape == (3, 4) and out["car"][0].mask.dtype == bool
    assert out["person"][0].box_xyxy == (0, 0, 4, 3)


def test_changing_prompt_order_after_prepare_is_rejected(tmp_path, monkeypatch):
    instance, prepared, _, _ = backend(tmp_path, monkeypatch)
    instance.prepare(["person", "car"])
    with pytest.raises(ValueError, match="순서"):
        instance.prepare(["car", "person"])
    assert len(prepared) == 1


@pytest.mark.parametrize("prompts", [[], [""], ["car", "car"], [None]])
def test_invalid_prompt_lists(tmp_path, monkeypatch, prompts):
    instance, _, _, _ = backend(tmp_path, monkeypatch)
    with pytest.raises(ValueError):
        instance.prepare(prompts)


def test_missing_text_assets_fail_without_network(tmp_path, monkeypatch):
    instance, prepared, predicted, _ = backend(tmp_path, monkeypatch)
    (tmp_path / "mobileclip_blt.ts").unlink()
    with pytest.raises(ValueError, match="자동 다운로드하지"):
        instance.prepare(["person", "car"])
    assert not prepared and not predicted


def test_cache_bound_to_model_subsets_in_requested_order(tmp_path):
    checkpoint = tmp_path / "weights.pt"
    checkpoint.write_bytes(b"weights")
    cache = checkpoint.with_suffix(module.EMBEDDING_SUFFIX)
    write_cache(cache, checkpoint_sha256(checkpoint))
    features = read_embeddings(cache, checkpoint_sha256(checkpoint), ["person", "car"])
    assert features.shape == (1, 2, 8)
    assert features[0, 0, 0] == 8 and features[0, 1, 0] == 0
    assert read_embeddings(cache, checkpoint_sha256(checkpoint), ["car"]).shape == (1, 1, 8)


@pytest.mark.parametrize("changes", [
    {"model_sha256": np.asarray("wrong-model")},
    {"format_version": np.asarray(2)},
    {"prompts": np.asarray(["car", "car"])},
    {"embeddings": np.full((1, 2, 8), np.nan, np.float32)},
    {"embeddings": np.zeros((2, 8), np.float32)},
    {"embeddings": np.zeros((1, 2, 8), np.float64)},
])
def test_invalid_cache_rejected(tmp_path, changes):
    cache = tmp_path / "embeddings.npz"
    write_cache(cache, "digest", **changes)
    with pytest.raises(ValueError, match="캐시"):
        read_embeddings(cache, "digest", ["car", "person"])


def test_cached_embeddings_skip_encoder_and_get_text_pe(tmp_path, monkeypatch):
    instance, prepared, _, classes = backend(tmp_path, monkeypatch)
    write_cache(instance._checkpoint.with_suffix(module.EMBEDDING_SUFFIX),
                checkpoint_sha256(instance._checkpoint))
    (tmp_path / "mobileclip_blt.ts").unlink()
    instance.prepare(["person", "car"])
    assert not prepared and len(classes) == 1
    assert classes[0][1].values[0, 0, 0] == 8


def test_binary_mask_threshold_is_not_fake_probability_control(tmp_path, monkeypatch):
    instance, prepared, predicted, _ = backend(tmp_path, monkeypatch)
    with pytest.raises(ValueError, match="0.5만"):
        instance.predict(np.zeros((3, 4, 3), np.uint8), ["person", "car"], 0.4, 0.9)
    assert not prepared and not predicted


@pytest.mark.parametrize("bad", ["class", "names", "mask_shape", "mask_value", "score", "missing_mask"])
def test_invalid_result_rejected(bad):
    item = result()
    if bad == "class":
        item.boxes.cls = Tensor([8, 0])
    elif bad == "names":
        item.names = {0: "car", 1: "person"}
    elif bad == "mask_shape":
        item.masks.data = Tensor(np.ones((2, 6, 8), bool))
    elif bad == "mask_value":
        item.masks.data = Tensor(np.full((2, 3, 4), 0.7))
    elif bad == "score":
        item.boxes.conf = Tensor([np.nan, 0.8])
    elif bad == "missing_mask":
        item.masks = None
    with pytest.raises(ValueError):
        YoloEBackend._convert_result(item, ["person", "car"], (3, 4))


def test_empty_result_and_exact_prompt_class_id_mapping():
    item = result(indices=())
    item.masks = None
    assert YoloEBackend._convert_result(item, ["person", "car"], (3, 4)) == {"person": [], "car": []}
    item = result(indices=(1, 1, 0))
    out = YoloEBackend._convert_result(item, ["person", "car"], (3, 4))
    assert len(out["car"]) == 2 and len(out["person"]) == 1
