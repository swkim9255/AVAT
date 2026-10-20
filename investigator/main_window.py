"""Top-level three-column investigation workflow."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from threading import Event
from typing import Any

from PySide6.QtCore import QDir, QSize, QStandardPaths, Qt, QTimer
from PySide6.QtGui import QAction, QCloseEvent, QKeyEvent, QKeySequence
from PySide6.QtWidgets import (
    QAbstractSpinBox,
    QApplication,
    QComboBox,
    QDialog,
    QFileDialog,
    QFrame,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QScrollArea,
    QSplitter,
    QStackedWidget,
    QTabBar,
    QTextEdit,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from investigator.diagnostics import timed_operation
from investigator.folder_watcher import ProjectFolderWatcher
from investigator.form_definition import FormDefinition
from investigator.models import DEFAULT_INSTITUTION, InvestigationCase, new_case
from investigator.project_session import ProjectSession
from investigator.project_tabs_state import ProjectTabsState, ProjectTabsStore
from investigator.report_export import ReportExportError, export_case_reports
from investigator.schema import (
    INVALID_FORMAT_MESSAGE,
    MISSING_VALUE_MESSAGE,
)
from investigator.speed_estimation import SpeedEstimate, estimate_speed
from investigator.statistics_media import probe_video_fps
from investigator.storage import CaseRepository, WorkspaceError, data_specification_path
from investigator.storage_worker import StorageWorker
from investigator.theme import MUTED, PRIMARY, material_icon
from investigator.video_controller import PlaybackState, VideoController, VideoMetadata
from investigator.video_copy import CopiedVideos, copy_videos
from investigator.video_import import (
    VIDEO_EXTENSIONS,
    VideoFolderScan,
    parse_accident_video_name,
    scan_video_folder,
)
from investigator.widgets.case_list import CaseListPanel
from investigator.widgets.csv_export_dialog import CsvExportDialog
from investigator.widgets.form_designer import FormDesigner
from investigator.widgets.form_panel import InvestigationPanel
from investigator.widgets.playback_panel import PlaybackPanel
from investigator.widgets.report_export_dialog import ReportExportDialog
from investigator.widgets.speed_panel import SpeedPanel
from investigator.widgets.statistics_panel import StatisticsPanel
from investigator.widgets.video_import_dialog import VideoImportDialog
from investigator.widgets.video_player import VideoPlayerPanel
from investigator.widgets.workspace_menu import ProjectTabBar, ViewModeSwitch, WorkspaceMenuBar


def default_workspace() -> Path:
    documents = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.DocumentsLocation)
    base = Path(documents) if documents else Path.cwd()
    return base / "사고영상조사"


class MainWindow(QMainWindow):
    def __init__(self, workspace: str | Path | None = None, parent: QWidget | None = None,
                 *, project_tabs_store: ProjectTabsStore | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("mainWindow")
        self.setWindowTitle("AVAT")
        self.setWindowIcon(material_icon("car_crash", PRIMARY))
        self.setMinimumSize(1180, 760)
        self.resize(1660, 940)

        self._project_tabs_store = project_tabs_store or ProjectTabsStore()
        self._project_tabs_warning = ""
        try:
            tabs_state = self._project_tabs_store.load()
        except (OSError, ValueError, RuntimeError):
            tabs_state = ProjectTabsState()
            self._project_tabs_warning = "저장된 프로젝트 탭 설정을 읽지 못했습니다. 프로젝트 폴더를 다시 열어 주세요."
        self._projects = [ProjectSession(CaseRepository(path), restored=True) for path in tabs_state.folders]
        selected = Path(workspace).expanduser().resolve() if workspace is not None else tabs_state.active_folder
        if selected is None:
            selected = default_workspace().resolve()
        active_index = next((index for index, project in enumerate(self._projects)
                             if project.repository.workspace == selected), None)
        if active_index is None:
            self._projects.append(ProjectSession(CaseRepository(selected)))
            active_index = len(self._projects) - 1
        self._active_project_index = active_index
        self.repository = self._projects[active_index].repository
        self.cases: list[InvestigationCase] = []
        self.current_uid: str | None = None
        self._opening_case: InvestigationCase | None = None
        self._dirty = False
        self._dirty_cases: set[str] = set()
        self._case_revisions: dict[str, int] = {}
        self._order_dirty = False
        self._saved_active_uid: str | None = None
        self._persisted_uids: set[str] = set()
        self._storage_worker = StorageWorker(self)
        self._commit_in_progress = False
        self._deferred_reorders: list[tuple[list[str], int]] = []
        self._workspace_load_failed = False
        self._switching_project = False
        self._video_import_in_progress = False
        self._project_tabs_timer = QTimer(self)
        self._project_tabs_timer.setSingleShot(True)
        self._project_tabs_timer.setInterval(250)
        self._project_tabs_timer.timeout.connect(self._save_project_tabs)
        self._auto_import_failed = False
        self._statistics_dirty = True
        self._statistics_timer = QTimer(self)
        self._statistics_timer.setSingleShot(True)
        self._statistics_timer.setInterval(300)
        self._statistics_timer.timeout.connect(self._refresh_statistics)
        self._statistics_media_worker = StorageWorker(self)
        self._statistics_media_cache: dict[str, tuple[int, int, float]] = {}
        self._statistics_media_attempted: set[str] = set()
        self._statistics_media_future = None
        self._statistics_media_cancel = Event()
        self._statistics_media_origin: Path | None = None
        self._statistics_media_timer = QTimer(self)
        self._statistics_media_timer.setInterval(50)
        self._statistics_media_timer.timeout.connect(self._finish_statistics_media)
        self.folder_watcher = ProjectFolderWatcher(self)
        self._watch_refresh_timer = QTimer(self)
        self._watch_refresh_timer.setSingleShot(True)
        self._watch_refresh_timer.timeout.connect(self._refresh_watched_videos)

        self.video_controller = VideoController(self)
        self.case_list = CaseListPanel(self)
        self.video_player = VideoPlayerPanel(self)
        self.investigation = InvestigationPanel(self)
        self.playback = PlaybackPanel(self)
        self.speed_panel = SpeedPanel(self)
        self._build_layout()
        self._build_menu()
        self._connect_signals()
        self._load_workspace()
        self._sync_project_tabs()
        project = self._projects[self._active_project_index]
        project.initialized = not self._workspace_load_failed
        project.load_failed = self._workspace_load_failed
        self._watch_active_project()
        if self._project_tabs_warning:
            self.statusBar().showMessage(self._project_tabs_warning, 12000)
        self._project_tabs_timer.start()

    @property
    def current_case(self) -> InvestigationCase | None:
        return next((case for case in self.cases if case.uid == self.current_uid), None)

    def _disk_operation(self, operation, *args, allow_edit=False, **kwargs):  # type: ignore[no-untyped-def]
        """Run commits off-thread; keep GUI timers/painting alive until committed."""
        self._commit_in_progress = True
        disabled = []
        for control in [self.case_list.report_export_button, self.report_export_action]:
            if control.isEnabled():
                control.setEnabled(False)
                disabled.append(control)
        if allow_edit:
            # Keep fields editable during an explicit draft save, but prevent
            # structural/navigation commands from reentering the transaction.
            for control in [
                self.case_list, self.video_player, self.investigation.save_button,
                self.project_tabs, self.project_add_button,
                self.mode_tabs,
                self.investigation.complete_button, self.investigation.next_button,
                self.investigation.edit_form_button,
                *self.findChildren(QAction),
            ]:
                if control.isEnabled():
                    control.setEnabled(False)
                    disabled.append(control)
        try:
            with timed_operation("storage_operation", count=len(args[0]) if args and isinstance(args[0], list) else None):
                return self._storage_worker.run(operation, *args, allow_input=allow_edit, **kwargs)
        finally:
            for control in disabled:
                control.setEnabled(True)
            self._commit_in_progress = False
            if allow_edit:
                case = self.current_case
                self.investigation.complete_button.setEnabled(
                    case is not None and case.status != "completed"
                )
            if self._deferred_reorders:
                QTimer.singleShot(0, self._flush_deferred_reorders)
            self._schedule_watch_refresh()

    def _flush_deferred_reorders(self) -> None:
        if self._commit_in_progress:
            return
        pending, self._deferred_reorders = self._deferred_reorders, []
        for uids, destination in pending:
            self.reorder_cases(uids, destination)

    def _copy_case(self, case: InvestigationCase) -> InvestigationCase:
        snapshot = InvestigationCase.from_dict(
            case.to_dict(), form_definition=self.repository.form_definition
        )
        snapshot.defaults_applied = case.defaults_applied
        return snapshot

    def _mark_case_dirty(self, case: InvestigationCase) -> None:
        self._dirty_cases.add(case.uid)
        self._case_revisions[case.uid] = self._case_revisions.get(case.uid, 0) + 1
        self._dirty = True
        self._invalidate_statistics()

    def _mark_saved(self, active_uid: str | None) -> None:
        self._dirty = False
        self._dirty_cases.clear()
        self._order_dirty = False
        self._saved_active_uid = active_uid
        self._persisted_uids = {case.uid for case in self.cases}
        if hasattr(self, "_projects"):
            project = self._projects[self._active_project_index]
            project.cases = self.cases
            project.current_uid = active_uid
        self._invalidate_statistics()

    def _save_candidates(
        self, candidates: list[InvestigationCase], active_uid: str | None,
        *, auto_import_exclusions: set[str] | None = None,
    ) -> None:
        """Persist only detached/new/edited cases and structural metadata."""
        previous = {case.uid: case for case in self.cases}
        candidate_uids = {case.uid for case in candidates}
        changes = [
            case for case in candidates
            if previous.get(case.uid) is not case or case.uid in self._dirty_cases
        ]
        snapshots = [self._copy_case(case) for case in changes]
        self._disk_operation(
            self.repository.save_changes, snapshots,
            deleted_uids=list(previous.keys() - candidate_uids),
            order=[case.uid for case in candidates], active_case_uid=active_uid,
            auto_import_exclusions=auto_import_exclusions,
        )
        # Configured defaults are applied only to genuinely new cases.
        saved_by_uid = {case.uid: case for case in snapshots}
        for index, case in enumerate(candidates):
            if case.uid in saved_by_uid:
                candidates[index] = saved_by_uid[case.uid]

    def _build_layout(self) -> None:
        self.workspace_menu = WorkspaceMenuBar(self)
        self.setMenuBar(self.workspace_menu)
        root_widget = QWidget(self)
        root_widget.setObjectName("appRoot")
        root = QVBoxLayout(root_widget)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(1)
        self.project_tabs = ProjectTabBar(root_widget)
        self.project_tabs.setObjectName("projectTabs")
        self.project_tabs.setAccessibleName("프로젝트 폴더 탭")
        self.project_tabs.setExpanding(False)
        self.project_tabs.setUsesScrollButtons(True)
        self.project_tabs.setElideMode(Qt.TextElideMode.ElideMiddle)
        self.project_tabs.setTabsClosable(True)
        self.project_tabs.setMovable(True)
        self.project_tabs.setAccessibleDescription("탭을 좌우로 드래그하여 프로젝트 폴더의 표시 순서를 변경합니다.")
        for project in self._projects:
            path = project.repository.workspace
            self.project_tabs.addTab(path.name or str(path))
        self.project_tabs.setCurrentIndex(self._active_project_index)
        self.project_add_button = QToolButton(root_widget)
        self.project_add_button.setIcon(material_icon("add", PRIMARY))
        self.project_add_button.setAccessibleName("프로젝트 폴더 열기")
        self.project_add_button.setToolTip("프로젝트 폴더 열기 · 현재 탭 / 새 탭")
        self.project_add_button.setFixedSize(34, 34)
        self.project_add_button.setStyleSheet("padding: 0;")
        self.view_mode = ViewModeSwitch(root_widget)
        self.mode_tabs = self.view_mode  # Preserve index-based view routing.
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
        self.speed_scroll = QScrollArea(self.video_workspace)
        self.speed_scroll.setObjectName("speedScroll")
        self.speed_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.speed_scroll.setWidgetResizable(True)
        self.speed_scroll.setMinimumWidth(0)
        self.speed_scroll.setMinimumHeight(180)
        self.speed_scroll.setMaximumHeight(340)
        self.speed_scroll.setWidget(self.speed_panel)
        self.speed_scroll.hide()
        video_layout.addWidget(self.speed_scroll, 0)
        self.splitter.addWidget(self.video_workspace)
        self.splitter.addWidget(self.investigation)
        self.splitter.setStretchFactor(0, 0)
        self.splitter.setStretchFactor(1, 5)
        self.splitter.setStretchFactor(2, 3)
        self.splitter.setSizes([275, 790, 510])
        self.workspace_pages = QStackedWidget(root_widget)
        self.workspace_pages.addWidget(self.splitter)
        self.statistics_panel = StatisticsPanel(self)
        self.workspace_pages.addWidget(self.statistics_panel)
        root.addWidget(self.workspace_pages, 1)
        self.setCentralWidget(root_widget)
        self._update_workspace_status()

    def _build_menu(self) -> None:
        file_menu = self.menuBar().addMenu("파일")
        workspace_action = QAction(material_icon("folder_open", PRIMARY), "프로젝트 폴더 열기…", self)
        workspace_action.setShortcut(QKeySequence("Ctrl+O"))
        workspace_action.triggered.connect(self.choose_workspace)
        file_menu.addAction(workspace_action)

        file_menu.addSeparator()

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
        self.report_export_action = QAction(
            material_icon("description", PRIMARY), "사고 분석 보고서 내보내기…", self
        )
        self.report_export_action.triggered.connect(self.export_reports)
        file_menu.addAction(self.report_export_action)
        backup_action = QAction("조사 데이터 백업…", self)
        backup_action.triggered.connect(self.backup_workspace)
        file_menu.addAction(backup_action)
        file_menu.addSeparator()
        exit_action = QAction("종료", self)
        exit_action.setShortcut(QKeySequence.StandardKey.Quit)
        exit_action.triggered.connect(self.close)
        file_menu.addAction(exit_action)

        settings_menu = self.menuBar().addMenu("설정")
        self.edit_form_action = QAction("조사 항목 편집…", self)
        self.edit_form_action.triggered.connect(self.edit_form)
        settings_menu.addAction(self.edit_form_action)

        help_menu = self.menuBar().addMenu("도움말")
        shortcuts_action = QAction("재생 단축키", self)
        shortcuts_action.triggered.connect(self._show_shortcuts)
        help_menu.addAction(shortcuts_action)
        self.workspace_menu.set_controls(self.project_tabs, self.project_add_button, self.view_mode)

    def _connect_signals(self) -> None:
        self.mode_tabs.currentChanged.connect(self._change_workspace_view)
        self.statistics_panel.case_activated.connect(self._open_statistics_case)
        self.statistics_panel.media_requested.connect(self._request_statistics_media)
        self.folder_watcher.videos_ready.connect(self._watched_videos_ready)
        self.folder_watcher.watch_error.connect(lambda message: self.statusBar().showMessage(message, 5000))
        self.project_tabs.tabMoved.connect(self._move_project_tab)
        self.project_tabs.currentChanged.connect(self.switch_project)
        self.project_tabs.tabCloseRequested.connect(self.close_project)
        self.project_add_button.clicked.connect(self.choose_workspace)
        self.case_list.case_selected.connect(self.select_case)
        self.case_list.video_selected.connect(self.select_video)
        self.case_list.new_case_requested.connect(self.create_case)
        self.case_list.add_video_requested.connect(self.choose_videos)
        self.case_list.delete_case_requested.connect(self.delete_case)
        self.case_list.delete_video_requested.connect(self.delete_video)
        self.case_list.case_reordered.connect(self.reorder_case)
        self.case_list.cases_reordered.connect(self.reorder_cases)
        self.case_list.export_requested.connect(self.export_csv)
        self.case_list.report_export_requested.connect(self.export_reports)
        self.video_player.files_dropped.connect(self._import_dropped_videos)
        self.video_player.rotation_requested.connect(self.rotate_video_clockwise)

        self.investigation.value_changed.connect(self._field_changed)
        self.playback.seek_requested.connect(self._seek_video)
        self.playback.relative_frames_requested.connect(self._step_frames)
        self.playback.relative_seconds_requested.connect(self._step_seconds)
        self.playback.toggle_playback_requested.connect(
            lambda: self.video_controller.toggle_playback()
        )
        self.playback.trigger_requested.connect(self.set_trigger_marker)
        self.playback.collision_requested.connect(self.set_collision_marker)
        self.playback.speed_point_requested.connect(self.toggle_speed_point)
        self.playback.speed_panel_toggled.connect(self._set_speed_panel_visible)
        self.speed_panel.add_point_requested.connect(self.toggle_speed_point)
        self.speed_panel.remove_point_requested.connect(self.remove_speed_point)
        self.speed_panel.clear_requested.connect(self.clear_speed_points)
        self.speed_panel.distance_changed.connect(self.set_speed_distance)
        self.speed_panel.seek_requested.connect(self._seek_video)
        self.speed_panel.apply_requested.connect(self.apply_speed_label)
        self.investigation.save_requested.connect(self.save_draft)
        self.investigation.complete_requested.connect(self.complete_case)
        self.investigation.next_requested.connect(self.next_case)
        self.investigation.edit_form_requested.connect(self.edit_form)

        self.video_controller.video_opened.connect(self._video_opened)
        self.video_controller.video_closed.connect(self._video_closed)
        self.video_controller.frame_ready.connect(self._frame_ready)
        self.video_controller.position_changed.connect(
            lambda frame, total: self.playback.set_position(frame)
        )
        self.video_controller.seek_requested_position.connect(self.playback.set_requested_position)
        self.video_controller.seek_cancelled.connect(self.playback.clear_pending_seek)
        self.video_controller.total_frames_changed.connect(self.playback.update_total_frames)
        self.video_controller.state_changed.connect(self._playback_state_changed)
        self.video_controller.error_occurred.connect(self._video_error)

    def _load_workspace(self) -> bool:
        """Load persisted cases, then silently discover videos beside the database."""

        self._workspace_load_failed = False
        try:
            project = self._projects[self._active_project_index]
            self.cases = (
                self._disk_operation(self.repository.load, require_existing=True) if project.restored
                else self._disk_operation(self.repository.load)
            )
            active_uid = self.repository.active_case_uid()
            self._mark_saved(active_uid)
            self._install_form(self.repository.form_definition)
        except WorkspaceError as exc:
            if not self._projects[self._active_project_index].restored:
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
                f"{exc}\n원본 데이터는 변경하지 않았습니다. 다른 탭을 선택하거나 프로젝트 폴더를 다시 열어 주세요.", 0
            )
            return False
        if active_uid not in {case.uid for case in self.cases}:
            active_uid = self.cases[0].uid if self.cases else None
        self.current_uid = None
        self.case_list.set_cases(self.cases, active_uid, selected_video_index=None)
        if active_uid is None:
            self._show_current_case(open_video=False)
        else:
            self.select_case(str(active_uid))
        self._set_workflow_enabled(True)
        # Clear the previous project's transient status before an import summary.
        self._update_workspace_status()
        return self.import_video_folder(
            self.repository.workspace,
            quiet_when_empty=True,
            respect_exclusions=True,
        )

    def _install_form(self, definition: FormDefinition) -> None:
        old = self.investigation
        tab_index = old.tabs.currentIndex()
        selected_tab_key = (
            old.sections[tab_index].key if 0 <= tab_index < len(old.sections) else None
        )
        self.investigation = InvestigationPanel(self, definition=definition)
        self.splitter.replaceWidget(2, self.investigation)
        old.deleteLater()
        self.investigation.value_changed.connect(self._field_changed)
        self.investigation.save_requested.connect(self.save_draft)
        self.investigation.complete_requested.connect(self.complete_case)
        self.investigation.next_requested.connect(self.next_case)
        self.investigation.edit_form_requested.connect(self.edit_form)
        self.investigation.tabs.setCurrentIndex(
            next(
                (
                    index
                    for index, section in enumerate(self.investigation.sections)
                    if section.key == selected_tab_key
                ),
                0,
            )
        )

    def edit_form(self) -> None:
        if self._commit_in_progress:
            return
        if self._workspace_load_failed:
            QMessageBox.warning(
                self, "작업 폴더 오류", "읽기 오류가 없는 작업 폴더를 먼저 선택해 주세요."
            )
            return
        dialog = FormDesigner(
            self.repository.form_definition,
            self,
            initial_definition=self.repository.initial_form_definition,
        )
        # Keep the editor open if a data compatibility or disk-write error occurs.
        while dialog.exec() == QDialog.DialogCode.Accepted:
            if self.apply_form_definition(dialog.definition):
                break

    def apply_form_definition(self, definition: FormDefinition) -> bool:
        if self._workspace_load_failed:
            return False
        try:
            definition = FormDefinition.from_dict(definition.to_dict())
            candidates = [
                InvestigationCase.from_dict(
                    case.to_dict(), form_definition=self.repository.form_definition
                ) for case in self.cases
            ]
            reopened = 0
            for original, case in zip(self.cases, candidates, strict=True):
                case.values = definition.migrate_values(
                    case.values, self.repository.form_definition
                )
                case.form_definition = definition
                introduced_errors = set(case.errors) - set(original.errors)
                if introduced_errors:
                    labels = ", ".join(definition.label(key) for key in sorted(introduced_errors))
                    raise ValueError(
                        f"{case.display_id}: 기존 값이 새 입력 방식·선택지와 맞지 않습니다 ({labels}). 기존 값을 정리한 뒤 적용해 주세요."
                    )
                if case.status == "completed" and not case.can_complete:
                    case.mark_edited()
                    reopened += 1
            self._disk_operation(self.repository.save,
                candidates, active_case_uid=self.current_uid, form_definition=definition
            )
        except (ValueError, TypeError, KeyError, WorkspaceError) as exc:
            QMessageBox.warning(self, "양식 적용 오류", str(exc))
            return False
        self.cases = candidates
        self._mark_saved(self.current_uid)
        self._install_form(definition)
        self.case_list.set_cases(self.cases, self.current_uid)
        self._show_current_case(open_video=False)
        message = "조사 양식을 적용하고 저장했습니다."
        self._set_workflow_enabled(True)
        if reopened:
            message += f" 새 완료 조건에 따라 {reopened}건이 미완료로 전환되었습니다."
        self.statusBar().showMessage(message, 7000)
        return True

    def _set_workflow_enabled(self, enabled: bool) -> None:
        has_case = enabled and self.current_case is not None
        self.case_list.new_button.setEnabled(enabled)
        self.case_list.delete_button.setEnabled(
            enabled and self.case_list.tree.currentItem() is not None
        )
        self.case_list.export_button.setEnabled(enabled and bool(self.cases))
        self.case_list.report_export_button.setEnabled(enabled and bool(self.cases))
        self.report_export_action.setEnabled(enabled and bool(self.cases))
        self.investigation.setEnabled(has_case)
        self.investigation.save_button.setEnabled(has_case)
        self.investigation.complete_button.setEnabled(
            has_case and self.current_case is not None and self.current_case.status != "completed"
        )
        self.investigation.next_button.setEnabled(has_case)

    def choose_workspace(self) -> None:
        if self._commit_in_progress or self._switching_project:
            return
        selected = QFileDialog.getExistingDirectory(
            self,
            "프로젝트 폴더 선택",
            str(self.repository.workspace),
            QFileDialog.Option.ShowDirsOnly,
        )
        if not selected:
            return
        # Selecting an already-open folder activates its one existing tab.
        destination = Path(selected).expanduser().resolve()
        if any(project.repository.workspace == destination for project in self._projects):
            self.open_project(destination)
            return
        mode = self._choose_project_open_mode(destination)
        if mode is not None:
            self.open_project(destination, new_tab=mode == "new")

    def _choose_project_open_mode(self, folder: Path) -> str | None:
        dialog = QMessageBox(self)
        dialog.setWindowTitle("프로젝트 폴더 열기")
        dialog.setText(f"‘{folder.name or folder}’ 프로젝트를 어디에서 열까요?")
        dialog.setInformativeText("프로젝트마다 사고 목록과 조사 내용이 별도로 저장됩니다.")
        current = dialog.addButton("현재 탭으로 불러오기", QMessageBox.ButtonRole.AcceptRole)
        new = dialog.addButton("새 탭에서 열기", QMessageBox.ButtonRole.ActionRole)
        cancel = dialog.addButton(QMessageBox.StandardButton.Cancel)
        dialog.setDefaultButton(new)
        dialog.setEscapeButton(cancel)
        dialog.exec()
        clicked = dialog.clickedButton()
        return "new" if clicked is new else "current" if clicked is current else None

    def _sync_project_tabs(self) -> None:
        blocked = self.project_tabs.blockSignals(True)
        try:
            self.project_tabs.setCurrentIndex(self._active_project_index)
            self.project_tabs.setTabsClosable(True)
            for index, project in enumerate(self._projects):
                path = project.repository.workspace
                name = path.name or str(path)
                if sum(other.repository.workspace.name == path.name for other in self._projects) > 1:
                    name = f"{name} — {path.parent.name or path.parent}"
                if project.pending_video_paths:
                    name += " •"
                self.project_tabs.setTabText(index, name)
                self.project_tabs.setTabIcon(index, material_icon("folder_open", PRIMARY))
                self.project_tabs.setTabToolTip(index, str(path))
                if self.project_tabs.tabButton(index, QTabBar.ButtonPosition.LeftSide) is not None:
                    self.project_tabs.setTabButton(index, QTabBar.ButtonPosition.LeftSide, None)
                close_button = self.project_tabs.tabButton(index, QTabBar.ButtonPosition.RightSide)
                if not isinstance(close_button, QToolButton) or close_button.objectName() != "closeProjectTab":
                    close_button = QToolButton(self.project_tabs)
                    close_button.setObjectName("closeProjectTab")
                    close_button.setIcon(material_icon("close", MUTED))
                    close_button.setIconSize(QSize(16, 16))
                    close_button.setFixedSize(20, 20)
                    close_button.clicked.connect(lambda checked=False, button=close_button: self._request_project_tab_close(button))
                    self.project_tabs.setTabButton(index, QTabBar.ButtonPosition.RightSide, close_button)
                close_button.setEnabled(len(self._projects) > 1)
                close_button.setAccessibleName(f"{name} 프로젝트 닫기")
                close_button.setToolTip("프로젝트 닫기" if len(self._projects) > 1 else "마지막 프로젝트는 닫을 수 없습니다.")
        finally:
            self.project_tabs.blockSignals(blocked)

    def _request_project_tab_close(self, button: QToolButton) -> None:
        for index in range(self.project_tabs.count()):
            if self.project_tabs.tabButton(index, QTabBar.ButtonPosition.RightSide) is button:
                self.project_tabs.tabCloseRequested.emit(index)
                return

    def _move_project_tab(self, source: int, destination: int) -> None:
        """Keep tab order and project identity aligned without reloading a project."""
        if source == destination or not (0 <= source < len(self._projects) and 0 <= destination < len(self._projects)):
            return
        if self._commit_in_progress or self._switching_project or self._video_import_in_progress:
            blocked = self.project_tabs.blockSignals(True)
            try:
                self.project_tabs.moveTab(destination, source)
            finally:
                self.project_tabs.blockSignals(blocked)
            return
        active = self._projects[self._active_project_index]
        self._projects.insert(destination, self._projects.pop(source))
        # QTabBar emits tabMoved before currentChanged. Update the active index
        # first so that the latter does not save/reopen an unchanged project.
        self._active_project_index = next(index for index, project in enumerate(self._projects) if project is active)
        self._project_tabs_timer.start()
        self.statusBar().showMessage("프로젝트 폴더 탭 순서를 변경했습니다.", 3000)

    def _save_project_tabs(self) -> None:
        if self._commit_in_progress or self._switching_project or self._video_import_in_progress:
            self._project_tabs_timer.start()
            return
        state = ProjectTabsState(
            tuple(project.repository.workspace for project in self._projects), self.repository.workspace,
        )
        try:
            self._project_tabs_store.save(state)
        except (OSError, ValueError, RuntimeError):
            self.statusBar().showMessage("프로젝트 탭 설정을 저장하지 못했습니다. 설정 폴더의 쓰기 권한을 확인해 주세요. 사고 데이터 저장과는 별개입니다.", 12000)

    def _capture_project(self) -> None:
        project = self._projects[self._active_project_index]
        project.repository = self.repository
        project.cases = self.cases
        project.current_uid = self.current_uid
        project.load_failed = self._workspace_load_failed
        project.initialized = not self._workspace_load_failed
        project.list_state = self.case_list.capture_view_state()
        index = self.investigation.tabs.currentIndex()
        project.form_tab_key = self.investigation.sections[index].key if index >= 0 else None
        page = self.investigation.tabs.currentWidget()
        project.form_scroll = page.verticalScrollBar().value() if page is not None else 0
        project.splitter_sizes = self.splitter.sizes()
        metadata = self.video_controller.metadata
        project.video_path = str(metadata.path) if metadata else None
        # Preserve the user's latest navigation target even if its asynchronous
        # decode has not completed when this project tab is left.
        project.video_frame = max(0, self.video_controller.requested_frame)
        project.statistics_state = self.statistics_panel.capture_state()

    def _leave_project(self) -> bool:
        if not self._workspace_load_failed and not self._save(show_message=False):
            return False
        self._capture_project()
        self.video_controller.close_video()
        return True

    def _activate_project(self, index: int) -> bool:
        project = self._projects[index]
        self._active_project_index = index
        self.repository = project.repository
        self.cases = project.cases
        self.current_uid = project.current_uid
        self._workspace_load_failed = project.load_failed
        self._case_revisions.clear()
        self._deferred_reorders.clear()
        self._mark_saved(self.current_uid)
        self.statistics_panel.restore_state(project.statistics_state)
        self._sync_project_tabs()
        self.case_list.set_cases([])
        self.case_list.search.clear()
        self._update_workspace_status()
        if not project.initialized:
            self.current_uid = None
            self.investigation.tabs.setCurrentIndex(0)
            self._load_workspace()
            project.initialized = not self._workspace_load_failed
            project.load_failed = self._workspace_load_failed
            self._watch_active_project()
            self._invalidate_statistics()
            return not self._workspace_load_failed

        self._install_form(self.repository.form_definition)
        current = project.list_state.get("current")
        video_index = current[1] if current and current[1] >= 0 else None
        self.case_list.set_cases(self.cases, self.current_uid, selected_video_index=video_index)
        self.case_list.restore_view_state(project.list_state)
        self._show_current_case(open_video=True)
        metadata = self.video_controller.metadata
        if metadata and str(metadata.path) == project.video_path:
            self.video_controller.seek(min(project.video_frame, metadata.last_frame))
        if project.form_tab_key:
            tab = next((i for i, section in enumerate(self.investigation.sections)
                        if section.key == project.form_tab_key), 0)
            self.investigation.tabs.setCurrentIndex(tab)
            self.investigation.tabs.currentWidget().verticalScrollBar().setValue(project.form_scroll)
        if project.splitter_sizes:
            self.splitter.setSizes(project.splitter_sizes)
        self._set_workflow_enabled(not self._workspace_load_failed)
        if self._workspace_load_failed:
            self.investigation.case_title.setText("작업 폴더 읽기 오류")
            self.investigation.completion_badge.setText("읽기 오류")
        self._watch_active_project()
        self._invalidate_statistics()
        return True

    def switch_project(self, index: int) -> bool:
        if index == self._active_project_index:
            return True
        if self._commit_in_progress or self._switching_project or self._video_import_in_progress or not 0 <= index < len(self._projects):
            self._sync_project_tabs()
            return False
        self._switching_project = True
        try:
            if not self._leave_project():
                self._sync_project_tabs()
                return False
            return self._activate_project(index)
        finally:
            self._switching_project = False
            self._project_tabs_timer.start()

    def open_project(self, folder: str | Path, *, new_tab: bool = True) -> bool:
        """Open an independent workspace, or activate its existing tab."""
        if self._commit_in_progress or self._switching_project or self._video_import_in_progress:
            return False
        path = Path(folder).expanduser().resolve()
        if not path.is_dir():
            QMessageBox.warning(self, "프로젝트 폴더", "존재하는 폴더를 선택해 주세요.")
            return False
        for index, project in enumerate(self._projects):
            if project.repository.workspace == path:
                if project.load_failed and not project.initialized and index == self._active_project_index:
                    self._switching_project = True
                    try:
                        return self._activate_project(index)
                    finally:
                        self._switching_project = False
                        self._project_tabs_timer.start()
                return self.switch_project(index)
        self._switching_project = True
        previous_index = self._active_project_index
        previous_project = self._projects[previous_index]
        try:
            if not self._leave_project():
                return False
            project = ProjectSession(CaseRepository(path))
            index = len(self._projects) if new_tab else previous_index
            if new_tab:
                self._projects.append(project)
                blocked = self.project_tabs.blockSignals(True)
                self.project_tabs.addTab(path.name or str(path))
                self.project_tabs.blockSignals(blocked)
            else:
                self._projects[index] = project
            if self._activate_project(index):
                return True
            # Failed opens keep the saved source project accessible in its tab.
            if new_tab:
                self._projects.pop(index)
                blocked = self.project_tabs.blockSignals(True)
                self.project_tabs.removeTab(index)
                self.project_tabs.blockSignals(blocked)
            else:
                self._projects[index] = previous_project
            self._activate_project(previous_index)
            self.statusBar().showMessage("프로젝트를 열지 못해 이전 프로젝트를 유지했습니다.", 5000)
            return False
        finally:
            self._switching_project = False
            self._project_tabs_timer.start()

    def close_project(self, index: int) -> bool:
        if self._commit_in_progress or self._switching_project or self._video_import_in_progress or len(self._projects) <= 1:
            return False
        if not 0 <= index < len(self._projects):
            return False
        self._switching_project = True
        try:
            active = index == self._active_project_index
            if active and not self._leave_project():
                return False
            removed = self._projects.pop(index)
            self.folder_watcher.remove_directory(removed.repository.workspace)
            blocked = self.project_tabs.blockSignals(True)
            self.project_tabs.removeTab(index)
            self.project_tabs.blockSignals(blocked)
            if active:
                self._activate_project(min(index, len(self._projects) - 1))
            else:
                self._active_project_index -= int(index < self._active_project_index)
                self._sync_project_tabs()
            return True
        finally:
            self._switching_project = False
            self._project_tabs_timer.start()

    def create_case(self) -> None:
        if not self._save(show_message=False):
            return
        candidates = list(self.cases)
        case = new_case(candidates)
        candidates.append(case)
        if not self._replace_cases_atomically(candidates, case.uid):
            return
        self.statusBar().showMessage(f"{case.display_id} 사고를 만들었습니다.", 3000)

    def reorder_case(self, uid: str, destination: int) -> None:
        if self._commit_in_progress:
            self._deferred_reorders.append(([uid], destination))
            return
        source = next((index for index, case in enumerate(self.cases) if case.uid == uid), -1)
        if source < 0 or not self.cases:
            return
        target = max(0, min(int(destination), len(self.cases) - 1))
        if source == target:
            return
        case = self.cases.pop(source)
        self.cases.insert(target, case)
        self._dirty = True
        self._order_dirty = True
        self.case_list.move_case(uid, target)
        self.statusBar().showMessage(
            f"{case.display_id} 표시 순서를 {target + 1}번째로 변경했습니다.", 3000
        )

    def reorder_cases(self, uids: list[str], destination: int) -> None:
        if self._commit_in_progress:
            self._deferred_reorders.append((list(uids), destination))
            return
        moving = [case for case in self.cases if case.uid in uids]
        if not moving:
            return
        remaining = [case for case in self.cases if case.uid not in uids]
        target = max(0, min(int(destination), len(remaining)))
        reordered = remaining[:target] + moving + remaining[target:]
        if [case.uid for case in reordered] == [case.uid for case in self.cases]:
            return
        self.cases[:] = reordered
        self.case_list.move_cases([case.uid for case in moving], target)
        self._dirty = True
        self._order_dirty = True
        self.statusBar().showMessage(f"사고 {len(moving)}개의 표시 순서를 변경했습니다.", 3000)

    def choose_videos(self) -> None:
        filters = "영상 파일 (*.mp4 *.avi *.mov *.mkv *.m4v *.wmv *.webm);;모든 파일 (*.*)"
        files, _ = QFileDialog.getOpenFileNames(self, "조사 영상 선택", QDir.homePath(), filters)
        if files:
            self._import_dropped_videos(files)

    def _choose_video_import_options(self) -> bool | None:
        dialog = VideoImportDialog(self.repository.workspace, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return None
        return dialog.copy_to_workspace.isChecked()

    def _import_dropped_videos(self, files: list[str]) -> None:
        copy = self._choose_video_import_options()
        if copy is not None:
            self.attach_videos(files, copy_to_workspace=copy)

    def _watch_active_project(self) -> None:
        if not self._workspace_load_failed:
            self.folder_watcher.add_directory(
                self.repository.workspace,
                known_paths={path for case in self.cases for path in case.videos},
            )
        self._schedule_watch_refresh()

    def _schedule_watch_refresh(self) -> None:
        if self._projects[self._active_project_index].pending_video_paths and not self._watch_refresh_timer.isActive():
            self._watch_refresh_timer.start(500)

    def _watched_videos_ready(self, folder: str, paths: list[str]) -> None:
        project = next((p for p in self._projects if p.repository.workspace == Path(folder)), None)
        if project is None:
            return
        project.pending_video_paths.update(paths)
        self._sync_project_tabs()
        self._schedule_watch_refresh()

    def _refresh_watched_videos(self) -> None:
        if (self._commit_in_progress or self._switching_project or self._video_import_in_progress
                or QApplication.activeModalWidget() is not None):
            self._watch_refresh_timer.start(500)
            return
        project = self._projects[self._active_project_index]
        if self._workspace_load_failed or not project.pending_video_paths:
            return
        paths = set(project.pending_video_paths)
        project.pending_video_paths.difference_update(paths)
        self._auto_import_failed = False
        self._video_import_in_progress = True
        try:
            self.import_video_folder(
                self.repository.workspace, quiet_when_empty=True, respect_exclusions=True,
                only_paths=paths, automatic=True,
            )
        finally:
            self._video_import_in_progress = False
        if self._auto_import_failed:
            project.pending_video_paths.update(paths)
            self._watch_refresh_timer.start(5000)
        self._sync_project_tabs()

    def _copy_import_files(self, files: list[str]) -> CopiedVideos | None:
        self.statusBar().showMessage("영상을 작업 폴더로 복사하고 있습니다…")
        try:
            return self._disk_operation(copy_videos, files, self.repository.workspace)
        except OSError as exc:
            QMessageBox.warning(self, "영상 복사 오류", f"영상을 복사할 수 없습니다.\n{exc}")
            return None

    def _rollback_video_copy(self, batch: CopiedVideos) -> None:
        try:
            self._disk_operation(batch.rollback)
            project = self._projects[self._active_project_index]
            project.pending_video_paths.difference_update(str(path) for path in batch.created)
        except OSError as exc:
            QMessageBox.warning(self, "영상 복사 정리 오류", f"복사한 일부 파일을 정리하지 못했습니다.\n{exc}")

    @staticmethod
    def _is_default_case_shell(case: InvestigationCase) -> bool:
        """Return whether *case* only contains the legacy automatic 001 identity."""

        return case.status == "draft" and case.values == {
            "institution": DEFAULT_INSTITUTION,
            "accident_classification": "001",
        }

    def import_video_folder(
        self,
        folder: str | Path,
        *,
        quiet_when_empty: bool = False,
        respect_exclusions: bool = False,
        copy_to_workspace: bool = False,
        only_paths: set[str] | None = None,
        automatic: bool = False,
    ) -> bool:
        """Group conventionally named videos into Samsung accident cases atomically."""

        if self._workspace_load_failed:
            self.statusBar().showMessage("읽기 오류가 없는 작업 폴더를 먼저 선택해 주세요.", 5000)
            return False
        try:
            scan = scan_video_folder(folder)
        except OSError as exc:
            if automatic:
                self._auto_import_failed = True
                self.statusBar().showMessage("작업 폴더를 읽지 못해 자동 갱신을 보류했습니다.", 5000)
                return False
            QMessageBox.warning(
                self,
                "영상 폴더 오류",
                f"영상 폴더를 읽을 수 없습니다.\n{exc}",
            )
            return False

        if only_paths is not None:
            selected = {str(Path(path).resolve()).casefold() for path in only_paths}
            scan = VideoFolderScan(
                tuple((number, matching) for number, paths in scan.groups
                      if (matching := tuple(path for path in paths if path.casefold() in selected))),
                tuple(path for path in scan.ignored_videos if path.casefold() in selected),
            )

        if not scan.groups:
            if not quiet_when_empty:
                detail = (
                    f" 파일명 규칙에 맞지 않는 영상 {len(scan.ignored_videos)}개를 건너뛰었습니다."
                    if scan.ignored_videos
                    else ""
                )
                QMessageBox.warning(
                    self,
                    "영상 폴더",
                    "13자리 사고번호로 된 영상을 찾지 못했습니다." + detail,
                )
            return False

        if copy_to_workspace:
            self._video_import_in_progress = True
            try:
                files = [path for _, paths in scan.groups for path in paths]
                batch = self._copy_import_files(files)
                if batch is None:
                    return False
                success = self.import_video_folder(
                    self.repository.workspace, quiet_when_empty=quiet_when_empty,
                    respect_exclusions=respect_exclusions, only_paths=set(batch.paths),
                )
                if not success:
                    self._rollback_video_copy(batch)
                return success
            finally:
                self._video_import_in_progress = False
                self._schedule_watch_refresh()

        # Copy on write: unchanged accidents retain their model identity.
        candidates = list(self.cases)
        detached: set[str] = set()
        candidate_positions = {case.uid: index for index, case in enumerate(candidates)}

        def detach(case: InvestigationCase) -> InvestigationCase:
            if case.uid not in detached:
                copy = self._copy_case(case)
                candidates[candidate_positions[case.uid]] = copy
                detached.add(case.uid)
                return copy
            return case
        candidate_exclusions = set(self.repository.auto_import_exclusions)
        scan_number_by_path: dict[str, str] = {}
        for accident_number, paths in scan.groups:
            for path in paths:
                path_key = str(Path(path).expanduser().resolve()).casefold()
                if respect_exclusions and path_key in candidate_exclusions:
                    continue
                scan_number_by_path[path_key] = accident_number
        rehomed_paths: set[str] = set()
        rehomed_markers: dict[str, dict[str, Any]] = {}
        repaired_videos = 0
        removed_default_cases = 0
        repair_focus_uid: str | None = None
        preferred_rehome_path: str | None = None

        # Older versions could attach the first conventionally named video to
        # the automatically created SAMSUNG-001 case. Repair only that exact,
        # otherwise-empty identity shell and only for files in this scan. This
        # leaves intentionally edited/manual cases untouched.
        for shell in list(candidates):
            if not self._is_default_case_shell(shell):
                continue
            shell = detach(shell)
            mapped = [
                (index, path, scan_number_by_path.get(str(Path(path).resolve()).casefold()))
                for index, path in enumerate(shell.videos)
            ]
            mapped = [item for item in mapped if item[2] is not None]
            unmapped_count = len(shell.videos) - len(mapped)
            mapped_numbers = {str(item[2]) for item in mapped}
            target_exists = any(
                other is not shell
                and str(other.values.get("institution", "")).strip().casefold()
                == DEFAULT_INSTITUTION
                and str(other.values.get("accident_classification", "")).strip() in mapped_numbers
                for other in candidates
            )

            if mapped and len(mapped_numbers) == 1 and unmapped_count == 0 and not target_exists:
                accident_number = next(iter(mapped_numbers))
                shell.values["accident_classification"] = accident_number
                shell.mark_edited()
                active_video = shell.active_video
                shell.videos.sort(
                    key=lambda path: (
                        (parse_accident_video_name(path) or ("", 10**12))[1],
                        Path(path).name.casefold(),
                    )
                )
                if active_video in shell.videos:
                    shell.active_video_index = shell.videos.index(active_video)
                renamed_paths = {str(Path(path).resolve()).casefold() for _, path, _ in mapped}
                rehomed_paths.update(renamed_paths)
                if not respect_exclusions:
                    candidate_exclusions.difference_update(renamed_paths)
                repaired_videos += len(mapped)
                repair_focus_uid = repair_focus_uid or shell.uid
                continue

            moved_indexes: list[int] = []
            case_marker = shell.markers_by_video.get("__case__")
            active_path_key = (
                str(Path(shell.active_video).resolve()).casefold() if shell.active_video else None
            )
            for index, path, accident_number in mapped:
                del accident_number
                path_key = str(Path(path).resolve()).casefold()
                for marker_key, marker in shell.markers_by_video.items():
                    if marker_key == "__case__":
                        continue
                    if str(Path(marker_key).resolve()).casefold() == path_key:
                        rehomed_markers[path_key] = dict(marker)
                        break
                rehomed_paths.add(path_key)
                if not respect_exclusions:
                    candidate_exclusions.discard(path_key)
                moved_indexes.append(index)
            if case_marker is not None and mapped:
                mapped_path_keys = {str(Path(path).resolve()).casefold() for _, path, _ in mapped}
                if active_path_key in mapped_path_keys:
                    rehomed_markers.setdefault(active_path_key, dict(case_marker))
                    shell.markers_by_video.pop("__case__", None)
            if active_path_key in rehomed_paths:
                preferred_rehome_path = active_path_key
            if moved_indexes:
                active_path = shell.active_video
                for index in sorted(moved_indexes, reverse=True):
                    shell.remove_video(index)
                if active_path in shell.videos:
                    shell.active_video_index = shell.videos.index(active_path)
                repaired_videos += len(moved_indexes)
            if moved_indexes and not shell.videos and not shell.markers_by_video:
                candidates.remove(shell)
                candidate_positions = {case.uid: index for index, case in enumerate(candidates)}
                removed_default_cases += 1

        by_identity = {
            (
                str(case.values.get("institution", "")).strip().casefold(),
                str(case.values.get("accident_classification", "")).strip(),
            ): case
            for case in candidates
        }
        path_owner: dict[str, str] = {}
        for case in candidates:
            for video in case.videos:
                key = str(Path(video).expanduser().resolve()).casefold()
                path_owner.setdefault(key, case.uid)

        imported_uid: str | None = None
        created_cases = 0
        added_videos = 0
        attached_videos = 0
        affected_uids: set[str] = set()
        duplicate_videos = 0
        conflicting_videos = 0

        for accident_number, paths in scan.groups:
            if respect_exclusions:
                paths = tuple(
                    path
                    for path in paths
                    if str(Path(path).expanduser().resolve()).casefold() not in candidate_exclusions
                )
                if not paths:
                    continue
            identity = (DEFAULT_INSTITUTION, accident_number)
            case = by_identity.get(identity)
            new_paths: list[str] = []
            for path in paths:
                key = str(Path(path).expanduser().resolve()).casefold()
                owner = path_owner.get(key)
                if owner is None:
                    new_paths.append(path)
                elif case is not None and owner == case.uid:
                    if key in rehomed_paths:
                        if key == preferred_rehome_path:
                            repair_focus_uid = case.uid
                        marker = rehomed_markers.get(key)
                        marker_exists = any(
                            marker_key != "__case__"
                            and str(Path(marker_key).resolve()).casefold() == key
                            for marker_key in case.markers_by_video
                        )
                        if marker is not None and not marker_exists:
                            case = detach(case)
                            by_identity[identity] = case
                            case.markers_by_video[str(Path(path).resolve())] = marker
                            case.mark_edited()
                    else:
                        duplicate_videos += 1
                else:
                    conflicting_videos += 1

            # Do not create an empty case when every matching file already
            # belongs to a different investigation.
            if case is None and not new_paths:
                continue
            # A repeat import is a true no-op: keep the current selection and
            # do not rewrite the workspace when this accident has no new file.
            if case is not None and not new_paths:
                continue
            if case is None:
                case = InvestigationCase(
                    values={
                        "institution": DEFAULT_INSTITUTION,
                        "accident_classification": accident_number,
                    }
                )
                candidates.append(case)
                candidate_positions[case.uid] = len(candidates) - 1
                detached.add(case.uid)
                by_identity[identity] = case
                created_cases += 1

            case = detach(case)
            by_identity[identity] = case
            added = case.attach_videos(new_paths)
            if not added:
                continue
            if imported_uid is None:
                imported_uid = case.uid
            attached_videos += added
            affected_uids.add(case.uid)
            for path in new_paths[:added]:
                key = str(Path(path).expanduser().resolve()).casefold()
                path_owner[key] = case.uid
                if key == preferred_rehome_path:
                    repair_focus_uid = case.uid
                marker = rehomed_markers.get(key)
                if marker is not None:
                    case.markers_by_video[str(Path(path).expanduser().resolve())] = marker
                if key not in rehomed_paths:
                    added_videos += 1
                if not respect_exclusions:
                    candidate_exclusions.discard(key)

            active_video = case.active_video
            case.videos.sort(
                key=lambda path: (
                    (parse_accident_video_name(path) or ("", 10**12))[1],
                    Path(path).name.casefold(),
                )
            )
            if active_video in case.videos:
                case.active_video_index = case.videos.index(active_video)

        repair_changed = bool(repaired_videos or removed_default_cases)
        if attached_videos == 0 and not repair_changed:
            if not quiet_when_empty:
                notes = []
                if duplicate_videos:
                    notes.append(f"중복 {duplicate_videos}개")
                if scan.ignored_videos:
                    notes.append(f"규칙 불일치 {len(scan.ignored_videos)}개")
                if conflicting_videos:
                    notes.append(f"다른 사고에 연결됨 {conflicting_videos}개")
                suffix = f" ({', '.join(notes)} 건너뜀)" if notes else ""
                self.statusBar().showMessage("새로 가져올 영상이 없습니다." + suffix, 6000)
            return False

        active_uid = repair_focus_uid or imported_uid
        available_uids = {case.uid for case in candidates}
        if active_uid not in available_uids:
            active_uid = self.current_uid if self.current_uid in available_uids else None
        if active_uid is None and candidates:
            active_uid = candidates[0].uid
        if automatic and self.current_uid in available_uids:
            active_uid = self.current_uid

        try:
            self._save_candidates(
                candidates, active_uid,
                auto_import_exclusions=candidate_exclusions,
            )
        except WorkspaceError as exc:
            if automatic:
                self._auto_import_failed = True
                self.statusBar().showMessage("새 영상 저장에 실패했습니다. 자동 갱신을 다시 시도합니다.", 5000)
                return False
            QMessageBox.critical(self, "저장 오류", str(exc))
            return False

        previous_uid = self.current_uid
        if not automatic:
            self.video_controller.close_video()
        self.cases = candidates
        self.current_uid = active_uid
        self._mark_saved(active_uid)
        if automatic:
            self.case_list.merge_cases(self.cases, active_uid)
            if previous_uid != active_uid:
                self.case_list.select_case(active_uid)
                self._show_current_case(open_video=True)
            else:
                self._refresh_case_presentation(rebuild_list=False)
        else:
            self.case_list.set_cases(self.cases, active_uid)
            self._show_current_case(open_video=True)
        self._set_workflow_enabled(True)

        notes = []
        if duplicate_videos:
            notes.append(f"중복 {duplicate_videos}개")
        if scan.ignored_videos:
            notes.append(f"규칙 불일치 {len(scan.ignored_videos)}개")
        if conflicting_videos:
            notes.append(f"다른 사고에 연결됨 {conflicting_videos}개")
        suffix = f" ({', '.join(notes)} 건너뜀)" if notes else ""
        created_note = f" (신규 사고 {created_cases}건)" if created_cases else ""
        repair_note = (
            f" 기존 기본 사고의 영상 {repaired_videos}개를 사고번호 기준으로 정리했습니다."
            if repaired_videos
            else ""
        )
        if added_videos:
            message = (
                f"사고 {len(affected_uids)}건에 영상 {added_videos}개를 불러왔습니다"
                f"{created_note}.{repair_note}{suffix}"
            )
        elif repaired_videos:
            message = repair_note.strip() + suffix
        else:
            message = "빈 기본 사고를 정리했습니다." + suffix
        self.statusBar().showMessage(message, 6000)
        return True

    def attach_videos(self, files: list[str], *, copy_to_workspace: bool = False) -> None:
        if not copy_to_workspace:
            self._attach_video_paths(files)
            return
        if self._workspace_load_failed:
            self.statusBar().showMessage("읽기 오류가 없는 작업 폴더를 먼저 선택해 주세요.", 5000)
            return
        self._video_import_in_progress = True
        try:
            batch = self._copy_import_files(files)
            if batch is not None and not self._attach_video_paths(batch.paths):
                self._rollback_video_copy(batch)
        finally:
            self._video_import_in_progress = False
            self._schedule_watch_refresh()

    def _attach_video_paths(self, files: list[str]) -> bool:
        if self._workspace_load_failed:
            self.statusBar().showMessage("읽기 오류가 없는 작업 폴더를 먼저 선택해 주세요.", 5000)
            return False
        valid = [
            path
            for path in files
            if Path(path).suffix.casefold() in VIDEO_EXTENSIONS and Path(path).is_file()
        ]
        if not valid:
            QMessageBox.warning(self, "영상 추가", "지원되는 영상 파일을 선택해 주세요.")
            return False

        candidates = list(self.cases)
        case = next((item for item in candidates if item.uid == self.current_uid), None)
        if case is not None:
            index = candidates.index(case)
            case = self._copy_case(case)
            candidates[index] = case
        parsed_names = [parse_accident_video_name(path) for path in valid]
        parsed_numbers = {parsed[0] for parsed in parsed_names if parsed is not None}
        all_conventional = all(parsed is not None for parsed in parsed_names)
        matching_case = (
            next(
                (
                    item
                    for item in candidates
                    if str(item.values.get("institution", "")).strip().casefold()
                    == DEFAULT_INSTITUTION
                    and str(item.values.get("accident_classification", "")).strip()
                    == next(iter(parsed_numbers))
                ),
                None,
            )
            if len(parsed_numbers) == 1
            else None
        )
        removed_default_shell = False

        if case is None and all_conventional and len(parsed_numbers) > 1:
            QMessageBox.warning(
                self,
                "영상 추가",
                "여러 사고번호의 영상은 프로젝트 폴더에 넣어 자동으로 불러와 주세요.",
            )
            return False

        if case is None:
            if all_conventional and len(parsed_numbers) == 1:
                case = matching_case or InvestigationCase(
                    values={
                        "institution": DEFAULT_INSTITUTION,
                        "accident_classification": next(iter(parsed_numbers)),
                    }
                )
            else:
                case = new_case(candidates)
            if case not in candidates:
                candidates.append(case)
        elif (
            self._is_default_case_shell(case)
            and not case.videos
            and not case.markers_by_video
            and all_conventional
            and len(parsed_numbers) == 1
        ):
            if matching_case is not None and matching_case is not case:
                candidates.remove(case)
                case = matching_case
                removed_default_shell = True
            else:
                case.values["accident_classification"] = next(iter(parsed_numbers))
                case.mark_edited()

        case_index = next(index for index, item in enumerate(candidates) if item.uid == case.uid)
        case = self._copy_case(case)
        candidates[case_index] = case
        added = case.attach_videos(valid)
        if not added:
            if removed_default_shell and self._replace_cases_atomically(candidates, case.uid):
                self.statusBar().showMessage("빈 기본 사고를 정리했습니다.", 3000)
                return True
            self.statusBar().showMessage("선택한 영상은 이미 현재 사고에 연결되어 있습니다.", 4000)
            return False
        updated_exclusions = set(self.repository.auto_import_exclusions)
        for path in valid:
            updated_exclusions.discard(str(Path(path).expanduser().resolve()).casefold())

        if not self._replace_cases_atomically(
            candidates,
            case.uid,
            auto_import_exclusions=updated_exclusions,
        ):
            return False
        self.statusBar().showMessage(f"영상 {added}개를 추가했습니다.", 3000)
        return True

    def _replace_cases_atomically(
        self,
        candidates: list[InvestigationCase],
        active_uid: str | None,
        *,
        auto_import_exclusions: set[str] | None = None,
    ) -> bool:
        """Persist candidate state before replacing live models and UI."""

        try:
            self._save_candidates(
                candidates, active_uid,
                auto_import_exclusions=auto_import_exclusions,
            )
        except WorkspaceError as exc:
            QMessageBox.critical(self, "저장 오류", str(exc))
            return False

        self.video_controller.close_video()
        self.cases = candidates
        self.current_uid = active_uid
        self._mark_saved(active_uid)
        self.case_list.set_cases(self.cases, active_uid, selected_video_index=None)
        self._show_current_case(open_video=True)
        self._set_workflow_enabled(True)
        return True

    def delete_case(self, uid: str) -> None:
        case_index = next(
            (index for index, case in enumerate(self.cases) if case.uid == uid),
            None,
        )
        if case_index is None:
            return
        case = self.cases[case_index]
        answer = QMessageBox.question(
            self,
            "사고 삭제",
            f"{case.display_id} 사고의 조사 내용과 영상 연결 정보를 삭제하시겠습니까?\n\n"
            "원본 영상 파일은 삭제되지 않습니다.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return

        candidates = list(self.cases)
        candidates.pop(case_index)
        if not candidates:
            active_uid = None
        elif uid == self.current_uid or self.current_uid not in {item.uid for item in candidates}:
            active_uid = candidates[min(case_index, len(candidates) - 1)].uid
        else:
            active_uid = str(self.current_uid)

        updated_exclusions = set(self.repository.auto_import_exclusions)
        updated_exclusions.update(
            str(Path(path).expanduser().resolve()).casefold() for path in case.videos
        )
        if not self._replace_cases_atomically(
            candidates,
            active_uid,
            auto_import_exclusions=updated_exclusions,
        ):
            return
        message = f"{case.display_id} 사고를 삭제했습니다. 원본 영상 파일은 유지됩니다."
        self.statusBar().showMessage(message, 5000)

    def delete_video(self, uid: str, index: int) -> None:
        source_case = next((case for case in self.cases if case.uid == uid), None)
        if source_case is None or not 0 <= index < len(source_case.videos):
            return
        video_path = source_case.videos[index]
        video_name = Path(video_path).name
        answer = QMessageBox.question(
            self,
            "영상 삭제",
            f"{video_name} 영상을 사고 목록에서 삭제하고 해당 영상의 마커도 삭제하시겠습니까?\n\n"
            "원본 영상 파일은 삭제되지 않습니다.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return

        candidates = list(self.cases)
        target_index = next(index for index, case in enumerate(candidates) if case.uid == uid)
        target = self._copy_case(candidates[target_index])
        candidates[target_index] = target
        target.remove_video(index)
        active_uid = (
            str(self.current_uid) if self.current_uid in {case.uid for case in candidates} else uid
        )
        updated_exclusions = set(self.repository.auto_import_exclusions)
        updated_exclusions.add(str(Path(video_path).expanduser().resolve()).casefold())
        if not self._replace_cases_atomically(
            candidates,
            active_uid,
            auto_import_exclusions=updated_exclusions,
        ):
            return
        self.statusBar().showMessage(
            f"{video_name} 영상을 목록에서 삭제했습니다. 원본 파일은 유지됩니다.",
            5000,
        )

    def select_case(self, uid: str) -> None:
        if uid == self.current_uid and self.current_case is not None:
            return
        if self.current_uid and not self._save(show_message=False):
            return
        self.current_uid = uid
        self.case_list.set_active_video(uid)
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
                self._sync_markers(previous_case)
            return
        self.current_uid = uid
        self._mark_case_dirty(target_case)
        self.case_list.update_case(target_case, active_uid=uid)
        self._show_current_case(open_video=False)

    def _show_current_case(self, *, open_video: bool) -> None:
        case = self.current_case
        if case is None:
            self.video_controller.close_video()
            self.video_player.clear()
            self.playback.clear_video()
            self.speed_panel.set_data([], SpeedEstimate(), 0.0)
            self.investigation.load_values({})
            self.investigation.case_title.setText("사고 없음")
            self.investigation.completion_badge.setObjectName("missingBadge")
            self.investigation.completion_badge.setText("미선택")
            self.investigation.completion_badge.style().unpolish(
                self.investigation.completion_badge
            )
            self.investigation.completion_badge.style().polish(self.investigation.completion_badge)
            return
        self.investigation.load_values(case.values)
        self._refresh_case_presentation(rebuild_list=False)
        self._sync_markers(case)
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
        self._opening_case = case
        try:
            return self.video_controller.open_video(path)
        finally:
            self._opening_case = None

    def _field_changed(self, key: str, value: Any) -> None:
        case = self.current_case
        if case is None:
            return
        case.set_value(key, value)
        value = self.repository.form_definition.canonical_values({key: value}).get(key)
        if key == "obstacle_exists" and value == "무":
            for detail_key in (
                "obstacle_vehicle_types",
                "obstacle_vehicle_state",
                "obstacle_pedestrians",
                "obstacle_road_facilities",
                "obstacle_building_landscape",
                "obstacle_other",
                "obstacle_state",
            ):
                case.values.pop(detail_key, None)
        if key == "incident_damage_unknown" and value == "확인불가":
            for count_key in (
                "incident_minor_injury",
                "incident_severe_injury",
                "incident_minor_injury_grades",
                "incident_minor_representative_injury_grade",
                "incident_severe_injury_grades",
                "incident_severe_representative_injury_grade",
                "incident_fatality",
                "incident_property_damage",
            ):
                case.values.pop(count_key, None)
        self._mark_case_dirty(case)
        self._refresh_case_presentation(rebuild_list=True)

    def _refresh_case_presentation(self, *, rebuild_list: bool) -> None:
        case = self.current_case
        if case is None:
            return
        missing = case.missing
        self.investigation.show_case(case.display_id, missing, case.status == "completed")
        self.investigation.complete_button.setEnabled(
            case.status != "completed" and not self._commit_in_progress
        )
        if rebuild_list:
            with timed_operation("case_row_update", count=1):
                self.case_list.update_case(case, active_uid=case.uid)

    def set_trigger_marker(self) -> None:
        case = self.current_case
        if case is None or not self.video_controller.is_open:
            return
        frame = self.video_controller.current_frame
        removing = case.trigger_frame == frame
        case.set_trigger(None if removing else frame)
        self._mark_case_dirty(case)
        self._sync_markers(case)
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
        self._mark_case_dirty(case)
        self._sync_markers(case)
        self._refresh_case_presentation(rebuild_list=True)
        if removing:
            self.statusBar().showMessage("충돌 마커를 해제했습니다.", 3000)
        else:
            self.statusBar().showMessage(
                f"충돌 시점을 {frame + 1:,}번 프레임에 지정했습니다.", 3000
            )

    def _sync_markers(self, case: InvestigationCase | None = None, *, fps: float | None = None) -> None:
        """Push T/C and speed points to the timeline and refresh the speed estimate."""

        case = case or self.current_case
        if case is None:
            self.playback.set_markers(None, None, [])
            self.speed_panel.set_data([], SpeedEstimate(), 0.0, context_key=None)
            self.speed_panel.add_button.setEnabled(False)
            return
        points = case.speed_points
        self.playback.set_markers(
            case.trigger_frame, case.collision_frame, [point.frame for point in points]
        )
        metadata = self.video_controller.metadata
        matching_video = metadata is not None and str(metadata.path) == case.active_video
        if fps is None:
            fps = metadata.reported_fps if matching_video else 0.0
        context = (str(self.repository.workspace), case.uid, case.active_video)
        self.speed_panel.set_data(points, estimate_speed(points, fps, case.trigger_frame), fps, context_key=context)
        self.speed_panel.add_button.setEnabled(matching_video)
        self.speed_panel.set_apply_available("trigger_vehicle_speed" in self.investigation.editors)

    def _set_speed_panel_visible(self, visible: bool) -> None:
        self.video_player.canvas.setMinimumHeight(140 if visible else 280)
        self.speed_scroll.setVisible(visible)

    def _show_speed_panel(self) -> None:
        if not self.playback.speed_panel_button.isChecked():
            self.playback.speed_panel_button.setChecked(True)

    def toggle_speed_point(self) -> None:
        case = self.current_case
        if case is None or not self.video_controller.is_open:
            return
        frame = self.video_controller.current_frame
        following = next((point for point in case.speed_points if point.frame > frame), None)
        added = case.toggle_speed_point(frame)
        self._mark_case_dirty(case)
        self._sync_markers(case)
        self._refresh_case_presentation(rebuild_list=True)
        if added:
            self._show_speed_panel()
            number = next(
                index for index, point in enumerate(case.speed_points, start=1) if point.frame == frame
            )
            message = f"속도 지점 {number}을 {frame + 1:,}번 프레임에 추가했습니다."
            timeout = 3000
            if following is not None and following.distance_m is not None:
                message += f" 기존 구간이 나뉘었으므로 {number + 1}번 지점의 거리를 다시 입력해 주세요."
                timeout = 8000
            self.statusBar().showMessage(message, timeout)
        else:
            self.statusBar().showMessage(f"{frame + 1:,}번 프레임의 속도 지점을 해제했습니다.", 3000)

    def remove_speed_point(self, frame: int) -> None:
        case = self.current_case
        if case is None or all(point.frame != frame for point in case.speed_points):
            return
        case.toggle_speed_point(frame)
        self._mark_case_dirty(case)
        self._sync_markers(case)
        self._refresh_case_presentation(rebuild_list=True)

    def clear_speed_points(self) -> None:
        case = self.current_case
        if case is None or not case.speed_points:
            return
        answer = QMessageBox.question(
            self,
            "속도 지점 삭제",
            f"현재 영상의 속도 지점 {len(case.speed_points)}개를 모두 삭제할까요?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        case.clear_speed_points()
        self._mark_case_dirty(case)
        self._sync_markers(case)
        self._refresh_case_presentation(rebuild_list=True)

    def set_speed_distance(self, frame: int, distance_m: float | None) -> None:
        case = self.current_case
        if case is None:
            return
        case.set_speed_distance(frame, distance_m)
        self._mark_case_dirty(case)
        self._sync_markers(case)
        self._refresh_case_presentation(rebuild_list=True)

    def apply_speed_label(self, label: str) -> None:
        case = self.current_case
        editor = self.investigation.editors.get("trigger_vehicle_speed")
        metadata = self.video_controller.metadata
        if case is None or editor is None or metadata is None or str(metadata.path) != case.active_video:
            return
        estimate = estimate_speed(case.speed_points, metadata.reported_fps, case.trigger_frame)
        if estimate.label != label:
            return
        canonical = self.repository.form_definition.canonical_values(case.values)
        old_source = canonical.get("trigger_vehicle_speed_source")
        if (old_source and old_source != "사고영상") or estimate.warnings or estimate.label_uncertain:
            message = (
                "거리·프레임으로 계산한 참고용 추정값을 적용할까요?\n"
                "기존 속도 구분을 변경하고 속도 출처를 ‘사고영상’으로 기록합니다."
            )
            if estimate.warnings:
                message += "\n경고가 있습니다. 측정 지점을 먼저 확인해 주세요."
            answer = QMessageBox.question(
                self, "속도 추정값 적용 확인",
                message,
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
        stored = self.repository.form_definition.stored_value("trigger_vehicle_speed", label)
        editor.set_value(stored)
        self._field_changed("trigger_vehicle_speed", stored)
        source_editor = self.investigation.editors.get("trigger_vehicle_speed_source")
        if source_editor is not None:
            source = self.repository.form_definition.stored_value("trigger_vehicle_speed_source", "사고영상")
            source_editor.set_value(source)
            self._field_changed("trigger_vehicle_speed_source", source)
        self.statusBar().showMessage(f"차량 속도 (추정)를 {label}으로 입력했습니다.", 4000)

    def save_draft(self) -> None:
        self._save(show_message=True)

    def _save(
        self,
        *,
        show_message: bool,
        auto_import_exclusions: set[str] | None = None,
    ) -> bool:
        if self._commit_in_progress:
            return False
        self._flush_deferred_reorders()
        if self._workspace_load_failed:
            self.statusBar().showMessage("읽기 오류가 없는 작업 폴더를 먼저 선택해 주세요.", 5000)
            return False
        current_uids = {case.uid for case in self.cases}
        changed_uids = self._dirty_cases | (current_uids - self._persisted_uids)
        if self._dirty and not changed_uids and not self._order_dirty and self.current_uid:
            changed_uids = {self.current_uid}
        deleted = self._persisted_uids - current_uids
        order_changed = self._order_dirty or current_uids != self._persisted_uids
        if not (
            changed_uids or deleted or order_changed
            or self.current_uid != self._saved_active_uid
            or auto_import_exclusions is not None
        ):
            if show_message:
                self.statusBar().showMessage("변경 사항이 없습니다. 모두 저장되어 있습니다.", 3000)
            return True
        try:
            revisions = {uid: self._case_revisions.get(uid, 0) for uid in changed_uids}
            saved_active_uid = self.current_uid
            snapshots = [self._copy_case(case) for case in self.cases if case.uid in changed_uids]
            self._disk_operation(self.repository.save_changes,
                snapshots,
                deleted_uids=list(deleted),
                order=[case.uid for case in self.cases] if order_changed else None,
                active_case_uid=self.current_uid,
                auto_import_exclusions=auto_import_exclusions,
                allow_edit=show_message,
            )
        except WorkspaceError as exc:
            self._flush_deferred_reorders()
            QMessageBox.critical(self, "저장 오류", str(exc))
            return False
        self._dirty_cases.difference_update(
            uid for uid, version in revisions.items()
            if self._case_revisions.get(uid, 0) == version
        )
        self._dirty = bool(self._dirty_cases)
        self._order_dirty = False
        self._persisted_uids = current_uids
        self._saved_active_uid = saved_active_uid
        self._flush_deferred_reorders()
        if not show_message and self._order_dirty:
            # A deferred drag must also be committed before closing/changing
            # workspaces; ordinary draft save can leave it visibly unsaved.
            return self._save(show_message=False)
        if show_message:
            message = "조사 내용을 임시저장했습니다."
            if self._dirty:
                message += " 저장 중 추가로 수정한 내용은 아직 미저장입니다."
            self.statusBar().showMessage(message, 5000)
        return True

    def complete_case(self) -> None:
        if self._commit_in_progress:
            return
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
            label_for = self.repository.form_definition.label
            messages = [f"• {label_for(key)}: {MISSING_VALUE_MESSAGE}" for key in missing]
            messages.extend(f"• {label_for(key)}: {message}" for key, message in errors.items())
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
        self._mark_case_dirty(case)
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
        self.case_list.set_active_video(target.uid)
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
        options = CsvExportDialog(Path(path), self)
        if options.exec() != QDialog.DialogCode.Accepted:
            return
        include_specification = options.include_specification.isChecked()
        try:
            destination = self._disk_operation(self.repository.export_csv,
                path, self.cases, include_specification=include_specification
            )
        except WorkspaceError as exc:
            QMessageBox.critical(self, "CSV 내보내기 오류", str(exc))
            return
        message = f"CSV를 저장했습니다: {destination}"
        if include_specification:
            message += f" · 데이터 명세서: {data_specification_path(destination).name}"
        self.statusBar().showMessage(message, 8000)

    def export_reports(self) -> None:
        """Export detached case snapshots without changing drafts or completion."""
        if self._commit_in_progress:
            return
        if not self.cases:
            QMessageBox.information(self, "사고 분석 보고서", "보고서로 만들 사고가 없습니다.")
            return
        selected = self.case_list.selected_case_uids()
        if not selected and self.current_uid:
            selected = [self.current_uid]
        options = ReportExportDialog(self.cases, selected, self)
        if options.exec() != QDialog.DialogCode.Accepted:
            return
        selected_uids = set(options.selected_uids())
        if not selected_uids:
            return
        folder = QFileDialog.getExistingDirectory(
            self,
            "사고 분석 보고서 저장 폴더 선택",
            str(self.repository.workspace),
            QFileDialog.Option.ShowDirsOnly,
        )
        if not folder:
            return
        snapshots = [self._copy_case(case) for case in self.cases if case.uid in selected_uids]
        if not snapshots:
            return
        self.statusBar().showMessage(f"사고 분석 보고서 {len(snapshots)}건을 생성하고 있습니다…")
        try:
            destinations = self._disk_operation(
                export_case_reports,
                snapshots,
                Path(folder),
                form_definition=self.repository.form_definition,
            )
        except (ReportExportError, OSError, ValueError) as exc:
            self.statusBar().showMessage("사고 분석 보고서 내보내기에 실패했습니다.", 5000)
            QMessageBox.critical(self, "사고 분석 보고서 내보내기 오류", str(exc))
            return
        message = f"사고 분석 보고서 {len(destinations)}건을 저장했습니다: {Path(folder)}"
        self.statusBar().showMessage(message, 10000)
        QMessageBox.information(self, "사고 분석 보고서 내보내기 완료", message)

    def backup_workspace(self) -> None:
        if not self._save(show_message=False):
            return
        suggested = self.repository.workspace / f"investigations_backup_{datetime.now():%Y%m%d_%H%M%S}.db"
        path, _ = QFileDialog.getSaveFileName(
            self, "조사 데이터 백업", str(suggested), "SQLite 파일 (*.db)"
        )
        if not path:
            return
        if not Path(path).suffix:
            path += ".db"
        try:
            destination = self._disk_operation(self.repository.backup, path)
        except WorkspaceError as exc:
            QMessageBox.critical(self, "백업 오류", str(exc))
            return
        self.statusBar().showMessage(f"조사 데이터를 백업했습니다: {destination}", 5000)

    def _video_opened(self, metadata: VideoMetadata) -> None:
        try:
            stat = metadata.path.stat()
            self._statistics_media_cache[str(metadata.path)] = (stat.st_size, stat.st_mtime_ns, metadata.reported_fps)
            self._invalidate_statistics()
        except OSError:
            pass
        self.video_player.set_video_name(metadata.path, (metadata.width, metadata.height))
        self.playback.set_video(metadata.total_frames, metadata.fps)
        case = self._opening_case or self.current_case
        self.video_player.set_rotation(case.video_rotation if case else 0)
        if case:
            self._sync_markers(case, fps=metadata.reported_fps)
        self.statusBar().showMessage(
            f"영상 열기 완료 · {metadata.fps:.2f} FPS · {metadata.total_frames:,} frames", 4000
        )

    def _video_closed(self) -> None:
        self.video_player.canvas.clear()
        self.video_player.set_rotation(0)
        self.playback.clear_video()
        self.speed_panel.set_data([], SpeedEstimate(), 0.0, context_key=None)
        self.speed_panel.add_button.setEnabled(False)

    def _frame_ready(self, image, frame: int, timestamp: float) -> None:  # type: ignore[no-untyped-def]
        del frame, timestamp
        self.video_player.canvas.set_frame(image)

    def rotate_video_clockwise(self) -> None:
        case = self.current_case
        if case is None or case.active_video is None or not self.video_controller.is_open:
            self.video_player.set_rotation(0)
            return
        degrees = (case.video_rotation + 90) % 360
        case.set_video_rotation(degrees)
        self.video_player.set_rotation(degrees)
        self._mark_case_dirty(case)
        direction = f"{degrees}°" if degrees else "원본 방향"
        self.statusBar().showMessage(f"영상 화면을 {direction}으로 표시합니다.", 3000)

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
        # Folder identity lives in the document tab; paths appear in its tooltip.
        self.statusBar().setToolTip("")
        self.statusBar().clearMessage()

    def _invalidate_statistics(self) -> None:
        self._statistics_dirty = True
        if hasattr(self, "mode_tabs") and self.mode_tabs.currentIndex() == 1:
            self._statistics_timer.start()

    def _change_workspace_view(self, index: int) -> None:
        if self._commit_in_progress or self._switching_project:
            blocked = self.mode_tabs.blockSignals(True)
            self.mode_tabs.setCurrentIndex(self.workspace_pages.currentIndex())
            self.mode_tabs.blockSignals(blocked)
            return
        self.workspace_pages.setCurrentIndex(index)
        if index == 1:
            if self.video_controller.is_playing:
                self.video_controller.pause()
            self._refresh_statistics()
            self._request_statistics_media()

    def _refresh_statistics(self) -> None:
        if self.mode_tabs.currentIndex() != 1:
            return
        if self._commit_in_progress or self._switching_project or self._video_import_in_progress:
            self._statistics_timer.start()
            return
        with timed_operation("statistics_refresh", count=len(self.cases)):
            self.statistics_panel.set_data(
                self.cases, self.repository.form_definition,
                {path: record[2] for path, record in self._statistics_media_cache.items() if record[2] > 0},
                self.repository.workspace,
            )
        self._statistics_dirty = False
        if any(case.active_video and case.active_video not in self._statistics_media_attempted for case in self.cases):
            self._request_statistics_media()

    def _open_statistics_case(self, uid: str, key: str = "") -> None:
        if self._commit_in_progress or not any(case.uid == uid for case in self.cases):
            return
        if not self._save(show_message=False):
            return
        self.mode_tabs.setCurrentIndex(0)
        self.case_list.search.clear()
        self.case_list.select_case(uid)
        self.select_case(uid)
        if key:
            key = {"site_location": "site_types", "obstacle_object_types": "obstacle_vehicle_types", "trigger_frame": ""}.get(key, key)
            if key:
                self.investigation.focus_field(key)

    def _request_statistics_media(self) -> None:
        if self._statistics_media_future is not None or self._workspace_load_failed:
            return
        paths = list(dict.fromkeys(case.active_video for case in self.cases if case.active_video))
        if not paths:
            return
        self._statistics_media_attempted.update(paths)
        self._statistics_media_cancel = Event()
        self._statistics_media_origin = self.repository.workspace
        self._statistics_media_future = self._statistics_media_worker.submit(
            probe_video_fps, paths, dict(self._statistics_media_cache), self._statistics_media_cancel,
        )
        self._statistics_media_timer.start()
        self.statusBar().showMessage("영상의 실제 FPS 정보를 확인하고 있습니다…")

    def _finish_statistics_media(self) -> None:
        future = self._statistics_media_future
        if future is None or not future.done():
            return
        self._statistics_media_timer.stop()
        self._statistics_media_future = None
        try:
            self._statistics_media_cache.update(future.result())
        except Exception:
            self.statusBar().showMessage("일부 영상 정보를 확인하지 못했습니다. 파일 상태를 확인해 주세요.", 5000)
        else:
            if self.repository.workspace == self._statistics_media_origin:
                self._invalidate_statistics()
                self.statusBar().showMessage("영상 통계 정보를 갱신했습니다.", 3000)
        if (self.repository.workspace != self._statistics_media_origin
                and self.mode_tabs.currentIndex() == 1):
            self._request_statistics_media()

    def _show_shortcuts(self) -> None:
        QMessageBox.information(
            self,
            "재생 단축키",
            "Space  재생 / 일시정지\n← / →  1프레임 이동\nCtrl+← / Ctrl+→  10프레임 이동\nJ / L  1초 이동\nM  속도 지점 추가 / 해제\nCtrl+S  임시저장",
        )

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if self.mode_tabs.currentIndex() != 0:
            super().keyPressEvent(event)
            return
        focus = QApplication.focusWidget()
        if isinstance(focus, (QLineEdit, QComboBox, QAbstractSpinBox, QTextEdit)):
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
            self._step_seconds(-1)
        elif key == Qt.Key.Key_L:
            self._step_seconds(1)
        elif key == Qt.Key.Key_M:
            self.toggle_speed_point()
        else:
            super().keyPressEvent(event)
            return
        event.accept()

    def closeEvent(self, event: QCloseEvent) -> None:
        if self._commit_in_progress or self._video_import_in_progress:
            event.ignore()
            self.statusBar().showMessage("저장 작업이 끝난 뒤 종료해 주세요.", 3000)
            return
        if not self._workspace_load_failed and not self._save(show_message=False):
            event.ignore()
            return
        self._project_tabs_timer.stop()
        self._save_project_tabs()
        self.video_controller.close_video()
        self._statistics_timer.stop()
        self._statistics_media_timer.stop()
        self._statistics_media_cancel.set()
        try:
            if self._statistics_media_future is not None:
                self._statistics_media_worker.wait(self._statistics_media_future)
        except Exception:
            # Optional metadata work must not prevent saving and closing the app.
            pass
        finally:
            self._statistics_media_future = None
            self._statistics_media_worker.shutdown()
        self._watch_refresh_timer.stop()
        self.folder_watcher.shutdown()
        self._storage_worker.shutdown()
        event.accept()


__all__ = ["MainWindow", "default_workspace"]
