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
# The incoming folder is also watched, so images that arrive from outside this
# process — phone uploads via stamp_phone/api.py, say — appear in the strip
# without a restart.
#
# A "Watch phone" toggle at the top of the strip starts/stops a background poll
# of a USB-connected iPhone (see ui/phone_watcher.py). Imports land in the same
# incoming folder, so they surface through the watcher above like any other file.
#
# Right-clicking a thumbnail offers Duplicate (copy the photo so it can be
# entered as a second stamp) and Delete (send it to the OS recycle bin).
#
# Signals emitted:  image_selected(str path)
#                   image_deleted(str path)
# Public methods:   add_thumbnail(path)          — called after a new capture
#                   refresh_thumbnail(path, pixmap) — called after an image is rotated
#                   stop_watching()              — called from Canvas.closeEvent

import os
from functools import partial

from PySide6.QtWidgets import (
    QScrollArea, QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QSizePolicy, QMenu, QMessageBox,
)
from PySide6.QtCore import Qt, Signal, QTimer, QFileSystemWatcher
from PySide6.QtGui import QPixmap, QImageReader

from config import INCOMING_DIR, THUMB_SIZE
from image_storage import duplicate_incoming, trash_image
from logger import logger
from ui.panel import Panel, hide_scrollbars
from ui.phone_watcher import PhoneWatcher

# How long to wait after the last filesystem event before rescanning. A single
# file write usually produces several notifications; coalescing them stops the
# strip rebuilding repeatedly mid-copy.
_RESCAN_DEBOUNCE_MS = 300

# Above this many new files at once, placeholder them and let the lazy loader
# decode what is on screen instead of decoding every one up front.
_EAGER_DECODE_LIMIT = 20


def load_pixmap(path: str) -> QPixmap:
    """Load an image as a QPixmap with its EXIF orientation applied.

    QPixmap(path) ignores the EXIF Orientation tag. Phone cameras record
    rotation there rather than rotating the pixels, so without this a portrait
    photo would sit sideways in the strip while cv2.imread() in the Preview
    panel — which does honour EXIF — showed it upright.

    Rotating on load rather than rewriting the file keeps the original bytes
    intact; re-encoding to bake the rotation in would cost a lossy generation.
    """
    reader = QImageReader(path)
    reader.setAutoTransform(True)
    image = reader.read()
    return QPixmap.fromImage(image) if not image.isNull() else QPixmap()


class HistoryPanel(Panel):

    image_selected = Signal(str)
    # Emitted after an incoming image is sent to the recycle bin, so Canvas can
    # drop it as the active image rather than leave a save pointing at a file
    # that is no longer there.
    image_deleted  = Signal(str)

    def __init__(self, parent=None):
        super().__init__("History", parent)

        self._labels: dict[str, QLabel] = {}
        self._pending: set[str] = set()   # placeholders not yet decoded
        # File mtime each thumbnail was decoded from, so _rescan can tell a
        # rewritten image (auto-crop, crop, revert) from an unchanged one.
        self._decoded_mtime: dict[str, float] = {}

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
        outer.addWidget(self._build_phone_controls())
        outer.addWidget(scroll)

        # Decode whatever has scrolled into view as the strip is scrolled or
        # the panel is resized. The scrollbars are hidden but still drive the
        # viewport, so these signals fire normally. rangeChanged covers resize
        # (the scroll range shifts when the viewport grows/shrinks).
        bar = scroll.verticalScrollBar()
        bar.valueChanged.connect(self._load_visible)
        bar.rangeChanged.connect(lambda *_: self._load_visible())

        self.load_history()

        # Watch the incoming folder so uploads from the phone show up live.
        # directoryChanged fires several times for one file, so it only arms a
        # timer; _rescan runs once the folder has been quiet for a moment.
        self._rescan_timer = QTimer(self)
        self._rescan_timer.setSingleShot(True)
        self._rescan_timer.setInterval(_RESCAN_DEBOUNCE_MS)
        self._rescan_timer.timeout.connect(self._rescan)

        self._watcher = QFileSystemWatcher(self)
        self._watcher.addPath(INCOMING_DIR)
        self._watcher.directoryChanged.connect(lambda *_: self._rescan_timer.start())

    # ------------------------------------------------------------------
    # Public
    # ------------------------------------------------------------------

    def add_thumbnail(self, path: str):
        # Guard against a double add: a local capture calls this directly while
        # the folder watcher independently notices the same new file.
        if path in self._labels:
            return
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
        self._decoded_mtime.pop(path, None)
        label = self._labels.pop(path, None)
        if label:
            label.setParent(None)
            label.deleteLater()

    def refresh_thumbnail(self, path: str, pixmap: QPixmap):
        label = self._labels.get(path)
        if label:
            self._pending.discard(path)
            # The caller already decoded the new version, so record its mtime —
            # otherwise the next rescan would decode the same file again.
            self._decoded_mtime[path] = self._safe_mtime(path)
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

    def stop_watching(self):
        """Stop the phone poll thread. Called from Canvas.closeEvent so the COM
        apartment is torn down before Qt shuts the process down under it."""
        self._phone_watcher.stop()

    # ------------------------------------------------------------------
    # Internal — phone import (USB)
    # ------------------------------------------------------------------

    def _build_phone_controls(self) -> QWidget:
        """The 'Watch phone' toggle strip above the thumbnail list.

        Imports land in INCOMING_DIR, so nothing here touches the strip
        directly — the folder watcher below notices them like any other new
        file, whether they arrived over USB or from the phone app's uploader.
        """
        bar = QWidget()
        row = QHBoxLayout(bar)
        row.setContentsMargins(4, 2, 4, 2)
        row.setSpacing(6)

        self._watch_btn = QPushButton("Watch phone")
        self._watch_btn.setCheckable(True)
        self._watch_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._watch_btn.setToolTip(
            "Poll a USB-connected iPhone and import new camera-roll photos"
        )
        self._watch_btn.toggled.connect(self._on_watch_toggled)
        row.addWidget(self._watch_btn)

        self._watch_status = QLabel("Off")
        self._watch_status.setObjectName("phoneWatchStatus")
        # Ignored horizontal policy: device names are long and this strip is
        # narrow, so the label must be free to shrink rather than forcing the
        # whole panel wider.
        self._watch_status.setSizePolicy(
            QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred
        )
        row.addWidget(self._watch_status, 1)

        # Named _phone_watcher, not _watcher: this class already has a
        # QFileSystemWatcher on that attribute, assigned after this runs.
        self._phone_watcher = PhoneWatcher(parent=self)
        self._phone_watcher.status_changed.connect(self._on_watch_status)
        return bar

    def _on_watch_toggled(self, checked: bool):
        if checked:
            self._phone_watcher.start()
        else:
            self._phone_watcher.stop()

    def _on_watch_status(self, text: str):
        # Full text in the tooltip because the label is free to elide/clip.
        self._watch_status.setText(text)
        self._watch_status.setToolTip(text)

    # ------------------------------------------------------------------
    # Internal — folder watching
    # ------------------------------------------------------------------

    @staticmethod
    def _safe_mtime(path: str) -> float:
        try:
            return os.path.getmtime(path)
        except OSError:
            return 0.0   # vanished between listing and sorting; sort it last

    def _rescan(self):
        """Reconcile the strip with what is actually in the incoming folder.

        Runs after the debounce timer rather than on every filesystem event.
        Only the .jpg suffix is listed, matching load_history() — the upload
        endpoint writes to a .part file and renames, so a partially written
        upload is never visible here.
        """
        try:
            on_disk = {
                os.path.join(INCOMING_DIR, f)
                for f in os.listdir(INCOMING_DIR)
                if f.lower().endswith(".jpg")
            }
        except OSError:
            return

        known = set(self._labels)

        for path in known - on_disk:
            self.remove_thumbnail(path)

        added = on_disk - known
        if added:
            # Oldest first — each insert goes to index 0, so the newest photo
            # ends up at the top of the strip.
            ordered = sorted(added, key=self._safe_mtime)
            if len(ordered) > _EAGER_DECODE_LIMIT:
                for path in ordered:
                    self._layout.insertWidget(0, self._make_label(path))
                self._load_visible()
            else:
                for path in ordered:
                    self.add_thumbnail(path)

        # A watched directory can be dropped when it is replaced rather than
        # modified; re-arm so later uploads still register.
        if INCOMING_DIR not in self._watcher.directories():
            self._watcher.addPath(INCOMING_DIR)

    def stop_watching(self):
        """Stop the phone poll thread. Called from Canvas.closeEvent so the COM
        apartment is torn down before Qt shuts the process down under it."""
        self._phone_watcher.stop()

    # ------------------------------------------------------------------
    # Internal — phone import (USB)
    # ------------------------------------------------------------------

    def _build_phone_controls(self) -> QWidget:
        """The 'Watch phone' toggle strip above the thumbnail list.

        Imports land in INCOMING_DIR, so nothing here touches the strip
        directly — the folder watcher below notices them like any other new
        file, whether they arrived over USB or from the phone app's uploader.
        """
        bar = QWidget()
        row = QHBoxLayout(bar)
        row.setContentsMargins(4, 2, 4, 2)
        row.setSpacing(6)

        self._watch_btn = QPushButton("Watch phone")
        self._watch_btn.setCheckable(True)
        self._watch_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._watch_btn.setToolTip(
            "Poll a USB-connected iPhone and import new camera-roll photos"
        )
        self._watch_btn.toggled.connect(self._on_watch_toggled)
        row.addWidget(self._watch_btn)

        self._watch_status = QLabel("Off")
        self._watch_status.setObjectName("phoneWatchStatus")
        # Ignored horizontal policy: device names are long and this strip is
        # narrow, so the label must be free to shrink rather than forcing the
        # whole panel wider.
        self._watch_status.setSizePolicy(
            QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred
        )
        row.addWidget(self._watch_status, 1)

        # Named _phone_watcher, not _watcher: this class already has a
        # QFileSystemWatcher on that attribute, assigned after this runs.
        self._phone_watcher = PhoneWatcher(parent=self)
        self._phone_watcher.status_changed.connect(self._on_watch_status)
        return bar

    def _on_watch_toggled(self, checked: bool):
        if checked:
            self._phone_watcher.start()
        else:
            self._phone_watcher.stop()

    def _on_watch_status(self, text: str):
        # Full text in the tooltip because the label is free to elide/clip.
        self._watch_status.setText(text)
        self._watch_status.setToolTip(text)

    # ------------------------------------------------------------------
    # Internal — folder watching
    # ------------------------------------------------------------------

    @staticmethod
    def _safe_mtime(path: str) -> float:
        try:
            return os.path.getmtime(path)
        except OSError:
            return 0.0   # vanished between listing and sorting; sort it last

    def _rescan(self):
        """Reconcile the strip with what is actually in the incoming folder.

        Runs after the debounce timer rather than on every filesystem event.
        Only the .jpg suffix is listed, matching load_history() — the upload
        endpoint writes to a .part file and renames, so a partially written
        upload is never visible here.
        """
        try:
            on_disk = {
                os.path.join(INCOMING_DIR, f)
                for f in os.listdir(INCOMING_DIR)
                if f.lower().endswith(".jpg")
            }
        except OSError:
            return

        known = set(self._labels)

        for path in known - on_disk:
            self.remove_thumbnail(path)

        # Re-decode anything rewritten since last drawn. Auto-crop is the reason
        # this is needed: a phone import or PWA upload publishes the file, this
        # rescan is debounced by 300ms, and the crop that follows takes longer
        # than that — so the thumbnail is drawn from the uncropped file and
        # would otherwise stay stale until the next launch. It also covers a
        # crop or revert applied from a second process.
        for path in known & on_disk:
            decoded = self._decoded_mtime.get(path)
            # Only images already on screen. A placeholder that has never been
            # decoded stays pending and is picked up by _load_visible when it is
            # scrolled to, reading the file as it is then — forcing it here
            # would decode the whole strip on every rescan.
            if decoded is not None and self._safe_mtime(path) != decoded:
                self._pending.add(path)
                self._load(path)

        added = on_disk - known
        if added:
            # Oldest first — each insert goes to index 0, so the newest photo
            # ends up at the top of the strip.
            ordered = sorted(added, key=self._safe_mtime)
            if len(ordered) > _EAGER_DECODE_LIMIT:
                for path in ordered:
                    self._layout.insertWidget(0, self._make_label(path))
                self._load_visible()
            else:
                for path in ordered:
                    self.add_thumbnail(path)

        # A watched directory can be dropped when it is replaced rather than
        # modified; re-arm so later uploads still register.
        if INCOMING_DIR not in self._watcher.directories():
            self._watcher.addPath(INCOMING_DIR)

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
        # Left button only — a right-click opens the context menu below, and
        # would otherwise also load the image into Preview on its way there.
        label.mousePressEvent = lambda e, p=path: (
            self.image_selected.emit(p)
            if e.button() == Qt.MouseButton.LeftButton
            else None
        )
        label.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        label.customContextMenuRequested.connect(partial(self._show_thumb_menu, path))
        self._labels[path] = label
        self._pending.add(path)
        return label

    # ------------------------------------------------------------------
    # Internal — thumbnail context menu
    # ------------------------------------------------------------------

    def _build_thumb_menu(self):
        """The right-click menu for one thumbnail, as (menu, duplicate, delete).

        Split out from _show_thumb_menu so what the menu offers can be checked
        without entering exec()'s modal loop.
        """
        menu = QMenu(self)
        dup_action = menu.addAction("Duplicate")
        dup_action.setToolTip("Copy this photo so it can be entered as a second stamp")
        menu.addSeparator()
        del_action = menu.addAction("Delete")
        del_action.setToolTip("Send this photo to the recycle bin")
        return menu, dup_action, del_action

    def _show_thumb_menu(self, path: str, pos):
        label = self._labels.get(path)
        if label is None:
            return
        menu, dup_action, del_action = self._build_thumb_menu()
        chosen = menu.exec(label.mapToGlobal(pos))
        if chosen is dup_action:
            self._duplicate_image(path)
        elif chosen is del_action:
            self._delete_image(path)

    def _duplicate_image(self, path: str):
        try:
            dest = duplicate_incoming(path)
        except Exception as e:
            logger.error(f"Could not duplicate {path}: {e}")
            QMessageBox.warning(
                self, "Duplicate Failed",
                f"Could not duplicate this image.\n\n{e}",
            )
            return
        # The folder watcher would pick this up on its own after the debounce;
        # adding it now just makes the copy appear immediately. add_thumbnail
        # ignores a path it already knows, so the two can't double up.
        self.add_thumbnail(dest)

    def _delete_image(self, path: str):
        """Send an incoming image to the recycle bin.

        No confirmation prompt: the recycle bin is the undo, and clearing a
        batch of rejects is meant to be quick.
        """
        try:
            trash_image(path)
        except Exception as e:
            logger.error(f"Could not delete {path}: {e}")
            QMessageBox.warning(
                self, "Delete Failed",
                f"Could not delete this image.\n\n{e}",
            )
            return
        self.remove_thumbnail(path)
        self.image_deleted.emit(path)

    def _load(self, path: str):
        """Decode + scale one placeholder's JPEG and drop it from the queue."""
        if path not in self._pending:
            return
        self._pending.discard(path)
        label = self._labels.get(path)
        if label is None:
            return
        # Remember which version of the file this pixmap came from, so _rescan
        # can spot the image being rewritten underneath us.
        self._decoded_mtime[path] = self._safe_mtime(path)
        label.setPixmap(
            load_pixmap(path).scaled(
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
