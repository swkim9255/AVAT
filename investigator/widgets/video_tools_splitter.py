"""Resizable video/tool workspace with independent remembered tool heights."""

from __future__ import annotations

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import QSplitter, QWidget


class VideoToolsSplitter(QSplitter):
    """Keep playback attached to video while resizing the optional tools below.

    The first child contains both the video and playback controls. Tool panels
    stay separate children so existing visibility checks and panel ownership
    remain valid. Sizes are remembered per tool, not shared between tools.
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(Qt.Orientation.Vertical, parent)
        self.setObjectName("videoToolsSplitter")
        self.setChildrenCollapsible(False)
        self.setHandleWidth(8)
        self.setOpaqueResize(True)
        self._tools: dict[str, QWidget] = {}
        self._tool_heights: dict[str, int] = {}
        self._restore_pending = False
        self.splitterMoved.connect(self._remember_visible_sizes)

    def add_video_area(self, widget: QWidget) -> None:
        if self.count():
            raise ValueError("Video area must be the first splitter child")
        self.addWidget(widget)
        self.setStretchFactor(0, 1)

    def add_tool_panel(self, key: str, widget: QWidget, *, default_height: int) -> None:
        if not self.count() or key in self._tools or default_height <= 0:
            raise ValueError("Add video first and use a unique tool key with positive height")
        self.addWidget(widget)
        self.setStretchFactor(self.indexOf(widget), 1)
        self._tools[key] = widget
        self._tool_heights[key] = default_height
        handle = self.handle(self.indexOf(widget))
        handle.setCursor(Qt.CursorShape.SplitVCursor)
        handle.setAccessibleName("영상 / 도구 패널 높이 조절")
        handle.setToolTip("드래그하여 영상과 도구 패널의 높이를 조절합니다.")

    def set_tool_visible(self, key: str, visible: bool) -> None:
        widget = self._tools[key]
        if visible == (not widget.isHidden()):
            return
        if not visible:
            self._remember_visible_sizes()
        widget.setVisible(visible)
        # Let the main window update the canvas's compact minimum before
        # restoring. This also handles opening a panel before first show.
        if visible and not self._restore_pending:
            self._restore_pending = True
            QTimer.singleShot(0, self._restore_visible_sizes)

    def _remember_visible_sizes(self, *_args) -> None:
        sizes = self.sizes()
        for key, widget in self._tools.items():
            index = self.indexOf(widget)
            if not widget.isHidden() and sizes[index] > 0:
                self._tool_heights[key] = sizes[index]

    def _restore_visible_sizes(self) -> None:
        self._restore_pending = False
        visible = [(key, widget) for key, widget in self._tools.items() if not widget.isHidden()]
        if not visible:
            return
        sizes = [0] * self.count()
        for key, widget in visible:
            sizes[self.indexOf(widget)] = self._tool_heights[key]
        # QSS borders can make a handle taller than handleWidth(). Using its
        # laid-out height prevents a few-pixel shrink on every reopen cycle.
        handle_space = sum(self.handle(self.indexOf(widget)).height() for _, widget in visible)
        available = max(1, self.height() - handle_space)
        sizes[0] = max(1, available - sum(sizes))
        # QSplitter clamps impossible sizes to each child's real minimum. No
        # fixed maximum prevents tools from using a large window's free area.
        self.setSizes(sizes)


__all__ = ["VideoToolsSplitter"]
