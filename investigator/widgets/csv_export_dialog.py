"""Options for exporting investigation data and companion analysis files."""

from pathlib import Path

from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QLabel,
    QVBoxLayout,
    QWidget,
)

from investigator.speed_csv import speed_csv_path
from investigator.storage import data_specification_path


class CsvExportDialog(QDialog):
    def __init__(self, destination: Path, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("CSV 내보내기 옵션")
        self.setMinimumWidth(440)
        layout = QVBoxLayout(self)
        description = QLabel(f"CSV 파일: {destination.name}", self)
        description.setWordWrap(True)
        layout.addWidget(description)
        speed_companion = speed_csv_path(destination)
        speed_detail = QLabel(
            f"속도 분석 CSV도 같은 폴더에 자동 저장합니다: {speed_companion.name}\n"
            "영상별 트리거·충돌·속도 지점의 시점과 계산된 속도를 포함하며, "
            "기존 조사 CSV의 칼럼은 유지합니다.",
            self,
        )
        speed_detail.setObjectName("speedCsvExportDetail")
        speed_detail.setWordWrap(True)
        layout.addWidget(speed_detail)
        if speed_companion.exists():
            speed_warning = QLabel(
                "같은 이름의 속도 분석 CSV가 있습니다. 내보내면 기존 파일을 덮어씁니다.",
                self,
            )
            speed_warning.setObjectName("speedCsvOverwriteWarning")
            speed_warning.setWordWrap(True)
            layout.addWidget(speed_warning)
        self.include_specification = QCheckBox("데이터 명세서(원본)도 함께 내보내기", self)
        self.include_specification.setObjectName("includeDataSpecification")
        self.include_specification.setChecked(True)
        layout.addWidget(self.include_specification)
        companion = data_specification_path(destination)
        detail = QLabel(
            f"같은 폴더에 {companion.name} 파일을 저장합니다.\n"
            "앱에 포함된 원본 명세서를 그대로 저장하며, "
            "조사 항목을 수정·추가해도 명세서는 바뀌지 않습니다.",
            self,
        )
        detail.setObjectName("dataSpecificationExportDetail")
        detail.setWordWrap(True)
        layout.addWidget(detail)
        if companion.exists():
            warning = QLabel("같은 이름의 명세서가 있습니다. 선택하면 기존 파일을 덮어씁니다.", self)
            warning.setWordWrap(True)
            layout.addWidget(warning)
            self.include_specification.toggled.connect(warning.setVisible)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel,
            parent=self,
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("내보내기")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("취소")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
