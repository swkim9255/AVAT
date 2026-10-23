from PySide6.QtCore import QItemSelectionModel

from investigator.models import InvestigationCase
from investigator.theme import PRIMARY
from investigator.widgets.case_list import VIDEO_INDEX_ROLE, CaseListPanel


def test_field_update_only_recomputes_changed_case_and_preserves_rows(qtbot, monkeypatch):
    panel = CaseListPanel()
    qtbot.addWidget(panel)
    cases = [InvestigationCase(videos=[f"video-{i}.mp4"]) for i in range(80)]
    reads = []
    original_missing = InvestigationCase.missing.fget

    def missing(case):
        reads.append(case.uid)
        return original_missing(case)

    monkeypatch.setattr(InvestigationCase, "missing", property(missing))
    panel.set_cases(cases, cases[0].uid)
    assert reads == [case.uid for case in cases]
    items = [panel.tree.topLevelItem(i) for i in range(len(cases))]
    children = [item.child(0) for item in items]
    items[0].setExpanded(False)
    items[10].setSelected(True)
    reads.clear()

    def no_rebuild(*args, **kwargs):
        raise AssertionError("An individual edit must not rebuild the tree")

    monkeypatch.setattr(panel, "set_cases", no_rebuild)
    monkeypatch.setattr(panel.tree, "clear", no_rebuild)
    cases[0].values["driver_sex"] = "남성"
    assert panel.update_case(cases[0])
    assert reads == [cases[0].uid]
    assert [panel.tree.topLevelItem(i) for i in range(len(cases))] == items
    assert [item.child(0) for item in items] == children
    assert not items[0].isExpanded()
    assert panel.tree.currentItem() is items[0]
    assert panel.tree.selected_cases() == [items[0], items[10]]
    assert items[0].text(1) == str(len(original_missing(cases[0])))
    assert not panel.update_case(InvestigationCase())


def test_completed_counter_updates_without_rechecking_other_cases(qtbot, monkeypatch):
    panel = CaseListPanel()
    qtbot.addWidget(panel)
    cases = [InvestigationCase() for _ in range(3)]
    panel.set_cases(cases, cases[0].uid)
    cases[0].status = "completed"
    panel.update_case(cases[0])
    assert panel.count_label.text() == "1/3 완료"
    assert panel.tree.topLevelItem(0).text(1) == "✓"
    panel.update_case(cases[0])
    assert panel.count_label.text() == "1/3 완료"
    cases[0].status = "draft"
    panel.update_case(cases[0])
    assert panel.count_label.text() == "0/3 완료"


def test_active_video_switch_only_recolors_old_and_new_children(qtbot, monkeypatch):
    panel = CaseListPanel()
    qtbot.addWidget(panel)
    cases = [InvestigationCase(videos=[f"{i}-a.mp4", f"{i}-b.mp4"]) for i in range(4)]
    panel.set_cases(cases, cases[0].uid)
    items = [panel.tree.topLevelItem(i) for i in range(4)]
    children = [item.child(j) for item in items for j in range(2)]
    changes = []
    panel.tree.model().dataChanged.connect(
        lambda top_left, bottom_right, roles: changes.append(top_left)
    )

    def no_missing(case):
        raise AssertionError("Changing the playing video must not validate any case")

    monkeypatch.setattr(InvestigationCase, "missing", property(no_missing))
    panel.set_active_video(cases[3].uid)
    assert len(changes) == 2
    assert children[0].foreground(0).color().name() != PRIMARY.casefold()
    assert children[6].foreground(0).color().name() == PRIMARY.casefold()
    assert panel.tree.currentItem() is items[0]
    assert [item.child(j) for item in items for j in range(2)] == children
    changes.clear()
    cases[3].active_video_index = 1
    panel.set_active_video(cases[3].uid)
    assert len(changes) == 2
    assert children[6].foreground(0).color().name() != PRIMARY.casefold()
    assert children[7].foreground(0).color().name() == PRIMARY.casefold()
    panel.set_active_video(None)
    assert all(child.foreground(0).color().name() != PRIMARY.casefold() for child in children)


def test_attachment_changes_reuse_surviving_children_and_update_filter(qtbot):
    panel = CaseListPanel()
    qtbot.addWidget(panel)
    first = InvestigationCase(videos=["a.mp4", "b.mp4"], active_video_index=1)
    second = InvestigationCase(videos=["other.mp4"])
    panel.set_cases([first, second], first.uid, 1)
    item = panel.tree.topLevelItem(0)
    other = panel.tree.topLevelItem(1)
    a, b = item.child(0), item.child(1)
    other.setSelected(True)
    item.setExpanded(False)
    first.videos = ["b.mp4", "new.mp4"]
    first.active_video_index = 0
    panel.update_case(first)
    assert panel.tree.topLevelItem(0) is item
    assert panel.tree.topLevelItem(1) is other
    assert item.child(0) is b
    assert b.data(0, VIDEO_INDEX_ROLE) == 0
    assert a.parent() is None
    assert panel.tree.currentItem() is b
    assert b.isSelected() and other.isSelected()
    assert not item.isExpanded()
    assert b.foreground(0).color().name() == PRIMARY.casefold()

    panel.search.setText("replacement")
    assert item.isHidden() and other.isHidden()
    first.videos = ["replacement.mp4"]
    panel.update_case(first)
    assert not item.isHidden() and other.isHidden()
    assert panel.tree.currentItem() is item
    assert other.isSelected()
    assert item.child(0).foreground(0).color().name() == PRIMARY.casefold()


def test_full_structural_refresh_keeps_multiple_selected_rows(qtbot):
    panel = CaseListPanel()
    qtbot.addWidget(panel)
    cases = [InvestigationCase(videos=[f"{i}.mp4"]) for i in range(3)]
    panel.set_cases(cases, cases[0].uid)
    panel.tree.topLevelItem(2).setSelected(True)
    panel.tree.setCurrentItem(
        panel.tree.topLevelItem(0), 0, QItemSelectionModel.SelectionFlag.NoUpdate
    )
    panel.set_cases(cases, cases[0].uid)
    assert panel.tree.selected_cases() == [panel.tree.topLevelItem(i) for i in (0, 2)]
