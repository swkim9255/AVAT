from __future__ import annotations

import sys
import threading
from types import SimpleNamespace

import pytest

from investigator import diagnostics


@pytest.fixture(autouse=True)
def clean_diagnostics(monkeypatch):
    diagnostics.shutdown_diagnostics()
    monkeypatch.setattr(diagnostics.faulthandler, "enable", lambda **kwargs: None)
    monkeypatch.setattr(diagnostics.faulthandler, "disable", lambda: None)
    yield
    diagnostics.shutdown_diagnostics()


def test_timing_and_memory_are_logged_without_exception_payload(tmp_path, monkeypatch):
    monkeypatch.setattr(diagnostics, "process_memory_bytes", lambda: 1000)
    assert diagnostics.configure_diagnostics(tmp_path) == tmp_path
    with diagnostics.timed_operation("save_changes", count=1):
        pass
    with (
        pytest.raises(ValueError, match="private case value"),
        diagnostics.timed_operation("load_workspace", count=100),
    ):
        raise ValueError("private case value")
    log = (tmp_path / "avat.log").read_text(encoding="utf-8")
    assert "operation=save_changes count=1" in log
    assert "memory_bytes=1000 outcome=ok" in log
    assert "operation=load_workspace count=100" in log
    assert "outcome=failed" in log
    assert "private case value" not in log


def test_reconfiguration_does_not_duplicate_hooks_or_handlers(tmp_path):
    original = sys.excepthook
    assert diagnostics.configure_diagnostics(tmp_path) == tmp_path
    hook = sys.excepthook
    assert diagnostics.configure_diagnostics(tmp_path) == tmp_path
    assert sys.excepthook is hook
    assert diagnostics.LOGGER.handlers.count(diagnostics._handler) == 1
    diagnostics.shutdown_diagnostics()
    assert sys.excepthook is original
    assert diagnostics._fault_file is None


def test_exception_hooks_preserve_original_behavior(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(sys, "excepthook", lambda *args: calls.append(args))
    monkeypatch.setattr(threading, "excepthook", lambda args: calls.append(args))
    diagnostics.configure_diagnostics(tmp_path)
    error = ValueError("private investigation information")
    sys.excepthook(ValueError, error, None)
    args = SimpleNamespace(exc_type=ValueError, exc_value=error, exc_traceback=None)
    threading.excepthook(args)
    assert calls == [(ValueError, error, None), args]
    log = (tmp_path / "avat.log").read_text(encoding="utf-8")
    assert log.count("unhandled_exception type=ValueError") == 2
    assert "private investigation information" not in log


def test_unwritable_log_destination_does_not_prevent_startup(tmp_path):
    not_directory = tmp_path / "file"
    not_directory.write_text("existing data", encoding="utf-8")
    assert diagnostics.configure_diagnostics(not_directory / "logs") is None
    assert not_directory.read_text(encoding="utf-8") == "existing data"
    assert diagnostics._handler is None


def test_fault_log_rotates_only_diagnostic_files(tmp_path, monkeypatch):
    monkeypatch.setattr(diagnostics, "MAX_LOG_BYTES", 10)
    crash_log = tmp_path / "avat-crash.log"
    crash_log.write_bytes(b"old fatal diagnostics")
    unrelated = tmp_path / "investigations.json"
    unrelated.write_text("unchanged", encoding="utf-8")
    diagnostics.configure_diagnostics(tmp_path)
    assert (tmp_path / "avat-crash.log.1").read_bytes() == b"old fatal diagnostics"
    assert crash_log.read_bytes() == b""
    assert unrelated.read_text(encoding="utf-8") == "unchanged"


def test_default_directory_uses_local_app_data(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    assert diagnostics.default_log_directory() == tmp_path / "AVAT" / "logs"


def test_memory_probe_returns_nonnegative_integer_or_unavailable():
    result = diagnostics.process_memory_bytes()
    assert result is None or (isinstance(result, int) and result > 0)
