"""Shared readable condition labels for statistics and report selection."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from investigator.statistics import AnyOf

if TYPE_CHECKING:
    from investigator.statistics import FilterItem, StatisticsEngine


def clean_field_label(label: str) -> str:
    """Hide implementation suffixes without removing meaningful units."""
    return re.sub(r"\s*\(\s*index\s*\)", "", label, flags=re.IGNORECASE).strip()


def condition_text(engine: StatisticsEngine, condition: FilterItem) -> str:
    if isinstance(condition, AnyOf):
        return "(" + " OR ".join(condition_text(engine, child)
                                   for child in condition.conditions) + ")"
    try:
        spec = engine.field(condition.field_key)
    except KeyError:
        return condition.field_key
    label = clean_field_label(spec.label)
    operator = condition.operator
    states = {
        "missing": "(미입력)", "not_missing": "입력됨", "not_applicable": "해당 없음",
        "invalid": "형식 오류",
    }
    if operator in states:
        return f"{label}: {states[operator]}"
    if operator in {"range", "between"}:
        low = f"{condition.lower:g} 이상" if condition.lower is not None else ""
        high = (f"{condition.upper:g} {'이하' if operator == 'between' else '미만'}"
                if condition.upper is not None else "")
        text = ' ~ '.join(filter(None, (low, high))) or '모든 숫자'
        return f"{label}: {text}" + (" 제외" if condition.exclude else "")

    def value_label(value) -> str:
        if value is None:
            return "(미입력)"
        return next((choice.label for choice in spec.choices if choice.value == value), str(value))

    if operator == "values":
        if condition.values == (None,) and condition.exclude:
            return f"{label}: 입력됨"
        text = ", ".join(value_label(value) for value in condition.values) or "선택 없음"
        return f"{label}: {text}" + (" 제외" if condition.exclude else "")
    value = value_label(condition.value)
    if operator in {"eq", "ne"}:
        return f"{label}: {value}" + (" 제외" if operator == "ne" else "")
    symbol = {"eq": "=", "ne": "≠", "gt": ">", "ge": "≥", "lt": "<", "le": "≤"}.get(operator, "=")
    return f"{label} {symbol} {value}"
