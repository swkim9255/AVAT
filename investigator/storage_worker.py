"""Serialized disk work off the GUI thread, with responsive commit boundaries."""

from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any

from PySide6.QtCore import QEventLoop, QObject, QTimer


class StorageWorker(QObject):
    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="avat-storage")
        self._closed = False

    def submit(self, operation: Callable, *args: Any, **kwargs: Any) -> Future:
        return self._executor.submit(operation, *args, **kwargs)

    def wait(self, future: Future, *, allow_input: bool = False) -> Any:
        """Keep paint/playback timers alive without accepting reentrant user actions."""
        if not future.done():
            loop = QEventLoop()
            timer = QTimer()
            timer.setInterval(5)
            timer.timeout.connect(lambda: loop.quit() if future.done() else None)
            timer.start()
            loop.exec(
                QEventLoop.ProcessEventsFlag.AllEvents if allow_input
                else QEventLoop.ProcessEventsFlag.ExcludeUserInputEvents
            )
            timer.stop()
        return future.result()

    def run(self, operation: Callable, *args: Any, allow_input: bool = False, **kwargs: Any) -> Any:
        return self.wait(self.submit(operation, *args, **kwargs), allow_input=allow_input)

    def shutdown(self) -> None:
        if not self._closed:
            self._executor.shutdown(wait=True)
            self._closed = True
