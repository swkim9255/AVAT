"""Audio output synchronized to the frame-accurate OpenCV video controller."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from PySide6.QtCore import QElapsedTimer, QLoggingCategory, QObject, QUrl, Signal
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer

from investigator.ffmpeg_logging import configure_qt_ffmpeg_log_level


@lru_cache(maxsize=1)
def configure_media_logging() -> None:
    # Disable Qt's information-level av_dump_format call at its source, rather
    # than redirecting stderr (which would also hide native decoder failures).
    QLoggingCategory.setFilterRules(
        "qt.multimedia.ffmpeg*.debug=false\n"
        "qt.multimedia.ffmpeg*.info=false"
    )


class AudioPlayback(QObject):
    error_occurred = Signal(str)

    def __init__(self, parent: QObject) -> None:
        super().__init__(parent)
        configure_media_logging()
        self._target_ms = 0
        self._playing = False
        self._player = QMediaPlayer(self)
        self._output = QAudioOutput(self)
        self._output.setVolume(1.0)
        self._player.setAudioOutput(self._output)
        self._player.tracksChanged.connect(self._disable_video_track)
        self._player.mediaStatusChanged.connect(self._status_changed)
        self._player.errorOccurred.connect(self._error)
        self._last_sync = QElapsedTimer()
        # Qt installs its native FFmpeg logger when its first player is made.
        # Set a native logging level after initialization. Python callbacks from
        # FFmpeg threads can deadlock Qt playback; do not intercept stderr.
        # Warning diagnostics are quiet, but errors/fatal messages remain visible.
        configure_qt_ffmpeg_log_level()

    def open(self, path: Path) -> None:
        self.close()
        self._player.setSource(QUrl.fromLocalFile(str(path)))

    def play(self, position_ms: int) -> None:
        self._playing = True
        self.seek(position_ms)
        self._player.play()

    def pause(self) -> None:
        self._playing = False
        self._player.pause()

    def seek(self, position_ms: int) -> None:
        self._target_ms = max(0, position_ms)
        # Scrubbing previews video silently. Avoid repeatedly flushing the audio
        # decoder while paused; play() applies the final position before resuming.
        if self._playing:
            self._player.setPosition(self._target_ms)
        self._last_sync.start()

    def sync(self, position_ms: int) -> None:
        self._target_ms = max(0, position_ms)
        # Do not flush the audio decoder on every frame. Correct sustained drift
        # after loading/slow decoding while tolerating normal output buffering.
        if (
            self._playing and self._player.hasAudio()
            and self._player.playbackState() == QMediaPlayer.PlaybackState.PlayingState
            and self._last_sync.isValid() and self._last_sync.elapsed() >= 500
            and abs(self._player.position() - position_ms) > 150
        ):
            self.seek(position_ms)

    def close(self) -> None:
        self._playing = False
        self._target_ms = 0
        self._player.stop()
        self._player.setSource(QUrl())
        self._last_sync.invalidate()

    def _disable_video_track(self) -> None:
        # OpenCV supplies the displayed frames. Qt only needs to decode audio.
        self._player.setActiveVideoTrack(-1)

    def _status_changed(self, status: QMediaPlayer.MediaStatus) -> None:
        if status == QMediaPlayer.MediaStatus.LoadedMedia:
            self._disable_video_track()
            self.seek(self._target_ms)
            if self._playing:
                self._player.play()

    def _error(self, _error: QMediaPlayer.Error, message: str) -> None:
        if not self._player.source().isEmpty():
            self.error_occurred.emit(f"영상 소리를 재생할 수 없습니다: {message}")
