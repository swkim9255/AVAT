from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from investigator.form_definition import FormDefinition
from investigator.models import InvestigationCase
from investigator.storage import CaseRepository, WorkspaceError
from tests.helpers import valid_values


def make_case(number: int) -> InvestigationCase:
    return InvestigationCase(values={**valid_values(), "accident_classification": f"{number:013d}"})


def legacy_payload(cases: list[InvestigationCase], definition: FormDefinition | None = None) -> dict:
    definition = definition or FormDefinition.default()
    return {
        "schema_version": 2,
        "form_definition": definition.to_dict(),
        "initial_form_definition": FormDefinition.default().to_dict(),
        "active_case_uid": cases[-1].uid if cases else None,
        "auto_import_exclusions": [],
        "cases": [case.to_dict() for case in cases],
    }


def read_metadata(path: Path) -> dict:
    with sqlite3.connect(path) as connection:
        return {key: json.loads(value) for key, value in connection.execute("SELECT key, value FROM workspace_metadata")}


def create_write_audit(path: Path) -> None:
    with sqlite3.connect(path) as connection:
        connection.executescript("""
            CREATE TABLE writes(uid TEXT, operation TEXT);
            CREATE TRIGGER case_updated AFTER UPDATE ON cases BEGIN
                INSERT INTO writes VALUES (new.uid, 'update');
            END;
            CREATE TRIGGER case_inserted AFTER INSERT ON cases BEGIN
                INSERT INTO writes VALUES (new.uid, 'insert');
            END;
            CREATE TRIGGER case_deleted AFTER DELETE ON cases BEGIN
                INSERT INTO writes VALUES (old.uid, 'delete');
            END;
        """)


def read_writes(path: Path) -> list[tuple[str, str]]:
    with sqlite3.connect(path) as connection:
        return connection.execute("SELECT uid, operation FROM writes").fetchall()


def test_incremental_save_updates_only_changed_row_and_leaves_other_metadata(tmp_path: Path) -> None:
    repo = CaseRepository(tmp_path)
    cases = [make_case(i) for i in range(10)]
    excluded = str(tmp_path / "skip.mp4").casefold()
    repo.save(cases, active_case_uid=cases[0].uid, auto_import_exclusions={excluded})
    metadata_before = read_metadata(repo.database_path)
    create_write_audit(repo.database_path)

    cases[5].set_value("remarks", "한 사고만 수정")
    repo.save_changes([cases[5]])
    assert read_writes(repo.database_path) == [(cases[5].uid, "update")]
    assert read_metadata(repo.database_path) == metadata_before

    repo.save_changes([cases[5]])
    repo.save(cases, active_case_uid=cases[0].uid)
    assert read_writes(repo.database_path) == [(cases[5].uid, "update")]
    assert repo.auto_import_exclusions == {excluded}


def test_metadata_save_does_not_read_or_validate_cases(tmp_path: Path, monkeypatch) -> None:
    repo = CaseRepository(tmp_path)
    cases = [make_case(i) for i in range(3)]
    repo.save(cases)
    create_write_audit(repo.database_path)
    monkeypatch.setattr(InvestigationCase, "from_dict", lambda *args, **kwargs: pytest.fail("Case decoding on metadata-only save"))
    repo.save_changes([], active_case_uid=cases[1].uid)
    assert repo.active_case_uid() == cases[1].uid
    assert read_writes(repo.database_path) == []


def test_delete_reorder_and_insert_are_atomic_and_persistent(tmp_path: Path) -> None:
    repo = CaseRepository(tmp_path)
    a, b, c, new = [make_case(i) for i in range(4)]
    repo.save([a, b, c], active_case_uid=b.uid)
    repo.save_changes([new], deleted_uids=[b.uid], order=[new.uid, c.uid, a.uid])
    reopened = CaseRepository(tmp_path)
    assert [case.uid for case in reopened.load()] == [new.uid, c.uid, a.uid]
    assert reopened.active_case_uid() is None


def test_invalid_order_rolls_back_case_change_deletion_and_metadata(tmp_path: Path) -> None:
    repo = CaseRepository(tmp_path)
    cases = [make_case(i) for i in range(3)]
    repo.save(cases, active_case_uid=cases[0].uid)
    before = [case.to_dict() for case in repo.load()]
    cases[1].set_value("remarks", "저장되지 않아야 함")
    with pytest.raises(WorkspaceError, match="표시 순서"):
        repo.save_changes([cases[1]], deleted_uids=[cases[0].uid], order=[cases[1].uid], active_case_uid=cases[2].uid)
    assert [case.to_dict() for case in repo.load()] == before
    assert repo.active_case_uid() == cases[0].uid


def test_partial_duplicate_identity_is_rejected_but_atomic_swap_is_allowed(tmp_path: Path) -> None:
    repo = CaseRepository(tmp_path)
    a, b = make_case(1), make_case(2)
    repo.save([a, b])
    a.set_value("accident_classification", b.values["accident_classification"])
    with pytest.raises(WorkspaceError, match="중복"):
        repo.save_changes([a])
    assert repo.load()[0].values["accident_classification"] == "0000000000001"
    b.set_value("accident_classification", "0000000000001")
    repo.save_changes([a, b])
    assert [case.values["accident_classification"] for case in repo.load()] == ["0000000000002", "0000000000001"]


def test_legacy_migration_preserves_all_state_and_never_rewrites_json(tmp_path: Path) -> None:
    repo = CaseRepository(tmp_path)
    definition = FormDefinition.default()
    definition.tabs[-1]["title"] = "상세 비고"
    a, b = make_case(1), make_case(2)
    a.videos = [str(tmp_path / "one.mp4"), str(tmp_path / "two.mp4")]
    a.active_video_index = 1
    a.set_trigger(5)
    a.set_collision(22)
    a.set_video_rotation(270)
    a.values["remarks"] = "긴 비고\n" * 100
    a.complete()
    payload = legacy_payload([b, a], definition)
    payload["auto_import_exclusions"] = [str(tmp_path / "skip.mp4")]
    source = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    repo.legacy_database_path.write_bytes(source)

    loaded = repo.load()
    assert repo.database_path.read_bytes().startswith(b"SQLite format 3\x00")
    assert [case.to_dict() for case in loaded] == [b.to_dict(), a.to_dict()]
    assert repo.active_case_uid() == a.uid
    assert repo.form_definition.to_dict() == definition.to_dict()
    assert repo.initial_form_definition.to_dict() == FormDefinition.default().to_dict()
    assert repo.auto_import_exclusions == {str(tmp_path / "skip.mp4").casefold()}
    loaded[0].set_value("remarks", "DB만 수정")
    repo.save_changes([loaded[0]])
    assert repo.legacy_database_path.read_bytes() == source
    assert not list(tmp_path.glob(".investigations.*"))


def test_migration_activation_failure_keeps_json_and_removes_staging_database(tmp_path: Path, monkeypatch) -> None:
    repo = CaseRepository(tmp_path)
    source = json.dumps(legacy_payload([make_case(1)])).encode()
    repo.legacy_database_path.write_bytes(source)

    def cannot_activate(*args):
        raise PermissionError("simulated write failure")

    monkeypatch.setattr("investigator.storage.os.rename", cannot_activate)
    with pytest.raises(WorkspaceError):
        repo.load()
    assert repo.legacy_database_path.read_bytes() == source
    assert not repo.database_path.exists()
    assert not list(tmp_path.glob(".investigations.*"))


def test_migration_verification_failure_never_activates_partial_database(tmp_path: Path, monkeypatch) -> None:
    repo = CaseRepository(tmp_path)
    source = json.dumps(legacy_payload([make_case(1)])).encode()
    repo.legacy_database_path.write_bytes(source)
    original = repo._read_payload

    def mismatch(connection):
        payload = original(connection)
        payload["cases"] = []
        return payload

    monkeypatch.setattr(repo, "_read_payload", mismatch)
    with pytest.raises(WorkspaceError, match="검증"):
        repo.load()
    assert repo.legacy_database_path.read_bytes() == source
    assert not repo.database_path.exists()


def test_existing_corrupt_db_never_falls_back_to_legacy_or_gets_overwritten(tmp_path: Path) -> None:
    repo = CaseRepository(tmp_path)
    repo.legacy_database_path.write_text(json.dumps(legacy_payload([make_case(1)])), encoding="utf-8")
    broken = b"not a SQLite database"
    repo.database_path.write_bytes(broken)
    with pytest.raises(WorkspaceError):
        repo.load()
    with pytest.raises(WorkspaceError):
        repo.save_changes([make_case(2)])
    assert repo.database_path.read_bytes() == broken


def test_backup_includes_committed_wal_and_is_standalone(tmp_path: Path) -> None:
    repo = CaseRepository(tmp_path)
    case = make_case(1)
    repo.save([case])
    held_connection = sqlite3.connect(repo.database_path)
    held_connection.execute("SELECT COUNT(*) FROM cases").fetchone()
    try:
        case.set_value("remarks", "WAL에 커밋한 최신 값")
        repo.save_changes([case], active_case_uid=case.uid)
        assert Path(str(repo.database_path) + "-wal").exists()
        destination = tmp_path / "backup" / "investigations.db"
        assert repo.backup(destination) == destination
    finally:
        held_connection.close()
    assert not Path(str(destination) + "-wal").exists()
    restored = CaseRepository(destination.parent)
    assert restored.load()[0].values["remarks"] == "WAL에 커밋한 최신 값"
    assert restored.active_case_uid() == case.uid


def test_repository_operations_use_thread_local_connections(tmp_path: Path) -> None:
    repo = CaseRepository(tmp_path)
    case = make_case(1)
    repo.save([case])
    case.set_value("remarks", "작업 스레드")
    with ThreadPoolExecutor(max_workers=1) as executor:
        executor.submit(repo.save_changes, [case]).result()
    assert repo.load()[0].values["remarks"] == "작업 스레드"


def test_active_uid_after_load_is_cached_without_reopening_database(tmp_path: Path, monkeypatch) -> None:
    repo = CaseRepository(tmp_path)
    case = make_case(1)
    repo.save([case], active_case_uid=case.uid)
    reopened = CaseRepository(tmp_path)
    reopened.load()
    monkeypatch.setattr(reopened, "_connection", lambda *args, **kwargs: pytest.fail("unexpected DB reread"))
    assert reopened.active_case_uid() == case.uid


@pytest.mark.parametrize("commit", [False, True])
def test_process_exit_preserves_committed_and_discards_uncommitted_changes(tmp_path: Path, commit: bool) -> None:
    repo = CaseRepository(tmp_path)
    case = make_case(1)
    case.values["remarks"] = "before"
    repo.save([case])
    code = """
import json, os, sqlite3, sys
db = sqlite3.connect(sys.argv[1])
db.execute('PRAGMA journal_mode=WAL')
db.execute('PRAGMA synchronous=FULL')
payload = json.loads(db.execute('SELECT payload FROM cases').fetchone()[0])
payload['values']['remarks'] = 'after'
db.execute('UPDATE cases SET payload=?', (json.dumps(payload),))
if sys.argv[2] == 'True':
    db.commit()
os._exit(0)
"""
    subprocess.run([sys.executable, "-c", code, str(repo.database_path), str(commit)], check=True, timeout=10)
    assert repo.load()[0].values["remarks"] == ("after" if commit else "before")


def test_form_change_revalidates_other_completed_cases_and_rolls_back(tmp_path: Path) -> None:
    repo = CaseRepository(tmp_path)
    case = make_case(1)
    case.complete()
    repo.save([case])
    original = repo.form_definition.to_dict()
    definition = FormDefinition.from_dict(repo.form_definition.to_dict())
    remarks = next(record for record in definition.records() if record["key"] == "remarks")
    remarks["required"] = True
    with pytest.raises(WorkspaceError, match="완료"):
        repo.save_changes([], form_definition=definition)
    assert repo.form_definition.to_dict() == original
    assert repo.load()[0].status == "completed"


def test_invalid_json_value_rolls_back_without_altering_existing_data(tmp_path: Path) -> None:
    repo = CaseRepository(tmp_path)
    case = make_case(1)
    repo.save([case])
    case.values["custom_nan"] = float("nan")
    with pytest.raises(WorkspaceError):
        repo.save_changes([case])
    assert "custom_nan" not in repo.load()[0].values


@pytest.mark.parametrize("key, value", [
    ("initial_form_definition", {"version": 999}),
    ("schema_version", 999),
    ("auto_import_exclusions", [None]),
])
def test_invalid_metadata_rejects_save_before_any_case_is_committed(tmp_path: Path, key: str, value) -> None:
    repo = CaseRepository(tmp_path)
    case = make_case(1)
    repo.save([case])
    with sqlite3.connect(repo.database_path) as connection:
        original_payload = connection.execute("SELECT payload FROM cases").fetchone()[0]
        connection.execute("UPDATE workspace_metadata SET value=? WHERE key=?", (json.dumps(value), key))
    case.set_value("remarks", "must not commit")
    with pytest.raises(WorkspaceError):
        repo.save_changes([case])
    with sqlite3.connect(repo.database_path) as connection:
        assert connection.execute("SELECT payload FROM cases").fetchone()[0] == original_payload


def test_missing_schema_version_rejects_save_without_silent_metadata_reset(tmp_path: Path) -> None:
    repo = CaseRepository(tmp_path)
    case = make_case(1)
    repo.save([case])
    with sqlite3.connect(repo.database_path) as connection:
        original_payload = connection.execute("SELECT payload FROM cases").fetchone()[0]
        connection.execute("DELETE FROM workspace_metadata WHERE key='schema_version'")
    case.set_value("remarks", "must not commit")
    with pytest.raises(WorkspaceError, match="누락"):
        repo.save_changes([case])
    with sqlite3.connect(repo.database_path) as connection:
        assert connection.execute("SELECT payload FROM cases").fetchone()[0] == original_payload
        assert not connection.execute("SELECT value FROM workspace_metadata WHERE key='schema_version'").fetchall()


@pytest.mark.parametrize("suffix", ["-wal", "-shm", "-journal"])
def test_backup_refuses_existing_destination_sidecars(tmp_path: Path, suffix: str) -> None:
    repo = CaseRepository(tmp_path)
    repo.save([make_case(1)])
    destination = tmp_path / "backup.db"
    destination.write_bytes(b"existing destination")
    sidecar = Path(str(destination) + suffix)
    sidecar.write_bytes(b"existing sidecar")
    with pytest.raises(WorkspaceError, match="보조 파일"):
        repo.backup(destination)
    assert destination.read_bytes() == b"existing destination"
    assert sidecar.read_bytes() == b"existing sidecar"


def test_cleanup_error_does_not_mask_migration_verification_error(tmp_path: Path, monkeypatch) -> None:
    repo = CaseRepository(tmp_path)
    repo.legacy_database_path.write_text(json.dumps(legacy_payload([make_case(1)])), encoding="utf-8")
    original_read = repo._read_payload
    original_unlink = Path.unlink

    def mismatch(connection):
        payload = original_read(connection)
        payload["cases"] = []
        return payload

    def cannot_unlink(path, *args, **kwargs):
        if path.name.startswith(".investigations."):
            raise PermissionError("simulated cleanup failure")
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(repo, "_read_payload", mismatch)
    monkeypatch.setattr(Path, "unlink", cannot_unlink)
    with pytest.raises(WorkspaceError, match="검증"):
        repo.load()
    assert not repo.database_path.exists()
