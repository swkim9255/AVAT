"""Interactive, project-scoped statistics matching the supplied AVAT prototype."""

from __future__ import annotations

import math
from pathlib import Path
from statistics import quantiles
from typing import Any

from PySide6.QtCore import QDate, Qt, QTimer, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QButtonGroup,
    QCheckBox,
    QDateEdit,
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMenu,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSplitter,
    QTabBar,
    QTableWidget,
    QTableWidgetItem,
    QToolButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from investigator.butterfly import (
    build_butterfly,
    butterfly_groups,
    group_value_key,
    normalize_group_values,
)
from investigator.form_definition import FormDefinition
from investigator.models import InvestigationCase
from investigator.schema import FieldKind
from investigator.statistics import (
    INTERVAL_KEY,
    SEVERITY_KEY,
    TRIGGER_SPEED_KEY,
    AnyOf,
    Category,
    CrossTableLimitError,
    Distribution,
    FilterItem,
    StatisticsEngine,
    StatsCondition,
    StatsFilter,
    normalize_condition,
    normalize_filter_items,
)
from investigator.statistics_conditions import clean_field_label, condition_text
from investigator.theme import PRIMARY, material_icon
from investigator.widgets.scroll_safe_combo import ScrollSafeComboBox
from investigator.widgets.statistics_charts import (
    BoxPlotWidget,
    StatisticsBarChart,
    StatisticsButterflyChart,
    StatisticsDonutChart,
    StatisticsHeatmapDelegate,
    StatisticsScatterPlot,
    StatisticsStackedChart,
)
from investigator.widgets.statistics_condition_dialog import (
    StatisticsConditionDialog as StatisticsConditionDialog,
)
from investigator.widgets.statistics_condition_popup import StatisticsConditionPopup
from investigator.widgets.statistics_group_popup import StatisticsGroupPopup


def label(text: str, name: str = "secondaryText") -> QLabel:
    widget = QLabel(text)
    widget.setTextFormat(Qt.TextFormat.PlainText)
    widget.setWordWrap(True)
    widget.setObjectName(name)
    widget.setMinimumWidth(0)
    widget.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
    return widget


def inline_label(text: str, name: str = "secondaryText") -> QLabel:
    widget = label(text, name)
    widget.setWordWrap(False)
    widget.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Preferred)
    return widget


def clear_layout(layout) -> None:  # type: ignore[no-untyped-def]
    while layout.count():
        item = layout.takeAt(0)
        if widget := item.widget():
            widget.hide()
            widget.deleteLater()
        elif child := item.layout():
            clear_layout(child)
            child.deleteLater()


def card(title: str = "") -> tuple[QFrame, QVBoxLayout]:
    frame = QFrame()
    frame.setObjectName("statCard")
    frame.setMinimumWidth(0)
    layout = QVBoxLayout(frame)
    layout.setContentsMargins(18, 16, 18, 16)
    layout.setSpacing(12)
    layout.setAlignment(Qt.AlignmentFlag.AlignTop)
    if title:
        layout.addWidget(label(title, "statTitle"))
    return frame, layout


def table(headers: list[str]) -> QTableWidget:
    widget = QTableWidget(0, len(headers))
    widget.setHorizontalHeaderLabels(headers)
    widget.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
    widget.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
    widget.setAlternatingRowColors(True)
    widget.verticalHeader().hide()
    widget.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
    widget.setMinimumHeight(220)
    return widget


class StatisticsPageStack(QWidget):
    """Keep only the current page in the layout, including height-for-width.

    QStackedLayout uses every page's largest height-for-width even when hidden,
    which otherwise stretches a short cross table to a long chart's height.
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._pages: list[QWidget] = []
        self._index = -1
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(0, 0, 0, 0)

    def addWidget(self, widget: QWidget) -> None:
        widget.setParent(self)
        widget.hide()
        self._pages.append(widget)
        if self._index < 0:
            self.setCurrentIndex(0)

    def count(self) -> int:
        return len(self._pages)

    def widget(self, index: int) -> QWidget:
        return self._pages[index]

    def currentWidget(self) -> QWidget | None:
        return self._pages[self._index] if self._index >= 0 else None

    def setCurrentIndex(self, index: int) -> None:
        if index == self._index:
            return
        if current := self.currentWidget():
            self._layout.removeWidget(current)
            current.hide()
        self._index = index
        self._layout.addWidget(self._pages[index])
        self._pages[index].show()
        self.updateGeometry()


class StatisticsToggle(QFrame):
    """Compact exclusive buttons with the same value API as the old combos."""

    currentIndexChanged = Signal(int)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("statisticsToggle")
        self._values: list[Any] = []
        self._index = -1
        self._buttons = QButtonGroup(self)
        self._buttons.setExclusive(True)
        self._buttons.idClicked.connect(self.setCurrentIndex)
        self._layout = QHBoxLayout(self)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.setSpacing(0)
        self.setStyleSheet(
            "QFrame#statisticsToggle { border:1px solid #C9CED6; border-radius:6px; background:white; }"
            "QFrame#statisticsToggle QPushButton { border:0; border-radius:4px; padding:5px 9px;"
            " min-width:0; min-height:24px; font-size:12px; background:transparent; }"
            "QFrame#statisticsToggle QPushButton:checked { background:#1B1F24; color:white; }"
            "QFrame#statisticsToggle QPushButton:hover:!checked { background:#EEF2F8; }"
        )

    def addItem(self, text: str, value: Any) -> None:
        index = len(self._values)
        self._values.append(value)
        button = QPushButton(text, self)
        button.setCheckable(True)
        self._buttons.addButton(button, index)
        self._layout.addWidget(button)
        if self._index < 0:
            self._index = index
            button.setChecked(True)

    def findData(self, value: Any) -> int:
        return self._values.index(value) if value in self._values else -1

    def currentData(self) -> Any:
        return self._values[self._index] if self._index >= 0 else None

    def currentIndex(self) -> int:
        return self._index

    def setCurrentIndex(self, index: int) -> None:
        if not 0 <= index < len(self._values) or index == self._index:
            return
        self._index = index
        self._buttons.button(index).setChecked(True)
        self.currentIndexChanged.emit(index)


class StatisticsPanel(QWidget):
    case_activated = Signal(str, str)
    media_requested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("statisticsPanel")
        self.engine = StatisticsEngine([], FormDefinition.default())
        self.workspace = Path.cwd()
        self.conditions: list[FilterItem] = []
        # Only used when accepting an older in-memory project state. New filters
        # keep graph conditions in the same top-level AND list as manual ones.
        self.required_conditions: list[StatsCondition] = []
        self._selected_conditions: set[int] = set()
        self._condition_popup: StatisticsConditionPopup | None = None
        self._group_popup: StatisticsGroupPopup | None = None
        self._condition_menu: QMenu | None = None
        self.pinned_fields = ["collision_targets", "incident_day", "weather", "driver_age"]
        self._updating = False
        self._selection: tuple[tuple[str, ...], tuple[StatsCondition, ...], str] | None = None
        self._export_headers: list[str] = []
        self._export_rows: list[list[Any]] = []
        self._export_widget: QWidget = self
        self._build()

    def _build(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        filters = QFrame(self)
        filters.setObjectName("statisticsFilters")
        filters.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        filter_layout = QVBoxLayout(filters)
        filter_layout.setContentsMargins(20, 12, 20, 12)
        top = QHBoxLayout()
        top.setSpacing(8)
        filter_label = label("필터")
        filter_label.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Preferred)
        filter_label.setWordWrap(False)
        top.addWidget(filter_label)
        self.status = ScrollSafeComboBox(self)
        self.status.setObjectName("statisticsStatus")
        for text, value in (("완료만", "completed"), ("전체 · 임시저장 포함", "all"), ("임시저장만", "draft")):
            self.status.addItem(text, value)
        top.addWidget(self.status)
        self.period = QCheckBox("기간", self)
        top.addWidget(self.period)
        self.date_from = QDateEdit(QDate(QDate.currentDate().year(), 1, 1), self)
        self.date_to = QDateEdit(QDate.currentDate(), self)
        for edit in (self.date_from, self.date_to):
            edit.setDisplayFormat("yyyy-MM-dd")
            edit.setCalendarPopup(True)
            edit.setFixedWidth(128)
            edit.setEnabled(False)
        top.addWidget(self.date_from)
        separator = label("~")
        separator.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Preferred)
        separator.setWordWrap(False)
        top.addWidget(separator)
        top.addWidget(self.date_to)
        self.add_condition_button = QPushButton("+ 조건 추가", self)
        self.add_condition_button.clicked.connect(self.add_condition_dialog)
        top.addWidget(self.add_condition_button)
        self.clear_button = QPushButton("조건 해제", self)
        self.clear_button.clicked.connect(self.clear_conditions)
        top.addWidget(self.clear_button)
        top.addStretch(1)
        self.target_label = label("대상 0건 / 전체 0건")
        self.target_label.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Preferred)
        self.target_label.setWordWrap(False)
        self.export_button = QPushButton(material_icon("download", PRIMARY), "내보내기", self)
        menu = QMenu(self.export_button)
        for title, kind in (("현재 그래프 이미지 · PNG", "png"), ("현재 보기의 표 · CSV", "csv"), ("개요 보고서 · PDF", "pdf")):
            menu.addAction(title, lambda k=kind: self.export_dialog(k))
        self.export_button.setMenu(menu)
        top.addWidget(self.export_button)
        filter_layout.addLayout(top)
        condition_row = QHBoxLayout()
        condition_row.setSpacing(8)
        self.group_conditions_button = QPushButton("OR로 묶기", self)
        self.group_conditions_button.setObjectName("groupStatisticsConditions")
        self.group_conditions_button.setMinimumHeight(32)
        self.group_conditions_button.setEnabled(False)
        self.group_conditions_button.setToolTip("칩 왼쪽 체크박스로 조건을 2개 이상 선택하세요. 묶음은 다시 묶을 수 없습니다.")
        self.group_conditions_button.clicked.connect(self.group_selected_conditions)
        condition_row.addWidget(self.group_conditions_button)
        self.condition_hint = label("기본은 AND, 선택한 조건끼리 OR로 묶습니다.")
        condition_row.addWidget(self.condition_hint, 1)
        filter_layout.addLayout(condition_row)
        self.chips_scroll = QScrollArea(self)
        self.chips_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.chips_scroll.setWidgetResizable(True)
        self.chips_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.chips_scroll.setFixedHeight(self._condition_chip_height())
        self.chips_scroll.horizontalScrollBar().rangeChanged.connect(self._resize_condition_strip)
        self.chips_widget = QWidget(self)
        self.chips_layout = QHBoxLayout(self.chips_widget)
        self.chips_layout.setContentsMargins(0, 0, 0, 0)
        self.chips_layout.setAlignment(Qt.AlignmentFlag.AlignVCenter)
        self.chips_scroll.setWidget(self.chips_widget)
        chip_row = QHBoxLayout()
        chip_row.setSpacing(12)
        chip_row.addWidget(self.chips_scroll, 1)
        chip_row.addWidget(self.target_label)
        filter_layout.addLayout(chip_row)
        root.addWidget(filters)
        self.view_tabs = QTabBar(self)
        self.view_tabs.setObjectName("statisticsViews")
        self.view_tabs.setExpanding(False)
        self.view_tabs.setStyleSheet("QTabBar#statisticsViews::tab { padding: 10px 14px; }")
        for name in ("개요", "항목 분석", "교차 분석", "영상 지표", "데이터 품질"):
            self.view_tabs.addTab(name)
        self.view_tabs.currentChanged.connect(self._view_changed)
        root.addWidget(self.view_tabs)
        body = QSplitter(Qt.Orientation.Horizontal, self)
        body.setChildrenCollapsible(False)
        self.main_scroll = QScrollArea(self)
        self.main_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.main_scroll.setWidgetResizable(True)
        self.content = QWidget(self)
        self.content_layout = QVBoxLayout(self.content)
        self.content_layout.setContentsMargins(20, 20, 12, 20)
        self.content_layout.setSpacing(16)
        self.note = label("", "warningNote")
        self.content_layout.addWidget(self.note)
        self.pages = StatisticsPageStack(self)
        self.page_layouts = []
        for _ in range(5):
            page = QWidget(self)
            page.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Ignored)
            layout = QVBoxLayout(page)
            layout.setContentsMargins(0, 0, 0, 0)
            layout.setSpacing(16)
            self.page_layouts.append(layout)
            self.pages.addWidget(page)
        self.content_layout.addWidget(self.pages)
        self.content_layout.addStretch(1)
        self.main_scroll.setWidget(self.content)
        body.addWidget(self.main_scroll)
        self.drill, drill_layout = card()
        self.drill.setMinimumWidth(240)
        self.drill.setMaximumWidth(360)
        self.drill_title = label("막대나 칸을 눌러 보세요", "statTitle")
        drill_layout.addWidget(self.drill_title)
        self.drill_count = label("해당 사고를 선택하면 조사 화면으로 이동합니다.")
        drill_layout.addWidget(self.drill_count)
        self.drill_list = QTreeWidget(self)
        self.drill_list.setHeaderHidden(True)
        self.drill_list.setRootIsDecorated(False)
        self.drill_list.setColumnCount(2)
        self.drill_list.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.drill_list.header().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self.drill_list.itemClicked.connect(self._open_drill_case)
        self.drill_list.itemActivated.connect(self._open_drill_case)
        drill_layout.addWidget(self.drill_list, 1)
        self.show_all_button = QPushButton("전체 사고 보기", self)
        self.show_all_button.clicked.connect(lambda: self._render_drill(all_rows=True))
        drill_layout.addWidget(self.show_all_button)
        self.pin_condition_button = QPushButton("이 조건을 필터로 고정", self)
        self.pin_condition_button.setProperty("primary", True)
        self.pin_condition_button.clicked.connect(self.pin_selection)
        drill_layout.addWidget(self.pin_condition_button)
        clear = QPushButton("선택 해제", self)
        clear.clicked.connect(self.clear_selection)
        drill_layout.addWidget(clear)
        body.addWidget(self.drill)
        body.setStretchFactor(0, 1)
        body.setSizes([1050, 310])
        root.addWidget(body, 1)
        self.status.currentIndexChanged.connect(self._filters_changed)
        self.period.toggled.connect(self._period_changed)
        self.date_from.dateChanged.connect(lambda: self._date_changed(True))
        self.date_to.dateChanged.connect(lambda: self._date_changed(False))
        self._render_drill()

    def set_data(self, cases: list[InvestigationCase], definition: FormDefinition,
                 video_fps: dict[str, float] | None = None, workspace: Path | None = None) -> None:
        self._close_condition_popup()
        self.engine = StatisticsEngine(cases, definition, video_fps)
        if workspace is not None:
            self.workspace = workspace
        old_count = sum(len(item.conditions) if isinstance(item, AnyOf) else 1 for item in self.conditions)
        old_count += len(self.required_conditions)

        def compatible(condition: StatsCondition) -> bool:
            try:
                self.engine.validate_condition(condition)
            except (KeyError, ValueError):
                return False
            return True

        kept: list[FilterItem] = []
        for item in (*self.conditions, *self.required_conditions):
            if isinstance(item, AnyOf):
                members = tuple(condition for condition in item.conditions if compatible(condition))
                if members:
                    kept.append(AnyOf(members) if len(members) > 1 else members[0])
            elif compatible(item):
                kept.append(item)
        self.conditions = kept
        self.required_conditions.clear()
        self._selected_conditions.clear()
        removed = old_count - sum(len(item.conditions) if isinstance(item, AnyOf) else 1 for item in kept)
        if removed:
            self.condition_hint.setText(f"조사 양식 변경으로 사용할 수 없는 조건 {removed}개를 해제했습니다.")
        self.clear_selection()
        self.refresh()

    def filters(self) -> StatsFilter:
        return StatsFilter(
            self.status.currentData(),
            self.date_from.date().toString("yyyyMMdd") if self.period.isChecked() else "",
            self.date_to.date().toString("yyyyMMdd") if self.period.isChecked() else "",
            tuple(self.conditions),
            required_conditions=tuple(self.required_conditions),
        )

    def capture_state(self) -> dict[str, Any]:
        return {"status": self.status.currentData(), "period": self.period.isChecked(),
                "from": self.date_from.date(), "to": self.date_to.date(),
                "conditions": normalize_filter_items(self.conditions, required_conditions=self.required_conditions),
                "pinned": tuple(self.pinned_fields),
                "filter_version": 2,
                "condition_mode": "all",
                "required_conditions": (),
                "view": self.view_tabs.currentIndex(), "item": getattr(self, "_item_key", "collision_targets"),
                "row": getattr(self, "_row_key", "weather"), "column": getattr(self, "_column_key", SEVERITY_KEY),
                "video_group": getattr(self, "_video_group", "collision_targets"),
                "mode": getattr(self, "_item_mode", "count"),
                "orientation": getattr(self, "_item_orientation", "auto"),
                "cross_view": getattr(self, "_cross_view", "heat"),
                "cross_mode": getattr(self, "_cross_mode", "row"),
                "butterfly_mode": getattr(self, "_butterfly_mode", "two"),
                "butterfly_unit": getattr(self, "_butterfly_unit", "percent"),
                "butterfly_group_key": getattr(self, "_butterfly_group_key", None),
                "butterfly_left": getattr(self, "_butterfly_left", None),
                "butterfly_right": getattr(self, "_butterfly_right", None),
                "butterfly_left_values": getattr(self, "_butterfly_left_values", ()),
                "butterfly_right_values": getattr(self, "_butterfly_right_values", ())}

    def restore_state(self, state: dict[str, Any]) -> None:
        self._close_condition_popup()
        self._updating = True
        try:
            self.status.setCurrentIndex(max(0, self.status.findData(state.get("status", "completed"))))
            self.period.setChecked(state.get("period", False))
            self.date_from.setDate(state.get("from", QDate(QDate.currentDate().year(), 1, 1)))
            self.date_to.setDate(state.get("to", QDate.currentDate()))
            self.conditions = list(normalize_filter_items(
                state.get("conditions", ()), state.get("condition_mode", "all"),
                state.get("required_conditions", ()),
            ))
            self.required_conditions.clear()
            self._selected_conditions.clear()
            self.pinned_fields = list(state.get("pinned", ("collision_targets", "incident_day", "weather", "driver_age")))
            self._item_key = state.get("item", "collision_targets")
            self._row_key, self._column_key = state.get("row", "weather"), state.get("column", SEVERITY_KEY)
            self._item_mode, self._cross_mode = state.get("mode", "count"), state.get("cross_mode", "row")
            self._item_orientation = state.get("orientation", "auto")
            self._cross_view = state.get("cross_view", "heat")
            self._butterfly_mode = state.get("butterfly_mode", "two")
            self._butterfly_unit = state.get("butterfly_unit", "percent")
            self._butterfly_group_key = state.get("butterfly_group_key")
            self._butterfly_left = state.get("butterfly_left")
            self._butterfly_right = state.get("butterfly_right")
            for side in ("left", "right"):
                old = state.get(f"butterfly_{side}")
                selected = state.get(f"butterfly_{side}_values", (old,) if old is not None else ())
                setattr(self, f"_butterfly_{side}_values", normalize_group_values(selected))
            self._video_group = state.get("video_group", "collision_targets")
            self.view_tabs.setCurrentIndex(state.get("view", 0))
        finally:
            self._updating = False

    def _period_changed(self) -> None:
        self.date_from.setEnabled(self.period.isChecked())
        self.date_to.setEnabled(self.period.isChecked())
        self._filters_changed()

    def _date_changed(self, start_changed: bool) -> None:
        if self._updating:
            return
        self._updating = True
        try:
            if self.date_from.date() > self.date_to.date():
                if start_changed:
                    self.date_to.setDate(self.date_from.date())
                else:
                    self.date_from.setDate(self.date_to.date())
        finally:
            self._updating = False
        self._filters_changed()

    def _filters_changed(self) -> None:
        if self._updating:
            return
        self._close_condition_popup()
        self._reset_condition_hint()
        self._selected_conditions.clear()
        self.clear_selection()
        self.refresh()

    def _view_changed(self, index: int) -> None:
        if self._updating:
            return
        self._close_condition_popup()
        self.clear_selection()
        self.refresh()
        if index == 3:
            self.media_requested.emit()

    def add_condition_dialog(self) -> None:
        self._open_condition_popup(self.add_condition_button)

    def _close_condition_popup(self) -> None:
        if self._group_popup is not None:
            self._group_popup.close()
            self._group_popup.deleteLater()
            self._group_popup = None
        if self._condition_popup is not None:
            self._condition_popup.close()
            self._condition_popup.deleteLater()
            self._condition_popup = None
        if self._condition_menu is not None:
            self._condition_menu.close()
            self._condition_menu.deleteLater()
            self._condition_menu = None

    def _open_condition_popup(self, anchor: QWidget, condition: StatsCondition | None = None,
                              index: int | None = None, member: int | None = None) -> None:
        self._close_condition_popup()
        popup = StatisticsConditionPopup(
            self.engine, self.filters(), condition, self, ignore_status=self.view_tabs.currentIndex() == 4,
        )
        self._condition_popup = popup

        def apply(updated: StatsCondition) -> None:
            if index is None:
                self.add_condition(updated)
            elif member is None:
                self.conditions[index] = updated
                self._conditions_edited()
            else:
                group = self.conditions[index]
                if not isinstance(group, AnyOf):
                    return
                members = list(group.conditions)
                members[member] = updated
                self.conditions[index] = AnyOf(tuple(members))
                self._conditions_edited()

        popup.applied.connect(apply)
        popup.open_at(anchor)

    def edit_condition(self, index: int) -> None:
        if not 0 <= index < len(self.conditions):
            return
        condition = self.conditions[index]
        anchor = self.add_condition_button
        for position in range(self.chips_layout.count()):
            chip = self.chips_layout.itemAt(position).widget()
            if chip is not None and chip.property("conditionIndex") == index:
                anchor = chip.findChild(QPushButton, "filterChip") or anchor
                break
        if not isinstance(condition, AnyOf):
            self._open_condition_popup(anchor, normalize_condition(condition), index)
            return
        # A group has no single field: choose the member to edit, then show the
        # same checklist popup. This menu never introduces nested OR groups.
        self._close_condition_popup()
        menu = QMenu(self)
        self._condition_menu = menu
        heading = menu.addAction("OR 묶음 · 수정할 조건을 선택하세요")
        heading.setEnabled(False)
        for member, item in enumerate(condition.conditions):
            action = menu.addAction(self.condition_text(item))
            action.triggered.connect(
                lambda checked=False, c=item, m=member: QTimer.singleShot(
                    0, lambda: self._open_condition_popup(anchor, normalize_condition(c), index, m),
                ),
            )
        menu.popup(anchor.mapToGlobal(anchor.rect().bottomLeft()))

    def _reset_condition_hint(self) -> None:
        self.condition_hint.setText("기본은 AND, 선택한 조건끼리 OR로 묶습니다.")

    def _conditions_edited(self) -> None:
        self._selected_conditions.clear()
        self._reset_condition_hint()
        self.clear_selection()
        self.refresh()

    def add_condition(self, condition: FilterItem) -> None:
        self.engine.validate_condition(condition)
        if condition not in self.conditions:
            self.conditions.append(condition)
        self._conditions_edited()

    def clear_conditions(self) -> None:
        self._close_condition_popup()
        self.conditions.clear()
        self.required_conditions.clear()
        self._conditions_edited()

    def _remove_condition(self, condition: FilterItem) -> None:
        self._remove_condition_at(self.conditions.index(condition))

    def _remove_condition_at(self, index: int) -> None:
        self._close_condition_popup()
        if 0 <= index < len(self.conditions):
            del self.conditions[index]
            self._conditions_edited()

    def _remove_required_condition(self, condition: StatsCondition) -> None:
        self.required_conditions.remove(condition)
        self._conditions_edited()

    def _select_condition(self, index: int, selected: bool) -> None:
        if selected and 0 <= index < len(self.conditions) and not isinstance(self.conditions[index], AnyOf):
            self._selected_conditions.add(index)
        else:
            self._selected_conditions.discard(index)
        self.group_conditions_button.setEnabled(len(self._selected_conditions) >= 2)

    def group_selected_conditions(self) -> None:
        indices = sorted(i for i in self._selected_conditions
                         if 0 <= i < len(self.conditions) and not isinstance(self.conditions[i], AnyOf))
        if len(indices) < 2:
            return
        self._close_condition_popup()
        group = AnyOf(tuple(self.conditions[i] for i in indices))
        selected = set(indices)
        self.conditions = [group if i == indices[0] else item
                           for i, item in enumerate(self.conditions) if i not in selected or i == indices[0]]
        self._conditions_edited()

    def ungroup_condition(self, index: int) -> None:
        if 0 <= index < len(self.conditions) and isinstance(self.conditions[index], AnyOf):
            self._close_condition_popup()
            self.conditions[index:index + 1] = self.conditions[index].conditions
            self._conditions_edited()

    def _condition_context_menu(self, index: int, anchor: QWidget, position) -> None:
        menu = QMenu(self)
        menu.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        menu.addAction("묶음 풀기", lambda: self.ungroup_condition(index))
        menu.popup(anchor.mapToGlobal(position))

    def condition_text(self, condition: FilterItem) -> str:
        return condition_text(self.engine, condition)

    def _condition_chip_height(self) -> int:
        self.group_conditions_button.ensurePolished()
        return max(32, self.group_conditions_button.sizeHint().height())

    def _resize_condition_strip(self, minimum: int, maximum: int) -> None:
        # Reserve scrollbar space only for overflowing chips, not as permanent
        # empty padding above/below every condition.
        scrollbar_height = self.chips_scroll.horizontalScrollBar().sizeHint().height()
        self.chips_scroll.setFixedHeight(self._condition_chip_height()
                                        + (scrollbar_height if maximum > minimum else 0))

    def filter_description(self) -> str:
        parts = [self.status.currentText()]
        if self.period.isChecked():
            parts.append(f"{self.date_from.text()} ~ {self.date_to.text()}")
        else:
            parts.append("전체 기간")
        if self.conditions:
            parts.append(" AND ".join(self.condition_text(c) for c in self.conditions))
        if self.required_conditions:
            parts.append("AND 그래프 선택: (" + " AND ".join(
                self.condition_text(c) for c in self.required_conditions
            ) + ")")
        if self.view_tabs.currentIndex() == 4:
            parts.append("데이터 품질은 상태 필터를 제외")
        return " · ".join(parts)

    def _condition_chip(self, index: int, condition: FilterItem) -> QFrame:
        chip = QFrame(self.chips_widget)
        chip.setObjectName("statisticsConditionChip")
        chip.setProperty("conditionIndex", index)
        chip.setFixedHeight(self._condition_chip_height())
        chip.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)
        chip.setStyleSheet(
            "QFrame#statisticsConditionChip { background:#FFFFFF; border:1px solid #C9CED6; border-radius:6px; }"
            "QPushButton#filterChip { border:0; background:transparent; border-radius:0; padding:0 3px; min-width:0; min-height:26px; }"
            "QPushButton#filterChip:hover { color:#225FC1; background:#F1F4F8; }"
            "QToolButton#filterChipRemove { border:0; background:transparent; padding:0; min-width:0; min-height:0; font-size:17px; }"
            "QToolButton#filterChipRemove:hover { color:#A52A2A; background:#FFF1F1; }"
        )
        row = QHBoxLayout(chip)
        row.setContentsMargins(5, 0, 3, 0)
        row.setSpacing(2)
        selector = QCheckBox(chip)
        selector.setObjectName("filterChipSelect")
        selector.setFixedWidth(16)
        selector.setProperty("conditionIndex", index)
        selector.setAccessibleName("OR로 묶을 조건 선택")
        selector.setChecked(index in self._selected_conditions)
        selector.setEnabled(not isinstance(condition, AnyOf))
        selector.setToolTip("묶음은 다시 묶을 수 없습니다." if isinstance(condition, AnyOf)
                            else "다른 조건과 OR로 묶으려면 선택하세요.")
        selector.toggled.connect(lambda checked, i=index: self._select_condition(i, checked))
        row.addWidget(selector)
        text = self.condition_text(condition)
        body = QPushButton(chip)
        body.setObjectName("filterChip")
        body.setProperty("conditionIndex", index)
        body.setText(body.fontMetrics().elidedText(text, Qt.TextElideMode.ElideRight, 360))
        body.setAccessibleName(text + " · 조건 수정")
        body.setToolTip(text + "\n클릭하여 수정" + (" · 우클릭하여 묶음 풀기" if isinstance(condition, AnyOf) else ""))
        body.clicked.connect(lambda checked=False, i=index: self.edit_condition(i))
        row.addWidget(body)
        remove = QToolButton(chip)
        remove.setObjectName("filterChipRemove")
        remove.setProperty("conditionIndex", index)
        remove.setText("×")
        remove.setFixedSize(22, 26)
        remove.setAccessibleName(text + " · 조건 삭제")
        remove.setToolTip("조건 삭제")
        remove.clicked.connect(lambda checked=False, i=index: self._remove_condition_at(i))
        row.addWidget(remove)
        if isinstance(condition, AnyOf):
            for widget in (chip, body):
                widget.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
                widget.customContextMenuRequested.connect(
                    lambda pos, i=index, anchor=widget: self._condition_context_menu(i, anchor, pos),
                )
        # Keep each chip content-sized; otherwise a crowded row compresses the
        # labels instead of exposing the scroll area's horizontal scrollbar.
        chip.ensurePolished()
        chip.setFixedWidth(chip.sizeHint().width())
        return chip

    def refresh(self) -> None:
        if self._updating:
            return
        if self.required_conditions:
            self.conditions.extend(self.required_conditions)
            self.required_conditions.clear()
        filters = self.filters()
        view = self.view_tabs.currentIndex()
        selected = self.engine.filtered_cases(filters, ignore_status=view == 4)
        self.target_label.setText(f"대상 {len(selected):,}건 / 전체 {len(self.engine.cases):,}건")
        self.note.setVisible(len(selected) < 20)
        self.note.setText("조건에 해당하는 사고가 없습니다. 필터를 조정해 주세요." if not selected else
                          f"대상 사고가 {len(selected)}건입니다. 비율과 함께 실제 건수를 확인해 주세요.")
        clear_layout(self.chips_layout)
        self._selected_conditions.intersection_update(
            i for i, item in enumerate(self.conditions) if not isinstance(item, AnyOf)
        )
        self.group_conditions_button.setEnabled(len(self._selected_conditions) >= 2)
        for index, condition in enumerate(self.conditions):
            if index:
                self.chips_layout.addWidget(inline_label("AND"))
            self.chips_layout.addWidget(self._condition_chip(index, condition))
        self.chips_layout.addStretch(1)
        self.clear_button.setEnabled(bool(self.conditions))
        clear_layout(self.page_layouts[view])
        for index in range(self.pages.count()):
            policy = QSizePolicy.Policy.Preferred if index == view else QSizePolicy.Policy.Ignored
            self.pages.widget(index).setSizePolicy(policy, policy)
        self.pages.setCurrentIndex(view)
        self._export_widget = self.pages.widget(view)
        [self._overview, self._item, self._cross, self._video, self._quality][view]()
        self.pages.currentWidget().layout().activate()
        self.pages.updateGeometry()
        self._render_drill()

    def _kpis(self, layout: QVBoxLayout, values: list[tuple[str, str, str, int | None]]) -> None:
        grid = QGridLayout()
        grid.setSpacing(12)
        for i, (title, value, detail, destination) in enumerate(values):
            frame, inner = card()
            inner.addWidget(label(title))
            inner.addWidget(label(value, "statValue"))
            if destination is not None:
                button = QPushButton(detail, self)
                button.setProperty("flat", True)
                button.clicked.connect(lambda checked=False, target=destination: self.view_tabs.setCurrentIndex(target))
                inner.addWidget(button)
            else:
                inner.addWidget(label(detail))
            grid.addWidget(frame, 0, i)
            grid.setColumnStretch(i, 1)
        layout.addLayout(grid)

    @staticmethod
    def _seconds(value: Any) -> str:
        return "확인 필요" if value is None else f"{value:.2f}초"

    def _overview(self) -> None:
        layout = self.page_layouts[0]
        summary = self.engine.overview(self.filters())
        injury = summary.get("injury_cases", summary["severe_cases"])
        self._kpis(layout, [
            ("사고 수", f"{summary['selected']:,}건", "현재 프로젝트 · 입력 중 변경 포함", None),
            ("인명피해 사고", f"{injury:,}건", "경상 · 중상 · 사망이 확인된 사고", None),
            ("평균 트리거 → 충돌", self._seconds(summary["mean_interval_seconds"]), "영상 지표 확인 →", 3),
            ("미입력이 있는 사고", f"{summary['missing_cases']:,}건", "데이터 품질에서 채우기 →", 4),
        ])
        layout.addWidget(label("고정한 항목", "statTitle"))
        grid = QGridLayout()
        grid.setSpacing(12)
        self._export_headers = ["항목", "값", "건수", "비율(%)", "분모"]
        self._export_rows = []
        for i, key in enumerate(k for k in self.pinned_fields if k in {f.key for f in self.engine.fields}):
            dist = self.engine.distribution(key, self.filters())
            frame, inner = card()
            heading = QHBoxLayout()
            heading.addWidget(label(dist.label, "statTitle"), 1)
            button = QPushButton("자세히 →", self)
            button.setProperty("flat", True)
            button.clicked.connect(lambda checked=False, k=key: self.open_item(k))
            heading.addWidget(button)
            inner.addLayout(heading)
            inner.addWidget(label(f"응답 {dist.answered:,}건 · 미입력 {dist.missing:,}건 · 분모 {dist.denominator:,}건"))
            inner.addWidget(self._chart(dist, mini=True))
            grid.addWidget(frame, i // 2, i % 2)
            self._export_rows.extend([[dist.label, b.label, b.count, round(b.percent(dist.denominator), 2), dist.denominator] for b in dist.buckets])
        grid.setColumnStretch(0, 1)
        grid.setColumnStretch(1, 1)
        layout.addLayout(grid)

    def _chart(self, dist: Distribution, *, mini: bool = False) -> StatisticsBarChart:
        chart = StatisticsBarChart(self)
        default_vertical = dist.kind == "numeric" or self.engine.is_ordered(dist.field_key)
        item_preferences = not mini and self.view_tabs.currentIndex() == 1
        orientation = getattr(self, "_item_orientation", "auto") if item_preferences else "auto"
        chart.set_data([{"label": b.label, "count": b.count, "uids": b.case_uids,
                         "value": b.value, "missing": b.missing} for b in dist.buckets],
                       dist.denominator, "percent" if getattr(self, "_item_mode", "count") == "percent" and item_preferences else "count",
                       vertical=default_vertical if orientation == "auto" else orientation == "v")
        chart.bucket_clicked.connect(lambda index, d=dist, c=chart: self.select_category(d.buckets[index], d.label, c, index))
        return chart

    def open_item(self, key: str) -> None:
        if key != getattr(self, "_item_key", None):
            self._item_orientation = "auto"
        self._item_key = key
        if self.view_tabs.currentIndex() == 1:
            self.clear_selection()
            self.refresh()
        else:
            self.view_tabs.setCurrentIndex(1)

    def _field_picker(self, value: str) -> ScrollSafeComboBox:
        combo = ScrollSafeComboBox(self)
        combo.setMinimumWidth(0)
        combo.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        for spec in self.engine.fields:
            combo.addItem(spec.label, spec.key)
        combo.setCurrentIndex(max(0, combo.findData(value)))
        return combo

    def _item(self) -> None:
        layout = self.page_layouts[1]
        row = QHBoxLayout()
        navigation, navigation_layout = card("조사 항목")
        navigation.setMaximumWidth(245)
        search = QLineEdit(self)
        search.setPlaceholderText("항목 찾기")
        navigation_layout.addWidget(search)
        tree = QTreeWidget(self)
        tree.setObjectName("statisticsFieldTree")
        tree.setHeaderHidden(True)
        tree.setMinimumHeight(430)
        tree.setIndentation(10)
        tree.setStyleSheet("QTreeWidget#statisticsFieldTree::item { min-height:26px; padding:3px 2px; font-size:12px; }")
        groups = {spec.key: section.short_title for section in self.engine.definition.specs() for spec in section.fields}
        nodes: dict[str, QTreeWidgetItem] = {}
        key = getattr(self, "_item_key", "collision_targets")
        if key not in {f.key for f in self.engine.fields}:
            key = "collision_targets" if "collision_targets" in {f.key for f in self.engine.fields} else SEVERITY_KEY
        for spec in self.engine.fields:
            group = groups.get(spec.key, "파생 지표")
            if group not in nodes:
                nodes[group] = QTreeWidgetItem(tree, [group])
            field_dist = self.engine.distribution(spec.key, self.filters())
            rate = field_dist.answered * 100 / field_dist.denominator if field_dist.denominator else 0.0
            item = QTreeWidgetItem(nodes[group], [spec.label, f"{rate:.0f}%"])
            item.setToolTip(0, f"{spec.label} · 응답 {field_dist.answered:,} / 해당 {field_dist.denominator:,}건")
            item.setData(0, Qt.ItemDataRole.UserRole, spec.key)
            if spec.key == key:
                tree.setCurrentItem(item)
        tree.expandAll()

        def filter_fields(text: str) -> None:
            for group in nodes.values():
                for i in range(group.childCount()):
                    item = group.child(i)
                    item.setHidden(text.casefold() not in item.text(0).casefold())
                group.setHidden(all(group.child(i).isHidden() for i in range(group.childCount())))

        search.textChanged.connect(filter_fields)
        tree.setColumnCount(2)
        tree.header().setStretchLastSection(False)
        tree.header().setMinimumSectionSize(28)
        tree.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        tree.header().setSectionResizeMode(1, QHeaderView.ResizeMode.Fixed)
        tree.header().resizeSection(1, 64)
        tree.itemClicked.connect(lambda item: self.open_item(item.data(0, Qt.ItemDataRole.UserRole)) if item.parent() else None)
        navigation_layout.addWidget(tree)
        navigation_layout.addWidget(label("오른쪽 숫자는 해당 사고의 입력률입니다."))
        row.addWidget(navigation, 1)
        dist = self.engine.distribution(key, self.filters())
        self._item_key = key
        frame, inner = card(dist.label)
        toolbar = QHBoxLayout()
        pin = QPushButton("고정 해제" if key in self.pinned_fields else "개요에 고정", self)
        pin.clicked.connect(lambda: self._toggle_pin(key))
        toolbar.addWidget(pin)
        toolbar.addStretch(1)
        self.item_mode = StatisticsToggle(self)
        modes = [("막대", "count"), ("비율", "percent")]
        if self._can_use_donut(dist):
            modes.append(("파이", "pie"))
        modes.append(("표", "table"))
        for text, value in modes:
            self.item_mode.addItem(text, value)
        self.item_mode.setCurrentIndex(max(0, self.item_mode.findData(getattr(self, "_item_mode", "count"))))
        self._item_mode = self.item_mode.currentData()
        self.item_mode.currentIndexChanged.connect(self._change_item_mode)
        toolbar.addWidget(self.item_mode)
        inner.addLayout(toolbar)
        if self._item_mode in {"count", "percent"}:
            direction = QHBoxLayout()
            direction.addWidget(inline_label("막대 방향"))
            direction.addStretch(1)
            self.item_orientation = StatisticsToggle(self)
            self.item_orientation.addItem("가로", "h")
            self.item_orientation.addItem("세로", "v")
            orientation = getattr(self, "_item_orientation", "auto")
            if orientation == "auto":
                orientation = "v" if dist.kind == "numeric" or self.engine.is_ordered(key) else "h"
            self.item_orientation.setCurrentIndex(max(0, self.item_orientation.findData(orientation)))
            self.item_orientation.currentIndexChanged.connect(self._change_item_orientation)
            direction.addWidget(self.item_orientation)
            inner.addLayout(direction)
        spec = self.engine.field(key)
        kinds = {"single": "선택형", "multi": "다중 선택", "digits": "숫자", "decimal": "수치", "text": "텍스트"}
        kind_text = kinds.get(spec.kind.value, spec.kind.value)
        if self.engine.is_ordered(key):
            kind_text += " · 순서 있음"
        inner.addWidget(label(f"{kind_text} · {groups.get(key, '파생 지표')} 탭"))
        inner.addWidget(label(f"응답 {dist.answered:,}건 · 미입력 {dist.missing:,}건 · 비율 분모 {dist.denominator:,}건 · 해당 없음 {dist.not_applicable:,}건"))
        if dist.summary.get("count"):
            summary_text = " · ".join(f"{name} {dist.summary[k]:,.2f}{spec.unit}" for k, name in (("mean", "평균"), ("median", "중앙값"), ("min", "최소"), ("max", "최대")))
            if "q1" in dist.summary and "q3" in dist.summary:
                summary_text += f" · 가운데 50% {dist.summary['q1']:,.2f}–{dist.summary['q3']:,.2f}{spec.unit}"
            inner.addWidget(label(summary_text))
        if getattr(self, "_item_mode", "count") == "table":
            widget = table(["값", "건수", "비율", "드릴다운"])
            widget.setRowCount(len(dist.buckets))
            for i, bucket in enumerate(dist.buckets):
                for j, text in enumerate((bucket.label, str(bucket.count), f"{bucket.percent(dist.denominator):.1f}%", "사고 보기 →")):
                    widget.setItem(i, j, QTableWidgetItem(text))
            widget.cellClicked.connect(lambda i, j: self.select_category(dist.buckets[i], dist.label))
            inner.addWidget(widget)
        elif self._item_mode == "pie":
            chart = StatisticsDonutChart(self)
            chart.set_data([dict(label=b.label, count=b.count, missing=b.missing) for b in dist.buckets], denominator=dist.denominator)
            chart.category_clicked.connect(lambda i: self.select_category(dist.buckets[i], dist.label, chart, i))
            inner.addWidget(chart)
        else:
            inner.addWidget(self._chart(dist))
        note = "다중 선택은 사고별로 집계하므로 비율 합계가 100%를 넘을 수 있습니다." if dist.multi else "비율은 해당 사고 수를 분모로 계산하며 미입력도 포함합니다."
        if dist.kind == "numeric":
            note += " 숫자 구간은 이상·미만 기준입니다. 구간 폭이 다르면 막대 높이만으로 밀도를 비교하지 마세요."
        inner.addWidget(label(note))
        row.addWidget(frame, 3)
        layout.addLayout(row)
        self._export_widget = frame
        self._export_headers = ["값", "건수", "비율(%)", "분모", "항목"]
        self._export_rows = [[b.label, b.count, round(b.percent(dist.denominator), 2), dist.denominator, dist.label] for b in dist.buckets]

    def _change_item_mode(self) -> None:
        self._item_mode = self.item_mode.currentData()
        self.refresh()

    def _can_use_donut(self, dist: Distribution) -> bool:
        return (
            self.engine.field(dist.field_key).kind == FieldKind.SINGLE
            and not dist.multi and not self.engine.is_ordered(dist.field_key)
            and len([bucket for bucket in dist.buckets if not bucket.missing]) <= 5
        )

    def _change_item_orientation(self) -> None:
        self._item_orientation = self.item_orientation.currentData()
        self.refresh()

    def _toggle_pin(self, key: str) -> None:
        if key in self.pinned_fields:
            self.pinned_fields.remove(key)
        else:
            self.pinned_fields.append(key)
        self.refresh()

    def _cross(self) -> None:
        frame, inner = card("교차 분석")
        presets = QHBoxLayout()
        presets.addWidget(inline_label("예시 조합"))
        field_keys = {field.key for field in self.engine.fields}
        categorical = [field.key for field in self.engine.fields if not self.engine.is_numeric(field.key)]
        first_category = next((key for key in categorical if key != SEVERITY_KEY), SEVERITY_KEY)
        for text, x, y in (
            ("범주 × 범주", "weather" if "weather" in field_keys else first_category, SEVERITY_KEY),
            ("범주 × 수치", "collision_targets" if "collision_targets" in field_keys else first_category, TRIGGER_SPEED_KEY),
            ("수치 × 수치", TRIGGER_SPEED_KEY, INTERVAL_KEY),
        ):
            button = QPushButton(text, self)
            button.setToolTip(f"{self.engine.field(x).label} × {self.engine.field(y).label}")
            button.clicked.connect(lambda checked=False, x=x, y=y: self._set_cross_pair(x, y))
            presets.addWidget(button)
        presets.addStretch(1)
        inner.addLayout(presets)
        toolbar = QGridLayout()
        self.cross_row = self._field_picker(getattr(self, "_row_key", "weather"))
        self.cross_column = self._field_picker(getattr(self, "_column_key", SEVERITY_KEY))
        row_label = label("행 · 가로축 항목")
        toolbar.addWidget(row_label, 0, 0)
        toolbar.addWidget(self.cross_row, 1, 0)
        toolbar.addWidget(label("×"), 1, 1)
        column_label = label("열 · 세로축 항목")
        toolbar.addWidget(column_label, 0, 2)
        toolbar.addWidget(self.cross_column, 1, 2)
        swap = QPushButton("행·열 바꾸기", self)
        swap.clicked.connect(self._swap_cross)
        toolbar.addWidget(swap, 1, 3)
        toolbar.setColumnStretch(0, 1)
        toolbar.setColumnStretch(2, 1)
        inner.addLayout(toolbar)
        self._row_key, self._column_key = self.cross_row.currentData(), self.cross_column.currentData()
        same = self._row_key == self._column_key
        row_numeric, column_numeric = self.engine.is_numeric(self._row_key), self.engine.is_numeric(self._column_key)
        self._cross_kind = "same" if same else "nn" if row_numeric and column_numeric else "cn" if row_numeric or column_numeric else "cc"
        controls = QHBoxLayout()
        self.cross_kind_badge = inline_label({"same": "같은 항목", "cc": "범주 × 범주", "cn": "범주 × 수치", "nn": "수치 × 수치"}[self._cross_kind], "statTypeBadge")
        self.cross_kind_badge.setStyleSheet("background:#E7EEF9; color:#17468C; padding:4px 10px; border-radius:6px; font-size:12px;")
        controls.addWidget(self.cross_kind_badge)
        controls.addStretch(1)
        self.cross_view = StatisticsToggle(self)
        if self._cross_kind == "cn":
            self.cross_view.addItem("상자수염", "box")
        else:
            self.cross_view.addItem("히트맵", "heat")
            self.cross_view.addItem("100% 막대", "stack")
        self.cross_view.addItem("나비형 비교", "butterfly")
        self.cross_view.setCurrentIndex(max(0, self.cross_view.findData(getattr(self, "_cross_view", "heat"))))
        self._cross_view = self.cross_view.currentData()
        self.cross_view.setVisible(self._cross_kind in {"cc", "cn"})
        if self._cross_view == "butterfly" and self._cross_kind in {"cc", "cn"}:
            row_label.setText("가운데 축 · 수치 구간" if row_numeric else "가운데 축" if not column_numeric else "좌우 그룹")
            column_label.setText("좌우 그룹" if row_numeric or not column_numeric else "가운데 축 · 수치 구간")
        controls.addWidget(self.cross_view)
        self.cross_mode = StatisticsToggle(self)
        self.cross_mode.addItem("행 기준 %", "row")
        self.cross_mode.addItem("건수", "count")
        self.cross_mode.setCurrentIndex(0 if getattr(self, "_cross_mode", "row") == "row" else 1)
        self.cross_mode.setVisible(self._cross_kind == "cc" and self._cross_view == "heat")
        inner.addLayout(controls)
        units = QHBoxLayout()
        units.addStretch(1)
        units.addWidget(self.cross_mode)
        inner.addLayout(units)
        for combo in (self.cross_row, self.cross_column, self.cross_mode, self.cross_view):
            combo.currentIndexChanged.connect(self._change_cross)
        self.page_layouts[2].addWidget(frame)
        self._export_widget = frame
        self._export_headers, self._export_rows = [], []
        if same:
            inner.addWidget(label("서로 다른 두 항목을 골라 주세요."))
        elif self._cross_view == "butterfly" and self._cross_kind in {"cc", "cn"}:
            self._cross_butterfly(inner)
        elif self._cross_kind == "cc":
            self._cross_categorical(inner)
        elif self._cross_kind == "cn":
            self._cross_numeric_groups(inner)
        else:
            self._cross_scatter(inner)

    def _set_cross_pair(self, row: str, column: str) -> None:
        self._row_key, self._column_key = row, column
        self.clear_selection()
        self.refresh()

    def _cross_butterfly(self, inner: QVBoxLayout) -> None:
        axis_key, group_key = self._row_key, self._column_key
        if self.engine.is_numeric(group_key):
            axis_key, group_key = group_key, axis_key
        try:
            groups = butterfly_groups(self.engine, group_key, self.filters())
        except (ValueError, CrossTableLimitError) as exc:
            inner.addWidget(label(str(exc), "warningNote"))
            return
        if len(groups) < 2:
            inner.addWidget(label("좌우를 비교할 수 있는 그룹이 2개 이상인 항목을 선택해 주세요.", "warningNote"))
            return
        values = tuple(group.value for group in groups)
        available = {group_value_key(value) for value in values}
        if getattr(self, "_butterfly_group_key", None) != group_key:
            self._butterfly_left_values, self._butterfly_right_values = (values[0],), (values[1],)
        self._butterfly_group_key = group_key
        left = tuple(value for value in getattr(self, "_butterfly_left_values", ())
                     if group_value_key(value) in available) or (values[0],)
        left_keys = {group_value_key(value) for value in left}
        right = tuple(value for value in getattr(self, "_butterfly_right_values", ())
                      if group_value_key(value) in available and group_value_key(value) not in left_keys)
        if not right:
            right = tuple(value for value in values if group_value_key(value) not in left_keys)[:1]
        self._butterfly_left_values, self._butterfly_right_values = left, right
        self._butterfly_left = left[0] if left else None
        self._butterfly_right = right[0] if right else None
        self.butterfly_mode = StatisticsToggle(self)
        self.butterfly_mode.addItem("두 그룹", "two")
        self.butterfly_mode.addItem("한 그룹 vs 나머지", "rest")
        self.butterfly_mode.setCurrentIndex(max(0, self.butterfly_mode.findData(getattr(self, "_butterfly_mode", "two"))))
        self._butterfly_mode = self.butterfly_mode.currentData()
        self.butterfly_unit = StatisticsToggle(self)
        self.butterfly_unit.addItem("그룹 내 %", "percent")
        self.butterfly_unit.addItem("건수", "count")
        self.butterfly_unit.setCurrentIndex(max(0, self.butterfly_unit.findData(getattr(self, "_butterfly_unit", "percent"))))
        self._butterfly_unit = self.butterfly_unit.currentData()
        controls = QHBoxLayout()
        controls.addWidget(self.butterfly_mode)
        controls.addStretch(1)
        controls.addWidget(self.butterfly_unit)
        inner.addLayout(controls)
        selectors = QGridLayout()
        self.butterfly_left = QPushButton(self)
        self.butterfly_right = QPushButton(self)
        labels = {group_value_key(group.value): group.label for group in groups}
        for side, button in (("left", self.butterfly_left), ("right", self.butterfly_right)):
            button.setMinimumWidth(0)
            button.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
            button.setMinimumHeight(34)
            selected = getattr(self, f"_butterfly_{side}_values")
            text = ", ".join(labels[group_value_key(value)] for value in selected) or "값 선택"
            button.setText(button.fontMetrics().elidedText(text, Qt.TextElideMode.ElideRight, 330) + "  ▾")
            button.setToolTip(text + "\n클릭하여 여러 값을 선택합니다.")
            button.setAccessibleName(("왼쪽" if side == "left" else "오른쪽") + " 그룹 값 선택: " + text)
            button.clicked.connect(lambda checked=False, s=side, k=group_key, g=groups: self._open_butterfly_group(s, k, g))
        if self._butterfly_mode == "rest":
            self.butterfly_right.setText("나머지 전체")
            self.butterfly_right.setEnabled(False)
        left_label, right_label = label("■ 왼쪽 그룹"), label("■ 오른쪽 그룹")
        left_label.setStyleSheet(f"color:{StatisticsButterflyChart.LEFT_COLOR};")
        right_label.setStyleSheet(f"color:{StatisticsButterflyChart.RIGHT_COLOR};")
        selectors.addWidget(left_label, 0, 0)
        selectors.addWidget(right_label, 0, 1)
        selectors.addWidget(self.butterfly_left, 1, 0)
        selectors.addWidget(self.butterfly_right, 1, 1)
        selectors.setColumnStretch(0, 1)
        selectors.setColumnStretch(1, 1)
        inner.addLayout(selectors)
        for combo in (self.butterfly_mode, self.butterfly_unit):
            combo.currentIndexChanged.connect(self._change_butterfly)
        try:
            report = build_butterfly(
                self.engine, axis_key, group_key, self.filters(), left_values=self._butterfly_left_values,
                right_values=self._butterfly_right_values, mode=self._butterfly_mode,
            )
        except (ValueError, CrossTableLimitError) as exc:
            inner.addWidget(label(str(exc), "warningNote"))
            return
        inner.addWidget(label(
            f"{report.axis_label} · {report.group_label} 비교 · 대상 {report.total:,}건 · 비교에서 제외 {report.excluded:,}건"
        ))
        chart = StatisticsButterflyChart(self)
        chart.set_data(
            [dict(label=row.label, left_count=row.left_count, right_count=row.right_count, missing=row.missing) for row in report.rows],
            left_label=report.left_label, right_label=report.right_label,
            left_total=report.left_total, right_total=report.right_total,
            unit=self._butterfly_unit, axis_label=report.axis_label,
        )
        inner.addWidget(chart)
        readouts = QHBoxLayout()
        selected_frame, selected_content = card("선택한 구간")
        self.butterfly_readout = label("막대나 가운데 구간을 눌러 보세요.")
        self.butterfly_readout.setObjectName("butterflyReadout")
        selected_content.addWidget(self.butterfly_readout)
        gap_frame, gap_content = card("차이가 가장 큰 구간")
        for readout_frame in (selected_frame, gap_frame):
            readout_frame.setStyleSheet("QFrame#statCard { background:#F6F7F9; border:1px solid #DFE4EB; }")
            readouts.addWidget(readout_frame, 1)
        inner.addLayout(readouts)

        def percentages(row):
            left = row.left_count * 100 / report.left_total if report.left_total else 0.0
            right = row.right_count * 100 / report.right_total if report.right_total else 0.0
            return left, right

        def choose_bucket(index: int, side: int) -> None:
            row = report.rows[index]
            if side == 2:
                ids = set(row.left_uids) | set(row.right_uids)
                uids = tuple(case.uid for case in self.engine.filtered_cases(self.filters()) if case.uid in ids)
            else:
                uids = row.left_uids if side == 0 else row.right_uids
            chart.set_selection(index, side)
            group_label = (report.left_label, report.right_label, "좌우 전체")[side]
            if side == 0:
                group_condition = StatsCondition(group_key, values=self._butterfly_left_values)
            elif side == 1 and self._butterfly_mode == "rest":
                group_condition = StatsCondition(group_key, values=(None, *self._butterfly_left_values), exclude=True)
            elif side == 1:
                group_condition = StatsCondition(group_key, values=self._butterfly_right_values)
            elif self._butterfly_mode == "rest":
                group_condition = StatsCondition(group_key, values=(None,), exclude=True)
            else:
                group_condition = StatsCondition(group_key, values=normalize_group_values(
                    (*self._butterfly_left_values, *self._butterfly_right_values)))
            conditions = (row.condition, group_condition) if row.condition is not None else ()
            # Invalid group answers are deliberately excluded from cohorts. Pin
            # only if the predicates reproduce the exact displayed accidents.
            if conditions:
                matched = {case.uid for case in self.engine.filtered_cases(self.filters())
                           if all(self.engine.matches(case, condition) for condition in conditions)}
                if matched != set(uids):
                    conditions = ()
            self.select_cases(uids, conditions, f"{report.axis_label} · {row.label} · {group_label}")
            left, right = percentages(row)
            difference = f" · 비율 차이 {left - right:+.1f}%p" if report.left_total and report.right_total else ""
            self.butterfly_readout.setText(
                f"{row.label} · {report.left_label} {row.left_count:,}건 ({left:.1f}%) / "
                f"{report.right_label} {row.right_count:,}건 ({right:.1f}%)" + difference
            )

        chart.bucket_clicked.connect(choose_bucket)
        if report.left_total and report.right_total and report.rows:
            best = max(report.rows, key=lambda row: abs(percentages(row)[0] - percentages(row)[1]))
            left, right = percentages(best)
            gap_content.addWidget(label(
                f"{best.label} · {report.left_label} {left:.1f}% / "
                f"{report.right_label} {right:.1f}% · 차이 {left - right:+.1f}%p", "statReadingMain"
            ))
        elif not report.left_total or not report.right_total:
            gap_content.addWidget(label("한쪽 그룹의 사고가 0건이어서 비율 차이를 비교할 수 없습니다.", "warningNote"))
        inner.addWidget(label("막대에 마우스를 올리면 건수·비율·분모가 표시됩니다. 막대를 누르면 해당 사고가 오른쪽 목록에 표시됩니다."))
        inner.addWidget(label("비율의 분모는 각 그룹의 전체 사고 수입니다. 좌우는 같은 눈금을 사용합니다. 그룹 미입력·형식 오류·비해당은 비교에서 제외합니다."))
        for warning in report.warnings:
            inner.addWidget(label(warning, "warningNote"))
        self._export_headers = [
            "가운데 축", "구간", "그룹 항목", "왼쪽 그룹", "왼쪽 건수", "왼쪽 분모", "왼쪽 비율(%)",
            "오른쪽 그룹", "오른쪽 건수", "오른쪽 분모", "오른쪽 비율(%)", "비율 차이(%p)",
            "비교 방식", "표시 단위", "대상 사고 수", "제외 사고 수", "좌우 중복 사고 수",
        ]
        self._export_rows = []
        for row in report.rows:
            left, right = percentages(row)
            self._export_rows.append([
                report.axis_label, row.label, report.group_label, report.left_label, row.left_count,
                report.left_total, round(left, 2), report.right_label, row.right_count, report.right_total,
                round(right, 2), round(left - right, 2) if report.left_total and report.right_total else None,
                "두 그룹" if self._butterfly_mode == "two" else "한 그룹 vs 나머지",
                "그룹 내 %" if self._butterfly_unit == "percent" else "건수", report.total, report.excluded, report.overlap,
            ])

    def _change_butterfly(self) -> None:
        self._close_condition_popup()
        self._butterfly_mode = self.butterfly_mode.currentData()
        self._butterfly_unit = self.butterfly_unit.currentData()
        self.clear_selection()
        self.refresh()

    def _open_butterfly_group(self, side: str, key: str, groups: tuple[Category, ...]) -> None:
        self._close_condition_popup()
        other = "right" if side == "left" else "left"
        blocked = getattr(self, f"_butterfly_{other}_values", ()) if self._butterfly_mode == "two" else ()
        popup = StatisticsGroupPopup(
            groups, getattr(self, f"_butterfly_{side}_values", ()),
            title=("왼쪽" if side == "left" else "오른쪽") + " 그룹 · " + clean_field_label(self.engine.field(key).label),
            blocked=blocked, parent=self,
        )
        self._group_popup = popup

        def apply(values: tuple) -> None:
            if self._butterfly_group_key != key:
                return
            setattr(self, f"_butterfly_{side}_values", normalize_group_values(values))
            self.clear_selection()
            self.refresh()

        popup.applied.connect(apply)
        popup.open_at(self.butterfly_left if side == "left" else self.butterfly_right)

    def _cross_categorical(self, inner: QVBoxLayout) -> None:
        try:
            cross = self.engine.cross_table(self._row_key, self._column_key, self.filters())
        except CrossTableLimitError as exc:
            inner.addWidget(label(str(exc), "warningNote"))
            self._export_headers, self._export_rows = [], []
            return
        widget = table([category.label for category in cross.columns] + ["사고 수"])
        widget.setItemDelegate(StatisticsHeatmapDelegate(widget))
        widget.setRowCount(len(cross.rows))
        widget.setVerticalHeaderLabels([category.label for category in cross.rows])
        widget.verticalHeader().show()
        widget.setAlternatingRowColors(False)
        widget.horizontalHeader().setMinimumSectionSize(110)
        widget.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.Stretch if len(cross.columns) < 8 else QHeaderView.ResizeMode.ResizeToContents
        )
        widget.setFixedHeight(max(220, min(580, len(cross.rows) * 64 + 50)))
        self._export_headers = [cross.row_label, cross.column_label, "건수", "행 기준 비율(%)", "행 사고 수"]
        self._export_rows = []
        small = 0
        for i, categories in enumerate(cross.cells):
            denominator = cross.rows[i].count
            for j, category in enumerate(categories):
                pct = category.percent(denominator)
                text = f"{pct:.1f}%\n{category.count}건" if getattr(self, "_cross_mode", "row") == "row" else f"{category.count}건\n{pct:.1f}%"
                item = QTableWidgetItem(text)
                color = QColor(PRIMARY)
                color.setAlphaF(0.06 + min(1, pct / 100) * 0.6)
                item.setBackground(color)
                item.setToolTip(f"{cross.rows[i].label} × {cross.columns[j].label} · {category.count}건" + (" · 5건 미만" if category.count < 5 else ""))
                item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                widget.setItem(i, j, item)
                small += category.count < 5
                self._export_rows.append([cross.rows[i].label, cross.columns[j].label, category.count, round(pct, 2), denominator])
            widget.setItem(i, len(cross.columns), QTableWidgetItem(str(denominator)))
            widget.setRowHeight(i, 64)

        def choose_cell(i: int, j: int) -> None:
            if j >= len(cross.columns):
                self.select_category(cross.rows[i], cross.row_label)
                return
            category = cross.cells[i][j]
            conditions = tuple(c for c in (cross.rows[i].condition, cross.columns[j].condition) if c)
            self.select_cases(category.case_uids, conditions, category.label)

        widget.cellClicked.connect(choose_cell)
        if cross.column_multi:
            stack_button = self.cross_view._buttons.button(1)
            stack_button.setEnabled(False)
            stack_button.setToolTip("열에 다중 선택 항목이 있어 사고 비율 합계가 100%를 넘을 수 있습니다. 히트맵으로 확인해 주세요.")
            self._cross_view = "heat"
            self.cross_view.blockSignals(True)
            self.cross_view.setCurrentIndex(0)
            self.cross_view.blockSignals(False)
            self.cross_mode.show()
        if self._cross_view == "stack":
            widget.hide()
            widget.deleteLater()
            chart = StatisticsStackedChart(self)
            chart.set_data(
                [dict(label=r.label, counts=[cell.count for cell in cross.cells[i]], total=r.count) for i, r in enumerate(cross.rows)],
                [c.label for c in cross.columns], ordered=self.engine.is_ordered(cross.column_key),
            )
            chart.cell_clicked.connect(choose_cell)
            inner.addWidget(chart)
            inner.addWidget(label("각 행을 100%로 맞춰 비교합니다. 막대를 누르면 해당 사고를 확인할 수 있습니다."))
        else:
            inner.addWidget(widget)
        inner.addWidget(label(f"대상 {cross.denominator:,}건 · {small}칸이 5건 미만입니다. 비율과 실제 건수를 함께 확인하세요.", "warningNote"))
        if cross.column_multi or cross.row_multi:
            inner.addWidget(label("다중 선택 항목을 포함합니다. 한 사고가 여러 칸에 집계될 수 있어 합계·행 비율의 합이 100%를 넘을 수 있습니다."))
        best = None
        for i, row in enumerate(cross.rows):
            if row.count < 5 or row.missing:
                continue
            for j, column in enumerate(cross.columns):
                if column.missing:
                    continue
                share = cross.cells[i][j].percent(row.count)
                overall = column.percent(cross.denominator)
                if best is None or share - overall > best[0]:
                    best = (share - overall, row.label, column.label, share, overall)
        main = (
            f"{best[1]}에서 {best[2]} 비율은 {best[3]:.1f}%입니다. 전체 대상에서는 {best[4]:.1f}%입니다."
            if best else "비율을 비교할 만큼 건수가 있는 행이 없습니다. 실제 건수를 함께 확인해 주세요."
        )
        test = self.engine.association(self._row_key, self._column_key, self.filters())
        self._analysis_reading(inner, main, test)
        self._append_test_export(test)

    def _cross_numeric_groups(self, inner: QVBoxLayout) -> None:
        category_key, numeric_key = (
            (self._column_key, self._row_key) if self.engine.is_numeric(self._row_key)
            else (self._row_key, self._column_key)
        )
        try:
            report = self.engine.numeric_groups(category_key, numeric_key, self.filters())
            if len(report.groups) > 100:
                raise CrossTableLimitError(len(report.groups), 1)
        except CrossTableLimitError as exc:
            inner.addWidget(label(str(exc), "warningNote"))
            return
        inner.addWidget(label(f"{report.category_label}별 {report.numeric_label} 분포", "statTitle"))
        inner.addWidget(label(f"대상 {report.total:,}건 · 유효 {report.valid_count:,}건 · 미입력·오류·해당 없음 제외 {report.excluded_count:,}건"))
        chart = BoxPlotWidget(self)
        chart.set_data([
            dict(label=g.label, min=g.min, q1=g.q1, median=g.median, q3=g.q3, max=g.max, n=g.n, values=list(g.values))
            for g in report.groups if g.n
        ], unit=report.unit, axis_label=report.numeric_label, allow_negative=True)
        groups = [group for group in report.groups if group.n]

        def select_group(index: int) -> None:
            group = groups[index]
            chart.set_selection(index)
            # A category condition alone would re-include accidents whose numeric
            # value is missing/invalid. Match the mockup: this drill is view-only.
            self.select_cases(group.case_uids, (), f"{report.category_label} · {group.label}")

        chart.group_clicked.connect(select_group)
        inner.addWidget(chart)
        inner.addWidget(label("상자: 가운데 50% · 굵은 선: 중앙값 · 가는 선: 최솟값~최댓값 · 점: 사고 하나. 5건 미만 그룹은 점선으로 표시합니다."))
        if report.multi:
            inner.addWidget(label("다중 선택은 같은 사고가 여러 그룹에 포함될 수 있습니다. 유효 건수는 중복을 제외한 사고 수입니다."))
        comparable = [group for group in groups if group.n >= 5]
        if len(comparable) >= 2:
            high, low = max(comparable, key=lambda g: g.median), min(comparable, key=lambda g: g.median)
            main = f"{report.numeric_label} 중앙값은 {high.label}에서 {high.median:,.2f}{report.unit}, {low.label}에서 {low.median:,.2f}{report.unit}입니다."
        else:
            main = "5건 이상인 그룹이 2개 미만입니다. 개별 사고와 실제 건수를 확인해 주세요."
        self._analysis_reading(inner, main, report.test)
        self._export_headers = [report.category_label, "건수", "최소", "1사분위", "중앙값", "3사분위", "최대", "수치 항목", "단위", "유효 사고 수", "제외 사고 수"]
        self._export_rows = [
            [g.label, g.n, g.min, g.q1, g.median, g.q3, g.max, report.numeric_label, report.unit, report.valid_count, report.excluded_count]
            for g in report.groups
        ]
        self._append_test_export(report.test)

    def _cross_scatter(self, inner: QVBoxLayout) -> None:
        report = self.engine.scatter(self._row_key, self._column_key, self.filters())
        inner.addWidget(label(f"대상 {report.total:,}건 · 유효 수치 쌍 {report.n:,}건 · 미입력·오류·해당 없음 제외 {report.excluded:,}건"))
        x_label = f"{report.x_label} ({report.x_unit})" if report.x_unit else report.x_label
        y_label = f"{report.y_label} ({report.y_unit})" if report.y_unit else report.y_label
        chart = StatisticsScatterPlot(self)
        chart.set_data(
            [dict(x=p.x, y=p.y, uid=p.case_uid, label=p.label) for p in report.points],
            x_label=x_label, y_label=y_label, slope=report.slope, intercept=report.intercept,
        )

        def select_point(uid: str) -> None:
            chart.set_selection(uid)
            self.select_cases((uid,), (), "선택한 점 · " + next(p.label for p in report.points if p.case_uid == uid))

        chart.point_clicked.connect(select_point)
        inner.addWidget(chart)
        inner.addWidget(label("● 사고 하나 · 주황 점선: 최소제곱 추세선. 점을 누르면 해당 사고를 확인할 수 있습니다."))
        if report.r is None:
            main = "두 수치의 직선 관계를 계산할 수 없습니다. 유효 건수와 값의 변화를 확인해 주세요."
        elif abs(report.r) < 0.1:
            main = f"뚜렷한 직선 관계가 보이지 않습니다 (r = {report.r:.2f})."
        else:
            direction = "증가" if report.r > 0 else "감소"
            main = f"{report.x_label} 값이 클수록 {report.y_label} 값이 {direction}하는 경향이 관측됩니다 (r = {report.r:.2f})."
        slope_text = (
            f"추세선 기울기 {report.slope:,.4g} {report.y_unit or '값'} / {report.x_unit or '값'}"
            if report.slope is not None else ""
        )
        self._analysis_reading(inner, main, report.test, extra="상관은 원인과 결과를 뜻하지 않습니다. 다른 조건과 개별 사고를 함께 확인해 주세요.", details=slope_text)
        self._export_headers = ["사고 UID", "사고번호", x_label, y_label, "기울기", "절편", "피어슨 r", "유효 쌍", "제외 사고 수"]
        self._export_rows = [[p.case_uid, p.label, p.x, p.y, report.slope, report.intercept, report.r, report.n, report.excluded] for p in report.points]
        self._append_test_export(report.test)

    @staticmethod
    def _format_p(value: float) -> str:
        return "< 0.001" if value < 0.001 else f"= {value:.3f}"

    def _analysis_reading(self, inner: QVBoxLayout, main: str, test, *, extra: str = "", details: str = "") -> None:
        frame, content = card("읽는 법")
        frame.setStyleSheet("QFrame#statCard { background:#F6F7F9; border:0; }")
        content.addWidget(label(main, "statReadingMain"))
        if test.p_value is not None:
            method = {"chi_square": "카이제곱 검정", "kruskal_wallis": "크러스컬–월리스 검정", "pearson": "피어슨 상관"}.get(test.method, test.method)
            text = f"{method} p {self._format_p(test.p_value)} · n={test.n:,}"
            if test.method == "chi_square" and test.effect_size is not None:
                text += f" · 크라메르 V = {test.effect_size:.2f}"
            if details:
                text += " · " + details
            if test.warnings:
                text = "참고용 · " + text
            content.addWidget(label(text))
        notes = [test.reason, *test.warnings, extra]
        for note in notes:
            if note:
                content.addWidget(label(note))
        if not extra:
            content.addWidget(label("관측된 자료에 대한 참고용 분석이며 인과관계를 판정하지 않습니다."))
        inner.addWidget(frame)

    def _append_test_export(self, test) -> None:
        self._export_headers += ["검정", "검정 표본 수", "검정 통계량", "p값", "효과 크기", "검정 안내"]
        values = [test.method, test.n, test.statistic, test.p_value, test.effect_size, " | ".join(filter(None, (test.reason, *test.warnings)))]
        for row in self._export_rows:
            row.extend(values)

    def _change_cross(self) -> None:
        self._row_key, self._column_key = self.cross_row.currentData(), self.cross_column.currentData()
        self._cross_mode = self.cross_mode.currentData()
        self._cross_view = self.cross_view.currentData()
        self.clear_selection()
        self.refresh()

    def _swap_cross(self) -> None:
        self._row_key, self._column_key = self.cross_column.currentData(), self.cross_row.currentData()
        self.clear_selection()
        self.refresh()

    def _video(self) -> None:
        layout = self.page_layouts[3]
        report = self.engine.video(self.filters())
        self._kpis(layout, [
            ("T·C를 모두 지정한 영상", f"{report.with_both:,} / {report.total_videos:,}개", "모든 연결 영상 기준", None),
            ("평균 트리거 → 충돌", self._seconds(report.intervals.summary.get("mean")), f"중앙값 {self._seconds(report.median_seconds)} · 유효 {report.valid_intervals:,}건", None),
            ("영상이 여러 개인 사고", f"{sum(len(case.videos) > 1 for case in self.engine.filtered_cases(self.filters())):,}건", "시간 통계는 현재 선택 영상 기준", None),
            ("마커가 빠진 영상", f"{report.total_videos - report.with_both:,}개", "데이터 품질에서 확인 →", 4),
        ])
        toolbar = QHBoxLayout()
        toolbar.addWidget(label(f"FPS 미확인 {report.missing_fps:,}건 · 역순 마커 {report.invalid_order:,}건 · 선택 영상 없음 {report.no_active_video:,}건"), 1)
        self.media_button = QPushButton("영상 정보 확인 / 새로고침", self)
        self.media_button.clicked.connect(self.media_requested)
        toolbar.addWidget(self.media_button)
        layout.addLayout(toolbar)
        frame, inner = card("트리거 → 충돌 시간 분포")
        inner.addWidget(self._chart(report.intervals))
        inner.addWidget(label("각 사고의 현재 선택 영상만 사용합니다. FPS를 확인할 수 없거나 T·C가 없거나 역순이면 시간 통계에서 제외합니다."))
        charts_layout = QHBoxLayout()
        charts_layout.addWidget(frame, 1)
        boxes, box_layout = card("항목별 트리거 → 충돌 시간")
        self.video_group = self._field_picker(getattr(self, "_video_group", "collision_targets"))
        self._video_group = self.video_group.currentData()
        self.video_group.currentIndexChanged.connect(self._change_video_group)
        box_layout.addWidget(self.video_group)
        groups = self.engine.distribution(self._video_group, self.filters())
        cases = {case.uid: case for case in self.engine.filtered_cases(self.filters())}
        rows = []
        for category in groups.buckets:
            values = [self.engine.case_value(cases[uid], INTERVAL_KEY) for uid in category.case_uids]
            values = sorted(v for v in values if isinstance(v, (int, float)) and math.isfinite(v))
            if not values:
                continue
            q1, med, q3 = quantiles(values, n=4, method="inclusive") if len(values) > 1 else (values[0],) * 3
            rows.append(dict(label=category.label, min=values[0], q1=q1, median=med, q3=q3, max=values[-1], n=len(values), values=values))
        chart = BoxPlotWidget(self)
        chart.set_data(rows)
        box_layout.addWidget(chart)
        box_layout.addWidget(label("상자는 가운데 50%, 선은 최솟값~최댓값입니다. 다중 선택이면 같은 사고가 여러 그룹에 포함될 수 있습니다."))
        charts_layout.addWidget(boxes, 1)
        layout.addLayout(charts_layout)
        self._export_headers = ["시간 구간(초)", "건수", "비율(%)", "분모"]
        self._export_rows = [[b.label, b.count, round(b.percent(report.intervals.denominator), 2), report.intervals.denominator] for b in report.intervals.buckets]

    def _change_video_group(self) -> None:
        self._video_group = self.video_group.currentData()
        self.refresh()

    def _quality(self) -> None:
        layout = self.page_layouts[4]
        report = self.engine.quality(self.filters())
        layout.addWidget(label(f"상태 필터를 제외한 {report.total:,}건 기준입니다. 기간·추가 조건은 적용하며 선택 항목은 필수 미입력 집계에서 제외합니다."))
        self._kpis(layout, [
            ("완료율", f"{report.completion_rate:.1f}%", f"완료 {report.completed} / {report.total}건", None),
            ("필수 항목 입력률", f"{report.required_answer_rate:.1f}%", "조건부 필수 항목의 해당 사고 기준", None),
            ("미입력 사고", f"{report.missing_cases:,}건", "선택 항목 제외", None),
            ("형식·마커 오류", f"{report.invalid_cases:,}건", "형식 및 마커 순서 확인", None),
        ])
        frame, inner = card("항목별 입력률 · 낮은 순")
        rates_grid = QGridLayout()
        rates_grid.setHorizontalSpacing(10)
        rates_grid.setVerticalSpacing(12)
        missing = {bucket.value: bucket for bucket in report.field_missing}
        for index, rate in enumerate(sorted(report.field_rates, key=lambda r: (r.rate, r.label))):
            caption = QLabel(rate.label, self)
            caption.setWordWrap(True)
            caption.setMinimumWidth(0)
            caption.setToolTip(f"해당 {rate.eligible:,}건 · 입력 {rate.answered:,}건 · 미입력 {rate.missing:,}건 · 형식 오류 {rate.invalid:,}건")
            progress = QProgressBar(self)
            progress.setRange(0, 1000)
            progress.setValue(round(rate.rate * 10))
            progress.setTextVisible(False)
            progress.setFixedHeight(12)
            color = "#D9822B" if rate.eligible and rate.rate < 80 else "#8CAEE3"
            progress.setStyleSheet("QProgressBar { border:0; border-radius:3px; background:#F1F3F6; } QProgressBar::chunk { background:" + color + "; border-radius:3px; }")
            progress.setAccessibleName(f"{rate.label} 입력률 {rate.rate:.1f}% · 해당 {rate.eligible}건")
            amount = label(f"{rate.rate:.0f}%" if rate.eligible else "해당 없음")
            if rate.eligible and rate.rate < 80:
                amount.setStyleSheet("color:#8A5300;")
            amount.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Preferred)
            rates_grid.addWidget(caption, index * 2, 0, 2, 1)
            rates_grid.addWidget(progress, index * 2, 1)
            rates_grid.addWidget(amount, index * 2, 2)
            hint = QLabel(f"해당 {rate.eligible:,}건 · 미입력 {rate.missing:,}건 · 오류 {rate.invalid:,}건", self)
            hint.setObjectName("secondaryText")
            hint.setWordWrap(True)
            rates_grid.addWidget(hint, index * 2 + 1, 1, 1, 2)
            if bucket := missing.get(rate.field_key):
                caption.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
                caption.setToolTip(caption.toolTip() + f"\n미입력 사고 {bucket.count:,}건")
        rates_grid.setColumnStretch(0, 2)
        rates_grid.setColumnStretch(1, 3)
        inner.addLayout(rates_grid)
        issues_frame, issues_layout = card("미입력·오류가 있는 사고 · 조사 열기")
        issues = table(["사고번호", "상태", "미입력 / 오류", "첫 확인 항목"])
        issues.setRowCount(len(report.issues))
        for i, issue in enumerate(report.issues):
            key = next(iter(issue.missing or issue.errors), "")
            values = (issue.display_id, "완료" if issue.status == "completed" else "임시저장",
                      f"{len(issue.missing)} / {len(issue.errors)}", self.engine.definition.label(key))
            for j, text in enumerate(values):
                issues.setItem(i, j, QTableWidgetItem(text))
        issues.cellClicked.connect(lambda i, j: self.case_activated.emit(report.issues[i].case_uid, next(iter(report.issues[i].missing or report.issues[i].errors), "")))
        issues_layout.addWidget(issues)
        columns = QHBoxLayout()
        columns.setAlignment(Qt.AlignmentFlag.AlignTop)
        columns.addWidget(frame, 1)
        columns.addWidget(issues_frame, 1)
        layout.addLayout(columns)
        self._export_headers = ["사고번호", "상태", "미입력 수", "오류 수", "미입력 항목", "오류 항목"]
        self._export_rows = [[issue.display_id, "완료" if issue.status == "completed" else "임시저장", len(issue.missing), len(issue.errors),
                              " | ".join(self.engine.definition.label(k) for k in issue.missing),
                              " | ".join(self.engine.definition.label(k) for k in issue.errors)] for issue in report.issues]

    def select_category(self, category: Category, title: str, chart: QWidget | None = None,
                        index: int | None = None) -> None:
        if chart is not None:
            chart.set_selection(index)
        self.select_cases(category.case_uids, (category.condition,) if category.condition else (), f"{title} · {category.label}")

    def select_cases(self, uids: tuple[str, ...], conditions: tuple[StatsCondition, ...], title: str) -> None:
        self._selection = (uids, conditions, title)
        self._render_drill()

    def clear_selection(self) -> None:
        self._selection = None
        if hasattr(self, "pages") and (page := self.pages.currentWidget()):
            for chart_type in (StatisticsBarChart, StatisticsButterflyChart, StatisticsDonutChart, StatisticsStackedChart, StatisticsScatterPlot, BoxPlotWidget):
                for chart in page.findChildren(chart_type):
                    chart.set_selection(None)
            for widget in page.findChildren(QTableWidget):
                widget.clearSelection()
            for widget in page.findChildren(QLabel, "butterflyReadout"):
                widget.setText("막대나 가운데 구간을 눌러 보세요.")
        if hasattr(self, "drill_list"):
            self._render_drill()

    def _render_drill(self, *, all_rows: bool = False) -> None:
        self.drill.setVisible(self.view_tabs.currentIndex() != 4 or self._selection is not None)
        self.drill_list.clear()
        self.show_all_button.hide()
        self.pin_condition_button.setEnabled(False)
        if self._selection is None:
            self.drill_title.setText("막대나 칸을 눌러 보세요")
            self.drill_count.setText("해당 사고를 선택하면 조사 화면으로 이동합니다.")
            return
        uids, conditions, title = self._selection
        self.drill_title.setText(title)
        self.drill_count.setText(f"해당 사고 {len(uids):,}건")
        by_uid = {case.uid: case for case in self.engine.cases}
        for uid in uids if all_rows else uids[:50]:
            if case := by_uid.get(uid):
                item = QTreeWidgetItem(self.drill_list, [case.display_id, "조사 →"])
                item.setData(0, Qt.ItemDataRole.UserRole, uid)
                item.setToolTip(0, case.display_id)
        self.show_all_button.setVisible(not all_rows and len(uids) > 50)
        self.show_all_button.setText(f"전체 {len(uids):,}건 보기")
        self.pin_condition_button.setEnabled(bool(conditions))

    def _open_drill_case(self, item: QTreeWidgetItem) -> None:
        self.case_activated.emit(item.data(0, Qt.ItemDataRole.UserRole), "")

    def pin_selection(self) -> None:
        if self._selection:
            for condition in self._selection[1]:
                if condition not in self.conditions:
                    self.conditions.append(condition)
            self._conditions_edited()

    def export_dialog(self, kind: str) -> None:
        path, _ = QFileDialog.getSaveFileName(self, "통계 내보내기", str(self.workspace / f"statistics.{kind}"), f"{kind.upper()} (*.{kind})")
        if not path:
            return
        destination = Path(path)
        if destination.suffix.lower() != f".{kind}":
            destination = destination.with_name(destination.name + f".{kind}")
        try:
            self.export_to(destination, kind)
        except (OSError, ValueError) as exc:
            QMessageBox.warning(self, "통계 내보내기 오류", str(exc))

    def export_to(self, path: Path, kind: str) -> Path:
        from investigator.statistics_export import (
            export_overview_pdf,
            export_statistics_csv,
            export_statistics_png,
        )
        title = f"AVAT · {self.workspace.name} · {self.view_tabs.tabText(self.view_tabs.currentIndex())}"
        filters = self.filter_description()
        if kind == "csv":
            return export_statistics_csv(path, self._export_headers, self._export_rows, title=title, filters=filters)
        if kind == "png":
            return export_statistics_png(path, self._export_widget, title=title, filters=filters,
                                         sample_count=len(self.engine.filtered_cases(self.filters(), ignore_status=self.view_tabs.currentIndex() == 4)))
        if kind == "pdf":
            title = f"AVAT · {self.workspace.name} · 개요 보고서"
            filters = self.filter_description().removesuffix(" · 데이터 품질은 상태 필터를 제외")
            tables = []
            for key in self.pinned_fields:
                if key not in {spec.key for spec in self.engine.fields}:
                    continue
                dist = self.engine.distribution(key, self.filters())
                tables.append((dist.label, ["값", "건수", "비율(%)", "분모"],
                               [[b.label, b.count, round(b.percent(dist.denominator), 2), dist.denominator] for b in dist.buckets]))
            return export_overview_pdf(path, title=title, filters=filters,
                                       summary=self.engine.overview(self.filters()), tables=tables)
        raise ValueError("지원하지 않는 통계 내보내기 형식입니다.")
