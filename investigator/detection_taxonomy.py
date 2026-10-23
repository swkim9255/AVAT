"""Fixed detector taxonomy, deliberately independent of editable survey forms."""

from __future__ import annotations

import hashlib
import json
import math
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any


def validate_taxonomy(data: Any) -> None:
    """Reject ambiguous keys/defaults before model or UI code consumes them."""
    if not isinstance(data, dict) or type(data.get("version")) is not int or data["version"] != 1:
        raise ValueError("검출 분류 설정의 version은 1이어야 합니다.")
    records = data.get("classes")
    if not isinstance(records, list) or not records:
        raise ValueError("검출 분류 설정에 classes 목록이 필요합니다.")
    class_keys, subtype_keys, prompts = set(), set(), set()
    for record in records:
        if not isinstance(record, dict) or any(
            not isinstance(record.get(key), str) or not record[key].strip()
            for key in ("key", "label", "group", "object_prefix")
        ):
            raise ValueError("검출 분류의 키·이름·그룹·ID 접두사를 확인해 주세요.")
        key = record["key"]
        if key in class_keys:
            raise ValueError(f"검출 대분류 키가 중복됩니다: {key}")
        class_keys.add(key)
        names = record.get("prompts")
        if (not isinstance(names, list) or not names
                or any(not isinstance(p, str) or not p.strip() for p in names)):
            raise ValueError(f"{key}: 프롬프트 목록을 확인해 주세요.")
        if len(set(names)) != len(names) or prompts.intersection(names):
            raise ValueError(f"{key}: 프롬프트가 중복되거나 다른 대분류와 겹칩니다.")
        prompts.update(names)
        subtypes = record.get("subtypes")
        if not isinstance(subtypes, list) or not subtypes:
            raise ValueError(f"{key}: 세분류 목록이 필요합니다.")
        allowed = set()
        for subtype in subtypes:
            if not isinstance(subtype, dict) or any(
                not isinstance(subtype.get(name), str) or not subtype[name].strip()
                for name in ("key", "label")
            ):
                raise ValueError(f"{key}: 세분류 키와 이름을 확인해 주세요.")
            if subtype["key"] in subtype_keys:
                raise ValueError(f"세분류 키가 중복됩니다: {subtype['key']}")
            allowed.add(subtype["key"])
            subtype_keys.add(subtype["key"])
            for role in ("trigger", "collision"):
                mapping = subtype.get(role)
                if mapping is not None and (
                    not isinstance(mapping, list) or len(mapping) != 2
                    or any(not isinstance(part, str) or not part.strip() for part in mapping)
                ):
                    raise ValueError(f"{key}: 조사 탭 연결은 항목/선택지 ID 두 개여야 합니다.")
        default = record.get("default_subtype")
        if default is not None and (not isinstance(default, str) or default not in allowed):
            raise ValueError(f"{key}: 기본 세분류가 선택지에 없습니다.")
        defaults = record.get("prompt_defaults", {})
        if (not isinstance(defaults, dict) or any(
            prompt not in names or not isinstance(subtype, str) or subtype not in allowed
            for prompt, subtype in defaults.items()
        )):
            raise ValueError(f"{key}: 프롬프트별 기본 세분류를 확인해 주세요.")
        group = record.get("suppression_group")
        if group is not None and (not isinstance(group, str) or not group.strip()):
            raise ValueError(f"{key}: 중복 제거 그룹을 확인해 주세요.")
        for name in ("threshold", "min_area_ratio", "nms_iou"):
            value = record.get(name, .4 if name == "threshold" else .0005 if name == "min_area_ratio" else .7)
            if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value) or not 0 <= value <= 1:
                raise ValueError(f"{key}: {name}은 0~1의 유한한 숫자여야 합니다.")
    special = data.get("special_vehicle_types")
    if not isinstance(special, dict) or any(
        not isinstance(key, str) or not key.strip() or not isinstance(value, str) or not value.strip()
        for key, value in special.items()
    ):
        raise ValueError("특수차량 상세 종류 설정을 확인해 주세요.")
    aliases = data.get("legacy_aliases", {})
    if not isinstance(aliases, dict) or any(
        not isinstance(key, str) or not key.strip() or not isinstance(value, str)
        or value not in class_keys | subtype_keys for key, value in aliases.items()
    ):
        raise ValueError("이전 검출 분류 변환 설정을 확인해 주세요.")


_PATH = Path(__file__).parent / "assets" / "detection_taxonomy.json"
_RAW = json.loads(_PATH.read_text(encoding="utf-8"))
validate_taxonomy(_RAW)
TAXONOMY_VERSION = _RAW["version"]


@dataclass(frozen=True, slots=True)
class DetectionSubtype:
    key: str
    label: str
    class_key: str
    group: str
    trigger: tuple[str, str] | None
    collision: tuple[str, str] | None


_RECORDS: tuple[dict[str, Any], ...] = tuple(
    {
        "threshold": 0.4,
        "suppression_group": None,
        "min_area_ratio": 0.0005,
        "nms_iou": 0.7,
        **record,
    }
    for record in _RAW["classes"]
)
_BY_KEY = {record["key"]: record for record in _RECORDS}
SUBTYPES = tuple(
    DetectionSubtype(
        sub["key"],
        sub["label"],
        record["key"],
        record["group"],
        tuple(sub["trigger"]) if sub.get("trigger") else None,
        tuple(sub["collision"]) if sub.get("collision") else None,
    )
    for record in _RECORDS
    for sub in record["subtypes"]
)
SUBTYPE_BY_KEY = {item.key: item for item in SUBTYPES}
SPECIAL_VEHICLE_TYPES: dict[str, str] = dict(_RAW["special_vehicle_types"])


def taxonomy_records() -> tuple[dict[str, Any], ...]:
    """Return detached configuration records; callers cannot mutate taxonomy."""
    return deepcopy(_RECORDS)


def taxonomy_version() -> int:
    return TAXONOMY_VERSION


def taxonomy_hash() -> str:
    canonical = json.dumps(_RAW, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def subtype_options(class_key: str | None) -> tuple[DetectionSubtype, ...]:
    """Fixed subtypes for a model class, not choices in the investigation form."""
    return tuple(item for item in SUBTYPES if item.class_key == class_key)


def legacy_subtype(key: str | None) -> str | None:
    if key in SUBTYPE_BY_KEY:
        return key
    alias = _RAW["legacy_aliases"].get(key)
    return alias if alias in SUBTYPE_BY_KEY else None


def coarse_class_key(key: str | None) -> str | None:
    if key in _BY_KEY:
        return key
    subtype = legacy_subtype(key)
    if subtype is not None:
        return SUBTYPE_BY_KEY[subtype].class_key
    alias = _RAW["legacy_aliases"].get(key)
    return alias if alias in _BY_KEY else None


def suggest_subtype(class_key: str, prompt: str | None) -> str | None:
    record = _BY_KEY.get(class_key)
    if record is None:
        return None
    default = record.get("prompt_defaults", {}).get(prompt)
    if default is not None:
        return default
    if record.get("default_subtype"):
        return record["default_subtype"]
    options = record["subtypes"]
    return options[0]["key"] if len(options) == 1 else None


def special_type_from_prompt(prompt: str | None) -> str | None:
    return {
        "excavator": "excavator",
        "tow truck": "tow_truck",
        "tow_truck": "tow_truck",
        "fire truck": "fire_truck",
        "fire_truck": "fire_truck",
        "ambulance": "ambulance",
    }.get(prompt)
