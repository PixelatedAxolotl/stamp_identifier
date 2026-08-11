# ui/panels/filter_builder.py
#
# FilterBuilder — the advanced "add a rule" panel for the gallery. It produces a
# list of rule dicts (see db.gallery_filters) and a match mode ("AND"/"OR"),
# which the gallery hands to StampService.get_gallery_stamps.
#
# FilterRuleRow — one rule: [field ▾] [operator ▾] [value editor] [✕]
#
# v1: one value per rule; filtering is applied on an explicit Apply click, not
# live as rows are edited (a half-built row shouldn't fire queries).

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QComboBox, QLineEdit, QDateEdit,
    QPushButton, QLabel, QSizePolicy,
)
from PySide6.QtCore import Qt, Signal, QDate
from PySide6.QtGui import QIntValidator

from db.gallery_filters import FIELDS, FIELDS_BY_KEY, OPS, FT, VALUELESS_OPS


class FilterRuleRow(QWidget):
    """A single filter rule. Emits `removed` so the builder can drop it."""

    removed = Signal(object)  # emits self

    def __init__(self, distinct_provider, parent=None):
        super().__init__(parent)
        self._distinct_provider = distinct_provider
        self._value_widget = None
        self._value_getter = None

        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(4)

        self._field = QComboBox()
        for spec in FIELDS:
            self._field.addItem(spec.label, spec.key)
        self._field.currentIndexChanged.connect(self._on_field_changed)
        row.addWidget(self._field, 3)

        self._op = QComboBox()
        self._op.currentIndexChanged.connect(self._rebuild_value_editor)
        row.addWidget(self._op, 2)

        # Value editor lives in this container; it's swapped out per field/op.
        self._value_host = QWidget()
        self._value_layout = QHBoxLayout(self._value_host)
        self._value_layout.setContentsMargins(0, 0, 0, 0)
        self._value_layout.setSpacing(0)
        self._value_host.setSizePolicy(QSizePolicy.Policy.Expanding,
                                       QSizePolicy.Policy.Fixed)
        row.addWidget(self._value_host, 4)

        remove_btn = QPushButton("✕")
        remove_btn.setFixedWidth(24)
        remove_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        remove_btn.setToolTip("Remove this filter")
        remove_btn.clicked.connect(lambda: self.removed.emit(self))
        row.addWidget(remove_btn, 0)

        # Prime operators + value editor for the initially selected field.
        self._populate_operators()

    # -- rule output --------------------------------------------------------

    def to_rule(self) -> dict | None:
        """Return this row as a rule dict, or None if it's incomplete."""
        field = self._field.currentData()
        op = self._op.currentData()
        if not field or not op:
            return None
        if op in VALUELESS_OPS:
            return {"field": field, "op": op}
        if self._value_getter is None:
            return None
        value = self._value_getter()
        if value in (None, ""):
            return None
        return {"field": field, "op": op, "value": value}

    # -- internal -----------------------------------------------------------

    def _current_spec(self):
        return FIELDS_BY_KEY.get(self._field.currentData())

    def _on_field_changed(self):
        self._populate_operators()  # also rebuilds the value editor

    def _populate_operators(self):
        spec = self._current_spec()
        self._op.blockSignals(True)
        self._op.clear()
        if spec is not None:
            for op_id, label in OPS[spec.type]:
                self._op.addItem(label, op_id)
        self._op.blockSignals(False)
        self._rebuild_value_editor()

    def _rebuild_value_editor(self):
        # Tear down the previous editor.
        if self._value_widget is not None:
            self._value_widget.deleteLater()
            self._value_widget = None
            self._value_getter = None

        spec = self._current_spec()
        op = self._op.currentData()
        if spec is None or op in VALUELESS_OPS:
            return  # valueless op — no editor needed

        widget, getter = self._make_value_editor(spec, op)
        self._value_widget = widget
        self._value_getter = getter
        if widget is not None:
            self._value_layout.addWidget(widget)

    def _make_value_editor(self, spec, op):
        """Return (widget, getter) for the given field/op."""
        if spec.type in (FT.ENUM, FT.RELATION):
            values = self._distinct_provider(spec.key) if self._distinct_provider else []
            if values:
                combo = QComboBox()
                for v in values:
                    combo.addItem(str(v), v)
                return combo, lambda c=combo: c.currentData()
            # No known values yet — let the user type one anyway.
            edit = QLineEdit()
            edit.setPlaceholderText("value")
            return edit, lambda e=edit: e.text().strip()

        if spec.type == FT.NUMBER:
            edit = QLineEdit()
            edit.setValidator(QIntValidator())
            edit.setPlaceholderText("number")
            return edit, lambda e=edit: e.text().strip()

        if spec.type == FT.DATE:
            date = QDateEdit()
            date.setCalendarPopup(True)
            date.setDisplayFormat("yyyy-MM-dd")
            date.setDate(QDate.currentDate())
            return date, lambda d=date: d.date().toString("yyyy-MM-dd")

        # TEXT
        edit = QLineEdit()
        edit.setPlaceholderText("value")
        return edit, lambda e=edit: e.text().strip()


class FilterBuilder(QWidget):
    """Holds the match-mode selector, a list of rule rows, and Apply/Clear.

    Emits `apply_requested` when Apply is clicked; the gallery reads `to_rules()`
    and `match()` at that point and reloads.
    """

    apply_requested = Signal()

    def __init__(self, distinct_provider, parent=None):
        super().__init__(parent)
        self._distinct_provider = distinct_provider
        self._rows: list[FilterRuleRow] = []

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(4)

        # Match mode
        match_row = QHBoxLayout()
        match_row.addWidget(QLabel("Match"))
        self._match = QComboBox()
        self._match.addItem("all rules", "AND")
        self._match.addItem("any rule", "OR")
        match_row.addWidget(self._match)
        match_row.addStretch()
        outer.addLayout(match_row)

        # Rows container
        self._rows_layout = QVBoxLayout()
        self._rows_layout.setContentsMargins(0, 0, 0, 0)
        self._rows_layout.setSpacing(4)
        outer.addLayout(self._rows_layout)

        # Buttons
        btn_row = QHBoxLayout()
        add_btn = QPushButton("＋ Add filter")
        add_btn.clicked.connect(lambda: self.add_rule())
        self._apply_btn = QPushButton("Apply")
        self._apply_btn.clicked.connect(self.apply_requested)
        clear_btn = QPushButton("Clear")
        clear_btn.clicked.connect(self._on_clear)
        btn_row.addWidget(add_btn)
        btn_row.addStretch()
        btn_row.addWidget(clear_btn)
        btn_row.addWidget(self._apply_btn)
        outer.addLayout(btn_row)

    # -- public -------------------------------------------------------------

    def to_rules(self) -> list[dict]:
        return [r for row in self._rows if (r := row.to_rule()) is not None]

    def match(self) -> str:
        return self._match.currentData()

    def add_rule(self) -> FilterRuleRow:
        row = FilterRuleRow(self._distinct_provider)
        row.removed.connect(self._remove_row)
        self._rows.append(row)
        self._rows_layout.addWidget(row)
        return row

    # -- internal -----------------------------------------------------------

    def _remove_row(self, row: FilterRuleRow):
        if row in self._rows:
            self._rows.remove(row)
            self._rows_layout.removeWidget(row)
            row.deleteLater()

    def _on_clear(self):
        for row in list(self._rows):
            self._remove_row(row)
        # Clearing is itself an apply — an emptied builder should show all stamps.
        self.apply_requested.emit()
