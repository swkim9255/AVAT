"""Choose whether imported videos should be copied into the project."""

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QCheckBox, QDialog, QDialogButtonBox, QLabel, QVBoxLayout, QWidget


class VideoImportDialog(QDialog):
    def __init__(self, workspace: Path, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("영상 가져오기 옵션")
        self.setMinimumWidth(440)
        layout = QVBoxLayout(self)
        self.copy_to_workspace = QCheckBox("작업 폴더로 복사", self)
        self.copy_to_workspace.setObjectName("copyVideosToWorkspace")
        layout.addWidget(self.copy_to_workspace)
        detail = QLabel(
            "선택하면 원본 영상을 현재 프로젝트 폴더에 복사합니다.\n"
            "선택하지 않으면 원래 위치의 영상을 연결합니다.\n"
            "같은 이름의 다른 영상은 순번을 붙여 보관합니다.", self,
        )
        detail.setWordWrap(True)
        layout.addWidget(detail)
        destination = QLabel(f"작업 폴더: {workspace}", self)
        destination.setTextFormat(Qt.TextFormat.PlainText)
        destination.setWordWrap(True)
        layout.addWidget(destination)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel, self
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("가져오기")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("취소")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
