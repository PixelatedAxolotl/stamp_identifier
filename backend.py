# backend.py
#
# Entry point for the new panel-based UI.
# The original app remains runnable via:  python stamp_identifier_v3.py

import sys
import faulthandler

from PySide6.QtWidgets import QApplication
from PySide6.QtGui import QFont

from ui.canvas import Canvas
from ui.skins import apply_skin
from ui.theme import APP_FONT_FILE, APP_FONT_SIZE, load_font_family
from db.backup import backup_on_launch


if __name__ == "__main__":
    faulthandler.enable()

    # Snapshot the database before the UI opens, so there is always a recent
    # pre-session backup to revert to. Non-blocking failures are logged.
    backup_on_launch()

    app = QApplication(sys.argv)

    if APP_FONT_FILE:
        family = load_font_family(APP_FONT_FILE)
        if family:
            app.setFont(QFont(family, APP_FONT_SIZE))

    # Install the QSS skin when SKIN == "qss" (no-op for the texture skin).
    # Launch-time switch — set STAMP_SKIN=qss to flip. See config.SKIN.
    apply_skin(app)

    canvas = Canvas()
    canvas.load_default_layout()
    canvas.show()

    sys.exit(app.exec())
