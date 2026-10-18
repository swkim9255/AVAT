"""Bounded, local-only runtime diagnostics without investigation payloads."""

from __future__ import annotations

import atexit
import ctypes
import faulthandler
import logging
import os
import sys
import threading
import time
import traceback
from collections.abc import Iterator
from contextlib import contextmanager
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import BinaryIO

LOGGER = logging.getLogger("investigator")
MAX_LOG_BYTES = 2 * 1024 * 1024
LOG_BACKUPS = 3
_handler: RotatingFileHandler | None = None
_fault_file: BinaryIO | None = None
_log_directory: Path | None = None
_previous_excepthook = None
_previous_thread_excepthook = None
_hooks_installed = False


def default_log_directory() -> Path:
    """Keep logs outside a selected workspace, including read-only workspaces."""
    base = os.environ.get("LOCALAPPDATA")
    if base:
        return Path(base) / "AVAT" / "logs"
    return Path.home() / ".avat" / "logs"


def _record_exception(exception_type: type[BaseException], tb: object) -> None:
    # Exception messages can contain investigation values or video paths. Store
    # the exception class and source stack only; the original hook stays intact.
    LOGGER.error(
        "unhandled_exception type=%s\n%s",
        exception_type.__name__,
        "".join(traceback.format_tb(tb)),
    )


def _exception_hook(exception_type, exception_value, tb) -> None:
    try:
        _record_exception(exception_type, tb)
    finally:
        if _previous_excepthook is not None:
            _previous_excepthook(exception_type, exception_value, tb)


def _thread_exception_hook(args) -> None:
    try:
        _record_exception(args.exc_type, args.exc_traceback)
    finally:
        if _previous_thread_excepthook is not None:
            _previous_thread_excepthook(args)


def _rotate_fault_log(path: Path) -> None:
    if not path.exists() or path.stat().st_size < MAX_LOG_BYTES:
        return
    for index in range(LOG_BACKUPS, 0, -1):
        source = path if index == 1 else path.with_name(f"{path.name}.{index - 1}")
        target = path.with_name(f"{path.name}.{index}")
        if source.exists():
            source.replace(target)


def configure_diagnostics(log_dir: str | Path | None = None) -> Path | None:
    """Install rotating logs and exception hooks; logging failure never blocks startup.

    Success returns the log directory. Repeated calls for the same directory do
    not duplicate handlers. Logs contain operation names/counts/timing/memory,
    never case identifiers, form values, or video filenames.
    """
    global _handler, _fault_file, _log_directory
    global _previous_excepthook, _previous_thread_excepthook, _hooks_installed

    directory = Path(log_dir) if log_dir is not None else default_log_directory()
    if _handler is not None and directory == _log_directory:
        return directory
    shutdown_diagnostics()
    try:
        directory.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(
            directory / "avat.log",
            maxBytes=MAX_LOG_BYTES,
            backupCount=LOG_BACKUPS,
            encoding="utf-8",
        )
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        LOGGER.setLevel(logging.INFO)
        LOGGER.addHandler(handler)
        _handler = handler
        _log_directory = directory
        crash_path = directory / "avat-crash.log"
        _rotate_fault_log(crash_path)
        _fault_file = crash_path.open("ab", buffering=0)
        faulthandler.enable(file=_fault_file, all_threads=True)
    except (OSError, RuntimeError, ValueError):
        shutdown_diagnostics()
        return None

    _previous_excepthook = sys.excepthook
    _previous_thread_excepthook = threading.excepthook
    sys.excepthook = _exception_hook
    threading.excepthook = _thread_exception_hook
    _hooks_installed = True
    LOGGER.info("diagnostics_started python=%s", sys.version.split()[0])
    return directory


def shutdown_diagnostics() -> None:
    """Flush and close owned handles, without replacing hooks installed by others."""
    global _handler, _fault_file, _log_directory, _hooks_installed
    if _hooks_installed:
        if sys.excepthook is _exception_hook:
            sys.excepthook = _previous_excepthook
        if threading.excepthook is _thread_exception_hook:
            threading.excepthook = _previous_thread_excepthook
        _hooks_installed = False
    if _fault_file is not None:
        faulthandler.disable()
        _fault_file.close()
        _fault_file = None
    if _handler is not None:
        LOGGER.removeHandler(_handler)
        _handler.close()
        _handler = None
    _log_directory = None


def process_memory_bytes() -> int | None:
    """Return Windows process working-set bytes when available, without dependencies."""
    if sys.platform != "win32":
        return None
    from ctypes import wintypes

    class ProcessMemoryCounters(ctypes.Structure):
        _fields_ = [
            ("cb", wintypes.DWORD),
            ("PageFaultCount", wintypes.DWORD),
            ("PeakWorkingSetSize", ctypes.c_size_t),
            ("WorkingSetSize", ctypes.c_size_t),
            ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
            ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
            ("PagefileUsage", ctypes.c_size_t),
            ("PeakPagefileUsage", ctypes.c_size_t),
        ]

    try:
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        psapi = ctypes.WinDLL("psapi", use_last_error=True)
        kernel.GetCurrentProcess.restype = wintypes.HANDLE
        get_memory = psapi.GetProcessMemoryInfo
        get_memory.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(ProcessMemoryCounters),
            wintypes.DWORD,
        ]
        get_memory.restype = wintypes.BOOL
        counters = ProcessMemoryCounters()
        counters.cb = ctypes.sizeof(counters)
        if get_memory(kernel.GetCurrentProcess(), ctypes.byref(counters), counters.cb):
            return int(counters.WorkingSetSize)
    except (AttributeError, OSError):
        pass
    return None


@contextmanager
def timed_operation(operation: str, *, count: int | None = None) -> Iterator[None]:
    """Measure a fixed, developer-supplied label; never pass a case value as the label."""
    start = time.perf_counter()
    outcome = "ok"
    try:
        yield
    except BaseException:
        outcome = "failed"
        raise
    finally:
        LOGGER.info(
            "operation=%s count=%s duration_ms=%.3f memory_bytes=%s outcome=%s",
            operation,
            count if count is not None else "-",
            (time.perf_counter() - start) * 1000,
            process_memory_bytes() or "-",
            outcome,
        )


atexit.register(shutdown_diagnostics)
