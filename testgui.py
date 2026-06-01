import sys
import cv2

from PySide6.QtWidgets import (
    QApplication,
    QMainWindow,
    QWidget,
    QLabel,
    QVBoxLayout,
    QHBoxLayout,
    QPushButton,
    QScrollArea,
)
from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QImage, QPixmap
import os
from datetime import datetime
import cv2

IMAGE_DIR = "storage/images"
THUMB_SIZE = (120, 120)
SIDEBAR_WIDTH = THUMB_SIZE[0] + 20
os.makedirs(IMAGE_DIR, exist_ok=True)


class StampIdentifierApp(QMainWindow):
    def __init__(self):
        super().__init__()

        self.setWindowTitle("Stamp Identifier")
        self.resize(1000, 650)
        self.setMinimumSize(900, 600)

        self.preview_mode = "live"  # or "image"
        self.current_frame = None
        self.current_image_path = None


        # ---- Central widget ----
        central = QWidget(self)
        self.setCentralWidget(central)

        layout = QVBoxLayout(central)

        # ---- Preview label ----
        self.preview_label = QLabel(self)
        self.preview_label.setAlignment(Qt.AlignCenter)
        self.preview_label.setStyleSheet("background-color: black;")
        layout.addWidget(self.preview_label, stretch=1)

        # ---- OpenCV Camera ----
        self.cap = cv2.VideoCapture(1)
        if not self.cap.isOpened():
            raise RuntimeError("Could not open webcam")

        # ---- Timer ----
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.update_frame)
        self.timer.start(15)

        # ---- Controls ----
        control_layout = QHBoxLayout()
        layout.addLayout(control_layout)

        self.capture_btn = QPushButton("Capture Image")
        self.capture_btn.clicked.connect(self.capture_image)
        control_layout.addWidget(self.capture_btn)

        self.camera_btn = QPushButton("Camera")
        self.camera_btn.clicked.connect(self.show_camera)
        self.camera_btn.setVisible(False)
        control_layout.addWidget(self.camera_btn)

        self.search_btn = QPushButton("Search")
        self.search_btn.setVisible(False)
        control_layout.addWidget(self.search_btn)

        # ---------- History Sidebar ----------
        self.history_container = QScrollArea()
        self.history_container.setFixedWidth(SIDEBAR_WIDTH)
        self.history_container.setWidgetResizable(True)

        self.history_inner = QWidget()
        self.history_layout = QVBoxLayout(self.history_inner)
        self.history_layout.setAlignment(Qt.AlignTop)
        self.history_container.setWidget(self.history_inner)

        # Add label at top
        title = QLabel("History")
        self.history_layout.addWidget(title)

        # Add sidebar to main layout
        self.main_layout.addWidget(self.history_container, 0, 1)

        # Hover tracking (optional)
        self.history_container.enterEvent = lambda event: setattr(self, "_hovering_history", True)
        self.history_container.leaveEvent = lambda event: setattr(self, "_hovering_history", False)
    
    #END INIT


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


    def update_frame(self):
        if self.preview_mode == "live":
            ret, frame = self.cap.read()
            if ret:
                self.current_frame = frame
                self.display_frame(frame)


    def display_frame(self, frame):
        h, w, ch = frame.shape
        bytes_per_line = ch * w

        # Convert BGR → RGB
        rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)

        qimg = QImage(
            rgb.data,
            w,
            h,
            bytes_per_line,
            QImage.Format_RGB888
        )

        pixmap = QPixmap.fromImage(qimg)

        # Scale to label size while preserving aspect ratio
        pixmap = pixmap.scaled(
            self.preview_label.size(),
            Qt.KeepAspectRatio,
            Qt.SmoothTransformation
        )

        self.preview_label.setPixmap(pixmap)

    def closeEvent(self, event):
        self.cap.release()
        event.accept()

    def capture_image(self):
        if self.current_frame is None:
            return

        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = os.path.join(IMAGE_DIR, f"stamp_{ts}.jpg")

        # Save image
        cv2.imwrite(path, self.current_frame)

        # Freeze preview
        self.current_image_path = path
        self.set_image_mode()

    def show_camera(self):
        """Switch back to live camera preview."""
        self.set_live_mode()

    def show_image(self, path=None):
        """Display a captured image."""
        if path:
            self.current_image_path = path
        if not self.current_image_path:
            return
        self.set_image_mode()


    # Toggle modes
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

        # Display captured image
        image = cv2.imread(self.current_image_path)
        if image is not None:
            self.display_frame(image)
        




def main():
    app = QApplication(sys.argv)
    window = StampIdentifierApp()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
