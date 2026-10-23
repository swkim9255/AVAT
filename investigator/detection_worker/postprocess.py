"""마스크 → 박스·폴리곤 변환과 중복 제거."""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import cv2
import numpy as np

from .backend import RawInstance
from .classes import ClassConfig, ClassDef


@dataclass
class Detection:
    class_key: str
    prompt: str
    score: float
    bbox_xywh: tuple[float, float, float, float]
    mask: np.ndarray | None = field(default=None, repr=False)
    polygon: list[list[float]] = field(default_factory=list)
    area: int | float = 0


def mask_to_bbox(mask: np.ndarray) -> tuple[float, float, float, float] | None:
    ys, xs = np.nonzero(mask)
    if xs.size == 0:
        return None
    x0, x1 = int(xs.min()), int(xs.max()) + 1
    y0, y1 = int(ys.min()), int(ys.max()) + 1
    return float(x0), float(y0), float(x1 - x0), float(y1 - y0)


def mask_to_polygon(mask: np.ndarray, max_points: int = 64) -> list[list[float]]:
    """가장 큰 외곽선을 단순화한 폴리곤. 나중에 폴리곤 주석으로 쓸 수 있게 보존한다."""
    contours, _ = cv2.findContours(
        mask.astype(np.uint8), cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE
    )
    if not contours:
        return []
    contour = max(contours, key=cv2.contourArea)
    eps = 0.005 * cv2.arcLength(contour, True)
    approx = cv2.approxPolyDP(contour, eps, True)
    while len(approx) > max_points:
        eps *= 1.5
        approx = cv2.approxPolyDP(contour, eps, True)
    return [[float(p[0][0]), float(p[0][1])] for p in approx]


def mask_iou(a: np.ndarray, b: np.ndarray) -> float:
    inter = np.logical_and(a, b).sum()
    if inter == 0:
        return 0.0
    union = np.logical_or(a, b).sum()
    return float(inter) / float(union)


def box_iou(a: tuple[float, ...], b: tuple[float, ...]) -> float:
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    ix = max(0.0, min(ax + aw, bx + bw) - max(ax, bx))
    iy = max(0.0, min(ay + ah, by + bh) - max(ay, by))
    inter = ix * iy
    union = aw * ah + bw * bh - inter
    return inter / union if union > 0 else 0.0


def _nms(dets: list[Detection], iou_thr: float) -> list[Detection]:
    kept: list[Detection] = []
    for det in sorted(dets, key=lambda d: d.score, reverse=True):
        if all(
            (mask_iou(det.mask, k.mask) if det.mask is not None and k.mask is not None
             else box_iou(det.bbox_xywh, k.bbox_xywh)) <= iou_thr
            for k in kept
        ):
            kept.append(det)
    return kept


def _clipped_box(
    box: tuple[float, float, float, float] | None,
    frame_shape: tuple[int, int],
) -> tuple[float, float, float, float] | None:
    """Validate pixel xyxy coordinates, clip to the frame and return xywh.

    Boundary overflow is normal for box detectors. Inverted, zero-sized or
    non-finite predictions are invalid; a valid box wholly outside the frame
    has no visible area and is discarded. No segmentation mask is fabricated.
    """
    if box is None:
        raise ValueError("마스크 없는 검출 결과에는 박스 좌표가 필요합니다.")
    try:
        x0, y0, x1, y1 = (float(value) for value in box)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("검출 박스 좌표는 유한한 숫자 4개여야 합니다.") from exc
    if (not all(math.isfinite(value) for value in (x0, y0, x1, y1))
            or x1 <= x0 or y1 <= y0):
        raise ValueError("검출 박스 좌표는 유한하고 너비와 높이가 양수여야 합니다.")
    h, w = frame_shape
    x0, y0 = max(0.0, x0), max(0.0, y0)
    x1, y1 = min(float(w), x1), min(float(h), y1)
    if x1 <= x0 or y1 <= y0:
        return None
    return x0, y0, x1 - x0, y1 - y0


def build_detections(
    raw: dict[str, list[RawInstance]],
    classes: tuple[ClassDef, ...],
    config: ClassConfig,
    frame_shape: tuple[int, int],
) -> list[Detection]:
    """프롬프트별 원시 결과를 클래스 단위 검출로 정리한다."""
    h, w = frame_shape
    frame_area = float(h * w)
    per_class: dict[str, list[Detection]] = {}
    for cls in classes:
        dets: list[Detection] = []
        for prompt in cls.prompts:
            instances = sorted(raw.get(prompt, []), key=lambda r: r.score, reverse=True)
            for inst in instances[: config.max_detections_per_prompt]:
                if not math.isfinite(inst.score) or not 0 <= inst.score <= 1:
                    raise ValueError("검출 모델이 유효하지 않은 신뢰도를 반환했습니다.")
                if inst.score < cls.threshold:
                    continue
                if inst.mask is None:
                    bbox = _clipped_box(inst.box_xyxy, frame_shape)
                    if bbox is None:
                        continue
                    area = bbox[2] * bbox[3]
                    if area < cls.min_area_ratio * frame_area:
                        continue
                    dets.append(Detection(cls.key, prompt, inst.score, bbox, area=area))
                    continue
                if inst.mask.ndim != 2 or inst.mask.shape != frame_shape:
                    raise ValueError("검출 마스크 크기가 원본 영상 프레임과 일치하지 않습니다.")
                mask = inst.mask.astype(bool)
                area = int(mask.sum())
                if area < cls.min_area_ratio * frame_area:
                    continue
                bbox = mask_to_bbox(mask)
                if bbox is None:
                    continue
                dets.append(Detection(cls.key, prompt, inst.score, bbox, mask, area=area))
        per_class[cls.key] = _nms(
            dets, cls.nms_iou if cls.nms_iou is not None else config.same_class_iou,
        )

    # 같은 suppression_group 안에서는 같은 영역을 두 클래스가 동시에 갖지 않게 한다.
    groups: dict[str, list[Detection]] = {}
    for cls in classes:
        if cls.suppression_group:
            groups.setdefault(cls.suppression_group, []).extend(per_class[cls.key])
    suppressed: set[int] = set()
    for members in groups.values():
        survivors = _nms(members, config.cross_class_iou)
        suppressed.update(id(d) for d in members if all(d is not s for s in survivors))

    out: list[Detection] = []
    for cls in classes:
        for det in per_class[cls.key]:
            if id(det) in suppressed:
                continue
            if det.mask is not None:
                det.polygon = mask_to_polygon(det.mask)
            out.append(det)
    return out
