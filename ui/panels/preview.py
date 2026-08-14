# ui/panels/preview.py
#
# PreviewPanel — camera live feed, capture/search, zoom, camera settings.
#
# Signals emitted:
#   capture_complete(str path)       — new image captured; Canvas routes to History + Fields
#   image_rotated(str, QPixmap)      — after rotate in image mode; Canvas routes to History
#   search_started()                 — Lens search kicked off; Canvas can track result polling
#
# Dependencies injected by Canvas after init:
#   set_browser_worker(worker)       — must be called before search will do anything
#   show_image(path)                 — called by Canvas when History thumbnail is clicked
#   stop_spinner()                   — called by Canvas when Lens results arrive

import os
import time
import threading
import cv2
from datetime import datetime

from PySide6.QtWidgets import (
    QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QSlider,
    QCheckBox, QGroupBox, QGridLayout, QWidget, QSizePolicy,
)
from PySide6.QtCore import Qt, Signal, QTimer, QEvent, QRectF
from PySide6.QtGui import QPixmap, QPainter, QPen, QColor
from PIL import Image
from PIL.ImageQt import ImageQt

from config import INCOMING_DIR, CAMERA_INDEX, CAMERA_FPS
from logger import logger
from ui.panel import Panel
from ui.spinner import SpinnerWidget


_ROT_MAP = {
    90:  cv2.ROTATE_90_CLOCKWISE,
    180: cv2.ROTATE_180,
    270: cv2.ROTATE_90_COUNTERCLOCKWISE,
}

_CAM_PROPS = [
    ("Brightness", cv2.CAP_PROP_BRIGHTNESS),
    ("Contrast",   cv2.CAP_PROP_CONTRAST),
    ("Saturation", cv2.CAP_PROP_SATURATION),
    ("Sharpness",  cv2.CAP_PROP_SHARPNESS),
]


class PreviewPanel(Panel):

    capture_complete = Signal(str)
    image_rotated    = Signal(str, QPixmap)
    search_started   = Signal()

    def __init__(self, parent=None):
        super().__init__("Preview + Controls", parent, bg_texture="demo_widget.png", show_label=False)

        # Camera / state
        self._cap              = cv2.VideoCapture()
        self._current_frame    = None
        self._cam_rotation     = 0
        self._cam_fail_count   = 0
        self.current_image_path: str | None = None
        self.preview_mode      = "live"
        self.zoom              = 1.0
        self._focus_tap_pos    = None
        self._browser_worker   = None
        self._request_counter  = 0

        # Background camera reader — cap.read() never runs on the main thread.
        # Reader thread continuously stores latest frame; display timer just picks it up.
        self._latest_frame: object       = None
        self._latest_frame_lock          = threading.Lock()
        self._cam_reader_running: bool   = False
        self._cam_reader_thread: threading.Thread | None = None
        self._opening_camera: bool       = False  # guard against duplicate async opens

        # slider dicts: cv2 prop id → (QSlider, QLabel)
        self._cam_prop_sliders:  dict[int, tuple] = {}
        self._cam_prop_defaults: dict[int, int]   = {}

        self._build_ui()

        self._frame_timer = QTimer(self)
        self._frame_timer.timeout.connect(self._update_frame)

        self._focus_tap_timer = QTimer(self)
        self._focus_tap_timer.setSingleShot(True)
        self._focus_tap_timer.setInterval(1500)
        self._focus_tap_timer.timeout.connect(self._clear_focus_tap)

        self._preview_label.installEventFilter(self)
        self.set_live_mode()

    # ------------------------------------------------------------------
    # Dependency injection
    # ------------------------------------------------------------------

    def set_browser_worker(self, worker):
        self._browser_worker = worker

    # ------------------------------------------------------------------
    # Public API (called by Canvas)
    # ------------------------------------------------------------------

    def show_image(self, path: str):
        """Load a saved image into the preview and switch to image mode."""
        if not path:            # "" is the clear signal — nothing to display
            return
        img = cv2.imread(path)
        if img is None:
            return
        self.current_image_path = path
        self._current_frame = img
        self.set_image_mode()
        self._display_frame(img)

    def stop_spinner(self):
        self._spinner.stop()

    # ------------------------------------------------------------------
    # Mode switching
    # ------------------------------------------------------------------

    def set_live_mode(self):
        import threading as _t
        logger.info(
            f"[CAM] set_live_mode() — thread={_t.current_thread().name} "
            f"opening_camera={self._opening_camera} "
            f"cam_thread_alive={self._cam_reader_thread.is_alive() if self._cam_reader_thread else False} "
            f"timer_active={self._frame_timer.isActive()}"
        )
        self.preview_mode = "live"
        self._capture_btn.setVisible(True)
        self._capture_only_btn.setVisible(True)
        self._camera_btn.setVisible(False)
        self._search_btn.setVisible(False)
        self._crop_btn.setVisible(False)

        # Start display timer immediately — _update_frame handles the spinner and
        # waits for frames; it does NOT need _on_camera_opened to have fired first.
        if not self._frame_timer.isActive():
            self._frame_timer.start(int(1000 / CAMERA_FPS))
            logger.info("[CAM] set_live_mode() → display timer started")

        # Start the camera thread if it isn't already running.
        if self._opening_camera:
            logger.info("[CAM] set_live_mode() → camera thread already opening, nothing to do")
            return
        if self._cam_reader_thread and self._cam_reader_thread.is_alive():
            logger.info("[CAM] set_live_mode() → camera thread already running, nothing to do")
            return
        logger.info("[CAM] set_live_mode() → starting camera thread")
        self._opening_camera = True
        self._cam_reader_running = True
        if hasattr(self, '_first_display_logged'):
            del self._first_display_logged
        self._spinner.start("Opening camera…")
        self._cam_reader_thread = threading.Thread(
            target=self._cam_thread_loop, daemon=True, name="CameraThread"
        )
        self._cam_reader_thread.start()

    def set_image_mode(self):
        logger.info(
            f"[CAM] set_image_mode() — "
            f"reader_running={self._cam_reader_running} "
            f"reader_alive={self._cam_reader_thread.is_alive() if self._cam_reader_thread else False} "
            f"cap.isOpened={self._cap.isOpened()}"
        )
        self.preview_mode = "image"
        self._capture_btn.setVisible(False)
        self._capture_only_btn.setVisible(False)
        self._camera_btn.setVisible(True)
        self._search_btn.setVisible(True)
        self._crop_btn.setVisible(True)
        # Stop the display timer only — reader thread keeps running so the
        # MSMF pipeline stays warm. Switching back to live is then instant.
        self._frame_timer.stop()
        logger.info("[CAM] set_image_mode() — display timer stopped, reader thread left running")

    # ------------------------------------------------------------------
    # Button actions
    # ------------------------------------------------------------------

    def capture_image(self):
        path = self._do_capture()
        if path:
            self.search_image()

    def capture_image_only(self):
        self._do_capture()

    def search_image(self):
        if not self.current_image_path or not self._browser_worker:
            return
        self._request_counter += 1
        self._spinner.start("Searching with Google Lens…")
        self._browser_worker.schedule_lens_search(
            self.current_image_path, self._request_counter
        )
        self.search_started.emit()

    def show_camera(self):
        logger.info(f"[CAM] Camera button clicked — calling set_live_mode()")
        self.set_live_mode()

    # ------------------------------------------------------------------
    # Internal — capture
    # ------------------------------------------------------------------

    def _do_capture(self) -> str | None:
        if self._current_frame is None:
            return None
        ts   = datetime.now().strftime("%Y%m%d_%H%M%S")
        # New captures are un-associated; they live in INCOMING_DIR until they
        # are attached to a stamp (storage.associate_image moves them to IMAGE_DIR).
        path = os.path.join(INCOMING_DIR, f"stamp_{ts}.jpg")
        try:
            frame = self._current_frame
            zoom  = self.zoom or 1.0
            if zoom > 1.0:
                fh, fw = frame.shape[:2]
                cw = max(1, int(fw / zoom))
                ch = max(1, int(fh / zoom))
                x1 = max(0, (fw - cw) // 2)
                y1 = max(0, (fh - ch) // 2)
                to_save = frame[y1:y1 + ch, x1:x1 + cw]
            else:
                to_save = frame
            cv2.imwrite(path, to_save)
        except Exception:
            cv2.imwrite(path, self._current_frame)
        self.current_image_path = path
        self.set_image_mode()
        img = cv2.imread(path)
        if img is not None:
            self._current_frame = img
            self._display_frame(img)
        self.capture_complete.emit(path)
        return path

    def _rotate_image(self):
        self._cam_rotation = (self._cam_rotation + 90) % 360
        self._rotate_btn.setToolTip(
            f"Rotate 90° clockwise (current: {self._cam_rotation}°)"
        )
        if self.preview_mode == "image" and self._current_frame is not None:
            self._current_frame = cv2.rotate(self._current_frame, cv2.ROTATE_90_CLOCKWISE)
            self._display_frame(self._current_frame)
            if self.current_image_path:
                cv2.imwrite(self.current_image_path, self._current_frame)
                pix = QPixmap(self.current_image_path)
                self.image_rotated.emit(self.current_image_path, pix)

    def _open_crop_dialog(self):
        if not self.current_image_path or not os.path.exists(self.current_image_path):
            return
        # CropDialog is defined in stamp_identifier_v3; import lazily to avoid
        # circular imports until it is migrated to its own module.
        try:
            from stamp_identifier_v3 import CropDialog
            from PySide6.QtWidgets import QDialog
            dlg = CropDialog(self.current_image_path, self)
            if dlg.exec() == QDialog.DialogCode.Accepted:
                img = cv2.imread(self.current_image_path)
                if img is not None:
                    self._current_frame = img
                    self._display_frame(img)
        except Exception as e:
            logger.warning(f"CropDialog unavailable: {e}")

    # ------------------------------------------------------------------
    # Internal — camera
    # ------------------------------------------------------------------

    def _open_camera(self) -> cv2.VideoCapture:
        import threading as _t
        logger.info(f"[CAM] _open_camera() start — thread={_t.current_thread().name} index={CAMERA_INDEX}")
        for attempt, (w, h) in enumerate([(0, 0), (640, 480), (1280, 720)]):
            res = f"{w}x{h}" if w else "default"
            logger.info(f"[CAM] _open_camera() attempt {attempt+1}/3 — res={res} calling VideoCapture()")
            t0 = time.time()
            cap = cv2.VideoCapture(CAMERA_INDEX, cv2.CAP_MSMF)
            logger.info(f"[CAM] _open_camera() VideoCapture() returned in {time.time()-t0:.2f}s — isOpened={cap.isOpened()}")
            if not cap.isOpened():
                cap.release()
                continue
            if w:
                cap.set(cv2.CAP_PROP_FRAME_WIDTH, w)
                cap.set(cv2.CAP_PROP_FRAME_HEIGHT, h)
            for warmup in range(5):
                logger.info(f"[CAM] _open_camera() warmup read {warmup+1}/5 — res={res}")
                t1 = time.time()
                ret, _ = cap.read()
                logger.info(f"[CAM] _open_camera() warmup read {warmup+1}/5 returned in {time.time()-t1:.2f}s — ret={ret}")
                if ret:
                    logger.info(f"[CAM] _open_camera() SUCCESS — res={res} total_time={time.time()-t0:.2f}s")
                    return cap
            logger.info(f"[CAM] _open_camera() all warmup reads failed for res={res}, releasing")
            cap.release()
        logger.warning("[CAM] _open_camera() FAILED — camera unavailable")
        return cv2.VideoCapture()

    def _cam_thread_loop(self):
        """Single camera thread: opens the camera AND reads frames — both on the same
        thread. This avoids MSMF's dislike of handing a VideoCapture between threads.
        Frames are stored in _latest_frame; _update_frame picks them up on the main thread."""
        import threading as _t
        logger.info(f"[CAM] _cam_thread_loop() — thread={_t.current_thread().name} opening camera")
        t0 = time.time()
        cap = self._open_camera()
        self._opening_camera = False

        if not cap.isOpened():
            logger.warning("[CAM] _cam_thread_loop() — failed to open camera")
            QTimer.singleShot(0, self._spinner.stop)
            return

        self._cap = cap
        logger.info(f"[CAM] _cam_thread_loop() — camera open in {time.time()-t0:.2f}s, entering frame loop")

        # Notify main thread to reapply settings (non-blocking; frames flow regardless)
        QTimer.singleShot(0, self._on_camera_ready)

        frame_count = 0
        t_loop = time.time()
        while self._cam_reader_running:
            t_read = time.time()
            ret, frame = cap.read()
            elapsed = time.time() - t_read
            if ret:
                frame_count += 1
                if frame_count == 1:
                    logger.info(f"[CAM] _cam_thread_loop() — FIRST FRAME after {time.time()-t0:.2f}s total, read took {elapsed:.3f}s")
                elif frame_count % 150 == 0:
                    fps = frame_count / (time.time() - t_loop)
                    logger.info(f"[CAM] _cam_thread_loop() — {frame_count} frames avg {fps:.1f} fps")
                self._cam_fail_count = 0
                if self._cam_rotation:
                    frame = cv2.rotate(frame, _ROT_MAP[self._cam_rotation])
                with self._latest_frame_lock:
                    self._latest_frame = frame
            else:
                self._cam_fail_count += 1
                logger.info(f"[CAM] _cam_thread_loop() — read FAILED (fail_count={self._cam_fail_count}) took {elapsed:.3f}s")
                if self._cam_fail_count >= CAMERA_FPS:
                    logger.info("[CAM] _cam_thread_loop() — too many failures, scheduling reconnect")
                    self._cam_fail_count = 0
                    self._cam_reader_running = False
                    QTimer.singleShot(0, self._trigger_reconnect)
                    break

        cap.release()
        logger.info("[CAM] _cam_thread_loop() — thread exiting")

    def stop_camera(self):
        """Cleanly stop the camera reader thread and release the device.

        MUST be called on shutdown BEFORE the widget/process is torn down. The
        reader thread calls cap.read() at ~30fps on a background thread; if the
        process starts tearing down MSMF/Qt underneath a live read gets a
        Windows access violation (see crash at _cam_thread_loop cap.read()).
        Signal the loop to stop, join the thread (it releases its own cap on
        exit), then release defensively in case the thread never started."""
        logger.info("[CAM] stop_camera() — signalling reader thread to stop")
        self._cam_reader_running = False
        t = self._cam_reader_thread
        if t and t.is_alive():
            t.join(timeout=2.0)
            if t.is_alive():
                logger.warning("[CAM] stop_camera() — reader thread did not exit within timeout")
        self._cam_reader_thread = None
        try:
            if self._cap.isOpened():
                self._cap.release()
        except Exception as e:
            logger.warning(f"[CAM] stop_camera() — cap release error: {e}")
        logger.info("[CAM] stop_camera() — done")

    def _on_camera_ready(self):
        """Called on main thread after camera opens. Reapplies slider settings.
        Does NOT start the display timer — set_live_mode already did that."""
        import threading as _t
        logger.info(f"[CAM] _on_camera_ready() — thread={_t.current_thread().name}")
        self._reapply_cam_settings()
        logger.info("[CAM] _on_camera_ready() — done")

    def _trigger_reconnect(self):
        """Main-thread callback: camera thread detected failure. Clear stale state
        and start a fresh camera thread."""
        logger.info("[CAM] _trigger_reconnect() — clearing stale cap, restarting camera thread")
        with self._latest_frame_lock:
            self._latest_frame = None
        self._cap = cv2.VideoCapture()
        if hasattr(self, '_first_display_logged'):
            del self._first_display_logged
        self._spinner.start("Reconnecting camera…")
        if not self._frame_timer.isActive():
            self._frame_timer.start(int(1000 / CAMERA_FPS))
        self._opening_camera = True
        self._cam_reader_running = True
        self._cam_reader_thread = threading.Thread(
            target=self._cam_thread_loop, daemon=True, name="CameraThread"
        )
        self._cam_reader_thread.start()

    def _reapply_cam_settings(self):
        logger.info("[CAM] _reapply_cam_settings() — start")
        for prop, (slider, _) in self._cam_prop_sliders.items():
            self._cap.set(prop, slider.value())
        auto = self._autofocus_cb.isChecked()
        self._cap.set(cv2.CAP_PROP_AUTOFOCUS, 1 if auto else 0)
        if not auto:
            self._cap.set(cv2.CAP_PROP_FOCUS, self._focus_slider.value())
        logger.info("[CAM] _reapply_cam_settings() — done")

    def _update_frame(self):
        """Display timer callback — main thread only. Never calls cap.read().
        Auto-stops spinner when the first frame arrives from the camera thread."""
        if self.preview_mode != "live":
            return
        with self._latest_frame_lock:
            frame = self._latest_frame
        if frame is None:
            return  # camera thread not ready yet; spinner is still showing
        if not hasattr(self, '_first_display_logged'):
            self._first_display_logged = True
            logger.info("[CAM] _update_frame() — FIRST frame on screen, stopping spinner")
            self._spinner.stop()
        self._current_frame = frame
        self._display_frame(frame)

    def _display_frame(self, frame):
        fh, fw = frame.shape[:2]
        zoom   = self.zoom or 1.0
        if zoom > 1.0:
            cw = max(1, int(fw / zoom))
            ch = max(1, int(fh / zoom))
            x1 = max(0, (fw - cw) // 2)
            y1 = max(0, (fh - ch) // 2)
            src = frame[y1:y1 + ch, x1:x1 + cw]
        else:
            src = frame

        sh, sw = src.shape[:2]
        w      = self._preview_label.width()  or sw
        h      = self._preview_label.height() or sh
        scale  = min(w / sw, h / sh)
        resized = cv2.resize(src, (max(1, int(sw * scale)), max(1, int(sh * scale))))
        rgb    = cv2.cvtColor(resized, cv2.COLOR_BGR2RGB)
        qt_img = QPixmap.fromImage(ImageQt(Image.fromarray(rgb)))

        if self._focus_tap_pos is not None:
            tap_x, tap_y = self._focus_tap_pos
            pw = qt_img.width()
            ph = qt_img.height()
            ox = (w - pw) / 2
            oy = (h - ph) / 2
            ix = tap_x - ox
            iy = tap_y - oy
            painter = QPainter(qt_img)
            painter.setPen(QPen(QColor(255, 215, 0), 2))
            box = 60
            painter.drawRect(int(ix - box // 2), int(iy - box // 2), box, box)
            painter.end()

        self._preview_label.setPixmap(qt_img)

    # ------------------------------------------------------------------
    # Internal — focus tap
    # ------------------------------------------------------------------

    def _handle_focus_tap(self, x: float, y: float):
        self._focus_tap_pos = (x, y)
        self._focus_tap_timer.start()
        if self._autofocus_cb.isChecked() and self._cap.isOpened():
            self._cap.set(cv2.CAP_PROP_AUTOFOCUS, 0)
            self._cap.set(cv2.CAP_PROP_AUTOFOCUS, 1)

    def _clear_focus_tap(self):
        self._focus_tap_pos = None

    # ------------------------------------------------------------------
    # Camera settings callbacks
    # ------------------------------------------------------------------

    def _toggle_cam_settings(self, checked: bool):
        self._cam_settings_panel.setVisible(checked)
        self._cam_settings_btn.setText(
            "Camera Settings ▲" if checked else "Camera Settings ▼"
        )

    def _on_cam_prop_changed(self, prop: int, value: int, val_lbl: QLabel):
        val_lbl.setText(str(value))
        if self._cap.isOpened():
            self._cap.set(prop, value)

    def _reset_cam_prop(self, prop: int):
        default = self._cam_prop_defaults.get(prop, 128)
        slider, _ = self._cam_prop_sliders[prop]
        slider.setValue(default)

    def _on_autofocus_toggled(self, auto: bool):
        if self._cap.isOpened():
            self._cap.set(cv2.CAP_PROP_AUTOFOCUS, 1 if auto else 0)
        self._focus_slider.setEnabled(not auto)

    def _on_focus_changed(self, value: int):
        self._focus_val_lbl.setText(str(value))
        if self._cap.isOpened():
            self._cap.set(cv2.CAP_PROP_FOCUS, value)

    def _on_zoom_changed(self, value: int):
        self.zoom = max(1.0, value / 100.0)
        self._zoom_label.setText(f"Zoom: {self.zoom:.1f}x")
        # Re-draw immediately. In live mode the frame timer would eventually
        # redraw anyway, but in image mode it is stopped and the new zoom would
        # never reach the screen.
        if self._current_frame is not None:
            self._display_frame(self._current_frame)

    # ------------------------------------------------------------------
    # Event filter — tap-to-focus on preview label
    # NOTE: hasattr guard required; eventFilter fires during super().__init__
    # before this class finishes assigning _preview_label.
    # ------------------------------------------------------------------

    def eventFilter(self, obj, event):
        if hasattr(self, '_preview_label') and obj is self._preview_label:
            etype = event.type()
            # Re-fit the current frame whenever the label is resized so the
            # camera view shrinks/grows with the widget instead of overflowing.
            # Covers image mode too, where the frame timer is stopped and would
            # otherwise never re-draw.
            if etype == QEvent.Type.Resize and self._current_frame is not None:
                self._display_frame(self._current_frame)
            elif (etype == QEvent.Type.MouseButtonPress
                    and self.preview_mode == "live"):
                pos = event.position()
                self._handle_focus_tap(pos.x(), pos.y())
        return super().eventFilter(obj, event)

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_ui(self):
        outer = QVBoxLayout(self.content_widget)
        outer.setContentsMargins(6, 6, 6, 6)
        outer.setSpacing(4)

        # Preview image
        self._preview_label = QLabel()
        self._preview_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        # Ignored (not Expanding) so the label's size is driven purely by the
        # layout, never by the pixmap it holds. A QLabel reports its pixmap's
        # size as its minimum size hint, which would otherwise stop the panel
        # from shrinking below the camera frame (the image would overflow).
        # keep the frame fitted to the label in _display_frame,
        # re-fitting on every resize (see eventFilter).
        self._preview_label.setSizePolicy(
            QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Ignored
        )
        self._preview_label.setMinimumHeight(180)
        self._preview_label.setMouseTracking(True)
        # Stretch factor 1 (everything else defaults to 0) so all extra vertical
        # space goes to the preview, not the fixed-height control rows below it.
        # Without this the leftover is split between the label and the button
        # row, leaving the image small with a large empty gap beneath it.
        outer.addWidget(self._preview_label, 1)

        # Button row
        btn_row = QHBoxLayout()
        btn_row.setAlignment(Qt.AlignmentFlag.AlignCenter)
        btn_row.setSpacing(4)

        self._capture_btn      = QPushButton("Capture + Search")
        self._capture_only_btn = QPushButton("Capture Only")
        self._camera_btn       = QPushButton("Camera")
        self._search_btn       = QPushButton("Search")
        self._rotate_btn       = QPushButton("↻")
        self._crop_btn         = QPushButton("Crop")

        self._rotate_btn.setFixedWidth(32)
        self._rotate_btn.setToolTip("Rotate 90° clockwise")
        self._crop_btn.setFixedWidth(60)

        self._capture_btn.clicked.connect(self.capture_image)
        self._capture_only_btn.clicked.connect(self.capture_image_only)
        self._camera_btn.clicked.connect(self.show_camera)
        self._search_btn.clicked.connect(self.search_image)
        self._rotate_btn.clicked.connect(self._rotate_image)
        self._crop_btn.clicked.connect(self._open_crop_dialog)

        for btn in (
            self._capture_btn, self._capture_only_btn, self._camera_btn,
            self._search_btn, self._rotate_btn, self._crop_btn,
        ):
            btn_row.addWidget(btn)

        outer.addLayout(btn_row)

        # Zoom row
        zoom_row = QHBoxLayout()
        zoom_row.setAlignment(Qt.AlignmentFlag.AlignRight)
        self._zoom_label = QLabel("Zoom: 1.0x")
        self._zoom_slider = QSlider(Qt.Orientation.Horizontal)
        self._zoom_slider.setRange(100, 400)
        self._zoom_slider.setValue(100)
        self._zoom_slider.setSingleStep(10)
        self._zoom_slider.setFixedWidth(180)
        self._zoom_slider.valueChanged.connect(self._on_zoom_changed)
        zoom_row.addWidget(self._zoom_label)
        zoom_row.addWidget(self._zoom_slider)
        outer.addLayout(zoom_row)

        # Spinner
        self._spinner = SpinnerWidget(QColor(90, 170, 255), self.content_widget)
        outer.addWidget(self._spinner)

        # Camera settings (collapsible)
        self._cam_settings_btn = QPushButton("Camera Settings ▼")
        self._cam_settings_btn.setCheckable(True)
        self._cam_settings_btn.setChecked(False)
        self._cam_settings_btn.clicked.connect(self._toggle_cam_settings)
        outer.addWidget(self._cam_settings_btn)

        self._cam_settings_panel = QGroupBox()
        self._cam_settings_panel.setFlat(True)
        cam_grid = QGridLayout(self._cam_settings_panel)
        cam_grid.setContentsMargins(4, 4, 4, 4)
        cam_grid.setSpacing(4)

        for row, (name, prop) in enumerate(_CAM_PROPS):
            initial = self._cap.get(prop)
            if initial < 0 or initial != initial:
                initial = 128
            initial = int(initial)
            self._cam_prop_defaults[prop] = initial

            lbl = QLabel(name)
            lbl.setFixedWidth(68)

            slider = QSlider(Qt.Orientation.Horizontal)
            slider.setRange(0, 255)
            slider.setValue(initial)

            val_lbl = QLabel(str(initial))
            val_lbl.setFixedWidth(28)
            val_lbl.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)

            reset_btn = QPushButton("↺")
            reset_btn.setFixedWidth(24)
            reset_btn.setToolTip(f"Reset {name}")
            reset_btn.clicked.connect(lambda _, p=prop: self._reset_cam_prop(p))

            slider.valueChanged.connect(
                lambda v, p=prop, vl=val_lbl: self._on_cam_prop_changed(p, v, vl)
            )

            cam_grid.addWidget(lbl,       row, 0)
            cam_grid.addWidget(slider,    row, 1)
            cam_grid.addWidget(val_lbl,   row, 2)
            cam_grid.addWidget(reset_btn, row, 3)

            self._cam_prop_sliders[prop] = (slider, val_lbl)

        # Focus row
        focus_row = len(_CAM_PROPS)
        cam_grid.addWidget(QLabel("Focus"), focus_row, 0)

        self._autofocus_cb = QCheckBox("Auto")
        self._autofocus_cb.setChecked(True)
        self._autofocus_cb.toggled.connect(self._on_autofocus_toggled)
        cam_grid.addWidget(self._autofocus_cb, focus_row, 1, 1, 3)

        focus_initial = self._cap.get(cv2.CAP_PROP_FOCUS)
        if focus_initial < 0 or focus_initial != focus_initial:
            focus_initial = 0
        focus_initial = int(focus_initial)

        self._focus_slider = QSlider(Qt.Orientation.Horizontal)
        self._focus_slider.setRange(0, 255)
        self._focus_slider.setValue(focus_initial)
        self._focus_slider.setEnabled(False)
        self._focus_slider.valueChanged.connect(self._on_focus_changed)

        self._focus_val_lbl = QLabel(str(focus_initial))
        self._focus_val_lbl.setFixedWidth(28)
        self._focus_val_lbl.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )

        focus_sub = QWidget()
        fs_layout = QHBoxLayout(focus_sub)
        fs_layout.setContentsMargins(0, 0, 0, 0)
        fs_layout.setSpacing(4)
        fs_layout.addWidget(self._focus_slider)
        fs_layout.addWidget(self._focus_val_lbl)

        tap_lbl = QLabel("<i>Tap preview to refocus</i>")
        tap_lbl.setStyleSheet("color: gray; font-size: 10px;")

        cam_grid.addWidget(focus_sub, focus_row + 1, 1, 1, 3)
        cam_grid.addWidget(tap_lbl,  focus_row + 2, 1, 1, 3)

        self._cam_settings_panel.setVisible(False)
        outer.addWidget(self._cam_settings_panel)
