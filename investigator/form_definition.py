"""Workspace-local form definitions with stable storage keys and CSV aliases."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field, replace
from typing import Any
from uuid import uuid4

from investigator.case_numbering import REPORT_ACCIDENT_NUMBER_COLUMN
from investigator.schema import (
    ALL_FIELDS,
    FIELD_BY_KEY,
    IDENTITY_FIELDS,
    INJURY_GRADE_RULES,
    SECTIONS,
    Choice,
    FieldKind,
    FieldSpec,
    SectionSpec,
    is_blank,
    missing_questions,
    validation_errors,
    values_to_csv,
)

METADATA_COLUMNS = {
    REPORT_ACCIDENT_NUMBER_COLUMN,
    "case_id",
    "workflow_status",
    "video_files",
    "active_video_file",
    "trigger_frame",
    "collision_frames",
    "video_markers",
}
# These controls participate in automatic calculations or conditional questions.
RULE_KEYS = {
    "institution",
    "accident_classification",
    "driver_age",
    "incident_date",
    "incident_day",
    "incident_time",
    "obstacle_exists",
    "incident_damage_unknown",
    "obstacle_state",
    "obstacle_vehicle_state",
    "collision_dist_value",
    "collision_dist_range",
    "obstacle_dist_value",
    "obstacle_dist_range",
    *INJURY_GRADE_RULES,
    *(key for rule in INJURY_GRADE_RULES.values() for key in rule[2:]),
    *(field.key for field in ALL_FIELDS if field.group == "damage_counts"),
}


def type_locked(key: str) -> bool:
    base = FIELD_BY_KEY.get(key)
    return key in RULE_KEYS or bool(base and any(c.csv_column for c in base.choices))


def required_locked(key: str) -> bool:
    base = FIELD_BY_KEY.get(key)
    return (
        bool(base and base.group in {"obstacle_detail", "damage_counts"})
        or key in {f.key for f in IDENTITY_FIELDS}
        or key
        in {
            "site_types",
            "site_special_areas",
            "site_manual_input",
            "obstacle_state",
            "obstacle_vehicle_state",
            "obstacle_dist_source",
            "obstacle_dist_range",
            *(rule[2] for rule in INJURY_GRADE_RULES.values()),
            "incident_fatality",
            "incident_property_damage",
        }
    )


def field_record(spec: FieldSpec) -> dict[str, Any]:
    return {
        "key": spec.key,
        "label": spec.label,
        "kind": spec.kind.value,
        "csv_column": spec.csv_column or "",
        "aliases": [],
        "required": spec.required,
        "editable": spec.editable,
        "default": None,
        "full_width": False,
        "choices": [
            {
                "id": c.value,
                "label": c.label,
                "value": c.value,
                "csv_column": c.csv_column or "",
                "value_aliases": [],
                "csv_aliases": [],
            }
            for c in spec.choices
        ],
    }


@dataclass
class FormDefinition:
    tabs: list[dict[str, Any]]
    # Read legacy workspaces without letting their historical export order
    # override the live form. to_dict() writes the current order for old readers.
    csv_column_order: list[str] = field(default_factory=list, compare=False)

    @classmethod
    def default(cls) -> FormDefinition:
        sections = (SectionSpec("identity", "식별 구분", "식별", IDENTITY_FIELDS), *SECTIONS)
        result = cls(
            [
                {
                    "key": s.key,
                    "title": s.title,
                    "short_title": s.short_title,
                    "fields": [field_record(f) for f in s.fields],
                }
                for s in sections
            ]
        )
        result._initialize_layout()
        return result

    def _initialize_layout(self) -> None:
        """Upgrade the formerly hard-coded screen layout, once, for older forms."""
        collision_order = [
            "collision_targets",
            "incident_damage_unknown",
            "incident_minor_injury_grades",
            "incident_severe_injury_grades",
            "incident_minor_representative_injury_grade",
            "incident_severe_representative_injury_grade",
            "incident_minor_injury",
            "incident_severe_injury",
            "incident_fatality",
            "incident_property_damage",
        ]
        for tab in self.tabs:
            tab["columns"] = 2 if tab["key"] in {"identity", "trigger", "collision_objects"} else 1
            for record in tab["fields"]:
                record["full_width"] = record["key"] in {
                    "collision_targets",
                    "incident_damage_unknown",
                }
            if tab["key"] == "environment":
                fields = tab["fields"]
                keys = [f["key"] for f in fields]
                if "incident_day" in keys and "incident_date" in keys:
                    day, date = keys.index("incident_day"), keys.index("incident_date")
                    fields[day], fields[date] = fields[date], fields[day]
            elif tab["key"] == "collision_objects":
                by_key = {f["key"]: f for f in tab["fields"]}
                tab["fields"] = [by_key[key] for key in collision_order if key in by_key] + [
                    f for f in tab["fields"] if f["key"] not in collision_order
                ]

    @classmethod
    def from_dict(cls, raw: Any) -> FormDefinition:
        if raw is None:
            return cls.default()
        if (
            not isinstance(raw, dict)
            or raw.get("version") not in {1, 2, 3, 4, 5}
            or not isinstance(raw.get("tabs"), list)
        ):
            raise ValueError("지원하지 않는 조사 양식 형식입니다.")
        result = cls(deepcopy(raw["tabs"]), deepcopy(raw.get("csv_column_order", [])))
        result._upgrade_choices(initialize=raw["version"] < 4)
        if raw["version"] < 5:
            result._restore_automatic_adult_expansion()
        if raw["version"] == 1:
            result._initialize_layout()
        elif raw["version"] == 2:
            for tab in result.tabs:
                if tab.get("key") == "identity":
                    tab["columns"] = 2
        result.validate()
        return result

    def to_dict(self) -> dict[str, Any]:
        return {"version": 5, "tabs": deepcopy(self.tabs), "csv_column_order": self.csv_headers()}

    def _restore_automatic_adult_expansion(self) -> None:
        """Repair the previous automatic defaults in already saved workspaces once.

        Version 5 keeps deliberate edits, including the same wording, unchanged.
        A prior explicit individual-column edit has history and is preserved too.
        """
        renamed_columns: dict[str, str] = {}
        originals = {
            "obstacle_pedestrians": ("어른", "어른", "obstacle_adult"),
            "collision_targets": ("보행자 · 어른", "pedestrian.adult", ""),
        }
        for record in self.records():
            original = originals.get(record["key"])
            if original is None:
                continue
            label, value, column = original
            for choice in record["choices"]:
                if (
                    choice.get("id", choice["value"]) != value
                    or choice["value"] != "어른, 청소년"
                    or choice["label"] != label.replace("어른", "어른, 청소년")
                    or choice.get("csv_column", "") != ("obstacle_adult,youth" if column else "")
                    or choice.get("csv_aliases")
                ):
                    continue
                old_value, old_column = choice["value"], choice.get("csv_column", "")
                choice.update(label=label, value=value, csv_column=column)
                choice["value_aliases"] = list(dict.fromkeys([
                    *(a for a in choice.get("value_aliases", []) if a != value), old_value,
                ]))
                if old_column:
                    renamed_columns[old_column] = column
            if record.get("default") is not None:
                record["default"] = self._migrate_value(record, record["default"])
        self.csv_column_order = list(dict.fromkeys(
            renamed_columns.get(column, column) for column in self.csv_column_order
        ))

    def _upgrade_choices(self, *, initialize: bool = True) -> None:
        """Add stable editing identities without renaming existing choices or values."""
        for record in self.records():
            # Accept files saved during the earlier category expansion. Restore
            # only their internal editing identity; keep the saved configuration.
            original_id = {
                "obstacle_pedestrians": "어른",
                "collision_targets": "pedestrian.adult",
            }.get(record["key"])
            identities = {c.get("id", c["value"]) for c in record.get("choices", [])}
            for choice in record.get("choices", []):
                if initialize:
                    choice.setdefault("id", choice["value"])
                    choice.setdefault("value_aliases", [])
                    choice.setdefault("csv_aliases", [])
                if (
                    original_id and original_id not in identities
                    and choice.get("id", choice["value"]) == "어른, 청소년"
                ):
                    choice["id"] = original_id

    def restored(self, initial: FormDefinition) -> FormDefinition:
        """Restore saved initial settings while keeping added fields and CSV links."""
        result = FormDefinition.from_dict(initial.to_dict())
        initial_tabs = {tab["key"]: tab for tab in result.tabs}
        for current_tab in self.tabs:
            target_tab = initial_tabs.get(current_tab["key"])
            if target_tab is None:
                result.tabs.append(deepcopy(current_tab))
                continue
            initial_fields = {f["key"]: f for f in target_tab["fields"]}
            for current in current_tab["fields"]:
                target = initial_fields.get(current["key"])
                if target is None:
                    target_tab["fields"].append(deepcopy(current))
                    continue
                known_choices = {c.get("id", c["value"]) for c in target["choices"]}
                target["choices"].extend(
                    deepcopy(c) for c in current["choices"]
                    if c.get("id", c["value"]) not in known_choices
                )
                targets = {c.get("id", c["value"]): c for c in target["choices"]}
                for choice in current["choices"]:
                    restored = targets[choice.get("id", choice["value"])]
                    for column in [choice.get("csv_column"), *choice.get("csv_aliases", [])]:
                        if column and column != restored.get("csv_column"):
                            aliases = restored.setdefault("csv_aliases", [])
                            if column not in aliases:
                                aliases.append(column)
                columns = {target["csv_column"], *(c.get("csv_column") for c in target["choices"])}
                for column in [current["csv_column"], *current.get("aliases", [])]:
                    if column and column not in columns and column not in target["aliases"]:
                        target["aliases"].append(column)
        result.validate()
        return result

    def records(self) -> list[dict[str, Any]]:
        return [f for tab in self.tabs for f in tab["fields"]]

    def specs(self) -> tuple[SectionSpec, ...]:
        result = []
        for tab in self.tabs:
            fields = []
            for record in tab["fields"]:
                base = FIELD_BY_KEY.get(record["key"])
                kind = FieldKind(record["kind"])
                spec = base or FieldSpec(record["key"], record["label"], kind)
                fields.append(
                    replace(
                        spec,
                        label=record["label"],
                        kind=kind,
                        csv_column=record.get("csv_column") or None,
                        choices=tuple(
                            Choice(c["label"], c["value"], c.get("csv_column") or None)
                            for c in record["choices"]
                        ),
                        required=record["required"],
                        editable=record["editable"],
                        max_length=spec.max_length if kind == spec.kind else None,
                        exact_length=spec.exact_length if kind == spec.kind else None,
                    )
                )
            result.append(SectionSpec(tab["key"], tab["title"], tab["short_title"], tuple(fields)))
        return tuple(result)

    @property
    def fields(self) -> tuple[FieldSpec, ...]:
        return tuple(f for s in self.specs() for f in s.fields)

    def validate(self) -> None:
        if not isinstance(self.csv_column_order, list) or any(
            not isinstance(c, str) for c in self.csv_column_order
        ):
            raise ValueError("CSV 칼럼 순서가 올바르지 않습니다.")
        if not self.tabs or self.tabs[0].get("key") != "identity":
            raise ValueError("식별 구분 영역이 필요합니다.")
        tab_keys: set[str] = set()
        field_keys: set[str] = set()
        owners = {column: "__metadata__" for column in METADATA_COLUMNS}
        for spec in ALL_FIELDS:
            for column in [spec.csv_column, *(c.csv_column for c in spec.choices)]:
                if column:
                    owners[column] = spec.key
        for tab in self.tabs:
            if not isinstance(tab, dict) or not isinstance(tab.get("fields"), list):
                raise ValueError("탭 구성이 올바르지 않습니다.")
            key = tab.get("key")
            if not isinstance(key, str) or not key or key in tab_keys:
                raise ValueError("탭 식별자가 중복되거나 비어 있습니다.")
            tab_keys.add(key)
            if type(tab.get("columns", 1)) is not int or tab.get("columns", 1) not in {1, 2}:
                raise ValueError("탭 배치는 1열 또는 2열이어야 합니다.")
            expected_keys = next(
                (
                    s.fields
                    for s in (SectionSpec("identity", "", "", IDENTITY_FIELDS), *SECTIONS)
                    if s.key == key
                ),
                (),
            )
            if any(
                f.key not in {r.get("key") for r in tab["fields"] if isinstance(r, dict)}
                for f in expected_keys
            ):
                raise ValueError("기존 항목은 원래 탭에서 유지해야 합니다.")
            for name in ("title", "short_title"):
                if not isinstance(tab.get(name), str) or not tab[name].strip():
                    raise ValueError("탭 이름을 입력해 주세요.")
            for record in tab["fields"]:
                if not isinstance(record, dict):
                    raise ValueError("항목 구성이 올바르지 않습니다.")
                key = record.get("key")
                if not isinstance(key, str) or not key or key in field_keys:
                    raise ValueError("항목 식별자가 중복되거나 비어 있습니다.")
                field_keys.add(key)
                if not isinstance(record.get("full_width", False), bool):
                    raise ValueError("항목 너비 설정이 올바르지 않습니다.")
                if not isinstance(record.get("label"), str) or not record["label"].strip():
                    raise ValueError("입력 항목 이름을 입력해 주세요.")
                kind = FieldKind(record.get("kind"))
                base = FIELD_BY_KEY.get(key)
                if base and type_locked(key) and kind != base.kind:
                    raise ValueError(
                        f"{record['label']}: 자동 계산·기존 CSV 연결 항목의 입력 방식은 유지해야 합니다."
                    )
                if any(not isinstance(record.get(flag), bool) for flag in ("required", "editable")):
                    raise ValueError("필수 여부와 직접 입력 설정이 올바르지 않습니다.")
                if base and required_locked(key) and record["required"] != base.required:
                    raise ValueError("식별·조건부 항목의 필수 입력 규칙은 유지해야 합니다.")
                choices = record.get("choices")
                if not isinstance(choices, list):
                    raise ValueError("선택지 목록이 올바르지 않습니다.")
                values: set[str] = set()
                choice_ids: set[str] = set()
                historical_values: set[str] = set()
                choice_columns: list[str] = []
                for choice in choices:
                    if not isinstance(choice, dict) or any(
                        not isinstance(choice.get(k), str) or not choice[k].strip()
                        for k in ("label", "value")
                    ):
                        raise ValueError("선택지 이름과 저장값을 입력해 주세요.")
                    if choice["value"] in values:
                        raise ValueError("선택지 저장값이 중복됩니다.")
                    values.add(choice["value"])
                    identity = choice.get("id", choice["value"])
                    if not isinstance(identity, str) or not identity or identity in choice_ids:
                        raise ValueError("선택지 식별자가 중복되거나 비어 있습니다.")
                    choice_ids.add(identity)
                    for alias_key in ("value_aliases", "csv_aliases"):
                        aliases = choice.get(alias_key, [])
                        if not isinstance(aliases, list) or any(
                            not isinstance(alias, str) or not alias.strip() for alias in aliases
                        ):
                            raise ValueError("선택지의 이전 저장값·CSV 연결이 올바르지 않습니다.")
                    linked_values = {choice["value"], *choice.get("value_aliases", [])}
                    if historical_values & linked_values:
                        raise ValueError("선택지의 저장값이 다른 선택지의 이전 저장값과 겹칩니다.")
                    historical_values.update(linked_values)
                    if choice.get("csv_column"):
                        choice_columns.append(choice["csv_column"])
                if kind in {FieldKind.SINGLE, FieldKind.MULTI} and not choices:
                    raise ValueError(f"{record['label']}: 선택지를 추가해 주세요.")
                if base:
                    for c in base.choices:
                        if c.value not in choice_ids:
                            raise ValueError(
                                f"{record['label']}: 기존 선택지는 유지해야 합니다."
                            )
                    if type_locked(key) and record["editable"] != base.editable:
                        raise ValueError("자동 계산 항목의 직접 입력 설정은 유지해야 합니다.")
                    if any(c.csv_column for c in base.choices) and len(choice_columns) != len(
                        choices
                    ):
                        raise ValueError(
                            f"{record['label']}: 새 선택지에도 개별 CSV 칼럼명을 입력해 주세요."
                        )
                column = record.get("csv_column")
                aliases = record.get("aliases", [])
                if not isinstance(column, str) or not isinstance(aliases, list):
                    raise ValueError("CSV 칼럼 설정이 올바르지 않습니다.")
                if not column and not choice_columns:
                    raise ValueError(f"{record['label']}: CSV 칼럼명을 입력해 주세요.")
                used = [
                    *([column] if column else []), *choice_columns, *aliases,
                    *(a for c in choices for a in c.get("csv_aliases", [])),
                ]
                if len(used) != len(set(used)):
                    raise ValueError(f"{record['label']}: CSV 칼럼명이 중복됩니다.")
                for column in used:
                    if (
                        not isinstance(column, str)
                        or not column.strip()
                        or any(c in column for c in "\r\n\t")
                        or column[0] in "=+-@"
                    ):
                        raise ValueError("CSV 칼럼명이 올바르지 않습니다.")
                    if column in owners and owners[column] != key:
                        raise ValueError(f"이미 사용 중인 CSV 칼럼명입니다: {column}")
                    owners[column] = key
        if not set(FIELD_BY_KEY).issubset(field_keys):
            raise ValueError("기존 조사 항목은 보존해야 합니다.")
        if not {"identity", *(s.key for s in SECTIONS)}.issubset(tab_keys):
            raise ValueError("기존 조사 탭은 보존해야 합니다.")
        defaults = {
            f["key"]: f.get("default") for f in self.records() if not is_blank(f.get("default"))
        }
        errors = self.errors(defaults)
        if errors:
            labels = {f.key: f.label for f in self.fields}
            raise ValueError(
                "기본값 형식이 잘못되었습니다: " + ", ".join(labels.get(k, k) for k in errors)
            )

    def apply_defaults(self, values: dict[str, Any]) -> None:
        for f in self.records():
            if f["key"] not in values and not is_blank(f.get("default")):
                values[f["key"]] = deepcopy(f["default"])

    def missing(self, values: dict[str, Any]) -> list[str]:
        return missing_questions(self.canonical_values(values), self._canonical_fields())

    def errors(self, values: dict[str, Any]) -> dict[str, str]:
        return validation_errors(self.canonical_values(values), self._canonical_fields())

    def _canonical_fields(self) -> tuple[FieldSpec, ...]:
        return tuple(
            replace(spec, choices=tuple(
                replace(choice, value=record_choice.get("id", record_choice["value"]))
                for choice, record_choice in zip(spec.choices, record["choices"], strict=True)
            ))
            for spec, record in zip(self.fields, self.records(), strict=True)
        )

    def canonical_values(self, values: dict[str, Any]) -> dict[str, Any]:
        result = dict(values)
        for record in self.records():
            mapping = {c["value"]: c.get("id", c["value"]) for c in record["choices"]}
            value = result.get(record["key"])
            if isinstance(value, list):
                result[record["key"]] = [mapping.get(item, item) for item in value]
            elif isinstance(value, str):
                result[record["key"]] = mapping.get(value, value)
        return result

    def stored_value(self, key: str, canonical_value: Any) -> Any:
        record = next(record for record in self.records() if record["key"] == key)
        return next((c["value"] for c in record["choices"]
                     if c.get("id", c["value"]) == canonical_value), canonical_value)

    def stored_values(self, values: dict[str, Any]) -> dict[str, Any]:
        result = dict(values)
        for record in self.records():
            key = record["key"]
            value = result.get(key)
            if isinstance(value, list):
                result[key] = [self.stored_value(key, item) for item in value]
            elif isinstance(value, str):
                result[key] = self.stored_value(key, value)
        return result

    @staticmethod
    def _migrate_value(record: dict[str, Any], value: Any) -> Any:
        mapping = {c.get("id", c["value"]): c["value"] for c in record["choices"]}
        mapping.update({
            alias: c["value"]
            for c in record["choices"]
            for alias in c.get("value_aliases", [])
        })
        mapping.update({c["value"]: c["value"] for c in record["choices"]})
        if isinstance(value, list):
            return list(dict.fromkeys(mapping.get(item, item) for item in value))
        return mapping.get(value, value) if isinstance(value, str) else value

    def migrate_values(
        self, values: dict[str, Any], previous: FormDefinition | None = None
    ) -> dict[str, Any]:
        """Migrate on a copy so cancelling or a failed save cannot change live cases."""
        result = deepcopy(values)
        previous_records = {r["key"]: r for r in previous.records()} if previous else {}
        for record in self.records():
            key = record["key"]
            if key not in result:
                continue
            old = previous_records.get(key)
            if old:
                current = {c.get("id", c["value"]): c["value"] for c in record["choices"]}
                mapping = {c["value"]: current.get(c.get("id", c["value"]), c["value"])
                           for c in old["choices"]}
                value = result[key]
                result[key] = (
                    list(dict.fromkeys(mapping.get(item, item) for item in value))
                    if isinstance(value, list) else mapping.get(value, value)
                )
            else:
                result[key] = self._migrate_value(record, result[key])
        return result

    def label(self, key: str) -> str:
        from investigator.schema import field_label

        return next((f.label for f in self.fields if f.key == key), field_label(key))

    def csv_headers(self) -> list[str]:
        """Export only live names, in the current tab/field/choice order.

        Historical aliases and csv_column_order remain readable for old saved
        forms, but must not resurrect deleted/renamed columns or override the
        editor's order. Stable field/choice identities still migrate old values.
        The date and weekday have no special ordering exception.
        """
        return list(dict.fromkeys(
            column
            for record in self.records()
            for column in [
                record.get("csv_column"),
                *(choice.get("csv_column") for choice in record["choices"]),
            ]
            if column
        ))

    def csv_row(self, values: dict[str, Any]) -> dict[str, str]:
        values = self.migrate_values(values)
        canonical = self.canonical_values(values)
        row = values_to_csv(canonical)
        for record, spec in zip(self.records(), self.fields, strict=True):
            value = values.get(spec.key)
            if (spec.group == "obstacle_detail" and canonical.get("obstacle_exists") == "무") or (
                spec.group == "damage_counts"
                and canonical.get("incident_damage_unknown") == "확인불가"
            ):
                value = None
            text = (
                ""
                if is_blank(value)
                else ("|".join(map(str, value)) if isinstance(value, list) else str(value))
            )
            base = FIELD_BY_KEY.get(spec.key)
            if base and spec.key == "incident_damage_unknown":
                text = row[base.csv_column]
            elif base and isinstance(value, list):
                configured = {c["value"]: c for c in record["choices"]}
                output_values = []
                for item in value:
                    choice = configured.get(item)
                    if choice is None:
                        output_values.append(str(item))
                    elif choice["value"] != choice.get("id", choice["value"]):
                        output_values.append(choice["value"])
                    else:
                        label = choice["label"]
                        output_values.append(
                            label.split(" · ", 1)[-1] if spec.key == "collision_targets" else label
                        )
                text = "|".join(output_values)
            elif base and base.csv_column and spec.key in INJURY_GRADE_RULES:
                text = row[base.csv_column]
            for column in [record["csv_column"], *record.get("aliases", []),
                           *([base.csv_column] if base and base.csv_column else [])]:
                if column:
                    row[column] = text
            selected = value if isinstance(value, list) else [value]
            base_choices = {c.value: c for c in base.choices} if base else {}
            for choice in record["choices"]:
                original = base_choices.get(choice.get("id", choice["value"]))
                columns = [choice.get("csv_column"), *choice.get("csv_aliases", []),
                           original.csv_column if original else None]
                for column in columns:
                    if column:
                        row[column] = "1" if choice["value"] in selected else ""
        return {column: row.get(column, "") for column in self.csv_headers()}


def new_field() -> dict[str, Any]:
    key = "custom_" + uuid4().hex
    return field_record(FieldSpec(key, "새 항목", FieldKind.TEXT, key))
