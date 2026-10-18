"""Options for exporting an investigation CSV and its data specification."""

from pathlib import Path

from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QLabel,
    QVBoxLayout,
    QWidget,
)

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
        self.include_specification = QCheckBox("데이터 명세서(94개 항목)도 함께 내보내기", self)
        self.include_specification.setObjectName("includeDataSpecification")
        self.include_specification.setChecked(True)
        layout.addWidget(self.include_specification)
        companion = data_specification_path(destination)
        detail = QLabel(f"같은 폴더에 {companion.name} 파일을 저장합니다.", self)
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
