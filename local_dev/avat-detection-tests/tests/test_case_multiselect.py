from PySide6.QtCore import QMimeData, QPointF, Qt
from PySide6.QtGui import QDropEvent
from PySide6.QtTest import QTest

from investigator.main_window import MainWindow
from investigator.models import InvestigationCase
from investigator.widgets.case_list import UID_ROLE, CaseListPanel


def test_ctrl_shift_selection(qtbot):
    panel = CaseListPanel()
    qtbot.addWidget(panel)
    panel.resize(380, 700)
    cases = [InvestigationCase() for _ in range(5)]
    panel.set_cases(cases)
    panel.show()
    tree = panel.tree
    activated = []
    panel.case_selected.connect(activated.append)

    def click(index, modifier=Qt.KeyboardModifier.NoModifier):
        QTest.mouseClick(tree.viewport(), Qt.MouseButton.LeftButton, modifier,
                        tree.visualItemRect(tree.topLevelItem(index)).center())

    click(0)
    click(2, Qt.KeyboardModifier.ControlModifier)
    assert tree.selected_cases() == [tree.topLevelItem(0), tree.topLevelItem(2)]
    click(2, Qt.KeyboardModifier.ControlModifier)
    assert tree.selected_cases() == [tree.topLevelItem(0)]
    click(1)
    click(4, Qt.KeyboardModifier.ShiftModifier)
    assert tree.selected_cases() == [tree.topLevelItem(i) for i in range(1, 5)]
    assert activated[-1] == cases[1].uid
    click(4)
    assert activated[-1] == cases[4].uid
    assert tree.selected_cases() == [tree.topLevelItem(4)]


def test_expansion_survives_refresh(qtbot):
    panel = CaseListPanel()
    qtbot.addWidget(panel)
    cases = [InvestigationCase(videos=[f"video{i}.mp4"]) for i in range(3)]
    panel.set_cases(cases)
    tree = panel.tree
    tree.collapseAll()
    tree.topLevelItem(1).setExpanded(True)
    panel.set_cases(cases, cases[1].uid)
    assert [tree.topLevelItem(i).isExpanded() for i in range(3)] == [False, True, False]


def test_group_drop_preserves_items_selection_and_saved_order(qtbot, tmp_path):
    window = MainWindow(tmp_path)
    qtbot.addWidget(window)
    window.show()
    for _ in range(5):
        window.create_case()
    tree = window.case_list.tree
    original = list(window.cases)
    items = [tree.topLevelItem(i) for i in range(5)]
    tree.setCurrentItem(items[1])
    items[3].setSelected(True)
    point = tree.visualItemRect(items[4]).bottomRight()
    event = QDropEvent(QPointF(point), Qt.DropAction.MoveAction, QMimeData(),
                       Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
    tree.dropEvent(event)
    assert event.isAccepted()
    expected = [original[i].uid for i in (0, 2, 4, 1, 3)]
    qtbot.waitUntil(lambda: [case.uid for case in window.cases] == expected)
    assert [tree.topLevelItem(i) for i in range(5)] == [items[i] for i in (0, 2, 4, 1, 3)]
    assert tree.selected_cases() == [items[1], items[3]]
    assert tree.currentItem() is items[1]
    assert [tree.topLevelItem(i).data(0, UID_ROLE) for i in range(5)] == expected
    window.save_draft()
    reopened = MainWindow(tmp_path)
    qtbot.addWidget(reopened)
    assert [case.uid for case in reopened.cases] == expected
