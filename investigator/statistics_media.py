"""Read actual video FPS metadata for statistics without decoding frames."""

from __future__ import annotations

import math
from contextlib import suppress
from pathlib import Path
from threading import Event

import cv2

from investigator.video_capture import open_video_capture


def probe_video_fps(
    paths: list[str],
    cache: dict[str, tuple[int, int, float]],
    cancel: Event | None = None,
) -> dict[str, tuple[int, int, float]]:
    """Return an updated cache keyed by resolved path, preserving the input cache.

    Entries contain file size, nanosecond modification time and actual FPS.
    Unavailable metadata is represented by zero, never an assumed playback FPS.
    Failed probes are cached until the file changes or the caller clears them.
    """
    updated = dict(cache)
    for raw_path in paths:
        if cancel is not None and cancel.is_set():
            break
        try:
            path = Path(raw_path).resolve()
        except (OSError, RuntimeError):
            continue
        key = str(path)
        try:
            metadata = path.stat()
            signature = (metadata.st_size, metadata.st_mtime_ns)
        except OSError:
            updated[key] = (0, 0, 0.0)
            continue
        previous = updated.get(key)
        if previous is not None and previous[:2] == signature:
            continue
        if cancel is not None and cancel.is_set():
            break
        fps = 0.0
        capture = None
        try:
            capture = open_video_capture(key)
            if capture.isOpened():
                value = float(capture.get(cv2.CAP_PROP_FPS))
                if math.isfinite(value) and value > 0:
                    fps = value
        except (cv2.error, OSError, RuntimeError, TypeError, ValueError, OverflowError):
            fps = 0.0
        finally:
            if capture is not None:
                with suppress(cv2.error, OSError, RuntimeError):
                    capture.release()
        updated[key] = (*signature, fps)
    return updated
