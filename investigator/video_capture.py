"""Conservative in-process metadata probes; playback uses an isolated decoder."""

from __future__ import annotations

from contextlib import suppress

import cv2


def open_video_capture(path: str) -> cv2.VideoCapture:
    """Open a file with FFmpeg and exactly one decoder thread.

    ``CAP_PROP_N_THREADS`` is an open-only FFmpeg option: calling ``set`` after
    opening the file is too late. Never retry with ``CAP_ANY`` or without the
    option, because that would silently reintroduce multithreaded decoding.
    Callers own the returned capture; unsuccessful candidates are released here.
    """
    thread_property = getattr(cv2, "CAP_PROP_N_THREADS", None)
    if thread_property is None:
        raise RuntimeError("OpenCV does not support single-threaded FFmpeg decoding.")

    capture = None
    try:
        capture = cv2.VideoCapture(path, cv2.CAP_FFMPEG, [thread_property, 1])
        if not capture.isOpened():
            raise RuntimeError("Unable to open the video with single-threaded FFmpeg decoding.")
    except Exception:
        if capture is not None:
            with suppress(cv2.error, OSError, RuntimeError):
                capture.release()
        raise
    return capture
