"""Keep Qt FFmpeg errors visible without installing a Python native callback.

Qt can synchronously wait for native playback threads while holding the GIL.
A Python FFmpeg callback can therefore deadlock even when it only forwards logs.
Use the native log-level setting instead; this suppresses FFmpeg warnings and
informational messages, not Qt logging categories or native decoder errors.
"""

from __future__ import annotations

import atexit
import ctypes
import sys
from pathlib import Path
from threading import RLock

_AV_LOG_ERROR = 16
_lock = RLock()
_configured = False
_library = None
_original_level: int | None = None


def _get_loaded_qt_avutil():
    if sys.platform != "win32":
        return None

    import PySide6

    package_path = Path(PySide6.__file__).resolve().parent
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    get_handle = kernel32.GetModuleHandleW
    get_handle.argtypes = [ctypes.c_wchar_p]
    get_handle.restype = ctypes.c_void_p
    get_path = kernel32.GetModuleFileNameW
    get_path.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_uint]
    get_path.restype = ctypes.c_uint
    for candidate in sorted(package_path.glob("avutil-*.dll")):
        candidate = candidate.resolve()
        handle = get_handle(str(candidate))
        if not handle:
            continue
        loaded_path = ctypes.create_unicode_buffer(32768)
        length = get_path(handle, loaded_path, len(loaded_path))
        if not length or length >= len(loaded_path):
            continue
        if Path(loaded_path.value).resolve() != candidate:
            continue
        # Existing handle only: do not LoadLibrary or search PATH for FFmpeg.
        return ctypes.CDLL(str(candidate), handle=handle)
    return None


def configure_qt_ffmpeg_log_level() -> bool:
    """Apply native ERROR level after QMediaPlayer initializes its backend.

    Existing stricter settings are preserved. Unsupported/unavailable libraries
    fail open without changing logging. No callback is installed or replaced.
    """
    global _configured, _library, _original_level
    with _lock:
        if _configured:
            return True
        try:
            library = _get_loaded_qt_avutil()
            if library is None:
                return False
            library.av_log_get_level.argtypes = []
            library.av_log_get_level.restype = ctypes.c_int
            library.av_log_set_level.argtypes = [ctypes.c_int]
            library.av_log_set_level.restype = None
            original_level = library.av_log_get_level()
            library.av_log_set_level(min(original_level, _AV_LOG_ERROR))
        except (AttributeError, ImportError, OSError, TypeError, ValueError):
            return False
        _library = library
        _original_level = original_level
        _configured = True
        return True


def restore_qt_ffmpeg_log_level() -> None:
    """Restore the previous native level; safe to call repeatedly at shutdown."""
    global _configured
    with _lock:
        if not _configured:
            return
        try:
            _library.av_log_set_level(_original_level)
        except (AttributeError, OSError, TypeError, ValueError):
            return
        _configured = False


atexit.register(restore_qt_ffmpeg_log_level)
