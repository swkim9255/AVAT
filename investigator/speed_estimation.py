"""Estimate the vehicle speed at the trigger moment from user-placed reference points.

The investigator steps through the video and adds a *speed point* every time the
camera vehicle passes a reference feature on the road (the end of a lane dash,
a crosswalk stripe, a pole …) and types the real distance between that feature
and the previous one. Each consecutive pair of points is a segment with a known
distance and a measured duration, i.e. an average speed.

The speed *at* the trigger frame is then estimated from the points nearest to
it: with three or more points a constant-acceleration (quadratic) position
model is fitted and differentiated at T; with two points the segment average is
used. A ±0.5-frame placement assumption supplies a sensitivity range, not a
statistical confidence interval or a guarantee of measurement accuracy. Real
distance error, variable-frame-rate timing and model error are not covered.

This module is pure Python so it can be unit-tested without Qt.
"""

from __future__ import annotations

import itertools
import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field

from investigator.schema import TRIGGER_VEHICLE_SPEED_CHOICES

MS_TO_KPH = 3.6
#: Points used around T for the local fit. More points smooth noise but blur
#: genuine acceleration changes far from the trigger.
FIT_WINDOW = 4
#: Conservative fallback threshold for a fit, not a physical acceleration limit.
MAX_PLAUSIBLE_ACCELERATION = 9.81
#: Enumerate corner trials of a ±0.5-frame placement-sensitivity model.
EXHAUSTIVE_BOUND_POINTS = 8

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
    #: Coefficients of v(t) = c0 + c1·t in km/h around T, for plotting the fit.
    velocity_line: tuple[float, float] | None = None
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


def _chains(points: Sequence[SpeedPoint]) -> list[list[tuple[int, float]]]:
    """Split points into runs whose consecutive distances are all known.

    Each chain is a list of ``(frame, cumulative_metres)`` starting at 0.
    """

    chains: list[list[tuple[int, float]]] = []
    current: list[tuple[int, float]] = []
    for index, point in enumerate(points):
        distance = _usable_distance(point) if index else None
        if current and distance is not None and point.frame > current[-1][0]:
            current.append((point.frame, current[-1][1] + distance))
            continue
        if len(current) >= 2:
            chains.append(current)
        current = [(point.frame, 0.0)]
    if len(current) >= 2:
        chains.append(current)
    return chains


def _fit_velocity(times: Sequence[float], positions: Sequence[float], at: float) -> tuple[float, float, float]:
    """Return (velocity m/s at ``at``, slope c1 of v(t), acceleration) from a fit.

    Two points: constant speed. Three or more: least-squares quadratic in time,
    centred on ``at`` for numerical stability.
    """

    if len(times) == 2:
        velocity = (positions[1] - positions[0]) / (times[1] - times[0])
        return velocity, 0.0, 0.0
    origin = times[len(times) // 2]
    scale = max(times) - min(times)
    if scale <= 0:
        raise ZeroDivisionError("지점 시간이 너무 가까워 계산할 수 없습니다.")
    xs = [(t - origin) / scale for t in times]
    # Normal equations for s = a + b·x + c·x².
    sums = [sum(x**power for x in xs) for power in range(5)]
    rhs = [sum(s * x**power for s, x in zip(positions, xs, strict=True)) for power in range(3)]
    matrix = [
        [sums[0], sums[1], sums[2]],
        [sums[1], sums[2], sums[3]],
        [sums[2], sums[3], sums[4]],
    ]
    _, b, c = _solve3(matrix, rhs)
    velocity = (b + 2 * c * ((at - origin) / scale)) / scale
    acceleration = 2 * c / scale**2
    return velocity, acceleration, acceleration


def _solve3(matrix: list[list[float]], rhs: list[float]) -> tuple[float, float, float]:
    """Solve a 3×3 linear system with Cramer's rule."""

    def det(m: list[list[float]]) -> float:
        return (
            m[0][0] * (m[1][1] * m[2][2] - m[1][2] * m[2][1])
            - m[0][1] * (m[1][0] * m[2][2] - m[1][2] * m[2][0])
            + m[0][2] * (m[1][0] * m[2][1] - m[1][1] * m[2][0])
        )

    base = det(matrix)
    if abs(base) < 1e-12:
        raise ZeroDivisionError("지점 시간이 너무 가까워 계산할 수 없습니다.")
    results = []
    for column in range(3):
        replaced = [row[:] for row in matrix]
        for row in range(3):
            replaced[row][column] = rhs[row]
        results.append(det(replaced) / base)
    return results[0], results[1], results[2]


def _window(chain: list[tuple[int, float]], trigger: int) -> list[tuple[int, float]]:
    """The ``FIT_WINDOW`` points nearest to T, kept in time order."""

    if len(chain) <= FIT_WINDOW:
        return chain
    best_start = min(
        range(len(chain) - FIT_WINDOW + 1),
        key=lambda start: (
            max(abs(frame - trigger) for frame, _ in chain[start : start + FIT_WINDOW]),
            -start,
        ),
    )
    return chain[best_start : best_start + FIT_WINDOW]


def estimate_speed(
    points: Iterable[SpeedPoint],
    fps: float,
    trigger_frame: int | None,
) -> SpeedEstimate:
    """Compute segment speeds and the speed at ``trigger_frame``."""

    ordered = normalize_points(points)
    estimate = SpeedEstimate(trigger_frame=trigger_frame)
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

    if trigger_frame is None:
        return estimate
    chains = _chains(ordered)
    if not chains:
        if ordered:
            estimate.warnings.append("거리가 입력된 구간이 2개 지점 이상 이어져야 T 속도를 계산해요.")
        return estimate

    def gap(chain: list[tuple[int, float]]) -> float:
        first, last = chain[0][0], chain[-1][0]
        return 0.0 if first <= trigger_frame <= last else min(abs(first - trigger_frame), abs(last - trigger_frame))

    chain = min(chains, key=lambda item: (gap(item), -len(item)))
    window = _window(chain, trigger_frame)
    origin_frame = window[0][0]
    times = [(frame - origin_frame) / fps for frame, _ in window]
    positions = [position for _, position in window]
    at = (trigger_frame - origin_frame) / fps

    try:
        velocity, slope, acceleration = _fit_velocity(times, positions, at)
    except ZeroDivisionError as exc:
        estimate.warnings.append(str(exc))
        return estimate

    span_first, span_last = window[0][0], window[-1][0]
    inside = span_first <= trigger_frame <= span_last
    if len(window) == 2:
        estimate.method = "구간 평균 속도" if inside else "인접 구간 평균 속도"
    else:
        estimate.method = "등가속도 근사" + ("" if inside else " · 범위 밖 외삽")

    if len(window) >= 3 and abs(acceleration) > MAX_PLAUSIBLE_ACCELERATION:
        estimate.warnings.append(
            f"지점 간 속도 변화가 커서(약 {abs(acceleration) / 9.81:.1f}g) 거리나 지점 위치를 확인해 주세요. "
            "T와 가장 가까운 구간의 평균 속도로 대신 계산했어요."
        )
        nearest = min(
            zip(window, window[1:], strict=False),
            key=lambda pair: (
                0 if pair[0][0] <= trigger_frame <= pair[1][0]
                else min(abs(pair[0][0] - trigger_frame), abs(pair[1][0] - trigger_frame)),
                abs((pair[0][0] + pair[1][0]) / 2 - trigger_frame),
            ),
        )
        window = list(nearest)
        origin_frame = window[0][0]
        times = [(frame - origin_frame) / fps for frame, _ in window]
        positions = [position for _, position in window]
        at = (trigger_frame - origin_frame) / fps
        velocity, slope, acceleration = _fit_velocity(times, positions, at)
        estimate.method = "인접 구간 평균 속도"

    span_first, span_last = window[0][0], window[-1][0]
    inside = span_first <= trigger_frame <= span_last

    if not inside:
        span = max(1, span_last - span_first)
        outside = min(abs(trigger_frame - span_first), abs(trigger_frame - span_last))
        if outside > span:
            estimate.warnings.append(
                "T가 지점 범위에서 멀리 떨어져 있어요. T 앞뒤로 지점을 추가하면 더 정확해요."
            )
        else:
            estimate.warnings.append("T가 지점 범위 밖이라 추정값이 외삽이에요.")

    low, high = _bounds(window, fps, (trigger_frame - window[0][0]) / fps)
    kph = velocity * MS_TO_KPH
    if not math.isfinite(kph) or kph < 0:
        estimate.warnings.append("속도를 유효하게 추정할 수 없어요. T 위치와 지점·거리를 확인해 주세요.")
        return estimate
    estimate.kph = kph
    estimate.kph_low = max(0.0, min(low, kph))
    estimate.kph_high = max(0.0, max(high, kph))
    estimate.points_used = tuple(frame for frame, _ in window)
    # v(t) in km/h with t in seconds: c0 + c1·t
    c1 = slope * MS_TO_KPH
    estimate.velocity_line = (kph - c1 * trigger_frame / fps, c1)
    return estimate


def _bounds(window: list[tuple[int, float]], fps: float, at: float) -> tuple[float, float]:
    """Corner-trial placement sensitivity, not a statistical confidence interval.

    ``at`` is seconds relative to the first point. Distance and model error are
    not covered by these frame-only sensitivity bounds.
    """

    half = 0.5 / fps
    count = len(window)
    if count <= EXHAUSTIVE_BOUND_POINTS:
        shifts: Iterable[tuple[int, ...]] = itertools.product((-1, 1), repeat=count)
    else:
        shifts = [tuple(1 if i % 2 else -1 for i in range(count)), tuple(-1 if i % 2 else 1 for i in range(count))]
    values: list[float] = []
    unbounded = False
    positions = [position for _, position in window]
    for signs in shifts:
        times = [(frame - window[0][0]) / fps + sign * half for (frame, _), sign in zip(window, signs, strict=True)]
        if any(later <= earlier for earlier, later in zip(times, times[1:], strict=False)):
            unbounded = True
            continue
        try:
            velocity, _, _ = _fit_velocity(times, positions, at)
        except ZeroDivisionError:
            continue
        if math.isfinite(velocity):
            values.append(velocity * MS_TO_KPH)
    if not values:
        return math.inf, math.inf
    return min(values), math.inf if unbounded else max(values)


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
    "SpeedEstimate",
    "SpeedPoint",
    "SpeedSegment",
    "estimate_speed",
    "points_from_json",
    "points_to_json",
    "speed_label",
]
