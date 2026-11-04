from __future__ import annotations

import pytest
from PySide6.QtGui import QFont

from investigator.qt_font_setup import configure_text_rendering
from investigator.theme import apply_theme


@pytest.mark.parametrize("platform", [None, "windows", "windows:darkmode=2"])
def test_windows_selects_directwrite_without_losing_platform_options(monkeypatch, platform):
    monkeypatch.setattr("investigator.qt_font_setup.sys.platform", "win32")
    if platform is None:
        monkeypatch.delenv("QT_QPA_PLATFORM", raising=False)
    else:
        monkeypatch.setenv("QT_QPA_PLATFORM", platform)
    configure_text_rendering()
    import os
    assert os.environ["QT_QPA_PLATFORM"] == f"{platform or 'windows'}:fontengine=directwrite"
    configure_text_rendering()
    assert os.environ["QT_QPA_PLATFORM"].count("fontengine=") == 1


@pytest.mark.parametrize("platform", [
    "offscreen", "minimal", "windows:fontengine=freetype",
    "windows:fontengine=directwrite", "windows:fontengine=gdi", "windows;offscreen",
])
def test_explicit_platform_or_engine_is_preserved(monkeypatch, platform):
    monkeypatch.setattr("investigator.qt_font_setup.sys.platform", "win32")
    monkeypatch.setenv("QT_QPA_PLATFORM", platform)
    configure_text_rendering()
    import os
    assert os.environ["QT_QPA_PLATFORM"] == platform


@pytest.mark.parametrize("arguments", [["app", "-platform", "offscreen"], ["app", "-platform=offscreen"]])
def test_explicit_qt_arguments_are_preserved(monkeypatch, arguments):
    monkeypatch.setattr("investigator.qt_font_setup.sys.platform", "win32")
    monkeypatch.delenv("QT_QPA_PLATFORM", raising=False)
    configure_text_rendering(arguments)
    import os
    assert "QT_QPA_PLATFORM" not in os.environ


def test_non_windows_platform_unchanged(monkeypatch):
    monkeypatch.setattr("investigator.qt_font_setup.sys.platform", "linux")
    monkeypatch.delenv("QT_QPA_PLATFORM", raising=False)
    configure_text_rendering()
    import os
    assert "QT_QPA_PLATFORM" not in os.environ


def test_ui_font_uses_antialiasing_and_vertical_hinting(qapp):
    apply_theme(qapp)
    assert qapp.font().hintingPreference() == QFont.HintingPreference.PreferVerticalHinting
    assert qapp.font().styleStrategy() & QFont.StyleStrategy.PreferAntialias
