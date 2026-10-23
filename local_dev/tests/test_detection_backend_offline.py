"""Offline SAM 3 adapter coverage without torch/model downloads or a GPU."""

from __future__ import annotations

import json
import os
import sys
from contextlib import contextmanager
from types import ModuleType, SimpleNamespace

import numpy as np
import pytest

from investigator.detection_worker.backend import Sam3Backend, _validate_local_model


class FakeTensor:
    def __init__(self, value, dtype="float32"):
        self.value = np.asarray(value)
        self.dtype = dtype

    def to(self, _target):
        return self

    def detach(self):
        return self

    def float(self):
        return self

    def numpy(self):
        return self.value

    def tolist(self):
        return self.value.tolist()


class FakeInputs(dict):
    def __getattr__(self, key):
        return self[key]

    def to(self, _device):
        return self


@pytest.fixture()
def model_folder(tmp_path):
    folder = tmp_path / "오프라인 모델"
    folder.mkdir()
    files = {
        "config.json": '{"model_type": "sam3"}',
        "processor_config.json": "{}",
        "tokenizer_config.json": "{}",
        "tokenizer.json": "{}",
        "model.safetensors": "fake test weights, never loaded",
    }
    for name, value in files.items():
        (folder / name).write_text(value, encoding="utf-8")
    return folder


@pytest.fixture()
def fake_runtime(monkeypatch):
    runtime = SimpleNamespace(
        cuda=False, bf16=False, processor_loads=[], model_loads=[], contexts=[],
        vision_calls=0, text_calls=[], model_calls=[], token_calls=[], fail_cuda=False,
        text_outputs=[],
    )

    @contextmanager
    def context(name, **kwargs):
        runtime.contexts.append(("enter", name, kwargs))
        yield
        runtime.contexts.append(("exit", name, kwargs))

    torch = ModuleType("torch")
    torch.float32, torch.bfloat16 = "float32", "bfloat16"
    torch.cuda = SimpleNamespace(
        is_available=lambda: runtime.cuda, is_bf16_supported=lambda: runtime.bf16
    )
    torch.zeros = lambda *_args, **_kwargs: None
    torch.inference_mode = lambda: context("inference")
    torch.autocast = lambda **kwargs: context("autocast", **kwargs)

    class FakeProcessor:
        @classmethod
        def from_pretrained(cls, source, **kwargs):
            runtime.processor_loads.append((source, kwargs))
            return cls()

        def __call__(self, *, text=None, images=None, return_tensors=None):
            assert return_tensors == "pt"
            if images is not None:
                return FakeInputs(pixel_values=FakeTensor(images), original_sizes=FakeTensor([[3, 4]]))
            runtime.token_calls.append(text)
            return FakeInputs(input_ids=FakeTensor([1, 2]), attention_mask=FakeTensor([1, 1]))

        def post_process_instance_segmentation(self, _outputs, **kwargs):
            assert kwargs["target_sizes"] == [[3, 4]]
            return [{"masks": FakeTensor(np.ones((1, 3, 4), bool)),
                     "scores": FakeTensor([0.9]), "boxes": FakeTensor([[0, 0, 4, 3]])}]

    class FakeModel:
        @classmethod
        def from_pretrained(cls, source, **kwargs):
            runtime.model_loads.append((source, kwargs))
            instance = cls()
            instance.dtype = kwargs["torch_dtype"]
            return instance

        def to(self, device):
            if device == "cuda" and runtime.fail_cuda:
                raise RuntimeError("test GPU load failure")
            return self

        def eval(self):
            return self

        def parameters(self):
            yield FakeTensor([], self.dtype)

        def get_vision_features(self, **_kwargs):
            runtime.vision_calls += 1
            return object()

        def get_text_features(self, **kwargs):
            runtime.text_calls.append(kwargs)
            features = SimpleNamespace(pooler_output=FakeTensor([[1.0]]))
            runtime.text_outputs.append(features)
            return features

        def __call__(self, **kwargs):
            runtime.model_calls.append(kwargs)
            # Match the installed Transformers 5.20 forward contract, not its
            # outdated text_embeds Tensor annotation/docstring.
            assert isinstance(kwargs["text_embeds"].pooler_output, FakeTensor)
            assert "input_ids" not in kwargs
            return object()

    transformers = ModuleType("transformers")
    transformers.Sam3Processor = FakeProcessor
    transformers.Sam3Model = FakeModel
    pil = ModuleType("PIL")
    pil.Image = SimpleNamespace(fromarray=lambda array: array)
    monkeypatch.setitem(sys.modules, "torch", torch)
    monkeypatch.setitem(sys.modules, "transformers", transformers)
    monkeypatch.setitem(sys.modules, "PIL", pil)
    return runtime


def test_local_model_forces_local_loading_without_mutating_environment(model_folder, fake_runtime):
    before = dict(os.environ)
    backend = Sam3Backend(str(model_folder), "cpu")
    assert backend.device == "cpu"
    assert fake_runtime.processor_loads == [(str(model_folder.resolve()), {"local_files_only": True})]
    assert fake_runtime.model_loads == [(str(model_folder.resolve()), {
        "torch_dtype": "float32", "local_files_only": True,
    })]
    assert dict(os.environ) == before


def test_remote_id_retains_online_loading_options(fake_runtime):
    Sam3Backend("facebook/sam3", "cpu")
    assert fake_runtime.processor_loads == [("facebook/sam3", {})]
    assert fake_runtime.model_loads == [("facebook/sam3", {"torch_dtype": "float32"})]


@pytest.mark.parametrize("missing", [
    "config.json", "processor_config.json", "tokenizer_config.json", "tokenizer.json", "model.safetensors",
])
def test_missing_required_files_report_before_importing_torch(model_folder, monkeypatch, missing):
    (model_folder / missing).unlink()
    monkeypatch.setitem(sys.modules, "torch", None)
    with pytest.raises(ValueError, match="누락") as error:
        Sam3Backend(str(model_folder), "cpu")
    assert missing in str(error.value)


def test_original_meta_checkpoint_explained_clearly(model_folder):
    (model_folder / "model.safetensors").unlink()
    (model_folder / "sam3.pt").write_bytes(b"meta checkpoint")
    with pytest.raises(ValueError, match="Meta 원본 sam3.pt.*Transformers"):
        Sam3Backend(str(model_folder))
    with pytest.raises(ValueError, match="파일이 아닌.*모델 폴더"):
        Sam3Backend(str(model_folder / "sam3.pt"))


def test_explicit_missing_local_path_does_not_become_remote_model(tmp_path):
    with pytest.raises(ValueError, match="로컬 모델 폴더가 없습니다"):
        Sam3Backend(str(tmp_path / "missing"))


def test_legacy_processor_and_tokenizer_layouts(model_folder, fake_runtime):
    (model_folder / "processor_config.json").rename(model_folder / "preprocessor_config.json")
    (model_folder / "tokenizer.json").unlink()
    (model_folder / "vocab.json").write_text("{}", encoding="utf-8")
    (model_folder / "merges.txt").write_text("#version: 0.2", encoding="utf-8")
    Sam3Backend(str(model_folder), "cpu")
    assert fake_runtime.model_loads


@pytest.mark.parametrize("content, message", [
    ("[]", "형식"), ("invalid json", "JSON 파일"),
    ('{"model_type": "sam2"}', "SAM 3 이미지 모델이 아닙니다"),
])
def test_invalid_config_has_actionable_error(model_folder, content, message):
    (model_folder / "config.json").write_text(content, encoding="utf-8")
    with pytest.raises(ValueError, match=message):
        _validate_local_model(model_folder)


@pytest.mark.parametrize("content", [b"", b"version https://git-lfs.github.com/spec/v1\noid sha256:123\n"])
def test_empty_and_git_lfs_pointer_weights_rejected(model_folder, content):
    (model_folder / "model.safetensors").write_bytes(content)
    with pytest.raises(ValueError, match="비어|Git LFS 포인터"):
        _validate_local_model(model_folder)


def make_shards(model_folder, weight_map):
    (model_folder / "model.safetensors").unlink()
    (model_folder / "model.safetensors.index.json").write_text(
        json.dumps({"weight_map": weight_map}), encoding="utf-8"
    )


def test_all_sharded_weights_must_be_copied(model_folder, fake_runtime):
    make_shards(model_folder, {"one": "model-1.safetensors", "two": "model-2.safetensors"})
    (model_folder / "model-1.safetensors").write_bytes(b"first")
    with pytest.raises(ValueError, match="model-2.safetensors"):
        Sam3Backend(str(model_folder), "cpu")
    (model_folder / "model-2.safetensors").write_bytes(b"second")
    Sam3Backend(str(model_folder), "cpu")
    assert fake_runtime.model_loads[0][1]["local_files_only"] is True


@pytest.mark.parametrize("weight_map", [{}, {"one": []}, {"one": "../outside"},
                                         {"one": "..\\outside"}, {"one": "C:\\outside"}])
def test_invalid_or_escaping_shard_index_rejected(model_folder, weight_map):
    make_shards(model_folder, weight_map)
    with pytest.raises(ValueError, match="올바르지"):
        _validate_local_model(model_folder)


@pytest.mark.parametrize("half, dtype", [(False, "float32"), (True, "bfloat16")])
def test_supported_cuda_uses_autocast_and_retains_half_option(fake_runtime, half, dtype):
    fake_runtime.cuda = fake_runtime.bf16 = True
    backend = Sam3Backend(device="cuda", half=half)
    assert fake_runtime.model_loads[0][1]["torch_dtype"] == dtype
    results = backend.predict(np.zeros((3, 4, 3), np.uint8), ["car"], 0.5, 0.5)
    assert ("enter", "autocast", {"device_type": "cuda", "dtype": "bfloat16"}) in fake_runtime.contexts
    assert results["car"][0].mask.shape == (3, 4)
    assert results["car"][0].score == pytest.approx(0.9)


@pytest.mark.parametrize("device, cuda", [("cpu", False), ("cuda", True)])
def test_cpu_or_unsupported_cuda_stays_float32_without_autocast(fake_runtime, device, cuda):
    fake_runtime.cuda = cuda
    backend = Sam3Backend(device=device, half=True)
    backend.predict(np.zeros((3, 4, 3), np.uint8), ["car"], 0.5, 0.5)
    assert fake_runtime.model_loads[0][1]["torch_dtype"] == "float32"
    assert not any(name == "autocast" for _state, name, _kwargs in fake_runtime.contexts)


def test_vision_per_frame_and_text_embeddings_per_prompt_are_reused(fake_runtime):
    backend = Sam3Backend(device="cpu")
    frame = np.zeros((3, 4, 3), np.uint8)
    backend.predict(frame, ["car", "person"], 0.5, 0.5)
    backend.predict(frame, ["person", "car"], 0.5, 0.5)
    assert fake_runtime.vision_calls == 2
    assert fake_runtime.token_calls == ["car", "person"]
    assert len(fake_runtime.text_calls) == 2
    assert len(fake_runtime.model_calls) == 4
    assert fake_runtime.model_calls[0]["text_embeds"] is fake_runtime.model_calls[3]["text_embeds"]
    assert fake_runtime.model_calls[1]["attention_mask"] is fake_runtime.model_calls[2]["attention_mask"]


def test_text_model_output_is_not_unwrapped_to_tensor(fake_runtime):
    backend = Sam3Backend(device="cpu")
    backend.predict(np.zeros((3, 4, 3), np.uint8), ["car"], 0.5, 0.5)
    assert fake_runtime.model_calls[0]["text_embeds"] is fake_runtime.text_outputs[0]


def test_auto_cuda_load_failure_falls_back_offline_and_disables_autocast(model_folder, fake_runtime):
    fake_runtime.cuda = fake_runtime.bf16 = fake_runtime.fail_cuda = True
    backend = Sam3Backend(str(model_folder), "auto", half=True)
    backend.predict(np.zeros((3, 4, 3), np.uint8), ["car"], 0.5, 0.5)
    assert backend.device == "cpu"
    assert "GPU load failure" in backend.fallback_reason
    assert [options["torch_dtype"] for _source, options in fake_runtime.model_loads] == ["bfloat16", "float32"]
    assert all(options["local_files_only"] for _source, options in fake_runtime.model_loads)
    assert not any(name == "autocast" for _state, name, _kwargs in fake_runtime.contexts)


def test_explicit_cuda_failure_is_not_silently_retried_on_cpu(fake_runtime):
    fake_runtime.cuda = fake_runtime.fail_cuda = True
    with pytest.raises(RuntimeError, match="GPU load failure"):
        Sam3Backend(device="cuda")
    assert len(fake_runtime.model_loads) == 1
