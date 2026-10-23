"""Tests must never read or write the developer's real AVAT settings."""

from __future__ import annotations

import tempfile
from pathlib import Path

from PySide6.QtCore import QSettings

from investigator.diagnostics import default_log_directory
from investigator.project_tabs_state import (
    ProjectTabsState,
    ProjectTabsStore,
    default_project_tabs_path,
)

THROWAWAY = Path(tempfile.gettempdir()).resolve()


def test_tabs_logs_and_qsettings_point_to_a_throwaway_location(qapp):
    settings = QSettings()
    assert settings.format() == QSettings.Format.IniFormat
    assert settings.status() == QSettings.Status.NoError
    for path in (
        default_project_tabs_path(), default_log_directory(), Path(settings.fileName()),
    ):
        assert THROWAWAY in path.resolve().parents


def test_values_written_here(qapp, tmp_path):
    QSettings().setValue("isolation/probe", "left behind?")
    ProjectTabsStore().save(ProjectTabsState((tmp_path,), tmp_path))


def test_do_not_reach_the_next_test(qapp):
    # Runs after the test above; real, shared storage would still hold both.
    assert QSettings().value("isolation/probe") is None
    assert ProjectTabsStore().load() == ProjectTabsState()
