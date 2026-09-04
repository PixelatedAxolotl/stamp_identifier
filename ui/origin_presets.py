# ui/origin_presets.py
#
# ManagePresetsDialog — rename, edit and delete the saved acquisition origin
# presets (db.models.OriginPreset).
#
# Presets are applied from the dropdown on a copy row's "Origin…" button; see
# FieldsPanel._add_copy_row. Applying one copies its values into that row and
# leaves nothing linked, so everything done here — renaming, editing, deleting —
# only affects future applications. Stamps already saved keep the provenance
# they were saved with.
#
# Deliberately separate from ui/field_defaults.py: the batch defaults pre-fill
# one origin into a new stamp's first copy row behind a global toggle, while
# presets are picked per row, at any time.

from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QListWidget, QListWidgetItem,
    QPushButton, QLabel, QInputDialog, QMessageBox, QDialogButtonBox,
)
from PySide6.QtCore import Qt

from db.session import SessionLocal
from db.service import OriginPresetService
from logger import logger


class ManagePresetsDialog(QDialog):
    """List of saved origin presets with rename / edit / delete."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Origin Presets")
        self.setMinimumSize(460, 360)

        outer = QVBoxLayout(self)

        intro = QLabel(
            "Saved acquisition origins you can apply to any copy row. Changes "
            "here affect future use only — stamps already saved keep the "
            "origin they were saved with."
        )
        intro.setWordWrap(True)
        intro.setStyleSheet("color: gray;")
        outer.addWidget(intro)

        self._list = QListWidget()
        self._list.currentItemChanged.connect(self._on_selection)
        outer.addWidget(self._list, 1)

        self._detail = QLabel("")
        self._detail.setWordWrap(True)
        self._detail.setStyleSheet("color: gray; font-style: italic;")
        outer.addWidget(self._detail)

        row = QHBoxLayout()
        self._rename_btn = QPushButton("Rename…")
        self._rename_btn.clicked.connect(self._rename)
        self._edit_btn = QPushButton("Edit values…")
        self._edit_btn.clicked.connect(self._edit_values)
        self._delete_btn = QPushButton("Delete")
        self._delete_btn.clicked.connect(self._delete)
        for b in (self._rename_btn, self._edit_btn, self._delete_btn):
            b.setEnabled(False)
            row.addWidget(b)
        row.addStretch()
        outer.addLayout(row)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        buttons.accepted.connect(self.accept)
        outer.addWidget(buttons)

        self._load()

    # ------------------------------------------------------------------

    def _load(self, select_id: int | None = None):
        self._list.clear()
        session = SessionLocal()
        try:
            for p in OriginPresetService.get_all(session):
                item = QListWidgetItem(p.name)
                item.setData(Qt.ItemDataRole.UserRole, p.id)
                item.setData(
                    Qt.ItemDataRole.UserRole + 1, OriginPresetService.to_origin(p)
                )
                self._list.addItem(item)
                if p.id == select_id:
                    self._list.setCurrentItem(item)
        except Exception as e:
            logger.error(f"Could not load origin presets: {e}")
        finally:
            session.close()

    def _current(self) -> tuple[int | None, dict]:
        item = self._list.currentItem()
        if item is None:
            return None, {}
        return (
            item.data(Qt.ItemDataRole.UserRole),
            item.data(Qt.ItemDataRole.UserRole + 1) or {},
        )

    def _on_selection(self, current, _prev):
        has = current is not None
        for b in (self._rename_btn, self._edit_btn, self._delete_btn):
            b.setEnabled(has)
        if not has:
            self._detail.clear()
            return
        from ui.panels.fields import origin_summary
        _id, origin = self._current()
        self._detail.setText(origin_summary(origin))

    # ------------------------------------------------------------------

    def _rename(self):
        preset_id, _ = self._current()
        if preset_id is None:
            return
        current_name = self._list.currentItem().text()
        name, ok = QInputDialog.getText(
            self, "Rename Preset", "New name:", text=current_name
        )
        if not ok:
            return
        session = SessionLocal()
        try:
            OriginPresetService.rename(session, preset_id, name)
        except ValueError as e:
            QMessageBox.warning(self, "Rename Failed", str(e))
            return
        except Exception as e:
            logger.error(f"Could not rename origin preset: {e}")
            QMessageBox.warning(self, "Rename Failed", str(e))
            return
        finally:
            session.close()
        self._load(select_id=preset_id)

    def _edit_values(self):
        """Reopen the origin editor on this preset's values and store the result."""
        preset_id, origin = self._current()
        if preset_id is None:
            return
        from ui.panels.fields import OriginDialog
        dlg = OriginDialog(self, origin)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        name = self._list.currentItem().text()
        session = SessionLocal()
        try:
            OriginPresetService.save(session, name, dlg.get_origin())
        except Exception as e:
            logger.error(f"Could not update origin preset: {e}")
            QMessageBox.warning(self, "Save Failed", str(e))
            return
        finally:
            session.close()
        self._load(select_id=preset_id)

    def _delete(self):
        preset_id, _ = self._current()
        if preset_id is None:
            return
        name = self._list.currentItem().text()
        if QMessageBox.question(
            self, "Delete Preset",
            f"Delete the preset '{name}'?\n\n"
            "Stamps already saved with it are not affected.",
        ) != QMessageBox.StandardButton.Yes:
            return
        session = SessionLocal()
        try:
            OriginPresetService.delete(session, preset_id)
        except Exception as e:
            logger.error(f"Could not delete origin preset: {e}")
            QMessageBox.warning(self, "Delete Failed", str(e))
            return
        finally:
            session.close()
        self._load()
