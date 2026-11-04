"""Read-only statistics over a workspace's investigation records.

This module deliberately has no Qt, video-decoder, or persistence dependencies.
Counts always represent distinct accidents, including for multiple-choice fields.
"""

from __future__ import annotations

import math
from collections import Counter
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime
from statistics import mean, median
from typing import Any

from investigator.form_definition import FormDefinition
from investigator.models import InvestigationCase
from investigator.schema import (
    Choice,
    FieldKind,
    FieldSpec,
    is_blank,
    missing_questions,
    validation_errors,
)
from investigator.speed_estimation import estimate_speed, points_from_json
from investigator.statistics_analysis import (
    StatisticalTest,
    chi_square,
    kruskal_wallis,
    pearson,
    percentile,
)

MISSING_LABEL = "미입력"
NOT_APPLICABLE_LABEL = "해당 없음"
INVALID_LABEL = "형식 오류"
SEVERITY_KEY = "severity"
INTERVAL_KEY = "trigger_collision_seconds"
TRIGGER_SPEED_KEY = "trigger_speed_kph"
MAX_CROSS_AXIS_CATEGORIES = 100
MAX_CROSS_CELLS = 2_500
_NUMERIC_EXCEPTIONS = {"accident_classification", "incident_date", "incident_time", "vehicle_model_year"}
_ORDERED_FIELDS = {
    "driver_age", "incident_day", "trigger_vehicle_speed", "collision_dist_range",
    "obstacle_dist_range", SEVERITY_KEY,
}
_SEVERITY_CHOICES = (
    Choice("사망", "fatal"), Choice("중상", "severe"), Choice("경상", "minor"),
    Choice("물적피해", "property"), Choice("피해 없음", "none"), Choice("확인불가", "unknown"),
)


class CrossTableLimitError(ValueError):
    """The requested table exceeds the desktop view's bounded matrix size."""

    def __init__(self, row_count: int, column_count: int) -> None:
        self.row_count = row_count
        self.column_count = column_count
        self.max_cells = MAX_CROSS_CELLS
        self.max_axis_categories = MAX_CROSS_AXIS_CATEGORIES
        super().__init__(
            f"분류가 너무 많아 교차표를 표시할 수 없습니다 "
            f"(행 {row_count:,}개 × 열 {column_count:,}개). "
            f"각 축 {MAX_CROSS_AXIS_CATEGORIES:,}개, 전체 {MAX_CROSS_CELLS:,}칸 이하가 되도록 "
            "필터로 사고를 좁히거나 선택형 항목을 골라 주세요."
        )


@dataclass(frozen=True, slots=True, init=False)
class StatsCondition:
    field_key: str
    value: Any = None
    operator: str = "eq"
    lower: float | None = None
    upper: float | None = None
    values: tuple[Any, ...] = ()
    exclude: bool = False

    def __init__(
        self, field_key: str, value: Any = None, operator: str = "eq",
        lower: float | None = None, upper: float | None = None, *,
        values: tuple[Any, ...] | list[Any] | None = None, exclude: bool = False,
    ) -> None:
        """A value set or half-open numeric range, with legacy call support.

        ``None`` inside ``values`` represents an unanswered field. Explicit
        value sets use their own operation so old ``ne`` filters keep excluding
        blanks, while new exclusion sets mean *none* of the selected responses.
        The original five positional arguments remain valid for chart/report
        callers and previously captured filter states.
        """
        explicit_values = values is not None
        if explicit_values:
            if not isinstance(values, (tuple, list)):
                raise ValueError("통계 조건의 선택값 형식이 잘못되었습니다.")
            selected = tuple(values)
            if operator == "eq":
                operator = ("range" if not selected and (lower is not None or upper is not None)
                            else "values")
        elif operator in {"missing", "not_missing"}:
            selected = (None,)
        elif operator in {"range", "between", "invalid", "not_applicable"}:
            selected = ()
        elif operator == "eq" and (lower is not None or upper is not None):
            selected, operator = (), "range"
        else:
            selected = (value,)
        for name, item in (
            ("field_key", field_key), ("value", None if operator == "values" else value),
            ("operator", operator),
            ("lower", lower), ("upper", upper), ("values", selected),
            ("exclude", bool(exclude) or operator == "not_missing"),
        ):
            object.__setattr__(self, name, item)


@dataclass(frozen=True, slots=True)
class AnyOf:
    """One non-nested OR group inside the outer AND filter list."""

    conditions: tuple[StatsCondition, ...]

    def __post_init__(self) -> None:
        conditions = tuple(self.conditions)
        if not conditions or any(not isinstance(item, StatsCondition) for item in conditions):
            raise ValueError("또는 묶음에는 하나 이상의 개별 조건만 넣을 수 있습니다.")
        object.__setattr__(self, "conditions", conditions)


FilterItem = StatsCondition | AnyOf


def normalize_condition(value: StatsCondition | dict[str, Any]) -> StatsCondition:
    """Read old saved conditions without losing legacy comparison semantics."""
    if isinstance(value, dict):
        if "field_key" not in value:
            raise ValueError("통계 조건의 항목이 없습니다.")
        kwargs = {key: value[key] for key in ("value", "operator", "lower", "upper", "exclude")
                  if key in value}
        if "values" in value:
            selected = value["values"]
            if not isinstance(selected, (list, tuple)):
                raise ValueError("통계 조건의 선택값 형식이 잘못되었습니다.")
            kwargs["values"] = tuple(selected)
        value = StatsCondition(value["field_key"], **kwargs)
    if not isinstance(value, StatsCondition):
        raise ValueError("통계 조건 형식이 잘못되었습니다.")
    if value.operator == "eq" and value.value is not None:
        return StatsCondition(value.field_key, values=(value.value,), exclude=value.exclude)
    if value.operator in {"missing", "not_missing"}:
        return StatsCondition(value.field_key, values=(None,),
                              exclude=value.operator == "not_missing")
    return value


def normalize_filter_items(
    conditions, condition_mode: str = "all", required_conditions=(),
) -> tuple[FilterItem, ...]:
    """Migrate old global OR / graph constraints into a flat outer AND list."""
    if condition_mode not in {"all", "any"}:
        raise ValueError("지원하지 않는 조건 결합 방식입니다.")

    def item(raw) -> FilterItem:
        if isinstance(raw, AnyOf):
            return AnyOf(tuple(normalize_condition(child) for child in raw.conditions))
        if isinstance(raw, dict) and "conditions" in raw and "field_key" not in raw:
            members = raw["conditions"]
            if not isinstance(members, (tuple, list)) or any(
                isinstance(child, AnyOf) or isinstance(child, dict) and "conditions" in child
                for child in members
            ):
                raise ValueError("또는 묶음은 중첩할 수 없습니다.")
            return AnyOf(tuple(normalize_condition(child) for child in members))
        return normalize_condition(raw)

    result = tuple(item(raw) for raw in conditions)
    if condition_mode == "any" and len(result) > 1:
        leaves = tuple(child for entry in result
                       for child in (entry.conditions if isinstance(entry, AnyOf) else (entry,)))
        result = (AnyOf(leaves),)
    return (*result, *(item(raw) for raw in required_conditions))


@dataclass(frozen=True, slots=True)
class StatsFilter:
    status: str = "completed"
    date_from: str = ""
    date_to: str = ""
    conditions: tuple[FilterItem, ...] = ()
    # Only the condition group is combined with OR; status/date remain AND.
    # Keep new options after the original fields for positional compatibility.
    condition_mode: str = "all"
    # Graph drill-down selections always narrow the manual condition group.
    required_conditions: tuple[StatsCondition, ...] = ()


@dataclass(frozen=True, slots=True)
class Category:
    label: str
    count: int
    case_uids: tuple[str, ...]
    value: Any = None
    missing: bool = False
    condition: StatsCondition | None = None

    def percent(self, denominator: int) -> float:
        return self.count * 100 / denominator if denominator else 0.0


@dataclass(frozen=True, slots=True)
class Distribution:
    field_key: str
    label: str
    kind: str
    total: int
    denominator: int
    answered: int
    missing: int
    not_applicable: int
    buckets: tuple[Category, ...]
    summary: dict[str, float | int]
    multi: bool = False


@dataclass(frozen=True, slots=True)
class CrossTable:
    row_key: str
    column_key: str
    row_label: str
    column_label: str
    rows: tuple[Category, ...]
    columns: tuple[Category, ...]
    cells: tuple[tuple[Category, ...], ...]
    total: int
    denominator: int
    row_multi: bool = False
    column_multi: bool = False


@dataclass(frozen=True, slots=True)
class QualityIssue:
    case_uid: str
    display_id: str
    status: str
    missing: tuple[str, ...]
    errors: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class FieldRate:
    field_key: str
    label: str
    eligible: int
    answered: int
    missing: int
    invalid: int
    rate: float


@dataclass(frozen=True, slots=True)
class QualityReport:
    total: int
    completed: int
    draft: int
    complete_valid: int
    missing_cases: int
    invalid_cases: int
    field_missing: tuple[Category, ...]
    issues: tuple[QualityIssue, ...]
    completion_rate: float
    required_answer_rate: float
    field_rates: tuple[FieldRate, ...] = ()


@dataclass(frozen=True, slots=True)
class NumericGroup:
    label: str
    case_uids: tuple[str, ...]
    values: tuple[float, ...]
    n: int
    min: float
    q1: float
    median: float
    q3: float
    max: float
    condition: StatsCondition | None


@dataclass(frozen=True, slots=True)
class NumericGroups:
    category_key: str
    numeric_key: str
    category_label: str
    numeric_label: str
    unit: str
    total: int
    valid_count: int
    excluded_count: int
    groups: tuple[NumericGroup, ...]
    test: StatisticalTest
    multi: bool = False


@dataclass(frozen=True, slots=True)
class ScatterPoint:
    case_uid: str
    x: float
    y: float
    label: str


@dataclass(frozen=True, slots=True)
class ScatterReport:
    x_key: str
    y_key: str
    x_label: str
    y_label: str
    x_unit: str
    y_unit: str
    total: int
    n: int
    excluded: int
    points: tuple[ScatterPoint, ...]
    slope: float | None
    intercept: float | None
    r: float | None
    test: StatisticalTest


@dataclass(frozen=True, slots=True)
class VideoReport:
    cases: int
    total_videos: int
    with_trigger: int
    with_collision: int
    with_both: int
    missing_fps: int
    invalid_order: int
    no_active_video: int
    valid_intervals: int
    intervals: Distribution
    median_seconds: float | None


def _number(value: Any) -> float | None:
    if isinstance(value, (bool, list, dict)) or _blank(value):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if math.isfinite(result) else None


def _blank(value: Any) -> bool:
    return (is_blank(value) or isinstance(value, str) and not value.strip()
            or isinstance(value, list) and all(_blank(item) for item in value))


def _date(value: Any) -> str | None:
    text = str(value or "").replace("-", "")
    if len(text) != 8 or not text.isdigit():
        return None
    try:
        datetime.strptime(text, "%Y%m%d")
    except ValueError:
        return None
    return text


def _dedupe(values: list[Any]) -> list[Any]:
    # Avoid requiring hashable values when examining legacy/malformed input.
    result: list[Any] = []
    for value in values:
        if value not in result:
            result.append(value)
    return result


class StatisticsEngine:
    """A snapshot of current in-memory records; never modifies an investigation.

    ``video_fps`` is supplied by the UI's background metadata reader. Missing or
    invalid FPS values exclude seconds metrics rather than assuming a frame rate.
    """

    def __init__(
        self,
        cases: list[InvestigationCase] | tuple[InvestigationCase, ...],
        definition: FormDefinition,
        video_fps: dict[str, float] | None = None,
    ) -> None:
        self.cases = tuple(cases)
        self.definition = definition
        self._definition_fields = definition.fields
        self._canonical_fields = definition._canonical_fields()
        self.fields = (
            *self._definition_fields,
            FieldSpec(SEVERITY_KEY, "사고 심각도", FieldKind.SINGLE, choices=_SEVERITY_CHOICES),
            FieldSpec(INTERVAL_KEY, "트리거 → 충돌 시간", FieldKind.DECIMAL, unit="초"),
            FieldSpec(TRIGGER_SPEED_KEY, "T 시점 추정 속도", FieldKind.DECIMAL, unit="km/h"),
        )
        self._fields = {field.key: field for field in self.fields}
        self._stored_values = {case.uid: deepcopy(case.values) for case in self.cases}
        self._values = {
            case.uid: definition.canonical_values(self._stored_values[case.uid]) for case in self.cases
        }
        self._statuses = {case.uid: case.status for case in self.cases}
        self._display_ids = {case.uid: case.display_id for case in self.cases}
        self._videos = {case.uid: tuple(case.videos) for case in self.cases}
        self._active_videos = {case.uid: case.active_video for case in self.cases}
        self._markers_by_case = {case.uid: deepcopy(case.markers_by_video) for case in self.cases}
        self._fps = {str(key).casefold(): value for key, value in (video_fps or {}).items()}
        self._filter_cache: dict[tuple, tuple[InvestigationCase, ...]] = {}
        self._distribution_cache: dict[tuple, Distribution] = {}
        self._cross_cache: dict[tuple, CrossTable] = {}
        self._quality_cache: dict[tuple, QualityReport] = {}
        self._video_cache: dict[tuple, VideoReport] = {}
        self._overview_cache: dict[tuple, dict[str, Any]] = {}
        self._validation_cache: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {}
        self._speed_cache: dict[str, float | None] = {}
        self._groups_cache: dict[tuple, NumericGroups] = {}
        self._scatter_cache: dict[tuple, ScatterReport] = {}
        self._association_cache: dict[tuple, StatisticalTest] = {}
        self._condition_choices_cache: dict[str, tuple[Choice, ...]] = {}
        self._condition_values_cache: dict[tuple[Any, ...], frozenset[Any]] = {}

    @staticmethod
    def _filter_key(filters: StatsFilter | None, *, ignore_status: bool = False) -> tuple:
        filters = filters or StatsFilter()

        def item_key(condition: FilterItem) -> tuple:
            if isinstance(condition, AnyOf):
                return ("any", tuple(item_key(child) for child in condition.conditions))
            return ("condition", condition.field_key, condition.operator, repr(condition.value),
                    repr(condition.values), condition.exclude, condition.lower, condition.upper)

        return (
            "all" if ignore_status else filters.status, filters.date_from, filters.date_to,
            tuple(item_key(condition) for condition in filters.conditions),
            filters.condition_mode,
            tuple(item_key(condition) for condition in filters.required_conditions),
        )

    def field(self, key: str) -> FieldSpec:
        return self._fields[key]

    def is_numeric(self, key: str) -> bool:
        spec = self.field(key)
        return spec.kind in {FieldKind.DIGITS, FieldKind.DECIMAL} and key not in _NUMERIC_EXCEPTIONS

    def is_ordered(self, key: str) -> bool:
        return key in _ORDERED_FIELDS

    def condition_choices(self, field_key: str) -> tuple[Choice, ...]:
        """Return configured labels plus distinct currently stored responses.

        The form's order and unused options are retained. Legacy/custom text
        responses follow in first-seen order; values are never replaced by CSV
        ordinal codes or display labels, so choosing one remains an exact match.
        """
        spec = self.field(field_key)
        if self.is_numeric(field_key):
            return ()
        if field_key not in self._condition_choices_cache:
            result = list(spec.choices)
            # Allowed observed values are hashable JSON scalars. Keep membership
            # linear overall even for thousands of distinct free-text answers.
            seen = {choice.value for choice in result}
            for case in self.cases:
                if not self.is_applicable(case, field_key):
                    continue
                raw = self.case_value(case, field_key)
                values = raw if isinstance(raw, list) else [raw]
                for value in values:
                    if (
                        _blank(value)
                        or not isinstance(value, (str, int, float, bool))
                        or isinstance(value, float) and not math.isfinite(value)
                        or value in seen
                    ):
                        continue
                    # JSON legacy values can be non-string scalars. Preserve
                    # their type as item data while showing a readable label.
                    result.append(Choice(str(value), value))
                    seen.add(value)
            self._condition_choices_cache[field_key] = tuple(result)
        return self._condition_choices_cache[field_key]

    def is_applicable(self, case: InvestigationCase, field_key: str) -> bool:
        spec = self.field(field_key)
        values = self._values[case.uid]
        if spec.group == "obstacle_detail" and values.get("obstacle_exists") == "무":
            return False
        if (
            field_key == "obstacle_vehicle_state"
            and values.get("obstacle_exists") == "유"
            and not values.get("obstacle_vehicle_types")
        ):
            return False
        return not (
            spec.group == "damage_counts"
            and values.get("incident_damage_unknown") == "확인불가"
        )

    def case_value(self, case: InvestigationCase, field_key: str) -> Any:
        if field_key == SEVERITY_KEY:
            return self._severity(case)
        if field_key == INTERVAL_KEY:
            return self._interval(case)[0]
        if field_key == TRIGGER_SPEED_KEY:
            return self._trigger_speed(case)
        # Stored choice values match the effective FieldSpec after customization.
        return self._stored_values[case.uid].get(field_key)

    def _trigger_speed(self, case: InvestigationCase) -> float | None:
        if case.uid not in self._speed_cache:
            path = self._active_videos[case.uid]
            fps = _number(self._fps.get(str(path).casefold())) if path else None
            marker = self._markers_by_case[case.uid].get(path, {}) if path else {}
            value = None
            if fps is not None and fps > 0 and marker.get("trigger_frame") is not None:
                estimate = estimate_speed(
                    points_from_json(marker.get("speed_points")), fps, marker["trigger_frame"]
                )
                value = _number(estimate.kph)
                if value is not None and value < 0:
                    value = None
            self._speed_cache[case.uid] = value
        return self._speed_cache[case.uid]

    def _severity(self, case: InvestigationCase) -> str | None:
        values = self._values[case.uid]
        if values.get("incident_damage_unknown") == "확인불가":
            return "unknown"
        count_keys = (
            ("incident_fatality", "fatal"), ("incident_severe_injury", "severe"),
            ("incident_minor_injury", "minor"), ("incident_property_damage", "property"),
        )
        for key, severity in count_keys:
            value = _number(values.get(key))
            if value is not None and value > 0:
                return severity
        numbers = [_number(values.get(key)) for key, _ in count_keys]
        if all(number == 0 for number in numbers):
            return "none"
        return None

    def _markers(self, case: InvestigationCase, path: str) -> tuple[int | None, int | None]:
        marker = self._markers_by_case[case.uid].get(path, {})
        trigger = marker.get("trigger_frame")
        collision = marker.get("collision_frame")
        if collision is None:
            collisions = marker.get("collision_frames", [])
            collision = collisions[0] if isinstance(collisions, list) and collisions else None
        return trigger, collision

    def _interval(self, case: InvestigationCase) -> tuple[float | None, str | None]:
        path = self._active_videos[case.uid]
        if path is None:
            return None, "no_video"
        trigger, collision = self._markers(case, path)
        if trigger is None or collision is None:
            return None, "missing_markers"
        trigger_number, collision_number = _number(trigger), _number(collision)
        if trigger_number is None or collision_number is None or trigger_number > collision_number:
            return None, "invalid_order"
        fps = _number(self._fps.get(str(path).casefold()))
        if fps is None or fps <= 0:
            return None, "missing_fps"
        return (collision_number - trigger_number) / fps, None

    def filtered_cases(
        self, filters: StatsFilter | None = None, *, ignore_status: bool = False
    ) -> tuple[InvestigationCase, ...]:
        filters = filters or StatsFilter()
        if filters.status not in {"all", "completed", "draft"}:
            raise ValueError("지원하지 않는 사고 상태입니다.")
        # Legacy global OR and graph constraints are accepted at this boundary;
        # evaluation itself has only outer AND and explicit one-level AnyOf.
        items = normalize_filter_items(
            filters.conditions, filters.condition_mode, filters.required_conditions,
        )
        # Validate even with no cases or when an earlier OR condition matches.
        for condition in items:
            self.validate_condition(condition)
        date_from = _date(filters.date_from) if filters.date_from else None
        date_to = _date(filters.date_to) if filters.date_to else None
        if filters.date_from and date_from is None or filters.date_to and date_to is None:
            raise ValueError("날짜 필터 형식이 잘못되었습니다.")
        if date_from and date_to and date_from > date_to:
            raise ValueError("시작일이 종료일보다 늦습니다.")
        cache_key = self._filter_key(filters, ignore_status=ignore_status)
        if cache_key in self._filter_cache:
            return self._filter_cache[cache_key]
        result = []
        for case in self.cases:
            if not ignore_status and filters.status != "all" and self._statuses[case.uid] != filters.status:
                continue
            if date_from or date_to:
                incident_date = _date(self._values[case.uid].get("incident_date"))
                if incident_date is None:
                    continue
                if date_from and incident_date < date_from or date_to and incident_date > date_to:
                    continue
            if all(self._matches_validated(case, condition) for condition in items):
                result.append(case)
        self._filter_cache[cache_key] = tuple(result)
        return self._filter_cache[cache_key]

    def validate_condition(self, condition: FilterItem) -> None:
        """Reject malformed conditions before any rows are examined."""
        if isinstance(condition, AnyOf):
            for child in condition.conditions:
                self.validate_condition(child)
            return
        if not isinstance(condition, StatsCondition):
            raise ValueError("통계 조건 형식이 잘못되었습니다.")
        self.field(condition.field_key)  # Fail early for deleted or mistyped fields.
        operator = condition.operator
        if operator in {"missing", "not_missing", "not_applicable", "invalid"}:
            return
        if operator == "values":
            numeric = self.is_numeric(condition.field_key)
            for value in condition.values:
                if value is None:
                    continue
                if (not isinstance(value, (str, int, float, bool))
                    or isinstance(value, float) and not math.isfinite(value)
                    or numeric and _number(value) is None):
                    raise ValueError("유효한 선택값을 입력해 주세요.")
            return
        if operator not in {"eq", "ne", "gt", "ge", "lt", "le", "between", "range"}:
            raise ValueError("지원하지 않는 통계 필터입니다.")
        numeric = self.is_numeric(condition.field_key)
        if not numeric:
            if operator not in {"eq", "ne"}:
                raise ValueError("수치형 항목에서만 숫자 비교를 사용할 수 있습니다.")
            return
        if operator in {"between", "range"}:
            lower, upper = condition.lower, condition.upper
            if operator == "between" and (lower is None or upper is None):
                raise ValueError("범위의 최솟값과 최댓값을 입력해 주세요.")
            if any(bound is not None and _number(bound) is None for bound in (lower, upper)):
                raise ValueError("유효한 숫자를 입력해 주세요.")
            if lower is not None and upper is not None and float(lower) > float(upper):
                raise ValueError("최솟값이 최댓값보다 큽니다.")
        elif _number(condition.value) is None:
            raise ValueError("유효한 숫자를 입력해 주세요.")

    def matches(self, case: InvestigationCase, condition: FilterItem) -> bool:
        self.validate_condition(condition)
        return self._matches_validated(case, condition)

    def _matches_validated(self, case: InvestigationCase, condition: FilterItem) -> bool:
        if isinstance(condition, AnyOf):
            return any(self._matches_validated(case, child) for child in condition.conditions)
        applicable = self.is_applicable(case, condition.field_key)
        if condition.operator == "not_applicable":
            return not applicable
        if not applicable:
            return False
        value = self.case_value(case, condition.field_key)
        if condition.operator == "missing":
            return _blank(value)
        if condition.operator == "not_missing":
            return not _blank(value)
        if condition.operator == "invalid":
            return not _blank(value) and self.is_numeric(condition.field_key) and _number(value) is None
        if condition.operator == "values":
            selected = self._condition_values_cache.get(condition.values)
            if selected is None:
                selected = frozenset(condition.values)
                self._condition_values_cache[condition.values] = selected
            if _blank(value):
                matched = None in selected
            elif self.is_numeric(condition.field_key):
                number = _number(value)
                matched = number is not None and any(
                    item is not None and number == _number(item) for item in selected
                )
            else:
                responses = value if isinstance(value, list) else (value,)
                matched = any(item in selected for item in responses
                              if isinstance(item, (str, int, float, bool)) and not _blank(item))
            return not matched if condition.exclude else matched
        if condition.operator in {"range", "between"}:
            number = _number(value)
            if number is None:
                return condition.exclude
            within_lower = condition.lower is None or number >= float(condition.lower)
            within_upper = condition.upper is None or (
                number <= float(condition.upper) if condition.operator == "between"
                else number < float(condition.upper)
            )
            matched = within_lower and within_upper
            return not matched if condition.exclude else matched
        if self.is_numeric(condition.field_key):
            number, comparison = _number(value), _number(condition.value)
            if number is None or comparison is None:
                return False
            return {
                "eq": number == comparison,
                "ne": number != comparison,
                "gt": number > comparison,
                "ge": number >= comparison,
                "lt": number < comparison,
                "le": number <= comparison,
            }[condition.operator]
        if _blank(value):
            return False
        if isinstance(value, list):
            selected = [item for item in value if not _blank(item)]
            return (
                condition.value in selected if condition.operator == "eq"
                else bool(selected) and condition.value not in selected
            )
        return value == condition.value if condition.operator == "eq" else value != condition.value

    def distribution(self, field_key: str, filters: StatsFilter | None = None) -> Distribution:
        return self._distribution(field_key, self.filtered_cases(filters))

    def _distribution(
        self, field_key: str, cases: tuple[InvestigationCase, ...]
    ) -> Distribution:
        cache_key = (field_key, tuple(case.uid for case in cases))
        if cache_key in self._distribution_cache:
            return self._distribution_cache[cache_key]
        spec = self.field(field_key)
        applicable = tuple(case for case in cases if self.is_applicable(case, field_key))
        missing_uids = tuple(case.uid for case in applicable if _blank(self.case_value(case, field_key)))
        answered = len(applicable) - len(missing_uids)
        if self.is_numeric(field_key):
            buckets, summary = self._numeric_buckets(field_key, applicable)
        else:
            buckets = self._categorical_buckets(field_key, applicable)
            summary = {}
        if missing_uids:
            buckets.append(Category(
                MISSING_LABEL, len(missing_uids), missing_uids, missing=True,
                condition=StatsCondition(field_key, operator="missing"),
            ))
        result = Distribution(
            field_key, spec.label, "numeric" if self.is_numeric(field_key) else spec.kind.value,
            len(cases), len(applicable), answered, len(missing_uids), len(cases) - len(applicable),
            tuple(buckets), summary, spec.kind == FieldKind.MULTI,
        )
        self._distribution_cache[cache_key] = result
        return result

    def _categorical_buckets(
        self, field_key: str, cases: tuple[InvestigationCase, ...]
    ) -> list[Category]:
        spec = self.field(field_key)
        label_by_value = {choice.value: choice.label for choice in spec.choices}
        groups: dict[str, tuple[Any, list[str]]] = {}
        for case in cases:
            value = self.case_value(case, field_key)
            if _blank(value):
                continue
            selected = _dedupe(value) if isinstance(value, list) else [value]
            for item in selected:
                if _blank(item):
                    continue
                key = str(item)
                if key not in groups:
                    groups[key] = (item, [])
                groups[key][1].append(case.uid)
        result = [
            Category(label_by_value.get(key, key), len(uids), tuple(uids), value,
                     condition=StatsCondition(field_key, value))
            for key, (value, uids) in groups.items()
        ]
        choice_order = {str(choice.value): index for index, choice in enumerate(spec.choices)}
        if field_key in _ORDERED_FIELDS:
            result.sort(key=lambda category: (choice_order.get(str(category.value), 10_000), category.label))
        else:
            result.sort(key=lambda category: (-category.count, category.label.casefold()))
        return result

    @staticmethod
    def _edges(field_key: str, values: list[float]) -> list[float]:
        if field_key == "vehicle_mileage":
            return [0, 20_000, 40_000, 60_000, 80_000, 100_000, 150_000, 200_000, math.inf]
        if field_key == INTERVAL_KEY:
            return [0, 0.5, 1, 2, 3, 5, 10, math.inf]
        low, high = min(values), max(values)
        if low == high:
            return [low, math.inf]
        raw_step = (high - low) / min(8, max(2, math.ceil(math.sqrt(len(values)))))
        power = 10 ** math.floor(math.log10(raw_step))
        factor = raw_step / power
        step = next(number for number in (1, 2, 5, 10) if number >= factor) * power
        start = math.floor(low / step) * step
        count = max(1, math.floor((high - start) / step) + 1)
        return [start + index * step for index in range(count + 1)]

    def _numeric_buckets(
        self, field_key: str, cases: tuple[InvestigationCase, ...]
    ) -> tuple[list[Category], dict[str, float | int]]:
        pairs: list[tuple[float, str]] = []
        invalid_uids = []
        for case in cases:
            raw = self.case_value(case, field_key)
            if _blank(raw):
                continue
            number = _number(raw)
            if number is None:
                invalid_uids.append(case.uid)
            else:
                pairs.append((number, case.uid))
        buckets = []
        summary: dict[str, float | int] = {"count": len(pairs)}
        if pairs:
            numbers = [number for number, _ in pairs]
            summary.update(min=min(numbers), max=max(numbers), mean=mean(numbers), median=median(numbers))
            ordered = sorted(numbers)
            summary.update(q1=percentile(ordered, 0.25), q3=percentile(ordered, 0.75))
            edges = self._edges(field_key, numbers)
            if min(numbers) < edges[0]:
                edges.insert(0, -math.inf)
            for lower, upper in zip(edges, edges[1:], strict=False):
                uids = tuple(uid for number, uid in pairs if lower <= number < upper)
                if lower == -math.inf:
                    label = f"{upper:g} 미만"
                elif upper == math.inf:
                    label = f"{lower:g} 이상"
                else:
                    label = f"{lower:g}–{upper:g} 미만"
                buckets.append(Category(
                    label, len(uids), uids, (lower, upper),
                    condition=StatsCondition(
                        field_key, operator="range",
                        lower=None if lower == -math.inf else lower,
                        upper=None if upper == math.inf else upper,
                    ),
                ))
        if invalid_uids:
            buckets.append(Category(
                INVALID_LABEL, len(invalid_uids), tuple(invalid_uids),
                condition=StatsCondition(field_key, operator="invalid"),
            ))
        return buckets, summary

    def cross_table(
        self, row_key: str, column_key: str, filters: StatsFilter | None = None
    ) -> CrossTable:
        cache_key = (row_key, column_key, self._filter_key(filters))
        if cache_key in self._cross_cache:
            return self._cross_cache[cache_key]
        cases = self.filtered_cases(filters)
        applicable = tuple(
            case for case in cases
            if self.is_applicable(case, row_key) and self.is_applicable(case, column_key)
        )
        rows = self._distribution(row_key, applicable)
        columns = self._distribution(column_key, applicable)
        row_count, column_count = len(rows.buckets), len(columns.buckets)
        if (
            row_count > MAX_CROSS_AXIS_CATEGORIES
            or column_count > MAX_CROSS_AXIS_CATEGORIES
            or row_count * column_count > MAX_CROSS_CELLS
        ):
            raise CrossTableLimitError(row_count, column_count)
        cells = []
        for row in rows.buckets:
            row_uids = set(row.case_uids)
            cell_row = []
            for column in columns.buckets:
                uids = tuple(uid for uid in column.case_uids if uid in row_uids)
                cell_row.append(Category(f"{row.label} × {column.label}", len(uids), uids))
            cells.append(tuple(cell_row))
        result = CrossTable(
            row_key, column_key, rows.label, columns.label, rows.buckets, columns.buckets,
            tuple(cells), len(cases), len(applicable), rows.multi, columns.multi,
        )
        self._cross_cache[cache_key] = result
        return result

    def _valid_numeric(self, case: InvestigationCase, key: str) -> float | None:
        if not self.is_applicable(case, key):
            return None
        number = _number(self.case_value(case, key))
        if number is None or key in self._validation(case)[1]:
            return None
        return number

    def association(
        self, row_key: str, column_key: str, filters: StatsFilter | None = None
    ) -> StatisticalTest:
        cache_key = row_key, column_key, self._filter_key(filters)
        if cache_key in self._association_cache:
            return self._association_cache[cache_key]
        if row_key == column_key:
            return StatisticalTest("chi_square", reason="서로 다른 두 항목을 선택해 주세요.")
        if self.is_numeric(row_key) or self.is_numeric(column_key):
            return StatisticalTest("chi_square", reason="카이제곱 검정에는 범주형 항목이 필요합니다.")
        cross = self.cross_table(row_key, column_key, filters)
        rows = [index for index, category in enumerate(cross.rows) if not category.missing]
        columns = [index for index, category in enumerate(cross.columns) if not category.missing]
        row_counts = Counter(uid for index in rows for uid in cross.rows[index].case_uids)
        column_counts = Counter(uid for index in columns for uid in cross.columns[index].case_uids)
        complete = row_counts.keys() & column_counts.keys()
        # Only valid categorical responses enter the independence test; the
        # descriptive table still preserves malformed legacy values visibly.
        cases = {case.uid: case for case in self.filtered_cases(filters)}
        complete = {
            uid for uid in complete
            if row_key not in self._validation(cases[uid])[1]
            and column_key not in self._validation(cases[uid])[1]
        }
        if any(row_counts[uid] > 1 or column_counts[uid] > 1 for uid in complete):
            result = StatisticalTest("chi_square", n=len(complete),
                reason="한 사고가 여러 범주에 중복되어 독립성 검정을 하지 않았습니다.")
        else:
            matrix = [[len(set(cross.cells[row][column].case_uids) & complete)
                       for column in columns] for row in rows]
            result = chi_square(matrix)
        self._association_cache[cache_key] = result
        return result

    def numeric_groups(
        self, category_key: str, numeric_key: str, filters: StatsFilter | None = None
    ) -> NumericGroups:
        cache_key = category_key, numeric_key, self._filter_key(filters)
        if cache_key in self._groups_cache:
            return self._groups_cache[cache_key]
        if self.is_numeric(category_key) or not self.is_numeric(numeric_key):
            raise ValueError("범주 항목과 수치 항목을 각각 선택해 주세요.")
        cases = self.filtered_cases(filters)
        by_uid = {case.uid: case for case in cases}
        distribution = self._distribution(category_key, cases)
        groups, memberships = [], Counter()
        for bucket in distribution.buckets:
            if bucket.missing:
                continue
            pairs = []
            for uid in bucket.case_uids:
                case = by_uid[uid]
                number = self._valid_numeric(case, numeric_key)
                if number is not None and category_key not in self._validation(case)[1]:
                    pairs.append((uid, number))
            if not pairs:
                continue
            values = tuple(sorted(value for _, value in pairs))
            uids = tuple(uid for uid, _ in pairs)
            memberships.update(uids)
            groups.append(NumericGroup(
                bucket.label, uids, values, len(values), values[0], percentile(values, 0.25),
                percentile(values, 0.5), percentile(values, 0.75), values[-1], bucket.condition,
            ))
        if any(count > 1 for count in memberships.values()):
            test = StatisticalTest("kruskal_wallis", n=len(memberships),
                reason="같은 사고가 여러 그룹에 중복되어 독립 표본 검정을 하지 않았습니다.")
        else:
            test = kruskal_wallis([group.values for group in groups])
        result = NumericGroups(
            category_key, numeric_key, self.field(category_key).label, self.field(numeric_key).label,
            self.field(numeric_key).unit, len(cases), len(memberships), len(cases) - len(memberships),
            tuple(groups), test, distribution.multi,
        )
        self._groups_cache[cache_key] = result
        return result

    def scatter(
        self, x_key: str, y_key: str, filters: StatsFilter | None = None
    ) -> ScatterReport:
        cache_key = x_key, y_key, self._filter_key(filters)
        if cache_key in self._scatter_cache:
            return self._scatter_cache[cache_key]
        if not self.is_numeric(x_key) or not self.is_numeric(y_key):
            raise ValueError("두 항목 모두 수치 항목이어야 합니다.")
        cases = self.filtered_cases(filters)
        points = []
        for case in cases:
            x, y = self._valid_numeric(case, x_key), self._valid_numeric(case, y_key)
            if x is not None and y is not None:
                points.append(ScatterPoint(case.uid, x, y, self._display_ids[case.uid]))
        if x_key == y_key:
            r = slope = intercept = None
            test = StatisticalTest("pearson", n=len(points), reason="서로 다른 두 항목을 선택해 주세요.")
        else:
            r, slope, intercept, test = pearson(
                tuple(point.x for point in points), tuple(point.y for point in points)
            )
        result = ScatterReport(
            x_key, y_key, self.field(x_key).label, self.field(y_key).label,
            self.field(x_key).unit, self.field(y_key).unit, len(cases), len(points),
            len(cases) - len(points), tuple(points), slope, intercept, r, test,
        )
        self._scatter_cache[cache_key] = result
        return result

    def _validation(self, case: InvestigationCase) -> tuple[tuple[str, ...], tuple[str, ...]]:
        if case.uid not in self._validation_cache:
            values = self._values[case.uid]
            missing = tuple(
                key for key in missing_questions(values, self._canonical_fields)
                if key not in self._fields or self.is_applicable(case, key)
            )
            errors = {
                key for key in validation_errors(values, self._canonical_fields)
                if key not in self._fields or self.is_applicable(case, key)
            }
            # The completion validator also checks legacy case-only markers and
            # orphaned persisted entries, not just currently attached videos.
            for path in self._markers_by_case[case.uid]:
                trigger, collision = self._markers(case, path)
                if trigger is not None and collision is not None and trigger > collision:
                    errors.add("trigger_frame")
            self._validation_cache[case.uid] = missing, tuple(sorted(errors))
        return self._validation_cache[case.uid]

    def _required_keys(self, case: InvestigationCase) -> set[str]:
        values = self._values[case.uid]
        result = {
            field.key for field in self._definition_fields
            if field.required and field.key != "site_types" and self.is_applicable(case, field.key)
        }
        result.add("site_location")
        if values.get("obstacle_exists") == "무":
            result.difference_update({"obstacle_dist_source", "obstacle_dist_value", "obstacle_dist_range"})
        elif values.get("obstacle_exists") == "유":
            result.update({"obstacle_object_types", "obstacle_state"})
            if values.get("obstacle_vehicle_types"):
                result.add("obstacle_vehicle_state")
        if values.get("incident_damage_unknown") == "확인 가능":
            result.update({
                "incident_minor_injury", "incident_severe_injury",
                "incident_fatality", "incident_property_damage",
            })
        return result

    def quality(self, filters: StatsFilter | None = None) -> QualityReport:
        # A completed-only analytics filter must not hide unfinished work here.
        cache_key = self._filter_key(filters, ignore_status=True)
        if cache_key in self._quality_cache:
            return self._quality_cache[cache_key]
        cases = self.filtered_cases(filters, ignore_status=True)
        missing_by_field: dict[str, list[str]] = {}
        issues = []
        missing_cases = invalid_cases = complete_valid = required_total = answered_total = 0
        rate_counts: dict[str, list[int]] = {}
        for case in cases:
            missing, errors = self._validation(case)
            required = self._required_keys(case)
            required_total += len(required)
            answered_total += len(required - set(missing) - set(errors))
            for key in required:
                counts = rate_counts.setdefault(key, [0, 0, 0, 0])
                counts[0] += 1
                if key in missing:
                    counts[2] += 1
                elif key in errors:
                    counts[3] += 1
                else:
                    counts[1] += 1
            missing_cases += bool(missing)
            invalid_cases += bool(errors)
            complete_valid += not missing and not errors
            for key in missing:
                missing_by_field.setdefault(key, []).append(case.uid)
            if missing or errors:
                issues.append(QualityIssue(
                    case.uid, self._display_ids[case.uid], self._statuses[case.uid], missing, errors,
                ))
        categories = tuple(sorted(
            (Category(self.definition.label(key), len(uids), tuple(uids), key,
                      condition=StatsCondition(key, operator="missing") if key in self._fields else None)
             for key, uids in missing_by_field.items()),
            key=lambda category: (-category.count, category.label),
        ))
        issues.sort(key=lambda issue: (-(len(issue.missing) + len(issue.errors)), issue.display_id))
        completed = sum(self._statuses[case.uid] == "completed" for case in cases)
        field_rates = tuple(sorted(
            (FieldRate(key, self.definition.label(key), eligible, answered, missing, invalid,
                       answered * 100 / eligible if eligible else 0.0)
             for key, (eligible, answered, missing, invalid) in rate_counts.items()),
            key=lambda rate: (rate.rate, rate.label),
        ))
        result = QualityReport(
            len(cases), completed, len(cases) - completed, complete_valid,
            missing_cases, invalid_cases, categories, tuple(issues),
            completed * 100 / len(cases) if cases else 0.0,
            answered_total * 100 / required_total if required_total else 0.0,
            field_rates,
        )
        self._quality_cache[cache_key] = result
        return result

    def video(self, filters: StatsFilter | None = None) -> VideoReport:
        cache_key = self._filter_key(filters)
        if cache_key in self._video_cache:
            return self._video_cache[cache_key]
        cases = self.filtered_cases(filters)
        total_videos = with_trigger = with_collision = with_both = 0
        reasons: Counter[str] = Counter()
        seconds = []
        for case in cases:
            for path in self._videos[case.uid]:
                total_videos += 1
                trigger, collision = self._markers(case, path)
                with_trigger += trigger is not None
                with_collision += collision is not None
                with_both += trigger is not None and collision is not None
            interval, reason = self._interval(case)
            if reason:
                reasons[reason] += 1
            elif interval is not None:
                seconds.append(interval)
        result = VideoReport(
            len(cases), total_videos, with_trigger, with_collision, with_both,
            reasons["missing_fps"], reasons["invalid_order"], reasons["no_video"],
            len(seconds), self._distribution(INTERVAL_KEY, cases),
            median(seconds) if seconds else None,
        )
        self._video_cache[cache_key] = result
        return result

    def overview(self, filters: StatsFilter | None = None) -> dict[str, Any]:
        cache_key = self._filter_key(filters)
        if cache_key in self._overview_cache:
            return self._overview_cache[cache_key]
        cases = self.filtered_cases(filters)
        video = self.video(filters)
        completed = sum(self._statuses[case.uid] == "completed" for case in cases)
        result = {
            "selected": len(cases), "total": len(self.cases), "completed": completed,
            "draft": len(cases) - completed, "videos": video.total_videos,
            "severe_cases": sum(self._severity(case) in {"fatal", "severe"} for case in cases),
            "injury_cases": sum(self._severity(case) in {"fatal", "severe", "minor"} for case in cases),
            "missing_cases": sum(bool(self._validation(case)[0]) for case in cases),
            "completion_rate": completed * 100 / len(cases) if cases else 0.0,
            "mean_interval_seconds": video.intervals.summary.get("mean"),
            "median_interval_seconds": video.median_seconds,
        }
        self._overview_cache[cache_key] = result
        return result
