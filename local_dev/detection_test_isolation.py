"""Keep feature/regression checks away from real per-user workspace settings."""

import os

import pytest
from PySide6.QtCore import QCoreApplication, QObject, QSettings, Signal

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture(autouse=True)
def isolate_project_settings(request, tmp_path, monkeypatch):
    if not {"qtbot", "qapp"}.intersection(request.fixturenames):
        yield
        return
    monkeypatch.setattr(
        "investigator.project_tabs_state.default_project_tabs_path",
        lambda: tmp_path / "settings" / "project_tabs.json",
    )
    organization = QCoreApplication.organizationName()
    application = QCoreApplication.applicationName()
    settings_format = QSettings.defaultFormat()
    QCoreApplication.setOrganizationName("AVAT-FeatureTests")
    QCoreApplication.setApplicationName("detection")
    QSettings.setDefaultFormat(QSettings.Format.IniFormat)
    QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope, str(tmp_path / "ini"))
    yield
    QCoreApplication.setOrganizationName(organization)
    QCoreApplication.setApplicationName(application)
    QSettings.setDefaultFormat(settings_format)


class SilentAudio(QObject):
    error_occurred = Signal(str)

    def __getattr__(self, name):
        return lambda *args, **kwargs: None


@pytest.fixture(autouse=True)
def isolate_synthetic_audio(request, monkeypatch):
    if request.path.name not in {"test_audio_playback.py", "test_media_logging.py"}:
        monkeypatch.setattr("investigator.video_controller.AudioPlayback", SilentAudio)
