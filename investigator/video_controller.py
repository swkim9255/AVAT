"""Frame-accurate OpenCV playback controller isolated from widgets."""

from __future__ import annotations

import math
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Protocol

import cv2
import numpy as np
from PySide6.QtCore import QObject, Qt, QTimer, Signal, Slot
from PySide6.QtGui import QImage

DEFAULT_FPS = 30.0


class PlaybackState(StrEnum):
    EMPTY = "empty"
    STOPPED = "stopped"
    PLAYING = "playing"
    PAUSED = "paused"


@dataclass(frozen=True, slots=True)
class VideoMetadata:
    path: Path
    fps: float
    total_frames: int
    width: int
    height: int

    @property
    def duration_seconds(self) -> float:
        return self.total_frames / self.fps if self.total_frames else 0.0

    @property
    def last_frame(self) -> int:
        return max(0, self.total_frames - 1)


class CaptureProtocol(Protocol):
    def isOpened(self) -> bool: ...  # noqa: N802
    def read(self) -> tuple[bool, np.ndarray | None]: ...
    def get(self, prop: int) -> float: ...
    def set(self, prop: int, value: float) -> bool: ...
    def release(self) -> None: ...


class VideoController(QObject):
    video_opened = Signal(object)
    video_closed = Signal()
    frame_ready = Signal(QImage, int, float)
    position_changed = Signal(int, int)
    total_frames_changed = Signal(int)
    state_changed = Signal(object)
    error_occurred = Signal(str)
    playback_finished = Signal()

    def __init__(
        self,
        parent: QObject | None = None,
        *,
        capture_factory: Callable[[str], CaptureProtocol] | None = None,
    ) -> None:
        super().__init__(parent)
        self._capture_factory = capture_factory or cv2.VideoCapture
        self._capture: CaptureProtocol | None = None
        self._metadata: VideoMetadata | None = None
        self._state = PlaybackState.EMPTY
        self._current_frame = -1
        self._current_image: QImage | None = None
        self._timer = QTimer(self)
        self._timer.setTimerType(Qt.TimerType.PreciseTimer)
        self._timer.timeout.connect(self._next_for_playback)

    @property
    def metadata(self) -> VideoMetadata | None:
        return self._metadata

    @property
    def state(self) -> PlaybackState:
        return self._state

    @property
    def is_open(self) -> bool:
        return self._capture is not None and self._metadata is not None

    @property
    def is_playing(self) -> bool:
        return self._state is PlaybackState.PLAYING

    @property
    def current_frame(self) -> int:
        return self._current_frame

    @property
    def fps(self) -> float:
        return self._metadata.fps if self._metadata else 0.0

    @property
    def total_frames(self) -> int:
        return self._metadata.total_frames if self._metadata else 0

    def current_image(self) -> QImage | None:
        return self._current_image.copy() if self._current_image else None

    @Slot(str, result=bool)
    def open_video(self, path: str | Path) -> bool:
        try:
            resolved = Path(path).expanduser().resolve()
        except (OSError, TypeError, ValueError):
            self.error_occurred.emit("영상 경로가 올바르지 않습니다.")
            return False
        if not resolved.is_file():
            self.error_occurred.emit(f"영상 파일이 존재하지 않습니다: {resolved}")
            return False

        candidate: CaptureProtocol | None = None
        try:
            candidate = self._capture_factory(str(resolved))
            if not candidate.isOpened():
                raise RuntimeError("open failed")
            ok, bgr = candidate.read()
            if not ok or bgr is None:
                raise RuntimeError("decode failed")
            fps = self._read_fps(candidate)
            image = self._to_qimage(bgr)
            count = self._read_frame_count(candidate)
            metadata = VideoMetadata(resolved, fps, count, image.width(), image.height())
        except Exception:
            self._safe_release(candidate)
            self.error_occurred.emit(f"영상을 열거나 첫 프레임을 읽을 수 없습니다: {resolved.name}")
            return False

        self.close_video()
        self._capture = candidate
        self._metadata = metadata
        self._current_frame = 0
        self._current_image = image
        self._timer.setInterval(max(1, round(1000 / fps)))
        self._set_state(PlaybackState.STOPPED)
        self.video_opened.emit(metadata)
        self.frame_ready.emit(image.copy(), 0, 0.0)
        self.position_changed.emit(0, count)
        return True

    @Slot()
    def close_video(self) -> None:
        had_video = self._capture is not None
        self._timer.stop()
        self._safe_release(self._capture)
        self._capture = None
        self._metadata = None
        self._current_frame = -1
        self._current_image = None
        self._set_state(PlaybackState.EMPTY)
        if had_video:
            self.video_closed.emit()

    @Slot(result=bool)
    def toggle_playback(self) -> bool:
        return self.pause() if self.is_playing else self.play()

    @Slot(result=bool)
    def play(self) -> bool:
        if not self.is_open:
            return False
        assert self._metadata is not None
        if (
            self._metadata.total_frames
            and self._current_frame >= self._metadata.last_frame
            and not self.seek(0)
        ):
            return False
        self._timer.start()
        self._set_state(PlaybackState.PLAYING)
        return True

    @Slot(result=bool)
    def pause(self) -> bool:
        if not self.is_open:
            return False
        self._timer.stop()
        self._set_state(PlaybackState.PAUSED)
        return True

    @Slot(int, result=bool)
    def seek(self, frame: int) -> bool:
        if not self.is_open:
            return False
        assert self._metadata is not None and self._capture is not None
        target = max(0, int(frame))
        if self._metadata.total_frames:
            target = min(target, self._metadata.last_frame)
        try:
            if not self._capture.set(cv2.CAP_PROP_POS_FRAMES, float(target)):
                raise RuntimeError("seek failed")
            ok, bgr = self._capture.read()
            if not ok or bgr is None:
                raise RuntimeError("decode failed")
            image = self._to_qimage(bgr)
        except Exception:
            self.error_occurred.emit(f"{target + 1:,}번 프레임으로 이동할 수 없습니다.")
            return False
        self._publish(image, target)
        return True

    @Slot(int, result=bool)
    def seek_relative(self, frames: int) -> bool:
        if not self.is_open:
            return False
        return self.seek(max(0, self._current_frame) + int(frames))

    @Slot(float, result=bool)
    def seek_seconds(self, seconds: float) -> bool:
        if not self.is_open:
            return False
        return self.seek_relative(round(float(seconds) * self.fps))

    @Slot(result=bool)
    def previous_frame(self) -> bool:
        if self.is_playing:
            self.pause()
        return self.seek_relative(-1)

    @Slot(result=bool)
    def next_frame(self) -> bool:
        if self.is_playing:
            self.pause()
        return self.seek_relative(1)

    @Slot()
    def _next_for_playback(self) -> None:
        if not self.is_open or not self.is_playing:
            return
        assert self._capture is not None and self._metadata is not None
        next_frame = self._current_frame + 1
        if self._metadata.total_frames and next_frame >= self._metadata.total_frames:
            self._finish()
            return
        try:
            ok, bgr = self._capture.read()
            if not ok or bgr is None:
                self._finish()
                return
            self._publish(self._to_qimage(bgr), next_frame)
        except Exception:
            self.error_occurred.emit(f"{next_frame + 1:,}번 프레임을 읽을 수 없습니다.")
            self._finish()

    def _publish(self, image: QImage, frame: int) -> None:
        self._current_frame = frame
        self._current_image = image.copy()
        timestamp = frame / self.fps if self.fps else 0.0
        self.frame_ready.emit(image.copy(), frame, timestamp)
        self.position_changed.emit(frame, self.total_frames)

    def _finish(self) -> None:
        self._timer.stop()
        if self._metadata is not None and self._metadata.total_frames == 0:
            observed_total = max(0, self._current_frame + 1)
            self._metadata = VideoMetadata(
                self._metadata.path,
                self._metadata.fps,
                observed_total,
                self._metadata.width,
                self._metadata.height,
            )
            self.total_frames_changed.emit(observed_total)
        self._set_state(PlaybackState.STOPPED)
        self.playback_finished.emit()

    def _set_state(self, state: PlaybackState) -> None:
        if state is self._state:
            return
        self._state = state
        self.state_changed.emit(state)

    @staticmethod
    def _to_qimage(frame: np.ndarray) -> QImage:
        array = np.asarray(frame)
        if array.ndim == 2:
            rgb = cv2.cvtColor(array, cv2.COLOR_GRAY2RGB)
        elif array.ndim == 3 and array.shape[2] == 3:
            rgb = cv2.cvtColor(array, cv2.COLOR_BGR2RGB)
        elif array.ndim == 3 and array.shape[2] == 4:
            rgb = cv2.cvtColor(array, cv2.COLOR_BGRA2RGB)
        else:
            raise ValueError(f"지원하지 않는 프레임 형식입니다: {array.shape}")
        rgb = np.ascontiguousarray(rgb, dtype=np.uint8)
        borrowed = QImage(
            rgb.data, rgb.shape[1], rgb.shape[0], rgb.strides[0], QImage.Format.Format_RGB888
        )
        return borrowed.copy()

    @staticmethod
    def _read_fps(capture: CaptureProtocol) -> float:
        with suppress(Exception):
            fps = float(capture.get(cv2.CAP_PROP_FPS))
            if math.isfinite(fps) and fps > 0:
                return fps
        return DEFAULT_FPS

    @staticmethod
    def _read_frame_count(capture: CaptureProtocol) -> int:
        with suppress(Exception):
            count = float(capture.get(cv2.CAP_PROP_FRAME_COUNT))
            if math.isfinite(count) and count > 0:
                return max(1, int(round(count)))
        return 0

    @staticmethod
    def _safe_release(capture: CaptureProtocol | None) -> None:
        if capture is not None:
            with suppress(Exception):
                capture.release()


__all__ = ["DEFAULT_FPS", "PlaybackState", "VideoController", "VideoMetadata"]
