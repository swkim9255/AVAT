"""Read-only, accident-level cohorts for a mirrored distribution comparison."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from investigator.statistics import (
    MAX_CROSS_AXIS_CATEGORIES,
    Category,
    CrossTableLimitError,
    StatisticsEngine,
    StatsCondition,
    StatsFilter,
)


@dataclass(frozen=True, slots=True)
class ButterflyRow:
    label: str
    left_uids: tuple[str, ...]
    right_uids: tuple[str, ...]
    missing: bool = False
    condition: StatsCondition | None = None

    @property
    def left_count(self) -> int:
        return len(self.left_uids)

    @property
    def right_count(self) -> int:
        return len(self.right_uids)


@dataclass(frozen=True, slots=True)
class ButterflyReport:
    axis_label: str
    group_label: str
    left_label: str
    right_label: str
    left_total: int
    right_total: int
    total: int
    excluded: int
    overlap: int
    rows: tuple[ButterflyRow, ...]
    warnings: tuple[str, ...]


def group_value_key(value: Any) -> tuple[type, Any]:
    """Keep stored scalar types, rather than matching labels or string codes."""
    if (value is None or not isinstance(value, (str, int, float, bool))
            or isinstance(value, float) and not math.isfinite(value)):
        raise ValueError("유효한 그룹 값을 선택해 주세요.")
    return type(value), value


def normalize_group_values(values) -> tuple[Any, ...]:
    if not isinstance(values, (tuple, list)):
        raise ValueError("그룹 선택값 형식이 잘못되었습니다.")
    selected, seen = [], set()
    for value in values:
        token = group_value_key(value)
        if token not in seen:
            selected.append(value)
            seen.add(token)
    return tuple(selected)


def butterfly_groups(engine: StatisticsEngine, key: str, filters: StatsFilter) -> tuple[Category, ...]:
    if engine.is_numeric(key):
        raise ValueError("좌우로 나눌 항목은 범주 항목을 선택해 주세요.")
    cases = engine.filtered_cases(filters)
    scoped = {case.uid for case in cases}
    # Keep the option universe independent of the current filters. A text value
    # disappearing from a slice must not reset the user's chosen groups.
    choices = {group_value_key(choice.value): (choice.label, choice.value, set())
               for choice in engine.field(key).choices}
    for case in engine.cases:
        if not engine.is_applicable(case, key) or key in engine._validation(case)[1]:
            continue
        raw = engine.case_value(case, key)
        for value in raw if isinstance(raw, list) else (raw,):
            if value is None or isinstance(value, str) and not value.strip():
                continue
            try:
                token = group_value_key(value)
            except ValueError:
                continue
            if token not in choices:
                choices[token] = (str(value), value, set())
            if case.uid in scoped:
                choices[token][2].add(case.uid)
    result = [Category(label, len(uids), tuple(case.uid for case in cases if case.uid in uids), value,
                       condition=StatsCondition(key, values=(value,)))
              for label, value, uids in choices.values()]
    if len(result) > MAX_CROSS_AXIS_CATEGORIES:
        raise CrossTableLimitError(1, len(result))
    return tuple(result)


def build_butterfly(
    engine: StatisticsEngine,
    axis_key: str,
    group_key: str,
    filters: StatsFilter,
    *,
    left_value: Any = None,
    right_value: Any = None,
    mode: str = "two",
    left_values: tuple[Any, ...] | None = None,
    right_values: tuple[Any, ...] | None = None,
) -> ButterflyReport:
    if axis_key == group_key:
        raise ValueError("서로 다른 가운데 축과 좌우 그룹 항목을 선택해 주세요.")
    if mode not in {"two", "rest"}:
        raise ValueError("두 그룹 또는 한 그룹 vs 나머지를 선택해 주세요.")
    options = butterfly_groups(engine, group_key, filters)
    selected_left = normalize_group_values(left_values if left_values is not None else (left_value,))
    selected_right = normalize_group_values(right_values if right_values is not None else (right_value,)) if mode == "two" else ()
    by_value = {group_value_key(group.value): group for group in options}
    left_keys = {group_value_key(value) for value in selected_left}
    right_keys = {group_value_key(value) for value in selected_right}
    if (not selected_left or mode == "two" and (not selected_right or left_keys & right_keys)
            or not left_keys <= by_value.keys() or not right_keys <= by_value.keys()):
        raise ValueError("서로 다른 좌우 그룹을 선택해 주세요.")
    left_label = ", ".join(by_value[group_value_key(value)].label for value in selected_left)
    cases = engine.filtered_cases(filters)
    order = [case.uid for case in cases]
    eligible = {uid for group in options for uid in group.case_uids}
    left_ids = {uid for token in left_keys for uid in by_value[token].case_uids}
    # This is a complement of accident membership, not a union of other labels.
    right_ids = (eligible - left_ids if mode == "rest" else
                 {uid for token in right_keys for uid in by_value[token].case_uids})
    right_label = (f"{left_label} 외 나머지" if mode == "rest" else
                   ", ".join(by_value[group_value_key(value)].label for value in selected_right))
    distribution = engine.distribution(axis_key, filters)
    buckets = list(distribution.buckets)
    if engine.is_ordered(axis_key):
        by_value = {str(bucket.value): bucket for bucket in buckets if not bucket.missing}
        ordered = [by_value.pop(str(choice.value), Category(choice.label, 0, (), choice.value))
                   for choice in engine.field(axis_key).choices]
        buckets = [*ordered, *by_value.values(), *(bucket for bucket in buckets if bucket.missing)]
    if distribution.not_applicable:
        absent = tuple(case.uid for case in cases if not engine.is_applicable(case, axis_key))
        buckets.append(Category("해당 없음", len(absent), absent, missing=True))
    if len(buckets) > MAX_CROSS_AXIS_CATEGORIES:
        raise CrossTableLimitError(len(buckets), 2)
    rows = []
    for bucket in buckets:
        members = set(bucket.case_uids)
        rows.append(ButterflyRow(
            bucket.label, tuple(uid for uid in order if uid in members and uid in left_ids),
            tuple(uid for uid in order if uid in members and uid in right_ids), bucket.missing,
            bucket.condition,
        ))
    warnings = []
    small = [f"{name} (n={count:,})" for name, count in ((left_label, len(left_ids)), (right_label, len(right_ids))) if count < 20]
    if small:
        warnings.append(", ".join(small) + "은 사고가 20건 미만입니다. 비율과 실제 건수를 함께 확인해 주세요.")
    overlap = len(left_ids & right_ids)
    if overlap:
        warnings.append(f"다중 선택으로 같은 사고 {overlap:,}건이 좌우 두 그룹에 포함되어 있습니다.")
    if distribution.multi:
        warnings.append("가운데 축은 다중 선택입니다. 한 사고가 여러 구간에 포함되어 비율 합계가 100%를 넘을 수 있습니다.")
    return ButterflyReport(
        distribution.label, engine.field(group_key).label, left_label, right_label,
        len(left_ids), len(right_ids), len(cases), len(cases) - len(left_ids | right_ids),
        overlap, tuple(rows), tuple(warnings),
    )
