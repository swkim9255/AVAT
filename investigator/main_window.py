"""Top-level three-column investigation workflow."""

from __future__ import annotations

import logging
import os
import re
from datetime import datetime
from pathlib import Path
from threading import Event
from typing import Any
from uuid import uuid4

from PySide6.QtCore import QDir, QLockFile, QStandardPaths, Qt, QTimer
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
    QTextEdit,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from investigator.case_numbering import build_case_number_map, case_number_sort_key
from investigator.detection_controller import DetectionMixin
from investigator.diagnostics import timed_operation
from investigator.folder_watcher import ProjectFolderWatcher
from investigator.form_definition import FormDefinition
from investigator.models import DEFAULT_INSTITUTION, InvestigationCase, new_case
from investigator.project_session import ProjectSession
from investigator.project_tabs_state import ProjectTabsState, ProjectTabsStore
from investigator.report_export import ReportExportError, export_case_reports
from investigator.runtime_paths import application_root, is_frozen
from investigator.schema import (
    INVALID_FORMAT_MESSAGE,
    MISSING_VALUE_MESSAGE,
)
from investigator.speed_csv import speed_csv_path
from investigator.speed_estimation import SpeedEstimate, estimate_speed
from investigator.statistics_media import probe_video_fps
from investigator.storage import (
    CSV_EXPORT_DIRECTORY,
    DATABASE_DIRECTORY,
    REPORT_EXPORT_DIRECTORY,
    CaseRepository,
    WorkspaceError,
    data_specification_path,
)
from investigator.user_data import migrate_workspace_user_data
from investigator.storage_worker import StorageWorker
from investigator.theme import PRIMARY, material_icon
from investigator.video_controller import PlaybackState, VideoController, VideoMetadata
from investigator.video_copy import CopiedVideos, copy_videos
from investigator.video_delete_plan import plan_video_deletion
from investigator.video_folders import (
    UNLINKED_FOLDER_KEY,
    VideoFolder,
    VideoFolderIndex,
    discover_video_folders,
    video_root,
)
from investigator.video_import import (
    VIDEO_EXTENSIONS,
    VideoFolderScan,
    parse_accident_video_name,
    scan_video_files,
    scan_video_folder,
    scan_video_tree,
    video_path_key,
)
from investigator.video_relink import (
    apply_workspace_video_path_mapping,
    build_auto_relink_plan,
    remap_video_path_list,
)
from investigator.video_trash import QuarantinedVideos, QuarantineRollbackError, quarantine_videos
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
from investigator.widgets.video_tools_splitter import VideoToolsSplitter
from investigator.widgets.workspace_menu import ProjectTabBar, ViewModeSwitch, WorkspaceMenuBar


WORKSPACE_LOCK_FILENAME = "작업관리.lock"


class WorkspaceLockedError(WorkspaceError):
    """다른 AVAT 창이 같은 작업 폴더를 사용 중이다."""


def default_workspace() -> Path:
    if is_frozen():
        return application_root()
    documents = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.DocumentsLocation)
    base = Path(documents) if documents else Path.cwd()
    return base / "사고영상조사"


def _video_number_sort_key(path: str) -> tuple:
    """Sort numbered clips naturally (1, 2, 10), without renaming files."""
    def natural(value: str) -> tuple:
        return tuple(
            (1, int(part)) if part.isdecimal() else (0, part.casefold())
            for part in re.split(r"([0-9]+)", value)
        )

    parsed = parse_accident_video_name(path)
    return (
        0 if parsed else 1,
        int(parsed[0]) if parsed else 0,
        parsed[1] if parsed else 0,
        natural(Path(path).name),
        natural(path),
    )


class MainWindow(DetectionMixin, QMainWindow):
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
            self._project_tabs_warning = "저장된 작업 폴더 설정을 읽지 못했습니다. 작업 폴더를 다시 열어 주세요."
        selected = (Path(workspace).expanduser().resolve() if workspace is not None
                    else default_workspace().resolve() if is_frozen() else tabs_state.active_folder)
        if selected is None:
            selected = default_workspace().resolve()
        self.repository = CaseRepository(selected)
        self._session = ProjectSession(self.repository, restored=workspace is None and tabs_state.active_folder is not None)
        self._video_folders: list[VideoFolder] = []
        self._folder_index = VideoFolderIndex()
        # 마지막 전체 스캔에서 본 영상과 누락 영상(새로고침 안내·감시 시작에 재사용).
        self._last_scan_video_paths: set[str] = set()
        self._last_missing_paths: set[str] = set()
        # 작업 폴더별 중복 실행 방지 잠금과, 자동 복원 실패 시 백업 덮어쓰기 보호.
        self._workspace_locks: dict[Path, QLockFile] = {}
        self._latest_backup_protected = False
        restore_video_state = selected == tabs_state.active_folder or (is_frozen() and workspace is None)
        self._active_video_folder: str | None = tabs_state.active_video_folder if restore_video_state else None
        self._video_folder_order = list(tabs_state.video_folder_order) if restore_video_state else []
        self._folder_view_states: dict[str, dict] = {}
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
        self._project_discovery_in_progress = False
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
        # 영상 FPS 확인 결과는 작업 폴더 DB에 저장해 다음 실행·CSV 내보내기에서 재사용한다.
        self._fps_cache_workspace: Path | None = None
        self._fps_pending: dict[str, tuple[int, int, float]] = {}
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
        self._init_detection()
        self._build_layout()
        self._build_menu()
        self._connect_signals()
        self._load_workspace()
        self._sync_project_tabs()
        project = self._session
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
        if hasattr(self, "_session"):
            project = self._session
            project.cases = self.cases
            project.current_uid = active_uid
        self._invalidate_statistics()

    def _save_candidates(
        self, candidates: list[InvestigationCase], active_uid: str | None,
        *, auto_import_exclusions: set[str] | None = None, prepare_off_thread: bool = False,
    ) -> None:
        """Persist only detached/new/edited cases and structural metadata."""
        previous = {case.uid: case for case in self.cases}
        candidate_uids = {case.uid for case in candidates}
        deleted = previous.keys() - candidate_uids
        changes = [
            case for case in candidates
            if previous.get(case.uid) is not case or case.uid in self._dirty_cases
        ]
        # 기존 사고의 상대 순서가 그대로이고 새 사고가 뒤에 붙기만 하면 DB는 새 행을
        # 끝 순서로 추가한다. 이때 전체 순서(수만 건 UPDATE)를 다시 쓰지 않는다.
        survivors = [case.uid for case in self.cases if case.uid not in deleted]
        candidate_order = [case.uid for case in candidates]
        order_needed = self._order_dirty or candidate_order[:len(survivors)] != survivors

        def prepare():
            return [self._copy_case(case) for case in changes]

        snapshots = self._disk_operation(prepare) if prepare_off_thread else prepare()
        self._disk_operation(
            self.repository.save_changes, snapshots,
            deleted_uids=list(deleted),
            order=candidate_order if order_needed else None, active_case_uid=active_uid,
            auto_import_exclusions=auto_import_exclusions,
        )
        # Configured defaults are applied only to genuinely new cases.
        saved_by_uid = {case.uid: case for case in snapshots}
        for index, case in enumerate(candidates):
            if case.uid in saved_by_uid:
                candidates[index] = saved_by_uid[case.uid]
        # 방금 저장한 사고의 검출 결과는 메모리에서 내리고 필요할 때 DB에서 다시 읽는다.
        self.repository.release_case_objects(snapshots)

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
        self.video_folder_tabs = self.project_tabs
        self.project_tabs.setAccessibleName("영상 폴더 탭")
        self.project_tabs.setExpanding(False)
        self.project_tabs.setUsesScrollButtons(True)
        self.project_tabs.setElideMode(Qt.TextElideMode.ElideMiddle)
        self.project_tabs.setTabsClosable(False)
        self.project_tabs.setMovable(True)
        self.project_tabs.setAccessibleDescription("선택한 영상 폴더의 사고와 영상을 표시합니다. 탭을 드래그하여 순서를 변경할 수 있습니다.")
        self.project_add_button = QToolButton(root_widget)
        self.project_add_button.setIcon(material_icon("folder_open", PRIMARY))
        self.project_add_button.setAccessibleName("작업 폴더 열기")
        self.project_add_button.setToolTip("작업 폴더 열기 · 현재 작업 폴더를 교체합니다.")
        self.project_add_button.setFixedSize(34, 34)
        self.project_add_button.setStyleSheet("padding: 0;")
        self.view_mode = ViewModeSwitch(root_widget)
        self.mode_tabs = self.view_mode  # Preserve index-based view routing.
        self.splitter = QSplitter(Qt.Orientation.Horizontal, root_widget)
        self.splitter.setObjectName("mainSplitter")
        self.splitter.setHandleWidth(8)
        self.splitter.setChildrenCollapsible(False)
        self.splitter.addWidget(self.case_list)
        self.video_workspace = QWidget(self.splitter)
        self.video_workspace.setObjectName("videoWorkspace")
        video_layout = QVBoxLayout(self.video_workspace)
        video_layout.setContentsMargins(0, 0, 0, 0)
        video_layout.setSpacing(1)
        self.video_tool_splitter = VideoToolsSplitter(self.video_workspace)
        video_layout.addWidget(self.video_tool_splitter, 1)
        self.video_playback_area = QWidget(self.video_workspace)
        playback_layout = QVBoxLayout(self.video_playback_area)
        playback_layout.setContentsMargins(0, 0, 0, 0)
        playback_layout.setSpacing(1)
        playback_layout.addWidget(self.video_player, 1)
        playback_layout.addWidget(self.playback, 0)
        self.video_tool_splitter.add_video_area(self.video_playback_area)
        self.speed_scroll = QScrollArea(self.video_workspace)
        self.speed_scroll.setObjectName("speedScroll")
        self.speed_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.speed_scroll.setWidgetResizable(True)
        self.speed_scroll.setMinimumWidth(0)
        self.speed_scroll.setMinimumHeight(140)
        self.speed_scroll.setWidget(self.speed_panel)
        self.speed_scroll.hide()
        self.video_tool_splitter.add_tool_panel("speed", self.speed_scroll, default_height=300)
        self._build_detection_area(self.video_workspace, video_layout)
        video_layout.removeWidget(self.detection_scroll)
        self.detection_scroll.setMaximumHeight(16777215)
        self.video_tool_splitter.add_tool_panel("detection", self.detection_scroll, default_height=340)
        self.splitter.addWidget(self.video_workspace)
        self.splitter.addWidget(self.investigation)
        for index, title in ((1, "사고 목록 너비 조절"), (2, "조사 입력 너비 조절")):
            handle = self.splitter.handle(index)
            handle.setCursor(Qt.CursorShape.SplitHCursor)
            handle.setAccessibleName(title)
            handle.setToolTip("드래그하여 " + title)
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
        workspace_action = QAction(material_icon("folder_open", PRIMARY), "작업 폴더 열기…", self)
        workspace_action.setShortcut(QKeySequence("Ctrl+O"))
        workspace_action.triggered.connect(self.choose_workspace)
        file_menu.addAction(workspace_action)

        file_menu.addSeparator()

        self.refresh_list_action = QAction(material_icon("refresh", PRIMARY), "목록 새로고침", self)
        self.refresh_list_action.setShortcut(QKeySequence("Ctrl+R"))
        self.refresh_list_action.triggered.connect(self.refresh_video_list)
        file_menu.addAction(self.refresh_list_action)

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
        # 앱 업데이트·재설치 후 백업 파일로 조사 데이터를 되살린다.
        self.restore_action = QAction("조사 데이터 불러오기…", self)
        self.restore_action.triggered.connect(self.restore_workspace)
        file_menu.addAction(self.restore_action)
        file_menu.addSeparator()
        exit_action = QAction("종료", self)
        exit_action.setShortcut(QKeySequence.StandardKey.Quit)
        exit_action.triggered.connect(self.close)
        file_menu.addAction(exit_action)

        settings_menu = self.menuBar().addMenu("설정")
        self.edit_form_action = QAction("조사 항목 편집…", self)
        self.edit_form_action.triggered.connect(self.edit_form)
        settings_menu.addAction(self.edit_form_action)
        self.detection_settings_action = QAction("객체 검출 환경…", self)
        self.detection_settings_action.triggered.connect(self.edit_detection_settings)
        settings_menu.addAction(self.detection_settings_action)

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
        self.project_tabs.currentChanged.connect(self.switch_video_folder)
        self.project_add_button.clicked.connect(self.choose_workspace)
        self.case_list.case_selected.connect(self.select_case)
        self.case_list.video_selected.connect(self.select_video)
        self.case_list.refresh_requested.connect(self.refresh_video_list)
        self.case_list.add_video_requested.connect(self.choose_videos)
        self.case_list.delete_case_requested.connect(self.delete_case)
        self.case_list.delete_video_requested.connect(self.delete_video)
        self.case_list.case_reordered.connect(self.reorder_case)
        self.case_list.cases_reordered.connect(self.reorder_cases)
        self.case_list.sort_ascending_requested.connect(self.sort_cases_ascending)
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
        self._connect_detection_signals()
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
        self.case_list.set_workspace(self.repository.workspace)
        restored_from_backup = False
        try:
            project = self._session
            # 같은 작업 폴더를 두 AVAT 창이 동시에 편집하지 못하게 먼저 잠근다.
            self._acquire_workspace_lock()
            if self.repository.workspace.is_dir():
                # 이전 배치(systems/backups, systems/삭제사고)를 사용자데이터 폴더로 옮긴다.
                self._disk_operation(migrate_workspace_user_data, self.repository.workspace)
                restored_from_backup = self._restore_latest_backup_if_missing()
            self.cases = (
                self._disk_operation(self.repository.load, require_existing=True) if project.restored
                else self._disk_operation(self.repository.load)
            )
            active_uid = self.repository.active_case_uid()
            self._mark_saved(active_uid)
            self._install_form(self.repository.form_definition)
        except WorkspaceError as exc:
            if isinstance(exc, WorkspaceLockedError):
                QMessageBox.warning(self, "작업 폴더 사용 중", str(exc))
            elif not self._session.restored:
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
                f"{exc}\n원본 데이터는 변경하지 않았습니다. 작업 폴더를 다시 열거나 "
                "파일 → 조사 데이터 불러오기…로 백업을 불러와 주세요.", 0
            )
            return False
        if active_uid not in {case.uid for case in self.cases}:
            active_uid = self.cases[0].uid if self.cases else None
        self.current_uid = active_uid
        # 경로 복구·새 영상 반영 전에는 목록 행을 만들지 않는다. 폴더 탭은 복구된
        # 경로 기준으로 정해야 하므로, 동기화가 끝난 뒤 현재 폴더 사고로 한 번만 만든다.
        self.case_list.set_video_filter(
            lambda path: False, scope_key=("__loading__", self.repository.workspace), rebuild=False,
        )
        self.case_list.set_cases(self.cases, active_uid, selected_video_index=None)
        # Clear the previous project's transient status before an import summary.
        self._update_workspace_status()
        # Repair old absolute links before attempting to decode a missing file.
        self._synchronize_project_videos(open_video=False)
        self._sync_project_tabs()
        self._apply_video_folder_scope()
        self._show_current_case(open_video=True)
        self._set_workflow_enabled(True)
        if restored_from_backup:
            self.statusBar().showMessage(
                f"조사 데이터 DB가 없어 최신 백업 파일에서 사고 {len(self.cases):,}건을 복원했습니다: "
                f"{self.repository.latest_backup_path.name}", 20000,
            )
        return True

    def _restore_latest_backup_if_missing(self) -> bool:
        """재설치 후처럼 DB가 없고 최신 백업이 있으면 자동으로 복원한다.

        백업을 읽지 못하면 백업 파일을 그대로 두고, 이번 실행 동안은 자동 백업이
        그 파일을 빈 데이터로 덮어쓰지 않도록 보호한다.
        """
        try:
            return bool(self._disk_operation(self.repository.restore_latest_if_missing))
        except WorkspaceError as exc:
            self._latest_backup_protected = True
            QMessageBox.warning(
                self, "백업 자동 복원 실패",
                f"{exc}\n\n백업 파일은 그대로 두었습니다. "
                "파일 → 조사 데이터 불러오기…로 다시 시도할 수 있습니다.",
            )
            return False

    def _acquire_workspace_lock(self) -> None:
        """같은 작업 폴더를 두 AVAT 창이 동시에 편집하지 못하게 한다."""
        workspace = self.repository.workspace
        if workspace in self._workspace_locks or not workspace.is_dir():
            return
        directory = workspace / DATABASE_DIRECTORY
        try:
            directory.mkdir(parents=True, exist_ok=True)
        except OSError:
            return  # 폴더를 만들 수 없으면 이어지는 로드가 원인을 안내한다.
        lock = QLockFile(str(directory / WORKSPACE_LOCK_FILENAME))
        # 오래 켜 둔 창의 잠금을 시간만으로 '버려진 잠금'으로 보지 않는다.
        # (잠근 프로세스가 끝났는지로만 판단한다.)
        lock.setStaleLockTime(0)
        if lock.tryLock(0):
            self._workspace_locks[workspace] = lock
            return
        if lock.error() == QLockFile.LockError.LockFailedError:
            # Qt는 같은 PC에서 끝난 프로세스의 잠금만 스스로 정리한다. 폴더째 다른 PC에서
            # 복사돼 온 잠금이나 내용이 비거나 깨진 잠금은 영구히 남으므로 여기서 정리한다.
            # Windows에서는 잠금을 가진 프로세스가 잠금 파일을 연 채로 두어 지울 수 없으므로,
            # 실제로 사용 중인 잠금은 지워지지 않고 아래 안내로 이어진다.
            owner = lock.getLockInfo()
            if lock.removeStaleLockFile() and lock.tryLock(0):
                logging.getLogger(__name__).warning("stale_workspace_lock_removed owner=%r", owner)
                self._workspace_locks[workspace] = lock
                return
            raise WorkspaceLockedError(
                "다른 AVAT 창에서 이 작업 폴더를 사용 중입니다.\n"
                "이미 열린 창을 사용하거나, 그 창을 닫은 뒤 다시 실행해 주세요.\n"
                "열린 창이 없는데도 이 안내가 나오면 작업 관리자에서 남아 있는 AVAT.exe를 종료해 주세요."
            )
        # 권한 문제 등으로 잠금 파일을 만들 수 없으면 사용을 막지 않는다.

    def _release_workspace_locks(self, *, keep: Path | None = None) -> None:
        for workspace in [path for path in self._workspace_locks if path != keep]:
            self._workspace_locks.pop(workspace).unlock()

    def _backup_latest_before_change(self) -> Path | None:
        """삭제·경로 복구처럼 되돌리기 어려운 작업 전에 최신 백업 1개를 갱신한다."""
        if self._latest_backup_protected:
            return None
        return self._disk_operation(self.repository.backup_latest)

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
            # 적용할 새 양식은 이 복사본으로 고정되므로 전 사고 재검증용 캐시를 켠다.
            definition = FormDefinition.from_dict(definition.to_dict()).seal()
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
        # 양식 적용은 모든 사고를 복사·저장하므로, 저장 직후 검출 결과를 다시 지연 상태로 내린다.
        self.repository.release_case_objects(candidates)
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
        self.case_list.refresh_button.setEnabled(enabled)
        self.refresh_list_action.setEnabled(enabled)
        self.case_list.delete_button.setEnabled(
            enabled and self.case_list.tree.currentItem() is not None
        )
        self.case_list.export_button.setEnabled(enabled and bool(self.cases))
        self.case_list.report_export_button.setEnabled(enabled and bool(self.cases))
        self.case_list.sort_button.setEnabled(enabled and bool(self.cases))
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
            "작업 폴더 선택",
            str(self.repository.workspace),
            QFileDialog.Option.ShowDirsOnly,
        )
        if not selected:
            return
        self.open_project(selected)

    def _sync_project_tabs(self) -> None:
        """Refresh video-folder tabs, never create another project/database."""
        try:
            folders = self._disk_operation(discover_video_folders, self.repository.workspace)
        except OSError as exc:
            self.statusBar().showMessage(f"영상 폴더를 읽지 못했습니다: {exc}", 6000)
            return
        # 경로→폴더 판정은 색인에 캐시해 사고 수만큼의 Path 연산을 피한다.
        # 폴더 구성이 그대로이면 기존 색인(캐시 포함)을 재사용한다.
        same_folders = set(folders) == {
            folder for folder in self._video_folders if folder.key != UNLINKED_FOLDER_KEY
        }
        folder_index = self._folder_index if same_folders else VideoFolderIndex(folders)
        if any(not case.videos or any(folder_index.key_for(path) is None for path in case.videos)
               for case in self.cases):
            folders.append(VideoFolder(UNLINKED_FOLDER_KEY, "미연결 / 기타 영상", self.repository.workspace))
        order = {key: index for index, key in enumerate(self._video_folder_order)}
        folders.sort(key=lambda folder: (order.get(folder.key, len(order)), folder.key.casefold()))
        self._video_folders = folders
        self._folder_index = folder_index
        self._video_folder_order = [folder.key for folder in folders]
        if self._active_video_folder not in self._video_folder_order:
            current = self.current_case
            self._active_video_folder = next(
                (folder.key for folder in folders if current and current.active_video
                 and self._folder_contains(folder, current.active_video)),
                folders[0].key if folders else None,
            )
        blocked = self.project_tabs.blockSignals(True)
        try:
            while self.project_tabs.count():
                self.project_tabs.removeTab(0)
            self.project_tabs.setTabsClosable(False)
            for index, folder in enumerate(folders):
                self.project_tabs.addTab(folder.label)
                self.project_tabs.setTabData(index, folder.key)
                self.project_tabs.setTabIcon(index, material_icon("folder_open", PRIMARY))
                self.project_tabs.setTabToolTip(index, str(folder.path))
            self.project_tabs.setCurrentIndex(next(
                (i for i, folder in enumerate(folders) if folder.key == self._active_video_folder), -1,
            ))
        finally:
            self.project_tabs.blockSignals(blocked)

    def _folder_contains(self, folder: VideoFolder, path: str) -> bool:
        return self._folder_index.contains(folder, path)

    def _folder_cases(self) -> list[InvestigationCase]:
        folder = next((item for item in self._video_folders if item.key == self._active_video_folder), None)
        if folder is None:
            return []
        return [case for case in self.cases if (not case.videos and folder.key == UNLINKED_FOLDER_KEY)
                or any(self._folder_contains(folder, path) for path in case.videos)]

    def _apply_video_folder_scope(self, *, rebuild_cases: bool = False) -> None:
        folder = next((item for item in self._video_folders if item.key == self._active_video_folder), None)
        predicate = (lambda path: self._folder_contains(folder, path)) if folder else (lambda path: False)
        show_empty = folder is not None and folder.key == UNLINKED_FOLDER_KEY
        scope_key = (self.repository.workspace, folder.key if folder else None)
        if rebuild_cases:
            # 사고 목록 자체가 바뀐 경우: 범위를 먼저 정하고 현재 폴더 사고로 한 번만 만든다.
            self.case_list.set_video_filter(
                predicate, show_empty=show_empty, scope_key=scope_key, rebuild=False,
            )
            self.case_list.set_cases(self.cases, self.current_uid, selected_video_index=None)
        else:
            self.case_list.set_video_filter(predicate, show_empty=show_empty, scope_key=scope_key)
        candidates = self._folder_cases()
        previous = self.current_case
        current = next((case for case in candidates if case.uid == self.current_uid), None)
        if current is None:
            current = candidates[0] if candidates else None
        self.current_uid = current.uid if current else None
        if current:
            if current.videos and not self._folder_contains(folder, current.active_video):
                current.active_video_index = next(
                    i for i, path in enumerate(current.videos) if self._folder_contains(folder, path)
                )
                self._mark_case_dirty(current)
            self.case_list.set_active_video(current.uid)
            if current is not previous:
                self.case_list.select_case(current.uid)
        self._set_workflow_enabled(not self._workspace_load_failed)

    def _focus_case_folder(self, case: InvestigationCase | None) -> None:
        """Reveal a manually attached/drilled-down case without duplicating it."""
        if case is None:
            return
        folder = next((item for item in self._video_folders if (
            case.active_video and self._folder_contains(item, case.active_video)
        ) or (not case.videos and item.key == UNLINKED_FOLDER_KEY)), None)
        if folder is not None:
            self._active_video_folder = folder.key
            blocked = self.project_tabs.blockSignals(True)
            self.project_tabs.setCurrentIndex(self._video_folders.index(folder))
            self.project_tabs.blockSignals(blocked)
        self._apply_video_folder_scope()

    def switch_video_folder(self, index: int) -> bool:
        if not 0 <= index < self.project_tabs.count():
            return False
        target = self.project_tabs.tabData(index)
        if target == self._active_video_folder:
            return True
        def restore_tab():
            blocked = self.project_tabs.blockSignals(True)
            self.project_tabs.setCurrentIndex(next(
                (i for i in range(self.project_tabs.count())
                 if self.project_tabs.tabData(i) == self._active_video_folder), -1,
            ))
            self.project_tabs.blockSignals(blocked)
        if (self._commit_in_progress or self._switching_project or self._video_import_in_progress
                or self._project_discovery_in_progress or self._workspace_load_failed):
            restore_tab()
            return False
        if not self._confirm_stop_detection() or not self._save(show_message=False):
            restore_tab()
            return False
        if self._active_video_folder:
            self._folder_view_states[self._active_video_folder] = {
                "list": self.case_list.capture_view_state(), "uid": self.current_uid,
                "path": self.current_case.active_video if self.current_case else None,
                "frame": self.video_controller.requested_frame,
            }
        self.video_controller.close_video()
        self._active_video_folder = target
        blocked = self.project_tabs.blockSignals(True)
        self.project_tabs.setCurrentIndex(index)
        self.project_tabs.blockSignals(blocked)
        state = self._folder_view_states.get(target, {})
        self.current_uid = state.get("uid")
        self._apply_video_folder_scope()
        if state:
            self.case_list.restore_view_state(state["list"])
        else:
            self.case_list.clear_search()
        self._show_current_case(open_video=True)
        if self.video_controller.is_open and self.current_case and self.current_case.active_video == state.get("path"):
            self.video_controller.seek(max(0, state.get("frame", 0)))
        self._project_tabs_timer.start()
        return True

    def _move_project_tab(self, source: int, destination: int) -> None:
        """Reorder folder views without reloading the DB or touching playback."""
        if source == destination or not (0 <= source < len(self._video_folders) and 0 <= destination < len(self._video_folders)):
            return
        if self._commit_in_progress or self._switching_project or self._video_import_in_progress:
            blocked = self.project_tabs.blockSignals(True)
            try:
                self.project_tabs.moveTab(destination, source)
            finally:
                self.project_tabs.blockSignals(blocked)
            return
        by_key = {folder.key: folder for folder in self._video_folders}
        self._video_folders = [by_key[self.project_tabs.tabData(i)] for i in range(self.project_tabs.count())]
        self._video_folder_order = [folder.key for folder in self._video_folders]
        self._project_tabs_timer.start()
        self.statusBar().showMessage("영상 폴더 탭 순서를 변경했습니다.", 3000)

    def _save_project_tabs(self) -> None:
        if self._commit_in_progress or self._switching_project or self._video_import_in_progress:
            self._project_tabs_timer.start()
            return
        state = ProjectTabsState(
            (self.repository.workspace,), self.repository.workspace,
            tuple(self._video_folder_order), self._active_video_folder,
        )
        try:
            self._project_tabs_store.save(state)
        except (OSError, ValueError, RuntimeError):
            self.statusBar().showMessage("작업 폴더 설정을 저장하지 못했습니다. 설정 폴더의 쓰기 권한을 확인해 주세요. 사고 데이터 저장과는 별개입니다.", 12000)

    def _capture_project(self) -> None:
        project = self._session
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
        if not self._confirm_stop_detection():
            return False
        if not self._workspace_load_failed and not self._save(show_message=False):
            return False
        self._flush_video_metadata()
        self._refresh_latest_backup_quietly()
        self._capture_project()
        self.video_controller.close_video()
        return True

    def _refresh_latest_backup_quietly(self, *, closing: bool = False) -> None:
        """작업 폴더를 떠나거나 종료할 때 바뀐 데이터가 있으면 최신 백업 1개를 갱신한다.

        재설치·업데이트 후 이 파일로 복원할 수 있다. 실패해도 종료를 막지 않는다.
        종료 중에는 중첩 이벤트 루프(타이머·신호 처리)를 돌리지 않도록 바로 실행한다.
        이때는 디스크 작업 스레드에 진행 중인 작업이 없다(_commit_in_progress 확인 후).
        """
        if (self._workspace_load_failed or self._latest_backup_protected
                or self._storage_worker.closed):
            return  # 종료 처리가 다시 불린 경우 등에는 할 일이 없다.
        try:
            if closing:
                self.repository.backup_latest(only_if_changed=True)
            else:
                self._disk_operation(self.repository.backup_latest, only_if_changed=True)
        except WorkspaceError:
            logging.getLogger(__name__).warning("latest_backup_refresh_failed", exc_info=True)

    def _activate_workspace(self, project: ProjectSession) -> bool:
        self._session = project
        self.repository = project.repository
        self.case_list.set_workspace(self.repository.workspace)
        self.cases = project.cases
        self.current_uid = project.current_uid
        self._workspace_load_failed = project.load_failed
        self._case_revisions.clear()
        self._deferred_reorders.clear()
        self._mark_saved(self.current_uid)
        self.statistics_panel.restore_state(project.statistics_state)
        self._sync_project_tabs()
        self.case_list.set_cases([])
        self.case_list.clear_search()
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
        # 범위를 먼저 정하고 현재 폴더 사고만으로 목록을 만든 뒤 보기 상태를 복원한다.
        self._apply_video_folder_scope(rebuild_cases=True)
        self.case_list.restore_view_state(project.list_state)
        self._synchronize_project_videos(open_video=False)
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

    def open_project(self, folder: str | Path, *, new_tab: bool = False) -> bool:
        """Replace the one workspace. ``new_tab`` is ignored for legacy callers."""
        if self._commit_in_progress or self._switching_project or self._video_import_in_progress:
            return False
        path = Path(folder).expanduser().resolve()
        if not path.is_dir():
            QMessageBox.warning(self, "작업 폴더", "존재하는 폴더를 선택해 주세요.")
            return False
        if self.repository.workspace == path and not self._workspace_load_failed:
            return self.refresh_video_list()
        self._switching_project = True
        previous_project = self._session
        previous_folders = (self._active_video_folder, list(self._video_folder_order), dict(self._folder_view_states))
        previous_protected = self._latest_backup_protected
        try:
            if not self._leave_project():
                return False
            project = ProjectSession(CaseRepository(path))
            self._active_video_folder = None
            self._video_folder_order = []
            self._folder_view_states = {}
            self._latest_backup_protected = False
            # 이전 프로젝트 사고로 목록을 다시 만들지 않고 범위만 초기화한다.
            self.case_list.set_video_filter(None, rebuild=False)
            self.folder_watcher.remove_directory(previous_project.repository.workspace)
            if self._activate_workspace(project):
                # 새 작업 폴더를 연 뒤에만 이전 작업 폴더의 중복 실행 잠금을 푼다.
                self._release_workspace_locks(keep=self.repository.workspace)
                return True
            self._active_video_folder, self._video_folder_order, self._folder_view_states = previous_folders
            self._latest_backup_protected = previous_protected
            self._activate_workspace(previous_project)
            self._release_workspace_locks(keep=previous_project.repository.workspace)
            self.statusBar().showMessage("작업 폴더를 열지 못해 이전 작업 폴더를 유지했습니다.", 5000)
            return False
        finally:
            self._switching_project = False
            self._project_tabs_timer.start()

    def create_case(self) -> None:
        """Compatibility helper for integrations; manual creation has no UI entry point."""
        if not self._save(show_message=False):
            return
        candidates = list(self.cases)
        case = new_case(candidates)
        candidates.append(case)
        if not self._replace_cases_atomically(candidates, case.uid):
            return
        self.statusBar().showMessage(f"{case.display_id} 사고를 만들었습니다.", 3000)

    def refresh_video_list(self) -> bool:
        """Rescan the project without creating a manual accident or resetting playback."""
        if (self._commit_in_progress or self._switching_project or self._video_import_in_progress
                or self._project_discovery_in_progress or self._workspace_load_failed):
            return False
        if self._detection_job is not None:
            self.statusBar().showMessage("객체 검출을 마친 뒤 목록을 새로고침해 주세요.", 5000)
            return False
        self.statusBar().showMessage("영상 목록을 새로고침하고 있습니다…")
        success = self._synchronize_project_videos()
        if success:
            self._watch_active_project()
            # 방금 동기화에서 확인한 누락 목록을 쓴다(전체 영상 존재 확인 반복 방지).
            if not self._last_missing_paths:
                self.statusBar().showMessage(
                    f"목록 새로고침 완료 · 사고 {len(self.cases):,}건 · 영상 {sum(len(case.videos) for case in self.cases):,}개",
                    5000,
                )
        return success

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

    def sort_cases_ascending(self) -> None:
        """Save numeric display order without reopening playback or editing IDs."""
        if self._commit_in_progress or self._switching_project or self._video_import_in_progress:
            self.statusBar().showMessage("현재 작업이 끝난 뒤 정렬해 주세요.", 4000)
            return
        if self._workspace_load_failed:
            self.statusBar().showMessage("읽기 오류가 없는 작업 폴더를 먼저 선택해 주세요.", 5000)
            return
        if not self.cases:
            return

        state = self.case_list.capture_view_state()
        previous_videos = {case.uid: tuple(case.videos) for case in self.cases}
        reordered = sorted(self.cases, key=case_number_sort_key)
        changed = [case.uid for case in reordered] != [case.uid for case in self.cases]
        if changed:
            self.cases[:] = reordered
            self._dirty = True
            self._order_dirty = True
        by_uid = {case.uid: case for case in self.cases}
        for case in self.cases:
            videos = sorted(case.videos, key=_video_number_sort_key)
            if videos == case.videos:
                continue
            active_path = case.active_video
            case.videos[:] = videos
            if active_path in videos:
                case.active_video_index = videos.index(active_path)
            # Attachments and annotations are keyed by path. Only their display
            # order and active index change; completed investigations stay so.
            self._mark_case_dirty(case)
            changed = True

        def remap(target):  # type: ignore[no-untyped-def]
            if target is None:
                return None
            uid, index = target
            if index < 0:
                return target
            old_videos = previous_videos.get(uid, ())
            case = by_uid.get(uid)
            if case is None or not 0 <= index < len(old_videos):
                return None
            return uid, case.videos.index(old_videos[index])

        state["current"] = remap(state.get("current"))
        state["selected"] = [
            replacement for target in state.get("selected", [])
            if (replacement := remap(target)) is not None
        ]
        if changed:
            self.case_list.set_cases(self.cases, self.current_uid)
            self.case_list.restore_view_state(state)
        if self._save(show_message=False):
            self.statusBar().showMessage("사고와 영상을 번호 오름차순으로 정렬하고 저장했습니다.", 5000)

    def choose_videos(self) -> None:
        filters = "영상 파일 (*.mp4 *.avi *.mov *.mkv *.m4v *.wmv *.webm);;모든 파일 (*.*)"
        files, _ = QFileDialog.getOpenFileNames(self, "조사 영상 선택", QDir.homePath(), filters)
        if files:
            self._import_dropped_videos(files)

    @staticmethod
    def _find_missing_videos(paths: tuple[str, ...]) -> set[str]:
        """디스크 작업 스레드에서 실행할 수 있도록 경로 스냅숏만 받는다."""
        return {path for path in paths if not os.path.isfile(path)}

    def _video_path_snapshot(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(path for case in self.cases for path in case.videos))

    def _missing_video_paths(self) -> set[str]:
        return self._find_missing_videos(self._video_path_snapshot())

    def relink_video_paths(
        self, mapping: dict[str, str], *, automatic: bool = False, open_video: bool = True,
    ) -> bool:
        """Back up and atomically reconnect existing cases, never re-import them."""
        if (not mapping or self._commit_in_progress
                or (not automatic and (self._switching_project or self._video_import_in_progress))
                or self._workspace_load_failed
                or self._detection_job is not None):
            return False
        previous_video = self.current_case.active_video if self.current_case else None
        try:
            candidates = self._disk_operation(
                apply_workspace_video_path_mapping,
                self.cases, mapping, workspace_root=self.repository.workspace,
            )
            exclusions = set(remap_video_path_list(
                self.repository.auto_import_exclusions, mapping,
                workspace_root=self.repository.workspace,
            ))
            # A recoverable, consistent backup includes committed WAL content.
            # 백업은 작업 폴더당 최신 1개만 유지한다(사용자데이터/백업/작업관리_백업.db).
            backup = self._backup_latest_before_change()
            self._save_candidates(
                candidates, self.current_uid, auto_import_exclusions=exclusions, prepare_off_thread=True,
            )
        except (OSError, ValueError, TypeError, WorkspaceError) as exc:
            if automatic:
                self._auto_import_failed = True
                self.statusBar().showMessage(
                    "영상 경로를 저장하지 못해 자동 복구를 보류했습니다. 기존 조사 내용과 연결은 유지됩니다.", 10000,
                )
            else:
                QMessageBox.warning(self, "영상 경로 복구 실패", str(exc))
            return False
        old_contexts = [
            self._object_history_key(case, path)
            for case, candidate in zip(self.cases, candidates, strict=True)
            for path, replacement in zip(case.videos, candidate.videos, strict=True)
            if path != replacement
        ]
        self.cases = candidates
        self.repository.auto_import_exclusions = exclusions
        for context in old_contexts:
            self._object_edit_history.clear(context)
        self._statistics_media_cache.clear()
        self._statistics_media_attempted.clear()
        self._fps_cache_workspace = None
        self._mark_saved(self.current_uid)
        list_state = self.case_list.capture_view_state()
        self.case_list.set_cases(self.cases, self.current_uid)
        self.case_list.restore_view_state(list_state)
        active_video = self.current_case.active_video if self.current_case else None
        if automatic and previous_video == active_video:
            # Repairing an inactive video must not reopen/reset the playing one
            # or reload the user's current form editor.
            self._refresh_case_presentation(rebuild_list=False)
        else:
            self._show_current_case(open_video=open_video)
        if not automatic:
            self._watch_active_project()
        self.statusBar().showMessage(
            f"영상 {len(mapping):,}개의 경로와 마커·속도 지점·회전·검출 결과를 함께 복구했습니다. "
            + (f"변경 전 백업: {backup.name}" if backup is not None else ""), 10000,
        )
        return True

    def _synchronize_project_videos(
        self, *, only_paths: set[str] | None = None, open_video: bool = True,
    ) -> bool:
        """Recover existing links first, then discover genuinely new cases.

        A missing or ambiguous link never blocks unrelated new videos. Its
        candidates are reserved, however, so they cannot become duplicate
        attachments before the old investigation has been recovered.
        """
        if (self._workspace_load_failed or self._project_discovery_in_progress
                or self._commit_in_progress or self._detection_job is not None):
            return False
        self._project_discovery_in_progress = True
        self._auto_import_failed = False
        try:
            workspace = self.repository.workspace
            # 끊어진 연결 확인과 스캔은 디스크 작업 스레드에서 한다(화면 정지 방지).
            missing = self._disk_operation(self._find_missing_videos, self._video_path_snapshot())
            if only_paths is not None and not missing:
                # 감시로 들어온 영상만 처리한다. 끊어진 연결이 없으면 복구 후보를 찾으려고
                # 프로젝트 전체를 다시 훑을 필요가 없다.
                scan = self._disk_operation(scan_video_files, sorted(only_paths), video_root(workspace))
            else:
                scan = self._disk_operation(scan_video_folder, video_root(workspace))
                self._last_scan_video_paths = {
                    *(path for _, paths in scan.groups for path in paths), *scan.ignored_videos,
                }
            reserved: frozenset[str] = frozenset()
            recovered = 0
            if missing:
                discovered = [path for _, paths in scan.groups for path in paths]
                discovered.extend(scan.ignored_videos)
                plan = self._disk_operation(
                    build_auto_relink_plan, self.cases, workspace,
                    discovered_paths=discovered,
                    excluded_paths=self.repository.auto_import_exclusions,
                    missing_paths=missing, trusted_scan=True,
                )
                reserved = plan.reserved_paths
                if plan.mapping:
                    if not self.relink_video_paths(plan.mapping, automatic=True, open_video=open_video):
                        return False
                    recovered = len(plan.mapping)
                    missing = self._disk_operation(
                        self._find_missing_videos, self._video_path_snapshot(),
                    )
            if reserved:
                reserved_keys = {video_path_key(path) for path in reserved}
                scan = VideoFolderScan(
                    tuple((number, matching) for number, paths in scan.groups
                          if (matching := tuple(path for path in paths
                                                if video_path_key(path) not in reserved_keys))),
                    tuple(path for path in scan.ignored_videos if video_path_key(path) not in reserved_keys),
                )
            self.import_video_folder(
                workspace, quiet_when_empty=True, respect_exclusions=True,
                only_paths=only_paths, automatic=True, _scan=scan, open_video=open_video,
            )
            if self._auto_import_failed:
                return False
            # 새로 가져온 영상은 방금 디스크에서 찾은 파일이라 누락 목록은 그대로다.
            self._last_missing_paths = missing
            if missing:
                prefix = f"영상 {recovered:,}개 자동 복구. " if recovered else ""
                self.statusBar().showMessage(
                    prefix + f"영상 {len(missing):,}개는 위치가 없거나 이름이 중복됩니다. "
                    "프로젝트 내부 영상 위치와 중복 파일명을 확인한 뒤 프로젝트를 다시 열어 주세요. "
                    "다른 새 영상은 자동 추가됩니다.", 0,
                )
            elif recovered:
                self.statusBar().showMessage(
                    f"영상 {recovered:,}개를 기존 사고에 자동으로 다시 연결했습니다. 조사 내용과 영상 주석을 유지했습니다.",
                    10000,
                )
            self._sync_project_tabs()
            previous_uid = self.current_uid
            previous_video = self.current_case.active_video if self.current_case else None
            self._apply_video_folder_scope()
            if open_video and (previous_uid != self.current_uid or previous_video != (
                self.current_case.active_video if self.current_case else None
            )):
                self._show_current_case(open_video=True)
            return True
        except (OSError, ValueError, WorkspaceError):
            self._auto_import_failed = True
            logger = logging.getLogger(__name__)
            logger.warning("project_video_discovery_failed", exc_info=True)
            self.statusBar().showMessage(
                "프로젝트 영상 검색을 완료하지 못했습니다. 기존 조사 내용은 유지됩니다. 폴더 접근 권한을 확인해 주세요.", 10000,
            )
            return False
        finally:
            self._project_discovery_in_progress = False


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
            workspace = self.repository.workspace
            if self.folder_watcher.is_watching(workspace):
                self.folder_watcher.add_directory(workspace)
            else:
                # 감시 시작용 전체 스캔은 디스크 작업 스레드에서 한다. 마지막 프로젝트
                # 스캔에서 이미 본 파일(규칙에 맞지 않는 이름·제외 영상 등)은 새 영상으로
                # 다시 보고하지 않아 시작 직후 불필요한 전체 동기화를 막는다.
                try:
                    scan = self._disk_operation(scan_video_tree, workspace)
                except OSError:
                    scan = None
                known = {path for case in self.cases for path in case.videos}
                known.update(self._last_scan_video_paths)
                self.folder_watcher.add_directory(workspace, known_paths=known, scan=scan)
        self._schedule_watch_refresh()

    def _schedule_watch_refresh(self) -> None:
        if self._session.pending_video_paths and not self._watch_refresh_timer.isActive():
            self._watch_refresh_timer.start(500)

    def _watched_videos_ready(self, folder: str, paths: list[str]) -> None:
        project = self._session
        if project.repository.workspace != Path(folder):
            return
        project.pending_video_paths.update(paths)
        self._schedule_watch_refresh()

    def _refresh_watched_videos(self) -> None:
        if (self._commit_in_progress or self._switching_project or self._video_import_in_progress
                or self._project_discovery_in_progress or self._detection_job is not None
                or QApplication.activeModalWidget() is not None):
            self._watch_refresh_timer.start(500)
            return
        project = self._session
        if self._workspace_load_failed or not project.pending_video_paths:
            return
        paths = set(project.pending_video_paths)
        project.pending_video_paths.difference_update(paths)
        self._auto_import_failed = False
        self._video_import_in_progress = True
        try:
            self._synchronize_project_videos(only_paths=paths)
        finally:
            self._video_import_in_progress = False
        if self._auto_import_failed:
            project.pending_video_paths.update(paths)
            self._watch_refresh_timer.start(5000)
        # 폴더 탭은 _synchronize_project_videos가 끝에서 이미 다시 맞춘다.

    def _copy_import_files(self, files: list[str], *, source_root: str | Path | None = None) -> CopiedVideos | None:
        self.statusBar().showMessage("영상을 작업 폴더로 복사하고 있습니다…")
        try:
            return self._disk_operation(
                copy_videos, files, self.repository.workspace, source_root=source_root,
            )
        except OSError as exc:
            QMessageBox.warning(self, "영상 복사 오류", f"영상을 복사할 수 없습니다.\n{exc}")
            return None

    def _rollback_video_copy(self, batch: CopiedVideos) -> None:
        try:
            self._disk_operation(batch.rollback)
            project = self._session
            project.pending_video_paths.difference_update(str(path) for path in batch.created)
        except OSError as exc:
            QMessageBox.warning(self, "영상 복사 정리 오류", f"복사한 일부 파일을 정리하지 못했습니다.\n{exc}")


    def import_video_folder(
        self,
        folder: str | Path,
        *,
        quiet_when_empty: bool = False,
        respect_exclusions: bool = False,
        copy_to_workspace: bool = False,
        only_paths: set[str] | None = None,
        automatic: bool = False,
        _scan: VideoFolderScan | None = None,
        open_video: bool = True,
    ) -> bool:
        """Group conventionally named videos into Samsung accident cases atomically."""

        if self._workspace_load_failed:
            self.statusBar().showMessage("읽기 오류가 없는 작업 폴더를 먼저 선택해 주세요.", 5000)
            return False
        try:
            scan = _scan if _scan is not None else self._disk_operation(scan_video_folder, folder)
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
            selected = {video_path_key(path) for path in only_paths}
            scan = VideoFolderScan(
                tuple((number, matching) for number, paths in scan.groups
                      if (matching := tuple(path for path in paths if video_path_key(path) in selected))),
                tuple(path for path in scan.ignored_videos if video_path_key(path) in selected),
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
                batch = self._copy_import_files(files, source_root=folder)
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

        by_identity = {
            (
                str(case.values.get("institution", "")).strip().casefold(),
                str(case.values.get("accident_classification", "")).strip(),
            ): case
            for case in candidates
        }
        # 저장된 영상 경로는 가져올 때 이미 resolve한 절대 경로라 문자열 정규화로 충분하다.
        # (사고 수만 건에서 영상마다 resolve()를 호출하면 화면이 수 초 멈췄다.)
        path_owner: dict[str, str] = {}
        for case in candidates:
            for video in case.videos:
                path_owner.setdefault(video_path_key(video), case.uid)

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
                    if video_path_key(path) not in candidate_exclusions
                )
                if not paths:
                    continue
            identity = (DEFAULT_INSTITUTION, accident_number)
            case = by_identity.get(identity)
            new_paths: list[str] = []
            for path in paths:
                owner = path_owner.get(video_path_key(path))
                if owner is None:
                    new_paths.append(path)
                elif case is not None and owner == case.uid:
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
                key = video_path_key(path)
                path_owner[key] = case.uid
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

        if attached_videos == 0:
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

        active_uid = imported_uid
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
            if previous_uid != active_uid and not self._project_discovery_in_progress:
                self.case_list.select_case(active_uid)
                self._show_current_case(open_video=open_video)
            else:
                self._refresh_case_presentation(rebuild_list=False)
        else:
            self.case_list.set_cases(self.cases, active_uid)
            self._sync_project_tabs()
            self._focus_case_folder(self.current_case)
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
        message = (
            f"사고 {len(affected_uids)}건에 영상 {added_videos}개를 불러왔습니다"
            f"{created_note}.{suffix}"
        )
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

        case_index = next(index for index, item in enumerate(candidates) if item.uid == case.uid)
        case = self._copy_case(case)
        candidates[case_index] = case
        added = case.attach_videos(valid)
        if not added:
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
        self._sync_project_tabs()
        self._focus_case_folder(self.current_case)
        self._show_current_case(open_video=True)
        self._set_workflow_enabled(True)
        return True

    def delete_case(self, uid: str) -> None:
        if self._commit_in_progress or self._video_import_in_progress or self._switching_project or self._workspace_load_failed:
            return
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
            "영상 파일은 프로젝트의 ‘삭제사고’ 폴더로 이동합니다.",
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

        self._delete_with_quarantine(
            candidates, active_uid, case.videos, label=case.display_id,
            success_text=f"{case.display_id} 사고를 DB에서 삭제했습니다.",
        )

    def delete_video(self, uid: str, index: int) -> None:
        if self._commit_in_progress or self._video_import_in_progress or self._switching_project or self._workspace_load_failed:
            return
        source_case = next((case for case in self.cases if case.uid == uid), None)
        if source_case is None or not 0 <= index < len(source_case.videos):
            return
        video_path = source_case.videos[index]
        video_name = Path(video_path).name
        answer = QMessageBox.question(
            self,
            "영상 삭제",
            f"{video_name} 영상을 사고 목록에서 삭제하고 해당 영상의 마커와 객체 라벨도 삭제하시겠습니까?\n\n"
            "같은 영상명의 기록이 남아 있으면 선택한 기록만 삭제하고 영상 파일은 유지합니다.\n"
            "마지막 기록을 삭제할 때 영상 파일을 프로젝트의 ‘삭제사고’ 폴더로 이동합니다.",
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
        self._delete_with_quarantine(
            candidates, active_uid, [video_path], label=source_case.display_id,
            success_text=f"{video_name} 영상 연결과 주석을 DB에서 삭제했습니다.",
        )

    def _delete_with_quarantine(
        self, candidates: list[InvestigationCase], active_uid: str | None, video_paths: list[str],
        *, label: str, success_text: str,
    ) -> bool:
        """Move recoverably, commit the DB, then publish new models to the UI."""
        if not self._confirm_stop_detection():
            return False

        def key(path: str) -> str:
            return str(Path(path).expanduser().resolve()).casefold()

        shared: tuple[str, ...] = ()
        removed_links: set[str] = set()
        exclusions = set(self.repository.auto_import_exclusions)
        metadata = self.video_controller.metadata
        playing_path = str(metadata.path) if metadata is not None else None
        previous_uid = self.current_uid
        previous_video = self.current_case.active_video if self.current_case else None
        previous_frame = self.video_controller.requested_frame
        was_playing = self.video_controller.is_playing
        batch: QuarantinedVideos | None = None
        player_released = False
        self._video_import_in_progress = True
        try:
            plan = self._disk_operation(
                plan_video_deletion, candidates, video_paths, self.repository.workspace,
            )
            movable, shared = plan.movable, plan.shared
            removed_links = {key(path) for path in plan.excluded_paths}
            exclusions.update(removed_links)
            close_player = playing_path is not None and key(playing_path) in {key(path) for path in movable}
            # 삭제 전 상태를 최신 백업 1개로 남긴다(사용자데이터/백업/작업관리_백업.db).
            self._backup_latest_before_change()
            # Release decoder/audio file handles before attempting Windows moves.
            if close_player:
                self.video_controller.close_video()
                player_released = True
            batch = self._disk_operation(
                quarantine_videos, movable, self.repository.workspace, case_label=label,
            )
            self._save_candidates(
                candidates, active_uid, auto_import_exclusions=exclusions, prepare_off_thread=True,
            )
        except (OSError, ValueError, WorkspaceError) as exc:
            restore_error = exc if isinstance(exc, QuarantineRollbackError) else None
            if batch is not None:
                try:
                    self._disk_operation(batch.rollback)
                except OSError as rollback_error:
                    restore_error = rollback_error
            if player_released and restore_error is None and self._open_active_video():
                self.video_controller.seek(max(0, previous_frame))
                if was_playing:
                    self.video_controller.play()
            message = f"삭제하지 못했습니다. 기존 DB와 조사 내용은 유지됩니다.\n{exc}"
            if restore_error is not None:
                message += f"\n일부 영상은 삭제사고 폴더에 남아 있습니다. 이동기록을 확인해 주세요.\n{restore_error}"
            QMessageBox.critical(self, "삭제 오류", message)
            return False
        finally:
            self._video_import_in_progress = False
            self._schedule_watch_refresh()

        # From here the DB transaction has committed: never roll the files back.
        journal_saved = self._disk_operation(batch.commit)
        candidate_by_uid = {case.uid: case for case in candidates}
        for old in self.cases:
            survivor = candidate_by_uid.get(old.uid)
            if survivor is old:
                continue  # 바뀌지 않은 사고(같은 객체)는 영상도 그대로다.
            surviving_paths = {key(path) for path in survivor.videos} if survivor else set()
            for path in old.videos:
                if key(path) not in surviving_paths:
                    self._object_edit_history.clear(self._object_history_key(old, path))
        self.cases = candidates
        self.current_uid = active_uid
        self.repository.auto_import_exclusions = exclusions
        self._mark_saved(active_uid)
        self._statistics_media_cache.clear()
        self._statistics_media_attempted.clear()
        self._fps_cache_workspace = None
        project = self._session
        project.pending_video_paths.difference_update(
            path for path in tuple(project.pending_video_paths) if key(path) in removed_links
        )
        self.case_list.set_cases(self.cases, active_uid, selected_video_index=None)
        self._sync_project_tabs()
        self._apply_video_folder_scope()
        current_video = self.current_case.active_video if self.current_case else None
        if previous_uid == self.current_uid and previous_video == current_video and not player_released:
            self._refresh_case_presentation(rebuild_list=False)
        else:
            self._show_current_case(open_video=True)
        self._set_workflow_enabled(True)
        self._watch_active_project()
        notes = [f"영상 {len(batch.moved):,}개를 삭제사고 폴더로 이동했습니다."]
        if batch.missing:
            notes.append(f"이미 없는 영상 {len(batch.missing):,}개의 연결을 정리했습니다.")
        if shared:
            notes.append(f"같은 영상명의 기록이 남은 영상 {len(shared):,}개는 원래 위치에 유지했습니다.")
        self.statusBar().showMessage(success_text + " " + " ".join(notes), 10000)
        if not journal_saved:
            QMessageBox.warning(
                self, "이동기록 저장 확인",
                f"DB 삭제와 영상 이동은 완료됐지만 이동기록을 마무리하지 못했습니다.\n{batch.operation_directory}",
            )
        return True

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
            self._refresh_detection()
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
        self._refresh_detection()
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
            self.detection_panel.set_markers(None, None)
            self.speed_panel.set_data([], SpeedEstimate(), 0.0, context_key=None)
            self.speed_panel.add_button.setEnabled(False)
            return
        points = case.speed_points
        self.detection_panel.set_markers(case.trigger_frame, case.collision_frame)
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
        if visible and self.playback.detection_panel_button.isChecked():
            # Speed and detection panels share the space under the video.
            self.playback.detection_panel_button.setChecked(False)
        self.video_tool_splitter.set_tool_visible("speed", visible)
        self._update_canvas_minimum()

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

    def _save_detection_case(self, case: InvestigationCase) -> bool:
        """Checkpoint one detection target without saving unrelated edits.

        A finished detector result must survive an abnormal exit before the
        user presses draft save. Use the existing serialized storage worker,
        and do not rewrite project selection/order or replace the live case.
        """
        if (
            self._commit_in_progress
            or self._workspace_load_failed
            or not any(item is case for item in self.cases)
        ):
            return False
        repository = self.repository
        revision = self._case_revisions.get(case.uid, 0)
        try:
            snapshot = self._copy_case(case)
            self._disk_operation(
                repository.save_changes, [snapshot], allow_edit=False,
            )
        except (WorkspaceError, OSError) as exc:
            # Diagnostics must not contain case values, paths, or raw results.
            logging.getLogger(__name__).warning(
                "detection_result_save_failed type=%s", type(exc).__name__,
            )
            self.statusBar().showMessage(
                "검출 결과를 저장하지 못했습니다. 임시저장으로 다시 저장해 주세요.",
                10000,
            )
            return False
        # The nested Qt event loop still processes timers/signals. Never clear
        # newer edits or touch the state of a project opened by such a callback.
        if self.repository is repository and any(item is case for item in self.cases):
            self._persisted_uids.add(case.uid)
            if self._case_revisions.get(case.uid, 0) == revision:
                self._dirty_cases.discard(case.uid)
            self._dirty = bool(self._dirty_cases) or self._order_dirty
        return True

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
        cases = self._folder_cases()
        if not self._save(show_message=False) or not cases:
            return
        current_index = next(
            (index for index, case in enumerate(cases) if case.uid == self.current_uid), 0
        )
        ordered = cases[current_index + 1 :] + cases[: current_index + 1]
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
        export_directory = self.repository.workspace / CSV_EXPORT_DIRECTORY
        try:
            export_directory.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            QMessageBox.warning(self, "CSV 저장 폴더 오류", str(exc))
            return
        suggested = export_directory / f"investigation_{datetime.now():%Y%m%d}.csv"
        path, _ = QFileDialog.getSaveFileName(
            self, "조사 결과 CSV 내보내기", str(suggested), "CSV 파일 (*.csv)"
        )
        if not path:
            return
        if not Path(path).suffix:
            path += ".csv"
        options = CsvExportDialog(Path(path), self, definition=self.repository.form_definition)
        if options.exec() != QDialog.DialogCode.Accepted:
            return
        include_specification = options.include_specification.isChecked()
        try:
            # 속도 CSV는 저장된 FPS 확인 결과를 재사용하고 새로 확인한 것만 다시 저장한다.
            self._ensure_fps_cache_loaded()
            fps_snapshot = dict(self._statistics_media_cache)
            destination = self._disk_operation(self.repository.export_csv,
                path, self.cases, include_specification=include_specification,
                video_metadata=fps_snapshot,
            )
        except WorkspaceError as exc:
            QMessageBox.critical(self, "CSV 내보내기 오류", str(exc))
            return
        changed = {
            key: entry for key, entry in fps_snapshot.items()
            if self._statistics_media_cache.get(key) != entry
        }
        if changed:
            self._statistics_media_cache.update(changed)
            self._fps_pending.update(changed)
            self._flush_video_metadata()
        message = f"CSV를 저장했습니다: {destination}"
        message += f" · 속도 분석: {speed_csv_path(destination).name}"
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
        options = ReportExportDialog(
            self.cases, selected, self, definition=self.repository.form_definition
        )
        if options.exec() != QDialog.DialogCode.Accepted:
            return
        selected_uids = set(options.selected_uids())
        if not selected_uids:
            return
        export_directory = self.repository.workspace / REPORT_EXPORT_DIRECTORY
        try:
            export_directory.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            QMessageBox.warning(self, "보고서 저장 폴더 오류", str(exc))
            return
        folder = QFileDialog.getExistingDirectory(
            self,
            "사고 분석 보고서 저장 폴더 선택",
            str(export_directory),
            QFileDialog.Option.ShowDirsOnly,
        )
        if not folder:
            return
        snapshots = [self._copy_case(case) for case in self.cases if case.uid in selected_uids]
        report_number_by_uid = build_case_number_map(self.cases)
        if not snapshots:
            return
        self.statusBar().showMessage(f"사고 분석 보고서 {len(snapshots)}건을 생성하고 있습니다…")
        try:
            destinations = self._disk_operation(
                export_case_reports,
                snapshots,
                Path(folder),
                form_definition=self.repository.form_definition,
                report_number_by_uid=report_number_by_uid,
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
        # 기본 위치는 작업 폴더당 하나만 유지하는 최신 백업 파일이다(덮어쓰기).
        # 다른 위치(외장 저장장치 등)를 고르면 그곳에 별도 사본을 만든다.
        suggested = self.repository.latest_backup_path
        try:
            suggested.parent.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
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
        if Path(destination) == self.repository.latest_backup_path:
            self._latest_backup_protected = False
        self.statusBar().showMessage(f"조사 데이터를 백업했습니다: {destination}", 5000)

    def restore_workspace(self) -> None:
        """백업 파일(.db)로 현재 작업 폴더의 조사 데이터를 바꾼다."""
        if self._commit_in_progress or self._switching_project or self._video_import_in_progress:
            return
        if not self._confirm_stop_detection():
            return
        latest = self.repository.latest_backup_path
        start = latest if latest.is_file() else (
            latest.parent if latest.parent.is_dir() else self.repository.workspace
        )
        path, _ = QFileDialog.getOpenFileName(
            self, "조사 데이터 불러오기", str(start), "SQLite 파일 (*.db);;모든 파일 (*.*)"
        )
        if not path:
            return
        answer = QMessageBox.question(
            self,
            "조사 데이터 불러오기",
            "현재 작업 폴더의 조사 데이터를 선택한 백업 파일의 내용으로 바꿉니다.\n"
            "바꾸기 전 현재 데이터는 최신 백업 파일로 저장합니다"
            "(읽을 수 없는 데이터는 다른 이름으로 보존합니다).\n\n계속할까요?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        if not self._workspace_load_failed and not self._save(show_message=False):
            return
        broken = self._workspace_load_failed and self.repository.database_path.exists()
        keep_current_as = (
            self.repository.database_path.with_name(
                f"작업관리_읽기오류_{datetime.now():%Y%m%d_%H%M%S}.db"
            ) if broken else None
        )
        staged: Path | None = None
        try:
            # 선택한 파일을 먼저 검증된 복사본으로 확보한 뒤 현재 데이터를 최신 백업으로 남긴다.
            staged = self._disk_operation(self.repository.stage_restore, path)
            if not broken and not self._latest_backup_protected:
                self._disk_operation(self.repository.backup_latest)
            self.video_controller.close_video()
            self._disk_operation(
                self.repository.activate_restore, staged, keep_current_as=keep_current_as,
            )
            staged = None
        except WorkspaceError as exc:
            if staged is not None:
                staged.unlink(missing_ok=True)
            QMessageBox.critical(
                self, "조사 데이터 불러오기 실패", f"{exc}\n\n현재 조사 데이터는 바꾸지 않았습니다.",
            )
            if not self._workspace_load_failed:
                self._show_current_case(open_video=True)
            return
        self._latest_backup_protected = False
        self._reload_after_restore()
        message = f"백업 파일에서 조사 데이터를 불러왔습니다(사고 {len(self.cases):,}건): {Path(path).name}"
        if keep_current_as is not None:
            message += f" · 읽을 수 없던 기존 데이터 보존: {keep_current_as.name}"
        self.statusBar().showMessage(message, 15000)

    def _reload_after_restore(self) -> None:
        """교체된 DB로 현재 작업 폴더를 처음부터 다시 읽는다(편집 기록·화면 상태 초기화)."""
        self._object_edit_history = type(self._object_edit_history)()
        self._folder_view_states = {}
        self._case_revisions.clear()
        self._deferred_reorders.clear()
        self._statistics_media_cache.clear()
        self._statistics_media_attempted.clear()
        self._fps_cache_workspace = None
        self.current_uid = None
        self.cases = []
        project = self._session
        project.cases = []
        project.current_uid = None
        project.restored = False
        loaded = self._load_workspace()
        project.initialized = loaded
        project.load_failed = not loaded
        self._watch_active_project()
        self._invalidate_statistics()

    def _video_opened(self, metadata: VideoMetadata) -> None:
        try:
            stat = metadata.path.stat()
            entry = (stat.st_size, stat.st_mtime_ns, metadata.reported_fps)
            if self._statistics_media_cache.get(str(metadata.path)) != entry:
                self._statistics_media_cache[str(metadata.path)] = entry
                self._fps_pending[str(metadata.path)] = entry  # 다음 저장 시점에 DB에 기록
            self._invalidate_statistics()
        except OSError:
            pass
        self.video_player.set_video_name(metadata.path, (metadata.width, metadata.height))
        self.playback.set_video(metadata.total_frames, metadata.fps)
        case = self._opening_case or self.current_case
        self.video_player.set_rotation(case.video_rotation if case else 0)
        if case:
            self._sync_markers(case, fps=metadata.reported_fps)
        self._detection_video_changed()
        self.statusBar().showMessage(
            f"영상 열기 완료 · {metadata.fps:.2f} FPS · {metadata.total_frames:,} frames", 4000
        )

    def _video_closed(self) -> None:
        self.video_player.canvas.clear()
        self.video_player.set_rotation(0)
        self.playback.clear_video()
        self.speed_panel.set_data([], SpeedEstimate(), 0.0, context_key=None)
        self.speed_panel.add_button.setEnabled(False)
        self._detection_video_changed()

    def _frame_ready(self, image, frame: int, timestamp: float) -> None:  # type: ignore[no-untyped-def]
        del timestamp
        self.video_player.canvas.set_frame(image)
        self._detection_frame_changed(frame)

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
        if any(path not in self._statistics_media_attempted for path in self._fps_needed_paths()):
            self._request_statistics_media()

    def _fps_needed_paths(self) -> list[str]:
        """FPS가 통계에 실제로 쓰이는 영상만 고른다.

        FPS는 트리거→충돌 시간과 트리거 시점 속도 계산에만 쓰인다. 트리거와 충돌
        (또는 속도 지점)이 지정된 현재 영상만 확인하면 수만 개 영상을 모두 열 필요가 없다.
        """
        paths: dict[str, None] = {}
        for case in self.cases:
            path = case.active_video
            if not path:
                continue
            marker = case.markers_by_video.get(path) or {}
            if marker.get("trigger_frame") is None:
                continue
            if marker.get("collision_frames") or marker.get("speed_points"):
                paths[path] = None
        return list(paths)

    def _ensure_fps_cache_loaded(self) -> None:
        """작업 폴더별로 DB에 저장된 FPS 확인 결과를 한 번 읽어 온다."""
        workspace = self.repository.workspace
        if self._fps_cache_workspace == workspace or self._workspace_load_failed:
            return
        self._fps_cache_workspace = workspace
        try:
            stored = self._disk_operation(self.repository.load_video_metadata)
        except WorkspaceError:
            logging.getLogger(__name__).warning("video_metadata_load_failed", exc_info=True)
            return
        for path, entry in stored.items():
            # 이번 실행에서 영상을 열어 얻은 값이 있으면 그것이 더 최신이다.
            self._statistics_media_cache.setdefault(path, entry)

    def _flush_video_metadata(self, *, closing: bool = False) -> None:
        """새로 확인한 FPS 정보를 작업 폴더 DB에 저장한다(실패해도 작업은 계속)."""
        if not self._fps_pending or self._workspace_load_failed or self._storage_worker.closed:
            return
        if self._commit_in_progress and not closing:
            return  # 진행 중인 저장이 끝난 뒤 다음 기회에 기록한다.
        pending, self._fps_pending = self._fps_pending, {}
        try:
            if closing:
                self.repository.save_video_metadata(pending)
            else:
                self._disk_operation(self.repository.save_video_metadata, pending)
        except WorkspaceError:
            logging.getLogger(__name__).warning("video_metadata_save_failed", exc_info=True)

    def _open_statistics_case(self, uid: str, key: str = "") -> None:
        if self._commit_in_progress or not any(case.uid == uid for case in self.cases):
            return
        if not self._save(show_message=False):
            return
        self.mode_tabs.setCurrentIndex(0)
        target = next(case for case in self.cases if case.uid == uid)
        self.current_uid = uid
        self._focus_case_folder(target)
        self.case_list.clear_search()
        self.case_list.select_case(uid)
        self._show_current_case(open_video=True)
        if key:
            key = {"site_location": "site_types", "obstacle_object_types": "obstacle_vehicle_types", "trigger_frame": ""}.get(key, key)
            if key:
                self.investigation.focus_field(key)

    def _request_statistics_media(self) -> None:
        if (self._statistics_media_future is not None or self._workspace_load_failed
                or self._commit_in_progress):
            return
        self._ensure_fps_cache_loaded()
        # FPS가 필요한 영상 중 이번 실행에서 아직 확인하지 않은 것만 백그라운드로 확인한다.
        # 저장된 결과와 크기·수정시각이 같으면 probe_video_fps가 영상을 열지 않는다.
        paths = [path for path in self._fps_needed_paths() if path not in self._statistics_media_attempted]
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
            result = future.result()
        except Exception:
            self.statusBar().showMessage("일부 영상 정보를 확인하지 못했습니다. 파일 상태를 확인해 주세요.", 5000)
        else:
            changed = {
                path: entry for path, entry in result.items()
                if self._statistics_media_cache.get(path) != entry
            }
            self._statistics_media_cache.update(result)
            if self.repository.workspace == self._statistics_media_origin:
                self._fps_pending.update(changed)
                self._flush_video_metadata()
                self._invalidate_statistics()
                self.statusBar().showMessage("영상 통계 정보를 갱신했습니다.", 3000)
        if (self.repository.workspace != self._statistics_media_origin
                and self.mode_tabs.currentIndex() == 1):
            self._request_statistics_media()

    def _show_shortcuts(self) -> None:
        QMessageBox.information(
            self,
            "재생 단축키",
            "Space  재생 / 일시정지\n← / →  1프레임 이동\nCtrl+← / Ctrl+→  10프레임 이동\nJ / L  1초 이동\nM  속도 지점 추가 / 해제\nCtrl+S  임시저장\n\n객체 검출 패널의 박스 편집\nCtrl+Z  실행 취소\nCtrl+Y / Ctrl+Shift+Z  다시 실행\n(텍스트 입력 중에는 해당 입력의 실행 취소/다시 실행)",
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
        if not self._confirm_stop_detection():
            event.ignore()
            return
        if not self._workspace_load_failed and not self._save(show_message=False):
            event.ignore()
            return
        self._flush_video_metadata(closing=True)
        self._refresh_latest_backup_quietly(closing=True)
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
        self._release_workspace_locks()
        event.accept()


__all__ = ["MainWindow", "default_workspace"]
