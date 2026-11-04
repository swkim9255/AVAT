"""The environment dialog must retain offline operation when saving settings."""

from PySide6.QtWidgets import QLabel

from investigator.detection_process import DetectionSettings
from investigator.widgets.detection_panel import DetectionSettingsDialog


def test_offline_selection_roundtrips(qtbot):
    settings = DetectionSettings(python="native-python.exe", model="local-sam3", offline=True)
    dialog = DetectionSettingsDialog(settings)
    qtbot.addWidget(dialog)
    assert dialog.offline_check.isChecked()
    saved = dialog.settings()
    assert saved.python == settings.python
    assert saved.model == settings.model
    assert saved.offline is True
    dialog.offline_check.setChecked(False)
    assert dialog.settings().offline is False


def test_invalid_manifest_is_visible_and_can_be_corrected(qtbot):
    dialog = DetectionSettingsDialog(DetectionSettings(config_error="Invalid local runtime"))
    qtbot.addWidget(dialog)
    assert any(label.text() == "Invalid local runtime" for label in dialog.findChildren(QLabel))
    dialog.python_edit.setText("corrected-python.exe")
    assert dialog.settings().config_error == ""
