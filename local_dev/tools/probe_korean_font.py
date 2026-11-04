from pathlib import Path
import sys

from PySide6.QtCore import QRect, Qt
from PySide6.QtGui import QColor, QFont, QImage, QPainter
from PySide6.QtWidgets import QApplication

from investigator.theme import ui_font_family

app = QApplication([])
canvas = QImage(800, 330, QImage.Format.Format_RGB32)
canvas.fill(Qt.GlobalColor.white)
painter = QPainter(canvas)
painter.setPen(QColor('#111827'))
painter.setRenderHint(QPainter.RenderHint.TextAntialiasing)
for index, size in enumerate([13, 14, 18, 24]):
    font = QFont(ui_font_family())
    font.setPixelSize(size)
    if len(sys.argv) > 2 and sys.argv[2] == 'nohint':
        font.setHintingPreference(QFont.HintingPreference.PreferNoHinting)
    painter.setFont(font)
    painter.drawText(QRect(15, index * 75 + 10, 770, 65), Qt.AlignmentFlag.AlignVCenter,
                     f'{size}px  전면  후면  충돌 방향  면  명  영  ㅁ  ㅇ')
painter.end()
destination = Path(sys.argv[1])
canvas.save(str(destination))
print(app.platformName(), destination)
