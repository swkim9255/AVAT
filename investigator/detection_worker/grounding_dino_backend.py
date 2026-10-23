"""Chunked, box-only Grounding DINO through the shared prompt interface.

Heavy dependencies are imported only when this optional backend is constructed.
Unresolved text labels are retained as diagnostics, never silently discarded.
"""

from __future__ import annotations

import inspect
import json
import logging
import math
import re
from collections.abc import Mapping, Sequence
from contextlib import nullcontext
from pathlib import Path

import numpy as np

from .backend import RawInstance, _pick_device

LOGGER = logging.getLogger(__name__)
DEFAULT_MODEL_ID = "IDEA-Research/grounding-dino-base"
TINY_MODEL_ID = "IDEA-Research/grounding-dino-tiny"


def _tokens(text: str) -> tuple[str, ...]:
    return tuple(re.findall(r"\w+", text.lower(), flags=re.UNICODE))


def _contains_phrase(longer: tuple[str, ...], shorter: tuple[str, ...]) -> bool:
    return bool(shorter) and any(
        longer[start:start + len(shorter)] == shorter
        for start in range(len(longer) - len(shorter) + 1)
    )


def map_prompt(label: str, prompts: Sequence[str]) -> tuple[str | None, str | None]:
    """Prefer an exact label, then an unambiguous most-specific token phrase.

    For example, ``truck`` matches the exact prompt before ``tow truck``;
    ``tow`` can match ``tow truck`` but ``car`` never matches ``cargo``.
    Multiple equally specific candidates are recorded as ambiguous instead of
    inventing a model class. The original prompt spelling is returned.
    """
    if not isinstance(label, str) or not (label_tokens := _tokens(label)):
        return None, "empty_label"
    pairs = [(prompt, _tokens(prompt)) for prompt in prompts]
    exact = [prompt for prompt, tokens in pairs if tokens == label_tokens]
    if len(exact) == 1:
        return exact[0], None
    if len(exact) > 1:
        return None, "ambiguous_label"
    partial = [
        (prompt, tokens) for prompt, tokens in pairs
        if _contains_phrase(tokens, label_tokens) or _contains_phrase(label_tokens, tokens)
    ]
    if not partial:
        return None, "unmapped_label"
    specificity = max(len(tokens) for _, tokens in partial)
    best = [prompt for prompt, tokens in partial if len(tokens) == specificity]
    return (best[0], None) if len(best) == 1 else (None, "ambiguous_label")


def prompt_chunks(
    prompts: Sequence[str], chunk_size: int = 8,
    prompt_groups: Mapping[str, Sequence[str]] | Sequence[Sequence[str]] | None = None,
) -> tuple[tuple[str, ...], ...]:
    """Pack entire model-class groups without splitting their prompts.

    The limit is a soft maximum only when one indivisible class exceeds it.
    Custom class configurations may pass their own groups; the app's fixed
    detection taxonomy is used otherwise. Unknown prompts remain singletons.
    """
    if isinstance(chunk_size, bool) or not isinstance(chunk_size, int) or chunk_size < 1:
        raise ValueError("Grounding DINO 청크 크기는 1 이상의 정수여야 합니다.")
    if (isinstance(prompts, str) or not isinstance(prompts, Sequence) or not prompts
            or any(not isinstance(p, str) or not _tokens(p) for p in prompts)
            or any("." in p for p in prompts)
            or len({_tokens(prompt) for prompt in prompts}) != len(prompts)):
        raise ValueError("Grounding DINO 프롬프트는 마침표 없는 중복되지 않는 문자열이어야 합니다.")
    if prompt_groups is None:
        from investigator.detection_taxonomy import taxonomy_records

        prompt_groups = [record["prompts"] for record in taxonomy_records()]
    groups = list(prompt_groups.values()) if isinstance(prompt_groups, Mapping) else prompt_groups
    owner: dict[tuple[str, ...], int] = {}
    for index, group in enumerate(groups):
        if isinstance(group, str) or not isinstance(group, Sequence):
            raise ValueError("Grounding DINO 대분류별 프롬프트 목록을 확인해 주세요.")
        for prompt in group:
            if not isinstance(prompt, str) or not (normalized := _tokens(prompt)):
                raise ValueError("Grounding DINO 대분류별 프롬프트를 확인해 주세요.")
            if normalized in owner:
                raise ValueError("Grounding DINO 대분류 간 프롬프트가 중복됩니다.")
            owner[normalized] = index
    atomic: dict[tuple[str, int], list[str]] = {}
    for index, prompt in enumerate(prompts):
        group_index = owner.get(_tokens(prompt))
        key = ("group", group_index) if group_index is not None else ("single", index)
        atomic.setdefault(key, []).append(prompt)
    chunks, current = [], []
    for group in atomic.values():
        if current and len(current) + len(group) > chunk_size:
            chunks.append(tuple(current))
            current = []
        current.extend(group)
        if len(current) >= chunk_size:
            chunks.append(tuple(current))
            current = []
    if current:
        chunks.append(tuple(current))
    return tuple(chunks)


def _threshold(value: float, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"Grounding DINO {name}는 0~1의 유한한 숫자여야 합니다.")
    value = float(value)
    if not math.isfinite(value) or not 0 <= value <= 1:
        raise ValueError(f"Grounding DINO {name}는 0~1의 유한한 숫자여야 합니다.")
    return value


def _model_source(model_id: str) -> tuple[str, dict]:
    if not isinstance(model_id, str) or not model_id.strip():
        raise ValueError("Grounding DINO 모델 ID 또는 로컬 모델 폴더를 지정해 주세요.")
    path = Path(model_id).expanduser()
    if path.is_dir():
        if not (path / "config.json").is_file():
            raise ValueError("Grounding DINO 로컬 모델 폴더에 config.json이 없습니다.")
        return str(path.resolve()), {"local_files_only": True}
    if path.is_file() or path.is_absolute() or model_id.startswith((".", "~")) or "\\" in model_id:
        raise ValueError("Grounding DINO의 Hugging Face 모델 폴더를 지정해 주세요.")
    return model_id, {}


def _as_numpy(value) -> np.ndarray:
    return value.detach().float().cpu().numpy() if hasattr(value, "detach") else np.asarray(value)


class GroundingDinoBackend:
    detector_name = "grounding_dino_prompt_detector"
    supports_segmentation = False
    supports_prompt_groups = True

    def __init__(
        self, model_id: str = DEFAULT_MODEL_ID, device: str = "auto", half: bool = True,
        *, chunk_size: int = 8, box_threshold: float = 0.3, text_threshold: float = 0.25,
    ):
        # Reject invalid configuration before loading optional libraries/weights.
        if isinstance(chunk_size, bool) or not isinstance(chunk_size, int) or chunk_size < 1:
            raise ValueError("Grounding DINO 청크 크기는 1 이상의 정수여야 합니다.")
        self.chunk_size = chunk_size
        self.box_threshold = _threshold(box_threshold, "박스 임계값")
        self.text_threshold = _threshold(text_threshold, "텍스트 임계값")
        source, load_options = _model_source(model_id)
        try:
            import torch
            from transformers import AutoModelForZeroShotObjectDetection, AutoProcessor
        except ImportError as exc:
            raise RuntimeError(
                "Grounding DINO 실행 환경(torch·transformers)을 찾지 못했습니다. "
                "검출용 Python 설정을 확인해 주세요."
            ) from exc
        self._torch = torch
        self.model_id = model_id
        self.device, self.fallback_reason = _pick_device(torch, device)
        self._half = bool(half and self.device == "cuda")
        self._dtype = torch.float16 if self._half else torch.float32
        self.processor = AutoProcessor.from_pretrained(source, **load_options)
        self.model = AutoModelForZeroShotObjectDetection.from_pretrained(
            source, torch_dtype=self._dtype, **load_options,
        ).to(self.device).eval()
        # Transformers renamed box_threshold to threshold. Inspect the actual
        # installed primary API, rather than catching unrelated TypeErrors.
        parameters = inspect.signature(
            self.processor.post_process_grounded_object_detection,
        ).parameters
        self._box_threshold_argument = "threshold" if "threshold" in parameters else "box_threshold"
        self._prompts: tuple[str, ...] | None = None
        self.chunks: tuple[tuple[str, ...], ...] = ()
        self._mapping_failures: list[dict] = []

    @property
    def inference_metadata(self) -> dict:
        """Serializable settings used by the actual backend, not user guesses."""
        return {
            "chunk_size": self.chunk_size,
            "chunks": [list(chunk) for chunk in self.chunks],
            "box_threshold": self.box_threshold,
            "text_threshold": self.text_threshold,
            "precision": "float16" if self._half else "float32",
            "supports_segmentation": False,
        }

    def prepare(
        self, prompts: list[str],
        prompt_groups: Mapping[str, Sequence[str]] | Sequence[Sequence[str]] | None = None,
    ) -> None:
        if self._prompts is not None:
            if self._prompts != tuple(prompts):
                raise ValueError("Grounding DINO 실행 중 프롬프트 목록이나 순서를 변경할 수 없습니다.")
            return
        self.chunks = prompt_chunks(prompts, self.chunk_size, prompt_groups)
        self._prompts = tuple(prompts)
        for chunk in self.chunks:
            if len(chunk) > self.chunk_size:
                LOGGER.warning(
                    "Grounding DINO class group exceeds chunk_size=%d; keeping %d prompts together",
                    self.chunk_size, len(chunk),
                )

    def drain_mapping_failures(self) -> list[dict]:
        """Return all unresolved outputs once; runner supplies frame context."""
        records, self._mapping_failures = self._mapping_failures, []
        return records

    def _convert_result(
        self, post: dict, chunk: Sequence[str], shape: tuple[int, int],
        output: dict[str, list[RawInstance]],
    ) -> None:
        labels = post.get("text_labels")
        if labels is None:
            labels = post.get("labels")  # Compatibility with older Transformers.
        boxes, scores = _as_numpy(post["boxes"]), _as_numpy(post["scores"])
        # Transformers 5.20's tokenizer can decode an empty label-ID list as
        # [""] rather than []. Only normalize that exact zero-detection case;
        # real box/label count mismatches must still fail loudly.
        if boxes.shape == (0, 4) and scores.shape == (0,) and isinstance(labels, list) and labels == [""]:
            return
        if (not isinstance(labels, (list, tuple)) or boxes.shape != (len(labels), 4)
                or scores.shape != (len(labels),)):
            raise ValueError("Grounding DINO 박스·신뢰도·라벨 수가 일치하지 않습니다.")
        if (not np.isfinite(boxes).all() or not np.isfinite(scores).all()
                or ((scores < 0) | (scores > 1)).any()
                or (boxes[:, 2:] < boxes[:, :2]).any()):
            raise ValueError("Grounding DINO가 유효하지 않은 박스 또는 신뢰도를 반환했습니다.")
        height, width = shape
        for label, coordinates, score in zip(labels, boxes, scores, strict=True):
            prompt, failure = map_prompt(label, chunk)
            box = np.clip(coordinates, (0, 0, 0, 0), (width, height, width, height))
            if failure:
                record = {
                    "label": label if isinstance(label, (str, int, float)) else str(label),
                    "chunk_prompts": list(chunk), "reason": failure,
                    "score": float(score), "box_xyxy": [float(v) for v in box],
                }
                self._mapping_failures.append(record)
                LOGGER.warning("Grounding DINO label mapping failed: %s", json.dumps(record, ensure_ascii=False))
                continue
            output[prompt].append(RawInstance(
                mask=None, score=float(score), box_xyxy=tuple(float(v) for v in box),
            ))

    def predict(self, frame_rgb, prompts, threshold, mask_threshold):
        if (not isinstance(frame_rgb, np.ndarray) or frame_rgb.ndim != 3
                or frame_rgb.shape[2] != 3 or frame_rgb.dtype != np.uint8
                or not frame_rgb.shape[0] or not frame_rgb.shape[1]):
            raise ValueError("Grounding DINO 입력은 원본 크기의 RGB uint8 프레임이어야 합니다.")
        # The caller's minimum class threshold is validated but intentionally
        # not combined with the independent box/text thresholds. Class-specific
        # confidence thresholds are still applied by common postprocessing.
        _threshold(threshold, "클래스 임계값")
        _threshold(mask_threshold, "마스크 임계값")
        self.prepare(prompts)
        output = {prompt: [] for prompt in prompts}
        # Grounding DINO constructs some sinusoidal positions in FP32 even
        # with FP16 weights. Autocast keeps their downstream linear layers
        # compatible, rather than requiring patches to Transformers internals.
        autocast = (
            self._torch.autocast(device_type="cuda", dtype=self._dtype)
            if self._half else nullcontext()
        )
        with self._torch.inference_mode(), autocast:
            for chunk in self.chunks:
                text = ". ".join(prompt.strip().lower() for prompt in chunk) + "."
                inputs = self.processor(images=frame_rgb, text=text, return_tensors="pt").to(self.device)
                # Keep token IDs and attention masks integer/bool; only image
                # floating tensors must match half-precision model weights.
                inputs["pixel_values"] = inputs["pixel_values"].to(self._dtype)
                result = self.model(**inputs)
                processed = self.processor.post_process_grounded_object_detection(
                    result, inputs["input_ids"],
                    **{self._box_threshold_argument: self.box_threshold},
                    text_threshold=self.text_threshold,
                    target_sizes=[tuple(frame_rgb.shape[:2])],
                )
                if not isinstance(processed, list) or len(processed) != 1:
                    raise ValueError("Grounding DINO가 한 프레임에 대한 결과를 반환하지 않았습니다.")
                self._convert_result(processed[0], chunk, frame_rgb.shape[:2], output)
        return output
