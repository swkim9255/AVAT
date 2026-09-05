"""Video canvas with an AVAT-derived restrained presentation."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QRect, Qt, Signal
from PySide6.QtGui import QColor, QDragEnterEvent, QDropEvent, QImage, QPainter, QPixmap
from PySide6.QtWidgets import QHBoxLayout, QLabel, QVBoxLayout, QWidget

from investigator.theme import MUTED, VIDEO_BACKGROUND, material_icon
from investigator.video_import import VIDEO_EXTENSIONS


class VideoCanvas(QWidget):
    files_dropped = Signal(list)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("videoCanvas")
        self.setAcceptDrops(True)
        self.setMinimumSize(420, 280)
        self.setStyleSheet(f"background: {VIDEO_BACKGROUND}; border: 0;")
        self._image: QImage | None = None
        self._placeholder_icon = material_icon("video_file", "#94A3B8")

    def set_frame(self, image: QImage) -> None:
        self._image = image.copy()
        self.update()

    def clear(self) -> None:
        self._image = None
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
        scaled = QPixmap.fromImage(self._image).scaled(
            available.size(),
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        x = (available.width() - scaled.width()) // 2
        y = (available.height() - scaled.height()) // 2
        painter.drawPixmap(x, y, scaled)

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
            return
        source = Path(path)
        suffix = f" · {resolution[0]}×{resolution[1]}" if resolution else ""
        self.video_info.setText(source.name + suffix)
        self.video_info.setToolTip(str(source))

    def clear(self) -> None:
        self.canvas.clear()
        self.set_video_name(None)


__all__ = ["VIDEO_EXTENSIONS", "VideoCanvas", "VideoPlayerPanel"]
