# ui/panels/results.py
#
# ResultsPanel — horizontal scroll of Google Lens result cards.
# Also surfaces the derived Scott # / country suggestions from the result.
#
# Signals emitted:
#   scott_country_suggested(str, str) — most frequent scott + country; Canvas
#                                       routes to FieldsPanel.set_lens_hints()
#
# Public API (called by Canvas):
#   show_results(msg)  — full result dict from browser_worker
#   clear()            — wipe results before a new search

import base64
import io

import requests
from PIL import Image
from PIL.ImageQt import ImageQt

from PySide6.QtWidgets import (
    QVBoxLayout, QHBoxLayout, QLabel, QScrollArea, QWidget, QToolButton,
)
from PySide6.QtCore import Qt, Signal, QUrl
from PySide6.QtGui import QPixmap, QDesktopServices

from config import THUMB_SIZE
from logger import logger
from ui.panel import Panel, hide_scrollbars, apply_card_glow


class _HStripScrollArea(QScrollArea):
    """Scroll area for the horizontal results strip.

    Routes a plain vertical mouse wheel to horizontal panning — with the
    scrollbar hidden there is no other mouse-only way to move the strip
    (Qt does not fall back to the horizontal bar on its own).
    """

    def wheelEvent(self, event):
        d     = event.angleDelta()
        delta = d.y() if abs(d.y()) >= abs(d.x()) else d.x()
        hbar  = self.horizontalScrollBar()
        hbar.setValue(hbar.value() - delta)
        event.accept()


class ResultsPanel(Panel):

    scott_country_suggested = Signal(str, str)

    def __init__(self, parent=None):
        super().__init__("Results", parent)

        outer = QVBoxLayout(self.content_widget)
        outer.setContentsMargins(6, 6, 6, 6)
        outer.setSpacing(4)

        hint_row = QHBoxLayout()
        self._scott_lbl   = QLabel("Most frequent Scott #: —")
        self._country_lbl = QLabel("Most likely country: —")
        hint_row.addWidget(self._scott_lbl)
        hint_row.addStretch()
        hint_row.addWidget(self._country_lbl)
        outer.addLayout(hint_row)

        scroll = _HStripScrollArea()
        scroll.setWidgetResizable(True)
        hide_scrollbars(scroll)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        # The viewport auto-fills an opaque palette colour that hides the panel
        # body behind it — the flat PANEL_BG in the texture skin, or the QSS
        # Panel gradient in the qss skin. Disable it so the panel background
        # shows through the results strip.
        scroll.setAutoFillBackground(False)
        scroll.viewport().setAutoFillBackground(False)

        self._inner  = QWidget()
        self._layout = QHBoxLayout(self._inner)
        self._layout.setAlignment(Qt.AlignmentFlag.AlignLeft)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.setSpacing(6)
        scroll.setWidget(self._inner)
        # setWidget() re-enables autoFillBackground on the inner widget — undo it.
        self._inner.setAutoFillBackground(False)
        outer.addWidget(scroll)

    # ------------------------------------------------------------------
    # Public
    # ------------------------------------------------------------------

    def clear(self):
        for i in reversed(range(self._layout.count())):
            item = self._layout.itemAt(i)
            if item and item.widget():
                item.widget().setParent(None)
        self._scott_lbl.setText("Most frequent Scott #: —")
        self._country_lbl.setText("Most likely country: —")

    def show_results(self, msg: dict):
        self.clear()

        scott   = msg.get("scott") or ""
        country = msg.get("country") or ""

        self._scott_lbl.setText(
            f"Most frequent Scott #: {scott}" if scott else "Most frequent Scott #: —"
        )
        self._country_lbl.setText(
            f"Most likely country: {country}" if country else "Most likely country: —"
        )
        if scott or country:
            self.scott_country_suggested.emit(scott, country)

        results = msg.get("filtered", [])
        if not results:
            self._layout.addWidget(QLabel("No visual matches found."))
            return

        for item in results:
            try:
                thumb_url = item["thumbnail_url"]
                if thumb_url.startswith("data:"):
                    _, encoded = thumb_url.split(",", 1)
                    thumb_data = base64.b64decode(encoded)
                else:
                    thumb_data = requests.get(thumb_url, timeout=10).content

                pixmap = QPixmap.fromImage(ImageQt(Image.open(io.BytesIO(thumb_data)))).scaled(
                    THUMB_SIZE[0], THUMB_SIZE[1],
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                )
                btn = QToolButton()
                btn.setIcon(pixmap)
                btn.setIconSize(pixmap.size())
                btn.setText(item.get("title", ""))
                btn.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextUnderIcon)
                # objectName drives the "#resultCard" QSS rule (see ui/skins.py) —
                # the same bevelled, glowing card body as the gallery. apply_card_glow
                # adds the violet bloom (both no-op in the texture skin).
                btn.setObjectName("resultCard")
                apply_card_glow(btn)
                url = item["link"]
                btn.clicked.connect(lambda *_, u=url: QDesktopServices.openUrl(QUrl(u)))
                self._layout.addWidget(btn)
            except Exception as e:
                logger.warning(f"Failed to render Lens result: {e}")
