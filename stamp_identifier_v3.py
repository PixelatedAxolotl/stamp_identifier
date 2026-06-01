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
    QGridLayout,
    QLineEdit, 
    QTextEdit, 
    QComboBox, 
    QCheckBox, 
    QListWidget, 
    QListWidgetItem,
    QSplitter,
    QSizePolicy
)
from PySide6.QtGui import QPixmap, QIcon
from PySide6.QtCore import Qt, QTimer, QSize
from PIL import Image
import base64

from db.crud import add_stamp, get_all_countries, get_stamps_by_country
from db.session import SessionLocal
from lens_controller import GoogleLensController
from colnect_controller import ColnectController
from helper_utils import (get_country_name)
from db.models import Stamp, Theme


import re
from collections import Counter
import requests

from PIL.ImageQt import ImageQt
import io
import webbrowser

# ---------------- CONFIG ----------------
IMAGE_DIR = "storage/images"
THUMB_SIZE = (120, 120)
SIDEBAR_WIDTH = THUMB_SIZE[0] + 20

# Queues for lens worker
lens_command_queue = queue.Queue()
lens_result_queue = queue.Queue()

# Queues for colnect worker (BEING IMPLEMENTED)
colnect_command_queue = queue.Queue()
colnect_result_queue = queue.Queue()


# ---------- ColnectWorker Thread ----------
class ColnectWorker(threading.Thread):
    def __init__(self, command_q, result_q):
        super().__init__(daemon=True)
        self.command_q = command_q
        self.result_q = result_q
        self.controller = None

    def run(self):
        self.controller = ColnectController()

        # Login
        try:
            self.controller.login()
            self.result_q.put({"type": "login_done", "error": None})
        except Exception as e:
            self.result_q.put({"type": "login_done", "error": str(e)})

        while True:
            command = self.command_q.get()

            if command["type"] == "shutdown":
                break

            try:
                if command["type"] == "search_stamp":
                    result = self.controller.search_stamp(command.get("scott_number"), command.get("country"))
                    self.result_q.put({"type": "search_done", "id": command.get("id"), "result": result, "error": None})

                elif command["type"] == "get_colnect_info":
                    result = self.controller.get_colnect_info()
                    self.result_q.put({"type": "get_info_done", "id": command.get("id"), "result": result, "error": None})
            except Exception as e:
                self.result_q.put({"type": f"{command['type']}_done", "id": command.get("id"), "result": None, "error": str(e)})

        # Clean up
        #try:
        self.controller.shutdown()
        #except Exception as e:
         #   self.result_q.put({"type": "shutdown_error", "error": str(e)})


# END OF COLNECTWORKER CLASS


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

        self.controller.shutdown()
# END OF LENSWORKER CLASS


# ---------- Main App ----------
class StampIdentifierApp(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Stamp Identifier")
        self.setMinimumSize(1000, 700)
        self.resize(1000, 650)

        os.makedirs(IMAGE_DIR, exist_ok=True)

        # Start LensWorker
        self.lens_worker = LensWorker(lens_command_queue, lens_result_queue)
        self.lens_worker.start()

        # Start ColnectWorker (BEING IMPLEMENTED)
        self.colnect_worker = ColnectWorker(colnect_command_queue, colnect_result_queue)
        self.colnect_worker.start()

        # Camera
        self.cap = cv2.VideoCapture(1, cv2.CAP_DSHOW)
        #self.cap = cv2.VideoCapture(0)
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
        self.populate_countries()
        self.set_live_mode()

        # Timer for updating camera preview
        self.timer = QTimer()
        self.timer.timeout.connect(self.update_frame)
        self.timer.start(30)  # ~33 FPS



        # Poll Lens results
        self.poll_lens_results()
        self.poll_colnect_results()
        self.load_themes()

    # ---------- Layout ----------
    def _build_layout(self):
        # =========================================================
        # ROOT: splitter is the top-level UI
        # =========================================================
        self.main_splitter = QSplitter(Qt.Horizontal)

        root_layout = QHBoxLayout(self)
        root_layout.setContentsMargins(0, 0, 0, 0)
        root_layout.addWidget(self.main_splitter)
        self.setLayout(root_layout)

        # =========================================================
        # LEFT: Database Sidebar (resizable)
        # =========================================================
        self.db_container = QScrollArea()
        self.db_container.setWidgetResizable(True)
        self.db_container.setMinimumWidth(180)
        self.db_container.setMaximumWidth(400)

        self.db_inner = QWidget()
        self.db_layout = QVBoxLayout(self.db_inner)
        self.db_layout.setAlignment(Qt.AlignTop)

        db_title = QLabel("Database")
        db_title.setStyleSheet("font-weight: bold;")
        self.db_layout.addWidget(db_title)

        self.db_list = QListWidget()
        self.db_list.setIconSize(QSize(64, 64))
        self.db_list.setSpacing(8)
        self.db_list.setWordWrap(True)
        self.db_layout.addWidget(self.db_list)

        self.db_container.setWidget(self.db_inner)
        self.db_list.itemClicked.connect(self.on_db_item_clicked)

        self.main_splitter.addWidget(self.db_container)

        # =========================================================
        # RIGHT: Main Content Container (grid)
        # =========================================================
        self.content_container = QWidget()
        self.content_layout = QGridLayout(self.content_container)
        self.content_layout.setContentsMargins(6, 6, 6, 6)

        self.main_splitter.addWidget(self.content_container)
        self.main_splitter.setSizes([240, 900])

        # =========================================================
        # Preview
        # =========================================================
        self.preview_label = QLabel()
        self.preview_label.setAlignment(Qt.AlignCenter)
        self.preview_label.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.preview_label.setMaximumHeight(400)
        self.content_layout.addWidget(self.preview_label, 0, 0, 1, 2)

        # =========================================================
        # History Sidebar (right side)
        # =========================================================
        self.history_container = QScrollArea()
        self.history_container.setWidgetResizable(True)
        self.history_container.setFixedWidth(SIDEBAR_WIDTH)

        self.history_inner = QWidget()
        self.history_layout = QVBoxLayout(self.history_inner)
        self.history_layout.setAlignment(Qt.AlignTop)
        self.history_container.setWidget(self.history_inner)

        self.history_layout.addWidget(QLabel("History"))
        self.content_layout.addWidget(self.history_container, 0, 2, 3, 1)

        # =========================================================
        # Controls
        # =========================================================
        controls_layout = QHBoxLayout()

        self.capture_btn = QPushButton("Capture Image")
        self.capture_btn.setFixedWidth(120)
        self.capture_btn.clicked.connect(self.capture_image)

        self.camera_btn = QPushButton("Camera")
        self.camera_btn.clicked.connect(self.show_camera)

        self.search_btn = QPushButton("Search")
        self.search_btn.clicked.connect(self.search_image)

        controls_layout.addWidget(self.capture_btn)
        controls_layout.addWidget(self.camera_btn)
        controls_layout.addWidget(self.search_btn)

        self.content_layout.addLayout(controls_layout, 1, 0, 1, 2)

        # =========================================================
        # Results Panel
        # =========================================================
        self.results_container = QScrollArea()
        self.results_container.setWidgetResizable(True)
        self.results_container.setFixedHeight(THUMB_SIZE[1] + 60)

        self.results_inner = QWidget()
        self.results_layout = QHBoxLayout(self.results_inner)
        self.results_layout.setAlignment(Qt.AlignLeft)
        self.results_container.setWidget(self.results_inner)

        self.content_layout.addWidget(self.results_container, 2, 0, 1, 2)

        # =========================================================
        # Info Panel (Themes + Fields)
        # =========================================================
        self.info_panel = QWidget()
        self.info_panel.setMinimumHeight(300)

        info_layout = QHBoxLayout(self.info_panel)

        # ---------- LEFT: Themes ----------
        themes_layout = QVBoxLayout()

        self.scott_panel = QLabel("Most frequent Scott #: None")
        self.scott_panel.setFixedWidth(200)
        themes_layout.addWidget(self.scott_panel)

        themes_layout.addWidget(QLabel("Themes:"))

        input_layout = QHBoxLayout()
        self.theme_input = QLineEdit()
        self.theme_input.setPlaceholderText("Add new theme...")
        self.add_theme_btn = QPushButton("Add Theme")
        self.add_theme_btn.clicked.connect(self.add_theme)

        input_layout.addWidget(self.theme_input)
        input_layout.addWidget(self.add_theme_btn)
        themes_layout.addLayout(input_layout)

        self.themes_input = QListWidget()
        self.themes_input.setSelectionMode(QListWidget.MultiSelection)
        themes_layout.addWidget(self.themes_input)

        info_layout.addLayout(themes_layout, 1)

        # ---------- RIGHT: Stamp Fields ----------
        fields_layout = QGridLayout()
        fields_layout.setHorizontalSpacing(10)
        fields_layout.setVerticalSpacing(2)

        # Row 0
        fields_layout.addWidget(QLabel("Title:"), 0, 0)
        self.title_input = QLineEdit()
        fields_layout.addWidget(self.title_input, 0, 1)

        fields_layout.addWidget(QLabel("Scott #:"), 0, 2)
        self.scott_input = QLineEdit()
        self.scott_input.setFixedWidth(50)
        fields_layout.addWidget(self.scott_input, 0, 3)

        fields_layout.addWidget(QLabel("Country:"), 0, 4)
        self.country_input = QLineEdit()
        self.country_input.editingFinished.connect(lambda: 
                self.country_input.setText(
                get_country_name(self.country_input.text().strip())
                or self.country_input.text()))
        fields_layout.addWidget(self.country_input, 0, 5)

        fields_layout.addWidget(QLabel("Series:"), 0, 6)
        self.series_input = QLineEdit()
        fields_layout.addWidget(self.series_input, 0, 7)

        # Row 1
        fields_layout.addWidget(QLabel("Emission:"), 1, 0)
        self.emission_input = QLineEdit()
        fields_layout.addWidget(self.emission_input, 1, 1)

        fields_layout.addWidget(QLabel("Face Value:"), 1, 2)
        self.face_value_input = QLineEdit()
        self.face_value_input.setFixedWidth(50)
        fields_layout.addWidget(self.face_value_input, 1, 3)

        fields_layout.addWidget(QLabel("Issued:"), 1, 4)
        self.issued_input = QLineEdit()
        fields_layout.addWidget(self.issued_input, 1, 5)

        fields_layout.addWidget(QLabel("Expired:"), 1, 6)
        self.expired_input = QLineEdit()
        fields_layout.addWidget(self.expired_input, 1, 7)

        # Row 2
        fields_layout.addWidget(QLabel("Size:"), 2, 0)
        self.size_input = QLineEdit()
        fields_layout.addWidget(self.size_input, 2, 1)

        fields_layout.addWidget(QLabel("Perforation:"), 2, 2)
        self.perforation_input = QLineEdit()
        self.perforation_input.setFixedWidth(50)
        fields_layout.addWidget(self.perforation_input, 2, 3)

        fields_layout.addWidget(QLabel("Paper:"), 2, 4)
        self.paper_input = QLineEdit()
        fields_layout.addWidget(self.paper_input, 2, 5)

        fields_layout.addWidget(QLabel("Gum:"), 2, 6)
        self.gum_input = QLineEdit()
        fields_layout.addWidget(self.gum_input, 2, 7)

        # Row 3
        fields_layout.addWidget(QLabel("Watermark:"), 3, 0)
        self.watermark_input = QLineEdit()
        fields_layout.addWidget(self.watermark_input, 3, 1)

        fields_layout.addWidget(QLabel("Printing:"), 3, 2)
        self.printing_input = QLineEdit()
        fields_layout.addWidget(self.printing_input, 3, 3)

        fields_layout.addWidget(QLabel("Format:"), 3, 4)
        self.format_input = QLineEdit()
        fields_layout.addWidget(self.format_input, 3, 5)

        fields_layout.addWidget(QLabel("Print Run:"), 3, 6)
        self.print_run_input = QLineEdit()
        self.print_run_input.setFixedWidth(50)
        fields_layout.addWidget(self.print_run_input, 3, 7)

        #Row 4
        fields_layout.addWidget(QLabel("Colors:"), 4, 0)
        self.colors_input = QLineEdit()
        fields_layout.addWidget(self.colors_input, 4, 1)

        fields_layout.addWidget(QLabel("Designers:"), 4, 2)
        self.designers_input = QLineEdit()
        fields_layout.addWidget(self.designers_input, 4, 3)

        fields_layout.addWidget(QLabel("Variants:"), 4, 4)
        self.variants_checkbox = QCheckBox()
        fields_layout.addWidget(self.variants_checkbox, 4, 5)

        fields_layout.addWidget(QLabel("Description:"), 4, 6)
        self.description_input = QLineEdit()
        fields_layout.addWidget(self.description_input, 4, 7, 1, 2)


        # Row 5: Save button
        self.save_btn = QPushButton("Add Stamp to Database")
        fields_layout.addWidget(self.save_btn, 5, 0, 1, 2)
        self.save_btn.clicked.connect(self.save_stamp_to_db)

        #info_layout.addWidget(self.info_panel, 5, 1, 1, 2)

        self.search_colnect_btn = QPushButton("Search Colnect")
        fields_layout.addWidget(self.search_colnect_btn, 5, 2, 1, 2)
        self.search_colnect_btn.clicked.connect(lambda: self.search_colnect(self.scott_input.text(), self.country_input.text()))
        #self.main_layout.addWidget(self.info_panel, 5, 0, 1, 2)

        self.get_colnect_info_btn = QPushButton("Get Colnect Info")
        fields_layout.addWidget(self.get_colnect_info_btn, 5, 4, 1, 2)
        self.get_colnect_info_btn.clicked.connect(self.get_stamp_info)
        #info_layout.addWidget(self.info_panel, 5, 1, 1, 2)

        # Add the fields panel to the right side of main layout
        info_layout.addLayout(fields_layout, 3)

        self.content_layout.addWidget(self.info_panel, 3, 0, 1, 3)
        # Column behavior
        self.content_layout.setColumnStretch(0, 1)
        self.content_layout.setColumnStretch(1, 1)
        self.content_layout.setColumnStretch(2, 0)  # history sidebar fixed

        # Row behavior (MOST IMPORTANT)
        self.content_layout.setRowStretch(0, 2)  # preview grows
        self.content_layout.setRowStretch(1, 0)  # controls fixed
        self.content_layout.setRowStretch(2, 0)  # results fixed
        self.content_layout.setRowStretch(3, 1)  # info panel grows



    def save_stamp_to_db(self):
        print(self.current_image_path)
        if not self.current_image_path:
            print("No image captured to save")
            return

        # Collect input
        stamp_data = {
            "title": self.title_input.text(),
            "scott_number": self.scott_input.text(),
            "country": self.country_input.text(),
            "series": self.series_input.text(),
            "emission": self.emission_input.text(),
            "face_value": self.face_value_input.text(),
            "issued_date": self.issued_input.text(),
            "expired_date": self.expired_input.text(),
            "size": self.size_input.text(),
            "perforation": self.perforation_input.text(),
            "paper": self.paper_input.text(),
            "gum": self.gum_input.text(),
            "watermark": self.watermark_input.text(),
            "printing": self.printing_input.text(),
            "format": self.format_input.text(),
            "print_run": self.print_run_input.text(),
            "colors": self.colors_input.text(),
            "designers": self.designers_input.text(),
            "description": self.description_input.text(),
            "variants": self.variants_checkbox.isChecked(),
            "themes": [item.text() for item in self.themes_input.selectedItems()],
            "image_path": self.current_image_path
        }
        session = SessionLocal()
        try:
                new_stamp = add_stamp(session, **stamp_data)
                session.commit()
                print(f"Stamp '{new_stamp.title}' added to database successfully.")

                self.populate_countries()
                #reset inputs
                self._reset_stamp_form()
        except Exception as e:
            session.rollback()
            print("Failed to add stamp:", e)

        finally:
            session.close()


    def _reset_stamp_form(self):
        self.title_input.clear()
        self.scott_input.clear()
        self.country_input.clear()
        self.series_input.clear()
        self.emission_input.clear()
        self.face_value_input.clear()
        self.issued_input.clear()
        self.expired_input.clear()
        self.size_input.clear()
        self.perforation_input.clear()
        self.paper_input.clear()
        self.gum_input.clear()
        self.watermark_input.clear()
        self.printing_input.clear()
        self.format_input.clear()
        self.print_run_input.clear()
        self.colors_input.clear()
        self.designers_input.clear()
        self.description_input.clear()

        self.variants_checkbox.setChecked(False)

        for i in range(self.themes_input.count()):
            self.themes_input.item(i).setSelected(False)

        self.current_image_path = None


    def add_theme(self, theme=None):
        session = SessionLocal()
        print ("Adding theme...")
        #Add a new theme to the list, if it doesn’t already exist, and select it.
        new_theme = self.theme_input.text().strip()
        if not new_theme:
            if theme is not None:
                new_theme = theme
            else:
                return  # Ignore empty input
        

        print ("New theme to add:", new_theme)
        # Check if the theme already exists in the list
        for i in range(self.themes_input.count()):
            if self.themes_input.item(i).text().lower() == new_theme.lower():
                # Theme already exists, just select it
                self.themes_input.item(i).setSelected(True)
                self.theme_input.clear()
                return

        # Check if the theme already exists in the database
        theme_exists = session.query(Theme).filter(Theme.name.ilike(new_theme)).first()
        if not theme_exists:
            # If the theme doesn't exist in the database, insert it
            theme = Theme(name=new_theme)
            session.add(theme)
            session.commit()

        # Add the theme to the UI list and select it
        self.themes_input.addItem(new_theme)
        self.themes_input.item(self.themes_input.count() - 1).setSelected(True)
        self.theme_input.clear()




    def load_themes(self):
        session = SessionLocal()
        self.themes_input.clear()
        try:
            themes = session.query(Theme).order_by(Theme.name).all()
        finally:
            session.close()
        for theme in themes:
            item = QListWidgetItem(theme.name)
            self.themes_input.addItem(item)





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
        #self.clear_results()

    def show_camera(self):
        self.set_live_mode()

    def search_image(self):
        # Clear previous results
        self.scott_panel.setText(f"Most frequent Scott number:")

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

    def search_colnect(self, scott_number, country):
        # Increment request counter and send search command
        self.request_counter += 1
        request_id = self.request_counter

        colnect_command_queue.put({
            "type": "search_stamp",
            "id": request_id,
            "scott_number": scott_number,
            "country": country
        })

    def get_stamp_info(self):
        self.request_counter += 1
        request_id = self.request_counter

        colnect_command_queue.put({
            "type": "get_colnect_info",
            "id": request_id
        })

    def fill_stamp_info(self, info):
        
        for i in range(self.themes_input.count()):
            self.themes_input.item(i).setSelected(False)

        # Fill the input fields with the retrieved info
        self.title_input.setText(info.get("name", ""))
        self.scott_input.setText(info.get("scott_number", ""))
        #self.country_input.setText(info.get("country", ""))
        self.series_input.setText(info.get("series", ""))
        self.emission_input.setText(info.get("emission", ""))
        self.face_value_input.setText(info.get("face_value", ""))
        self.issued_input.setText(info.get("issued_date", ""))
        self.expired_input.setText(info.get("expired_date", ""))
        self.size_input.setText(info.get("size", ""))
        self.perforation_input.setText(info.get("perforation", ""))
        self.paper_input.setText(info.get("paper", ""))
        self.gum_input.setText(info.get("gum", ""))
        self.watermark_input.setText(info.get("watermark", ""))
        self.printing_input.setText(info.get("printing", ""))
        self.format_input.setText(info.get("format", ""))
        self.print_run_input.setText(info.get("print_run", ""))
        self.colors_input.setText(info.get("colors", ""))
        self.designers_input.setText(info.get("designers", ""))
        self.description_input.setText(info.get("description", ""))
        self.variants_checkbox.setChecked(bool(info.get("variants", False)))

        for theme in info.get("themes", []):
            print ("Adding theme from Colnect info:", theme)
            self.add_theme(theme)

    def render_results(self, results):
        # Clear previous results
        for i in reversed(range(self.results_layout.count())):
            item = self.results_layout.itemAt(i)
            widget = item.widget()
            if widget:
                widget.setParent(None)

        scott_numbers = []  # temporary list

        # Handle no results
        if not results:
            placeholder = QLabel("No visual matches found.")
            self.results_layout.addWidget(placeholder)
            self.scott_panel.setText("Most frequent Scott #: None")
            return

        for item in results:
            try:
                thumb_url = item["thumbnail_url"]

                # Load image
                if thumb_url.startswith("data:"):
                    header, encoded = thumb_url.split(",", 1)
                    thumb_data = base64.b64decode(encoded)
                else:
                    thumb_data = requests.get(thumb_url, timeout=10).content

                img = Image.open(io.BytesIO(thumb_data))
                qt_img = QPixmap.fromImage(ImageQt(img)).scaled(
                    THUMB_SIZE[0], THUMB_SIZE[1], Qt.KeepAspectRatio, Qt.SmoothTransformation
                )

                # Create button
                btn = QToolButton()
                btn.setIcon(qt_img)
                btn.setIconSize(qt_img.size())
                btn.setText(item.get("title", ""))
                btn.setToolButtonStyle(Qt.ToolButtonTextUnderIcon)
                btn.clicked.connect(lambda checked, l=item["link"]: webbrowser.open(l))
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

                # msg["results"] is all raw results
                most_common = self.lens_worker.controller.extract_scott_numbers(msg["results"])
                if most_common:
                    self.scott_panel.setText(f"Most frequent Scott number: {most_common}")

                    if not self.scott_input.text():
                        self.scott_input.setText(most_common)

                else:
                    self.scott_panel.setText("Most frequent Scott number: N/A")

                # Now filter results for display
                filtered_results = self.lens_worker.controller.filter_results_by_domain(msg["results"])
                self.render_results(filtered_results)

        except queue.Empty:
            pass
        QTimer.singleShot(100, self.poll_lens_results)



    def poll_colnect_results(self):
        try:
            while True:
                msg = colnect_result_queue.get_nowait()
                if msg["type"] == "login_done":
                    if msg["error"]:
                        print("Colnect login failed:", msg["error"])
                    else:
                        print("Colnect login successful")
                
                elif msg["type"] == "search_done":
                    if msg["error"]:
                        print("Colnect search failed:", msg["error"])
                        continue
                elif msg["type"] == "get_info_done":
                    if msg["error"]:
                        print("Colnect get info failed:", msg["error"])
                        continue

                    # msg["result"] is a dictionary of stamp info
                    self.fill_stamp_info(msg["result"])
        except queue.Empty:
            pass
        QTimer.singleShot(100, self.poll_colnect_results)





### Display from Database ###

    def populate_countries(self):
        session = SessionLocal()
        self.db_list.clear()
        try:
            countries = get_all_countries(session)
            for country in countries:
                item = QListWidgetItem(country)
                item.setData(Qt.UserRole, {"type": "country", "country": country})
                self.db_list.addItem(item)
        except Exception as e:
            session.rollback()
            print("Failed to fetch and populate countries:", e)

        finally:
            session.close()

    def on_db_item_clicked(self, item):
        payload = item.data(Qt.UserRole)
        if not payload:
            return

        if payload.get("type") == "country":
            self.populate_stamps_for_country(payload["country"])



    def populate_stamps_for_country(self, country: str):
        self.db_list.clear()
        session = SessionLocal()
        stamps = get_stamps_by_country(session, country)

        try:
            for stamp in stamps:
                title = stamp["title"] or "(Untitled)"
                series = stamp["series"] or ""
                label = f"{title} — {series}" if series else title

                item = QListWidgetItem(label)

                if stamp["image_path"]:
                    item.setIcon(QIcon(stamp["image_path"]))

                item.setData(Qt.UserRole, {
                    "type": "stamp",
                    "stamp_id": stamp["id"],
                    "country": country,
                    **stamp
                })

                self.db_list.addItem(item)
        except Exception as e:
            session.rollback()
            print("Failed to populate stamps for country:", e)
        finally:
            session.close()

        # Optional: add a "Back" item
        back_item = QListWidgetItem("← Back to countries")
        back_item.setData(Qt.UserRole, {"type": "back"})
        self.db_list.insertItem(0, back_item)


    def on_db_item_clicked(self, item):
        payload = item.data(Qt.UserRole)
        if not payload:
            return

        match payload.get("type"):
            case "country":
                self.populate_stamps_for_country(payload["country"])

            case "stamp":
                self.load_stamp_into_editor(payload["stamp_id"])

            case "back":
                self.populate_countries()


    def load_stamp_into_editor(self, stamp_id: int):
        session = SessionLocal()
        try:
            stamp = session.get(Stamp, stamp_id)

            self.title_input.setText(stamp.title or "")
            self.country_input.setText(stamp.country or "")
            self.series_input.setText(stamp.series or "")
            self.scott_input.setText(stamp.scott_number or "")
            self.emission_input.setText(stamp.emission or "")
            self.face_value_input.setText(stamp.face_value or "")
            self.issued_input.setText(stamp.issued_date.strftime("%Y-%m-%d") if stamp.issued_date else "")
            self.expired_input.setText(stamp.expired_date.strftime("%Y-%m-%d") if stamp.expired_date else "")
            self.size_input.setText(stamp.size or "")
            self.perforation_input.setText(stamp.perforation or "")
            self.paper_input.setText(stamp.paper or "")
            self.gum_input.setText(stamp.gum or "")
            self.watermark_input.setText(stamp.watermark or "")
            self.printing_input.setText(stamp.printing or "")
            self.format_input.setText(stamp.format or "")
            self.print_run_input.setText(stamp.print_run or "")
            self.colors_input.setText(stamp.colors or "")
            self.designers_input.setText(stamp.designers or "")
            self.description_input.setText(stamp.description or "")
            self.variants_checkbox.setChecked(stamp.variants or False)
            
            # Load themes
            stamp_theme_names = {
                theme.name for theme in (stamp.themes or [])
            }

            for i in range(self.themes_input.count()):
                item = self.themes_input.item(i)
                item.setSelected(item.text() in stamp_theme_names)

            # Load image
            if stamp.image_path and os.path.exists(stamp.image_path):
                self.show_image(stamp.image_path)
            else:
                self.current_image_path = None
                print("Stamp image not found.")
            
                



        except Exception as e:
            print(f"Error loading stamp {stamp_id}: {e}")
            return
        finally:
            session.close()










    # ---------- Close ----------
def closeEvent(self, event):
    print("Shutting down StampIdentifierApp cleanly...")

    # --- stop Colnect worker ---
    if self.colnect_worker:
        self.colnect_worker.shutdown()
        self.colnect_worker.wait(5000)  # QThread-safe

    # --- stop Lens worker ---
    if self.lens_worker:
        self.lens_worker.shutdown()
        self.lens_worker.wait(5000)

    # --- release camera ---
    if self.cap and self.cap.isOpened():
        self.cap.release()

    event.accept()




# ---------- Run App ----------
if __name__ == "__main__":
    import sys
    from PIL.ImageQt import ImageQt

    app = QApplication(sys.argv)
    window = StampIdentifierApp()
    window.show()
    sys.exit(app.exec())
