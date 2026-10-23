"""Optional annotations survive saves without entering investigation outputs."""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing

import pytest

from investigator.detection_process import DetectionSettings, _load_runtime_manifest
from investigator.form_definition import FormDefinition
from investigator.models import InvestigationCase
from investigator.object_detection import ROLE_TRIGGER, DetectedObject, ObjectBox, VideoObjects
from investigator.report_export import _context
from investigator.statistics import StatisticsEngine
from investigator.storage import CaseRepository, WorkspaceError


def add_optional(case):
    case.set_video_objects(VideoObjects([DetectedObject(
        "p", "P001", ROLE_TRIGGER, None, [ObjectBox(0, (10, 10, 20, 20), .9, "person")],
        class_key="pedestrian", subtype_key=None,
    )]))


def schema(path):
    with sqlite3.connect(path) as connection:
        return json.loads(connection.execute(
            "SELECT value FROM workspace_metadata WHERE key='schema_version'"
        ).fetchone()[0])


def test_first_object_save_backs_up_before_upgrade_and_preserves_future_version(tmp_path):
    repository = CaseRepository(tmp_path / "project")
    case = InvestigationCase(videos=["clip.mp4"])
    repository.save([case])
    assert schema(repository.database_path) in (1, 2)
    add_optional(case)
    repository.save_changes([case])
    assert schema(repository.database_path) == 3
    backups = list((repository.workspace / ".backups").glob("*.db"))
    assert len(backups) == 1
    with closing(sqlite3.connect(backups[0])) as connection:
        connection.row_factory = sqlite3.Row
        backup_cases = repository._decode_payload(repository._read_payload(connection))[0]
    assert not backup_cases[0].objects_by_video
    restored = repository.load()[0]
    assert restored.video_objects().objects[0].subtype_key is None
    assert restored.video_objects().objects[0].boxes[0].source_prompt == "person"
    repository.save_changes([restored], form_definition=FormDefinition.default())
    assert schema(repository.database_path) == 3
    assert len(list((repository.workspace / ".backups").glob("*.db"))) == 1


def test_backup_failure_does_not_write_new_annotations(tmp_path, monkeypatch):
    repository = CaseRepository(tmp_path / "project")
    case = InvestigationCase(videos=["clip.mp4"])
    repository.save([case])
    add_optional(case)
    monkeypatch.setattr(repository, "backup", lambda *_: (_ for _ in ()).throw(OSError("disk full")))
    with pytest.raises(WorkspaceError, match="백업"):
        repository.save_changes([case])
    assert not repository.load()[0].objects_by_video
    assert schema(repository.database_path) in (1, 2)


def test_optional_objects_do_not_change_form_statistics_csv_word_or_completed_status(tmp_path):
    definition = FormDefinition.default()
    case = InvestigationCase(videos=["clip.mp4"], values={"obstacle_exists": "무"})
    case.status, case.completed_at = "completed", "kept"
    repository = CaseRepository(tmp_path)
    repository.export_csv(tmp_path / "before.csv", [case])
    before_csv = (tmp_path / "before.csv").read_bytes()
    before_word = _context(case, definition)
    before_stats = StatisticsEngine([case], definition).case_value(case, "obstacle_pedestrians")
    before_values = dict(case.values)
    add_optional(case)
    assert case.status == "completed" and case.completed_at == "kept"
    assert case.values == before_values
    assert _context(case, definition) == before_word
    assert StatisticsEngine([case], definition).case_value(case, "obstacle_pedestrians") == before_stats
    repository.export_csv(tmp_path / "after.csv", [case])
    assert (tmp_path / "after.csv").read_bytes() == before_csv


def test_yoloe_manifest_remains_compatible_with_dino_default(tmp_path):
    executable = tmp_path / "python.exe"
    executable.touch()
    checkpoint = tmp_path / "model.pt"
    checkpoint.write_bytes(b"checkpoint")
    manifest = tmp_path / "runtime.json"
    manifest.write_text(json.dumps({
        "version": 1, "backend": "yoloe", "python": "python.exe", "model": "model.pt", "offline": True,
    }), encoding="utf-8")
    settings, error = _load_runtime_manifest(manifest)
    assert not error and settings.backend == "yoloe"
    assert settings.model == str(checkpoint)
    assert DetectionSettings().backend == "grounding_dino"
