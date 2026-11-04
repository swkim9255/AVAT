"""Main-window behaviour for range object detection and role/label editing.

Kept apart from ``main_window.py`` so the detection feature stays readable.
``DetectionMixin`` relies on MainWindow attributes (cases, repository,
video_controller, video_player, playback, investigation, statusBar, …).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QAbstractSpinBox,
    QApplication,
    QComboBox,
    QFrame,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QScrollArea,
    QTextEdit,
)

from investigator.detection_process import (
    DetectionJob,
    DetectionRequest,
    DetectionSettings,
)
from investigator.models import InvestigationCase
from investigator.object_detection import (
    OBSTACLE_EXISTS_FIELD,
    ROLE_BOTH,
    ROLE_TITLES,
    ROLE_TRIGGER,
    SOURCE_AUTOMATIC,
    LabelRef,
    VideoObjects,
    add_manual_object,
    applies_to_role,
    delete_objects,
    effective_annotation,
    form_updates,
    merge_detection_result,
    object_display_text,
    set_objects_label,
    set_objects_reviewed,
    set_objects_subtype,
    split_object_frames,
)
from investigator.object_history import ObjectEditHistory
from investigator.theme import MUTED
from investigator.widgets.detection_panel import (
    ROLE_COLORS,
    DetectionPanel,
    DetectionSettingsDialog,
)
from investigator.widgets.video_player import OverlayBox


@dataclass(slots=True)
class _JobContext:
    workspace: Path
    case_uid: str
    video_path: str
    request: DetectionRequest


def _same_path(left: str | None, right: str | None) -> bool:
    if not left or not right:
        return False
    return str(Path(left)).casefold() == str(Path(right)).casefold()


class DetectionMixin:
    # -- construction ----------------------------------------------------------

    def _init_detection(self) -> None:
        self.detection_panel = DetectionPanel(self)
        self._detection_job: DetectionJob | None = None
        self._detection_context: _JobContext | None = None
        self._selected_object_uids: list[str] = []
        self._overlay_frame = 0
        self._detection_objects = VideoObjects()
        self._objects_context: tuple[str, str, str] | None = None
        self._overlay_by_frame: dict[int, list] = {}
        self._overlay_styles: dict[tuple[str, int], tuple[str, str]] = {}
        self._object_edit_history = ObjectEditHistory()
        self._object_edit_shortcuts: list[QShortcut] = []
        self._restoring_object_edit = False
        application = QApplication.instance()
        if application is not None:
            application.aboutToQuit.connect(self._shutdown_detection)

    def _build_detection_area(self, parent, layout) -> None:  # type: ignore[no-untyped-def]
        self.detection_scroll = QScrollArea(parent)
        self.detection_scroll.setObjectName("detectionScroll")
        self.detection_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.detection_scroll.setWidgetResizable(True)
        self.detection_scroll.setMinimumHeight(140)
        self.detection_scroll.setWidget(self.detection_panel)
        self.detection_scroll.hide()
        layout.addWidget(self.detection_scroll, 0)

    def _connect_detection_signals(self) -> None:
        panel = self.detection_panel
        self.playback.detection_panel_toggled.connect(self._set_detection_panel_visible)
        self.playback.range_selected.connect(self._timeline_range_selected)
        panel.range_changed.connect(self._detection_range_changed)
        panel.run_requested.connect(self.run_detection)
        panel.cancel_requested.connect(self.cancel_detection)
        panel.settings_requested.connect(self.edit_detection_settings)
        panel.seek_requested.connect(self._seek_video)
        panel.selection_changed.connect(self._object_selection_changed)
        panel.confidence_threshold_changed.connect(lambda _value: self._update_overlay())
        panel.label_change_requested.connect(self._change_object_label)
        panel.subtype_change_requested.connect(self._change_object_subtype)
        panel.split_frame_requested.connect(self._split_object_frame)
        panel.reviewed_change_requested.connect(self._change_object_reviewed)
        panel.delete_requested.connect(self._delete_objects)
        panel.draw_mode_toggled.connect(self._set_object_draw_mode)
        panel.apply_requested.connect(self.apply_objects_to_form)
        self.video_player.canvas.box_clicked.connect(self._canvas_box_clicked)
        self.video_player.canvas.box_drawn.connect(self._canvas_box_drawn)
        for sequence, callback in (
            ("Ctrl+Z", self.undo_object_edit),
            ("Ctrl+Y", self.redo_object_edit),
            ("Ctrl+Shift+Z", self.redo_object_edit),
        ):
            shortcut = QShortcut(QKeySequence(sequence), self.video_workspace)
            shortcut.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
            shortcut.setAutoRepeat(False)
            shortcut.setEnabled(False)
            shortcut.activated.connect(callback)
            self._object_edit_shortcuts.append(shortcut)

    # -- visibility ------------------------------------------------------------

    def _update_canvas_minimum(self) -> None:
        compact = self.speed_scroll.isVisibleTo(self) or self.detection_scroll.isVisibleTo(self)
        self.video_player.canvas.setMinimumHeight(140 if compact else 280)

    def _set_detection_panel_visible(self, visible: bool) -> None:
        if visible and self.playback.speed_panel_button.isChecked():
            # Only one panel under the video at a time; opening one closes the other.
            self.playback.speed_panel_button.setChecked(False)
        self.video_tool_splitter.set_tool_visible("detection", visible)
        for shortcut in self._object_edit_shortcuts:
            shortcut.setEnabled(visible)
        self._update_canvas_minimum()
        if visible:
            self._refresh_detection()
            self._detection_range_changed(*self.detection_panel.frame_range)
        else:
            self.playback.set_selection_range(None)
            self.detection_panel.draw_button.setChecked(False)
            self._update_overlay()

    # -- current state -----------------------------------------------------------

    def _detection_case(self) -> tuple[InvestigationCase | None, bool]:
        """(current case, whether its active video is the open one)."""

        case = self._opening_case or self.current_case
        metadata = self.video_controller.metadata
        matching = (
            case is not None
            and metadata is not None
            and _same_path(str(metadata.path), case.active_video)
        )
        return case, matching

    def _current_objects(self) -> VideoObjects:
        case, matching = self._detection_case()
        if case is None or not matching:
            return VideoObjects()
        context = (str(self.repository.workspace), case.uid, case.active_video)
        if context != self._objects_context:
            self._cache_detection_objects(case)
        return self._detection_objects

    def _cache_detection_objects(self, case: InvestigationCase | None) -> None:
        self._objects_context = (
            (str(self.repository.workspace), case.uid, case.active_video) if case else None
        )
        self._detection_objects = case.video_objects() if case else VideoObjects()
        self._overlay_by_frame = {}
        self._overlay_styles = {}
        for item in self._detection_objects.objects:
            for box in item.boxes:
                annotation = effective_annotation(item, box.frame)
                color = ROLE_COLORS[item.role] if annotation.subtype_key else MUTED
                text = f"{item.object_id} {object_display_text(item, box.frame)}"
                self._overlay_styles[item.uid, box.frame] = (color, text)
                self._overlay_by_frame.setdefault(box.frame, []).append((item, box))

    def _refresh_detection(self) -> None:
        """Push objects of the open video to the panel and the overlay."""

        if not self.detection_scroll.isVisibleTo(self):
            # Hidden detection must not deserialize every object's frame history
            # while the user simply reviews or scrubs a video.
            self._cache_detection_objects(None)
            self._update_overlay()
            return
        case, matching = self._detection_case()
        self._cache_detection_objects(case if matching else None)
        objects = self._detection_objects
        known = {item.uid for item in objects.objects}
        self._selected_object_uids = [uid for uid in self._selected_object_uids if uid in known]
        self.detection_panel.set_objects(objects, self.repository.form_definition)
        self.detection_panel.set_current_frame(self._overlay_frame)
        self._update_overlay()

    def _detection_video_changed(self) -> None:
        self._overlay_frame = 0
        self.playback.set_selection_range(None)
        metadata = self.video_controller.metadata
        if metadata is None:
            self.detection_panel.set_video(0, 0.0)
        else:
            self.detection_panel.set_video(metadata.total_frames, metadata.reported_fps)
        self._selected_object_uids = []
        self.detection_panel.draw_button.setChecked(False)
        self._refresh_detection()

    def _detection_frame_changed(self, frame: int) -> None:
        self._overlay_frame = frame
        if not self.detection_scroll.isVisibleTo(self):
            return
        self.detection_panel.set_current_frame(frame)
        self._update_overlay()

    def _update_overlay(self) -> None:
        canvas = self.video_player.canvas
        if not self.detection_scroll.isVisibleTo(self):
            # Boxes are shown only while the detection panel is open.
            canvas.set_overlay([])
            return
        _case, matching = self._detection_case()
        if not matching:
            canvas.set_overlay([])
            return
        self._current_objects()
        selected = set(self._selected_object_uids)
        threshold = self.detection_panel.confidence_threshold
        boxes = []
        for item, box in self._overlay_by_frame.get(self._overlay_frame, []):
            if item.source == SOURCE_AUTOMATIC and box.score is not None and box.score < threshold:
                continue
            color, text = self._overlay_styles[item.uid, box.frame]
            boxes.append(OverlayBox(item.uid, box.bbox, color, text, item.uid in selected))
        canvas.set_overlay(boxes)

    def _store_objects(
        self, case: InvestigationCase, objects: VideoObjects, video_path: str,
        *, refresh: bool = True,
    ) -> None:
        case.set_video_objects(objects, video_path=video_path)
        self._mark_case_dirty(case)
        if not refresh:
            return
        if case is self.current_case:
            self._refresh_case_presentation(rebuild_list=True)
            self._refresh_detection()
        else:
            self.case_list.update_case(case, active_uid=self.current_uid)

    def _edit_current_objects(self, change) -> None:  # type: ignore[no-untyped-def]
        case, matching = self._detection_case()
        if case is None or not matching:
            return
        try:
            before = case.video_objects()
            objects = change(before)
        except ValueError as exc:
            self.statusBar().showMessage(str(exc), 5000)
            return
        selected = self.detection_panel.selected_uids()
        before_uids = {item.uid for item in before.objects}
        after_uids = {item.uid for item in objects.objects}
        added = [item.uid for item in objects.objects if item.uid not in before_uids]
        after_selection = added or [uid for uid in selected if uid in after_uids]
        changed = self._object_edit_history.record(
            self._object_history_key(case, case.active_video),
            before,
            objects,
            before_selection=selected,
            after_selection=after_selection,
            frame=self._overlay_frame,
        )
        if not changed:
            return
        self._store_objects(case, objects, case.active_video)

    def _object_history_key(self, case: InvestigationCase, video_path: str) -> tuple[str, str, str]:
        return (
            str(self.repository.workspace.resolve()).casefold(),
            case.uid,
            str(Path(video_path).resolve()).casefold(),
        )

    def undo_object_edit(self) -> None:
        self._restore_object_edit(redo=False)

    def redo_object_edit(self) -> None:
        self._restore_object_edit(redo=True)

    def _restore_object_edit(self, *, redo: bool) -> None:
        # Native text/numeric editing keeps its own undo/redo. Box shortcuts are
        # scoped to the video workspace, never to the investigation sidebar.
        focus = QApplication.focusWidget()
        if isinstance(focus, (QLineEdit, QComboBox, QAbstractSpinBox, QTextEdit, QPlainTextEdit)):
            return
        if (
            not self.detection_scroll.isVisibleTo(self)
            or self.mode_tabs.currentIndex() != 0
            or self._commit_in_progress
            or self._switching_project
            or self._workspace_load_failed
            or self._detection_job is not None
        ):
            return
        if self.video_player.canvas.cancel_drawing():
            self.statusBar().showMessage("작성 중인 박스를 취소했습니다.", 3000)
            return
        case, matching = self._detection_case()
        if case is None or not matching:
            return
        key = self._object_history_key(case, case.active_video)
        operation = self._object_edit_history.redo if redo else self._object_edit_history.undo
        result = operation(key, case.video_objects())
        if result is None:
            self.statusBar().showMessage(
                "다시 실행할 박스 편집이 없습니다." if redo else "실행 취소할 박스 편집이 없습니다.",
                3000,
            )
            return
        if self.video_controller.is_playing:
            self.video_controller.pause()
        self._restoring_object_edit = True
        try:
            self._store_objects(case, result.objects, case.active_video)
            self.detection_panel.select_uids(result.selection)
        finally:
            self._restoring_object_edit = False
        if result.frame != self._overlay_frame:
            self._seek_video(result.frame)
        self.video_player.canvas.setFocus(Qt.FocusReason.ShortcutFocusReason)
        self.statusBar().showMessage("박스 편집을 다시 실행했습니다." if redo else "박스 편집을 실행 취소했습니다.", 3000)

    # -- range ---------------------------------------------------------------

    def _timeline_range_selected(self, start: int, end: int) -> None:
        if self._detection_job is not None:
            return
        if not self.playback.detection_panel_button.isChecked():
            self.playback.detection_panel_button.setChecked(True)
        self.detection_panel.set_selected_range(start, end)
        self._detection_range_changed(*self.detection_panel.frame_range)

    def _detection_range_changed(self, start: int, end: int) -> None:
        if not self.detection_scroll.isVisibleTo(self):
            return
        self.playback.set_selection_range(start, end)

    # -- running ---------------------------------------------------------------

    def edit_detection_settings(self) -> None:
        dialog = DetectionSettingsDialog(DetectionSettings.load(), self)
        if dialog.exec():
            dialog.settings().save()

    def run_detection(self) -> None:
        if self._detection_job is not None:
            return
        case, matching = self._detection_case()
        if case is None or not matching:
            self.statusBar().showMessage("검출할 영상을 먼저 열어 주세요.", 4000)
            return
        panel = self.detection_panel
        start, end = panel.frame_range
        metadata = self.video_controller.metadata
        end = min(end, max(0, metadata.total_frames - 1))
        classes = panel.class_keys
        if not classes:
            self.statusBar().showMessage("검출 클래스를 하나 이상 선택해 주세요.", 4000)
            return
        if self.video_controller.is_playing:
            self.video_controller.pause()
        request = DetectionRequest(
            video_path=Path(case.active_video),
            start_frame=start,
            end_frame=end,
            frame_step=panel.frame_step,
            classes=classes,
            role=ROLE_BOTH,
            case_uid=case.uid,
            workspace=str(self.repository.workspace),
        )
        job = DetectionJob(request, DetectionSettings.load(), self)
        self._detection_job = job
        self._detection_context = _JobContext(
            self.repository.workspace, case.uid, case.active_video, request
        )
        job.status.connect(panel.set_status)
        job.started.connect(lambda total: panel.set_progress(0, total))
        job.progress.connect(lambda done, total, _frame: panel.set_progress(done, total))
        job.finished.connect(self._detection_finished)
        job.failed.connect(self._detection_failed)
        job.cancelled.connect(self._detection_cancelled)
        panel.set_running(True)
        panel.set_status(
            f"{ROLE_TITLES[request.role]} · {request.start_frame + 1:,}~{request.end_frame + 1:,} "
            f"프레임 ({request.frame_count:,}장) 검출을 준비합니다…"
        )
        job.start()

    def cancel_detection(self) -> None:
        if self._detection_job is not None:
            self._detection_job.cancel()

    def _stop_detection(self, *, kill: bool) -> None:
        job, self._detection_job = self._detection_job, None
        self._detection_context = None
        if job is not None:
            if kill:
                job.kill()
            job.deleteLater()
        self.detection_panel.set_running(False)

    def _shutdown_detection(self) -> None:
        """Also release the child worker when app.quit bypasses closeEvent."""
        self._stop_detection(kill=True)

    def _confirm_stop_detection(self) -> bool:
        """Before leaving a project or closing: stop a running job (asks first)."""

        if self._detection_job is None:
            return True
        answer = QMessageBox.question(
            self,
            "객체 검출 중",
            "객체 검출이 진행 중입니다. 중단하고 계속할까요?\n중단하면 이번 검출 결과는 저장되지 않습니다.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return False
        self._stop_detection(kill=True)
        self.detection_panel.set_status("객체 검출을 중단했습니다.")
        return True

    def _detection_finished(self, document: dict) -> None:
        context = self._detection_context
        if context is None:
            return
        if self._commit_in_progress:
            # A save's nested Qt loop may deliver worker completion. Finish that
            # transaction before taking another detached snapshot/checkpoint.
            def retry() -> None:
                if self._detection_context is context:
                    self._detection_finished(document)
            QTimer.singleShot(50, retry)
            return
        self._stop_detection(kill=False)
        case = next((item for item in self.cases if item.uid == context.case_uid), None)
        if (
            case is None
            or self.repository.workspace != context.workspace
            or not any(_same_path(path, context.video_path) for path in case.videos)
        ):
            self.detection_panel.set_status("검출 대상 사고나 영상이 삭제되어 결과를 버렸습니다.")
            return
        request = context.request
        try:
            objects, added, replaced = merge_detection_result(
                case.objects_for_video(context.video_path),
                document,
                role=request.role,
                definition=self.repository.form_definition,
                start_frame=request.start_frame,
                end_frame=request.end_frame,
                frame_step=request.frame_step,
                classes=request.classes,
            )
        except (KeyError, TypeError, ValueError) as exc:
            self.detection_panel.set_status(f"검출 결과를 반영하지 못했습니다: {exc}")
            return
        # A fresh automatic run forms a history boundary for its own target,
        # even if the user selected a different case while it was running.
        self._object_edit_history.clear(self._object_history_key(case, context.video_path))
        # Commit the result before exposing a large interactive table. A later
        # decoder/UI failure must not discard an already completed detection.
        self._store_objects(case, objects, context.video_path, refresh=False)
        self.detection_panel.set_status("검출 결과를 자동 저장합니다…")
        saved = self._save_detection_case(case)
        if case is self.current_case:
            self._refresh_case_presentation(rebuild_list=True)
            self._refresh_detection()
        else:
            self.case_list.update_case(case, active_uid=self.current_uid)
        warnings = document.get("warnings") or []
        message = f"검출 완료 · 새 객체 {added}개"
        message += " · 자동 저장됨" if saved else " · 자동 저장 실패(미저장)"
        if replaced:
            message += f" · 이전 자동 검출 {replaced}개 교체"
        if warnings:
            message += f" · 경고 {len(warnings)}건"
        message += ". 라벨을 확인한 뒤 객체·충돌 탭에 반영해 주세요."
        self.detection_panel.set_status(message)
        self.statusBar().showMessage(message, 6000)
        if not saved:
            QMessageBox.warning(
                self, "검출 결과 자동 저장 실패",
                "검출 결과는 현재 화면에 유지되지만 디스크에 저장하지 못했습니다. "
                "작업 폴더의 권한과 여유 공간을 확인한 뒤 임시저장을 눌러 주세요.",
            )

    def _detection_failed(self, message: str) -> None:
        self._stop_detection(kill=False)
        self.detection_panel.set_status(message)
        QMessageBox.warning(self, "객체 검출 실패", message)

    def _detection_cancelled(self) -> None:
        self._stop_detection(kill=False)
        self.detection_panel.set_status("검출을 취소했습니다. 결과는 반영하지 않았습니다.")

    # -- editing ---------------------------------------------------------------

    def _set_object_draw_mode(self, enabled: bool) -> None:
        if enabled and self.video_controller.is_playing:
            self.video_controller.pause()
        self.video_player.canvas.set_draw_mode(enabled)

    def _object_selection_changed(self, uids: list[str]) -> None:
        self._selected_object_uids = list(uids)
        if len(uids) == 1 and not self._restoring_object_edit:
            item = self._current_objects().find(uids[0])
            if item is not None:
                threshold = self.detection_panel.confidence_threshold
                frames = [
                    box.frame for box in item.boxes
                    if item.source != SOURCE_AUTOMATIC or box.score is None or box.score >= threshold
                ]
                if frames and self._overlay_frame not in frames:
                    self._seek_video(min(frames, key=lambda frame: (abs(frame - self._overlay_frame), frame)))
        self._update_overlay()

    def _canvas_box_clicked(self, uid: str) -> None:
        if not self.detection_scroll.isVisibleTo(self):
            return
        self.detection_panel.select_uids([uid] if uid else [])

    def _canvas_box_drawn(self, bbox: tuple[float, float, float, float]) -> None:
        frame = self._overlay_frame
        created = []

        def change(objects: VideoObjects) -> VideoObjects:
            result, item = add_manual_object(objects, frame, bbox, ROLE_BOTH, None)
            created.append(item)
            return result

        self._edit_current_objects(change)
        if created:
            self.detection_panel.select_uids([created[0].uid])
            self.statusBar().showMessage(
                f"{created[0].object_id} 객체를 추가했습니다. 라벨을 선택해 주세요.", 4000
            )

    def _change_object_label(self, uids: list[str], label: LabelRef | None) -> None:
        definition = self.repository.form_definition
        self._edit_current_objects(
            lambda objects: set_objects_label(objects, uids, label, definition)
        )
        self.detection_panel.select_uids(uids)

    def _change_object_reviewed(self, uids: list[str], reviewed: bool) -> None:
        frame = None if self.detection_panel.all_id_frames.isChecked() else self._overlay_frame
        self._edit_current_objects(lambda objects: set_objects_reviewed(objects, uids, reviewed, frame=frame))
        self.detection_panel.select_uids(uids)

    def _change_object_subtype(self, uids: list[str], subtype: str | None,
                               frame: int | None, detail: str | None) -> None:
        self._edit_current_objects(lambda objects: set_objects_subtype(
            objects, uids, subtype, frame=frame, special_vehicle_type=detail,
        ))
        self.detection_panel.select_uids(uids)

    def _split_object_frame(self, uid: str) -> None:
        created = []

        def change(objects: VideoObjects) -> VideoObjects:
            result, item = split_object_frames(objects, uid, [self._overlay_frame])
            created.append(item.uid)
            return result

        self._edit_current_objects(change)
        if created:
            self.detection_panel.select_uids(created)

    def _delete_objects(self, uids: list[str]) -> None:
        self._edit_current_objects(lambda objects: delete_objects(objects, uids))

    # -- form ----------------------------------------------------------------

    def apply_objects_to_form(self, role: str) -> None:
        case, matching = self._detection_case()
        if case is None or not matching:
            return
        definition = self.repository.form_definition
        objects = case.video_objects().objects
        try:
            # Validate every ID before changing any investigation field.
            updates = form_updates(objects, role, definition, case.values)
            if any(update.field_key not in self.investigation.editors for update in updates):
                raise ValueError("현재 조사 양식에 연결할 입력 항목이 없어 반영할 수 없습니다.")
        except ValueError as exc:
            QMessageBox.warning(self, "조사 탭 반영 불가", str(exc))
            return
        canonical = definition.canonical_values(case.values)
        set_exists = (
            role == ROLE_TRIGGER
            and OBSTACLE_EXISTS_FIELD in self.investigation.editors
            and canonical.get(OBSTACLE_EXISTS_FIELD) != "유"
            and any(
                applies_to_role(item, ROLE_TRIGGER)
                for item in objects
            )
        )
        tab = "객체" if role == ROLE_TRIGGER else "충돌"
        if not updates and not set_exists:
            self.statusBar().showMessage(f"'{tab}' 탭에 새로 추가할 항목이 없습니다.", 4000)
            return
        lines = [f"· {text}" for update in updates for text in update.added_texts]
        if set_exists:
            lines.insert(0, "· 장애물 유무 → 유")
        message = f"'{tab}' 탭에 다음 항목을 추가합니다 (기존 선택은 유지).\n\n" + "\n".join(lines)
        answer = QMessageBox.question(
            self,
            f"{tab} 탭에 반영",
            message,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.Yes,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        if set_exists:
            stored = definition.stored_value(OBSTACLE_EXISTS_FIELD, "유")
            self.investigation.editors[OBSTACLE_EXISTS_FIELD].set_value(stored)
            self._field_changed(OBSTACLE_EXISTS_FIELD, stored)
        for update in updates:
            self.investigation.editors[update.field_key].set_value(update.stored_values)
            self._field_changed(update.field_key, update.stored_values)
        section = "trigger_objects" if role == ROLE_TRIGGER else "collision_objects"
        tab_index = next(
            (i for i, item in enumerate(self.investigation.sections) if item.key == section),
            None,
        )
        if tab_index is not None:
            self.investigation.tabs.setCurrentIndex(tab_index)
        added = sum(len(update.added_texts) for update in updates)
        self.statusBar().showMessage(f"'{tab}' 탭에 {added}개 항목을 추가했습니다.", 4000)


__all__ = ["DetectionMixin"]
