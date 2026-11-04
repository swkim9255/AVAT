"""Optional annotations survive saves without entering investigation outputs."""

from __future__ import annotations

import json
import sqlite3

from investigator.detection_process import DetectionSettings, _load_runtime_manifest
from investigator.form_definition import FormDefinition
from investigator.models import InvestigationCase
from investigator.object_detection import ROLE_TRIGGER, DetectedObject, ObjectBox, VideoObjects
from investigator.report_export import _context
from investigator.statistics import StatisticsEngine
from investigator.storage import CaseRepository


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


def test_object_save_preserves_current_version_without_creating_backups(tmp_path):
    repository = CaseRepository(tmp_path / "project")
    case = InvestigationCase(videos=["clip.mp4"])
    repository.save([case])
    assert schema(repository.database_path) == 4
    add_optional(case)
    repository.save_changes([case])
    assert schema(repository.database_path) == 4
    assert not repository.backup_directory.exists()
    restored = repository.load()[0]
    assert restored.video_objects().objects[0].subtype_key is None
    assert restored.video_objects().objects[0].boxes[0].source_prompt == "person"
    repository.save_changes([restored], form_definition=FormDefinition.default())
    assert schema(repository.database_path) == 4
    assert not repository.backup_directory.exists()


def test_normal_annotation_save_does_not_invoke_backup(tmp_path, monkeypatch):
    repository = CaseRepository(tmp_path / "project")
    case = InvestigationCase(videos=["clip.mp4"])
    repository.save([case])
    add_optional(case)

    def unexpected_backup(*args, **kwargs):
        raise AssertionError("Normal annotation saves must not create an upgrade backup")

    monkeypatch.setattr(repository, "backup", unexpected_backup)
    repository.save_changes([case])
    assert repository.load()[0].video_objects().objects[0].uid == "p"
    assert schema(repository.database_path) == 4


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


def test_previous_yoloe_manifest_is_not_loaded(tmp_path):
    executable = tmp_path / "python.exe"
    executable.touch()
    checkpoint = tmp_path / "model.pt"
    checkpoint.write_bytes(b"checkpoint")
    manifest = tmp_path / "runtime.json"
    manifest.write_text(json.dumps({
        "version": 1, "backend": "yoloe", "python": "python.exe", "model": "model.pt", "offline": True,
    }), encoding="utf-8")
    settings, error = _load_runtime_manifest(manifest)
    assert settings is None and "Grounding DINO" in error
    assert checkpoint.read_bytes() == b"checkpoint"
    assert DetectionSettings().backend == "grounding_dino"
