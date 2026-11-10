"""프로그램 아이콘(EXE 파일·창 제목줄·작업표시줄)이 같은 avat.ico를 쓰는지 확인한다."""

from __future__ import annotations

import ast
from pathlib import Path

from PySide6.QtCore import QSize

from investigator import theme

ROOT = Path(__file__).resolve().parents[2]
ICON_SIZES = [16, 20, 24, 32, 40, 48, 64, 96, 128, 256]


def test_app_icon_file_has_every_windows_size(qapp):  # type: ignore[no-untyped-def]
    assert theme.APP_ICON_PATH == ROOT / "investigator" / "assets" / "app_icon" / "avat.ico"
    icon = theme.app_icon()
    assert not icon.isNull()
    assert sorted(size.width() for size in icon.availableSizes()) == ICON_SIZES
    assert icon.pixmap(QSize(256, 256)).size() == QSize(256, 256)


def test_missing_icon_file_falls_back_to_no_icon(qapp, tmp_path, monkeypatch):  # type: ignore[no-untyped-def]
    monkeypatch.setattr(theme, "APP_ICON_PATH", tmp_path / "missing.ico")
    assert theme.app_icon().isNull()


def test_spec_embeds_the_same_icon_in_exe():
    source = (ROOT / "packaging" / "AVAT.spec").read_text(encoding="utf-8")
    ast.parse(source)
    assert 'root / "investigator" / "assets" / "app_icon" / "avat.ico"' in source
    assert "icon=str(app_icon)" in source


def test_app_sets_icon_and_main_window_does_not_override_it():
    app_source = (ROOT / "investigator" / "app.py").read_text(encoding="utf-8")
    assert "app.setWindowIcon(app_icon())" in app_source
    # AppUserModelID는 QApplication을 만들기 전에, 소스 실행에서만 지정한다.
    assert app_source.index("_use_own_taskbar_group()\n    app = QApplication") > 0
    assert 'sys.platform != "win32" or is_frozen()' in app_source
    window_source = (ROOT / "investigator" / "main_window.py").read_text(encoding="utf-8")
    assert "setWindowIcon" not in window_source
    assert 'material_icon("car_crash"' not in window_source
