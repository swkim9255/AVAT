"""Dropdowns that let closed-state wheel input scroll the surrounding form."""

from PySide6.QtGui import QWheelEvent
from PySide6.QtWidgets import QComboBox


class ScrollSafeComboBox(QComboBox):
    def wheelEvent(self, event: QWheelEvent) -> None:
        if self.view().isVisible():
            super().wheelEvent(event)
        else:
            # Ignoring (not consuming) the event lets Qt propagate it to the
            # ancestor scroll area, including when this control has focus.
            event.ignore()
