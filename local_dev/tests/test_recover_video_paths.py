"""Standalone recovery writes a new DB and never changes its source/media."""

import hashlib
import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from recover_video_paths import APPLICATION_ID, RecoveryError, recover


def seed(tmp_path, *, conflict=False):
    workspace = tmp_path / "project"
    video = workspace / "01_사고동영상/2026/2026100200002_2.mp4"
    video.parent.mkdir(parents=True)
    video.write_bytes(b"real video placeholder; no decoder required")
    old, other = "C:/prj/2026100200002_2.mp4", "D:/old/2026100200002_2.mp4"
    case = {"uid": "original-uid", "values": {"institution": "samsung", "accident_classification": "2026100200002",
                                              "remarks": "조사 내용은 변경하지 않음"},
            "videos": [old, other], "active_video_index": 1,
            "workflow": {"status": "draft", "created_at": "preserve-date"},
            "markers_by_video": {old: {"trigger_frame": 10, "collision_frames": [30],
                                       "speed_points": [{"frame": 5, "distance_m": None},
                                                        {"frame": 15, "distance_m": 8}]},
                                 other: {"trigger_frame": 20 if conflict else 10, "collision_frames": [30],
                                         "speed_points": [{"frame": 5, "distance_m": None},
                                                          {"frame": 15, "distance_m": None if conflict else 8}]}}}
    source = tmp_path / "investigations.db"
    with sqlite3.connect(source) as connection:
        connection.executescript(f"""
            PRAGMA application_id={APPLICATION_ID}; PRAGMA user_version=1;
            CREATE TABLE workspace_metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL);
            CREATE TABLE cases(uid TEXT PRIMARY KEY,position INTEGER,institution TEXT,accident_number TEXT,status TEXT,payload TEXT);
        """)
        connection.executemany("INSERT INTO workspace_metadata VALUES(?,?)", [
            (key, json.dumps(value)) for key, value in {"schema_version": 3, "form_definition": {},
                "initial_form_definition": {}, "active_case_uid": case["uid"], "auto_import_exclusions": []}.items()
        ])
        connection.execute("INSERT INTO cases VALUES(?,0,'samsung','2026100200002','draft',?)",
                           (case["uid"], json.dumps(case)))
    return source, workspace, video, case


def read(path):
    with sqlite3.connect(path) as connection:
        return (json.loads(connection.execute("SELECT payload FROM cases").fetchone()[0]),
                {key: json.loads(value) for key, value in connection.execute("SELECT * FROM workspace_metadata")})


def test_maps_windows_paths_and_annotation_keys_and_preserves_source(tmp_path):
    source, workspace, video, case = seed(tmp_path)
    before = hashlib.sha256(source.read_bytes()).digest()
    output = workspace / "01_사고동영상/작업관리/작업관리.db"
    report = recover(source, workspace, output)
    result, metadata = read(output)
    relative = video.relative_to(workspace).as_posix()
    assert result["videos"] == [relative] and result["active_video_index"] == 0
    assert result["markers_by_video"] == {relative: case["markers_by_video"][case["videos"][0]]}
    assert result["values"] == case["values"] and result["workflow"] == case["workflow"]
    assert result["uid"] == case["uid"]
    assert metadata["schema_version"] == 4 and metadata["path_storage"] == "workspace-relative-v1"
    assert report["video_links_before"] == 2 and report["video_links_after"] == 1
    assert hashlib.sha256(source.read_bytes()).digest() == before
    assert video.read_bytes() == b"real video placeholder; no decoder required"


def test_conflicts_abort_without_output_by_default(tmp_path):
    source, workspace, _, _ = seed(tmp_path, conflict=True)
    output = tmp_path / "must-not-create/fixed.db"
    with pytest.raises(RecoveryError, match="충돌"):
        recover(source, workspace, output)
    assert not output.parent.exists()


def test_explicit_most_complete_keeps_whole_winning_marker_revision(tmp_path):
    source, workspace, video, case = seed(tmp_path, conflict=True)
    output = tmp_path / "fixed.db"
    report = recover(source, workspace, output, conflict="most-complete")
    result, _ = read(output)
    relative = video.relative_to(workspace).as_posix()
    assert result["markers_by_video"][relative] == case["markers_by_video"][case["videos"][0]]
    assert report["conflicts"][0]["selected"] == case["videos"][0]


def test_dry_run_only_returns_plan(tmp_path):
    source, workspace, _, _ = seed(tmp_path)
    output = tmp_path / "must-not-create/fixed.db"
    report = recover(source, workspace, output, dry_run=True)
    assert report["dry_run"] and report["cases"] == 1
    assert not output.parent.exists()


@pytest.mark.parametrize("kind", ["absent", "ambiguous"])
def test_unresolvable_file_names_abort_without_guessing(tmp_path, kind):
    source, workspace, video, _ = seed(tmp_path)
    if kind == "absent":
        video.unlink()
    else:
        duplicate = video.parent.parent / "another" / video.name
        duplicate.parent.mkdir()
        duplicate.write_bytes(b"different video")
    with pytest.raises(RecoveryError, match="확정"):
        recover(source, workspace, tmp_path / "fixed.db")
    assert not (tmp_path / "fixed.db").exists()


def test_never_overwrites_source_or_existing_output(tmp_path):
    source, workspace, _, _ = seed(tmp_path)
    existing = tmp_path / "existing.db"
    existing.write_bytes(b"keep")
    for output in (source, existing):
        with pytest.raises(RecoveryError, match="덮어쓸"):
            recover(source, workspace, output)
    assert existing.read_bytes() == b"keep"


def test_refuses_active_wal_source(tmp_path):
    source, workspace, _, _ = seed(tmp_path)
    Path(str(source) + "-wal").write_bytes(b"active")
    with pytest.raises(RecoveryError, match="AVAT를 종료"):
        recover(source, workspace, tmp_path / "fixed.db")


def test_management_and_deleted_video_trees_are_not_candidates(tmp_path):
    source, workspace, _, _ = seed(tmp_path)
    for directory in ("작업관리", "삭제사고", "systems"):
        ignored = workspace / "01_사고동영상" / directory / "2026100200002_2.mp4"
        ignored.parent.mkdir()
        ignored.write_bytes(b"must ignore")
    assert recover(source, workspace, tmp_path / "fixed.db")["cases"] == 1


def blank_source(source, case, *, all_blank=False):
    case["values"] = {key: case["values"][key] for key in ("institution", "accident_classification")}
    paths = case["videos"] if all_blank else case["videos"][:1]
    for path in paths:
        case["markers_by_video"][path] = {"trigger_frame": None, "collision_frames": []}
    with sqlite3.connect(source) as connection:
        connection.execute("UPDATE cases SET payload=?", (json.dumps(case),))


def test_blank_connections_are_archived_in_original_format_not_deleted(tmp_path):
    source, workspace, video, case = seed(tmp_path)
    blank_source(source, case)
    original = source.read_bytes()
    output = tmp_path / "fixed.db"
    report = recover(source, workspace, output)
    restored, _ = read(output)
    archived, archive_metadata = read(workspace / "미입력 삭제 영상/investigations.db")
    assert restored["videos"] == [video.relative_to(workspace).as_posix()]
    assert archived["videos"] == case["videos"][:1]
    assert archived["markers_by_video"] == {case["videos"][0]: case["markers_by_video"][case["videos"][0]]}
    assert archive_metadata["schema_version"] == 3
    assert report["archived_video_links"] == 1
    assert source.read_bytes() == original and video.is_file()


def test_fully_blank_case_is_only_in_archive_and_wont_be_auto_reimported(tmp_path):
    source, workspace, video, case = seed(tmp_path)
    blank_source(source, case, all_blank=True)
    output = tmp_path / "fixed.db"
    report = recover(source, workspace, output)
    with sqlite3.connect(output) as connection:
        assert connection.execute("SELECT count(*) FROM cases").fetchone()[0] == 0
        metadata = {key: json.loads(value) for key, value in connection.execute("SELECT * FROM workspace_metadata")}
    assert metadata["active_case_uid"] is None
    assert metadata["auto_import_exclusions"] == [video.relative_to(workspace).as_posix()]
    archived, _ = read(workspace / "미입력 삭제 영상/investigations.db")
    assert archived["videos"] == case["videos"]
    assert report["archived_video_links"] == 2 and video.is_file()


def test_missing_unentered_video_is_archived_without_blocking_recovery(tmp_path):
    source, workspace, video, case = seed(tmp_path)
    blank_source(source, case, all_blank=True)
    video.unlink()
    assert recover(source, workspace, tmp_path / "fixed.db")["archived_video_links"] == 2


def test_existing_archive_is_not_overwritten_and_main_output_is_not_created(tmp_path):
    source, workspace, _, case = seed(tmp_path)
    blank_source(source, case)
    archive = workspace / "미입력 삭제 영상/investigations.db"
    archive.parent.mkdir()
    archive.write_bytes(b"keep existing archive")
    output = tmp_path / "fixed.db"
    with pytest.raises(RecoveryError, match="미입력 DB를 덮어쓸"):
        recover(source, workspace, output)
    assert archive.read_bytes() == b"keep existing archive" and not output.exists()


def test_dry_run_does_not_create_the_archive(tmp_path):
    source, workspace, _, case = seed(tmp_path)
    blank_source(source, case)
    report = recover(source, workspace, tmp_path / "fixed.db", dry_run=True)
    assert report["archived_video_links"] == 1
    assert not (workspace / "미입력 삭제 영상").exists()


def version_one(source, case, version=1):
    case["values"].pop("remarks", None)
    case.pop("markers_by_video", None)
    case["markers"] = {"trigger_frame": 0, "collision_frames": [30],
                       "speed_points": [{"frame": 0, "distance_m": None},
                                        {"frame": 15, "distance_m": 8}]}
    with sqlite3.connect(source) as connection:
        connection.execute("UPDATE workspace_metadata SET value=? WHERE key='schema_version'", (json.dumps(version),))
        connection.execute("DELETE FROM workspace_metadata WHERE key IN ('form_definition','initial_form_definition','auto_import_exclusions')")
        connection.execute("UPDATE cases SET payload=?", (json.dumps(case),))


@pytest.mark.parametrize("version", [1, "1"])
def test_v1_missing_forms_and_case_markers_migrate_to_app_readable_v4(tmp_path, version):
    from investigator.storage import CaseRepository

    source, workspace, video, case = seed(tmp_path)
    version_one(source, case, version)
    before = source.read_bytes()
    output = tmp_path / "fixed.db"
    report = recover(source, workspace, output)
    raw, metadata = read(output)
    relative = video.relative_to(workspace).as_posix()
    assert raw["videos"] == [relative]
    assert "markers" not in raw
    assert raw["markers_by_video"][relative] == case["markers"]
    assert metadata["schema_version"] == 4
    assert metadata["form_definition"]["tabs"]
    assert metadata["initial_form_definition"] == metadata["form_definition"]
    assert report["source_schema_version"] == 1 and report["default_form_added"]
    assert report["archived_video_links"] == 1
    with sqlite3.connect(output) as connection:
        connection.row_factory = sqlite3.Row
        payload = CaseRepository._read_payload(connection)
    cases, *_ = CaseRepository._decode_payload(payload, workspace=workspace)
    assert cases[0].trigger_frame == 0 and cases[0].collision_frame == 30
    archived, archive_metadata = read(workspace / "미입력 삭제 영상/investigations.db")
    assert archive_metadata["schema_version"] == 1
    # Legacy-format archives belong to this standalone tool, not the running
    # app. The case-level marker applied only to the other, active video.
    assert archived.get("markers_by_video", {}) == {}
    assert "markers" not in archived
    assert archived["videos"] == case["videos"][:1]
    assert source.read_bytes() == before


def test_v1_custom_form_is_preserved(tmp_path):
    from investigator.form_definition import FormDefinition

    source, workspace, _, case = seed(tmp_path)
    version_one(source, case)
    form = FormDefinition.default().to_dict()
    form["tabs"][1]["title"] = "기존 조사 양식 이름"
    with sqlite3.connect(source) as connection:
        connection.execute("INSERT INTO workspace_metadata VALUES('form_definition',?)", (json.dumps(form),))
    output = tmp_path / "fixed.db"
    report = recover(source, workspace, output)
    _, metadata = read(output)
    assert metadata["form_definition"] == form
    assert not report["default_form_added"]


def test_v1_recovery_cli_runs_without_third_party_libraries(tmp_path):
    source, workspace, _, case = seed(tmp_path)
    version_one(source, case)
    script = Path(__file__).resolve().parents[2] / "recover_video_paths.py"
    completed = subprocess.run([sys.executable, "-S", "-X", "utf8", str(script),
                                "--source", str(source), "--workspace", str(workspace), "--dry-run"],
                               capture_output=True, text=True, encoding="utf-8", timeout=15)
    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout)["source_schema_version"] == 1


@pytest.mark.parametrize("version", [2, 5, None, True, "5"])
def test_unknown_versions_still_fail_with_actual_value_in_error(tmp_path, version):
    source, workspace, _, _ = seed(tmp_path)
    with sqlite3.connect(source) as connection:
        connection.execute("UPDATE workspace_metadata SET value=? WHERE key='schema_version'", (json.dumps(version),))
    with pytest.raises(RecoveryError, match="지원하지 않는 데이터 버전.*지원: 1, 3, 4"):
        recover(source, workspace, tmp_path / "fixed.db")
