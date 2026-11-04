"""Project-wide numeric ordering and derived report IDs; originals stay intact."""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Iterable
from datetime import date
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from investigator.models import InvestigationCase

REPORT_ACCIDENT_NUMBER_COLUMN = "report_accident_number"
_DECIMAL = re.compile(r"[0-9]+\Z")
_ACCIDENT_NUMBER = re.compile(r"[0-9]{13}\Z")


def original_case_number(case: InvestigationCase) -> str:
    value = case.values.get("accident_classification")
    return "" if value is None else str(value).strip()


def case_number_sort_key(case: InvestigationCase) -> tuple[int, int, str, str, str]:
    """Numeric IDs first; blanks/non-numeric IDs last with deterministic ties."""
    number = original_case_number(case)
    numeric = _DECIMAL.fullmatch(number) is not None
    return (
        0 if numeric else 1,
        int(number) if numeric else 0,
        number.casefold(),
        str(case.values.get("institution") or "").casefold(),
        case.uid,
    )


def build_case_number_map(cases: Iterable[InvestigationCase]) -> dict[str, str]:
    """Number valid YYYYMMDDxxxxx IDs by original numeric order within each day.

    Use the full project, not a report selection, so the report and CSV agree.
    Invalid/manual IDs have no derived number (CSV null; reports keep original).
    Adding/removing cases can recompute these derived ordinals. No model changes.
    """
    sequence: dict[str, int] = defaultdict(int)
    result = {}
    for case in sorted(cases, key=case_number_sort_key):
        number = original_case_number(case)
        if _ACCIDENT_NUMBER.fullmatch(number) is None:
            continue
        day = number[:8]
        try:
            date(int(day[:4]), int(day[4:6]), int(day[6:8]))
        except ValueError:
            continue
        sequence[day] += 1
        result[case.uid] = f"{day}_{sequence[day]:03d}"
    return result
