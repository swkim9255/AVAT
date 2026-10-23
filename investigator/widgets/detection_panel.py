"""Object detection range, run controls, and subtype editing of detected objects."""

from __future__ import annotations

import math

from PySide6.QtCore import QItemSelection, QSettings, QSignalBlocker, Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QBoxLayout,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QProgressBar,
    QPushButton,
    QSizePolicy,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from investigator.detection_process import (
    DEFAULT_MODEL,
    DINO_BASE_MODEL,
    DINO_TINY_MODEL,
    REPOSITORY_ROOT,
    DetectionSettings,
    _load_runtime_manifest,
    default_model,
)
from investigator.detection_taxonomy import (
    SPECIAL_VEHICLE_TYPES,
    SUBTYPES,
    coarse_class_key,
    subtype_options,
)
from investigator.form_definition import FormDefinition
from investigator.object_detection import (
    CLASS_BY_KEY,
    CLASS_GROUPS,
    DETECTION_CLASSES,
    ROLE_BOTH,
    ROLE_COLLISION,
    ROLE_SHORT,
    ROLE_TRIGGER,
    SOURCE_AUTOMATIC,
    DetectedObject,
    ObjectBox,
    VideoObjects,
    effective_annotation,
    object_display_text,
    object_subtypes,
)
from investigator.theme import DANGER, MUTED, PRIMARY, material_icon
from investigator.widgets.scroll_safe_combo import ScrollSafeComboBox

ROLE_COLORS = {ROLE_BOTH: PRIMARY, ROLE_TRIGGER: PRIMARY, ROLE_COLLISION: DANGER}
SETTINGS_CLASSES = "detection/classes"
SETTINGS_CONFIDENCE_THRESHOLD = "detection/confidence_threshold"
DEFAULT_CONFIDENCE_THRESHOLD = 0.50
MAX_EXPANDED_TABLE_ROWS = 100
UID_ROLE = Qt.ItemDataRole.UserRole


def selected_class_keys(settings: QSettings | None = None) -> list[str]:
    stored = (settings or QSettings()).value(SETTINGS_CLASSES, None)
    if isinstance(stored, str):
        stored = [stored] if stored else []
    keys = list(dict.fromkeys(
        coarse_class_key(key) for key in (stored or []) if coarse_class_key(key) in CLASS_BY_KEY
    ))
    return keys if stored is not None else [item.key for item in DETECTION_CLASSES]


class ClassPickerDialog(QDialog):
    """Choose detection classes (union of the '객체' and '충돌' choices)."""

    def __init__(self, selected: list[str], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("검출 클래스 선택")
        root = QVBoxLayout(self)
        hint = QLabel(
            "트리거·충돌 관여 객체의 합집합을 검출합니다. "
            "검출용 분류는 조사 양식과 독립적으로 관리됩니다.",
            self,
        )
        hint.setWordWrap(True)
        hint.setStyleSheet(f"color: {MUTED};")
        root.addWidget(hint)
        self.checkboxes: dict[str, QCheckBox] = {}
        grid = QGridLayout()
        for column, group in enumerate(CLASS_GROUPS):
            box = QGroupBox(group, self)
            layout = QVBoxLayout(box)
            for item in DETECTION_CLASSES:
                if item.group != group:
                    continue
                checkbox = QCheckBox(item.label, box)
                checkbox.setChecked(item.key in selected)
                checkbox.setToolTip("프롬프트: " + ", ".join(item.prompts))
                layout.addWidget(checkbox)
                self.checkboxes[item.key] = checkbox
            layout.addStretch(1)
            grid.addWidget(box, 0, column)
        root.addLayout(grid)
        shortcuts = QHBoxLayout()
        select_all = QPushButton("전체 선택", self)
        select_none = QPushButton("전체 해제", self)
        select_all.clicked.connect(lambda: self._set_all(True))
        select_none.clicked.connect(lambda: self._set_all(False))
        shortcuts.addWidget(select_all)
        shortcuts.addWidget(select_none)
        shortcuts.addStretch(1)
        root.addLayout(shortcuts)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel, self
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

    def _set_all(self, checked: bool) -> None:
        for checkbox in self.checkboxes.values():
            checkbox.setChecked(checked)

    def selected(self) -> list[str]:
        return [key for key, checkbox in self.checkboxes.items() if checkbox.isChecked()]


class DetectionSettingsDialog(QDialog):
    def __init__(self, settings: DetectionSettings, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("객체 검출 환경")
        self.setMinimumWidth(520)
        root = QVBoxLayout(self)
        note = QLabel(
            "기본 실행·배포 모델은 Grounding DINO입니다. 박스만 검출하며 Base/Tiny를 선택할 수 있습니다. "
            "다른 모델 선택지는 기존 설정 호환용으로 유지되며 이번 배포 준비물에는 포함하지 않습니다. "
            "AVAT 본체와 같은 환경을 사용할 수 있습니다. "
            "별도 지정이 없으면 작업 공간의 detection-runtime.json을 사용하고, "
            "해당 파일도 없으면 AVAT를 실행한 Python을 사용합니다.",
            self,
        )
        note.setWordWrap(True)
        note.setStyleSheet(f"color: {MUTED};")
        root.addWidget(note)
        form = QFormLayout()
        self.backend_combo = ScrollSafeComboBox(self)
        self.backend_combo.addItem("Grounding DINO (기본 · 박스 전용)", "grounding_dino")
        self.backend_combo.addItem("YOLOE 11-L", "yoloe")
        self.backend_combo.addItem("SAM3", "sam3")
        self.backend_combo.addItem("EfficientSAM3 TV-M (경량·실험)", "efficientsam3")
        self.backend_combo.setCurrentIndex(max(0, self.backend_combo.findData(settings.backend)))
        form.addRow("검출 모델", self.backend_combo)
        self._active_backend = settings.backend
        self._backend_settings = {settings.backend: settings}
        python_row = QHBoxLayout()
        self.python_edit = QLineEdit(settings.python, self)
        self.python_edit.setPlaceholderText(r"예: C:\avat\.venv-grounding-dino\Scripts\python.exe")
        browse = QPushButton("찾기…", self)
        browse.clicked.connect(self._browse)
        python_row.addWidget(self.python_edit, 1)
        python_row.addWidget(browse)
        form.addRow("검출용 Python", python_row)
        self.model_edit = QLineEdit(settings.model, self)
        self.model_edit.setPlaceholderText(default_model(settings.backend) or DEFAULT_MODEL)
        form.addRow("모델 (파일·폴더 또는 ID)", self.model_edit)
        self.device_combo = ScrollSafeComboBox(self)
        for label, value in (("자동 (GPU 우선)", "auto"), ("GPU (CUDA)", "cuda"), ("CPU", "cpu")):
            self.device_combo.addItem(label, value)
        self.device_combo.setCurrentIndex(max(0, self.device_combo.findData(settings.device)))
        form.addRow("장치", self.device_combo)
        self.offline_check = QCheckBox("오프라인 실행 (로컬 모델만 사용)", self)
        self.offline_check.setChecked(bool(settings.offline))
        self.offline_check.setToolTip("인터넷 다운로드를 사용하지 않습니다. 완전한 로컬 모델 폴더를 지정해 주세요.")
        form.addRow("모델 연결", self.offline_check)
        self.dino_options_widget = QGroupBox("Grounding DINO 설정", self)
        dino_form = QFormLayout(self.dino_options_widget)
        self.dino_model_combo = ScrollSafeComboBox(self.dino_options_widget)
        self.dino_model_combo.addItem("Base (기본)", DINO_BASE_MODEL)
        self.dino_model_combo.addItem("Tiny", DINO_TINY_MODEL)
        self.dino_model_combo.addItem("직접 지정 (위 모델 경로·ID 사용)", "")
        dino_form.addRow("모델 크기", self.dino_model_combo)
        self.dino_chunk_spin = QSpinBox(self.dino_options_widget)
        self.dino_chunk_spin.setRange(1, 10000)
        self.dino_chunk_spin.setToolTip("한 번에 처리할 프롬프트 수입니다. 같은 대분류는 나누지 않아 이 수를 초과할 수 있습니다.")
        dino_form.addRow("프롬프트 청크 크기", self.dino_chunk_spin)
        self.dino_box_spin = QDoubleSpinBox(self.dino_options_widget)
        self.dino_text_spin = QDoubleSpinBox(self.dino_options_widget)
        for spin in (self.dino_box_spin, self.dino_text_spin):
            spin.setRange(0, 1)
            spin.setDecimals(3)
            spin.setSingleStep(0.05)
        self.dino_box_spin.setToolTip("모델 박스 임계값입니다. 이후 기존 클래스별 임계값도 적용됩니다.")
        self.dino_text_spin.setToolTip("검출 박스의 출력 텍스트를 선택할 토큰 임계값입니다.")
        dino_form.addRow("박스 임계값", self.dino_box_spin)
        dino_form.addRow("텍스트 임계값", self.dino_text_spin)
        self.dino_fp16_check = QCheckBox("GPU에서 FP16 사용", self.dino_options_widget)
        self.dino_fp16_check.setToolTip("CPU 실행 시 FP32를 사용합니다.")
        dino_form.addRow("정밀도", self.dino_fp16_check)
        self._set_dino_options(settings)
        form.addRow(self.dino_options_widget)
        self.dino_options_widget.setVisible(settings.backend == "grounding_dino")
        self.dino_model_combo.currentIndexChanged.connect(self._dino_model_changed)
        self.model_edit.textChanged.connect(self._sync_dino_model_selection)
        self._sync_dino_model_selection()
        root.addLayout(form)
        self.backend_combo.currentIndexChanged.connect(self._backend_changed)
        if settings.config_error:
            error = QLabel(settings.config_error, self)
            error.setWordWrap(True)
            error.setStyleSheet(f"color: {DANGER};")
            root.addWidget(error)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel, self
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

    def _browse(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "검출용 Python 선택", self.python_edit.text())
        if path:
            self.python_edit.setText(path)

    def _set_dino_options(self, settings: DetectionSettings) -> None:
        self.dino_chunk_spin.setValue(settings.dino_chunk_size)
        self.dino_box_spin.setValue(settings.dino_box_threshold)
        self.dino_text_spin.setValue(settings.dino_text_threshold)
        self.dino_fp16_check.setChecked(settings.dino_fp16)

    def _sync_dino_model_selection(self) -> None:
        model = self.model_edit.text().strip() or default_model(str(self.backend_combo.currentData()))
        variant = next((model_id for model_id in (DINO_BASE_MODEL, DINO_TINY_MODEL)
                        if model.replace("\\", "/").rstrip("/").split("/")[-1] == model_id.split("/")[-1]), "")
        with QSignalBlocker(self.dino_model_combo):
            self.dino_model_combo.setCurrentIndex(max(0, self.dino_model_combo.findData(variant)))

    def _dino_model_changed(self) -> None:
        model_id = self.dino_model_combo.currentData()
        if model_id:
            local = REPOSITORY_ROOT / "runtime/models" / str(model_id).split("/")[-1]
            self.model_edit.setText(str(local) if local.is_dir() else str(model_id))

    def _backend_changed(self) -> None:
        previous = self.settings()
        previous.backend = self._active_backend
        previous.model = self.model_edit.text().strip() or default_model(self._active_backend)
        self._backend_settings[self._active_backend] = previous
        backend = str(self.backend_combo.currentData())
        preset = self._backend_settings.get(backend)
        if preset is None:
            name = {
                "yoloe": "detection-runtime.yoloe.json",
                "efficientsam3": "detection-runtime.efficientsam3.json",
                "sam3": "detection-runtime.sam3.json",
                "grounding_dino": "detection-runtime.grounding-dino.json",
            }[backend]
            preset, _error = _load_runtime_manifest(REPOSITORY_ROOT / name)
            if preset is None and backend in {"sam3", "grounding_dino"}:
                preset, _error = _load_runtime_manifest(REPOSITORY_ROOT / "detection-runtime.json")
            if preset is not None and preset.backend != backend:
                preset = None
        if preset is not None:
            self.python_edit.setText(preset.python)
            self.model_edit.setText(preset.model)
            self.device_combo.setCurrentIndex(max(0, self.device_combo.findData(preset.device)))
            self.offline_check.setChecked(bool(preset.offline))
            self._set_dino_options(preset)
        else:
            # Never accidentally load the other architecture's weights.
            self.model_edit.clear()
            if backend == "grounding_dino":
                self._set_dino_options(DetectionSettings(backend=backend))
        self.model_edit.setPlaceholderText(
            "avat-efficient.json이 있는 로컬 모델 폴더"
            if backend == "efficientsam3" else default_model(backend)
        )
        self._active_backend = backend
        self.dino_options_widget.setVisible(backend == "grounding_dino")
        self._sync_dino_model_selection()

    def settings(self) -> DetectionSettings:
        return DetectionSettings(
            python=self.python_edit.text().strip(),
            model=self.model_edit.text().strip() or default_model(str(self.backend_combo.currentData())),
            device=str(self.device_combo.currentData()),
            offline=self.offline_check.isChecked(),
            backend=str(self.backend_combo.currentData()),
            dino_chunk_size=self.dino_chunk_spin.value(),
            dino_box_threshold=self.dino_box_spin.value(),
            dino_text_threshold=self.dino_text_spin.value(),
            dino_fp16=self.dino_fp16_check.isChecked(),
        )


class DetectionPanel(QWidget):
    run_requested = Signal()
    cancel_requested = Signal()
    settings_requested = Signal()
    range_changed = Signal(int, int)  # zero-based, inclusive
    seek_requested = Signal(int)
    selection_changed = Signal(list)
    label_change_requested = Signal(list, object)  # legacy integration
    subtype_change_requested = Signal(list, object, object, object)  # UIDs, subtype, frame/None, detail
    split_frame_requested = Signal(str)
    reviewed_change_requested = Signal(list, bool)
    delete_requested = Signal(list)
    draw_mode_toggled = Signal(bool)
    apply_requested = Signal(str)  # role
    confidence_threshold_changed = Signal(float)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("panel")
        self._total_frames = 0
        self._fps = 0.0
        self._current_frame = 0
        self._trigger: int | None = None
        self._collision: int | None = None
        self._objects = VideoObjects()
        self._objects_by_uid: dict[str, DetectedObject] = {}
        self._objects_by_frame: dict[int, list[DetectedObject]] = {}
        self._sorted_objects: list[DetectedObject] = []
        self._filtered_objects: list[DetectedObject] = []
        self._filtered_boxes: dict[str, list[ObjectBox]] = {}
        self._table_uids: tuple[str, ...] = ()
        self._table_rows_by_uid: dict[str, int] = {}
        self._definition: FormDefinition = FormDefinition.default()
        self._cache_label_options()
        self._running = False
        self._class_keys = selected_class_keys()
        self._updating_table = False
        self._responsive_rows: list[tuple[QBoxLayout, int]] = []

        root = QVBoxLayout(self)
        root.setContentsMargins(16, 8, 16, 8)
        root.setSpacing(6)

        # -- heading: title, classes, settings --------------------------------------
        heading = QBoxLayout(QBoxLayout.Direction.TopToBottom)
        self._responsive_rows.append((heading, 570))
        title = QLabel("객체 검출 · 라벨링", self)
        title.setObjectName("sectionTitle")
        heading.addWidget(title)
        heading_widget, heading_actions = self._control_row()
        heading_actions.addStretch(1)
        self.classes_button = QPushButton(self)
        self.classes_button.setIcon(material_icon("category", PRIMARY))
        heading_actions.addWidget(self.classes_button)
        self.settings_button = QToolButton(self)
        self.settings_button.setIcon(material_icon("settings", PRIMARY))
        self.settings_button.setToolTip("객체 검출 환경 (검출용 Python·모델·장치)")
        self.settings_button.setAccessibleName("객체 검출 환경")
        heading_actions.addWidget(self.settings_button)
        heading.addWidget(heading_widget)
        root.addLayout(heading)

        # -- range ---------------------------------------------------------------
        range_rows = QBoxLayout(QBoxLayout.Direction.TopToBottom)
        self._responsive_rows.append((range_rows, 820))
        range_widget, range_row = self._control_row()
        range_row.setSpacing(6)
        range_title = QLabel("검출 구간", self)
        range_title.setToolTip(
            "타임라인에서 Shift+드래그로도 구간을 선택할 수 있습니다.\n"
            "시작과 끝을 같게 지정하면 해당 프레임 하나만 검출합니다."
        )
        range_row.addWidget(range_title)
        self.start_spin = self._frame_spin(self)
        self.end_spin = self._frame_spin(self)
        self.start_spin.setToolTip("시작 프레임 (1부터)")
        self.end_spin.setToolTip("끝 프레임 (포함)")
        self.start_spin.setAccessibleName("시작 프레임")
        self.end_spin.setAccessibleName("끝 프레임")
        range_row.addWidget(self.start_spin)
        range_row.addWidget(QLabel("~", self))
        range_row.addWidget(self.end_spin)
        range_row.addStretch(1)
        range_rows.addWidget(range_widget)
        shortcuts_widget, shortcuts = self._control_row()
        self._responsive_rows.append((shortcuts, 1050))
        self.step_spin = QSpinBox(self)
        self.step_spin.setRange(1, 300)
        self.step_spin.setValue(1)
        self.step_spin.setPrefix("간격 ")
        self.step_spin.setMaximumWidth(96)
        self.step_spin.setToolTip("구간 안에서 몇 프레임마다 검출할지 정합니다.")
        shortcuts.addWidget(self.step_spin)
        self.start_here_button = QPushButton("현재 프레임을 시작으로 지정", self)
        self.start_here_button.setToolTip(
            "영상에 현재 표시된 프레임을 검출 구간의 시작 프레임으로 지정합니다."
        )
        self.end_here_button = QPushButton("현재 프레임을 끝으로 지정", self)
        self.end_here_button.setToolTip(
            "영상에 현재 표시된 프레임을 검출 구간의 끝 프레임으로 지정합니다 (끝 프레임 포함)."
        )
        self.trigger_collision_button = QPushButton("T~C", self)
        self.trigger_collision_button.setToolTip("트리거 마커부터 충돌 마커까지")
        for button in (self.start_here_button, self.end_here_button, self.trigger_collision_button):
            shortcuts.addWidget(button)
        shortcuts.addStretch(1)
        range_rows.addWidget(shortcuts_widget)
        root.addLayout(range_rows)

        # -- run -----------------------------------------------------------------
        run_widget, run_actions = self._control_row()
        self.range_summary = QLabel("", self)
        self.range_summary.setStyleSheet(f"color: {MUTED};")
        self.range_summary.setMinimumWidth(0)
        self.range_summary.setWordWrap(True)
        run_actions.addWidget(self.range_summary, 1)
        self.run_button = QPushButton(material_icon("play_circle", PRIMARY), "검출 실행", self)
        self.run_button.setObjectName("completeButton")
        self.cancel_button = QPushButton(material_icon("stop_circle", PRIMARY), "취소", self)
        self.cancel_button.setEnabled(False)
        run_actions.addWidget(self.run_button)
        run_actions.addWidget(self.cancel_button)
        root.addWidget(run_widget)

        status_row = QHBoxLayout()
        self.progress = QProgressBar(self)
        self.progress.setRange(0, 1)
        self.progress.setValue(0)
        self.progress.setTextVisible(True)
        self.progress.setFormat("%v / %m")
        self.progress.setFixedWidth(150)
        self.status_label = QLabel("", self)
        self.status_label.setWordWrap(True)
        self.status_label.setStyleSheet(f"color: {MUTED};")
        status_row.addWidget(self.progress)
        status_row.addWidget(self.status_label, 1)
        root.addLayout(status_row)

        # -- objects ------------------------------------------------------------
        list_rows = QBoxLayout(QBoxLayout.Direction.TopToBottom)
        self._responsive_rows.append((list_rows, 920))
        list_widget, list_header = self._control_row()
        self.objects_title = QLabel("검출 객체 0개", self)
        self.objects_title.setStyleSheet("font-weight: 650;")
        self.objects_title.setMinimumWidth(0)
        self.objects_title.setWordWrap(True)
        list_header.addWidget(self.objects_title)
        list_header.addStretch(1)
        self.current_only = QCheckBox("현재 프레임만", self)
        list_header.addWidget(self.current_only)
        list_rows.addWidget(list_widget)
        confidence_widget, confidence_row = self._control_row()
        confidence_row.addWidget(QLabel("표시 신뢰도 ≥", self))
        self.confidence_spin = QDoubleSpinBox(self)
        self.confidence_spin.setRange(0.0, 1.0)
        self.confidence_spin.setDecimals(2)
        self.confidence_spin.setSingleStep(0.05)
        try:
            confidence = float(
                QSettings().value(SETTINGS_CONFIDENCE_THRESHOLD, DEFAULT_CONFIDENCE_THRESHOLD)
            )
            if not math.isfinite(confidence):
                raise ValueError
        except (TypeError, ValueError, OverflowError):
            confidence = DEFAULT_CONFIDENCE_THRESHOLD
        self.confidence_spin.setValue(min(1.0, max(0.0, confidence)))
        self.confidence_spin.setMaximumWidth(96)
        self.confidence_spin.setToolTip(
            "이 값 이상의 신뢰도를 가진 자동 검출 박스만 영상과 표에 표시합니다.\n"
            "저장된 결과는 삭제하지 않으며 값을 낮추면 다시 표시됩니다.\n"
            "재검출 없이 즉시 적용되며, 직접 그린 박스는 항상 표시합니다."
        )
        confidence_row.addWidget(self.confidence_spin)
        confidence_row.addStretch(1)
        list_rows.addWidget(confidence_widget)
        apply_rows = QBoxLayout(QBoxLayout.Direction.TopToBottom)
        self._responsive_rows.append((apply_rows, 630))
        draw_widget, draw_row = self._control_row()
        self.draw_button = QPushButton(
            material_icon("highlight_alt", PRIMARY), "사각형 그리기", self
        )
        self.draw_button.setCheckable(True)
        self.draw_button.setToolTip(
            "영상 위를 드래그해 공통 객체를 직접 추가합니다.\n"
            "Ctrl+Z 실행 취소 · Ctrl+Y / Ctrl+Shift+Z 다시 실행"
        )
        draw_row.addWidget(self.draw_button)
        draw_row.addStretch(1)
        apply_rows.addWidget(draw_widget)
        apply_widget, apply_row = self._control_row()
        self.apply_trigger_button = QPushButton(
            material_icon("playlist_add_check", PRIMARY), "객체 탭에 반영", self
        )
        self.apply_trigger_button.setToolTip(
            "트리거 시점 관여 객체의 라벨을 '객체' 탭 장애물 항목에 추가합니다."
        )
        self.apply_collision_button = QPushButton(
            material_icon("playlist_add_check", PRIMARY), "충돌 탭에 반영", self
        )
        self.apply_collision_button.setToolTip(
            "충돌 관여 객체의 라벨을 '충돌' 탭 충돌 대상에 추가합니다."
        )
        apply_row.addWidget(self.apply_trigger_button)
        apply_row.addWidget(self.apply_collision_button)
        apply_row.addStretch(1)
        apply_rows.addWidget(apply_widget)
        list_rows.addLayout(apply_rows)
        root.addLayout(list_rows)

        self.table = QTableWidget(0, 7, self)
        self.table.setHorizontalHeaderLabels(
            ["ID", "역할", "라벨", "프레임", "박스", "신뢰도", "상태"]
        )
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().hide()
        self.table.verticalHeader().setDefaultSectionSize(26)
        self.table.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.table.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.table.setToolTip("더블클릭하면 객체가 있는 프레임으로 이동합니다.")
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        header.setResizeContentsPrecision(100)
        header.geometriesChanged.connect(self._resize_object_table)
        self.table.verticalHeader().sectionResized.connect(self._resize_object_table)
        self.table.horizontalScrollBar().rangeChanged.connect(self._resize_object_table)
        root.addWidget(self.table)

        edit_rows = QBoxLayout(QBoxLayout.Direction.TopToBottom)
        self._responsive_rows.append((edit_rows, 1040))
        combo_widget = QWidget(self)
        combo_row = QBoxLayout(QBoxLayout.Direction.TopToBottom, combo_widget)
        combo_row.setContentsMargins(0, 0, 0, 0)
        self._responsive_rows.append((combo_row, 620))
        self.label_combo = ScrollSafeComboBox(self)
        self.label_combo.setMinimumContentsLength(10)
        self.label_combo.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon
        )
        self.label_combo.setMinimumWidth(0)
        self.label_combo.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        combo_row.addWidget(self.label_combo, 1)
        self.special_combo = ScrollSafeComboBox(self)
        self.special_combo.setToolTip("특수차량 상세 종류 (선택 입력)")
        self.special_combo.addItem("특수차량 상세 (선택)", None)
        for key, label in SPECIAL_VEHICLE_TYPES.items():
            self.special_combo.addItem(label, key)
        combo_row.addWidget(self.special_combo)
        self.special_combo.setMinimumWidth(0)
        self.special_combo.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        edit_rows.addWidget(combo_widget, 1)
        self.all_id_frames = QCheckBox("동일 ID 전체 프레임에 적용", self)
        self.all_id_frames.setChecked(True)
        self.all_id_frames.setToolTip("해제하면 현재 프레임의 세분류·검수 상태만 수정합니다.")
        root.addWidget(self.all_id_frames)
        edit_widget, edit_row = self._control_row()
        self._responsive_rows.append((edit_row, 720))
        self.reviewed_button = QPushButton(material_icon("task_alt", PRIMARY), "확인", self)
        self.reviewed_button.setCheckable(True)
        self.reviewed_button.setToolTip(
            "검토 완료 표시. 확인한 자동 객체는 재검출 때 교체되지 않습니다."
        )
        self.split_frame_button = QPushButton("현재 프레임을 새 ID로 분리", self)
        self.split_frame_button.setToolTip("현재 프레임 박스를 새 객체 ID로 옮깁니다. 좌표는 변경하지 않습니다.")
        self.delete_button = QPushButton(material_icon("delete", DANGER), "객체 삭제", self)
        edit_row.addWidget(self.reviewed_button)
        edit_row.addWidget(self.split_frame_button)
        edit_row.addWidget(self.delete_button)
        edit_row.addStretch(1)
        edit_rows.addWidget(edit_widget)
        root.addLayout(edit_rows)
        root.addStretch(1)

        self.settings_button.clicked.connect(self.settings_requested)
        self.start_spin.valueChanged.connect(self._range_edited)
        self.end_spin.valueChanged.connect(self._range_edited)
        self.step_spin.valueChanged.connect(self._update_summary)
        self.start_here_button.clicked.connect(lambda: self._set_range_edge(start=True))
        self.end_here_button.clicked.connect(lambda: self._set_range_edge(start=False))
        self.trigger_collision_button.clicked.connect(self._range_trigger_to_collision)
        self.classes_button.clicked.connect(self._choose_classes)
        self.run_button.clicked.connect(self.run_requested)
        self.cancel_button.clicked.connect(self.cancel_requested)
        self.current_only.toggled.connect(lambda _checked: self._rebuild_table())
        self.confidence_spin.valueChanged.connect(self._confidence_changed)
        self.draw_button.toggled.connect(self.draw_mode_toggled)
        self.table.itemSelectionChanged.connect(self._table_selection_changed)
        self.table.cellDoubleClicked.connect(self._row_activated)
        self.label_combo.activated.connect(self._label_chosen)
        self.special_combo.activated.connect(lambda _index: self._label_chosen(self.label_combo.currentIndex()))
        self.all_id_frames.toggled.connect(lambda _checked: self._update_editor())
        self.reviewed_button.clicked.connect(
            lambda checked: self._emit_for_selection(self.reviewed_change_requested, bool(checked))
        )
        self.delete_button.clicked.connect(lambda: self._emit_for_selection(self.delete_requested))
        self.split_frame_button.clicked.connect(self._split_frame)
        self.apply_trigger_button.clicked.connect(lambda: self.apply_requested.emit(ROLE_TRIGGER))
        self.apply_collision_button.clicked.connect(
            lambda: self.apply_requested.emit(ROLE_COLLISION)
        )
        self._update_classes_button()
        self._update_editor()
        self.set_video(0, 0.0)
        self._resize_object_table()
        self._update_responsive_layout()

    def _control_row(self) -> tuple[QWidget, QHBoxLayout]:
        widget = QWidget(self)
        layout = QHBoxLayout(widget)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        return widget, layout

    def _update_responsive_layout(self) -> None:
        for layout, threshold in self._responsive_rows:
            direction = (
                QBoxLayout.Direction.TopToBottom
                if self.width() < threshold
                else QBoxLayout.Direction.LeftToRight
            )
            if layout.direction() != direction:
                layout.setDirection(direction)

    def resizeEvent(self, event) -> None:  # type: ignore[no-untyped-def]
        super().resizeEvent(event)
        self._update_responsive_layout()

    @staticmethod
    def _frame_spin(parent: QWidget) -> QSpinBox:
        spin = QSpinBox(parent)
        spin.setRange(1, 1)
        spin.setGroupSeparatorShown(True)
        spin.setMinimumWidth(78)
        spin.setMaximumWidth(96)
        return spin

    # -- inputs from the main window -------------------------------------------

    def set_video(self, total_frames: int, fps: float) -> None:
        self._total_frames = max(0, int(total_frames))
        self._fps = max(0.0, float(fps))
        maximum = max(1, self._total_frames)
        for spin in (self.start_spin, self.end_spin):
            with QSignalBlocker(spin):
                spin.setRange(1, maximum)
        with QSignalBlocker(self.start_spin), QSignalBlocker(self.end_spin):
            self.start_spin.setValue(1)
            self.end_spin.setValue(1)
        self.set_current_frame(0)
        self.draw_button.setEnabled(self._total_frames > 0 and not self._running)
        self._update_range_enabled()
        self._update_summary()
        self.range_changed.emit(*self.frame_range)
        self.set_markers(self._trigger, self._collision)
        self._update_run_enabled()

    def set_current_frame(self, frame: int) -> None:
        self._current_frame = max(0, int(frame))
        if self.current_only.isChecked() and (
            tuple(item.uid for item in self._visible_objects()) != self._table_uids
        ):
            self._rebuild_table()
        else:
            # Frame changes affect only this action. Rebuilding label options on
            # every playback/scrub frame would needlessly repopulate the combo.
            items = self._selected_objects()
            self.split_frame_button.setEnabled(
                len(items) == 1 and items[0].box_at(self._current_frame) is not None
            )
            if not self.all_id_frames.isChecked():
                self._update_editor()

    def set_markers(self, trigger: int | None, collision: int | None) -> None:
        self._trigger, self._collision = trigger, collision
        self.trigger_collision_button.setEnabled(
            self._total_frames > 0
            and trigger is not None
            and collision is not None
            and not self._running
        )

    def set_objects(self, objects: VideoObjects, definition: FormDefinition) -> None:
        selected = self.selected_uids()
        self._objects = objects
        self._definition = definition
        self._cache_label_options()
        self._objects_by_uid = {item.uid: item for item in objects.objects}
        self._sorted_objects = sorted(
            objects.objects, key=lambda item: (item.first_frame, item.object_id)
        )
        self._cache_filtered_objects()
        self._rebuild_table(selected)

    def set_selected_range(self, start: int, end: int) -> None:
        """Range picked on the timeline (zero-based, inclusive)."""

        self._apply_range(start, end, emit=False)

    def set_running(self, running: bool) -> None:
        self._running = running
        self.cancel_button.setEnabled(running)
        for widget in (
            self.classes_button,
            self.settings_button,
        ):
            widget.setEnabled(not running)
        if running:
            self.progress.setRange(0, 0)
        else:
            self.progress.setRange(0, 1)
            self.progress.setValue(0)
        self.set_markers(self._trigger, self._collision)
        self.draw_button.setEnabled(not running and self._total_frames > 0)
        self._update_range_enabled()
        self._update_run_enabled()

    def set_progress(self, processed: int, total: int) -> None:
        self.progress.setRange(0, max(1, total))
        self.progress.setValue(processed)

    def set_status(self, text: str) -> None:
        self.status_label.setText(text)

    # -- range ---------------------------------------------------------------

    @property
    def frame_range(self) -> tuple[int, int]:
        """Zero-based inclusive range."""

        start, end = self.start_spin.value() - 1, self.end_spin.value() - 1
        return (min(start, end), max(start, end))

    @property
    def frame_step(self) -> int:
        return self.step_spin.value()

    @property
    def new_object_role(self) -> str:
        return ROLE_BOTH

    def set_new_object_role(self, role: str) -> None:
        """Compatibility hook: new objects are common to both investigation tabs."""

    @property
    def class_keys(self) -> list[str]:
        return list(self._class_keys)

    @property
    def confidence_threshold(self) -> float:
        """Minimum visible automatic-box score; raw saved results are untouched."""

        return self.confidence_spin.value()

    def _confidence_changed(self, value: float) -> None:
        QSettings().setValue(SETTINGS_CONFIDENCE_THRESHOLD, value)
        self._cache_filtered_objects()
        self._rebuild_table()
        self.confidence_threshold_changed.emit(value)
        self.selection_changed.emit(self.selected_uids())

    def _cache_label_options(self) -> None:
        # Detection labels never follow editable investigation choices.
        self._subtype_options = {item.key: subtype_options(item.key) for item in DETECTION_CLASSES}

    def _cache_filtered_objects(self) -> None:
        threshold = self.confidence_threshold
        self._filtered_objects = []
        self._filtered_boxes = {}
        self._objects_by_frame = {}
        for item in self._sorted_objects:
            boxes = [
                box
                for box in item.boxes
                if item.source != SOURCE_AUTOMATIC or box.score is None or box.score >= threshold
            ]
            if not boxes:
                continue
            self._filtered_objects.append(item)
            self._filtered_boxes[item.uid] = boxes
            for frame in {box.frame for box in boxes}:
                self._objects_by_frame.setdefault(frame, []).append(item)

    def _update_range_enabled(self) -> None:
        for widget in (
            self.start_spin,
            self.end_spin,
            self.step_spin,
            self.start_here_button,
            self.end_here_button,
        ):
            widget.setEnabled(not self._running and self._total_frames > 0)

    def _apply_range(self, start: int, end: int, *, emit: bool) -> None:
        start, end = sorted((max(0, int(start)), max(0, int(end))))
        with QSignalBlocker(self.start_spin), QSignalBlocker(self.end_spin):
            self.start_spin.setValue(start + 1)
            self.end_spin.setValue(end + 1)
        self._update_summary()
        if emit:
            self.range_changed.emit(*self.frame_range)

    def _range_edited(self) -> None:
        self._update_summary()
        self.range_changed.emit(*self.frame_range)

    def _set_range_edge(self, *, start: bool) -> None:
        low, high = self.frame_range
        if start:
            low = self._current_frame
            high = max(high, low)
        else:
            high = self._current_frame
            low = min(low, high)
        self._apply_range(low, high, emit=True)

    def _range_trigger_to_collision(self) -> None:
        if self._trigger is None or self._collision is None:
            return
        self._apply_range(self._trigger, self._collision, emit=True)

    def processed_frame_count(self) -> int:
        start, end = self.frame_range
        return len(range(start, end + 1, self.frame_step))

    def _update_summary(self) -> None:
        start, end = self.frame_range
        count = self.processed_frame_count()
        if start == end:
            text = f"{start + 1:,}번 프레임"
        else:
            seconds = (end - start + 1) / self._fps if self._fps else 0.0
            text = f"{end - start + 1:,}프레임 ({seconds:.1f}초) · 처리 {count:,}장"
        self.range_summary.setText(text)

    # -- classes -------------------------------------------------------------

    def _choose_classes(self) -> None:
        dialog = ClassPickerDialog(self._class_keys, self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self._class_keys = dialog.selected()
            QSettings().setValue(SETTINGS_CLASSES, self._class_keys)
            self._update_classes_button()
            self._update_run_enabled()

    def _update_classes_button(self) -> None:
        self.classes_button.setText(f"검출 클래스 {len(self._class_keys)}/{len(DETECTION_CLASSES)}")
        names = ", ".join(CLASS_BY_KEY[key].label for key in self._class_keys) or "선택 없음"
        self.classes_button.setToolTip(names)

    def _update_run_enabled(self) -> None:
        self.run_button.setEnabled(
            not self._running and self._total_frames > 0 and bool(self._class_keys)
        )

    # -- object table ----------------------------------------------------------

    def _resize_object_table(self, *_args: object) -> None:
        """Fit visible rows, leaving vertical scrolling to the containing panel."""

        header = self.table.horizontalHeader()
        spare = self.table.viewport().width() - sum(
            header.sectionSize(column) for column in range(self.table.columnCount()) if column != 2
        )
        # Narrow panels scroll inside the table rather than collapsing labels
        # to a few clipped characters or forcing the parent panel sideways.
        with QSignalBlocker(header), QSignalBlocker(self.table.horizontalScrollBar()):
            if spare < 190:
                header.setSectionResizeMode(2, QHeaderView.ResizeMode.Interactive)
                if header.sectionSize(2) < 190:
                    header.resizeSection(2, 190)
            else:
                header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        rows = self.table.verticalHeader()
        # Keep one blank row when no objects match, without a large empty table.
        # Thousands of objects must not allocate a tens-of-thousands-pixel
        # viewport. Keep small tables fluid, then scroll the table itself.
        capped = self.table.rowCount() > MAX_EXPANDED_TABLE_ROWS
        policy = (
            Qt.ScrollBarPolicy.ScrollBarAsNeeded
            if capped
            else Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        if self.table.verticalScrollBarPolicy() != policy:
            self.table.setVerticalScrollBarPolicy(policy)
        body_height = max(
            min(rows.length(), rows.defaultSectionSize() * MAX_EXPANDED_TABLE_ROWS),
            rows.defaultSectionSize(),
        )
        height = self.table.horizontalHeader().height() + body_height + 2 * self.table.frameWidth()
        horizontal = self.table.horizontalScrollBar()
        if horizontal.maximum() > horizontal.minimum():
            height += horizontal.sizeHint().height()
        if self.table.minimumHeight() != height or self.table.maximumHeight() != height:
            self.table.setFixedHeight(height)

    def _visible_objects(self) -> list[DetectedObject]:
        if self.current_only.isChecked():
            return self._objects_by_frame.get(self._current_frame, [])
        return self._filtered_objects

    def _rebuild_table(self, selected: list[str] | None = None) -> None:
        selected = self.selected_uids() if selected is None else selected
        self._updating_table = True
        self.table.setUpdatesEnabled(False)
        header = self.table.horizontalHeader()
        # ResizeToContents during each setItem() performs repeated scans of
        # every populated row. Resize once after the full batch instead.
        header.setSectionResizeMode(QHeaderView.ResizeMode.Fixed)
        try:
            with QSignalBlocker(self.table):
                visible = self._visible_objects()
                self._table_uids = tuple(item.uid for item in visible)
                self._table_rows_by_uid = {uid: row for row, uid in enumerate(self._table_uids)}
                self.table.setRowCount(len(visible))
                for row, item in enumerate(visible):
                    boxes = self._filtered_boxes[item.uid]
                    scores = [box.score for box in boxes if box.score is not None]
                    score = sum(scores) / len(scores) if scores else None
                    first, last = min(box.frame for box in boxes), max(box.frame for box in boxes)
                    frames = f"{first + 1:,}" if first == last else f"{first + 1:,}~{last + 1:,}"
                    status = (
                        "확인"
                        if item.reviewed
                        else ("자동" if item.source == SOURCE_AUTOMATIC else "수동")
                    )
                    text = object_display_text(item)
                    cells = [
                        item.object_id,
                        ROLE_SHORT[item.role],
                        text,
                        frames,
                        str(len(boxes)),
                        "-" if score is None else f"{score:.2f}",
                        status,
                    ]
                    for column, text in enumerate(cells):
                        cell = QTableWidgetItem(text)
                        cell.setToolTip(text)
                        cell.setData(UID_ROLE, item.uid)
                        if column == 1:
                            cell.setForeground(QColor(ROLE_COLORS[item.role]))
                        if column == 2 and (None in object_subtypes(item) or len(object_subtypes(item)) > 1):
                            cell.setForeground(QColor(DANGER))
                        self.table.setItem(row, column, cell)
                self._select_existing_rows(selected)
        finally:
            header.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
            header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
            self._updating_table = False
            self.table.setUpdatesEnabled(True)
        self._resize_object_table()
        unlabeled = sum(1 for item in self._objects.objects if None in object_subtypes(item))
        mixed = sum(1 for item in self._objects.objects if len(object_subtypes(item)) > 1)
        total = len(self._objects.objects)
        self.objects_title.setText(
            f"검출 객체 {total}개 · 표시 {len(self._table_uids)}개"
            + (f" · 라벨 미지정 {unlabeled}개" if unlabeled else "")
            + (f" · 혼합 ID {mixed}개 (반영 불가)" if mixed else "")
        )
        self.apply_trigger_button.setEnabled(
            any(i.role in (ROLE_TRIGGER, ROLE_BOTH) for i in self._objects.objects)
        )
        self.apply_collision_button.setEnabled(
            any(i.role in (ROLE_COLLISION, ROLE_BOTH) for i in self._objects.objects)
        )
        self._update_editor()

    def _add_row_selection(self, row: int) -> None:
        model = self.table.selectionModel()
        index = self.table.model().index(row, 0)
        model.select(
            index,
            model.SelectionFlag.Select | model.SelectionFlag.Rows,
        )

    def selected_uids(self) -> list[str]:
        rows = sorted({index.row() for index in self.table.selectionModel().selectedRows()})
        return [self.table.item(row, 0).data(UID_ROLE) for row in rows if self.table.item(row, 0)]

    def select_uids(self, uids: list[str]) -> None:
        wanted = set(uids).intersection(self._table_rows_by_uid)
        if wanted == set(self.selected_uids()):
            return
        # Canvas clicks change selection only, not data. Recreating all seven
        # cells for thousands of rows here used to freeze the GUI thread.
        with QSignalBlocker(self.table):
            rows = self._select_existing_rows(wanted)
        self._update_editor()
        if rows:
            self.table.scrollToItem(self.table.item(rows[0], 0))
        self.selection_changed.emit(self.selected_uids())

    def _select_existing_rows(self, uids) -> list[int]:  # type: ignore[no-untyped-def]
        rows = sorted({self._table_rows_by_uid[uid] for uid in uids if uid in self._table_rows_by_uid})
        selection = QItemSelection()
        model = self.table.model()
        for row in rows:
            selection.select(model.index(row, 0), model.index(row, self.table.columnCount() - 1))
        selection_model = self.table.selectionModel()
        selection_model.select(
            selection,
            selection_model.SelectionFlag.ClearAndSelect | selection_model.SelectionFlag.Rows,
        )
        return rows

    def _table_selection_changed(self) -> None:
        if self._updating_table:
            return
        self._update_editor()
        self.selection_changed.emit(self.selected_uids())

    def _row_activated(self, row: int, _column: int) -> None:
        uid = self.table.item(row, 0).data(UID_ROLE)
        item = self._objects.find(uid)
        if item is not None:
            boxes = self._filtered_boxes.get(uid, [])
            if boxes:
                self.seek_requested.emit(
                    min((box.frame for box in boxes), key=lambda f: (abs(f - self._current_frame), f))
                )

    def _selected_objects(self) -> list[DetectedObject]:
        return [item for uid in self.selected_uids() if (item := self._objects_by_uid.get(uid))]

    def _update_editor(self) -> None:
        items = self._selected_objects()
        has_selection = bool(items)
        for widget in (self.reviewed_button, self.delete_button):
            widget.setEnabled(has_selection)
        self.split_frame_button.setEnabled(
            len(items) == 1 and items[0].box_at(self._current_frame) is not None
        )
        groups = {item.class_key for item in items}
        whole_id = self.all_id_frames.isChecked()
        editable = has_selection and len(groups) == 1 and (
            whole_id or all(item.box_at(self._current_frame) is not None for item in items)
        )
        annotations = [effective_annotation(item, self._current_frame) for item in items]
        labels = set().union(*(object_subtypes(item) for item in items)) if whole_id else {
            annotation.subtype_key for annotation in annotations
        }
        with QSignalBlocker(self.label_combo):
            self.label_combo.clear()
            self.label_combo.addItem("세분류 미지정", None)
            options = (
                SUBTYPES if groups == {None}
                else self._subtype_options.get(next(iter(groups)), ()) if len(groups) == 1 else ()
            )
            for option in options:
                self.label_combo.addItem(option.label, option.key)
            self.label_combo.setCurrentIndex(
                max(0, self.label_combo.findData(next(iter(labels)))) if len(labels) == 1 else -1
            )
            self.label_combo.setPlaceholderText("혼합 · 세분류 통일 필요")
            self.label_combo.setEnabled(editable)
            self.label_combo.setToolTip(
                "검출용 세분류 — 조사 양식 편집과 독립적입니다."
                if editable else "같은 대분류의 객체를 선택해 주세요. 현재 프레임 적용 시 박스가 있어야 합니다."
            )
        is_special = labels == {"special"}
        with QSignalBlocker(self.special_combo):
            self.special_combo.setVisible(is_special)
            self.special_combo.setEnabled(editable and is_special)
            details = {annotation.special_vehicle_type for annotation in annotations}
            detail = next(iter(details)) if len(details) == 1 else None
            self.special_combo.setCurrentIndex(max(0, self.special_combo.findData(detail)))
        with QSignalBlocker(self.reviewed_button):
            self.reviewed_button.setChecked(has_selection and all(
                item.reviewed if whole_id else annotation.reviewed
                for item, annotation in zip(items, annotations, strict=True)
            ))

    def _emit_for_selection(self, signal, *args) -> None:  # type: ignore[no-untyped-def]
        uids = self.selected_uids()
        if uids:
            signal.emit(uids, *args)

    def _label_chosen(self, index: int) -> None:
        if index < 0:
            return
        subtype = self.label_combo.itemData(index)
        frame = None if self.all_id_frames.isChecked() else self._current_frame
        detail = self.special_combo.currentData() if subtype == "special" else None
        self._emit_for_selection(self.subtype_change_requested, subtype, frame, detail)

    def _split_frame(self) -> None:
        uids = self.selected_uids()
        if len(uids) == 1:
            self.split_frame_requested.emit(uids[0])


__all__ = [
    "ClassPickerDialog",
    "DetectionPanel",
    "DetectionSettingsDialog",
    "ROLE_COLORS",
    "selected_class_keys",
]
