"""Generate a disposable live-form example and verification inputs."""

import json
from importlib.resources import files
from pathlib import Path

from investigator.csv_specification import build_specification, specification_rows
from investigator.form_definition import FormDefinition, field_record
from investigator.schema import Choice, FieldKind, FieldSpec


ROOT = Path("C:/samsungfire")
WORK = ROOT / "runtime/workbooks/dynamic-specification"
OUTPUT = ROOT / "outputs/avat-dynamic-specification"
definition = FormDefinition.default()
evidence = next(tab for tab in definition.tabs if tab["key"] == "evidence")
definition.tabs.remove(evidence)
definition.tabs.insert(1, evidence)
driver = next(tab for tab in definition.tabs if tab["key"] == "driver")
driver["fields"].reverse()
driver["fields"].insert(1, field_record(FieldSpec(
    "custom_review_note", "추가 조사 내용", FieldKind.TEXT, "custom_review_note",
)))
driver["fields"].insert(2, field_record(FieldSpec(
    "custom_review_flags", "추가 확인 항목", FieldKind.MULTI, "custom_review_flags",
    (Choice("자료 확인", "documents", "custom_documents_checked"),
     Choice("현장 확인", "scene", "custom_scene_checked")),
)))
definition.validate()
template = files("investigator").joinpath("assets", "AVAT 데이터 명세서.xlsx").read_bytes()
OUTPUT.mkdir(parents=True, exist_ok=True)
destination = OUTPUT / "조사결과_데이터명세서.xlsx"
destination.write_bytes(build_specification(template, definition))
rows = specification_rows(definition)
(WORK / "expected.json").write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
print(json.dumps({"path": str(destination), "columns": len(rows)}, ensure_ascii=False))
