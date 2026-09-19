# ui/crop_dialog.py
#
# CropDialog — review and adjust a crop, then write it over the image.
#
# Opens with autocrop's proposal already selected when it has one, so the common
# path is "looks right, Apply". The selection can be dragged, moved and resized
# from its handles, and Auto-Detect puts the proposal back after a manual
# fiddle. When autocrop declines — most often because the stamp runs off the
# frame and it will not guess — the dialog simply opens with nothing selected,
# which is exactly the manual crop this replaced.
#
# Applying is destructive: it rewrites the file in place and discards pixels.
# snapshot_original() copies the untouched image aside first so the Revert
# button in the Preview panel can put it back (see image_storage).

import os

from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QWidget,
    QSizePolicy, QMessageBox,
)
from PySide6.QtCore import Qt, QRectF, QPointF
from PySide6.QtGui import QPainter, QPen, QColor, QBrush, QPixmap

import autocrop
from logger import logger
from ui.panels.history import load_pixmap

# Half-width of a drag handle, and how far from one the cursor may sit and still
# grab it. The grab margin is the larger of the two so handles on a small
# selection stay usable without drawing them big enough to hide the stamp.
_HANDLE = 4
_GRAB = 9

# Smallest selection, in image pixels. Stops a stray click collapsing the
# selection to nothing and then "cropping" to it.
_MIN_SIZE = 8

# Handle order: the eight positions clockwise from the top-left corner.
_TL, _T, _TR, _R, _BR, _B, _BL, _L = range(8)
_MOVE, _NEW = 8, 9

_CURSORS = {
    _TL: Qt.CursorShape.SizeFDiagCursor, _BR: Qt.CursorShape.SizeFDiagCursor,
    _TR: Qt.CursorShape.SizeBDiagCursor, _BL: Qt.CursorShape.SizeBDiagCursor,
    _T:  Qt.CursorShape.SizeVerCursor,   _B:  Qt.CursorShape.SizeVerCursor,
    _L:  Qt.CursorShape.SizeHorCursor,   _R:  Qt.CursorShape.SizeHorCursor,
    _MOVE: Qt.CursorShape.SizeAllCursor,
}


class CropCanvas(QWidget):
    """Shows an image and maintains a crop selection over it.

    The selection is held in *image* coordinates rather than widget ones, so it
    survives the dialog being resized: the mapping is recomputed on every paint
    instead of the rectangle being re-fitted. It also means crop_rect_in_image()
    is a rounding step rather than a conversion, which keeps the applied crop
    exactly where the handles were left.
    """

    def __init__(self, pixmap: QPixmap, parent=None):
        super().__init__(parent)
        self._pixmap = pixmap
        self._selection: QRectF | None = None
        self._drag_mode: int | None = None
        self._drag_anchor: QPointF | None = None    # the corner being dragged against
        self._move_offset: QPointF | None = None
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setMouseTracking(True)
        self.setCursor(Qt.CursorShape.CrossCursor)

    # ------------------------------------------------------------------
    # Coordinate mapping
    # ------------------------------------------------------------------

    def _view(self) -> tuple[float, float, float]:
        """(origin x, origin y, scale) of the image as currently drawn."""
        pw, ph = self._pixmap.width(), self._pixmap.height()
        if not pw or not ph:
            return 0.0, 0.0, 1.0
        scale = min(self.width() / pw, self.height() / ph)
        return (self.width() - pw * scale) / 2, (self.height() - ph * scale) / 2, scale

    def _to_widget(self, rect: QRectF) -> QRectF:
        ox, oy, scale = self._view()
        return QRectF(ox + rect.x() * scale, oy + rect.y() * scale,
                      rect.width() * scale, rect.height() * scale)

    def _to_image(self, point: QPointF) -> QPointF:
        ox, oy, scale = self._view()
        if scale <= 0:
            return QPointF(0, 0)
        # Clamped, so dragging out over the letterbox margin pins the edge to
        # the image instead of selecting empty space beside it.
        return QPointF(
            min(max((point.x() - ox) / scale, 0.0), float(self._pixmap.width())),
            min(max((point.y() - oy) / scale, 0.0), float(self._pixmap.height())),
        )

    # ------------------------------------------------------------------
    # Selection
    # ------------------------------------------------------------------

    def set_selection(self, box: tuple[int, int, int, int] | None):
        """Set the selection from an (x1, y1, x2, y2) box in image pixels."""
        if box is None:
            self._selection = None
        else:
            x1, y1, x2, y2 = box
            self._selection = QRectF(x1, y1, x2 - x1, y2 - y1)
        self.update()

    def crop_rect_in_image(self) -> tuple[int, int, int, int] | None:
        """(x1, y1, x2, y2) in image pixels, or None if nothing usable is set."""
        if self._selection is None:
            return None
        rect = self._selection.normalized()
        x1 = max(0, int(round(rect.left())))
        y1 = max(0, int(round(rect.top())))
        x2 = min(self._pixmap.width(), int(round(rect.right())))
        y2 = min(self._pixmap.height(), int(round(rect.bottom())))
        if x2 - x1 < _MIN_SIZE or y2 - y1 < _MIN_SIZE:
            return None
        return x1, y1, x2, y2

    def _handle_points(self, rect: QRectF) -> dict[int, QPointF]:
        cx, cy = rect.center().x(), rect.center().y()
        return {
            _TL: QPointF(rect.left(), rect.top()),
            _T:  QPointF(cx, rect.top()),
            _TR: QPointF(rect.right(), rect.top()),
            _R:  QPointF(rect.right(), cy),
            _BR: QPointF(rect.right(), rect.bottom()),
            _B:  QPointF(cx, rect.bottom()),
            _BL: QPointF(rect.left(), rect.bottom()),
            _L:  QPointF(rect.left(), cy),
        }

    def _hit(self, pos: QPointF) -> int:
        """Which handle (or _MOVE, or _NEW) the cursor is over."""
        if self._selection is None:
            return _NEW
        widget_rect = self._to_widget(self._selection.normalized())
        for handle, point in self._handle_points(widget_rect).items():
            if (abs(point.x() - pos.x()) <= _GRAB and abs(point.y() - pos.y()) <= _GRAB):
                return handle
        return _MOVE if widget_rect.contains(pos) else _NEW

    # ------------------------------------------------------------------
    # Painting
    # ------------------------------------------------------------------

    def paintEvent(self, _event):
        painter = QPainter(self)
        ox, oy, scale = self._view()
        drawn = QRectF(ox, oy, self._pixmap.width() * scale, self._pixmap.height() * scale)
        painter.drawPixmap(drawn.toRect(), self._pixmap)

        if self._selection is None:
            return

        selection = self._to_widget(self._selection.normalized())

        # Dim everything outside the selection so the crop reads at a glance.
        # Four bands rather than a painter path: cheaper, and no seam artefacts
        # where the rectangles meet.
        shade = QColor(0, 0, 0, 110)
        painter.fillRect(QRectF(drawn.left(), drawn.top(),
                                drawn.width(), selection.top() - drawn.top()), shade)
        painter.fillRect(QRectF(drawn.left(), selection.bottom(),
                                drawn.width(), drawn.bottom() - selection.bottom()), shade)
        painter.fillRect(QRectF(drawn.left(), selection.top(),
                                selection.left() - drawn.left(), selection.height()), shade)
        painter.fillRect(QRectF(selection.right(), selection.top(),
                                drawn.right() - selection.right(), selection.height()), shade)

        painter.setPen(QPen(QColor(255, 255, 255), 1, Qt.PenStyle.DashLine))
        painter.drawRect(selection)

        painter.setPen(QPen(QColor(30, 30, 30), 1))
        painter.setBrush(QBrush(QColor(255, 255, 255)))
        for point in self._handle_points(selection).values():
            painter.drawRect(QRectF(point.x() - _HANDLE, point.y() - _HANDLE,
                                    _HANDLE * 2, _HANDLE * 2))

    # ------------------------------------------------------------------
    # Mouse
    # ------------------------------------------------------------------

    def mousePressEvent(self, event):
        if event.button() != Qt.MouseButton.LeftButton:
            return
        pos = event.position()
        mode = self._hit(pos)
        image_point = self._to_image(pos)

        if mode == _NEW:
            self._selection = QRectF(image_point, image_point)
            self._drag_mode = _BR
            self._drag_anchor = QPointF(image_point)
        elif mode == _MOVE:
            self._drag_mode = _MOVE
            self._move_offset = image_point - self._selection.normalized().topLeft()
        else:
            rect = self._selection.normalized()
            self._selection = rect
            # Anchor the opposite corner so dragging a handle past it flips the
            # rectangle the way every other crop tool does.
            self._drag_mode = mode
            self._drag_anchor = QPointF(
                rect.right() if mode in (_TL, _L, _BL) else rect.left(),
                rect.bottom() if mode in (_TL, _T, _TR) else rect.top(),
            )
        self.update()

    def mouseMoveEvent(self, event):
        pos = event.position()
        if self._drag_mode is None:
            self.setCursor(_CURSORS.get(self._hit(pos), Qt.CursorShape.CrossCursor))
            return

        point = self._to_image(pos)
        if self._drag_mode == _MOVE:
            rect = self._selection.normalized()
            top_left = point - self._move_offset
            # Keep the whole selection on the image while moving it.
            x = min(max(top_left.x(), 0.0), self._pixmap.width() - rect.width())
            y = min(max(top_left.y(), 0.0), self._pixmap.height() - rect.height())
            self._selection = QRectF(x, y, rect.width(), rect.height())
        else:
            rect = QRectF(self._selection)
            anchor = self._drag_anchor
            if self._drag_mode in (_TL, _TR, _BL, _BR):
                self._selection = QRectF(anchor, point).normalized()
            elif self._drag_mode in (_T, _B):
                self._selection = QRectF(
                    rect.left(), min(anchor.y(), point.y()),
                    rect.width(), abs(point.y() - anchor.y()))
            else:   # _L, _R
                self._selection = QRectF(
                    min(anchor.x(), point.x()), rect.top(),
                    abs(point.x() - anchor.x()), rect.height())
        self.update()

    def mouseReleaseEvent(self, event):
        if event.button() != Qt.MouseButton.LeftButton:
            return
        self._drag_mode = None
        self._drag_anchor = None
        self._move_offset = None
        if self._selection is not None:
            self._selection = self._selection.normalized()
        self.update()


class CropDialog(QDialog):

    def __init__(self, image_path: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Crop Image")
        self.setMinimumSize(640, 520)
        self._image_path = image_path

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)

        # Phone photos are stored in the camera's native landscape buffer with
        # an EXIF Orientation tag saying how to turn them upright. QPixmap(path)
        # ignores that tag, so a portrait photo would lie on its side here even
        # though the History strip and the Preview panel — which both honour
        # EXIF — show it upright. Load it the same way they do, and crop in that
        # same upright space (see _apply). autocrop works in that space too.
        self._canvas = CropCanvas(load_pixmap(image_path))
        layout.addWidget(self._canvas, 1)

        self._hint = QLabel()
        self._hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._hint.setStyleSheet("color: gray; font-size: 11px;")
        layout.addWidget(self._hint)

        btn_row = QHBoxLayout()
        btn_row.addStretch()
        self._auto_btn = QPushButton("Auto-Detect")
        self._auto_btn.setFixedWidth(100)
        self._auto_btn.setToolTip("Put the automatic proposal back")
        self._auto_btn.clicked.connect(self._auto_detect)
        apply_btn = QPushButton("Apply Crop")
        apply_btn.setFixedWidth(100)
        apply_btn.setDefault(True)
        apply_btn.clicked.connect(self._apply)
        cancel_btn = QPushButton("Cancel")
        cancel_btn.setFixedWidth(80)
        cancel_btn.clicked.connect(self.reject)
        for btn in (self._auto_btn, apply_btn, cancel_btn):
            btn_row.addWidget(btn)
        layout.addLayout(btn_row)

        self._auto_detect(announce=True)

    def _auto_detect(self, announce: bool = False):
        """Run the detector and select what it proposes.

        A decline is not an error and is not reported as one — the dialog just
        falls back to the manual crop, which is what the button did before this
        feature existed.
        """
        box = None
        try:
            box = autocrop.detect(self._image_path)
        except Exception as e:
            # Never let a detector failure block a manual crop.
            logger.warning(f"autocrop failed for {self._image_path}: {e}")

        self._canvas.set_selection(box)
        if box is None:
            self._hint.setText(
                "No stamp detected automatically — drag a rectangle to select the crop area."
            )
        else:
            self._hint.setText(
                "Drag the handles to adjust, drag inside to move, "
                "or drag a new rectangle to start over."
            )
        if announce:
            logger.info(
                f"CropDialog: autocrop {'proposed ' + str(box) if box else 'declined'} "
                f"for {os.path.basename(self._image_path)}"
            )

    def _apply(self):
        rect = self._canvas.crop_rect_in_image()
        if rect is None:
            QMessageBox.warning(self, "No Selection",
                                "Drag a rectangle over the image to select a crop area first.")
            return
        try:
            # Shared with the automatic path, so a manual crop and an unattended
            # one snapshot and write identically. The selection is already in the
            # EXIF-upright space crop_in_place() expects.
            autocrop.crop_in_place(self._image_path, rect)
            self.accept()
        except Exception as e:
            QMessageBox.critical(self, "Crop Failed", str(e))
