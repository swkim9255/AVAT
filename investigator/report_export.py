"""Fill the retained Word report template without rebuilding its layout.

Only text slots and checkbox states in Word story parts are edited. Styles,
tables, relationships, section geometry, and all other ZIP members are retained.
No Word installation or third-party document package is needed for export.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from datetime import datetime
from importlib.resources import files
from io import BytesIO
from pathlib import Path
from typing import Any
from xml.dom import Node, minidom
from xml.parsers.expat import ExpatError
from zipfile import BadZipFile, ZipFile

from investigator.case_numbering import REPORT_ACCIDENT_NUMBER_COLUMN, build_case_number_map
from investigator.form_definition import FormDefinition
from investigator.models import InvestigationCase
from investigator.schema import (
    ALL_FIELDS,
    FIELD_BY_KEY,
    INJURY_GRADE_RULES,
    incident_day_for_date,
    injury_grade_summary,
    is_blank,
)

REPORT_TEMPLATE_FILENAME = "사고분석보고서.docx"
_W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_W14 = "http://schemas.microsoft.com/office/word/2010/wordml"
_XML = "http://www.w3.org/XML/1998/namespace"
_SLOT = re.compile(r"\{\{\s*([^{}]+?)\s*\}\}")
_STORY = re.compile(r"word/(?:document|header\d+|footer\d+|footnotes|endnotes)\.xml\Z")
_BAD_XML_CHAR = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\ud800-\udfff\ufffe\uffff]")


class ReportExportError(RuntimeError):
    """The report could not be generated safely."""


def _display(value: Any) -> str:
    if is_blank(value) or (isinstance(value, str) and not value.strip()):
        return "-"
    if isinstance(value, list):
        return ", ".join(map(str, value))
    return str(value)


def _context(
    case: InvestigationCase,
    definition: FormDefinition,
    *,
    report_number: str | None = None,
) -> tuple[dict[str, str], dict[str, bool]]:
    """Resolve both stable model keys and old/current CSV aliases.

    Base-template checkboxes follow stable choice IDs even when a user changes
    a choice label, stored value, or individual CSV column in the form editor.
    """
    migrated = definition.migrate_values(case.values)
    canonical = definition.canonical_values(migrated)
    for key, (minimum, maximum, count_key, representative_key) in INJURY_GRADE_RULES.items():
        summary = injury_grade_summary(canonical.get(key), minimum=minimum, maximum=maximum)
        if summary and summary[0]:
            canonical.update(zip((key, count_key, representative_key), summary, strict=True))
    if day := incident_day_for_date(canonical.get("incident_date")):
        canonical["incident_day"] = day
    else:
        canonical.pop("incident_day", None)
    records = {record["key"]: record for record in definition.records()}
    specs = {spec.key: spec for spec in (*ALL_FIELDS, *definition.fields)}
    texts: dict[str, str] = {}
    checks: dict[str, bool] = {}
    stable_texts: dict[str, str] = {}
    stable_checks: dict[str, bool] = {}
    original_option_checks: dict[str, bool] = {}
    for key, spec in specs.items():
        base = FIELD_BY_KEY.get(key)
        record = records.get(key, {})
        value = canonical.get(key)
        group = base.group if base else spec.group
        if (group == "obstacle_detail" and canonical.get("obstacle_exists") == "무") or (
            group == "damage_counts" and canonical.get("incident_damage_unknown") == "확인불가"
        ):
            value = None
        configured = {
            choice.get("id", choice["value"]): choice for choice in record.get("choices", [])
        }
        base_choices = {choice.value: choice for choice in base.choices} if base else {}
        choice_ids = list(dict.fromkeys([*base_choices, *configured]))
        selected = value if isinstance(value, list) else ([] if is_blank(value) else [value])
        aliases = list(dict.fromkeys(filter(None, [
            key, spec.csv_column, base.csv_column if base else None,
            record.get("csv_column"), *record.get("aliases", []),
        ])))
        labels = {}
        for choice_id in choice_ids:
            choice_record = configured.get(choice_id, {})
            original = base_choices.get(choice_id)
            label = choice_record.get("label", original.label if original else choice_id)
            if key == "collision_targets":
                label = label.split(" · ", 1)[-1]
            labels[choice_id] = label
            checked = choice_id in selected
            columns = filter(None, [
                original.csv_column if original else None,
                choice_record.get("csv_column"), *choice_record.get("csv_aliases", []),
            ])
            for column in columns:
                checks[column] = checked
                texts[column] = "1" if checked else "-"
            if original and original.csv_column:
                stable_checks[original.csv_column] = checked
                stable_texts[original.csv_column] = "1" if checked else "-"
            option_names = filter(None, [
                choice_id, label, choice_record.get("value"),
                *(choice_record.get("value_aliases", [])),
                original.label.split(" · ", 1)[-1] if original else None,
            ])
            for name in option_names:
                for alias in aliases:
                    checks[f"{alias}|{name}"] = checked
                for alias in filter(None, [key, base.csv_column if base else None]):
                    stable_checks[f"{alias}|{name}"] = checked
            if original:
                for alias in filter(None, [key, base.csv_column]):
                    for name in [original.value, original.label.split(" · ", 1)[-1]]:
                        original_option_checks[f"{alias}|{name}"] = checked
        display_value = [labels.get(item, item) for item in selected] if isinstance(value, list) else labels.get(value, value)
        text = _display(display_value)
        if key == "accident_classification" and report_number is not None:
            text = report_number
        elif key == "institution" and not is_blank(value):
            text = str(value).lower()
        elif key == "incident_date" and incident_day_for_date(value):
            text = datetime.strptime(str(value), "%Y%m%d").strftime("%Y-%m-%d")
        elif key == "incident_time" and re.fullmatch(r"(?:[01]\d|2[0-3])[0-5]\d", str(value)):
            text = f"{str(value)[:2]}:{str(value)[2:]}"
        for alias in aliases:
            texts[alias] = text
            checks[alias] = not is_blank(value) and str(value).casefold() not in {"0", "false"}
        # CSV columns can legally share another field's *model* key. Never let
        # an unrelated custom alias hijack a retained-template identity slot.
        for alias in filter(None, [key, base.csv_column if base else None]):
            stable_texts[alias] = text
            stable_checks[alias] = not is_blank(value) and str(value).casefold() not in {"0", "false"}
    texts.update(stable_texts)
    checks.update(stable_checks)
    checks.update(original_option_checks)
    texts["incident_hour"] = texts["incident_time"]
    texts["case_id"] = _report_display_id(case, report_number)
    texts[REPORT_ACCIDENT_NUMBER_COLUMN] = _display(report_number)
    return texts, checks


def _elements(node: minidom.Element, namespace: str, name: str) -> list[minidom.Element]:
    return list(node.getElementsByTagNameNS(namespace, name))


def _text(node: minidom.Element) -> str:
    return "".join(child.data for child in node.childNodes if child.nodeType == Node.TEXT_NODE)


def _set_text(node: minidom.Element, text: str) -> None:
    for child in list(node.childNodes):
        node.removeChild(child)
    node.setAttributeNS(_XML, "xml:space", "preserve")
    node.appendChild(node.ownerDocument.createTextNode(text))


def _own_texts(paragraph: minidom.Element) -> list[minidom.Element]:
    result = []
    for node in _elements(paragraph, _W, "t"):
        ancestor = node.parentNode
        while ancestor is not None and not (
            ancestor.namespaceURI == _W and ancestor.localName == "p"
        ):
            ancestor = ancestor.parentNode
        if ancestor is paragraph:
            result.append(node)
    return result


def _keep_remarks_heading(paragraph: minidom.Element) -> None:
    """Keep the source heading with its notes table when long notes flow on."""
    table = paragraph.parentNode
    while table is not None and not (table.namespaceURI == _W and table.localName == "tbl"):
        table = table.parentNode
    if table is None:
        return
    heading = table.previousSibling
    while heading is not None and heading.nodeType != Node.ELEMENT_NODE:
        heading = heading.previousSibling
    if heading is None or heading.namespaceURI != _W or heading.localName != "p":
        return
    if not _own_texts(heading):
        return
    prefix = f"{heading.prefix}:" if heading.prefix else ""
    properties = next((node for node in heading.childNodes if node.namespaceURI == _W and node.localName == "pPr"), None)
    if properties is None:
        properties = heading.ownerDocument.createElementNS(_W, f"{prefix}pPr")
        heading.insertBefore(properties, heading.firstChild)
    if not _elements(properties, _W, "keepNext"):
        keep = heading.ownerDocument.createElementNS(_W, f"{prefix}keepNext")
        properties.insertBefore(keep, properties.firstChild)


def _fill_paragraph(paragraph: minidom.Element, context: dict[str, str]) -> bool:
    nodes = _own_texts(paragraph)
    source = [_text(node) for node in nodes]
    matches = list(_SLOT.finditer("".join(source)))
    if not matches:
        return False
    if any(match.group(1).strip() == "remarks" for match in matches):
        _keep_remarks_heading(paragraph)
    offsets = []
    position = 0
    for text in source:
        offsets.append(position)
        position += len(text)
    for match in reversed(matches):
        key = match.group(1).strip()
        if key not in context:
            raise ReportExportError(f"보고서 양식에 연결되지 않은 변수가 있습니다: {key}")
        value = context[key]
        if _BAD_XML_CHAR.search(value):
            raise ReportExportError(f"보고서에 사용할 수 없는 제어 문자가 있습니다: {key}")
        touched = [i for i, start in enumerate(offsets) if start < match.end() and start + len(source[i]) > match.start()]
        first, last = touched[0], touched[-1]
        start = match.start() - offsets[first]
        end = match.end() - offsets[last]
        if first == last:
            _set_text(nodes[first], _text(nodes[first])[:start] + value + _text(nodes[first])[end:])
        else:
            _set_text(nodes[first], _text(nodes[first])[:start] + value)
            for index in touched[1:-1]:
                _set_text(nodes[index], "")
            _set_text(nodes[last], _text(nodes[last])[end:])
    # Word needs explicit breaks/tabs; raw newlines in a w:t are not sufficient.
    for node in nodes:
        content = _text(node).replace("\r\n", "\n").replace("\r", "\n")
        if "\n" not in content and "\t" not in content:
            continue
        parent = node.parentNode
        for fragment in re.split(r"([\n\t])", content):
            if fragment in {"\n", "\t"}:
                local_name = "br" if fragment == "\n" else "tab"
                name = f"{parent.prefix}:{local_name}" if parent.prefix else local_name
                replacement = node.ownerDocument.createElementNS(_W, name)
            else:
                replacement = node.cloneNode(deep=False)
                _set_text(replacement, fragment)
            parent.insertBefore(replacement, node)
        parent.removeChild(node)
    return True


def _fill_story(data: bytes, context: dict[str, str], states: dict[str, bool]) -> bytes:
    if b"<!DOCTYPE" in data.upper() or b"<!ENTITY" in data.upper():
        raise ReportExportError("외부 엔터티가 포함된 보고서 양식은 사용할 수 없습니다.")
    document = minidom.parseString(data)
    changed = False
    for paragraph in _elements(document.documentElement, _W, "p"):
        changed = _fill_paragraph(paragraph, context) or changed
    for control in _elements(document.documentElement, _W, "sdt"):
        checkboxes = _elements(control, _W14, "checkbox")
        tags = _elements(control, _W, "tag")
        if not checkboxes or not tags:
            continue
        tag = tags[0].getAttributeNS(_W, "val")
        if tag not in states:
            raise ReportExportError(f"보고서 양식에 연결되지 않은 체크박스가 있습니다: {tag}")
        checked = states[tag]
        state = _elements(checkboxes[0], _W14, "checked")
        if not state:
            raise ReportExportError(f"체크박스 상태가 없는 보고서 양식입니다: {tag}")
        attribute = state[0].getAttributeNodeNS(_W14, "val")
        if attribute is not None:
            attribute.value = "1" if checked else "0"
        else:
            prefix = state[0].prefix
            if not prefix:
                prefix = "w14"
                state[0].setAttributeNS("http://www.w3.org/2000/xmlns/", "xmlns:w14", _W14)
            state[0].setAttributeNS(_W14, f"{prefix}:val", "1" if checked else "0")
        # Keep the content control and its typography; update only its cache.
        cached = _elements(control, _W, "t")
        if not cached:
            raise ReportExportError(f"체크박스 표시가 없는 보고서 양식입니다: {tag}")
        _set_text(cached[0], "☑" if checked else "☐")
        for extra in cached[1:]:
            _set_text(extra, "")
        changed = True
    result = document.toxml(encoding="UTF-8") if changed else data
    document.unlink()
    return result


def _template_bytes(template_path: str | Path | None) -> bytes:
    try:
        if template_path is None:
            return files("investigator").joinpath("assets", REPORT_TEMPLATE_FILENAME).read_bytes()
        return Path(template_path).read_bytes()
    except OSError as exc:
        raise ReportExportError(f"보고서 양식을 읽을 수 없습니다: {template_path or REPORT_TEMPLATE_FILENAME}") from exc


def _render_case(
    template: bytes,
    case: InvestigationCase,
    definition: FormDefinition,
    *,
    report_number: str | None = None,
) -> bytes:
    context, states = _context(case, definition, report_number=report_number)
    output = BytesIO()
    try:
        with ZipFile(BytesIO(template)) as source, ZipFile(output, "w") as destination:
            infos = source.infolist()
            if "word/document.xml" not in source.namelist():
                raise ReportExportError("올바른 Word 보고서 양식이 아닙니다.")
            if len(infos) > 2000 or sum(info.file_size for info in infos) > 100 * 1024 * 1024:
                raise ReportExportError("보고서 양식의 크기가 너무 큽니다.")
            if len({info.filename for info in infos}) != len(infos):
                raise ReportExportError("중복된 구성 요소가 있는 보고서 양식입니다.")
            destination.comment = source.comment
            for info in infos:
                data = source.read(info)
                if _STORY.fullmatch(info.filename):
                    data = _fill_story(data, context, states)
                destination.writestr(info, data)
    except ReportExportError:
        raise
    except (BadZipFile, KeyError, ValueError, ExpatError) as exc:
        raise ReportExportError(f"보고서 양식을 처리할 수 없습니다: {exc}") from exc
    return output.getvalue()


def _report_display_id(case: InvestigationCase, report_number: str | None) -> str:
    if report_number is None:
        return case.display_id
    institution = str(case.values.get("institution", "")).strip().upper()
    return f"{institution}-{report_number}" if institution else report_number


def _safe_name(case: InvestigationCase, report_number: str | None = None) -> str:
    display_id = _report_display_id(case, report_number)
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", display_id).strip(" .")
    return f"{name[:100] or case.uid[:12]}_사고분석보고서"


def export_case_reports(
    cases: Sequence[InvestigationCase],
    destination_directory: str | Path,
    *,
    form_definition: FormDefinition | None = None,
    template_path: str | Path | None = None,
    report_number_by_uid: Mapping[str, str] | None = None,
) -> list[Path]:
    """Export one DOCX per selected case, without overwriting existing files.

    Report snapshots do not save or complete an investigation. A failed batch
    removes only its own newly created files, leaving previous exports intact.
    Pass the whole project's number map when exporting a subset so the report
    identifier agrees with CSV exports and is not renumbered by the selection.
    """
    selected = list(dict((case.uid, case) for case in cases).values())
    if not selected:
        raise ReportExportError("보고서로 내보낼 사고를 선택해 주세요.")
    numbers = (
        build_case_number_map(selected)
        if report_number_by_uid is None
        else report_number_by_uid
    )
    template = _template_bytes(template_path)
    destination = Path(destination_directory).expanduser().resolve()
    created: list[Path] = []
    try:
        destination.mkdir(parents=True, exist_ok=True)
        for case in selected:
            definition = form_definition or case.form_definition or FormDefinition.default()
            report_number = numbers.get(case.uid)
            data = _render_case(template, case, definition, report_number=report_number)
            stem = _safe_name(case, report_number)
            suffix = 1
            while True:
                path = destination / f"{stem}{'' if suffix == 1 else f'_{suffix}'}.docx"
                try:
                    stream = path.open("xb")
                    break
                except FileExistsError:
                    suffix += 1
            created.append(path)
            with stream:
                stream.write(data)
        return created
    except Exception as exc:
        cleanup_failed = []
        for path in created:
            try:
                path.unlink()
            except OSError:
                cleanup_failed.append(str(path))
        detail = f"\n정리하지 못한 파일: {', '.join(cleanup_failed)}" if cleanup_failed else ""
        if isinstance(exc, ReportExportError) and not detail:
            raise
        raise ReportExportError(f"보고서를 내보낼 수 없습니다: {exc}{detail}") from exc


__all__ = ["REPORT_TEMPLATE_FILENAME", "ReportExportError", "export_case_reports"]
