"""Read-only, accident-level cohorts for a mirrored distribution comparison."""

from __future__ import annotations

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


def butterfly_groups(engine: StatisticsEngine, key: str, filters: StatsFilter) -> tuple[Category, ...]:
    if engine.is_numeric(key):
        raise ValueError("좌우로 나눌 항목은 범주 항목을 선택해 주세요.")
    cases = engine.filtered_cases(filters)
    eligible = {
        case.uid for case in cases
        if engine.is_applicable(case, key) and key not in engine._validation(case)[1]
    }
    buckets = engine.distribution(key, filters).buckets
    groups = {
        str(bucket.value): Category(
            bucket.label, len(uids), uids, bucket.value, condition=bucket.condition,
        )
        for bucket in buckets if not bucket.missing
        if (uids := tuple(uid for uid in bucket.case_uids if uid in eligible))
    }
    # Keep configured empty groups available, e.g. women n=0 in a male-only slice.
    result = []
    for choice in engine.field(key).choices:
        result.append(groups.pop(str(choice.value), Category(
            choice.label, 0, (), choice.value, condition=StatsCondition(key, choice.value),
        )))
    result.extend(groups.values())
    if len(result) > MAX_CROSS_AXIS_CATEGORIES:
        raise CrossTableLimitError(1, len(result))
    return tuple(result)


def build_butterfly(
    engine: StatisticsEngine,
    axis_key: str,
    group_key: str,
    filters: StatsFilter,
    *,
    left_value: Any,
    right_value: Any = None,
    mode: str = "two",
) -> ButterflyReport:
    if axis_key == group_key:
        raise ValueError("서로 다른 가운데 축과 좌우 그룹 항목을 선택해 주세요.")
    if mode not in {"two", "rest"}:
        raise ValueError("두 그룹 또는 한 그룹 vs 나머지를 선택해 주세요.")
    options = butterfly_groups(engine, group_key, filters)
    left = next((group for group in options if group.value == left_value), None)
    right = next((group for group in options if group.value == right_value), None)
    if left is None or mode == "two" and (right is None or right.value == left.value):
        raise ValueError("서로 다른 좌우 그룹을 선택해 주세요.")
    cases = engine.filtered_cases(filters)
    order = [case.uid for case in cases]
    eligible = {uid for group in options for uid in group.case_uids}
    left_ids = set(left.case_uids)
    # This is a complement of accident membership, not a union of other labels.
    right_ids = eligible - left_ids if mode == "rest" else set(right.case_uids)
    right_label = f"{left.label} 외 나머지" if mode == "rest" else right.label
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
        ))
    warnings = []
    small = [f"{name} (n={count:,})" for name, count in ((left.label, len(left_ids)), (right_label, len(right_ids))) if count < 20]
    if small:
        warnings.append(", ".join(small) + "은 사고가 20건 미만입니다. 비율과 실제 건수를 함께 확인해 주세요.")
    overlap = len(left_ids & right_ids)
    if overlap:
        warnings.append(f"다중 선택으로 같은 사고 {overlap:,}건이 좌우 두 그룹에 포함되어 있습니다.")
    if distribution.multi:
        warnings.append("가운데 축은 다중 선택입니다. 한 사고가 여러 구간에 포함되어 비율 합계가 100%를 넘을 수 있습니다.")
    return ButterflyReport(
        distribution.label, engine.field(group_key).label, left.label, right_label,
        len(left_ids), len(right_ids), len(cases), len(cases) - len(left_ids | right_ids),
        overlap, tuple(rows), tuple(warnings),
    )
