import pytest
from PySide6.QtCore import Qt
from PySide6.QtGui import QFontMetrics
from PySide6.QtWidgets import QLabel, QTabBar, QWidget

from investigator.main_window import MainWindow
from investigator.theme import apply_theme


def assert_single_row(window):
    bar = window.menuBar()
    controls = bar.cornerWidget(Qt.Corner.TopRightCorner)
    assert controls is not None
    assert controls.geometry().right() < bar.width()
    last_menu = bar.actionGeometry(bar.actions()[-1])
    tabs = window.project_tabs
    tab_left = tabs.mapTo(bar, tabs.rect().topLeft())
    assert tab_left.x() > last_menu.right()
    add = window.project_add_button
    add_right = add.mapTo(bar, add.rect().topRight())
    mode = window.view_mode
    mode_left = mode.mapTo(bar, mode.rect().topLeft())
    mode_right = mode.mapTo(bar, mode.rect().bottomRight())
    assert add_right.x() < mode_left.x()
    assert mode_right.x() < bar.width()
    assert mode_right.y() < bar.height()
    assert tab_left.y() >= 0
    assert window.centralWidget().y() == bar.height()


@pytest.mark.parametrize("width", [1180, 1660])
def test_single_row_menus_document_tabs_and_mode_switch(qtbot, qapp, tmp_path, width):
    apply_theme(qapp)
    window = MainWindow(tmp_path)
    qtbot.addWidget(window)
    window.resize(width, 760)
    window.show()
    qtbot.wait(10)
    assert window.findChild(QWidget, "appHeader") is None
    assert window.findChild(QWidget, "appBrand") is None
    bar = window.menuBar()
    assert [action.text() for action in bar.actions()] == ["파일", "설정", "도움말"]
    assert window.findChild(QLabel, "workspacePath") is None
    assert window.case_list.findChild(QLabel, "projectFolderName") is None
    assert not any(label.text() == "프로젝트" for label in window.case_list.findChildren(QLabel))
    assert window.centralWidget().layout().count() == 1
    assert window.centralWidget().layout().itemAt(0).widget() is window.workspace_pages
    assert not isinstance(window.view_mode, QTabBar)
    assert window.project_tabs.tabsClosable()
    assert window.project_tabs.elideMode() == Qt.TextElideMode.ElideMiddle
    assert window.project_tabs.tabToolTip(0) == str(tmp_path.resolve())
    assert str(tmp_path.resolve()) not in window.statusBar().currentMessage()
    assert_single_row(window)


def test_folder_names_only_in_tabs_and_full_paths_in_tooltips(qtbot, qapp, tmp_path):
    apply_theme(qapp)
    first = tmp_path / "사고영상조사"
    second = tmp_path / ("long_project_" * 8)
    first.mkdir()
    second.mkdir()
    window = MainWindow(first)
    qtbot.addWidget(window)
    window.resize(1180, 760)
    window.show()
    assert window.open_project(second)
    qtbot.wait(10)
    tabs = window.project_tabs
    assert tabs.tabText(0) == first.name
    assert tabs.tabRect(0).width() >= 190
    assert tabs.tabToolTip(1) == str(second.resolve())
    assert tabs.elideMode() == Qt.TextElideMode.ElideMiddle
    assert_single_row(window)
    assert window.switch_project(0)
    assert tabs.tabToolTip(tabs.currentIndex()) == str(first.resolve())


def test_segment_buttons_change_views_exclusively(qtbot, tmp_path):
    window = MainWindow(tmp_path)
    qtbot.addWidget(window)
    window.show()
    investigate, statistics = window.view_mode.buttons
    assert investigate.isChecked() and not statistics.isChecked()
    qtbot.mouseClick(statistics, Qt.MouseButton.LeftButton)
    assert window.workspace_pages.currentWidget() is window.statistics_panel
    assert statistics.isChecked() and not investigate.isChecked()
    qtbot.mouseClick(statistics, Qt.MouseButton.LeftButton)
    assert statistics.isChecked()
    qtbot.mouseClick(investigate, Qt.MouseButton.LeftButton)
    assert window.workspace_pages.currentWidget() is window.splitter
    assert investigate.isChecked() and not statistics.isChecked()


def test_close_buttons_track_shifted_indices_and_keep_last_project(qtbot, tmp_path):
    first, second, third = [tmp_path / name for name in ("first", "second", "third")]
    for folder in (first, second, third):
        folder.mkdir()
    window = MainWindow(first)
    qtbot.addWidget(window)
    window.show()
    assert not window.project_tabs.tabButton(0, QTabBar.ButtonPosition.RightSide).isEnabled()
    assert window.open_project(second)
    assert window.open_project(third)
    tabs = window.project_tabs
    qtbot.mouseClick(tabs.tabButton(0, QTabBar.ButtonPosition.RightSide), Qt.MouseButton.LeftButton)
    assert tabs.count() == 2 and window.repository.workspace == third
    qtbot.mouseClick(tabs.tabButton(0, QTabBar.ButtonPosition.RightSide), Qt.MouseButton.LeftButton)
    assert tabs.count() == 1 and window.repository.workspace == third
    assert not tabs.tabButton(0, QTabBar.ButtonPosition.RightSide).isEnabled()


def test_many_document_tabs_scroll_without_covering_menus_or_mode(qtbot, qapp, tmp_path):
    apply_theme(qapp)
    window = MainWindow(tmp_path / "first")
    qtbot.addWidget(window)
    window.show()
    for index in range(7):
        folder = tmp_path / f"project-{index}-long-label"
        folder.mkdir()
        assert window.open_project(folder)
    for width in (1660, 1180, 1400):
        window.resize(width, 760)
        qtbot.wait(10)
        assert window.width() == width
        assert_single_row(window)
    assert window.project_tabs.usesScrollButtons()
    window.project_tabs.setCurrentIndex(0)
    assert window.repository.workspace == (tmp_path / "first").resolve()
    assert_single_row(window)


@pytest.mark.parametrize("width", [1180, 1660])
def test_document_tab_contents_align_and_close_button_has_inner_padding(qtbot, qapp, tmp_path, width):
    apply_theme(qapp)
    first, second = tmp_path / "사고영상조사", tmp_path / "review"
    first.mkdir()
    second.mkdir()
    window = MainWindow(first)
    qtbot.addWidget(window)
    window.resize(width, 760)
    window.show()
    assert window.open_project(second)
    for selected in (0, 1):
        window.project_tabs.setCurrentIndex(selected)
        qtbot.wait(10)
        tabs = window.project_tabs
        for index in range(2):
            tab = tabs.tabRect(index)
            icon, text, close = tabs._item_rects(index)
            button = tabs.tabButton(index, QTabBar.ButtonPosition.RightSide)
            assert tab.height() <= tabs.height()
            assert button.size().toTuple() == (20, 20)
            assert button.geometry() == close
            assert icon.center().y() == text.center().y() == close.center().y()
            assert text.left() > icon.right()
            assert text.right() < close.left()
            assert tab.right() - 6 - close.right() >= 12
            if index == 0:
                font = tabs.font()
                font.setBold(index == selected)
                assert QFontMetrics(font).horizontalAdvance(tabs.tabText(index)) <= text.width()
