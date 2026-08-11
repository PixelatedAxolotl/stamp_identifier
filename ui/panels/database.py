# ui/panels/database.py
#
# DatabasePanel — hierarchical browser for countries, series, and physical
# locations with bulk-delete support.
#
# Signals emitted:
#   stamp_load_requested(int stamp_id) — single stamp click; Canvas routes to Fields
#   collection_changed()               — after bulk delete; Canvas routes to Gallery

from PySide6.QtWidgets import (
    QVBoxLayout, QLabel, QComboBox, QListWidget, QListWidgetItem,
    QPushButton, QMenu, QMessageBox,
)
from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtCore import QUrl

from db.session import SessionLocal
from db.service import StampService, SeriesService, PhysicalLocationService
from logger import logger
from ui.panel import Panel, hide_scrollbars


class DatabasePanel(Panel):

    stamp_load_requested = Signal(int)
    collection_changed   = Signal()

    def __init__(self, parent=None):
        super().__init__("Database", parent)

        # Navigation state
        self._db_view               = "countries"   # "countries" | "series" | "locations"
        self._current_db_country    = None
        self._current_db_series_id  = None
        self._current_db_location_id = None
        # Parallel to db_list rows — avoids Qt UserRole storage
        self._db_item_data: list[str] = []

        layout = QVBoxLayout(self.content_widget)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(6)

        self._count_label = QLabel("0 stamps · 0 countries")
        self._count_label.setStyleSheet("color: gray; font-size: 11px;")
        layout.addWidget(self._count_label)

        self._view_combo = QComboBox()
        self._view_combo.addItems(["Countries", "Series", "Locations"])
        self._view_combo.currentTextChanged.connect(
            lambda text: self._switch_db_view(text.lower())
        )
        layout.addWidget(self._view_combo)

        self._list = QListWidget()
        self._list.setIconSize(__import__('PySide6.QtCore', fromlist=['QSize']).QSize(64, 64))
        self._list.setSpacing(4)
        self._list.setWordWrap(True)
        self._list.itemClicked.connect(self._on_item_clicked)
        self._list.itemSelectionChanged.connect(self._update_bulk_delete_btn)
        self._list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._list.customContextMenuRequested.connect(self._on_context_menu)
        hide_scrollbars(self._list)
        layout.addWidget(self._list)

        self._bulk_delete_btn = QPushButton("Delete Selected")
        self._bulk_delete_btn.setVisible(False)
        self._bulk_delete_btn.clicked.connect(self._bulk_delete_stamps)
        layout.addWidget(self._bulk_delete_btn)

        self._update_collection_count()
        self.populate_countries()

    # ------------------------------------------------------------------
    # Public refresh (called by Canvas when collection_changed fires)
    # ------------------------------------------------------------------

    def refresh(self):
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

    # ------------------------------------------------------------------
    # View switching
    # ------------------------------------------------------------------

    def _switch_db_view(self, view: str):
        self._db_view = view
        self._current_db_country      = None
        self._current_db_series_id    = None
        self._current_db_location_id  = None
        label = view.capitalize()
        if self._view_combo.currentText() != label:
            self._view_combo.blockSignals(True)
            self._view_combo.setCurrentText(label)
            self._view_combo.blockSignals(False)
        if view == "series":
            self.populate_series()
        elif view == "locations":
            self.populate_locations()
        else:
            self.populate_countries()

    # ------------------------------------------------------------------
    # Populate methods
    # ------------------------------------------------------------------

    def populate_countries(self):
        self._list.setSelectionMode(QListWidget.SelectionMode.SingleSelection)
        self._bulk_delete_btn.setVisible(False)
        self._list.blockSignals(True)
        try:
            self._clear_list()
            session = SessionLocal()
            try:
                for country in StampService.get_all_countries(session):
                    self._list.addItem(QListWidgetItem(country))
                    self._db_item_data.append(f"country|{country}")
            except Exception as e:
                logger.error(f"Failed to fetch countries: {e}")
            finally:
                session.close()
        finally:
            self._list.blockSignals(False)

    def populate_series(self):
        self._current_db_country     = None
        self._current_db_series_id   = None
        self._list.setSelectionMode(QListWidget.SelectionMode.SingleSelection)
        self._bulk_delete_btn.setVisible(False)
        self._list.blockSignals(True)
        try:
            self._clear_list()
            session = SessionLocal()
            try:
                for s in SeriesService.get_all_with_country(session):
                    name    = s["name"] or "(Unnamed)"
                    country = s["country"]
                    count   = s["stamp_count"]
                    text    = f"{name}  —  {country}" if country else name
                    if count:
                        text += f"  ({count} stamp{'s' if count != 1 else ''})"
                    if s["series_url"]:
                        text += "  ↗"
                    self._list.addItem(QListWidgetItem(text))
                    self._db_item_data.append(f"series|{s['id']}|{s['series_url'] or ''}")
            except Exception as e:
                logger.error(f"Failed to fetch series: {e}")
            finally:
                session.close()
        finally:
            self._list.blockSignals(False)

    def populate_stamps_for_series(self, series_id: int):
        self._current_db_series_id  = series_id
        self._current_db_country    = None
        self._list.setSelectionMode(QListWidget.SelectionMode.ExtendedSelection)
        self._bulk_delete_btn.setVisible(True)
        self._bulk_delete_btn.setEnabled(False)
        self._list.blockSignals(True)
        try:
            self._clear_list()
            session = SessionLocal()
            try:
                for stamp in SeriesService.get_stamps(session, series_id):
                    title = stamp["title"] or "(Untitled)"
                    scott = stamp["scott_number"] or ""
                    self._list.addItem(QListWidgetItem(f"#{scott}  {title}"))
                    self._db_item_data.append(f"stamp|{stamp['id']}")
            except Exception as e:
                logger.error(f"Failed to populate stamps for series {series_id}: {e}")
            finally:
                session.close()
            self._list.insertItem(0, QListWidgetItem("← Back to series"))
            self._db_item_data.insert(0, "back")
        finally:
            self._list.blockSignals(False)

    def populate_locations(self):
        self._current_db_country      = None
        self._current_db_series_id    = None
        self._current_db_location_id  = None
        self._list.setSelectionMode(QListWidget.SelectionMode.SingleSelection)
        self._bulk_delete_btn.setVisible(False)
        self._list.blockSignals(True)
        try:
            self._clear_list()
            session = SessionLocal()
            try:
                for loc in PhysicalLocationService.get_all_with_count(session):
                    count = loc["stamp_count"]
                    label = f"{loc['name']}  ({count} stamp{'s' if count != 1 else ''})"
                    self._list.addItem(QListWidgetItem(label))
                    self._db_item_data.append(f"location|{loc['id']}")
            except Exception as e:
                logger.error(f"Failed to fetch locations: {e}")
            finally:
                session.close()
        finally:
            self._list.blockSignals(False)

    def populate_stamps_for_country(self, country: str):
        self._current_db_country = country
        self._list.setSelectionMode(QListWidget.SelectionMode.ExtendedSelection)
        self._bulk_delete_btn.setVisible(True)
        self._bulk_delete_btn.setEnabled(False)
        self._list.blockSignals(True)
        try:
            self._clear_list()
            session = SessionLocal()
            try:
                for stamp in StampService.get_stamps_by_country(session, country):
                    title  = stamp["title"] or "(Untitled)"
                    series = stamp["series"] or ""
                    scott  = stamp["scott_number"] or ""
                    label  = f"#{scott}  {title} — {series}" if series else f"#{scott}  {title}"
                    self._list.addItem(QListWidgetItem(label))
                    self._db_item_data.append(f"stamp|{stamp['id']}")
            except Exception as e:
                session.rollback()
                logger.error(f"Failed to populate stamps for country: {e}")
            finally:
                session.close()
            self._list.insertItem(0, QListWidgetItem("← Back to countries"))
            self._db_item_data.insert(0, "back")
        finally:
            self._list.blockSignals(False)

    def populate_stamps_for_location(self, location_id: int):
        self._current_db_location_id  = location_id
        self._current_db_country      = None
        self._current_db_series_id    = None
        self._list.setSelectionMode(QListWidget.SelectionMode.ExtendedSelection)
        self._bulk_delete_btn.setVisible(True)
        self._bulk_delete_btn.setEnabled(False)
        self._list.blockSignals(True)
        try:
            self._clear_list()
            session = SessionLocal()
            try:
                for stamp in PhysicalLocationService.get_stamps(session, location_id):
                    title   = stamp["title"] or "(Untitled)"
                    scott   = stamp["scott_number"] or ""
                    country = stamp["country"] or ""
                    self._list.addItem(QListWidgetItem(f"#{scott}  {title}  [{country}]"))
                    self._db_item_data.append(f"stamp|{stamp['id']}")
            except Exception as e:
                logger.error(f"Failed to populate stamps for location {location_id}: {e}")
            finally:
                session.close()
            self._list.insertItem(0, QListWidgetItem("← Back to locations"))
            self._db_item_data.insert(0, "back")
        finally:
            self._list.blockSignals(False)

    # ------------------------------------------------------------------
    # Item interaction
    # ------------------------------------------------------------------

    def _on_item_clicked(self, item: QListWidgetItem):
        row = self._list.row(item)
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
                if len(self._list.selectedItems()) == 1:
                    self.stamp_load_requested.emit(int(item_value))
            case "back":
                if self._db_view == "series":
                    self.populate_series()
                elif self._db_view == "locations":
                    self.populate_locations()
                else:
                    self.populate_countries()

    def _on_context_menu(self, pos):
        item = self._list.itemAt(pos)
        if not item:
            return
        row = self._list.row(item)
        if row < 0 or row >= len(self._db_item_data):
            return
        raw = self._db_item_data[row]
        item_type, _, rest = raw.partition("|")
        if item_type != "series":
            return
        _, _, url = rest.partition("|")
        if not url:
            return
        menu = QMenu(self._list)
        action = menu.addAction("Open in Colnect ↗")
        if menu.exec(self._list.viewport().mapToGlobal(pos)) == action:
            if not url.startswith("http"):
                url = f"https://colnect.com{url}"
            QDesktopServices.openUrl(QUrl(url))

    # ------------------------------------------------------------------
    # Bulk delete
    # ------------------------------------------------------------------

    def _update_bulk_delete_btn(self):
        selected = [
            it for it in self._list.selectedItems()
            if self._db_item_data[self._list.row(it)].startswith("stamp|")
        ]
        n = len(selected)
        self._bulk_delete_btn.setEnabled(n > 0)
        self._bulk_delete_btn.setText(
            f"Delete Selected ({n})" if n > 0 else "Delete Selected"
        )

    def _bulk_delete_stamps(self):
        selected = [
            it for it in self._list.selectedItems()
            if self._db_item_data[self._list.row(it)].startswith("stamp|")
        ]
        if not selected:
            return
        n = len(selected)
        resp = QMessageBox.question(
            self,
            "Delete Stamps",
            f"Delete {n} stamp{'s' if n != 1 else ''} from the database?\nThis cannot be undone.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if resp != QMessageBox.StandardButton.Yes:
            return

        session = SessionLocal()
        deleted = 0
        try:
            for item in selected:
                stamp_id = int(self._db_item_data[self._list.row(item)].partition("|")[2])
                if StampService.delete_stamp(session, stamp_id):
                    deleted += 1
        except Exception as e:
            logger.error(f"Error during bulk delete: {e}")
        finally:
            session.close()

        logger.info(f"Bulk deleted {deleted} stamp(s)")
        self.collection_changed.emit()
        self.refresh()

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _update_collection_count(self):
        session = SessionLocal()
        try:
            stamps    = StampService.get_total_count(session)
            countries = StampService.get_country_count(session)
        except Exception:
            stamps = countries = 0
        finally:
            session.close()
        stamp_str   = f"{stamps} stamp{'s' if stamps != 1 else ''}"
        country_str = f"{countries} countr{'ies' if countries != 1 else 'y'}"
        self._count_label.setText(f"{stamp_str} · {country_str}")

    def _clear_list(self):
        # takeItem() avoids the Windows modelReset crash that QListWidget.clear() triggers
        while self._list.count() > 0:
            self._list.takeItem(0)
        self._db_item_data = []
