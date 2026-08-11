# ui/panels/themes.py
#
# ThemesPanel — searchable, sortable theme/tag list.
# Coordinates with FieldsPanel: FieldsPanel calls get/set_selected_themes()
# when loading or saving a stamp.
#
# Public API (called by FieldsPanel via injected reference):
#   get_selected_themes() -> list[str]
#   set_selected_themes(names: list[str])
#   load_themes()

from PySide6.QtWidgets import (
    QVBoxLayout, QHBoxLayout, QLabel, QLineEdit, QPushButton,
    QCheckBox, QListWidget, QListWidgetItem, QMenu, QMessageBox, QInputDialog,
)
from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor

from db.session import SessionLocal
from db.service import ThemeService
from db.models import Theme
from helper_utils import load_theme_implications
from logger import logger
from ui.panel import Panel, hide_scrollbars


class ThemesPanel(Panel):

    def __init__(self, parent=None):
        super().__init__("Themes", parent)

        self._theme_sort      = "alpha"   # "alpha" | "recent" | "selected"
        self._reloading_themes = False

        outer = QVBoxLayout(self.content_widget)
        outer.setContentsMargins(6, 6, 6, 6)
        outer.setSpacing(4)

        # Header: label + sort button
        header = QHBoxLayout()
        header.addWidget(QLabel("Themes:"))
        header.addStretch()
        self._sort_btn = QPushButton("A-Z")
        self._sort_btn.setFixedWidth(54)
        self._sort_btn.setToolTip("Toggle sort: alphabetical / recently used / selected first")
        self._sort_btn.clicked.connect(self._toggle_sort)
        header.addWidget(self._sort_btn)
        outer.addLayout(header)

        # Quick checkboxes for common tags
        self._unchecked_chk  = QCheckBox("Unchecked")
        self._overprints_chk = QCheckBox("Has Overprints/Surcharges")
        self._unchecked_chk.clicked.connect(
            lambda checked: self._quick_theme_toggled("Unchecked", checked)
        )
        self._overprints_chk.clicked.connect(
            lambda checked: self._quick_theme_toggled("Has Overprints/Surcharges", checked)
        )
        outer.addWidget(self._unchecked_chk)
        outer.addWidget(self._overprints_chk)

        # Search
        self._search = QLineEdit()
        self._search.setPlaceholderText("Search themes…")
        self._search.textChanged.connect(self._filter_themes)
        outer.addWidget(self._search)

        # Add theme row
        add_row = QHBoxLayout()
        self._add_input = QLineEdit()
        self._add_input.setPlaceholderText("Add new theme…")
        add_btn = QPushButton("Add")
        add_btn.clicked.connect(self.add_theme)
        add_row.addWidget(self._add_input)
        add_row.addWidget(add_btn)
        outer.addLayout(add_row)

        # Management buttons
        aliases_btn = QPushButton("Manage Aliases…")
        aliases_btn.clicked.connect(self._open_alias_dialog)
        implications_btn = QPushButton("Manage Implications…")
        implications_btn.clicked.connect(self._open_implications_dialog)
        outer.addWidget(aliases_btn)
        outer.addWidget(implications_btn)

        # Theme list
        self._list = QListWidget()
        self._list.setSelectionMode(QListWidget.SelectionMode.NoSelection)
        self._list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._list.customContextMenuRequested.connect(self._show_context_menu)
        self._list.itemSelectionChanged.connect(self._on_selection_changed)
        self._list.itemClicked.connect(self._on_item_clicked)
        hide_scrollbars(self._list)
        outer.addWidget(self._list)

        self.load_themes()

    # ------------------------------------------------------------------
    # Public API for FieldsPanel coordination
    # ------------------------------------------------------------------

    def get_selected_themes(self) -> list[str]:
        return [item.text() for item in self._list.selectedItems()]

    def set_selected_themes(self, names: list[str]):
        name_set = {n.lower() for n in names}
        self._reloading_themes = True
        self._list.setSelectionMode(QListWidget.SelectionMode.MultiSelection)
        self._list.blockSignals(True)
        try:
            for i in range(self._list.count()):
                item = self._list.item(i)
                item.setSelected(item.text().lower() in name_set)
        finally:
            self._list.blockSignals(False)
            self._reloading_themes = False
        self._sync_quick_checkboxes()
        if self._theme_sort == "selected":
            self.load_themes()

    def set_interactive(self, enabled: bool):
        """FieldsPanel calls this: NoSelection in add mode, MultiSelection in edit mode."""
        mode = (QListWidget.SelectionMode.MultiSelection
                if enabled else QListWidget.SelectionMode.NoSelection)
        self._list.setSelectionMode(mode)

    def load_themes(self):
        previously_selected = {item.text() for item in self._list.selectedItems()}
        session = SessionLocal()
        try:
            self._reloading_themes = True
            self._list.blockSignals(True)
            self._list.clear()

            if self._theme_sort == "recent":
                themes = ThemeService.get_themes_by_recent_use(session)
            else:
                themes = ThemeService.get_all_themes(session)

            if self._theme_sort == "selected":
                selected   = [t for t in themes if t.name in previously_selected]
                unselected = [t for t in themes if t.name not in previously_selected]
                themes = selected + unselected

            names = [t.name for t in themes]
            self._list.addItems(names)
            if previously_selected:
                for i, name in enumerate(names):
                    if name in previously_selected:
                        self._list.item(i).setSelected(True)
            logger.info(f"Loaded {len(themes)} themes")
        except Exception as e:
            logger.error(f"Failed to load themes: {e}")
        finally:
            self._list.blockSignals(False)
            self._reloading_themes = False
            session.close()
        self._filter_themes(self._search.text())

    def add_theme(self, name: str | None = None):
        new_name = (name or self._add_input.text()).strip()
        if not new_name:
            return

        # If it already exists in the list, just select it
        for i in range(self._list.count()):
            if self._list.item(i).text().lower() == new_name.lower():
                self._list.setSelectionMode(QListWidget.SelectionMode.MultiSelection)
                self._list.blockSignals(True)
                self._list.item(i).setSelected(True)
                self._list.blockSignals(False)
                self._add_input.clear()
                if self._theme_sort == "selected":
                    self.load_themes()
                return

        session = SessionLocal()
        try:
            ThemeService.create_or_get_theme(session, new_name)
            self._list.setSelectionMode(QListWidget.SelectionMode.MultiSelection)
            self._list.blockSignals(True)
            new_item = QListWidgetItem(new_name)
            self._list.addItem(new_item)
            new_item.setSelected(True)
            self._list.blockSignals(False)
            if self._theme_sort == "selected":
                self.load_themes()
        except Exception as e:
            logger.error(f"Failed to add theme '{new_name}': {e}")
        finally:
            self._add_input.clear()
            session.close()

    # ------------------------------------------------------------------
    # Sort / filter
    # ------------------------------------------------------------------

    def _toggle_sort(self):
        modes  = ("alpha", "recent", "selected")
        labels = {"alpha": "A-Z", "recent": "Recent", "selected": "★"}
        self._theme_sort = modes[(modes.index(self._theme_sort) + 1) % len(modes)]
        self._sort_btn.setText(labels[self._theme_sort])
        self.load_themes()

    def _filter_themes(self, text: str = ""):
        text = (text or "").strip().lower()
        for i in range(self._list.count()):
            item = self._list.item(i)
            item.setHidden(bool(text) and text not in item.text().lower())

    # ------------------------------------------------------------------
    # Selection / implication logic
    # ------------------------------------------------------------------

    def _on_selection_changed(self):
        if self._theme_sort != "selected" or self._reloading_themes:
            return
        self.load_themes()

    def _on_item_clicked(self, item: QListWidgetItem):
        if not item.isSelected():
            return
        implications = load_theme_implications()
        implied = implications.get(item.text(), [])
        if not implied:
            return
        implied_lower = {x.lower() for x in implied}
        self._list.blockSignals(True)
        for i in range(self._list.count()):
            li = self._list.item(i)
            if li.text().lower() in implied_lower:
                li.setSelected(True)
        self._list.blockSignals(False)
        if self._theme_sort == "selected":
            self.load_themes()

    def _quick_theme_toggled(self, theme_name: str, checked: bool):
        self._list.setSelectionMode(QListWidget.SelectionMode.MultiSelection)
        target = theme_name.lower()
        found  = None
        for i in range(self._list.count()):
            if self._list.item(i).text().lower() == target:
                found = self._list.item(i)
                break
        self._list.blockSignals(True)
        if found:
            found.setSelected(checked)
        self._list.blockSignals(False)
        if checked and found:
            implications  = load_theme_implications()
            implied       = implications.get(theme_name, [])
            implied_lower = {x.lower() for x in implied}
            self._list.blockSignals(True)
            for i in range(self._list.count()):
                li = self._list.item(i)
                if li.text().lower() in implied_lower:
                    li.setSelected(True)
            self._list.blockSignals(False)
        self._list.repaint()
        if self._theme_sort == "selected":
            self.load_themes()
        self._sync_quick_checkboxes()

    def _sync_quick_checkboxes(self):
        selected = {
            self._list.item(i).text().lower()
            for i in range(self._list.count())
            if self._list.item(i).isSelected()
        }
        self._unchecked_chk.blockSignals(True)
        self._overprints_chk.blockSignals(True)
        self._unchecked_chk.setChecked("unchecked" in selected)
        self._overprints_chk.setChecked("has overprints/surcharges" in selected)
        self._unchecked_chk.blockSignals(False)
        self._overprints_chk.blockSignals(False)

    # ------------------------------------------------------------------
    # Context menu — rename / delete
    # ------------------------------------------------------------------

    def _show_context_menu(self, pos):
        item = self._list.itemAt(pos)
        if not item:
            return
        menu = QMenu(self)
        rename_action = menu.addAction("Rename tag…")
        delete_action = menu.addAction("Delete tag…")
        action = menu.exec(self._list.mapToGlobal(pos))
        if action == rename_action:
            self._rename_theme(item)
        elif action == delete_action:
            self._delete_theme(item)

    def _rename_theme(self, item: QListWidgetItem):
        old_name = item.text()
        new_name, ok = QInputDialog.getText(self, "Rename Tag", "New name:", text=old_name)
        if not ok or not new_name.strip() or new_name.strip() == old_name:
            return
        new_name = new_name.strip()
        session  = SessionLocal()
        try:
            if ThemeService.rename_theme(session, old_name, new_name):
                item.setText(new_name)
            else:
                QMessageBox.warning(self, "Rename Failed",
                    f"Could not rename '{old_name}' — name may already exist.")
        except Exception as e:
            logger.error(f"Error renaming theme '{old_name}': {e}")
        finally:
            session.close()

    def _delete_theme(self, item: QListWidgetItem):
        name  = item.text()
        reply = QMessageBox.warning(
            self, "Delete Tag",
            f"Delete tag \"{name}\"?\n\nIt will be removed from all stamps.",
            QMessageBox.StandardButton.Ok | QMessageBox.StandardButton.Cancel,
        )
        if reply != QMessageBox.StandardButton.Ok:
            return
        session = SessionLocal()
        try:
            theme = session.query(Theme).filter_by(name=name).one_or_none()
            if theme:
                ThemeService.delete_theme(session, theme.id)
                self._list.takeItem(self._list.row(item))
            else:
                QMessageBox.warning(self, "Delete Failed", f"Tag \"{name}\" not found.")
        except Exception as e:
            logger.error(f"Error deleting theme '{name}': {e}")
        finally:
            session.close()

    # ------------------------------------------------------------------
    # Dialog launchers (lazy-import from v3 until migrated)
    # ------------------------------------------------------------------

    def _open_alias_dialog(self):
        try:
            from stamp_identifier_v3 import TagAliasDialog
            TagAliasDialog(self).exec()
        except Exception as e:
            logger.warning(f"TagAliasDialog unavailable: {e}")

    def _open_implications_dialog(self):
        try:
            from stamp_identifier_v3 import ThemeImplicationsDialog
            ThemeImplicationsDialog(self).exec()
        except Exception as e:
            logger.warning(f"ThemeImplicationsDialog unavailable: {e}")
