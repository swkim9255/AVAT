"""Companion CSV for per-video timeline markers and existing speed estimates.

Frames remain zero-based internally; display_frame is the one-based UI number.
Segment averages and the estimated speed at T are deliberately separate fields.
No new instantaneous collision-speed model is introduced by this export.
"""

from __future__ import annotations

import csv
import json
import math
from collections.abc import Iterable, Iterator, Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any, TextIO

from investigator.case_numbering import build_case_number_map, original_case_number
from investigator.speed_estimation import estimate_speed, points_from_json
from investigator.statistics_media import probe_video_fps

if TYPE_CHECKING:
    from investigator.models import InvestigationCase

SPEED_CSV_HEADERS = (
    "case_id", "accident_classification", "report_accident_number",
    "video_file", "video_name", "event_type", "point_index",
    "frame_index", "display_frame", "time_seconds", "timecode", "fps",
    "previous_point_frame_index", "previous_point_time_seconds",
    "distance_from_previous_m", "segment_speed_kph", "trigger_speed_estimate_kph",
    "estimate_method", "estimate_warnings", "missing_distance_points",
)

# Export-only categories: do not change the explanatory UI/engine warnings.
_WARNING_ORDER = (
    "FPS 미확인", "거리 미입력", "계산 지점 부족", "짧은 구간",
    "범위 밖 추정", "속도 변화 큼", "속도 계산 불가", "기타 계산 경고",
)


def _warning_category(message: str) -> str:
    """Map known engine warnings to short stable categories, never leak prose.

    Missing-point numbers come from the normalized data, not a parse of these
    localized messages. An unfamiliar engine warning remains visible generically.
    """
    if "FPS" in message:
        return "FPS 미확인"
    if "거리가 비어" in message:
        return "거리 미입력"
    if "2개 지점" in message:
        return "계산 지점 부족"
    if "1프레임" in message:
        return "짧은 구간"
    if "범위" in message and ("밖" in message or "떨어" in message):
        return "범위 밖 추정"
    if "속도 변화" in message:
        return "속도 변화 큼"
    if "계산할 수 없" in message or "추정할 수 없" in message:
        return "속도 계산 불가"
    return "기타 계산 경고"


def _warning_text(categories: Iterable[str]) -> str | None:
    present = set(categories)
    return " | ".join(category for category in _WARNING_ORDER if category in present) or None


def speed_csv_path(csv_path: str | Path) -> Path:
    """Keep the automatic companion next to the selected investigation CSV."""
    destination = Path(csv_path)
    return destination.with_name(f"{destination.stem}_속도분석.csv")


def _frame(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        result = int(value)
        if result < 0 or float(value) != result:
            return None
        return result
    except (TypeError, ValueError, OverflowError):
        return None


def _video_keys(case: InvestigationCase) -> list[str]:
    # Do not export stale markers belonging to a video that was removed.
    return list(dict.fromkeys(case.videos)) if case.videos else ["__case__"]


def _has_markers(markers: Any) -> bool:
    if not isinstance(markers, dict):
        return False
    if _frame(markers.get("trigger_frame")) is not None:
        return True
    if _frame(markers.get("collision_frame")) is not None:
        return True
    collisions = markers.get("collision_frames", [])
    return (
        isinstance(collisions, list) and any(_frame(item) is not None for item in collisions)
        or bool(points_from_json(markers.get("speed_points", [])))
    )


def _fps(video: str | None, fps_by_video: Mapping[str, float]) -> float | None:
    if video is None:
        return None
    value = fps_by_video.get(video)
    if value is None:
        try:
            value = fps_by_video.get(str(Path(video).resolve()))
        except (OSError, RuntimeError, ValueError):
            return None
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if math.isfinite(result) and result > 0 else None


def _number(value: float | None) -> float | None:
    return round(value, 6) if value is not None and math.isfinite(value) else None


def _timecode(seconds: float | None) -> str | None:
    if seconds is None:
        return None
    milliseconds = round(seconds * 1000)
    hours, milliseconds = divmod(milliseconds, 3_600_000)
    minutes, milliseconds = divmod(milliseconds, 60_000)
    whole_seconds, milliseconds = divmod(milliseconds, 1000)
    return f"{hours:02d}:{minutes:02d}:{whole_seconds:02d}.{milliseconds:03d}"


def iter_speed_rows(
    cases: Iterable[InvestigationCase],
    *,
    fps_by_video: Mapping[str, float],
    report_numbers: Mapping[str, str] | None = None,
) -> Iterator[dict[str, Any]]:
    """Yield actual marked events without mutating cases or the active video.

    FPS must come from video metadata, not the player's fallback playback rate.
    Absent markers produce no rows; absent metadata retains frames/distances.
    """
    cases = list(cases)
    if report_numbers is None:
        report_numbers = build_case_number_map(cases)
    for case in cases:
        for key in _video_keys(case):
            markers = case.markers_by_video.get(key, {})
            if not isinstance(markers, dict):
                continue
            video = None if key == "__case__" else key
            fps = _fps(video, fps_by_video)
            trigger = _frame(markers.get("trigger_frame"))
            collision = _frame(markers.get("collision_frame"))
            if collision is None:
                legacy = markers.get("collision_frames", [])
                if isinstance(legacy, list):
                    collision = min(
                        (frame for item in legacy if (frame := _frame(item)) is not None),
                        default=None,
                    )
            points = points_from_json(markers.get("speed_points", []))
            missing_distances = [
                index for index, point in enumerate(points, 1)
                if index > 1 and point.distance_m is None
            ]
            segments = {
                segment.end_frame: segment
                for segment in estimate_speed(points, fps or 0, None).segments
            }
            trigger_estimate = estimate_speed(points, fps or 0, trigger)
            # A point may also be T or C: keep each role, ordered by video time.
            events = []
            if trigger is not None:
                events.append((trigger, 0, "trigger", None))
            if collision is not None:
                events.append((collision, 1, "collision", None))
            events.extend((point.frame, 2, "speed_point", index)
                          for index, point in enumerate(points))
            for frame, _, event_type, index in sorted(events):
                seconds = frame / fps if fps else None
                row = dict.fromkeys(SPEED_CSV_HEADERS)
                row.update({
                    "case_id": case.display_id,
                    "accident_classification": original_case_number(case) or None,
                    "report_accident_number": report_numbers.get(case.uid),
                    "video_file": video,
                    "video_name": Path(video).name if video else None,
                    "event_type": event_type,
                    "frame_index": frame,
                    "display_frame": frame + 1,
                    "time_seconds": _number(seconds),
                    "timecode": _timecode(seconds),
                    "fps": fps,
                })
                warnings = []
                if fps is None:
                    warnings.append("FPS 미확인")
                if event_type == "trigger":
                    row["trigger_speed_estimate_kph"] = _number(trigger_estimate.kph)
                    row["estimate_method"] = trigger_estimate.method or None
                    if missing_distances:
                        row["missing_distance_points"] = list(missing_distances)
                        warnings.append("거리 미입력")
                    if fps:
                        warnings.extend(_warning_category(message) for message in trigger_estimate.warnings)
                elif event_type == "speed_point":
                    row["point_index"] = index + 1
                    point = points[index]
                    if index:
                        previous = points[index - 1]
                        row["previous_point_frame_index"] = previous.frame
                        row["previous_point_time_seconds"] = _number(
                            previous.frame / fps if fps else None
                        )
                        row["distance_from_previous_m"] = point.distance_m
                        segment = segments.get(frame)
                        if segment:
                            row["segment_speed_kph"] = _number(segment.kph)
                            row["estimate_method"] = "이전 지점부터의 구간 평균 속도"
                            if segment.frames <= 1:
                                warnings.append("짧은 구간")
                        if point.distance_m is None:
                            row["missing_distance_points"] = [index + 1]
                            warnings.append("거리 미입력")
                row["estimate_warnings"] = _warning_text(warnings)
                yield row


def _csv_text(value: Any) -> str:
    if value is None or isinstance(value, float) and not math.isfinite(value):
        return "null"
    if isinstance(value, list):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":")) if value else "null"
    result = str(value)
    if not result.strip():
        return "null"
    if result.lstrip().startswith(("=", "+", "-", "@")):
        return "'" + result
    return result


def write_speed_csv(
    handle: TextIO,
    cases: Iterable[InvestigationCase],
    *,
    report_numbers: Mapping[str, str] | None = None,
) -> None:
    """Write to a caller-owned staged file; only metadata is read from videos."""
    cases = list(cases)
    paths = list(dict.fromkeys(
        video for case in cases for video in case.videos
        if _has_markers(case.markers_by_video.get(video))
    ))
    metadata = probe_video_fps(paths, {})
    fps_by_video = {path: entry[2] for path, entry in metadata.items()}
    writer = csv.DictWriter(handle, fieldnames=SPEED_CSV_HEADERS)
    writer.writeheader()
    for row in iter_speed_rows(cases, fps_by_video=fps_by_video, report_numbers=report_numbers):
        writer.writerow({key: _csv_text(row.get(key)) for key in SPEED_CSV_HEADERS})
