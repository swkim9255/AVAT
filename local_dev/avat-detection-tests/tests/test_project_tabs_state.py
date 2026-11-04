from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest
from PySide6.QtWidgets import QMessageBox

import investigator.main_window as main_window_module
import investigator.project_tabs_state as tabs_module
from investigator.main_window import MainWindow
from investigator.project_tabs_state import (
    ProjectTabsState,
    ProjectTabsStore,
    default_project_tabs_path,
)
from investigator.storage import CaseRepository, WorkspaceError
from tests.test_statistics_panel import accident


def test_default_store_uses_per_user_app_data_not_investigation_folder(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    assert default_project_tabs_path() == tmp_path / "AVAT" / "project_tabs.json"
    monkeypatch.delenv("LOCALAPPDATA")
    monkeypatch.setattr(tabs_module.Path, "home", classmethod(lambda cls: tmp_path))
    assert default_project_tabs_path() == tmp_path / ".avat" / "project_tabs.json"


def test_absent_store_does_not_create_folders(tmp_path):
    store = ProjectTabsStore(tmp_path / "settings" / "project_tabs.json")
    assert store.load() == ProjectTabsState()
    assert not store.path.parent.exists()


def test_store_roundtrips_unicode_order_and_selected_path_without_case_data(tmp_path):
    folders = tuple(tmp_path / name for name in ("사고 영상 A", "검수 B", "C"))
    state = ProjectTabsState(folders, folders[1])
    store = ProjectTabsStore(tmp_path / "settings" / "project_tabs.json")
    store.save(state)
    assert store.load() == state
    assert set(json.loads(store.path.read_text(encoding="utf-8"))) == {"version", "folders", "active_folder"}
    assert all(not folder.exists() for folder in folders)


def test_duplicate_paths_are_deduplicated_without_reordering_and_unknown_selection_falls_back(tmp_path):
    first, second = tmp_path / "first", tmp_path / "second"
    state = ProjectTabsState.from_dict({
        "version": 1, "folders": [str(first), str(second), str(first / ".")],
        "active_folder": str(tmp_path / "not_in_list"),
    })
    assert state == ProjectTabsState((first, second), first)


@pytest.mark.parametrize("payload", [
    None, [], {}, {"version": 2, "folders": []},
    {"version": 1, "folders": "not a list"}, {"version": 1, "folders": [None]},
    {"version": 1, "folders": [""]}, {"version": 1, "folders": ["../unexpected"]},
    {"version": 1, "folders": [], "active_folder": 12},
    {"version": 1, "folders": [], "active_folder": "relative"},
])
def test_invalid_settings_do_not_become_workspace_paths(payload):
    with pytest.raises(ValueError):
        ProjectTabsState.from_dict(payload)


def test_interrupted_atomic_write_preserves_previous_complete_settings(tmp_path, monkeypatch):
    store = ProjectTabsStore(tmp_path / "project_tabs.json")
    initial = ProjectTabsState((tmp_path / "A",), tmp_path / "A")
    store.save(initial)
    original = store.path.read_bytes()
    real_save_file = tabs_module.QSaveFile

    class PartialWrite(real_save_file):
        def write(self, data):
            super().write(data[:5])
            return 5

    monkeypatch.setattr(tabs_module, "QSaveFile", PartialWrite)
    with pytest.raises(OSError):
        store.save(ProjectTabsState((tmp_path / "B",), tmp_path / "B"))
    assert store.path.read_bytes() == original
    assert store.load() == initial


@pytest.fixture
def session(qtbot, tmp_path):
    folders = [tmp_path / name for name in ("project_A", "project_B", "project_C", "project_D")]
    for index, path in enumerate(folders):
        case = accident(f"{index + 1:03}", remarks=path.name)
        CaseRepository(path).save([case], active_case_uid=case.uid)
    store = ProjectTabsStore(tmp_path / "settings" / "project_tabs.json")

    def create(workspace=None):
        window = MainWindow(workspace, project_tabs_store=store)
        qtbot.addWidget(window)
        return window

    return folders, store, create


def assert_tabs(window, folders, active):
    assert [project.repository.workspace for project in window._projects] == list(folders)
    assert [window.project_tabs.tabToolTip(i) for i in range(len(folders))] == [str(path) for path in folders]
    assert window.repository.workspace == active
    assert window.project_tabs.currentIndex() == window._active_project_index == list(folders).index(active)


def test_restart_restores_order_selected_tab_and_saved_investigations_lazily(session):
    (first, second, third, _), store, create = session
    window = create(first)
    assert window.open_project(second)
    assert window.open_project(third)
    window.project_tabs.moveTab(2, 0)
    assert window.switch_project(2)
    window._field_changed("remarks", "edited before shutdown")
    assert window.close()
    assert store.load() == ProjectTabsState((third, first, second), second)
    reopened = create()
    assert_tabs(reopened, [third, first, second], second)
    assert reopened.current_case.uid == "002"
    assert reopened.current_case.values["remarks"] == "edited before shutdown"
    assert [project.initialized for project in reopened._projects] == [False, False, True]
    assert reopened.switch_project(0)
    assert reopened.current_case.uid == "003"
    assert reopened.switch_project(1)
    assert reopened.current_case.uid == "001"
    assert CaseRepository(second).load()[0].values["remarks"] == "edited before shutdown"


def test_tab_changes_are_autosaved_without_waiting_for_application_exit(session, qtbot):
    (first, second, third, _), store, create = session
    window = create(first)
    assert window.open_project(second)
    assert window.open_project(third)
    window.project_tabs.moveTab(2, 0)
    assert window.switch_project(1)
    expected = ProjectTabsState((third, first, second), first)
    qtbot.waitUntil(lambda: store.load() == expected, timeout=3000)
    assert window.close_project(2)
    qtbot.waitUntil(lambda: store.load() == ProjectTabsState((third, first), first), timeout=3000)


@pytest.mark.parametrize("existing", [True, False])
def test_explicit_workspace_keeps_restored_tabs_and_activates_existing_or_adds_new(session, existing):
    (first, second, third, fourth), store, create = session
    store.save(ProjectTabsState((third, first, second), second))
    selected = first if existing else fourth
    window = create(selected)
    expected = [third, first, second] if existing else [third, first, second, fourth]
    assert_tabs(window, expected, selected)
    assert window.current_case.values["remarks"] == selected.name
    assert sum(project.initialized for project in window._projects) == 1


def test_closed_and_replaced_tabs_are_not_restored_but_original_data_remains(session):
    (first, second, third, fourth), store, create = session
    window = create(first)
    assert window.open_project(second)
    assert window.open_project(third)
    assert window.close_project(1)
    assert window.switch_project(0)
    assert window.open_project(fourth, new_tab=False)
    assert window.close()
    assert store.load() == ProjectTabsState((fourth, third), fourth)
    reopened = create()
    assert_tabs(reopened, [fourth, third], fourth)
    assert CaseRepository(first).load()[0].uid == "001"
    assert CaseRepository(second).load()[0].uid == "002"


def test_missing_restored_folder_keeps_tab_without_recreation_and_can_recover(session, tmp_path, monkeypatch):
    (first, _, _, _), store, create = session
    missing = tmp_path / "disconnected_drive" / "investigations"
    store.save(ProjectTabsState((missing, first), missing))
    monkeypatch.setattr(QMessageBox, "critical", lambda *args: pytest.fail("restored missing folder blocked startup"))
    window = create()
    assert_tabs(window, [missing, first], missing)
    assert window._workspace_load_failed
    assert window.cases == []
    assert not window.investigation.isEnabled()
    assert not missing.parent.exists()
    assert window.close()
    assert store.load().folders == (missing, first)
    reopened = create()
    assert not missing.exists()
    recovered_case = accident("099", remarks="drive is back")
    CaseRepository(missing).save([recovered_case])
    assert reopened.open_project(missing)
    assert_tabs(reopened, [missing, first], missing)
    assert not reopened._workspace_load_failed
    assert reopened.current_case.uid == "099"
    assert reopened.investigation.isEnabled()


def test_unreadable_inactive_project_is_not_loaded_until_selected_and_is_never_overwritten(session, tmp_path, monkeypatch):
    (first, _, _, _), store, create = session
    corrupt = tmp_path / "corrupt"
    corrupt.mkdir()
    database = corrupt / "investigations.db"
    database.write_bytes(b"preserve this corrupt database")
    original = database.read_bytes()
    store.save(ProjectTabsState((corrupt, first), first))
    monkeypatch.setattr(QMessageBox, "critical", lambda *args: pytest.fail("restored folder blocked startup"))
    window = create()
    assert_tabs(window, [corrupt, first], first)
    assert not window._workspace_load_failed
    assert window.current_case.uid == "001"
    assert not window._projects[0].initialized
    assert not window.switch_project(0)
    assert window._workspace_load_failed
    assert window.close()
    assert database.read_bytes() == original
    assert store.load().folders == (corrupt, first)


def test_corrupt_settings_do_not_prevent_opening_a_valid_project(session):
    (first, _, _, _), store, create = session
    store.path.parent.mkdir(parents=True)
    store.path.write_text("{ interrupted config", encoding="utf-8")
    window = create(first)
    assert_tabs(window, [first], first)
    assert "탭 설정을 읽지 못했습니다" in window.statusBar().currentMessage()
    assert window.current_case.uid == "001"
    assert window.close()
    assert store.load() == ProjectTabsState((first,), first)


def test_settings_write_failure_does_not_prevent_saving_investigation_or_exit(session, monkeypatch):
    (first, _, _, _), store, create = session
    window = create(first)
    window._field_changed("remarks", "investigation save still works")

    def fail(state):
        raise OSError("settings disk unavailable")

    monkeypatch.setattr(store, "save", fail)
    window._save_project_tabs()
    assert "탭 설정을 저장하지 못했습니다" in window.statusBar().currentMessage()
    assert window.close()
    assert CaseRepository(first).load()[0].values["remarks"] == "investigation save still works"


def test_failed_project_open_or_data_save_does_not_add_an_invalid_restored_tab(session, tmp_path, monkeypatch):
    (first, _, _, _), store, create = session
    window = create(first)
    corrupt = tmp_path / "cannot_open"
    corrupt.mkdir()
    (corrupt / "investigations.db").write_bytes(b"bad")
    monkeypatch.setattr(QMessageBox, "critical", lambda *args: None)
    assert not window.open_project(corrupt)
    window._save_project_tabs()
    assert store.load() == ProjectTabsState((first,), first)
    window._field_changed("remarks", "unsaved")

    def fail_save(*args, **kwargs):
        raise WorkspaceError("disk full")

    with monkeypatch.context() as patch:
        patch.setattr(window.repository, "save_changes", fail_save)
        assert not window.close()
        assert window._dirty
    assert window.close()


def test_first_launch_without_settings_uses_default_workspace(session, tmp_path, monkeypatch):
    _, _, create = session
    default = tmp_path / "default_project"
    monkeypatch.setattr(main_window_module, "default_workspace", lambda: default)
    window = create()
    assert_tabs(window, [default], default)
    assert default.is_dir()
    assert window.cases == []


def test_session_save_waits_for_structural_operation_to_finish(session):
    (first, second, _, _), store, create = session
    window = create(first)
    window._save_project_tabs()
    original = store.load()
    window._switching_project = True
    try:
        window._save_project_tabs()
        assert store.load() == original
        assert window._project_tabs_timer.isActive()
    finally:
        window._switching_project = False
    assert window.open_project(second)
    window._save_project_tabs()
    assert store.load() == ProjectTabsState((first, second), second)


def test_restored_repository_read_does_not_recreate_a_missing_directory(tmp_path):
    missing = tmp_path / "unmounted" / "project"
    with pytest.raises(WorkspaceError, match="폴더를 찾을 수 없습니다"):
        CaseRepository(missing).load(require_existing=True)
    assert not missing.parent.exists()


def test_restored_repository_can_read_existing_data_without_creating_directories(tmp_path, monkeypatch):
    repository = CaseRepository(tmp_path / "existing")
    repository.save([accident("001")])
    monkeypatch.setattr(repository, "ensure_workspace", lambda: pytest.fail("restored load tried to create its workspace"))
    assert repository.load(require_existing=True)[0].uid == "001"


def test_restored_path_that_became_a_file_is_not_overwritten(session, tmp_path):
    _, store, create = session
    collision = tmp_path / "former_project_folder"
    collision.write_text("keep this file", encoding="utf-8")
    store.save(ProjectTabsState((collision,), collision))
    window = create()
    assert window._workspace_load_failed
    assert window.close()
    assert collision.read_text(encoding="utf-8") == "keep this file"
    assert store.load().folders == (collision,)


def test_tabs_restore_across_separate_application_processes(tmp_path):
    """No Python globals or living windows may be required for restoration."""
    script = """
import json
import sys
from pathlib import Path
from PySide6.QtCore import QTimer
from investigator.app import QApplication, MainWindow
app = QApplication([])
root = Path(sys.argv[2])
if sys.argv[1] == 'first':
    window = MainWindow(root / 'A')
    for name in ('B', 'C'):
        folder = root / name
        folder.mkdir()
        assert window.open_project(folder)
    window.project_tabs.moveTab(2, 0)
    assert window.switch_project(2)
else:
    window = MainWindow()
print(json.dumps({'folders':[p.repository.workspace.name for p in window._projects],
                  'active':window.repository.workspace.name,
                  'loaded':[p.initialized for p in window._projects]}))
window.show()
QTimer.singleShot(0, window.close)
app.exec()
"""
    environment = dict(os.environ, LOCALAPPDATA=str(tmp_path / "user_data"),
                       QT_QPA_PLATFORM="offscreen", PYTHONIOENCODING="utf-8")
    results = []
    for mode in ("first", "restored"):
        run = subprocess.run(
            [sys.executable, "-c", script, mode, str(tmp_path)], env=environment,
            capture_output=True, encoding="utf-8", check=True, timeout=30,
        )
        results.append(json.loads(run.stdout.strip()))
    assert results[0]["folders"] == results[1]["folders"] == ["C", "A", "B"]
    assert results[0]["active"] == results[1]["active"] == "B"
    assert results[1]["loaded"] == [False, False, True]
