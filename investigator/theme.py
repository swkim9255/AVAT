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

PRIMARY = "#1F5FBF"
PRIMARY_HOVER = "#17468C"
PRIMARY_SUBTLE = "#E7EEF9"
TEXT = "#1B1F24"
MUTED = "#5B636E"
BORDER = "#DDE1E6"
BACKGROUND = "#F5F7FA"
SURFACE = "#FFFFFF"
SUBTLE = "#F1F3F6"
CONTROL_BORDER = "#C9CED6"
SELECTED_BORDER = "#B9CDEC"
DANGER = "#B5472B"
SUCCESS = "#1E5B34"
SUCCESS_SUBTLE = "#E3F2E8"
WARNING = "#8A5300"
WARNING_SUBTLE = "#FFF4DC"
WARNING_BORDER = "#F0D49A"
STAT_BAR = "#8CAEE3"
STAT_BAR_DIM = "#CFDCEF"
STAT_MISSING = "#C3C9D2"
VIDEO_BACKGROUND = "#111827"
INVESTIGATION_FONT_SIZE = 13

_ASSET_DIR = Path(__file__).resolve().parent / "assets" / "material_symbols"
_FONT_PATH = _ASSET_DIR / "MaterialSymbolsOutlined[FILL,GRAD,opsz,wght].ttf"
_CODEPOINTS_PATH = _ASSET_DIR / "MaterialSymbolsOutlined[FILL,GRAD,opsz,wght].codepoints"
_FONT_FAMILY: str | None = None


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
    """Use Windows' Malgun Gothic for UI text, leaving symbol icons separate."""
    return "Malgun Gothic"


class _MaterialIconEngine(QIconEngine):
    def __init__(self, glyph: str, color: str) -> None:
        super().__init__()
        self.glyph = glyph
        self.color = QColor(color)

    def clone(self) -> QIconEngine:
        return _MaterialIconEngine(self.glyph, self.color.name())

    def paint(self, painter: QPainter, rect: QRect, mode: QIcon.Mode, state: QIcon.State) -> None:
        del state
        color = QColor("#9AA1AB") if mode is QIcon.Mode.Disabled else self.color
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
    """Apply the shared investigation and statistics prototype visual system."""

    ui_family = ui_font_family()
    app_font = QFont(ui_family)
    # Keep native item-view metrics and general UI text at the same 13 px size.
    app_font.setPixelSize(13)
    # Preserve Korean counters/strokes at fractional display scales instead
    # of distorting glyph outlines to snap every stroke horizontally.
    app_font.setHintingPreference(QFont.HintingPreference.PreferVerticalHinting)
    app_font.setStyleStrategy(QFont.StyleStrategy.PreferAntialias)
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
    palette.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.Text, QColor("#9AA1AB"))
    palette.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.ButtonText, QColor("#9AA1AB"))
    palette.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.Base, QColor(SUBTLE))
    app.setPalette(palette)

    app.setStyleSheet(
        f"""
        * {{
            font-family: "{ui_family}";
            font-size: 13px;
            color: {TEXT};
        }}
        QWidget[investigationField="true"], QWidget[investigationField="true"] * {{
            font-size: {INVESTIGATION_FONT_SIZE}px;
        }}
        QMainWindow, QWidget#appRoot {{ background: {BACKGROUND}; }}
        QDialog {{ background: {SURFACE}; }}
        QWidget#panel, QFrame#card, QWidget#statCard, QWidget#statisticsHeader,
        QWidget#statisticsFilters, QScrollArea#formScroll,
        QTabWidget::pane {{
            background: {SURFACE};
        }}
        QWidget#panel, QFrame#card, QWidget#statCard {{
            border: 1px solid {BORDER};
            border-radius: 10px;
        }}
        QWidget#statisticsPanel, QScrollArea#statisticsScroll {{
            background: {BACKGROUND}; border: 0;
        }}
        QWidget#statisticsScrollContents {{ background: {BACKGROUND}; }}
        QWidget#statisticsFilters {{ border-bottom: 1px solid {BORDER}; }}
        QLabel#sectionTitle {{ font-size: 16px; font-weight: 700; }}
        QLabel#caseTitle {{ font-size: 20px; font-weight: 700; color: {TEXT}; }}
        QLabel#statTitle {{ font-size: 15px; font-weight: 600; }}
        QLabel#statValue {{ font-size: 28px; font-weight: 700; }}
        QLabel#muted, QLabel#secondaryText {{ color: {MUTED}; font-size: 12px; }}
        QLabel#requiredMark {{ color: {PRIMARY}; font-weight: 700; }}
        QLabel#missingBadge {{
            color: {WARNING}; background: {WARNING_SUBTLE}; border: 1px solid {WARNING_BORDER};
            border-radius: 10px; padding: 3px 9px; font-size: 11px; font-weight: 600;
        }}
        QLabel#completeBadge {{
            color: {SUCCESS}; background: {SUCCESS_SUBTLE}; border: 1px solid #BEDBC8;
            border-radius: 10px; padding: 3px 9px; font-size: 11px; font-weight: 600;
        }}
        QLabel#warningNote, QFrame#warningNote {{
            color: {WARNING}; background: {WARNING_SUBTLE}; border: 1px solid {WARNING_BORDER};
            border-radius: 8px; padding: 10px 14px; font-size: 13px;
        }}
        QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox, QDateEdit, QTextEdit,
        QPlainTextEdit {{
            min-height: 34px; background: {SURFACE}; border: 1px solid {CONTROL_BORDER};
            border-radius: 6px; padding: 0 10px; selection-background-color: {PRIMARY};
            selection-color: white;
        }}
        QTextEdit, QPlainTextEdit {{ padding: 8px 10px; }}
        QLineEdit:hover, QComboBox:hover, QSpinBox:hover, QDateEdit:hover {{
            border-color: #8A93A0;
        }}
        QLineEdit:focus, QComboBox:focus, QSpinBox:focus, QDoubleSpinBox:focus,
        QDateEdit:focus, QTextEdit:focus, QPlainTextEdit:focus {{ border-color: {PRIMARY}; }}
        QLineEdit[invalid="true"], QComboBox[invalid="true"], QTextEdit[invalid="true"],
        QPlainTextEdit[invalid="true"] {{ border: 1px solid {DANGER}; background: #FFF1EE; }}
        QLineEdit[calculated="true"], QLineEdit:disabled, QComboBox:disabled,
        QSpinBox:disabled, QTextEdit:disabled {{
            background: {SUBTLE}; color: {MUTED}; border-color: {BORDER};
        }}
        QComboBox::drop-down {{ border: 0; width: 28px; }}
        QComboBox QAbstractItemView {{ background: {SURFACE}; border: 1px solid {BORDER}; selection-background-color: {PRIMARY_SUBTLE}; selection-color: {TEXT}; padding: 4px; }}
        QPushButton, QToolButton {{
            min-height: 32px; background: {SURFACE}; border: 1px solid {CONTROL_BORDER};
            border-radius: 6px; padding: 0 12px; font-weight: 500;
        }}
        QPushButton:hover, QToolButton:hover {{ background: {SUBTLE}; border-color: #8A93A0; }}
        QPushButton:pressed, QToolButton:pressed {{ background: #E5E9EF; }}
        QPushButton:disabled, QToolButton:disabled {{ color: #9AA1AB; background: {SUBTLE}; border-color: {BORDER}; }}
        QPushButton[primary="true"] {{ color: white; background: {PRIMARY}; border-color: {PRIMARY}; }}
        QPushButton[primary="true"]:hover {{ background: {PRIMARY_HOVER}; }}
        QPushButton[success="true"] {{ color: white; background: {SUCCESS}; border-color: {SUCCESS}; }}
        QPushButton#completeButton {{
            background: {PRIMARY_SUBTLE}; color: {PRIMARY_HOVER}; border-color: {SELECTED_BORDER};
            font-weight: 600;
        }}
        QPushButton#completeButton:hover {{ background: #D7E4F7; border-color: {PRIMARY}; }}
        QPushButton#completeButton:disabled {{
            color: #9AA1AB; background: {SUBTLE}; border-color: {BORDER};
        }}
        QPushButton[flat="true"], QToolButton[flat="true"] {{ border-color: transparent; background: transparent; }}
        QPushButton#filterChip, QToolButton#filterChip {{
            min-height: 28px; border-radius: 15px; padding: 0 12px; font-size: 13px;
        }}
        QPushButton#filterChip:checked, QToolButton#filterChip:checked {{
            background: {PRIMARY_SUBTLE}; color: {PRIMARY_HOVER}; border-color: {SELECTED_BORDER};
        }}
        QToolButton[transport="true"] {{
            min-width: 32px; max-width: 32px; min-height: 32px; max-height: 32px;
            background: transparent; border: 1px solid transparent; border-radius: 4px; padding: 0;
        }}
        QToolButton[transport="true"]:hover {{ background: {SUBTLE}; border-color: transparent; }}
        QToolButton[transport="true"]:pressed {{ background: #E5E9EF; }}
        QToolButton[transport="true"]:disabled {{ background: transparent; border-color: transparent; }}
        QToolButton#playPauseButton {{
            min-width: 38px; max-width: 38px; min-height: 38px; max-height: 38px;
            background: {SURFACE}; border: 2px solid {PRIMARY}; border-radius: 21px; padding: 0;
        }}
        QToolButton#playPauseButton:hover {{ background: {PRIMARY_SUBTLE}; border-color: {PRIMARY}; }}
        QToolButton#playPauseButton:pressed {{ background: #D7E4F7; }}
        QToolButton#playPauseButton:disabled {{ background: {SUBTLE}; border-color: {BORDER}; }}
        QLineEdit#caseSearch QToolButton {{
            min-width: 20px; max-width: 20px; min-height: 20px; max-height: 20px;
            padding: 0; margin: 0; border: 0; background: transparent;
        }}
        QCheckBox {{ spacing: 7px; }}
        QCheckBox::indicator {{ width: 14px; height: 14px; }}
        QScrollArea#formScroll > QWidget > QWidget {{ background: {SURFACE}; }}
        QTabWidget::pane {{ border: 0; border-top: 1px solid {BORDER}; }}
        QTabBar::tab {{
            background: {SURFACE}; color: {MUTED}; border: 0; border-bottom: 2px solid transparent;
            padding: 10px 3px; min-width: 0; font-size: 13px; font-weight: 500;
        }}
        QTabBar::tab:selected {{ color: {TEXT}; border-bottom-color: {PRIMARY}; font-weight: 600; }}
        QTabBar::tab:hover:!selected {{ color: {TEXT}; background: {SUBTLE}; }}
        QTabBar#projectTabs {{ background: transparent; border: 0; }}
        QTabBar#projectTabs::tab {{
            background: {SUBTLE}; color: {MUTED}; border: 1px solid {BORDER};
            border-radius: 6px; padding: 6px 12px; margin-right: 6px;
            min-width: 160px; max-width: 260px; font-size: 14px;
        }}
        QTabBar#projectTabs::tab:selected {{
            background: {PRIMARY_SUBTLE}; border-color: {SELECTED_BORDER}; color: {TEXT};
        }}
        QFrame#viewModeSwitch {{
            background: {SURFACE}; border: 1px solid {CONTROL_BORDER}; border-radius: 7px;
        }}
        QFrame#viewModeSwitch QPushButton {{
            border: 0; border-radius: 0; padding: 0; margin: 0;
            min-width: 0; min-height: 0; background: transparent; font-size: 14px;
        }}
        QFrame#viewModeSwitch QPushButton#investigationMode {{
            border-top-left-radius: 6px; border-bottom-left-radius: 6px;
        }}
        QFrame#viewModeSwitch QPushButton#statisticsMode {{
            border-top-right-radius: 6px; border-bottom-right-radius: 6px;
        }}
        QFrame#viewModeSwitch QPushButton:checked {{ background: {TEXT}; color: white; }}
        QFrame#viewModeSwitch QPushButton:hover:!checked {{ background: {SUBTLE}; }}
        QFrame#viewModeSwitch QPushButton:focus {{ border: 1px solid {PRIMARY}; }}
        QFrame#viewModeSwitch QPushButton:disabled {{ color: #9AA1AB; }}
        QWidget#workspaceControls {{ background: {SURFACE}; }}
        QFrame#workspaceDivider {{ border: 0; background: {BORDER}; }}
        QMenuBar#workspaceMenu {{ padding: 8px 10px; }}
        QTabBar#projectTabs QToolButton {{
            padding: 0; border: 1px solid {BORDER}; background: {SURFACE};
        }}
        QTabBar#projectTabs QToolButton#closeProjectTab {{
            border: 0; padding: 0; background: transparent; border-radius: 3px;
            min-width: 20px; max-width: 20px; min-height: 20px; max-height: 20px;
        }}
        QTabBar#projectTabs QToolButton#closeProjectTab:hover {{ background: {CONTROL_BORDER}; }}
        QTabBar#statisticsViews {{ background: {SURFACE}; border-bottom: 1px solid {BORDER}; }}
        QTabBar#statisticsViews::tab {{
            font-size: 14px; padding: 10px 14px; border-bottom-width: 3px;
        }}
        QTreeWidget {{ background: {SURFACE}; border: 0; outline: 0; }}
        QTreeWidget::item {{ min-height: 38px; border-radius: 6px; padding: 2px 4px; }}
        QTreeWidget::item:selected {{ background: {PRIMARY_SUBTLE}; color: {TEXT}; }}
        QTreeWidget::item:hover:!selected {{ background: #EEF2F8; }}
        QTableView {{
            background: {SURFACE}; alternate-background-color: {SUBTLE}; border: 1px solid {BORDER};
            gridline-color: {BORDER}; border-radius: 6px; selection-background-color: {PRIMARY_SUBTLE};
            selection-color: {TEXT}; outline: 0;
        }}
        QTableView::item {{ padding: 7px 10px; border: 0; }}
        QTableView::item:selected {{ background: {PRIMARY_SUBTLE}; color: {TEXT}; }}
        QHeaderView::section {{
            background: {SUBTLE}; border: 0; border-right: 1px solid {BORDER};
            border-bottom: 1px solid {BORDER}; padding: 9px 10px; font-size: 13px;
            font-weight: 600; color: {MUTED};
        }}
        QTreeWidget QHeaderView {{ font-size: 13px; }}
        QTreeWidget QHeaderView::section {{ padding: 0; }}
        QTableCornerButton::section {{ background: {SUBTLE}; border: 0; }}
        QProgressBar {{
            background: {SUBTLE}; border: 0; border-radius: 4px; min-height: 8px;
            text-align: center; color: {TEXT};
        }}
        QProgressBar::chunk {{ background: {STAT_BAR}; border-radius: 4px; }}
        QScrollArea {{ border: 0; background: transparent; }}
        QScrollBar:vertical {{ background: transparent; width: 10px; margin: 2px; }}
        QScrollBar::handle:vertical {{ background: {CONTROL_BORDER}; border-radius: 4px; min-height: 28px; }}
        QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
        QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{ background: transparent; }}
        QScrollBar:horizontal {{ background: transparent; height: 10px; margin: 2px; }}
        QScrollBar::handle:horizontal {{ background: {CONTROL_BORDER}; border-radius: 4px; min-width: 28px; }}
        QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{ width: 0; }}
        QScrollBar::add-page:horizontal, QScrollBar::sub-page:horizontal {{ background: transparent; }}
        QSlider::groove:horizontal {{ height: 5px; background: {CONTROL_BORDER}; border-radius: 2px; }}
        QSlider::sub-page:horizontal {{ background: {PRIMARY}; border-radius: 2px; }}
        QSlider::handle:horizontal {{ background: {PRIMARY}; border: 3px solid white; width: 15px; margin: -7px 0; border-radius: 9px; }}
        QSplitter::handle {{ background: {BACKGROUND}; }}
        QSplitter::handle:horizontal {{ width: 6px; }}
        QSplitter#mainSplitter::handle:horizontal {{
            width: 8px; background: {CONTROL_BORDER};
            border-left: 2px solid {BACKGROUND}; border-right: 2px solid {BACKGROUND};
        }}
        QSplitter#mainSplitter::handle:horizontal:hover {{ background: {STAT_BAR}; }}
        QSplitter#mainSplitter::handle:horizontal:pressed {{ background: {PRIMARY}; }}
        QSplitter#videoToolsSplitter::handle:vertical {{
            height: 8px; background: {CONTROL_BORDER};
            border-top: 2px solid {BACKGROUND}; border-bottom: 2px solid {BACKGROUND};
        }}
        QSplitter#videoToolsSplitter::handle:vertical:hover {{ background: {STAT_BAR}; }}
        QSplitter#videoToolsSplitter::handle:vertical:pressed {{ background: {PRIMARY}; }}
        QMenuBar {{ background: {SURFACE}; border-bottom: 1px solid {BORDER}; padding: 3px 10px; }}
        QMenuBar::item {{ background: transparent; padding: 5px 10px; border-radius: 4px; font-size: 13px; }}
        QMenuBar::item:selected {{ background: {SUBTLE}; }}
        QMenu {{ background: {SURFACE}; border: 1px solid {CONTROL_BORDER}; padding: 5px; }}
        QMenu::item {{ padding: 8px 24px; border-radius: 4px; font-size: 13px; }}
        QMenu::item:selected {{ background: {PRIMARY_SUBTLE}; color: {TEXT}; }}
        QMenu::item:disabled {{ color: #9AA1AB; }}
        QMenu::separator {{ height: 1px; background: {BORDER}; margin: 4px; }}
        QStatusBar {{ background: {SURFACE}; border-top: 1px solid {BORDER}; }}
        QStatusBar, QStatusBar QLabel {{ font-size: 12px; color: {MUTED}; }}
        QToolTip {{ color: {TEXT}; background: {SURFACE}; border: 1px solid {BORDER}; padding: 5px; }}
        """
    )


__all__ = [
    "BACKGROUND",
    "BORDER",
    "CONTROL_BORDER",
    "DANGER",
    "MUTED",
    "PRIMARY",
    "PRIMARY_HOVER",
    "PRIMARY_SUBTLE",
    "SELECTED_BORDER",
    "STAT_BAR",
    "STAT_BAR_DIM",
    "STAT_MISSING",
    "SUBTLE",
    "SUCCESS",
    "SUCCESS_SUBTLE",
    "SURFACE",
    "TEXT",
    "VIDEO_BACKGROUND",
    "WARNING",
    "WARNING_BORDER",
    "WARNING_SUBTLE",
    "apply_theme",
    "material_icon",
]
