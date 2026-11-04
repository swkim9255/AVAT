from __future__ import annotations

from concurrent.futures import Future
from pathlib import Path
from threading import Event

import cv2
import pytest
from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtWidgets import QApplication, QMessageBox

import investigator.main_window as main_window_module
from investigator.main_window import MainWindow
from investigator.models import InvestigationCase
from investigator.statistics import StatsCondition
from investigator.storage import CaseRepository, WorkspaceError
from tests.helpers import valid_values
from tests.test_ui_smoke import _Capture


class SilentAudio(QObject):
    error_occurred = Signal(str)

    def open(self, *_args):
        pass

    def close(self):
        pass

    def play(self, *_args):
        pass

    def pause(self):
        pass

    def seek(self, *_args):
        pass

    def sync(self, *_args):
        pass


class MetadataCapture(_Capture):
    def __init__(self, fps=20.0):
        super().__init__(opened=True, total_frames=200)
        self.reported_fps = fps

    def get(self, prop):
        if prop == cv2.CAP_PROP_FPS:
            return self.reported_fps
        return super().get(prop)


def accident(uid, *, status="completed", **values):
    raw = valid_values()
    raw.update(accident_classification=uid, **values)
    return InvestigationCase(uid=uid, values=raw, status=status)


@pytest.fixture
def make_window(qtbot, monkeypatch):
    monkeypatch.setattr("investigator.video_controller.AudioPlayback", SilentAudio)
    monkeypatch.setattr(cv2, "VideoCapture", lambda *_args: MetadataCapture())

    def create(workspace: Path, cases: list[InvestigationCase]):
        repository = CaseRepository(workspace)
        repository.save(cases, active_case_uid=cases[0].uid if cases else None)
        window = MainWindow(workspace)
        # Keep placeholder media on the explicitly injected synchronous path.
        # Resolve cv2 dynamically so individual FPS-metadata overrides and the
        # metadata-probe worker still exercise the same capture fixture.
        window.video_controller._capture_factory = lambda path: cv2.VideoCapture(path)
        qtbot.addWidget(window)
        window.show()
        return window

    return create


def enter_statistics(window):
    window.mode_tabs.setCurrentIndex(1)
    window._refresh_statistics()
    assert window.workspace_pages.currentWidget() is window.statistics_panel


def test_main_mode_tabs_show_actual_workspace_counts_and_empty_workspace(make_window, tmp_path):
    window = make_window(tmp_path / "actual", [
        accident("001"), accident("002"), accident("003", status="draft"),
    ])
    assert [window.mode_tabs.tabText(index) for index in range(2)] == ["조사", "통계"]
    enter_statistics(window)
    assert window.statistics_panel.target_label.text() == "대상 2건 / 전체 3건"
    other = tmp_path / "empty"
    other.mkdir()
    assert window.open_project(other)
    window._refresh_statistics()
    assert window.statistics_panel.workspace == other
    assert window.statistics_panel.target_label.text() == "대상 0건 / 전체 0건"
    window.mode_tabs.setCurrentIndex(0)
    assert window.workspace_pages.currentWidget() is not window.statistics_panel


def test_project_tabs_keep_statistics_conditions_views_and_pins_independent(make_window, tmp_path):
    first, second = tmp_path / "A", tmp_path / "B"
    window = make_window(first, [accident("001", weather="맑음")])
    CaseRepository(second).save([accident("002", status="draft", weather="강수")])
    enter_statistics(window)
    panel = window.statistics_panel
    panel.add_condition(StatsCondition("weather", "맑음"))
    panel.pinned_fields = ["weather", "driver_age"]
    panel.open_item("driver_age")
    assert window.open_project(second)
    window._refresh_statistics()
    assert panel.conditions == []
    assert panel.view_tabs.currentIndex() == 0
    assert panel.pinned_fields == ["collision_targets", "incident_day", "weather", "driver_age"]
    panel.status.setCurrentIndex(panel.status.findData("all"))
    panel.add_condition(StatsCondition("weather", "강수"))
    panel.pinned_fields = ["vehicle_type"]
    panel.view_tabs.setCurrentIndex(2)
    assert window.switch_project(0)
    window._refresh_statistics()
    assert panel.conditions == [StatsCondition("weather", values=("맑음",))]
    assert panel.status.currentData() == "completed"
    assert panel.pinned_fields == ["weather", "driver_age"]
    assert panel.view_tabs.currentIndex() == 1
    assert panel._item_key == "driver_age"
    assert panel.target_label.text() == "대상 1건 / 전체 1건"
    assert window.switch_project(1)
    window._refresh_statistics()
    assert panel.conditions == [StatsCondition("weather", values=("강수",))]
    assert panel.status.currentData() == "all"
    assert panel.pinned_fields == ["vehicle_type"]
    assert panel.view_tabs.currentIndex() == 2


def test_dragged_project_tabs_keep_active_and_inactive_statistics_state(make_window, tmp_path):
    first, second = tmp_path / "first", tmp_path / "second"
    window = make_window(first, [accident("001", weather="맑음")])
    CaseRepository(second).save([accident("002", status="draft", weather="강수")])
    enter_statistics(window)
    panel = window.statistics_panel
    panel.add_condition(StatsCondition("weather", "맑음"))
    panel.pinned_fields = ["weather", "driver_age"]
    panel.open_item("driver_age")
    first_state = panel.capture_state()
    assert window.open_project(second)
    window._refresh_statistics()
    panel.status.setCurrentIndex(panel.status.findData("all"))
    panel.add_condition(StatsCondition("weather", "강수"))
    panel.pinned_fields = ["vehicle_type"]
    panel.view_tabs.setCurrentIndex(2)
    second_state = panel.capture_state()
    engine = panel.engine
    window.project_tabs.moveTab(1, 0)
    assert window.repository.workspace == second
    assert window._active_project_index == window.project_tabs.currentIndex() == 0
    assert panel.engine is engine
    assert panel.capture_state() == second_state
    assert window.switch_project(1)
    window._refresh_statistics()
    assert panel.capture_state() == first_state
    assert panel.target_label.text() == "대상 1건 / 전체 1건"
    assert window.switch_project(0)
    window._refresh_statistics()
    assert panel.capture_state() == second_state
    assert panel.target_label.text() == "대상 1건 / 전체 1건"


def test_statistics_activation_opens_real_case_clears_search_and_focuses_missing_field(make_window, tmp_path, qtbot):
    window = make_window(tmp_path, [accident("001"), accident("002", status="draft", driver_sex=None)])
    window.case_list.search.setText("not matching")
    enter_statistics(window)
    window.statistics_panel.view_tabs.setCurrentIndex(4)
    window.statistics_panel.case_activated.emit("002", "driver_sex")
    assert window.mode_tabs.currentIndex() == 0
    assert window.current_uid == "002"
    assert window.case_list.search.text() == ""
    editor = window.investigation.editors["driver_sex"]
    qtbot.waitUntil(lambda: QApplication.focusWidget() is editor.control)
    assert window.investigation.sections[window.investigation.tabs.currentIndex()].key == "driver"


def test_statistics_use_unsaved_edits_without_changing_storage_or_case(make_window, tmp_path):
    window = make_window(tmp_path, [accident("001", weather="맑음")])
    window._field_changed("weather", "강수")
    before = window.current_case.to_dict()
    assert window._dirty
    window.statistics_panel.status.setCurrentIndex(window.statistics_panel.status.findData("all"))
    enter_statistics(window)
    distribution = window.statistics_panel.engine.distribution("weather", window.statistics_panel.filters())
    assert [(bucket.label, bucket.count) for bucket in distribution.buckets] == [("강수", 1)]
    for index in range(5):
        window.statistics_panel.view_tabs.setCurrentIndex(index)
    assert window.current_case.to_dict() == before
    assert window._dirty
    stored = CaseRepository(tmp_path).load()[0]
    assert stored.values["weather"] == "맑음"
    assert stored.status == "completed"


def test_entering_statistics_pauses_playback_and_preserves_frame(make_window, tmp_path, qtbot):
    window = make_window(tmp_path, [accident("001", status="draft")])
    video = tmp_path / "manual.mp4"
    video.write_bytes(b"fixture video")
    window.attach_videos([str(video)])
    assert window.video_controller.seek(25)
    assert window.video_controller.play()
    enter_statistics(window)
    frame = window.video_controller.current_frame
    assert frame == 25
    assert not window.video_controller.is_playing
    qtbot.wait(60)
    assert window.video_controller.current_frame == frame
    window.mode_tabs.setCurrentIndex(0)
    assert window.video_controller.current_frame == frame
    assert not window.video_controller.is_playing


def test_statistics_keyboard_does_not_start_hidden_video_or_seek(make_window, tmp_path, qtbot):
    window = make_window(tmp_path, [accident("001", status="draft")])
    video = tmp_path / "manual.mp4"
    video.write_bytes(b"fixture video")
    window.attach_videos([str(video)])
    window.video_controller.seek(25)
    enter_statistics(window)
    window.setFocus()
    qtbot.keyClick(window, Qt.Key.Key_Space)
    qtbot.keyClick(window, Qt.Key.Key_L)
    assert not window.video_controller.is_playing
    assert window.video_controller.current_frame == 25


def test_missing_actual_fps_never_uses_playback_fallback_in_statistics(make_window, tmp_path, monkeypatch, qtbot):
    monkeypatch.setattr(cv2, "VideoCapture", lambda *_args: MetadataCapture(fps=0))
    window = make_window(tmp_path, [accident("001", status="draft")])
    video = tmp_path / "unknown_fps.mp4"
    video.write_bytes(b"fixture video")
    window.attach_videos([str(video)])
    window.current_case.set_trigger(0)
    window.current_case.set_collision(60)
    window._mark_case_dirty(window.current_case)
    window.statistics_panel.status.setCurrentIndex(window.statistics_panel.status.findData("all"))
    enter_statistics(window)
    window.statistics_panel.view_tabs.setCurrentIndex(3)
    qtbot.waitUntil(lambda: window._statistics_media_future is None)
    window._refresh_statistics()
    report = window.statistics_panel.engine.video(window.statistics_panel.filters())
    assert report.valid_intervals == 0
    assert report.missing_fps == 1
    assert report.median_seconds is None
    assert not window._statistics_media_cache.get(str(video), (0, 0, 0))[2]


def test_media_worker_populates_actual_fps_cache_then_refreshes_real_interval(make_window, tmp_path, monkeypatch, qtbot):
    window = make_window(tmp_path, [accident("001", status="draft")])
    video = tmp_path / "manual.mp4"
    video.write_bytes(b"fixture video")
    window.attach_videos([str(video)])
    window.current_case.set_trigger(10)
    window.current_case.set_collision(70)
    window._statistics_media_cache.clear()
    observed = []

    def probe(paths, cache, cancel):
        observed.append((paths, cache, cancel.is_set()))
        stat = video.stat()
        return {str(video): (stat.st_size, stat.st_mtime_ns, 60.0)}

    monkeypatch.setattr(main_window_module, "probe_video_fps", probe)
    window.statistics_panel.status.setCurrentIndex(window.statistics_panel.status.findData("all"))
    enter_statistics(window)
    window.statistics_panel.view_tabs.setCurrentIndex(3)
    qtbot.waitUntil(lambda: window._statistics_media_future is None)
    window._refresh_statistics()
    assert observed and observed[0][0] == [str(video)]
    assert observed[0][1] == {}
    assert not observed[0][2]
    report = window.statistics_panel.engine.video(window.statistics_panel.filters())
    assert report.valid_intervals == 1
    assert report.median_seconds == 1


def test_save_failure_keeps_statistics_drill_and_unsaved_case(make_window, tmp_path, monkeypatch):
    window = make_window(tmp_path, [accident("001"), accident("002")])
    window._field_changed("remarks", "unsaved edit")
    enter_statistics(window)
    original = window.repository.save_changes

    def failed(*_args, **_kwargs):
        raise WorkspaceError("simulated disk full")

    monkeypatch.setattr(QMessageBox, "critical", lambda *_args: None)
    monkeypatch.setattr(window.repository, "save_changes", failed)
    window.statistics_panel.case_activated.emit("002", "driver_sex")
    assert window.mode_tabs.currentIndex() == 1
    assert window.current_uid == "001"
    assert window._dirty
    assert window.current_case.values["remarks"] == "unsaved edit"
    monkeypatch.setattr(window.repository, "save_changes", original)


def test_previous_project_metadata_completion_does_not_starve_current_video_view(make_window, tmp_path, monkeypatch, qtbot):
    first, second = tmp_path / "A", tmp_path / "B"
    window = make_window(first, [accident("001", status="draft")])
    CaseRepository(second).save([accident("002", status="draft")])
    video_a = first / "a.mp4"
    video_a.write_bytes(b"first video")
    window.attach_videos([str(video_a)])
    window.current_case.set_trigger(0)
    window.current_case.set_collision(30)
    window._mark_case_dirty(window.current_case)
    window._statistics_media_cache.clear()
    entered, release = Event(), Event()
    calls = []

    def probe(paths, cache, cancel):
        calls.append(tuple(paths))
        if str(video_a) in paths:
            entered.set()
            assert release.wait(5)
        result = dict(cache)
        for path in paths:
            stat = Path(path).stat()
            result[path] = (stat.st_size, stat.st_mtime_ns, 60.0)
        return result

    monkeypatch.setattr(main_window_module, "probe_video_fps", probe)
    enter_statistics(window)
    window.statistics_panel.view_tabs.setCurrentIndex(3)
    qtbot.waitUntil(entered.is_set)
    try:
        assert window.open_project(second)
        video_b = second / "b.mp4"
        video_b.write_bytes(b"second video")
        window.attach_videos([str(video_b)])
        window.current_case.set_trigger(0)
        window.current_case.set_collision(60)
        window._mark_case_dirty(window.current_case)
        window._statistics_media_cache.pop(str(video_b), None)
        window._refresh_statistics()
        window.statistics_panel.status.setCurrentIndex(window.statistics_panel.status.findData("all"))
        window.statistics_panel.view_tabs.setCurrentIndex(3)
    finally:
        release.set()
    qtbot.waitUntil(lambda: window._statistics_media_future is None)
    qtbot.waitUntil(lambda: any(str(video_b) in paths for paths in calls), timeout=1500)
    window._refresh_statistics()
    assert window.repository.workspace == second
    assert window._statistics_media_cache[str(video_a)][2] == 60
    assert window.statistics_panel.engine.video(window.statistics_panel.filters()).median_seconds == 1


def test_close_cancels_metadata_worker_and_shuts_down_both_executors(make_window, tmp_path, monkeypatch, qtbot):
    window = make_window(tmp_path, [accident("001", status="draft")])
    video = tmp_path / "manual.mp4"
    video.write_bytes(b"fixture video")
    window.attach_videos([str(video)])
    entered, cancelled = Event(), Event()

    def probe(_paths, _cache, cancel):
        entered.set()
        assert cancel.wait(5)
        cancelled.set()
        return {}

    monkeypatch.setattr(main_window_module, "probe_video_fps", probe)
    enter_statistics(window)
    window.statistics_panel.view_tabs.setCurrentIndex(3)
    qtbot.waitUntil(entered.is_set)
    assert window.close()
    assert cancelled.is_set()
    assert window._statistics_media_cancel.is_set()
    assert window._statistics_media_worker._closed
    assert window._storage_worker._closed
    assert not window._statistics_media_timer.isActive()
    assert not window._statistics_timer.isActive()


def test_close_survives_failed_metadata_future(make_window, tmp_path):
    window = make_window(tmp_path, [accident("001")])
    future = Future()
    future.set_exception(RuntimeError("metadata decoder failure"))
    window._statistics_media_future = future
    assert window.close()
    assert window._statistics_media_worker._closed
