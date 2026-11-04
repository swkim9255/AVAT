"""Review missing video links without changing accidents or moving files."""

from __future__ import annotations

import ntpath
from pathlib import Path

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from investigator.models import InvestigationCase
from investigator.storage_worker import StorageWorker
from investigator.video_import import VIDEO_EXTENSIONS
from investigator.video_relink import (
    RelinkMatch,
    apply_workspace_video_path_mapping,
    plan_relink,
)
from investigator.widgets.scroll_safe_combo import ScrollSafeComboBox

REASONS = {
    "relative_root": "상대경로 일치",
    "suffix": "하위 경로 일치 · 확인 필요",
    "filename_only": "파일명만 일치 · 직접 선택",
    "ambiguous": "중복 후보 · 직접 선택",
    "not_found": "후보 없음 · 파일 지정 가능",
    "relative_missing": "기존 하위 경로 없음 · 직접 선택",
    "target_conflict": "다른 영상과 연결 충돌 · 직접 선택",
}
VIDEO_FILTER = "영상 파일 (" + " ".join(f"*{ext}" for ext in sorted(VIDEO_EXTENSIONS)) + ")"


def _key(path: str, workspace: Path) -> str:
    value = path if ntpath.isabs(path) else str(workspace / path)
    return ntpath.normpath(value.replace("/", "\\")).casefold()


class VideoRelinkDialog(QDialog):
    """A selection-only dialog; MainWindow owns the eventual backup and commit."""

    def __init__(
        self,
        cases: list[InvestigationCase],
        workspace: Path,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("영상 폴더 다시 연결")
        self.resize(1060, 610)
        self.setMinimumSize(640, 390)
        self._cases = list(cases)
        self._workspace = Path(workspace).expanduser().resolve()
        self._matches: tuple[RelinkMatch, ...] = ()
        self._combos: list[QComboBox] = []
        self._worker = StorageWorker(self)
        self._busy = False
        self._closed = False
        self._scan_roots: tuple[str, str] | None = None
        self.finished.connect(self._finish_worker)

        layout = QVBoxLayout(self)
        note = QLabel(
            "이동·이름 변경한 영상의 새 위치를 연결합니다. "
            "T/C·속도·회전·검출 결과·조사값·완료 상태는 유지하며 영상 파일은 이동하지 않습니다.",
            self,
        )
        note.setWordWrap(True)
        layout.addWidget(note)

        form = QFormLayout()
        folder_row = QWidget(self)
        folder_layout = QHBoxLayout(folder_row)
        folder_layout.setContentsMargins(0, 0, 0, 0)
        default_folder = self._workspace / "영상폴더"
        self.new_root_edit = QLineEdit(
            str(default_folder if default_folder.is_dir() else self._workspace), folder_row
        )
        self.new_root_edit.setMinimumWidth(0)
        self.browse_button = QPushButton("폴더 선택…", folder_row)
        self.browse_button.clicked.connect(self._browse_folder)
        folder_layout.addWidget(self.new_root_edit, 1)
        folder_layout.addWidget(self.browse_button)
        form.addRow("새 영상 상위 폴더", folder_row)

        self.old_root_edit = QLineEdit(self)
        self.old_root_edit.setPlaceholderText("선택 사항 · 변경 전 상위 폴더 경로를 붙여넣으세요")
        self.old_root_edit.setToolTip(
            "예: D:\\기존 영상폴더 → 새 폴더의 하위 경로와 정확히 대응합니다. "
            "존재하지 않는 이전 폴더도 입력할 수 있습니다."
        )
        form.addRow("이전 영상 상위 폴더", self.old_root_edit)
        layout.addLayout(form)

        controls = QHBoxLayout()
        self.scan_button = QPushButton("연결 검사", self)
        self.scan_button.clicked.connect(self.refresh)
        self.scan_hint = QLabel("파일명만 같거나 후보가 여러 개인 경우 직접 선택해 주세요.", self)
        self.scan_hint.setWordWrap(True)
        controls.addWidget(self.scan_button)
        controls.addWidget(self.scan_hint, 1)
        layout.addLayout(controls)

        self.table = QTableWidget(0, 5, self)
        self.table.setHorizontalHeaderLabels(["사고", "기존 영상", "새 영상", "확인 결과", ""])
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.table.setTextElideMode(Qt.TextElideMode.ElideMiddle)
        self.table.setWordWrap(False)
        self.table.verticalHeader().setVisible(False)
        header = self.table.horizontalHeader()
        header.setMinimumSectionSize(45)
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(4, QHeaderView.ResizeMode.Fixed)
        self.table.setColumnWidth(0, 150)
        self.table.setColumnWidth(3, 170)
        self.table.setColumnWidth(4, 85)
        layout.addWidget(self.table, 1)

        footer = QHBoxLayout()
        self.count_label = QLabel("연결 검사를 실행해 주세요.", self)
        self.count_label.setWordWrap(True)
        footer.addWidget(self.count_label, 1)
        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel, self)
        self.apply_button = self.buttons.addButton("선택한 영상 연결", QDialogButtonBox.ButtonRole.AcceptRole)
        self.apply_button.setEnabled(False)
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        footer.addWidget(self.buttons)
        layout.addLayout(footer)
        self.new_root_edit.textChanged.connect(self._paths_changed)
        self.old_root_edit.textChanged.connect(self._paths_changed)
        self.new_root_edit.returnPressed.connect(self.refresh)
        self.old_root_edit.returnPressed.connect(self.refresh)
        # Avoid recursively scanning an entire legacy workspace (with runtimes,
        # models, etc.) before its user has selected an intended video root.
        if default_folder.is_dir():
            QTimer.singleShot(0, self._initial_refresh)

    def _initial_refresh(self) -> None:
        if not self._closed and not self._matches:
            self.refresh()

    def _finish_worker(self, _result: int) -> None:
        self._closed = True
        self._worker.shutdown()

    def _paths_changed(self) -> None:
        self.apply_button.setEnabled(False)
        self.count_label.setText("폴더 경로가 변경되었습니다. 연결 검사를 다시 실행해 주세요.")

    def _browse_folder(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "새 영상 상위 폴더 선택", self.new_root_edit.text())
        if folder:
            self.new_root_edit.setText(folder)
            self.refresh()

    def refresh(self) -> None:
        if self._busy or self._closed:
            return
        root = Path(self.new_root_edit.text().strip()).expanduser()
        if not root.is_dir():
            QMessageBox.warning(self, "영상 연결 검사", "새 영상 상위 폴더를 확인해 주세요.")
            return
        self._busy = True
        self.scan_button.setEnabled(False)
        self.browse_button.setEnabled(False)
        self.apply_button.setEnabled(False)
        self.count_label.setText("영상을 찾고 있습니다…")
        try:
            matches = self._worker.run(
                plan_relink,
                self._cases,
                root,
                old_root=self.old_root_edit.text().strip() or None,
                workspace_root=self._workspace,
            )
        except (OSError, ValueError) as error:
            self._matches = ()
            self._combos.clear()
            self.table.setRowCount(0)
            self.count_label.setText("영상 연결 검사를 완료하지 못했습니다.")
            QMessageBox.warning(self, "영상 연결 검사", str(error))
        else:
            self._render_matches(matches)
        finally:
            self._busy = False
            self.scan_button.setEnabled(True)
            self.browse_button.setEnabled(True)

    def _render_matches(self, matches: tuple[RelinkMatch, ...]) -> None:
        self._scan_roots = (self.new_root_edit.text().strip(), self.old_root_edit.text().strip())
        self._matches = matches
        self._combos.clear()
        self.table.setRowCount(len(matches))
        labels: dict[str, list[str]] = {}
        for case in self._cases:
            for path in case.videos:
                labels.setdefault(_key(path, self._workspace), []).append(case.display_id)
        for row, match in enumerate(matches):
            for column, text in (
                (0, ", ".join(dict.fromkeys(labels.get(_key(match.old_path, self._workspace), [])))),
                (1, match.old_path),
                (3, REASONS.get(match.reason, match.reason)),
            ):
                item = QTableWidgetItem(text)
                item.setToolTip(text)
                self.table.setItem(row, column, item)
            combo = ScrollSafeComboBox(self.table)
            combo.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
            combo.setMinimumWidth(0)
            combo.setMinimumContentsLength(12)
            combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
            combo.setMaxVisibleItems(12)
            combo.addItem("연결하지 않음", None)
            for path in match.candidates:
                combo.addItem(path, path)
                combo.setItemData(combo.count() - 1, path, Qt.ItemDataRole.ToolTipRole)
            if match.proposed_path is not None:
                combo.setCurrentIndex(combo.findData(match.proposed_path))
            combo.setToolTip(combo.currentData() or "새 영상 선택")
            combo.currentIndexChanged.connect(lambda _index, box=combo: self._selection_changed(box))
            self._combos.append(combo)
            self.table.setCellWidget(row, 2, combo)
            button = QPushButton("파일 지정…", self.table)
            button.clicked.connect(lambda _checked=False, index=row: self._choose_file(index))
            self.table.setCellWidget(row, 4, button)
            self.table.setRowHeight(row, max(34, combo.sizeHint().height() + 4))
        self._update_count()

    def _selection_changed(self, combo: QComboBox) -> None:
        combo.setToolTip(combo.currentData() or "새 영상 선택")
        self._update_count()

    def _choose_file(self, row: int) -> None:
        filename, _ = QFileDialog.getOpenFileName(
            self, "이전 영상에 대응하는 파일 선택", self.new_root_edit.text(), VIDEO_FILTER,
        )
        if not filename:
            return
        path = Path(filename).expanduser().resolve()
        if not path.is_file() or path.suffix.casefold() not in VIDEO_EXTENSIONS:
            QMessageBox.warning(self, "영상 파일 지정", "지원하는 영상 파일을 선택해 주세요.")
            return
        combo = self._combos[row]
        target = str(path)
        index = combo.findData(target)
        if index < 0:
            combo.addItem(target, target)
            index = combo.count() - 1
            combo.setItemData(index, target, Qt.ItemDataRole.ToolTipRole)
        combo.setCurrentIndex(index)
        status = self.table.item(row, 3)
        status.setText("직접 지정 · 확인 필요")
        status.setToolTip(target)

    def selected_mapping(self) -> dict[str, str]:
        return {
            match.old_path: str(combo.currentData())
            for match, combo in zip(self._matches, self._combos, strict=True)
            if combo.currentData() is not None
        }

    def _update_count(self) -> None:
        selected = len(self.selected_mapping())
        total = len(self._matches)
        self.count_label.setText(
            f"연결 끊긴 영상 {total}개 · 선택 {selected}개 · 미선택 {total - selected}개"
            if total else "연결이 끊긴 영상이 없습니다."
        )
        roots_match = self._scan_roots == (
            self.new_root_edit.text().strip(), self.old_root_edit.text().strip()
        )
        self.apply_button.setEnabled(selected > 0 and roots_match)

    def accept(self) -> None:
        mapping = self.selected_mapping()
        if not mapping or not self.apply_button.isEnabled():
            return
        try:
            # A final dry validation catches duplicate explicit targets and
            # files disappearing after scanning. It never mutates live cases.
            self._busy = True
            self.apply_button.setEnabled(False)
            self._worker.run(
                apply_workspace_video_path_mapping,
                self._cases,
                mapping,
                workspace_root=self._workspace,
            )
        except (OSError, ValueError) as error:
            QMessageBox.warning(self, "영상 연결 확인", str(error))
            self._update_count()
            return
        finally:
            self._busy = False
        self._worker.shutdown()
        super().accept()

    def reject(self) -> None:
        self._worker.shutdown()
        super().reject()
