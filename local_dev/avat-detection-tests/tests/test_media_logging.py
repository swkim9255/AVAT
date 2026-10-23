from __future__ import annotations

import subprocess
import sys

from PySide6.QtCore import QLoggingCategory

from investigator.audio_playback import configure_media_logging
from tests.media_fixture import create_av_clip


def test_media_info_disabled_without_disabling_warnings_and_errors():
    configure_media_logging()
    for name in ("qt.multimedia.ffmpeg", "qt.multimedia.ffmpeg.mediadataholder"):
        category = QLoggingCategory(name)
        assert not category.isDebugEnabled()
        assert not category.isInfoEnabled()
        assert category.isWarningEnabled()
        assert category.isCriticalEnabled()
    assert QLoggingCategory("investigator.unrelated").isInfoEnabled()


def test_fresh_media_backend_does_not_print_startup_or_file_metadata(tmp_path):
    path = create_av_clip(tmp_path / "metadata-output.avi")
    result = subprocess.run(
        [sys.executable, "-c", """
import sys
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication
from investigator.video_controller import VideoController
app = QApplication([])
controller = VideoController()
assert controller.open_video(sys.argv[1])
QTimer.singleShot(500, app.quit)
app.exec()
controller.close_video()
""", str(path)],
        capture_output=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stderr.decode("utf-8", errors="replace")
    assert b"Using Qt multimedia" not in result.stderr
    assert b"Input #0" not in result.stderr
    assert b"Duration:" not in result.stderr
