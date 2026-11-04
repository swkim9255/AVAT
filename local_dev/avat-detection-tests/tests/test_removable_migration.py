import builtins
from pathlib import Path

import pytest

from investigator import storage
from investigator.models import InvestigationCase


@pytest.fixture
def without_migration(monkeypatch):
    """Simulate the documented one-line comment and a missing optional module."""
    source = Path(storage.__file__).read_text(encoding="utf-8")
    line = "from investigator.legacy_migration import migrate_legacy as migrator"
    assert source.count(line) == 1
    source = source.replace(line, "# " + line)
    original_import = builtins.__import__

    def no_migration(name, *args, **kwargs):
        if name == "investigator.legacy_migration":
            raise ModuleNotFoundError(name)
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_migration)
    namespace = {"__name__": "investigator.storage_without_migration"}
    exec(compile(source, storage.__file__, "exec"), namespace)
    return namespace["CaseRepository"], namespace["WorkspaceError"]


def test_disabled_migration_allows_new_database_and_existing_database(without_migration, tmp_path):
    repository_class, _ = without_migration
    repo = repository_class(tmp_path)
    assert repo.load() == []
    case = InvestigationCase()
    repo.save_changes([case], active_case_uid=case.uid)
    repo.legacy_database_path.write_text("old JSON must not be read", encoding="utf-8")
    reopened = repository_class(tmp_path)
    assert reopened.load()[0].uid == case.uid
    reopened.save_changes([])
    assert reopened.backup(tmp_path / "backup.db").exists()


@pytest.mark.parametrize("operation", ["load", "save", "backup"])
def test_disabled_migration_blocks_json_only_without_data_loss(without_migration, tmp_path, operation):
    repository_class, error = without_migration
    repo = repository_class(tmp_path)
    repo.legacy_database_path.write_text('{"cases": []}', encoding="utf-8")
    original = repo.legacy_database_path.read_bytes()
    with pytest.raises(error, match="자동 이전 기능이 비활성화"):
        if operation == "load":
            repo.load()
        elif operation == "save":
            repo.save_changes([])
        else:
            repo.backup(tmp_path / "backup.db")
    assert repo.legacy_database_path.read_bytes() == original
    assert not repo.database_path.exists()
