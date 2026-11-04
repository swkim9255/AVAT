"""Capture the report selection UI without opening a real project."""

from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from PySide6.QtWidgets import QApplication

from investigator.models import InvestigationCase
from investigator.theme import apply_theme
from investigator.widgets.report_export_dialog import ReportExportDialog

app = QApplication([])
apply_theme(app)
cases = []
for index in range(6):
    number = f"2026102000{index + 1:03d}"
    case = InvestigationCase(videos=[f"{number}_{part + 1}.mp4" for part in range(index % 3 + 1)])
    case.values["institution"] = "samsung"
    case.values["accident_classification"] = number
    case.status = "completed" if index in (0, 2, 4) else "draft"
    cases.append(case)
dialog = ReportExportDialog(cases, [cases[0].uid, cases[2].uid, cases[3].uid])
dialog.resize(680, 540)
dialog.show()
app.processEvents()
destination = Path(__file__).with_name("dialog.png")
assert dialog.grab().save(str(destination))
print(destination)
dialog.close()
