"""Preserve unsupported native view/metadata on the Artifact Tool-authored edit.

The import/export API omitted sheetViews. Keep every original ZIP member and
every original worksheet byte except its extent and the authored added row.
The values below are read from the verified Artifact Tool export, not invented.
"""
from copy import deepcopy
from pathlib import Path
import re
from xml.etree import ElementTree as E
from zipfile import ZipFile

source = Path("runtime/workbooks/metadata-update/original-spec.xlsx")
output = Path("outputs/avat-case-numbering/AVAT 데이터 명세서.xlsx")
namespace = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
ns = {"m": namespace}
with ZipFile(output) as archive:
    strings = ["".join(item.itertext()) for item in E.fromstring(archive.read("xl/sharedStrings.xml"))] if "xl/sharedStrings.xml" in archive.namelist() else []
    edited = E.fromstring(archive.read("xl/worksheets/sheet1.xml"))
    authored = {}
    for cell in edited.findall(".//m:row[@r='96']/m:c", ns):
        if cell.get("t") == "s":
            authored[cell.get("r")] = strings[int(cell.find("m:v", ns).text)]
        elif cell.get("t") == "inlineStr":
            authored[cell.get("r")] = "".join(cell.find("m:is", ns).itertext())
        elif cell.find("m:v", ns) is not None:
            authored[cell.get("r")] = cell.find("m:v", ns).text
assert authored == {"A96": "report_accident_number", "B96": "보고서 사고번호(날짜_순번)"}
with ZipFile(source) as archive:
    entries = [(info, archive.read(info.filename)) for info in archive.infolist()]
sheet = next(content for info, content in entries if info.filename == "xl/worksheets/sheet1.xml")
root = E.fromstring(sheet)
new_row = deepcopy(root.find(".//m:row[@r='95']", ns))
new_row.set("r", "96")
for cell in new_row.findall("m:c", ns):
    reference = cell.get("r").replace("95", "96")
    cell.set("r", reference)
    cell.set("t", "inlineStr")
    for child in list(cell):
        cell.remove(child)
    text = E.SubElement(E.SubElement(cell, "{" + namespace + "}is"), "{" + namespace + "}t")
    text.text = authored[reference]
row_pattern = rb'<row\b(?=[^>]*\br="96")[^>]*(?:/>|>.*?</row>)'
updated, count = re.subn(row_pattern, lambda _match: E.tostring(new_row, encoding="utf-8"),
                         sheet, count=1, flags=re.DOTALL)
assert count == 1
with ZipFile(output, "w") as archive:
    for info, content in entries:
        archive.writestr(info, updated if info.filename == "xl/worksheets/sheet1.xml" else content)
print("Preserved all original ZIP members, original cells/styles, and native views; appended authored row only.")
