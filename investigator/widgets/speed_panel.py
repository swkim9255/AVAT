"""Speed reference points, per-segment speeds, and the trigger-speed estimate."""

from __future__ import annotations

import math

from PySide6.QtCore import QPointF, QRectF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (
    QAbstractItemView,
    QBoxLayout,
    QDoubleSpinBox,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QSizePolicy,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from investigator.speed_estimation import (
    SPEED_BAND_LIMITS,
    SpeedEstimate,
    SpeedPoint,
)
from investigator.theme import BORDER, MUTED, PRIMARY, TEXT, material_icon
from investigator.widgets.playback_panel import SPEED_POINT, format_timestamp

WARNING_TEXT = "#8A5300"
WARNING_BACKGROUND = "#FFF4DC"
BAND_NAMES = ("크립", "저속", "중속", "고속")


class DistanceEditor(QDoubleSpinBox):
    def wheelEvent(self, event) -> None:  # type: ignore[no-untyped-def]
        # Scrolling the speed panel must not silently alter measured distances.
        event.ignore()


def _kph(value: float | None) -> str:
    if value is None:
        return "-"
    if math.isinf(value):
        return "∞"
    return f"{value:.0f}" if value >= 10 else f"{value:.1f}"


class SpeedChart(QWidget):
    """Speed over time: segment averages, the local fit, and the value at T."""

    point_clicked = Signal(int)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setMinimumSize(QSize(220, 150))
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setAccessibleName("속도 그래프")
        self.setMouseTracking(True)
        self._points: list[SpeedPoint] = []
        self._estimate = SpeedEstimate()
        self._fps = 0.0
        self._tick_hitboxes: list[tuple[QRectF, int]] = []

    def set_data(self, points: list[SpeedPoint], estimate: SpeedEstimate, fps: float) -> None:
        self._points = points
        self._estimate = estimate
        self._fps = fps
        description = "지점이 없습니다."
        if estimate.kph is not None:
            description = f"T 시점 약 {_kph(estimate.kph)} km/h"
        elif estimate.segments:
            description = f"구간 {len(estimate.segments)}개"
        self.setAccessibleDescription(description)
        self.update()

    def _plot_rect(self) -> QRectF:
        return QRectF(44, 10, max(10, self.width() - 56), max(10, self.height() - 40))

    def paintEvent(self, event) -> None:  # type: ignore[no-untyped-def]
        del event
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        plot = self._plot_rect()
        estimate = self._estimate
        fps = self._fps
        self._tick_hitboxes = []
        font = painter.font()
        font.setPixelSize(11)
        painter.setFont(font)

        frames = [point.frame for point in self._points]
        if estimate.trigger_frame is not None:
            frames.append(estimate.trigger_frame)
        if fps <= 0 or len(frames) < 2:
            painter.setPen(QColor(MUTED))
            painter.drawText(
                self.rect(),
                Qt.AlignmentFlag.AlignCenter | Qt.TextFlag.TextWordWrap,
                "기준 지점을 2개 이상 추가하고 거리를 입력하면\n구간별 속도가 그래프로 나타나요.",
            )
            return

        t_min, t_max = min(frames) / fps, max(frames) / fps
        pad = max(0.05, (t_max - t_min) * 0.06)
        t_min, t_max = t_min - pad, t_max + pad
        speeds = [s.kph_high for s in estimate.segments if math.isfinite(s.kph_high)]
        speeds += [s.kph for s in estimate.segments if math.isfinite(s.kph)]
        if estimate.kph_high is not None and math.isfinite(estimate.kph_high):
            speeds.append(estimate.kph_high)
        top = max([20.0, *speeds]) * 1.15
        nice = next((step for step in (20, 40, 60, 80, 100, 120, 160, 200, 300) if step >= top), top)

        def x_of(seconds: float) -> float:
            return plot.left() + (seconds - t_min) / (t_max - t_min) * plot.width()

        def y_of(kph: float) -> float:
            return plot.bottom() - min(max(kph, 0.0), nice) / nice * plot.height()

        # Label bands: the thresholds that decide the 차량 속도 category.
        painter.setPen(QPen(QColor(BORDER), 1, Qt.PenStyle.DashLine))
        for limit in SPEED_BAND_LIMITS:
            if limit < nice:
                painter.drawLine(QPointF(plot.left(), y_of(limit)), QPointF(plot.right(), y_of(limit)))
        painter.setPen(QColor(MUTED))
        lower = 0.0
        for limit, name in zip((*SPEED_BAND_LIMITS, math.inf), BAND_NAMES, strict=True):
            upper = min(limit, nice)
            if lower < nice and y_of(lower) - y_of(upper) > 13:
                painter.drawText(
                    QRectF(plot.right() - 40, y_of(upper), 38, y_of(lower) - y_of(upper)),
                    Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                    name,
                )
            lower = limit
        for value in (0, nice / 2, nice):
            painter.drawText(
                QRectF(0, y_of(value) - 8, plot.left() - 6, 16),
                Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                f"{value:.0f}",
            )
        painter.save()
        painter.translate(10, plot.center().y())
        painter.rotate(-90)
        painter.drawText(QRectF(-30, -10, 60, 14), Qt.AlignmentFlag.AlignCenter, "km/h")
        painter.restore()

        # Axis with ticks labelled 1, 2, 3 … and T (the layout the investigator sketched).
        painter.setPen(QPen(QColor(TEXT), 1.2))
        painter.drawLine(QPointF(plot.left(), plot.bottom()), QPointF(plot.right(), plot.bottom()))
        for number, point in enumerate(self._points, start=1):
            x = x_of(point.frame / fps)
            painter.setPen(QPen(QColor(SPEED_POINT), 1.2))
            painter.drawLine(QPointF(x, plot.bottom() - 4), QPointF(x, plot.bottom() + 4))
            box = QRectF(x - 10, plot.bottom() + 5, 20, 16)
            painter.drawText(box, Qt.AlignmentFlag.AlignCenter, str(number))
            self._tick_hitboxes.append((box.adjusted(-4, -10, 4, 2), point.frame))

        # Segment averages as flat bars with their ±0.5 frame range.
        for segment in estimate.segments:
            x0, x1 = x_of(segment.start_frame / fps), x_of(segment.end_frame / fps)
            if math.isfinite(segment.kph_high):
                band = QColor(SPEED_POINT)
                band.setAlpha(28)
                painter.fillRect(
                    QRectF(x0, y_of(segment.kph_high), x1 - x0, y_of(segment.kph_low) - y_of(segment.kph_high)),
                    band,
                )
            if math.isfinite(segment.kph):
                painter.setPen(QPen(QColor(SPEED_POINT), 2))
                painter.drawLine(QPointF(x0, y_of(segment.kph)), QPointF(x1, y_of(segment.kph)))

        # Local fit used for T, drawn over the points it used and out to T.
        if estimate.velocity_line is not None and estimate.points_used and estimate.trigger_frame is not None:
            c0, c1 = estimate.velocity_line
            used = [frame / fps for frame in estimate.points_used] + [estimate.trigger_frame / fps]
            start, end = min(used), max(used)
            path = QPainterPath()
            steps = 24
            for step in range(steps + 1):
                seconds = start + (end - start) * step / steps
                point = QPointF(x_of(seconds), y_of(c0 + c1 * seconds))
                if step:
                    path.lineTo(point)
                else:
                    path.moveTo(point)
            painter.setPen(QPen(QColor(PRIMARY), 1.5, Qt.PenStyle.DashLine))
            painter.drawPath(path)

        if estimate.trigger_frame is not None:
            x = x_of(estimate.trigger_frame / fps)
            painter.setPen(QPen(QColor(PRIMARY), 1.2))
            painter.drawLine(QPointF(x, plot.bottom() - 4), QPointF(x, plot.bottom() + 4))
            font.setBold(True)
            painter.setFont(font)
            painter.drawText(QRectF(x - 10, plot.bottom() + 5, 20, 16), Qt.AlignmentFlag.AlignCenter, "T")
            font.setBold(False)
            painter.setFont(font)
            if estimate.kph is not None:
                if estimate.kph_low is not None and estimate.kph_high is not None and math.isfinite(estimate.kph_high):
                    painter.setPen(QPen(QColor(PRIMARY), 1.5))
                    painter.drawLine(QPointF(x, y_of(estimate.kph_low)), QPointF(x, y_of(estimate.kph_high)))
                    for value in (estimate.kph_low, estimate.kph_high):
                        painter.drawLine(QPointF(x - 4, y_of(value)), QPointF(x + 4, y_of(value)))
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(QColor(PRIMARY))
                painter.drawEllipse(QPointF(x, y_of(estimate.kph)), 4.5, 4.5)
                top_y = y_of(estimate.kph_high) if (
                    estimate.kph_high is not None and math.isfinite(estimate.kph_high)
                ) else y_of(estimate.kph)
                label_rect = QRectF(x - 40, top_y - 22, 80, 18)
                label_rect.moveLeft(min(max(label_rect.left(), plot.left()), plot.right() - 80))
                if label_rect.top() < 0:
                    label_rect.moveTop(y_of(estimate.kph) + 8)
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(QColor(255, 255, 255, 220))
                painter.drawRoundedRect(label_rect.adjusted(14, 1, -14, -1), 4, 4)
                font.setBold(True)
                painter.setFont(font)
                painter.setPen(QColor(PRIMARY))
                painter.drawText(label_rect, Qt.AlignmentFlag.AlignCenter, f"T {_kph(estimate.kph)} km/h")

    def mousePressEvent(self, event) -> None:  # type: ignore[no-untyped-def]
        if event.button() != Qt.MouseButton.LeftButton:
            super().mousePressEvent(event)
            return
        position = event.position()
        for box, frame in self._tick_hitboxes:
            if box.contains(position):
                self.point_clicked.emit(frame)
                event.accept()
                return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:  # type: ignore[no-untyped-def]
        frame = next((frame for box, frame in self._tick_hitboxes if box.contains(event.position())), None)
        if frame is not None:
            self.setCursor(Qt.CursorShape.PointingHandCursor)
            self.setToolTip(f"{frame + 1:,}번 프레임 · 클릭하여 이동")
        else:
            self.unsetCursor()
            self.setToolTip("")
        super().mouseMoveEvent(event)


class SpeedPanel(QWidget):
    """Lists speed points, their distances, segment speeds, and the T estimate."""

    add_point_requested = Signal()
    remove_point_requested = Signal(int)
    clear_requested = Signal()
    distance_changed = Signal(int, object)
    seek_requested = Signal(int)
    apply_requested = Signal(str)

    COLUMNS = ("지점", "프레임", "시각", "이전 지점부터 거리", "구간 평균 속도")

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("panel")
        self._fps = 0.0
        self._points: list[SpeedPoint] = []
        self._estimate = SpeedEstimate()
        self._updating = False
        self._can_apply = True
        self._context_key: object = None
        self._context_token = repr(None)

        root = QVBoxLayout(self)
        root.setContentsMargins(18, 10, 18, 12)
        root.setSpacing(8)

        self.header = QGridLayout()
        caption = QWidget(self)
        caption_layout = QHBoxLayout(caption)
        caption_layout.setContentsMargins(0, 0, 0, 0)
        title = QLabel("트리거 시점 속도 추정", self)
        title.setStyleSheet("font-size: 14px; font-weight: 650;")
        title.setMinimumWidth(0)
        title.setWordWrap(True)
        caption_layout.addWidget(title)
        hint = QLabel(
            "차량이 차선 끝·횡단보도 줄 같은 기준을 지날 때마다 지점을 추가하고, 실제 거리를 입력하세요.",
            self,
        )
        hint.setStyleSheet(f"color: {MUTED}; font-size: 11px;")
        hint.setWordWrap(True)
        hint.setMinimumWidth(0)
        hint.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        caption_layout.addWidget(hint, 1)
        self.header.addWidget(caption, 0, 0)
        self.actions = QWidget(self)
        actions_layout = QHBoxLayout(self.actions)
        actions_layout.setContentsMargins(0, 0, 0, 0)
        actions_layout.setSpacing(6)
        self.add_button = QPushButton(material_icon("add_location", SPEED_POINT), "현재 프레임 추가 (M)", self)
        self.remove_button = QPushButton(material_icon("delete", PRIMARY), "선택 삭제", self)
        self.clear_button = QPushButton("모두 삭제", self)
        for button in (self.add_button, self.remove_button, self.clear_button):
            button.setStyleSheet("padding: 4px 7px; min-height: 24px; font-size: 12px;")
            actions_layout.addWidget(button)
        self.header.addWidget(self.actions, 0, 1)
        root.addLayout(self.header)

        self.body = QBoxLayout(QBoxLayout.Direction.LeftToRight)
        self.body.setSpacing(12)
        self.table = QTableWidget(0, len(self.COLUMNS), self)
        self.table.setObjectName("speedPointsTable")
        self.table.setStyleSheet(
            "QTableWidget#speedPointsTable { font-size:12px; }"
            "QTableWidget#speedPointsTable::item { padding:2px 4px; }"
            "QTableWidget#speedPointsTable QHeaderView::section { padding:4px 5px; font-size:11px; }"
        )
        self.table.setHorizontalHeaderLabels(list(self.COLUMNS))
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setAccessibleName("속도 기준 지점 목록")
        header_view = self.table.horizontalHeader()
        for column in range(len(self.COLUMNS)):
            header_view.setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
        header_view.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        self.table.setMinimumWidth(0)
        self.table.setMinimumHeight(190)
        self.table.verticalHeader().setDefaultSectionSize(32)
        self.body.addWidget(self.table, 5)
        self.chart = SpeedChart(self)
        self.body.addWidget(self.chart, 4)
        root.addLayout(self.body, 1)

        result = QHBoxLayout()
        result.setSpacing(10)
        self.result_label = QLabel(self)
        self.result_label.setTextFormat(Qt.TextFormat.RichText)
        self.result_label.setStyleSheet("font-size: 13px;")
        self.result_label.setWordWrap(True)
        self.result_label.setMinimumWidth(0)
        self.result_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        result.addWidget(self.result_label, 1)
        self.apply_button = QPushButton(material_icon("check", PRIMARY), "차량 속도에 적용", self)
        self.apply_button.setToolTip("트리거 탭의 차량 속도 (추정) 항목에 이 구간을 입력합니다.")
        self.apply_button.setStyleSheet("padding: 4px 7px; min-height: 26px; font-size:12px;")
        result.addWidget(self.apply_button)
        root.addLayout(result)
        self.warning_label = QLabel(self)
        self.warning_label.setWordWrap(True)
        self.warning_label.setStyleSheet(
            f"color: {WARNING_TEXT}; background: {WARNING_BACKGROUND}; border-radius: 6px; padding: 6px 10px; font-size: 12px;"
        )
        root.addWidget(self.warning_label)
        self.warning_label.setMinimumWidth(0)
        self.warning_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.sensitivity_note = QLabel("일정 FPS 가정의 참고용 추정 · 범위는 ±0.5프레임 배치 민감도이며 거리·모델 오차는 포함하지 않습니다.", self)
        self.sensitivity_note.setToolTip("가변 FPS 영상에서는 프레임 간 시간이 일정하지 않아 결과가 달라질 수 있습니다. 실제 기준물 간 거리와 지점 배치를 확인하세요. 표시 범위는 통계적 신뢰구간이 아닙니다.")
        self.sensitivity_note.setWordWrap(True)
        self.sensitivity_note.setMinimumWidth(0)
        self.sensitivity_note.setStyleSheet(f"color:{MUTED}; font-size:11px;")
        self.sensitivity_note.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        root.addWidget(self.sensitivity_note)

        self.add_button.clicked.connect(self.add_point_requested)
        self.remove_button.clicked.connect(self._remove_selected)
        self.clear_button.clicked.connect(self.clear_requested)
        self.apply_button.clicked.connect(self._apply)
        self.table.cellClicked.connect(self._row_clicked)
        self.table.itemSelectionChanged.connect(self._update_buttons)
        self.chart.point_clicked.connect(self.seek_requested)
        self.set_data([], SpeedEstimate(), 0.0)

    def set_apply_available(self, available: bool) -> None:
        self._can_apply = available
        self._update_buttons()

    def set_data(self, points: list[SpeedPoint], estimate: SpeedEstimate, fps: float, *, context_key: object = None) -> None:
        changed_context = context_key != self._context_key
        selected_frame = None if changed_context else self._selected_frame()
        self._points = points
        self._estimate = estimate
        self._fps = fps
        segment_by_end = {segment.end_frame: segment for segment in estimate.segments}
        self._updating = True
        try:
            if changed_context:
                self._context_key = context_key
                self._context_token = repr(context_key)
                self.table.setRowCount(0)
            self.table.setRowCount(len(points))
            for row, point in enumerate(points):
                number = QTableWidgetItem(str(row + 1))
                number.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                number.setForeground(QColor(SPEED_POINT))
                if point.frame in estimate.points_used:
                    number.setToolTip("T 속도 계산에 사용한 지점")
                    font = number.font()
                    font.setBold(True)
                    number.setFont(font)
                self.table.setItem(row, 0, number)
                self.table.setItem(row, 1, QTableWidgetItem(f"{point.frame + 1:,}"))
                time_text = format_timestamp(point.frame / fps) if fps > 0 else "FPS 미확인"
                self.table.setItem(row, 2, QTableWidgetItem(time_text))
                if row == 0:
                    first = QTableWidgetItem("시작 지점")
                    first.setForeground(QColor(MUTED))
                    self.table.removeCellWidget(row, 3)
                    self.table.setItem(row, 3, first)
                else:
                    self.table.setItem(row, 3, QTableWidgetItem())
                    editor = self.table.cellWidget(row, 3)
                    if not isinstance(editor, QDoubleSpinBox) or editor.property("speedPointFrame") != point.frame:
                        editor = self._distance_editor()
                        self.table.setCellWidget(row, 3, editor)
                    # QAbstractSpinBox.frame is a built-in bool (border visibility),
                    # not a place to store the measured video's frame number.
                    editor.setProperty("speedPointFrame", point.frame)
                    editor.setProperty("speedContext", self._context_token)
                    editor.setAccessibleName(f"{row + 1}번 지점까지 거리")
                    if not editor.hasFocus():
                        editor.blockSignals(True)
                        editor.setMaximum(max(1_000_000_000, point.distance_m or 0.0))
                        editor.setValue(point.distance_m or 0.0)
                        editor.blockSignals(False)
                segment = segment_by_end.get(point.frame) if row else None
                speed_text = "-"
                if segment is not None:
                    speed_text = f"{_kph(segment.kph)} km/h  ({_kph(segment.kph_low)}–{_kph(segment.kph_high)})"
                speed = QTableWidgetItem(speed_text)
                speed.setForeground(QColor(TEXT if segment else MUTED))
                speed.setToolTip("거리 ÷ 구간 시간 × 3.6. 괄호는 ±0.5프레임 배치 가정의 민감도 범위이며 통계적 신뢰구간이 아닙니다.")
                self.table.setItem(row, 4, speed)
                if point.frame == selected_frame:
                    self.table.selectRow(row)
        finally:
            self._updating = False
        self.chart.set_data(points, estimate, fps)
        self._update_result()
        self._update_buttons()

    def _distance_editor(self) -> QDoubleSpinBox:
        editor = DistanceEditor(self.table)
        editor.setObjectName("speedDistance")
        editor.setStyleSheet("QDoubleSpinBox#speedDistance { min-height:22px; max-height:26px; padding:1px 4px; font-size:12px; }")
        editor.setRange(0.0, 1_000_000_000)
        editor.setDecimals(2)
        editor.setSingleStep(0.5)
        editor.setSuffix(" m")
        editor.setSpecialValueText("거리 입력")
        # Persist every valid edit before a point addition or refresh can rebuild
        # the table, and update the speed without requiring Enter/focus loss.
        editor.setKeyboardTracking(True)
        editor.valueChanged.connect(lambda value, editor=editor: self._distance_edited(editor, value))
        return editor

    def _distance_edited(self, editor: QDoubleSpinBox, value: float) -> None:
        frame = editor.property("speedPointFrame")
        if self._updating or frame is None or editor.property("speedContext") != self._context_token:
            return
        row = next((row for row, point in enumerate(self._points) if point.frame == frame), None)
        # Removed/replaced widgets may emit a final focus-out signal. Only the
        # current editor of a non-start point can change the model.
        if row is None or row == 0 or self.table.cellWidget(row, 3) is not editor:
            return
        self.distance_changed.emit(int(frame), value if value > 0 else None)

    def _update_result(self) -> None:
        estimate = self._estimate
        if estimate.trigger_frame is None:
            text = f"<span style='color:{MUTED}'>T 트리거를 지정하면 T 시점의 속도를 계산해요.</span>"
        elif estimate.kph is None:
            text = f"<span style='color:{MUTED}'>T 근처에 거리가 입력된 지점이 2개 이상 필요해요.</span>"
        else:
            label = estimate.label or ""
            text = (
                f"T 시점 약 <b style='font-size:16px'>{_kph(estimate.kph)} km/h</b> "
                f"<span style='color:{MUTED}'>({_kph(estimate.kph_low)}–{_kph(estimate.kph_high)} km/h · "
                f"{estimate.method} · 지점 {len(estimate.points_used)}개)</span>"
                f" → <b style='color:{PRIMARY}'>{label}</b>"
            )
        self.result_label.setText(text)
        warnings = list(estimate.warnings)
        if estimate.label_uncertain:
            warnings.insert(
                0,
                "추정 범위가 속도 구간 경계에 걸쳐 있어요. 영상을 다시 확인하거나 지점을 더 추가해 주세요.",
            )
        self.warning_label.setText("\n".join(f"· {warning}" for warning in warnings))
        self.warning_label.setVisible(bool(warnings))

    def _selected_frame(self) -> int | None:
        rows = self.table.selectionModel().selectedRows() if self.table.selectionModel() else []
        if not rows or not (0 <= rows[0].row() < len(self._points)):
            return None
        return self._points[rows[0].row()].frame

    def _update_buttons(self) -> None:
        self.remove_button.setEnabled(self._selected_frame() is not None)
        self.clear_button.setEnabled(bool(self._points))
        self.apply_button.setEnabled(self._can_apply and self._estimate.label is not None)

    def _row_clicked(self, row: int, column: int) -> None:
        if column == 3:
            return
        if 0 <= row < len(self._points):
            self.seek_requested.emit(self._points[row].frame)

    def _remove_selected(self) -> None:
        frame = self._selected_frame()
        if frame is not None:
            self.remove_point_requested.emit(frame)

    def _apply(self) -> None:
        if self._estimate.label is not None:
            self.apply_requested.emit(self._estimate.label)

    def resizeEvent(self, event) -> None:  # type: ignore[no-untyped-def]
        super().resizeEvent(event)
        narrow = self.width() < 700
        self.body.setDirection(QBoxLayout.Direction.TopToBottom if narrow else QBoxLayout.Direction.LeftToRight)
        self.header.removeWidget(self.actions)
        self.header.addWidget(self.actions, 1 if self.width() < 820 else 0, 0 if self.width() < 820 else 1)


__all__ = ["SpeedChart", "SpeedPanel"]
