"""SAM 3 추론 백엔드.

worker 본체는 `PromptBackend` 프로토콜에만 의존하므로 테스트에서는 가짜
백엔드로 바꿔 끼울 수 있다. torch/transformers는 실제 백엔드를 만들 때만
불러온다.
"""

from __future__ import annotations

import json
from contextlib import nullcontext
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath
from typing import Protocol

import numpy as np


@dataclass
class RawInstance:
    mask: np.ndarray | None  # (H, W) bool; box-only backends return None
    score: float
    box_xyxy: tuple[float, float, float, float] | None = None


class PromptBackend(Protocol):
    device: str
    model_id: str

    def predict(
        self,
        frame_rgb: np.ndarray,
        prompts: list[str],
        threshold: float,
        mask_threshold: float,
    ) -> dict[str, list[RawInstance]]:
        """한 프레임에 여러 텍스트 프롬프트를 적용해 프롬프트별 인스턴스를 돌려준다."""
        ...


class Sam3Backend:
    """Hugging Face transformers의 Sam3Model을 쓰는 백엔드."""

    detector_name = "sam3_prompt_detector"

    def __init__(self, model_id: str = "facebook/sam3", device: str = "auto", half: bool = False):
        # 로컬 모델을 지정했으면 라이브러리/모델을 로드하기 전에 불완전한 복사를
        # 안내한다. local_files_only는 전역 HF 환경변수를 바꾸지 않고 다운로드를 막는다.
        local_path = _local_model_path(model_id)
        if local_path is not None:
            _validate_local_model(local_path)
        load_options = {"local_files_only": True} if local_path is not None else {}

        import torch
        from transformers import Sam3Model, Sam3Processor

        self._torch = torch
        self.model_id = model_id
        self.device, self.fallback_reason = _pick_device(torch, device)
        use_bfloat16 = self.device == "cuda" and torch.cuda.is_bf16_supported()
        self._autocast_dtype = torch.bfloat16 if use_bfloat16 else None
        # 기본은 FP32 가중치 + BF16 autocast. 기존 --half는 가중치도 BF16으로
        # 읽는 옵션으로 유지하고, CPU/미지원 GPU에는 FP32를 사용한다.
        dtype = torch.bfloat16 if half and use_bfloat16 else torch.float32
        source = str(local_path) if local_path is not None else model_id
        self.processor = Sam3Processor.from_pretrained(source, **load_options)
        try:
            self.model = Sam3Model.from_pretrained(source, torch_dtype=dtype, **load_options).to(
                self.device
            )
        except RuntimeError as exc:  # CUDA 초기화·메모리 오류 시 CPU로 대체
            if self.device != "cuda" or device == "cuda":
                raise
            self.fallback_reason = f"CUDA 로드 실패, CPU로 대체: {exc}"
            self.device = "cpu"
            self._autocast_dtype = None
            self.model = Sam3Model.from_pretrained(
                source, torch_dtype=torch.float32, **load_options
            ).to("cpu")
        self.model.eval()
        self._dtype = next(self.model.parameters()).dtype
        self._text_cache: dict[str, object] = {}

    def _text_inputs(self, prompt: str):
        cached = self._text_cache.get(prompt)
        if cached is None:
            inputs = self.processor(text=prompt, return_tensors="pt").to(self.device)
            features = self.model.get_text_features(**inputs)
            # 배포 환경의 Transformers 5.20은 forward에서 text_embeds의
            # pooler_output을 읽으므로 get_text_features의 ModelOutput 자체를
            # 재사용해야 한다. Tensor만 추출하면 AttributeError가 발생한다.
            cached = {
                "text_embeds": features,
                "attention_mask": inputs.get("attention_mask"),
            }
            self._text_cache[prompt] = cached
        return cached

    def predict(self, frame_rgb, prompts, threshold, mask_threshold):
        from PIL import Image

        torch = self._torch
        if frame_rgb.ndim != 3 or frame_rgb.shape[2] != 3 or frame_rgb.dtype != np.uint8:
            raise ValueError("SAM 3 입력은 원본 크기의 RGB uint8 프레임이어야 합니다.")
        image = Image.fromarray(frame_rgb)
        img_inputs = self.processor(images=image, return_tensors="pt").to(self.device)
        target_sizes = img_inputs.get("original_sizes").tolist()
        pixel_values = img_inputs.pixel_values.to(self._dtype)
        results: dict[str, list[RawInstance]] = {}
        autocast = (
            torch.autocast(device_type="cuda", dtype=self._autocast_dtype)
            if self._autocast_dtype is not None
            else nullcontext()
        )
        with torch.inference_mode(), autocast:
            # 프레임당 비전 특징은 한 번만 계산하고 프롬프트마다 재사용한다.
            vision_embeds = self.model.get_vision_features(pixel_values=pixel_values)
            for prompt in prompts:
                outputs = self.model(vision_embeds=vision_embeds, **self._text_inputs(prompt))
                post = self.processor.post_process_instance_segmentation(
                    outputs,
                    threshold=threshold,
                    mask_threshold=mask_threshold,
                    target_sizes=target_sizes,
                )[0]
                masks = post["masks"].detach().to("cpu").numpy().astype(bool)
                scores = post["scores"].detach().float().to("cpu").numpy()
                boxes = post["boxes"].detach().float().to("cpu").numpy()
                results[prompt] = [
                    RawInstance(mask=masks[i], score=float(scores[i]), box_xyxy=tuple(boxes[i]))
                    for i in range(len(scores))
                ]
        return results


def create_backend(model_id: str, device: str = "auto", half: bool = False,
                   backend: str = "sam3", *, chunk_size: int = 8,
                   box_threshold: float = .3, text_threshold: float = .25) -> PromptBackend:
    if backend == "sam3":
        return Sam3Backend(model_id, device, half)
    if backend == "efficientsam3":
        from .efficient_backend import EfficientSam3Backend

        return EfficientSam3Backend(model_id, device, half)
    if backend == "yoloe":
        from .yoloe_backend import YoloEBackend

        return YoloEBackend(model_id, device, half)
    if backend == "grounding_dino":
        from .grounding_dino_backend import GroundingDinoBackend

        return GroundingDinoBackend(
            model_id, device, half, chunk_size=chunk_size,
            box_threshold=box_threshold, text_threshold=text_threshold,
        )
    raise ValueError("지원하지 않는 검출 백엔드입니다.")


def _local_model_path(model_id: str) -> Path | None:
    path = Path(model_id).expanduser()
    if path.is_dir():
        return path.resolve()
    if path.is_file():
        raise ValueError(
            "SAM 3 모델은 파일이 아닌 Hugging Face 모델 폴더를 지정해 주세요. "
            "Meta 원본 sam3.pt는 Transformers 형식으로 변환해야 합니다."
        )
    if path.is_absolute() or model_id.startswith((".", "~")) or "\\" in model_id:
        raise ValueError(f"SAM 3 로컬 모델 폴더가 없습니다: {path}")
    return None


def _validate_local_model(path: Path) -> None:
    """오프라인 실행에 필요한 HF 파일과 shard 복사 누락을 미리 점검한다."""
    missing: list[str] = []
    for name in ("config.json", "tokenizer_config.json"):
        if not _has_content(path / name):
            missing.append(name)
    if not any(_has_content(path / name) for name in ("processor_config.json", "preprocessor_config.json")):
        missing.append("processor_config.json (또는 preprocessor_config.json)")
    if not _has_content(path / "tokenizer.json") and not all(
        _has_content(path / name) for name in ("vocab.json", "merges.txt")
    ):
        missing.append("tokenizer.json (또는 vocab.json + merges.txt)")

    weights = next(
        (path / name for name in ("model.safetensors", "pytorch_model.bin") if _has_content(path / name)),
        None,
    )
    index = next(
        (
            path / name
            for name in ("model.safetensors.index.json", "pytorch_model.bin.index.json")
            if _has_content(path / name)
        ),
        None,
    )
    if weights is None and index is None:
        if any(path.glob("*.pt")):
            raise ValueError(
                "Meta 원본 sam3.pt만으로는 현재 Transformers 백엔드를 실행할 수 없습니다. "
                "Hugging Face 형식의 모델 폴더(config·processor·tokenizer·model.safetensors)가 "
                "필요합니다. 원본 가중치를 변환하거나 변환된 모델을 복사해 주세요."
            )
        missing.append("model.safetensors (또는 PyTorch 가중치/분할 가중치 index)")
    if missing:
        raise ValueError(f"SAM 3 로컬 모델 파일이 누락되거나 비어 있습니다: {', '.join(missing)}\n폴더: {path}")

    config = _read_json(path / "config.json")
    if config.get("model_type", "sam3") != "sam3":
        raise ValueError("SAM 3 이미지 모델이 아닙니다. model_type이 sam3인 모델 폴더를 지정해 주세요.")
    if weights is not None:
        _check_weight_file(weights)
    elif index is not None:
        weight_map = _read_json(index).get("weight_map")
        if not isinstance(weight_map, dict) or not weight_map:
            raise ValueError(f"SAM 3 분할 가중치 index의 weight_map이 올바르지 않습니다: {index.name}")
        names = list(weight_map.values())
        if not all(isinstance(name, str) and name for name in names):
            raise ValueError(f"SAM 3 분할 가중치 파일명이 올바르지 않습니다: {index.name}")
        for name in set(names):
            shard = Path(name)
            if shard.is_absolute() or ".." in shard.parts or ".." in PureWindowsPath(name).parts or ":" in name or name.startswith(("/", "\\")):
                raise ValueError(f"SAM 3 분할 가중치 파일 경로가 올바르지 않습니다: {name}")
            _check_weight_file(path / shard)


def _has_content(path: Path) -> bool:
    return path.is_file() and path.stat().st_size > 0


def _read_json(path: Path) -> dict:
    try:
        content = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as exc:
        raise ValueError(f"SAM 3 모델 JSON 파일을 읽을 수 없습니다: {path.name}") from exc
    if not isinstance(content, dict):
        raise ValueError(f"SAM 3 모델 JSON 파일 형식이 올바르지 않습니다: {path.name}")
    return content


def _check_weight_file(path: Path) -> None:
    if not _has_content(path):
        raise ValueError(f"SAM 3 모델 가중치 파일이 누락되거나 비어 있습니다: {path.name}")
    with path.open("rb") as stream:
        if stream.read(128).startswith(b"version https://git-lfs.github.com/spec/v1"):
            raise ValueError(
                f"SAM 3 모델 가중치가 실제 파일이 아닌 Git LFS 포인터입니다: {path.name}. "
                "실제 가중치 파일을 복사해 주세요."
            )


def _pick_device(torch, requested: str) -> tuple[str, str | None]:
    if requested == "cpu":
        return "cpu", None
    if torch.cuda.is_available():
        try:
            torch.zeros(1, device="cuda")
            return "cuda", None
        except RuntimeError as exc:
            if requested == "cuda":
                raise
            return "cpu", f"CUDA 초기화 실패, CPU로 대체: {exc}"
    if requested == "cuda":
        raise RuntimeError("CUDA를 요청했지만 사용할 수 있는 GPU가 없습니다.")
    return "cpu", "CUDA를 사용할 수 없어 CPU로 실행합니다(매우 느림)."
