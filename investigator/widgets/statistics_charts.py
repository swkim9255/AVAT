"""Native, keyboard-accessible charts for accident statistics.

The widgets accept plain records so they can also render a saved or exported
snapshot without retaining case objects or a statistics engine.
"""

from __future__ import annotations

import math
from typing import Any

from PySide6.QtCore import QPointF, QRect, QRectF, QSize, Qt, Signal
from PySide6.QtGui import (
    QBrush,
    QColor,
    QFont,
    QKeyEvent,
    QMouseEvent,
    QPainter,
    QPainterPath,
    QPaintEvent,
    QPen,
)
from PySide6.QtWidgets import (
    QScrollArea,
    QSizePolicy,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QToolTip,
    QWidget,
)

from investigator.theme import BORDER, MUTED, PRIMARY, PRIMARY_SUBTLE, TEXT

_BAR = "#8CAEE3"
_DIM_BAR = "#CFDCEF"
_MISSING_BAR = "#C3C9D2"
_TRACK = "#F1F3F6"
_WRAP = Qt.TextFlag.TextWordWrap | Qt.TextFlag.TextWrapAnywhere
_CATEGORY_COLORS = ("#2F6BD8", "#E07B2E", "#1F9A86", "#8B5CC9", "#C94F7C")


def _number_text(value: float) -> str:
    if value and (abs(value) >= 100_000 or abs(value) < 0.001):
        return f"{value:.2g}"
    return f"{value:,.2f}".rstrip("0").rstrip(".")


def _numeric_axis(values: list[float]) -> tuple[float, float]:
    """Include zero when useful, with a finite range even for constant data."""
    if not values:
        return 0.0, 1.0
    low, high = min(0.0, min(values)), max(0.0, max(values))
    if low == high:
        return 0.0, 1.0
    span = high - low
    if not math.isfinite(span):
        return low, high
    raw_step = span / 4
    if raw_step <= 0:
        return low, high
    magnitude = 10.0 ** math.floor(math.log10(raw_step))
    step = next((n * magnitude for n in (1, 2, 5, 10) if n * magnitude >= raw_step), raw_step)
    start, end = math.floor(low / step) * step, math.ceil(high / step) * step
    return (start, end) if math.isfinite(start) and math.isfinite(end) else (low, high)


def _fraction(value: float, low: float, high: float) -> float:
    scale = max(abs(low), abs(high), 1.0)
    span = high / scale - low / scale
    return (value / scale - low / scale) / span if span else 0.5


class _InteractiveStatisticsChart(QWidget):
    """Shared native pointer, focus and keyboard behavior for new chart types."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._selected: int | None = None
        self._hovered: int | None = None
        self._focus_index = 0
        self.setMinimumWidth(0)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        policy = QSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)
        policy.setHeightForWidth(True)
        self.setSizePolicy(policy)

    def _indices(self) -> list[int]:
        return []

    def _item_rect(self, index: int) -> QRectF:
        return QRectF()

    def _index_at(self, point: QPointF) -> int | None:
        return next((i for i in self._indices() if self._item_rect(i).contains(point)), None)

    def _description_at(self, index: int) -> str:
        return ""

    def _activate(self, index: int) -> None:
        pass

    def _select_index(self, index: int | None) -> None:
        self._selected = index if index in self._indices() else None
        if self._selected is not None:
            self._focus_index = self._selected
        self.update()

    def set_selection(self, index: int | None) -> None:
        self._select_index(index)

    def _data_changed(self, empty_text: str = "표시할 데이터가 없습니다.") -> None:
        if self._hovered is not None:
            QToolTip.hideText()
        self._selected = self._hovered = None
        indices = self._indices()
        self._focus_index = indices[0] if indices else 0
        self.setToolTip("")
        self.setCursor(Qt.CursorShape.ArrowCursor)
        self.setAccessibleDescription(
            "; ".join(self._description_at(index) for index in indices) or empty_text
        )
        self.setMinimumHeight(self.heightForWidth(max(1, self.width())))
        self.updateGeometry()
        self.update()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self.setMinimumHeight(self.heightForWidth(max(1, self.width())))

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        index = self._index_at(event.position())
        self._hovered = index
        text = self._description_at(index) if index is not None else ""
        self.setToolTip(text)
        if text:
            QToolTip.showText(event.globalPosition().toPoint(), text, self)
        else:
            QToolTip.hideText()
        self.setCursor(
            Qt.CursorShape.PointingHandCursor if index is not None else Qt.CursorShape.ArrowCursor
        )
        self.update()
        super().mouseMoveEvent(event)

    def leaveEvent(self, event) -> None:
        self._hovered = None
        self.setToolTip("")
        QToolTip.hideText()
        self.setCursor(Qt.CursorShape.ArrowCursor)
        self.update()
        super().leaveEvent(event)

    def hideEvent(self, event) -> None:
        if self._hovered is not None:
            QToolTip.hideText()
        self._hovered = None
        super().hideEvent(event)

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            index = self._index_at(event.position())
            if index is not None:
                self.setFocus(Qt.FocusReason.MouseFocusReason)
                self._select_index(index)
                self._activate(index)
                event.accept()
                return
        super().mousePressEvent(event)

    def keyPressEvent(self, event: QKeyEvent) -> None:
        indices = self._indices()
        if not indices:
            super().keyPressEvent(event)
            return
        key = event.key()
        position = indices.index(self._focus_index) if self._focus_index in indices else 0
        if key in {Qt.Key.Key_Right, Qt.Key.Key_Down}:
            self._focus_index = indices[min(len(indices) - 1, position + 1)]
        elif key in {Qt.Key.Key_Left, Qt.Key.Key_Up}:
            self._focus_index = indices[max(0, position - 1)]
        elif key == Qt.Key.Key_Home:
            self._focus_index = indices[0]
        elif key == Qt.Key.Key_End:
            self._focus_index = indices[-1]
        elif key in {Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Space}:
            self._select_index(self._focus_index)
            self._activate(self._focus_index)
        elif key == Qt.Key.Key_Escape:
            self._select_index(None)
        else:
            super().keyPressEvent(event)
            return
        event.accept()
        self._ensure_focus_visible()
        self.update()

    def _ensure_focus_visible(self) -> None:
        ancestor = self.parentWidget()
        while ancestor is not None:
            if isinstance(ancestor, QScrollArea) and ancestor.widget() is not None:
                point = self.mapTo(
                    ancestor.widget(), self._item_rect(self._focus_index).center().toPoint()
                )
                ancestor.ensureVisible(point.x(), point.y(), 12, 24)
                return
            ancestor = ancestor.parentWidget()

    def focusInEvent(self, event) -> None:
        super().focusInEvent(event)
        self._ensure_focus_visible()
        self.update()

    def focusOutEvent(self, event) -> None:
        super().focusOutEvent(event)
        self.update()

    def _paint_item_focus(self, painter: QPainter, index: int, rect: QRectF) -> None:
        if self.hasFocus() and index == self._focus_index:
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setPen(QPen(QColor(PRIMARY), 1, Qt.PenStyle.DashLine))
            painter.drawRoundedRect(rect.adjusted(1, 1, -1, -1), 5, 5)


class StatisticsButterflyChart(_InteractiveStatisticsChart):
    """Mirrored bars with one shared count/percentage scale for both cohorts."""

    bucket_clicked = Signal(int, int)  # row, side (0 left, 1 right, 2 both)
    LEFT_COLOR = "#2F63D6"
    RIGHT_COLOR = "#D2691E"

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._rows: list[dict[str, Any]] = []
        self._totals = (0, 0)
        self._labels = ("왼쪽", "오른쪽")
        self._unit = "percent"
        self._axis_label = ""
        self.setAccessibleName("나비형 비교 차트")

    def set_data(
        self, rows: list[dict[str, Any]], *, left_label: str, right_label: str,
        left_total: int, right_total: int, unit: str = "percent", axis_label: str = "",
    ) -> None:
        if unit not in {"percent", "count"}:
            raise ValueError("나비형 비교 단위는 percent 또는 count여야 합니다.")
        totals = (int(left_total), int(right_total))
        if min(totals) < 0:
            raise ValueError("그룹의 사고 수는 0 이상이어야 합니다.")
        normalized = []
        for row in rows:
            counts = (int(row["left_count"]), int(row["right_count"]))
            if any(count < 0 or count > totals[side] for side, count in enumerate(counts)):
                raise ValueError("구간 건수는 각 그룹의 사고 수를 넘을 수 없습니다.")
            normalized.append({**row, "label": str(row["label"]), "left_count": counts[0], "right_count": counts[1]})
        self._rows, self._totals, self._unit = normalized, totals, unit
        self._labels, self._axis_label = (str(left_label), str(right_label)), str(axis_label)
        self._data_changed()

    def _indices(self) -> list[int]:
        return list(range(len(self._rows) * 3))

    def _activate(self, index: int) -> None:
        row, position = divmod(index, 3)
        self.bucket_clicked.emit(row, (0, 2, 1)[position])

    def set_selection(self, row: int | None, side: int = 2) -> None:
        self._select_index(None if row is None else row * 3 + (0, 2, 1)[side])

    def _count(self, row: int, side: int) -> int:
        return self._rows[row]["left_count" if side == 0 else "right_count"]

    def _percent(self, row: int, side: int) -> float:
        return self._count(row, side) * 100 / self._totals[side] if self._totals[side] else 0.0

    def _value(self, row: int, side: int) -> float:
        return self._percent(row, side) if self._unit == "percent" else self._count(row, side)

    def _description_at(self, index: int) -> str:
        row, position = divmod(index, 3)
        sides = (0, 1) if position == 1 else (0 if position == 0 else 1,)
        return self._rows[row]["label"] + " · " + " / ".join(
            f"{self._labels[side]} {self._count(row, side):,}건 ({self._percent(row, side):.1f}%, 분모 {self._totals[side]:,}건)"
            for side in sides
        )

    @staticmethod
    def _center_width(width: int) -> int:
        return min(160, max(58, int(width * 0.20)))

    def _side_width(self, width: int) -> float:
        return max(1.0, (width - self._center_width(width) - 32) / 2)

    def _header_height(self, width: int) -> int:
        metrics = self.fontMetrics()
        height = max(
            *(metrics.boundingRect(QRect(0, 0, int(self._side_width(width)), 100000), _WRAP, text).height() for text in self._labels),
            metrics.boundingRect(QRect(0, 0, self._center_width(width) - 8, 100000), _WRAP, self._axis_label).height(),
        )
        return max(46, height + 28)

    def _row_heights(self, width: int) -> list[int]:
        return [max(38, self.fontMetrics().boundingRect(
            QRect(0, 0, self._center_width(width) - 8, 100000), _WRAP, row["label"],
        ).height() + 12) for row in self._rows]

    def heightForWidth(self, width: int) -> int:
        return self._header_height(width) + sum(self._row_heights(width)) + 48 if self._rows else 120

    def sizeHint(self) -> QSize:
        return QSize(680, self.heightForWidth(max(1, self.width())))

    def minimumSizeHint(self) -> QSize:
        return QSize(0, self.heightForWidth(max(1, self.width())))

    def row_rect(self, row: int) -> QRectF:
        if not 0 <= row < len(self._rows):
            return QRectF()
        heights = self._row_heights(self.width())
        return QRectF(8, self._header_height(self.width()) + sum(heights[:row]), max(1, self.width() - 16), heights[row])

    def cell_rect(self, row: int, side: int) -> QRectF:
        rect = self.row_rect(row)
        side_width = self._side_width(self.width())
        center_width = self._center_width(self.width())
        if side == 0:
            return QRectF(rect.left(), rect.top(), side_width, rect.height())
        if side == 1:
            return QRectF(rect.left() + side_width + center_width + 16, rect.top(), side_width, rect.height())
        return QRectF(rect.left() + side_width + 8, rect.top(), center_width, rect.height())

    def _item_rect(self, index: int) -> QRectF:
        row, position = divmod(index, 3)
        return self.cell_rect(row, (0, 2, 1)[position])

    def _scale_top(self) -> float:
        values = [self._value(i, side) for i in range(len(self._rows)) for side in (0, 1)]
        return _numeric_axis(values)[1]

    def _bar_width(self) -> float:
        return max(1.0, self._side_width(self.width()) - 58)

    def bar_rect(self, row: int, side: int) -> QRectF:
        cell = self.cell_rect(row, side)
        width = self._bar_width() * self._value(row, side) / self._scale_top()
        left = cell.right() - width if side == 0 else cell.left()
        return QRectF(left, cell.center().y() - 11, width, 22)

    def paintEvent(self, event: QPaintEvent) -> None:
        del event
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        if not self._rows:
            painter.setPen(QColor(MUTED))
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter | _WRAP, "비교할 데이터가 없습니다.")
            return
        first, last = self.row_rect(0), self.row_rect(len(self._rows) - 1)
        top, bar_width = self._scale_top(), self._bar_width()
        header_height = self._header_height(self.width())
        center = self.cell_rect(0, 2)
        painter.setPen(QColor(MUTED))
        painter.drawText(QRectF(center.left(), 2, center.width(), header_height - 8), Qt.AlignmentFlag.AlignCenter | _WRAP, self._axis_label)
        tick_count = 4 if self.width() >= 560 else 2 if self.width() >= 380 else 1
        for side in (0, 1):
            area = self.cell_rect(0, side)
            color = QColor(self.LEFT_COLOR if side == 0 else self.RIGHT_COLOR)
            painter.setPen(color)
            painter.drawText(QRectF(area.left(), 2, area.width(), header_height - 24), Qt.AlignmentFlag.AlignCenter | _WRAP, self._labels[side])
            painter.setPen(QColor(MUTED))
            painter.drawText(QRectF(area.left(), header_height - 24, area.width(), 20), Qt.AlignmentFlag.AlignCenter, f"n={self._totals[side]:,}")
            edge = area.right() if side == 0 else area.left()
            for tick in range(tick_count + 1):
                fraction = tick / tick_count
                x = edge + (-1 if side == 0 else 1) * bar_width * fraction
                painter.setPen(QPen(QColor(BORDER), 1, Qt.PenStyle.DashLine))
                painter.drawLine(QPointF(x, first.top()), QPointF(x, last.bottom()))
                painter.setPen(QColor(MUTED))
                text = _number_text(top * fraction) + ("%" if self._unit == "percent" else "")
                painter.drawText(QRectF(x - 28, last.bottom() + 8, 56, 20), Qt.AlignmentFlag.AlignCenter, text)
        selected_row = None if self._selected is None else self._selected // 3
        hovered_row = None if self._hovered is None else self._hovered // 3
        for index, row in enumerate(self._rows):
            center = self.cell_rect(index, 2).adjusted(0, 3, 0, -3)
            active = index in {selected_row, hovered_row}
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(TEXT if active else _TRACK))
            painter.drawRoundedRect(center, 4, 4)
            painter.setPen(QColor("white" if active else MUTED if row.get("missing") else TEXT))
            painter.drawText(center.adjusted(4, 0, -4, 0), Qt.AlignmentFlag.AlignCenter | _WRAP, row["label"])
            for side in (0, 1):
                bar, cell = self.bar_rect(index, side), self.cell_rect(index, side)
                color = QColor(self.LEFT_COLOR if side == 0 else self.RIGHT_COLOR)
                if selected_row is not None and selected_row != index:
                    color.setAlphaF(0.35)
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(color)
                if bar.width() > 0:
                    painter.drawRoundedRect(bar, 3, 3)
                text = f"{self._percent(index, side):.1f}%" if self._unit == "percent" else f"{self._count(index, side):,}"
                left = max(cell.left(), bar.left() - 58) if side == 0 else min(bar.right() + 4, cell.right() - 54)
                painter.setPen(QColor(MUTED))
                painter.drawText(QRectF(left, cell.top(), min(54, cell.width()), cell.height()), Qt.AlignmentFlag.AlignVCenter | (Qt.AlignmentFlag.AlignRight if side == 0 else Qt.AlignmentFlag.AlignLeft), text)
        if self.hasFocus() and self._focus_index in self._indices():
            self._paint_item_focus(painter, self._focus_index, self._item_rect(self._focus_index))


class StatisticsDonutChart(_InteractiveStatisticsChart):
    """Single-choice proportions with clickable slices and a wrapping legend."""

    category_clicked = Signal(int)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._rows: list[dict[str, Any]] = []
        self._denominator = 0
        self.setAccessibleName("범주별 도넛 비율 그래프")

    def set_data(self, rows: list[dict[str, Any]], *, denominator: int) -> None:
        self._rows = [
            {**row, "label": str(row.get("label", "")), "count": max(0, int(row["count"]))}
            for row in rows
        ]
        self._denominator = max(0, int(denominator))
        self._data_changed()

    def _indices(self) -> list[int]:
        return list(range(len(self._rows)))

    def _description_at(self, index: int) -> str:
        row = self._rows[index]
        percent = row["count"] / self._denominator * 100 if self._denominator else 0
        return f"{row['label']} · {row['count']:,}건 · {percent:.1f}%"

    def _activate(self, index: int) -> None:
        self.category_clicked.emit(index)

    def _layout_geometry(self, width: int) -> tuple[QRectF, list[QRectF]]:
        width = max(1, width)
        beside = width >= 560
        diameter = min(240, max(20, width - 16))
        circle = QRectF(4 if beside else (width - diameter) / 2, 8, diameter, diameter)
        legend_x = diameter + 28 if beside else 0
        legend_width = max(1, width - legend_x)
        y = 8.0 if beside else circle.bottom() + 16
        rects = []
        for row in self._rows:
            compact = legend_width < 260
            label_width = max(20, legend_width - (40 if compact else 140))
            height = max(
                38,
                self.fontMetrics()
                .boundingRect(QRect(0, 0, int(label_width), 100000), _WRAP, row["label"])
                .height()
                + (38 if compact else 14),
            )
            rects.append(QRectF(legend_x, y, legend_width, height))
            y += height + 2
        return circle, rects

    def heightForWidth(self, width: int) -> int:
        circle, rects = self._layout_geometry(width)
        return math.ceil(max(circle.bottom(), rects[-1].bottom() if rects else 0) + 10)

    def sizeHint(self) -> QSize:
        return QSize(600, self.heightForWidth(max(1, self.width())))

    def minimumSizeHint(self) -> QSize:
        return QSize(0, self.heightForWidth(max(1, self.width())))

    def category_rect(self, index: int) -> QRectF:
        rects = self._layout_geometry(self.width())[1]
        return rects[index] if 0 <= index < len(rects) else QRectF()

    def _item_rect(self, index: int) -> QRectF:
        return self.category_rect(index)

    def slice_path(self, index: int) -> QPainterPath:
        path = QPainterPath()
        total = sum(row["count"] for row in self._rows)
        if not 0 <= index < len(self._rows) or not total or not self._rows[index]["count"]:
            return path
        circle = self._layout_geometry(self.width())[0]
        start = sum(row["count"] for row in self._rows[:index]) / total * 360
        span = self._rows[index]["count"] / total * 360
        path.moveTo(circle.center())
        path.arcTo(circle, 90 - start, -span)
        path.closeSubpath()
        hole = QPainterPath()
        margin = circle.width() * 0.24
        hole.addEllipse(circle.adjusted(margin, margin, -margin, -margin))
        return path.subtracted(hole)

    def _index_at(self, point: QPointF) -> int | None:
        legend = super()._index_at(point)
        if legend is not None:
            return legend
        return next((i for i in self._indices() if self.slice_path(i).contains(point)), None)

    def _color(self, index: int) -> QColor:
        color = QColor(
            _MISSING_BAR if self._rows[index].get("missing") else _CATEGORY_COLORS[index % 5]
        )
        if self._selected is not None and self._selected != index:
            color.setAlphaF(0.3)
        return color

    def paintEvent(self, event: QPaintEvent) -> None:
        del event
        painter = QPainter(self)
        painter.setRenderHints(
            QPainter.RenderHint.Antialiasing | QPainter.RenderHint.TextAntialiasing
        )
        circle, rects = self._layout_geometry(self.width())
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor("#E4E7EB"))
        painter.drawEllipse(circle)
        for index in self._indices():
            painter.setBrush(self._color(index))
            painter.setPen(
                QPen(QColor(PRIMARY), 2)
                if index in {self._selected, self._hovered}
                else QPen(Qt.PenStyle.NoPen)
            )
            painter.drawPath(self.slice_path(index))
        margin = circle.width() * 0.24
        hole = circle.adjusted(margin, margin, -margin, -margin)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor("#FFFFFF"))
        painter.drawEllipse(hole)
        painter.setPen(QColor(MUTED))
        if hole.width() >= 90:
            painter.drawText(hole.adjusted(4, 0, -4, -8), Qt.AlignmentFlag.AlignCenter, "비율 분모")
        font = QFont(painter.font())
        font.setPixelSize(max(12, min(22, round(hole.width() / 5))))
        font.setBold(True)
        painter.setFont(font)
        painter.setPen(QColor(TEXT))
        painter.drawText(
            hole.adjusted(4, 28 if hole.width() >= 90 else 0, -4, 0),
            Qt.AlignmentFlag.AlignCenter,
            f"{self._denominator:,}건",
        )
        painter.setFont(self.font())
        for index, (row, rect) in enumerate(zip(self._rows, rects, strict=True)):
            compact = rect.width() < 260
            label_height = rect.height() - 24 if compact else rect.height()
            if index in {self._hovered, self._selected}:
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(QColor(PRIMARY_SUBTLE))
                painter.drawRoundedRect(rect, 6, 6)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(self._color(index))
            painter.drawRoundedRect(
                QRectF(rect.x() + 8, rect.y() + label_height / 2 - 6, 12, 12), 3, 3
            )
            percent = row["count"] / self._denominator * 100 if self._denominator else 0
            painter.setPen(QColor(PRIMARY if index == self._selected else TEXT))
            painter.drawText(
                QRectF(
                    rect.x() + 30,
                    rect.y() + 4,
                    max(1, rect.width() - (40 if compact else 140)),
                    label_height - 8,
                ),
                Qt.AlignmentFlag.AlignVCenter | _WRAP,
                row["label"],
            )
            value_y = rect.bottom() - 24 if compact else rect.y()
            value_height = 24 if compact else rect.height()
            value_width = max(1, (rect.width() - 40) / 2) if compact else 54
            percent_rect = QRectF(
                rect.x() + 30 if compact else rect.right() - 108, value_y, value_width, value_height
            )
            count_rect = QRectF(
                rect.right() - value_width - 4 if compact else rect.right() - 52,
                value_y,
                value_width if compact else 48,
                value_height,
            )
            painter.drawText(
                percent_rect,
                Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                self.fontMetrics().elidedText(
                    f"{percent:.1f}%", Qt.TextElideMode.ElideRight, int(percent_rect.width())
                ),
            )
            painter.setPen(QColor(MUTED))
            painter.drawText(
                count_rect,
                Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                self.fontMetrics().elidedText(
                    f"{row['count']:,}건", Qt.TextElideMode.ElideRight, int(count_rect.width())
                ),
            )
            self._paint_item_focus(painter, index, rect)


class StatisticsStackedChart(_InteractiveStatisticsChart):
    """100% row compositions; segment geometry uses the row's selection sum."""

    cell_clicked = Signal(int, int)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._rows: list[dict[str, Any]] = []
        self._columns: list[str] = []
        self._ordered = False
        self.setAccessibleName("범주별 100% 누적 막대 그래프")

    def set_data(
        self, rows: list[dict[str, Any]], columns: list[str], *, ordered: bool = False
    ) -> None:
        normalized = []
        for row in rows:
            counts = [max(0, int(value)) for value in row.get("counts", [])]
            if len(counts) != len(columns):
                raise ValueError("누적 막대의 각 행은 열 수와 같은 개수의 건수가 필요합니다.")
            normalized.append(
                {
                    **row,
                    "label": str(row.get("label", "")),
                    "counts": counts,
                    "total": max(0, int(row.get("total", sum(counts)))),
                }
            )
        self._rows, self._columns, self._ordered = (
            normalized,
            list(map(str, columns)),
            bool(ordered),
        )
        self._data_changed()

    def _indices(self) -> list[int]:
        return [
            i * len(self._columns) + j
            for i, row in enumerate(self._rows)
            for j, count in enumerate(row["counts"])
            if count > 0
        ]

    def _cell(self, index: int) -> tuple[int, int]:
        return divmod(index, max(1, len(self._columns)))

    def _description_at(self, index: int) -> str:
        i, j = self._cell(index)
        row = self._rows[i]
        denominator = sum(row["counts"])
        percent = row["counts"][j] / denominator * 100 if denominator else 0
        suffix = " · 행 선택 합계 기준" if denominator != row["total"] else ""
        return (
            f"{row['label']} · {self._columns[j]} · {row['counts'][j]:,}건 · "
            f"{percent:.1f}% · n={row['total']:,}{suffix}"
        )

    def _activate(self, index: int) -> None:
        self.cell_clicked.emit(*self._cell(index))

    def set_selection(self, row: int | tuple[int, int] | None, column: int | None = None) -> None:
        if isinstance(row, tuple):
            row, column = row
        index = (
            row * len(self._columns) + column if row is not None and column is not None else None
        )
        self._select_index(index)

    def _row_rects(self, width: int) -> list[QRectF]:
        label_width = max(20, min(130, round(width * 0.24)))
        y, rects = 6.0, []
        for row in self._rows:
            height = max(
                46,
                self.fontMetrics()
                .boundingRect(QRect(0, 0, label_width, 100000), _WRAP, row["label"])
                .height()
                + 12,
            )
            rects.append(QRectF(0, y, max(1, width), height))
            y += height + 6
        return rects

    def _legend_rects(self, width: int) -> list[QRectF]:
        rows = self._row_rects(width)
        x, y = 4, (rows[-1].bottom() + 16 if rows else 12)
        rects = []
        for column in self._columns:
            item_width = min(max(1, width - 8), self.fontMetrics().horizontalAdvance(column) + 34)
            height = max(
                26,
                self.fontMetrics()
                .boundingRect(QRect(0, 0, max(1, item_width - 22), 100000), _WRAP, column)
                .height()
                + 6,
            )
            if x > 4 and x + item_width > width - 4:
                x, y = 4, max(rect.bottom() for rect in rects) + 4
            rects.append(QRectF(x, y, item_width, height))
            x += item_width + 10
        return rects

    def heightForWidth(self, width: int) -> int:
        rows, legends = self._row_rects(width), self._legend_rects(width)
        return math.ceil(
            max([120 if not self._rows else 0] + [r.bottom() for r in rows + legends]) + 8
        )

    def sizeHint(self) -> QSize:
        return QSize(600, self.heightForWidth(self.width()))

    def minimumSizeHint(self) -> QSize:
        return QSize(0, self.heightForWidth(self.width()))

    def cell_rect(self, row: int, column: int) -> QRectF:
        if not 0 <= row < len(self._rows) or not 0 <= column < len(self._columns):
            return QRectF()
        rect = self._row_rects(self.width())[row]
        values = self._rows[row]["counts"]
        total = sum(values)
        if not total or not values[column]:
            return QRectF()
        label_width = max(20, min(130, round(self.width() * 0.24)))
        suffix = min(58, max(36, round(self.width() * 0.15)))
        start, plot_width = label_width + 10, max(1, self.width() - label_width - suffix - 22)
        return QRectF(
            start + plot_width * sum(values[:column]) / total,
            rect.center().y() - 17,
            plot_width * values[column] / total,
            34,
        )

    def _item_rect(self, index: int) -> QRectF:
        return self.cell_rect(*self._cell(index))

    def column_color(self, column: int) -> QColor:
        if self._columns[column] in {"미입력", "형식 오류"}:
            return QColor(_MISSING_BAR)
        if not self._ordered:
            return QColor(_CATEGORY_COLORS[column % len(_CATEGORY_COLORS)])
        color = QColor(PRIMARY)
        alpha = 0.18 + 0.72 * column / max(1, len(self._columns) - 1)
        return QColor(
            round(color.red() * alpha + 255 * (1 - alpha)),
            round(color.green() * alpha + 255 * (1 - alpha)),
            round(color.blue() * alpha + 255 * (1 - alpha)),
        )

    def paintEvent(self, event: QPaintEvent) -> None:
        del event
        painter = QPainter(self)
        painter.setRenderHints(
            QPainter.RenderHint.Antialiasing | QPainter.RenderHint.TextAntialiasing
        )
        if not self._rows:
            painter.setPen(QColor(MUTED))
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "표시할 데이터가 없습니다.")
            return
        label_width = max(20, min(130, round(self.width() * 0.24)))
        suffix = min(58, max(36, round(self.width() * 0.15)))
        for i, (row, rect) in enumerate(
            zip(self._rows, self._row_rects(self.width()), strict=True)
        ):
            painter.setPen(QColor(TEXT))
            painter.drawText(
                QRectF(2, rect.y(), label_width, rect.height()),
                Qt.AlignmentFlag.AlignVCenter | _WRAP,
                row["label"],
            )
            painter.setPen(QColor(MUTED))
            painter.drawText(
                QRectF(self.width() - suffix, rect.y(), suffix - 2, rect.height()),
                Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter | _WRAP,
                f"n={row['total']:,}",
            )
            track = QRectF(
                label_width + 10,
                rect.center().y() - 17,
                max(1, self.width() - label_width - suffix - 22),
                34,
            )
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(_TRACK))
            painter.drawRoundedRect(track, 5, 5)
            painter.save()
            clip = QPainterPath()
            clip.addRoundedRect(track, 5, 5)
            painter.setClipPath(clip)
            for j, count in enumerate(row["counts"]):
                if not count:
                    continue
                index = i * len(self._columns) + j
                segment = self.cell_rect(i, j)
                color = self.column_color(j)
                if self._selected is not None and index != self._selected:
                    color.setAlphaF(0.35)
                painter.fillRect(segment, color)
                text = f"{count / sum(row['counts']) * 100:.0f}%"
                if segment.width() >= self.fontMetrics().horizontalAdvance(text) + 8:
                    painter.setPen(QColor("#FFFFFF" if color.lightnessF() < 0.63 else "#0E1E36"))
                    painter.drawText(segment, Qt.AlignmentFlag.AlignCenter, text)
            painter.restore()
        for index in self._indices():
            rect = self._item_rect(index)
            if index in {self._selected, self._hovered}:
                painter.setBrush(Qt.BrushStyle.NoBrush)
                painter.setPen(QPen(QColor(TEXT if index == self._selected else PRIMARY), 2))
                painter.drawRect(rect.adjusted(1, 1, -1, -1))
            self._paint_item_focus(painter, index, rect)
        for j, rect in enumerate(self._legend_rects(self.width())):
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(self.column_color(j))
            painter.drawRoundedRect(QRectF(rect.x(), rect.y() + 6, 12, 12), 3, 3)
            painter.setPen(QColor(TEXT))
            painter.drawText(
                rect.adjusted(20, 0, 0, 0), Qt.AlignmentFlag.AlignVCenter | _WRAP, self._columns[j]
            )


class StatisticsScatterPlot(_InteractiveStatisticsChart):
    """Finite numeric pairs, actual accident IDs and an optional supplied fit."""

    point_clicked = Signal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._points: list[dict[str, Any]] = []
        self._x_label = self._y_label = ""
        self._x_bounds = self._y_bounds = (0.0, 1.0)
        self._trend: tuple[float, float] | None = None
        self.setAccessibleName("사고별 수치 산점도")

    def set_data(
        self,
        points: list[dict[str, Any]],
        *,
        x_label: str,
        y_label: str,
        slope: float | None = None,
        intercept: float | None = None,
    ) -> None:
        normalized = []
        for point in points:
            try:
                x, y = float(point["x"]), float(point["y"])
            except (KeyError, TypeError, ValueError, OverflowError):
                continue
            if not math.isfinite(x) or not math.isfinite(y) or not point.get("uid"):
                continue
            normalized.append(
                {
                    **point,
                    "x": x,
                    "y": y,
                    "uid": str(point["uid"]),
                    "label": str(point.get("label", point["uid"])),
                }
            )
        self._points = normalized
        self._x_label, self._y_label = str(x_label), str(y_label)
        self._x_bounds = _numeric_axis([p["x"] for p in normalized])
        self._y_bounds = _numeric_axis([p["y"] for p in normalized])
        self._trend = None
        if slope is not None and intercept is not None:
            try:
                fit = float(slope), float(intercept)
                if all(math.isfinite(value) for value in fit):
                    self._trend = fit
            except (TypeError, ValueError, OverflowError):
                pass
        self._data_changed()

    def _indices(self) -> list[int]:
        return list(range(len(self._points)))

    def _description_at(self, index: int) -> str:
        point = self._points[index]
        return f"{point['label']} · {self._x_label} {_number_text(point['x'])} · {self._y_label} {_number_text(point['y'])}"

    def _activate(self, index: int) -> None:
        self.point_clicked.emit(self._points[index]["uid"])

    def set_selection(self, uid: str | None) -> None:
        self._select_index(
            next((i for i, point in enumerate(self._points) if point["uid"] == uid), None)
        )

    def heightForWidth(self, width: int) -> int:
        return max(300, min(420, round(width * 0.55) + 90))

    def sizeHint(self) -> QSize:
        return QSize(600, 380)

    def minimumSizeHint(self) -> QSize:
        return QSize(0, self.heightForWidth(self.width()))

    def plot_rect(self) -> QRectF:
        metrics = self.fontMetrics()
        title_height = max(
            22,
            min(
                48,
                metrics.boundingRect(
                    QRect(0, 0, max(1, self.width() - 12), 100000), _WRAP, self._y_label
                ).height(),
            ),
        )
        x_height = max(
            22,
            min(
                48,
                metrics.boundingRect(
                    QRect(0, 0, max(1, self.width() - 12), 100000), _WRAP, self._x_label
                ).height(),
            ),
        )
        left = min(64, max(32, round(self.width() * 0.14)))
        return QRectF(
            left,
            title_height + 10,
            max(1, self.width() - left - 16),
            max(1, self.height() - title_height - x_height - 50),
        )

    def _map(self, x: float, y: float) -> QPointF:
        plot = self.plot_rect()
        return QPointF(
            plot.x() + plot.width() * _fraction(x, *self._x_bounds),
            plot.bottom() - plot.height() * _fraction(y, *self._y_bounds),
        )

    def point_rect(self, index: int) -> QRectF:
        if not 0 <= index < len(self._points):
            return QRectF()
        point = self._points[index]
        position = self._map(point["x"], point["y"])
        return QRectF(position.x() - 7, position.y() - 7, 14, 14)

    def _item_rect(self, index: int) -> QRectF:
        return self.point_rect(index)

    def _index_at(self, point: QPointF) -> int | None:
        distances = [
            ((point.x() - rect.center().x()) ** 2 + (point.y() - rect.center().y()) ** 2, i)
            for i in self._indices()
            if (rect := self.point_rect(i)).contains(point)
        ]
        return min(distances)[1] if distances and min(distances)[0] <= 49 else None

    def trend_line(self) -> tuple[QPointF, QPointF] | None:
        if self._trend is None or not self._points:
            return None
        slope, intercept = self._trend
        low_x, high_x = self._x_bounds
        low_y, high_y = self._y_bounds
        points = []
        for x in (low_x, high_x):
            y = slope * x + intercept
            if math.isfinite(y) and low_y <= y <= high_y:
                points.append((x, y))
        if slope:
            for y in (low_y, high_y):
                x = (y - intercept) / slope
                if math.isfinite(x) and low_x <= x <= high_x and (x, y) not in points:
                    points.append((x, y))
        return (self._map(*points[0]), self._map(*points[-1])) if len(points) >= 2 else None

    def paintEvent(self, event: QPaintEvent) -> None:
        del event
        painter = QPainter(self)
        painter.setRenderHints(
            QPainter.RenderHint.Antialiasing | QPainter.RenderHint.TextAntialiasing
        )
        if not self._points:
            painter.setPen(QColor(MUTED))
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "표시할 데이터가 없습니다.")
            return
        plot = self.plot_rect()
        painter.setPen(QColor(TEXT))
        painter.drawText(
            QRectF(4, 0, max(1, self.width() - 8), plot.top() - 5),
            Qt.AlignmentFlag.AlignVCenter | _WRAP,
            "↑ " + self._y_label,
        )
        painter.drawText(
            QRectF(
                4, plot.bottom() + 29, max(1, self.width() - 8), self.height() - plot.bottom() - 29
            ),
            Qt.AlignmentFlag.AlignRight | _WRAP,
            self._x_label + " →",
        )
        painter.save()
        tick_font = QFont(painter.font())
        tick_font.setPixelSize(11)
        painter.setFont(tick_font)
        for fraction in (0, 0.25, 0.5, 0.75, 1):
            x, y = plot.left() + plot.width() * fraction, plot.bottom() - plot.height() * fraction
            painter.setPen(QColor("#EEF0F3"))
            painter.drawLine(QPointF(x, plot.top()), QPointF(x, plot.bottom()))
            painter.drawLine(QPointF(plot.left(), y), QPointF(plot.right(), y))
            painter.setPen(QColor(MUTED))
            x_value = self._x_bounds[0] * (1 - fraction) + self._x_bounds[1] * fraction
            y_value = self._y_bounds[0] * (1 - fraction) + self._y_bounds[1] * fraction
            painter.drawText(
                QRectF(
                    max(0, x - 28), plot.bottom() + 5, min(56, self.width() - max(0, x - 28)), 20
                ),
                Qt.AlignmentFlag.AlignHCenter,
                _number_text(x_value),
            )
            painter.drawText(
                QRectF(0, y - 9, max(1, plot.left() - 8), 20),
                Qt.AlignmentFlag.AlignRight,
                _number_text(y_value),
            )
        painter.restore()
        painter.setPen(QPen(QColor("#8A93A0"), 1))
        painter.drawLine(plot.bottomLeft(), plot.bottomRight())
        painter.drawLine(plot.topLeft(), plot.bottomLeft())
        painter.save()
        painter.setClipRect(plot.adjusted(-10, -10, 10, 10))
        if line := self.trend_line():
            painter.setPen(QPen(QColor("#D9822B"), 2, Qt.PenStyle.DashLine))
            painter.drawLine(*line)
        indices = [i for i in self._indices() if i != self._selected]
        if self._selected is not None:
            indices.append(self._selected)
        for index in indices:
            center = self.point_rect(index).center()
            selected = index == self._selected
            color = QColor(PRIMARY)
            color.setAlphaF(1 if selected else (0.2 if self._selected is not None else 0.55))
            painter.setBrush(color)
            painter.setPen(QPen(QColor("#FFFFFF"), 2) if selected else QPen(Qt.PenStyle.NoPen))
            painter.drawEllipse(center, 6 if selected else 4, 6 if selected else 4)
            if (
                selected
                or index == self._hovered
                or (self.hasFocus() and index == self._focus_index)
            ):
                painter.setBrush(Qt.BrushStyle.NoBrush)
                painter.setPen(
                    QPen(
                        QColor(PRIMARY),
                        1.5,
                        Qt.PenStyle.DashLine
                        if not selected and index != self._hovered
                        else Qt.PenStyle.SolidLine,
                    )
                )
                painter.drawEllipse(center, 8, 8)
        painter.restore()


class StatisticsHeatmapDelegate(QStyledItemDelegate):
    """Paint heatmap cells explicitly so application item QSS cannot mask them."""

    def paint(self, painter: QPainter, option: QStyleOptionViewItem, index) -> None:
        background = index.data(Qt.ItemDataRole.BackgroundRole)
        if isinstance(background, QBrush) and background.style() != Qt.BrushStyle.NoBrush:
            color = background.color()
        elif isinstance(background, QColor) and background.isValid():
            color = background
        else:
            super().paint(painter, option, index)
            return
        item = QStyleOptionViewItem(option)
        self.initStyleOption(item, index)
        alpha = color.alphaF()
        # A solid color also survives transparent viewports and image export.
        fill = QColor(
            round(color.red() * alpha + 255 * (1 - alpha)),
            round(color.green() * alpha + 255 * (1 - alpha)),
            round(color.blue() * alpha + 255 * (1 - alpha)),
        )
        rect = QRectF(item.rect).adjusted(2, 2, -2, -2)
        painter.save()
        try:
            painter.setClipRect(item.rect)
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            painter.fillRect(item.rect, QColor("#FFFFFF"))
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(fill)
            painter.drawRoundedRect(rect, 5, 5)
            painter.setFont(item.font)
            foreground = index.data(Qt.ItemDataRole.ForegroundRole)
            text_color = foreground.color() if isinstance(foreground, QBrush) else QColor(TEXT)
            painter.setPen(
                text_color if item.state & QStyle.StateFlag.State_Enabled else QColor(MUTED)
            )
            painter.drawText(
                item.rect.adjusted(6, 3, -6, -3),
                Qt.AlignmentFlag.AlignCenter | _WRAP,
                item.text,
            )
            selected = bool(item.state & QStyle.StateFlag.State_Selected)
            focused = bool(item.state & QStyle.StateFlag.State_HasFocus)
            hovered = bool(item.state & QStyle.StateFlag.State_MouseOver)
            if selected or focused or hovered:
                painter.setBrush(Qt.BrushStyle.NoBrush)
                painter.setPen(
                    QPen(
                        QColor(PRIMARY if selected or focused else MUTED),
                        2 if selected else 1,
                        Qt.PenStyle.DashLine if focused and not selected else Qt.PenStyle.SolidLine,
                    )
                )
                painter.drawRoundedRect(rect, 5, 5)
        finally:
            painter.restore()


class StatisticsBarChart(QWidget):
    """Clickable horizontal categories or vertical histogram buckets.

    ``rows`` contain ``label`` and ``count`` with optional ``missing``, ``value``
    and ``uids`` entries. Activation emits the index in the supplied row list.
    Put a long horizontal chart inside a resizable ``QScrollArea``; its minimum
    height follows its wrapped labels and every category remains reachable.
    """

    bucket_clicked = Signal(int)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._rows: list[dict[str, Any]] = []
        self._denominator = 0
        self._mode = "count"
        self._vertical = False
        self._selected: int | None = None
        self._hovered: int | None = None
        self._focus_index = 0
        policy = QSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)
        policy.setHeightForWidth(True)
        self.setSizePolicy(policy)
        self.setMinimumWidth(0)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setMouseTracking(True)
        self.setAccessibleName("통계 막대 그래프")

    def set_data(
        self,
        rows: list[dict[str, Any]],
        denominator: int,
        mode: str = "count",
        vertical: bool = False,
    ) -> None:
        if mode not in {"count", "percent"}:
            raise ValueError("표시 방식은 count 또는 percent여야 합니다.")
        self._rows = [
            {**row, "label": str(row.get("label", "")), "count": max(0, int(row["count"]))}
            for row in rows
        ]
        self._denominator = max(0, int(denominator))
        self._mode = mode
        self._vertical = bool(vertical)
        self._selected = None
        self._hovered = None
        self._focus_index = 0
        self.setToolTip("")
        self.setAccessibleDescription(
            "; ".join(self._description(index) for index in range(len(self._rows)))
            or "표시할 데이터가 없습니다."
        )
        self._sync_height()
        self.updateGeometry()
        self.update()

    def set_selection(self, index: int | None) -> None:
        self._selected = index if index is not None and 0 <= index < len(self._rows) else None
        if self._selected is not None:
            self._focus_index = self._selected
        self.update()

    def _description(self, index: int) -> str:
        row = self._rows[index]
        percent = row["count"] / self._denominator * 100 if self._denominator else 0.0
        return f"{row['label']} · {row['count']:,}건 · {percent:.1f}%"

    def _value(self, row: dict[str, Any]) -> str:
        if self._mode == "percent":
            percent = row["count"] / self._denominator * 100 if self._denominator else 0.0
            return f"{percent:.1f}%"
        return f"{row['count']:,}"

    def _label_width(self, width: int) -> int:
        return max(30, min(210, int(max(1, width - 80) * 0.36)))

    def _horizontal_heights(self, width: int) -> list[int]:
        label_width = self._label_width(width)
        metrics = self.fontMetrics()
        return [
            max(
                34,
                metrics.boundingRect(QRect(0, 0, label_width, 100000), _WRAP, row["label"]).height()
                + 14,
            )
            for row in self._rows
        ]

    def _vertical_label_height(self, width: int) -> int:
        if not self._rows:
            return 32
        label_width = max(8, int((max(1, width) - 16) / len(self._rows)) - 8)
        return min(
            120,
            max(
                self.fontMetrics()
                .boundingRect(QRect(0, 0, label_width, 100000), _WRAP, row["label"])
                .height()
                for row in self._rows
            ),
        )

    def heightForWidth(self, width: int) -> int:
        if not self._rows:
            return 120
        if self._vertical:
            return 210 + self._vertical_label_height(width)
        return sum(self._horizontal_heights(width)) + 8

    def sizeHint(self) -> QSize:
        return QSize(560, self.heightForWidth(max(160, self.width())))

    def minimumSizeHint(self) -> QSize:
        return QSize(0, self.heightForWidth(max(1, self.width())))

    def _sync_height(self) -> None:
        self.setMinimumHeight(self.heightForWidth(max(1, self.width())))

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._sync_height()

    def bucket_rect(self, index: int) -> QRectF:
        """Return a category hit area, also useful to expose chart navigation."""

        if not 0 <= index < len(self._rows):
            return QRectF()
        return self._bucket_rects()[index]

    def _bucket_rects(self) -> list[QRectF]:
        if not self._rows:
            return []
        if self._vertical:
            column_width = max(1.0, (self.width() - 16) / len(self._rows))
            return [
                QRectF(8 + index * column_width, 4, column_width, self.height() - 8)
                for index in range(len(self._rows))
            ]
        heights = self._horizontal_heights(self.width())
        result = []
        y = 4
        for height in heights:
            result.append(QRectF(0, y, self.width(), height))
            y += height
        return result

    def _index_at(self, point: QPointF) -> int | None:
        return next(
            (index for index, rect in enumerate(self._bucket_rects()) if rect.contains(point)),
            None,
        )

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        index = self._index_at(event.position())
        if index != self._hovered:
            self._hovered = index
            self.setCursor(
                Qt.CursorShape.PointingHandCursor
                if index is not None
                else Qt.CursorShape.ArrowCursor
            )
            self.setToolTip(self._description(index) if index is not None else "")
            self.update()
        if index is not None:
            QToolTip.showText(event.globalPosition().toPoint(), self._description(index), self)
        else:
            QToolTip.hideText()
        super().mouseMoveEvent(event)

    def leaveEvent(self, event) -> None:
        self._hovered = None
        self.setToolTip("")
        QToolTip.hideText()
        self.update()
        super().leaveEvent(event)

    def hideEvent(self, event) -> None:
        if self._hovered is not None:
            QToolTip.hideText()
        self._hovered = None
        super().hideEvent(event)

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            index = self._index_at(event.position())
            if index is not None:
                self.setFocus(Qt.FocusReason.MouseFocusReason)
                self.set_selection(index)
                self.bucket_clicked.emit(index)
                event.accept()
                return
        super().mousePressEvent(event)

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if not self._rows:
            super().keyPressEvent(event)
            return
        key = event.key()
        if key in {Qt.Key.Key_Right, Qt.Key.Key_Down}:
            self._focus_index = min(len(self._rows) - 1, self._focus_index + 1)
        elif key in {Qt.Key.Key_Left, Qt.Key.Key_Up}:
            self._focus_index = max(0, self._focus_index - 1)
        elif key == Qt.Key.Key_Home:
            self._focus_index = 0
        elif key == Qt.Key.Key_End:
            self._focus_index = len(self._rows) - 1
        elif key in {Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Space}:
            self.set_selection(self._focus_index)
            self.bucket_clicked.emit(self._focus_index)
        else:
            super().keyPressEvent(event)
            return
        event.accept()
        self._ensure_focus_visible()
        self.update()

    def _ensure_focus_visible(self) -> None:
        if not self._rows:
            return
        ancestor = self.parentWidget()
        while ancestor is not None:
            if isinstance(ancestor, QScrollArea) and ancestor.widget() is not None:
                point = self.mapTo(
                    ancestor.widget(), self.bucket_rect(self._focus_index).center().toPoint()
                )
                ancestor.ensureVisible(point.x(), point.y(), 12, 24)
                return
            ancestor = ancestor.parentWidget()

    def focusInEvent(self, event) -> None:
        super().focusInEvent(event)
        self._ensure_focus_visible()
        self.update()

    def focusOutEvent(self, event) -> None:
        super().focusOutEvent(event)
        self.update()

    def paintEvent(self, event: QPaintEvent) -> None:
        painter = QPainter(self)
        painter.setRenderHints(
            QPainter.RenderHint.Antialiasing | QPainter.RenderHint.TextAntialiasing
        )
        if not self._rows:
            painter.setPen(QColor(MUTED))
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "표시할 데이터가 없습니다.")
            return
        maximum = max(1, *(row["count"] for row in self._rows))
        vertical_label_height = self._vertical_label_height(self.width()) if self._vertical else 0
        for index, (row, rect) in enumerate(zip(self._rows, self._bucket_rects(), strict=True)):
            if not rect.intersects(event.rect().toRectF()):
                continue
            if index == self._hovered or index == self._selected:
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(QColor(PRIMARY_SUBTLE))
                painter.drawRoundedRect(rect.adjusted(0, 1, 0, -1), 6, 6)
            color = (
                _MISSING_BAR
                if row.get("missing")
                else (
                    PRIMARY
                    if index == self._selected
                    else (_DIM_BAR if self._selected is not None else _BAR)
                )
            )
            if self._vertical:
                self._paint_vertical(painter, rect, row, maximum, color, vertical_label_height)
            else:
                self._paint_horizontal(painter, rect, row, maximum, color)
            if self.hasFocus() and index == self._focus_index:
                painter.setBrush(Qt.BrushStyle.NoBrush)
                painter.setPen(QPen(QColor(PRIMARY), 1, Qt.PenStyle.DashLine))
                painter.drawRoundedRect(rect.adjusted(1, 1, -1, -1), 6, 6)

    def _paint_horizontal(self, painter, rect, row, maximum, color) -> None:
        label_width = self._label_width(self.width())
        count_width = max(54, self.fontMetrics().horizontalAdvance(self._value(row)) + 8)
        plot_x = label_width + 18
        plot_width = max(1, self.width() - plot_x - count_width - 14)
        track = QRectF(plot_x, rect.center().y() - 8, plot_width, 16)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(_TRACK))
        painter.drawRoundedRect(track, 3, 3)
        if row["count"]:
            painter.setBrush(QColor(color))
            painter.drawRoundedRect(
                QRectF(track.x(), track.y(), max(2, plot_width * row["count"] / maximum), 16), 3, 3
            )
        painter.setPen(QColor(MUTED if row.get("missing") else TEXT))
        painter.drawText(
            QRectF(6, rect.y() + 4, label_width, rect.height() - 8),
            Qt.AlignmentFlag.AlignVCenter | _WRAP,
            row["label"],
        )
        painter.drawText(
            QRectF(self.width() - count_width - 6, rect.y(), count_width, rect.height()),
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
            self._value(row),
        )

    def _paint_vertical(self, painter, rect, row, maximum, color, label_height) -> None:
        plot_bottom = self.height() - label_height - 14
        plot_height = max(24, plot_bottom - 32)
        plot_width = max(1, rect.width() - 10)
        bar_height = plot_height * row["count"] / maximum
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(color))
        if row["count"]:
            painter.drawRoundedRect(
                QRectF(rect.x() + 5, plot_bottom - bar_height, plot_width, bar_height), 3, 3
            )
        painter.setPen(QColor(MUTED if row.get("missing") else TEXT))
        count_label = self.fontMetrics().elidedText(
            self._value(row), Qt.TextElideMode.ElideRight, max(1, int(rect.width()) - 4)
        )
        painter.drawText(
            QRectF(rect.x() + 2, max(4, plot_bottom - bar_height - 25), rect.width() - 4, 22),
            Qt.AlignmentFlag.AlignCenter,
            count_label,
        )
        painter.drawText(
            QRectF(rect.x() + 3, plot_bottom + 9, max(1, rect.width() - 6), label_height),
            Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignHCenter | _WRAP,
            row["label"],
        )
        painter.setPen(QColor(BORDER))
        painter.drawLine(QPointF(rect.x(), plot_bottom + 1), QPointF(rect.right(), plot_bottom + 1))


class BoxPlotWidget(_InteractiveStatisticsChart):
    """Horizontal five-number summaries without opening video files to paint."""

    group_clicked = Signal(int)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._rows: list[dict[str, Any]] = []
        self._unit = "초"
        self._axis_label = ""
        self.setAccessibleName("그룹별 트리거에서 충돌까지 시간 상자 그래프")

    def set_data(
        self,
        rows: list[dict[str, Any]],
        *,
        unit: str = "초",
        axis_label: str = "",
        allow_negative: bool = False,
    ) -> None:
        normalized = []
        for row in rows:
            keys = ("min", "q1", "median", "q3", "max")
            empty = row.get("n") == 0 and any(row.get(key) is None for key in keys)
            try:
                values = [0.0] * 5 if empty else [float(row[key]) for key in keys]
            except (KeyError, TypeError, ValueError, OverflowError) as exc:
                raise ValueError(
                    "상자 그래프에는 정렬된 유효한 다섯 수 요약이 필요합니다."
                ) from exc
            if not all(
                math.isfinite(v) and (allow_negative or v >= 0) for v in values
            ) or values != sorted(values):
                raise ValueError("상자 그래프에는 정렬된 유효한 다섯 수 요약이 필요합니다.")
            points = []
            for value in row.get("values", []):
                try:
                    point = float(value)
                except (TypeError, ValueError, OverflowError):
                    continue
                if math.isfinite(point) and values[0] <= point <= values[-1]:
                    points.append(point)
            normalized.append(
                {
                    **row,
                    "label": str(row["label"]),
                    "n": max(0, int(row.get("n", len(points)))),
                    "values": points,
                    "empty": empty,
                    **dict(zip(keys, values, strict=True)),
                }
            )
        self._rows = normalized
        self._unit, self._axis_label = str(unit), str(axis_label)
        self._data_changed(
            "유효한 영상 지표가 없습니다." if unit == "초" else "유효한 수치가 없습니다."
        )

    def _indices(self) -> list[int]:
        return list(range(len(self._rows)))

    def _activate(self, index: int) -> None:
        self.group_clicked.emit(index)

    def _description_at(self, index: int) -> str:
        return self._description(self._rows[index])

    def _description(self, row: dict[str, Any]) -> str:
        if row.get("empty"):
            return f"{row['label']} · n=0 · 유효한 수치가 없습니다."
        suffix = " · 표본 적음" if row.get("n", 0) < 5 else ""
        return (
            f"{row['label']} · n={row.get('n', 0)} · 중앙값 {row['median']:.2f}{self._unit} · "
            f"가운데 50% {row['q1']:.2f}–{row['q3']:.2f}{self._unit} · "
            f"범위 {row['min']:.2f}–{row['max']:.2f}{self._unit}{suffix}"
        )

    def _label_width(self, width: int) -> int:
        return max(36, min(170, int(width * 0.28)))

    def _suffix(self, row: dict[str, Any], *, compact: bool = False) -> str:
        if row.get("empty"):
            return "n=0 · 유효한 수치 없음"
        separator = " · " if compact else "\n"
        return f"중앙 {_number_text(row['median'])}{self._unit}{separator}n={row.get('n', 0)}" + (
            " · 표본 적음" if row.get("n", 0) < 5 else ""
        )

    def _compact_label_height(self, row: dict[str, Any], width: int) -> int:
        return max(
            22,
            self.fontMetrics()
            .boundingRect(QRect(0, 0, max(1, width - 8), 100000), _WRAP, row["label"])
            .height(),
        )

    def _summary_width(self, width: int) -> int:
        longest = max(
            (
                self.fontMetrics().horizontalAdvance(line)
                for row in self._rows
                for line in self._suffix(row).splitlines()
            ),
            default=96,
        )
        return min(max(104, longest + 10), max(104, round(width * 0.35)))

    def _row_heights(self, width: int) -> list[int]:
        if width < 360:
            return [
                self._compact_label_height(row, width)
                + 50
                + max(
                    22,
                    self.fontMetrics()
                    .boundingRect(
                        QRect(0, 0, max(1, width - 16), 100000),
                        _WRAP,
                        self._suffix(row, compact=True),
                    )
                    .height(),
                )
                for row in self._rows
            ]
        suffix_width = self._summary_width(width)
        return [
            max(
                64 if row.get("n", 0) < 5 else 48,
                self.fontMetrics()
                .boundingRect(QRect(0, 0, self._label_width(width), 100000), _WRAP, row["label"])
                .height()
                + 12,
                self.fontMetrics()
                .boundingRect(QRect(0, 0, suffix_width - 4, 100000), _WRAP, self._suffix(row))
                .height()
                + 8,
            )
            for row in self._rows
        ]

    def heightForWidth(self, width: int) -> int:
        return (
            sum(self._row_heights(width)) + (58 if self._axis_label else 34) if self._rows else 120
        )

    def sizeHint(self) -> QSize:
        return QSize(500, self.heightForWidth(self.width()))

    def minimumSizeHint(self) -> QSize:
        return QSize(0, self.heightForWidth(self.width()))

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self.setMinimumHeight(self.heightForWidth(self.width()))

    def group_rect(self, index: int) -> QRectF:
        if not 0 <= index < len(self._rows):
            return QRectF()
        heights = self._row_heights(self.width())
        return QRectF(0, 4 + sum(heights[:index]), self.width(), heights[index])

    def _item_rect(self, index: int) -> QRectF:
        return self.group_rect(index)

    def _value_x(self, value: float) -> float:
        plot_x, plot_width, _ = self._plot_dimensions()
        axis = _numeric_axis(
            [
                value
                for row in self._rows
                if not row.get("empty")
                for value in (row["min"], row["max"])
            ]
        )
        return plot_x + _fraction(value, *axis) * plot_width

    def _plot_dimensions(self) -> tuple[float, float, int]:
        if self.width() < 360:
            return 8, max(1, self.width() - 16), 0
        suffix_width = self._summary_width(self.width())
        plot_x = self._label_width(self.width()) + 16
        return plot_x, max(1, self.width() - plot_x - suffix_width - 12), suffix_width

    def _plot_center(self, index: int) -> float:
        rect = self.group_rect(index)
        if self.width() < 360:
            return rect.y() + self._compact_label_height(self._rows[index], self.width()) + 20
        return rect.center().y()

    def _point_positions(self, index: int) -> list[QPointF]:
        row = self._rows[index]
        center = self._plot_center(index)
        return [
            QPointF(self._value_x(value), center + ((i * 37 + 11) % 23) - 11)
            for i, value in enumerate(row.get("values", []))
        ]

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        super().mouseMoveEvent(event)

    def leaveEvent(self, event) -> None:
        self.setToolTip("")
        super().leaveEvent(event)

    def paintEvent(self, event: QPaintEvent) -> None:
        del event
        painter = QPainter(self)
        painter.setRenderHints(
            QPainter.RenderHint.Antialiasing | QPainter.RenderHint.TextAntialiasing
        )
        if not self._rows:
            painter.setPen(QColor(MUTED))
            painter.drawText(
                self.rect(), Qt.AlignmentFlag.AlignCenter, "유효한 영상 지표가 없습니다."
            )
            return
        minimum, maximum = _numeric_axis(
            [
                value
                for row in self._rows
                if not row.get("empty")
                for value in (row["min"], row["max"])
            ]
        )
        label_width = self._label_width(self.width())
        plot_x, plot_width, suffix_width = self._plot_dimensions()
        compact = self.width() < 360
        y = 4
        for index, (row, height) in enumerate(
            zip(self._rows, self._row_heights(self.width()), strict=True)
        ):
            center = self._plot_center(index)
            if index in {self._hovered, self._selected}:
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(QColor(PRIMARY_SUBTLE))
                painter.drawRoundedRect(self.group_rect(index).adjusted(0, 1, 0, -1), 6, 6)
            painter.setPen(QColor(PRIMARY if index == self._selected else TEXT))
            painter.drawText(
                QRectF(
                    4,
                    y,
                    self.width() - 8 if compact else label_width,
                    self._compact_label_height(row, self.width()) if compact else height,
                ),
                Qt.AlignmentFlag.AlignVCenter | _WRAP,
                row["label"],
            )
            if row.get("empty"):
                painter.setPen(QColor(MUTED))
                painter.drawText(
                    QRectF(
                        plot_x,
                        center - 10 if compact else y,
                        plot_width + suffix_width,
                        24 if compact else height,
                    ),
                    Qt.AlignmentFlag.AlignVCenter,
                    "n=0 · 유효한 수치 없음",
                )
                self._paint_item_focus(painter, index, self.group_rect(index))
                y += height
                continue
            painter.setPen(QPen(QColor(MUTED), 1))
            minimum_x, maximum_x = self._value_x(row["min"]), self._value_x(row["max"])
            painter.drawLine(QPointF(minimum_x, center), QPointF(maximum_x, center))
            painter.drawLine(QPointF(minimum_x, center - 5), QPointF(minimum_x, center + 5))
            painter.drawLine(QPointF(maximum_x, center - 5), QPointF(maximum_x, center + 5))
            weak = row.get("n", 0) < 5
            painter.setPen(
                QPen(QColor(PRIMARY), 1, Qt.PenStyle.DashLine if weak else Qt.PenStyle.SolidLine)
            )
            fill = QColor(PRIMARY)
            fill.setAlphaF(0.1 if weak else 0.22)
            painter.setBrush(fill)
            painter.drawRoundedRect(
                QRectF(
                    self._value_x(row["q1"]),
                    center - 10,
                    max(1, self._value_x(row["q3"]) - self._value_x(row["q1"])),
                    20,
                ),
                3,
                3,
            )
            painter.save()
            painter.setClipRect(QRectF(plot_x - 3, y, plot_width + 6, height))
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(16, 35, 63, 115))
            for point in self._point_positions(index):
                painter.drawEllipse(point, 2.5, 2.5)
            painter.restore()
            painter.setPen(QPen(QColor(TEXT), 2))
            median_x = self._value_x(row["median"])
            painter.drawLine(QPointF(median_x, center - 12), QPointF(median_x, center + 12))
            painter.setPen(QColor(MUTED))
            painter.drawText(
                QRectF(
                    8 if compact else self.width() - suffix_width,
                    center + 22 if compact else y,
                    self.width() - 16 if compact else suffix_width - 4,
                    y + height - center - 22 if compact else height,
                ),
                (Qt.AlignmentFlag.AlignLeft if compact else Qt.AlignmentFlag.AlignRight)
                | Qt.AlignmentFlag.AlignVCenter
                | _WRAP,
                self._suffix(row, compact=compact),
            )
            self._paint_item_focus(painter, index, self.group_rect(index))
            y += height
        painter.setPen(QColor(BORDER))
        painter.drawLine(QPointF(plot_x, y + 1), QPointF(plot_x + plot_width, y + 1))
        painter.setPen(QColor(MUTED))
        tick_font = QFont(painter.font())
        tick_font.setPixelSize(11)
        painter.setFont(tick_font)
        for fraction in (0.0, 0.5, 1.0):
            tick_x = plot_x + plot_width * fraction
            tick_width = min(90, max(1, plot_width / 3))
            tick_left = max(0, min(self.width() - tick_width, tick_x - tick_width / 2))
            painter.drawText(
                QRectF(tick_left, y + 6, tick_width, 22),
                Qt.AlignmentFlag.AlignHCenter,
                f"{_number_text(minimum * (1 - fraction) + maximum * fraction)}{self._unit}",
            )
        if self._axis_label:
            painter.drawText(
                QRectF(plot_x, y + 28, plot_width, 24),
                Qt.AlignmentFlag.AlignHCenter | _WRAP,
                self._axis_label,
            )
