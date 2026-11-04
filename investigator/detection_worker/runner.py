"""선택한 영상 구간을 샘플링해 검출 결과 문서를 만든다."""

from __future__ import annotations

import hashlib
import json
import math
import os
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import cv2

from . import RESULT_SCHEMA_VERSION, WORKER_NAME, WORKER_VERSION
from .backend import PromptBackend
from .classes import ClassConfig, ClassDef
from .events import CancelToken, EventWriter
from .postprocess import build_detections
from .tracking import SimpleTracker


class Cancelled(Exception):
    pass


@dataclass
class RunOptions:
    video_path: Path
    output_path: Path
    sample_fps: float = 2.0
    frame_step: int | None = None  # 지정하면 sample_fps 대신 N프레임 간격
    start_frame: int = 0
    end_frame: int | None = None  # 포함하지 않음
    mask_threshold: float = 0.5
    include_polygons: bool = True

    def validate(self) -> None:
        if self.start_frame < 0:
            raise ValueError("시작 프레임은 0 이상이어야 합니다.")
        if self.end_frame is not None and self.end_frame <= self.start_frame:
            raise ValueError("끝 프레임은 시작 프레임보다 커야 합니다.")
        if self.frame_step is not None and self.frame_step <= 0:
            raise ValueError("프레임 간격은 1 이상이어야 합니다.")
        if not math.isfinite(self.sample_fps) or self.sample_fps <= 0:
            raise ValueError("샘플링 FPS는 0보다 큰 유한한 숫자여야 합니다.")
        if not math.isfinite(self.mask_threshold) or not 0 <= self.mask_threshold <= 1:
            raise ValueError("마스크 임계값은 0~1이어야 합니다.")


def plan_frames(frame_count: int, video_fps: float, opts: RunOptions) -> list[int]:
    """처리할 프레임 번호 목록. frame_step 또는 sample_fps로 간격을 정한다."""
    opts.validate()
    if frame_count <= 0:
        raise ValueError("영상 프레임 수를 확인할 수 없습니다.")
    start = max(0, opts.start_frame)
    end = frame_count if opts.end_frame is None else min(frame_count, opts.end_frame)
    if start >= end:
        raise ValueError(f"처리 구간이 비었습니다: start={start}, end={end}")
    fps = video_fps if math.isfinite(video_fps) and video_fps > 0 else 30.0
    if opts.frame_step is not None:
        step = int(opts.frame_step)
    else:
        step = max(1, round(fps / opts.sample_fps)) if opts.sample_fps > 0 else 1
    return list(range(start, end, step))


def _open_video(path: Path) -> cv2.VideoCapture:
    """재생과 같은 FFmpeg 백엔드로 열고, 디코딩은 CPU로 명시한다.

    CAP_ANY는 FFmpeg 실패 시 하드웨어 디코더를 쓸 수 있는 Media Foundation으로
    넘어갈 수 있다. GPU 드라이버 사용은 모델 추론(CUDA)으로만 한정한다.
    """
    hw_property = getattr(cv2, "CAP_PROP_HW_ACCELERATION", None)
    if hw_property is None:
        return cv2.VideoCapture(str(path), cv2.CAP_FFMPEG)
    return cv2.VideoCapture(str(path), cv2.CAP_FFMPEG, [hw_property, cv2.VIDEO_ACCELERATION_NONE])


def run(
    opts: RunOptions,
    backend: PromptBackend,
    config: ClassConfig,
    classes: tuple[ClassDef, ...],
    events: EventWriter,
    cancel: CancelToken,
    device_note: str | None = None,
) -> dict:
    opts.validate()
    if cancel.cancelled:
        raise Cancelled()
    initial_stat = opts.video_path.stat()
    cap = _open_video(opts.video_path)
    if not cap.isOpened():
        cap.release()
        raise RuntimeError(f"영상을 열 수 없습니다: {opts.video_path}")
    try:
        frame_count = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        video_fps = float(cap.get(cv2.CAP_PROP_FPS))
        if not math.isfinite(video_fps) or video_fps <= 0:
            video_fps = 30.0
        width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        frames = plan_frames(frame_count, video_fps, opts)
        prompts = sorted({p for c in classes for p in c.prompts})
        # 백엔드 단계의 점수 문턱값은 클래스별 문턱값 중 최솟값으로 넓게 잡는다.
        backend_threshold = min(c.threshold for c in classes)

        # Optional lifecycle hook: YOLOE prepares all text embeddings before the
        # first frame. SAM3 and old third-party/fake backends need no hook.
        prepare = getattr(backend, "prepare", None)
        if callable(prepare):
            events.emit("preparing_prompts", prompts=len(prompts))
            if getattr(backend, "supports_prompt_groups", False):
                prepare(prompts, prompt_groups={c.key: list(c.prompts) for c in classes})
            else:
                prepare(prompts)
            if cancel.cancelled:
                raise Cancelled()

        events.emit(
            "started",
            device=backend.device,
            device_note=device_note,
            total_frames=len(frames),
            video_frame_count=frame_count,
            video_fps=video_fps,
            classes=[c.key for c in classes],
            prompts=prompts,
        )

        tracker = SimpleTracker()
        prefix_counters: dict[str, int] = {}
        object_ids: dict[int, str] = {}
        records: list[dict] = []
        mapping_failures: list[dict] = []
        class_by_key = {c.key: c for c in classes}

        next_sequential = 0
        for sample_index, frame_index in enumerate(frames):
            if cancel.cancelled:
                raise Cancelled()
            # 간격이 촘촘하면 순차 읽기가 seek보다 빠르고 정확하다.
            if frame_index != next_sequential and not cap.set(cv2.CAP_PROP_POS_FRAMES, frame_index):
                raise RuntimeError(f"{frame_index + 1:,}번 프레임으로 이동하지 못했습니다.")
            ok, frame_bgr = cap.read()
            next_sequential = frame_index + 1
            if not ok or frame_bgr is None or not frame_bgr.size:
                # Treat an incomplete run as a failure: merging a partial empty
                # result could otherwise delete earlier automatic annotations.
                raise RuntimeError(
                    f"{frame_index + 1:,}번 프레임을 읽지 못했습니다. "
                    "영상 파일을 확인해 주세요. 기존 검출 결과는 유지됩니다."
                )
            height, width = frame_bgr.shape[:2]
            frame_rgb = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2RGB)
            raw = backend.predict(frame_rgb, prompts, backend_threshold, opts.mask_threshold)
            drain_failures = getattr(backend, "drain_mapping_failures", None)
            if callable(drain_failures):
                failures = drain_failures()
                mapping_failures.extend({**failure, "frame_index": frame_index} for failure in failures)
                if failures:
                    events.emit(
                        "warning", code="label_mapping_failure", frame_index=frame_index,
                        count=len(failures), message=f"{frame_index + 1}번 프레임: 라벨 매핑 실패 {len(failures)}건 (결과 로그에 보존)",
                    )
            if cancel.cancelled:
                raise Cancelled()
            dets = build_detections(raw, classes, config, frame_rgb.shape[:2])
            track_ids = tracker.update(sample_index, [(d.class_key, d.bbox_xywh) for d in dets])

            for det, tid in zip(dets, track_ids, strict=True):
                if tid not in object_ids:
                    prefix = class_by_key[det.class_key].object_prefix
                    prefix_counters[prefix] = prefix_counters.get(prefix, 0) + 1
                    object_ids[tid] = f"{prefix}{prefix_counters[prefix]:03d}"
                record = {
                    "frame_index": frame_index,
                    "timestamp_ms": round(frame_index / video_fps * 1000.0, 1),
                    "class_key": det.class_key,
                    "model_class_key": det.class_key,
                    "source_prompt": det.prompt,
                    "suggested_subtype_key": _suggested_subtype(det.class_key, det.prompt),
                    "reviewed": False,
                    "class_label": class_by_key[det.class_key].label,
                    "bbox_xywh": [round(v, 1) for v in det.bbox_xywh],
                    "score": round(det.score, 4),
                    "track_id": tid,
                    "object_id": object_ids[tid],
                    "metadata": {
                        "prompt": det.prompt,
                        "mask_area_px" if det.mask is not None else "box_area_px": det.area,
                    },
                }
                if opts.include_polygons and det.polygon:
                    record["metadata"]["polygon"] = [
                        [round(x, 1), round(y, 1)] for x, y in det.polygon
                    ]
                records.append(record)

            events.emit(
                "progress",
                processed=sample_index + 1,
                total=len(frames),
                frame_index=frame_index,
                detections_in_frame=len(dets),
            )
    finally:
        cap.release()

    events.emit("finalizing", message="영상 정보를 확인하고 결과를 정리합니다…")
    checksum = _sha256(opts.video_path, cancel)
    final_stat = opts.video_path.stat()
    if (initial_stat.st_size, initial_stat.st_mtime_ns) != (
        final_stat.st_size, final_stat.st_mtime_ns
    ):
        raise RuntimeError("검출 중 영상 파일이 변경되었습니다. 다시 실행해 주세요.")

    return {
        "schema_version": RESULT_SCHEMA_VERSION,
        "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "provenance": {
            "source": "automatic_detection",
            "detector": getattr(backend, "detector_name", WORKER_NAME),
            "detector_version": WORKER_VERSION,
            "model_id": backend.model_id,
            "device": backend.device,
            "tracker": "simple_iou_center_v1",
            "review_status": "unreviewed",
            "supports_segmentation": getattr(backend, "supports_segmentation", True),
            **getattr(backend, "inference_metadata", {}),
            **config.notes,
        },
        "video": {
            "path": str(opts.video_path),
            "sha256": checksum,
            "frame_count": frame_count,
            "fps": video_fps,
            "width": width,
            "height": height,
        },
        "sampling": {
            "sample_fps": opts.sample_fps,
            "frame_step": opts.frame_step,
            "processed_frames": frames,
            "start_frame": frames[0],
            "end_frame_exclusive": min(opts.end_frame, frame_count)
            if opts.end_frame is not None else frame_count,
        },
        "classes": [
            {
                "key": c.key,
                "label": c.label,
                "prompts": list(c.prompts),
                "threshold": c.threshold,
                "object_prefix": c.object_prefix,
                "nms_iou": c.nms_iou,
            }
            for c in classes
        ],
        "detections": records,
        **({
            "mapping_failures": mapping_failures,
            "detection_summary": {
                "processed_frames": len(frames),
                "coarse_detection_counts": {
                    c.key: sum(record["class_key"] == c.key for record in records) for c in classes
                },
                "label_mapping_failure_count": len(mapping_failures),
            },
        } if callable(getattr(backend, "drain_mapping_failures", None)) else {}),
    }


def _suggested_subtype(class_key: str, prompt: str) -> str | None:
    """Custom legacy configs need not be part of the current taxonomy."""
    from investigator.detection_taxonomy import suggest_subtype

    return suggest_subtype(class_key, prompt)


def _sha256(path: Path, cancel: CancelToken | None = None) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            if cancel is not None and cancel.cancelled:
                raise Cancelled()
            h.update(chunk)
    return h.hexdigest()


def write_atomic(doc: dict, path: Path) -> None:
    """임시 파일에 쓰고 fsync 후 교체한다. 중간에 실패하면 기존 파일은 그대로다."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(doc, f, ensure_ascii=False, indent=1, allow_nan=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
