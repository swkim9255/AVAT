"""Native failures must be checked in a subprocess, not hidden by Python mocks."""

from __future__ import annotations

import os
import subprocess
import sys

import pytest

from tests.media_fixture import create_av_clip


@pytest.mark.parametrize("disable_audio", ["0", "1"])
def test_real_repeated_seek_switch_and_exit_does_not_abort(tmp_path, disable_audio):
    paths = [create_av_clip(tmp_path / f"clip{index}.avi") for index in range(2)]
    script = """
import sys
import os
import time
from PySide6.QtCore import QTimer
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication
from investigator.video_controller import VideoController
app = QApplication([])
controller = VideoController()
app.aboutToQuit.connect(controller.close_video)

def wait_until(predicate, timeout=5):
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, 'asynchronous decode did not complete'
        app.processEvents()
        QTest.qWait(1)

for cycle in range(8):
    assert controller.open_video(sys.argv[1 + cycle % 2])
    assert controller.decoder_pid is not None
    assert controller.decoder_pid != os.getpid()
    assert controller.decoder_threads >= 1
    for step in range(80):
        target = step * 17 % 60
        # The tiny fixture fits entirely in the image cache. Bypass only that
        # cache here so every request exercises the native-process transport.
        controller._cache.clear()
        controller._cache_bytes = 0
        assert controller.seek(target)
        # Await every completed decode, not merely 640 queued/coalesced inputs.
        wait_until(lambda: not controller.is_seeking and controller.current_frame == target)
    assert controller.play()
    controller._next_for_playback()
    controller.pause()
    wait_until(lambda: not controller.is_seeking)
    controller.close_video()
    assert controller.decoder_pid is None
QTimer.singleShot(0, app.quit)
app.exec()
controller.close_video()
print('640 seeks and 8 switches completed')
"""
    environment = dict(os.environ, QT_QPA_PLATFORM="offscreen", AVAT_DISABLE_AUDIO=disable_audio)
    run = subprocess.run(
        [sys.executable, "-c", script, *(str(path) for path in paths)],
        env=environment, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60,
    )
    assert run.returncode == 0, run.stderr
    assert "640 seeks and 8 switches completed" in run.stdout
    assert "Assertion fctx" not in run.stderr
    assert "destroyed before end of thread" not in run.stderr
