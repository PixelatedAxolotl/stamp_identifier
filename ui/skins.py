# ui/skins.py
#
# The QSS ("qss") skin — a global Qt Style Sheet applied to the whole app when
# config.SKIN == "qss". It is the texture-free counterpart to the paint-driven
# artwork skin: every background, border and color below is drawn by Qt from
# these rules instead of from image files (see ui/theme.USE_TEXTURES and the
# gated paintEvents in ui/panel.py + ui/canvas.py).
#
# Design brief: cozy depth with a subtle paranormal/supernatural glow — a dark
# candle-lit room seen through a veil. Deep indigo-black surfaces, warm ember
# undertones for the "cozy", and spectral violet/cyan accents for the "haunted".
#
# On "glows": true soft blur is NOT expressible in QSS — it needs a
# QGraphicsDropShadowEffect in code. Everything here fakes the glow with bright,
# saturated accent borders and layered gradients. If you want real bloom later,
# attach a drop-shadow effect to panels/inputs; the palette below is chosen to
# pair with a violet (#8a6cff) shadow.

from ui.theme import USE_TEXTURES

# --- palette -----------------------------------------------------------------
# Kept as names here so the sheet reads intentionally; edit in one place.
_BASE_DEEP    = "#0c0913"   # window void, deepest indigo-black
_BASE_MID     = "#141021"   # secondary void
_PANEL_TOP    = "#241c38"   # panel body, lit top
_PANEL_BOT    = "#181123"   # panel body, shadowed bottom
_TITLE_TOP    = "#332748"   # title band, lit
_TITLE_BOT    = "#241a38"   # title band, shadowed
_INPUT_BG     = "#120d1e"   # inset field well
_BORDER       = "#3a2d55"   # resting edges
_BORDER_SOFT  = "#2a2140"   # faint dividers
_GLOW_VIOLET  = "#8a6cff"   # spectral violet — focus/hover "glow"
_GLOW_CYAN    = "#74e0c9"   # ectoplasm cyan — secondary accent
_EMBER        = "#e6a15c"   # warm candle glow — cozy accent
_TEXT         = "#ddd3ec"   # soft lavender-white body text
_TEXT_DIM     = "#8f84a8"   # muted lavender-grey
_TITLE_TEXT   = "#cbb6ff"   # spectral violet-white for titles
_SELECT_BG    = "#5a3fa6"   # selection wash


SUPERNATURAL_QSS = f"""
/* ---- window surface: a dark room with a faint warm heart ---------------- */
#canvas {{
    background: qradialgradient(cx:0.5, cy:0.42, radius:0.9,
                                fx:0.5, fy:0.4,
                                stop:0    #1c1530,
                                stop:0.45 #130e21,
                                stop:1    {_BASE_DEEP});
}}

/* ---- panels: lifted cards with a soft violet rim -------------------------*/
Panel {{
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                                stop:0 {_PANEL_TOP}, stop:1 {_PANEL_BOT});
    border: 1px solid {_BORDER};
    border-radius: 11px;
}}

/* ---- title band: brighter, with a glowing underline ----------------------*/
#panelTitleBar {{
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                               stop:0 {_TITLE_TOP}, stop:1 {_TITLE_BOT});
    border-top-left-radius: 11px;
    border-top-right-radius: 11px;
    border-bottom: 1px solid {_GLOW_VIOLET};
}}
#panelTitleLabel {{
    color: {_TITLE_TEXT};
    font-weight: bold;
    letter-spacing: 1px;
    padding-left: 2px;
}}

/* ---- default text ------------------------------------------------------- */
QLabel {{
    color: {_TEXT};
    background: transparent;
}}
QCheckBox {{
    color: {_TEXT};
    background: transparent;
    spacing: 6px;
}}
QCheckBox::indicator {{
    width: 14px; height: 14px;
    border: 1px solid {_BORDER};
    border-radius: 3px;
    background: {_INPUT_BG};
}}
QCheckBox::indicator:hover {{ border-color: {_GLOW_VIOLET}; }}
QCheckBox::indicator:checked {{
    background: qradialgradient(cx:0.5, cy:0.5, radius:0.7,
                                stop:0 {_GLOW_CYAN}, stop:1 {_SELECT_BG});
    border-color: {_GLOW_CYAN};
}}

/* ---- buttons: dim runes that flare on hover ----------------------------- */
QPushButton {{
    color: {_TEXT};
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                               stop:0 #2b2140, stop:1 #201830);
    border: 1px solid {_BORDER};
    border-radius: 6px;
    padding: 4px 12px;
}}
QPushButton:hover {{
    color: #f1eaff;
    border: 1px solid {_GLOW_VIOLET};
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                               stop:0 #362a50, stop:1 #271d3a);
}}
QPushButton:pressed {{
    background: #191228;
    border: 1px solid {_GLOW_CYAN};
}}
QPushButton:disabled {{
    color: {_TEXT_DIM};
    border-color: {_BORDER_SOFT};
    background: #1a1428;
}}

/* small frameless-window + minimise glyph buttons */
#winBtn, #panelMinBtn {{
    padding: 0px;
    border-radius: 5px;
    color: {_TITLE_TEXT};
    background: rgba(20, 14, 32, 0.6);
    border: 1px solid {_BORDER_SOFT};
}}
#winBtn:hover, #panelMinBtn:hover {{
    color: #fff;
    border: 1px solid {_GLOW_VIOLET};
    background: rgba(58, 40, 92, 0.8);
}}

/* ---- text inputs: inset wells that glow when focused -------------------- */
QLineEdit, QPlainTextEdit, QTextEdit, QSpinBox, QComboBox {{
    color: {_TEXT};
    background: {_INPUT_BG};
    border: 1px solid {_BORDER};
    border-radius: 5px;
    padding: 2px 6px;
    selection-background-color: {_SELECT_BG};
    selection-color: #fff;
}}
QLineEdit:focus, QPlainTextEdit:focus, QTextEdit:focus,
QSpinBox:focus, QComboBox:focus {{
    border: 1px solid {_GLOW_VIOLET};
}}
QComboBox::drop-down {{
    border: none;
    width: 18px;
}}
QComboBox QAbstractItemView {{
    background: {_BASE_MID};
    color: {_TEXT};
    border: 1px solid {_GLOW_VIOLET};
    selection-background-color: {_SELECT_BG};
    outline: none;
}}

/* Spin boxes hide their native up/down buttons (NoButtons, set in fields.py)
   because those glyphs render as faint dots under this skin; a pair of plain
   +1 / -1 QPushButtons drives them instead and inherits the button style
   above. */

/* ---- lists: entries that light up under the cursor --------------------- */
QListWidget, QTreeWidget, QTableWidget {{
    background: transparent;
    border: 1px solid {_BORDER_SOFT};
    border-radius: 6px;
    color: {_TEXT};
    outline: none;
}}
QListWidget::item, QTreeWidget::item {{
    padding: 3px 4px;
    /* transparent 1px frame reserved on every row so the selected row's real
       border below doesn't nudge the text sideways */
    border: 1px solid transparent;
}}
QListWidget::item:hover, QTreeWidget::item:hover {{
    background: rgba(116, 224, 201, 0.10);
}}
QListWidget::item:selected, QTreeWidget::item:selected {{
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                               stop:0 rgba(138,108,255,0.45),
                               stop:1 rgba(90,63,166,0.45));
    /* squared corners with a thin brighter frame so the fill's edge reads as
       an intentional band rather than a hard rounded cutoff */
    border: 1px solid rgba(138,108,255,0.65);
    color: #fff;
}}

/* ---- gallery cards: lifted relic tiles with a bevelled, glowing rim ----- */
/* The top-lit gradient plus a bright top/left edge and dark bottom/right edge
   fake a subtle 3D bevel; the real soft violet bloom is a drop-shadow effect
   attached in gallery.py (QSS can't blur). Hovering flares the whole rim. */
#galleryCard {{
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                               stop:0 #2b2141, stop:1 #17101f);
    border-radius: 8px;
    border-top:    1px solid #4c3b72;
    border-left:   1px solid #40315f;
    border-right:  1px solid #150f21;
    border-bottom: 1px solid #0d0914;
}}
#galleryCard:hover {{
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                               stop:0 #33274d, stop:1 #1d1429);
    border: 1px solid {_GLOW_VIOLET};
}}

/* ---- lens result cards: the same relic tile, as a clickable tool button - */
#resultCard {{
    color: {_TEXT};
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                               stop:0 #2b2141, stop:1 #17101f);
    border-radius: 8px;
    border-top:    1px solid #4c3b72;
    border-left:   1px solid #40315f;
    border-right:  1px solid #150f21;
    border-bottom: 1px solid #0d0914;
    padding: 6px;
}}
#resultCard:hover {{
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                               stop:0 #33274d, stop:1 #1d1429);
    border: 1px solid {_GLOW_VIOLET};
}}
#resultCard:pressed {{
    background: #191228;
    border: 1px solid {_GLOW_CYAN};
}}

/* ---- scrollbars (mostly hidden, styled for the few that show) ---------- */
QScrollBar:vertical {{
    background: transparent; width: 10px; margin: 2px;
}}
QScrollBar::handle:vertical {{
    background: {_BORDER}; border-radius: 5px; min-height: 24px;
}}
QScrollBar::handle:vertical:hover {{ background: {_GLOW_VIOLET}; }}
QScrollBar:horizontal {{
    background: transparent; height: 10px; margin: 2px;
}}
QScrollBar::handle:horizontal {{
    background: {_BORDER}; border-radius: 5px; min-width: 24px;
}}
QScrollBar::handle:horizontal:hover {{ background: {_GLOW_VIOLET}; }}
QScrollBar::add-line, QScrollBar::sub-line {{ width: 0; height: 0; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}

/* ---- popups / dialogs / menus keep the same veil ----------------------- */
QDialog, QMessageBox, QInputDialog {{
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                               stop:0 {_PANEL_TOP}, stop:1 {_BASE_MID});
    color: {_TEXT};
}}
QMenu {{
    background: {_BASE_MID};
    color: {_TEXT};
    border: 1px solid {_GLOW_VIOLET};
    border-radius: 6px;
}}
QMenu::item {{ padding: 4px 18px; border-radius: 4px; }}
QMenu::item:selected {{ background: {_SELECT_BG}; color: #fff; }}
QToolTip {{
    background: #1a1330;
    color: {_GLOW_CYAN};
    border: 1px solid {_GLOW_VIOLET};
    padding: 3px 6px;
}}
"""


def apply_skin(app) -> bool:
    """Install the QSS skin on the application when SKIN == "qss".

    No-op (returns False) in the texture skin so the paint-driven artwork is
    left untouched. Called once at startup from backend.py, after the font is
    set. Returns True when the stylesheet was applied.
    """
    if USE_TEXTURES:
        return False
    app.setStyleSheet(SUPERNATURAL_QSS)
    return True
