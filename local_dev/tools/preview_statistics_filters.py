"""Render generated test-only statistics without opening a user project."""

from pathlib import Path

from investigator.qt_font_setup import configure_text_rendering

configure_text_rendering()

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from investigator.form_definition import FormDefinition  # noqa: E402
from investigator.models import InvestigationCase  # noqa: E402
from investigator.statistics import StatsCondition  # noqa: E402
from investigator.theme import apply_theme  # noqa: E402
from investigator.widgets.statistics_panel import StatisticsPanel  # noqa: E402


app = QApplication([])
apply_theme(app)
panel = StatisticsPanel()
panel.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
cases = [InvestigationCase(
    uid=f"preview-{i}", status="completed", values={
        "institution": "samsung", "accident_classification": f"202001130{i:04d}",
        "weather": "맑음" if i < 3 else "강수",
        "driver_sex": "여성" if i % 2 else "남성",
        "driver_age": "35-39세", "incident_date": "20260101",
    },
) for i in range(5)]
panel.set_data(cases, FormDefinition.default())
panel.add_condition(StatsCondition("weather", "맑음"))
panel.add_condition(StatsCondition("driver_sex", "여성"))
panel.condition_mode.setCurrentIndex(panel.condition_mode.findData("any"))
panel.resize(1400, 800)
panel.show()
app.processEvents()
destination = Path("local_dev/reports/statistics-conditions")
destination.mkdir(parents=True, exist_ok=True)
panel.grab().save(str(destination / "union-panel.png"))
panel.select_cases(("preview-1",), (StatsCondition("driver_age", "35-39세"),), "연령대 선택")
panel.pin_selection()
app.processEvents()
panel.grab().save(str(destination / "union-pinned-panel.png"))
print(panel.target_label.text())
print(panel.filter_description())
panel.close()
