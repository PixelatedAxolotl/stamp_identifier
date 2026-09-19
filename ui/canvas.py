# ui/canvas.py
#
# Canvas — the main window surface.  Panels are children positioned absolutely.
#
# Owns:
#   - AsyncBrowserWorker + result queues (lens, colnect)
#   - Poll timers that drain both result queues and route to the right panels
#   - All inter-panel signal wiring
#   - Tab strip for minimized panels (Phase 4)
#
# Later phases will add:
#   - Layout save / restore to config  (Phase 5)

import json
import os
import queue
import multiprocessing as mp

from PySide6.QtWidgets import (
    QWidget, QLabel, QVBoxLayout, QHBoxLayout, QPushButton, QApplication,
)
from PySide6.QtCore import Qt, QTimer, QSize, QRect, QPoint, QEvent, Signal
from PySide6.QtGui import QPainter, QPalette, QColor

from browser_worker import AsyncBrowserWorker
from config import LAYOUT_FILE
from logger import logger
from ui.panel import Panel, _apply_bg
from ui.panels.database import DatabasePanel
from ui.panels.fields import FieldsPanel
from ui.panels.gallery import GalleryPanel
from ui.panels.history import HistoryPanel
from ui.panels.preview import PreviewPanel
from ui.panels.results import ResultsPanel
from ui.panels.themes import ThemesPanel
from ui.theme import (
    CANVAS_BG, CANVAS_BG_SCALE, CANVAS_BG_TEXTURE,
    MIN_BTN_BG, MIN_BTN_FG, get_texture, USE_TEXTURES,
)

# Width (px) of the invisible band around the window edge that starts a
# window resize when the canvas background is grabbed there.
_WIN_EDGE_MARGIN = 8

# Maps panel title → implementation class.
# To register a new panel: add one import above and one entry here.
_PANEL_CLASSES = {
    "Database":           DatabasePanel,
    "Fields":             FieldsPanel,
    "Gallery":            GalleryPanel,
    "History":            HistoryPanel,
    "Preview + Controls": PreviewPanel,
    "Results":            ResultsPanel,
    "Themes":             ThemesPanel,
}

# Default panel layout: (title, x, y, width, height)
# Edit these to change where panels appear on first launch. These pixel
# coordinates are authored against the _DESIGN_W × _DESIGN_H reference below
# (their own bounding box) and stored internally as fractions of the window,
# so the whole arrangement scales to fit any window size (see _entry_to_frac).
_DEFAULT_LAYOUT = [
    ("Gallery",            20,   20,  260, 500),
    ("Preview + Controls", 300,  20,  500, 500),
    ("History",            1080, 20,  180, 420),
    ("Results",            300,  540, 600, 140),
    ("Database",           20,   540, 260, 250),
    ("Themes",             820,  20,  240, 460),
    ("Fields",             300,  700, 400, 800),
]

# Reference canvas size the _DEFAULT_LAYOUT pixels are authored against — its
# own bounding box, so every default panel maps to a fraction within [0, 1].
_DESIGN_W = max(x + w for _t, x, _y, w, _h in _DEFAULT_LAYOUT)
_DESIGN_H = max(y + h for _t, _x, y, _w, h in _DEFAULT_LAYOUT)


# ---------------------------------------------------------------------------
# Docking — paper strips
# ---------------------------------------------------------------------------
#
# A docked (minimized) panel is represented by a "paper strip": a colored tab
# that sticks out of the edge of the book.  Clicking a strip restores (undocks)
# its panel.  Each dockable panel has one authored slot in _DOCK_SLOTS.

# Paper-strip dimensions, in pixels.
_STRIP_STICKOUT = 26   # how far a tab protrudes from its edge
_STRIP_SPAN     = 96   # a tab's length along its edge

# Dock slots: panel title → (edge, nx, ny, color, texture)
#   edge    — which book edge the tab sits on; sets the placeholder's
#             orientation (left/right → tall & thin, top/bottom → wide & short).
#             A texture carries its own shape, so edge is ignored once set.
#   nx, ny  — normalized [0..1] center within the book *texture rect* (not the
#             window), so tabs stay glued to the artwork as the window resizes.
#             Tune these to line up with the painted tabs once the art exists.
#   color   — flat placeholder fill, shown until a texture is set (or if its
#             file is missing)
#   texture — strip-art filename in ui/assets/, or None for the placeholder.
#             A textured tab is drawn at its native size scaled to the book, so
#             it keeps its aspect ratio and tracks the artwork on resize.
_DOCK_SLOTS = {
    "Gallery":            ("left",   55/1444, 210/885, QColor("#3f77c0"), "blue_tab_left.png"),
    "Database":           ("left",   67/1444, 325/885,  QColor("#c93f3f"), "red_tab_left.png"),
    "Results":            ("left", 62/1444 , 445/885, QColor("#317543"), "green_tab_left.png"),
    "History":            ("left",  60/1444,  570/855,   QColor("#c9a24c"), "yellow_tab_left.png"),
    "Preview + Controls": ("right",  1365/1444, 265/855, QColor("#7a4f9e"), "purple_tab_right.png"),  # art: 1378,249 of 1444x885
    "Fields":             ("right",  1372/1444, 500/855,   QColor("#813f44"), "red_tab_right.png"),
    "Themes":             ("right",  1387/1444,  652/855,   QColor("#7a6d4f"), "green_tab_right.png"),
}


class PaperStrip(QWidget):
    """Docked representation of a panel — a colored tab at the book's edge.

    Paints a flat color for now.  Pass a texture name once the strip art is
    ready and it paints that instead, falling back to the color if the file is
    missing (same drop-in pattern as get_texture elsewhere).
    """

    clicked = Signal()

    def __init__(self, color: QColor, texture: str | None = None, parent=None):
        super().__init__(parent)
        self._color   = color
        self._texture = texture
        # Let the tab art's transparent regions show the book behind it.
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def paintEvent(self, event):
        painter = QPainter(self)
        # QSS skin: draw the flat slot color as a clean tab, no strip art.
        pixmap  = get_texture(self._texture) if (self._texture and USE_TEXTURES) else None
        if pixmap is not None:
            scaled = pixmap.scaled(
                self.size(),
                Qt.AspectRatioMode.IgnoreAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
            painter.drawPixmap(0, 0, scaled)
        else:
            painter.fillRect(self.rect(), self._color)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit()
        super().mousePressEvent(event)


# ---------------------------------------------------------------------------
# Canvas
# ---------------------------------------------------------------------------

class Canvas(QWidget):
    """Top-level window.  Panels are absolute-positioned children."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Stamp Identifier")
        self.setMinimumSize(800, 600)
        self.resize(1200, 500)

        # Frameless — the book IS the window; no OS title bar.  Its jobs are
        # reimplemented in the "Window management" section below: drag empty
        # background to move, grab a window edge to resize, double-click the
        # background to maximize/restore, and the small top-right buttons
        # minimize/maximize/close.  Alt+F4 and the taskbar work as usual.
        self.setWindowFlags(Qt.WindowType.Window | Qt.WindowType.FramelessWindowHint)
        self.setMouseTracking(True)
        # Armed on background press; a real window move starts only after the
        # drag threshold so plain clicks and double-clicks stay intact.
        self._move_press_pos: QPoint | None = None

        # Texture skin: flat CANVAS_BG autofill sits behind the book texture
        # painted in paintEvent. QSS skin: hand the whole surface to the global
        # stylesheet (see ui/skins.py "#canvas") via WA_StyledBackground.
        if USE_TEXTURES:
            p = self.palette()
            p.setColor(QPalette.ColorRole.Window, CANVAS_BG)
            self.setPalette(p)
            self.setAutoFillBackground(True)
        else:
            self.setObjectName("canvas")
            self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)

        self._panels: list[Panel] = []

        # Each panel's home geometry as fractions of the window (fx, fy, fw, fh),
        # the source of truth for placement. Pixel geometry is recomputed from
        # these on every resize so panels scale with the window instead of being
        # clipped off-frame. Updated whenever the user drags/resizes a panel.
        self._panel_fracs: dict[Panel, tuple[float, float, float, float]] = {}

        # Named panel references — set in load_default_layout
        self._preview_panel:  PreviewPanel  | None = None
        self._history_panel:  HistoryPanel  | None = None
        self._gallery_panel:  GalleryPanel  | None = None
        self._database_panel: DatabasePanel | None = None
        self._results_panel:  ResultsPanel  | None = None
        self._themes_panel:   ThemesPanel   | None = None
        self._fields_panel:   FieldsPanel   | None = None

        # Single source of truth for the image backing the stamp currently
        # being identified. Every source that changes the active image (capture,
        # history click, stamp/reassign load) routes through set_current_image_path;
        # FieldsPanel reads it back at save time via an injected provider. This is
        # deliberately distinct from PreviewPanel.current_image_path, which is that
        # panel's own display state (the file it is showing / rotating / cropping).
        self.current_image_path: str | None = None

        # Browser worker + result queues (owned by Canvas)
        self._lens_queue     = mp.Queue()
        self._colnect_queue  = mp.Queue()
        self._browser_worker = AsyncBrowserWorker(self._lens_queue, self._colnect_queue)

        # Poll timer — drains both result queues every 100 ms
        self._poll_timer = QTimer(self)
        self._poll_timer.setInterval(100)
        self._poll_timer.timeout.connect(self._poll_results)

        # panel → its paper strip.  A strip is created for every dockable panel
        # in load_default_layout and shown only while that panel is docked.
        self._strips: dict[Panel, PaperStrip] = {}

        # Window control buttons — placeholder chrome styled like the panel
        # minimize button; retheme or fold into the book art later.
        self._window_btns = QWidget(self)
        btn_row = QHBoxLayout(self._window_btns)
        btn_row.setContentsMargins(0, 0, 0, 0)
        btn_row.setSpacing(2)
        for glyph, tip, slot in (
            ("—", "Minimise",           self.showMinimized),
            ("▢", "Maximise / restore", self._toggle_maximized),
            ("✕", "Close",              self.close),
        ):
            btn = QPushButton(glyph)
            btn.setObjectName("winBtn")
            btn.setFixedSize(22, 22)
            btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            btn.setToolTip(tip)
            btn.clicked.connect(slot)
            _apply_bg(btn, MIN_BTN_BG)
            pal = btn.palette()
            pal.setColor(QPalette.ColorRole.ButtonText, MIN_BTN_FG)
            btn.setPalette(pal)
            btn_row.addWidget(btn)
        self._position_window_btns()

        # Panels raise themselves on every click; this app-level filter puts
        # the window buttons back on top right after any such raise.
        QApplication.instance().installEventFilter(self)

    # ------------------------------------------------------------------
    # Background texture
    # ------------------------------------------------------------------

    def paintEvent(self, event):
        # QSS skin: skip the book texture; the stylesheet paints #canvas.
        if not USE_TEXTURES:
            return super().paintEvent(event)
        pixmap = get_texture(CANVAS_BG_TEXTURE)
        if pixmap is None:
            return super().paintEvent(event)

        # Draw the book texture at the cover-fit rect from _texture_rect().
        # Scaling to that rect's exact size (which already carries the expanded
        # aspect ratio) reproduces a KeepAspectRatioByExpanding fill, cropping
        # overflow — undistorted at any window size.
        rect   = self._texture_rect()
        scaled = pixmap.scaled(
            rect.size(),
            Qt.AspectRatioMode.IgnoreAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        painter = QPainter(self)
        painter.drawPixmap(rect.topLeft(), scaled)
        super().paintEvent(event)

    def _texture_rect(self) -> QRect:
        """Rect the book texture occupies in Canvas coords (cover-fit).

        Dock slots are positioned against this rect so paper strips stay glued
        to the artwork as the window resizes.  Matches the paintEvent fill
        exactly; falls back to the full widget rect when the texture is
        missing.  CANVAS_BG_SCALE zooms on top of the fill (see ui/theme.py).
        """
        # QSS skin has no book art; positioning dock slots against the full
        # window maps their normalized centers onto the real window edges.
        if not USE_TEXTURES:
            return self.rect()
        pixmap = get_texture(CANVAS_BG_TEXTURE)
        if pixmap is None:
            return self.rect()
        target = QSize(round(self.width()  * CANVAS_BG_SCALE),
                       round(self.height() * CANVAS_BG_SCALE))
        scaled = pixmap.size().scaled(
            target, Qt.AspectRatioMode.KeepAspectRatioByExpanding
        )
        x = (self.width()  - scaled.width())  // 2
        y = (self.height() - scaled.height()) // 2
        return QRect(x, y, scaled.width(), scaled.height())

    def _slot_rect(self, edge: str, nx: float, ny: float,
                   texture: str | None = None) -> QRect:
        """Pixel rect for a dock slot given its edge + normalized center.

        A textured tab is sized to its native dimensions scaled by the book's
        current scale, so it keeps its aspect ratio and tracks the artwork as
        the window resizes.  A placeholder (no texture) uses the flat strip
        dimensions, oriented by edge.
        """
        tex = self._texture_rect()
        cx  = tex.x() + round(nx * tex.width())
        cy  = tex.y() + round(ny * tex.height())

        # QSS skin: ignore strip art and use the flat placeholder tab dimensions.
        pix = get_texture(texture) if (texture and USE_TEXTURES) else None
        if pix is not None:
            book  = get_texture(CANVAS_BG_TEXTURE)
            scale = tex.width() / book.width() if book and book.width() else 1.0
            w = max(1, round(pix.width()  * scale))
            h = max(1, round(pix.height() * scale))
        elif edge in ("left", "right"):
            w, h = _STRIP_STICKOUT, _STRIP_SPAN
        else:
            w, h = _STRIP_SPAN, _STRIP_STICKOUT
        return QRect(cx - w // 2, cy - h // 2, w, h)

    # ------------------------------------------------------------------
    # Panel management
    # ------------------------------------------------------------------

    def add_panel(self, panel: Panel, frac: tuple[float, float, float, float]) -> Panel:
        panel.setParent(self)
        self._panels.append(panel)
        self._panel_fracs[panel] = frac
        panel.show()
        return panel

    # ------------------------------------------------------------------
    # Responsive geometry — panels scale with the window (fractions → px)
    # ------------------------------------------------------------------

    def _entry_to_frac(self, entry) -> tuple[float, float, float, float]:
        """Normalize a layout entry to (fx, fy, fw, fh) fractions of the window.

        Accepts new-style saved entries (already fractional: fx/fy/fw/fh),
        old-style pixel entries (x/y/w/h dicts), and the _DEFAULT_LAYOUT tuples
        — the latter two are divided by the _DESIGN_W/H reference.
        """
        if isinstance(entry, dict):
            if "fx" in entry:
                return (entry["fx"], entry["fy"], entry["fw"], entry["fh"])
            x, y, w, h = entry["x"], entry["y"], entry["w"], entry["h"]
        else:
            _title, x, y, w, h = entry
        return (x / _DESIGN_W, y / _DESIGN_H, w / _DESIGN_W, h / _DESIGN_H)

    def _relayout_panels(self):
        """Recompute every panel's pixel geometry from its stored fraction.

        Hybrid scaling: position and size both scale with the window, but size
        is floored at each panel's own minimum and the whole rect is clamped to
        stay fully inside the frame — so nothing is ever clipped off-screen.
        """
        W, H = self.width(), self.height()
        if W <= 0 or H <= 0:
            return
        for panel in self._panels:
            fr = self._panel_fracs.get(panel)
            if fr is None:
                continue
            fx, fy, fw, fh = fr
            w = min(W, max(panel.minimumWidth(),  round(fw * W)))
            h = min(H, max(panel.minimumHeight(), round(fh * H)))
            x = max(0, min(round(fx * W), W - w))
            y = max(0, min(round(fy * H), H - h))
            panel.setGeometry(x, y, w, h)

    def _store_panel_fracs(self, panel: Panel):
        """Capture a panel's current geometry as window fractions (on drag/resize)."""
        W, H = self.width(), self.height()
        if W <= 0 or H <= 0:
            return
        g = panel.geometry()
        self._panel_fracs[panel] = (g.x() / W, g.y() / H, g.width() / W, g.height() / H)

    def load_default_layout(self):
        saved            = self._load_layout()
        layout_entries   = saved if saved else _DEFAULT_LAYOUT
        minimized_titles = {
            e["title"] for e in layout_entries
            if isinstance(e, dict) and e.get("minimized")
        }

        for entry in layout_entries:
            title = entry["title"] if isinstance(entry, dict) else entry[0]
            frac  = self._entry_to_frac(entry)

            cls = _PANEL_CLASSES.get(title)
            if cls:
                p = cls(self)
            else:
                p = Panel(title, self)
                lbl = QLabel(f"[ {title} ]")
                lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
                QVBoxLayout(p.content_widget).addWidget(lbl)
            self.add_panel(p, frac)

        # Size/position every panel from its fraction for the current window.
        self._relayout_panels()

        # Store typed references
        for p in self._panels:
            if isinstance(p, PreviewPanel):
                self._preview_panel = p
            elif isinstance(p, HistoryPanel):
                self._history_panel = p
            elif isinstance(p, GalleryPanel):
                self._gallery_panel = p
            elif isinstance(p, DatabasePanel):
                self._database_panel = p
            elif isinstance(p, ResultsPanel):
                self._results_panel = p
            elif isinstance(p, ThemesPanel):
                self._themes_panel = p
            elif isinstance(p, FieldsPanel):
                self._fields_panel = p

        self._inject_dependencies()
        self._wire_signals()
        self._start_services()

        # Build a paper strip for every dockable panel (hidden until docked)
        self._create_strips()

        # Stow any panels that were docked when the layout was saved
        for panel in self._panels:
            if panel.title in minimized_titles:
                self._dock(panel)

    # ------------------------------------------------------------------
    # Dependency injection
    # ------------------------------------------------------------------

    def _inject_dependencies(self):
        if self._fields_panel and self._themes_panel:
            self._fields_panel.set_themes_panel(self._themes_panel)
        if self._preview_panel:
            self._preview_panel.set_browser_worker(self._browser_worker)
        if self._fields_panel:
            self._fields_panel.set_browser_worker(self._browser_worker)
            # Fields reads the active image from Canvas (single source of truth)
            # rather than caching its own copy.
            self._fields_panel.set_image_path_provider(lambda: self.current_image_path)

    def set_current_image_path(self, path: str | None):
        """Update the image backing the stamp being identified. Wired to every
        source that changes the active image; normalizes falsy paths to None."""
        self.current_image_path = path or None

    def _on_incoming_image_deleted(self, path: str):
        """Clear the active image when that exact file has just been deleted.

        Only the active image is touched — the Preview panel keeps showing what
        it already decoded into memory, which is harmless and avoids a jarring
        blank on a delete that had nothing to do with what is on screen.
        """
        if self.current_image_path and os.path.normpath(
            self.current_image_path
        ) == os.path.normpath(path):
            self.set_current_image_path(None)

    def _on_similar_search(self, image_path: str):
        """Match `image_path` against the collection and show the results.

        Canvas owns this rather than the Preview panel because the pieces live
        in three places: Preview has the image, Fields has the country and face
        value that narrow the pool, and the candidate query needs a database
        session.

        Runs synchronously. A filtered pool is a few dozen images and returns in
        well under a tenth of a second; the unfiltered fall-back is the slow
        case, and it is bounded by the collection size rather than open-ended,
        so a wait cursor covers it without the complexity of a worker thread.
        """
        from PySide6.QtWidgets import QApplication, QMessageBox
        from PySide6.QtGui import QCursor

        import visual_search
        from db.service import ImageService
        from db.session import SessionLocal
        from ui.similar_dialog import SimilarResultsDialog

        if not image_path or not os.path.exists(image_path):
            QMessageBox.information(self, "Find Similar", "No image to search.")
            return

        filters = (self._fields_panel.get_search_filters()
                   if self._fields_panel else {"country": "", "face_value": ""})

        session = SessionLocal()
        try:
            candidates = ImageService.get_visual_candidates(
                session,
                country=filters.get("country") or None,
                face_value=filters.get("face_value") or None,
            )
        except Exception as e:
            logger.exception("visual search: candidate query failed")
            QMessageBox.warning(self, "Find Similar", f"Could not read the collection:\n{e}")
            return
        finally:
            session.close()

        # A filter that matches nothing is a typo, not an empty collection —
        # searching everything instead would silently ignore what was typed.
        if not candidates:
            QMessageBox.information(
                self, "Find Similar",
                "No stamps match those filters, so there is nothing to compare "
                "against.\n\nCheck the country and face value fields, or clear "
                "them to search the whole collection."
            )
            return

        meta = {c["file_path"]: c for c in candidates}
        QApplication.setOverrideCursor(QCursor(Qt.CursorShape.WaitCursor))
        try:
            matches = visual_search.search(image_path, list(meta), limit=25)
        except Exception as e:
            logger.exception("visual search: match failed")
            QMessageBox.warning(self, "Find Similar", f"Search failed:\n{e}")
            return
        finally:
            QApplication.restoreOverrideCursor()

        logger.info(
            f"visual search: {len(matches)} result(s) from {len(meta)} candidates "
            f"(country={filters.get('country')!r} value={filters.get('face_value')!r})"
        )

        dlg = SimilarResultsDialog(image_path, matches, meta, len(meta), filters, self)
        if self._fields_panel:
            dlg.stamp_chosen.connect(self._fields_panel.load_stamp)
        dlg.exec()

    # ------------------------------------------------------------------
    # Signal wiring
    # ------------------------------------------------------------------

    def _wire_signals(self):
        p = self._preview_panel
        h = self._history_panel
        g = self._gallery_panel
        d = self._database_panel
        r = self._results_panel
        f = self._fields_panel

        # Capture → History thumbnail + current image + clear fields
        if p and h:
            p.capture_complete.connect(h.add_thumbnail)
        if p:
            p.capture_complete.connect(self.set_current_image_path)
        if p and f:
            p.capture_complete.connect(lambda _: f.clear_for_new_capture())

        # Rotate → History thumbnail refresh
        if p and h:
            p.image_changed.connect(h.refresh_thumbnail)

        # Image associated with a stamp → drop it from the incoming/History strip
        if f and h:
            f.image_associated.connect(h.remove_thumbnail)

        # Image reassigned away from a stamp → its predecessor returns to the
        # incoming pool, so add it back to the History strip.
        if f and h:
            f.image_deassociated.connect(h.add_thumbnail)

        # New search → clear old results
        if p and r:
            p.search_started.connect(r.clear)

        # History click → load image in Preview + set as current image + reset
        # the Fields form to add mode. Picking a past capture means "start a
        # fresh add/search with this image", so any stamp that was loaded for
        # editing must be cleared and the Add buttons restored — the old UI did
        # this via show_image(clear_metadata=True). clear_for_new_capture()
        # only resets the form (clear_image stays False), so the image the
        # Preview just showed and the active-image path set above both survive.
        if h and p:
            h.image_selected.connect(p.show_image)
        if h:
            h.image_selected.connect(self.set_current_image_path)
        if h and f:
            h.image_selected.connect(lambda _: f.clear_for_new_capture())

        # History right-click → Delete. If the image that just went to the
        # recycle bin was the one being identified, forget it, so a save can't
        # go looking for a file that is no longer there.
        if h:
            h.image_deleted.connect(self._on_incoming_image_deleted)

        # Gallery / Database stamp click → load stamp in Fields
        if g and f:
            g.stamp_load_requested.connect(f.load_stamp)
        if d and f:
            d.stamp_load_requested.connect(f.load_stamp)

        # Preview "Find Similar" → match against the local collection
        if p:
            p.similar_search_requested.connect(self._on_similar_search)

        # Lens result suggestion → fill empty Scott # / country in Fields
        if r and f:
            r.scott_country_suggested.connect(f.set_lens_hints)

        # Fields save/delete → refresh Gallery + Database
        if f and g:
            f.collection_changed.connect(g.refresh)
        if f and d:
            f.collection_changed.connect(d.refresh)

        # Database bulk-delete → refresh Gallery + self
        if d and g:
            d.collection_changed.connect(g.refresh)
        if d:
            d.collection_changed.connect(d.refresh)

        # Fields loads/reassigns/clears an image → show in Preview + set current
        if f and p:
            f.stamp_image_load_requested.connect(p.show_image)
        if f:
            f.stamp_image_load_requested.connect(self.set_current_image_path)

        # Minimize button → dock the panel to its paper strip; drag/resize
        # end → re-capture the panel's fraction so responsive layout follows it.
        for panel in self._panels:
            panel.minimize_requested.connect(lambda pnl=panel: self._dock(pnl))
            panel.geometry_committed.connect(lambda pnl=panel: self._store_panel_fracs(pnl))

    # ------------------------------------------------------------------
    # Docking  (expanded panel  ⇄  paper strip)
    # ------------------------------------------------------------------

    def _create_strips(self):
        """Make one hidden PaperStrip per dockable panel, wired to undock."""
        for panel in self._panels:
            slot = _DOCK_SLOTS.get(panel.title)
            if slot is None:
                continue
            _edge, _nx, _ny, color, texture = slot
            strip = PaperStrip(color, texture, parent=self)
            strip.clicked.connect(lambda pnl=panel: self._undock(pnl))
            strip.hide()
            self._strips[panel] = strip
            self._position_strip(panel, strip)

    def _dock(self, panel: Panel):
        """Stow a panel: hide the body, reveal its paper strip.

        Single orchestration point for expanded → docked.  The morph animation
        will live here later — snapshot the panel before panel.dock(), tween,
        then show the strip — without changing this structure or its callers.
        """
        if panel._minimized:
            return
        strip = self._strips.get(panel)
        if strip is None:
            return                       # panel has no dock slot; leave it be
        panel.dock()                     # mechanism: hide real panel at home geometry
        self._position_strip(panel, strip)
        strip.show()
        strip.raise_()

    def _undock(self, panel: Panel):
        """Restore a panel: hide its paper strip, show the body again."""
        strip = self._strips.get(panel)
        if strip is not None:
            strip.hide()
        panel.undock()                   # mechanism: show real panel at home geometry

    def _position_strip(self, panel: Panel, strip: PaperStrip):
        slot = _DOCK_SLOTS.get(panel.title)
        if slot is None:
            return
        edge, nx, ny, _color, texture = slot
        strip.setGeometry(self._slot_rect(edge, nx, ny, texture))

    def _position_strips(self):
        for panel, strip in self._strips.items():
            self._position_strip(panel, strip)

    # ------------------------------------------------------------------
    # Canvas resize — keep paper strips glued to the book artwork
    # ------------------------------------------------------------------

    def resizeEvent(self, event):
        self._relayout_panels()
        self._position_strips()
        self._position_window_btns()
        super().resizeEvent(event)

    # ------------------------------------------------------------------
    # Window management (frameless) — move / resize / min / max / close
    # ------------------------------------------------------------------

    def _toggle_maximized(self):
        if self.isMaximized():
            self.showNormal()
        else:
            self.showMaximized()

    def _position_window_btns(self):
        s = self._window_btns.sizeHint()
        self._window_btns.setGeometry(self.width() - s.width() - 6, 6,
                                      s.width(), s.height())
        self._window_btns.raise_()

    def _window_edges(self, pos: QPoint) -> Qt.Edges:
        """Which window edge(s) the point sits on, for frameless resize."""
        edges = Qt.Edges()
        if self.isMaximized():
            return edges              # a maximized window has no resize edges
        m = _WIN_EDGE_MARGIN
        if pos.x() <= m:                  edges |= Qt.Edge.LeftEdge
        if pos.x() >= self.width() - m:   edges |= Qt.Edge.RightEdge
        if pos.y() <= m:                  edges |= Qt.Edge.TopEdge
        if pos.y() >= self.height() - m:  edges |= Qt.Edge.BottomEdge
        return edges

    @staticmethod
    def _edge_cursor(edges: Qt.Edges) -> Qt.CursorShape:
        l = bool(edges & Qt.Edge.LeftEdge)
        r = bool(edges & Qt.Edge.RightEdge)
        t = bool(edges & Qt.Edge.TopEdge)
        b = bool(edges & Qt.Edge.BottomEdge)
        if (l and t) or (r and b):
            return Qt.CursorShape.SizeFDiagCursor
        if (r and t) or (l and b):
            return Qt.CursorShape.SizeBDiagCursor
        if l or r:
            return Qt.CursorShape.SizeHorCursor
        if t or b:
            return Qt.CursorShape.SizeVerCursor
        return Qt.CursorShape.ArrowCursor

    def _on_background(self, pos: QPoint) -> bool:
        """True only when pos is over bare book background, not a child widget.

        A press on a panel border (resize) or title bar reaches Canvas by event
        propagation — Qt's base mousePressEvent ignores it, bubbling it to the
        parent. Without this guard the window-move/resize would fire on top of
        the panel's own drag/resize. childAt() returns the panel there, so we
        skip; it returns None only on empty canvas.
        """
        return self.childAt(pos) is None

    # The window move/resize handlers below only act on the empty background;
    # over any panel/strip/button they defer to that child's own handling.

    def mousePressEvent(self, event):
        pos = event.position().toPoint()
        if (event.button() == Qt.MouseButton.LeftButton
                and self._on_background(pos) and self.windowHandle()):
            edges = self._window_edges(pos)
            if edges:
                self.windowHandle().startSystemResize(edges)
            else:
                self._move_press_pos = event.globalPosition().toPoint()
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._move_press_pos is not None:
            moved = (event.globalPosition().toPoint() - self._move_press_pos).manhattanLength()
            if moved >= QApplication.startDragDistance() and self.windowHandle():
                self._move_press_pos = None
                self.windowHandle().startSystemMove()
            return
        pos = event.position().toPoint()
        if self._on_background(pos):
            self.setCursor(self._edge_cursor(self._window_edges(pos)))
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._move_press_pos = None
        super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self, event):
        pos = event.position().toPoint()
        if (event.button() == Qt.MouseButton.LeftButton
                and self._on_background(pos)
                and not self._window_edges(pos)):
            self._toggle_maximized()
        super().mouseDoubleClickEvent(event)

    def eventFilter(self, obj, event):
        # App-level filter: panels raise themselves on any mouse press, which
        # would bury the window buttons; re-raise them right afterwards
        # (singleShot(0) runs after the panel's own raise has happened).
        if event.type() == QEvent.Type.MouseButtonPress:
            QTimer.singleShot(0, self._window_btns.raise_)
        return False

    # ------------------------------------------------------------------
    # Layout persistence
    # ------------------------------------------------------------------

    def _load_layout(self) -> list[dict] | None:
        try:
            with open(LAYOUT_FILE) as f:
                return json.load(f)
        except FileNotFoundError:
            return None
        except Exception as e:
            logger.warning(f"Could not read layout file: {e}")
            return None

    def _save_layout(self):
        data = []
        for panel in self._panels:
            fx, fy, fw, fh = self._panel_fracs.get(panel, (0.0, 0.0, 0.2, 0.2))
            data.append({
                "title":     panel.title,
                "fx":        fx,
                "fy":        fy,
                "fw":        fw,
                "fh":        fh,
                "minimized": panel._minimized,
            })
        try:
            os.makedirs(os.path.dirname(os.path.abspath(LAYOUT_FILE)), exist_ok=True)
            with open(LAYOUT_FILE, "w") as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            logger.warning(f"Could not save layout: {e}")

    # ------------------------------------------------------------------
    # Background services
    # ------------------------------------------------------------------

    def _start_services(self):
        self._browser_worker.start()
        self._poll_timer.start()

    # ------------------------------------------------------------------
    # Result queue polling  (runs every 100 ms)
    # ------------------------------------------------------------------

    def _poll_results(self):
        self._drain_lens_queue()
        self._drain_colnect_queue()

    def _drain_lens_queue(self):
        try:
            while True:
                msg = self._lens_queue.get_nowait()
                if self._preview_panel:
                    self._preview_panel.stop_spinner()
                if msg.get("error"):
                    logger.error(f"Lens search failed: {msg['error']}")
                    continue
                if self._results_panel:
                    self._results_panel.show_results(msg)
        except queue.Empty:
            pass
        except Exception as e:
            logger.error(f"Error draining lens queue: {e}")

    def _drain_colnect_queue(self):
        try:
            while True:
                msg      = self._colnect_queue.get_nowait()
                msg_type = msg.get("type")

                if msg_type == "login_done":
                    if msg.get("error"):
                        logger.error(f"Colnect login failed: {msg['error']}")
                    else:
                        logger.info("Colnect login successful")

                elif msg_type == "search_done":
                    if self._fields_panel:
                        self._fields_panel.stop_colnect_spinner()
                    if msg.get("error"):
                        logger.error(f"Colnect search failed: {msg['error']}")

                elif msg_type == "get_info_done":
                    if self._fields_panel:
                        self._fields_panel.stop_colnect_spinner()
                    if msg.get("error"):
                        logger.error(f"Colnect get-info failed: {msg['error']}")
                        continue
                    if self._fields_panel and msg.get("result"):
                        self._fields_panel.fill_from_colnect(msg["result"])

        except queue.Empty:
            pass
        except Exception as e:
            logger.error(f"Error draining colnect queue: {e}")

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------

    def closeEvent(self, event):
        self._save_layout()
        self._poll_timer.stop()
        # Stop the camera reader thread BEFORE Qt/MSMF teardown, otherwise the
        # background thread's cap.read() races the shutdown and access-violates.
        try:
            if getattr(self, '_preview_panel', None):
                self._preview_panel.stop_camera()
        except Exception as e:
            logger.warning(f"Camera shutdown error: {e}")
        # Stop the phone poll thread before Qt tears down, so its COM apartment
        # is closed while the process is still healthy.
        try:
            if getattr(self, '_history_panel', None):
                self._history_panel.stop_watching()
        except Exception as e:
            logger.warning(f"Phone watcher shutdown error: {e}")
        try:
            self._browser_worker.shutdown()
            self._browser_worker.join(timeout=3)
        except Exception as e:
            logger.warning(f"Browser worker shutdown error: {e}")
        super().closeEvent(event)
