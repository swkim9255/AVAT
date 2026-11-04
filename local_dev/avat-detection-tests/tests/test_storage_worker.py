from __future__ import annotations

import threading

import pytest
from PySide6.QtCore import QTimer

from investigator.storage_worker import StorageWorker


@pytest.fixture
def worker(qtbot):
    worker = StorageWorker()
    yield worker
    worker.shutdown()


def test_operations_run_off_main_thread_in_one_serial_thread(worker):
    main_thread = threading.get_ident()
    order = []

    def record(number):
        order.append(number)
        return threading.get_ident()

    first = worker.submit(record, 1)
    second = worker.submit(record, 2)
    first_thread = worker.wait(first)
    second_thread = worker.wait(second)
    assert first_thread == second_thread
    assert first_thread != main_thread
    assert order == [1, 2]


def test_gui_timer_can_release_background_operation_while_waiting(worker):
    released = threading.Event()
    timer_fired = []

    def tick():
        timer_fired.append(True)
        released.set()

    timer = QTimer()
    timer.setSingleShot(True)
    timer.timeout.connect(tick)
    timer.start(20)
    try:
        assert worker.run(released.wait, 1.0)
    finally:
        timer.stop()
    assert timer_fired == [True]


def test_error_propagates_and_next_operation_can_retry(worker):
    def fail():
        raise OSError("synthetic disk failure")

    with pytest.raises(OSError, match="synthetic disk failure"):
        worker.run(fail)
    assert worker.run(lambda: "retry succeeded") == "retry succeeded"


def test_shutdown_is_idempotent_and_refuses_new_work(worker):
    worker.shutdown()
    worker.shutdown()
    with pytest.raises(RuntimeError):
        worker.submit(lambda: None)
