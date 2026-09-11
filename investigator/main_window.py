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
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from investigator.models import DEFAULT_INSTITUTION, InvestigationCase, new_case
from investigator.schema import (
    INVALID_FORMAT_MESSAGE,
    MISSING_VALUE_MESSAGE,
    field_label,
)
from investigator.storage import CaseRepository, WorkspaceError
from investigator.theme import PRIMARY, material_icon
from investigator.video_controller import PlaybackState, VideoController, VideoMetadata
from investigator.video_import import (
    VIDEO_EXTENSIONS,
    parse_accident_video_name,
    scan_video_folder,
)
from investigator.widgets.case_list import CaseListPanel
from investigator.widgets.form_panel import InvestigationPanel
from investigator.widgets.playback_panel import PlaybackPanel
from investigator.widgets.video_player import VideoPlayerPanel


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

        folder_action = QAction(material_icon("folder_open", PRIMARY), "영상 폴더 불러오기…", self)
        folder_action.triggered.connect(self.choose_video_folder)
        file_menu.addAction(folder_action)
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
        self.case_list.video_folder_requested.connect(self.choose_video_folder)
        self.case_list.add_video_requested.connect(self.choose_videos)
        self.case_list.delete_case_requested.connect(self.delete_case)
        self.case_list.delete_video_requested.connect(self.delete_video)
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

    def _load_workspace(self) -> bool:
        """Load persisted cases, then silently discover videos beside the database."""

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
        # Establish the passive workspace message first so an automatic import
        # summary remains visible instead of being immediately overwritten.
        self._update_workspace_status()
        return self.import_video_folder(
            self.repository.workspace,
            quiet_when_empty=True,
            respect_exclusions=True,
        )

    def _set_workflow_enabled(self, enabled: bool) -> None:
        has_case = enabled and self.current_case is not None
        self.case_list.new_button.setEnabled(enabled)
        self.case_list.video_button.setEnabled(enabled)
        self.case_list.delete_button.setEnabled(
            enabled and self.case_list.tree.currentItem() is not None
        )
        self.case_list.export_button.setEnabled(enabled and bool(self.cases))
        self.investigation.setEnabled(has_case)
        self.investigation.save_button.setEnabled(has_case)
        self.investigation.complete_button.setEnabled(
            has_case and self.current_case is not None and self.current_case.status != "completed"
        )
        self.investigation.next_button.setEnabled(has_case)

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
        imported = self._load_workspace()
        if not self._workspace_load_failed and not imported:
            self.statusBar().showMessage("작업 폴더를 열었습니다.", 3000)

    def create_case(self) -> None:
        if not self._save(show_message=False):
            return
        candidates = [InvestigationCase.from_dict(item.to_dict()) for item in self.cases]
        case = new_case(candidates)
        candidates.append(case)
        if not self._replace_cases_atomically(candidates, case.uid):
            return
        self.statusBar().showMessage(f"{case.display_id} 사고를 만들었습니다.", 3000)

    def choose_videos(self) -> None:
        filters = "영상 파일 (*.mp4 *.avi *.mov *.mkv *.m4v *.wmv *.webm);;모든 파일 (*.*)"
        files, _ = QFileDialog.getOpenFileNames(self, "조사 영상 선택", QDir.homePath(), filters)
        if files:
            self.attach_videos(files)

    def choose_video_folder(self) -> None:
        selected = QFileDialog.getExistingDirectory(
            self,
            "사고 영상 폴더 선택",
            QDir.homePath(),
            QFileDialog.Option.ShowDirsOnly,
        )
        if selected:
            self.import_video_folder(selected)

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
    ) -> bool:
        """Group conventionally named videos into Samsung accident cases atomically."""

        if self._workspace_load_failed:
            self.statusBar().showMessage("읽기 오류가 없는 작업 폴더를 먼저 선택해 주세요.", 5000)
            return False
        try:
            scan = scan_video_folder(folder)
        except OSError as exc:
            QMessageBox.warning(
                self,
                "영상 폴더 오류",
                f"영상 폴더를 읽을 수 없습니다.\n{exc}",
            )
            return False

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

        # Work on detached model copies and replace both disk state and live
        # state only after the repository's atomic save succeeds.
        candidates = [InvestigationCase.from_dict(case.to_dict()) for case in self.cases]
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
                by_identity[identity] = case
                created_cases += 1

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

        try:
            self.repository.save(
                candidates,
                active_case_uid=active_uid,
                auto_import_exclusions=candidate_exclusions,
            )
        except WorkspaceError as exc:
            QMessageBox.critical(self, "저장 오류", str(exc))
            return False

        self.video_controller.close_video()
        self.cases = candidates
        self.current_uid = active_uid
        self._dirty = False
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

        candidates = [InvestigationCase.from_dict(case.to_dict()) for case in self.cases]
        case = next((item for item in candidates if item.uid == self.current_uid), None)
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
                "여러 사고번호의 영상은 '영상 폴더' 기능으로 불러와 주세요.",
            )
            return

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

        added = case.attach_videos(valid)
        if not added:
            if removed_default_shell and self._replace_cases_atomically(candidates, case.uid):
                self.statusBar().showMessage("빈 기본 사고를 정리했습니다.", 3000)
                return
            self.statusBar().showMessage("선택한 영상은 이미 현재 사고에 연결되어 있습니다.", 4000)
            return
        updated_exclusions = set(self.repository.auto_import_exclusions)
        for path in valid:
            updated_exclusions.discard(str(Path(path).expanduser().resolve()).casefold())

        if not self._replace_cases_atomically(
            candidates,
            case.uid,
            auto_import_exclusions=updated_exclusions,
        ):
            return
        self.statusBar().showMessage(f"영상 {added}개를 추가했습니다.", 3000)

    def _replace_cases_atomically(
        self,
        candidates: list[InvestigationCase],
        active_uid: str | None,
        *,
        auto_import_exclusions: set[str] | None = None,
    ) -> bool:
        """Persist candidate state before replacing live models and UI."""

        try:
            self.repository.save(
                candidates,
                active_case_uid=active_uid,
                auto_import_exclusions=auto_import_exclusions,
            )
        except WorkspaceError as exc:
            QMessageBox.critical(self, "저장 오류", str(exc))
            return False

        self.video_controller.close_video()
        self.cases = candidates
        self.current_uid = active_uid
        self._dirty = False
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

        candidates = [InvestigationCase.from_dict(item.to_dict()) for item in self.cases]
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

        candidates = [InvestigationCase.from_dict(item.to_dict()) for item in self.cases]
        target = next(case for case in candidates if case.uid == uid)
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
        self.case_list.set_cases(self.cases, uid, selected_video_index=index)
        self._show_current_case(open_video=False)

    def _show_current_case(self, *, open_video: bool) -> None:
        case = self.current_case
        if case is None:
            self.video_controller.close_video()
            self.video_player.clear()
            self.playback.clear_video()
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

    def _save(
        self,
        *,
        show_message: bool,
        auto_import_exclusions: set[str] | None = None,
    ) -> bool:
        if self._workspace_load_failed:
            self.statusBar().showMessage("읽기 오류가 없는 작업 폴더를 먼저 선택해 주세요.", 5000)
            return False
        try:
            self.repository.save(
                self.cases,
                active_case_uid=self.current_uid,
                auto_import_exclusions=auto_import_exclusions,
            )
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
        if isinstance(focus, (QLineEdit, QComboBox, QSpinBox, QTextEdit)):
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
