# ui/panels/fields.py
#
# FieldsPanel — all stamp metadata inputs, copies, location, and action buttons.
#
# Signals emitted:
#   collection_changed()           — after save or delete; Canvas routes to Gallery + Database
#   stamp_image_load_requested(str) — after loading a stamp with an image; Canvas routes to Preview
#
# Dependencies injected by Canvas after init:
#   set_themes_panel(panel)        — ThemesPanel reference for reading/writing selections
#   set_browser_worker(worker)     — for Colnect search / get-info
#
# Public API (called by Canvas):
#   load_stamp(stamp_id)           — load existing stamp into editor
#   set_image_path_provider(fn)    — callable returning the active image path;
#                                     Canvas owns it, this panel does not cache it
#   set_lens_hints(scott, country) — called by Canvas on ResultsPanel.scott_country_suggested
#   fill_from_colnect(info)        — called by Canvas when Colnect get-info completes
#   clear_for_new_capture()        — called by Canvas on PreviewPanel.capture_complete

import os

from PySide6.QtWidgets import (
    QVBoxLayout, QHBoxLayout, QFormLayout, QLabel, QLineEdit, QPushButton,
    QComboBox, QCheckBox, QScrollArea, QWidget, QFrame, QSizePolicy,
    QMessageBox, QInputDialog, QSpinBox, QDialog, QDialogButtonBox,
)
from PySide6.QtCore import Qt, Signal, QTimer
from PySide6.QtGui import QColor, QDesktopServices
from PySide6.QtCore import QUrl

from db.session import SessionLocal
from db.service import (
    StampService, SeriesService, PhysicalLocationService,
    StampCopyService, OriginService,
)
from db.models import Stamp, VariantSet
from image_storage import associate_image, deassociate_image
from helper_utils import get_country_name, load_tag_aliases, load_theme_implications, COUNTRY_MAP
from logger import logger
from ui.panel import Panel, hide_scrollbars
from ui.spinner import SpinnerWidget

def _same_path(a: str | None, b: str | None) -> bool:
    """True if two filesystem paths point at the same file, tolerant of case
    and separators (Windows). Used to tell an image reassignment apart from an
    ordinary edit that carries the unchanged image path through."""
    if not a or not b:
        return a == b
    return os.path.normcase(os.path.normpath(a)) == os.path.normcase(os.path.normpath(b))


# Padding between the panel edge and the field content (all four sides).
_CONTENT_MARGIN = 34

# Floor width for plain single-line fields when the panel is narrowed —
# QLineEdit's own default minimumSizeHint is only a few characters wide.
_FIELD_MIN_W = 10

# Maximum on-screen width (in px) for each editable text field. This is the
# single place to tune field sizing: set a number to cap that field's box at
# that width, or use None to let it grow to fill the panel. Keys match the
# `_field(key)` calls in _build_ui() below.
_FIELD_WIDTHS: dict[str, int | None] = {
    "title":           250,
    "scott":           50,
    "country":         150,
    "series":          250,
    "emission":        120,
    "face_value":      50,
    "issued":          50,
    "expired":         50,
    "size":            60,
    "perforation":     80,
    "paper":           80,
    "gum":             100,
    "watermark":       100,
    "printing":        100,
    "format":          150,
    "print_run":       80,
    "colors":          100,
    "designers":       150,
    "description":     250,
    "series_comments": None,
}

# Maximum on-screen width (in px) for each dropdown (QComboBox). Same idea as
# _FIELD_WIDTHS: set a number to cap that dropdown, or None to let it grow.
_DROPDOWN_WIDTHS: dict[str, int | None] = {
    "series_complete": None,   # "Series Complete:" combo in the field form
    "location":        None,   # "Physical Location:" combo
    "copy_condition":  None,   # condition combo in each Copies row
}

# Maximum on-screen width (in px) for each whole form row — i.e. the label
# plus the field plus any inline buttons on that row. None = no cap (the row
# grows to fill). This is independent of _FIELD_WIDTHS / _DROPDOWN_WIDTHS,
# which cap only the input box inside the row. Keys match the `_add_row(key…)`
# calls in _build_ui() below.
_ROW_WIDTHS: dict[str, int | None] = {
    "title":           530,
    "scott":           None,
    "country":         None,
    "series":          None,
    "emission":        None,
    "face_value":      None,
    "issued":          530,
    "expired":         None,
    "series_complete": 200,
    "size":            None,
    "perforation":     None,
    "paper":           None,
    "gum":             None,
    "watermark":       None,
    "printing":        None,
    "format":          None,
    "print_run":       None,
    "colors":          300,
    "designers":       None,
    "variants":        100,
    "description":     530,
}

# Height (in px) for every field row — a single value for all rows, not
# per-row. Sets the height of each input box and its row so they stay uniform.
# None = leave each widget at its natural height. Tune this one number to make
# the rows taller or more compact.
_ROW_HEIGHT: int | None = None

# Label text for each field key.
_FIELD_LABELS: dict[str, str] = {
    "title":           "Title:",
    "scott":           "Scott #:",
    "country":         "Country:",
    "series":          "Series:",
    "emission":        "Emission:",
    "face_value":      "Face Value:",
    "issued":          "Issued:",
    "expired":         "Expired:",
    "series_complete": "Series Complete:",
    "size":            "Size:",
    "perforation":     "Perforation:",
    "paper":           "Paper:",
    "gum":             "Gum:",
    "watermark":       "Watermark:",
    "printing":        "Printing:",
    "format":          "Format:",
    "print_run":       "Print Run:",
    "colors":          "Colors:",
    "designers":       "Designers:",
    "variants":        "Variants:",
    "description":     "Description:",
}

# Field arrangement — the single place that controls how fields are laid out.
# Each inner list is one on-screen line; put as many keys on a line as wanted
# to place those fields side by side, or keep a key alone for its own line.
# Reorder and regroup freely. A key left out here is still built but not shown;
# an unknown key is skipped with a warning.
_FORM_LAYOUT: list[list[str]] = [
    ["title", "scott"],
    ["country"],
    ["series"],
    ["series_complete"],
    ["emission", "face_value"],
    ["issued", "expired"],
    ["size", "perforation"],
    ["paper", "gum"],
    ["watermark", "printing"],
    ["format", "print_run"],
    ["colors", "designers"],
    ["variants"],
    ["description"],
]

# Horizontal gap (px) inserted between field groups that share one line.
_LINE_GROUP_GAP = 5


def _disable_horizontal_scroll(area: QScrollArea) -> None:
    """Remove a scroll area's horizontal scroll ability while keeping vertical.

    ScrollBarAlwaysOff only *hides* the horizontal scrollbar — whenever the
    content is wider than the viewport it can still be panned sideways with a
    trackpad or shift+wheel. Pinning the horizontal range to zero (and keeping
    it pinned as Qt recomputes it on every resize/layout) makes any overflow
    clip at the panel edge instead of scrolling.
    """
    area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
    hbar = area.horizontalScrollBar()
    hbar.rangeChanged.connect(lambda _lo, _hi: hbar.setRange(0, 0))
    hbar.setRange(0, 0)


def origin_summary(origin: dict | None) -> str:
    """Compact one-line description of a copy's origin for the row label."""
    if not origin:
        return "No origin"
    parts = []
    if origin.get("location"):
        parts.append(origin["location"])
    if origin.get("dealer"):
        parts.append(origin["dealer"])
    tail = []
    if origin.get("method"):
        tail.append(origin["method"])
    if origin.get("price"):
        tail.append(f"${origin['price']}")
    if origin.get("acquired_date"):
        tail.append(origin["acquired_date"])
    if tail:
        parts.append(" ".join(tail))
    return " · ".join(parts) if parts else "No origin"


class OriginDialog(QDialog):
    """Edit the acquisition origin of a single copy: where/whom it came from,
    how it was acquired, price, date, and notes. Location and dealer are
    editable combos seeded with existing entries so they stay reusable while
    still letting a brand-new one be typed."""

    def __init__(self, parent=None, origin: dict | None = None):
        super().__init__(parent)
        self.setWindowTitle("Copy Origin")
        origin = origin or {}

        form = QFormLayout(self)

        session = SessionLocal()
        try:
            locations = [l.name for l in OriginService.get_all_locations(session)]
            dealers   = [d.name for d in OriginService.get_all_dealers(session)]
            methods   = OriginService.get_method_suggestions(session)
        finally:
            session.close()

        self._location = QComboBox()
        self._location.setEditable(True)
        self._location.addItem("")
        self._location.addItems(locations)
        self._location.setCurrentText(origin.get("location", ""))

        self._dealer = QComboBox()
        self._dealer.setEditable(True)
        self._dealer.addItem("")
        self._dealer.addItems(dealers)
        self._dealer.setCurrentText(origin.get("dealer", ""))

        self._method = QComboBox()
        self._method.setEditable(True)
        self._method.addItem("")
        self._method.addItems(methods)
        self._method.setCurrentText(origin.get("method", ""))

        self._price = QLineEdit(origin.get("price", ""))
        self._price.setPlaceholderText("e.g. 5.00")

        self._date = QLineEdit(origin.get("acquired_date", ""))
        self._date.setPlaceholderText("YYYY-MM-DD")

        self._notes = QLineEdit(origin.get("notes", ""))

        form.addRow("Location:", self._location)
        form.addRow("Dealer / Seller:", self._dealer)
        form.addRow("Acquired by:", self._method)
        form.addRow("Price:", self._price)
        form.addRow("Date:", self._date)
        form.addRow("Notes:", self._notes)

        # Track the values we auto-filled so switching locations/methods can
        # update them, while a value the user typed themselves is never
        # overwritten. Connected after initial values are set so loading an
        # existing origin doesn't trip the handlers.
        self._autofilled_date  = ""
        self._autofilled_price = ""
        self._location.activated.connect(self._maybe_fill_date)
        self._method.currentTextChanged.connect(self._maybe_fill_price)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

    # Methods with no purchase price — selecting one pre-fills the price as 0.
    _FREE_METHODS = {"given", "traded", "found", "inherited"}

    def _maybe_fill_date(self, *_):
        """When a location is picked, seed the date from the last acquisition
        there. Only fills a blank date or one we auto-filled before, so a
        manually entered date is preserved."""
        loc = self._location.currentText().strip()
        if not loc:
            return
        session = SessionLocal()
        try:
            date = OriginService.get_location_date(session, loc)
        finally:
            session.close()
        cur = self._date.text().strip()
        if date and cur in ("", self._autofilled_date):
            self._date.setText(date)
            self._autofilled_date = date

    def _maybe_fill_price(self, *_):
        """Pre-fill price 0 for gift/trade/found/inherited; clear our own 0 when
        switching back to a paid method. Never touches a price the user typed."""
        method = self._method.currentText().strip().lower()
        cur = self._price.text().strip()
        if method in self._FREE_METHODS:
            if cur in ("", self._autofilled_price):
                self._price.setText("0")
                self._autofilled_price = "0"
        elif self._autofilled_price and cur == self._autofilled_price:
            self._price.clear()
            self._autofilled_price = ""

    def get_origin(self) -> dict:
        return {
            "location":      self._location.currentText().strip(),
            "dealer":        self._dealer.currentText().strip(),
            "method":        self._method.currentText().strip(),
            "price":         self._price.text().strip(),
            "acquired_date": self._date.text().strip(),
            "notes":         self._notes.text().strip(),
        }


class FieldsPanel(Panel):

    collection_changed        = Signal()
    stamp_image_load_requested = Signal(str)
    # Emitted with the image's former incoming path after it has been associated
    # with a stamp (and moved to IMAGE_DIR). Canvas routes it to the History
    # strip so the now-assigned capture drops off the un-associated list.
    image_associated          = Signal(str)
    # Emitted with an image's new incoming path after it has been de-associated
    # (moved from IMAGE_DIR back to INCOMING_DIR) because a stamp's image was
    # reassigned to a different one. Canvas routes it to the History strip so the
    # freed image reappears in the un-associated list.
    image_deassociated        = Signal(str)

    def __init__(self, parent=None):
        super().__init__("Fields", parent, bg_texture="fields_canvas.png", show_label=False)

        self.current_stamp_id      = None
        self.current_series_id: int | None  = None
        self.current_variant_set_id: int | None = None
        self._duplicate_stamp_id: int | None    = None

        self._themes_panel   = None
        self._browser_worker = None
        # Returns the active image path; injected by Canvas (single source of
        # truth). The panel no longer caches its own copy.
        self._get_image_path = lambda: None

        self._build_ui()

        # Debounce duplicate-stamp check
        self._dup_check_timer = QTimer(self)
        self._dup_check_timer.setSingleShot(True)
        self._dup_check_timer.setInterval(400)
        self._dup_check_timer.timeout.connect(self._check_duplicate_stamp)

        self._set_add_mode()

    # ------------------------------------------------------------------
    # Dependency injection
    # ------------------------------------------------------------------

    def set_themes_panel(self, panel):
        self._themes_panel = panel

    def set_browser_worker(self, worker):
        self._browser_worker = worker

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def set_image_path_provider(self, fn):
        """Injected by Canvas: a callable returning the active image path."""
        self._get_image_path = fn

    def clear_for_new_capture(self):
        """Call after a new image is captured to reset fields for a new stamp.

        Only the form fields are cleared — the active image is owned by Canvas
        and was just set by the capture, so it must be left intact here.
        """
        self._reset_form()

    def set_lens_hints(self, scott: str, country: str):
        """Fill Scott # and country from Lens suggestions if fields are empty."""
        if scott and not self._scott_input.text():
            self._scott_input.setText(scott)
        if country and not self._country_input.text():
            self._country_input.setText(country)

    def load_stamp(self, stamp_id: int):
        session = SessionLocal()
        try:
            stamp = session.get(Stamp, stamp_id)
            if stamp is None:
                return
            self._set_edit_mode(stamp_id)

            self._title_input.setText(stamp.title or "")
            self._country_input.setText(stamp.country or "")
            self._series_input.setText(stamp.series or "")
            self._scott_input.setText(stamp.scott_number or "")
            self._emission_input.setText(stamp.emission or "")
            self._face_value_input.setText(stamp.face_value or "")
            self._issued_input.setText(
                stamp.issued_date.strftime("%Y-%m-%d") if stamp.issued_date else ""
            )
            self._expired_input.setText(
                stamp.expired_date.strftime("%Y-%m-%d") if stamp.expired_date else ""
            )
            self._size_input.setText(stamp.size or "")
            self._perforation_input.setText(stamp.perforation or "")
            self._paper_input.setText(stamp.paper or "")
            self._gum_input.setText(stamp.gum or "")
            self._watermark_input.setText(stamp.watermark or "")
            self._printing_input.setText(stamp.printing or "")
            self._format_input.setText(stamp.format or "")
            self._print_run_input.setText(
                str(stamp.print_run) if stamp.print_run is not None else ""
            )
            self._colors_input.setText(stamp.colors or "")
            self._designers_input.setText(stamp.designers or "")
            self._description_input.setText(stamp.description or "")
            self._variants_cb.setChecked(stamp.variants or False)

            # Series info
            self.current_series_id = stamp.series_id
            s = stamp.series_obj
            has_series = s is not None
            self._series_url_btn.setEnabled(has_series and bool(s.series_url if s else None))
            self._view_series_btn.setEnabled(has_series)
            self._set_series_complete(
                s.series_complete if s else "", default="NO" if has_series else ""
            )
            self._series_comments_input.setText(s.comments or "" if s else "")
            self._series_comments_row.setVisible(has_series)
            self._refresh_series_count()

            # Location
            self._load_location_combo(select_id=stamp.physical_location_id)

            # Copies
            self._clear_copies()
            for c in stamp.copies:
                self._add_copy_row(
                    c.condition, c.quantity, OriginService.origin_to_dict(c.origin)
                )

            # Variant set
            self.current_variant_set_id = stamp.variant_set_id
            if stamp.variant_set_id and stamp.variant_set:
                self._variant_set_lbl.setText(stamp.variant_set.name)
                self._variant_set_lbl.setStyleSheet("font-style: normal;")
                notes = stamp.variant_set.notes or ""
                self._variant_set_notes_lbl.setText(notes)
                self._variant_set_notes_lbl.setVisible(bool(notes))
            else:
                self._variant_set_lbl.setText("None")
                self._variant_set_lbl.setStyleSheet("color: gray; font-style: italic;")

            # Themes
            if self._themes_panel:
                self._themes_panel.load_themes()
                stamp_theme_names = {t.name for t in (stamp.themes or [])}
                self._themes_panel.set_selected_themes(stamp_theme_names)

            # Image — notify Canvas (owner) either way: "" clears the current image.
            image_path = stamp.images[0].file_path if stamp.images else None
            if image_path and os.path.exists(image_path):
                self.stamp_image_load_requested.emit(image_path)
            else:
                self.stamp_image_load_requested.emit("")

        except Exception as e:
            logger.error(f"Error loading stamp {stamp_id}: {e}")
            QMessageBox.critical(self, "Load Failed", f"Could not load stamp:\n\n{e}")
        finally:
            session.close()

    def _set_series_complete(self, val: str | None, default: str = ""):
        """Select the series-complete combo entry for ``val``. When ``val`` is
        empty, fall back to ``default`` — "NO" when a series is present (its
        completeness just hasn't been recorded yet), or blank when there's no
        series at all."""
        idx = self._series_complete_combo.findText(val or default)
        if idx < 0:
            idx = self._series_complete_combo.findText("")
        self._series_complete_combo.setCurrentIndex(max(idx, 0))

    def _refresh_series_count(self):
        """Show how many stamps in the current series are already saved in the
        DB. Reads self.current_series_id; hides the label when there's no
        series."""
        if not self.current_series_id:
            self._series_count_lbl.clear()
            self._series_count_lbl.setVisible(False)
            return
        session = SessionLocal()
        try:
            n = SeriesService.count_stamps(session, self.current_series_id)
        except Exception as e:
            logger.warning(f"Failed to count series stamps: {e}")
            self._series_count_lbl.setVisible(False)
            return
        finally:
            session.close()
        self._series_count_lbl.setText(f"{n} in DB")
        self._series_count_lbl.setToolTip(
            f"{n} stamp{'' if n == 1 else 's'} in this series saved in the database"
        )
        self._series_count_lbl.setVisible(True)

    def fill_from_colnect(self, info: dict):
        """Populate fields from Colnect get-info result."""
        self._title_input.setText(info.get("name", ""))
        self._scott_input.setText(info.get("scott_number", ""))
        if info.get("country"):
            self._country_input.setText(info["country"])

        series_name = info.get("series") or ""
        series_url  = info.get("series_url") or ""
        if not series_name and series_url:
            slug        = series_url.rstrip("/").split("/")[-1]
            series_name = slug.split("-", 1)[-1].replace("_", " ").strip() if "-" in slug else slug

        self._series_input.setText(series_name)

        if series_url and series_name:
            session = SessionLocal()
            try:
                s = SeriesService.get_or_create_by_url(session, name=series_name, url=series_url)
                self.current_series_id = s.id
                self._series_url_btn.setEnabled(True)
                self._view_series_btn.setEnabled(True)
                self._series_comments_input.setText(s.comments or "")
                self._series_comments_row.setVisible(True)
                self._set_series_complete(s.series_complete, default="NO")
            except Exception as e:
                logger.error(f"Failed to find/create series '{series_name}': {e}")
                self.current_series_id = None
            finally:
                session.close()
        else:
            self.current_series_id = None
            self._series_url_btn.setEnabled(False)
            self._view_series_btn.setEnabled(False)
            self._series_comments_row.setVisible(False)

        self._refresh_series_count()

        self._emission_input.setText(info.get("emission", ""))
        self._face_value_input.setText(info.get("face_value", ""))
        self._issued_input.setText(info.get("issued_date", ""))
        self._expired_input.setText(info.get("expired_date", ""))
        self._size_input.setText(info.get("size", ""))
        self._perforation_input.setText(info.get("perforation", ""))
        self._paper_input.setText(info.get("paper", ""))
        self._gum_input.setText(info.get("gum", ""))
        self._watermark_input.setText(info.get("watermark", ""))
        self._printing_input.setText(info.get("printing", ""))
        self._format_input.setText(info.get("format", ""))
        self._print_run_input.setText(info.get("print_run", ""))
        self._colors_input.setText(info.get("colors", ""))
        self._designers_input.setText(info.get("designers", ""))
        self._description_input.setText(info.get("description", ""))
        self._variants_cb.setChecked(bool(info.get("variants", False)))

        if self._themes_panel:
            self._themes_panel.load_themes()
            self._themes_panel.set_interactive(True)
            raw_themes   = info.get("themes", [])
            aliases      = load_tag_aliases()
            implications = load_theme_implications()
            resolved     = []
            for name in raw_themes:
                name = aliases.get(name.strip(), name.strip())
                if name and name not in resolved:
                    resolved.append(name)
                for implied in implications.get(name, []):
                    if implied not in resolved:
                        resolved.append(implied)
            # Create missing themes in DB, then select all
            session = SessionLocal()
            try:
                from db.service import ThemeService
                for name in resolved:
                    try:
                        ThemeService.create_or_get_theme(session, name)
                    except Exception:
                        pass
            finally:
                session.close()
            # Reload list so newly created themes appear, then select
            self._themes_panel.load_themes()
            self._themes_panel.set_selected_themes(resolved)

    def start_colnect_spinner(self, text: str = ""):
        self._colnect_spinner.start(text)

    def stop_colnect_spinner(self):
        self._colnect_spinner.stop()

    # ------------------------------------------------------------------
    # Save / delete
    # ------------------------------------------------------------------

    def save_stamp(self):
        image_path = self._get_image_path()
        if not image_path and not self.current_stamp_id:
            QMessageBox.warning(self, "Cannot Save", "No image captured.")
            return

        themes = self._themes_panel.get_selected_themes() if self._themes_panel else []

        stamp_data = {
            "title":               self._title_input.text(),
            "scott_number":        self._scott_input.text(),
            "country":             self._country_input.text(),
            "series":              self._series_input.text(),
            "emission":            self._emission_input.text(),
            "face_value":          self._face_value_input.text(),
            "issued_date":         self._issued_input.text(),
            "expired_date":        self._expired_input.text(),
            "size":                self._size_input.text(),
            "perforation":         self._perforation_input.text(),
            "paper":               self._paper_input.text(),
            "gum":                 self._gum_input.text(),
            "watermark":           self._watermark_input.text(),
            "printing":            self._printing_input.text(),
            "format":              self._format_input.text(),
            "print_run":           self._print_run_input.text(),
            "colors":              self._colors_input.text(),
            "designers":           self._designers_input.text(),
            "description":         self._description_input.text(),
            "variants":            self._variants_cb.isChecked(),
            "owned":               True,
            "variant_set_id":      self.current_variant_set_id if self._variants_cb.isChecked() else None,
            "themes":              themes,
            "image_path":          image_path,
            "series_id":           self.current_series_id,
            "physical_location_id": self._location_combo.currentData(),
        }
        copies_data     = self._get_copies_data()
        series_complete = self._series_complete_combo.currentText() or None
        series_comments = self._series_comments_input.text().strip() or None

        # Pre-check duplicate before opening the main session
        if not self.current_stamp_id:
            scott   = stamp_data["scott_number"].strip()
            country = stamp_data["country"].strip()
            if scott and country:
                with SessionLocal() as chk:
                    existing = chk.query(Stamp).filter(
                        Stamp.scott_number == scott,
                        Stamp.country == country,
                    ).first()
                if existing:
                    QMessageBox.warning(
                        self, "Duplicate Stamp",
                        f"Scott #{scott} from {country} is already in your collection."
                    )
                    return

        # Get the stamp's image into its final home before the record is written
        # so the stored path is correct, rolling the filesystem move back if the
        # save fails (deassociate_image) so nothing is stranded.
        #
        #   incoming_path — the new image's former INCOMING_DIR path, dropped
        #                   from the History strip once it's associated.
        #   old_db_path   — an existing stamp's previous image (in IMAGE_DIR),
        #                   returned to the incoming pool once the reassignment
        #                   to a different image commits.
        incoming_path = None
        old_db_path   = None
        if image_path:
            if not self.current_stamp_id:
                # New stamp: its image is a fresh capture sitting in INCOMING_DIR.
                associated_path = associate_image(image_path)
                if associated_path != image_path:
                    incoming_path = image_path
                    stamp_data["image_path"] = associated_path
            else:
                # Existing stamp: only touch files if a *different* image was
                # picked (a reassignment). Ordinary edits pass the unchanged
                # already-associated path, which must be left in place.
                with SessionLocal() as chk:
                    st = chk.get(Stamp, self.current_stamp_id)
                    current_db_path = st.images[0].file_path if (st and st.images) else None
                if not _same_path(image_path, current_db_path):
                    associated_path = associate_image(image_path)
                    if associated_path != image_path:
                        incoming_path = image_path
                    stamp_data["image_path"] = associated_path
                    old_db_path = current_db_path

        saved_ok = False
        session  = SessionLocal()
        try:
            series_name = stamp_data.get("series", "").strip()
            if series_name and not self.current_series_id:
                s = SeriesService.get_or_create_by_name(session, series_name, _commit=False)
                self.current_series_id = s.id
                stamp_data["series_id"] = self.current_series_id

            if self.current_stamp_id:
                StampService.update_stamp(session, self.current_stamp_id, _commit=False, **stamp_data)
                StampCopyService.set_copies(session, self.current_stamp_id, copies_data, _commit=False)
                logger.info(f"Stamp ID {self.current_stamp_id} updated")
            else:
                new_stamp = StampService.create_stamp(session, _commit=False, **stamp_data)
                StampCopyService.set_copies(session, new_stamp.id, copies_data, _commit=False)
                logger.info(f"Stamp '{new_stamp.title}' added")

            if self.current_series_id:
                SeriesService.update(session, self.current_series_id, _commit=False,
                                     series_complete=series_complete, comments=series_comments)
            session.commit()
            saved_ok = True
        except ValueError as e:
            session.rollback()
            QMessageBox.warning(self, "Cannot Save", str(e))
        except Exception as e:
            session.rollback()
            logger.error(f"Failed to save stamp: {e}")
            QMessageBox.critical(self, "Save Failed", f"An error occurred:\n\n{e}")
        finally:
            session.close()

        if saved_ok:
            if incoming_path:
                # Capture is now assigned — drop it from the incoming/history strip.
                self.image_associated.emit(incoming_path)
            if old_db_path:
                # Reassigned to a different image — return the previous one to
                # the incoming pool so it reappears in the History strip.
                reverted = deassociate_image(old_db_path)
                if reverted and os.path.exists(reverted):
                    self.image_deassociated.emit(reverted)
            QTimer.singleShot(0, self._post_save_refresh)
        elif incoming_path:
            # Save failed after the move — put the file back in incoming.
            deassociate_image(stamp_data["image_path"])

    def delete_stamp(self):
        if not self.current_stamp_id:
            return
        resp = QMessageBox.question(
            self, "Delete Stamp", "Delete this stamp from the database?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if resp != QMessageBox.StandardButton.Yes:
            return
        session = SessionLocal()
        try:
            if StampService.delete_stamp(session, self.current_stamp_id):
                logger.info(f"Deleted stamp ID {self.current_stamp_id}")
                self._reset_form(clear_image=True)
                QTimer.singleShot(0, self.collection_changed.emit)
        except Exception as e:
            logger.error(f"Error deleting stamp: {e}")
        finally:
            session.close()

    # ------------------------------------------------------------------
    # Colnect buttons
    # ------------------------------------------------------------------

    def _search_colnect(self):
        if not self._browser_worker:
            return
        country = self._country_input.text()
        if self._filter_search_cb.isChecked():
            # Alternate method: filter by whichever of these fields are filled in
            # (blanks are skipped controller-side). Keys must match the 'key' of a
            # spec in AsyncColnectController._FILTER_SPECS. To add a filter later
            # (e.g. theme), add one entry here and a matching spec there.
            issued = self._issued_input.text().strip()
            themes = self._themes_panel.get_selected_themes() if self._themes_panel else []
            filters = {
                # Colnect's theme filter takes one theme; use the first selected.
                "theme":      themes[0] if themes else "",
                "year":       issued[:4] if issued else "",
                "face_value": self._face_value_input.text().strip(),
            }
            self._colnect_spinner.start("Filtering stamps on Colnect…")
            self._browser_worker.schedule_colnect_search(
                "", country, 0, method="filter", filters=filters)
        else:
            scott = self._scott_input.text()
            self._colnect_spinner.start("Navigating to stamp on Colnect…")
            self._browser_worker.schedule_colnect_search(scott, country, 0)

    def _on_filter_search_toggled(self, checked: bool):
        # In add mode the themes panel is normally non-interactive; make it
        # selectable while the filter search is on so a theme can be chosen to
        # search with. Edit mode is already interactive, so leave it untouched.
        if self._themes_panel and self.current_stamp_id is None:
            self._themes_panel.set_interactive(checked)

    def _get_colnect_info(self):
        if not self._browser_worker:
            return
        self._colnect_spinner.start("Retrieving stamp info from Colnect…")
        self._browser_worker.schedule_colnect_info(0)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _post_save_refresh(self):
        self._reset_form(clear_image=True)
        self.collection_changed.emit()

    def _reset_form(self, clear_image: bool = False):
        for le in (
            self._title_input, self._scott_input, self._country_input,
            self._series_input, self._emission_input, self._face_value_input,
            self._issued_input, self._expired_input, self._size_input,
            self._perforation_input, self._paper_input, self._gum_input,
            self._watermark_input, self._printing_input, self._format_input,
            self._print_run_input, self._colors_input, self._designers_input,
            self._description_input, self._series_comments_input,
        ):
            le.clear()
        self._variants_cb.setChecked(False)
        self.current_variant_set_id = None
        self._variant_set_lbl.setText("None")
        self._variant_set_lbl.setStyleSheet("color: gray; font-style: italic;")
        self._variant_set_notes_lbl.setText("")
        self._variant_set_notes_lbl.setVisible(False)
        self.current_series_id = None
        self._series_url_btn.setEnabled(False)
        self._view_series_btn.setEnabled(False)
        self._set_series_complete("")
        self._series_comments_row.setVisible(False)
        self._refresh_series_count()
        self._location_combo.setCurrentIndex(0)
        self._clear_copies()
        self._duplicate_warning.setVisible(False)
        self._duplicate_stamp_id = None
        if self._themes_panel:
            self._themes_panel.set_selected_themes([])
        self.current_stamp_id   = None
        # The active image is owned by Canvas. Only clear it when finishing a
        # stamp (save/delete) — never on clear_for_new_capture, where a capture
        # has just set it.
        if clear_image:
            self.stamp_image_load_requested.emit("")
        self._set_add_mode()

    def _set_add_mode(self):
        self.current_stamp_id = None
        if self._themes_panel:
            # Normally read-only in add mode, but keep it selectable if the filter
            # search is on so a theme survives capture resets and stays pickable.
            cb = getattr(self, "_filter_search_cb", None)
            self._themes_panel.set_interactive(cb is not None and cb.isChecked())
        self._save_btn.setText("Add Stamp to Database")
        self._delete_btn.setVisible(False)
        self._reassign_btn.setVisible(False)
        if self._copies_layout.count() == 0:
            self._add_copy_row("Fine Used (FU)", 1)

    def _set_edit_mode(self, stamp_id: int):
        self.current_stamp_id = stamp_id
        if self._themes_panel:
            self._themes_panel.set_interactive(True)
        self._save_btn.setText("Update Stamp Entry")
        self._delete_btn.setVisible(True)
        self._reassign_btn.setVisible(True)

    def _schedule_duplicate_check(self):
        self._dup_check_timer.start()

    def _check_duplicate_stamp(self):
        if self.current_stamp_id:
            self._duplicate_warning.setVisible(False)
            return
        scott   = self._scott_input.text().strip()
        country = self._country_input.text().strip()
        if not scott or not country:
            self._duplicate_warning.setVisible(False)
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
            self._duplicate_warning.setText(
                '<span style="color:red;">⚠ Already in your collection — '
                '<a href="preview" style="color:red;">view</a></span>'
            )
            self._duplicate_warning.setVisible(True)
        else:
            self._duplicate_stamp_id = None
            self._duplicate_warning.setVisible(False)

    def _show_duplicate_preview(self, _href=None):
        if not self._duplicate_stamp_id:
            return
        try:
            from stamp_identifier_v3 import DuplicateStampPreviewDialog
            dlg = DuplicateStampPreviewDialog(self, self._duplicate_stamp_id)
            dlg.exec()
            if dlg.edit_requested:
                self.load_stamp(self._duplicate_stamp_id)
        except Exception as e:
            logger.warning(f"DuplicateStampPreviewDialog unavailable: {e}")

    def _add_copy_row(self, condition: str = "", quantity: int = 1, origin: dict | None = None):
        row = QWidget()
        row._origin = origin or {}
        rl  = QHBoxLayout(row)
        rl.setContentsMargins(0, 0, 0, 0)
        rl.setSpacing(0)

        cond = QComboBox()
        cond.addItems(StampCopyService.CONDITIONS)
        cond.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToContents)
        cond.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        _cond_w = _DROPDOWN_WIDTHS.get("copy_condition")
        if _cond_w is not None:
            cond.setMaximumWidth(_cond_w)
        idx = cond.findText(condition)
        if idx >= 0:
            cond.setCurrentIndex(idx)

        qty = QSpinBox()
        qty.setRange(1, 9999)
        qty.setValue(max(1, quantity))
        qty.setFixedWidth(48)
        # Hide the native up/down buttons: their glyphs render as faint dots
        # under the QSS skin. Use plain +1 / -1 text buttons instead — reliable
        # and they already pick up the light body-text color.
        qty.setButtonSymbols(QSpinBox.ButtonSymbols.NoButtons)

        inc_btn = QPushButton("+1")
        inc_btn.setFixedWidth(30)
        inc_btn.clicked.connect(qty.stepUp)
        dec_btn = QPushButton("-1")
        dec_btn.setFixedWidth(30)
        dec_btn.clicked.connect(qty.stepDown)

        del_btn = QPushButton("×")
        del_btn.setFixedWidth(24)
        del_btn.clicked.connect(lambda: (row.setParent(None), row.deleteLater()))

        origin_btn = QPushButton("Origin…")
        origin_btn.setFixedWidth(64)
        origin_lbl = QLabel(origin_summary(row._origin))
        origin_lbl.setStyleSheet("color: gray; font-style: italic;")
        origin_lbl.setTextInteractionFlags(Qt.TextInteractionFlag.NoTextInteraction)
        origin_lbl.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        origin_btn.clicked.connect(lambda: self._edit_copy_origin(row, origin_lbl))

        rl.addWidget(cond, 1)
        rl.addWidget(qty)
        rl.addWidget(dec_btn)
        rl.addWidget(inc_btn)
        rl.addWidget(del_btn)
        rl.addStretch(1)
        rl.addWidget(origin_btn)
        rl.addWidget(origin_lbl, 1)
        self._copies_layout.addWidget(row)

    def _edit_copy_origin(self, row, origin_lbl):
        dlg = OriginDialog(self, row._origin)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            row._origin = dlg.get_origin()
            origin_lbl.setText(origin_summary(row._origin))

    def _clear_copies(self):
        while self._copies_layout.count():
            item = self._copies_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

    def _get_copies_data(self) -> list[dict]:
        copies = []
        for i in range(self._copies_layout.count()):
            item = self._copies_layout.itemAt(i)
            if not (item and item.widget()):
                continue
            cond = item.widget().findChild(QComboBox)
            qty  = item.widget().findChild(QSpinBox)
            if cond and qty:
                copies.append({
                    "condition": cond.currentText(),
                    "quantity": qty.value(),
                    "origin": getattr(item.widget(), "_origin", {}) or {},
                })
        return copies

    def _load_location_combo(self, select_id: int | None = None):
        session = SessionLocal()
        try:
            locations = PhysicalLocationService.get_all(session)
        finally:
            session.close()
        self._location_combo.blockSignals(True)
        self._location_combo.clear()
        self._location_combo.addItem("— not stored —", None)
        for loc in locations:
            self._location_combo.addItem(loc.name, loc.id)
        if select_id is not None:
            idx = self._location_combo.findData(select_id)
            self._location_combo.setCurrentIndex(max(idx, 0))
        self._location_combo.blockSignals(False)

    def _add_new_location(self):
        name, ok = QInputDialog.getText(self, "New Location", "Location name:")
        if not ok or not name.strip():
            return
        session = SessionLocal()
        try:
            loc = PhysicalLocationService.create(session, name.strip())
            self._load_location_combo(select_id=loc.id)
        except ValueError as e:
            QMessageBox.warning(self, "Cannot Add", str(e))
        finally:
            session.close()

    def _browse_country(self):
        try:
            from stamp_identifier_v3 import CountryPickerDialog
            from PySide6.QtWidgets import QDialog
            dlg = CountryPickerDialog(self)
            if dlg.exec() == QDialog.DialogCode.Accepted and dlg.selected_name:
                self._country_input.setText(dlg.selected_name)
        except Exception as e:
            logger.warning(f"CountryPickerDialog unavailable: {e}")

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
                    self._country_input.blockSignals(True)
                    try:
                        self._country_input.setText(name)
                    finally:
                        self._country_input.blockSignals(False)
                return
            # For 2-letter codes not in the map, try pycountry
            if len(t) == 2 and t.isalpha():
                name = get_country_name(t)
                if name and name != text:
                    self._country_input.blockSignals(True)
                    try:
                        self._country_input.setText(name)
                    finally:
                        self._country_input.blockSignals(False)
        except Exception:
            pass

    def _open_series_url(self):
        if not self.current_series_id:
            return
        session = SessionLocal()
        try:
            s   = SeriesService.get_by_id(session, self.current_series_id)
            url = (s.series_url or "") if s else ""
        finally:
            session.close()
        if url:
            if not url.startswith("http"):
                url = f"https://colnect.com{url}"
            QDesktopServices.openUrl(QUrl(url))

    def _view_series_in_db(self):
        if not self.current_series_id:
            return
        try:
            from stamp_identifier_v3 import SeriesStampsDialog
            SeriesStampsDialog(self, self.current_series_id).exec()
        except Exception as e:
            logger.warning(f"SeriesStampsDialog unavailable: {e}")

    def _open_variant_set_picker(self):
        try:
            from stamp_identifier_v3 import VariantSetDialog
            from PySide6.QtWidgets import QDialog
            title   = self._title_input.text().strip()
            issued  = self._issued_input.text().strip()
            country = self._country_input.text().strip()
            year    = issued[:4] if issued else ""
            dlg     = VariantSetDialog(self, title, year, country, self.current_variant_set_id)
            if dlg.exec() == QDialog.DialogCode.Accepted:
                self.current_variant_set_id = dlg.selected_id
                if dlg.selected_id is not None:
                    session = SessionLocal()
                    try:
                        vs = session.get(VariantSet, dlg.selected_id)
                        self._variant_set_lbl.setText(vs.name if vs else "None")
                        self._variant_set_lbl.setStyleSheet("font-style: normal;")
                        notes = (vs.notes or "") if vs else ""
                        self._variant_set_notes_lbl.setText(notes)
                        self._variant_set_notes_lbl.setVisible(bool(notes))
                    finally:
                        session.close()
                else:
                    self._variant_set_lbl.setText("None")
                    self._variant_set_lbl.setStyleSheet("color: gray; font-style: italic;")
        except Exception as e:
            logger.warning(f"VariantSetDialog unavailable: {e}")

    def _reassign_image(self):
        try:
            from stamp_identifier_v3 import ImageBrowserDialog
            from PySide6.QtWidgets import QDialog
            dlg = ImageBrowserDialog(self)
            if dlg.exec() == QDialog.DialogCode.Accepted and dlg.selected_path:
                # Canvas owns the active image; the emit routes it there + to Preview.
                self.stamp_image_load_requested.emit(dlg.selected_path)
        except Exception as e:
            logger.warning(f"ImageBrowserDialog unavailable: {e}")

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_ui(self):
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        _disable_horizontal_scroll(scroll)
        hide_scrollbars(scroll)
        # QScrollArea's viewport auto-fills its own background by default,
        # inheriting the palette up to Canvas (CANVAS_BG) since nothing here
        # sets one — that opaque fill would otherwise hide the panel texture
        # painted behind it in Panel.paintEvent.
        scroll.setAutoFillBackground(False)
        scroll.viewport().setAutoFillBackground(False)

        inner  = QWidget()
        outer  = QVBoxLayout(inner)
        outer.setContentsMargins(_CONTENT_MARGIN, _CONTENT_MARGIN, _CONTENT_MARGIN, _CONTENT_MARGIN)
        outer.setSpacing(4)
        scroll.setWidget(inner)
        # QScrollArea.setWidget() flips autoFillBackground back to True on
        # the widget it's given, undoing the transparency set above — must
        # be disabled again after this call, not before it.
        inner.setAutoFillBackground(False)

        wrapper = QVBoxLayout(self.content_widget)
        wrapper.setContentsMargins(0, 0, 0, 0)
        wrapper.addWidget(scroll)

        # Field form — one label+field per row, stacked vertically. Each row
        # is wrapped in its own container so that its total width (label +
        # field + any inline buttons) can be capped independently via
        # _ROW_WIDTHS; a plain QFormLayout shares its columns across every row
        # and so can't cap a single row's total.
        form = QFormLayout()
        form.setHorizontalSpacing(10)
        form.setVerticalSpacing(8)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)

        # Fixed label-column width, sized to the widest label so nothing clips
        # and the first-column labels stay aligned across every line.
        fm          = self.fontMetrics()
        label_col_w = max(fm.horizontalAdvance(t) for t in _FIELD_LABELS.values()) + 6

        def _field(key: str) -> QLineEdit:
            le = QLineEdit()
            le.setMinimumWidth(_FIELD_MIN_W)
            max_w = _FIELD_WIDTHS.get(key)
            if max_w is not None:
                le.setMaximumWidth(max_w)
            if _ROW_HEIGHT is not None:
                le.setFixedHeight(_ROW_HEIGHT)
            return le

        def _add_line(keys: list[str]):
            # Build one on-screen line holding every field in `keys`. The first
            # field's label uses the aligned label column; any further fields on
            # the same line use compact natural-width labels, each preceded by a
            # small gap. The trailing stretch pins the whole line to the left.
            row = QWidget()
            hl  = QHBoxLayout(row)
            hl.setContentsMargins(0, 0, 0, 0)
            hl.setSpacing(form.horizontalSpacing())
            placed = 0
            for key in keys:
                widget = self._field_widgets.get(key)
                if widget is None:
                    logger.warning(f"Fields layout: unknown field key '{key}' — skipped")
                    continue
                if placed > 0:
                    hl.addSpacing(_LINE_GROUP_GAP)
                lbl = QLabel(_FIELD_LABELS.get(key, key))
                lbl.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
                if placed == 0:
                    lbl.setFixedWidth(label_col_w)
                hl.addWidget(lbl)
                # Each field grows to its own _FIELD_WIDTHS max (stretch 1);
                # fields sharing a line split the leftover space, each still
                # capped at its max width.
                hl.addWidget(widget, 1)
                placed += 1
            hl.addStretch()
            first   = next((k for k in keys if k in self._field_widgets), None)
            row_max = _ROW_WIDTHS.get(first) if first else None
            if row_max is not None:
                row.setMaximumWidth(row_max)
            # Uniform row height (incl. lines whose field isn't a text box, e.g.
            # the Variants checkbox) so every line matches, not just the inputs.
            if _ROW_HEIGHT is not None:
                row.setFixedHeight(_ROW_HEIGHT)
            form.addRow(row)

        # --- build every field widget --------------------------------------
        # self._field_widgets[key] is the widget placed on a line: the plain
        # input for most, or a wrapper (input + inline buttons) for country /
        # series. The per-field self._*_input attributes are kept as-is since
        # the rest of the class reads/writes them by name.
        self._field_widgets: dict[str, QWidget] = {}

        self._title_input = _field("title")
        self._field_widgets["title"] = self._title_input

        self._scott_input = _field("scott")
        self._scott_input.textChanged.connect(self._schedule_duplicate_check)
        self._field_widgets["scott"] = self._scott_input

        country_wrap   = QWidget()
        country_hl     = QHBoxLayout(country_wrap)
        country_hl.setContentsMargins(0, 0, 0, 0)
        country_hl.setSpacing(2)
        self._country_input = _field("country")
        self._country_input.editingFinished.connect(
            lambda: self._country_input.setText(
                get_country_name(self._country_input.text().strip())
                or self._country_input.text()
            )
        )
        self._country_input.textChanged.connect(self._auto_expand_country)
        self._country_input.textChanged.connect(self._schedule_duplicate_check)
        country_browse = QPushButton("▼")
        country_browse.setFixedWidth(24)
        country_browse.clicked.connect(self._browse_country)
        country_hl.addWidget(self._country_input)
        country_hl.addWidget(country_browse)
        country_hl.addStretch()   # pin input+button left so it aligns with the column
        self._field_widgets["country"] = country_wrap

        series_wrap = QWidget()
        series_hl   = QHBoxLayout(series_wrap)
        series_hl.setContentsMargins(0, 0, 0, 0)
        series_hl.setSpacing(2)
        self._series_input = _field("series")
        series_hl.addWidget(self._series_input)
        self._view_series_btn = QPushButton("View in DB")
        self._view_series_btn.setFixedWidth(76)
        self._view_series_btn.setEnabled(False)
        self._view_series_btn.clicked.connect(self._view_series_in_db)
        series_hl.addWidget(self._view_series_btn)
        self._series_url_btn = QPushButton("↗")
        self._series_url_btn.setFixedWidth(28)
        self._series_url_btn.setEnabled(False)
        self._series_url_btn.clicked.connect(self._open_series_url)
        series_hl.addWidget(self._series_url_btn)
        # Count of stamps in this series already saved in the DB. Populated by
        # _refresh_series_count() whenever a series is loaded/fetched.
        self._series_count_lbl = QLabel("")
        self._series_count_lbl.setStyleSheet("color: gray; font-size: 11px;")
        self._series_count_lbl.setVisible(False)
        series_hl.addWidget(self._series_count_lbl)
        series_hl.addStretch()   # pin input+buttons left so it aligns with the column
        self._field_widgets["series"] = series_wrap

        self._emission_input = _field("emission")
        self._field_widgets["emission"] = self._emission_input

        self._face_value_input = _field("face_value")
        self._field_widgets["face_value"] = self._face_value_input

        self._issued_input = _field("issued")
        self._field_widgets["issued"] = self._issued_input

        self._expired_input = _field("expired")
        self._field_widgets["expired"] = self._expired_input

        self._series_complete_combo = QComboBox()
        self._series_complete_combo.addItems(["NO", "YES", "YES MISSING VARIANTS", ""])
        _sc_w = _DROPDOWN_WIDTHS.get("series_complete")
        if _sc_w is not None:
            self._series_complete_combo.setMaximumWidth(_sc_w)
        if _ROW_HEIGHT is not None:
            self._series_complete_combo.setFixedHeight(_ROW_HEIGHT)
        self._field_widgets["series_complete"] = self._series_complete_combo

        self._size_input = _field("size")
        self._field_widgets["size"] = self._size_input

        self._perforation_input = _field("perforation")
        self._field_widgets["perforation"] = self._perforation_input

        self._paper_input = _field("paper")
        self._field_widgets["paper"] = self._paper_input

        self._gum_input = _field("gum")
        self._field_widgets["gum"] = self._gum_input

        self._watermark_input = _field("watermark")
        self._field_widgets["watermark"] = self._watermark_input

        self._printing_input = _field("printing")
        self._field_widgets["printing"] = self._printing_input

        self._format_input = _field("format")
        self._field_widgets["format"] = self._format_input

        self._print_run_input = _field("print_run")
        self._field_widgets["print_run"] = self._print_run_input

        self._colors_input = _field("colors")
        self._field_widgets["colors"] = self._colors_input

        self._designers_input = _field("designers")
        self._field_widgets["designers"] = self._designers_input

        self._variants_cb = QCheckBox()
        self._field_widgets["variants"] = self._variants_cb

        self._description_input = _field("description")
        self._field_widgets["description"] = self._description_input

        # --- place the fields on lines per _FORM_LAYOUT --------------------
        for _line in _FORM_LAYOUT:
            _add_line(_line)

        outer.addLayout(form)

        # Variant set row (shown when variants checkbox is checked)
        self._variant_set_row = QWidget()
        vs_rl = QHBoxLayout(self._variant_set_row)
        vs_rl.setContentsMargins(0, 2, 0, 2)
        vs_rl.addWidget(QLabel("Variant Set:"))
        self._variant_set_lbl = QLabel("None")
        self._variant_set_lbl.setStyleSheet("color: gray; font-style: italic;")
        vs_rl.addWidget(self._variant_set_lbl)
        vs_select_btn = QPushButton("Select…")
        vs_select_btn.clicked.connect(self._open_variant_set_picker)
        vs_rl.addWidget(vs_select_btn)
        vs_rl.addStretch()
        self._variant_set_row.setVisible(False)
        outer.addWidget(self._variant_set_row)

        self._variant_set_notes_lbl = QLabel("")
        self._variant_set_notes_lbl.setStyleSheet("color: gray; font-style: italic; font-size: 10px;")
        self._variant_set_notes_lbl.setWordWrap(True)
        self._variant_set_notes_lbl.setVisible(False)
        outer.addWidget(self._variant_set_notes_lbl)

        self._variants_cb.toggled.connect(self._variant_set_row.setVisible)
        self._variants_cb.toggled.connect(
            lambda checked: self._variant_set_notes_lbl.setVisible(
                checked and bool(self._variant_set_notes_lbl.text())
            )
        )

        # Duplicate warning
        self._duplicate_warning = QLabel()
        self._duplicate_warning.setOpenExternalLinks(False)
        self._duplicate_warning.linkActivated.connect(self._show_duplicate_preview)
        self._duplicate_warning.setVisible(False)
        outer.addWidget(self._duplicate_warning)

        # Series comments row
        self._series_comments_row = QWidget()
        scr_hl = QHBoxLayout(self._series_comments_row)
        scr_hl.setContentsMargins(0, 0, 0, 0)
        scr_hl.addWidget(QLabel("Series Notes:"))
        self._series_comments_input = _field("series_comments")
        self._series_comments_input.setPlaceholderText("Notes about this series…")
        scr_hl.addWidget(self._series_comments_input)
        self._series_comments_row.setVisible(False)
        outer.addWidget(self._series_comments_row)

        # Physical location
        loc_row = QHBoxLayout()
        loc_row.addWidget(QLabel("Physical Location:"))
        self._location_combo = QComboBox()
        self._location_combo.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        _loc_w = _DROPDOWN_WIDTHS.get("location")
        if _loc_w is not None:
            self._location_combo.setMaximumWidth(_loc_w)
        loc_row.addWidget(self._location_combo)
        add_loc_btn = QPushButton("+")
        add_loc_btn.setFixedWidth(28)
        add_loc_btn.setToolTip("Add new location")
        add_loc_btn.clicked.connect(self._add_new_location)
        loc_row.addWidget(add_loc_btn)
        loc_row.addStretch()
        outer.addLayout(loc_row)
        self._load_location_combo()

        # Copies section
        copies_header = QHBoxLayout()
        copies_header.addWidget(QLabel("Copies:"))
        copies_header.addStretch()
        add_copy_btn = QPushButton("+ Add Row")
        add_copy_btn.setFixedWidth(90)
        add_copy_btn.clicked.connect(lambda: self._add_copy_row())
        copies_header.addWidget(add_copy_btn)
        outer.addLayout(copies_header)

        copies_scroll = QScrollArea()
        copies_scroll.setWidgetResizable(True)
        copies_scroll.setMinimumHeight(60)
        copies_scroll.setMaximumHeight(160)
        _disable_horizontal_scroll(copies_scroll)
        hide_scrollbars(copies_scroll)
        copies_scroll.setFrameShape(QFrame.Shape.NoFrame)
        copies_scroll.setAutoFillBackground(False)
        copies_scroll.viewport().setAutoFillBackground(False)
        copies_inner = QWidget()
        self._copies_layout = QVBoxLayout(copies_inner)
        self._copies_layout.setContentsMargins(0, 0, 0, 0)
        self._copies_layout.setSpacing(0)
        self._copies_layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        copies_scroll.setWidget(copies_inner)
        copies_inner.setAutoFillBackground(False)  # setWidget() re-enables it; undo again
        outer.addWidget(copies_scroll)

        # Action buttons
        action_row = QHBoxLayout()
        self._save_btn = QPushButton("Add Stamp to Database")
        self._save_btn.clicked.connect(self.save_stamp)
        self._delete_btn = QPushButton("Delete Stamp")
        self._delete_btn.setVisible(False)
        self._delete_btn.clicked.connect(self.delete_stamp)
        self._reassign_btn = QPushButton("Reassign Image →")
        self._reassign_btn.setVisible(False)
        self._reassign_btn.clicked.connect(self._reassign_image)
        search_colnect_btn   = QPushButton("Search Colnect")
        search_colnect_btn.clicked.connect(self._search_colnect)
        get_colnect_info_btn = QPushButton("Get Colnect Info")
        get_colnect_info_btn.clicked.connect(self._get_colnect_info)
        # Session-only toggle: when checked, Search Colnect filters by country +
        # selected theme + year (from Date Issued) + face value instead of Scott #.
        self._filter_search_cb = QCheckBox("Value/Year")
        self._filter_search_cb.setToolTip(
            "Search Colnect by country + selected theme + year + face value "
            "instead of Scott number")
        # Enable theme selection while this is on so a theme can be picked to
        # search with, even in add mode (where the panel is otherwise read-only).
        self._filter_search_cb.toggled.connect(self._on_filter_search_toggled)

        for btn in (self._save_btn, self._delete_btn, self._reassign_btn):
            action_row.addWidget(btn)

        # Colnect actions on their own row so all five buttons stay visible when
        # editing an existing stamp (Delete + Reassign appear then, and a single
        # row would overflow the panel width).
        colnect_row = QHBoxLayout()
        colnect_row.addWidget(search_colnect_btn)
        colnect_row.addWidget(self._filter_search_cb)
        colnect_row.addWidget(get_colnect_info_btn)

        # Colnect row on top, then the save (Add/Update) + Delete/Reassign row
        # below it, so "Add Stamp to Database" sits under the Colnect buttons.
        outer.addLayout(colnect_row)
        outer.addLayout(action_row)

        # Colnect spinner
        self._colnect_spinner = SpinnerWidget(QColor(80, 200, 160), self.content_widget)
        outer.addWidget(self._colnect_spinner)

        outer.addStretch(1)
