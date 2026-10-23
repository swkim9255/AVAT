"""검출 대상 클래스 정의.

각 클래스는 하나 이상의 영어 텍스트 프롬프트를 가진다. 같은 클래스의 여러
프롬프트 결과는 하나로 합쳐지고 중복 마스크는 제거된다.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class ClassDef:
    key: str
    label: str
    prompts: tuple[str, ...]
    object_prefix: str = "R"
    threshold: float = 0.4
    enabled: bool = True
    # 같은 그룹 안의 클래스끼리는 같은 영역을 두고 경쟁한다(예: 건물벽 vs 담벼락).
    suppression_group: str | None = None
    min_area_ratio: float = 0.0005
    # Coarse classes may retain the older, stricter cross-prompt NMS policy.
    nms_iou: float | None = None


@dataclass(frozen=True)
class ClassConfig:
    classes: tuple[ClassDef, ...]
    cross_class_iou: float = 0.6
    same_class_iou: float = 0.7
    max_detections_per_prompt: int = 30
    notes: dict = field(default_factory=dict)

    def enabled(self, keys: list[str] | None = None) -> tuple[ClassDef, ...]:
        if keys is not None:
            wanted = set(keys)
            unknown = wanted - {c.key for c in self.classes}
            if unknown:
                raise ValueError(f"알 수 없는 클래스: {', '.join(sorted(unknown))}")
            return tuple(c for c in self.classes if c.key in wanted)
        return tuple(c for c in self.classes if c.enabled)


def _default_config() -> ClassConfig:
    # Detection taxonomy is deliberately independent of editable survey forms.
    from investigator.detection_taxonomy import (
        taxonomy_hash,
        taxonomy_records,
        taxonomy_version,
    )

    return _config_from_dict({
        "classes": list(taxonomy_records()),
        "taxonomy_version": taxonomy_version(),
        "taxonomy_hash": taxonomy_hash(),
    })


def load_config(path: str | Path | None) -> ClassConfig:
    """JSON 설정 파일을 읽는다. 없으면 앱의 기본 클래스 표를 쓴다."""
    if path is None:
        return _default_config()
    return _config_from_dict(json.loads(Path(path).read_text(encoding="utf-8")))


def _config_from_dict(data: dict) -> ClassConfig:
    if not isinstance(data, dict) or not isinstance(data.get("classes"), list):
        raise ValueError("검출 설정에 classes 목록이 필요합니다.")
    classes = []
    for item in data["classes"]:
        if not isinstance(item, dict) or not isinstance(item.get("key"), str) or not item["key"]:
            raise ValueError("검출 클래스 key는 비어 있지 않은 문자열이어야 합니다.")
        prompts = item.get("prompts")
        if not isinstance(prompts, list) or not prompts or not all(
            isinstance(prompt, str) and prompt.strip() for prompt in prompts
        ):
            raise ValueError(f"클래스 {item.get('key')!r}에 prompts가 없습니다.")
        classes.append(
            ClassDef(
                key=item["key"],
                label=item.get("label", item["key"]),
                prompts=tuple(prompts),
                object_prefix=item.get("object_prefix", "R"),
                threshold=float(item.get("threshold", 0.4)),
                enabled=bool(item.get("enabled", True)),
                suppression_group=item.get("suppression_group"),
                min_area_ratio=float(item.get("min_area_ratio", 0.0005)),
                nms_iou=float(item["nms_iou"]) if item.get("nms_iou") is not None else None,
            )
        )
    keys = [c.key for c in classes]
    if len(keys) != len(set(keys)):
        raise ValueError("클래스 key가 중복되었습니다.")
    config = ClassConfig(
        classes=tuple(classes),
        cross_class_iou=float(data.get("cross_class_iou", 0.6)),
        same_class_iou=float(data.get("same_class_iou", 0.7)),
        max_detections_per_prompt=int(data.get("max_detections_per_prompt", 30)),
        notes={key: data[key] for key in ("taxonomy_version", "taxonomy_hash") if key in data},
    )
    ratios = [config.cross_class_iou, config.same_class_iou]
    ratios.extend(value for cls in classes for value in (cls.threshold, cls.min_area_ratio))
    ratios.extend(cls.nms_iou for cls in classes if cls.nms_iou is not None)
    if not all(math.isfinite(value) and 0 <= value <= 1 for value in ratios):
        raise ValueError("검출 임계값과 면적·IoU 비율은 0~1이어야 합니다.")
    if config.max_detections_per_prompt <= 0:
        raise ValueError("프롬프트별 최대 검출 수는 1 이상이어야 합니다.")
    return config
