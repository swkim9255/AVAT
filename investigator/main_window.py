"""Top-level three-column investigation workflow."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

from PySide6.QtCore import QDir, QStandardPaths, Qt
from PySide6.QtGui import QAction, QCloseEvent, QKeyEvent, QKeySequence
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QFileDialog,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QSpinBox,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from investigator.models import InvestigationCase, new_case
from investigator.schema import (
    INVALID_FORMAT_MESSAGE,
    MISSING_VALUE_MESSAGE,
    field_label,
)
from investigator.storage import CaseRepository, WorkspaceError
from investigator.theme import PRIMARY, material_icon
from investigator.video_controller import PlaybackState, VideoController, VideoMetadata
from investigator.widgets.case_list import CaseListPanel
from investigator.widgets.form_panel import InvestigationPanel
from investigator.widgets.playback_panel import PlaybackPanel
from investigator.widgets.video_player import VIDEO_EXTENSIONS, VideoPlayerPanel


def default_workspace() -> Path:
    documents = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.DocumentsLocation)
    base = Path(documents) if documents else Path.cwd()
    return base / "사고영상조사"


class MainWindow(QMainWindow):
    def __init__(self, workspace: str | Path | None = None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("mainWindow")
        self.setWindowTitle("사고 영상 조사 도구")
        self.setWindowIcon(material_icon("car_crash", PRIMARY))
        self.setMinimumSize(1180, 760)
        self.resize(1660, 940)

        self.repository = CaseRepository(workspace or default_workspace())
        self.cases: list[InvestigationCase] = []
        self.current_uid: str | None = None
        self._dirty = False
        self._workspace_load_failed = False

        self.video_controller = VideoController(self)
        self.case_list = CaseListPanel(self)
        self.video_player = VideoPlayerPanel(self)
        self.investigation = InvestigationPanel(self)
        self.playback = PlaybackPanel(self)
        self._build_layout()
        self._build_menu()
        self._connect_signals()
        self._load_workspace()

    @property
    def current_case(self) -> InvestigationCase | None:
        return next((case for case in self.cases if case.uid == self.current_uid), None)

    def _build_layout(self) -> None:
        root_widget = QWidget(self)
        root_widget.setObjectName("appRoot")
        root = QVBoxLayout(root_widget)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(1)
        self.splitter = QSplitter(Qt.Orientation.Horizontal, root_widget)
        self.splitter.setObjectName("mainSplitter")
        self.splitter.setChildrenCollapsible(False)
        self.splitter.addWidget(self.case_list)
        self.video_workspace = QWidget(self.splitter)
        self.video_workspace.setObjectName("videoWorkspace")
        video_layout = QVBoxLayout(self.video_workspace)
        video_layout.setContentsMargins(0, 0, 0, 0)
        video_layout.setSpacing(1)
        video_layout.addWidget(self.video_player, 1)
        video_layout.addWidget(self.playback, 0)
        self.splitter.addWidget(self.video_workspace)
        self.splitter.addWidget(self.investigation)
        self.splitter.setStretchFactor(0, 0)
        self.splitter.setStretchFactor(1, 5)
        self.splitter.setStretchFactor(2, 3)
        self.splitter.setSizes([275, 790, 510])
        root.addWidget(self.splitter, 1)
        self.setCentralWidget(root_widget)
        self._update_workspace_status()

    def _build_menu(self) -> None:
        file_menu = self.menuBar().addMenu("파일")
        workspace_action = QAction(material_icon("folder_open", PRIMARY), "작업 폴더 열기…", self)
        workspace_action.setShortcut(QKeySequence("Ctrl+O"))
        workspace_action.triggered.connect(self.choose_workspace)
        file_menu.addAction(workspace_action)

        new_action = QAction(material_icon("add", PRIMARY), "새 사고", self)
        new_action.setShortcut(QKeySequence("Ctrl+N"))
        new_action.triggered.connect(self.create_case)
        file_menu.addAction(new_action)

        video_action = QAction(material_icon("video_file", PRIMARY), "현재 사고에 영상 추가…", self)
        video_action.setShortcut(QKeySequence("Ctrl+Shift+O"))
        video_action.triggered.connect(self.choose_videos)
        file_menu.addAction(video_action)
        file_menu.addSeparator()

        save_action = QAction(material_icon("save", PRIMARY), "임시저장", self)
        save_action.setShortcut(QKeySequence.StandardKey.Save)
        save_action.triggered.connect(self.save_draft)
        file_menu.addAction(save_action)
        export_action = QAction(material_icon("download", PRIMARY), "CSV 내보내기…", self)
        export_action.setShortcut(QKeySequence("Ctrl+E"))
        export_action.triggered.connect(self.export_csv)
        file_menu.addAction(export_action)
        file_menu.addSeparator()
        exit_action = QAction("종료", self)
        exit_action.setShortcut(QKeySequence.StandardKey.Quit)
        exit_action.triggered.connect(self.close)
        file_menu.addAction(exit_action)

        help_menu = self.menuBar().addMenu("도움말")
        shortcuts_action = QAction("재생 단축키", self)
        shortcuts_action.triggered.connect(self._show_shortcuts)
        help_menu.addAction(shortcuts_action)

    def _connect_signals(self) -> None:
        self.case_list.case_selected.connect(self.select_case)
        self.case_list.video_selected.connect(self.select_video)
        self.case_list.new_case_requested.connect(self.create_case)
        self.case_list.add_video_requested.connect(self.choose_videos)
        self.case_list.export_requested.connect(self.export_csv)
        self.video_player.files_dropped.connect(self.attach_videos)

        self.investigation.value_changed.connect(self._field_changed)
        self.playback.seek_requested.connect(self._seek_video)
        self.playback.relative_frames_requested.connect(self._step_frames)
        self.playback.relative_seconds_requested.connect(self._step_seconds)
        self.playback.toggle_playback_requested.connect(
            lambda: self.video_controller.toggle_playback()
        )
        self.playback.trigger_requested.connect(self.set_trigger_marker)
        self.playback.collision_requested.connect(self.set_collision_marker)
        self.investigation.save_requested.connect(self.save_draft)
        self.investigation.complete_requested.connect(self.complete_case)
        self.investigation.next_requested.connect(self.next_case)

        self.video_controller.video_opened.connect(self._video_opened)
        self.video_controller.video_closed.connect(self._video_closed)
        self.video_controller.frame_ready.connect(self._frame_ready)
        self.video_controller.position_changed.connect(
            lambda frame, total: self.playback.set_position(frame)
        )
        self.video_controller.total_frames_changed.connect(self.playback.update_total_frames)
        self.video_controller.state_changed.connect(self._playback_state_changed)
        self.video_controller.error_occurred.connect(self._video_error)

    def _load_workspace(self) -> None:
        self._workspace_load_failed = False
        try:
            active_uid = self.repository.active_case_uid()
            self.cases = self.repository.load()
        except WorkspaceError as exc:
            QMessageBox.critical(self, "작업 폴더 오류", str(exc))
            self._workspace_load_failed = True
            self.cases = []
            self.current_uid = None
            self.case_list.set_cases([])
            self.video_controller.close_video()
            self.video_player.clear()
            self.playback.clear_video()
            self.investigation.load_values({})
            self.investigation.case_title.setText("작업 폴더 읽기 오류")
            self.investigation.completion_badge.setText("읽기 오류")
            self._set_workflow_enabled(False)
            self.statusBar().showMessage(
                "원본 데이터는 변경하지 않았습니다. 다른 작업 폴더를 선택해 주세요.", 0
            )
            return
        if not self.cases:
            self.cases = [new_case()]
            active_uid = self.cases[0].uid
            self._save(show_message=False)
        if active_uid not in {case.uid for case in self.cases}:
            active_uid = self.cases[0].uid
        self.case_list.set_cases(self.cases, active_uid)
        self.select_case(str(active_uid))
        self._set_workflow_enabled(True)
        self._update_workspace_status()

    def _set_workflow_enabled(self, enabled: bool) -> None:
        self.case_list.new_button.setEnabled(enabled)
        self.case_list.video_button.setEnabled(enabled)
        self.case_list.export_button.setEnabled(enabled)
        self.investigation.setEnabled(enabled)
        self.investigation.save_button.setEnabled(enabled)
        self.investigation.complete_button.setEnabled(
            enabled and self.current_case is not None and self.current_case.status != "completed"
        )
        self.investigation.next_button.setEnabled(enabled)

    def choose_workspace(self) -> None:
        selected = QFileDialog.getExistingDirectory(
            self,
            "조사 작업 폴더 선택",
            str(self.repository.workspace),
            QFileDialog.Option.ShowDirsOnly,
        )
        if not selected:
            return
        if not self._workspace_load_failed and not self._save(show_message=False):
            return
        self.video_controller.close_video()
        self.repository = CaseRepository(selected)
        self.cases = []
        self.current_uid = None
        self._load_workspace()
        if not self._workspace_load_failed:
            self.statusBar().showMessage("작업 폴더를 열었습니다.", 3000)

    def create_case(self) -> None:
        if not self._save(show_message=False):
            return
        case = new_case(self.cases)
        self.cases.append(case)
        self.current_uid = case.uid
        self._dirty = True
        self.case_list.set_cases(self.cases, case.uid)
        self._show_current_case(open_video=True)
        if not self._save(show_message=False):
            return
        self.statusBar().showMessage(f"{case.display_id} 사고를 만들었습니다.", 3000)

    def choose_videos(self) -> None:
        filters = "영상 파일 (*.mp4 *.avi *.mov *.mkv *.m4v *.wmv *.webm);;모든 파일 (*.*)"
        files, _ = QFileDialog.getOpenFileNames(self, "조사 영상 선택", QDir.homePath(), filters)
        if files:
            self.attach_videos(files)

    def attach_videos(self, files: list[str]) -> None:
        if self._workspace_load_failed:
            self.statusBar().showMessage("읽기 오류가 없는 작업 폴더를 먼저 선택해 주세요.", 5000)
            return
        valid = [
            path
            for path in files
            if Path(path).suffix.casefold() in VIDEO_EXTENSIONS and Path(path).is_file()
        ]
        if not valid:
            QMessageBox.warning(self, "영상 추가", "지원되는 영상 파일을 선택해 주세요.")
            return
        case = self.current_case
        if case is None:
            case = new_case(self.cases)
            self.cases.append(case)
            self.current_uid = case.uid
        before = len(case.videos)
        case.attach_videos(valid)
        if before == 0 and case.videos:
            case.active_video_index = 0
        self._dirty = True
        self.case_list.set_cases(self.cases, case.uid)
        if not self._open_active_video():
            self.video_controller.close_video()
            self.video_player.clear()
            self.playback.clear_video()
        if not self._save(show_message=False):
            return
        self.statusBar().showMessage(f"영상 {len(case.videos) - before}개를 추가했습니다.", 3000)

    def select_case(self, uid: str) -> None:
        if uid == self.current_uid and self.current_case is not None:
            return
        if self.current_uid and not self._save(show_message=False):
            return
        self.current_uid = uid
        self._show_current_case(open_video=True)

    def select_video(self, uid: str, index: int) -> None:
        target_case = next((case for case in self.cases if case.uid == uid), None)
        if target_case is None or not (0 <= index < len(target_case.videos)):
            return
        if uid != self.current_uid and self.current_uid and not self._save(show_message=False):
            return
        previous_uid = self.current_uid
        previous_index = target_case.active_video_index
        target_case.active_video_index = index
        if not self._open_active_video(case=target_case, preserve_on_failure=True):
            target_case.active_video_index = previous_index
            self.current_uid = previous_uid
            self.case_list.set_cases(self.cases, previous_uid)
            previous_case = self.current_case
            if previous_case:
                self.playback.set_markers(
                    previous_case.trigger_frame, previous_case.collision_frame
                )
            return
        self.current_uid = uid
        self._dirty = True
        self.case_list.set_cases(self.cases, uid)
        self._show_current_case(open_video=False)

    def _show_current_case(self, *, open_video: bool) -> None:
        case = self.current_case
        if case is None:
            self.video_controller.close_video()
            self.video_player.clear()
            self.playback.clear_video()
            return
        self.investigation.load_values(case.values)
        self._refresh_case_presentation(rebuild_list=False)
        self.playback.set_markers(case.trigger_frame, case.collision_frame)
        if open_video and not self._open_active_video():
            self.video_controller.close_video()
            self.video_player.clear()
            self.playback.clear_video()

    def _open_active_video(
        self,
        *,
        case: InvestigationCase | None = None,
        preserve_on_failure: bool = False,
    ) -> bool:
        case = case or self.current_case
        path = case.active_video if case else None
        if not path:
            self.video_controller.close_video()
            self.video_player.clear()
            self.playback.clear_video()
            return True
        if not Path(path).is_file():
            if not preserve_on_failure:
                self.video_player.set_video_name(path)
            self.statusBar().showMessage(f"영상 파일을 찾을 수 없습니다: {path}", 6000)
            return False
        return self.video_controller.open_video(path)

    def _field_changed(self, key: str, value: Any) -> None:
        case = self.current_case
        if case is None:
            return
        case.set_value(key, value)
        if key == "obstacle_exists" and value == "무":
            for detail_key in (
                "obstacle_vehicle_types",
                "obstacle_vehicle_state",
                "obstacle_pedestrians",
                "obstacle_road_facilities",
                "obstacle_building_landscape",
                "obstacle_other",
                "obstacle_state",
                "obstacle_dist_source",
                "obstacle_dist_value",
                "obstacle_dist_range",
            ):
                case.values.pop(detail_key, None)
        if key == "incident_damage_unknown" and value == "확인불가":
            for count_key in (
                "incident_minor_injury",
                "incident_severe_injury",
                "incident_fatality",
                "incident_property_damage",
            ):
                case.values.pop(count_key, None)
        self._dirty = True
        self._refresh_case_presentation(rebuild_list=True)

    def _refresh_case_presentation(self, *, rebuild_list: bool) -> None:
        case = self.current_case
        if case is None:
            return
        missing = case.missing
        self.investigation.show_case(case.display_id, missing, case.status == "completed")
        self.investigation.complete_button.setEnabled(case.status != "completed")
        if rebuild_list:
            self.case_list.set_cases(self.cases, case.uid)

    def set_trigger_marker(self) -> None:
        case = self.current_case
        if case is None or not self.video_controller.is_open:
            return
        frame = self.video_controller.current_frame
        removing = case.trigger_frame == frame
        case.set_trigger(None if removing else frame)
        self._dirty = True
        self.playback.set_markers(case.trigger_frame, case.collision_frame)
        self._refresh_case_presentation(rebuild_list=True)
        if removing:
            self.statusBar().showMessage("T 트리거 마커를 해제했습니다.", 3000)
        else:
            self.statusBar().showMessage(f"T 트리거를 {frame + 1:,}번 프레임에 지정했습니다.", 3000)

    def set_collision_marker(self) -> None:
        case = self.current_case
        if case is None or not self.video_controller.is_open:
            return
        frame = self.video_controller.current_frame
        removing = case.collision_frame == frame
        case.set_collision(None if removing else frame)
        self._dirty = True
        self.playback.set_markers(case.trigger_frame, case.collision_frame)
        self._refresh_case_presentation(rebuild_list=True)
        if removing:
            self.statusBar().showMessage("충돌 마커를 해제했습니다.", 3000)
        else:
            self.statusBar().showMessage(
                f"충돌 시점을 {frame + 1:,}번 프레임에 지정했습니다.", 3000
            )

    def save_draft(self) -> None:
        self._save(show_message=True)

    def _save(self, *, show_message: bool) -> bool:
        if self._workspace_load_failed:
            self.statusBar().showMessage("읽기 오류가 없는 작업 폴더를 먼저 선택해 주세요.", 5000)
            return False
        try:
            self.repository.save(self.cases, active_case_uid=self.current_uid)
        except WorkspaceError as exc:
            QMessageBox.critical(self, "저장 오류", str(exc))
            return False
        self._dirty = False
        if show_message:
            self.statusBar().showMessage("조사 내용을 임시저장했습니다.", 3000)
        return True

    def complete_case(self) -> None:
        case = self.current_case
        if case is None:
            return
        missing = case.missing
        errors = case.errors
        identity = (
            str(case.values.get("institution", "")).strip().casefold(),
            str(case.values.get("accident_classification", "")).strip(),
        )
        if all(identity) and any(
            other.uid != case.uid
            and (
                str(other.values.get("institution", "")).strip().casefold(),
                str(other.values.get("accident_classification", "")).strip(),
            )
            == identity
            for other in self.cases
        ):
            errors["accident_classification"] = INVALID_FORMAT_MESSAGE
        editor_key_by_issue = {
            "site_location": "site_types",
            "obstacle_object_types": "obstacle_vehicle_types",
        }
        field_errors: dict[str, str] = {}
        for key in missing:
            editor_key = editor_key_by_issue.get(key, key)
            if editor_key in self.investigation.editors:
                field_errors[editor_key] = MISSING_VALUE_MESSAGE
        for key, message in errors.items():
            editor_key = editor_key_by_issue.get(key, key)
            if editor_key in self.investigation.editors:
                field_errors[editor_key] = message
        self.investigation.show_validation(field_errors)
        if missing or errors:
            messages = [f"• {field_label(key)}: {MISSING_VALUE_MESSAGE}" for key in missing]
            messages.extend(f"• {field_label(key)}: {message}" for key, message in errors.items())
            summary = "\n".join(messages[:8])
            if len(messages) > 8:
                summary += f"\n• 그 외 {len(messages) - 8}개"
            title = MISSING_VALUE_MESSAGE if missing else INVALID_FORMAT_MESSAGE
            QMessageBox.warning(self, title, f"{title}.\n\n{summary}")
            first_key = next(iter(missing or errors))
            self.investigation.focus_field(first_key)
            return
        # Capture the draft workflow before completing so a failed disk write can
        # never leave an in-memory case looking completed.
        draft_workflow = (case.status, case.completed_at, case.updated_at)
        try:
            case.complete()
        except ValueError as exc:
            QMessageBox.warning(self, MISSING_VALUE_MESSAGE, str(exc))
            return
        self._dirty = True
        if not self._save(show_message=False):
            case.status, case.completed_at, case.updated_at = draft_workflow
            self._dirty = True
            self._refresh_case_presentation(rebuild_list=True)
            return
        self._refresh_case_presentation(rebuild_list=True)
        self.statusBar().showMessage(f"{case.display_id} 조사를 완료했습니다.", 4000)

    def next_case(self) -> None:
        if not self._save(show_message=False) or not self.cases:
            return
        current_index = next(
            (index for index, case in enumerate(self.cases) if case.uid == self.current_uid), 0
        )
        ordered = self.cases[current_index + 1 :] + self.cases[: current_index + 1]
        target = next(
            (
                case
                for case in ordered
                if case.uid != self.current_uid and case.status != "completed"
            ),
            None,
        )
        if target is None:
            target = next((case for case in ordered if case.uid != self.current_uid), None)
        if target is None:
            self.statusBar().showMessage("다음 사고가 없습니다.", 3000)
            return
        self.current_uid = target.uid
        self.case_list.select_case(target.uid)
        self._show_current_case(open_video=True)

    def export_csv(self) -> None:
        if not self._save(show_message=False):
            return
        suggested = self.repository.workspace / f"investigation_{datetime.now():%Y%m%d}.csv"
        path, _ = QFileDialog.getSaveFileName(
            self, "조사 결과 CSV 내보내기", str(suggested), "CSV 파일 (*.csv)"
        )
        if not path:
            return
        if not Path(path).suffix:
            path += ".csv"
        try:
            destination = self.repository.export_csv(path, self.cases)
        except WorkspaceError as exc:
            QMessageBox.critical(self, "CSV 내보내기 오류", str(exc))
            return
        self.statusBar().showMessage(f"CSV를 저장했습니다: {destination}", 6000)

    def _video_opened(self, metadata: VideoMetadata) -> None:
        self.video_player.set_video_name(metadata.path, (metadata.width, metadata.height))
        self.playback.set_video(metadata.total_frames, metadata.fps)
        case = self.current_case
        if case:
            self.playback.set_markers(case.trigger_frame, case.collision_frame)
        self.statusBar().showMessage(
            f"영상 열기 완료 · {metadata.fps:.2f} FPS · {metadata.total_frames:,} frames", 4000
        )

    def _video_closed(self) -> None:
        self.video_player.canvas.clear()
        self.playback.clear_video()

    def _frame_ready(self, image, frame: int, timestamp: float) -> None:  # type: ignore[no-untyped-def]
        del frame, timestamp
        self.video_player.canvas.set_frame(image)

    def _playback_state_changed(self, state: PlaybackState) -> None:
        self.playback.set_playing(state is PlaybackState.PLAYING)

    def _video_error(self, message: str) -> None:
        self.statusBar().showMessage(message, 7000)

    def _seek_video(self, frame: int) -> None:
        if self.video_controller.is_playing:
            self.video_controller.pause()
        self.video_controller.seek(frame)

    def _step_frames(self, frames: int) -> None:
        if self.video_controller.is_playing:
            self.video_controller.pause()
        self.video_controller.seek_relative(frames)

    def _step_seconds(self, seconds: float) -> None:
        if self.video_controller.is_playing:
            self.video_controller.pause()
        self.video_controller.seek_seconds(seconds)

    def _update_workspace_status(self) -> None:
        self.statusBar().setToolTip(str(self.repository.workspace))
        self.statusBar().showMessage(f"작업 폴더 · {self.repository.workspace}", 4000)

    def _show_shortcuts(self) -> None:
        QMessageBox.information(
            self,
            "재생 단축키",
            "Space  재생 / 일시정지\n← / →  1프레임 이동\nCtrl+← / Ctrl+→  10프레임 이동\nJ / L  5초 이동\nCtrl+S  임시저장",
        )

    def keyPressEvent(self, event: QKeyEvent) -> None:
        focus = QApplication.focusWidget()
        if isinstance(focus, (QLineEdit, QComboBox, QSpinBox)):
            super().keyPressEvent(event)
            return
        key = event.key()
        modifiers = event.modifiers()
        if key == Qt.Key.Key_Space:
            self.video_controller.toggle_playback()
        elif key == Qt.Key.Key_Left:
            self._step_frames(-10 if modifiers & Qt.KeyboardModifier.ControlModifier else -1)
        elif key == Qt.Key.Key_Right:
            self._step_frames(10 if modifiers & Qt.KeyboardModifier.ControlModifier else 1)
        elif key == Qt.Key.Key_J:
            self._step_seconds(-5)
        elif key == Qt.Key.Key_L:
            self._step_seconds(5)
        else:
            super().keyPressEvent(event)
            return
        event.accept()

    def closeEvent(self, event: QCloseEvent) -> None:
        if not self._workspace_load_failed and not self._save(show_message=False):
            event.ignore()
            return
        self.video_controller.close_video()
        event.accept()


__all__ = ["MainWindow", "default_workspace"]
