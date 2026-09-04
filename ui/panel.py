# ui/panel.py
#
# Panel  — base class for every draggable / resizable / minimizable widget.
# _TitleBar — internal drag handle; lives at the top of every Panel.
#
# Subclasses add their content widgets to self.content_widget.

from PySide6.QtWidgets import (
    QWidget, QHBoxLayout, QLabel, QPushButton, QAbstractScrollArea,
    QGraphicsDropShadowEffect,
)
from PySide6.QtCore import Qt, QPoint, QRect, QEvent, Signal
from PySide6.QtGui import QCursor, QPainter, QPalette, QColor

from ui.theme import (
    PANEL_BG, PANEL_TITLE_BG, PANEL_TITLE_FG,
    MIN_BTN_BG, MIN_BTN_FG, get_texture, get_texture_border,
    USE_TEXTURES,
)

_RESIZE_MARGIN = 6
_TITLE_H       = 26
_MIN_W         = 120
_MIN_H         = _TITLE_H + 60

_CURSOR_MAP = {
    'left':         Qt.CursorShape.SizeHorCursor,
    'right':        Qt.CursorShape.SizeHorCursor,
    'top':          Qt.CursorShape.SizeVerCursor,
    'bottom':       Qt.CursorShape.SizeVerCursor,
    'top_left':     Qt.CursorShape.SizeFDiagCursor,
    'top_right':    Qt.CursorShape.SizeBDiagCursor,
    'bottom_left':  Qt.CursorShape.SizeBDiagCursor,
    'bottom_right': Qt.CursorShape.SizeFDiagCursor,
}


def _apply_bg(widget: QWidget, color):
    # In the QSS skin the global stylesheet owns every background, so this
    # palette autofill would only fight it — skip it entirely. In the texture
    # skin this is the flat-color background (used directly, or as the fallback
    # behind a texture).
    if not USE_TEXTURES:
        return
    p = widget.palette()
    p.setColor(QPalette.ColorRole.Window, color)
    widget.setPalette(p)
    widget.setAutoFillBackground(True)


def apply_card_glow(widget: QWidget) -> None:
    """Attach the spectral violet bloom used on gallery / lens-result cards.

    A real soft glow can't be expressed in QSS, so the "#galleryCard" /
    "#resultCard" rules in ui/skins.py supply the bevelled rim and this adds
    the blur. No-op in the texture skin, whose parchment palette isn't built
    to pair with the violet glow. The effect is parented to `widget`, so it's
    freed when the card is deleted (cards are rebuilt on every reload).
    """
    if USE_TEXTURES:
        return
    glow = QGraphicsDropShadowEffect(widget)
    glow.setBlurRadius(16)
    glow.setColor(QColor(138, 108, 255, 150))  # _GLOW_VIOLET, soft alpha
    glow.setOffset(0, 0)
    widget.setGraphicsEffect(glow)


# Azure, sitting between the skin's violet (#8a6cff) and cyan (#74e0c9) accents
# so it reads as a distinct "this is information" blue rather than more theme.
_DEFAULT_GLOW_COLOR = QColor(92, 157, 255, 170)


def set_default_glow(widget: QWidget, on: bool) -> None:
    """Mark (or unmark) a form field as still holding a batch-default value.

    Unlike apply_card_glow this carries meaning rather than decoration — it says
    where a value came from — so it renders in both skins instead of no-opping
    on textures. Form fields carry no other QGraphicsEffect, so replacing the
    widget's effect wholesale is safe; the effect is parented to `widget`.
    """
    if on:
        glow = QGraphicsDropShadowEffect(widget)
        glow.setBlurRadius(14)
        glow.setColor(_DEFAULT_GLOW_COLOR)
        glow.setOffset(0, 0)
        widget.setGraphicsEffect(glow)
    else:
        widget.setGraphicsEffect(None)


def hide_scrollbars(area: QAbstractScrollArea) -> None:
    """Hide both scrollbars (and reclaim their gutters) on a scrollable widget.

    The hidden scrollbars still drive the viewport, so wheel / trackpad /
    keyboard scrolling keeps working — only the visual chrome goes away.
    Works on QScrollArea, QListWidget, and any other QAbstractScrollArea.
    """
    area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
    area.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)


# ---------------------------------------------------------------------------
# Internal title bar
# ---------------------------------------------------------------------------

class _TitleBar(QWidget):
    """Drag handle at the top of every Panel. Also owns the minimize button."""

    def __init__(self, title: str, panel: 'Panel', show_label: bool = True, transparent_bg: bool = False):
        super().__init__(panel)
        self._panel = panel
        self.setFixedHeight(_TITLE_H)
        self.setMouseTracking(True)
        self.setCursor(Qt.CursorShape.OpenHandCursor)
        # objectName + WA_StyledBackground let the QSS skin paint the title band
        # (see ui/skins.py "#panelTitleBar"). Harmless in the texture skin, where
        # no global stylesheet is set and the palette/transparent path below wins.
        self.setObjectName("panelTitleBar")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        if not transparent_bg:
            _apply_bg(self, PANEL_TITLE_BG)
        # else: left transparent so the panel's own texture, painted behind
        # in Panel.paintEvent, shows through the title-bar strip too.

        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 0, 4, 0)
        layout.setSpacing(4)

        self._label = QLabel(title)
        self._label.setObjectName("panelTitleLabel")
        p = self._label.palette()
        p.setColor(QPalette.ColorRole.WindowText, PANEL_TITLE_FG)
        self._label.setPalette(p)
        self._label.setVisible(show_label)  # kept (not removed) so .title still reports real text
        layout.addWidget(self._label)
        layout.addStretch()

        min_btn = QPushButton("—")
        min_btn.setObjectName("panelMinBtn")
        min_btn.setFixedSize(18, 18)
        min_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        min_btn.setToolTip("Minimise")
        min_btn.clicked.connect(panel.minimize_requested)
        _apply_bg(min_btn, MIN_BTN_BG)
        p2 = min_btn.palette()
        p2.setColor(QPalette.ColorRole.ButtonText, MIN_BTN_FG)
        min_btn.setPalette(p2)
        layout.addWidget(min_btn)

    @property
    def title(self) -> str:
        return self._label.text()

    def set_title(self, text: str):
        self._label.setText(text)

    # --- drag / corner-resize events --------------------------------------

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            gp = event.globalPosition().toPoint()
            local = self._panel.mapFromGlobal(gp)
            d = self._panel._get_resize_dir(local)
            if d in ('top_left', 'top_right'):
                self._panel._start_resize(gp, d)
            else:
                self._panel._start_drag(gp)

    def mouseMoveEvent(self, event):
        gp = event.globalPosition().toPoint()
        if self._panel._resizing:
            self._panel._do_resize(gp)
        elif self._panel._dragging:
            self._panel._do_drag(gp)
            self.setCursor(Qt.CursorShape.ClosedHandCursor)
        else:
            local = self._panel.mapFromGlobal(gp)
            d = self._panel._get_resize_dir(local)
            if d in ('top_left', 'top_right'):
                self.setCursor(QCursor(_CURSOR_MAP[d]))
            else:
                self.setCursor(Qt.CursorShape.OpenHandCursor)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._panel._end_drag()
            self._panel._end_resize()
            self.setCursor(Qt.CursorShape.OpenHandCursor)

    def leaveEvent(self, event):
        # Revert the top-corner resize cursor when the pointer leaves the title
        # bar (onto the content area or off the panel top). Left alone during an
        # active drag/resize, which owns the cursor until release.
        if not self._panel._resizing and not self._panel._dragging:
            self.setCursor(Qt.CursorShape.OpenHandCursor)
        super().leaveEvent(event)


# ---------------------------------------------------------------------------
# Panel base class
# ---------------------------------------------------------------------------

class Panel(QWidget):
    """
    Base class for all draggable / resizable / minimizable panels.

    Panels live as direct children of Canvas at absolute positions (no layout
    manager on the Canvas side).  Subclasses populate self.content_widget with
    their own widgets and layouts.
    """

    minimize_requested = Signal()     # emitted when the minimize button is clicked
    geometry_committed = Signal()     # emitted after the user finishes a drag or resize

    def __init__(self, title: str, parent=None, bg_texture: str | None = None, show_label: bool = True):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setMouseTracking(True)

        # Optional background texture (see ui/theme.py). When set, the panel
        # skips the flat PANEL_BG autofill entirely — paintEvent draws the
        # texture instead, and its transparent regions show whatever is
        # behind rather than a solid color "showing through". PANEL_BG is
        # only used there as a fallback if the texture file is missing.
        # The 9-slice border (see _draw_nine_slice) is looked up centrally
        # from ui.theme.TEXTURE_BORDERS by filename, not passed in here.
        self._bg_texture        = bg_texture
        self._bg_texture_border = get_texture_border(bg_texture) if bg_texture else None
        if not self._bg_texture:
            _apply_bg(self, PANEL_BG)

        # Drag state
        self._dragging       = False
        self._drag_offset    = QPoint()

        # Resize state
        self._resizing            = False
        self._resize_dir          = None
        self._resize_start_geom   = QRect()
        self._resize_start_pos    = QPoint()

        # Docked (minimized) state.  When docked the panel is simply hidden; it
        # keeps its full geometry so its home position/size survive for restore
        # and layout-save without being stashed separately (docking never
        # resizes the real widget).
        self._minimized        = False

        # Children — positioned manually in _layout_children so the Panel
        # border is always exposed for resize hit-testing.
        self._title_bar    = _TitleBar(title, self, show_label=show_label, transparent_bg=bool(bg_texture))
        self.content_widget = QWidget(self)
        # Give the content area its own default cursor so it never inherits the
        # panel's resize cursor. The panel sets a resize shape (SizeHor/…) on its
        # own widget while the pointer is on the 6px border; without an explicit
        # cursor here, moving from that border onto the content would show the
        # stuck resize cursor, because the panel only reverts its own cursor on
        # move events over its exposed border — which it stops getting once the
        # pointer is over this child.
        self.content_widget.setCursor(Qt.CursorShape.ArrowCursor)
        if not self._bg_texture:
            _apply_bg(self.content_widget, PANEL_BG)
        # else: left transparent so the Panel's own textured background
        # (painted in paintEvent) shows through behind the content widgets.

        # Raise panel to front whenever any child is clicked.
        # NOTE: subclasses that override eventFilter must guard attribute access
        # with hasattr() — this filter fires during super().__init__ before the
        # subclass constructor has finished assigning its own attributes.
        self._title_bar.installEventFilter(self)
        self.content_widget.installEventFilter(self)

        self.setMinimumSize(_MIN_W, _MIN_H)
        self._layout_children()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def title(self) -> str:
        return self._title_bar.title

    def set_title(self, text: str):
        self._title_bar.set_title(text)

    # ------------------------------------------------------------------
    # Background texture
    # ------------------------------------------------------------------

    def paintEvent(self, event):
        # QSS skin: don't draw the texture — WA_StyledBackground (set in
        # __init__) lets the global stylesheet paint the panel body instead.
        if self._bg_texture and USE_TEXTURES:
            pixmap = get_texture(self._bg_texture)
            painter = QPainter(self)
            if pixmap is not None:
                self._draw_nine_slice(painter, pixmap)
            else:
                # Texture file not found yet — fall back to the flat color
                # instead of rendering fully invisible.
                painter.fillRect(self.rect(), PANEL_BG)
        super().paintEvent(event)

    def _draw_nine_slice(self, painter: QPainter, pixmap):
        """Draw `pixmap` scaled to fill the panel without distorting its edges.

        Splits the source into a 3x3 grid using self._bg_texture_border: the 4
        corner cells are blitted at native size (never scaled), the 4 edge
        cells are stretched along one axis only, and the center cell is
        stretched both ways to fill whatever's left. Reused by every panel
        that sets bg_texture — this is generic, not specific to any one
        texture. Border comes from ui.theme.TEXTURE_BORDERS — either a single
        int (uniform) or an (x0, x1, y0, y1) per-side tuple.
        """
        tw, th = self.width(), self.height()
        sw, sh = pixmap.width(), pixmap.height()

        border = self._bg_texture_border
        if isinstance(border, tuple):
            x0, x1, y0, y1 = border
        else:
            x0 = x1 = y0 = y1 = border
        # Clamp so no border can exceed half the source or half the panel
        # (which would make corners overlap / invert).
        x0 = max(0, min(x0, sw // 2, tw // 2))
        x1 = max(0, min(x1, sw // 2, tw // 2))
        y0 = max(0, min(y0, sh // 2, th // 2))
        y1 = max(0, min(y1, sh // 2, th // 2))

        sx = (0, x0, sw - x1, sw)
        sy = (0, y0, sh - y1, sh)
        tx = (0, x0, tw - x1, tw)
        ty = (0, y0, th - y1, th)

        for row in range(3):
            sh_ = sy[row + 1] - sy[row]
            th_ = ty[row + 1] - ty[row]
            if sh_ <= 0 or th_ <= 0:
                continue
            for col in range(3):
                sw_ = sx[col + 1] - sx[col]
                tw_ = tx[col + 1] - tx[col]
                if sw_ <= 0 or tw_ <= 0:
                    continue
                src = QRect(sx[col], sy[row], sw_, sh_)
                dst = QRect(tx[col], ty[row], tw_, th_)
                painter.drawPixmap(dst, pixmap, src)

    def dock(self):
        """Hide the panel body.  Mechanism only — the Canvas decides when.

        The real widget is never resized to dock, so its home geometry stays
        intact while hidden and is restored verbatim by undock().  The morph
        animation will later wrap the Canvas-side call, snapshotting the panel
        before this runs.
        """
        if self._minimized:
            return
        self._minimized = True
        self.hide()

    def undock(self):
        """Show the panel body again at its retained home geometry."""
        if not self._minimized:
            return
        self._minimized = False
        self.show()
        self._layout_children()
        self.raise_()

    # ------------------------------------------------------------------
    # Child geometry  (manual, keeps panel borders exposed for resize)
    # ------------------------------------------------------------------

    def _layout_children(self):
        w, h = self.width(), self.height()
        self._title_bar.setGeometry(0, 0, w, _TITLE_H)
        if not self._minimized:
            cw = max(0, w - 2 * _RESIZE_MARGIN)
            ch = max(0, h - _TITLE_H - _RESIZE_MARGIN)
            self.content_widget.setGeometry(_RESIZE_MARGIN, _TITLE_H, cw, ch)

    def resizeEvent(self, event):
        self._layout_children()
        super().resizeEvent(event)

    # ------------------------------------------------------------------
    # Drag
    # ------------------------------------------------------------------

    def _start_drag(self, global_pos: QPoint):
        self._dragging = True
        parent = self.parentWidget()
        panel_global = parent.mapToGlobal(self.pos()) if parent else self.pos()
        self._drag_offset = global_pos - panel_global
        self.raise_()

    def _do_drag(self, global_pos: QPoint):
        if not self._dragging:
            return
        parent = self.parentWidget()
        new_tl_global = global_pos - self._drag_offset
        new_pos = parent.mapFromGlobal(new_tl_global) if parent else new_tl_global
        self.move(new_pos)

    def _end_drag(self):
        was_dragging = self._dragging
        self._dragging = False
        if was_dragging:
            self.geometry_committed.emit()

    # ------------------------------------------------------------------
    # Resize
    # ------------------------------------------------------------------

    def _get_resize_dir(self, local_pos: QPoint):
        m = _RESIZE_MARGIN
        x, y = local_pos.x(), local_pos.y()
        w, h = self.width(), self.height()
        on_l = x < m
        on_r = x > w - m
        on_t = y < m
        on_b = y > h - m
        if on_t and on_l: return 'top_left'
        if on_t and on_r: return 'top_right'
        if on_b and on_l: return 'bottom_left'
        if on_b and on_r: return 'bottom_right'
        if on_l:  return 'left'
        if on_r:  return 'right'
        if on_t:  return 'top'
        if on_b:  return 'bottom'
        return None

    def _start_resize(self, global_pos: QPoint, direction: str):
        self._resizing          = True
        self._resize_dir        = direction
        self._resize_start_geom = self.geometry()
        self._resize_start_pos  = global_pos
        self.raise_()

    def _do_resize(self, global_pos: QPoint):
        if not self._resizing:
            return
        dx = global_pos.x() - self._resize_start_pos.x()
        dy = global_pos.y() - self._resize_start_pos.y()
        g  = self._resize_start_geom
        d  = self._resize_dir
        x, y, w, h = g.x(), g.y(), g.width(), g.height()

        if 'right' in d:
            w = max(_MIN_W, g.width() + dx)
        if 'bottom' in d:
            h = max(_MIN_H, g.height() + dy)
        if 'left' in d:
            new_w = max(_MIN_W, g.width() - dx)
            x = g.x() + (g.width() - new_w)
            w = new_w
        if 'top' in d:
            new_h = max(_MIN_H, g.height() - dy)
            y = g.y() + (g.height() - new_h)
            h = new_h

        self.setGeometry(x, y, w, h)

    def _end_resize(self):
        was_resizing = self._resizing
        self._resizing   = False
        self._resize_dir = None
        if was_resizing:
            self.geometry_committed.emit()

    # ------------------------------------------------------------------
    # Mouse events  (fire on the exposed border / corner areas)
    # ------------------------------------------------------------------

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            d = self._get_resize_dir(event.position().toPoint())
            if d:
                self._start_resize(event.globalPosition().toPoint(), d)
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._resizing:
            self._do_resize(event.globalPosition().toPoint())
        else:
            d = self._get_resize_dir(event.position().toPoint())
            self.setCursor(QCursor(_CURSOR_MAP[d]) if d else Qt.CursorShape.ArrowCursor)
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._end_resize()
            self.setCursor(Qt.CursorShape.ArrowCursor)
        super().mouseReleaseEvent(event)

    def leaveEvent(self, event):
        # The pointer left the panel's exposed border — either onto a child
        # widget or off the panel entirely. mouseMoveEvent only reverts the
        # resize cursor while the pointer stays over that border, so without
        # this the cursor stays stuck on the last resize shape. Don't touch it
        # mid-resize: the drag owns the cursor until the button is released.
        if not self._resizing:
            self.setCursor(Qt.CursorShape.ArrowCursor)
        super().leaveEvent(event)

    # ------------------------------------------------------------------
    # Event filter — raise to front on any child click
    # ------------------------------------------------------------------

    def eventFilter(self, obj, event):
        if event.type() == QEvent.Type.MouseButtonPress:
            self.raise_()
        return False
