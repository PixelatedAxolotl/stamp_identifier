import cv2
import os
import queue
import multiprocessing as mp
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
    QComboBox,
    QSlider,
    QCheckBox,
    QListWidget,
    QListWidgetItem,
    QSplitter,
    QSizePolicy,
    QMessageBox,
    QDialog,
    QTableWidget,
    QTableWidgetItem,
    QHeaderView,
    QMenu,
    QInputDialog,
    QFrame,
    QGroupBox,
    QSpinBox,
    QRubberBand,
)
from PySide6.QtGui import QPixmap, QIcon, QPainter, QPen, QColor, QDesktopServices
from PySide6.QtCore import Qt, QTimer, QSize, QRect, QRectF, QUrl, QEvent
from PIL import Image
import base64

from db.session import SessionLocal
from db.service import StampService, ThemeService, VariantSetService, PhysicalLocationService, ImageService, StampCopyService, SeriesService
from browser_worker import AsyncBrowserWorker
from helper_utils import get_country_name, load_tag_aliases, save_tag_aliases, load_theme_implications, save_theme_implications, COUNTRY_MAP, reload_country_overrides
from db.models import Stamp, StampImage, Theme, VariantSet, Series
from logger import logger
from image_storage import associate_image, deassociate_image
from config import (
    IMAGE_DIR, INCOMING_DIR, THUMB_SIZE, SIDEBAR_WIDTH, CAMERA_INDEX,
    MIN_WINDOW_WIDTH, MIN_WINDOW_HEIGHT, DEFAULT_WINDOW_WIDTH, DEFAULT_WINDOW_HEIGHT,
    CAMERA_FPS, COUNTRY_OVERRIDES_FILE,
    PANEL_GALLERY_MIN_W, PANEL_DB_MIN_W, PANEL_DB_MAX_W,
    PANEL_CONTENT_MIN_W, PANEL_THEMES_MIN_W, PANEL_HISTORY_MIN_W,
    SPLITTER_MAIN_INIT, SPLITTER_INFO_H_INIT, SPLITTER_CONTENT_V_INIT, SPLITTER_CONTENT_H_INIT,
    FIELD_INPUT_HEIGHT,
)

GALLERY_COLS      = 3
GALLERY_CARD_W    = 160
GALLERY_WIDTH     = 570   # panel width when open
GALLERY_PAGE_SIZE = 24    # stamps per page (8 rows × 3 cols)

import math
import pycountry
import requests

from PIL.ImageQt import ImageQt
import io
import webbrowser

# Result queues — written by AsyncBrowserWorker (child process), read by Qt poll timers.
# Must be multiprocessing.Queue so data crosses the process boundary.
lens_result_queue    = mp.Queue()
colnect_result_queue = mp.Queue()


# ---------- Spinner Widget ----------
class SpinnerWidget(QWidget):
    """Rotating arc + status text. Call start(text) / stop() to control."""

    def __init__(self, color: QColor | None = None, parent=None):
        super().__init__(parent)
        self._angle = 0
        self._text  = ""
        self._color = color or QColor(90, 170, 255)
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.setInterval(30)          # ~33 fps
        self.setFixedHeight(22)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.setVisible(False)

    def start(self, text: str = ""):
        self._text = text
        self.setVisible(True)
        self._timer.start()

    def stop(self):
        self._timer.stop()
        self.setVisible(False)

    def _tick(self):
        self._angle = (self._angle + 8) % 360
        self.update()

    def paintEvent(self, _event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)

        r    = (self.height() - 4) // 2
        cx   = r + 2
        cy   = self.height() // 2
        rect = QRectF(cx - r, cy - r, r * 2, r * 2)

        # Gray track ring
        pen = QPen(QColor(220, 220, 220), 2.0)
        pen.setCapStyle(Qt.RoundCap)
        p.setPen(pen)
        p.drawEllipse(rect)

        # Colored spinning arc (270° sweep)
        pen = QPen(self._color, 2.5)
        pen.setCapStyle(Qt.RoundCap)
        p.setPen(pen)
        p.drawArc(rect, int((90 - self._angle) * 16), int(-270 * 16))

        # Status text
        if self._text:
            p.setPen(QColor(130, 130, 130))
            tx = cx + r + 7
            p.drawText(tx, 0, self.width() - tx - 2, self.height(),
                       Qt.AlignVCenter | Qt.AlignLeft, self._text)


# ---------- Tag Alias Dialog ----------
class TagAliasDialog(QDialog):
    """Edit the Colnect-tag → local-tag mapping stored in tag_aliases.json."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Tag Aliases")
        self.setMinimumWidth(450)

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(
            "Map Colnect tag names to local tag names.\n"
            "When a stamp is imported, the Colnect tag is replaced by local tag."
        ))

        self.table = QTableWidget(0, 2)
        self.table.setHorizontalHeaderLabels(["Colnect Tag", "Your Tag"])
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        layout.addWidget(self.table)

        input_row = QHBoxLayout()
        self.colnect_input = QLineEdit()
        self.colnect_input.setPlaceholderText("Colnect tag (e.g. Men)")
        self.local_input = QLineEdit()
        self.local_input.setPlaceholderText("Your tag (e.g. The Boyz)")
        add_btn = QPushButton("Add")
        add_btn.clicked.connect(self._add_row)
        input_row.addWidget(self.colnect_input)
        input_row.addWidget(QLabel("→"))
        input_row.addWidget(self.local_input)
        input_row.addWidget(add_btn)
        layout.addLayout(input_row)

        remove_btn = QPushButton("Remove Selected")
        remove_btn.clicked.connect(self._remove_selected)
        layout.addWidget(remove_btn)

        btn_row = QHBoxLayout()
        save_btn = QPushButton("Save")
        save_btn.clicked.connect(self._save_and_close)
        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(self.reject)
        btn_row.addWidget(save_btn)
        btn_row.addWidget(cancel_btn)
        layout.addLayout(btn_row)

        self._load()

    def _load(self):
        self.table.setRowCount(0)
        for colnect_name, local_name in load_tag_aliases().items():
            row = self.table.rowCount()
            self.table.insertRow(row)
            self.table.setItem(row, 0, QTableWidgetItem(colnect_name))
            self.table.setItem(row, 1, QTableWidgetItem(local_name))

    def _add_row(self):
        colnect = self.colnect_input.text().strip()
        local = self.local_input.text().strip()
        if not colnect or not local:
            return
        row = self.table.rowCount()
        self.table.insertRow(row)
        self.table.setItem(row, 0, QTableWidgetItem(colnect))
        self.table.setItem(row, 1, QTableWidgetItem(local))
        self.colnect_input.clear()
        self.local_input.clear()

    def _remove_selected(self):
        rows = sorted({i.row() for i in self.table.selectedItems()}, reverse=True)
        for row in rows:
            self.table.removeRow(row)

    def _save_and_close(self):
        aliases = {}
        for row in range(self.table.rowCount()):
            k_item = self.table.item(row, 0)
            v_item = self.table.item(row, 1)
            if k_item and v_item:
                k, v = k_item.text().strip(), v_item.text().strip()
                if k and v:
                    aliases[k] = v
        save_tag_aliases(aliases)
        self.accept()


# ---------- Duplicate Stamp Preview Dialog ----------
class DuplicateStampPreviewDialog(QDialog):
    """Small popup preview of an already-collected stamp, with a one-click path into its editor."""

    def __init__(self, parent, stamp_id: int):
        super().__init__(parent)
        self.setWindowTitle("Stamp Already in Collection")
        self.edit_requested = False

        session = SessionLocal()
        try:
            stamp = session.query(Stamp).get(stamp_id)
            title = stamp.title if stamp else ""
            scott = stamp.scott_number if stamp else ""
            country = stamp.country if stamp else ""
            image_path = stamp.images[0].file_path if stamp and stamp.images else None
        finally:
            session.close()

        layout = QVBoxLayout(self)

        thumb = QLabel()
        thumb.setFixedSize(150, 150)
        thumb.setAlignment(Qt.AlignCenter)
        thumb.setCursor(Qt.PointingHandCursor)
        thumb.setToolTip("Click to edit this stamp")
        if image_path and os.path.exists(image_path):
            pix = QPixmap(image_path).scaled(150, 150, Qt.KeepAspectRatio, Qt.SmoothTransformation)
        else:
            pix = QPixmap(150, 150)
            pix.fill(Qt.lightGray)
        thumb.setPixmap(pix)
        thumb.mousePressEvent = lambda *_: self._open_in_editor()
        layout.addWidget(thumb, alignment=Qt.AlignCenter)

        info_label = QLabel(f"<b>{title}</b><br>Scott #{scott} — {country}")
        info_label.setAlignment(Qt.AlignCenter)
        layout.addWidget(info_label)

        btn_row = QHBoxLayout()
        edit_btn = QPushButton("Edit This Stamp")
        edit_btn.clicked.connect(self._open_in_editor)
        close_btn = QPushButton("Close")
        close_btn.clicked.connect(self.reject)
        btn_row.addWidget(edit_btn)
        btn_row.addWidget(close_btn)
        layout.addLayout(btn_row)

    def _open_in_editor(self):
        self.edit_requested = True
        self.accept()


# ---------- Theme Implications Dialog ----------
class ThemeImplicationsDialog(QDialog):
    """Edit theme implication mappings stored in theme_implications.json."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Theme Implications")
        self.setMinimumWidth(520)

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(
            "When a trigger theme is selected, the listed implied themes are also auto-selected.\n"
            "Separate implied themes with commas."
        ))

        self.table = QTableWidget(0, 2)
        self.table.setHorizontalHeaderLabels(["Trigger Theme", "Implied Themes (comma-separated)"])
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        layout.addWidget(self.table)

        input_row = QHBoxLayout()
        self.trigger_input = QLineEdit()
        self.trigger_input.setPlaceholderText("Trigger theme")
        self.implied_input = QLineEdit()
        self.implied_input.setPlaceholderText("Implied themes (comma-separated)")
        add_btn = QPushButton("Add")
        add_btn.clicked.connect(self._add_row)
        input_row.addWidget(self.trigger_input)
        input_row.addWidget(QLabel("→"))
        input_row.addWidget(self.implied_input)
        input_row.addWidget(add_btn)
        layout.addLayout(input_row)

        remove_btn = QPushButton("Remove Selected")
        remove_btn.clicked.connect(self._remove_selected)
        layout.addWidget(remove_btn)

        btn_row = QHBoxLayout()
        save_btn = QPushButton("Save")
        save_btn.clicked.connect(self._save_and_close)
        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(self.reject)
        btn_row.addWidget(save_btn)
        btn_row.addWidget(cancel_btn)
        layout.addLayout(btn_row)

        self._load()

    def _load(self):
        self.table.setRowCount(0)
        for trigger, implied_list in load_theme_implications().items():
            row = self.table.rowCount()
            self.table.insertRow(row)
            self.table.setItem(row, 0, QTableWidgetItem(trigger))
            self.table.setItem(row, 1, QTableWidgetItem(", ".join(implied_list)))

    def _add_row(self):
        trigger = self.trigger_input.text().strip()
        implied = self.implied_input.text().strip()
        if not trigger or not implied:
            return
        row = self.table.rowCount()
        self.table.insertRow(row)
        self.table.setItem(row, 0, QTableWidgetItem(trigger))
        self.table.setItem(row, 1, QTableWidgetItem(implied))
        self.trigger_input.clear()
        self.implied_input.clear()

    def _remove_selected(self):
        rows = sorted({i.row() for i in self.table.selectedItems()}, reverse=True)
        for row in rows:
            self.table.removeRow(row)

    def _save_and_close(self):
        implications = {}
        for row in range(self.table.rowCount()):
            k_item = self.table.item(row, 0)
            v_item = self.table.item(row, 1)
            if k_item and v_item:
                k = k_item.text().strip()
                v = v_item.text().strip()
                if k and v:
                    implied = [x.strip() for x in v.split(",") if x.strip()]
                    if implied:
                        implications[k] = implied
        save_theme_implications(implications)
        self.accept()


# ---------- Variant Set Dialog ----------
class VariantSetDialog(QDialog):
    """Browse, select, or create a variant set for a stamp."""

    _THUMB = 100
    _COLS  = 3

    def __init__(self, parent, stamp_title: str, stamp_year: str,
                 stamp_country: str, current_id: int | None):
        super().__init__(parent)
        self.setWindowTitle("Variant Sets")
        self.setMinimumSize(640, 440)
        self._selected_id    = current_id
        self._stamp_title    = stamp_title
        self._stamp_year     = stamp_year
        self._stamp_country  = stamp_country

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("Select an existing variant set or create a new one.\n"
                                "Click a set to preview the stamps it contains."))

        splitter = QSplitter(Qt.Horizontal)
        layout.addWidget(splitter)

        # Left: search box + list of variant sets
        left_panel = QWidget()
        left_layout = QVBoxLayout(left_panel)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.setSpacing(4)

        self._search = QLineEdit()
        self._search.setPlaceholderText("Search variant sets…")
        self._search.setClearButtonEnabled(True)
        self._search.textChanged.connect(self._filter_list)
        left_layout.addWidget(self._search)

        self.vs_list = QListWidget()
        self.vs_list.currentItemChanged.connect(self._on_set_selected)
        left_layout.addWidget(self.vs_list)
        splitter.addWidget(left_panel)

        # Right: thumbnail preview of stamps in the selected set
        self.thumb_scroll = QScrollArea()
        self.thumb_scroll.setWidgetResizable(True)
        self.thumb_inner  = QWidget()
        self.thumb_grid   = QGridLayout(self.thumb_inner)
        self.thumb_grid.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        self.thumb_grid.setSpacing(6)
        self.thumb_scroll.setWidget(self.thumb_inner)
        splitter.addWidget(self.thumb_scroll)
        splitter.setSizes([220, 420])

        # Notes section (shown when a set is selected)
        notes_row = QHBoxLayout()
        notes_row.addWidget(QLabel("Notes:"))
        self._notes_edit = QLineEdit()
        self._notes_edit.setPlaceholderText("Notes about this variant set…")
        self._notes_edit.setEnabled(False)
        notes_row.addWidget(self._notes_edit)
        layout.addLayout(notes_row)

        # Buttons
        btn_row = QHBoxLayout()
        create_btn = QPushButton("Create New…")
        create_btn.clicked.connect(self._create_new)
        self.rename_btn = QPushButton("Rename…")
        self.rename_btn.setEnabled(False)
        self.rename_btn.clicked.connect(self._rename_selected)
        self.select_btn = QPushButton("Select")
        self.select_btn.clicked.connect(self._accept_with_save)
        self.select_btn.setEnabled(False)
        clear_btn = QPushButton("Clear")
        clear_btn.clicked.connect(self._clear)
        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(self.reject)
        btn_row.addWidget(create_btn)
        btn_row.addWidget(self.rename_btn)
        btn_row.addStretch()
        btn_row.addWidget(clear_btn)
        btn_row.addWidget(self.select_btn)
        btn_row.addWidget(cancel_btn)
        layout.addLayout(btn_row)

        self._load_list()

    @property
    def selected_id(self) -> int | None:
        return self._selected_id

    def _load_list(self):
        self.vs_list.clear()
        session = SessionLocal()
        try:
            for vs in VariantSetService.get_all(session):
                item = QListWidgetItem(vs.name)
                item.setData(Qt.UserRole, vs.id)
                self.vs_list.addItem(item)
                if vs.id == self._selected_id:
                    self.vs_list.setCurrentItem(item)
        finally:
            session.close()

    def _filter_list(self, text: str):
        text = text.strip().lower()
        for i in range(self.vs_list.count()):
            item = self.vs_list.item(i)
            item.setHidden(bool(text) and text not in item.text().lower())

    def _on_set_selected(self, current, _prev):
        # Auto-save notes for the previously selected set before switching
        if _prev is not None:
            prev_id = _prev.data(Qt.UserRole)
            if prev_id is not None:
                self._save_notes(prev_id)

        if not current:
            self._selected_id = None
            self.select_btn.setEnabled(False)
            self.rename_btn.setEnabled(False)
            self._notes_edit.clear()
            self._notes_edit.setEnabled(False)
            return
        self._selected_id = current.data(Qt.UserRole)
        self.select_btn.setEnabled(True)
        self.rename_btn.setEnabled(True)
        self._load_thumbnails(self._selected_id)
        self._load_notes(self._selected_id)

    def _load_notes(self, variant_set_id: int):
        session = SessionLocal()
        try:
            vs = session.get(VariantSet, variant_set_id)
            self._notes_edit.setText(vs.notes or "" if vs else "")
            self._notes_edit.setEnabled(True)
        finally:
            session.close()

    def _save_notes(self, variant_set_id: int):
        session = SessionLocal()
        try:
            VariantSetService.save_notes(session, variant_set_id, self._notes_edit.text())
        except Exception:
            session.rollback()
        finally:
            session.close()

    def _rename_selected(self):
        current = self.vs_list.currentItem()
        if not current:
            return
        vs_id = current.data(Qt.UserRole)
        new_name, ok = QInputDialog.getText(
            self, "Rename Variant Set", "New name:", text=current.text()
        )
        if not ok or not new_name.strip():
            return
        session = SessionLocal()
        try:
            vs = VariantSetService.rename(session, vs_id, new_name.strip())
            current.setText(vs.name)
        except ValueError as e:
            QMessageBox.warning(self, "Rename Failed", str(e))
        finally:
            session.close()

    def _load_thumbnails(self, variant_set_id: int):
        # Clear old grid cards by reparenting them to a hidden off-screen container
        # and scheduling it for deletion. This avoids two crash modes:
        #   1. setWidget() double-free: setWidget deletes the C++ widget while Python
        #      still holds self.thumb_inner, causing an access violation when Python's
        #      GC later calls __del__ on the already-destroyed C++ object.
        #   2. takeAt/deleteLater paint race: removed-but-not-yet-deleted widgets can
        #      still receive paint events on the next event loop tick.
        # Reparenting to an invisible, parentless container means no paint events fire
        # during the deleteLater window, and Qt/PyQt5 own the lifetime cleanly.
        old_container = QWidget()
        while self.thumb_grid.count():
            item = self.thumb_grid.takeAt(0)
            if item and item.widget():
                item.widget().setParent(old_container)
        old_container.deleteLater()

        session = SessionLocal()
        try:
            stamps = VariantSetService.get_stamps(session, variant_set_id)
        finally:
            session.close()

        T = self._THUMB
        for idx, s in enumerate(stamps):
            card = QFrame()
            card.setFixedWidth(T + 16)
            card.setFrameShape(QFrame.StyledPanel)
            cl = QVBoxLayout(card)
            cl.setContentsMargins(3, 3, 3, 3)
            cl.setSpacing(2)

            thumb = QLabel()
            thumb.setFixedSize(T, T)
            thumb.setAlignment(Qt.AlignCenter)
            ip = s.get("image_path")
            if ip and os.path.exists(ip):
                pix = QPixmap(ip).scaled(T, T, Qt.KeepAspectRatio, Qt.SmoothTransformation)
            else:
                pix = QPixmap(T, T)
                pix.fill(Qt.lightGray)
            thumb.setPixmap(pix)
            cl.addWidget(thumb)

            lbl = QLabel(f"#{s.get('scott_number', '')}  {(s.get('title') or '')[:22]}")
            lbl.setWordWrap(True)
            lbl.setMaximumWidth(T + 10)
            cl.addWidget(lbl)

            self.thumb_grid.addWidget(card, idx // self._COLS, idx % self._COLS)

    def _country_abbrev(self) -> str:
        country = self._stamp_country or ""
        if not country:
            return ""
        try:
            import pycountry
            results = pycountry.countries.search_fuzzy(country)
            if results:
                return results[0].alpha_2
        except Exception:
            pass
        return country[:3].upper()

    def _create_new(self):
        year   = self._stamp_year or ""
        title  = self._stamp_title or "Untitled"
        abbrev = self._country_abbrev()
        if year and abbrev:
            suggested = f"{title} ({year}) {abbrev}"
        elif year:
            suggested = f"{title} ({year})"
        elif abbrev:
            suggested = f"{title} {abbrev}"
        else:
            suggested = title

        name_dlg = QDialog(self)
        name_dlg.setWindowTitle("New Variant Set")
        dlg_layout = QVBoxLayout(name_dlg)
        dlg_layout.addWidget(QLabel("Name:"))
        name_edit = QLineEdit(suggested)
        name_edit.selectAll()
        dlg_layout.addWidget(name_edit)

        warn_lbl = QLabel()
        warn_lbl.setOpenExternalLinks(False)
        warn_lbl.setVisible(False)
        dlg_layout.addWidget(warn_lbl)

        btn_row = QHBoxLayout()
        ok_btn = QPushButton("Create")
        ok_btn.clicked.connect(name_dlg.accept)
        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(name_dlg.reject)
        btn_row.addWidget(ok_btn)
        btn_row.addWidget(cancel_btn)
        dlg_layout.addLayout(btn_row)

        existing_id   = [None]
        chose_existing = [False]

        def _check(text):
            txt = text.strip()
            if not txt:
                warn_lbl.setVisible(False)
                existing_id[0] = None
                return
            session = SessionLocal()
            try:
                vs = session.query(VariantSet).filter(VariantSet.name.ilike(txt)).one_or_none()
            finally:
                session.close()
            if vs:
                existing_id[0] = vs.id
                warn_lbl.setText(
                    '<span style="color:red;">⚠ Variant set already exists — '
                    '<a href="use" style="color:red;">use existing</a></span>'
                )
                warn_lbl.setVisible(True)
            else:
                existing_id[0] = None
                warn_lbl.setVisible(False)

        def _use_existing(_href):
            if not existing_id[0]:
                return
            for i in range(self.vs_list.count()):
                item = self.vs_list.item(i)
                if item.data(Qt.UserRole) == existing_id[0]:
                    self.vs_list.setCurrentItem(item)
                    break
            self._selected_id = existing_id[0]
            chose_existing[0] = True
            name_dlg.accept()

        warn_lbl.linkActivated.connect(_use_existing)
        name_edit.textChanged.connect(_check)
        _check(suggested)

        if name_dlg.exec() != QDialog.Accepted or chose_existing[0]:
            return

        name = name_edit.text().strip()
        if not name:
            return
        session = SessionLocal()
        try:
            vs = VariantSetService.create(session, name)
            item = QListWidgetItem(vs.name)
            item.setData(Qt.UserRole, vs.id)
            self.vs_list.addItem(item)
            self.vs_list.setCurrentItem(item)
        except Exception as e:
            QMessageBox.warning(self, "Error", str(e))
        finally:
            session.close()

    def _accept_with_save(self):
        if self._selected_id is not None:
            self._save_notes(self._selected_id)
        self.accept()

    def _clear(self):
        self._selected_id = None
        self.accept()




# ---------- Country Picker Dialog ----------
class CountryPickerDialog(QDialog):
    """Searchable list of all countries showing both name and ISO code."""

    def __init__(self, parent):
        super().__init__(parent)
        self.setWindowTitle("Select Country")
        self.setMinimumSize(360, 460)
        self._selected_name = None

        layout = QVBoxLayout(self)

        self._search = QLineEdit()
        self._search.setPlaceholderText("Type to filter…")
        self._search.textChanged.connect(self._filter)
        layout.addWidget(self._search)

        self._list = QListWidget()
        self._list.currentItemChanged.connect(self._on_select)
        self._list.itemDoubleClicked.connect(lambda _: self.accept())
        layout.addWidget(self._list)

        btn_row = QHBoxLayout()
        self._ok_btn = QPushButton("Select")
        self._ok_btn.setEnabled(False)
        self._ok_btn.clicked.connect(self.accept)
        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(self.reject)
        btn_row.addStretch()
        btn_row.addWidget(self._ok_btn)
        btn_row.addWidget(cancel_btn)
        layout.addLayout(btn_row)

        for label, name in self._build_entries():
            item = QListWidgetItem(label)
            item.setData(Qt.UserRole, name)
            self._list.addItem(item)

    @property
    def selected_name(self) -> str | None:
        return self._selected_name

    @staticmethod
    def _build_entries() -> list[tuple[str, str]]:
        """Return sorted list of (display_label, country_name) pairs."""
        # Re-read country_overrides.json so overrides added since the app started
        # (edited on disk) show up as soon as the picker is reopened. The old
        # main window kept COUNTRY_MAP fresh with a file-watcher timer that the
        # canvas UI dropped; reloading on open restores that behaviour here.
        reload_country_overrides()

        seen_codes: set[str] = set()
        rows: list[tuple[str, str]] = []

        for c in pycountry.countries:
            code = c.alpha_2.upper()
            # Prefer the override name if one exists for this code
            name = COUNTRY_MAP.get(code, c.name)
            rows.append((f"{name}  ({code})", name))
            seen_codes.add(code)

        # Non-standard codes from overrides (e.g. "UK") not covered by pycountry
        for code, name in COUNTRY_MAP.items():
            if code.upper() not in seen_codes:
                rows.append((f"{name}  ({code})", name))

        rows.sort(key=lambda x: x[0].lower())
        return rows

    def _filter(self, text: str):
        text = text.strip().lower()
        for i in range(self._list.count()):
            item = self._list.item(i)
            item.setHidden(bool(text) and text not in item.text().lower())
        self._list.setCurrentItem(None)
        self._selected_name = None
        self._ok_btn.setEnabled(False)

    def _on_select(self, current, _prev):
        self._selected_name = current.data(Qt.UserRole) if current else None
        self._ok_btn.setEnabled(self._selected_name is not None)


# ---------- Image Browser Dialog ----------
class ImageBrowserDialog(QDialog):
    """Browse un-associated captures in INCOMING_DIR and select one to assign to a stamp."""

    _THUMB = 110
    _COLS  = 4

    def __init__(self, parent, current_stamp_id: int | None = None):
        super().__init__(parent)
        self.setWindowTitle("Browse Image History")
        self.setMinimumSize(640, 520)
        self._selected_path  = None
        self._selected_card  = None
        self._current_stamp_id = current_stamp_id

        layout = QVBoxLayout(self)

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.inner = QWidget()
        self.grid  = QGridLayout(self.inner)
        self.grid.setSpacing(8)
        self.grid.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        self.scroll.setWidget(self.inner)
        layout.addWidget(self.scroll)

        self.info_lbl = QLabel("Click an image to select it, double-click to assign immediately.")
        self.info_lbl.setAlignment(Qt.AlignCenter)
        layout.addWidget(self.info_lbl)

        btn_row = QHBoxLayout()
        self._ok_btn = QPushButton("Assign to This Stamp")
        self._ok_btn.setEnabled(False)
        self._ok_btn.clicked.connect(self.accept)
        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(self.reject)
        btn_row.addStretch()
        btn_row.addWidget(self._ok_btn)
        btn_row.addWidget(cancel_btn)
        layout.addLayout(btn_row)

        # Thumbnails load lazily: each card's JPEG is only decoded once the
        # card scrolls into (or near) the viewport, so opening the dialog stays
        # fast even with thousands of images in the incoming folder.
        self._lazy: dict[str, QLabel] = {}   # path → thumb label (card carries geometry)
        self._pending: set[str] = set()      # thumbs not yet decoded
        bar = self.scroll.verticalScrollBar()
        bar.valueChanged.connect(self._load_visible)
        bar.rangeChanged.connect(lambda *_: self._load_visible())

        self._load_images()

    @property
    def selected_path(self) -> str | None:
        return self._selected_path

    def _load_images(self):
        while self.grid.count():
            item = self.grid.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self._lazy.clear()
        self._pending.clear()

        try:
            files = sorted(
                [f for f in os.listdir(INCOMING_DIR) if f.lower().endswith((".jpg", ".png"))],
                key=lambda f: os.path.getmtime(os.path.join(INCOMING_DIR, f)),
                reverse=True,
            )
        except Exception:
            files = []

        # Build a path → stamp label map from the DB
        session = SessionLocal()
        try:
            assigned: dict[str, str] = {}
            for img in session.query(StampImage).all():
                s = img.stamp
                if s:
                    assigned[img.file_path] = f"#{s.scott_number}  {(s.title or '')[:18]}  [{s.country}]"
                else:
                    assigned[img.file_path] = "Assigned"
        finally:
            session.close()

        T = self._THUMB
        for idx, fname in enumerate(files):
            path = os.path.join(INCOMING_DIR, fname)

            card = QFrame()
            card.setFixedWidth(T + 20)
            card.setFrameShape(QFrame.StyledPanel)
            card.setCursor(Qt.PointingHandCursor)
            card_layout = QVBoxLayout(card)
            card_layout.setContentsMargins(4, 4, 4, 4)
            card_layout.setSpacing(2)

            # Fixed-size placeholder — the JPEG is decoded later, on demand, in
            # _load_visible. The fixed size keeps the grid geometry stable so the
            # visibility math stays correct whether or not the thumb has loaded.
            thumb = QLabel()
            thumb.setFixedSize(T, T)
            thumb.setAlignment(Qt.AlignCenter)

            if path in assigned:
                status = QLabel(assigned[path])
                status.setStyleSheet("color: #4a9eff; font-size: 9px;")
            else:
                status = QLabel("Unassigned")
                status.setStyleSheet("color: gray; font-size: 9px;")
            status.setWordWrap(True)
            status.setMaximumWidth(T + 12)

            card_layout.addWidget(thumb)
            card_layout.addWidget(status)

            card.mousePressEvent        = lambda _e, p=path, c=card: self._select(p, c)
            thumb.mousePressEvent       = lambda _e, p=path, c=card: self._select(p, c)
            card.mouseDoubleClickEvent  = lambda _e, p=path, c=card: self._double_click(p, c)
            thumb.mouseDoubleClickEvent = lambda _e, p=path, c=card: self._double_click(p, c)

            self.grid.addWidget(card, idx // self._COLS, idx % self._COLS)
            self._lazy[path] = thumb
            self._pending.add(path)

        # Decode the first screenful once the grid has been laid out.
        QTimer.singleShot(0, self._load_visible)

    def _load_visible(self):
        """Decode any pending thumbnails within (or one screen of) the view."""
        if not self._pending:
            return
        vp_h = self.scroll.viewport().height()
        if vp_h <= 0:
            return
        # Reading widget geometry does not trigger a pending layout pass; force
        # it so card positions are current instead of stale zeros.
        self.grid.activate()
        offset = self.scroll.verticalScrollBar().value()
        top    = offset - vp_h            # preload a screen above and below so
        bottom = offset + 2 * vp_h        # scrolling stays ahead of the reveal
        T = self._THUMB
        for path in list(self._pending):
            thumb = self._lazy.get(path)
            card  = thumb.parentWidget() if thumb else None
            if card is None:
                continue
            y = card.y()
            if y + card.height() >= top and y <= bottom:
                self._pending.discard(path)
                if os.path.exists(path):
                    thumb.setPixmap(
                        QPixmap(path).scaled(T, T, Qt.KeepAspectRatio, Qt.SmoothTransformation)
                    )

    def showEvent(self, event):
        super().showEvent(event)
        self._load_visible()

    def _select(self, path: str, card: QFrame):
        if self._selected_card:
            self._selected_card.setStyleSheet("")
        self._selected_path = path
        self._selected_card = card
        card.setStyleSheet("QFrame { border: 2px solid #4a9eff; }")
        self._ok_btn.setEnabled(True)
        self.info_lbl.setText(f"Selected: {os.path.basename(path)}")

    def _double_click(self, path: str, card: QFrame):
        self._select(path, card)
        self.accept()


# ---------- Stamp Picker Dialog ----------
class StampPickerDialog(QDialog):
    """Search and select a stamp — used for reassigning images."""

    def __init__(self, parent, exclude_stamp_id: int | None = None):
        super().__init__(parent)
        self.setWindowTitle("Assign Image to Stamp")
        self.setMinimumSize(500, 380)
        self._selected_id = None
        self._exclude_id = exclude_stamp_id

        layout = QVBoxLayout(self)

        self._search = QLineEdit()
        self._search.setPlaceholderText("Search title, Scott #, country…")
        self._search.textChanged.connect(self._run_search)
        layout.addWidget(self._search)

        self._list = QListWidget()
        self._list.currentItemChanged.connect(self._on_select)
        self._list.itemDoubleClicked.connect(lambda _: self.accept())
        layout.addWidget(self._list)

        btn_row = QHBoxLayout()
        self._ok_btn = QPushButton("Assign to This Stamp")
        self._ok_btn.setEnabled(False)
        self._ok_btn.clicked.connect(self.accept)
        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(self.reject)
        btn_row.addStretch()
        btn_row.addWidget(self._ok_btn)
        btn_row.addWidget(cancel_btn)
        layout.addLayout(btn_row)

        self._run_search("")

    @property
    def selected_id(self) -> int | None:
        return self._selected_id

    def _run_search(self, text: str):
        self._list.clear()
        self._selected_id = None
        self._ok_btn.setEnabled(False)
        session = SessionLocal()
        try:
            if text.strip():
                stamps = StampService.search_stamps(session, text.strip())
            else:
                stamps = session.query(Stamp).order_by(Stamp.added_to_db.desc()).limit(60).all()
        finally:
            session.close()
        for s in stamps:
            if s.id == self._exclude_id:
                continue
            item = QListWidgetItem(f"#{s.scott_number}  {s.title}  [{s.country}]")
            item.setData(Qt.UserRole, s.id)
            self._list.addItem(item)

    def _on_select(self, current, _prev):
        self._selected_id = current.data(Qt.UserRole) if current else None
        self._ok_btn.setEnabled(self._selected_id is not None)


# ---------- Series Stamps Dialog ----------
class SeriesStampsDialog(QDialog):
    """Show all stamps in a series that are already in the local database."""

    _THUMB = 100
    _COLS  = 4

    def __init__(self, parent, series_id: int):
        super().__init__(parent)
        self._series_id = series_id
        self.setMinimumSize(620, 480)

        layout = QVBoxLayout(self)

        session = SessionLocal()
        try:
            s = SeriesService.get_by_id(session, series_id)
            series_name = s.name if s else "Unknown Series"
            stamps = SeriesService.get_stamps(session, series_id)
        finally:
            session.close()

        self.setWindowTitle(f"Series: {series_name}")

        header = QLabel(f"<b>{series_name}</b> — {len(stamps)} stamp(s) in your collection")
        header.setWordWrap(True)
        layout.addWidget(header)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        inner = QWidget()
        grid  = QGridLayout(inner)
        grid.setSpacing(8)
        grid.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        scroll.setWidget(inner)
        layout.addWidget(scroll)

        T = self._THUMB
        for idx, stamp in enumerate(stamps):
            card = QFrame()
            card.setFixedWidth(T + 20)
            card.setFrameShape(QFrame.StyledPanel)
            card.setCursor(Qt.PointingHandCursor)
            cl = QVBoxLayout(card)
            cl.setContentsMargins(4, 4, 4, 4)
            cl.setSpacing(2)

            thumb = QLabel()
            thumb.setFixedSize(T, T)
            thumb.setAlignment(Qt.AlignCenter)
            ip = stamp.get("image_path")
            if ip and os.path.exists(ip):
                pix = QPixmap(ip).scaled(T, T, Qt.KeepAspectRatio, Qt.SmoothTransformation)
            else:
                pix = QPixmap(T, T)
                pix.fill(Qt.lightGray)
            thumb.setPixmap(pix)

            lbl = QLabel(f"#{stamp.get('scott_number', '')}  {(stamp.get('title') or '')[:20]}")
            lbl.setWordWrap(True)
            lbl.setMaximumWidth(T + 12)
            cl.addWidget(thumb)
            cl.addWidget(lbl)

            stamp_id = stamp["id"]
            card.mousePressEvent = lambda *_, sid=stamp_id: self._open_stamp(sid)
            grid.addWidget(card, idx // self._COLS, idx % self._COLS)

        if not stamps:
            grid.addWidget(QLabel("No stamps from this series are in your collection yet."), 0, 0)

        close_btn = QPushButton("Close")
        close_btn.clicked.connect(self.accept)
        layout.addWidget(close_btn)

    def _open_stamp(self, stamp_id: int):
        self.accept()
        # Walk up the parent chain to whichever ancestor can load a stamp.
        # The redesigned FieldsPanel exposes load_stamp(); the old monolith
        # app exposes load_stamp_into_editor().
        p = self.parent()
        while p is not None:
            for method in ("load_stamp", "load_stamp_into_editor"):
                fn = getattr(p, method, None)
                if callable(fn):
                    fn(stamp_id)
                    return
            p = p.parent() if hasattr(p, "parent") else None


# ---------- Crop Dialog ----------
class _CropCanvas(QWidget):
    """Displays an image and lets the user drag a rubber-band crop selection."""

    def __init__(self, pixmap: QPixmap, parent=None):
        super().__init__(parent)
        self._pixmap = pixmap
        self._origin = None
        self._selection: QRect | None = None
        self._rubber_band = QRubberBand(QRubberBand.Rectangle, self)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.setCursor(Qt.CrossCursor)

    def paintEvent(self, _event):
        p = QPainter(self)
        scaled = self._pixmap.scaled(self.size(), Qt.KeepAspectRatio, Qt.SmoothTransformation)
        x = (self.width()  - scaled.width())  // 2
        y = (self.height() - scaled.height()) // 2
        p.drawPixmap(x, y, scaled)

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self._origin = event.position().toPoint()
            self._selection = None
            self._rubber_band.setGeometry(QRect(self._origin, QSize()))
            self._rubber_band.show()

    def mouseMoveEvent(self, event):
        if self._origin is not None:
            self._rubber_band.setGeometry(
                QRect(self._origin, event.position().toPoint()).normalized()
            )

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton and self._origin is not None:
            self._selection = QRect(self._origin, event.position().toPoint()).normalized()
            self._origin = None

    def _image_rect(self) -> tuple[int, int, int, int]:
        """Return (ox, oy, scaled_w, scaled_h) — the drawn image rect on the canvas."""
        scaled = self._pixmap.scaled(self.size(), Qt.KeepAspectRatio, Qt.SmoothTransformation)
        ox = (self.width()  - scaled.width())  // 2
        oy = (self.height() - scaled.height()) // 2
        return ox, oy, scaled.width(), scaled.height()

    def crop_rect_in_image(self) -> tuple[int, int, int, int] | None:
        """Return (x1, y1, x2, y2) in original image pixels, or None if no selection."""
        if self._selection is None or not self._selection.isValid():
            return None
        ox, oy, sw, sh = self._image_rect()
        pw, ph = self._pixmap.width(), self._pixmap.height()
        scale_x = pw / sw
        scale_y = ph / sh
        x1 = max(0,  int((self._selection.left()   - ox) * scale_x))
        y1 = max(0,  int((self._selection.top()    - oy) * scale_y))
        x2 = min(pw, int((self._selection.right()  - ox) * scale_x))
        y2 = min(ph, int((self._selection.bottom() - oy) * scale_y))
        if x2 <= x1 or y2 <= y1:
            return None
        return x1, y1, x2, y2


class CropDialog(QDialog):
    def __init__(self, image_path: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Crop Image")
        self.setMinimumSize(640, 520)
        self._image_path = image_path

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)

        self._canvas = _CropCanvas(QPixmap(image_path))
        layout.addWidget(self._canvas, 1)

        hint = QLabel("Click and drag to select the crop area, then click Apply.")
        hint.setAlignment(Qt.AlignCenter)
        hint.setStyleSheet("color: gray; font-size: 11px;")
        layout.addWidget(hint)

        btn_row = QHBoxLayout()
        btn_row.addStretch()
        apply_btn = QPushButton("Apply Crop")
        apply_btn.setFixedWidth(100)
        apply_btn.clicked.connect(self._apply)
        cancel_btn = QPushButton("Cancel")
        cancel_btn.setFixedWidth(80)
        cancel_btn.clicked.connect(self.reject)
        btn_row.addWidget(apply_btn)
        btn_row.addWidget(cancel_btn)
        layout.addLayout(btn_row)

    def _apply(self):
        rect = self._canvas.crop_rect_in_image()
        if rect is None:
            QMessageBox.warning(self, "No Selection",
                                "Drag a rectangle over the image to select a crop area first.")
            return
        x1, y1, x2, y2 = rect
        try:
            img = Image.open(self._image_path)
            img.crop((x1, y1, x2, y2)).save(self._image_path)
            self.accept()
        except Exception as e:
            QMessageBox.critical(self, "Crop Failed", str(e))


# ---------- Main App ----------
class StampIdentifierApp(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Stamp Identifier")
        self.setMinimumSize(MIN_WINDOW_WIDTH, MIN_WINDOW_HEIGHT)
        self.resize(DEFAULT_WINDOW_WIDTH, DEFAULT_WINDOW_HEIGHT)

        os.makedirs(IMAGE_DIR, exist_ok=True)

        # Single async worker managing both browsers on one event loop
        self.browser_worker = AsyncBrowserWorker(lens_result_queue, colnect_result_queue)
        self.browser_worker.start()

        # Camera — DISABLED FOR TESTING (re-enable by restoring the _open_camera() call below)
        self.cap = cv2.VideoCapture()  # null capture; isOpened() == False
        logger.warning("Camera disabled for testing — running in image-only mode")
        # self.cap = self._open_camera()
        # if self.cap is None or not self.cap.isOpened():
        #     logger.warning("Could not open webcam — app will start in image-only mode")
        #     self.cap = cv2.VideoCapture()  # null capture; isOpened() == False

        self.current_frame = None
        self.current_image_path = None
        self._cam_rotation = 0  # degrees CW: 0, 90, 180, 270
        self._cam_fail_count = 0
        self._gallery_resize_timer = QTimer()
        self._gallery_resize_timer.setSingleShot(True)
        self._gallery_resize_timer.timeout.connect(self._on_gallery_resize)
        self.current_stamp_id = None
        self.current_variant_set_id = None
        self.current_series_id = None
        self._current_db_country = None
        self.preview_mode = "live"
        self._gallery_open = False
        self._gallery_tag_mode = "OR"
        self._gallery_page = 0
        self._theme_sort = "alpha"
        self._reloading_themes = False
        self._db_view = "countries"          # "countries", "series", or "locations"
        self._current_db_series_id = None
        self._current_db_location_id = None
        self._db_item_data: list = []   # parallel to db_list rows — no Qt UserRole storage

        # Zoom factor (1.0 = no zoom). Controlled by UI slider.
        self.zoom = 1.0
        self.zoom_label = None
        self.zoom_slider = None

        # Focus tap overlay: (label_x, label_y) or None
        self.focus_tap_pos = None
        self.focus_tap_timer = QTimer()
        self.focus_tap_timer.setSingleShot(True)
        self.focus_tap_timer.timeout.connect(self._clear_focus_tap)

        # Hover tracking (optional)
        self._hovering_history = False

        # path → history thumbnail QLabel (for in-place thumbnail refresh after rotate)
        self._history_labels: dict[str, QLabel] = {}

        # Request counter
        self.request_counter = 0

        # Layout
        self._build_layout()
        self.load_history()
        self.populate_countries()
        self._update_collection_count()
        self.set_live_mode()

        # Timer for updating camera preview — not started while camera is disabled
        self.timer = QTimer()
        self.timer.timeout.connect(self.update_frame)
        # self.timer.start(int(1000 / CAMERA_FPS))  # Re-enable with camera

        # Debounce timer: fires _check_duplicate_stamp only after typing pauses
        self._dup_check_timer = QTimer()
        self._dup_check_timer.setSingleShot(True)
        self._dup_check_timer.setInterval(400)
        self._dup_check_timer.timeout.connect(self._check_duplicate_stamp)



        # Poll Lens results
        self.poll_lens_results()
        self.poll_colnect_results()
        self.load_themes()

        # Watch country_overrides.json for live edits
        self._overrides_mtime = self._get_overrides_mtime()
        self._overrides_watcher = QTimer()
        self._overrides_watcher.timeout.connect(self._check_overrides_file)
        self._overrides_watcher.start(2000)

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
        # GALLERY PANEL (leftmost, initially collapsed)
        # =========================================================
        self.gallery_panel = QWidget()
        self.gallery_panel.setMinimumWidth(PANEL_GALLERY_MIN_W)
        gallery_layout = QVBoxLayout(self.gallery_panel)
        gallery_layout.setContentsMargins(6, 6, 6, 6)
        gallery_layout.setSpacing(6)

        gallery_header = QLabel("Gallery")
        gallery_header.setStyleSheet("font-weight: bold; font-size: 13px;")
        gallery_layout.addWidget(gallery_header)

        # Search row
        gallery_search_row = QHBoxLayout()
        self.gallery_search = QLineEdit()
        self.gallery_search.setPlaceholderText("Search title, Scott #, country…")
        self.gallery_search.returnPressed.connect(self._apply_gallery_filters)
        gallery_search_btn = QPushButton("Search")
        gallery_search_btn.clicked.connect(self._apply_gallery_filters)
        gallery_search_row.addWidget(self.gallery_search)
        gallery_search_row.addWidget(gallery_search_btn)
        gallery_layout.addLayout(gallery_search_row)

        # Filter row — country + sort (tags moved to their own row below)
        gallery_filter_row = QHBoxLayout()
        self.gallery_country_filter = QComboBox()
        self.gallery_country_filter.addItem("All Countries", "")
        self.gallery_country_filter.currentIndexChanged.connect(self._apply_gallery_filters)
        self.gallery_sort = QComboBox()
        for label in ["Newest First", "Oldest First", "Scott #", "Country", "Title"]:
            self.gallery_sort.addItem(label)
        self.gallery_sort.currentIndexChanged.connect(self._apply_gallery_filters)
        gallery_filter_row.addWidget(self.gallery_country_filter)
        gallery_filter_row.addWidget(self.gallery_sort)
        gallery_layout.addLayout(gallery_filter_row)

        # Tags row — multi-select list with AND/OR mode toggle
        tag_header = QHBoxLayout()
        tag_header.addWidget(QLabel("Tags:"))
        self.gallery_tag_mode_btn = QPushButton("OR")
        self.gallery_tag_mode_btn.setFixedWidth(44)
        self.gallery_tag_mode_btn.setToolTip("Toggle between OR (any tag) and AND (all tags)")
        self.gallery_tag_mode_btn.clicked.connect(self._toggle_gallery_tag_mode)
        tag_header.addWidget(self.gallery_tag_mode_btn)
        tag_header.addStretch()
        gallery_layout.addLayout(tag_header)

        self.gallery_tag_list = QListWidget()
        self.gallery_tag_list.setSelectionMode(QListWidget.MultiSelection)
        self.gallery_tag_list.setFixedHeight(88)
        self.gallery_tag_list.itemSelectionChanged.connect(self._apply_gallery_filters)
        gallery_layout.addWidget(self.gallery_tag_list)

        # Card grid inside a scroll area
        self.gallery_scroll = QScrollArea()
        self.gallery_scroll.setWidgetResizable(True)
        self.gallery_scroll.installEventFilter(self)
        self.gallery_inner = QWidget()
        self.gallery_grid = QGridLayout(self.gallery_inner)
        self.gallery_grid.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        self.gallery_grid.setSpacing(8)
        self.gallery_scroll.setWidget(self.gallery_inner)
        gallery_layout.addWidget(self.gallery_scroll)

        # Pagination controls
        pagination_row = QHBoxLayout()
        self.gallery_prev_btn = QPushButton("◀")
        self.gallery_prev_btn.setFixedWidth(36)
        self.gallery_prev_btn.setEnabled(False)
        self.gallery_prev_btn.clicked.connect(self._gallery_prev_page)
        self.gallery_page_label = QLabel("Page 1 of 1")
        self.gallery_page_label.setAlignment(Qt.AlignCenter)
        self.gallery_next_btn = QPushButton("▶")
        self.gallery_next_btn.setFixedWidth(36)
        self.gallery_next_btn.setEnabled(False)
        self.gallery_next_btn.clicked.connect(self._gallery_next_page)
        pagination_row.addStretch()
        pagination_row.addWidget(self.gallery_prev_btn)
        pagination_row.addWidget(self.gallery_page_label)
        pagination_row.addWidget(self.gallery_next_btn)
        pagination_row.addStretch()
        gallery_layout.addLayout(pagination_row)

        self.main_splitter.addWidget(self.gallery_panel)
        self.main_splitter.setCollapsible(0, True)

        # =========================================================
        # DATABASE SIDEBAR (middle panel)
        # =========================================================
        self.db_container = QScrollArea()
        self.db_container.setWidgetResizable(True)
        self.db_container.setMinimumWidth(PANEL_DB_MIN_W)
        self.db_container.setMaximumWidth(PANEL_DB_MAX_W)

        self.db_inner = QWidget()
        self.db_layout = QVBoxLayout(self.db_inner)
        self.db_layout.setAlignment(Qt.AlignTop)

        # Gallery toggle button at the top of the DB sidebar
        self.gallery_toggle_btn = QPushButton("▶ Gallery")
        self.gallery_toggle_btn.clicked.connect(self.toggle_gallery)
        self.db_layout.addWidget(self.gallery_toggle_btn)

        db_title = QLabel("Database")
        db_title.setStyleSheet("font-weight: bold;")
        self.db_layout.addWidget(db_title)

        self.collection_count_label = QLabel("0 stamps in collection")
        self.collection_count_label.setStyleSheet("color: gray; font-size: 11px;")
        self.db_layout.addWidget(self.collection_count_label)

        self.db_view_combo = QComboBox()
        self.db_view_combo.addItems(["Countries", "Series", "Locations"])
        self.db_view_combo.currentTextChanged.connect(
            lambda text: self._switch_db_view(text.lower())
        )
        self.db_layout.addWidget(self.db_view_combo)

        self.db_list = QListWidget()
        self.db_list.setIconSize(QSize(64, 64))
        self.db_list.setSpacing(8)
        self.db_list.setWordWrap(True)
        self.db_layout.addWidget(self.db_list)

        self.bulk_delete_btn = QPushButton("Delete Selected")
        self.bulk_delete_btn.setVisible(False)
        self.bulk_delete_btn.clicked.connect(self._bulk_delete_stamps)
        self.db_layout.addWidget(self.bulk_delete_btn)

        self.db_container.setWidget(self.db_inner)
        self.db_list.itemClicked.connect(self.on_db_item_clicked)
        self.db_list.itemSelectionChanged.connect(self._update_bulk_delete_btn)
        self.db_list.setContextMenuPolicy(Qt.CustomContextMenu)
        self.db_list.customContextMenuRequested.connect(self._on_db_list_context_menu)

        self.main_splitter.addWidget(self.db_container)

        # =========================================================
        # RIGHT: Main Content Container (grid)
        # =========================================================
        self.content_container = QWidget()
        self.content_container.setMinimumWidth(PANEL_CONTENT_MIN_W)
        content_root = QHBoxLayout(self.content_container)
        content_root.setContentsMargins(0, 0, 0, 0)

        self.main_splitter.addWidget(self.content_container)
        self.main_splitter.setSizes(SPLITTER_MAIN_INIT)   # gallery hidden initially

        # =========================================================
        # Outer horizontal splitter: main stack | history sidebar
        # =========================================================
        self.content_h_splitter = QSplitter(Qt.Horizontal)
        self.content_h_splitter.setChildrenCollapsible(False)
        content_root.addWidget(self.content_h_splitter)

        # =========================================================
        # Inner vertical splitter: preview+controls | results | info
        # =========================================================
        self.content_v_splitter = QSplitter(Qt.Vertical)
        self.content_v_splitter.setMinimumWidth(0)
        self.content_v_splitter.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self.content_h_splitter.addWidget(self.content_v_splitter)

        # =========================================================
        # TOP: Preview + Controls (grouped so they move together)
        # =========================================================
        top_widget = QWidget()
        top_layout = QVBoxLayout(top_widget)
        top_layout.setContentsMargins(6, 6, 6, 2)
        top_layout.setSpacing(4)

        self.preview_label = QLabel()
        self.preview_label.setAlignment(Qt.AlignCenter)
        self.preview_label.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.preview_label.setMinimumHeight(200)
        self.preview_label.setMouseTracking(True)
        top_layout.addWidget(self.preview_label)

        # Controls widget (buttons + zoom)
        controls_widget = QWidget()
        controls_wrapper = QVBoxLayout(controls_widget)
        controls_wrapper.setContentsMargins(0, 0, 0, 0)
        controls_wrapper.setSpacing(2)

        buttons_layout = QHBoxLayout()
        buttons_layout.setAlignment(Qt.AlignCenter)

        self.capture_btn = QPushButton("Capture + Search")
        self.capture_btn.setFixedWidth(120)
        self.capture_btn.clicked.connect(self.capture_image)

        self.capture_only_btn = QPushButton("Capture Only")
        self.capture_only_btn.setFixedWidth(100)
        self.capture_only_btn.clicked.connect(self.capture_image_only)

        self.camera_btn = QPushButton("Camera")
        self.camera_btn.clicked.connect(self.show_camera)

        self.search_btn = QPushButton("Search")
        self.search_btn.clicked.connect(self.search_image)

        self.rotate_btn = QPushButton("↻")
        self.rotate_btn.setFixedWidth(32)
        self.rotate_btn.setToolTip("Rotate camera 90° clockwise")
        self.rotate_btn.clicked.connect(self._rotate_camera)

        self.crop_btn = QPushButton("Crop")
        self.crop_btn.setFixedWidth(60)
        self.crop_btn.setToolTip("Crop the captured image")
        self.crop_btn.clicked.connect(self._open_crop_dialog)

        buttons_layout.addWidget(self.capture_btn)
        buttons_layout.addWidget(self.capture_only_btn)
        buttons_layout.addWidget(self.camera_btn)
        buttons_layout.addWidget(self.search_btn)
        buttons_layout.addWidget(self.rotate_btn)
        buttons_layout.addWidget(self.crop_btn)

        zoom_layout = QHBoxLayout()
        zoom_layout.setAlignment(Qt.AlignRight)

        self.zoom_label = QLabel("Zoom: 1.0x")
        self.zoom_slider = QSlider()
        self.zoom_slider.setOrientation(Qt.Horizontal)
        self.zoom_slider.setRange(100, 400)
        self.zoom_slider.setValue(100)
        self.zoom_slider.setSingleStep(10)
        self.zoom_slider.setFixedWidth(200)
        self.zoom_slider.valueChanged.connect(self.on_zoom_changed)

        zoom_layout.addWidget(self.zoom_label)
        zoom_layout.addWidget(self.zoom_slider)

        controls_wrapper.addLayout(buttons_layout)
        controls_wrapper.addLayout(zoom_layout)

        # Camera settings (collapsible)
        self.cam_settings_btn = QPushButton("Camera Settings ▼")
        self.cam_settings_btn.setCheckable(True)
        self.cam_settings_btn.setChecked(False)
        self.cam_settings_btn.clicked.connect(self._toggle_cam_settings)

        self.cam_settings_panel = QGroupBox()
        self.cam_settings_panel.setFlat(True)
        cam_grid = QGridLayout(self.cam_settings_panel)
        cam_grid.setContentsMargins(4, 4, 4, 4)
        cam_grid.setSpacing(4)

        self._cam_prop_sliders = {}  # prop -> (slider, val_label)
        self._cam_prop_defaults = {}  # prop -> initial value

        cam_props = [
            ("Brightness", cv2.CAP_PROP_BRIGHTNESS),
            ("Contrast",   cv2.CAP_PROP_CONTRAST),
            ("Saturation", cv2.CAP_PROP_SATURATION),
            ("Sharpness",  cv2.CAP_PROP_SHARPNESS),
        ]
        for row, (name, prop) in enumerate(cam_props):
            initial = self.cap.get(prop)
            if initial < 0 or initial != initial:  # invalid / NaN
                initial = 128
            initial = int(initial)
            self._cam_prop_defaults[prop] = initial

            lbl = QLabel(name)
            lbl.setFixedWidth(68)

            slider = QSlider(Qt.Horizontal)
            slider.setRange(0, 255)
            slider.setValue(initial)

            val_lbl = QLabel(str(initial))
            val_lbl.setFixedWidth(28)
            val_lbl.setAlignment(Qt.AlignRight | Qt.AlignVCenter)

            reset_btn = QPushButton("↺")
            reset_btn.setFixedWidth(24)
            reset_btn.setToolTip(f"Reset {name}")
            reset_btn.clicked.connect(lambda _, p=prop: self._reset_cam_prop(p))

            slider.valueChanged.connect(lambda v, p=prop, vl=val_lbl: self._on_cam_prop_changed(p, v, vl))

            cam_grid.addWidget(lbl,       row, 0)
            cam_grid.addWidget(slider,    row, 1)
            cam_grid.addWidget(val_lbl,   row, 2)
            cam_grid.addWidget(reset_btn, row, 3)

            self._cam_prop_sliders[prop] = (slider, val_lbl)

        # Focus row
        focus_row = len(cam_props)
        focus_lbl = QLabel("Focus")
        focus_lbl.setFixedWidth(68)

        self.autofocus_cb = QCheckBox("Auto")
        self.autofocus_cb.setChecked(True)
        self.autofocus_cb.toggled.connect(self._on_autofocus_toggled)

        focus_initial = self.cap.get(cv2.CAP_PROP_FOCUS)
        if focus_initial < 0 or focus_initial != focus_initial:
            focus_initial = 0
        focus_initial = int(focus_initial)

        self.focus_slider = QSlider(Qt.Horizontal)
        self.focus_slider.setRange(0, 255)
        self.focus_slider.setValue(focus_initial)
        self.focus_slider.setEnabled(False)

        self.focus_val_lbl = QLabel(str(focus_initial))
        self.focus_val_lbl.setFixedWidth(28)
        self.focus_val_lbl.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.focus_slider.valueChanged.connect(self._on_focus_changed)

        focus_sub = QWidget()
        focus_sub_layout = QHBoxLayout(focus_sub)
        focus_sub_layout.setContentsMargins(0, 0, 0, 0)
        focus_sub_layout.setSpacing(4)
        focus_sub_layout.addWidget(self.focus_slider)
        focus_sub_layout.addWidget(self.focus_val_lbl)

        tap_lbl = QLabel("<i>Tap preview to refocus</i>")
        tap_lbl.setStyleSheet("color: gray; font-size: 10px;")

        cam_grid.addWidget(focus_lbl,         focus_row, 0)
        cam_grid.addWidget(self.autofocus_cb, focus_row, 1, 1, 3)
        cam_grid.addWidget(focus_sub,         focus_row + 1, 1, 1, 3)
        cam_grid.addWidget(tap_lbl,           focus_row + 2, 1, 1, 3)

        self.cam_settings_panel.setVisible(False)

        controls_wrapper.addWidget(self.cam_settings_btn)
        controls_wrapper.addWidget(self.cam_settings_panel)

        # Install event filter on preview_label for tap-to-focus clicks
        self.preview_label.installEventFilter(self)

        top_layout.addWidget(controls_widget)

        self.lens_spinner = SpinnerWidget(QColor(90, 170, 255))
        top_layout.addWidget(self.lens_spinner)

        self.content_v_splitter.addWidget(top_widget)

        # =========================================================
        # MIDDLE: Results Panel
        # =========================================================
        self.results_container = QScrollArea()
        self.results_container.setWidgetResizable(True)
        self.results_container.setMinimumHeight(60)

        self.results_inner = QWidget()
        self.results_layout = QHBoxLayout(self.results_inner)
        self.results_layout.setAlignment(Qt.AlignLeft)
        self.results_container.setWidget(self.results_inner)

        self.content_v_splitter.addWidget(self.results_container)

        # =========================================================
        # BOTTOM: Info Panel — horizontal splitter: themes | fields
        # =========================================================
        self.info_h_splitter = QSplitter(Qt.Horizontal)
        self.info_h_splitter.setMinimumWidth(0)
        self.info_h_splitter.setMinimumHeight(5 * FIELD_INPUT_HEIGHT + 220)

        # --- Themes side ---
        themes_widget = QWidget()
        themes_widget.setMinimumWidth(PANEL_THEMES_MIN_W)
        themes_layout = QVBoxLayout(themes_widget)
        themes_layout.setContentsMargins(6, 4, 6, 6)

        themes_header = QHBoxLayout()
        themes_header.addWidget(QLabel("Themes:"))
        themes_header.addStretch()
        self.theme_sort_btn = QPushButton("A-Z")
        self.theme_sort_btn.setFixedWidth(54)
        self.theme_sort_btn.setToolTip("Toggle sort: alphabetical / recently used / selected first")
        self.theme_sort_btn.clicked.connect(self._toggle_theme_sort)
        themes_header.addWidget(self.theme_sort_btn)
        themes_layout.addLayout(themes_header)

        self.unchecked_chk = QCheckBox("Unchecked")
        self.overprints_chk = QCheckBox("Has Overprints/Surcharges")
        self.unchecked_chk.clicked.connect(
            lambda checked: self._quick_theme_toggled("Unchecked", checked))
        self.overprints_chk.clicked.connect(
            lambda checked: self._quick_theme_toggled("Has Overprints/Surcharges", checked))
        themes_layout.addWidget(self.unchecked_chk)
        themes_layout.addWidget(self.overprints_chk)

        self.theme_search = QLineEdit()
        self.theme_search.setPlaceholderText("Search themes…")
        self.theme_search.textChanged.connect(self._filter_themes)
        themes_layout.addWidget(self.theme_search)

        input_layout = QHBoxLayout()
        self.theme_input = QLineEdit()
        self.theme_input.setPlaceholderText("Add new theme...")
        self.add_theme_btn = QPushButton("Add Theme")
        self.add_theme_btn.clicked.connect(self.add_theme)
        input_layout.addWidget(self.theme_input)
        input_layout.addWidget(self.add_theme_btn)
        themes_layout.addLayout(input_layout)

        aliases_btn = QPushButton("Manage Aliases…")
        aliases_btn.clicked.connect(lambda: TagAliasDialog(self).exec())
        themes_layout.addWidget(aliases_btn)

        implications_btn = QPushButton("Manage Implications…")
        implications_btn.clicked.connect(lambda: ThemeImplicationsDialog(self).exec())
        themes_layout.addWidget(implications_btn)

        self.themes_input = QListWidget()
        self.themes_input.setSelectionMode(QListWidget.NoSelection)
        self.themes_input.setContextMenuPolicy(Qt.CustomContextMenu)
        self.themes_input.customContextMenuRequested.connect(self._show_theme_context_menu)
        self.themes_input.itemSelectionChanged.connect(self._on_theme_selection_changed)
        self.themes_input.itemClicked.connect(self._on_theme_item_clicked)
        themes_layout.addWidget(self.themes_input)

        self.info_h_splitter.addWidget(themes_widget)

        # --- Fields side ---
        fields_widget = QWidget()
        fields_widget.setMinimumWidth(0)
        fields_widget.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        fields_outer = QVBoxLayout(fields_widget)
        fields_outer.setContentsMargins(0, 4, 6, 6)
        fields_outer.setSpacing(4)

        self.scott_panel = QLabel("Most frequent Scott #: None")
        fields_outer.addWidget(self.scott_panel)

        self.country_panel = QLabel("Most likely country: —")
        fields_outer.addWidget(self.country_panel)

        fields_layout = QGridLayout()
        fields_layout.setHorizontalSpacing(10)
        fields_layout.setVerticalSpacing(5)
        fields_layout.setColumnMinimumWidth(5, 180)

        # Row 0
        fields_layout.addWidget(QLabel("Title:"), 0, 0)
        self.title_input = QLineEdit()
        fields_layout.addWidget(self.title_input, 0, 1)

        fields_layout.addWidget(QLabel("Scott #:"), 0, 2)
        self.scott_input = QLineEdit()
        self.scott_input.setFixedWidth(50)
        self.scott_input.textChanged.connect(self._schedule_duplicate_check)
        fields_layout.addWidget(self.scott_input, 0, 3)

        fields_layout.addWidget(QLabel("Country:"), 0, 4)
        country_container = QWidget()
        country_container.setMinimumWidth(30)
        country_hl = QHBoxLayout(country_container)
        country_hl.setContentsMargins(0, 0, 0, 0)
        country_hl.setSpacing(2)
        self.country_input = QLineEdit()
        self.country_input.editingFinished.connect(lambda:
                self.country_input.setText(
                get_country_name(self.country_input.text().strip())
                or self.country_input.text()))
        self.country_input.textChanged.connect(self._auto_expand_country)
        self.country_input.textChanged.connect(self._schedule_duplicate_check)
        country_browse_btn = QPushButton("▼")
        country_browse_btn.setFixedWidth(24)
        country_browse_btn.setToolTip("Browse countries")
        country_browse_btn.clicked.connect(self._browse_country)
        country_hl.addWidget(self.country_input)
        country_hl.addWidget(country_browse_btn)
        fields_layout.addWidget(country_container, 0, 5)

        fields_layout.addWidget(QLabel("Series:"), 0, 6)
        self.series_input = QLineEdit()
        fields_layout.addWidget(self.series_input, 0, 7)
        self.view_series_btn = QPushButton("View in DB")
        self.view_series_btn.setFixedWidth(76)
        self.view_series_btn.setToolTip("Browse stamps in this series from local database")
        self.view_series_btn.setEnabled(False)
        self.view_series_btn.clicked.connect(self._view_series_in_db)
        fields_layout.addWidget(self.view_series_btn, 0, 8)
        self.series_open_url_btn = QPushButton("↗")
        self.series_open_url_btn.setFixedWidth(28)
        self.series_open_url_btn.setToolTip("Open series page on Colnect")
        self.series_open_url_btn.setEnabled(False)
        self.series_open_url_btn.clicked.connect(self._open_series_url)
        fields_layout.addWidget(self.series_open_url_btn, 0, 9)

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

        fields_layout.addWidget(QLabel("Series Complete:"), 1, 8)
        self.series_complete_combo = QComboBox()
        self.series_complete_combo.addItems(["NO", "YES", "YES MISSING VARIANTS", ""])
        fields_layout.addWidget(self.series_complete_combo, 1, 9)

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

        # Row 4
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

        # Row 5: variant set selector — only visible when variants is checked
        self.variant_set_row = QWidget()
        vs_row_layout = QHBoxLayout(self.variant_set_row)
        vs_row_layout.setContentsMargins(0, 2, 0, 2)
        vs_row_layout.addWidget(QLabel("Variant Set:"))
        self.variant_set_label = QLabel("None")
        self.variant_set_label.setStyleSheet("color: gray; font-style: italic;")
        vs_row_layout.addWidget(self.variant_set_label)
        self.variant_set_btn = QPushButton("Select…")
        self.variant_set_btn.clicked.connect(self._open_variant_set_picker)
        vs_row_layout.addWidget(self.variant_set_btn)
        vs_row_layout.addStretch()
        self.variant_set_row.setVisible(False)
        fields_layout.addWidget(self.variant_set_row, 5, 0, 1, 8)

        self.variant_set_notes_lbl = QLabel("")
        self.variant_set_notes_lbl.setStyleSheet("color: gray; font-style: italic; font-size: 10px;")
        self.variant_set_notes_lbl.setWordWrap(True)
        self.variant_set_notes_lbl.setVisible(False)
        fields_layout.addWidget(self.variant_set_notes_lbl, 6, 0, 1, 8)

        self.variants_checkbox.toggled.connect(self.variant_set_row.setVisible)
        self.variants_checkbox.toggled.connect(
            lambda checked: self.variant_set_notes_lbl.setVisible(
                checked and bool(self.variant_set_notes_lbl.text())
            )
        )

        fields_outer.addLayout(fields_layout)

        # Duplicate-stamp warning (shown when Scott # + country match an existing stamp)
        self.duplicate_warning_label = QLabel()
        self.duplicate_warning_label.setOpenExternalLinks(False)
        self.duplicate_warning_label.linkActivated.connect(self._show_duplicate_preview)
        self.duplicate_warning_label.setVisible(False)
        fields_outer.addWidget(self.duplicate_warning_label)
        self._duplicate_stamp_id = None

        # Series comments row (shown whenever a series is linked)
        self.series_comments_row = QWidget()
        scr_layout = QHBoxLayout(self.series_comments_row)
        scr_layout.setContentsMargins(0, 0, 0, 0)
        scr_layout.addWidget(QLabel("Series Notes:"))
        self.series_comments_input = QLineEdit()
        self.series_comments_input.setPlaceholderText("Notes about this series…")
        scr_layout.addWidget(self.series_comments_input)
        self.series_comments_row.setVisible(False)
        fields_outer.addWidget(self.series_comments_row)

        # Physical location row
        location_row = QHBoxLayout()
        location_row.addWidget(QLabel("Physical Location:"))
        self.location_combo = QComboBox()
        self.location_combo.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        location_row.addWidget(self.location_combo)
        add_loc_btn = QPushButton("+")
        add_loc_btn.setFixedWidth(28)
        add_loc_btn.setToolTip("Add new location")
        add_loc_btn.clicked.connect(self._add_new_location)
        location_row.addWidget(add_loc_btn)
        location_row.addStretch()
        fields_outer.addLayout(location_row)
        self._load_location_combo()

        # Copies section (quantity + condition per row)
        copies_section = QVBoxLayout()
        copies_section.setContentsMargins(0, 0, 0, 0)
        copies_section.setSpacing(0)

        copies_header = QHBoxLayout()
        copies_header.setContentsMargins(0, 0, 0, 0)
        copies_header.setSpacing(0)
        copies_lbl = QLabel("Copies:")
        add_copy_btn = QPushButton("+ Add Row")
        add_copy_btn.setFixedWidth(90)
        add_copy_btn.clicked.connect(lambda: self._add_copy_row())
        copies_header.addWidget(copies_lbl)
        copies_header.addStretch()
        copies_header.addWidget(add_copy_btn)
        copies_section.addLayout(copies_header)

        self.copies_scroll = QScrollArea()
        self.copies_scroll.setWidgetResizable(True)
        self.copies_scroll.setMinimumHeight(FIELD_INPUT_HEIGHT * 2)
        self.copies_scroll.setMaximumHeight(FIELD_INPUT_HEIGHT * 4)
        self.copies_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.copies_scroll.setFrameShape(QFrame.NoFrame)
        self.copies_inner = QWidget()
        self.copies_layout = QVBoxLayout(self.copies_inner)
        self.copies_layout.setContentsMargins(0, 0, 0, 0)
        self.copies_layout.setSpacing(0)
        self.copies_layout.setAlignment(Qt.AlignTop)
        self.copies_scroll.setWidget(self.copies_inner)
        copies_section.addWidget(self.copies_scroll)

        fields_outer.addLayout(copies_section)

        # Action buttons live below the grid so they don't shift when row 5 appears
        action_row = QHBoxLayout()
        self.save_btn = QPushButton("Add Stamp to Database")
        self.save_btn.clicked.connect(self.save_stamp_to_db)
        self.delete_btn = QPushButton("Delete Stamp")
        self.delete_btn.setVisible(False)
        self.delete_btn.clicked.connect(self.delete_stamp)
        self.search_colnect_btn = QPushButton("Search Colnect")
        self.search_colnect_btn.clicked.connect(
            lambda: self.search_colnect(self.scott_input.text(), self.country_input.text()))
        self.get_colnect_info_btn = QPushButton("Get Colnect Info")
        self.get_colnect_info_btn.clicked.connect(self.get_stamp_info)
        self.reassign_img_btn = QPushButton("Reassign Image →")
        self.reassign_img_btn.setVisible(False)
        self.reassign_img_btn.clicked.connect(self._reassign_image)
        self.browse_history_btn = QPushButton("Browse History…")
        self.browse_history_btn.setVisible(False)
        self.browse_history_btn.clicked.connect(self._assign_from_history)
        action_row.addWidget(self.save_btn)
        action_row.addWidget(self.delete_btn)
        action_row.addWidget(self.reassign_img_btn)
        action_row.addWidget(self.browse_history_btn)
        action_row.addWidget(self.search_colnect_btn)
        action_row.addWidget(self.get_colnect_info_btn)
        fields_outer.addLayout(action_row)

        self.colnect_spinner = SpinnerWidget(QColor(80, 200, 160))
        fields_outer.addWidget(self.colnect_spinner)
        fields_outer.addStretch(1)

        for _le in fields_widget.findChildren(QLineEdit):
            _le.setFixedHeight(FIELD_INPUT_HEIGHT)

        self.info_h_splitter.addWidget(fields_widget)

        self.info_h_splitter.setSizes(SPLITTER_INFO_H_INIT)
        self.content_v_splitter.addWidget(self.info_h_splitter)

        # =========================================================
        # History Sidebar (rightmost in content_h_splitter)
        # =========================================================
        self.history_container = QScrollArea()
        self.history_container.setWidgetResizable(True)
        self.history_container.setMinimumWidth(PANEL_HISTORY_MIN_W)
        self.history_container.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.history_container.setFrameShape(QFrame.NoFrame)

        self.history_inner = QWidget()
        self.history_inner.setMinimumWidth(0)
        self.history_layout = QVBoxLayout(self.history_inner)
        self.history_layout.setAlignment(Qt.AlignTop)
        self.history_container.setWidget(self.history_inner)
        self.history_layout.addWidget(QLabel("History"))

        self.content_h_splitter.addWidget(self.history_container)

        # Initial sizes
        self.content_v_splitter.setSizes(SPLITTER_CONTENT_V_INIT)
        self.content_h_splitter.setSizes(SPLITTER_CONTENT_H_INIT)

        self._set_add_mode()

    def _schedule_duplicate_check(self):
        """Restart the debounce timer so the DB query only fires after typing pauses."""
        self._dup_check_timer.start()

    def _check_duplicate_stamp(self):
        if getattr(self, 'duplicate_warning_label', None) is None:
            return

        # Only relevant when adding a new stamp — editing an existing one will
        # naturally match itself, which isn't a duplicate.
        if self.current_stamp_id:
            self.duplicate_warning_label.setVisible(False)
            self._duplicate_stamp_id = None
            return

        scott = self.scott_input.text().strip()
        country = self.country_input.text().strip()
        if not scott or not country:
            self.duplicate_warning_label.setVisible(False)
            self._duplicate_stamp_id = None
            return

        session = SessionLocal()
        try:
            existing = session.query(Stamp).filter(
                Stamp.scott_number == scott,
                Stamp.country == country,
            ).first()
            existing_id = existing.id if existing else None
        finally:
            session.close()

        if existing_id:
            self._duplicate_stamp_id = existing_id
            self.duplicate_warning_label.setText(
                '<span style="color:red;">⚠ Already in your collection — '
                '<a href="preview" style="color:red;">view</a></span>'
            )
            self.duplicate_warning_label.setVisible(True)
        else:
            self._duplicate_stamp_id = None
            self.duplicate_warning_label.setVisible(False)

    def _show_duplicate_preview(self, _href=None):
        if not self._duplicate_stamp_id:
            return
        dlg = DuplicateStampPreviewDialog(self, self._duplicate_stamp_id)
        dlg.exec()
        if dlg.edit_requested:
            self.load_stamp_into_editor(self._duplicate_stamp_id)

    def save_stamp_to_db(self):
        print(self.current_image_path)
        if not self.current_image_path and not self.current_stamp_id:
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
            "owned": True,
            "variant_set_id": self.current_variant_set_id if self.variants_checkbox.isChecked() else None,
            "themes": [item.text() for item in self.themes_input.selectedItems()],
            "image_path": self.current_image_path,
            "series_id": self.current_series_id,
            "physical_location_id": self.location_combo.currentData(),
        }
        copies_data = self._get_copies_data()
        series_complete = self.series_complete_combo.currentText() or None
        series_comments = self.series_comments_input.text().strip() or None
        # Pre-check for duplicates before opening the main session, so the error
        # dialog is shown cleanly without any partial session state to clean up.
        if not self.current_stamp_id:
            scott = stamp_data.get("scott_number", "").strip()
            country = stamp_data.get("country", "").strip()
            if scott and country:
                with SessionLocal() as check_session:
                    existing = check_session.query(Stamp).filter(
                        Stamp.scott_number == scott,
                        Stamp.country == country,
                    ).first()
                if existing:
                    self._show_warning(
                        "Duplicate Stamp",
                        f"Scott #{scott} from {country} is already in your collection."
                    )
                    return

        # A new stamp's image is a fresh capture in INCOMING_DIR; move it into the
        # associated-images folder before writing the record. Reverted below if
        # the save fails. Editing an existing stamp leaves its image untouched.
        incoming_path = None
        if not self.current_stamp_id and self.current_image_path:
            associated_path = associate_image(self.current_image_path)
            if associated_path != self.current_image_path:
                incoming_path = self.current_image_path
                stamp_data["image_path"] = associated_path

        saved_ok = False
        session = SessionLocal()
        try:
            series_name = stamp_data.get("series", "").strip()
            if series_name and not self.current_series_id:
                # No Colnect-linked series yet (manual entry) — link by name so that
                # series_complete still lives on the shared Series row.
                s = SeriesService.get_or_create_by_name(session, series_name, _commit=False)
                self.current_series_id = s.id
                stamp_data["series_id"] = self.current_series_id

            if self.current_stamp_id:
                StampService.update_stamp(session, self.current_stamp_id, _commit=False, **stamp_data)
                StampCopyService.set_copies(session, self.current_stamp_id, copies_data, _commit=False)
                logger.info(f"Stamp ID {self.current_stamp_id} updated successfully")
            else:
                new_stamp = StampService.create_stamp(session, _commit=False, **stamp_data)
                StampCopyService.set_copies(session, new_stamp.id, copies_data, _commit=False)
                logger.info(f"Stamp '{new_stamp.title}' added successfully")
            if self.current_series_id:
                SeriesService.update(session, self.current_series_id, _commit=False,
                                     series_complete=series_complete,
                                     comments=series_comments)
            # Single commit after all operations — avoids the multiple BEGIN/COMMIT
            # cycles that were causing psycopg2 connection state churn.
            session.commit()
            saved_ok = True
        except ValueError as e:
            session.rollback()
            logger.error(f"Validation error: {e}")
            self._show_warning("Cannot Save", str(e))
        except Exception as e:
            session.rollback()
            logger.error(f"Failed to save stamp: {e}")
            self._show_error("Save Failed", f"An error occurred while saving the stamp:\n\n{e}")
        finally:
            session.close()  # always close before any Qt widget operations

        if saved_ok:
            if incoming_path:
                # Capture is now assigned — drop its incoming/history thumbnail.
                lbl = self._history_labels.pop(incoming_path, None)
                if lbl is not None:
                    lbl.setParent(None)
                    lbl.deleteLater()
            # Defer all widget operations to the next event loop iteration so that
            # save_stamp_to_db's call stack is fully unwound before Qt touches the
            # list widget. This prevents the access violation at db_list.clear().
            QTimer.singleShot(0, self._post_save_refresh)
        elif incoming_path:
            # Save failed after the move — return the file to incoming.
            deassociate_image(stamp_data["image_path"])


    def _post_save_refresh(self):
        self._refresh_db_view()
        self._reset_stamp_form()   # sets current_stamp_id=None, calls _set_add_mode
        if self._gallery_open:
            self._populate_gallery_filters()
            self.load_gallery()

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
        self.current_variant_set_id = None
        self.variant_set_label.setText("None")
        self.variant_set_label.setStyleSheet("color: gray; font-style: italic;")
        if getattr(self, 'variant_set_notes_lbl', None):
            self.variant_set_notes_lbl.setText("")
            self.variant_set_notes_lbl.setVisible(False)
        self.current_series_id = None
        if getattr(self, 'series_open_url_btn', None):
            self.series_open_url_btn.setEnabled(False)
        if getattr(self, 'view_series_btn', None):
            self.view_series_btn.setEnabled(False)
        if getattr(self, 'series_complete_combo', None):
            self.series_complete_combo.setCurrentIndex(0)
        if getattr(self, 'series_comments_input', None):
            self.series_comments_input.clear()
        if getattr(self, 'series_comments_row', None):
            self.series_comments_row.setVisible(False)
        if getattr(self, 'location_combo', None):
            self.location_combo.setCurrentIndex(0)
        if getattr(self, 'copies_layout', None):
            self._clear_copies()

        self.themes_input.blockSignals(True)
        for i in range(self.themes_input.count()):
            self.themes_input.item(i).setSelected(False)
        self.themes_input.blockSignals(False)
        self._sync_quick_theme_checkboxes()

        self.current_image_path = None
        self.current_stamp_id = None
        self._set_add_mode()


    def _clear_metadata_fields(self):
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

        self.themes_input.blockSignals(True)
        for i in range(self.themes_input.count()):
            self.themes_input.item(i).setSelected(False)
        self.themes_input.blockSignals(False)
        self._sync_quick_theme_checkboxes()

        self.scott_panel.setText("Most frequent Scott #: ")
        if getattr(self, 'country_panel', None):
            self.country_panel.setText("Most likely country: —")


    def add_theme(self, theme=None):
        new_theme = self.theme_input.text().strip()
        if not new_theme:
            if theme is not None:
                new_theme = theme.strip()
        if not new_theme:
            return

        logger.debug(f"Adding theme: {new_theme}")
        session = SessionLocal()

        # Check if the theme already exists in the list
        for i in range(self.themes_input.count()):
            if self.themes_input.item(i).text().strip().lower() == new_theme.lower():
                self._set_themes_interactive(True)
                # Block signals to avoid triggering load_themes() (which would clear
                # the list mid-setSelected) while selecting the item.
                self.themes_input.blockSignals(True)
                self.themes_input.item(i).setSelected(True)
                self.themes_input.blockSignals(False)
                self.theme_input.clear()
                self.themes_input.repaint()
                if self._theme_sort == "selected":
                    self.load_themes()
                session.close()
                return

        try:
            ThemeService.create_or_get_theme(session, new_theme)
            self._set_themes_interactive(True)
            # Block signals before manipulating items: selecting the new item would
            # fire itemSelectionChanged → load_themes() → clear(), which deletes the
            # C++ item object while Qt is still mid-processing setSelected → crash.
            self.themes_input.blockSignals(True)
            new_item = QListWidgetItem(new_theme)
            self.themes_input.addItem(new_item)
            new_item.setSelected(True)
            self.themes_input.blockSignals(False)
            self.themes_input.repaint()
            if self._theme_sort == "selected":
                self.load_themes()
        except Exception as e:
            logger.error(f"Failed to add theme '{new_theme}': {e}")
        finally:
            self.theme_input.clear()
            session.close()




    def _show_theme_context_menu(self, pos):
        item = self.themes_input.itemAt(pos)
        if not item:
            return
        menu = QMenu(self)
        rename_action = menu.addAction("Rename tag…")
        delete_action = menu.addAction("Delete tag…")
        action = menu.exec(self.themes_input.mapToGlobal(pos))
        if action == rename_action:
            self._rename_theme(item)
        elif action == delete_action:
            self._delete_theme(item)

    def _rename_theme(self, item):
        old_name = item.text()
        new_name, ok = QInputDialog.getText(self, "Rename Tag", "New name:", text=old_name)
        if not ok or not new_name.strip() or new_name.strip() == old_name:
            return
        new_name = new_name.strip()
        session = SessionLocal()
        try:
            success = ThemeService.rename_theme(session, old_name, new_name)
            if success:
                item.setText(new_name)
            else:
                QMessageBox.warning(self, "Rename Failed",
                    f"Could not rename '{old_name}' — name may already exist.")
        except Exception as e:
            logger.error(f"Error renaming theme '{old_name}': {e}")
        finally:
            session.close()

    def _delete_theme(self, item):
        name = item.text()
        reply = QMessageBox.warning(
            self, "Delete Tag",
            f"Delete tag \"{name}\"?\n\nIt will be removed from all stamps, but the stamps will not be deleted.",
            QMessageBox.Ok | QMessageBox.Cancel,
        )
        if reply != QMessageBox.Ok:
            return
        session = SessionLocal()
        try:
            theme = session.query(Theme).filter_by(name=name).one_or_none()
            if theme:
                ThemeService.delete_theme(session, theme.id)
                self.themes_input.takeItem(self.themes_input.row(item))
            else:
                QMessageBox.warning(self, "Delete Failed", f"Tag \"{name}\" not found in database.")
        except Exception as e:
            logger.error(f"Error deleting theme '{name}': {e}")
        finally:
            session.close()

    def _filter_themes(self, text: str = ""):
        text = (text or "").strip().lower()
        for i in range(self.themes_input.count()):
            item = self.themes_input.item(i)
            item.setHidden(bool(text) and text not in item.text().lower())

    def _on_theme_selection_changed(self):
        if self._theme_sort != "selected" or self._reloading_themes:
            return
        try:
            self.load_themes()
        except Exception as e:
            logger.error(f"Failed to reload themes on selection change: {e}")

    def _on_theme_item_clicked(self, item):
        if not item.isSelected():
            return
        implications = load_theme_implications()
        implied = implications.get(item.text(), [])
        if not implied:
            return
        implied_lower = {x.lower() for x in implied}
        self.themes_input.blockSignals(True)
        for i in range(self.themes_input.count()):
            list_item = self.themes_input.item(i)
            if list_item.text().lower() in implied_lower:
                list_item.setSelected(True)
        self.themes_input.blockSignals(False)
        if self._theme_sort == "selected":
            try:
                self.load_themes()
            except Exception as e:
                logger.error(f"Failed to reload themes after implication: {e}")

    def _toggle_theme_sort(self):
        modes = ("alpha", "recent", "selected")
        labels = {"alpha": "A-Z", "recent": "Recent", "selected": "★"}
        try:
            idx = modes.index(self._theme_sort)
        except ValueError:
            idx = 0
        self._theme_sort = modes[(idx + 1) % len(modes)]
        self.theme_sort_btn.setText(labels[self._theme_sort])
        try:
            self.load_themes()
        except Exception as e:
            logger.error(f"Failed to reload themes after sort change: {e}")

    def load_themes(self):
        # selectedItems() returns existing Python wrappers without creating one
        # per-index; avoids ~500 temporary QListWidgetItem wrapper allocations
        # that the old item(i).isSelected() loop produced for a 250-item list.
        previously_selected = {item.text() for item in self.themes_input.selectedItems()}

        session = SessionLocal()
        try:
            # Block itemSelectionChanged for the entire rebuild so that setSelected()
            # calls below don't re-enter load_themes() or _on_theme_selection_changed.
            self.themes_input.blockSignals(True)
            self.themes_input.clear()
            if self._theme_sort == "recent":
                themes = ThemeService.get_themes_by_recent_use(session)
            else:
                themes = ThemeService.get_all_themes(session)

            if self._theme_sort == "selected":
                selected   = [t for t in themes if t.name in previously_selected]
                unselected = [t for t in themes if t.name not in previously_selected]
                themes = selected + unselected

            names = [theme.name for theme in themes]
            # addItems() is a single C++ batch operation: Qt creates all items
            # internally without Python-level QListWidgetItem wrapper objects,
            # avoiding 250+ individual Python↔C++ ownership handoffs.
            self.themes_input.addItems(names)
            if previously_selected:
                for i, name in enumerate(names):
                    if name in previously_selected:
                        self.themes_input.item(i).setSelected(True)
            logger.info(f"Loaded {len(themes)} themes")
        except Exception as e:
            logger.error(f"Failed to load themes: {e}")
        finally:
            self.themes_input.blockSignals(False)
            session.close()
        # Re-apply any active search after reloading
        if getattr(self, 'theme_search', None):
            self._filter_themes(self.theme_search.text())





    # ---------- Camera Preview ----------
    def update_frame(self):
        if self.preview_mode == "live":
            ret, frame = self.cap.read()
            if ret:
                self._cam_fail_count = 0
                if self._cam_rotation:
                    _rot_map = {
                        90:  cv2.ROTATE_90_CLOCKWISE,
                        180: cv2.ROTATE_180,
                        270: cv2.ROTATE_90_COUNTERCLOCKWISE,
                    }
                    frame = cv2.rotate(frame, _rot_map[self._cam_rotation])
                self.current_frame = frame
                self.display_frame(frame)
            else:
                self._cam_fail_count += 1
                # After ~1 second of consecutive failures, attempt to reconnect
                if self._cam_fail_count >= CAMERA_FPS:
                    self._cam_fail_count = 0
                    self._try_reconnect_camera()

    def _rotate_camera(self):
        self._cam_rotation = (self._cam_rotation + 90) % 360
        self.rotate_btn.setToolTip(f"Rotate camera 90° clockwise (current: {self._cam_rotation}°)")
        if self.preview_mode == "image" and self.current_frame is not None:
            self.current_frame = cv2.rotate(self.current_frame, cv2.ROTATE_90_CLOCKWISE)
            self.display_frame(self.current_frame)
            if self.current_image_path:
                cv2.imwrite(self.current_image_path, self.current_frame)
                label = self._history_labels.get(self.current_image_path)
                if label:
                    pixmap = QPixmap(self.current_image_path).scaled(
                        THUMB_SIZE[0], THUMB_SIZE[1], Qt.KeepAspectRatio, Qt.SmoothTransformation
                    )
                    label.setPixmap(pixmap)

    def _open_crop_dialog(self):
        if not self.current_image_path or not os.path.exists(self.current_image_path):
            return
        dlg = CropDialog(self.current_image_path, self)
        if dlg.exec() == QDialog.Accepted:
            img = cv2.imread(self.current_image_path)
            if img is not None:
                self.current_frame = img
                self.display_frame(img)

    def _open_camera(self) -> cv2.VideoCapture:
        """Open the camera using MSMF (Windows Media Foundation) only.
        DSHOW is intentionally excluded — its COM buffer allocator has a bug that
        gradually corrupts the process heap at 30 fps, eventually causing access
        violations in unrelated Qt code. If MSMF fails to deliver frames, run
        camera-less; the user can still import images manually.
        MSMF can report isOpened()=True yet fail to grab any frames on some driver
        stacks (error 0xC00D36B4). Try a few explicit-resolution variants to work
        around driver negotiation failures before giving up."""
        for w, h in [(0, 0), (640, 480), (1280, 720)]:
            cap = cv2.VideoCapture(CAMERA_INDEX, cv2.CAP_MSMF)
            if not cap.isOpened():
                cap.release()
                continue
            if w:
                cap.set(cv2.CAP_PROP_FRAME_WIDTH, w)
                cap.set(cv2.CAP_PROP_FRAME_HEIGHT, h)
            for _ in range(5):  # drain frames while MSMF pipeline warms up
                ret, _ = cap.read()
                if ret:
                    logger.info(f"Camera opened with MSMF backend ({w or 'default'}x{h or 'default'})")
                    return cap
            cap.release()
        logger.warning("MSMF camera probe failed — running in image-only mode (DSHOW excluded)")
        return cv2.VideoCapture()  # null capture; isOpened() == False

    def _try_reconnect_camera(self):
        try:
            self.cap.release()
        except Exception:
            pass
        try:
            self.cap = self._open_camera()
            if self.cap.isOpened():
                self._reapply_camera_settings()
                logger.info("Camera reconnected successfully")
            else:
                logger.debug("Camera reconnection attempt failed — will retry")
        except Exception as e:
            logger.debug(f"Camera reconnection error: {e}")

    def _get_overrides_mtime(self) -> float:
        try:
            if COUNTRY_OVERRIDES_FILE and os.path.exists(COUNTRY_OVERRIDES_FILE):
                return os.path.getmtime(COUNTRY_OVERRIDES_FILE)
        except Exception:
            pass
        return 0.0

    def _check_overrides_file(self):
        mtime = self._get_overrides_mtime()
        if mtime != self._overrides_mtime:
            self._overrides_mtime = mtime
            reload_country_overrides()
            logger.info("country_overrides.json reloaded")

    def _reapply_camera_settings(self):
        """Re-apply slider values to a freshly opened camera."""
        for prop, (slider, _) in getattr(self, '_cam_prop_sliders', {}).items():
            self.cap.set(prop, slider.value())
        if getattr(self, 'autofocus_cb', None):
            auto = self.autofocus_cb.isChecked()
            self.cap.set(cv2.CAP_PROP_AUTOFOCUS, 1 if auto else 0)
            if not auto and getattr(self, 'focus_slider', None):
                self.cap.set(cv2.CAP_PROP_FOCUS, self.focus_slider.value())

    def display_frame(self, frame):
        # Apply center-crop zoom if zoom > 1.0, then scale to preview label
        fh, fw = frame.shape[:2]
        zoom = getattr(self, 'zoom', 1.0) or 1.0

        if zoom > 1.0:
            crop_w = max(1, int(fw / zoom))
            crop_h = max(1, int(fh / zoom))
            x1 = max(0, (fw - crop_w) // 2)
            y1 = max(0, (fh - crop_h) // 2)
            src = frame[y1:y1 + crop_h, x1:x1 + crop_w]
        else:
            src = frame

        sh, sw = src.shape[:2]
        w = self.preview_label.width() or sw
        h = self.preview_label.height() or sh
        scale = min(w / sw, h / sh)
        resized = cv2.resize(src, (max(1, int(sw * scale)), max(1, int(sh * scale))))
        rgb = cv2.cvtColor(resized, cv2.COLOR_BGR2RGB)
        img = Image.fromarray(rgb)
        qt_img = QPixmap.fromImage(ImageQt(img))

        # Draw tap-to-focus overlay
        if getattr(self, 'focus_tap_pos', None) is not None:
            tap_x, tap_y = self.focus_tap_pos
            lw = self.preview_label.width()
            lh = self.preview_label.height()
            pw = qt_img.width()
            ph = qt_img.height()
            # Image is centered in the label
            offset_x = (lw - pw) / 2
            offset_y = (lh - ph) / 2
            ix = tap_x - offset_x
            iy = tap_y - offset_y
            box = 60
            painter = QPainter(qt_img)
            painter.setPen(QPen(QColor(255, 215, 0), 2))
            painter.drawRect(int(ix - box // 2), int(iy - box // 2), box, box)
            painter.end()

        self.preview_label.setPixmap(qt_img)

    def _get_cropped_frame(self, frame):
        """Return a cropped-and-scaled BGR image matching the preview's crop/scale."""
        try:
            fh, fw = frame.shape[:2]
            zoom = getattr(self, 'zoom', 1.0) or 1.0

            if zoom > 1.0:
                crop_w = max(1, int(fw / zoom))
                crop_h = max(1, int(fh / zoom))
                x1 = max(0, (fw - crop_w) // 2)
                y1 = max(0, (fh - crop_h) // 2)
                src = frame[y1:y1 + crop_h, x1:x1 + crop_w]
            else:
                src = frame

            sh, sw = src.shape[:2]
            w = self.preview_label.width() or sw
            h = self.preview_label.height() or sh
            scale = min(w / sw, h / sh)
            resized = cv2.resize(src, (max(1, int(sw * scale)), max(1, int(sh * scale))))
            return resized
        except Exception:
            return frame

    # ---------- Camera Settings ----------
    def _toggle_cam_settings(self, checked: bool):
        self.cam_settings_panel.setVisible(checked)
        self.cam_settings_btn.setText("Camera Settings ▲" if checked else "Camera Settings ▼")

    def _on_cam_prop_changed(self, prop: int, value: int, val_lbl: QLabel):
        val_lbl.setText(str(value))
        if getattr(self, 'cap', None) and self.cap.isOpened():
            self.cap.set(prop, value)

    def _reset_cam_prop(self, prop: int):
        default = self._cam_prop_defaults.get(prop, 128)
        slider, _ = self._cam_prop_sliders[prop]
        slider.setValue(default)

    def _on_autofocus_toggled(self, auto: bool):
        if getattr(self, 'cap', None) and self.cap.isOpened():
            self.cap.set(cv2.CAP_PROP_AUTOFOCUS, 1 if auto else 0)
        self.focus_slider.setEnabled(not auto)

    def _on_focus_changed(self, value: int):
        self.focus_val_lbl.setText(str(value))
        if getattr(self, 'cap', None) and self.cap.isOpened():
            self.cap.set(cv2.CAP_PROP_FOCUS, value)

    def eventFilter(self, obj, event):
        preview_label = getattr(self, 'preview_label', None)
        if preview_label is not None and obj is preview_label \
                and event.type() == QEvent.Type.MouseButtonPress:
            if self.preview_mode == "live":
                pos = event.position()
                self._handle_focus_tap(pos.x(), pos.y())
        gallery_scroll = getattr(self, 'gallery_scroll', None)
        if gallery_scroll is not None and obj is gallery_scroll \
                and event.type() == QEvent.Type.Resize:
            if self._gallery_open:
                self._gallery_resize_timer.start(180)
        return super().eventFilter(obj, event)

    def _on_gallery_resize(self):
        if self._gallery_open:
            self.load_gallery()

    def _handle_focus_tap(self, x: float, y: float):
        self.focus_tap_pos = (x, y)
        self.focus_tap_timer.start(1500)
        # Force a refocus: toggle autofocus off then on (works on cameras that support it)
        if getattr(self, 'autofocus_cb', None) and self.autofocus_cb.isChecked():
            self.cap.set(cv2.CAP_PROP_AUTOFOCUS, 0)
            self.cap.set(cv2.CAP_PROP_AUTOFOCUS, 1)

    def _clear_focus_tap(self):
        self.focus_tap_pos = None

    # ---------- Modes ----------
    def set_live_mode(self):
        self.preview_mode = "live"
        self.capture_btn.setVisible(True)
        self.capture_only_btn.setVisible(True)
        self.camera_btn.setVisible(False)
        self.search_btn.setVisible(False)
        self.crop_btn.setVisible(False)

        # Reopen the camera if it was released when leaving live mode.
        if not getattr(self, 'cap', None) or not self.cap.isOpened():
            self.cap = self._open_camera()
            if self.cap.isOpened():
                self._reapply_camera_settings()
            else:
                logger.warning("Camera could not be reopened when returning to live mode")
        # Restart the frame timer (hasattr guard: timer is created after set_live_mode
        # is called during __init__, so it may not exist yet at that point).
        if hasattr(self, 'timer') and not self.timer.isActive():
            self.timer.start(int(1000 / CAMERA_FPS))

    def set_image_mode(self):
        self.preview_mode = "image"
        self.capture_btn.setVisible(False)
        self.capture_only_btn.setVisible(False)
        self.camera_btn.setVisible(True)
        self.search_btn.setVisible(True)
        self.crop_btn.setVisible(True)
        # Stop the frame timer and release the camera while viewing a static image.
        # The camera's MSMF/DSHOW internal capture thread runs continuously at 30 fps
        # even when the feed is not displayed, and is a known source of heap corruption
        # on Windows. Releasing it eliminates that background activity entirely while
        # the camera is not needed.
        if hasattr(self, 'timer'):
            self.timer.stop()
        if getattr(self, 'cap', None) and self.cap.isOpened():
            self.cap.release()

    # ---------- Actions ----------
    def _do_capture(self) -> str | None:
        """Save the current frame to disk and switch to image mode. Returns the saved path."""
        if self.current_frame is None:
            return None
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        # Captures start un-associated in INCOMING_DIR; associate_image() moves
        # them into IMAGE_DIR when they're attached to a stamp.
        path = os.path.join(INCOMING_DIR, f"stamp_{ts}.jpg")
        try:
            # Apply zoom crop at full camera resolution — do NOT scale down to the
            # preview label size, which is what _get_cropped_frame does and is the
            # cause of blurry saved images.
            frame = self.current_frame
            zoom = getattr(self, 'zoom', 1.0) or 1.0
            if zoom > 1.0:
                fh, fw = frame.shape[:2]
                crop_w = max(1, int(fw / zoom))
                crop_h = max(1, int(fh / zoom))
                x1 = max(0, (fw - crop_w) // 2)
                y1 = max(0, (fh - crop_h) // 2)
                to_save = frame[y1:y1 + crop_h, x1:x1 + crop_w]
            else:
                to_save = frame
            cv2.imwrite(path, to_save)
        except Exception:
            cv2.imwrite(path, self.current_frame)
        self.add_history_thumbnail(path)
        self.current_image_path = path
        self.current_stamp_id = None
        self._clear_metadata_fields()
        self._set_add_mode()
        self.set_image_mode()
        self.display_frame(cv2.imread(path))
        return path

    def capture_image(self):
        """Capture the current frame and immediately trigger a Google Lens search."""
        if self._do_capture():
            self.search_image()

    def capture_image_only(self):
        """Capture the current frame without triggering a search."""
        self._do_capture()


    def show_image(self, path, clear_metadata=True):
        image = cv2.imread(path)
        if image is None:
            return
        self.current_image_path = path
        self.current_frame = image
        if clear_metadata:
            self._clear_metadata_fields()
            self._set_add_mode()
        self.set_image_mode()
        self.display_frame(image)
        #self.clear_results()

    def _assign_from_history(self):
        """Open the image browser and assign the chosen image to the current stamp."""
        if not self.current_stamp_id:
            return
        dlg = ImageBrowserDialog(self, current_stamp_id=self.current_stamp_id)
        if dlg.exec() != QDialog.Accepted or not dlg.selected_path:
            return
        path = dlg.selected_path
        session = SessionLocal()
        try:
            existing = session.query(StampImage).filter_by(file_path=path).one_or_none()
            if existing is None:
                ImageService.add_image(session, self.current_stamp_id, path)
            elif existing.stamp_id != self.current_stamp_id:
                existing.stamp_id = self.current_stamp_id
                session.commit()
            # already assigned to this stamp — nothing to do
            self.current_image_path = path
            self.show_image(path, clear_metadata=False)
            if self._gallery_open:
                self.load_gallery()
        except Exception as e:
            logger.error(f"Error assigning image from history: {e}")
            self._show_error("Assign Failed", str(e))
        finally:
            session.close()

    def _reassign_image(self):
        if not self.current_stamp_id:
            return
        if not self.current_image_path:
            self._show_warning("No Image", "This stamp has no image to reassign.")
            return
        dlg = StampPickerDialog(self, exclude_stamp_id=self.current_stamp_id)
        if dlg.exec() != QDialog.Accepted or dlg.selected_id is None:
            return
        session = SessionLocal()
        try:
            ok = ImageService.reassign_image(
                session, self.current_image_path, self.current_stamp_id, dlg.selected_id
            )
            if ok:
                self.current_image_path = None
                self.set_live_mode()
                if self._gallery_open:
                    self.load_gallery()
            else:
                self._show_warning("Reassign Failed",
                                   "Image record not found in the database for this stamp.")
        except Exception as e:
            logger.error(f"Error reassigning image: {e}")
            self._show_error("Reassign Failed", str(e))
        finally:
            session.close()

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

        self.lens_spinner.start("Searching with Google Lens…")
        self.browser_worker.schedule_lens_search(self.current_image_path, request_id)

    def search_colnect(self, scott_number, country):
        # Increment request counter and send search command
        self.request_counter += 1
        request_id = self.request_counter

        self.colnect_spinner.start("Navigating to stamp on Colnect…")
        self.browser_worker.schedule_colnect_search(scott_number, country, request_id)

    def get_stamp_info(self):
        self.request_counter += 1
        request_id = self.request_counter

        self.colnect_spinner.start("Retrieving stamp info from Colnect…")
        self.browser_worker.schedule_colnect_info(request_id)

    def fill_stamp_info(self, info):
        # Block the selection-changed reload for the duration of this fill so that
        # programmatic setSelected calls don't cascade into repeated load_themes()
        # calls while we're mid-iteration over the widget.
        self._reloading_themes = True
        try:
            for i in range(self.themes_input.count()):
                self.themes_input.item(i).setSelected(False)

            # Fill the input fields with the retrieved info
            self.title_input.setText(info.get("name", ""))
            self.scott_input.setText(info.get("scott_number", ""))
            if info.get("country"):
                self.country_input.setText(info["country"])
            series_name = info.get("series") or ""
            self.series_input.setText(series_name)
            series_url = info.get("series_url") or ""
            if series_url and not series_name:
                # Colnect didn't return the series name — derive it from the URL slug.
                # URL pattern: .../series/404427-Slovak_Ornamental_Wireworking
                slug = series_url.rstrip("/").split("/")[-1]
                name_part = slug.split("-", 1)[-1] if "-" in slug else slug
                series_name = name_part.replace("_", " ").strip()
                if series_name:
                    self.series_input.setText(series_name)
            if series_url and series_name:
                session = SessionLocal()
                try:
                    s = SeriesService.get_or_create_by_url(
                        session, name=series_name, url=series_url
                    )
                    self.current_series_id = s.id
                    self.series_open_url_btn.setEnabled(True)
                    self.view_series_btn.setEnabled(True)
                    self.series_comments_input.setText(s.comments or "")
                    self.series_comments_row.setVisible(True)
                    val = s.series_complete or ""
                    idx = self.series_complete_combo.findText(val) if val else 0
                    self.series_complete_combo.setCurrentIndex(max(idx, 0))
                except Exception as e:
                    logger.error(f"Failed to create/find series '{series_name}': {e}")
                    self.current_series_id = None
                finally:
                    session.close()
            else:
                self.current_series_id = None
                self.series_open_url_btn.setEnabled(False)
                self.view_series_btn.setEnabled(False)
                self.series_comments_row.setVisible(False)
                blank_idx = self.series_complete_combo.findText("")
                self.series_complete_combo.setCurrentIndex(blank_idx if blank_idx >= 0 else self.series_complete_combo.count() - 1)
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

            themes = info.get("themes", [])
            logger.debug(f"Colnect scraped themes: {themes}")

            # Populate themes from DB into UI and then add scraped themes if needed
            self.themes_input.clear()
            self.load_themes()
            self._set_themes_interactive(True)

            aliases = load_tag_aliases()
            implications = load_theme_implications()

            # Apply aliases, then expand with implications
            raw_names = []
            for theme in themes:
                name = (theme or "").strip()
                if not name:
                    continue
                name = aliases.get(name, name)
                raw_names.append(name)

            all_names = list(raw_names)
            for name in raw_names:
                for implied in implications.get(name, []):
                    if implied not in all_names:
                        all_names.append(implied)

            session = SessionLocal()
            try:
                selected_names = []
                for name in all_names:
                    selected_names.append(name)
                    existing_item = None

                    for i in range(self.themes_input.count()):
                        item = self.themes_input.item(i)
                        if item.text().strip().lower() == name.lower():
                            existing_item = item
                            break

                    if existing_item:
                        # Block signals for the same reason as add_theme: setSelected
                        # firing itemSelectionChanged while we're mid-iteration is unsafe.
                        self.themes_input.blockSignals(True)
                        existing_item.setSelected(True)
                        self.themes_input.blockSignals(False)
                    else:
                        try:
                            ThemeService.create_or_get_theme(session, name)
                            new_item = QListWidgetItem(name)
                            self.themes_input.blockSignals(True)
                            self.themes_input.addItem(new_item)
                            new_item.setSelected(True)
                            self.themes_input.blockSignals(False)
                        except Exception as e:
                            logger.debug(f"Failed to create/select theme '{name}': {e}")

                if selected_names:
                    logger.debug(f"Displayed themes in UI: {selected_names}")
                    self.themes_input.repaint()
                else:
                    logger.debug("No themes available to display.")
            finally:
                session.close()

        finally:
            # Always release the guard so _on_theme_selection_changed works again,
            # even if an exception occurred partway through the fill.
            self._reloading_themes = False

        # In "selected first" mode rebuild the list now that themes have been
        # applied, so they appear at the top.
        if self._theme_sort == "selected":
            try:
                self.load_themes()
            except Exception as e:
                logger.error(f"Failed to reload themes after Colnect fill: {e}")
        self._sync_quick_theme_checkboxes()

        if self.current_stamp_id:
            self._set_edit_mode(self.current_stamp_id)

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
            self.scott_panel.setText("Most frequent Scott #: None")
            return

        for item in results:
            try:
                thumb_url = item["thumbnail_url"]

                # Load image
                if thumb_url.startswith("data:"):
                    _, encoded = thumb_url.split(",", 1)
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
                btn.clicked.connect(lambda *_, l=item["link"]: self._open_lens_result(l))
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
        label.mousePressEvent = lambda *_, p=path: self.show_image(p)
        self.history_layout.addWidget(label)
        self._history_labels[path] = label

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
            self.add_history_thumbnail(os.path.join(INCOMING_DIR, f))

    # ---------- Lens Polling ----------
    def poll_lens_results(self):
        try:
            while True:
                msg = lens_result_queue.get_nowait()
                self.lens_spinner.stop()
                if msg["error"]:
                    logger.error(f"Lens search failed: {msg['error']}")
                    self._show_warning("Search Failed", f"Google Lens search failed:\n\n{msg['error']}")
                    continue

                # Derived values are computed in the browser subprocess and
                # included in the message so the main process needs no _lens reference.
                most_common = msg.get("scott")
                if most_common:
                    self.scott_panel.setText(f"Most frequent Scott number: {most_common}")
                    if not self.scott_input.text():
                        self.scott_input.setText(most_common)
                else:
                    self.scott_panel.setText("Most frequent Scott number: N/A")

                most_country = msg.get("country")
                if most_country:
                    self.country_panel.setText(f"Most likely country: {most_country}")
                    if not self.country_input.text():
                        self.country_input.setText(most_country)
                else:
                    self.country_panel.setText("Most likely country: —")

                # Use pre-filtered results from the subprocess
                self.render_results(msg.get("filtered", []))

        except queue.Empty:
            pass
        except Exception as e:
            logger.error(f"Unexpected error in poll_lens_results: {e}")
        finally:
            QTimer.singleShot(100, self.poll_lens_results)

    def _reset_zoom(self):
        self.zoom = 1.0
        if getattr(self, 'zoom_slider', None):
            self.zoom_slider.setValue(100)
        if getattr(self, 'zoom_label', None):
            self.zoom_label.setText("Zoom: 1.0x")

    def on_zoom_changed(self, value: int):
        try:
            self.zoom = max(1.0, value / 100.0)
            self.zoom_label.setText(f"Zoom: {self.zoom:.1f}x")
            # Refresh current preview
            if getattr(self, 'current_frame', None) is not None:
                self.display_frame(self.current_frame)
        except Exception:
            pass



    def poll_colnect_results(self):
        try:
            while True:
                msg = colnect_result_queue.get_nowait()
                if msg["type"] == "login_done":
                    if msg["error"]:
                        logger.error(f"Colnect login failed: {msg['error']}")
                        self._colnect_available = False
                        self._show_warning("Colnect Login Failed",
                            f"Could not log in to Colnect:\n\n{msg['error']}")
                    else:
                        logger.info("Colnect login successful")
                        self._colnect_available = True

                elif msg["type"] == "search_done":
                    self.colnect_spinner.stop()
                    if msg["error"]:
                        logger.error(f"Colnect search failed: {msg['error']}")
                        self._show_warning("Colnect Search Failed",
                            f"Could not navigate to stamp on Colnect:\n\n{msg['error']}")
                        continue
                elif msg["type"] == "get_info_done":
                    self.colnect_spinner.stop()
                    if msg["error"]:
                        logger.error(f"Colnect get info failed: {msg['error']}")
                        self._show_warning("Colnect Info Failed",
                            f"Could not retrieve stamp info from Colnect:\n\n{msg['error']}")
                        continue

                    # msg["result"] is a dictionary of stamp info
                    try:
                        self.fill_stamp_info(msg["result"])
                    except Exception as e:
                        logger.error(f"Error filling stamp info from Colnect: {e}")
                        self._show_warning("Fill Error",
                            f"Stamp info was retrieved but could not be applied to the form:\n\n{e}")

        except queue.Empty:
            pass
        except Exception as e:
            logger.error(f"Unexpected error in poll_colnect_results: {e}")
        finally:
            QTimer.singleShot(100, self.poll_colnect_results)





    # ---------- Gallery ----------

    def toggle_gallery(self):
        if self._gallery_open:
            sizes = self.main_splitter.sizes()
            sizes[0] = 0
            self.main_splitter.setSizes(sizes)
            self.gallery_toggle_btn.setText("▶ Gallery")
            self._gallery_open = False
        else:
            self._populate_gallery_filters()
            self.load_gallery()
            sizes = self.main_splitter.sizes()
            total = sum(sizes)
            db_w = sizes[1] or 240
            content_w = max(PANEL_CONTENT_MIN_W, total - GALLERY_WIDTH - db_w)
            self.main_splitter.setSizes([GALLERY_WIDTH, db_w, content_w])
            self.gallery_toggle_btn.setText("◀ Gallery")
            self._gallery_open = True

    def _populate_gallery_filters(self):
        session = SessionLocal()
        try:
            countries = StampService.get_all_countries(session)
            self.gallery_country_filter.blockSignals(True)
            self.gallery_country_filter.clear()
            self.gallery_country_filter.addItem("All Countries", "")
            for c in countries:
                self.gallery_country_filter.addItem(c, c)
            self.gallery_country_filter.blockSignals(False)

            themes = ThemeService.get_all_themes(session)
            self.gallery_tag_list.blockSignals(True)
            self.gallery_tag_list.clear()
            for t in themes:
                self.gallery_tag_list.addItem(t.name)
            self.gallery_tag_list.blockSignals(False)
        finally:
            session.close()

    def _toggle_gallery_tag_mode(self):
        if self._gallery_tag_mode == "OR":
            self._gallery_tag_mode = "AND"
            self.gallery_tag_mode_btn.setText("AND")
        else:
            self._gallery_tag_mode = "OR"
            self.gallery_tag_mode_btn.setText("OR")
        self._gallery_page = 0
        self._apply_gallery_filters()

    def _apply_gallery_filters(self):
        if self._gallery_open:
            self._gallery_page = 0   # reset to first page whenever filters change
            self.load_gallery()

    def _gallery_prev_page(self):
        if self._gallery_page > 0:
            self._gallery_page -= 1
            self.load_gallery()

    def _gallery_next_page(self):
        self._gallery_page += 1
        self.load_gallery()

    def load_gallery(self):
        sort_key = {"Newest First": "date", "Oldest First": "oldest",
                    "Scott #": "scott", "Country": "country", "Title": "title"}
        session = SessionLocal()
        try:
            stamps, total = StampService.get_gallery_stamps(
                session,
                search=self.gallery_search.text().strip(),
                country=self.gallery_country_filter.currentData() or "",
                tags=[i.text() for i in self.gallery_tag_list.selectedItems()],
                tag_mode=self._gallery_tag_mode,
                sort=sort_key.get(self.gallery_sort.currentText(), "date"),
                limit=GALLERY_PAGE_SIZE,
                offset=self._gallery_page * GALLERY_PAGE_SIZE,
            )
        finally:
            session.close()

        total_pages = max(1, math.ceil(total / GALLERY_PAGE_SIZE))
        self._gallery_page = min(self._gallery_page, total_pages - 1)
        self.gallery_page_label.setText(f"Page {self._gallery_page + 1} of {total_pages}")
        self.gallery_prev_btn.setEnabled(self._gallery_page > 0)
        self.gallery_next_btn.setEnabled(self._gallery_page < total_pages - 1)

        # Clear existing cards
        while self.gallery_grid.count():
            item = self.gallery_grid.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        viewport_w = self.gallery_scroll.viewport().width() or GALLERY_WIDTH
        cols = max(1, viewport_w // (GALLERY_CARD_W + self.gallery_grid.spacing()))

        for idx, stamp_data in enumerate(stamps):
            card = self._build_stamp_card(stamp_data)
            self.gallery_grid.addWidget(card, idx // cols, idx % cols)

    def _build_stamp_card(self, stamp_data: dict) -> QFrame:
        card = QFrame()
        card.setFixedWidth(GALLERY_CARD_W)
        card.setFrameShape(QFrame.StyledPanel)
        card.setCursor(Qt.PointingHandCursor)

        layout = QVBoxLayout(card)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(3)

        # Thumbnail
        thumb = QLabel()
        thumb_size = GALLERY_CARD_W - 12
        thumb.setFixedSize(thumb_size, thumb_size)
        thumb.setAlignment(Qt.AlignCenter)
        image_path = stamp_data.get("image_path")
        if image_path and os.path.exists(image_path):
            pix = QPixmap(image_path).scaled(
                thumb_size, thumb_size,
                Qt.KeepAspectRatio, Qt.SmoothTransformation
            )
        else:
            pix = QPixmap(thumb_size, thumb_size)
            pix.fill(Qt.lightGray)
        thumb.setPixmap(pix)
        layout.addWidget(thumb)

        # Title (up to 2 lines)
        title_lbl = QLabel(stamp_data.get("title") or "")
        title_lbl.setWordWrap(True)
        title_lbl.setMaximumHeight(36)
        layout.addWidget(title_lbl)

        # Scott # / Country
        info_lbl = QLabel(f"#{stamp_data.get('scott_number', '')}  ·  {stamp_data.get('country', '')}")
        info_lbl.setStyleSheet("color: gray; font-size: 10px;")
        info_lbl.setWordWrap(True)
        layout.addWidget(info_lbl)

        stamp_id = stamp_data["id"]
        card.mousePressEvent = lambda *_, sid=stamp_id: self.load_stamp_into_editor(sid)

        return card

    ### Display from Database ###

    def _clear_db_list(self):
        """Avoid QListWidget.clear() which crashes on Windows via modelReset signal
        dispatch. takeItem() removes items one-by-one (rowsRemoved path) instead."""
        while self.db_list.count() > 0:
            self.db_list.takeItem(0)
        self._db_item_data = []

    def populate_countries(self):
        self.db_list.setSelectionMode(QListWidget.SingleSelection)
        self.bulk_delete_btn.setVisible(False)
        self.db_list.blockSignals(True)
        try:
            self._clear_db_list()
            session = SessionLocal()
            try:
                countries = StampService.get_all_countries(session)
                for country in countries:
                    self.db_list.addItem(QListWidgetItem(country))
                    self._db_item_data.append(f"country|{country}")
                logger.info(f"Loaded {len(countries)} countries from database")
            except Exception as e:
                logger.error(f"Failed to fetch countries: {e}")
            finally:
                session.close()
        finally:
            self.db_list.blockSignals(False)

    def _update_collection_count(self):
        session = SessionLocal()
        try:
            stamps = StampService.get_total_count(session)
            countries = StampService.get_country_count(session)
        except Exception:
            stamps = countries = 0
        finally:
            session.close()
        stamp_str = f"{stamps} stamp{'s' if stamps != 1 else ''}"
        country_str = f"{countries} countr{'ies' if countries != 1 else 'y'}"
        self.collection_count_label.setText(f"{stamp_str} · {country_str}")

    def _refresh_db_view(self):
        """Re-populate the database panel in whatever context is currently active."""
        self._update_collection_count()
        if self._current_db_series_id:
            self.populate_stamps_for_series(self._current_db_series_id)
        elif self._current_db_location_id:
            self.populate_stamps_for_location(self._current_db_location_id)
        elif self._current_db_country:
            self.populate_stamps_for_country(self._current_db_country)
        elif self._db_view == "series":
            self.populate_series()
        elif self._db_view == "locations":
            self.populate_locations()
        else:
            self.populate_countries()

    def _switch_db_view(self, view: str):
        self._db_view = view
        self._current_db_country = None
        self._current_db_series_id = None
        self._current_db_location_id = None
        # Sync combo without re-triggering the signal
        label = view.capitalize()
        if self.db_view_combo.currentText() != label:
            self.db_view_combo.blockSignals(True)
            self.db_view_combo.setCurrentText(label)
            self.db_view_combo.blockSignals(False)
        if view == "series":
            self.populate_series()
        elif view == "locations":
            self.populate_locations()
        else:
            self.populate_countries()

    def populate_series(self):
        self._current_db_country = None
        self._current_db_series_id = None
        self.db_list.setSelectionMode(QListWidget.SingleSelection)
        self.bulk_delete_btn.setVisible(False)
        self.db_list.blockSignals(True)
        try:
            self._clear_db_list()
            session = SessionLocal()
            try:
                series_list = SeriesService.get_all_with_country(session)
                for s in series_list:
                    name = s["name"] or "(Unnamed)"
                    country = s["country"]
                    count = s["stamp_count"]
                    text = f"{name}  —  {country}" if country else name
                    if count:
                        text += f"  ({count} stamp{'s' if count != 1 else ''})"
                    if s["series_url"]:
                        text += "  ↗"
                    self.db_list.addItem(QListWidgetItem(text))
                    url = s["series_url"] or ""
                    self._db_item_data.append(f"series|{s['id']}|{url}")
            except Exception as e:
                logger.error(f"Failed to fetch series: {e}")
            finally:
                session.close()
        finally:
            self.db_list.blockSignals(False)

    def populate_stamps_for_series(self, series_id: int):
        self._current_db_series_id = series_id
        self._current_db_country = None
        self.db_list.setSelectionMode(QListWidget.ExtendedSelection)
        self.bulk_delete_btn.setVisible(True)
        self.bulk_delete_btn.setEnabled(False)
        self.db_list.blockSignals(True)
        try:
            self._clear_db_list()
            session = SessionLocal()
            try:
                stamps = SeriesService.get_stamps(session, series_id)
                for stamp in stamps:
                    title = stamp["title"] or "(Untitled)"
                    scott = stamp["scott_number"] or ""
                    label = f"#{scott}  {title}"
                    self.db_list.addItem(QListWidgetItem(label))
                    self._db_item_data.append(f"stamp|{stamp['id']}")
            except Exception as e:
                logger.error(f"Failed to populate stamps for series {series_id}: {e}")
            finally:
                session.close()
            self.db_list.insertItem(0, QListWidgetItem("← Back to series"))
            self._db_item_data.insert(0, "back")
        finally:
            self.db_list.blockSignals(False)

    def populate_locations(self):
        self._current_db_country = None
        self._current_db_series_id = None
        self._current_db_location_id = None
        self.db_list.setSelectionMode(QListWidget.SingleSelection)
        self.bulk_delete_btn.setVisible(False)
        self.db_list.blockSignals(True)
        try:
            self._clear_db_list()
            session = SessionLocal()
            try:
                locations = PhysicalLocationService.get_all_with_count(session)
                for loc in locations:
                    count = loc["stamp_count"]
                    label = f"{loc['name']}  ({count} stamp{'s' if count != 1 else ''})"
                    self.db_list.addItem(QListWidgetItem(label))
                    self._db_item_data.append(f"location|{loc['id']}")
            except Exception as e:
                logger.error(f"Failed to fetch locations: {e}")
            finally:
                session.close()
        finally:
            self.db_list.blockSignals(False)

    def populate_stamps_for_location(self, location_id: int):
        self._current_db_location_id = location_id
        self._current_db_country = None
        self._current_db_series_id = None
        self.db_list.setSelectionMode(QListWidget.ExtendedSelection)
        self.bulk_delete_btn.setVisible(True)
        self.bulk_delete_btn.setEnabled(False)
        self.db_list.blockSignals(True)
        try:
            self._clear_db_list()
            session = SessionLocal()
            try:
                stamps = PhysicalLocationService.get_stamps(session, location_id)
                for stamp in stamps:
                    title = stamp["title"] or "(Untitled)"
                    scott = stamp["scott_number"] or ""
                    country = stamp["country"] or ""
                    label = f"#{scott}  {title}  [{country}]"
                    self.db_list.addItem(QListWidgetItem(label))
                    self._db_item_data.append(f"stamp|{stamp['id']}")
            except Exception as e:
                logger.error(f"Failed to populate stamps for location {location_id}: {e}")
            finally:
                session.close()
            self.db_list.insertItem(0, QListWidgetItem("← Back to locations"))
            self._db_item_data.insert(0, "back")
        finally:
            self.db_list.blockSignals(False)

    def _open_colnect_url(self, url: str):
        if not url.startswith("http"):
            url = f"https://colnect.com{url}"
        self._open_in_colnect_browser_or_fallback(url)

    def _open_in_colnect_browser_or_fallback(self, url: str):
        if getattr(self, "_colnect_available", False) and self.browser_worker.is_alive():
            self.browser_worker.schedule_open_url(url)
        else:
            QDesktopServices.openUrl(QUrl(url))

    def _open_lens_result(self, url: str):
        if "colnect.com" in url:
            self._open_in_colnect_browser_or_fallback(url)
        else:
            webbrowser.open(url)

    def _on_db_list_context_menu(self, pos):
        item = self.db_list.itemAt(pos)
        if not item:
            return
        row = self.db_list.row(item)
        if row < 0 or row >= len(self._db_item_data):
            return
        raw = self._db_item_data[row]
        item_type, _, rest = raw.partition("|")
        if item_type != "series":
            return
        _, _, url = rest.partition("|")
        if not url:
            return
        menu = QMenu(self.db_list)
        open_action = menu.addAction("Open in Colnect ↗")
        if menu.exec_(self.db_list.viewport().mapToGlobal(pos)) == open_action:
            self._open_colnect_url(url)

    def _browse_country(self):
        dlg = CountryPickerDialog(self)
        if dlg.exec() == QDialog.Accepted and dlg.selected_name:
            self.country_input.setText(dlg.selected_name)

    def _auto_expand_country(self, text: str):
        """Instantly expand 2-letter codes on each keystroke only when unambiguous.
        Longer codes (3+) are expanded on Tab/Enter/blur via editingFinished."""
        try:
            t = (text or "").strip()
            if not t or len(t) > 2:
                return
            key = t.upper()
            # If this text is a prefix of any longer COUNTRY_MAP key, wait —
            # the user may still be typing (e.g. "US" while intending "USSR").
            if any(k.startswith(key) and len(k) > len(key) for k in COUNTRY_MAP):
                return
            # Direct COUNTRY_MAP hit
            if key in COUNTRY_MAP:
                name = COUNTRY_MAP[key]
                if name != text:
                    self.country_input.blockSignals(True)
                    try:
                        self.country_input.setText(name)
                    finally:
                        self.country_input.blockSignals(False)
                return
            # For 2-letter codes not in the map, try pycountry
            if len(t) == 2 and t.isalpha():
                name = get_country_name(t)
                if name and name != text:
                    self.country_input.blockSignals(True)
                    try:
                        self.country_input.setText(name)
                    finally:
                        self.country_input.blockSignals(False)
        except Exception:
            pass

    def populate_stamps_for_country(self, country: str):
        self._current_db_country = country
        self.db_list.setSelectionMode(QListWidget.ExtendedSelection)
        self.bulk_delete_btn.setVisible(True)
        self.bulk_delete_btn.setEnabled(False)
        self.db_list.blockSignals(True)
        try:
            self._clear_db_list()
            session = SessionLocal()
            try:
                stamps = StampService.get_stamps_by_country(session, country)
                for stamp in stamps:
                    title = stamp["title"] or "(Untitled)"
                    series = stamp["series"] or ""
                    scott = stamp["scott_number"] or ""
                    label = f"#{scott}  {title} — {series}" if series else f"#{scott}  {title}"
                    self.db_list.addItem(QListWidgetItem(label))
                    self._db_item_data.append(f"stamp|{stamp['id']}")
            except Exception as e:
                session.rollback()
                logger.error(f"Failed to populate stamps for country: {e}")
            finally:
                session.close()
            self.db_list.insertItem(0, QListWidgetItem("← Back to countries"))
            self._db_item_data.insert(0, "back")
        finally:
            self.db_list.blockSignals(False)


    def on_db_item_clicked(self, item):
        row = self.db_list.row(item)
        if row < 0 or row >= len(self._db_item_data):
            return
        raw = self._db_item_data[row]
        item_type, _, item_value = raw.partition("|")
        match item_type:
            case "country":
                self.populate_stamps_for_country(item_value)
            case "series":
                series_id_str = item_value.partition("|")[0]
                self.populate_stamps_for_series(int(series_id_str))
            case "location":
                self.populate_stamps_for_location(int(item_value))
            case "stamp":
                if len(self.db_list.selectedItems()) == 1:
                    self.load_stamp_into_editor(int(item_value))
            case "back":
                if self._db_view == "series":
                    self.populate_series()
                elif self._db_view == "locations":
                    self.populate_locations()
                else:
                    self.populate_countries()

    def _update_bulk_delete_btn(self):
        selected_stamps = [
            it for it in self.db_list.selectedItems()
            if self.db_list.row(it) < len(self._db_item_data)
            if self._db_item_data[self.db_list.row(it)].startswith("stamp|")
        ]
        n = len(selected_stamps)
        self.bulk_delete_btn.setEnabled(n > 0)
        self.bulk_delete_btn.setText(f"Delete Selected ({n})" if n > 0 else "Delete Selected")


    def load_stamp_into_editor(self, stamp_id: int):
        session = SessionLocal()
        try:
            stamp = session.get(Stamp, stamp_id)

            # Set edit mode (and current_stamp_id) before populating fields below,
            # so the duplicate-stamp check doesn't mistake this stamp for a clash
            # with itself while scott_input/country_input are being filled in.
            self._set_edit_mode(stamp_id)

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
            self.print_run_input.setText(str(stamp.print_run) if stamp.print_run is not None else "")
            self.colors_input.setText(stamp.colors or "")
            self.designers_input.setText(stamp.designers or "")
            self.description_input.setText(stamp.description or "")
            self.variants_checkbox.setChecked(stamp.variants or False)

            # Load series info from linked Series record
            self.current_series_id = stamp.series_id
            s = stamp.series_obj
            has_series = s is not None
            self.series_open_url_btn.setEnabled(has_series and bool(s.series_url))
            self.view_series_btn.setEnabled(has_series)
            val = (s.series_complete if s else "") or ""
            idx = self.series_complete_combo.findText(val) if val else 0
            self.series_complete_combo.setCurrentIndex(max(idx, 0))
            self.series_comments_input.setText(s.comments or "" if s else "")
            self.series_comments_row.setVisible(has_series)

            # Load physical location
            self._load_location_combo(select_id=stamp.physical_location_id)

            # Load copies
            self._clear_copies()
            for c in stamp.copies:
                self._add_copy_row(c.condition, c.quantity)

            # Load variant set
            self.current_variant_set_id = stamp.variant_set_id
            if stamp.variant_set_id and stamp.variant_set:
                self.variant_set_label.setText(stamp.variant_set.name)
                self.variant_set_label.setStyleSheet("color: white; font-style: normal;")
                notes = stamp.variant_set.notes or ""
                self.variant_set_notes_lbl.setText(notes)
                self.variant_set_notes_lbl.setVisible(bool(notes))
            else:
                self.variant_set_label.setText("None")
                self.variant_set_label.setStyleSheet("color: gray; font-style: italic;")

            # Load themes
            self.load_themes()
            stamp_theme_names = {
                theme.name for theme in (stamp.themes or [])
            }

            # Block signals so setSelected() can't re-enter load_themes() → clear()
            # while we're mid-iteration (would free items Qt's selection model still
            # holds pointers to, corrupting the C++ heap).
            self.themes_input.blockSignals(True)
            try:
                for i in range(self.themes_input.count()):
                    item = self.themes_input.item(i)
                    item.setSelected(item.text() in stamp_theme_names)
            finally:
                self.themes_input.blockSignals(False)
            # Re-sort in "selected first" mode now that selections are stable
            if self._theme_sort == "selected":
                self.load_themes()
            self._sync_quick_theme_checkboxes()

            # Load image — images are stored in the StampImage relationship, not on the stamp itself
            image_path = stamp.images[0].file_path if stamp.images else None
            if image_path and os.path.exists(image_path):
                self.show_image(image_path, clear_metadata=False)
            else:
                self.current_image_path = None
                logger.debug("Stamp has no associated image.")





        except Exception as e:
            logger.error(f"Error loading stamp {stamp_id}: {e}")
            self._show_error("Load Failed", f"Could not load stamp:\n\n{e}")
            return
        finally:
            session.close()




    def _open_series_url(self):
        if not self.current_series_id:
            return
        session = SessionLocal()
        try:
            s = SeriesService.get_by_id(session, self.current_series_id)
            url = (s.series_url or "") if s else ""
        finally:
            session.close()
        if url:
            if not url.startswith("http"):
                url = f"https://colnect.com{url}"
            self._open_in_colnect_browser_or_fallback(url)

    def _view_series_in_db(self):
        if not self.current_series_id:
            return
        dlg = SeriesStampsDialog(self, self.current_series_id)
        dlg.exec()

    def _add_copy_row(self, condition: str = "", quantity: int = 1):
        row = QWidget()
        rl = QHBoxLayout(row)
        rl.setContentsMargins(0, 0, 0, 0)
        rl.setSpacing(0)

        cond = QComboBox()
        cond.addItems(StampCopyService.CONDITIONS)
        cond.setSizeAdjustPolicy(QComboBox.AdjustToContents)
        cond.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        idx = cond.findText(condition)
        if idx >= 0:
            cond.setCurrentIndex(idx)

        qty = QSpinBox()
        qty.setRange(1, 9999)
        qty.setValue(max(1, quantity))
        qty.setFixedWidth(72)

        del_btn = QPushButton("×")
        del_btn.setFixedWidth(24)
        del_btn.setToolTip("Remove this row")
        del_btn.clicked.connect(lambda: (row.setParent(None), row.deleteLater()))

        rl.addWidget(cond, 1)
        rl.addWidget(qty)
        rl.addWidget(del_btn)
        rl.addStretch(1)
        self.copies_layout.addWidget(row)

    def _clear_copies(self):
        while self.copies_layout.count():
            item = self.copies_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

    def _get_copies_data(self) -> list[dict]:
        copies = []
        for i in range(self.copies_layout.count()):
            item = self.copies_layout.itemAt(i)
            if not (item and item.widget()):
                continue
            cond = item.widget().findChild(QComboBox)
            qty  = item.widget().findChild(QSpinBox)
            if cond and qty:
                copies.append({"condition": cond.currentText(), "quantity": qty.value()})
        return copies

    def _load_location_combo(self, select_id: int | None = None):
        """Repopulate the location combo from DB, optionally pre-selecting a location by ID."""
        session = SessionLocal()
        try:
            locations = PhysicalLocationService.get_all(session)
        finally:
            session.close()

        self.location_combo.blockSignals(True)
        self.location_combo.clear()
        self.location_combo.addItem("— not stored —", None)
        for loc in locations:
            self.location_combo.addItem(loc.name, loc.id)
        if select_id is not None:
            idx = self.location_combo.findData(select_id)
            self.location_combo.setCurrentIndex(max(idx, 0))
        else:
            self.location_combo.setCurrentIndex(0)
        self.location_combo.blockSignals(False)

    def _add_new_location(self):
        name, ok = QInputDialog.getText(self, "New Location", "Location name:")
        if not ok or not name.strip():
            return
        session = SessionLocal()
        try:
            loc = PhysicalLocationService.create(session, name.strip())
            self._load_location_combo(select_id=loc.id)
        except ValueError as e:
            self._show_warning("Cannot Add", str(e))
        finally:
            session.close()

    def _open_variant_set_picker(self):
        title   = self.title_input.text().strip()
        issued  = self.issued_input.text().strip()
        year    = issued[:4] if issued else ""
        country = self.country_input.text().strip()
        dlg = VariantSetDialog(self, title, year, country, self.current_variant_set_id)
        if dlg.exec() == QDialog.Accepted:
            self.current_variant_set_id = dlg.selected_id
            if dlg.selected_id is not None:
                session = SessionLocal()
                try:
                    vs = session.get(VariantSet, dlg.selected_id)
                    self.variant_set_label.setText(vs.name if vs else "None")
                    self.variant_set_label.setStyleSheet("color: white; font-style: normal;")
                    notes = (vs.notes or "") if vs else ""
                    self.variant_set_notes_lbl.setText(notes)
                    self.variant_set_notes_lbl.setVisible(bool(notes))
                finally:
                    session.close()
            else:
                self.variant_set_label.setText("None")
                self.variant_set_label.setStyleSheet("color: gray; font-style: italic;")

    def _show_error(self, title: str, message: str):
        QMessageBox.critical(self, title, message)

    def _show_warning(self, title: str, message: str):
        QMessageBox.warning(self, title, message)

    def _set_themes_interactive(self, enabled: bool):
        mode = QListWidget.MultiSelection if enabled else QListWidget.NoSelection
        if getattr(self, 'themes_input', None):
            self.themes_input.setSelectionMode(mode)

    def _quick_theme_toggled(self, theme_name: str, checked: bool):
        self._set_themes_interactive(True)
        target_lower = theme_name.lower()
        found_item = None
        for i in range(self.themes_input.count()):
            if self.themes_input.item(i).text().lower() == target_lower:
                found_item = self.themes_input.item(i)
                break
        self.themes_input.blockSignals(True)
        if found_item:
            found_item.setSelected(checked)
        self.themes_input.blockSignals(False)
        if checked and found_item:
            implications = load_theme_implications()
            implied = implications.get(theme_name, [])
            if implied:
                implied_lower = {x.lower() for x in implied}
                self.themes_input.blockSignals(True)
                for i in range(self.themes_input.count()):
                    li = self.themes_input.item(i)
                    if li.text().lower() in implied_lower:
                        li.setSelected(True)
                self.themes_input.blockSignals(False)
        self.themes_input.repaint()
        if self._theme_sort == "selected":
            self.load_themes()
        self._sync_quick_theme_checkboxes()

    def _sync_quick_theme_checkboxes(self):
        if not getattr(self, 'unchecked_chk', None):
            return
        selected = {
            self.themes_input.item(i).text().lower()
            for i in range(self.themes_input.count())
            if self.themes_input.item(i).isSelected()
        }
        self.unchecked_chk.blockSignals(True)
        self.overprints_chk.blockSignals(True)
        self.unchecked_chk.setChecked("unchecked" in selected)
        self.overprints_chk.setChecked("has overprints/surcharges" in selected)
        self.unchecked_chk.blockSignals(False)
        self.overprints_chk.blockSignals(False)

    # ---------- Close ----------
    def _set_add_mode(self):
        self.current_stamp_id = None
        self._set_themes_interactive(False)
        if getattr(self, 'save_btn', None):
            self.save_btn.setText("Add Stamp to Database")
        if getattr(self, 'delete_btn', None):
            self.delete_btn.setVisible(False)
        if getattr(self, 'reassign_img_btn', None):
            self.reassign_img_btn.setVisible(False)
        if getattr(self, 'browse_history_btn', None):
            self.browse_history_btn.setVisible(False)
        if getattr(self, 'copies_layout', None) and self.copies_layout.count() == 0:
            self._add_copy_row("Fine Used (FU)", 1)

    def _set_edit_mode(self, stamp_id: int):
        self.current_stamp_id = stamp_id
        self._set_themes_interactive(True)
        if getattr(self, 'save_btn', None):
            self.save_btn.setText("Update Stamp Entry")
        if getattr(self, 'delete_btn', None):
            self.delete_btn.setVisible(True)
        if getattr(self, 'reassign_img_btn', None):
            self.reassign_img_btn.setVisible(True)
        if getattr(self, 'browse_history_btn', None):
            self.browse_history_btn.setVisible(True)

    def delete_stamp(self):
        if not self.current_stamp_id:
            print("No stamp selected for deletion")
            return

        response = QMessageBox.question(
            self,
            "Delete Stamp",
            "Delete this stamp from the database?",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )

        if response != QMessageBox.Yes:
            return

        session = SessionLocal()
        try:
            success = StampService.delete_stamp(session, self.current_stamp_id)
            if success:
                logger.info(f"Deleted stamp ID {self.current_stamp_id}")
                self._reset_stamp_form()
                self._refresh_db_view()
                self.current_stamp_id = None
                self._set_add_mode()
                if self._gallery_open:
                    self.load_gallery()
            else:
                logger.error(f"Failed to delete stamp ID {self.current_stamp_id}")
        except Exception as e:
            logger.error(f"Error deleting stamp: {e}")
        finally:
            session.close()

    def _bulk_delete_stamps(self):
        selected_stamps = [
            it for it in self.db_list.selectedItems()
            if self.db_list.row(it) < len(self._db_item_data)
            if self._db_item_data[self.db_list.row(it)].startswith("stamp|")
        ]
        if not selected_stamps:
            return

        n = len(selected_stamps)
        response = QMessageBox.question(
            self,
            "Delete Stamps",
            f"Delete {n} stamp{'s' if n != 1 else ''} from the database?\nThis cannot be undone.",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if response != QMessageBox.Yes:
            return

        session = SessionLocal()
        deleted = 0
        try:
            for item in selected_stamps:
                stamp_id = int(self._db_item_data[self.db_list.row(item)].partition("|")[2])
                if StampService.delete_stamp(session, stamp_id):
                    deleted += 1
        except Exception as e:
            logger.error(f"Error during bulk delete: {e}")
        finally:
            session.close()

        logger.info(f"Bulk deleted {deleted} stamp(s)")
        self._reset_stamp_form()
        self._set_add_mode()
        self._refresh_db_view()
        if self._gallery_open:
            self.load_gallery()

    def closeEvent(self, event):
        print("Shutting down StampIdentifierApp...")

        # Stop polling timers
        try:
            self.timer.stop()
        except Exception:
            pass

        # --- stop async browser worker ---
        if getattr(self, 'browser_worker', None):
            try:
                self.browser_worker.shutdown()
                self.browser_worker.join(timeout=15)
            except Exception as e:
                print(f"Error stopping browser worker: {e}")

        # --- release camera ---
        try:
            if getattr(self, 'cap', None) and self.cap.isOpened():
                self.cap.release()
        except Exception:
            pass

        # Call parent handler
        try:
            super().closeEvent(event)
        except Exception:
            event.accept()



# ---------- Run App ----------
if __name__ == "__main__":
    import sys
    import faulthandler
    from PIL.ImageQt import ImageQt

    # Write a C-level backtrace to stderr on access violations / segfaults.
    # Harmless in normal operation; essential for diagnosing silent fatal crashes.
    faulthandler.enable()

    # Snapshot the database before the UI opens, so there is always a recent
    # pre-session backup to revert to. Non-blocking failures are logged.
    from db.backup import backup_on_launch
    backup_on_launch()

    app = QApplication(sys.argv)
    window = StampIdentifierApp()
    window.show()
    sys.exit(app.exec())
