"""Canonical-only database discovery and workspace-relative video identities."""

from __future__ import annotations

import os
from copy import deepcopy
from pathlib import Path

import pytest

from investigator.models import InvestigationCase
from investigator.object_detection import ROLE_BOTH, DetectedObject, ObjectBox, VideoObjects
from investigator.storage import (
    AUTO_IMPORT_EXCLUSIONS_KEY,
    DATABASE_DIRECTORY,
    DATABASE_FILENAME,
    PATH_STORAGE,
    CaseRepository,
    WorkspaceError,
)


def read_payload(repository):
    with repository._connection() as connection:
        return repository._read_payload(connection)


def annotated_case(workspace):
    videos = [str(workspace / "영상폴더" / year / "영상1" / "전방영상.mp4") for year in ("21년 영상", "22년 영상")]
    case = InvestigationCase(values={"institution": "samsung", "accident_classification": "2021010100001"}, videos=videos)
    case.markers_by_video = {
        videos[0]: {"trigger_frame": 4, "collision_frames": [10], "speed_points": [{"frame": 2, "distance_m": None}, {"frame": 5, "distance_m": 8}]},
        "__case__": {"trigger_frame": None, "collision_frames": []},
    }
    case.rotation_by_video = {videos[0]: 90, videos[1]: 270}
    case.set_video_objects(VideoObjects([DetectedObject(
        "object-uid", "V001", ROLE_BOTH, None,
        [ObjectBox(4, (10, 20, 30, 40), .9, "car")],
        class_key="vehicle", subtype_key="passenger",
    )]))
    return case


def test_new_layout_persists_relative_paths_without_mutating_case(tmp_path):
    repository = CaseRepository(tmp_path / "project")
    case = annotated_case(repository.workspace)
    before = deepcopy(case.to_dict())
    excluded = str(repository.workspace / "영상폴더" / "deleted.mp4")
    repository.save([case], active_case_uid=case.uid, auto_import_exclusions={excluded})
    assert repository.database_path == repository.workspace / DATABASE_DIRECTORY / DATABASE_FILENAME
    assert not (repository.workspace / 'investigations.db').exists()
    assert case.to_dict() == before
    raw = read_payload(repository)
    assert raw["schema_version"] == 4
    assert raw["path_storage"] == PATH_STORAGE
    assert raw["cases"][0]["videos"] == ["영상폴더/21년 영상/영상1/전방영상.mp4", "영상폴더/22년 영상/영상1/전방영상.mp4"]
    assert raw["cases"][0]["rotation_by_video"] == {raw["cases"][0]["videos"][0]: 90, raw["cases"][0]["videos"][1]: 270}
    assert "__case__" in raw["cases"][0]["markers_by_video"]
    assert raw[AUTO_IMPORT_EXCLUSIONS_KEY] == ["영상폴더/deleted.mp4"]
    restored = repository.load()[0]
    assert restored.to_dict() == before
    assert repository.auto_import_exclusions == {excluded.casefold()}
    assert repository.active_case_uid() == case.uid


def test_rename_workspace_rebases_all_video_identities_not_only_video_list(tmp_path):
    workspace = tmp_path / "original"
    repository = CaseRepository(workspace)
    case = annotated_case(repository.workspace)
    repository.save([case], auto_import_exclusions={str(workspace / "영상폴더" / "deleted.mp4")})
    stored_before = read_payload(repository)
    renamed = tmp_path / "renamed workspace"
    workspace.rename(renamed)
    moved = CaseRepository(renamed)
    restored = moved.load()[0]
    assert all(Path(path).is_absolute() and Path(path).is_relative_to(renamed) for path in restored.videos)
    assert restored.rotation_by_video == {restored.videos[0]: 90, restored.videos[1]: 270}
    assert restored.markers_by_video[restored.videos[0]]["trigger_frame"] == 4
    assert restored.video_objects().objects[0].uid == "object-uid"
    assert restored.uid == case.uid
    assert restored.created_at == case.created_at and restored.updated_at == case.updated_at
    assert moved.auto_import_exclusions == {str(renamed / "영상폴더" / "deleted.mp4").casefold()}
    assert read_payload(moved) == stored_before
    moved.save_changes([restored])
    assert read_payload(moved) == stored_before


def test_incremental_save_only_rewrites_changed_case(tmp_path):
    repository = CaseRepository(tmp_path / "project")
    cases = [InvestigationCase(values={"institution": "samsung", "accident_classification": str(i)}) for i in (1, 2)]
    repository.save(cases)
    with repository._connection() as connection:
        connection.executescript("CREATE TABLE changed(uid TEXT); CREATE TRIGGER watch_cases AFTER UPDATE OF payload ON cases BEGIN INSERT INTO changed VALUES(new.uid); END;")
    cases[0].values["driver_sex"] = "남성"
    repository.save_changes([cases[0]])
    with repository._connection() as connection:
        assert [row[0] for row in connection.execute("SELECT uid FROM changed")] == [cases[0].uid]


def test_external_same_volume_video_is_relative_and_round_trips(tmp_path):
    repository = CaseRepository(tmp_path / "project")
    video = str(tmp_path / "external" / "clip.mp4")
    case = InvestigationCase(videos=[video])
    repository.save([case])
    assert read_payload(repository)["cases"][0]["videos"] == ["../external/clip.mp4"]
    assert repository.load()[0].videos == [video]


@pytest.mark.skipif(os.name != "nt", reason="Windows drives")
def test_cross_drive_path_remains_absolute(tmp_path):
    repository = CaseRepository(tmp_path / "project")
    drive = "Z:" if repository.workspace.drive.upper() != "Z:" else "Y:"
    video = drive + "\\external\\clip.mp4"
    repository.save([InvestigationCase(videos=[video])])
    assert read_payload(repository)["cases"][0]["videos"] == [video]
    assert repository.load()[0].videos == [video]


def test_relative_payload_requires_workspace_and_correct_format_marker(tmp_path):
    repository = CaseRepository(tmp_path)
    repository.save([InvestigationCase()])
    payload = read_payload(repository)
    with pytest.raises(WorkspaceError, match="작업 폴더"):
        repository._decode_payload(payload)
    payload["path_storage"] = "unknown"
    with pytest.raises(WorkspaceError):
        repository._decode_payload(payload, workspace=tmp_path)


def test_backup_refuses_original_legacy_source_destination(tmp_path):
    repository = CaseRepository(tmp_path / "project")
    repository.save([])
    with pytest.raises(WorkspaceError):
        repository.backup(repository.workspace / 'investigations.db')


def test_relative_inputs_are_anchored_at_workspace_not_current_directory(tmp_path, monkeypatch):
    repository = CaseRepository(tmp_path / "project")
    unrelated = tmp_path / "unrelated"
    unrelated.mkdir()
    monkeypatch.chdir(unrelated)
    case = InvestigationCase(videos=["nested/clip.mp4"])
    case.set_trigger(7)
    repository.save([case])
    loaded = repository.load()[0]
    assert loaded.videos == [str(repository.workspace / "nested" / "clip.mp4")]
    assert loaded.trigger_frame == 7


def test_current_database_load_does_not_rewrite_cases_or_create_backups(tmp_path):
    repository = CaseRepository(tmp_path / "project")
    case = annotated_case(repository.workspace)
    original = case.to_dict()
    repository.save([case], active_case_uid=case.uid)
    stored = read_payload(repository)
    with repository._connection() as connection:
        connection.executescript(
            "CREATE TABLE changed(uid TEXT); "
            "CREATE TRIGGER watch_cases AFTER UPDATE OF payload ON cases "
            "BEGIN INSERT INTO changed VALUES(new.uid); END;"
        )

    loaded, = repository.load()

    assert loaded.to_dict() == original
    assert repository.active_case_uid() == case.uid
    assert read_payload(repository) == stored
    assert not repository.backup_directory.exists()
    with repository._connection() as connection:
        assert connection.execute("SELECT COUNT(*) FROM changed").fetchone()[0] == 0


def test_load_rejects_duplicate_case_identity_without_changing_rows(tmp_path):
    repository = CaseRepository(tmp_path / "project")
    case = annotated_case(repository.workspace)
    repository.save([case])
    duplicate = annotated_case(repository.workspace)
    with repository._connection() as connection:
        repository._write_case(connection, duplicate, 1)
    stored = read_payload(repository)

    with pytest.raises(WorkspaceError, match="중복 사고 식별자"):
        repository.load()

    assert read_payload(repository) == stored
    assert not repository.backup_directory.exists()


@pytest.mark.parametrize("relative", [
    "investigations.db", "investigations.json", "작업관리.db",
    "작업관리/investigations.db", "작업관리/investigations.json", "other/작업관리.db",
    "systems/작업관리.db", "작업관리/작업관리.db", "작업관리DB/작업관리.db",
    "01_사고동영상/작업관리/기존.db",
])
@pytest.mark.parametrize("operation", ["load", "save", "backup"])
def test_other_database_names_are_never_opened_or_modified(tmp_path, monkeypatch, relative, operation):
    repository = CaseRepository(tmp_path / "project")
    ignored = repository.workspace / relative
    ignored.parent.mkdir(parents=True, exist_ok=True)
    ignored.write_bytes(b"not a valid DB: must not be read")
    before = ignored.read_bytes()
    connection = repository._connection
    opened = []

    def record(path=None, **kwargs):
        target = path or repository.database_path
        opened.append(target)
        assert target != ignored
        return connection(path, **kwargs)

    monkeypatch.setattr(repository, "_connection", record)
    if operation == "load":
        assert repository.load() == []
        assert not repository.database_path.exists()
        assert not opened
    elif operation == "save":
        case = InvestigationCase()
        repository.save([case])
        assert repository.load()[0].uid == case.uid
    else:
        repository.backup(tmp_path / "explicit backup.db")
        assert repository.database_path.is_file()
    assert ignored.read_bytes() == before
    assert not repository.backup_directory.exists()


def test_valid_wrong_name_is_ignored_until_user_renames_it(tmp_path):
    repository = CaseRepository(tmp_path / "project")
    case = annotated_case(repository.workspace)
    repository.save([case])
    ignored = repository.workspace / "investigations.db"
    repository.database_path.rename(ignored)
    before = ignored.read_bytes()
    assert CaseRepository(repository.workspace).load() == []
    assert ignored.read_bytes() == before
    ignored.rename(repository.database_path)
    assert CaseRepository(repository.workspace).load()[0].uid == case.uid


def test_backup_cannot_overwrite_ignored_old_systems_db(tmp_path):
    repository = CaseRepository(tmp_path)
    repository.save([])
    old = tmp_path / "systems" / "작업관리.db"
    old.parent.mkdir()
    old.write_bytes(b"existing old database, must remain untouched")
    with pytest.raises(WorkspaceError, match="원본 데이터"):
        repository.backup(old)
    assert old.read_bytes() == b"existing old database, must remain untouched"


def test_corrupt_canonical_never_falls_back_to_other_names(tmp_path):
    repository = CaseRepository(tmp_path / "project")
    repository.ensure_workspace()
    repository.database_path.write_bytes(b"broken canonical")
    ignored = repository.workspace / "investigations.db"
    ignored.write_bytes(b"do not read")
    with pytest.raises(WorkspaceError):
        repository.load()
    assert repository.database_path.read_bytes() == b"broken canonical"
    assert ignored.read_bytes() == b"do not read"
