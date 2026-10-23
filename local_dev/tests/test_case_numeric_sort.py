"""Numeric display sorting persists without changing investigations/playback."""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path

import pytest
from PySide6.QtCore import QItemSelectionModel
from PySide6.QtWidgets import QMessageBox

from investigator.main_window import MainWindow
from investigator.models import InvestigationCase
from investigator.object_detection import ROLE_TRIGGER, add_manual_object
from investigator.project_tabs_state import ProjectTabsStore
from investigator.schema import ALL_FIELDS, FieldKind
from investigator.storage import CaseRepository, WorkspaceError
from investigator.video_controller import VideoMetadata


@pytest.fixture
def window(qtbot, tmp_path, monkeypatch):
    for method in ("warning", "critical", "information"):
        monkeypatch.setattr(QMessageBox, method, staticmethod(lambda *args, **kwargs: None))
    widget = MainWindow(
        tmp_path / "project", project_tabs_store=ProjectTabsStore(tmp_path / "tabs.json"),
    )

    def before_close(target):
        target._commit_in_progress = False
        target._video_import_in_progress = False
        target._switching_project = False
        target._workspace_load_failed = True

    qtbot.addWidget(widget, before_close_func=before_close)
    yield widget
    before_close(widget)
    widget.close()


def _case(window, number, suffixes=(2, 10, 1)):
    values = {}
    for field in ALL_FIELDS:
        if not field.required:
            continue
        if field.kind in {FieldKind.SINGLE, FieldKind.MULTI}:
            first = field.choices[0].value
            values[field.key] = [first] if field.kind is FieldKind.MULTI else first
        elif field.key in {"vehicle_model_year", "incident_time", "incident_date"}:
            values[field.key] = {
                "vehicle_model_year": "2023", "incident_time": "0930", "incident_date": "20260828",
            }[field.key]
        elif field.kind in {FieldKind.DECIMAL, FieldKind.DIGITS}:
            values[field.key] = "1"
        else:
            values[field.key] = "테스트"
    values.update({
        "institution": "samsung", "accident_classification": number,
        "vehicle_manufacturer": "kia", "vehicle_model_name": "테스트 차량",
        "obstacle_exists": "무", "incident_damage_unknown": "확인불가",
    })
    return InvestigationCase(
        values=values,
        videos=[str(window.repository.workspace / f"{number}_{suffix}.mp4") for suffix in suffixes],
    )


def _install(window, cases, current=None):
    current = current or cases[0]
    window.repository.save(cases, active_case_uid=current.uid)
    window.cases = cases
    window.current_uid = current.uid
    window._mark_saved(current.uid)
    window.case_list.set_cases(cases, current.uid, current.active_video_index)


def _saved(window):
    repository = CaseRepository(window.repository.workspace)
    return repository, repository.load()


def test_numeric_sort_orders_all_cases_and_clips_and_saves(window, monkeypatch):
    cases = [_case(window, number) for number in (
        "2023011302572", "2020011302572", "2019011302572", "2020011302571",
    )]
    active = cases[1]
    active.active_video_index = 1
    active.set_trigger(42)
    active.set_collision(50)
    active.set_video_rotation(90)
    objects, _ = add_manual_object(active.video_objects(), 42, (1, 2, 20, 30), ROLE_TRIGGER, None)
    active.set_video_objects(objects)
    active.status = "completed"
    _install(window, cases, active)
    originals = {case.uid: deepcopy(case.values) for case in cases}
    markers = deepcopy(active.markers_by_video)
    rotations = deepcopy(active.rotation_by_video)
    annotations = deepcopy(active.objects_by_video)
    playing_path = active.active_video
    metadata = VideoMetadata(Path(playing_path), 30.0, 1000, 1920, 1080, 30.0)
    window.video_controller._metadata = metadata
    window.video_controller._current_frame = 42

    def unexpected(*args, **kwargs):
        pytest.fail("display sorting must not reopen or seek the playing video")

    monkeypatch.setattr(window.video_controller, "open_video", unexpected)
    monkeypatch.setattr(window.video_controller, "seek", unexpected)
    window.case_list.sort_button.click()

    assert [case.values["accident_classification"] for case in window.cases] == [
        "2019011302572", "2020011302571", "2020011302572", "2023011302572",
    ]
    for case in window.cases:
        assert [Path(path).stem.rsplit("_", 1)[1] for path in case.videos] == ["1", "2", "10"]
        assert case.values == originals[case.uid]
    assert active.active_video == playing_path and active.active_video_index == 2
    assert active.markers_by_video == markers
    assert active.rotation_by_video == rotations
    assert active.objects_by_video == annotations
    assert active.status == "completed"
    assert window.current_case is active
    assert window.video_controller.metadata is metadata
    assert window.video_controller.current_frame == 42
    assert not window._dirty and not window._order_dirty

    repository, saved = _saved(window)
    assert [case.uid for case in saved] == [case.uid for case in window.cases]
    assert [case.videos for case in saved] == [case.videos for case in window.cases]
    saved_active = next(case for case in saved if case.uid == active.uid)
    assert saved_active.active_video == playing_path
    assert saved_active.status == "completed"
    assert saved_active.objects_by_video == annotations
    assert repository.active_case_uid() == active.uid


def test_sort_remaps_multi_selected_video_paths_and_preserves_filter_and_toggles(window):
    late, early, middle = [_case(window, number) for number in (
        "2023011302572", "2019011302572", "2021011302572",
    )]
    _install(window, [late, early, middle], late)
    panel = window.case_list
    late_item, early_item, middle_item = [panel.tree.topLevelItem(i) for i in range(3)]
    late_item.setExpanded(False)
    middle_item.setExpanded(False)
    early_item.child(1).setSelected(True)
    middle_item.setSelected(True)
    panel.tree.setCurrentItem(
        late_item.child(0), 0, QItemSelectionModel.SelectionFlag.NoUpdate,
    )
    panel.search.setText("2019")
    before = panel.capture_view_state()

    window.sort_cases_ascending()

    after = panel.capture_view_state()
    assert after["search"] == before["search"] == "2019"
    assert after["expanded"] == before["expanded"]
    assert after["current"] == (late.uid, 1)
    assert set(after["selected"]) == {(late.uid, 1), (early.uid, 2), (middle.uid, -1)}
    assert [case.uid for case in window.cases] == [early.uid, middle.uid, late.uid]
    assert panel.tree.topLevelItem(1).isHidden()
    assert panel.tree.topLevelItem(2).isHidden()
    assert not panel.tree.topLevelItem(0).isHidden()


def test_nonstandard_video_names_use_natural_numeric_sort(window):
    case = _case(window, "2020011302572", suffixes=())
    names = ["camera10.mp4", "camera2.mp4", "camera1.mp4"]
    case.videos = [str(window.repository.workspace / name) for name in names]
    case.active_video_index = 0
    _install(window, [case])
    window.sort_cases_ascending()
    assert [Path(path).name for path in case.videos] == [
        "camera1.mp4", "camera2.mp4", "camera10.mp4",
    ]
    assert case.active_video_index == 2
    assert Path(case.active_video).name == "camera10.mp4"


def test_numeric_case_ids_do_not_use_lexical_order_and_manual_ids_sort_last(window):
    cases = [_case(window, number, (1,)) for number in ("10", "ABC", "2", "1")]
    _install(window, cases)
    window.sort_cases_ascending()
    assert [case.values["accident_classification"] for case in window.cases] == [
        "1", "2", "10", "ABC",
    ]


@pytest.mark.parametrize("flag", [
    "_commit_in_progress", "_switching_project", "_video_import_in_progress", "_workspace_load_failed",
])
def test_sort_is_rejected_during_unavailable_workflow(window, monkeypatch, flag):
    cases = [_case(window, number) for number in ("2023011302572", "2019011302572")]
    _install(window, cases)
    before = [case.to_dict() for case in cases]
    monkeypatch.setattr(window, flag, True)
    monkeypatch.setattr(window, "_save", lambda **kwargs: pytest.fail("must not save while blocked"))
    window.sort_cases_ascending()
    assert [case.to_dict() for case in cases] == before
    assert window.statusBar().currentMessage()


def test_save_failure_keeps_sorted_rows_dirty_for_retry(window, monkeypatch):
    late, early = [_case(window, number) for number in ("2023011302572", "2019011302572")]
    _install(window, [late, early])
    errors = []
    monkeypatch.setattr(QMessageBox, "critical", lambda *args: errors.append(args[2]))

    def fail(*args, **kwargs):
        raise WorkspaceError("test disk failure")

    monkeypatch.setattr(window.repository, "save_changes", fail)
    window.sort_cases_ascending()
    assert [case.uid for case in window.cases] == [early.uid, late.uid]
    assert window.case_list.tree.topLevelItem(0).text(0) == early.display_id
    assert window._dirty and window._order_dirty
    assert {late.uid, early.uid} <= window._dirty_cases
    assert errors == ["test disk failure"]
    _, saved = _saved(window)
    assert [case.uid for case in saved] == [late.uid, early.uid]
