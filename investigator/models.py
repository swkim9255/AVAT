"""Persistent case model for one accident investigation."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from investigator.schema import (
    INVALID_FORMAT_MESSAGE,
    MISSING_VALUE_MESSAGE,
    missing_questions,
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
    status: str = "draft"
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)
    completed_at: str | None = None

    def __post_init__(self) -> None:
        self.values = normalize_json_value(self.values)
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
        return missing_questions(self.values)

    @property
    def errors(self) -> dict[str, str]:
        errors = validation_errors(dict(self.values))
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
        self.mark_edited()

    def mark_edited(self) -> None:
        self.updated_at = utc_now()
        if self.status == "completed":
            self.status = "draft"
            self.completed_at = None

    def attach_videos(self, paths: list[str | Path]) -> None:
        case_markers = self.markers_by_video.pop("__case__", None) if not self.videos else None
        known = {str(Path(path).expanduser().resolve()).casefold() for path in self.videos}
        for path in paths:
            resolved = str(Path(path).expanduser().resolve())
            if resolved.casefold() not in known:
                self.videos.append(resolved)
                known.add(resolved.casefold())
        if case_markers is not None and self.videos:
            self.markers_by_video[self.videos[0]] = case_markers
        self.mark_edited()

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
            status=str(workflow.get("status") or "draft"),
            created_at=str(workflow.get("created_at") or utc_now()),
            updated_at=str(workflow.get("updated_at") or utc_now()),
            completed_at=workflow.get("completed_at"),
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
