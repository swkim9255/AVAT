from __future__ import annotations

import csv
import hashlib
import sqlite3
from decimal import Decimal

import pytest

from investigator.statistics import StatisticsEngine, StatsCondition, StatsFilter
from tools.sample_statistics import COLUMNS, DEFAULT_SOURCE, ROOT, import_project, read_source

SOURCE = DEFAULT_SOURCE


def test_import_preserves_every_csv_value_and_workflow_without_fake_evidence(tmp_path):
    original = SOURCE.read_bytes()
    repository, created = import_project(SOURCE, tmp_path / "samples")
    cases = repository.load()
    rows, digest = read_source(SOURCE)
    assert created and len(cases) == len(rows) == 152
    assert hashlib.sha256(SOURCE.read_bytes()).hexdigest() == digest
    assert SOURCE.read_bytes() == original
    for case, row in zip(cases, rows, strict=True):
        assert {column: case.values['csv_' + column] for column in COLUMNS} == row
        assert case.status == 'draft' and case.values['institution'] == 'samsung'
        assert not case.videos and not case.markers_by_video
        assert case.values.get('incident_minor_injury') is None
        assert case.values.get('incident_severe_injury') is None
        assert case.values.get('incident_date') is None
        assert Decimal(case.values['vehicle_mileage']) == Decimal(row['vehicle_mileage_10k_km']) * 10000


def test_csv_completed_filter_and_raw_numeric_statistics_match_source(tmp_path):
    repository, _ = import_project(SOURCE, tmp_path / "samples")
    cases = repository.load()
    engine = StatisticsEngine(cases, repository.form_definition)
    filters = StatsFilter(status='all', conditions=(StatsCondition('csv_workflow_status', 'completed'),))
    selected = engine.filtered_cases(filters)
    assert len(selected) == 128
    assert len(engine.filtered_cases(StatsFilter(status='all'))) == 152
    cross = engine.cross_table('csv_light_condition', 'csv_damage_level', filters)
    assert sum(cell.count for row in cross.cells for cell in row) == 128
    scatter = engine.scatter('csv_trigger_speed_kph', 'csv_trigger_to_collision_s', filters)
    assert scatter.n == 128 and scatter.excluded == 0
    assert scatter.r is not None and scatter.slope is not None
    groups = engine.numeric_groups('csv_collision_target', 'csv_trigger_speed_kph', filters)
    assert groups.valid_count == 128
    assert sum(group.n for group in groups.groups) == 128
    assert all(engine.case_value(case, 'trigger_speed_kph') is None for case in cases)
    assert engine.video(StatsFilter(status='all')).total_videos == 0


def test_repeat_import_does_not_overwrite_a_modified_sample(tmp_path):
    repository, _ = import_project(SOURCE, tmp_path / "samples")
    cases = repository.load()
    cases[0].set_value('remarks', 'User sample edits must survive reopening')
    repository.save_changes([cases[0]])
    with sqlite3.connect(repository.database_path) as connection:
        before = list(connection.iterdump())
    repeated, created = import_project(SOURCE, repository.workspace)
    assert not created
    with sqlite3.connect(repeated.database_path) as connection:
        assert list(connection.iterdump()) == before
    assert repeated.load()[0].values['remarks'] == 'User sample edits must survive reopening'


def test_import_refuses_non_sample_destination_and_malformed_csv(tmp_path):
    target = tmp_path / "existing"
    target.mkdir()
    marker = target / 'real-data.txt'
    marker.write_text('Keep existing files', encoding='utf-8')
    with pytest.raises(ValueError, match='기존 파일'):
        import_project(SOURCE, target)
    assert marker.read_text(encoding='utf-8') == 'Keep existing files'
    with pytest.raises(ValueError, match='별도 샘플'):
        import_project(SOURCE, ROOT)
    malformed = tmp_path / 'bad.csv'
    rows, _ = read_source(SOURCE)
    rows[0]['trigger_speed_kph'] = 'NaN'
    with malformed.open('w', encoding='utf-8', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=COLUMNS)
        writer.writeheader()
        writer.writerows(rows)
    with pytest.raises(ValueError, match='유한한'):
        import_project(malformed, tmp_path / 'not-created')
    assert not (tmp_path / 'not-created').exists()


def test_sample_launcher_opens_real_project_filtered_to_source_completed(qapp, qtbot, monkeypatch, tmp_path):
    from investigator import app as application
    from tools.sample_statistics import open_statistics

    repository, _ = import_project(SOURCE, tmp_path / 'samples')
    windows = []
    real_window = application.MainWindow

    def main_window(workspace):
        window = real_window(workspace)
        qtbot.addWidget(window)
        windows.append(window)
        return window

    monkeypatch.setattr(application, 'MainWindow', main_window)
    monkeypatch.setattr(application, 'QApplication', lambda *_: qapp)
    monkeypatch.setattr(qapp, 'exec', lambda: 0)
    assert open_statistics(repository.workspace) == 0
    window = windows[0]
    assert window.mode_tabs.currentIndex() == 1
    assert window.statistics_panel.view_tabs.currentIndex() == 2
    assert window.statistics_panel.target_label.text() == '대상 128건 / 전체 152건'
    assert window.statistics_panel._row_key == 'csv_light_condition'
    assert window.statistics_panel._column_key == 'csv_damage_level'
    window.close()
