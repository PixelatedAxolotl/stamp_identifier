# ui/country_field.py
#
# Shared behaviour for any Country text box, so the Fields panel and the batch
# Defaults dialog (ui/field_defaults.py) behave identically instead of each
# wiring their own subset.
#
# Country entry expands abbreviations in two stages, because the two cases want
# different timing:
#
#   live (textChanged)      — a 1-2 character code expands the moment it becomes
#                             unambiguous, so "FR" turns into "France" as the R
#                             is typed. Held back while the text is still a
#                             prefix of a longer known code: "US" waits, because
#                             "USSR" exists and the user may not be done.
#   commit (editingFinished)— on Tab/Enter/blur anything get_country_name can
#                             resolve is expanded, including the longer custom
#                             codes ("DDR", "CEY") that the live pass skips.
#
# This lived as FieldsPanel._auto_expand_country, which read self._country_input
# directly and so could not be reused; the Defaults dialog wired only the commit
# stage and its country box sat inert while typing. Both stages take the widget
# as an argument here so either caller gets the whole behaviour.
#
# Kept in the ui package rather than helper_utils so that module stays free of a
# Qt dependency — it is otherwise pure lookup logic.

from PySide6.QtWidgets import QLineEdit, QPushButton, QDialog

from helper_utils import COUNTRY_MAP, get_country_name
from logger import logger


def _set_silently(line_edit: QLineEdit, text: str) -> None:
    """setText without re-entering the textChanged handler that called us."""
    line_edit.blockSignals(True)
    try:
        line_edit.setText(text)
    finally:
        line_edit.blockSignals(False)


def _expand_live(line_edit: QLineEdit, text: str) -> None:
    """Expand a short code on each keystroke, but only once it is unambiguous."""
    try:
        t = (text or "").strip()
        if not t or len(t) > 2:
            return
        key = t.upper()
        # Still a prefix of something longer ("US" vs "USSR") — the user may not
        # have finished typing, so leave it for the commit stage.
        if any(k.startswith(key) and len(k) > len(key) for k in COUNTRY_MAP):
            return
        if key in COUNTRY_MAP:
            name = COUNTRY_MAP[key]
            if name != text:
                _set_silently(line_edit, name)
            return
        # A 2-letter code that is not overridden locally may still be a real
        # ISO alpha-2, which get_country_name resolves via pycountry.
        if len(t) == 2 and t.isalpha():
            name = get_country_name(t)
            if name and name != text:
                _set_silently(line_edit, name)
    except Exception:
        # Expansion is a convenience; never let it block typing.
        pass


def _expand_on_commit(line_edit: QLineEdit) -> None:
    """Expand whatever is in the box on Tab/Enter/blur, leaving it alone if the
    text resolves to nothing (a country typed in full, or a genuine unknown)."""
    current = line_edit.text()
    line_edit.setText(get_country_name(current.strip()) or current)


def attach_country_expansion(line_edit: QLineEdit) -> QLineEdit:
    """Wire both expansion stages onto `line_edit`. Returns it for chaining.

    Additive: callers can still connect their own textChanged/editingFinished
    slots (the Fields panel hangs its duplicate check off the same box).
    """
    line_edit.textChanged.connect(lambda text: _expand_live(line_edit, text))
    line_edit.editingFinished.connect(lambda: _expand_on_commit(line_edit))
    return line_edit


def make_country_browse_button(line_edit: QLineEdit, parent=None,
                               on_selected=None) -> QPushButton:
    """The '▼' button that opens the searchable country picker and writes the
    chosen name into `line_edit`.

    `on_selected` is called with the chosen name after the text is set. The
    picker writes via setText, which does not emit textEdited, so a caller that
    keys off editing (the Defaults dialog ticks a row when its value changes)
    needs this to notice a pick.
    """
    button = QPushButton("▼", parent)
    button.setFixedWidth(24)
    button.setToolTip("Browse countries")

    def browse():
        try:
            # Imported lazily: the picker still lives in the old monolith, which
            # is expensive to import and pulls in the whole legacy UI.
            from stamp_identifier_v3 import CountryPickerDialog
        except Exception as e:
            logger.warning(f"CountryPickerDialog unavailable: {e}")
            return
        try:
            dlg = CountryPickerDialog(parent or line_edit)
            if dlg.exec() == QDialog.DialogCode.Accepted and dlg.selected_name:
                line_edit.setText(dlg.selected_name)
                if on_selected is not None:
                    on_selected(dlg.selected_name)
        except Exception as e:
            logger.warning(f"Country picker failed: {e}")

    button.clicked.connect(browse)
    return button
