from __future__ import annotations

import importlib
from types import SimpleNamespace

import pytest


@pytest.mark.parametrize("raise_from_loop", [False, True])
def test_application_loop_exit_releases_native_decoder(monkeypatch, raise_from_loop):
    # Loading the entry point must not create real per-user diagnostics here.
    monkeypatch.setattr("investigator.diagnostics.configure_diagnostics", lambda: None)
    entry = importlib.import_module("investigator.app")
    closed = []
    callbacks = []

    class Application:
        aboutToQuit = SimpleNamespace(connect=callbacks.append)

        def setApplicationName(self, value):
            self.application_name = value

        def setApplicationDisplayName(self, value):
            self.display_name = value

        def setOrganizationName(self, value):
            self.organization_name = value

        def setStyle(self, *_):
            pass

        def exec(self):
            if raise_from_loop:
                raise RuntimeError("event loop failed")
            for callback in callbacks:
                callback()
            assert closed == [True]
            return 7

    app = Application()
    window = SimpleNamespace(video_controller=SimpleNamespace(close_video=lambda: closed.append(True)), show=lambda: None)
    monkeypatch.setattr(entry, "QApplication", lambda *_: app)
    monkeypatch.setattr(entry, "apply_theme", lambda *_: None)
    monkeypatch.setattr(entry, "MainWindow", lambda *_: window)
    if raise_from_loop:
        with pytest.raises(RuntimeError, match="event loop failed"):
            entry.main([])
        assert closed == [True]
    else:
        assert entry.main([]) == 7
        assert closed == [True, True]
    assert app.application_name == app.display_name == app.organization_name == "AVAT"
