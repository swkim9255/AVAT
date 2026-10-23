"""객체 검출 worker를 별도 Python 프로세스(QProcess)에서 실행한다.

The worker can use the same Python environment as AVAT, or an explicitly
configured interpreter. It reports JSONL events on stdout and writes the
result document only on success, so a cancelled or failed run leaves nothing
behind for the GUI to merge.
"""

from __future__ import annotations

import json
import math
import shutil
import sys
import tempfile
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path

from PySide6.QtCore import QObject, QProcess, QProcessEnvironment, QSettings, QTimer, Signal

from investigator.object_detection import worker_class_config

REPOSITORY_ROOT = Path(__file__).resolve().parent.parent
SETTINGS_PYTHON = "detection/python"
SETTINGS_MODEL = "detection/model"
SETTINGS_DEVICE = "detection/device"
SETTINGS_OFFLINE = "detection/offline"
SETTINGS_BACKEND = "detection/backend"
SETTINGS_DINO_CHUNK_SIZE = "detection/dino_chunk_size"
SETTINGS_DINO_BOX_THRESHOLD = "detection/dino_box_threshold"
SETTINGS_DINO_TEXT_THRESHOLD = "detection/dino_text_threshold"
SETTINGS_DINO_FP16 = "detection/dino_fp16"
RUNTIME_MANIFEST = "detection-runtime.json"
DEFAULT_BACKEND = "grounding_dino"
SAM3_MODEL = "facebook/sam3"
DINO_BASE_MODEL = "IDEA-Research/grounding-dino-base"
DINO_TINY_MODEL = "IDEA-Research/grounding-dino-tiny"
YOLOE_MODEL = str(REPOSITORY_ROOT / "runtime/models/yoloe-11l/yoloe-11l-seg.pt")
DEFAULT_MODEL = DINO_BASE_MODEL


def default_model(backend: str) -> str:
    return {"yoloe": YOLOE_MODEL, "sam3": SAM3_MODEL, "efficientsam3": "",
            "grounding_dino": DINO_BASE_MODEL}.get(backend, "")
CANCEL_GRACE_MS = 15_000
MAX_EVENT_BUFFER = 256 * 1024
MISSING_RUNTIME_HINT = (
    "객체 검출 실행 환경(torch·torchvision·transformers)을 찾지 못했습니다. "
    "설정 → 객체 검출 환경에서 검출용 Python을 지정해 주세요."
)


@dataclass(slots=True)
class DetectionSettings:
    python: str = ""
    model: str = DEFAULT_MODEL
    device: str = "auto"
    offline: bool | None = None
    config_error: str = ""
    backend: str = DEFAULT_BACKEND
    dino_chunk_size: int = 8
    dino_box_threshold: float = 0.3
    dino_text_threshold: float = 0.25
    dino_fp16: bool = True

    @property
    def executable(self) -> str:
        return self.python.strip() or sys.executable

    @classmethod
    def load(cls, settings: QSettings | None = None) -> DetectionSettings:
        store = settings or QSettings()
        native, manifest_error = _load_runtime_manifest(REPOSITORY_ROOT / RUNTIME_MANIFEST)
        python = str(store.value(SETTINGS_PYTHON, "") or "").strip()
        # A deliberately configured interpreter keeps its complete environment
        # settings. Merely opening AVAT must never change the user's QSettings.
        # An unused broken workspace manifest must not disable this override.
        if not python and native is not None:
            return native
        device = str(store.value(SETTINGS_DEVICE, "auto") or "auto")
        offline = native.offline if native is not None else None
        if store.contains(SETTINGS_OFFLINE):
            offline = _setting_bool(store.value(SETTINGS_OFFLINE), bool(offline))
        # Old explicit settings predate backend selection and refer to SAM3.
        legacy = bool(python) or store.contains(SETTINGS_MODEL)
        backend = str(store.value(SETTINGS_BACKEND, "sam3" if legacy else DEFAULT_BACKEND))
        dino_options, dino_error = {}, ""
        if backend == "grounding_dino":
            try:
                dino_options = _dino_options({
                    "dino_chunk_size": store.value(SETTINGS_DINO_CHUNK_SIZE, 8),
                    "dino_box_threshold": store.value(SETTINGS_DINO_BOX_THRESHOLD, 0.3),
                    "dino_text_threshold": store.value(SETTINGS_DINO_TEXT_THRESHOLD, 0.25),
                    "dino_fp16": _setting_bool(store.value(SETTINGS_DINO_FP16, True), True),
                }, from_settings=True)
            except ValueError as exc:
                dino_error = str(exc)
        return cls(
            python=python,
            model=str(store.value(SETTINGS_MODEL, default_model(backend)) or default_model(backend)),
            device=device if device in {"auto", "cuda", "cpu"} else "auto",
            offline=offline,
            config_error=(manifest_error if not python else "") or dino_error,
            backend=backend,
            **dino_options,
        )

    def save(self, settings: QSettings | None = None) -> None:
        store = settings or QSettings()
        store.setValue(SETTINGS_PYTHON, self.python.strip())
        store.setValue(SETTINGS_MODEL, self.model.strip() or default_model(self.backend))
        store.setValue(SETTINGS_DEVICE, self.device)
        store.setValue(SETTINGS_BACKEND, self.backend)
        store.setValue(SETTINGS_DINO_CHUNK_SIZE, self.dino_chunk_size)
        store.setValue(SETTINGS_DINO_BOX_THRESHOLD, self.dino_box_threshold)
        store.setValue(SETTINGS_DINO_TEXT_THRESHOLD, self.dino_text_threshold)
        store.setValue(SETTINGS_DINO_FP16, self.dino_fp16)
        if self.offline is not None:
            store.setValue(SETTINGS_OFFLINE, self.offline)


def _setting_bool(value: object, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in {"true", "1"}:
        return True
    if text in {"false", "0"}:
        return False
    return default


def _dino_options(data: dict, *, from_settings: bool = False) -> dict:
    chunk = data.get("dino_chunk_size", 8)
    if from_settings and isinstance(chunk, str):
        with suppress(ValueError):
            chunk = int(chunk)
    if isinstance(chunk, bool) or not isinstance(chunk, int) or chunk < 1:
        raise ValueError("Grounding DINO 청크 크기는 1 이상의 정수여야 합니다.")
    options = {"dino_chunk_size": chunk}
    for key, default in (("dino_box_threshold", 0.3), ("dino_text_threshold", 0.25)):
        value = data.get(key, default)
        if from_settings and isinstance(value, str):
            with suppress(ValueError):
                value = float(value)
        if (isinstance(value, bool) or not isinstance(value, (int, float))
                or not 0 <= value <= 1 or not math.isfinite(value)):
            raise ValueError(f"Grounding DINO {key}는 0~1의 유한한 숫자여야 합니다.")
        options[key] = float(value)
    fp16 = data.get("dino_fp16", True)
    if not isinstance(fp16, bool):
        raise ValueError("Grounding DINO dino_fp16은 true 또는 false여야 합니다.")
    options["dino_fp16"] = fp16
    return options


def _validate_dino_model(model: str, *, offline: bool = False) -> None:
    """Reject mistaken local folders before starting the native worker."""
    path = Path(model).expanduser()
    local = (path.exists() or path.is_absolute() or model.startswith((".", "~"))
             or "\\" in model or model.count("/") != 1)
    if not local:
        if offline:
            raise ValueError("오프라인 Grounding DINO 실행에는 로컬 모델 폴더를 지정해 주세요.")
        return
    if not path.is_dir():
        raise ValueError("Grounding DINO 로컬 모델 폴더가 없습니다.")
    try:
        config = json.loads((path / "config.json").read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        raise ValueError("Grounding DINO 로컬 모델 폴더의 config.json을 확인해 주세요.") from exc
    if not isinstance(config, dict) or config.get("model_type") not in {"grounding-dino", "grounding_dino"}:
        raise ValueError("Grounding DINO 모델 폴더가 아닙니다. config.json의 model_type을 확인해 주세요.")


def _load_runtime_manifest(path: Path) -> tuple[DetectionSettings | None, str]:
    """Load a relocatable, local-only detection environment without Qt writes."""

    if not path.exists():
        return None, ""
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        if not isinstance(data, dict):
            raise ValueError("설정은 JSON 객체여야 합니다.")
        version = data.get("version", 1)
        if isinstance(version, bool) or not isinstance(version, int) or version != 1:
            raise ValueError("지원하지 않는 설정 버전입니다. version은 1이어야 합니다.")
        device = data.get("device", "auto")
        if not isinstance(device, str) or device not in {"auto", "cuda", "cpu"}:
            raise ValueError("device는 auto, cuda, cpu 중 하나여야 합니다.")
        offline = data.get("offline", False)
        if not isinstance(offline, bool):
            raise ValueError("offline은 true 또는 false여야 합니다.")
        backend = data.get("backend", "sam3")
        if backend not in {"sam3", "efficientsam3", "yoloe", "grounding_dino"}:
            raise ValueError("backend는 yoloe, sam3, efficientsam3 또는 grounding_dino이어야 합니다.")
        paths: dict[str, Path] = {}
        for key in ("python", "model"):
            value = data.get(key)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{key} 경로를 지정해 주세요.")
            resolved = Path(value.strip()).expanduser()
            if not resolved.is_absolute():
                resolved = path.parent / resolved
            paths[key] = resolved.resolve()
        if not paths["python"].is_file():
            raise ValueError(f"검출용 Python 파일이 없습니다: {paths['python']}")
        if backend == "yoloe":
            if not paths["model"].is_file() or paths["model"].suffix.lower() != ".pt":
                raise ValueError(f"YOLOE 로컬 .pt 모델 파일이 없습니다: {paths['model']}")
        elif not paths["model"].is_dir():
            raise ValueError(f"로컬 모델 폴더가 없습니다: {paths['model']}")
        if backend == "grounding_dino":
            _validate_dino_model(str(paths["model"]), offline=True)
        dino_options = _dino_options(data) if backend == "grounding_dino" else {}
        return DetectionSettings(
            python=str(paths["python"]),
            model=str(paths["model"]),
            device=device,
            offline=offline,
            backend=backend,
            **dino_options,
        ), ""
    except (OSError, ValueError, TypeError, RecursionError) as exc:
        return None, (
            f"객체 검출 실행 설정을 읽을 수 없습니다: {path}\n{exc}\n"
            "설정 파일의 경로·형식을 확인하거나 설정 → 객체 검출 환경에서 "
            "검출용 Python을 지정해 주세요."
        )


@dataclass(slots=True)
class DetectionRequest:
    video_path: Path
    start_frame: int  # zero-based, inclusive
    end_frame: int  # zero-based, inclusive
    frame_step: int
    classes: list[str]
    role: str
    case_uid: str = ""
    workspace: str = ""
    extra_args: list[str] = field(default_factory=list)

    @property
    def frame_count(self) -> int:
        return len(range(self.start_frame, self.end_frame + 1, max(1, self.frame_step)))


def friendly_error(message: str) -> str:
    text = message.strip()
    if "No module named 'ultralytics'" in text:
        return "YOLOE 실행 환경(ultralytics)을 찾지 못했습니다. 설정 → 객체 검출 환경에서 YOLOE용 Python을 지정해 주세요."
    if any(f"No module named '{module}'" in text for module in ("torch", "torchvision", "transformers")):
        return MISSING_RUNTIME_HINT
    if "Sam3" in text and ("cannot import" in text or "has no attribute" in text):
        return "설치된 transformers가 SAM 3를 지원하지 않습니다. transformers를 최신 버전으로 올려 주세요."
    if "gated" in text.casefold() or "401" in text:
        return (
            "검출 모델에 접근할 수 없습니다. 로컬 모델 경로 또는 Hugging Face 접근 권한·로그인 상태를 확인해 주세요."
        )
    if "out of memory" in text.casefold():
        return "GPU 메모리가 부족합니다. 검출 클래스를 줄이거나 CPU 장치를 선택해 주세요."
    return text or "객체 검출 중 알 수 없는 오류가 발생했습니다."


class DetectionJob(QObject):
    started = Signal(int)  # frames to process
    progress = Signal(int, int, int)  # processed, total, zero-based frame
    status = Signal(str)
    finished = Signal(dict)  # worker result document
    failed = Signal(str)
    cancelled = Signal()

    def __init__(
        self,
        request: DetectionRequest,
        settings: DetectionSettings,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self.request = request
        self.settings = settings
        self._temp_dir = Path(tempfile.mkdtemp(prefix="avat-detect-"))
        self._output = self._temp_dir / "result.json"
        self._cancel_file = self._temp_dir / "cancel"
        self._buffer = b""
        self._stderr = b""
        self._completed = False
        self._error: str | None = None
        self._cancel_requested = False
        self._done = False
        self._process = QProcess(self)
        self._process.readyReadStandardOutput.connect(self._read_stdout)
        self._process.readyReadStandardError.connect(self._read_stderr)
        self._process.finished.connect(self._process_finished)
        self._process.errorOccurred.connect(self._process_error)
        self._kill_timer = QTimer(self)
        self._kill_timer.setSingleShot(True)
        self._kill_timer.timeout.connect(self._process.kill)

    @property
    def is_running(self) -> bool:
        return self._process.state() != QProcess.ProcessState.NotRunning

    def arguments(self) -> list[str]:
        config_path = self._temp_dir / "classes.json"
        config_path.write_text(
            json.dumps(worker_class_config(self.request.classes), ensure_ascii=False),
            encoding="utf-8",
        )
        request = self.request
        backend_args = []
        if self.settings.backend == "grounding_dino":
            options = _dino_options({key: getattr(self.settings, key) for key in (
                "dino_chunk_size", "dino_box_threshold", "dino_text_threshold", "dino_fp16",
            )})
            backend_args = [
                "--dino-chunk-size", str(options["dino_chunk_size"]),
                "--dino-box-threshold", str(options["dino_box_threshold"]),
                "--dino-text-threshold", str(options["dino_text_threshold"]),
                "--half" if options["dino_fp16"] else "--no-half",
            ]
        return [
            "-m",
            "investigator.detection_worker",
            "--video",
            str(request.video_path),
            "--output",
            str(self._output),
            "--config",
            str(config_path),
            "--classes",
            ",".join(request.classes),
            "--start-frame",
            str(request.start_frame),
            "--end-frame",
            str(request.end_frame + 1),
            "--frame-step",
            str(max(1, request.frame_step)),
            "--model",
            self.settings.model or default_model(self.settings.backend),
            "--device",
            self.settings.device,
            "--backend",
            self.settings.backend,
            "--cancel-file",
            str(self._cancel_file),
            *backend_args,
            *request.extra_args,
        ]

    def start(self) -> None:
        if self.is_running or self._done:
            return
        try:
            if self.settings.config_error:
                raise ValueError(self.settings.config_error)
            if self.settings.backend not in {"sam3", "efficientsam3", "yoloe", "grounding_dino"}:
                raise ValueError("지원하지 않는 검출 백엔드입니다.")
            if self.settings.backend == "efficientsam3" and not self.settings.model.strip():
                raise ValueError("EfficientSAM3 로컬 모델 폴더를 지정해 주세요.")
            if self.settings.backend == "yoloe":
                model = Path(self.settings.model or default_model("yoloe"))
                if not model.is_file() or model.suffix.lower() != ".pt":
                    raise ValueError("YOLOE 로컬 .pt 모델 파일을 지정해 주세요.")
            if self.settings.backend == "grounding_dino":
                _validate_dino_model(self.settings.model or DINO_BASE_MODEL, offline=bool(self.settings.offline))
            if self.request.start_frame < 0 or self.request.end_frame < self.request.start_frame:
                raise ValueError("검출 시작·끝 프레임을 확인해 주세요.")
            if self.request.frame_step <= 0:
                raise ValueError("검출 프레임 간격은 1 이상이어야 합니다.")
            if not self.request.classes:
                raise ValueError("검출 클래스를 하나 이상 선택해 주세요.")
            arguments = self.arguments()
        except (OSError, ValueError, TypeError) as exc:
            self._finish_failed(str(exc))
            return
        environment = QProcessEnvironment.systemEnvironment()
        existing = environment.value("PYTHONPATH", "")
        separator = ";" if sys.platform.startswith("win") else ":"
        environment.insert(
            "PYTHONPATH", str(REPOSITORY_ROOT) + (separator + existing if existing else "")
        )
        environment.insert("PYTHONIOENCODING", "utf-8")
        environment.insert("PYTHONUTF8", "1")
        environment.insert("PYTHONUNBUFFERED", "1")
        environment.insert("YOLO_AUTOINSTALL", "false")
        environment.insert("YOLO_CONFIG_DIR", str(self._temp_dir / "ultralytics"))
        if self.settings.offline:
            environment.insert("HF_HUB_OFFLINE", "1")
            environment.insert("TRANSFORMERS_OFFLINE", "1")
            environment.insert("HF_HUB_DISABLE_TELEMETRY", "1")
        self._process.setProcessEnvironment(environment)
        self._process.setWorkingDirectory(str(REPOSITORY_ROOT))
        self._process.setProgram(self.settings.executable)
        self._process.setArguments(arguments)
        self.status.emit("검출 프로세스를 시작합니다…")
        self._process.start()

    def cancel(self) -> None:
        if not self.is_running or self._cancel_requested or self._done:
            return
        self._cancel_requested = True
        self.status.emit("현재 프레임을 마치고 취소합니다…")
        try:
            # Do not start a worker stdin reader thread before native ML DLLs
            # load: on Windows that can deadlock scipy/OpenBLAS initialization.
            # The worker already supports cooperative, thread-free polling.
            self._cancel_file.touch(exist_ok=True)
        except OSError:
            # Cancellation must remain bounded even if the temp directory was
            # removed or writing the marker is denied. Never raise from a slot.
            self.status.emit("취소 신호를 기록할 수 없어 검출 프로세스를 종료합니다…")
            self._process.terminate()
        self._kill_timer.start(CANCEL_GRACE_MS)

    def kill(self) -> None:
        """Stop immediately (application exit or project switch)."""

        self._cancel_requested = True
        self._kill_timer.stop()
        if self.is_running:
            self._process.kill()
            self._process.waitForFinished(3000)
        self._cleanup()

    def _read_stdout(self) -> None:
        self._buffer += bytes(self._process.readAllStandardOutput())
        *lines, self._buffer = self._buffer.split(b"\n")
        for raw in lines:
            self._handle_line(raw)
        # Third-party model libraries sometimes print progress without newlines.
        # The protocol itself uses short, flushed JSON records only.
        self._buffer = self._buffer[-MAX_EVENT_BUFFER:]

    def _read_stderr(self) -> None:
        # Keep only the tail: model loading can print long progress output.
        self._stderr = (self._stderr + bytes(self._process.readAllStandardError()))[-8000:]

    def _handle_line(self, raw: bytes) -> None:
        text = raw.decode("utf-8", errors="replace").strip()
        if not text:
            return
        try:
            event = json.loads(text)
        except json.JSONDecodeError:
            return
        if not isinstance(event, dict):
            return
        kind = event.get("type")
        if kind == "loading_model":
            self.status.emit(f"모델을 불러오는 중… ({event.get('model_id', '')})")
        elif kind == "started":
            try:
                total = max(0, int(event.get("total_frames", 0)))
            except (ValueError, TypeError, OverflowError):
                return
            device = str(event.get("device", "")).upper()
            note = event.get("device_note")
            self.status.emit(
                f"{device}에서 {total:,}개 프레임을 처리합니다." + (f" {note}" if note else "")
            )
            self.started.emit(total)
        elif kind == "progress":
            try:
                values = tuple(int(event.get(key, 0)) for key in ("processed", "total", "frame_index"))
            except (ValueError, TypeError, OverflowError):
                return
            if all(0 <= value <= 2_147_483_647 for value in values):
                self.progress.emit(*values)
        elif kind == "warning":
            self.status.emit(str(event.get("message", "")))
        elif kind == "finalizing":
            self.status.emit(str(event.get("message", "검출 결과를 정리합니다…")))
        elif kind == "completed":
            self._completed = True
        elif kind == "error":
            self._error = friendly_error(str(event.get("message", "")))

    def _process_error(self, error: QProcess.ProcessError) -> None:
        if error == QProcess.ProcessError.FailedToStart and not self._done:
            self._finish_failed(
                f"검출용 Python을 실행할 수 없습니다: {self.settings.executable}\n"
                "설정 → 객체 검출 환경에서 경로를 확인해 주세요."
            )

    def _process_finished(self, exit_code: int, _status: QProcess.ExitStatus) -> None:
        if self._done:
            return
        self._kill_timer.stop()
        self._read_stdout()
        self._read_stderr()
        if self._buffer:
            self._handle_line(self._buffer)
            self._buffer = b""
        # Cancellation wins even if the worker finished between the click and
        # delivery of its final event. Never merge a result the user cancelled.
        if self._cancel_requested or exit_code == 2:
            self._done = True
            self._cleanup()
            self.cancelled.emit()
            return
        if self._completed and not self._error and exit_code == 0 and self._output.is_file():
            try:
                document = json.loads(self._output.read_text(encoding="utf-8"))
                if not isinstance(document, dict) or not isinstance(document.get("detections"), list):
                    raise ValueError("검출 결과 문서 형식이 올바르지 않습니다.")
            except (OSError, ValueError) as exc:
                self._finish_failed(f"검출 결과를 읽지 못했습니다: {exc}")
                return
            self._done = True
            self._cleanup()
            self.finished.emit(document)
            return
        stderr = self._stderr.decode("utf-8", errors="replace")
        self._finish_failed(self._error or friendly_error(stderr[-600:]))

    def _finish_failed(self, message: str) -> None:
        if self._done:
            return
        self._kill_timer.stop()
        self._done = True
        self._cleanup()
        self.failed.emit(message)

    def _cleanup(self) -> None:
        shutil.rmtree(self._temp_dir, ignore_errors=True)


__all__ = ["DetectionJob", "DetectionRequest", "DetectionSettings", "default_model", "friendly_error"]
