# ui/collapsible.py
#
# CollapsibleSection — a header toggle (▸/▾) over a content area that shows or
# hides on click. Generic, layout-only; no DB or app knowledge. Used by the
# gallery to tuck the filter builder away until it's needed.

from PySide6.QtWidgets import QWidget, QVBoxLayout, QToolButton, QSizePolicy
from PySide6.QtCore import Qt, Signal


class CollapsibleSection(QWidget):
    """A titled section whose body can be collapsed to just its header.

    Add body widgets to `self.body` (or its layout via `add_widget`). The header
    button toggles visibility; `toggled(bool)` fires with the new expanded
    state so a host can, for example, save it in a layout.
    """

    toggled = Signal(bool)

    def __init__(self, title: str, parent=None, expanded: bool = False):
        super().__init__(parent)
        self._title = title

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(2)

        self._header = QToolButton()
        self._header.setObjectName("collapsibleHeader")
        self._header.setCheckable(True)
        self._header.setChecked(expanded)
        self._header.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self._header.setArrowType(Qt.ArrowType.DownArrow if expanded else Qt.ArrowType.RightArrow)
        self._header.setText(title)
        self._header.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._header.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self._header.setCursor(Qt.CursorShape.PointingHandCursor)
        self._header.clicked.connect(self._on_clicked)
        outer.addWidget(self._header)

        self.body = QWidget()
        self._body_layout = QVBoxLayout(self.body)
        self._body_layout.setContentsMargins(0, 0, 0, 0)
        self._body_layout.setSpacing(4)
        self.body.setVisible(expanded)
        outer.addWidget(self.body)

    # -- public -------------------------------------------------------------

    def add_widget(self, widget: QWidget):
        self._body_layout.addWidget(widget)

    def add_layout(self, layout):
        self._body_layout.addLayout(layout)

    def set_expanded(self, expanded: bool):
        if expanded == self._header.isChecked():
            # Keep the arrow/body in sync even when the check state already
            # matches (e.g. programmatic restore before first show).
            self._sync(expanded)
            return
        self._header.setChecked(expanded)
        self._sync(expanded)

    def is_expanded(self) -> bool:
        return self._header.isChecked()

    # -- internal -----------------------------------------------------------

    def _on_clicked(self):
        expanded = self._header.isChecked()
        self._sync(expanded)
        self.toggled.emit(expanded)

    def _sync(self, expanded: bool):
        self._header.setArrowType(
            Qt.ArrowType.DownArrow if expanded else Qt.ArrowType.RightArrow
        )
        self.body.setVisible(expanded)
