# ui/theme.py
#
# Visual constants for Phase 2 placeholder styling, plus texture loading.
# Flat QColor values below are the fallback used whenever a named texture
# hasn't been supplied yet — drop image files into ui/assets/textures/ (or
# ui/assets/, for pre-existing art) to activate them.

import os

from PySide6.QtGui import QColor, QFontDatabase, QPixmap

from config import SKIN

# ---------------------------------------------------------------------------
# Active skin
# ---------------------------------------------------------------------------
# USE_TEXTURES gates every texture-drawing paintEvent and palette background in
# the UI. When False (SKIN == "qss") those all defer to the global Qt Style
# Sheet in ui/skins.py instead. Bound once at import — a launch-time switch, not
# a live toggle (see config.SKIN).
USE_TEXTURES = SKIN != "qss"

# Canvas surface
CANVAS_BG       = QColor("#3c2a1a")

# Panel body
PANEL_BG        = QColor("#0b5542")

# Panel title bar
PANEL_TITLE_BG  = QColor("#7a5c3a")
PANEL_TITLE_FG  = QColor("#ffffff")

# Panel border / resize margin color (same as body for now)
PANEL_BORDER    = QColor("#c4a882")

# Minimize button
MIN_BTN_BG      = QColor("#5a3e20")
MIN_BTN_FG      = QColor("#f0e0c0")

# ---------------------------------------------------------------------------
# Textures
# ---------------------------------------------------------------------------

_ASSETS_DIR = os.path.join(os.path.dirname(__file__), "assets")

# name -> texture filename (searched under ui/assets/)
CANVAS_BG_TEXTURE = "demo_canvas.png"

# Zoom applied to the canvas texture after it's scaled to cover the window.
# 1.0 = fills the window exactly (default cover fit). >1.0 zooms in and crops
# more; <1.0 shrinks it, exposing CANVAS_BG at the edges.
CANVAS_BG_SCALE = 1.0

_texture_cache: dict[str, QPixmap | None] = {}


def get_texture(filename: str) -> QPixmap | None:
    """Load and cache an image from ui/assets/ by filename.

    Returns None if the file is missing so callers can fall back to a flat
    QColor — lets texture files be dropped in later without touching code.
    """
    if filename not in _texture_cache:
        pix = QPixmap(os.path.join(_ASSETS_DIR, filename))
        _texture_cache[filename] = pix if not pix.isNull() else None
    return _texture_cache[filename]


# ---------------------------------------------------------------------------
# 9-slice borders — how many source-image pixels on each edge of a texture
# are treated as fixed-size corners/edges instead of being scaled, so
# torn-edge artwork stays crisp at any panel size (see Panel._draw_nine_slice).
# These are just measurements of the existing texture files above — not
# separate image assets.
# ---------------------------------------------------------------------------

# Used for any texture with no entry in TEXTURE_BORDERS below.
DEFAULT_TEXTURE_BORDER = 32

# filename -> (x0, x1, y0, y1) i.e. (left, right, top, bottom), or a single
# int for a uniform border on all four sides. Add/adjust an entry here
# whenever a texture's torn edge looks stretched or over-cropped.
TEXTURE_BORDERS: dict[str, int | tuple[int, int, int, int]] = {
    "demo_widget.png":   (19, 12, 14, 19),
    "fields_canvas.png": (22, 13, 9, 9),
    "camera_canvas.png": (18, 6, 9, 17),
    "demo_canvas.png":   (13, 12, 15, 14),
}


def get_texture_border(filename: str) -> int | tuple[int, int, int, int]:
    """9-slice border for a texture — see TEXTURE_BORDERS."""
    return TEXTURE_BORDERS.get(filename, DEFAULT_TEXTURE_BORDER)


# ---------------------------------------------------------------------------
# Fonts
# ---------------------------------------------------------------------------

_FONTS_DIR = os.path.join(_ASSETS_DIR, "fonts")

# Drop a .ttf/.otf into ui/assets/fonts/ and set its filename here to
# activate it app-wide. Leave as None to keep the system default font.
APP_FONT_FILE = "CinzelDecorative-Regular.ttf"
APP_FONT_SIZE = 6

_font_family_cache: dict[str, str | None] = {}


def load_font_family(filename: str) -> str | None:
    """Register a font file from ui/assets/fonts/ and return its family name.

    Returns None if the file is missing or invalid so callers can fall back
    to the system default font — same pattern as get_texture().
    """
    if filename not in _font_family_cache:
        font_id = QFontDatabase.addApplicationFont(os.path.join(_FONTS_DIR, filename))
        families = QFontDatabase.applicationFontFamilies(font_id) if font_id != -1 else []
        _font_family_cache[filename] = families[0] if families else None
    return _font_family_cache[filename]
