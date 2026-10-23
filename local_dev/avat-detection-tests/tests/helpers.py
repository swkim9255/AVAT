from __future__ import annotations

from datetime import datetime

from investigator.schema import ALL_FIELDS, FieldKind


def valid_values() -> dict[str, object]:
    values: dict[str, object] = {}
    for field in ALL_FIELDS:
        if not field.required:
            continue
        if field.kind in {FieldKind.SINGLE, FieldKind.MULTI}:
            choice = field.choices[0].value
            values[field.key] = [choice] if field.kind is FieldKind.MULTI else choice
        elif field.key == "vehicle_model_year":
            values[field.key] = "2023"
        elif field.key == "incident_time":
            values[field.key] = "0930"
        elif field.key == "incident_date":
            values[field.key] = "20260828"
        elif field.kind is FieldKind.DECIMAL:
            values[field.key] = "10"
        elif field.kind is FieldKind.DIGITS:
            values[field.key] = "1"
        else:
            values[field.key] = "테스트"
    values["institution"] = "samsung"
    values["accident_classification"] = "001"
    values["vehicle_manufacturer"] = "kia"
    values["vehicle_model_name"] = "테스트 차량"
    values["obstacle_exists"] = "무"
    values["incident_damage_unknown"] = "확인불가"
    parsed = datetime.strptime(str(values["incident_date"]), "%Y%m%d")
    values["incident_day"] = ("월", "화", "수", "목", "금", "토", "일")[parsed.weekday()]
    return values
