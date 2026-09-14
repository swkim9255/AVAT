"""Persistent case model for one accident investigation."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from investigator.form_definition import FormDefinition
from investigator.schema import (
    INJURY_GRADE_RULES,
    INVALID_FORMAT_MESSAGE,
    LEGACY_AVAILABLE_DATA_VALUES,
    LEGACY_TRIGGER_VEHICLE_SPEED_VALUES,
    MISSING_VALUE_MESSAGE,
    incident_day_for_date,
    injury_grade_summary,
    missing_questions,
    normalize_driver_age,
    validation_errors,
)

SCHEMA_VERSION = 1
DEFAULT_INSTITUTION = "samsung"


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def normalize_json_value(value: Any) -> Any:
    """Copy JSON-compatible values and reject unsupported objects."""

    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, list):
        return [normalize_json_value(item) for item in value]
    if isinstance(value, dict):
        return {str(key): normalize_json_value(item) for key, item in value.items()}
    raise TypeError(f"JSON으로 저장할 수 없는 값입니다: {type(value).__name__}")


@dataclass(slots=True)
class InvestigationCase:
    """A case, its attached videos, investigation values, and timeline markers."""

    uid: str = field(default_factory=lambda: uuid4().hex)
    values: dict[str, Any] = field(default_factory=dict)
    videos: list[str] = field(default_factory=list)
    active_video_index: int = 0
    markers_by_video: dict[str, dict[str, Any]] = field(default_factory=dict)
    rotation_by_video: dict[str, int] = field(default_factory=dict)
    status: str = "draft"
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)
    completed_at: str | None = None
    form_definition: FormDefinition | None = field(default=None, repr=False, compare=False)
    defaults_applied: bool = field(default=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        self.values = normalize_json_value(self.values)
        legacy_speed = self.values.get("trigger_vehicle_speed")
        if isinstance(legacy_speed, str) and legacy_speed in LEGACY_TRIGGER_VEHICLE_SPEED_VALUES:
            self.values["trigger_vehicle_speed"] = LEGACY_TRIGGER_VEHICLE_SPEED_VALUES[legacy_speed]
        available_data = self.values.get("available_data")
        if isinstance(available_data, list):
            self.values["available_data"] = list(
                dict.fromkeys(
                    LEGACY_AVAILABLE_DATA_VALUES.get(item, item) for item in available_data
                )
            )
        legacy_grades = self.values.pop("incident_injury_grades", None)
        self.values.pop("incident_representative_injury_grade", None)
        if legacy_grades is not None:
            legacy_summary = injury_grade_summary(legacy_grades, minimum=1, maximum=14)
            if legacy_summary and legacy_summary[0]:
                legacy_numbers = [int(item) for item in legacy_summary[0].split(", ")]
                for grade_key, numbers in (
                    (
                        "incident_minor_injury_grades",
                        [item for item in legacy_numbers if item >= 12],
                    ),
                    (
                        "incident_severe_injury_grades",
                        [item for item in legacy_numbers if item <= 11],
                    ),
                ):
                    _, _, count_key, representative_key = INJURY_GRADE_RULES[grade_key]
                    if grade_key not in self.values:
                        self.values[grade_key] = ", ".join(map(str, numbers))
                        self.values[count_key] = str(len(numbers))
                        if numbers:
                            self.values[representative_key] = str(numbers[0])
                        else:
                            self.values.pop(representative_key, None)
            elif legacy_summary is None:
                self.values.setdefault("incident_severe_injury_grades", legacy_grades)
        for grade_key, (
            minimum,
            maximum,
            count_key,
            representative_key,
        ) in INJURY_GRADE_RULES.items():
            injury_summary = injury_grade_summary(
                self.values.get(grade_key), minimum=minimum, maximum=maximum
            )
            if injury_summary and injury_summary[0]:
                self.values.update(
                    {
                        grade_key: injury_summary[0],
                        count_key: injury_summary[1],
                        representative_key: injury_summary[2],
                    }
                )
            elif not self.values.get(grade_key):
                self.values.pop(grade_key, None)
                self.values.pop(representative_key, None)
        normalized_age = normalize_driver_age(self.values.get("driver_age"))
        if normalized_age is not None:
            self.values["driver_age"] = normalized_age
        normalized_day = incident_day_for_date(self.values.get("incident_date"))
        if normalized_day is None:
            self.values.pop("incident_day", None)
        else:
            self.values["incident_day"] = normalized_day
        self.videos = [str(Path(path)) for path in self.videos]
        self.active_video_index = max(0, int(self.active_video_index))
        if self.videos:
            self.active_video_index = min(self.active_video_index, len(self.videos) - 1)
        else:
            self.active_video_index = 0
        normalized_markers: dict[str, dict[str, Any]] = {}
        for marker_key, marker_value in self.markers_by_video.items():
            if not isinstance(marker_value, dict):
                continue
            raw_trigger = marker_value.get("trigger_frame")
            trigger = None if raw_trigger is None else max(0, int(raw_trigger))
            raw_collision = marker_value.get("collision_frame")
            if raw_collision is None:
                legacy_collisions = marker_value.get("collision_frames", [])
                if not isinstance(legacy_collisions, list):
                    raise TypeError("collision_frames 항목은 숫자 배열이어야 합니다.")
                collisions = sorted({max(0, int(frame)) for frame in legacy_collisions})
                collision = collisions[0] if collisions else None
            else:
                collision = max(0, int(raw_collision))
            normalized_markers[str(marker_key)] = {
                "trigger_frame": trigger,
                # Keep the persisted plural key for schema-v1/CSV compatibility,
                # while normalizing every video to zero or one collision frame.
                "collision_frames": [] if collision is None else [collision],
            }
        self.markers_by_video = normalized_markers
        normalized_rotations: dict[str, int] = {}
        known_videos = {str(Path(path)).casefold() for path in self.videos}
        for video_path, raw_degrees in self.rotation_by_video.items():
            try:
                degrees = int(raw_degrees) % 360
            except (TypeError, ValueError):
                continue
            normalized_path = str(Path(str(video_path)))
            if degrees in {90, 180, 270} and normalized_path.casefold() in known_videos:
                normalized_rotations[normalized_path] = degrees
        self.rotation_by_video = normalized_rotations
        if self.status not in {"draft", "completed"}:
            self.status = "draft"

    @property
    def display_id(self) -> str:
        institution = str(self.values.get("institution", "")).strip().upper()
        number = str(self.values.get("accident_classification", "")).strip()
        if institution and number:
            return f"{institution}-{number}"
        if institution:
            return f"{institution}-미입력"
        return "미지정 사고"

    @property
    def active_video(self) -> str | None:
        if not self.videos:
            return None
        return self.videos[self.active_video_index]

    @property
    def marker_key(self) -> str:
        return self.active_video or "__case__"

    @property
    def video_rotation(self) -> int:
        return self.rotation_by_video.get(self.active_video or "", 0)

    def _current_markers(self) -> dict[str, Any]:
        return self.markers_by_video.get(
            self.marker_key,
            {"trigger_frame": None, "collision_frames": []},
        )

    @property
    def trigger_frame(self) -> int | None:
        value = self._current_markers().get("trigger_frame")
        return None if value is None else int(value)

    @property
    def collision_frame(self) -> int | None:
        collisions = self._current_markers().get("collision_frames", [])
        return int(collisions[0]) if collisions else None

    @property
    def collision_frames(self) -> list[int]:
        collision = self.collision_frame
        return [] if collision is None else [collision]

    @property
    def missing(self) -> list[str]:
        return (
            self.form_definition.missing(self.values)
            if self.form_definition
            else missing_questions(self.values)
        )

    @property
    def errors(self) -> dict[str, str]:
        errors = (
            self.form_definition.errors(dict(self.values))
            if self.form_definition
            else validation_errors(dict(self.values))
        )
        for marker in self.markers_by_video.values():
            trigger = marker.get("trigger_frame")
            collisions = marker.get("collision_frames", [])
            collision = collisions[0] if collisions else None
            if trigger is not None and collision is not None and int(trigger) > int(collision):
                errors["trigger_frame"] = INVALID_FORMAT_MESSAGE
                break
        return errors

    @property
    def can_complete(self) -> bool:
        return not self.missing and not self.errors

    def set_value(self, key: str, value: Any) -> None:
        normalized = normalize_json_value(value)
        if normalized in (None, "", []):
            self.values.pop(key, None)
        else:
            self.values[key] = normalized
        if key in INJURY_GRADE_RULES:
            minimum, maximum, count_key, representative_key = INJURY_GRADE_RULES[key]
            injury_summary = injury_grade_summary(normalized, minimum=minimum, maximum=maximum)
            if injury_summary and injury_summary[0]:
                self.values.update(
                    {
                        key: injury_summary[0],
                        count_key: injury_summary[1],
                        representative_key: injury_summary[2],
                    }
                )
            elif normalized in (None, ""):
                for derived_key in (count_key, representative_key):
                    self.values.pop(derived_key, None)
        self.mark_edited()

    def mark_edited(self) -> None:
        self.updated_at = utc_now()
        if self.status == "completed":
            self.status = "draft"
            self.completed_at = None

    def attach_videos(self, paths: list[str | Path]) -> int:
        was_empty = not self.videos
        known = {str(Path(path).expanduser().resolve()).casefold() for path in self.videos}
        added: list[str] = []
        for path in paths:
            resolved = str(Path(path).expanduser().resolve())
            if resolved.casefold() not in known:
                added.append(resolved)
                known.add(resolved.casefold())

        # A repeated folder import must be a true no-op. In particular, do not
        # reopen a completed investigation merely because all paths were
        # already attached.
        if not added:
            return 0

        case_markers = self.markers_by_video.pop("__case__", None) if was_empty else None
        self.videos.extend(added)
        if case_markers is not None:
            self.markers_by_video[self.videos[0]] = case_markers
        self.mark_edited()
        return len(added)

    def remove_video(self, index: int) -> str:
        """Remove one video link and its markers without touching the source file."""

        if not 0 <= index < len(self.videos):
            raise IndexError("영상 인덱스가 범위를 벗어났습니다.")

        previous_active = self.active_video_index
        removed = self.videos.pop(index)
        removed_key = str(Path(removed).expanduser().resolve()).casefold()
        for marker_key in list(self.markers_by_video):
            if marker_key == "__case__":
                continue
            if str(Path(marker_key).expanduser().resolve()).casefold() == removed_key:
                self.markers_by_video.pop(marker_key, None)
        for rotation_key in list(self.rotation_by_video):
            if str(Path(rotation_key).expanduser().resolve()).casefold() == removed_key:
                self.rotation_by_video.pop(rotation_key, None)

        if not self.videos:
            self.active_video_index = 0
            self.markers_by_video.clear()
            self.rotation_by_video.clear()
        elif index < previous_active:
            self.active_video_index = previous_active - 1
        elif index == previous_active:
            # Prefer the next item, or the previous item when the removed video
            # was the last one in the list.
            self.active_video_index = min(index, len(self.videos) - 1)
        else:
            self.active_video_index = min(previous_active, len(self.videos) - 1)
        self.mark_edited()
        return removed

    def set_video_rotation(self, degrees: int) -> None:
        """Persist a clockwise presentation angle for the active video."""

        path = self.active_video
        if path is None:
            return
        normalized = int(degrees) % 360
        if normalized not in {0, 90, 180, 270}:
            raise ValueError("영상 회전 각도는 90도 단위여야 합니다.")
        if normalized:
            self.rotation_by_video[path] = normalized
        else:
            self.rotation_by_video.pop(path, None)
        # Rotation is a viewing preference and must not reopen a completed case.
        self.updated_at = utc_now()

    def set_trigger(self, frame: int | None) -> None:
        marker = self._current_markers()
        marker["trigger_frame"] = None if frame is None else max(0, int(frame))
        marker["collision_frames"] = list(marker.get("collision_frames", []))[:1]
        self.markers_by_video[self.marker_key] = marker
        self.mark_edited()

    def set_collision(self, frame: int | None) -> None:
        marker = self._current_markers()
        marker["trigger_frame"] = marker.get("trigger_frame")
        marker["collision_frames"] = [] if frame is None else [max(0, int(frame))]
        self.markers_by_video[self.marker_key] = marker
        self.mark_edited()

    def complete(self) -> None:
        if not self.can_complete:
            raise ValueError(MISSING_VALUE_MESSAGE)
        self.status = "completed"
        self.completed_at = utc_now()
        self.updated_at = self.completed_at

    def to_dict(self) -> dict[str, Any]:
        return {
            "uid": self.uid,
            "values": normalize_json_value(self.values),
            "videos": list(self.videos),
            "active_video_index": self.active_video_index,
            "markers_by_video": normalize_json_value(self.markers_by_video),
            "rotation_by_video": normalize_json_value(self.rotation_by_video),
            "workflow": {
                "status": self.status,
                "created_at": self.created_at,
                "updated_at": self.updated_at,
                "completed_at": self.completed_at,
            },
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> InvestigationCase:
        raw_workflow = data.get("workflow", {})
        raw_values = data.get("values", {})
        raw_videos = data.get("videos", [])
        raw_markers = data.get("markers_by_video", {})
        raw_rotations = data.get("rotation_by_video", {})
        if not isinstance(raw_workflow, dict):
            raise TypeError("workflow 항목은 JSON 객체여야 합니다.")
        if not isinstance(raw_values, dict):
            raise TypeError("values 항목은 JSON 객체여야 합니다.")
        if not isinstance(raw_videos, list) or any(
            not isinstance(path, str) for path in raw_videos
        ):
            raise TypeError("videos 항목은 문자열 배열이어야 합니다.")
        if not isinstance(raw_markers, dict):
            raise TypeError("markers_by_video 항목은 JSON 객체여야 합니다.")
        if not isinstance(raw_rotations, dict):
            raise TypeError("rotation_by_video 항목은 JSON 객체여야 합니다.")

        workflow = raw_workflow
        videos = list(raw_videos)
        active_video_index = int(data.get("active_video_index") or 0)
        markers_by_video = dict(raw_markers)
        legacy_markers = data.get("markers") if isinstance(data.get("markers"), dict) else None
        if legacy_markers is not None and not markers_by_video:
            marker_key = (
                str(videos[min(max(0, active_video_index), len(videos) - 1)])
                if videos
                else "__case__"
            )
            markers_by_video[marker_key] = legacy_markers
        return cls(
            uid=str(data.get("uid") or uuid4().hex),
            values=dict(raw_values),
            videos=videos,
            active_video_index=active_video_index,
            markers_by_video=markers_by_video,
            rotation_by_video=dict(raw_rotations),
            status=str(workflow.get("status") or "draft"),
            created_at=str(workflow.get("created_at") or utc_now()),
            updated_at=str(workflow.get("updated_at") or utc_now()),
            completed_at=workflow.get("completed_at"),
            defaults_applied=True,
        )


def next_case_number(cases: list[InvestigationCase], institution: str = DEFAULT_INSTITUTION) -> str:
    """Return the next zero-padded numeric identifier for an institution."""

    used: list[int] = []
    for case in cases:
        if str(case.values.get("institution", "")).casefold() != institution.casefold():
            continue
        raw = str(case.values.get("accident_classification", ""))
        if raw.isdigit():
            used.append(int(raw))
    return f"{max(used, default=0) + 1:03d}"


def new_case(cases: list[InvestigationCase] | None = None) -> InvestigationCase:
    existing = cases or []
    return InvestigationCase(
        values={
            "institution": DEFAULT_INSTITUTION,
            "accident_classification": next_case_number(existing, DEFAULT_INSTITUTION),
        }
    )


__all__ = [
    "DEFAULT_INSTITUTION",
    "InvestigationCase",
    "SCHEMA_VERSION",
    "new_case",
    "next_case_number",
]
