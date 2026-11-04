"""Optional, local-only EfficientSAM3 image backend (no imports in the GUI)."""

from __future__ import annotations

import importlib
import json
import sys
from contextlib import nullcontext
from pathlib import Path

import numpy as np

from .backend import RawInstance, _pick_device

PROFILE_NAME = "avat-efficient.json"


def read_profile(folder: str | Path) -> dict:
    root = Path(folder).expanduser().resolve()
    path = root / PROFILE_NAME
    if not path.is_file():
        raise ValueError(f"EfficientSAM3 모델 폴더에 {PROFILE_NAME}이 필요합니다: {root}")
    try:
        profile = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        raise ValueError("EfficientSAM3 모델 설정 형식이 잘못되었습니다.") from exc
    if not isinstance(profile, dict) or type(profile.get("version")) is not int or profile["version"] != 1:
        raise ValueError("EfficientSAM3 모델 설정 version은 1이어야 합니다.")
    variants = {"tinyvit": {"5m", "11m", "21m"}, "repvit": {"m0.9", "m1.1", "m2.3"},
                "efficientvit": {"b0", "b1", "b2"}}
    backbone, name = profile.get("backbone_type"), profile.get("model_name")
    if not isinstance(backbone, str) or not isinstance(name, str) or name not in variants.get(backbone, set()):
        raise ValueError("EfficientSAM3 이미지 인코더 설정을 확인해 주세요.")
    for key in ("source", "checkpoint"):
        if not isinstance(profile.get(key), str) or not profile[key].strip():
            raise ValueError(f"EfficientSAM3 {key} 경로를 지정해 주세요.")
        profile[key] = (root / profile[key]).resolve()
    if not profile["checkpoint"].is_relative_to(root):
        raise ValueError("EfficientSAM3 가중치는 지정한 모델 폴더 안에 있어야 합니다.")
    if not profile["checkpoint"].is_file() or profile["checkpoint"].stat().st_size == 0:
        raise ValueError("EfficientSAM3 가중치 파일이 없거나 비어 있습니다.")
    source = profile["source"]
    if not any((source / name).is_file() for name in
               ("sam3/model_builder.py", "sam3/sam3/model_builder.py")):
        raise ValueError("EfficientSAM3 소스 폴더에 sam3/model_builder.py가 없습니다.")
    return profile


def checkpoint_state(checkpoint: dict) -> dict:
    """Normalize the upstream detector wrapper, without silently dropping keys."""
    if not isinstance(checkpoint, dict):
        raise ValueError("EfficientSAM3 가중치 형식이 잘못되었습니다.")
    state = checkpoint.get("model", checkpoint)
    if not isinstance(state, dict) or not state:
        raise ValueError("EfficientSAM3 모델 가중치가 비어 있습니다.")
    result = {}
    for key, value in state.items():
        if not isinstance(key, str):
            raise ValueError("EfficientSAM3 가중치 이름이 잘못되었습니다.")
        key = key.removeprefix("detector.").replace("student_trunk.", "")
        if key in result:
            raise ValueError("EfficientSAM3 가중치 이름이 중복됩니다.")
        result[key] = value
    return result


class EfficientSam3Backend:
    detector_name = "efficientsam3_prompt_detector"

    def __init__(self, model_id: str, device: str = "auto", half: bool = False):
        profile = read_profile(model_id)
        import torch

        self._torch = torch
        self.model_id = str(Path(model_id).resolve())
        self.device, self.fallback_reason = _pick_device(torch, device)
        tf32_matmul = torch.backends.cuda.matmul.allow_tf32
        tf32_cudnn = torch.backends.cudnn.allow_tf32
        source = str(profile["source"])
        existing = sys.modules.get("sam3")
        if existing is not None and not Path(existing.__file__).resolve().is_relative_to(profile["source"]):
            raise RuntimeError("다른 sam3 패키지가 로드되어 있습니다. 별도 검출 프로세스에서 실행해 주세요.")
        if source not in sys.path:
            sys.path.insert(0, source)
        try:
            builder = importlib.import_module("sam3.model_builder").build_efficientsam3_image_model
            processor_class = importlib.import_module("sam3.model.sam3_image_processor").Sam3Processor
        except ImportError as exc:
            raise RuntimeError(
                "EfficientSAM3 추론 의존성을 찾지 못했습니다. requirements.txt 설치 여부와 "
                "검출용 Python 설정을 확인해 주세요. " + str(exc)
            ) from exc
        finally:
            # Upstream enables TF32 globally at import. Preserve the caller's
            # precision policy, so model comparison does not change it silently.
            torch.backends.cuda.matmul.allow_tf32 = tf32_matmul
            torch.backends.cudnn.allow_tf32 = tf32_cudnn
        # Build without weights, then load strictly. Upstream's permissive
        # loader can otherwise run a partly random model after a wrong variant.
        self.model = builder(
            checkpoint_path=None, device="cpu", load_from_HF=False,
            backbone_type=profile["backbone_type"], model_name=profile["model_name"],
            text_encoder_type="MobileCLIP-S0", text_encoder_context_length=16,
            enable_inst_interactivity=False, compile=False,
        )
        state = torch.load(profile["checkpoint"], map_location="cpu", weights_only=True)
        self.model.load_state_dict(checkpoint_state(state), strict=True)
        use_bf16 = self.device == "cuda" and torch.cuda.is_bf16_supported()
        self._autocast_dtype = torch.bfloat16 if use_bf16 else None
        if half and use_bf16:
            self.model.to(dtype=torch.bfloat16)
        self.model.to(self.device).eval()
        self.processor = processor_class(self.model, device=self.device)
        self._text_cache: dict[str, dict] = {}

    def predict(self, frame_rgb, prompts, threshold, mask_threshold):
        from PIL import Image

        if frame_rgb.ndim != 3 or frame_rgb.shape[2] != 3 or frame_rgb.dtype != np.uint8:
            raise ValueError("EfficientSAM3 입력은 RGB uint8 프레임이어야 합니다.")
        torch = self._torch
        autocast = (torch.autocast(device_type="cuda", dtype=self._autocast_dtype)
                    if self._autocast_dtype is not None else nullcontext())
        results = {}
        with torch.inference_mode(), autocast:
            self.processor.confidence_threshold = threshold
            state = self.processor.set_image(Image.fromarray(frame_rgb))
            state["geometric_prompt"] = self.model._get_dummy_prompt()
            for prompt in prompts:
                if prompt not in self._text_cache:
                    self._text_cache[prompt] = self.model.backbone.forward_text([prompt], device=self.device)
                state["backbone_out"].update(self._text_cache[prompt])
                state = self.processor._forward_grounding(state)
                masks = (state["masks_logits"] > mask_threshold).squeeze(1).cpu().numpy()
                scores = state["scores"].detach().float().cpu().numpy()
                boxes = state["boxes"].detach().float().cpu().numpy()
                results[prompt] = [RawInstance(mask=masks[i], score=float(scores[i]),
                                              box_xyxy=tuple(boxes[i])) for i in range(len(scores))]
        return results
