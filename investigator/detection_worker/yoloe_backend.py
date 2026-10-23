"""Local-only YOLOE instance segmentation through the shared prompt interface.

The optional model lives in the worker process; importing this module does not
import torch, Ultralytics, CLIP or Qt. No missing asset is downloaded at runtime.
"""

from __future__ import annotations

import hashlib
import importlib.util
import math
import os
import tempfile
from pathlib import Path

import numpy as np

from .backend import RawInstance, _pick_device

EMBEDDING_SUFFIX = ".text-embeddings.npz"
_RUNTIME_CONFIG_DIRECTORY: tempfile.TemporaryDirectory | None = None


def _isolated_library_settings() -> None:
    global _RUNTIME_CONFIG_DIRECTORY
    # Ultralytics otherwise writes settings and telemetry preferences into the
    # user's roaming profile merely by importing the package. Keep worker-only
    # library state temporary, including on read-only packaged deployments.
    if not os.environ.get("YOLO_CONFIG_DIR"):
        if _RUNTIME_CONFIG_DIRECTORY is None:
            _RUNTIME_CONFIG_DIRECTORY = tempfile.TemporaryDirectory(prefix="avat-yoloe-")
        os.environ["YOLO_CONFIG_DIR"] = _RUNTIME_CONFIG_DIRECTORY.name


def local_checkpoint(model_id: str | Path) -> Path:
    path = Path(model_id).expanduser().resolve()
    if path.suffix.lower() != ".pt" or not path.is_file() or path.stat().st_size == 0:
        raise ValueError("YOLOE의 로컬 .pt 가중치 파일을 지정해 주세요. 자동 다운로드하지 않습니다.")
    with path.open("rb") as stream:
        if stream.read(128).startswith(b"version https://git-lfs.github.com/spec/v1"):
            raise ValueError("YOLOE 가중치가 Git LFS 포인터입니다. 실제 가중치 파일을 복사해 주세요.")
    return path


def checkpoint_sha256(path: Path) -> str:
    checksum = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            checksum.update(chunk)
    return checksum.hexdigest()


def read_embeddings(path: Path, model_hash: str, prompts: list[str]) -> np.ndarray:
    """Load model-bound features, preserving the exact requested prompt order.

    A cache for the complete fixed vocabulary may also serve a selected subset.
    It must contain *post-reparameterization* features from YOLOE.get_text_pe.
    """
    try:
        with np.load(path, allow_pickle=False) as data:
            if set(data.files) != {"format_version", "model_sha256", "prompts", "embeddings"}:
                raise ValueError("필수 정보가 없습니다.")
            version, digest = data["format_version"], data["model_sha256"]
            names, embeddings = data["prompts"], data["embeddings"]
            if version.shape != () or version.dtype.kind not in "iu" or version.item() != 1:
                raise ValueError("지원하지 않는 버전입니다.")
            if digest.shape != () or digest.dtype.kind != "U" or digest.item() != model_hash:
                raise ValueError("가중치의 SHA256이 다릅니다.")
            if names.ndim != 1 or names.dtype.kind != "U" or len(set(names.tolist())) != len(names):
                raise ValueError("프롬프트 목록이 잘못되었습니다.")
            if (embeddings.dtype != np.float32 or embeddings.ndim != 3
                    or embeddings.shape[:2] != (1, len(names)) or embeddings.shape[2] <= 0
                    or not np.isfinite(embeddings).all()):
                raise ValueError("임베딩의 크기 또는 값이 잘못되었습니다.")
            index = {name: i for i, name in enumerate(names.tolist())}
            if any(prompt not in index for prompt in prompts):
                raise ValueError("선택한 프롬프트가 캐시에 없습니다.")
            return embeddings[:, [index[prompt] for prompt in prompts], :].copy()
    except (OSError, ValueError, KeyError, EOFError) as exc:
        raise ValueError(f"YOLOE 텍스트 임베딩 캐시를 확인해 주세요: {path.name}: {exc}") from exc


def _local_text_encoder(torch, path: Path, device: str):
    # Check dependencies before importing Ultralytics' text_model, whose own
    # missing-CLIP path can otherwise attempt an automatic pip installation.
    if importlib.util.find_spec("clip") is None:
        raise RuntimeError("YOLOE 텍스트 토크나이저 CLIP이 없습니다. 검출 환경을 확인해 주세요.")
    import clip

    if not callable(getattr(clip, "tokenize", None)):
        raise RuntimeError("YOLOE에 필요한 CLIP 토크나이저가 아닌 다른 clip 패키지입니다.")
    tokenizer_module = getattr(clip, "clip", None)
    tokenizer = getattr(tokenizer_module, "_tokenizer", None)
    if tokenizer is None or not getattr(tokenizer, "encoder", None):
        raise RuntimeError("CLIP 토크나이저의 로컬 어휘 파일이 없습니다.")

    class LocalTextEncoder(torch.nn.Module):
        def __init__(self):
            super().__init__()
            self.encoder = torch.jit.load(str(path), map_location=device).eval()

        def tokenize(self, texts):
            return clip.tokenize(texts, truncate=True).to(device)

        def encode_text(self, tokens):
            # mobileclip_blt.ts includes normalization in its traced graph.
            return self.encoder(tokens).float()

    return LocalTextEncoder().eval()


class YoloEBackend:
    detector_name = "yoloe_prompt_detector"
    inference_imgsz = 640

    def __init__(self, model_id: str, device: str = "auto", half: bool = False):
        self._checkpoint = local_checkpoint(model_id)
        # This backend is local-only even when the app's general offline switch
        # is off. Inference must never install packages or fetch remote assets.
        os.environ["YOLO_AUTOINSTALL"] = "false"
        os.environ["YOLO_OFFLINE"] = "true"
        _isolated_library_settings()
        try:
            import torch
            from ultralytics import YOLOE, utils
        except ImportError as exc:
            raise RuntimeError(
                "YOLOE 실행 환경을 찾지 못했습니다. requirements.txt 설치 여부와 "
                "검출용 Python 설정을 확인해 주세요."
            ) from exc
        # Also cover a library imported previously in a benchmark process.
        utils.AUTOINSTALL = False
        utils.ONLINE = False
        utils.SETTINGS.update({"sync": False})
        # Ultralytics는 save=False여도 예측마다 빈 runs/<task>/predictN 폴더를 만들고,
        # 기본 위치는 git 저장소 루트나 현재 작업 폴더(앱 폴더)다. 읽기 전용 배포 폴더에서
        # 검출이 실패하지 않도록 이 프로세스 전용 임시 설정 폴더 아래에 고정한다.
        self._runs_dir = Path(os.environ["YOLO_CONFIG_DIR"]) / "runs"
        self._torch = torch
        self.model_id = str(self._checkpoint)
        self.device, self.fallback_reason = _pick_device(torch, device)
        self._half = bool(half and self.device == "cuda")
        self.model = YOLOE(str(self._checkpoint), task="segment", verbose=False).to(self.device)
        if (self.model.task != "segment"
                or not callable(getattr(self.model.model, "get_text_pe", None))):
            raise ValueError("YOLOE segmentation 가중치(yoloe-11l-seg.pt)가 필요합니다.")
        self._prompts: tuple[str, ...] | None = None

    def prepare(self, prompts: list[str]) -> None:
        if (not prompts or any(not isinstance(p, str) or not p.strip() for p in prompts)
                or len(set(prompts)) != len(prompts)):
            raise ValueError("YOLOE 프롬프트는 중복되지 않는 비어 있지 않은 문자열이어야 합니다.")
        if self._prompts is not None:
            if self._prompts != tuple(prompts):
                raise ValueError("YOLOE 실행 중 프롬프트 순서나 목록을 변경할 수 없습니다.")
            return
        cache = self._checkpoint.with_suffix(EMBEDDING_SUFFIX)
        torch = self._torch
        with torch.inference_mode():
            if cache.is_file():
                array = read_embeddings(cache, checkpoint_sha256(self._checkpoint), prompts)
                features = torch.from_numpy(array).to(self.device)
            else:
                encoder_path = self._checkpoint.parent / "mobileclip_blt.ts"
                if not encoder_path.is_file() or not encoder_path.stat().st_size:
                    raise ValueError(
                        "YOLOE 모델 폴더에 mobileclip_blt.ts 또는 검증된 텍스트 임베딩 "
                        "캐시가 필요합니다. 자동 다운로드하지 않습니다."
                    )
                self.model.model.clip_model = _local_text_encoder(torch, encoder_path, self.device)
                try:
                    # The pinned public wrapper does not expose cache_clip_model.
                    # Its underlying API uses the injected *local* encoder here,
                    # rather than silently constructing/downloading another one.
                    features = self.model.model.get_text_pe(prompts, cache_clip_model=True)
                finally:
                    self.model.model.clip_model = None
            if (features.ndim != 3 or tuple(features.shape[:2]) != (1, len(prompts))
                    or not bool(torch.isfinite(features).all())):
                raise ValueError("YOLOE 텍스트 임베딩의 크기 또는 값이 잘못되었습니다.")
            self.model.set_classes(list(prompts), features)
        self._prompts = tuple(prompts)

    def predict(self, frame_rgb, prompts, threshold, mask_threshold):
        if (not isinstance(frame_rgb, np.ndarray) or frame_rgb.ndim != 3
                or frame_rgb.shape[2] != 3 or frame_rgb.dtype != np.uint8
                or frame_rgb.shape[0] == 0 or frame_rgb.shape[1] == 0):
            raise ValueError("YOLOE 입력은 원본 크기의 RGB uint8 프레임이어야 합니다.")
        if not math.isfinite(threshold) or not 0 <= threshold <= 1:
            raise ValueError("YOLOE 신뢰도는 0~1이어야 합니다.")
        # Ultralytics Results contains already-binarized masks, not probabilities.
        # Reject unsupported values instead of pretending to threshold them.
        if mask_threshold != 0.5:
            raise ValueError("YOLOE는 이진 마스크를 반환하므로 마스크 임계값은 0.5만 지원합니다.")
        self.prepare(prompts)
        results = self.model.predict(
            source=np.ascontiguousarray(frame_rgb[:, :, ::-1]),
            device=self.device, half=self._half, conf=threshold,
            imgsz=self.inference_imgsz,
            retina_masks=True, agnostic_nms=False, iou=1.0, max_det=1000,
            stream=False, save=False, verbose=False, augment=False,
            project=str(self._runs_dir), name="predict", exist_ok=True,
        )
        if len(results) != 1:
            raise ValueError("YOLOE가 한 프레임에 대한 결과를 반환하지 않았습니다.")
        return self._convert_result(results[0], prompts, frame_rgb.shape[:2])

    @staticmethod
    def _convert_result(result, prompts: list[str], shape: tuple[int, int]):
        names = result.names
        expected = dict(enumerate(prompts))
        actual = names if isinstance(names, dict) else dict(enumerate(names))
        if actual != expected or tuple(result.orig_shape) != shape:
            raise ValueError("YOLOE 결과의 프롬프트 번호 또는 원본 크기가 일치하지 않습니다.")
        output: dict[str, list[RawInstance]] = {prompt: [] for prompt in prompts}
        boxes = result.boxes
        if boxes is None or len(boxes) == 0:
            if result.masks is not None and len(result.masks.data):
                raise ValueError("YOLOE 박스와 마스크 수가 일치하지 않습니다.")
            return output
        if result.masks is None:
            raise ValueError("YOLOE segmentation 마스크가 없습니다.")
        masks = result.masks.data.detach().cpu().numpy()
        scores = boxes.conf.detach().float().cpu().numpy()
        indices = boxes.cls.detach().float().cpu().numpy()
        coordinates = boxes.xyxy.detach().float().cpu().numpy()
        count = len(boxes)
        if (masks.shape != (count, *shape) or scores.shape != (count,)
                or indices.shape != (count,) or coordinates.shape != (count, 4)):
            raise ValueError("YOLOE 마스크 크기 또는 박스·마스크 수가 원본과 일치하지 않습니다.")
        if (not np.isfinite(scores).all() or ((scores < 0) | (scores > 1)).any()
                or not np.isfinite(indices).all() or (indices != np.floor(indices)).any()
                or ((indices < 0) | (indices >= len(prompts))).any()
                or not np.isfinite(coordinates).all()
                or (coordinates[:, 2:] < coordinates[:, :2]).any()
                or not np.isin(masks, (0, 1)).all()):
            raise ValueError("YOLOE가 유효하지 않은 신뢰도·클래스·마스크를 반환했습니다.")
        for index in range(count):
            prompt = prompts[int(indices[index])]
            output[prompt].append(RawInstance(
                mask=masks[index].astype(bool, copy=True), score=float(scores[index]),
                box_xyxy=tuple(float(value) for value in coordinates[index]),
            ))
        return output
