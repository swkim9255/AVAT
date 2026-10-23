from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np
import pytest
from PySide6.QtCore import QDate
from PySide6.QtWidgets import QFileDialog, QMessageBox

from investigator.main_window import MainWindow
from investigator.models import InvestigationCase
from investigator.storage import CaseRepository, WorkspaceError
from investigator.video_import import parse_accident_video_name, scan_video_folder


class _Capture:
    """Small valid decoder used by folder-import integration tests."""

    def __init__(self) -> None:
        self.position = 0
        self.frame = np.zeros((4, 6, 3), dtype=np.uint8)

    def isOpened(self) -> bool:  # noqa: N802
        return True

    def read(self):  # type: ignore[no-untyped-def]
        if self.position > 0:
            return False, None
        self.position += 1
        return True, self.frame.copy()

    def get(self, prop: int) -> float:
        if prop == cv2.CAP_PROP_FPS:
            return 20.0
        if prop == cv2.CAP_PROP_FRAME_COUNT:
            return 1.0
        return 0.0

    def set(self, prop: int, value: float) -> bool:
        if prop != cv2.CAP_PROP_POS_FRAMES:
            return False
        self.position = int(value)
        return True

    def release(self) -> None:
        pass


@pytest.mark.parametrize(
    ("filename", "expected"),
    [
        ("0000000000123.mp4", ("0000000000123", 0)),
        ("0000000000123_1.avi", ("0000000000123", 1)),
        ("0000000000123_12.MOV", ("0000000000123", 12)),
        ("123456789012.mp4", None),
        ("12345678901234.mp4", None),
        ("1234567890123_0.mp4", None),
        ("1234567890123_extra.mp4", None),
        ("1234567890123.txt", None),
    ],
)
def test_parse_accident_video_name_requires_thirteen_digits_and_optional_sequence(
    filename: str,
    expected: tuple[str, int] | None,
) -> None:
    assert parse_accident_video_name(filename) == expected


def test_scan_video_folder_groups_and_naturally_sorts_direct_children(tmp_path: Path) -> None:
    video_folder = tmp_path / "incoming"
    video_folder.mkdir()
    names = (
        "9999999999999.webm",
        "0000000000123_10.mp4",
        "0000000000123_2.mov",
        "0000000000123.MKV",
        "0000000000123_1.avi",
        "not-an-accident.mp4",
        "123456789012.txt",
        "notes.txt",
    )
    for name in names:
        (video_folder / name).write_bytes(b"video")

    nested = video_folder / "nested"
    nested.mkdir()
    (nested / "1111111111111.mp4").write_bytes(b"nested video")

    scan = scan_video_folder(video_folder)

    assert [number for number, _ in scan.groups] == ["0000000000123", "9999999999999"]
    assert [Path(path).name for path in scan.groups[0][1]] == [
        "0000000000123.MKV",
        "0000000000123_1.avi",
        "0000000000123_2.mov",
        "0000000000123_10.mp4",
    ]
    assert [Path(path).name for path in scan.groups[1][1]] == ["9999999999999.webm"]
    assert [Path(path).name for path in scan.ignored_videos] == ["not-an-accident.mp4"]
    assert scan.video_count == 5
    assert all("nested" not in Path(path).parts for _, paths in scan.groups for path in paths)


def test_folder_import_populates_samsung_cases_without_default_placeholder(
    qtbot, tmp_path: Path
) -> None:  # type: ignore[no-untyped-def]
    workspace = tmp_path / "workspace"
    video_folder = tmp_path / "incoming"
    video_folder.mkdir()
    for name in (
        "0000000000123_2.mov",
        "9999999999999.webm",
        "0000000000123.mp4",
        "0000000000123_1.avi",
    ):
        (video_folder / name).write_bytes(b"video")

    window = MainWindow(workspace)
    qtbot.addWidget(window)
    assert window.cases == []
    assert window.current_case is None
    window.video_controller._capture_factory = lambda path: _Capture()

    window.import_video_folder(video_folder)

    by_number = {str(case.values["accident_classification"]): case for case in window.cases}
    assert set(by_number) == {"0000000000123", "9999999999999"}
    first = by_number["0000000000123"]
    second = by_number["9999999999999"]
    assert first.values["institution"] == "samsung"
    assert second.values["institution"] == "samsung"
    assert [Path(path).name for path in first.videos] == [
        "0000000000123.mp4",
        "0000000000123_1.avi",
        "0000000000123_2.mov",
    ]
    assert [Path(path).name for path in second.videos] == ["9999999999999.webm"]
    assert first.active_video_index == 0
    assert second.active_video_index == 0
    assert window.current_case is first
    assert window.investigation.editors["institution"].value() == "samsung"
    assert window.investigation.editors["accident_classification"].value() == "0000000000123"
    persisted = {
        str(case.values["accident_classification"]): case
        for case in CaseRepository(workspace).load()
    }
    assert set(persisted) == {"0000000000123", "9999999999999"}
    assert [Path(path).name for path in persisted["0000000000123"].videos] == [
        "0000000000123.mp4",
        "0000000000123_1.avi",
        "0000000000123_2.mov",
    ]


def test_attaching_single_sequenced_video_to_empty_workspace_creates_named_case(
    qtbot, monkeypatch, tmp_path: Path
) -> None:  # type: ignore[no-untyped-def]
    workspace = tmp_path / "workspace"
    source = tmp_path / "incoming" / "1234567890123_1.mp4"
    source.parent.mkdir()
    source.write_bytes(b"video")
    monkeypatch.setattr(MainWindow, "_open_active_video", lambda self, **kwargs: True)

    window = MainWindow(workspace)
    qtbot.addWidget(window)
    assert window.cases == []
    assert window.current_case is None

    window.attach_videos([str(source)])

    assert len(window.cases) == 1
    assert window.current_case is not None
    assert window.current_case.display_id == "SAMSUNG-1234567890123"
    assert window.current_case.videos == [str(source.resolve())]
    assert all(case.display_id != "SAMSUNG-001" for case in window.cases)
    persisted = CaseRepository(workspace).load()
    assert len(persisted) == 1
    assert persisted[0].uid == window.current_case.uid
    assert persisted[0].display_id == "SAMSUNG-1234567890123"
    assert persisted[0].videos == [str(source.resolve())]


def test_reimport_merges_new_videos_without_duplicate_cases_or_paths(qtbot, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    workspace = tmp_path / "workspace"
    video_folder = tmp_path / "incoming"
    video_folder.mkdir()
    (video_folder / "0123456789012.mp4").write_bytes(b"video")
    (video_folder / "0123456789012_1.mp4").write_bytes(b"video")

    window = MainWindow(workspace)
    qtbot.addWidget(window)
    window.video_controller._capture_factory = lambda path: _Capture()
    window.import_video_folder(video_folder)
    assert len(window.cases) == 1
    imported = window.cases[0]
    imported.set_value("driver_sex", "여성")
    original_uid = imported.uid

    window.import_video_folder(video_folder)
    (video_folder / "0123456789012_2.mp4").write_bytes(b"video")
    window.import_video_folder(video_folder)

    assert len(window.cases) == 1
    assert window.cases[0].uid == original_uid
    assert window.cases[0].values["driver_sex"] == "여성"
    assert [Path(path).name for path in window.cases[0].videos] == [
        "0123456789012.mp4",
        "0123456789012_1.mp4",
        "0123456789012_2.mp4",
    ]
    assert len({Path(path).resolve() for path in window.cases[0].videos}) == 3


def test_calendar_selection_writes_date_and_computed_weekday(qtbot, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    window = MainWindow(tmp_path / "workspace")
    qtbot.addWidget(window)
    window.create_case()
    case = window.current_case
    assert case is not None
    date_editor = window.investigation.editors["incident_date"]
    day_editor = window.investigation.editors["incident_day"]

    date_editor.calendar_widget.clicked.emit(QDate(2026, 9, 4))

    assert date_editor.line_edit.text() == "20260904"
    assert date_editor.value() == "20260904"
    assert day_editor.value() == "금"
    assert case.values["incident_date"] == "20260904"
    assert case.values["incident_day"] == "금"

    date_editor.calendar_widget.clicked.emit(QDate(2024, 2, 29))

    assert date_editor.line_edit.text() == "20240229"
    assert day_editor.value() == "목"
    assert case.values["incident_date"] == "20240229"
    assert case.values["incident_day"] == "목"

    date_editor.line_edit.selectAll()
    qtbot.keyClicks(date_editor.line_edit, "20250105")

    assert date_editor.line_edit.text() == "20250105"
    assert day_editor.value() == "일"
    assert case.values["incident_date"] == "20250105"
    assert case.values["incident_day"] == "일"

    date_editor.line_edit.selectAll()
    qtbot.keyClicks(date_editor.line_edit, "20260230")

    assert date_editor.line_edit.text() == "20260230"
    assert day_editor.value() is None
    assert "incident_day" not in case.values


def test_duplicate_folder_import_keeps_current_selection_and_does_not_rewrite(
    qtbot, tmp_path: Path
) -> None:  # type: ignore[no-untyped-def]
    workspace = tmp_path / "workspace"
    video_folder = tmp_path / "incoming"
    video_folder.mkdir()
    for name in ("1000000000000.mp4", "2000000000000.mp4"):
        (video_folder / name).write_bytes(b"video")

    window = MainWindow(workspace)
    qtbot.addWidget(window)
    window.video_controller._capture_factory = lambda path: _Capture()
    window.import_video_folder(video_folder)
    second = next(
        case for case in window.cases if case.values["accident_classification"] == "2000000000000"
    )
    window.select_case(second.uid)
    cases_before = list(window.cases)
    database_before = window.repository.database_path.read_bytes()

    window.import_video_folder(video_folder)

    assert window.current_case is second
    assert all(
        current is previous for current, previous in zip(window.cases, cases_before, strict=True)
    )
    assert window.repository.database_path.read_bytes() == database_before
    assert "새로 가져올 영상이 없습니다" in window.statusBar().currentMessage()


def test_folder_import_save_failure_keeps_live_cases_unchanged(
    qtbot, monkeypatch, tmp_path: Path
) -> None:  # type: ignore[no-untyped-def]
    video_folder = tmp_path / "incoming"
    video_folder.mkdir()
    (video_folder / "1234567890123.mp4").write_bytes(b"video")
    window = MainWindow(tmp_path / "workspace")
    qtbot.addWidget(window)
    original_cases = window.cases
    assert original_cases == []
    assert window.current_case is None
    original_save = window.repository.save_changes
    critical_messages: list[str] = []
    monkeypatch.setattr(
        window.repository,
        "save_changes",
        lambda *args, **kwargs: (_ for _ in ()).throw(WorkspaceError("disk failure")),
    )
    monkeypatch.setattr(
        "investigator.main_window.QMessageBox.critical",
        lambda parent, title, message: critical_messages.append(f"{title}: {message}"),
    )

    window.import_video_folder(video_folder)

    assert window.cases is original_cases
    assert window.cases == []
    assert window.current_case is None
    assert critical_messages == ["저장 오류: disk failure"]
    monkeypatch.setattr(window.repository, "save_changes", original_save)
    window.close()


def test_workspace_load_repairs_legacy_default_video_into_existing_case_with_markers(
    qtbot, monkeypatch, tmp_path: Path
) -> None:  # type: ignore[no-untyped-def]
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    number = "1234567890123"
    first = workspace / f"{number}_1.mp4"
    second = workspace / f"{number}_2.mp4"
    third = workspace / f"{number}_3.mp4"
    for path in (first, second, third):
        path.write_bytes(b"video")

    legacy = InvestigationCase(values={"institution": "samsung", "accident_classification": "001"})
    legacy.attach_videos([first])
    legacy.set_trigger(17)
    legacy.set_collision(29)
    target = InvestigationCase(
        values={
            "institution": "samsung",
            "accident_classification": number,
            "driver_sex": "여성",
        }
    )
    target.attach_videos([third, second])
    CaseRepository(workspace).save([legacy, target], active_case_uid=legacy.uid)
    monkeypatch.setattr(MainWindow, "_open_active_video", lambda self, **kwargs: True)

    window = MainWindow(workspace)
    qtbot.addWidget(window)

    assert len(window.cases) == 1
    repaired = window.cases[0]
    assert repaired.uid == target.uid
    assert repaired.values["driver_sex"] == "여성"
    assert repaired.display_id == f"SAMSUNG-{number}"
    assert [Path(path).name for path in repaired.videos] == [
        f"{number}_1.mp4",
        f"{number}_2.mp4",
        f"{number}_3.mp4",
    ]
    assert repaired.markers_by_video[str(first.resolve())] == {
        "trigger_frame": 17,
        "collision_frames": [29],
    }
    assert all(case.display_id != "SAMSUNG-001" for case in window.cases)

    persisted = CaseRepository(workspace).load()
    assert len(persisted) == 1
    assert persisted[0].uid == target.uid
    assert persisted[0].videos == repaired.videos
    assert persisted[0].markers_by_video == repaired.markers_by_video


def test_workspace_load_renames_wrong_only_legacy_case_without_changing_uid(
    qtbot, monkeypatch, tmp_path: Path
) -> None:  # type: ignore[no-untyped-def]
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    number = "1234567890123"
    source = workspace / f"{number}_1.mp4"
    source.write_bytes(b"video")
    legacy = InvestigationCase(values={"institution": "samsung", "accident_classification": "001"})
    legacy.attach_videos([source])
    legacy.set_trigger(41)
    legacy.set_collision(57)
    CaseRepository(workspace).save([legacy], active_case_uid=legacy.uid)
    monkeypatch.setattr(MainWindow, "_open_active_video", lambda self, **kwargs: True)

    window = MainWindow(workspace)
    qtbot.addWidget(window)

    assert len(window.cases) == 1
    repaired = window.current_case
    assert repaired is not None
    assert repaired.uid == legacy.uid
    assert repaired.display_id == f"SAMSUNG-{number}"
    assert repaired.videos == [str(source.resolve())]
    assert repaired.trigger_frame == 41
    assert repaired.collision_frame == 57
    persisted = CaseRepository(workspace).load()
    assert len(persisted) == 1
    assert persisted[0].uid == legacy.uid
    assert persisted[0].display_id == f"SAMSUNG-{number}"


def test_legacy_repair_save_failure_keeps_live_cases_and_database_unchanged(
    qtbot, monkeypatch, tmp_path: Path
) -> None:  # type: ignore[no-untyped-def]
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    number = "1234567890123"
    first = workspace / f"{number}_1.mp4"
    second = workspace / f"{number}_2.mp4"
    first.write_bytes(b"video")
    second.write_bytes(b"video")
    legacy = InvestigationCase(values={"institution": "samsung", "accident_classification": "001"})
    legacy.attach_videos([first])
    legacy.set_trigger(23)
    target = InvestigationCase(values={"institution": "samsung", "accident_classification": number})
    target.attach_videos([second])
    repository = CaseRepository(workspace)
    repository.save([legacy, target], active_case_uid=legacy.uid)
    expected_cases = [case.to_dict() for case in repository.load()]
    database_before = repository.database_path.read_bytes()
    original_save = CaseRepository.save_changes
    critical_messages: list[str] = []

    def fail_save(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        del self, args, kwargs
        raise WorkspaceError("disk failure")

    monkeypatch.setattr(CaseRepository, "save_changes", fail_save)
    monkeypatch.setattr(MainWindow, "_open_active_video", lambda self, **kwargs: True)
    monkeypatch.setattr(
        QMessageBox,
        "critical",
        lambda parent, title, message: critical_messages.append(f"{title}: {message}"),
    )

    window = MainWindow(workspace)
    qtbot.addWidget(window)

    assert [case.to_dict() for case in window.cases] == expected_cases
    assert window.current_case is not None
    assert window.current_case.uid == legacy.uid
    assert window.repository.database_path.read_bytes() == database_before
    assert critical_messages == ["저장 오류: disk failure"]
    monkeypatch.setattr(CaseRepository, "save_changes", original_save)


def test_unrelated_scan_does_not_remove_user_created_empty_001_case(
    qtbot, monkeypatch, tmp_path: Path
) -> None:  # type: ignore[no-untyped-def]
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    unrelated = workspace / "9999999999999.mp4"
    unrelated.write_bytes(b"video")
    manual = InvestigationCase(values={"institution": "samsung", "accident_classification": "001"})
    CaseRepository(workspace).save([manual], active_case_uid=manual.uid)
    monkeypatch.setattr(MainWindow, "_open_active_video", lambda self, **kwargs: True)

    window = MainWindow(workspace)
    qtbot.addWidget(window)

    assert {case.display_id for case in window.cases} == {
        "SAMSUNG-001",
        "SAMSUNG-9999999999999",
    }
    assert any(case.uid == manual.uid for case in window.cases)


def test_excluded_legacy_video_is_not_detached_during_automatic_repair(
    qtbot, monkeypatch, tmp_path: Path
) -> None:  # type: ignore[no-untyped-def]
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    number = "1234567890123"
    first = workspace / f"{number}_1.mp4"
    second = workspace / f"{number}_2.mp4"
    first.write_bytes(b"video")
    second.write_bytes(b"video")
    legacy = InvestigationCase(values={"institution": "samsung", "accident_classification": "001"})
    legacy.attach_videos([first])
    legacy.set_trigger(11)
    target = InvestigationCase(values={"institution": "samsung", "accident_classification": number})
    target.attach_videos([second])
    CaseRepository(workspace).save(
        [legacy, target],
        active_case_uid=target.uid,
        auto_import_exclusions={str(first.resolve()).casefold()},
    )
    monkeypatch.setattr(MainWindow, "_open_active_video", lambda self, **kwargs: True)

    window = MainWindow(workspace)
    qtbot.addWidget(window)

    preserved = next(case for case in window.cases if case.uid == legacy.uid)
    assert preserved.videos == [str(first.resolve())]
    assert preserved.trigger_frame == 11
    assert str(first.resolve()).casefold() in window.repository.auto_import_exclusions


def test_legacy_duplicate_path_moves_source_marker_when_target_has_none(
    qtbot, monkeypatch, tmp_path: Path
) -> None:  # type: ignore[no-untyped-def]
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    number = "1234567890123"
    source = workspace / f"{number}_1.mp4"
    source.write_bytes(b"video")
    legacy = InvestigationCase(values={"institution": "samsung", "accident_classification": "001"})
    legacy.attach_videos([source])
    legacy.set_trigger(31)
    target = InvestigationCase(values={"institution": "samsung", "accident_classification": number})
    target.attach_videos([source])
    CaseRepository(workspace).save([legacy, target], active_case_uid=legacy.uid)
    monkeypatch.setattr(MainWindow, "_open_active_video", lambda self, **kwargs: True)

    window = MainWindow(workspace)
    qtbot.addWidget(window)

    assert len(window.cases) == 1
    repaired = window.cases[0]
    assert repaired.uid == target.uid
    assert repaired.videos == [str(source.resolve())]
    assert repaired.trigger_frame == 31


def test_workspace_load_auto_imports_direct_videos_without_empty_folder_warning(
    qtbot, monkeypatch, tmp_path: Path
) -> None:  # type: ignore[no-untyped-def]
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "0123456789012_2.mp4").write_bytes(b"video")
    (workspace / "0123456789012_1.mp4").write_bytes(b"video")
    warnings: list[str] = []
    monkeypatch.setattr(
        "investigator.video_controller.VideoController.open_video",
        lambda self, path: True,
    )
    monkeypatch.setattr(
        QMessageBox,
        "warning",
        lambda parent, title, message: warnings.append(f"{title}: {message}"),
    )

    window = MainWindow(workspace)
    qtbot.addWidget(window)

    assert warnings == []
    assert len(window.cases) == 1
    assert window.current_case is not None
    assert window.current_case.values["accident_classification"] == "0123456789012"
    assert [Path(path).name for path in window.current_case.videos] == [
        "0123456789012_1.mp4",
        "0123456789012_2.mp4",
    ]
    assert "불러왔습니다" in window.statusBar().currentMessage()
    assert CaseRepository(workspace).load()[0].videos == window.current_case.videos

    empty_workspace = tmp_path / "empty"
    empty = MainWindow(empty_workspace)
    qtbot.addWidget(empty)
    assert warnings == []
    assert empty.cases == []
    assert empty.current_case is None
    assert empty.case_list.tree.topLevelItemCount() == 0


def test_workspace_load_merges_json_and_choose_workspace_keeps_import_summary(
    qtbot, monkeypatch, tmp_path: Path
) -> None:  # type: ignore[no-untyped-def]
    workspace = tmp_path / "selected"
    workspace.mkdir()
    first_video = workspace / "1111111111111.mp4"
    second_video = workspace / "1111111111111_1.mp4"
    first_video.write_bytes(b"video")
    case = InvestigationCase(
        values={
            "institution": "samsung",
            "accident_classification": "1111111111111",
            "driver_sex": "여성",
        }
    )
    case.attach_videos([first_video])
    CaseRepository(workspace).save([case], active_case_uid=case.uid)
    second_video.write_bytes(b"video")
    monkeypatch.setattr(
        "investigator.video_controller.VideoController.open_video",
        lambda self, path: True,
    )

    window = MainWindow(tmp_path / "initial")
    qtbot.addWidget(window)
    monkeypatch.setattr(
        QFileDialog,
        "getExistingDirectory",
        lambda *args, **kwargs: str(workspace),
    )
    monkeypatch.setattr(window, "_choose_project_open_mode", lambda folder: "current")
    window.choose_workspace()

    assert len(window.cases) == 1
    assert window.current_case is not None
    assert window.current_case.uid == case.uid
    assert window.current_case.values["driver_sex"] == "여성"
    assert [Path(path).name for path in window.current_case.videos] == [
        "1111111111111.mp4",
        "1111111111111_1.mp4",
    ]
    assert "불러왔습니다" in window.statusBar().currentMessage()
    assert "작업 폴더를 열었습니다" not in window.statusBar().currentMessage()


def test_corrupt_workspace_with_video_is_not_auto_imported_or_rewritten(
    qtbot, monkeypatch, tmp_path: Path
) -> None:  # type: ignore[no-untyped-def]
    workspace = tmp_path / "corrupt"
    workspace.mkdir()
    database = workspace / "investigations.json"
    original = b"{broken evidence"
    database.write_bytes(original)
    (workspace / "2222222222222.mp4").write_bytes(b"video")
    critical: list[str] = []
    monkeypatch.setattr(
        QMessageBox,
        "critical",
        lambda parent, title, message: critical.append(f"{title}: {message}"),
    )

    window = MainWindow(workspace)
    qtbot.addWidget(window)

    assert window._workspace_load_failed
    assert window.cases == []
    assert database.read_bytes() == original
    assert len(critical) == 1


def test_deleted_video_stays_excluded_after_reload_but_explicit_import_restores_it(
    qtbot, monkeypatch, tmp_path: Path
) -> None:  # type: ignore[no-untyped-def]
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    base_video = workspace / "1234567890123.mp4"
    removed_video = workspace / "1234567890123_1.mp4"
    base_video.write_bytes(b"video")
    removed_video.write_bytes(b"video")
    monkeypatch.setattr(MainWindow, "_open_active_video", lambda self, **kwargs: True)
    monkeypatch.setattr(
        QMessageBox,
        "question",
        lambda *args, **kwargs: QMessageBox.StandardButton.Yes,
    )

    window = MainWindow(workspace)
    qtbot.addWidget(window)
    case = window.current_case
    assert case is not None
    assert len(case.videos) == 2

    window.delete_video(case.uid, 1)

    assert window.current_case is not None
    assert [Path(path).name for path in window.current_case.videos] == ["1234567890123.mp4"]
    assert str(removed_video.resolve()).casefold() in window.repository.auto_import_exclusions
    window.close()

    reopened = MainWindow(workspace)
    qtbot.addWidget(reopened)
    assert reopened.current_case is not None
    assert [Path(path).name for path in reopened.current_case.videos] == ["1234567890123.mp4"]

    assert reopened.import_video_folder(workspace)
    assert reopened.current_case is not None
    assert [Path(path).name for path in reopened.current_case.videos] == [
        "1234567890123.mp4",
        "1234567890123_1.mp4",
    ]
    assert str(removed_video.resolve()).casefold() not in reopened.repository.auto_import_exclusions


def test_deleted_case_stays_excluded_after_reload_but_explicit_import_restores_it(
    qtbot, monkeypatch, tmp_path: Path
) -> None:  # type: ignore[no-untyped-def]
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    video = workspace / "1234567890123.mp4"
    video.write_bytes(b"video")
    monkeypatch.setattr(MainWindow, "_open_active_video", lambda self, **kwargs: True)
    monkeypatch.setattr(
        QMessageBox,
        "question",
        lambda *args, **kwargs: QMessageBox.StandardButton.Yes,
    )

    window = MainWindow(workspace)
    qtbot.addWidget(window)
    case = window.current_case
    assert case is not None

    window.delete_case(case.uid)

    assert window.cases == []
    assert window.current_case is None
    assert str(video.resolve()).casefold() in window.repository.auto_import_exclusions
    window.close()

    reopened = MainWindow(workspace)
    qtbot.addWidget(reopened)
    assert reopened.cases == []
    assert reopened.current_case is None

    assert reopened.import_video_folder(workspace)
    assert len(reopened.cases) == 1
    assert reopened.current_case is not None
    assert reopened.current_case.values["accident_classification"] == "1234567890123"
    assert reopened.current_case.videos == [str(video.resolve())]
    assert str(video.resolve()).casefold() not in reopened.repository.auto_import_exclusions
