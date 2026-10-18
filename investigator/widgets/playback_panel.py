"""Marked timeline and frame-accurate transport controls."""

from __future__ import annotations

from typing import Literal

from PySide6.QtCore import QEvent, QPoint, QRect, QSize, Qt, Signal
from PySide6.QtGui import QColor, QMouseEvent, QPainter, QPolygon
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSlider,
    QSpinBox,
    QStyle,
    QStyleOptionSlider,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from investigator.theme import DANGER, MUTED, PRIMARY, PRIMARY_HOVER, material_icon

MarkerKind = Literal["trigger", "collision"]
COLLISION_HOVER = "#EA580C"
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

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(Qt.Orientation.Horizontal, parent)
        self.setMinimumHeight(56)
        self.setTracking(True)
        self.setMouseTracking(True)
        self.setAccessibleName("영상 타임라인")
        self.trigger_frame: int | None = None
        self.collision_frame: int | None = None
        self._hovered_marker: MarkerKind | None = None
        self.sliderMoved.connect(self.seek_requested)

    def set_markers(self, trigger: int | None, collision: int | None) -> None:
        self.trigger_frame = trigger
        self.collision_frame = collision
        self._set_hovered_marker(None)
        descriptions = []
        if trigger is not None:
            descriptions.append(f"T 트리거 {int(trigger) + 1:,}번 프레임")
        if collision is not None:
            descriptions.append(f"C 충돌 {int(collision) + 1:,}번 프레임")
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
        return QRect(x + 14, 1, 24, 18).intersected(self.rect())

    def _collision_label_rect(self) -> QRect | None:
        if self.collision_frame is None:
            return None
        x = self._x_for_value(self.collision_frame)
        return QRect(x + 14, self.height() - 22, 32, 18).intersected(self.rect())

    def _marker_at(self, position: QPoint) -> tuple[MarkerKind, int] | None:
        trigger_rect = self._trigger_interaction_rect()
        trigger_label = self._trigger_label_rect()
        if (trigger_rect is not None and trigger_rect.contains(position)) or (
            trigger_label is not None and trigger_label.contains(position)
        ):
            assert self.trigger_frame is not None
            return "trigger", self.trigger_frame
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
        return QColor(COLLISION_HOVER if self._hovered_marker == marker else DANGER)

    def _set_hovered_marker(self, marker: MarkerKind | None) -> None:
        if marker == self._hovered_marker:
            return
        self._hovered_marker = marker
        if marker is None:
            self.unsetCursor()
            self.setToolTip("")
        else:
            frame = self.trigger_frame if marker == "trigger" else self.collision_frame
            label = "T 트리거" if marker == "trigger" else "C 충돌"
            self.setCursor(Qt.CursorShape.PointingHandCursor)
            self.setToolTip(f"{label} · {int(frame) + 1:,}번 프레임 (클릭하여 이동)")
        self.update()

    def _seek_to_marker(self, frame: int, event: QMouseEvent) -> None:
        target = max(self.minimum(), min(self.maximum(), int(frame)))
        self.setValue(target)
        self.seek_requested.emit(target)
        event.accept()

    def paintEvent(self, event) -> None:  # type: ignore[no-untyped-def]
        super().paintEvent(event)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
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
            painter.drawText(QRect(x + 16, 1, 24, 18), "T")
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
        start, span, upside_down = self._slider_metrics()
        slider_position = max(0, min(span, round(event.position().x()) - start))
        value = QStyle.sliderValueFromPosition(
            self.minimum(),
            self.maximum(),
            slider_position,
            span,
            upside_down,
        )
        self.setValue(value)
        self.seek_requested.emit(value)
        event.accept()


class PlaybackPanel(QWidget):
    seek_requested = Signal(int)
    relative_frames_requested = Signal(int)
    relative_seconds_requested = Signal(float)
    toggle_playback_requested = Signal()
    trigger_requested = Signal()
    collision_requested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("panel")
        self._fps = 0.0
        self._total_frames = 0
        self._frame = 0
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
        legend = QLabel("◆ T 트리거    ▼ C 충돌", self)
        legend.setStyleSheet(f"color: {MUTED}; font-size: 11px;")
        timeline_header.addWidget(legend)
        root.addLayout(timeline_header)

        self.timeline = MarkerSlider(self)
        self.timeline.setObjectName("videoTimeline")
        self.timeline.setRange(0, 0)
        self.timeline.setEnabled(False)
        self.timeline.seek_requested.connect(self.seek_requested)
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

        bottom = QHBoxLayout()
        bottom.setSpacing(8)
        bottom.addStretch(1)
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
        bottom.addSpacing(8)
        self.trigger_button = QPushButton(material_icon("flag", PRIMARY), "T 지정", self)
        self.trigger_button.setToolTip("현재 프레임의 트리거 마커 지정/해제")
        self.collision_button = QPushButton(material_icon("car_crash", PRIMARY), "충돌 지정", self)
        self.collision_button.setToolTip("현재 프레임의 충돌 마커 지정/해제")
        bottom.addWidget(self.trigger_button)
        bottom.addWidget(self.collision_button)
        bottom.addStretch(1)
        root.addLayout(bottom)

        self.back_1s.clicked.connect(lambda: self.relative_seconds_requested.emit(-1.0))
        self.back_10f.clicked.connect(lambda: self.relative_frames_requested.emit(-10))
        self.back_1f.clicked.connect(lambda: self.relative_frames_requested.emit(-1))
        self.play_button.clicked.connect(self.toggle_playback_requested)
        self.next_1f.clicked.connect(lambda: self.relative_frames_requested.emit(1))
        self.next_10f.clicked.connect(lambda: self.relative_frames_requested.emit(10))
        self.next_1s.clicked.connect(lambda: self.relative_seconds_requested.emit(1.0))
        self.trigger_button.clicked.connect(self.trigger_requested)
        self.collision_button.clicked.connect(self.collision_requested)
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

    def set_video(self, total_frames: int, fps: float) -> None:
        self._total_frames = max(0, int(total_frames))
        self._fps = max(0.0, float(fps))
        maximum = max(0, self._total_frames - 1)
        self.timeline.setRange(0, maximum)
        self.frame_input.setRange(1, max(1, self._total_frames))
        self.frame_total.setText(f"/ {self._total_frames:,}" if self._total_frames else "/ ?")
        self.set_video_enabled(True)
        self.set_position(0)

    def clear_video(self) -> None:
        self._fps = 0.0
        self._total_frames = 0
        self._frame = 0
        self.timeline.setRange(0, 0)
        self.timeline.setValue(0)
        self.frame_input.setRange(1, 1)
        self.frame_input.setValue(1)
        self.frame_total.setText("/ 0")
        self.time_label.setText("00:00.00 / 00:00.00")
        self.timeline.set_markers(None, None)
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
            self.frame_input,
            self.go_button,
        ):
            widget.setEnabled(enabled)

    def set_position(self, frame: int) -> None:
        self._frame = max(0, int(frame))
        if not self._total_frames:
            self.timeline.setMaximum(max(self.timeline.maximum(), self._frame))
            self.frame_input.setMaximum(max(self.frame_input.maximum(), self._frame + 1))
        self.timeline.blockSignals(True)
        self.timeline.setValue(self._frame)
        self.timeline.blockSignals(False)
        self.frame_input.setValue(self._frame + 1)
        current_seconds = self._frame / self._fps if self._fps else 0.0
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

    def set_markers(self, trigger: int | None, collision: int | None) -> None:
        self.timeline.set_markers(trigger, collision)


__all__ = ["MarkerSlider", "PlaybackPanel", "format_timestamp"]
