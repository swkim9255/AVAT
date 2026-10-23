"""Opt-in real Windows GPU smoke test; never modifies a user's project.

Set AVAT_RUN_NATIVE_SAM3=1 and optionally AVAT_NATIVE_SMOKE_VIDEO to run.
Set AVAT_NATIVE_SAME_ENV=1 to explicitly run both GUI and worker with this Python.
The normal suite skips this machine-specific, model-dependent check.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest
from PySide6.QtWidgets import QMessageBox

ROOT = Path(__file__).resolve().parents[2]
pytestmark = pytest.mark.skipif(
    os.environ.get("AVAT_RUN_NATIVE_SAM3") != "1", reason="Requires prepared native SAM 3 runtime"
)


def test_real_window_detection_overlay_and_save(qtbot, tmp_path, monkeypatch):
    from investigator.detection_process import DetectionSettings
    from investigator.main_window import MainWindow
    from investigator.project_tabs_state import ProjectTabsStore
    from investigator.storage import CaseRepository

    video = Path(os.environ.get(
        "AVAT_NATIVE_SMOKE_VIDEO", str(ROOT / "avat_ai/Suspicious Sudden Acceleration Accident 2.mp4")
    )).resolve()
    assert video.is_file()
    settings = DetectionSettings.load()
    assert settings.offline and not settings.config_error
    same_environment = os.environ.get("AVAT_NATIVE_SAME_ENV") == "1"
    if same_environment:
        # QSettings is isolated by detection_test_isolation, never the real user.
        DetectionSettings(
            python=sys.executable, model=settings.model,
            device=settings.device, offline=settings.offline,
        ).save()
    errors = []
    monkeypatch.setattr(QMessageBox, "warning", lambda _parent, title, text, *args: errors.append((title, text)))
    monkeypatch.setattr(QMessageBox, "question", lambda *args, **kwargs: QMessageBox.StandardButton.Yes)
    workspace = tmp_path / "isolated-native-project"
    window = MainWindow(workspace, project_tabs_store=ProjectTabsStore(tmp_path / "tabs.json"))
    qtbot.addWidget(window)
    window.show()
    try:
        window.attach_videos([str(video)])
        qtbot.waitUntil(lambda: window.video_controller.is_open, timeout=10_000)
        window._seek_video(100)
        qtbot.waitUntil(lambda: window._overlay_frame == 100, timeout=10_000)
        window.playback.detection_panel_button.setChecked(True)
        panel = window.detection_panel
        panel._class_keys = ["passenger"]
        panel.set_selected_range(100, 102)
        panel.step_spin.setValue(2)
        panel.run_button.click()
        assert window._detection_job is not None
        if same_environment:
            assert Path(window._detection_job.settings.executable).resolve() == Path(sys.executable).resolve()
        qtbot.waitUntil(lambda: window._detection_job is None, timeout=120_000)
        assert not errors, errors
        case = window.current_case
        objects = case.video_objects()
        assert objects.objects
        assert objects.runs[-1].device == "cuda"
        assert panel.table.rowCount() == sum(
            any(box.score is None or box.score >= panel.confidence_threshold for box in item.boxes)
            for item in objects.objects
        )
        assert window.video_player.canvas.overlay
        assert "검출 완료" in panel.status_label.text()
        # No draft-save click is needed to keep a completed detection run.
        checkpoint = next(item for item in CaseRepository(workspace).load() if item.uid == case.uid)
        assert checkpoint.video_objects().to_dict() == objects.to_dict()
        assert "자동 저장됨" in panel.status_label.text()
        window.save_draft()
        saved = next(item for item in CaseRepository(workspace).load() if item.uid == case.uid)
        assert saved.video_objects().to_dict() == objects.to_dict()
        print(f"Native GUI smoke passed (same Python={same_environment}): {len(objects.objects)} objects, "
              f"{len(window.video_player.canvas.overlay)} overlay boxes, saved/reloaded")
    finally:
        window._shutdown_detection()
        window.video_controller.close_video()
        window._dirty = False
        window.close()
