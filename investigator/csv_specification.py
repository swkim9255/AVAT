"""Build the CSV dictionary from the live form, retaining the bundled XLSX style.

Only the dictionary's first sheet data and its used range change. No Excel,
Node.js or extra spreadsheet runtime is needed in the distributed application.
"""

from __future__ import annotations

import math
import re
import unicodedata
from collections.abc import Sequence
from io import BytesIO
from typing import TYPE_CHECKING
from xml.etree import ElementTree as ET
from xml.sax.saxutils import escape, quoteattr
from zipfile import BadZipFile, ZipFile

from investigator.case_numbering import REPORT_ACCIDENT_NUMBER_COLUMN

if TYPE_CHECKING:
    from investigator.form_definition import FormDefinition

METADATA_HEADERS = (
    "case_id", "workflow_status", "video_files", "active_video_file",
    "trigger_frame", "collision_frames", "video_markers",
)
_METADATA_LABELS = {
    "case_id": "사고 식별자",
    "workflow_status": "조사 상태",
    "video_files": "전체 영상 경로",
    "active_video_file": "현재 선택 영상 경로",
    "trigger_frame": "현재 선택 영상 트리거 프레임(0부터)",
    "collision_frames": "현재 선택 영상 충돌 프레임(0부터)",
    "video_markers": "전체 영상의 트리거·충돌·속도 지점(JSON)",
    REPORT_ACCIDENT_NUMBER_COLUMN: "보고서 사고번호(날짜_순번)(비식별화 목적)",
}
_NS = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
_SHEET_PATH = "xl/worksheets/sheet1.xml"
_XML_TEXT = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\ud800-\udfff\ufffe\uffff]")
_MAX_ROWS = 1_048_576
_MAX_CELL_LENGTH = 32_767


class DataSpecificationError(OSError):
    """An invalid template or unsupported text must not commit partial exports."""


def csv_export_headers(definition: FormDefinition) -> list[str]:
    # 보고서 번호를 맨 앞에 두고 조사 항목은 현재 편집 양식의 순서를 따른다.
    return [REPORT_ACCIDENT_NUMBER_COLUMN, *METADATA_HEADERS, *definition.csv_headers()]


def specification_rows(
    definition: FormDefinition, *, headers: Sequence[str] | None = None,
) -> list[tuple[str, str]]:
    """One description per exported name, in precisely the exported order."""
    expected = csv_export_headers(definition)
    if headers is not None and list(headers) != expected:
        raise DataSpecificationError("CSV와 데이터 명세서의 칼럼 순서가 일치하지 않습니다.")
    descriptions = dict(_METADATA_LABELS)
    for record in definition.records():
        if column := record.get("csv_column"):
            descriptions[column] = record["label"]
        for choice in record["choices"]:
            if column := choice.get("csv_column"):
                descriptions[column] = f"{record['label']} ({choice['label']})"
    return [(column, descriptions[column]) for column in expected]


def _cell_text(cell: ET.Element, shared: list[str]) -> str:
    if cell.get("t") == "inlineStr":
        inline = cell.find("m:is", _NS)
        return "" if inline is None else "".join(inline.itertext())
    value = cell.find("m:v", _NS)
    if value is None or value.text is None:
        return ""
    return shared[int(value.text)] if cell.get("t") == "s" else value.text


def _safe_text(text: str) -> str:
    # Use literal inline strings, never formulas, even for user-edited labels.
    if _XML_TEXT.search(text) or len(text) > _MAX_CELL_LENGTH:
        raise DataSpecificationError(
            "데이터 명세서의 칼럼명 또는 항목 이름에 Excel에서 지원하지 않는 문자나 길이가 있습니다."
        )
    return escape(text).replace("\r", "&#13;")


def _display_units(text: str) -> float:
    return sum(2 if unicodedata.east_asian_width(char) in {"W", "F"} else 1 for char in text)


def _row_height(values: tuple[str, str], widths: tuple[float, float], minimum: float) -> float:
    # Preserve the existing fonts/widths and extend wrapped text vertically.
    lines = max(
        sum(max(1, math.ceil(_display_units(line) / max(1, width - 2)))
            for line in value.replace("\r\n", "\n").replace("\r", "\n").split("\n"))
        for value, width in zip(values, widths, strict=True)
    )
    height = max(minimum, lines * 17.0)
    if height > 409:
        raise DataSpecificationError("데이터 명세서에서 표시하기에 항목 이름이 너무 깁니다. 이름을 줄여 주세요.")
    return height


def _render_sheet(xml: bytes, shared: list[str], rows: list[tuple[str, str]]) -> bytes:
    sheet = ET.fromstring(xml)
    sheet_data = sheet.find("m:sheetData", _NS)
    if sheet_data is None or not list(sheet_data):
        raise DataSpecificationError("데이터 명세서 서식에 표가 없습니다.")
    if any(sheet.find(f"m:{tag}", _NS) is not None for tag in ("tableParts", "mergeCells")):
        raise DataSpecificationError("지원하지 않는 데이터 명세서 서식입니다.")
    source = xml.decode("utf-8")
    match = re.search(
        r"<(?P<prefix>[\w.-]+:)?sheetData\b[^>]*>(?P<data>.*?)</(?:[\w.-]+:)?sheetData>",
        source, re.DOTALL,
    )
    if match is None:
        raise DataSpecificationError("데이터 명세서 표를 찾을 수 없습니다.")
    prefix = match.group("prefix") or ""
    header_match = re.search(
        r"<(?:[\w.-]+:)?row\b[^>]*\br=[\"']1[\"'][^>]*>.*?</(?:[\w.-]+:)?row>",
        match.group("data"), re.DOTALL,
    )
    if header_match is None:
        raise DataSpecificationError("데이터 명세서 머리글을 찾을 수 없습니다.")
    styles: dict[str, tuple[dict[str, str], dict[str, str], dict[str, str]]] = {}
    for row in sheet_data:
        cells = {re.sub(r"\d+$", "", cell.get("r", "")): cell for cell in row}
        if "A" in cells and "B" in cells and row.get("r") != "1":
            name = _cell_text(cells["A"], shared)
            if name:
                styles[name] = (dict(row.attrib), dict(cells["A"].attrib), dict(cells["B"].attrib))
    if not styles:
        raise DataSpecificationError("데이터 명세서의 본문 서식을 찾을 수 없습니다.")
    # The final populated row is the existing supplemental-field style.
    generic = next(reversed(styles.values()))
    widths = [34.125, 49.5625]
    for col in sheet.findall("m:cols/m:col", _NS):
        for index in range(2):
            if int(col.get("min", "1")) <= index + 1 <= int(col.get("max", "1")):
                widths[index] = float(col.get("width", str(widths[index])))
    fragments = [header_match.group(0)]
    for number, values in enumerate(rows, 2):
        row_attrs, a_attrs, b_attrs = styles.get(values[0], generic)
        attrs = {key: value for key, value in row_attrs.items()
                 if key not in {"r", "spans", "ht", "customHeight", "hidden"}}
        attrs.update(r=str(number), ht=str(_row_height(
            values, tuple(widths), float(row_attrs.get("ht", "16.9")),
        )), customHeight="1", spans="1:2")
        attr_text = " ".join(f"{key}={quoteattr(value)}" for key, value in attrs.items())
        fragments.append(f"<{prefix}row {attr_text}>")
        for letter, text, cell_attrs in zip(("A", "B"), values, (a_attrs, b_attrs), strict=True):
            style = cell_attrs.get("s", "0")
            fragments.append(
                f'<{prefix}c r="{letter}{number}" s={quoteattr(style)} t="inlineStr">'
                f'<{prefix}is><{prefix}t xml:space="preserve">{_safe_text(text)}</{prefix}t>'
                f'</{prefix}is></{prefix}c>'
            )
        fragments.append(f"</{prefix}row>")
    source = source[:match.start("data")] + "".join(fragments) + source[match.end("data"):]
    last = len(rows) + 1
    source, count = re.subn(
        r'(<(?:[\w.-]+:)?dimension\b[^>]*\bref=)[\"\'][^\"\']*[\"\']',
        lambda found: found.group(1) + f'"A1:B{last}"', source, count=1,
    )
    if count != 1:
        raise DataSpecificationError("데이터 명세서의 사용 범위를 찾을 수 없습니다.")
    # Reset only navigation to show the newly ordered first fields on open.
    source = re.sub(r'(\btopLeftCell=)[\"\'][^\"\']*[\"\']', r'\1"A1"', source)
    def reset_selection(selection: re.Match) -> str:
        tag = re.sub(r'(\bactiveCell=)[\"\'][^\"\']*[\"\']', r'\1"A1"', selection.group())
        return re.sub(r'(\bsqref=)[\"\'][^\"\']*[\"\']', r'\1"A1"', tag)

    source = re.sub(r'<(?:[\w.-]+:)?selection\b[^>]*>', reset_selection, source)
    # Parse again before committing an invalid workbook to an export file.
    ET.fromstring(source)
    return source.encode("utf-8")


def build_specification(
    template: bytes, definition: FormDefinition, *, headers: Sequence[str] | None = None,
) -> bytes:
    """Fill the shipped two-column template with the exact live CSV dictionary."""
    rows = specification_rows(definition, headers=headers)
    if len(rows) + 1 > _MAX_ROWS:
        raise DataSpecificationError("데이터 명세서가 Excel의 최대 행 수를 초과합니다.")
    try:
        with ZipFile(BytesIO(template)) as original:
            shared = []
            if "xl/sharedStrings.xml" in original.namelist():
                shared = ["".join(item.itertext()) for item in ET.fromstring(
                    original.read("xl/sharedStrings.xml"),
                )]
            rendered = _render_sheet(original.read(_SHEET_PATH), shared, rows)
            result = BytesIO()
            with ZipFile(result, "w") as output:
                for item in original.infolist():
                    output.writestr(item, rendered if item.filename == _SHEET_PATH
                                    else original.read(item.filename))
                output.comment = original.comment
            return result.getvalue()
    except DataSpecificationError:
        raise
    except (BadZipFile, ET.ParseError, KeyError, IndexError, TypeError, ValueError, RuntimeError) as exc:
        raise DataSpecificationError("데이터 명세서 서식을 읽거나 생성하지 못했습니다.") from exc
