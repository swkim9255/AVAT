"""Single-row menus, document tabs and investigation/statistics mode switch."""

from PySide6.QtCore import QPointF, QRect, QSize, Qt, Signal
from PySide6.QtGui import (
    QColor,
    QFontMetrics,
    QIcon,
    QMouseEvent,
    QPainter,
    QPen,
    QResizeEvent,
    QShowEvent,
)
from PySide6.QtWidgets import (
    QApplication,
    QButtonGroup,
    QFrame,
    QHBoxLayout,
    QMenuBar,
    QPushButton,
    QSizePolicy,
    QStyle,
    QStyleOptionTab,
    QStylePainter,
    QTabBar,
    QToolButton,
    QWidget,
)

from investigator.theme import MUTED, PRIMARY, TEXT


class ProjectTabBar(QTabBar):
    """Use one geometry for folder icons, labels and inset close buttons."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._reorder_press: QPointF | None = None
        self._painting_drag = False

    def mousePressEvent(self, event: QMouseEvent) -> None:
        self._reorder_press = None
        self._painting_drag = False
        if event.button() == Qt.MouseButton.LeftButton and self.isMovable() and self.tabAt(event.position().toPoint()) >= 0:
            self._reorder_press = event.position()
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if (self._reorder_press is not None and event.buttons() & Qt.MouseButton.LeftButton
                and (event.position() - self._reorder_press).manhattanLength() >= QApplication.startDragDistance()):
            self._painting_drag = True
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        super().mouseReleaseEvent(event)
        if event.button() == Qt.MouseButton.LeftButton:
            self._reorder_press = None
            self._painting_drag = False
            self.update()

    def hideEvent(self, event) -> None:  # type: ignore[no-untyped-def]
        self._reorder_press = None
        self._painting_drag = False
        super().hideEvent(event)

    def _item_rects(self, index: int) -> tuple[QRect, QRect, QRect]:
        rect = self.tabRect(index)
        # The final 6 px belong to the gap between document tabs, not the tab.
        content = rect.adjusted(12, 0, -18, 0)
        icon = QRect(content.left(), rect.top() + (rect.height() - 18) // 2, 18, 18)
        close = QRect(content.right() - 19, rect.top() + (rect.height() - 20) // 2, 20, 20)
        text = QRect(icon.right() + 11, rect.top(), max(0, close.left() - icon.right() - 19), rect.height())
        return icon, text, close

    def paintEvent(self, event) -> None:  # type: ignore[no-untyped-def]
        if self._painting_drag:
            # Native painting honors the moving-tab overlay and slide offsets.
            # Painting fixed tabRect()s here would duplicate/hide folder labels.
            super().paintEvent(event)
            return
        del event
        painter = QStylePainter(self)
        painter.setRenderHint(QPainter.RenderHint.TextAntialiasing)
        for index in range(self.count()):
            if not self.isTabVisible(index):
                continue
            option = QStyleOptionTab()
            self.initStyleOption(option, index)
            painter.drawControl(QStyle.ControlElement.CE_TabBarTabShape, option)
            icon_rect, text_rect, close_rect = self._item_rects(index)
            if button := self.tabButton(index, QTabBar.ButtonPosition.RightSide):
                button.setGeometry(close_rect)
            enabled = self.isEnabled() and self.isTabEnabled(index)
            self.tabIcon(index).paint(painter, icon_rect, Qt.AlignmentFlag.AlignCenter,
                                     QIcon.Mode.Normal if enabled else QIcon.Mode.Disabled)
            font = self.font()
            font.setBold(index == self.currentIndex())
            painter.setFont(font)
            painter.setPen(QColor(TEXT if enabled and index == self.currentIndex() else MUTED))
            text = QFontMetrics(font).elidedText(self.tabText(index), self.elideMode(), text_rect.width())
            painter.drawText(text_rect, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, text)
            if self.hasFocus() and index == self.currentIndex():
                painter.setPen(QPen(QColor(PRIMARY), 1, Qt.PenStyle.DashLine))
                painter.setBrush(Qt.BrushStyle.NoBrush)
                painter.drawRoundedRect(option.rect.adjusted(2, 2, -8, -2), 4, 4)


class ViewModeSwitch(QFrame):
    """Exclusive segment buttons with the index API used by view routing."""

    currentChanged = Signal(int)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("viewModeSwitch")
        self.setAccessibleName("보기 모드")
        self._index = 0
        self.group = QButtonGroup(self)
        self.group.setExclusive(True)
        self.buttons: list[QPushButton] = []
        layout = QHBoxLayout(self)
        layout.setContentsMargins(1, 1, 1, 1)
        layout.setSpacing(0)
        for index, text in enumerate(("조사", "통계")):
            button = QPushButton(text, self)
            button.setObjectName("investigationMode" if index == 0 else "statisticsMode")
            button.setCheckable(True)
            button.setAccessibleName(f"{text} 보기")
            button.setFixedSize(68, 32)
            self.group.addButton(button, index)
            self.buttons.append(button)
            layout.addWidget(button)
        self.buttons[0].setChecked(True)
        self.group.idClicked.connect(self.setCurrentIndex)
        self.setFixedSize(138, 34)

    def currentIndex(self) -> int:
        return self._index

    def setCurrentIndex(self, index: int) -> None:
        if index not in (0, 1):
            return
        self.buttons[index].setChecked(True)
        if index != self._index:
            self._index = index
            self.currentChanged.emit(index)

    def tabText(self, index: int) -> str:
        # Retain compatibility with existing index-based view integrations.
        return self.buttons[index].text()


class MenuControls(QWidget):
    def sizeHint(self) -> QSize:
        # QMenuBar positions a corner widget using sizeHint, not fixedWidth.
        return self.minimumSize()


class WorkspaceMenuBar(QMenuBar):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("workspaceMenu")
        self.setNativeMenuBar(False)
        self.setFixedHeight(52)
        self._controls: QWidget | None = None
        self._menu_extent = 0

    def set_controls(self, tabs: QTabBar, add_button: QToolButton, mode: ViewModeSwitch) -> None:
        self._menu_extent = max((self.actionGeometry(action).right() for action in self.actions()), default=0) + 16
        self._controls = MenuControls(self)
        self._controls.setObjectName("workspaceControls")
        self._controls.setFixedHeight(36)
        layout = QHBoxLayout(self._controls)
        layout.setContentsMargins(0, 0, 8, 0)
        layout.setSpacing(8)
        divider = QFrame(self._controls)
        divider.setObjectName("workspaceDivider")
        divider.setFrameShape(QFrame.Shape.VLine)
        divider.setFixedSize(1, 24)
        layout.addWidget(divider)
        tabs.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        tabs.setMinimumWidth(190)
        layout.addWidget(tabs)
        layout.addWidget(add_button)
        layout.addStretch(1)
        layout.addWidget(mode)
        self._resize_controls()
        self.setCornerWidget(self._controls, Qt.Corner.TopRightCorner)

    def _resize_controls(self) -> None:
        if self._controls is not None:
            self._controls.setFixedWidth(max(0, self.width() - self._menu_extent - 12))
            self._controls.updateGeometry()

    def resizeEvent(self, event: QResizeEvent) -> None:
        self._resize_controls()
        super().resizeEvent(event)

    def showEvent(self, event: QShowEvent) -> None:
        self._resize_controls()
        super().showEvent(event)
