# ui/field_defaults.py
#
# Batch defaults — values that are pre-filled into the Fields form whenever a
# brand-new stamp is started (a History thumbnail is picked, or a fresh capture
# is taken). Intended for entering a batch of stamps that share the same
# country / acquisition details: set them once, leave them on for the batch,
# switch them off when the batch is done.
#
# Defaults are NEVER applied when an existing collection stamp is loaded from
# the Gallery or Database — that path goes through FieldsPanel.load_stamp(),
# which this module is deliberately not wired into.
#
# Two pieces live here:
#   StampDefaults  — the values + per-field on/off flags + master toggle,
#                    persisted as JSON (config.DEFAULTS_FILE).
#   DefaultsDialog — the editor, opened from the "Defaults…" button in the
#                    Fields panel.

import json
import os

from PySide6.QtWidgets import (
    QDialog, QDialogButtonBox, QVBoxLayout, QHBoxLayout, QGridLayout, QLabel,
    QLineEdit, QCheckBox, QComboBox, QSpinBox, QScrollArea, QWidget, QFrame,
    QListWidget, QAbstractItemView, QPushButton,
)
from PySide6.QtCore import Qt

from config import DEFAULTS_FILE
from db.session import SessionLocal
from db.service import (
    ThemeService, PhysicalLocationService, StampCopyService, OriginService,
)
from helper_utils import get_country_name
from logger import logger


# Stamp fields that can carry a default, in the order they appear in the
# dialog. Keys match FieldsPanel's own field keys so the labels can be shared.
STAMP_FIELD_KEYS: list[str] = [
    "title", "scott", "country", "series", "series_complete",
    "emission", "face_value", "issued", "expired",
    "size", "perforation", "paper", "gum", "watermark", "printing",
    "format", "print_run", "colors", "designers", "description", "variants",
]

# Fields whose editor is not a plain text box.
_BOOL_KEYS  = {"variants"}
_COMBO_KEYS = {"series_complete": ["", "NO", "YES", "YES MISSING VARIANTS"]}

# Non-stamp-field keys, with their dialog labels.
PLACEMENT_LABELS = {
    "physical_location": "Physical Location:",
    "themes":            "Themes:",
}
COPY_LABELS = {
    "copy_condition": "Condition:",
    "copy_quantity":  "Quantity:",
}
ORIGIN_LABELS = {
    "origin_location": "Location:",
    "origin_dealer":   "Dealer / Seller:",
    "origin_method":   "Acquired by:",
    "origin_price":    "Price:",
    "origin_date":     "Date:",
    "origin_notes":    "Notes:",
}

# Origin default key -> the key used in a copy row's `_origin` dict.
ORIGIN_KEY_MAP = {
    "origin_location": "location",
    "origin_dealer":   "dealer",
    "origin_method":   "method",
    "origin_price":    "price",
    "origin_date":     "acquired_date",
    "origin_notes":    "notes",
}

ALL_KEYS: list[str] = (
    STAMP_FIELD_KEYS
    + list(PLACEMENT_LABELS)
    + list(COPY_LABELS)
    + list(ORIGIN_LABELS)
)

# Keys shown in the one-line summary next to the toggle, most identifying first.
_SUMMARY_ORDER = [
    "country", "origin_location", "origin_date", "origin_dealer",
    "physical_location", "series", "themes",
]


def _is_set(value) -> bool:
    """True if `value` is worth applying. Booleans and numbers always count —
    only blank text and empty theme lists are treated as 'nothing to apply'."""
    if value is None:
        return False
    if isinstance(value, str):
        return value.strip() != ""
    if isinstance(value, (list, tuple, set, dict)):
        return len(value) > 0
    return True


class StampDefaults:
    """Per-field default values plus their on/off flags, persisted to JSON.

    A default is only applied when the master toggle (`active`) is on, the
    field's own flag is on, and the stored value is non-empty.
    """

    def __init__(self, path: str = DEFAULTS_FILE):
        self.path = path
        self.active: bool = False
        self.values:  dict = {}
        self.enabled: dict[str, bool] = {}
        self.load()

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def load(self):
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except FileNotFoundError:
            return
        except Exception as e:
            logger.warning(f"Could not read stamp defaults from {self.path}: {e}")
            return
        if not isinstance(data, dict):
            logger.warning(f"Ignoring malformed stamp defaults in {self.path}")
            return
        self.active  = bool(data.get("active", False))
        self.values  = dict(data.get("values") or {})
        self.enabled = {k: bool(v) for k, v in (data.get("enabled") or {}).items()}

    def save(self):
        try:
            parent = os.path.dirname(self.path)
            if parent:
                os.makedirs(parent, exist_ok=True)
            with open(self.path, "w", encoding="utf-8") as fh:
                json.dump(
                    {"active": self.active, "values": self.values, "enabled": self.enabled},
                    fh, indent=2, ensure_ascii=False,
                )
        except Exception as e:
            logger.error(f"Could not write stamp defaults to {self.path}: {e}")

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    def is_set(self, key: str) -> bool:
        """True if `key` is ticked and holds a value — regardless of whether the
        master toggle is currently on. This is what the Fields panel indicator
        describes, so switching the master toggle off doesn't make a configured
        batch look like it was erased."""
        return bool(self.enabled.get(key)) and _is_set(self.values.get(key))

    def is_on(self, key: str) -> bool:
        """True if `key`'s default should be applied right now."""
        return self.active and self.is_set(key)

    def get(self, key: str, fallback=None):
        """The value to apply for `key`, or `fallback` when it isn't active."""
        return self.values.get(key) if self.is_on(key) else fallback

    def active_keys(self) -> list[str]:
        """Every ticked, non-empty key, in ALL_KEYS order."""
        return [k for k in ALL_KEYS if self.is_set(k)]

    def any_active(self, keys) -> bool:
        return any(self.is_on(k) for k in keys)

    def summary(self) -> str:
        """One-line description of what's on, for the Fields panel indicator."""
        keys = self.active_keys()
        if not keys:
            return "none set"
        ordered = [k for k in _SUMMARY_ORDER if k in keys]
        ordered += [k for k in keys if k not in ordered]
        parts = []
        for key in ordered[:3]:
            value = self.values.get(key)
            if isinstance(value, (list, tuple)):
                value = ", ".join(str(v) for v in value)
            elif isinstance(value, bool):
                value = key.replace("_", " ")
            parts.append(str(value))
        text = " · ".join(parts)
        extra = len(keys) - len(parts)
        if extra > 0:
            text += f"  (+{extra} more)"
        return text

    def tooltip(self) -> str:
        """Full key: value listing, shown on hover over the indicator."""
        keys = self.active_keys()
        if not keys:
            return "No defaults are set. Click Defaults… to add some."
        lines = []
        for key in keys:
            value = self.values.get(key)
            if isinstance(value, (list, tuple)):
                value = ", ".join(str(v) for v in value)
            # Qualify the origin rows — their bare labels ("Location:") would
            # otherwise be indistinguishable from the stamp's Physical Location.
            prefix = "Acquired · " if key in ORIGIN_LABELS else ""
            lines.append(f"{prefix}{_label_for(key)} {value}")
        return "\n".join(lines)


def _label_for(key: str, field_labels: dict | None = None) -> str:
    if field_labels and key in field_labels:
        return field_labels[key]
    for table in (PLACEMENT_LABELS, COPY_LABELS, ORIGIN_LABELS):
        if key in table:
            return table[key]
    return key.replace("_", " ").title() + ":"


class DefaultsDialog(QDialog):
    """Editor for the batch defaults.

    Every row is a checkbox + label + editor. The checkbox is what decides
    whether that default is applied, so a value can be parked (kept typed in
    but switched off) instead of having to be cleared and retyped. Editing a
    row's value ticks its checkbox automatically.

    `field_labels` is FieldsPanel's own label map, passed in so the stamp-field
    rows read exactly like the real form without this module importing (and
    circularly depending on) the panel.
    """

    def __init__(self, parent, defaults: StampDefaults, field_labels: dict | None = None):
        super().__init__(parent)
        self.setWindowTitle("Stamp Entry Defaults")
        self.setMinimumWidth(460)
        self.setMinimumHeight(560)

        self._defaults    = defaults
        self._field_labels = field_labels or {}
        self._checks: dict[str, QCheckBox] = {}
        self._editors: dict[str, QWidget]  = {}

        self._load_choices()

        outer = QVBoxLayout(self)

        intro = QLabel(
            "Values ticked here are filled in automatically when a new stamp is "
            "started from the History strip or a fresh capture. Existing stamps "
            "opened from the Gallery or Database are never touched."
        )
        intro.setWordWrap(True)
        intro.setStyleSheet("color: gray;")
        outer.addWidget(intro)

        self._master_cb = QCheckBox("Apply these defaults to new stamps")
        self._master_cb.setChecked(defaults.active)
        outer.addWidget(self._master_cb)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        inner = QWidget()
        self._grid = QGridLayout(inner)
        self._grid.setContentsMargins(0, 4, 0, 4)
        self._grid.setHorizontalSpacing(8)
        self._grid.setVerticalSpacing(4)
        self._grid.setColumnStretch(2, 1)
        scroll.setWidget(inner)
        outer.addWidget(scroll, 1)

        self._build_rows()

        footer = QHBoxLayout()
        clear_btn = QPushButton("Clear All")
        clear_btn.setToolTip("Untick every row and erase its value")
        clear_btn.clicked.connect(self._clear_all)
        footer.addWidget(clear_btn)
        untick_btn = QPushButton("Untick All")
        untick_btn.setToolTip("Switch every row off but keep the values for later")
        untick_btn.clicked.connect(self._untick_all)
        footer.addWidget(untick_btn)
        footer.addStretch()
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        footer.addWidget(buttons)
        outer.addLayout(footer)

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------

    def _load_choices(self):
        """Read the suggestion lists (locations, dealers, themes…) in one go."""
        session = SessionLocal()
        try:
            self._locations       = [l.name for l in PhysicalLocationService.get_all(session)]
            self._origin_locations = [l.name for l in OriginService.get_all_locations(session)]
            self._dealers         = [d.name for d in OriginService.get_all_dealers(session)]
            self._methods         = OriginService.get_method_suggestions(session)
            self._themes          = sorted(t.name for t in ThemeService.get_all_themes(session))
        except Exception as e:
            logger.warning(f"Could not load default suggestions: {e}")
            self._locations = self._origin_locations = self._dealers = []
            self._methods = self._themes = []
        finally:
            session.close()

    def _add_header(self, text: str):
        row = self._grid.rowCount()
        if row > 0:
            spacer = QLabel()
            spacer.setFixedHeight(6)
            self._grid.addWidget(spacer, row, 0, 1, 3)
            row += 1
        lbl = QLabel(text)
        lbl.setStyleSheet("font-weight: bold;")
        self._grid.addWidget(lbl, row, 0, 1, 3)

    def _add_row(self, key: str, editor: QWidget, label: str | None = None,
                 align_top: bool = False):
        row = self._grid.rowCount()
        cb  = QCheckBox()
        cb.setChecked(bool(self._defaults.enabled.get(key)))
        cb.setToolTip("Apply this default")
        # Tall editors (the themes list) read better with their checkbox and
        # label beside the first entry rather than floating at the mid-point.
        flags = Qt.AlignmentFlag.AlignTop if align_top else Qt.AlignmentFlag(0)
        self._grid.addWidget(cb, row, 0, flags)
        self._grid.addWidget(QLabel(label or _label_for(key, self._field_labels)), row, 1, flags)
        self._grid.addWidget(editor, row, 2)
        self._checks[key]  = cb
        self._editors[key] = editor
        # Typing a value implies wanting it: tick the row so a filled-in default
        # can't silently sit switched off.
        self._connect_autotick(key, editor, cb)

    def _connect_autotick(self, key: str, editor: QWidget, cb: QCheckBox):
        def tick(*_):
            if not cb.isChecked():
                cb.setChecked(True)
        if isinstance(editor, QLineEdit):
            editor.textEdited.connect(tick)
        elif isinstance(editor, QComboBox):
            editor.activated.connect(tick)
            if editor.isEditable():
                editor.lineEdit().textEdited.connect(tick)
        elif isinstance(editor, QSpinBox):
            editor.valueChanged.connect(tick)
        elif isinstance(editor, QCheckBox):
            editor.clicked.connect(tick)
        elif isinstance(editor, QListWidget):
            editor.itemSelectionChanged.connect(tick)

    def _build_rows(self):
        vals = self._defaults.values

        self._add_header("Stamp Fields")
        for key in STAMP_FIELD_KEYS:
            stored = vals.get(key)
            if key in _BOOL_KEYS:
                editor = QCheckBox()
                editor.setChecked(bool(stored))
            elif key in _COMBO_KEYS:
                editor = QComboBox()
                editor.addItems(_COMBO_KEYS[key])
                idx = editor.findText(str(stored or ""))
                editor.setCurrentIndex(max(idx, 0))
            else:
                editor = QLineEdit(str(stored or ""))
            self._add_row(key, editor)

        # Expand country codes the same way the real Country field does.
        country = self._editors.get("country")
        if isinstance(country, QLineEdit):
            country.editingFinished.connect(
                lambda: country.setText(
                    get_country_name(country.text().strip()) or country.text()
                )
            )

        self._add_header("Placement")
        loc = QComboBox()
        loc.addItem("")
        loc.addItems(self._locations)
        loc.setCurrentText(str(vals.get("physical_location") or ""))
        self._add_row("physical_location", loc)

        themes = QListWidget()
        themes.setSelectionMode(QAbstractItemView.SelectionMode.MultiSelection)
        themes.addItems(self._themes)
        themes.setMaximumHeight(110)
        stored_themes = {str(t).lower() for t in (vals.get("themes") or [])}
        for i in range(themes.count()):
            item = themes.item(i)
            item.setSelected(item.text().lower() in stored_themes)
        self._add_row("themes", themes, align_top=True)

        self._add_header("Default Copy Row")
        cond = QComboBox()
        cond.addItems(StampCopyService.CONDITIONS)
        idx = cond.findText(str(vals.get("copy_condition") or ""))
        if idx >= 0:
            cond.setCurrentIndex(idx)
        self._add_row("copy_condition", cond)

        qty = QSpinBox()
        qty.setRange(1, 9999)
        try:
            qty.setValue(max(1, int(vals.get("copy_quantity") or 1)))
        except (TypeError, ValueError):
            qty.setValue(1)
        self._add_row("copy_quantity", qty)

        self._add_header("Acquisition (copy origin)")
        for key, choices in (
            ("origin_location", self._origin_locations),
            ("origin_dealer",   self._dealers),
            ("origin_method",   self._methods),
        ):
            combo = QComboBox()
            combo.setEditable(True)
            combo.addItem("")
            combo.addItems(choices)
            combo.setCurrentText(str(vals.get(key) or ""))
            self._add_row(key, combo)

        price = QLineEdit(str(vals.get("origin_price") or ""))
        price.setPlaceholderText("e.g. 5.00")
        self._add_row("origin_price", price)

        date = QLineEdit(str(vals.get("origin_date") or ""))
        date.setPlaceholderText("YYYY-MM-DD")
        self._add_row("origin_date", date)

        self._add_row("origin_notes", QLineEdit(str(vals.get("origin_notes") or "")))

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------

    def _untick_all(self):
        for cb in self._checks.values():
            cb.setChecked(False)

    def _clear_all(self):
        for key, editor in self._editors.items():
            if isinstance(editor, QLineEdit):
                editor.clear()
            elif isinstance(editor, QComboBox):
                if editor.isEditable():
                    editor.setCurrentText("")
                else:
                    editor.setCurrentIndex(0)
            elif isinstance(editor, QSpinBox):
                editor.setValue(1)
            elif isinstance(editor, QCheckBox):
                editor.setChecked(False)
            elif isinstance(editor, QListWidget):
                editor.clearSelection()
        # Clearing the values fires the auto-tick handlers on some widgets, so
        # untick afterwards rather than before.
        self._untick_all()

    def _read_editor(self, key: str):
        editor = self._editors[key]
        if isinstance(editor, QLineEdit):
            return editor.text().strip()
        if isinstance(editor, QComboBox):
            return editor.currentText().strip()
        if isinstance(editor, QSpinBox):
            return editor.value()
        if isinstance(editor, QCheckBox):
            return editor.isChecked()
        if isinstance(editor, QListWidget):
            return [i.text() for i in editor.selectedItems()]
        return None

    def accept(self):
        d = self._defaults
        d.active = self._master_cb.isChecked()
        for key in self._editors:
            d.values[key]  = self._read_editor(key)
            d.enabled[key] = self._checks[key].isChecked()
        d.save()
        super().accept()
