# ui/panels/history.py
#
# HistoryPanel — vertical thumbnail strip of previously captured images.
# Clicking a thumbnail emits image_selected(path) for the Canvas to route
# to the Preview panel.
#
# Thumbnails load lazily: on startup every incoming image gets a cheap
# fixed-size placeholder (no decode), and the JPEG is only read + scaled
# once its placeholder scrolls into (or near) the viewport. This keeps
# startup fast even with thousands of images in the incoming folder.
#
# Signals emitted:  image_selected(str path)
# Public methods:   add_thumbnail(path)          — called after a new capture
#                   refresh_thumbnail(path, pixmap) — called after an image is rotated

import os

from PySide6.QtWidgets import QScrollArea, QWidget, QVBoxLayout, QLabel
from PySide6.QtCore import Qt, Signal, QTimer
from PySide6.QtGui import QPixmap

from config import INCOMING_DIR, THUMB_SIZE
from ui.panel import Panel, hide_scrollbars


class HistoryPanel(Panel):

    image_selected = Signal(str)

    def __init__(self, parent=None):
        super().__init__("History", parent)

        self._labels: dict[str, QLabel] = {}
        self._pending: set[str] = set()   # placeholders not yet decoded

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        hide_scrollbars(scroll)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        # The viewport auto-fills its background by default, inheriting an
        # opaque palette colour that hides the panel body behind it — the flat
        # PANEL_BG in the texture skin, or the QSS Panel gradient (the cozy
        # indigo card) in the qss skin. Disable it on the scroll area and its
        # viewport so the panel background shows through the thumbnail strip.
        scroll.setAutoFillBackground(False)
        scroll.viewport().setAutoFillBackground(False)
        self._scroll = scroll

        self._inner = QWidget()
        self._layout = QVBoxLayout(self._inner)
        self._layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        self._layout.setContentsMargins(4, 4, 4, 4)
        self._layout.setSpacing(6)
        scroll.setWidget(self._inner)
        # setWidget() flips autoFillBackground back to True on the inner widget,
        # undoing the transparency — must be disabled again after this call.
        self._inner.setAutoFillBackground(False)

        outer = QVBoxLayout(self.content_widget)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(scroll)

        # Decode whatever has scrolled into view as the strip is scrolled or
        # the panel is resized. The scrollbars are hidden but still drive the
        # viewport, so these signals fire normally. rangeChanged covers resize
        # (the scroll range shifts when the viewport grows/shrinks).
        bar = scroll.verticalScrollBar()
        bar.valueChanged.connect(self._load_visible)
        bar.rangeChanged.connect(lambda *_: self._load_visible())

        self.load_history()

    # ------------------------------------------------------------------
    # Public
    # ------------------------------------------------------------------

    def add_thumbnail(self, path: str):
        # A freshly captured image lands at the top of the strip and is
        # immediately visible, so decode it right away rather than deferring.
        label = self._make_label(path)
        self._layout.insertWidget(0, label)
        self._load(path)

    def remove_thumbnail(self, path: str):
        """Drop a thumbnail — called once its image is associated with a stamp
        and moved out of the incoming folder, so the strip only shows images
        still waiting to be assigned."""
        self._pending.discard(path)
        label = self._labels.pop(path, None)
        if label:
            label.setParent(None)
            label.deleteLater()

    def refresh_thumbnail(self, path: str, pixmap: QPixmap):
        label = self._labels.get(path)
        if label:
            self._pending.discard(path)
            label.setPixmap(
                pixmap.scaled(
                    THUMB_SIZE[0], THUMB_SIZE[1],
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                )
            )

    def load_history(self):
        try:
            files = sorted(
                [f for f in os.listdir(INCOMING_DIR) if f.lower().endswith(".jpg")],
                key=lambda f: os.path.getmtime(os.path.join(INCOMING_DIR, f)),
                reverse=True,
            )
        except Exception:
            files = []
        for f in files:
            path = os.path.join(INCOMING_DIR, f)
            self._layout.addWidget(self._make_label(path))
        # Decode the first screenful once geometry has been laid out.
        QTimer.singleShot(0, self._load_visible)

    # ------------------------------------------------------------------
    # Internal — lazy loading
    # ------------------------------------------------------------------

    def _make_label(self, path: str) -> QLabel:
        """Create a fixed-size placeholder for `path` (no image decode)."""
        label = QLabel()
        # Fixed size keeps the layout — and therefore the scroll geometry and
        # visibility math — stable whether or not the pixmap has loaded yet.
        label.setFixedSize(THUMB_SIZE[0], THUMB_SIZE[1])
        label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        label.setCursor(Qt.CursorShape.PointingHandCursor)
        label.mousePressEvent = lambda *_, p=path: self.image_selected.emit(p)
        self._labels[path] = label
        self._pending.add(path)
        return label

    def _load(self, path: str):
        """Decode + scale one placeholder's JPEG and drop it from the queue."""
        if path not in self._pending:
            return
        self._pending.discard(path)
        label = self._labels.get(path)
        if label is None:
            return
        label.setPixmap(
            QPixmap(path).scaled(
                THUMB_SIZE[0], THUMB_SIZE[1],
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
        )

    def _load_visible(self):
        """Decode any pending placeholders within (or one screen of) the view."""
        if not self._pending:
            return
        vp_h = self._scroll.viewport().height()
        if vp_h <= 0:
            return
        # Force the layout to position children now — reading label.y() does
        # not trigger a pending layout pass, and stale (all-zero) positions
        # would make every placeholder look visible and defeat the laziness.
        self._layout.activate()
        offset = self._scroll.verticalScrollBar().value()
        # Preload a screenful above and below so fast scrolling stays ahead of
        # the reveal.
        top = offset - vp_h
        bottom = offset + 2 * vp_h
        for path in list(self._pending):
            label = self._labels.get(path)
            if label is None:
                continue
            y = label.y()
            if y + label.height() >= top and y <= bottom:
                self._load(path)

    def showEvent(self, event):
        super().showEvent(event)
        self._load_visible()
