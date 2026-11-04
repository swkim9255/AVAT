from __future__ import annotations

import sys

import pytest
from PySide6 import QtMultimedia
from PySide6.QtMultimedia import QMediaPlayer

from investigator.video_controller import PlaybackState, VideoController
from tests.media_fixture import create_av_clip


@pytest.fixture(autouse=True)
def preserve_native_standard_handles(capfd):
    # Windows native Qt/FFmpeg DLLs may cache the stderr HANDLE. Pytest's
    # per-test descriptor redirection can close/reuse it as QProcess stdin,
    # injecting a delayed codec warning into the decoder's command pipe.
    # Keep real audio/decoder behavior and stable handles; no mocks or muted
    # native warnings are used by these integration tests.
    if sys.platform == "win32":
        with capfd.disabled():
            yield
    else:
        yield


@pytest.mark.skipif(
    not hasattr(QtMultimedia, "QAudioBufferOutput"), reason="Buffer inspection requires Qt 6.8+"
)
def test_real_video_decodes_audio_and_follows_pause_seek_and_close(qtbot, tmp_path):
    path = create_av_clip(tmp_path / "with-tone.avi")
    controller = VideoController()
    errors = []
    controller.error_occurred.connect(errors.append)
    try:
        assert controller.open_video(path)
        assert controller.fps == 20
        assert controller.total_frames == 60
        audio = controller._audio
        assert audio is not None
        buffers = QtMultimedia.QAudioBufferOutput(audio)
        decoded = []
        buffers.audioBufferReceived.connect(lambda buffer: decoded.append(buffer.sampleCount()))
        audio._player.setAudioBufferOutput(buffers)
        qtbot.waitUntil(audio._player.hasAudio, timeout=5000)
        assert audio._player.audioOutput() is audio._output
        assert not audio._output.isMuted()
        assert audio._output.volume() > 0
        assert controller.play()
        qtbot.waitUntil(lambda: any(count > 0 for count in decoded), timeout=5000)
        qtbot.waitUntil(lambda: controller.current_frame > 2, timeout=5000)
        assert controller.pause()
        assert audio._player.playbackState() == QMediaPlayer.PlaybackState.PausedState
        assert controller.seek(30)
        qtbot.waitUntil(lambda: controller.current_frame == 30, timeout=5000)
        assert audio._target_ms == 1500
        assert controller.play()
        qtbot.waitUntil(lambda: audio._player.position() > 1600, timeout=5000)
        assert controller.pause()
        frame = controller.current_frame
        qtbot.wait(100)
        assert controller.current_frame == frame
        assert not errors
    finally:
        controller.close_video()
    assert audio._player.source().isEmpty()
    assert controller.state == PlaybackState.EMPTY


def test_silent_video_still_plays_and_repeated_seeks_do_not_fail(qtbot, tmp_path):
    path = create_av_clip(tmp_path / "silent.avi", with_audio=False)
    controller = VideoController()
    errors = []
    controller.error_occurred.connect(errors.append)
    try:
        assert controller.open_video(path)
        for index in range(150):
            assert controller.seek(index * 13 % 60)
        assert controller.play()
        try:
            qtbot.waitUntil(lambda: controller.current_frame > 20)
        except Exception as exc:
            exc.add_note(
                f"state={controller.state} current={controller.current_frame} "
                f"requested={controller.requested_frame} seeking={controller.is_seeking} "
                f"inflight={controller._inflight!r} pending={controller._pending_seek!r} "
                f"timer={controller._timer.isActive()} resume={controller._resume_after_seek} "
                f"revision={controller._revision} published={controller._published_revision} errors={errors!r}"
            )
            raise
        assert not controller._audio._player.hasAudio()
        assert not errors
    finally:
        controller.close_video()


def test_paused_scrubbing_defers_audio_decoder_seeks_until_play(qtbot, monkeypatch):
    from PySide6.QtCore import QObject

    from investigator.audio_playback import AudioPlayback

    parent = QObject()
    audio = AudioPlayback(parent)
    calls = []
    monkeypatch.setattr(audio._player, "setPosition", calls.append)
    for position in range(100, 10000, 100):
        audio.seek(position)
    assert calls == []
    assert audio._target_ms == 9900
    audio.play(9900)
    assert calls == [9900]
    audio.pause()
    audio.seek(12000)
    assert calls == [9900]
    audio.close()
