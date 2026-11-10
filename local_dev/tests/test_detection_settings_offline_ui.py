"""설정 창은 항상 오프라인·번들 Grounding DINO Base로 저장하고 모델을 고르지 않는다."""

from PySide6.QtWidgets import QCheckBox, QComboBox, QFormLayout, QLabel, QLineEdit

import investigator.detection_process as process
from investigator.detection_process import DetectionSettings
from investigator.widgets.detection_panel import DetectionSettingsDialog

REMOVED_ROWS = ("검출 모델", "모델 (파일·폴더 또는 ID)", "모델 연결", "모델 크기")


def _row_labels(dialog):
    labels = []
    for form in dialog.findChildren(QFormLayout):
        for row in range(form.rowCount()):
            item = form.itemAt(row, QFormLayout.ItemRole.LabelRole)
            if item is not None and isinstance(item.widget(), QLabel):
                labels.append(item.widget().text())
    return labels


def test_dialog_has_no_model_or_connection_choices(qtbot, tmp_path, monkeypatch, bundled_dino_model):
    monkeypatch.setattr(process, "REPOSITORY_ROOT", tmp_path)
    settings = DetectionSettings(python="native-python.exe", model="local-sam3", offline=False, device="cpu")
    dialog = DetectionSettingsDialog(settings)
    qtbot.addWidget(dialog)
    for name in ("backend_combo", "model_edit", "offline_check", "dino_model_combo"):
        assert not hasattr(dialog, name)
    labels = _row_labels(dialog)
    assert not set(REMOVED_ROWS) & set(labels)
    assert {"검출용 Python", "장치", "프롬프트 청크 크기", "박스 임계값", "텍스트 임계값", "정밀도"} <= set(labels)
    # 남은 입력은 검출용 Python 한 칸, 선택 상자는 장치 하나, 체크박스는 FP16 하나뿐이다.
    assert len(dialog.findChildren(QLineEdit)) == 1 + 3  # Python 경로 + 숫자 상자 3개의 내부 입력칸
    assert len(dialog.findChildren(QComboBox)) == 1
    assert [box.text() for box in dialog.findChildren(QCheckBox)] == ["GPU에서 FP16 사용"]
    assert any(label.text() == (
        "Grounding DINO Base 모델(박스 전용)을 오프라인으로 실행합니다. "
        "별도 지정이 없으면 작업 공간의 detection-runtime.json을 사용하고, "
        "해당 파일도 없으면 AVAT를 실행한 Python을 사용합니다."
    ) for label in dialog.findChildren(QLabel))
    saved = dialog.settings()
    assert saved.python == settings.python
    assert saved.device == "cpu"
    assert saved.model == str(bundled_dino_model)
    assert saved.offline is True
    assert saved.backend == "grounding_dino"


def test_invalid_manifest_is_visible_and_can_be_corrected(qtbot):
    dialog = DetectionSettingsDialog(DetectionSettings(config_error="Invalid local runtime"))
    qtbot.addWidget(dialog)
    assert any(label.text() == "Invalid local runtime" for label in dialog.findChildren(QLabel))
    dialog.python_edit.setText("corrected-python.exe")
    assert dialog.settings().config_error == ""
