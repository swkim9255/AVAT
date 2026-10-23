"""Case-number sorting is explicit and leaves navigation to the controller."""

from __future__ import annotations

import pytest
from PySide6.QtCore import Qt

from investigator.models import InvestigationCase
from investigator.widgets.case_list import UID_ROLE, CaseListPanel


def _case(number: str) -> InvestigationCase:
    return InvestigationCase(
        values={"Institution": "samsung", "accident_classification": number},
        videos=[f"C:/videos/{number}_2.mp4", f"C:/videos/{number}_1.mp4"],
    )


@pytest.fixture
def panel(qtbot):
    widget = CaseListPanel()
    qtbot.addWidget(widget)
    return widget


def test_sort_button_is_named_icon_and_disabled_without_cases(panel):
    assert panel.sort_button.accessibleName() == "사고번호 오름차순 정렬"
    assert panel.sort_button.toolTip() == "사고번호 오름차순 정렬"
    assert not panel.sort_button.icon().isNull()
    assert panel.sort_button.text() == ""
    assert not panel.sort_button.isEnabled()


def test_sort_request_preserves_selection_expansion_and_search(panel, qtbot):
    later, earlier = _case("2023011302572"), _case("2019011302572")
    panel.set_cases([later, earlier], selected_uid=later.uid, selected_video_index=1)
    panel.tree.topLevelItem(0).setExpanded(False)
    panel.tree.topLevelItem(1).setSelected(True)
    panel.search.setText("0113")
    before = panel.capture_view_state()
    with qtbot.waitSignal(panel.sort_ascending_requested, timeout=1000):
        qtbot.mouseClick(panel.sort_button, Qt.MouseButton.LeftButton)
    assert panel.capture_view_state() == before
    assert [panel.tree.topLevelItem(i).data(0, UID_ROLE) for i in range(2)] == [
        later.uid,
        earlier.uid,
    ]


def test_sort_stays_available_for_one_case_and_no_search_matches(panel):
    case = _case("2019011302572")
    panel.set_cases([case])
    assert panel.sort_button.isEnabled()
    panel.search.setText("no matches")
    assert panel.tree.topLevelItem(0).isHidden()
    assert panel.sort_button.isEnabled()


@pytest.mark.parametrize("clear_method", ["set_cases", "merge_cases"])
def test_sort_disables_when_last_case_is_removed(panel, clear_method):
    panel.set_cases([_case("2019011302572")])
    getattr(panel, clear_method)([], None)
    assert not panel.sort_button.isEnabled()


def test_sort_control_fits_minimum_sidebar_width(panel, qtbot):
    panel.resize(240, 600)
    panel.show()
    qtbot.wait(10)
    assert panel.sort_button.width() == panel.sort_button.height() == 36
    assert panel.search.geometry().right() < panel.sort_button.geometry().left()
    assert panel.sort_button.geometry().right() < panel.width()
