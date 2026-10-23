"""Read-only scope check for the bundled CSV data dictionary."""
from pathlib import Path
from xml.etree import ElementTree as E
from zipfile import ZipFile

NS = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}


def read(path):
    with ZipFile(path) as archive:
        strings = ["".join(e.itertext()) for e in E.fromstring(archive.read("xl/sharedStrings.xml"))] if "xl/sharedStrings.xml" in archive.namelist() else []
        sheet = E.fromstring(archive.read("xl/worksheets/sheet1.xml"))
        cells = {}
        for cell in sheet.findall(".//m:c", NS):
            value = cell.find("m:v", NS)
            kind = cell.get("t")
            if kind == "s":
                text = strings[int(value.text)]
            elif kind == "inlineStr":
                text = "".join(cell.find("m:is", NS).itertext())
            else:
                text = value.text if value is not None else None
            if text is not None:
                cells[cell.get("r")] = text
        return cells, sheet


original, old_sheet = read(Path("runtime/workbooks/metadata-update/original-spec.xlsx"))
edited, new_sheet = read(Path("outputs/avat-case-numbering/AVAT 데이터 명세서.xlsx"))
assert all(edited.get(key) == value for key, value in original.items())
assert set(edited) - set(original) == {"A96", "B96"}
assert edited["A96"] == "report_accident_number"
assert edited["B96"] == "보고서 사고번호(날짜_순번)"
assert not new_sheet.findall(".//m:f", NS)
rows = [int(row.get("r")) for row in new_sheet.findall("m:sheetData/m:row", NS)]
assert rows == sorted(set(rows))
for tag in ("cols", "sheetViews", "mergeCells", "sheetFormatPr"):
    old, new = old_sheet.find("m:" + tag, NS), new_sheet.find("m:" + tag, NS)
    print(tag, "old", E.tostring(old).decode() if old is not None else None,
          "new", E.tostring(new).decode() if new is not None else None)
print("Verified: original 190 cell values preserved; exactly two new cells; no formulas.")
