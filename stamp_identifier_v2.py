import cv2
import os
import threading
import queue
from datetime import datetime
from PySide6.QtWidgets import (
    QApplication, 
    QWidget, 
    QLabel, 
    QPushButton,
    QToolButton, 
    QVBoxLayout, 
    QHBoxLayout,
    QScrollArea, 
    QGridLayout
)
from PySide6.QtGui import QPixmap
from PySide6.QtCore import Qt, QTimer
from PIL import Image
import base64
from lens_controller import GoogleLensController

# ---------------- CONFIG ----------------
IMAGE_DIR = "storage/images"
THUMB_SIZE = (120, 120)
SIDEBAR_WIDTH = THUMB_SIZE[0] + 20

# Queues for lens worker
lens_command_queue = queue.Queue()
lens_result_queue = queue.Queue()


# ---------- LensWorker Thread ----------
class LensWorker(threading.Thread):
    def __init__(self, command_q, result_q):
        super().__init__(daemon=True)
        self.command_q = command_q
        self.result_q = result_q
        self.controller = None

    def run(self):
        # IMPORTANT: Playwright is created inside this thread
        self.controller = GoogleLensController()

        while True:
            command = self.command_q.get()

            if command["type"] == "shutdown":
                break

            if command["type"] == "search":
                image_path = command["image_path"]
                request_id = command["id"]

                try:
                    results = self.controller.search(image_path)
                    self.result_q.put({
                        "id": request_id,
                        "results": results,
                        "error": None
                    })
                except Exception as e:
                    self.result_q.put({
                        "id": request_id,
                        "results": None,
                        "error": str(e)
                    })

        self.controller.close()
# END OF LENSWORKER CLASS


# ---------- Main App ----------
class StampIdentifierApp(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Stamp Identifier")
        self.setMinimumSize(900, 600)
        self.resize(1000, 650)

        os.makedirs(IMAGE_DIR, exist_ok=True)

        # Start LensWorker
        self.lens_worker = LensWorker(lens_command_queue, lens_result_queue)
        self.lens_worker.start()

        # Camera
        self.cap = cv2.VideoCapture(1)
        if not self.cap.isOpened():
            raise RuntimeError("Could not open webcam")

        self.current_frame = None
        self.current_image_path = None
        self.preview_mode = "live"

        # Hover tracking (optional)
        self._hovering_history = False

        # Request counter
        self.request_counter = 0

        # Layout
        self._build_layout()
        self.load_history()
        self.set_live_mode()

        # Timer for updating camera preview
        self.timer = QTimer()
        self.timer.timeout.connect(self.update_frame)
        self.timer.start(30)  # ~33 FPS



        # Poll Lens results
        self.poll_lens_results()

    # ---------- Layout ----------
    def _build_layout(self):
        self.main_layout = QGridLayout(self)
        self.setLayout(self.main_layout)

        # ---------- Preview ----------
        self.preview_label = QLabel()
        self.preview_label.setAlignment(Qt.AlignCenter)
        self.main_layout.addWidget(self.preview_label, 0, 0)

        # ---------- History Sidebar ----------
        self.history_container = QScrollArea()
        self.history_container.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.history_container.setFixedWidth(SIDEBAR_WIDTH)
        self.history_container.setWidgetResizable(True)

        self.history_inner = QWidget()
        self.history_layout = QVBoxLayout(self.history_inner)
        self.history_layout.setAlignment(Qt.AlignTop)
        self.history_container.setWidget(self.history_inner)

        title = QLabel("History")
        self.history_layout.addWidget(title)
        self.main_layout.addWidget(self.history_container, 0, 1)

        # Hover tracking
        self.history_container.enterEvent = lambda e: setattr(self, "_hovering_history", True)
        self.history_container.leaveEvent = lambda e: setattr(self, "_hovering_history", False)

        # ---------- Controls ----------
        self.capture_btn = QPushButton("Capture Image")
        self.capture_btn.setFixedWidth(120)  # width in pixels
        self.capture_btn.clicked.connect(self.capture_image)

        self.camera_btn = QPushButton("Camera")
        self.camera_btn.clicked.connect(self.show_camera)

        self.search_btn = QPushButton("Search")
        self.search_btn.clicked.connect(self.search_image)  # will implement later

        controls_layout = QHBoxLayout()
        controls_layout.addWidget(self.capture_btn)
        controls_layout.addWidget(self.camera_btn)
        controls_layout.addWidget(self.search_btn)
        self.main_layout.addLayout(controls_layout, 1, 0, 1, 2)

        # ---------- Results Panel ----------
        self.results_container = QScrollArea()
        self.results_container.setWidgetResizable(True)
        self.results_container.setFixedHeight(THUMB_SIZE[1] + 60)  # adjust height for title/text

        self.results_inner = QWidget()
        self.results_layout = QHBoxLayout(self.results_inner)
        self.results_layout.setAlignment(Qt.AlignLeft)
        self.results_container.setWidget(self.results_inner)

        self.main_layout.addWidget(self.results_container, 2, 0, 1, 2)

        # Enable horizontal scrolling with trackpad/mouse wheel
        def on_results_wheel(event):
            delta = event.angleDelta().y()  # vertical scroll
            self.results_container.horizontalScrollBar().setValue(
                self.results_container.horizontalScrollBar().value() - delta
            )
            event.accept()

        #self.results_container.wheelEvent = on_results_wheel



    # ---------- Camera Preview ----------
    def update_frame(self):
        if self.preview_mode == "live":
            ret, frame = self.cap.read()
            if ret:
                self.current_frame = frame
                self.display_frame(frame)

    def display_frame(self, frame):
        fh, fw, _ = frame.shape
        w = self.preview_label.width()
        h = self.preview_label.height()
        scale = min(w / fw, h / fh)
        resized = cv2.resize(frame, (int(fw * scale), int(fh * scale)))
        rgb = cv2.cvtColor(resized, cv2.COLOR_BGR2RGB)
        img = Image.fromarray(rgb)
        qt_img = QPixmap.fromImage(ImageQt(img))
        self.preview_label.setPixmap(qt_img)

    # ---------- Modes ----------
    def set_live_mode(self):
        self.preview_mode = "live"
        self.capture_btn.setVisible(True)
        self.camera_btn.setVisible(False)
        self.search_btn.setVisible(False)

    def set_image_mode(self):
        self.preview_mode = "image"
        self.capture_btn.setVisible(False)
        self.camera_btn.setVisible(True)
        self.search_btn.setVisible(True)

    # ---------- Actions ----------
    def capture_image(self):
        if self.current_frame is None:
            return
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = os.path.join(IMAGE_DIR, f"stamp_{ts}.jpg")
        cv2.imwrite(path, self.current_frame)
        self.add_history_thumbnail(path)

        self.current_image_path = path
        self.set_image_mode()
        self.display_frame(cv2.imread(path))

        self.search_image()


    def show_image(self, path):
        image = cv2.imread(path)
        if image is None:
            return
        self.current_image_path = path
        self.set_image_mode()
        self.display_frame(image)

    def show_camera(self):
        self.set_live_mode()

    def search_image(self):
        # Clear previous results
        for i in reversed(range(self.results_layout.count())):
            item = self.results_layout.itemAt(i)
            widget = item.widget()
            if widget is not None:
                widget.setParent(None)  # Removes widget from layout and deletes it

        if not self.current_image_path:
            return

        # Increment request counter and send search command
        self.request_counter += 1
        request_id = self.request_counter

        lens_command_queue.put({
            "type": "search",
            "id": request_id,
            "image_path": self.current_image_path
        })


    def render_results(self, results):
        # Clear previous results
        for i in reversed(range(self.results_layout.count())):
            item = self.results_layout.itemAt(i)
            widget = item.widget()
            if widget:
                widget.setParent(None)

        # Handle no results
        if not results:
            placeholder = QLabel("No visual matches found.")
            self.results_layout.addWidget(placeholder)
            return

        # Display each result
        for item in results:
            try:
                thumb_url = item["thumbnail_url"]

                # Load image from base64 or URL
                if thumb_url.startswith("data:"):
                    header, encoded = thumb_url.split(",", 1)
                    thumb_data = base64.b64decode(encoded)
                else:
                    import requests
                    thumb_data = requests.get(thumb_url, timeout=10).content

                from PIL.ImageQt import ImageQt
                from PySide6.QtGui import QPixmap
                import io
                from PySide6.QtWidgets import QPushButton
                import webbrowser
                from PySide6.QtCore import Qt

                img = Image.open(io.BytesIO(thumb_data))
                qt_img = QPixmap.fromImage(ImageQt(img)).scaled(
                    THUMB_SIZE[0], THUMB_SIZE[1],
                    Qt.KeepAspectRatio, Qt.SmoothTransformation
                )

                # Create button
                btn = QToolButton()
                btn.setIcon(qt_img)
                btn.setIconSize(qt_img.size())
                btn.setText(item.get("title", ""))
                btn.setToolButtonStyle(Qt.ToolButtonTextUnderIcon)
                btn.clicked.connect(lambda checked, l=item["link"]: webbrowser.open(l))

                # Add button to results layout
                self.results_layout.addWidget(btn)

            except Exception as e:
                print("Failed to render Lens result:", e)



    # ---------- History ----------
    def add_history_thumbnail(self, path):
        pixmap = QPixmap(path).scaled(
            THUMB_SIZE[0], THUMB_SIZE[1], Qt.KeepAspectRatio, Qt.SmoothTransformation
        )
        label = QLabel()
        label.setPixmap(pixmap)
        label.setCursor(Qt.PointingHandCursor)
        label.mousePressEvent = lambda e, p=path: self.show_image(p)
        self.history_layout.addWidget(label)

    def load_history(self):
        for f in sorted(os.listdir(IMAGE_DIR), reverse=True):
            if f.lower().endswith(".jpg"):
                self.add_history_thumbnail(os.path.join(IMAGE_DIR, f))

    # ---------- Lens Polling ----------
    def poll_lens_results(self):
        try:
            while True:
                msg = lens_result_queue.get_nowait()
                if msg["error"]:
                    print("Lens search failed:", msg["error"])
                    continue
                self.render_results(msg["results"])
        except queue.Empty:
            pass
        QTimer.singleShot(100, self.poll_lens_results)


    # ---------- Close ----------
    def closeEvent(self, event):
        self.cap.release()
        lens_command_queue.put({"type": "shutdown"})
        event.accept()


# ---------- Run App ----------
if __name__ == "__main__":
    import sys
    from PIL.ImageQt import ImageQt

    app = QApplication(sys.argv)
    window = StampIdentifierApp()
    window.show()
    sys.exit(app.exec())
