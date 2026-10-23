from pathlib import Path
import sys

from PySide6.QtGui import QFontInfo
from PySide6.QtWidgets import QApplication, QVBoxLayout, QWidget

from investigator.qt_font_setup import configure_text_rendering
from investigator.schema import FIELD_BY_KEY
from investigator.theme import apply_theme
from investigator.widgets.form_panel import FieldEditor

configure_text_rendering()
app = QApplication([])
app.setStyle('Fusion')
apply_theme(app)
root = QWidget()
layout = QVBoxLayout(root)
editor = FieldEditor(FIELD_BY_KEY['collision_directions'], root)
layout.addWidget(editor)
editor.set_value(['전면', '후면'])
root.resize(500, 350)
root.ensurePolished()
layout.activate()
app.processEvents()
destination = Path(sys.argv[1])
root.grab().save(str(destination))
font = editor.checkboxes[0][0].font()
print('native platform', app.platformName(), 'font', QFontInfo(font).family(),
      'pixels', font.pixelSize(), 'hinting', font.hintingPreference(), 'image', destination)
