"""사용자가 찍은 기준 지점으로 구간별 평균 속도와 T(트리거) 시점 속도를 계산한다.

조사자는 영상을 넘기며 차량이 도로의 기준물(차선 끝, 횡단보도 줄, 기둥 등)을
지날 때마다 *속도 지점*을 추가하고, 바로 앞 지점과의 실제 거리를 입력한다.
연속한 두 지점은 거리와 걸린 시간을 아는 구간이므로 평균 속도를 구할 수 있다.

T 시점 속도는 추정하지 않는다. T가 들어 있는 두 지점(A.frame < T ≤ B.frame,
T가 첫 지점과 같으면 첫 구간)의 거리가 입력된 경우에만 그 구간의 평균 속도를
T 시점 속도로 쓴다. 지점을 여러 개 찍는 것은 구간별 속도 추세를 보기 위한 것이며,
T 계산에는 항상 두 지점만 쓴다. 범위는 각 지점 ±0.5프레임 배치(구간 길이 ±1프레임)
민감도이며 통계적 신뢰구간이나 거리 오차를 뜻하지 않는다.

Qt 없이 단위 테스트할 수 있도록 순수 Python으로 둔다.
"""

from __future__ import annotations

import math
from collections.abc import Iterable
from dataclasses import dataclass, field

from investigator.schema import TRIGGER_VEHICLE_SPEED_CHOICES

MS_TO_KPH = 3.6
TRIGGER_METHOD = "구간 평균 속도"

STOPPED = TRIGGER_VEHICLE_SPEED_CHOICES[0].value
CREEP = TRIGGER_VEHICLE_SPEED_CHOICES[1].value
LOW = TRIGGER_VEHICLE_SPEED_CHOICES[2].value
MEDIUM = TRIGGER_VEHICLE_SPEED_CHOICES[3].value
HIGH = TRIGGER_VEHICLE_SPEED_CHOICES[4].value

#: (exclusive upper bound in km/h, stored choice value). A value exactly on a
#: boundary belongs to the faster class, e.g. 10 km/h is 저속.
SPEED_BANDS: tuple[tuple[float, str], ...] = (
    (0.0, STOPPED),
    (10.0, CREEP),
    (30.0, LOW),
    (60.0, MEDIUM),
    (math.inf, HIGH),
)
SPEED_BAND_LIMITS = (10.0, 30.0, 60.0)


@dataclass(frozen=True, slots=True)
class SpeedPoint:
    """A reference point; ``distance_m`` is measured from the previous point."""

    frame: int
    distance_m: float | None = None


@dataclass(frozen=True, slots=True)
class SpeedSegment:
    index: int
    start_frame: int
    end_frame: int
    distance_m: float
    kph: float
    kph_low: float
    kph_high: float

    @property
    def frames(self) -> int:
        return self.end_frame - self.start_frame


@dataclass(slots=True)
class SpeedEstimate:
    segments: list[SpeedSegment] = field(default_factory=list)
    trigger_frame: int | None = None
    kph: float | None = None
    kph_low: float | None = None
    kph_high: float | None = None
    method: str = ""
    points_used: tuple[int, ...] = ()
    #: 예전 근사 곡선용 필드. 이제는 항상 None이다(그래프 점선을 그리지 않음).
    velocity_line: tuple[float, float] | None = None
    #: T가 들어 있는 구간의 두 지점 프레임. 거리가 비어 있어도 채우고, 구간이 없으면 None.
    trigger_segment: tuple[int, int] | None = None
    warnings: list[str] = field(default_factory=list)

    @property
    def label(self) -> str | None:
        return None if self.kph is None else speed_label(self.kph)

    @property
    def label_uncertain(self) -> bool:
        if self.kph_low is None or self.kph_high is None:
            return False
        if not math.isfinite(self.kph_low) or not math.isfinite(self.kph_high):
            return True
        return speed_label(self.kph_low) != speed_label(self.kph_high)


def speed_label(kph: float) -> str:
    """Map a speed to the stored value of the 차량 속도 (추정) choice."""

    value = float(kph)
    if not math.isfinite(value) or value < 0:
        raise ValueError("속도는 0 이상의 유한한 수여야 합니다.")
    if value == 0:
        return STOPPED
    for upper, label in SPEED_BANDS:
        if value < upper:
            return label
    return HIGH


def normalize_points(points: Iterable[SpeedPoint]) -> list[SpeedPoint]:
    """Sort by frame and keep one point per frame (the last one wins)."""

    by_frame: dict[int, SpeedPoint] = {}
    for point in points:
        try:
            frame = int(point.frame)
        except (TypeError, ValueError, OverflowError):
            continue
        if frame < 0 or isinstance(point.frame, bool) or frame != point.frame:
            continue
        by_frame[frame] = SpeedPoint(frame, _usable_distance(point))
    result = [by_frame[frame] for frame in sorted(by_frame)]
    if result:
        result[0] = SpeedPoint(result[0].frame, None)
    return result


def _usable_distance(point: SpeedPoint) -> float | None:
    if point.distance_m is None:
        return None
    try:
        distance = float(point.distance_m)
    except (TypeError, ValueError):
        return None
    return distance if distance > 0 and math.isfinite(distance) else None


def _segment_kph(distance: float, seconds: float) -> float:
    return math.inf if seconds <= 0 else distance / seconds * MS_TO_KPH


def _trigger_segment(ordered: list[SpeedPoint], trigger: int) -> tuple[SpeedPoint, SpeedPoint] | None:
    """T가 들어 있는 인접 두 지점(A.frame < T ≤ B.frame)을 찾는다.

    T가 지점과 같은 프레임이면 그 지점에서 끝나는 앞 구간을 쓰고, T가 첫 지점과
    같으면 첫 구간을 쓴다. 지점이 2개 미만이거나 T가 지점 범위 밖이면 None이다.
    """

    if len(ordered) < 2:
        return None
    if trigger == ordered[0].frame:
        return ordered[0], ordered[1]
    for start, end in zip(ordered, ordered[1:], strict=False):
        if start.frame < trigger <= end.frame:
            return start, end
    return None


def estimate_speed(
    points: Iterable[SpeedPoint],
    fps: float,
    trigger_frame: int | None,
) -> SpeedEstimate:
    """Compute segment speeds and the average speed of the segment containing T."""

    ordered = normalize_points(points)
    estimate = SpeedEstimate(trigger_frame=trigger_frame)
    trigger_pair = None if trigger_frame is None else _trigger_segment(ordered, trigger_frame)
    if trigger_pair is not None:
        estimate.trigger_segment = (trigger_pair[0].frame, trigger_pair[1].frame)
    if fps <= 0 or not math.isfinite(fps):
        if ordered:
            estimate.warnings.append("영상 FPS를 알 수 없어 속도를 계산할 수 없어요.")
        return estimate
    frame_seconds = 1.0 / fps

    for index in range(1, len(ordered)):
        previous, point = ordered[index - 1], ordered[index]
        distance = _usable_distance(point)
        if distance is None:
            continue
        seconds = (point.frame - previous.frame) / fps
        estimate.segments.append(
            SpeedSegment(
                index=index,
                start_frame=previous.frame,
                end_frame=point.frame,
                distance_m=distance,
                kph=_segment_kph(distance, seconds),
                kph_low=_segment_kph(distance, seconds + frame_seconds),
                kph_high=_segment_kph(distance, seconds - frame_seconds),
            )
        )
    if any(segment.frames <= 1 for segment in estimate.segments):
        estimate.warnings.append(
            "1프레임 이하로 짧은 구간이 있어요. 더 멀리 떨어진 기준 지점을 쓰면 정확해져요."
        )
    missing = [index + 1 for index in range(1, len(ordered)) if _usable_distance(ordered[index]) is None]
    if missing:
        estimate.warnings.append(
            "거리가 비어 있는 지점이 있어요: " + ", ".join(f"{number}번" for number in missing)
        )

    if trigger_pair is None:
        return estimate
    # T 구간의 거리(A→B)가 있으면 그 구간 평균 속도와 ±1프레임 범위를 그대로 쓴다.
    segment = next(
        (item for item in estimate.segments if item.end_frame == trigger_pair[1].frame), None
    )
    if segment is None:
        return estimate
    estimate.kph = segment.kph
    estimate.kph_low = segment.kph_low
    estimate.kph_high = segment.kph_high
    estimate.method = TRIGGER_METHOD
    estimate.points_used = (segment.start_frame, segment.end_frame)
    return estimate


def points_from_json(raw: object) -> list[SpeedPoint]:
    """Parse the persisted ``speed_points`` list, skipping malformed entries."""

    if not isinstance(raw, list):
        return []
    points: list[SpeedPoint] = []
    for item in raw:
        if not isinstance(item, dict) or item.get("frame") is None:
            continue
        try:
            raw_frame = item["frame"]
            if isinstance(raw_frame, bool):
                continue
            frame = max(0, int(raw_frame))
            if isinstance(raw_frame, float) and raw_frame != frame:
                continue
        except (TypeError, ValueError, OverflowError):
            continue
        distance = item.get("distance_m")
        try:
            distance_value = None if distance in (None, "") or isinstance(distance, bool) else float(distance)
        except (TypeError, ValueError):
            distance_value = None
        if distance_value is not None and not (math.isfinite(distance_value) and distance_value > 0):
            distance_value = None
        points.append(SpeedPoint(frame, distance_value))
    return normalize_points(points)


def points_to_json(points: Iterable[SpeedPoint]) -> list[dict[str, object]]:
    return [
        {"frame": int(point.frame), "distance_m": point.distance_m}
        for point in normalize_points(points)
    ]


__all__ = [
    "SPEED_BANDS",
    "SPEED_BAND_LIMITS",
    "TRIGGER_METHOD",
    "SpeedEstimate",
    "SpeedPoint",
    "SpeedSegment",
    "estimate_speed",
    "points_from_json",
    "points_to_json",
    "speed_label",
]
