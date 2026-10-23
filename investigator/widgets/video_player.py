"""Video canvas with an AVAT-derived restrained presentation."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import QPointF, QRect, QRectF, QSize, Qt, Signal
from PySide6.QtGui import (
    QColor,
    QDragEnterEvent,
    QDropEvent,
    QFontMetrics,
    QImage,
    QMouseEvent,
    QPainter,
    QPen,
    QPixmap,
    QTransform,
)
from PySide6.QtWidgets import QHBoxLayout, QLabel, QToolButton, QVBoxLayout, QWidget

from investigator.theme import MUTED, PRIMARY, VIDEO_BACKGROUND, material_icon
from investigator.video_import import VIDEO_EXTENSIONS


@dataclass(frozen=True, slots=True)
class OverlayBox:
    """An object box in source-video pixel coordinates (x, y, width, height)."""

    uid: str
    bbox: tuple[float, float, float, float]
    color: str
    text: str
    selected: bool = False


class VideoCanvas(QWidget):
    files_dropped = Signal(list)
    box_clicked = Signal(str)  # empty string: clicked outside every box
    box_drawn = Signal(object)  # (x, y, width, height) in source-video pixels

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("videoCanvas")
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setAcceptDrops(True)
        self.setMinimumSize(420, 280)
        self.setStyleSheet(f"background: {VIDEO_BACKGROUND}; border: 0;")
        self._image: QImage | None = None
        self._source_image: QImage | None = None
        # Smoothly scaled frame for the current widget size. Overlay, hover and
        # box-drawing repaints reuse it instead of rescaling the full frame.
        self._scaled: QPixmap | None = None
        self._scaled_key: tuple[int, int, int] | None = None
        self._rotation_degrees = 0
        self._placeholder_icon = material_icon("video_file", "#94A3B8")
        self._overlay: list[OverlayBox] = []
        self._draw_mode = False
        self._draw_start: QPointF | None = None
        self._draw_current: QPointF | None = None

    # -- object overlay -------------------------------------------------------

    def set_overlay(self, boxes: list[OverlayBox]) -> None:
        self._overlay = list(boxes)
        self.update()

    @property
    def overlay(self) -> list[OverlayBox]:
        return list(self._overlay)

    def set_draw_mode(self, enabled: bool) -> None:
        self._draw_mode = bool(enabled)
        self._draw_start = self._draw_current = None
        if enabled:
            self.setCursor(Qt.CursorShape.CrossCursor)
        else:
            self.unsetCursor()
        self.update()

    def cancel_drawing(self) -> bool:
        """Discard an unfinished gesture without creating or editing an object."""
        active = self._draw_start is not None
        self._draw_start = self._draw_current = None
        if active:
            self.update()
        return active

    @property
    def draw_mode(self) -> bool:
        return self._draw_mode

    def _source_size(self) -> tuple[int, int] | None:
        if self._source_image is None or self._source_image.isNull():
            return None
        return self._source_image.width(), self._source_image.height()

    def _image_rect(self) -> tuple[QRectF, float] | None:
        """Displayed image rectangle and the source-to-widget scale."""

        if self._image is None or self._image.isNull():
            return None
        width = self._image.width()
        # Use Qt's integer aspect-ratio rounding, exactly like the painted
        # pixmap. Mapping through a separate float calculation drifts at edges.
        shown = self._image.size().scaled(self.size(), Qt.AspectRatioMode.KeepAspectRatio)
        shown_w, shown_h = shown.width(), shown.height()
        if min(shown_w, shown_h) <= 0:
            return None
        x = (self.width() - shown_w) // 2
        y = (self.height() - shown_h) // 2
        return QRectF(x, y, shown_w, shown_h), shown_w / width

    def source_to_widget(self, x: float, y: float) -> QPointF | None:
        size = self._source_size()
        geometry = self._image_rect()
        if size is None or geometry is None:
            return None
        width, height = size
        rotation = self._rotation_degrees
        if rotation == 90:
            dx, dy = height - y, x
        elif rotation == 180:
            dx, dy = width - x, height - y
        elif rotation == 270:
            dx, dy = y, width - x
        else:
            dx, dy = x, y
        target, scale = geometry
        scale_y = target.height() / self._image.height()
        return QPointF(target.x() + dx * scale, target.y() + dy * scale_y)

    def widget_to_source(self, point: QPointF) -> QPointF | None:
        size = self._source_size()
        geometry = self._image_rect()
        if size is None or geometry is None:
            return None
        width, height = size
        target, scale = geometry
        dx = (point.x() - target.x()) / scale
        scale_y = target.height() / self._image.height()
        dy = (point.y() - target.y()) / scale_y
        rotation = self._rotation_degrees
        if rotation == 90:
            x, y = dy, height - dx
        elif rotation == 180:
            x, y = width - dx, height - dy
        elif rotation == 270:
            x, y = width - dy, dx
        else:
            x, y = dx, dy
        return QPointF(min(max(x, 0.0), width), min(max(y, 0.0), height))

    def _box_widget_rect(self, bbox: tuple[float, float, float, float]) -> QRectF | None:
        x, y, w, h = bbox
        first = self.source_to_widget(x, y)
        second = self.source_to_widget(x + w, y + h)
        if first is None or second is None:
            return None
        return QRectF(first, second).normalized()

    def _paint_overlay(self, painter: QPainter) -> None:
        metrics = QFontMetrics(painter.font())
        ordered = sorted(self._overlay, key=lambda box: box.selected)
        for box in ordered:
            rect = self._box_widget_rect(box.bbox)
            if rect is None:
                continue
            color = QColor(box.color)
            painter.save()
            if box.selected:
                fill = QColor(color)
                fill.setAlpha(42)
                painter.fillRect(rect, fill)
            painter.setPen(QPen(color, 3 if box.selected else 2))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRect(rect)
            text = metrics.elidedText(box.text, Qt.TextElideMode.ElideRight, 260)
            tag = QRectF(0, 0, metrics.horizontalAdvance(text) + 10, metrics.height() + 4)
            top = rect.top() - tag.height()
            left = min(max(0.0, rect.left()), max(0.0, self.width() - tag.width()))
            tag.moveTo(left, top if top >= 0 else rect.top())
            painter.fillRect(tag, color)
            painter.setPen(QColor("#FFFFFF"))
            painter.drawText(tag, Qt.AlignmentFlag.AlignCenter, text)
            painter.restore()
        if self._draw_start is not None and self._draw_current is not None:
            painter.save()
            painter.setPen(QPen(QColor("#FACC15"), 2, Qt.PenStyle.DashLine))
            painter.drawRect(QRectF(self._draw_start, self._draw_current).normalized())
            painter.restore()

    def box_at(self, point: QPointF) -> str:
        hits = []
        for box in self._overlay:
            rect = self._box_widget_rect(box.bbox)
            if rect is not None and rect.adjusted(-3, -3, 3, 3).contains(point):
                hits.append((rect.width() * rect.height(), box.uid))
        return min(hits)[1] if hits else ""

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() != Qt.MouseButton.LeftButton or self._image is None:
            super().mousePressEvent(event)
            return
        self.setFocus(Qt.FocusReason.MouseFocusReason)
        point = event.position()
        geometry = self._image_rect()
        if self._draw_mode and geometry is not None and geometry[0].contains(point):
            self._draw_start = self._draw_current = point
            event.accept()
            return
        if not self._draw_mode:
            self.box_clicked.emit(self.box_at(point))
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if self._draw_start is not None:
            geometry = self._image_rect()
            point = event.position()
            if geometry is not None:
                area = geometry[0]
                point = QPointF(
                    min(max(point.x(), area.left()), area.right()),
                    min(max(point.y(), area.top()), area.bottom()),
                )
            self._draw_current = point
            self.update()
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if self._draw_start is not None and event.button() == Qt.MouseButton.LeftButton:
            start = self.widget_to_source(self._draw_start)
            # A quick gesture can have its last move coalesced. The release
            # position is authoritative, just as it is for timeline scrubbing.
            end = self.widget_to_source(event.position())
            self._draw_start = self._draw_current = None
            self.update()
            if start is not None and end is not None:
                rect = QRectF(start, end).normalized()
                if rect.width() >= 4 and rect.height() >= 4:
                    self.box_drawn.emit((rect.x(), rect.y(), rect.width(), rect.height()))
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def set_frame(self, image: QImage) -> None:
        # Do not attach an unfinished drag to a different displayed frame.
        self._draw_start = self._draw_current = None
        # QImage is copy-on-write: sharing the decoded frame avoids a full-frame
        # copy per displayed frame, and a later writer detaches its own copy.
        self._source_image = QImage(image)
        self._update_display_image()

    @property
    def rotation_degrees(self) -> int:
        return self._rotation_degrees

    def set_rotation(self, degrees: int) -> None:
        normalized = int(degrees) % 360
        if normalized not in {0, 90, 180, 270}:
            raise ValueError("영상 회전 각도는 90도 단위여야 합니다.")
        if normalized == self._rotation_degrees:
            return
        self._draw_start = self._draw_current = None
        self._rotation_degrees = normalized
        self._update_display_image()

    def _update_display_image(self) -> None:
        if self._source_image is None:
            self._image = None
        elif self._rotation_degrees:
            self._image = self._source_image.transformed(
                QTransform().rotate(self._rotation_degrees),
                Qt.TransformationMode.SmoothTransformation,
            )
        else:
            self._image = self._source_image
        self.update()

    def clear(self) -> None:
        self._source_image = None
        self._image = None
        self._scaled = self._scaled_key = None
        self._overlay = []
        self._draw_start = self._draw_current = None
        self.set_draw_mode(False)
        self.update()

    def paintEvent(self, event) -> None:  # type: ignore[no-untyped-def]
        del event
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor(VIDEO_BACKGROUND))
        if self._image is None or self._image.isNull():
            icon_size = 52
            icon_rect = QRect(
                (self.width() - icon_size) // 2,
                max(20, self.height() // 2 - 58),
                icon_size,
                icon_size,
            )
            self._placeholder_icon.paint(painter, icon_rect)
            painter.setPen(QColor("#CBD5E1"))
            painter.drawText(
                QRect(20, icon_rect.bottom() + 16, self.width() - 40, 28),
                Qt.AlignmentFlag.AlignCenter,
                "영상을 추가하거나 이곳에 놓아주세요",
            )
            painter.setPen(QColor("#64748B"))
            painter.drawText(
                QRect(20, icon_rect.bottom() + 43, self.width() - 40, 22),
                Qt.AlignmentFlag.AlignCenter,
                "MP4 · AVI · MOV · MKV",
            )
            return
        available = self.rect()
        key = (self._image.cacheKey(), available.width(), available.height())
        if self._scaled is None or self._scaled_key != key:
            self._scaled = QPixmap.fromImage(self._image).scaled(
                available.size(),
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
            self._scaled_key = key
        scaled = self._scaled
        x = (available.width() - scaled.width()) // 2
        y = (available.height() - scaled.height()) // 2
        painter.drawPixmap(x, y, scaled)
        if self._overlay or self._draw_start is not None:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            self._paint_overlay(painter)

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:
        urls = event.mimeData().urls()
        if any(Path(url.toLocalFile()).suffix.casefold() in VIDEO_EXTENSIONS for url in urls):
            event.acceptProposedAction()

    def dropEvent(self, event: QDropEvent) -> None:
        paths = [
            url.toLocalFile()
            for url in event.mimeData().urls()
            if Path(url.toLocalFile()).suffix.casefold() in VIDEO_EXTENSIONS
        ]
        if paths:
            self.files_dropped.emit(paths)
            event.acceptProposedAction()


class VideoPlayerPanel(QWidget):
    files_dropped = Signal(list)
    rotation_requested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("panel")
        self.setMinimumWidth(450)
        root = QVBoxLayout(self)
        root.setContentsMargins(16, 16, 16, 16)
        root.setSpacing(10)
        heading = QHBoxLayout()
        title = QLabel("영상 플레이어", self)
        title.setObjectName("sectionTitle")
        heading.addWidget(title)
        heading.addStretch(1)
        self.video_info = QLabel("선택된 영상 없음", self)
        self.video_info.setStyleSheet(f"color: {MUTED};")
        self.video_info.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        heading.addWidget(self.video_info)
        self.rotate_button = QToolButton(self)
        self.rotate_button.setObjectName("rotateVideo90Button")
        self.rotate_button.setIcon(material_icon("rotate_90_degrees_cw", PRIMARY))
        self.rotate_button.setIconSize(QSize(20, 20))
        self.rotate_button.setFixedSize(36, 36)
        self.rotate_button.setEnabled(False)
        self.rotate_button.setToolTip("시계 방향으로 90° 회전 · 현재 원본 방향")
        self.rotate_button.setAccessibleName("영상 90도 회전")
        self.rotate_button.setStyleSheet("QToolButton#rotateVideo90Button { padding: 0; }")
        self.rotate_button.clicked.connect(lambda checked=False: self.rotation_requested.emit())
        heading.addWidget(self.rotate_button)
        root.addLayout(heading)
        self.canvas = VideoCanvas(self)
        self.canvas.files_dropped.connect(self.files_dropped)
        root.addWidget(self.canvas, 1)

    def set_video_name(
        self, path: str | Path | None, resolution: tuple[int, int] | None = None
    ) -> None:
        if path is None:
            self.video_info.setText("선택된 영상 없음")
            self.video_info.setToolTip("")
            self.rotate_button.setEnabled(False)
            return
        source = Path(path)
        suffix = f" · {resolution[0]}×{resolution[1]}" if resolution else ""
        self.video_info.setText(source.name + suffix)
        self.video_info.setToolTip(str(source))
        self.rotate_button.setEnabled(True)

    def set_rotation(self, degrees: int) -> None:
        normalized = int(degrees) % 360
        self.canvas.set_rotation(normalized)
        current = f"{normalized}°" if normalized else "원본 방향"
        self.rotate_button.setToolTip(f"시계 방향으로 90° 회전 · 현재 {current}")

    def clear(self) -> None:
        self.canvas.clear()
        self.set_rotation(0)
        self.set_video_name(None)


__all__ = ["VIDEO_EXTENSIONS", "OverlayBox", "VideoCanvas", "VideoPlayerPanel"]
