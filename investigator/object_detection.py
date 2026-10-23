"""Independent object detector taxonomy, annotations, and explicit survey bridging.

Raw detection data is optional and never enters survey values automatically.
"""

from __future__ import annotations

import math
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from investigator.detection_taxonomy import (
    SPECIAL_VEHICLE_TYPES,
    SUBTYPE_BY_KEY,
    SUBTYPES,
    coarse_class_key,
    legacy_subtype,
    special_type_from_prompt,
    subtype_options,
    suggest_subtype,
    taxonomy_hash,
    taxonomy_records,
    taxonomy_version,
)

if TYPE_CHECKING:
    from investigator.form_definition import FormDefinition

ROLE_TRIGGER = "trigger"
ROLE_COLLISION = "collision"
ROLE_BOTH = "both"
ROLES = (ROLE_TRIGGER, ROLE_COLLISION, ROLE_BOTH)
ROLE_TITLES = {ROLE_TRIGGER: "트리거 시점 관여 객체", ROLE_COLLISION: "충돌 관여 객체",
               ROLE_BOTH: "트리거·충돌 공통 객체"}
ROLE_SHORT = {ROLE_TRIGGER: "트리거", ROLE_COLLISION: "충돌", ROLE_BOTH: "공통"}

SOURCE_AUTOMATIC = "automatic_detection"
SOURCE_MANUAL = "manual"

TRIGGER_TAB = "trigger_objects"
COLLISION_TAB = "collision_objects"
TRIGGER_OBJECT_FIELDS = (
    "obstacle_vehicle_types",
    "obstacle_pedestrians",
    "obstacle_road_facilities",
    "obstacle_building_landscape",
    "obstacle_other",
)
COLLISION_TARGET_FIELD = "collision_targets"
OBSTACLE_EXISTS_FIELD = "obstacle_exists"
UNLABELED_TEXT = "라벨 미지정"


@dataclass(frozen=True, slots=True)
class LabelRef:
    """A form choice identified by its field key and stable choice id."""

    field_key: str
    choice_id: str

    def to_dict(self) -> dict[str, str]:
        return {"field": self.field_key, "choice": self.choice_id}

    @classmethod
    def from_dict(cls, raw: Any) -> LabelRef | None:
        if raw is None:
            return None
        if (
            not isinstance(raw, dict)
            or not isinstance(raw.get("field"), str)
            or not isinstance(raw.get("choice"), str)
        ):
            raise TypeError("객체 라벨 형식이 올바르지 않습니다.")
        return cls(raw["field"], raw["choice"])


@dataclass(frozen=True, slots=True)
class DetectionClass:
    key: str
    label: str
    group: str
    prompts: tuple[str, ...]
    object_prefix: str
    trigger: LabelRef | None
    collision: LabelRef | None
    threshold: float = 0.4
    suppression_group: str | None = None
    min_area_ratio: float = 0.0005


def _ref(raw: tuple[str, str] | list[str] | None) -> LabelRef | None:
    return LabelRef(*raw) if raw else None


# Detection vocabulary is fixed and does not follow workspace form edits.
DETECTION_CLASSES: tuple[DetectionClass, ...] = tuple(
    DetectionClass(
        item["key"],
        item["label"],
        item["group"],
        tuple(item["prompts"]),
        item["object_prefix"],
        _ref(item["subtypes"][0].get("trigger")) if len(item["subtypes"]) == 1 else None,
        _ref(item["subtypes"][0].get("collision")) if len(item["subtypes"]) == 1 else None,
        item["threshold"],
        item["suppression_group"],
        item["min_area_ratio"],
    )
    for item in taxonomy_records()
)
CLASS_BY_KEY = {item.key: item for item in DETECTION_CLASSES}
CLASS_GROUPS = tuple(dict.fromkeys(item.group for item in DETECTION_CLASSES))
MANUAL_PREFIX = "U"


def worker_class_config(keys: Iterable[str]) -> dict[str, Any]:
    """Class/prompt configuration for the detection worker (``--config``)."""

    raw_keys = set(keys)
    wanted = {coarse_class_key(key) for key in raw_keys}
    unknown = {key for key in raw_keys if coarse_class_key(key) is None}
    if unknown:
        raise ValueError(f"알 수 없는 검출 클래스: {', '.join(sorted(unknown))}")
    config_records = {item["key"]: item for item in taxonomy_records()}
    return {
        "taxonomy_version": taxonomy_version(),
        "taxonomy_hash": taxonomy_hash(),
        "cross_class_iou": 0.6,
        "same_class_iou": 0.7,
        "max_detections_per_prompt": 30,
        "classes": [
            {
                "key": item.key,
                "label": item.label,
                "prompts": list(item.prompts),
                "object_prefix": item.object_prefix,
                "threshold": item.threshold,
                "suppression_group": item.suppression_group,
                "min_area_ratio": item.min_area_ratio,
                "enabled": True,
                "nms_iou": config_records[item.key]["nms_iou"],
            }
            for item in DETECTION_CLASSES
            if item.key in wanted
        ],
    }


# ---------------------------------------------------------------------------
# Role-dependent label options taken from the workspace form definition.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class LabelOption:
    ref: LabelRef
    text: str
    group: str


def _field_group_title(label: str) -> str:
    """'장애물(도로시설물)' -> '도로시설물'; '장애물(차량) · 차량 종류' -> '차량'."""

    text = label.split("·")[0].strip()
    if "(" in text and text.endswith(")"):
        return text[text.index("(") + 1 : -1].strip() or text
    return text


def label_options(definition: FormDefinition, role: str) -> list[LabelOption]:
    if role == ROLE_BOTH:
        # Legacy LabelRef APIs may still supply a reference from either tab.
        # New common detections store their independent subtype instead.
        return [*label_options(definition, ROLE_TRIGGER),
                *label_options(definition, ROLE_COLLISION)]
    records = {record["key"]: record for record in definition.records()}
    options: list[LabelOption] = []
    if role == ROLE_TRIGGER:
        for key in TRIGGER_OBJECT_FIELDS:
            record = records.get(key)
            if record is None or record.get("kind") != "multi":
                continue
            group = _field_group_title(record["label"])
            for choice in record["choices"]:
                ref = LabelRef(key, choice.get("id", choice["value"]))
                options.append(LabelOption(ref, f"{group} · {choice['label']}", group))
    elif role == ROLE_COLLISION:
        record = records.get(COLLISION_TARGET_FIELD)
        if record is not None and record.get("kind") == "multi":
            for choice in record["choices"]:
                ref = LabelRef(COLLISION_TARGET_FIELD, choice.get("id", choice["value"]))
                text = choice["label"]
                group = text.split("·")[0].strip() if "·" in text else ""
                options.append(LabelOption(ref, text, group))
    else:
        raise ValueError(f"알 수 없는 객체 역할: {role}")
    return options


def label_exists(definition: FormDefinition, role: str, ref: LabelRef | None) -> bool:
    return ref is not None and any(option.ref == ref for option in label_options(definition, role))


def label_text(definition: FormDefinition, role: str | None, ref: LabelRef | None) -> str:
    if role is None or ref is None:
        return UNLABELED_TEXT
    for option in label_options(definition, role):
        if option.ref == ref:
            return option.text
    return f"{UNLABELED_TEXT}(삭제된 항목)"


def subtype_label(subtype_key: str | None, role: str) -> LabelRef | None:
    if role == ROLE_BOTH:
        # A shared record has no single survey-tab reference. Resolve it only
        # when explicitly applying to one of the two investigation tabs.
        return None
    subtype = SUBTYPE_BY_KEY.get(subtype_key or "")
    if subtype is None:
        return None
    return _ref(subtype.trigger if role == ROLE_TRIGGER else subtype.collision)


def subtype_for_label(role: str, ref: LabelRef | None, preferred: str | None = None) -> str | None:
    if ref is None:
        return None
    roles = (ROLE_TRIGGER, ROLE_COLLISION) if role == ROLE_BOTH else (role,)
    matches = [item.key for item in SUBTYPES
               if any(subtype_label(item.key, target) == ref for target in roles)]
    return preferred if preferred in matches else (matches[0] if matches else None)


def default_label(definition: FormDefinition, class_key: str | None, role: str) -> LabelRef | None:
    subtype = legacy_subtype(class_key)
    if subtype is None:
        subtype = suggest_subtype(class_key or "", None)
    ref = subtype_label(subtype, role)
    return ref if label_exists(definition, role, ref) else None


def convert_label(
    definition: FormDefinition,
    ref: LabelRef | None,
    from_role: str | None,
    to_role: str,
    class_key: str | None = None,
) -> LabelRef | None:
    if from_role == to_role:
        return ref
    subtype = subtype_for_label(from_role or "", ref, legacy_subtype(class_key))
    converted = subtype_label(subtype, to_role)
    return converted if label_exists(definition, to_role, converted) else None


def class_for_label(role: str, ref: LabelRef | None) -> str | None:
    subtype = subtype_for_label(role, ref)
    return SUBTYPE_BY_KEY[subtype].class_key if subtype else None


# ---------------------------------------------------------------------------
# Per-video object storage.
# ---------------------------------------------------------------------------


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _finite(value: Any, name: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{name} 값은 숫자여야 합니다.")
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{name} 값은 유한한 숫자여야 합니다.") from exc
    if not math.isfinite(number):
        raise ValueError(f"{name} 값이 유한한 수가 아닙니다.")
    return number


def _frame_number(value: Any) -> int:
    number = _finite(value, "프레임")
    if number < 0 or not number.is_integer():
        raise ValueError("프레임 번호는 0 이상의 정수여야 합니다.")
    return int(number)


@dataclass(frozen=True, slots=True)
class ObjectBox:
    frame: int
    bbox: tuple[float, float, float, float]  # x, y, width, height in source pixels
    score: float | None = None
    source_prompt: str | None = None

    def __post_init__(self) -> None:
        frame = _frame_number(self.frame)
        if not isinstance(self.bbox, (list, tuple)) or len(self.bbox) != 4:
            raise TypeError("객체 박스 좌표는 숫자 4개여야 합니다.")
        x, y, w, h = (_finite(value, "박스 좌표") for value in self.bbox)
        if x < 0 or y < 0 or w <= 0 or h <= 0:
            raise ValueError("객체 박스는 원본 영상 안의 양수 크기 좌표여야 합니다.")
        score = None if self.score is None else _finite(self.score, "신뢰도")
        if score is not None and not 0 <= score <= 1:
            raise ValueError("신뢰도는 0~1 사이여야 합니다.")
        object.__setattr__(self, "frame", frame)
        object.__setattr__(self, "bbox", (x, y, w, h))
        object.__setattr__(self, "score", score)
        if self.source_prompt is not None and not isinstance(self.source_prompt, str):
            raise TypeError("원본 프롬프트는 문자열이어야 합니다.")

    def to_dict(self) -> dict[str, Any]:
        # Editing a label must not round or otherwise alter source coordinates.
        result: dict[str, Any] = {
            "frame": self.frame,
            "bbox": list(self.bbox),
            "source_prompt": self.source_prompt,
        }
        if self.score is not None:
            result["score"] = self.score
        return result

    @classmethod
    def from_dict(cls, raw: Any) -> ObjectBox:
        if not isinstance(raw, dict):
            raise TypeError("객체 박스는 JSON 객체여야 합니다.")
        bbox = raw.get("bbox")
        if not isinstance(bbox, list) or len(bbox) != 4:
            raise TypeError("객체 박스 좌표는 숫자 4개여야 합니다.")
        x, y, w, h = (_finite(v, "박스 좌표") for v in bbox)
        if w <= 0 or h <= 0:
            raise ValueError("객체 박스 크기는 0보다 커야 합니다.")
        score = raw.get("score")
        if raw.get("frame") is None:
            raise TypeError("객체 박스에 프레임 번호가 없습니다.")
        return cls(
            _frame_number(raw["frame"]),
            (x, y, w, h),
            None if score is None else _finite(score, "신뢰도"),
            raw.get("source_prompt"),
        )


OBJECT_SCHEMA_VERSION = 2


@dataclass(frozen=True, slots=True)
class ObjectAnnotation:
    subtype_key: str | None = None
    reviewed: bool = False
    special_vehicle_type: str | None = None
    subtype_source: str = "unassigned"

    def to_dict(self) -> dict[str, Any]:
        return {
            "subtype_key": self.subtype_key,
            "reviewed": self.reviewed,
            "special_vehicle_type": self.special_vehicle_type,
            "subtype_source": self.subtype_source,
        }

    @classmethod
    def from_dict(cls, raw: Any) -> ObjectAnnotation:
        if not isinstance(raw, dict):
            raise TypeError("프레임별 세분류는 JSON 객체여야 합니다.")
        subtype = raw.get("subtype_key")
        special = raw.get("special_vehicle_type")
        if subtype is not None and (not isinstance(subtype, str) or not subtype):
            raise TypeError("세분류 키는 문자열이어야 합니다.")
        if subtype is not None and subtype not in SUBTYPE_BY_KEY:
            raise ValueError("알 수 없는 객체 세분류입니다.")
        if special is not None and special not in SPECIAL_VEHICLE_TYPES:
            raise ValueError("알 수 없는 특수차량 상세 종류입니다.")
        if special is not None and subtype != "special":
            raise ValueError("특수차량 상세 종류는 특수 세분류에만 지정할 수 있습니다.")
        source = raw.get("subtype_source", "unassigned")
        if source not in {"unassigned", "default", "user", "legacy", "legacy_unmapped"}:
            raise ValueError("알 수 없는 세분류 출처입니다.")
        if "reviewed" in raw and not isinstance(raw["reviewed"], bool):
            raise TypeError("검수 완료 여부는 참/거짓이어야 합니다.")
        return cls(subtype, bool(raw.get("reviewed", False)), special, source)


@dataclass(slots=True)
class DetectedObject:
    uid: str
    object_id: str
    role: str
    label: LabelRef | None
    boxes: list[ObjectBox]
    class_key: str | None = None
    source: str = SOURCE_MANUAL
    run_id: str | None = None
    reviewed: bool = False
    subtype_key: str | None = None
    suggested_subtype_key: str | None = None
    subtype_source: str = "unassigned"
    special_vehicle_type: str | None = None
    frame_annotations: dict[int, ObjectAnnotation] = field(default_factory=dict)
    legacy_class_key: str | None = None
    source_prompt_hint: str | None = None

    def __post_init__(self) -> None:
        if self.source_prompt_hint is not None and not isinstance(self.source_prompt_hint, str):
            raise TypeError("원본 프롬프트는 문자열이어야 합니다.")
        original = self.class_key
        coarse = coarse_class_key(original)
        if original and coarse != original:
            # Direct constructors used by old integrations are migrated too.
            self.legacy_class_key = self.legacy_class_key or original
            inferred = subtype_for_label(self.role, self.label, legacy_subtype(original))
            self.subtype_key = self.subtype_key or inferred or legacy_subtype(original)
            self.subtype_source = "legacy"
        self.class_key = coarse or original

    @property
    def model_class_key(self) -> str | None:
        return self.class_key

    @property
    def source_prompt(self) -> str | None:
        observed = [box for box in self.boxes if box.source_prompt]
        return (
            max(observed, key=lambda box: box.score or 0).source_prompt
            if observed
            else self.source_prompt_hint
        )

    @property
    def frames(self) -> list[int]:
        return sorted({box.frame for box in self.boxes})

    @property
    def first_frame(self) -> int:
        return min(box.frame for box in self.boxes)

    @property
    def last_frame(self) -> int:
        return max(box.frame for box in self.boxes)

    @property
    def mean_score(self) -> float | None:
        scores = [box.score for box in self.boxes if box.score is not None]
        return sum(scores) / len(scores) if scores else None

    def box_at(self, frame: int) -> ObjectBox | None:
        return next((box for box in self.boxes if box.frame == frame), None)

    def nearest_frame(self, frame: int) -> int:
        return min(self.frames, key=lambda candidate: (abs(candidate - frame), candidate))

    def metadata_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "object_schema_version": OBJECT_SCHEMA_VERSION,
            "uid": self.uid,
            "object_id": self.object_id,
            "role": self.role,
            "label": None if self.label is None else self.label.to_dict(),
            "source": self.source,
            "reviewed": self.reviewed,
            "model_class_key": self.class_key,
            "source_prompt": self.source_prompt,
            "subtype_key": self.subtype_key,
            "suggested_subtype_key": self.suggested_subtype_key,
            "subtype_source": self.subtype_source,
            "special_vehicle_type": self.special_vehicle_type,
            "frame_annotations": {
                str(frame): annotation.to_dict()
                for frame, annotation in sorted(self.frame_annotations.items())
            },
        }
        if self.class_key:
            result["class_key"] = self.class_key
        if self.run_id:
            result["run_id"] = self.run_id
        if self.legacy_class_key:
            result["legacy_class_key"] = self.legacy_class_key
        return result

    def to_dict(self) -> dict[str, Any]:
        result = self.metadata_dict()
        result["boxes"] = [box.to_dict() for box in sorted(self.boxes, key=lambda b: b.frame)]
        return result

    @classmethod
    def from_dict(cls, raw: Any) -> DetectedObject:
        if not isinstance(raw, dict):
            raise TypeError("검출 객체는 JSON 객체여야 합니다.")
        version = raw.get(
            "object_schema_version",
            2
            if any(key in raw for key in ("subtype_key", "model_class_key", "frame_annotations"))
            else 1,
        )
        if isinstance(version, bool) or version not in (1, OBJECT_SCHEMA_VERSION):
            raise ValueError("지원하지 않는 객체 데이터 버전입니다.")
        role = raw.get("role")
        if role not in ROLES:
            raise ValueError(f"알 수 없는 객체 역할입니다: {role}")
        raw_boxes = raw.get("boxes")
        if not isinstance(raw_boxes, list) or not raw_boxes:
            raise TypeError("검출 객체에는 박스가 하나 이상 필요합니다.")
        boxes: dict[int, ObjectBox] = {}
        for value in raw_boxes:
            box = ObjectBox.from_dict(value)
            boxes[box.frame] = box
        source = raw.get("source", SOURCE_MANUAL)
        if source not in {SOURCE_AUTOMATIC, SOURCE_MANUAL}:
            raise ValueError(f"알 수 없는 객체 출처입니다: {source}")
        original = raw.get("model_class_key", raw.get("class_key"))
        original = original if isinstance(original, str) and original else None
        coarse = coarse_class_key(original) or original
        label = LabelRef.from_dict(raw.get("label"))
        legacy_key = raw.get("legacy_class_key")
        if version == 1:
            # Never consult editable form text or invent a lost source prompt.
            inferred = subtype_for_label(role, label, legacy_subtype(original))
            subtype = inferred or (None if label is not None else legacy_subtype(original))
            if subtype is None and label is None:
                subtype = suggest_subtype(coarse or "", raw.get("source_prompt"))
            annotation = ObjectAnnotation(
                subtype,
                bool(raw.get("reviewed", False)),
                special_type_from_prompt(raw.get("source_prompt") or original)
                if subtype == "special"
                else None,
                "legacy" if subtype else ("legacy_unmapped" if label else "unassigned"),
            )
            suggested = legacy_subtype(original)
            legacy_key = legacy_key or original
        else:
            annotation = ObjectAnnotation.from_dict(raw)
            suggested = raw.get("suggested_subtype_key")
            if suggested is not None and (
                not isinstance(suggested, str) or suggested not in SUBTYPE_BY_KEY
            ):
                raise ValueError("알 수 없는 추천 세분류입니다.")
        frame_raw = raw.get("frame_annotations", {})
        if not isinstance(frame_raw, dict):
            raise TypeError("프레임별 세분류는 JSON 객체여야 합니다.")
        annotations = {}
        for frame, value in frame_raw.items():
            number = _frame_number(frame)
            if number not in boxes:
                raise ValueError("박스가 없는 프레임에 세분류가 지정되어 있습니다.")
            annotations[number] = ObjectAnnotation.from_dict(value)
        return cls(
            uid=str(raw.get("uid") or uuid4().hex),
            object_id=str(raw.get("object_id") or ""),
            role=role,
            label=label,
            boxes=[boxes[frame] for frame in sorted(boxes)],
            class_key=coarse,
            source=source,
            run_id=str(raw["run_id"]) if raw.get("run_id") else None,
            reviewed=annotation.reviewed,
            subtype_key=annotation.subtype_key,
            suggested_subtype_key=suggested,
            subtype_source=annotation.subtype_source,
            special_vehicle_type=annotation.special_vehicle_type,
            frame_annotations=annotations,
            legacy_class_key=legacy_key,
            source_prompt_hint=raw.get("source_prompt")
            if not any(box.source_prompt for box in boxes.values())
            else None,
        )


def effective_annotation(item: DetectedObject, frame: int) -> ObjectAnnotation:
    return item.frame_annotations.get(
        frame,
        ObjectAnnotation(
            item.subtype_key,
            item.reviewed,
            item.special_vehicle_type,
            item.subtype_source,
        ),
    )


def object_subtypes(item: DetectedObject) -> set[str | None]:
    return {effective_annotation(item, box.frame).subtype_key for box in item.boxes}


def _protected_annotation(item: DetectedObject, frame: int) -> bool:
    annotation = effective_annotation(item, frame)
    if annotation.reviewed or annotation.subtype_source in {"user", "legacy_unmapped"}:
        return True
    # Legacy corrected labels must not be replaced merely because old files
    # did not mark them reviewed. Default legacy predictions remain replaceable.
    return bool(
        annotation.subtype_source == "legacy"
        and item.legacy_class_key
        and annotation.subtype_key != legacy_subtype(item.legacy_class_key)
    )


def object_display_text(item: DetectedObject, frame: int | None = None) -> str:
    keys = (
        object_subtypes(item) if frame is None else {effective_annotation(item, frame).subtype_key}
    )
    coarse = CLASS_BY_KEY.get(item.class_key or "")
    group = coarse.label if coarse else "수동 객체"
    if len(keys) > 1:
        return f"{group} · 혼합 (반영 불가)"
    key = next(iter(keys), None)
    subtype = SUBTYPE_BY_KEY.get(key or "")
    if subtype is None:
        return f"{group} · 세분류 미지정"
    return subtype.label if group == subtype.label else f"{group} · {subtype.label}"


@dataclass(slots=True)
class DetectionRun:
    run_id: str
    role: str
    start_frame: int
    end_frame: int  # inclusive
    frame_step: int
    classes: list[str]
    detector: str = ""
    model_id: str = ""
    device: str = ""
    created_at: str = field(default_factory=utc_now)
    object_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "role": self.role,
            "start_frame": self.start_frame,
            "end_frame": self.end_frame,
            "frame_step": self.frame_step,
            "classes": list(self.classes),
            "detector": self.detector,
            "model_id": self.model_id,
            "device": self.device,
            "created_at": self.created_at,
            "object_count": self.object_count,
        }

    @classmethod
    def from_dict(cls, raw: Any) -> DetectionRun:
        if not isinstance(raw, dict):
            raise TypeError("검출 실행 기록은 JSON 객체여야 합니다.")
        if not raw.get("run_id"):
            raise TypeError("검출 실행 기록에 run_id가 없습니다.")
        return cls(
            run_id=str(raw["run_id"]),
            role=str(raw.get("role", ROLE_TRIGGER)),
            start_frame=max(0, int(raw.get("start_frame", 0))),
            end_frame=max(0, int(raw.get("end_frame", 0))),
            frame_step=max(1, int(raw.get("frame_step", 1))),
            classes=[str(item) for item in raw.get("classes", [])],
            detector=str(raw.get("detector", "")),
            model_id=str(raw.get("model_id", "")),
            device=str(raw.get("device", "")),
            created_at=str(raw.get("created_at") or utc_now()),
            object_count=max(0, int(raw.get("object_count", 0))),
        )


@dataclass(slots=True)
class VideoObjects:
    objects: list[DetectedObject] = field(default_factory=list)
    runs: list[DetectionRun] = field(default_factory=list)

    def is_empty(self) -> bool:
        return not self.objects and not self.runs

    def to_dict(self) -> dict[str, Any]:
        return {
            "object_schema_version": OBJECT_SCHEMA_VERSION,
            "objects": [item.to_dict() for item in self.objects],
            "runs": [run.to_dict() for run in self.runs],
        }

    @classmethod
    def from_dict(cls, raw: Any) -> VideoObjects:
        if not isinstance(raw, dict):
            raise TypeError("영상 객체 데이터는 JSON 객체여야 합니다.")
        version = raw.get("object_schema_version", 1)
        if isinstance(version, bool) or version not in (1, OBJECT_SCHEMA_VERSION):
            raise ValueError("지원하지 않는 객체 데이터 버전입니다.")
        raw_objects = raw.get("objects", [])
        raw_runs = raw.get("runs", [])
        if not isinstance(raw_objects, list) or not isinstance(raw_runs, list):
            raise TypeError("objects/runs 항목은 배열이어야 합니다.")
        objects = [
            DetectedObject.from_dict(
                {"object_schema_version": version, **item}
                if version == OBJECT_SCHEMA_VERSION and isinstance(item, dict)
                else item
            )
            for item in raw_objects
        ]
        uids = [item.uid for item in objects]
        if len(uids) != len(set(uids)):
            raise ValueError("중복된 객체 UID가 있습니다.")
        return cls(objects, [DetectionRun.from_dict(item) for item in raw_runs])

    def copy(self) -> VideoObjects:
        return VideoObjects.from_dict(self.to_dict())

    def find(self, uid: str) -> DetectedObject | None:
        return next((item for item in self.objects if item.uid == uid), None)

    def at_frame(self, frame: int) -> list[tuple[DetectedObject, ObjectBox]]:
        result = []
        for item in self.objects:
            box = item.box_at(frame)
            if box is not None:
                result.append((item, box))
        return result


def normalize_objects_by_video(raw: Any) -> dict[str, dict[str, Any]]:
    """Validate the persisted mapping, dropping empty per-video entries."""

    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise TypeError("objects_by_video 항목은 JSON 객체여야 합니다.")
    result: dict[str, dict[str, Any]] = {}
    for key, value in raw.items():
        video_objects = VideoObjects.from_dict(value)
        if not video_objects.is_empty():
            result[str(key)] = video_objects.to_dict()
    return result


def next_object_id(prefix: str, objects: Iterable[DetectedObject]) -> str:
    used = 0
    for item in objects:
        if item.object_id.startswith(prefix) and item.object_id[len(prefix) :].isdigit():
            used = max(used, int(item.object_id[len(prefix) :]))
    return f"{prefix}{used + 1:03d}"


def merge_detection_result(
    current: VideoObjects,
    document: dict[str, Any],
    *,
    role: str,
    definition: FormDefinition,
    start_frame: int,
    end_frame: int,
    frame_step: int,
    classes: list[str],
) -> tuple[VideoObjects, int, int]:
    """Merge a worker result document into a copy of ``current``.

    Unreviewed automatic boxes inside the processed range are replaced. A
    common run replaces old automatic results from either role, without
    duplicating the worker inference or the resulting object tracks. Manual
    and corrected/reviewed annotations remain protected. Legacy role-specific
    callers continue to replace only the requested role.
    Returns (objects, added, replaced). Inputs are never modified on failure.
    """

    if role not in ROLES:
        raise ValueError(f"알 수 없는 객체 역할: {role}")
    if not isinstance(document, dict) or not isinstance(document.get("detections"), list):
        raise ValueError("검출 결과 문서 형식이 올바르지 않습니다.")
    start_frame, end_frame = _frame_number(start_frame), _frame_number(end_frame)
    frame_step = _frame_number(frame_step)
    if start_frame > end_frame or frame_step < 1:
        raise ValueError("검출 구간 또는 프레임 간격이 올바르지 않습니다.")
    if not classes or any(coarse_class_key(key) not in CLASS_BY_KEY for key in classes):
        raise ValueError("검출 클래스를 하나 이상 올바르게 지정해 주세요.")
    wanted_classes = {coarse_class_key(key) for key in classes}
    source_size = None
    video = document.get("video")
    if isinstance(video, dict) and "width" in video and "height" in video:
        width = _finite(video["width"], "영상 너비")
        height = _finite(video["height"], "영상 높이")
        if width <= 0 or height <= 0:
            raise ValueError("원본 영상 크기가 올바르지 않습니다.")
        source_size = (width, height)
    result = current.copy()
    replaced = 0
    kept: list[DetectedObject] = []
    for item in result.objects:
        if (item.source == SOURCE_AUTOMATIC
                and (role != ROLE_BOTH or item.class_key in wanted_classes)
                and (role == ROLE_BOTH or item.role == role)):
            remaining = [
                box
                for box in item.boxes
                if (
                    not start_frame <= box.frame <= end_frame
                    or _protected_annotation(item, box.frame)
                )
            ]
            if len(remaining) != len(item.boxes):
                replaced += 1
            if not remaining:
                continue
            item.boxes = remaining
            item.frame_annotations = {
                frame: value
                for frame, value in item.frame_annotations.items()
                if any(box.frame == frame for box in remaining)
            }
        kept.append(item)
    result.objects = kept

    provenance = (
        document.get("provenance", {}) if isinstance(document.get("provenance"), dict) else {}
    )
    run = DetectionRun(
        run_id=uuid4().hex,
        role=role,
        start_frame=start_frame,
        end_frame=end_frame,
        frame_step=frame_step,
        classes=list(classes),
        detector=str(provenance.get("detector", "")),
        model_id=str(provenance.get("model_id", "")),
        device=str(provenance.get("device", "")),
    )
    # Reserve IDs from the complete pre-merge document, including automatic
    # tracks removed above. Scanning every old/new object for each new track
    # made merging thousands of short tracks quadratic.
    prefixes = {CLASS_BY_KEY[key].object_prefix for key in wanted_classes}
    id_highwater = dict.fromkeys(prefixes, 0)
    for item in current.objects:
        for prefix in prefixes:
            suffix = item.object_id[len(prefix) :]
            if item.object_id.startswith(prefix) and suffix.isdigit():
                id_highwater[prefix] = max(id_highwater[prefix], int(suffix))
    grouped: dict[str, DetectedObject] = {}
    legacy_evidence: dict[str, tuple[float, str]] = {}
    boxes_by_group: dict[str, dict[int, ObjectBox]] = {}
    for det in document["detections"]:
        if not isinstance(det, dict):
            raise ValueError("검출 결과 항목 형식이 올바르지 않습니다.")
        frame = _frame_number(det["frame_index"])
        if not start_frame <= frame <= end_frame:
            continue
        original_key = det.get("class_key")
        class_key = coarse_class_key(original_key)
        if class_key not in wanted_classes:
            raise ValueError("요청하지 않은 검출 클래스가 결과에 포함되어 있습니다.")
        metadata = det.get("metadata") if isinstance(det.get("metadata"), dict) else {}
        prompt = det.get("source_prompt", metadata.get("prompt"))
        box = ObjectBox.from_dict(
            {
                "frame": frame,
                "bbox": det.get("bbox_xywh"),
                "score": det.get("score"),
                "source_prompt": prompt,
            }
        )
        x, y, w, h = box.bbox
        if source_size is not None and (
            x + w > source_size[0] + 0.1 or y + h > source_size[1] + 0.1
        ):
            raise ValueError("검출 박스가 원본 영상 크기를 벗어났습니다.")
        group_key = f"{class_key}:{det.get('object_id') or det.get('track_id') or uuid4().hex}"
        legacy = legacy_subtype(original_key)
        if legacy and (
            group_key not in legacy_evidence or ((box.score or 0) > legacy_evidence[group_key][0])
        ):
            legacy_evidence[group_key] = (box.score or 0, legacy)
        obj = grouped.get(group_key)
        if obj is None:
            prefix = CLASS_BY_KEY[class_key].object_prefix
            object_id = f"{prefix}{id_highwater[prefix] + 1:03d}"
            for reserved_prefix in prefixes:
                suffix = object_id[len(reserved_prefix) :]
                if object_id.startswith(reserved_prefix) and suffix.isdigit():
                    id_highwater[reserved_prefix] = max(id_highwater[reserved_prefix], int(suffix))
            obj = DetectedObject(
                uid=uuid4().hex,
                # A newly detected track must not inherit the ID of an object
                # that this run just replaced entirely.
                object_id=object_id,
                role=role,
                label=None,
                boxes=[],
                class_key=class_key,
                source=SOURCE_AUTOMATIC,
                run_id=run.run_id,
            )
            grouped[group_key] = obj
            boxes_by_group[group_key] = {}
        frame_boxes = boxes_by_group[group_key]
        existing = frame_boxes.get(frame)
        if existing is None or (box.score or 0) > (existing.score or 0):
            frame_boxes[frame] = box
    for key, obj in grouped.items():
        frame_boxes = boxes_by_group[key]
        obj.boxes = [frame_boxes[frame] for frame in sorted(frame_boxes)]
        strongest = max(obj.boxes, key=lambda box: box.score or 0)
        suggested = suggest_subtype(obj.class_key or "", strongest.source_prompt)
        # Old external worker documents retain explicit class evidence, but
        # never turn a modern pedestrian prompt into an age guess.
        if suggested is None:
            evidence = legacy_evidence.get(key)
            if evidence:
                suggested = evidence[1]
        obj.suggested_subtype_key = suggested
        obj.subtype_key = suggested
        obj.subtype_source = "default" if suggested else "unassigned"
        obj.special_vehicle_type = (
            special_type_from_prompt(strongest.source_prompt) if suggested == "special" else None
        )
        obj.label = subtype_label(suggested, role)
    run.object_count = len(grouped)
    result.objects.extend(grouped.values())
    result.runs.append(run)
    return result, len(grouped), replaced


def add_manual_object(
    current: VideoObjects,
    frame: int,
    bbox: tuple[float, float, float, float],
    role: str,
    label: LabelRef | None,
) -> tuple[VideoObjects, DetectedObject]:
    if role not in ROLES:
        raise ValueError(f"알 수 없는 객체 역할: {role}")
    box = ObjectBox(frame, bbox)
    _, _, w, h = box.bbox
    if w < 2 or h < 2:
        raise ValueError("사각형이 너무 작습니다.")
    result = current.copy()
    item = DetectedObject(
        uid=uuid4().hex,
        object_id=next_object_id(MANUAL_PREFIX, result.objects),
        role=role,
        label=label,
        boxes=[box],
        class_key=class_for_label(role, label),
        source=SOURCE_MANUAL,
        reviewed=True,
        subtype_key=subtype_for_label(role, label),
        subtype_source="user" if label else "unassigned",
    )
    result.objects.append(item)
    return result, item


def set_objects_role(
    current: VideoObjects,
    uids: Iterable[str],
    role: str,
    definition: FormDefinition,
) -> VideoObjects:
    if role not in ROLES:
        raise ValueError(f"알 수 없는 객체 역할: {role}")
    result = current.copy()
    targets = set(uids)
    for item in result.objects:
        if item.uid in targets and item.role != role:
            item.role = role
            item.label = subtype_label(item.subtype_key, role)
    return result


def set_objects_subtype(
    current: VideoObjects,
    uids: Iterable[str],
    subtype_key: str | None,
    *,
    frame: int | None = None,
    special_vehicle_type: str | None = None,
) -> VideoObjects:
    """Set independent subtype for whole IDs or their current-frame boxes.

    This never reads or modifies the workspace survey definition or values.
    """
    if subtype_key is not None and subtype_key not in SUBTYPE_BY_KEY:
        raise ValueError("알 수 없는 객체 세분류입니다.")
    annotation = ObjectAnnotation.from_dict(
        {
            "subtype_key": subtype_key,
            "reviewed": False,
            "special_vehicle_type": special_vehicle_type,
            "subtype_source": "user",
        }
    )
    result = current.copy()
    targets = set(uids)
    for item in result.objects:
        if item.uid not in targets:
            continue
        if frame is not None and item.box_at(frame) is None:
            continue
        subtype = SUBTYPE_BY_KEY.get(subtype_key or "")
        if subtype and item.source != SOURCE_MANUAL and subtype.class_key != item.class_key:
            raise ValueError("모델 대분류에 맞는 세분류를 선택해 주세요.")
        if subtype and item.source == SOURCE_MANUAL:
            # A manually drawn object has no model classification to preserve.
            # Prevent creating incompatible cross-category frame overrides.
            if frame is not None and item.class_key and subtype.class_key != item.class_key:
                raise ValueError("현재 프레임만 수정할 때는 기존 대분류의 세분류를 선택해 주세요.")
            item.class_key = subtype.class_key
        previous = (
            [effective_annotation(item, box.frame) for box in item.boxes]
            if frame is None
            else [effective_annotation(item, frame)]
        )
        if all(
            old.subtype_key == subtype_key and old.special_vehicle_type == special_vehicle_type
            for old in previous
        ):
            continue  # An unchanged choice must not toggle review or invalidate redo.
        if frame is None:
            item.subtype_key = annotation.subtype_key
            item.reviewed = annotation.reviewed
            item.special_vehicle_type = annotation.special_vehicle_type
            item.subtype_source = annotation.subtype_source
            item.frame_annotations.clear()
            item.label = subtype_label(subtype_key, item.role)
        else:
            item.frame_annotations[_frame_number(frame)] = annotation
    return result


def set_objects_label(
    current: VideoObjects,
    uids: Iterable[str],
    label: LabelRef | None,
    definition: FormDefinition,
) -> VideoObjects:
    """Legacy bridge for callers supplying survey references instead of subtypes."""
    result = current.copy()
    targets = set(uids)
    for uid in targets:
        item = result.find(uid)
        if item is None:
            continue
        if label is not None and not label_exists(definition, item.role, label):
            raise ValueError("객체 역할에 맞지 않는 라벨입니다.")
        subtype = subtype_for_label(item.role, label, item.subtype_key)
        if subtype is not None:
            result = set_objects_subtype(result, [item.uid], subtype)
            # Retain the old API's explicit label-confirmation semantics. New
            # subtype controls use a separate review action.
            result.find(item.uid).reviewed = True
        else:
            item.label = label  # Retain unknown legacy/custom corrections.
            item.subtype_key = None
            item.subtype_source = "legacy_unmapped" if label else "user"
            item.frame_annotations.clear()
            item.reviewed = True
    return result


def set_objects_reviewed(
    current: VideoObjects,
    uids: Iterable[str],
    reviewed: bool,
    *,
    frame: int | None = None,
) -> VideoObjects:
    result = current.copy()
    targets = set(uids)
    for item in result.objects:
        if item.uid not in targets:
            continue
        if frame is None:
            item.reviewed = bool(reviewed)
            item.frame_annotations = {
                number: ObjectAnnotation(
                    a.subtype_key, bool(reviewed), a.special_vehicle_type, a.subtype_source
                )
                for number, a in item.frame_annotations.items()
            }
        elif item.box_at(frame) is not None:
            old = effective_annotation(item, frame)
            item.frame_annotations[_frame_number(frame)] = ObjectAnnotation(
                old.subtype_key,
                bool(reviewed),
                old.special_vehicle_type,
                old.subtype_source,
            )
    return result


def split_object_frames(
    current: VideoObjects,
    uid: str,
    frames: Iterable[int],
) -> tuple[VideoObjects, DetectedObject]:
    """Move selected boxes to a fresh ID without altering any source geometry."""
    result = current.copy()
    item = result.find(uid)
    if item is None:
        raise ValueError("분리할 객체를 찾을 수 없습니다.")
    wanted = {_frame_number(frame) for frame in frames}
    selected = [box for box in item.boxes if box.frame in wanted]
    if not selected:
        raise ValueError("분리할 프레임의 박스를 선택해 주세요.")
    payload = item.to_dict()
    payload["uid"] = uuid4().hex
    definition = CLASS_BY_KEY.get(item.class_key or "")
    prefix = definition.object_prefix if definition else MANUAL_PREFIX
    payload["object_id"] = next_object_id(prefix, result.objects)
    payload["boxes"] = [box.to_dict() for box in selected]
    payload["frame_annotations"] = {
        str(frame): annotation.to_dict()
        for frame, annotation in item.frame_annotations.items()
        if frame in wanted
    }
    new = DetectedObject.from_dict(payload)
    item.boxes = [box for box in item.boxes if box.frame not in wanted]
    item.frame_annotations = {
        frame: annotation
        for frame, annotation in item.frame_annotations.items()
        if frame not in wanted
    }
    position = result.objects.index(item)
    if not item.boxes:
        result.objects.remove(item)
    else:
        position += 1
    result.objects.insert(position, new)
    return result, new


def delete_objects(current: VideoObjects, uids: Iterable[str]) -> VideoObjects:
    targets = set(uids)
    result = current.copy()
    result.objects = [item for item in result.objects if item.uid not in targets]
    return result


def delete_box(current: VideoObjects, uid: str, frame: int) -> VideoObjects:
    """Remove one frame's box; the object disappears with its last box."""

    result = current.copy()
    for item in list(result.objects):
        if item.uid == uid:
            item.boxes = [box for box in item.boxes if box.frame != frame]
            item.frame_annotations.pop(frame, None)
            if not item.boxes:
                result.objects.remove(item)
    return result


@dataclass(frozen=True, slots=True)
class FormUpdate:
    field_key: str
    stored_values: list[str]
    added_texts: list[str]


def applies_to_role(item: DetectedObject, role: str) -> bool:
    """Common detections are eligible for either explicit survey-tab action."""
    return role in (ROLE_TRIGGER, ROLE_COLLISION) and item.role in (role, ROLE_BOTH)


def form_updates(
    objects: Iterable[DetectedObject],
    role: str,
    definition: FormDefinition,
    values: dict[str, Any],
) -> list[FormUpdate]:
    """Choices to add for common objects and legacy objects of the target role.

    Fixed detector subtypes bridge to existing stable survey choice IDs only.
    Missing/mixed subtypes and unavailable survey choices fail atomically.
    """

    records = {record["key"]: record for record in definition.records()}
    wanted: dict[str, list[str]] = {}
    if role not in (ROLE_TRIGGER, ROLE_COLLISION):
        raise ValueError("반영할 조사 탭을 선택해 주세요.")
    candidates = [item for item in objects if applies_to_role(item, role)]
    # Validate the complete selection first; no partial form mutation is allowed.
    per_id: dict[str, set[str | None]] = {}
    for item in candidates:
        per_id.setdefault(item.object_id, set()).update(object_subtypes(item))
    for object_id, subtypes in per_id.items():
        if len(subtypes) > 1:
            raise ValueError(
                f"{object_id}: 같은 ID에 서로 다른 세분류가 있습니다. "
                "ID를 분리하거나 세분류를 통일해 주세요."
            )
        if not subtypes or None in subtypes:
            raise ValueError(f"{object_id}: 세분류를 먼저 선택해 주세요.")
    for item in candidates:
        subtype_key = next(iter(object_subtypes(item)))
        subtype = SUBTYPE_BY_KEY.get(subtype_key or "")
        if subtype is None:
            raise ValueError(f"{item.object_id}: 세분류를 확인해 주세요.")
        if item.class_key and subtype.class_key != item.class_key:
            raise ValueError(
                f"{item.object_id}: 기존 라벨이 모델 대분류와 일치하지 않습니다. "
                "세분류를 다시 선택해 주세요."
            )
        ref = subtype_label(subtype_key, role)
        if not label_exists(definition, role, ref):
            if role == ROLE_TRIGGER and subtype_key == "pm":
                raise ValueError("[객체] 탭에는 PM 선택지가 없어 반영할 수 없습니다.")
            tab = "객체" if role == ROLE_TRIGGER else "충돌"
            raise ValueError(
                f"{item.object_id}: [{tab}] 탭에 '{subtype.label}' 선택지가 없어 반영할 수 없습니다."
            )
        assert ref is not None
        ids = wanted.setdefault(ref.field_key, [])
        if ref.choice_id not in ids:
            ids.append(ref.choice_id)
    canonical = definition.canonical_values(values)
    updates: list[FormUpdate] = []
    for field_key, ids in wanted.items():
        record = records[field_key]
        existing = canonical.get(field_key)
        current_ids = (
            list(existing)
            if isinstance(existing, list)
            else ([existing] if isinstance(existing, str) and existing.strip() else [])
        )
        if existing is not None and not isinstance(existing, (list, str)):
            # Never silently replace legacy/custom non-choice data.
            continue
        new_ids = [choice_id for choice_id in ids if choice_id not in current_ids]
        if not new_ids:
            continue
        merged_ids = [*current_ids, *new_ids]
        # Keep the form's choice order for a stable CSV/UI presentation.
        order = [c.get("id", c["value"]) for c in record["choices"]]
        merged_ids.sort(key=lambda value: order.index(value) if value in order else len(order))
        texts = [
            next(c["label"] for c in record["choices"] if c.get("id", c["value"]) == choice_id)
            for choice_id in new_ids
        ]
        updates.append(
            FormUpdate(
                field_key,
                [definition.stored_value(field_key, choice_id) for choice_id in merged_ids],
                texts,
            )
        )
    return updates


__all__ = [
    "OBJECT_SCHEMA_VERSION",
    "ObjectAnnotation",
    "effective_annotation",
    "object_subtypes",
    "object_display_text",
    "subtype_label",
    "subtype_options",
    "subtype_for_label",
    "set_objects_subtype",
    "split_object_frames",
    "CLASS_BY_KEY",
    "CLASS_GROUPS",
    "DETECTION_CLASSES",
    "ROLES",
    "ROLE_COLLISION",
    "ROLE_BOTH",
    "ROLE_TITLES",
    "ROLE_TRIGGER",
    "DetectedObject",
    "DetectionClass",
    "DetectionRun",
    "FormUpdate",
    "LabelOption",
    "LabelRef",
    "ObjectBox",
    "VideoObjects",
    "add_manual_object",
    "applies_to_role",
    "class_for_label",
    "convert_label",
    "default_label",
    "delete_box",
    "delete_objects",
    "form_updates",
    "label_options",
    "label_text",
    "merge_detection_result",
    "normalize_objects_by_video",
    "set_objects_label",
    "set_objects_reviewed",
    "set_objects_role",
    "worker_class_config",
]
