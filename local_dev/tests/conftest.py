"""Keep every test away from the real user's AVAT settings.

The app keeps project tabs and logs under %LOCALAPPDATA%\\AVAT and detection
preferences in QSettings() (the registry on Windows). Without this file, tests
that open MainWindow added their temporary projects to the developer's real
project tabs. Each test now gets an empty, throwaway location instead.
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

import pytest
from PySide6.QtCore import QCoreApplication, QSettings

_SESSION_HOME = Path(tempfile.mkdtemp(prefix="avat-tests-"))


def _use_settings_home(home: Path) -> None:
    os.environ["LOCALAPPDATA"] = str(home)
    QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope, str(home / "qsettings"))


def pytest_configure(config: pytest.Config) -> None:
    # Runs before collection: importing investigator.app already opens the log file.
    QSettings.setDefaultFormat(QSettings.Format.IniFormat)
    # Without an organization name QSettings() silently stores nothing, so
    # persistence tests depended on which earlier test had set one.
    if not QCoreApplication.organizationName():
        QCoreApplication.setOrganizationName("AVAT-tests")
    _use_settings_home(_SESSION_HOME)


def pytest_unconfigure(config: pytest.Config) -> None:
    diagnostics = sys.modules.get("investigator.diagnostics")
    if diagnostics is not None:
        diagnostics.shutdown_diagnostics()  # release the log files before removal
    shutil.rmtree(_SESSION_HOME, ignore_errors=True)


@pytest.fixture(autouse=True)
def isolated_user_settings(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Fresh tabs, logs and QSettings for each test; nothing leaks between tests."""
    home = tmp_path_factory.mktemp("user-settings")
    previous = os.environ["LOCALAPPDATA"]
    _use_settings_home(home)
    yield home
    _use_settings_home(Path(previous))
