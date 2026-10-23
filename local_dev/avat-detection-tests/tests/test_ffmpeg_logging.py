from __future__ import annotations

import os
import subprocess
import sys

import pytest

from investigator import ffmpeg_logging
from tests.media_fixture import create_av_clip


class FakeLibrary:
    def __init__(self, level=32):
        self.level = level
        self.set_calls = []

        def get_level():
            return self.level

        def set_level(level):
            self.level = level
            self.set_calls.append(level)

        self.av_log_get_level = get_level
        self.av_log_set_level = set_level


@pytest.fixture
def library(monkeypatch):
    ffmpeg_logging.restore_qt_ffmpeg_log_level()
    fake = FakeLibrary()
    monkeypatch.setattr(ffmpeg_logging, "_get_loaded_qt_avutil", lambda: fake)
    yield fake
    ffmpeg_logging.restore_qt_ffmpeg_log_level()


def test_native_log_level_is_idempotent_and_restores_previous_level(library):
    assert ffmpeg_logging.configure_qt_ffmpeg_log_level()
    assert library.level == 16
    assert ffmpeg_logging.configure_qt_ffmpeg_log_level()
    assert library.set_calls == [16]
    ffmpeg_logging.restore_qt_ffmpeg_log_level()
    ffmpeg_logging.restore_qt_ffmpeg_log_level()
    assert library.set_calls == [16, 32]


@pytest.mark.parametrize("previous_level", [-8, 0, 8, 16])
def test_does_not_relax_existing_stricter_log_level(library, previous_level):
    library.level = previous_level
    assert ffmpeg_logging.configure_qt_ffmpeg_log_level()
    assert library.level == previous_level
    ffmpeg_logging.restore_qt_ffmpeg_log_level()
    assert library.level == previous_level


def test_missing_native_library_fails_open(monkeypatch):
    ffmpeg_logging.restore_qt_ffmpeg_log_level()
    monkeypatch.setattr(ffmpeg_logging, "_get_loaded_qt_avutil", lambda: None)
    assert not ffmpeg_logging.configure_qt_ffmpeg_log_level()
    ffmpeg_logging.restore_qt_ffmpeg_log_level()


def test_unsupported_platform_does_not_search_or_load_libraries(monkeypatch):
    import ctypes

    monkeypatch.setattr(ffmpeg_logging.sys, "platform", "linux")
    monkeypatch.setattr(ctypes, "CDLL", lambda *_args, **_kwargs: pytest.fail("loaded DLL"))
    assert ffmpeg_logging._get_loaded_qt_avutil() is None


@pytest.mark.skipif(sys.platform != "win32", reason="Native Qt FFmpeg Windows DLL integration")
def test_native_warning_is_suppressed_errors_remain_and_level_is_restored():
    script = r'''
import ctypes
from PySide6.QtWidgets import QApplication
from PySide6.QtMultimedia import QMediaPlayer
from investigator.audio_playback import configure_media_logging
from investigator.ffmpeg_logging import (
    configure_qt_ffmpeg_log_level, restore_qt_ffmpeg_log_level,
)
import investigator.ffmpeg_logging as logging
app = QApplication([])
configure_media_logging()
player = QMediaPlayer()
assert configure_qt_ffmpeg_log_level()
assert configure_qt_ffmpeg_log_level()
log = logging._library.av_log
log.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_char_p]
log.restype = None
log(None, 24, b'stream %d, timescale not set\n', ctypes.c_int(0))
log(None, 24, b'AVAT other native warning\n')
log(None, 16, b'AVAT preserved error %d\n', ctypes.c_int(7))
player.stop()
restore_qt_ffmpeg_log_level()
restore_qt_ffmpeg_log_level()
log(None, 24, b'stream %d, timescale not set\n', ctypes.c_int(88))
assert configure_qt_ffmpeg_log_level()
print('native log configuration complete', flush=True)
'''
    result = subprocess.run(
        [sys.executable, "-c", script],
        env=dict(os.environ, QT_QPA_PLATFORM="offscreen"),
        capture_output=True, timeout=10,
    )
    assert result.returncode == 0, result.stderr.decode(errors="replace")
    assert b"native log configuration complete" in result.stdout
    assert b"stream 0, timescale not set" not in result.stderr
    assert b"AVAT other native warning" not in result.stderr
    assert b"AVAT preserved error 7" in result.stderr
    assert b"stream 88, timescale not set" in result.stderr


@pytest.mark.skipif(sys.platform != "win32", reason="Native Qt audio integration")
def test_real_audio_play_pause_and_close_does_not_deadlock(tmp_path):
    path = create_av_clip(tmp_path / "native-logging-tone.avi")
    script = r'''
import sys
from pathlib import Path
from PySide6.QtWidgets import QApplication
from PySide6.QtCore import QTimer
from investigator.audio_playback import AudioPlayback
from investigator.ffmpeg_logging import restore_qt_ffmpeg_log_level
app = QApplication([])
audio = AudioPlayback(app)
audio.open(Path(sys.argv[1]))
QTimer.singleShot(200, lambda: audio.play(0))
QTimer.singleShot(600, audio.pause)
QTimer.singleShot(700, lambda: audio.play(800))
def close():
    audio.close()
    restore_qt_ffmpeg_log_level()
    app.quit()
QTimer.singleShot(1000, close)
app.exec()
print('audio playback and close complete', flush=True)
'''
    result = subprocess.run(
        [sys.executable, "-c", script, str(path)],
        env=dict(os.environ, QT_QPA_PLATFORM="offscreen"),
        capture_output=True, timeout=10,
    )
    assert result.returncode == 0, result.stderr.decode(errors="replace")
    assert b"audio playback and close complete" in result.stdout
