"""Width-aware checkbox groups for narrow investigation columns."""

from PySide6.QtCore import QEvent, QRect, QSize, Qt
from PySide6.QtGui import QPaintEvent, QResizeEvent
from PySide6.QtWidgets import (
    QCheckBox,
    QFrame,
    QGridLayout,
    QSizePolicy,
    QStyle,
    QStyleOptionButton,
    QStylePainter,
)


class WrappingCheckBox(QCheckBox):
    def __init__(self, text, parent=None):
        super().__init__(text, parent)
        policy = QSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        policy.setHeightForWidth(True)
        self.setSizePolicy(policy)
        self.setMinimumWidth(0)

    def _indicator_space(self) -> int:
        return self.style().pixelMetric(QStyle.PixelMetric.PM_IndicatorWidth, None, self) + 7

    def heightForWidth(self, width: int) -> int:
        bounds = self.fontMetrics().boundingRect(
            QRect(0, 0, max(1, width - self._indicator_space()), 100000),
            Qt.TextFlag.TextWordWrap | Qt.TextFlag.TextWrapAnywhere,
            self.text(),
        )
        return (
            max(
                bounds.height(),
                self.style().pixelMetric(QStyle.PixelMetric.PM_IndicatorHeight, None, self),
            )
            + 4
        )

    def sizeHint(self) -> QSize:
        return QSize(160, self.heightForWidth(160))

    def minimumSizeHint(self) -> QSize:
        return QSize(0, self.fontMetrics().height() + 4)

    def hitButton(self, position) -> bool:
        return self.rect().contains(position)

    def changeEvent(self, event: QEvent) -> None:
        super().changeEvent(event)
        if event.type() in {
            QEvent.Type.FontChange,
            QEvent.Type.ApplicationFontChange,
            QEvent.Type.StyleChange,
        }:
            # Wrapped height depends on the polished font, not the provisional
            # font Qt assigned while constructing this control.
            self.updateGeometry()

    def paintEvent(self, event: QPaintEvent) -> None:
        del event
        option = QStyleOptionButton()
        self.initStyleOption(option)
        option.text = ""
        painter = QStylePainter(self)
        painter.save()
        painter.drawControl(QStyle.ControlElement.CE_CheckBox, option)
        painter.restore()
        painter.setFont(self.font())
        self.style().drawItemText(
            painter,
            self.rect().adjusted(self._indicator_space(), 0, 0, 0),
            Qt.AlignmentFlag.AlignLeft
            | Qt.AlignmentFlag.AlignVCenter
            | Qt.TextFlag.TextWordWrap
            | Qt.TextFlag.TextWrapAnywhere,
            self.palette(),
            self.isEnabled(),
            self.text(),
            self.foregroundRole(),
        )


class ResponsiveChoiceGroup(QFrame):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.grid = QGridLayout(self)
        self.grid.setContentsMargins(10, 8, 10, 8)
        self.grid.setHorizontalSpacing(12)
        self.grid.setVerticalSpacing(8)
        self._choices: list[QCheckBox] = []
        self._columns = 0

    def add_choice(self, checkbox: QCheckBox) -> None:
        self._choices.append(checkbox)
        self._arrange(force=True)

    def _arrange(self, *, force=False) -> None:
        columns = 2 if self.width() >= 360 and len(self._choices) > 3 else 1
        if columns == self._columns and not force:
            return
        self._columns = columns
        for checkbox in self._choices:
            self.grid.removeWidget(checkbox)
        self.grid.setColumnStretch(0, 1)
        self.grid.setColumnStretch(1, int(columns == 2))
        for index, checkbox in enumerate(self._choices):
            self.grid.addWidget(checkbox, index // columns, index % columns)
        self.updateGeometry()

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        self._arrange()
