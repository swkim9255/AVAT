"""Visual tokens and bundled Material Symbols used by the application."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from PySide6.QtCore import QRect, QSize, Qt
from PySide6.QtGui import (
    QColor,
    QFont,
    QFontDatabase,
    QIcon,
    QIconEngine,
    QPainter,
    QPalette,
    QPixmap,
)
from PySide6.QtWidgets import QApplication

PRIMARY = "#003CDC"
PRIMARY_HOVER = "#174FE6"
PRIMARY_SUBTLE = "#EEF3FF"
TEXT = "#111827"
MUTED = "#64748B"
BORDER = "#E2E8F0"
BACKGROUND = "#F5F7FA"
SURFACE = "#FFFFFF"
SUBTLE = "#F8FAFC"
DANGER = "#C2410C"
SUCCESS = "#12805C"
VIDEO_BACKGROUND = "#111827"

_ASSET_DIR = Path(__file__).resolve().parent / "assets" / "material_symbols"
_FONT_PATH = _ASSET_DIR / "MaterialSymbolsOutlined[FILL,GRAD,opsz,wght].ttf"
_CODEPOINTS_PATH = _ASSET_DIR / "MaterialSymbolsOutlined[FILL,GRAD,opsz,wght].codepoints"
_UI_FONT_PATH = Path(__file__).resolve().parent / "assets" / "fonts" / "NotoSansKR-VF.ttf"
_FONT_FAMILY: str | None = None
_UI_FONT_FAMILY: str | None = None


@lru_cache(maxsize=1)
def _codepoints() -> dict[str, int]:
    mapping: dict[str, int] = {}
    for line in _CODEPOINTS_PATH.read_text(encoding="utf-8").splitlines():
        if line.strip():
            name, value = line.split()
            mapping[name] = int(value, 16)
    return mapping


def material_font(size: int) -> QFont:
    global _FONT_FAMILY
    if _FONT_FAMILY is None:
        font_id = QFontDatabase.addApplicationFont(str(_FONT_PATH))
        families = QFontDatabase.applicationFontFamilies(font_id) if font_id >= 0 else []
        if not families:
            raise RuntimeError(f"Material Symbols 글꼴을 불러올 수 없습니다: {_FONT_PATH}")
        _FONT_FAMILY = families[0]
    font = QFont(_FONT_FAMILY)
    font.setPixelSize(max(1, size))
    font.setStyleStrategy(QFont.StyleStrategy.NoFontMerging)
    return font


def ui_font_family() -> str:
    global _UI_FONT_FAMILY
    if _UI_FONT_FAMILY is None:
        font_id = QFontDatabase.addApplicationFont(str(_UI_FONT_PATH))
        families = QFontDatabase.applicationFontFamilies(font_id) if font_id >= 0 else []
        if not families:
            raise RuntimeError(f"한글 UI 글꼴을 불러올 수 없습니다: {_UI_FONT_PATH}")
        _UI_FONT_FAMILY = families[0]
    return _UI_FONT_FAMILY


class _MaterialIconEngine(QIconEngine):
    def __init__(self, glyph: str, color: str) -> None:
        super().__init__()
        self.glyph = glyph
        self.color = QColor(color)

    def clone(self) -> QIconEngine:
        return _MaterialIconEngine(self.glyph, self.color.name())

    def paint(self, painter: QPainter, rect: QRect, mode: QIcon.Mode, state: QIcon.State) -> None:
        del state
        color = QColor("#94A3B8") if mode is QIcon.Mode.Disabled else self.color
        painter.save()
        painter.setRenderHints(
            QPainter.RenderHint.Antialiasing | QPainter.RenderHint.TextAntialiasing
        )
        painter.setPen(color)
        painter.setFont(material_font(max(12, min(rect.width(), rect.height()))))
        painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, self.glyph)
        painter.restore()

    def pixmap(self, size: QSize, mode: QIcon.Mode, state: QIcon.State) -> QPixmap:
        pixmap = QPixmap(size)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        self.paint(painter, pixmap.rect(), mode, state)
        painter.end()
        return pixmap


@lru_cache(maxsize=256)
def material_icon(name: str, color: str = PRIMARY) -> QIcon:
    try:
        glyph = chr(_codepoints()[name])
    except KeyError as exc:
        raise ValueError(f"알 수 없는 Material Symbol입니다: {name}") from exc
    material_font(20)
    return QIcon(_MaterialIconEngine(glyph, color))


def apply_theme(app: QApplication) -> None:
    """Apply the AVAT-derived white and Samsung-blue visual system."""

    ui_family = ui_font_family()
    app_font = QFont(ui_family)
    app_font.setPixelSize(13)
    app.setFont(app_font)
    palette = QPalette(app.palette())
    colors = {
        QPalette.ColorRole.Window: BACKGROUND,
        QPalette.ColorRole.WindowText: TEXT,
        QPalette.ColorRole.Base: SURFACE,
        QPalette.ColorRole.AlternateBase: SUBTLE,
        QPalette.ColorRole.Text: TEXT,
        QPalette.ColorRole.Button: SURFACE,
        QPalette.ColorRole.ButtonText: TEXT,
        QPalette.ColorRole.Highlight: PRIMARY,
        QPalette.ColorRole.HighlightedText: SURFACE,
        QPalette.ColorRole.PlaceholderText: MUTED,
        QPalette.ColorRole.Link: PRIMARY,
    }
    for group in (QPalette.ColorGroup.Active, QPalette.ColorGroup.Inactive):
        for role, color in colors.items():
            palette.setColor(group, role, QColor(color))
    palette.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.Text, QColor("#94A3B8"))
    palette.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.ButtonText, QColor("#94A3B8"))
    app.setPalette(palette)

    app.setStyleSheet(
        f"""
        * {{
            font-family: "{ui_family}";
            font-size: 13px;
            color: {TEXT};
        }}
        QMainWindow, QWidget#appRoot {{ background: {BACKGROUND}; }}
        QWidget#panel, QFrame#card, QScrollArea#formScroll, QTabWidget::pane {{
            background: {SURFACE};
        }}
        QFrame#card {{
            border: 1px solid {BORDER};
            border-radius: 10px;
        }}
        QLabel#sectionTitle {{ font-size: 18px; font-weight: 700; }}
        QLabel#caseTitle {{ font-size: 22px; font-weight: 750; color: {TEXT}; }}
        QLabel#muted {{ color: {MUTED}; font-size: 11px; }}
        QLabel#requiredMark {{ color: {PRIMARY}; font-weight: 700; }}
        QLabel#missingBadge {{
            color: {DANGER}; background: #FFF7ED; border: 1px solid #FED7AA;
            border-radius: 10px; padding: 3px 9px; font-size: 11px; font-weight: 700;
        }}
        QLabel#completeBadge {{
            color: {SUCCESS}; background: #ECFDF5; border: 1px solid #A7F3D0;
            border-radius: 10px; padding: 3px 9px; font-size: 11px; font-weight: 700;
        }}
        QLineEdit, QComboBox, QSpinBox, QTextEdit {{
            min-height: 34px; background: {SURFACE}; border: 1px solid #CBD5E1;
            border-radius: 7px; padding: 0 10px; selection-background-color: {PRIMARY};
        }}
        QTextEdit {{ padding: 8px 10px; }}
        QLineEdit:focus, QComboBox:focus, QSpinBox:focus, QTextEdit:focus {{ border: 2px solid {PRIMARY}; }}
        QLineEdit[invalid="true"], QComboBox[invalid="true"], QTextEdit[invalid="true"] {{ border: 1px solid #DC2626; background: #FEF2F2; }}
        QComboBox::drop-down {{ border: 0; width: 28px; }}
        QComboBox QAbstractItemView {{ background: {SURFACE}; border: 1px solid {BORDER}; selection-background-color: {PRIMARY_SUBTLE}; selection-color: {TEXT}; padding: 4px; }}
        QPushButton, QToolButton {{
            min-height: 34px; background: {SURFACE}; border: 1px solid #CBD5E1;
            border-radius: 7px; padding: 0 12px; font-weight: 600;
        }}
        QPushButton:hover, QToolButton:hover {{ background: #F1F5F9; border-color: #94A3B8; }}
        QPushButton:pressed, QToolButton:pressed {{ background: #E2E8F0; }}
        QPushButton:disabled, QToolButton:disabled {{ color: #94A3B8; background: #F8FAFC; border-color: {BORDER}; }}
        QPushButton[primary="true"] {{ color: white; background: {PRIMARY}; border-color: {PRIMARY}; }}
        QPushButton[primary="true"]:hover {{ background: {PRIMARY_HOVER}; }}
        QPushButton[success="true"] {{ color: white; background: {SUCCESS}; border-color: {SUCCESS}; }}
        QPushButton[flat="true"], QToolButton[flat="true"] {{ border-color: transparent; background: transparent; }}
        QToolButton[transport="true"] {{
            min-width: 32px; max-width: 32px; min-height: 32px; max-height: 32px;
            background: transparent; border: 1px solid transparent; border-radius: 4px; padding: 0;
        }}
        QToolButton[transport="true"]:hover {{ background: #F1F5F9; border-color: transparent; }}
        QToolButton[transport="true"]:pressed {{ background: #E2E8F0; }}
        QToolButton[transport="true"]:disabled {{ background: transparent; border-color: transparent; }}
        QToolButton#playPauseButton {{
            min-width: 38px; max-width: 38px; min-height: 38px; max-height: 38px;
            background: {SURFACE}; border: 2px solid {PRIMARY}; border-radius: 21px; padding: 0;
        }}
        QToolButton#playPauseButton:hover {{ background: {PRIMARY_SUBTLE}; border-color: {PRIMARY}; }}
        QToolButton#playPauseButton:pressed {{ background: #DCE6FF; }}
        QToolButton#playPauseButton:disabled {{ background: #F8FAFC; border-color: {BORDER}; }}
        QLineEdit#caseSearch QToolButton {{
            min-width: 20px; max-width: 20px; min-height: 20px; max-height: 20px;
            padding: 0; margin: 0; border: 0; background: transparent;
        }}
        QCheckBox {{ spacing: 7px; }}
        QTabWidget::pane {{ border: 0; border-top: 1px solid {BORDER}; }}
        QTabBar::tab {{
            background: {SURFACE}; color: {MUTED}; border: 0; border-bottom: 2px solid transparent;
            padding: 11px 3px; min-width: 0; font-weight: 600;
        }}
        QTabBar::tab:selected {{ color: {PRIMARY}; border-bottom-color: {PRIMARY}; }}
        QTabBar::tab:hover:!selected {{ color: {TEXT}; background: {SUBTLE}; }}
        QTreeWidget {{ background: {SURFACE}; border: 0; outline: 0; }}
        QTreeWidget::item {{ min-height: 38px; border-radius: 6px; padding: 2px 4px; }}
        QTreeWidget::item:selected {{ background: {PRIMARY_SUBTLE}; color: {TEXT}; }}
        QTreeWidget::item:hover:!selected {{ background: #F1F5F9; }}
        QScrollBar:vertical {{ background: transparent; width: 10px; margin: 2px; }}
        QScrollBar::handle:vertical {{ background: #CBD5E1; border-radius: 4px; min-height: 28px; }}
        QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
        QSlider::groove:horizontal {{ height: 5px; background: #CBD5E1; border-radius: 2px; }}
        QSlider::sub-page:horizontal {{ background: {PRIMARY}; border-radius: 2px; }}
        QSlider::handle:horizontal {{ background: {PRIMARY}; border: 3px solid white; width: 15px; margin: -7px 0; border-radius: 9px; }}
        QSplitter::handle {{ background: {BORDER}; }}
        QSplitter::handle:horizontal {{ width: 1px; }}
        QStatusBar {{ background: {SURFACE}; border-top: 1px solid {BORDER}; }}
        QToolTip {{ color: {TEXT}; background: {SURFACE}; border: 1px solid {BORDER}; padding: 5px; }}
        """
    )


__all__ = [
    "BACKGROUND",
    "BORDER",
    "DANGER",
    "MUTED",
    "PRIMARY",
    "PRIMARY_SUBTLE",
    "SUCCESS",
    "SURFACE",
    "TEXT",
    "VIDEO_BACKGROUND",
    "apply_theme",
    "material_icon",
]
