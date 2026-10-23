"""Frame-accurate OpenCV playback controller isolated from widgets."""

from __future__ import annotations

import logging
import math
import os
from collections import OrderedDict
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

from investigator.audio_playback import AudioPlayback
from investigator.diagnostics import process_memory_bytes
from investigator.video_capture import open_video_capture
from investigator.video_decoder import ProcessDecoder

LOGGER = logging.getLogger(__name__)
DEFAULT_FPS = 30.0
DECODER_THREADS_VARIABLE = "AVAT_DECODER_THREADS"


def configured_decoder_threads() -> int:
    """Return the FFmpeg decoder thread setting; 0 keeps OpenCV's automatic choice.

    Automatic mode uses one decoder thread per logical core (at most 16), so
    scrubbing can briefly load every core. AVAT_DECODER_THREADS (for example 2)
    caps that on a slow or overheating PC for diagnosis; invalid values are ignored.
    """
    raw = os.environ.get(DECODER_THREADS_VARIABLE, "").strip()
    try:
        threads = int(raw) if raw else 0
    except ValueError:
        return 0
    return min(max(threads, 0), 16)


class PlaybackState(StrEnum):
    EMPTY = "empty"
    STOPPED = "stopped"
    PLAYING = "playing"
    PAUSED = "paused"


@dataclass(frozen=True, slots=True)
class VideoMetadata:
    path: Path
    # Playback rate, including the fallback used for media without FPS metadata.
    fps: float
    total_frames: int
    width: int
    height: int
    # Actual metadata only: zero means unavailable and must not imply 30 FPS.
    reported_fps: float = 0.0

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
    seek_requested_position = Signal(int)
    seek_cancelled = Signal()

    def __init__(
        self,
        parent: QObject | None = None,
        *,
        capture_factory: Callable[[str], CaptureProtocol] | None = None,
    ) -> None:
        super().__init__(parent)
        self._process_backend_enabled = capture_factory is None
        self._capture_factory = capture_factory or open_video_capture
        self._capture: CaptureProtocol | None = None
        self._decoder: ProcessDecoder | None = None
        self._decoder_threads: int | None = None
        self._decoder_thread_setting = configured_decoder_threads()
        self._generation = 0
        self._request_id = 0
        self._revision = 0
        self._published_revision = 0
        self._inflight: tuple[int, int, bool, int] | None = None
        self._pending_seek: tuple[int, int] | None = None
        self._seek_target: int | None = None
        self._resume_after_seek = False
        self._cache: OrderedDict[int, QImage] = OrderedDict()
        self._cache_bytes = 0
        self._crash_counts: dict[Path, int] = {}
        self._safe_files: set[Path] = set()
        self._next_decode_frame: int | None = None
        self._metadata: VideoMetadata | None = None
        self._state = PlaybackState.EMPTY
        self._current_frame = -1
        self._current_image: QImage | None = None
        self._audio: AudioPlayback | None = None
        # Disable the Qt decoder entirely for native-crash diagnosis, not just
        # mute its output (muting would keep the second decoder running).
        self._audio_enabled = os.environ.get("AVAT_DISABLE_AUDIO", "").strip().lower() not in {"1", "true", "yes", "on"}
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
        return (self._capture is not None or self._decoder is not None) and self._metadata is not None

    @property
    def decoder_pid(self) -> int | None:
        return self._decoder.pid if self._decoder else None

    @property
    def decoder_threads(self) -> int | None:
        return self._decoder_threads

    @property
    def is_seeking(self) -> bool:
        return self._seek_target is not None

    @property
    def requested_frame(self) -> int:
        return self._seek_target if self._seek_target is not None else self._current_frame

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

        # Injected factories remain synchronous; normal playback never creates
        # a native capture in the GUI process.
        if self._process_backend_enabled and self._capture_factory is open_video_capture:
            return self._open_process_video(resolved)

        candidate: CaptureProtocol | None = None
        try:
            candidate = self._capture_factory(str(resolved))
            if not candidate.isOpened():
                raise RuntimeError("open failed")
            ok, bgr = candidate.read()
            if not ok or bgr is None:
                raise RuntimeError("decode failed")
            reported_fps = self._read_reported_fps(candidate)
            fps = reported_fps or DEFAULT_FPS
            image = self._to_qimage(bgr)
            count = self._read_frame_count(candidate)
            metadata = VideoMetadata(
                resolved, fps, count, image.width(), image.height(), reported_fps
            )
        except Exception:
            self._safe_release(candidate)
            self.error_occurred.emit(f"영상을 열거나 첫 프레임을 읽을 수 없습니다: {resolved.name}")
            return False

        self.close_video()
        self._capture = candidate
        self._metadata = metadata
        self._current_frame = 0
        self._next_decode_frame = 1
        self._current_image = image
        if self._audio_enabled and self._audio is None:
            self._audio = AudioPlayback(self)
            self._audio.error_occurred.connect(self.error_occurred)
        if self._audio is not None:
            self._audio.open(resolved)
        self._timer.setInterval(max(1, round(1000 / fps)))
        self._set_state(PlaybackState.STOPPED)
        self.video_opened.emit(metadata)
        self.frame_ready.emit(image.copy(), 0, 0.0)
        self.position_changed.emit(0, count)
        return True

    @Slot()
    def close_video(self) -> None:
        had_video = self._metadata is not None
        self._generation += 1
        self._timer.stop()
        self._resume_after_seek = False
        self._inflight = None
        self._pending_seek = None
        self._seek_target = None
        self.seek_cancelled.emit()
        decoder, self._decoder = self._decoder, None
        if decoder is not None:
            decoder.close()
            decoder.deleteLater()
        self._decoder_threads = None
        self._cache.clear()
        self._cache_bytes = 0
        if self._audio is not None:
            self._audio.close()
        self._safe_release(self._capture)
        self._capture = None
        self._next_decode_frame = None
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
            and not self.is_seeking
            and not self.seek(0)
        ):
            return False
        if self._decoder is not None and (self._inflight is not None or self._pending_seek):
            self._resume_after_seek = True
            self._set_state(PlaybackState.PLAYING)
            return True
        self._timer.start()
        if self._audio is not None:
            self._audio.play(self._position_ms())
        self._set_state(PlaybackState.PLAYING)
        return True

    @Slot(result=bool)
    def pause(self) -> bool:
        if not self.is_open:
            return False
        self._timer.stop()
        self._resume_after_seek = False
        if self._audio is not None:
            self._audio.pause()
        self._set_state(PlaybackState.PAUSED)
        return True

    @Slot(int, result=bool)
    def seek(self, frame: int) -> bool:
        if not self.is_open:
            return False
        assert self._metadata is not None
        target = max(0, int(frame))
        if self._metadata.total_frames:
            target = min(target, self._metadata.last_frame)
        if self._decoder is not None:
            return self._seek_process(target)
        assert self._capture is not None
        if target == self._current_frame and self._next_decode_frame == target + 1:
            return True
        try:
            # read() already points to the next frame. Avoid a decoder flush
            # for each forward 1F step; arbitrary/backward jumps still seek.
            expected = self._next_decode_frame
            self._next_decode_frame = None
            if expected != target and not self._capture.set(cv2.CAP_PROP_POS_FRAMES, float(target)):
                raise RuntimeError("seek failed")
            ok, bgr = self._capture.read()
            if not ok or bgr is None:
                raise RuntimeError("decode failed")
            image = self._to_qimage(bgr)
            self._next_decode_frame = target + 1
        except Exception:
            self._next_decode_frame = None
            self.pause()
            self.error_occurred.emit(f"{target + 1:,}번 프레임으로 이동할 수 없습니다.")
            return False
        self._publish(image, target)
        if self._audio is not None:
            self._audio.seek(self._position_ms())
        return True

    @Slot(int, result=bool)
    def seek_relative(self, frames: int) -> bool:
        if not self.is_open:
            return False
        return self.seek(max(0, self.requested_frame) + int(frames))

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
        if self._decoder is not None:
            if self._inflight is not None or self._pending_seek is not None:
                return
            next_frame = self._current_frame + 1
            if self.total_frames and next_frame >= self.total_frames:
                self._finish()
            else:
                self._dispatch_process(next_frame, playback=True, revision=self._revision)
            return
        assert self._capture is not None and self._metadata is not None
        next_frame = self._current_frame + 1
        if self._metadata.total_frames and next_frame >= self._metadata.total_frames:
            self._finish()
            return
        if self._next_decode_frame != next_frame:
            # A failed seek/read can leave the native decoder at a different
            # position from the last displayed image. Never mislabel its frame.
            self.seek(next_frame)
            return
        try:
            self._next_decode_frame = None
            ok, bgr = self._capture.read()
            if not ok or bgr is None:
                self._finish()
                return
            image = self._to_qimage(bgr)
            self._next_decode_frame = next_frame + 1
            self._publish(image, next_frame)
        except Exception:
            self._next_decode_frame = None
            self._finish()
            self.error_occurred.emit(f"{next_frame + 1:,}번 프레임을 읽을 수 없습니다.")

    def _open_process_video(self, resolved: Path) -> bool:
        threads = 1 if resolved in self._safe_files else self._decoder_thread_setting
        candidate = ProcessDecoder(self)
        opened = candidate.open(resolved, threads)
        if opened is None:
            candidate.deleteLater()
            # Startup aborts are isolated too. One file-specific safe retry,
            # never silently force every healthy video back to one thread.
            if threads != 1:
                candidate = ProcessDecoder(self)
                opened = candidate.open(resolved, 1)
                if opened is not None:
                    self._safe_files.add(resolved)
            if opened is None:
                candidate.deleteLater()
                self.error_occurred.emit(f"영상을 열거나 첫 프레임을 읽을 수 없습니다: {resolved.name}")
                return False
        information, image = opened
        self.close_video()
        reported_fps = float(information["reported_fps"])
        fps = reported_fps or DEFAULT_FPS
        count = int(information["total_frames"])
        self._metadata = VideoMetadata(resolved, fps, count, image.width(), image.height(), reported_fps)
        self._current_frame = 0
        self._current_image = QImage(image)
        self._revision = self._published_revision = 0
        self._attach_decoder(candidate, information)
        self._remember_frame(0, image)
        if self._audio_enabled and self._audio is None:
            self._audio = AudioPlayback(self)
            self._audio.error_occurred.connect(self.error_occurred)
        if self._audio is not None:
            self._audio.open(resolved)
        self._timer.setInterval(max(1, round(1000 / fps)))
        # Activity breadcrumbs to line up with Windows crash times; no names/paths.
        LOGGER.info(
            "video_opened width=%s height=%s fps=%.3f frames=%s codec=%s decoder_threads=%s "
            "safe_mode=%s audio=%s memory_bytes=%s",
            image.width(), image.height(), fps, count, information.get("codec") or "-",
            self._decoder_threads, resolved in self._safe_files, self._audio is not None,
            process_memory_bytes() or "-",
        )
        self._set_state(PlaybackState.STOPPED)
        self.video_opened.emit(self._metadata)
        self.frame_ready.emit(QImage(image), 0, 0.0)
        self.position_changed.emit(0, count)
        if resolved in self._safe_files:
            self.error_occurred.emit("이 영상은 디코더 오류로 안전 모드(1스레드)를 사용합니다.")
        return True

    def _attach_decoder(self, decoder: ProcessDecoder, information: dict) -> None:
        self._decoder = decoder
        self._decoder_threads = int(information["threads"])
        # Bound QObject receivers are disconnected by Qt at destruction. Do not
        # capture parent/child wrappers in lambdas: those create GC cycles whose
        # teardown can race native media objects after the controller is closed.
        decoder.frame_decoded.connect(self._decoder_frame_arrived)
        decoder.end_of_stream.connect(self._decoder_eof_arrived)
        decoder.request_failed.connect(self._decoder_error_arrived)
        decoder.failed.connect(self._decoder_failed_arrived)

    @Slot(QImage, int, int, float)
    def _decoder_frame_arrived(self, image: QImage, request: int, frame: int, elapsed: float) -> None:
        decoder = self.sender()
        if isinstance(decoder, ProcessDecoder):
            self._process_frame(decoder, image, request, frame)

    @Slot(int)
    def _decoder_eof_arrived(self, request: int) -> None:
        decoder = self.sender()
        if isinstance(decoder, ProcessDecoder):
            self._process_eof(decoder, request)

    @Slot(int, str)
    def _decoder_error_arrived(self, request: int, message: str) -> None:
        decoder = self.sender()
        if isinstance(decoder, ProcessDecoder):
            self._process_request_failed(decoder, request, message)

    @Slot(str)
    def _decoder_failed_arrived(self, message: str) -> None:
        decoder = self.sender()
        if isinstance(decoder, ProcessDecoder):
            self._process_failed(decoder, message)

    def _remember_frame(self, frame: int, image: QImage) -> None:
        previous = self._cache.pop(frame, None)
        if previous is not None:
            self._cache_bytes -= previous.sizeInBytes()
        # QImage is implicitly shared and immutable here. Bound retained pixels
        # by bytes, not frame count (4K frames must not create a giant cache).
        if image.sizeInBytes() <= 64 * 1024 * 1024:
            self._cache[frame] = QImage(image)
            self._cache_bytes += image.sizeInBytes()
        while self._cache_bytes > 64 * 1024 * 1024:
            _, removed = self._cache.popitem(last=False)
            self._cache_bytes -= removed.sizeInBytes()

    def _seek_process(self, target: int) -> bool:
        self._revision += 1
        revision = self._revision
        self._seek_target = target
        self.seek_requested_position.emit(target)
        if self.is_playing:
            self._resume_after_seek = True
            self._timer.stop()
            if self._audio is not None:
                self._audio.pause()
        if self._audio is not None:
            self._audio.seek(round(target / self.fps * 1000))
        cached = self._cache.get(target)
        if cached is not None:
            self._cache.move_to_end(target)
            self._pending_seek = None
            self._seek_target = None
            self._published_revision = revision
            self._publish(cached, target)
            self._resume_process_playback()
            return True
        # One request in flight; overwrite the one pending target, not the pipe.
        self._pending_seek = (target, revision)
        if self._inflight is None:
            self._dispatch_pending_seek()
        return True  # accepted, not a synchronous decoding-completion promise

    def _dispatch_pending_seek(self) -> None:
        if self._pending_seek is None or self._inflight is not None:
            return
        frame, revision = self._pending_seek
        self._pending_seek = None
        self._dispatch_process(frame, playback=False, revision=revision)

    def _dispatch_process(self, frame: int, *, playback: bool, revision: int) -> None:
        decoder = self._decoder
        if decoder is None:
            return
        self._request_id += 1
        request = self._request_id
        self._inflight = (request, frame, playback, revision)
        if not decoder.request(request, frame, playback=playback):
            self._process_failed(decoder, "영상 디코더에 요청을 전달할 수 없습니다.")

    def _process_frame(self, decoder: ProcessDecoder, image: QImage, request: int, frame: int) -> None:
        if decoder is not self._decoder or self._inflight is None or self._inflight[0] != request:
            return
        _, _, playback, revision = self._inflight
        self._inflight = None
        self._remember_frame(frame, image)
        # Pause freezes playback; cached/manual previews can still finish. A
        # cache hit newer than this native request must not be overwritten.
        if revision >= self._published_revision and (not playback or self.is_playing):
            self._published_revision = revision
            if frame == self._seek_target and self._pending_seek is None:
                self._seek_target = None
            self._publish(image, frame)
        if decoder is not self._decoder:
            return  # a frame subscriber switched/closed the video
        self._dispatch_pending_seek()
        self._resume_process_playback()

    def _resume_process_playback(self) -> None:
        if self._resume_after_seek and self._inflight is None and self._pending_seek is None:
            self._resume_after_seek = False
            self._timer.start()
            if self._audio is not None:
                self._audio.play(self._position_ms())

    def _process_eof(self, decoder: ProcessDecoder, request: int) -> None:
        if decoder is not self._decoder or self._inflight is None or self._inflight[0] != request:
            return
        _, _, playback, _ = self._inflight
        self._inflight = None
        if self._pending_seek is not None:
            self._dispatch_pending_seek()
        elif playback:
            self._finish()
        else:
            self._cancel_process_seek("요청한 영상 프레임을 읽을 수 없습니다.")

    def _process_request_failed(self, decoder: ProcessDecoder, request: int, message: str) -> None:
        if decoder is self._decoder and self._inflight is not None and self._inflight[0] == request:
            self._inflight = None
            self._cancel_process_seek(message)

    def _cancel_process_seek(self, message: str) -> None:
        self.pause()
        self._pending_seek = None
        self._seek_target = None
        self.seek_cancelled.emit()
        self.position_changed.emit(self._current_frame, self.total_frames)
        self.error_occurred.emit(message)

    def _process_failed(self, decoder: ProcessDecoder, message: str) -> None:
        if decoder is not self._decoder or self._metadata is None:
            return
        target = max(0, self.requested_frame)
        path = self._metadata.path
        self.pause()
        self._inflight = None
        self._pending_seek = None
        self._decoder = None
        decoder.close()
        decoder.deleteLater()
        failures = self._crash_counts.get(path, 0) + 1
        self._crash_counts[path] = failures
        if failures >= 2:
            self._safe_files.add(path)
        LOGGER.warning(
            "decoder_recovery failures=%s safe_mode=%s frame=%s", failures, path in self._safe_files,
            target,
        )
        if failures > 2:
            self._seek_target = None
            self.seek_cancelled.emit()
            self.error_occurred.emit(f"{message} 자동 복구를 중단했습니다. 영상을 다시 열어 주세요.")
            return
        self.error_occurred.emit(f"{message} 조사 내용은 유지한 채 영상 디코더를 다시 시작합니다.")
        generation = self._generation
        QTimer.singleShot(0, lambda: self._recover_process(path, target, generation))

    def _recover_process(self, path: Path, target: int, generation: int) -> None:
        if generation != self._generation or self._metadata is None or self._metadata.path != path:
            return
        candidate = ProcessDecoder(self)
        information = candidate.open(
            path, 1 if path in self._safe_files else self._decoder_thread_setting
        )
        if information is None:
            candidate.deleteLater()
            self._seek_target = None
            self.seek_cancelled.emit()
            self.error_occurred.emit("영상 디코더를 복구하지 못했습니다. 영상을 다시 열어 주세요.")
            return
        self._attach_decoder(candidate, information[0])
        self._seek_process(target)
        if path in self._safe_files:
            self.error_occurred.emit("이 영상의 반복 오류로 안전 모드(1스레드)로 전환했습니다.")

    def _publish(self, image: QImage, frame: int) -> None:
        self._current_frame = frame
        self._current_image = QImage(image)
        if self._audio is not None and self.is_playing:
            self._audio.sync(self._position_ms())
        timestamp = frame / self.fps if self.fps else 0.0
        self.frame_ready.emit(QImage(image), frame, timestamp)
        self.position_changed.emit(frame, self.total_frames)

    def _finish(self) -> None:
        self._timer.stop()
        self._resume_after_seek = False
        if self._audio is not None:
            self._audio.pause()
        if self._metadata is not None and self._metadata.total_frames == 0:
            observed_total = max(0, self._current_frame + 1)
            self._metadata = VideoMetadata(
                self._metadata.path,
                self._metadata.fps,
                observed_total,
                self._metadata.width,
                self._metadata.height,
                self._metadata.reported_fps,
            )
            self.total_frames_changed.emit(observed_total)
        self._set_state(PlaybackState.STOPPED)
        self.playback_finished.emit()

    def _position_ms(self) -> int:
        return round(max(0, self._current_frame) * 1000 / self.fps) if self.fps else 0

    def _set_state(self, state: PlaybackState) -> None:
        if state is self._state:
            return
        self._state = state
        LOGGER.info("playback_state state=%s frame=%s", state.value, self._current_frame)
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
        """Read the playback rate, retaining the historical fallback behavior."""
        return VideoController._read_reported_fps(capture) or DEFAULT_FPS

    @staticmethod
    def _read_reported_fps(capture: CaptureProtocol) -> float:
        """Read actual FPS metadata; unavailable or invalid values remain zero."""
        with suppress(Exception):
            fps = float(capture.get(cv2.CAP_PROP_FPS))
            if math.isfinite(fps) and fps > 0:
                return fps
        return 0.0

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
