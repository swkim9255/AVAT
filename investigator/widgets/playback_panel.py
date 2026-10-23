"""Marked timeline and frame-accurate transport controls."""

from __future__ import annotations

from typing import Literal

from PySide6.QtCore import QEvent, QPoint, QRect, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QMouseEvent, QPainter, QPen, QPolygon
from PySide6.QtWidgets import (
    QBoxLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QSlider,
    QSpinBox,
    QStyle,
    QStyleOptionSlider,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from investigator.theme import DANGER, MUTED, PRIMARY, PRIMARY_HOVER, material_icon

MarkerKind = Literal["trigger", "collision", "speed"]
COLLISION_HOVER = "#EA580C"
SELECTION_RANGE = "#1F5FBF"
SPEED_POINT = "#0F766E"
SPEED_POINT_HOVER = "#14958A"
MARKER_INTERACTION_SIZE = 25


def format_timestamp(seconds: float) -> str:
    """Format as HH:MM:SS.cc, omitting hours for short videos."""

    centiseconds = max(0, round(float(seconds) * 100))
    hours, remainder = divmod(centiseconds, 360_000)
    minutes, remainder = divmod(remainder, 6_000)
    secs, fraction = divmod(remainder, 100)
    if hours:
        return f"{hours:02d}:{minutes:02d}:{secs:02d}.{fraction:02d}"
    return f"{minutes:02d}:{secs:02d}.{fraction:02d}"


class MarkerSlider(QSlider):
    seek_requested = Signal(int)
    # Inclusive zero-based frame range chosen with Shift+drag for object detection.
    range_selected = Signal(int, int)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(Qt.Orientation.Horizontal, parent)
        self.setMinimumHeight(56)
        self.setTracking(True)
        self.setMouseTracking(True)
        self.setAccessibleName("영상 타임라인")
        self.trigger_frame: int | None = None
        self.collision_frame: int | None = None
        self.speed_frames: list[int] = []
        self._hovered_marker: MarkerKind | None = None
        self._hovered_speed_index: int | None = None
        self._painted_speed_index: int | None = None
        self._last_seek_requested: int | None = None
        self._groove_dragging = False
        self.selection_range: tuple[int, int] | None = None
        self._range_anchor: int | None = None
        self.sliderMoved.connect(self._emit_seek_requested)

    def set_selection_range(self, start: int | None, end: int | None = None) -> None:
        """Show (or clear) the detection range without emitting a signal."""

        if start is None:
            self.selection_range = None
            self._range_anchor = None
        else:
            end = start if end is None else end
            low, high = sorted((int(start), int(end)))
            self.selection_range = (
                max(self.minimum(), min(self.maximum(), low)),
                max(self.minimum(), min(self.maximum(), high)),
            )
        self.update()

    @property
    def selecting_range(self) -> bool:
        return self._range_anchor is not None

    def _emit_seek_requested(self, frame: int) -> None:
        self._last_seek_requested = int(frame)
        self.seek_requested.emit(int(frame))

    def set_markers(
        self,
        trigger: int | None,
        collision: int | None,
        speed_frames: list[int] | None = None,
    ) -> None:
        self.trigger_frame = trigger
        self.collision_frame = collision
        self.speed_frames = sorted(int(frame) for frame in (speed_frames or []))
        self._set_hovered_marker(None)
        descriptions = []
        if trigger is not None:
            descriptions.append(f"T 트리거 {int(trigger) + 1:,}번 프레임")
        if collision is not None:
            descriptions.append(f"C 충돌 {int(collision) + 1:,}번 프레임")
        if self.speed_frames:
            descriptions.append(f"속도 지점 {len(self.speed_frames)}개")
        marker_description = ", ".join(descriptions) if descriptions else "지정된 마커 없음"
        self.setAccessibleDescription(
            f"{marker_description}. 마커를 클릭하면 해당 프레임으로 이동합니다."
        )
        self.update()

    @property
    def hovered_marker(self) -> MarkerKind | None:
        return self._hovered_marker

    def _x_for_value(self, value: int) -> int:
        start, span, upside_down = self._slider_metrics()
        position = QStyle.sliderPositionFromValue(
            self.minimum(),
            self.maximum(),
            max(self.minimum(), min(self.maximum(), int(value))),
            span,
            upside_down,
        )
        return start + position

    def _slider_metrics(self) -> tuple[int, int, bool]:
        option = QStyleOptionSlider()
        self.initStyleOption(option)
        handle = self.style().subControlRect(
            QStyle.ComplexControl.CC_Slider,
            option,
            QStyle.SubControl.SC_SliderHandle,
            self,
        )
        groove = self.style().subControlRect(
            QStyle.ComplexControl.CC_Slider,
            option,
            QStyle.SubControl.SC_SliderGroove,
            self,
        )
        start = groove.x() + handle.width() // 2
        span = max(1, groove.width() - handle.width())
        return start, span, option.upsideDown

    def _trigger_interaction_rect(self) -> QRect | None:
        if self.trigger_frame is None:
            return None
        x = self._x_for_value(self.trigger_frame)
        center_y = 14
        half_size = MARKER_INTERACTION_SIZE // 2
        return QRect(
            x - half_size,
            center_y - half_size,
            MARKER_INTERACTION_SIZE,
            MARKER_INTERACTION_SIZE,
        ).intersected(self.rect())

    def _collision_interaction_rect(self) -> QRect | None:
        if self.collision_frame is None:
            return None
        x = self._x_for_value(self.collision_frame)
        center_y = self.height() - 13
        half_size = MARKER_INTERACTION_SIZE // 2
        return QRect(
            x - half_size,
            center_y - half_size,
            MARKER_INTERACTION_SIZE,
            MARKER_INTERACTION_SIZE,
        ).intersected(self.rect())

    def _trigger_label_rect(self) -> QRect | None:
        if self.trigger_frame is None:
            return None
        x = self._x_for_value(self.trigger_frame)
        rect = QRect(x + 14, 1, 12, 18).intersected(self.rect())
        if any(rect.intersects(self._speed_interaction_rect(frame)) for frame in self.speed_frames):
            return None
        return rect

    def _collision_label_rect(self) -> QRect | None:
        if self.collision_frame is None:
            return None
        x = self._x_for_value(self.collision_frame)
        return QRect(x + 14, self.height() - 22, 32, 18).intersected(self.rect())

    def _speed_interaction_rect(self, frame: int) -> QRect:
        x = self._x_for_value(frame)
        return QRect(x - 8, 2, 16, 24).intersected(self.rect())

    def _speed_point_at(self, position: QPoint) -> int | None:
        nearest: tuple[int, int] | None = None
        for index, frame in enumerate(self.speed_frames):
            rect = self._speed_interaction_rect(frame)
            if rect.contains(position):
                distance = abs(rect.center().x() - position.x())
                if nearest is None or distance < nearest[0]:
                    nearest = (distance, index)
        return None if nearest is None else nearest[1]

    def _marker_at(self, position: QPoint) -> tuple[MarkerKind, int] | None:
        if position.y() <= 10:
            speed_index = self._speed_point_at(position)
            if speed_index is not None:
                self._hovered_speed_index = speed_index
                return "speed", self.speed_frames[speed_index]
        trigger_rect = self._trigger_interaction_rect()
        trigger_label = self._trigger_label_rect()
        if (trigger_rect is not None and trigger_rect.contains(position)) or (
            trigger_label is not None and trigger_label.contains(position)
        ):
            assert self.trigger_frame is not None
            return "trigger", self.trigger_frame
        speed_index = self._speed_point_at(position)
        if speed_index is not None:
            self._hovered_speed_index = speed_index
            return "speed", self.speed_frames[speed_index]
        collision_rect = self._collision_interaction_rect()
        collision_label = self._collision_label_rect()
        if (collision_rect is not None and collision_rect.contains(position)) or (
            collision_label is not None and collision_label.contains(position)
        ):
            assert self.collision_frame is not None
            return "collision", self.collision_frame
        return None

    def _marker_color(self, marker: MarkerKind) -> QColor:
        if marker == "trigger":
            return QColor(PRIMARY_HOVER if self._hovered_marker == marker else PRIMARY)
        if marker == "speed":
            return QColor(SPEED_POINT)
        return QColor(COLLISION_HOVER if self._hovered_marker == marker else DANGER)

    def _set_hovered_marker(self, marker: MarkerKind | None) -> None:
        speed_index = self._hovered_speed_index if marker == "speed" else None
        if marker == self._hovered_marker and (
            marker != "speed" or speed_index == self._painted_speed_index
        ):
            return
        self._hovered_marker = marker
        self._hovered_speed_index = speed_index
        self._painted_speed_index = speed_index
        if marker is None:
            self.unsetCursor()
            self.setToolTip("")
        elif marker == "speed" and speed_index is not None:
            frame = self.speed_frames[speed_index]
            self.setCursor(Qt.CursorShape.PointingHandCursor)
            self.setToolTip(
                f"속도 지점 {speed_index + 1} · {int(frame) + 1:,}번 프레임 (클릭하여 이동)"
            )
        else:
            frame = self.trigger_frame if marker == "trigger" else self.collision_frame
            label = "T 트리거" if marker == "trigger" else "C 충돌"
            self.setCursor(Qt.CursorShape.PointingHandCursor)
            self.setToolTip(f"{label} · {int(frame) + 1:,}번 프레임 (클릭하여 이동)")
        self.update()

    def _seek_to_marker(self, frame: int, event: QMouseEvent) -> None:
        target = max(self.minimum(), min(self.maximum(), int(frame)))
        self.setValue(target)
        self._emit_seek_requested(target)
        event.accept()

    def paintEvent(self, event) -> None:  # type: ignore[no-untyped-def]
        super().paintEvent(event)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        if self.selection_range is not None:
            painter.save()
            start, end = self.selection_range
            left, right = sorted((self._x_for_value(start), self._x_for_value(end)))
            band = QRect(left - 2, self.height() // 2 - 9, max(4, right - left + 4), 18)
            fill = QColor(SELECTION_RANGE)
            fill.setAlpha(46)
            painter.setBrush(fill)
            painter.setPen(QPen(QColor(SELECTION_RANGE), 1.2))
            painter.drawRoundedRect(band, 4, 4)
            painter.restore()
        for index, frame in enumerate(self.speed_frames):
            painter.save()
            x = self._x_for_value(frame)
            hovered = self._hovered_marker == "speed" and self._hovered_speed_index == index
            color = QColor(SPEED_POINT_HOVER if hovered else SPEED_POINT)
            painter.setPen(QPen(color, 2 if hovered else 1.5))
            painter.drawLine(x, 19, x, 25)
            font = painter.font()
            font.setPixelSize(10)
            font.setBold(hovered)
            painter.setFont(font)
            painter.drawText(QRect(x - 8, 2, 16, 16), Qt.AlignmentFlag.AlignCenter, str(index + 1))
            painter.restore()
        if self.trigger_frame is not None:
            painter.save()
            x = self._x_for_value(self.trigger_frame)
            center_y = 14
            hovered = self._hovered_marker == "trigger"
            color = self._marker_color("trigger")
            if hovered:
                highlight = QColor(color)
                highlight.setAlpha(32)
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(highlight)
                painter.drawRoundedRect(self._trigger_interaction_rect(), 6, 6)
                font = painter.font()
                font.setBold(True)
                painter.setFont(font)
            radius = 6 if hovered else 5
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(color)
            painter.drawPolygon(
                QPolygon(
                    [
                        QPoint(x, center_y - radius),
                        QPoint(x + radius, center_y),
                        QPoint(x, center_y + radius),
                        QPoint(x - radius, center_y),
                    ]
                )
            )
            painter.setPen(color)
            if label_rect := self._trigger_label_rect():
                painter.drawText(label_rect, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, "T")
            painter.restore()
        if self.collision_frame is not None:
            painter.save()
            x = self._x_for_value(self.collision_frame)
            center_y = self.height() - 13
            hovered = self._hovered_marker == "collision"
            color = self._marker_color("collision")
            if hovered:
                highlight = QColor(color)
                highlight.setAlpha(32)
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(highlight)
                painter.drawRoundedRect(self._collision_interaction_rect(), 6, 6)
                font = painter.font()
                font.setBold(True)
                painter.setFont(font)
            radius = 6 if hovered else 5
            half_height = 6 if hovered else 5
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(color)
            painter.drawPolygon(
                QPolygon(
                    [
                        QPoint(x, center_y - half_height),
                        QPoint(x + radius, center_y + half_height),
                        QPoint(x - radius, center_y + half_height),
                    ]
                )
            )
            painter.setPen(color)
            painter.drawText(QRect(x + 16, self.height() - 22, 32, 18), "C")
            painter.restore()

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if self._range_anchor is not None:
            if event.buttons() & Qt.MouseButton.LeftButton:
                value = self._value_at_position(event.position().toPoint())
                self.set_selection_range(self._range_anchor, value)
                event.accept()
                return
            self._range_anchor = None
        if self._groove_dragging:
            # A press away from the handle uses absolute track coordinates;
            # native handle drags below still retain their original grab offset.
            if self.isSliderDown() and event.buttons() & Qt.MouseButton.LeftButton:
                self.setSliderPosition(self._value_at_position(event.position().toPoint()))
                event.accept()
                return
            # Respect cancelled gestures (e.g. an intervening video change) and
            # never turn a later passive hover into an unintended seek.
            self._groove_dragging = False
            self.setSliderDown(False)
        marker = self._marker_at(event.position().toPoint())
        self._set_hovered_marker(marker[0] if marker is not None else None)
        super().mouseMoveEvent(event)

    def leaveEvent(self, event: QEvent) -> None:
        self._set_hovered_marker(None)
        super().leaveEvent(event)

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() != Qt.MouseButton.LeftButton:
            super().mousePressEvent(event)
            return
        position = event.position().toPoint()
        self._groove_dragging = False
        if event.modifiers() & Qt.KeyboardModifier.ShiftModifier:
            # Shift+drag chooses a detection range instead of seeking.
            self._range_anchor = self._value_at_position(position)
            self._set_hovered_marker(None)
            self.set_selection_range(self._range_anchor, self._range_anchor)
            event.accept()
            return
        marker = self._marker_at(position)
        if marker is not None:
            self._set_hovered_marker(marker[0])
            self._seek_to_marker(marker[1], event)
            return
        option = QStyleOptionSlider()
        self.initStyleOption(option)
        handle = self.style().subControlRect(
            QStyle.ComplexControl.CC_Slider, option, QStyle.SubControl.SC_SliderHandle, self
        )
        if handle.contains(position):
            super().mousePressEvent(event)
            return
        value = self._value_at_position(position)
        self.setValue(value)
        # Jump and start dragging in the same press. QSlider's native groove
        # press never ran, so explicitly own this gesture until mouse release.
        self._groove_dragging = True
        self.setSliderDown(True)
        self._set_hovered_marker(None)
        self._emit_seek_requested(value)
        event.accept()

    def _value_at_position(self, position: QPoint) -> int:
        start, span, upside_down = self._slider_metrics()
        pixel = max(0, min(span, position.x() - start))
        return QStyle.sliderValueFromPosition(
            self.minimum(), self.maximum(), pixel, span, upside_down
        )

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if self._range_anchor is not None and event.button() == Qt.MouseButton.LeftButton:
            value = self._value_at_position(event.position().toPoint())
            start, end = sorted((self._range_anchor, value))
            self._range_anchor = None
            self.set_selection_range(start, end)
            self.range_selected.emit(start, end)
            event.accept()
            return
        was_dragging = self.isSliderDown()
        if self._groove_dragging and event.button() == Qt.MouseButton.LeftButton:
            # Include the final pointer location even if its last move event
            # was compressed; unchanged positions do not emit sliderMoved twice.
            self.setSliderPosition(self._value_at_position(event.position().toPoint()))
            self._groove_dragging = False
            self.setSliderDown(False)
            event.accept()
        else:
            super().mouseReleaseEvent(event)
        if was_dragging and self.sliderPosition() != self._last_seek_requested:
            # Always submit the final drag target, without decoding it twice
            # when sliderMoved already submitted that same position.
            self._emit_seek_requested(self.sliderPosition())


class PlaybackPanel(QWidget):
    seek_requested = Signal(int)
    relative_frames_requested = Signal(int)
    relative_seconds_requested = Signal(float)
    toggle_playback_requested = Signal()
    trigger_requested = Signal()
    collision_requested = Signal()
    speed_point_requested = Signal()
    speed_panel_toggled = Signal(bool)
    detection_panel_toggled = Signal(bool)
    range_selected = Signal(int, int)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("panel")
        self._fps = 0.0
        self._total_frames = 0
        self._frame = 0
        self._requested_frame: int | None = None
        self._height_timer = QTimer(self)
        self._height_timer.setSingleShot(True)
        self._height_timer.timeout.connect(self._update_minimum_height)
        self.setMinimumHeight(196)
        root = QVBoxLayout(self)
        root.setContentsMargins(18, 10, 18, 12)
        root.setSpacing(7)

        timeline_header = QHBoxLayout()
        self.time_label = QLabel("00:00.00 / 00:00.00", self)
        self.time_label.setObjectName("timelineTime")
        self.time_label.setStyleSheet("font-size: 14px; font-weight: 650;")
        timeline_header.addWidget(self.time_label)
        timeline_header.addStretch(1)
        self.timeline_legend = QLabel("◆ T 트리거 ▲ C 충돌  | 속도 지점", self)
        self.timeline_legend.setObjectName("timelineLegend")
        self.timeline_legend.setStyleSheet(f"color: {MUTED}; font-size: 11px;")
        self.timeline_legend.setWordWrap(False)
        self.timeline_legend.setSizePolicy(QSizePolicy.Policy.Minimum, QSizePolicy.Policy.Fixed)
        timeline_header.addWidget(self.timeline_legend)
        root.addLayout(timeline_header)

        self.timeline = MarkerSlider(self)
        self.timeline.setObjectName("videoTimeline")
        self.timeline.setRange(0, 0)
        self.timeline.setEnabled(False)
        self.timeline.seek_requested.connect(self.seek_requested)
        self.timeline.range_selected.connect(self.range_selected)
        root.addWidget(self.timeline)

        controls = QHBoxLayout()
        controls.setSpacing(6)
        controls.addStretch(1)
        self.back_1s = self._transport_button("replay", "1초 뒤로 이동")
        self.back_10f = self._transport_button("fast_rewind", "10프레임 뒤로")
        self.back_1f = self._transport_button("skip_previous", "이전 프레임")
        self.play_button = self._transport_button("play_arrow", "영상 재생")
        self.play_button.setObjectName("playPauseButton")
        self.play_button.setIconSize(QSize(26, 26))
        self.next_1f = self._transport_button("skip_next", "다음 프레임")
        self.next_10f = self._transport_button("fast_forward", "10프레임 앞으로")
        self.next_1s = self._transport_button("forward_media", "1초 앞으로 이동")
        for button in (
            self.back_1s,
            self.back_10f,
            self.back_1f,
            self.play_button,
            self.next_1f,
            self.next_10f,
            self.next_1s,
        ):
            controls.addWidget(button)
        controls.addStretch(1)
        root.addLayout(controls)

        frames = QWidget(self)
        bottom = QHBoxLayout(frames)
        bottom.setContentsMargins(0, 0, 0, 0)
        bottom.setSpacing(8)
        bottom.addWidget(QLabel("Frame", self))
        self.frame_input = QSpinBox(self)
        self.frame_input.setObjectName("frameInput")
        self.frame_input.setRange(1, 1)
        self.frame_input.setGroupSeparatorShown(True)
        self.frame_input.setMinimumWidth(78)
        self.frame_input.setEnabled(False)
        bottom.addWidget(self.frame_input)
        self.frame_total = QLabel("/ 0", self)
        self.frame_total.setStyleSheet(f"color: {MUTED};")
        bottom.addWidget(self.frame_total)
        self.go_button = QPushButton("이동", self)
        self.go_button.setObjectName("goFrameButton")
        self.go_button.setEnabled(False)
        bottom.addWidget(self.go_button)
        marks = QWidget(self)
        marker_row = QHBoxLayout(marks)
        marker_row.setContentsMargins(0, 0, 0, 0)
        marker_row.setSpacing(8)
        self.trigger_button = QPushButton(material_icon("flag", PRIMARY), "T 지정", self)
        self.trigger_button.setToolTip("현재 프레임의 트리거 마커 지정/해제")
        self.collision_button = QPushButton(material_icon("car_crash", PRIMARY), "충돌 지정", self)
        self.collision_button.setToolTip("현재 프레임의 충돌 마커 지정/해제")
        self.speed_point_button = QPushButton(material_icon("add_location", SPEED_POINT), "속도 지점", self)
        self.speed_point_button.setToolTip(
            "현재 프레임에 속도 기준 지점 추가/해제 (M)\n"
            "차량이 차선 끝, 횡단보도 줄 같은 기준을 지날 때마다 추가하세요."
        )
        self.speed_panel_button = QToolButton(self)
        self.speed_panel_button.setIcon(material_icon("speed", PRIMARY))
        self.speed_panel_button.setCheckable(True)
        self.speed_panel_button.setToolTip("속도 추정 패널 열기/닫기")
        self.speed_panel_button.setAccessibleName("속도 추정 패널")
        self.speed_panel_button.setFixedSize(36, 34)
        self.speed_panel_button.setStyleSheet("padding:0; min-height:0;")
        self.detection_panel_button = QToolButton(self)
        self.detection_panel_button.setObjectName("detectionPanelButton")
        self.detection_panel_button.setIcon(material_icon("frame_inspect", PRIMARY))
        self.detection_panel_button.setCheckable(True)
        self.detection_panel_button.setToolTip("객체 검출·라벨링 패널 열기/닫기")
        self.detection_panel_button.setAccessibleName("객체 검출 패널")
        self.detection_panel_button.setFixedSize(36, 34)
        self.detection_panel_button.setStyleSheet("padding:0; min-height:0;")
        for button in (
            self.trigger_button,
            self.collision_button,
            self.speed_point_button,
            self.speed_panel_button,
            self.detection_panel_button,
        ):
            marker_row.addWidget(button)
        self.input_rows = QBoxLayout(QBoxLayout.Direction.LeftToRight)
        self.input_rows.setSpacing(10)
        self.input_rows.addWidget(frames, 0, Qt.AlignmentFlag.AlignCenter)
        self.input_rows.addWidget(marks, 0, Qt.AlignmentFlag.AlignCenter)
        centered = QHBoxLayout()
        centered.addStretch(1)
        centered.addLayout(self.input_rows)
        centered.addStretch(1)
        root.addLayout(centered)

        self.back_1s.clicked.connect(lambda: self.relative_seconds_requested.emit(-1.0))
        self.back_10f.clicked.connect(lambda: self.relative_frames_requested.emit(-10))
        self.back_1f.clicked.connect(lambda: self.relative_frames_requested.emit(-1))
        self.play_button.clicked.connect(self.toggle_playback_requested)
        self.next_1f.clicked.connect(lambda: self.relative_frames_requested.emit(1))
        self.next_10f.clicked.connect(lambda: self.relative_frames_requested.emit(10))
        self.next_1s.clicked.connect(lambda: self.relative_seconds_requested.emit(1.0))
        self.trigger_button.clicked.connect(self.trigger_requested)
        self.collision_button.clicked.connect(self.collision_requested)
        self.speed_point_button.clicked.connect(self.speed_point_requested)
        self.speed_panel_button.toggled.connect(self.speed_panel_toggled)
        self.detection_panel_button.toggled.connect(self.detection_panel_toggled)
        self.go_button.clicked.connect(
            lambda: self.seek_requested.emit(self.frame_input.value() - 1)
        )
        self.frame_input.lineEdit().returnPressed.connect(
            lambda: self.seek_requested.emit(self.frame_input.value() - 1)
        )
        self.set_video_enabled(False)

    def _transport_button(self, icon: str, tooltip: str) -> QToolButton:
        button = QToolButton(self)
        button.setIcon(material_icon(icon, PRIMARY))
        button.setIconSize(QSize(20, 20))
        button.setProperty("transport", True)
        button.setProperty("materialIconName", icon)
        button.setToolTip(tooltip)
        button.setAccessibleName(tooltip)
        return button

    def resizeEvent(self, event) -> None:  # type: ignore[no-untyped-def]
        super().resizeEvent(event)
        self.input_rows.setDirection(
            QBoxLayout.Direction.TopToBottom if self.width() < 680 else QBoxLayout.Direction.LeftToRight
        )
        # Stacking the frame inputs and marker buttons adds a real second row.
        # Reserve its height so the video/speed area cannot squeeze the bottom
        # controls outside this panel in a narrow application window.
        self._height_timer.start(0)

    def _update_minimum_height(self) -> None:
        # Child QWidget size hints are recalculated after the direction change
        # reaches Qt's layout pass, rather than inside the parent's resize.
        self.layout().activate()
        self.setMinimumHeight(max(196, self.layout().minimumSize().height()))

    def set_video(self, total_frames: int, fps: float) -> None:
        self.timeline.set_selection_range(None)
        self.timeline.setSliderDown(False)
        self._requested_frame = None
        self._total_frames = max(0, int(total_frames))
        self._fps = max(0.0, float(fps))
        maximum = max(0, self._total_frames - 1)
        self.timeline.setRange(0, maximum)
        self.frame_input.setRange(1, max(1, self._total_frames))
        self.frame_total.setText(f"/ {self._total_frames:,}" if self._total_frames else "/ ?")
        self.set_video_enabled(True)
        self.set_position(0)

    def clear_video(self) -> None:
        self.timeline.setSliderDown(False)
        self._fps = 0.0
        self._total_frames = 0
        self._frame = 0
        self._requested_frame = None
        self.timeline.setRange(0, 0)
        self.timeline.setValue(0)
        self.frame_input.setRange(1, 1)
        self.frame_input.setValue(1)
        self.frame_total.setText("/ 0")
        self.time_label.setText("00:00.00 / 00:00.00")
        self.timeline.set_markers(None, None, [])
        self.timeline.set_selection_range(None)
        self.set_video_enabled(False)

    def update_total_frames(self, total_frames: int) -> None:
        self._total_frames = max(0, int(total_frames))
        if self._total_frames:
            self.timeline.setMaximum(max(0, self._total_frames - 1))
            self.frame_input.setMaximum(max(1, self._total_frames))
            self.frame_total.setText(f"/ {self._total_frames:,}")
        self.set_position(self._frame)

    def set_video_enabled(self, enabled: bool) -> None:
        for widget in (
            self.timeline,
            self.back_1s,
            self.back_10f,
            self.back_1f,
            self.play_button,
            self.next_1f,
            self.next_10f,
            self.next_1s,
            self.trigger_button,
            self.collision_button,
            self.speed_point_button,
            self.frame_input,
            self.go_button,
        ):
            widget.setEnabled(enabled)

    def set_position(self, frame: int) -> None:
        """Record a decoded position without pulling an active seek backward."""

        self._frame = max(0, int(frame))
        if self._requested_frame == self._frame:
            self._requested_frame = None
        if self.timeline.isSliderDown() or self._requested_frame is not None:
            return
        self._display_position(self._frame)

    def set_requested_position(self, frame: int) -> None:
        """Immediately reflect the user's target while decoding is in flight."""

        target = max(0, int(frame))
        if self._total_frames:
            target = min(target, self._total_frames - 1)
        self._requested_frame = target
        self._display_position(target)

    def clear_pending_seek(self) -> None:
        """Restore the last decoded position if a requested seek is cancelled."""

        self._requested_frame = None
        if not self.timeline.isSliderDown():
            self._display_position(self._frame)

    def _display_position(self, frame: int) -> None:
        if not self._total_frames:
            self.timeline.setMaximum(max(self.timeline.maximum(), frame))
            self.frame_input.setMaximum(max(self.frame_input.maximum(), frame + 1))
        blocked = self.timeline.blockSignals(True)
        self.timeline.setValue(frame)
        self.timeline.blockSignals(blocked)
        self.frame_input.setValue(frame + 1)
        current_seconds = frame / self._fps if self._fps else 0.0
        if not self._total_frames:
            self.time_label.setText(f"{format_timestamp(current_seconds)} / --:--.--")
        else:
            total_seconds = max(0, self._total_frames - 1) / self._fps if self._fps else 0.0
            self.time_label.setText(
                f"{format_timestamp(current_seconds)} / {format_timestamp(total_seconds)}"
            )

    def set_playing(self, playing: bool) -> None:
        icon_name = "pause" if playing else "play_arrow"
        self.play_button.setIcon(material_icon(icon_name, PRIMARY))
        self.play_button.setProperty("materialIconName", icon_name)
        label = "영상 일시정지" if playing else "영상 재생"
        self.play_button.setToolTip(label)
        self.play_button.setAccessibleName(label)

    def set_markers(
        self,
        trigger: int | None,
        collision: int | None,
        speed_frames: list[int] | None = None,
    ) -> None:
        self.timeline.set_markers(trigger, collision, speed_frames)

    def set_selection_range(self, start: int | None, end: int | None = None) -> None:
        self.timeline.set_selection_range(start, end)

    @property
    def total_frames(self) -> int:
        return self._total_frames


__all__ = ["MarkerSlider", "PlaybackPanel", "format_timestamp"]
