"""GUI 프로세스로 보내는 JSONL 이벤트와 취소 신호 처리."""

from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path
from typing import TextIO


class EventWriter:
    """한 줄에 JSON 객체 하나. 매 줄마다 flush 해서 QProcess가 바로 읽게 한다."""

    def __init__(self, stream: TextIO | None = None):
        self._stream = stream or sys.stdout
        self._lock = threading.Lock()

    def emit(self, event_type: str, **payload) -> None:
        record = {"type": event_type, "ts": round(time.time(), 3), **payload}
        line = json.dumps(record, ensure_ascii=False)
        with self._lock:
            self._stream.write(line + "\n")
            self._stream.flush()


class CancelToken:
    """취소 신호: 표준 입력의 `cancel` 줄 또는 취소 파일 존재."""

    def __init__(self, cancel_file: str | Path | None = None, watch_stdin: bool = False):
        self._event = threading.Event()
        self._cancel_file = Path(cancel_file) if cancel_file else None
        if watch_stdin:
            threading.Thread(target=self._watch_stdin, daemon=True).start()

    def _watch_stdin(self) -> None:
        try:
            for line in sys.stdin:
                if line.strip().lower() == "cancel":
                    self._event.set()
                    return
        except (OSError, ValueError):
            return

    def cancel(self) -> None:
        self._event.set()

    @property
    def cancelled(self) -> bool:
        if self._event.is_set():
            return True
        if self._cancel_file is not None and self._cancel_file.exists():
            self._event.set()
            return True
        return False
