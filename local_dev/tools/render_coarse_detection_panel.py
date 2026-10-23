"""Render the detector editor with native Windows fonts, without project writes."""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
os.environ["QT_QPA_PLATFORM"] = "windows:fontengine=directwrite"


def main():
    from PySide6.QtCore import QSettings, Qt
    from PySide6.QtWidgets import QApplication, QScrollArea

    from investigator.form_definition import FormDefinition
    from investigator.object_detection import (
        ROLE_TRIGGER,
        DetectedObject,
        ObjectBox,
        VideoObjects,
        set_objects_subtype,
    )
    from investigator.qt_font_setup import configure_text_rendering
    from investigator.theme import apply_theme
    from investigator.widgets.detection_panel import DetectionPanel

    output = ROOT / "local_dev/reports/coarse-detection-ui-20261023-final"
    output.mkdir(exist_ok=False)
    configure_text_rendering()
    app = QApplication(["avat-detector-editor-qa"])
    app.setOrganizationName("AVAT-qa-isolated")
    app.setApplicationName("coarse-detection-render")
    apply_theme(app)
    with tempfile.TemporaryDirectory(prefix="avat-detector-render-") as directory:
        QSettings.setDefaultFormat(QSettings.Format.IniFormat)
        QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope, directory)
        area = QScrollArea()
        area.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen)
        area.setWidgetResizable(True)
        panel = DetectionPanel()
        area.setWidget(panel)
        panel.set_video(1200, 30)
        objects = VideoObjects([
            DetectedObject("v", "V001", ROLE_TRIGGER, None,
                           [ObjectBox(f, (10, 10, 30, 30), .9, "car") for f in (0, 1)],
                           class_key="vehicle", subtype_key="passenger"),
            DetectedObject("p", "P001", ROLE_TRIGGER, None, [ObjectBox(0, (10, 10, 30, 30), .9, "person")],
                           class_key="pedestrian"),
            DetectedObject("m", "M001", ROLE_TRIGGER, None, [ObjectBox(0, (10, 10, 30, 30), .9, "scooter")],
                           class_key="two_wheeler", subtype_key="pm"),
        ])
        objects = set_objects_subtype(objects, ["v"], "truck", frame=1)
        panel.set_objects(objects, FormDefinition.default())
        panel.select_uids(["v"])
        for width in (1000, 450):
            area.resize(width, 780)
            area.show()
            for _ in range(5):
                app.processEvents()
            path = output / f"panel-{width}.png"
            if not area.grab().save(str(path)):
                raise RuntimeError("Could not save detector editor preview")
            print(f"{path}: horizontal-scroll={area.horizontalScrollBar().maximum()}")
        area.close()


if __name__ == "__main__":
    main()
