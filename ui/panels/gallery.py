# ui/panels/gallery.py
#
# GalleryPanel — paginated stamp card grid with a quick-search box, sort
# options, and a collapsible advanced filter builder (see ui/panels/
# filter_builder.py) that filters on any combination of stamp fields.
#
# Signals emitted:
#   stamp_load_requested(int stamp_id) — card click; Canvas routes to Fields

import os
import math

from PySide6.QtWidgets import (
    QVBoxLayout, QHBoxLayout, QLabel, QLineEdit, QPushButton,
    QComboBox, QScrollArea, QWidget, QGridLayout,
    QFrame, QSizePolicy,
)
from PySide6.QtCore import Qt, Signal, QEvent, QTimer
from PySide6.QtGui import QPixmap

from db.session import SessionLocal
from db.service import StampService
from logger import logger
from ui.panel import Panel, hide_scrollbars, apply_card_glow
from ui.collapsible import CollapsibleSection
from ui.panels.filter_builder import FilterBuilder

_CARD_W       = 160
# Approximate rendered card height (thumb 148 + title/info labels + margins),
# used only to estimate how many rows fit in the viewport when sizing a page.
# Slightly conservative so the grid fills the visible area rather than leaving
# a gap; a small amount of scroll is preferable to blank space.
_CARD_H       = 212
# Floor so a small/short panel still shows a sensible number of cards.
_MIN_PAGE_SIZE = 24
_SORT_MAP     = {
    "Newest First": "date",
    "Oldest First": "oldest",
    "Scott #":      "scott",
    "Country":      "country",
    "Title":        "title",
}


class GalleryPanel(Panel):

    stamp_load_requested = Signal(int)

    def __init__(self, parent=None):
        super().__init__("Gallery", parent)

        self._page = 0
        # Distinct-value cache for the filter builder's enum/relation dropdowns,
        # keyed by field. Cleared on refresh() so new countries/tags/etc show up.
        self._distinct_cache: dict[str, list[str]] = {}

        # Debounce resize-triggered reloads so DB is only hit once per drag,
        # not continuously while the resize handle is being moved.
        self._resize_timer = QTimer(self)
        self._resize_timer.setSingleShot(True)
        self._resize_timer.setInterval(150)
        self._resize_timer.timeout.connect(self.load)

        outer = QVBoxLayout(self.content_widget)
        outer.setContentsMargins(6, 6, 6, 6)
        outer.setSpacing(6)

        # Search row
        search_row = QHBoxLayout()
        self._search = QLineEdit()
        self._search.setPlaceholderText("Search title, Scott #, country, tags…")
        self._search.returnPressed.connect(self._apply_filters)
        search_btn = QPushButton("Search")
        search_btn.clicked.connect(self._apply_filters)
        search_row.addWidget(self._search)
        search_row.addWidget(search_btn)
        outer.addLayout(search_row)

        # Sort row
        filter_row = QHBoxLayout()
        filter_row.addWidget(QLabel("Sort:"))
        self._sort = QComboBox()
        self._sort.addItems(list(_SORT_MAP))
        self._sort.currentIndexChanged.connect(self._apply_filters)
        filter_row.addWidget(self._sort)
        filter_row.addStretch()
        outer.addLayout(filter_row)

        # Advanced filter builder — collapsed until needed. Applies on its own
        # Apply button (wired to _apply_filters), not live as rows are edited.
        self._filter_section = CollapsibleSection("Filters", expanded=False)
        self._filter_builder = FilterBuilder(self._distinct_values)
        self._filter_builder.apply_requested.connect(self._apply_filters)
        self._filter_section.add_widget(self._filter_builder)
        outer.addWidget(self._filter_section)

        # Card grid in a scroll area
        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        hide_scrollbars(self._scroll)
        # Drop the default StyledPanel frame so the cards sit directly on the
        # panel background instead of inside an un-themed bordered box (matches
        # the History and Results strips).
        self._scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        self._scroll.installEventFilter(self)
        # The viewport auto-fills an opaque palette colour that hides the panel
        # body behind it (flat PANEL_BG in the texture skin, or the QSS Panel
        # gradient in the qss skin). Disable it so the panel background shows
        # through the gaps between cards.
        self._scroll.setAutoFillBackground(False)
        self._scroll.viewport().setAutoFillBackground(False)
        self._grid_widget = QWidget()
        self._grid = QGridLayout(self._grid_widget)
        self._grid.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        self._grid.setSpacing(12)
        self._scroll.setWidget(self._grid_widget)
        # setWidget() re-enables autoFillBackground on the grid widget — undo it.
        self._grid_widget.setAutoFillBackground(False)
        outer.addWidget(self._scroll)

        # Pagination
        page_row = QHBoxLayout()
        self._prev_btn = QPushButton("◀")
        self._prev_btn.setFixedWidth(36)
        self._prev_btn.setEnabled(False)
        self._prev_btn.clicked.connect(self._prev_page)
        self._page_label = QLabel("Page 1 of 1")
        self._page_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._next_btn = QPushButton("▶")
        self._next_btn.setFixedWidth(36)
        self._next_btn.setEnabled(False)
        self._next_btn.clicked.connect(self._next_page)
        page_row.addStretch()
        page_row.addWidget(self._prev_btn)
        page_row.addWidget(self._page_label)
        page_row.addWidget(self._next_btn)
        page_row.addStretch()
        outer.addLayout(page_row)

        self.load()

    # ------------------------------------------------------------------
    # Public
    # ------------------------------------------------------------------

    def refresh(self):
        """Called by Canvas when collection_changed fires."""
        # Distinct values may have changed (new country, tag, series, …), so
        # drop the cache; the builder re-fetches lazily the next time a field
        # dropdown is built.
        self._distinct_cache.clear()
        self.load()

    def _grid_dimensions(self) -> tuple[int, int]:
        """Columns and rows of cards that fit in the current viewport."""
        spacing = self._grid.spacing()
        viewport_w = self._scroll.viewport().width()  or 400
        viewport_h = self._scroll.viewport().height() or 400
        cols = max(1, viewport_w // (_CARD_W + spacing))
        rows = max(1, viewport_h // (_CARD_H + spacing))
        return cols, rows

    def _page_size(self) -> int:
        """How many cards to load per page — enough to fill the visible grid."""
        cols, rows = self._grid_dimensions()
        return max(_MIN_PAGE_SIZE, cols * rows)

    def load(self):
        page_size = self._page_size()
        session = SessionLocal()
        try:
            stamps, total = StampService.get_gallery_stamps(
                session,
                search=self._search.text().strip(),
                sort=_SORT_MAP.get(self._sort.currentText(), "date"),
                rules=self._filter_builder.to_rules(),
                match=self._filter_builder.match(),
                limit=page_size,
                offset=self._page * page_size,
            )
        finally:
            session.close()

        total_pages = max(1, math.ceil(total / page_size))
        self._page = min(self._page, total_pages - 1)
        self._page_label.setText(f"Page {self._page + 1} of {total_pages}")
        self._prev_btn.setEnabled(self._page > 0)
        self._next_btn.setEnabled(self._page < total_pages - 1)

        # Clear existing cards
        while self._grid.count():
            item = self._grid.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        cols, _ = self._grid_dimensions()

        for idx, stamp_data in enumerate(stamps):
            card = self._build_card(stamp_data)
            self._grid.addWidget(card, idx // cols, idx % cols)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _distinct_values(self, field: str) -> list[str]:
        """Provider passed to the FilterBuilder for enum/relation dropdowns.

        Cached per field for the life of the panel; refresh() clears the cache.
        """
        if field not in self._distinct_cache:
            session = SessionLocal()
            try:
                self._distinct_cache[field] = StampService.get_distinct_values(session, field)
            finally:
                session.close()
        return self._distinct_cache[field]

    def _apply_filters(self):
        self._page = 0
        self.load()

    def _prev_page(self):
        if self._page > 0:
            self._page -= 1
            self.load()

    def _next_page(self):
        self._page += 1
        self.load()

    def _build_card(self, stamp_data: dict) -> QFrame:
        card = QFrame()
        card.setFixedWidth(_CARD_W)
        card.setFrameShape(QFrame.Shape.StyledPanel)
        card.setCursor(Qt.CursorShape.PointingHandCursor)
        # objectName drives the "#galleryCard" QSS rule (see ui/skins.py) which
        # paints the bevelled, glowing-rim card body in the qss skin. Harmless
        # in the texture skin, where StyledPanel above draws the frame instead.
        card.setObjectName("galleryCard")
        card.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        apply_card_glow(card)  # spectral violet bloom (qss skin only)

        layout = QVBoxLayout(card)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(3)

        thumb_size = _CARD_W - 12
        thumb = QLabel()
        thumb.setFixedSize(thumb_size, thumb_size)
        thumb.setAlignment(Qt.AlignmentFlag.AlignCenter)
        image_path = stamp_data.get("image_path")
        if image_path and os.path.exists(image_path):
            pix = QPixmap(image_path).scaled(
                thumb_size, thumb_size,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
        else:
            pix = QPixmap(thumb_size, thumb_size)
            pix.fill(Qt.GlobalColor.lightGray)
        thumb.setPixmap(pix)
        layout.addWidget(thumb)

        title_lbl = QLabel(stamp_data.get("title") or "")
        title_lbl.setWordWrap(True)
        title_lbl.setMaximumHeight(36)
        layout.addWidget(title_lbl)

        info_lbl = QLabel(
            f"#{stamp_data.get('scott_number', '')}  ·  {stamp_data.get('country', '')}"
        )
        info_lbl.setStyleSheet("color: gray; font-size: 10px;")
        info_lbl.setWordWrap(True)
        layout.addWidget(info_lbl)

        stamp_id = stamp_data["id"]
        card.mousePressEvent = lambda *_, sid=stamp_id: self.stamp_load_requested.emit(sid)

        return card

    # ------------------------------------------------------------------
    # Resize event filter — recalculate grid columns when panel is resized
    # ------------------------------------------------------------------
    # TODO: change to only hit db if resize hits thresholds that would change num cols
    def eventFilter(self, obj, event):
        if (hasattr(self, '_scroll')
                and obj is self._scroll
                and event.type() == QEvent.Type.Resize):
            self._resize_timer.start()
        return super().eventFilter(obj, event)
