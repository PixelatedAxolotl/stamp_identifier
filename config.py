"""
Configuration vars
Loads settings from environment variables and sets defaults for camera + UI.
"""
import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

# ============ DATABASE ============
DATABASE_URL = os.getenv("STAMP_DB_URL")

# ============ DATABASE BACKUPS ============
# On launch the app writes a compressed pg_dump (custom format) snapshot of the
# database into BACKUP_DIR. See db/backup.py; restore with restore_db.py.
BACKUP_DIR = os.getenv("STAMP_BACKUP_DIR", "backups")
os.makedirs(BACKUP_DIR, exist_ok=True)

# How many of the most recent backups to keep. Older ones are pruned after each
# successful backup so on-launch dumps don't grow without bound.
BACKUP_KEEP = int(os.getenv("STAMP_BACKUP_KEEP", "20"))

# Skip the on-launch backup if the newest existing backup is younger than this
# many minutes. Stops rapid restarts (e.g. during development) from producing a
# flood of near-identical dumps. Set to 0 to back up on every launch.
BACKUP_MIN_INTERVAL_MIN = int(os.getenv("STAMP_BACKUP_MIN_INTERVAL_MIN", "60"))

# Path to the pg_dump / pg_restore executables. If unset search the standard
# Windows PostgreSQL install location, then fall back to bare names on PATH.
def _find_pg_bin(exe):
    override = os.getenv("STAMP_PG_BIN")
    if override:
        cand = Path(override) / f"{exe}.exe"
        if cand.exists():
            return str(cand)
    for base in sorted(Path(r"C:\Program Files\PostgreSQL").glob("*/bin"), reverse=True):
        cand = base / f"{exe}.exe"
        if cand.exists():
            return str(cand)
    return exe  # rely on PATH

PG_DUMP = _find_pg_bin("pg_dump")
PG_RESTORE = _find_pg_bin("pg_restore")

# ============ IMAGE STORAGE ============
# Two folders under storage/:
#   IMAGE_DIR    — images that are associated with a stamp in the database.
#   INCOMING_DIR — freshly captured images that are not yet associated.
# A capture lands in INCOMING_DIR; once it is attached to a stamp it is moved
# into IMAGE_DIR (see storage.associate_image). Keeping associated images in
# IMAGE_DIR preserves the paths stored in the DB and the phone API's basename
# lookups (see stamp_phone/api.py).
IMAGE_DIR = os.getenv("STAMP_IMAGE_DIR", "storage/images")
os.makedirs(IMAGE_DIR, exist_ok=True)

INCOMING_DIR = os.getenv("STAMP_INCOMING_DIR", "storage/incoming")
os.makedirs(INCOMING_DIR, exist_ok=True)

# ============ LAYOUT PERSISTENCE ============
LAYOUT_FILE = os.getenv("STAMP_LAYOUT_FILE", "storage/layout.json")

# ============ STAMP ENTRY DEFAULTS ============
# Values pre-filled into the Fields form when a new stamp is started (History
# click / fresh capture) — see ui/field_defaults.py. Holds the per-field values,
# their individual on/off flags, and the master toggle.
DEFAULTS_FILE = os.getenv("STAMP_DEFAULTS_FILE", "storage/stamp_defaults.json")

# ============ UI SKIN ============
# Which visual skin the app renders with. Read once at startup (see backend.py
# and ui/theme.py) — this is a launch-time switch, not a live toggle:
#   "texture" — the paint-driven artwork look (book/torn-paper/tab textures).
#               Custom paintEvent methods draw the images; QSS is off.
#   "qss"     — a stylesheet-driven look. The texture paintEvents no-op and a
#               global Qt Style Sheet (ui/skins.py) drives every color/border.
# Override with STAMP_SKIN=qss (or =texture) to flip between them per launch.
SKIN = os.getenv("STAMP_SKIN", "texture").strip().lower()

# ============ CAMERA ============
# Camera index (0 = default, 1 = secondary, etc.)
# Set via STAMP_CAMERA_INDEX env var to override
CAMERA_INDEX = int(os.getenv("STAMP_CAMERA_INDEX", "1"))

# ============ THUMBNAIL DISPLAY ============
THUMB_SIZE = (120, 120)
SIDEBAR_WIDTH = THUMB_SIZE[0] + 20

# ============ COLNECT SETTINGS ============
COLNECT_USERNAME = os.getenv("COLNECT_USERNAME", "")
COLNECT_PASSWORD = os.getenv("COLNECT_PASSWORD", "")
COLNECT_HEADLESS = os.getenv("COLNECT_HEADLESS", "false").lower() == "true"

if not COLNECT_USERNAME or not COLNECT_PASSWORD:
    print("WARNING: Colnect credentials not set. Set COLNECT_USERNAME and COLNECT_PASSWORD environment variables.")

# ============ GOOGLE LENS SETTINGS ============
LENS_HEADLESS = os.getenv("LENS_HEADLESS", "false").lower() == "true"
LENS_ALLOWED_DOMAINS = [
    d.strip()
    for d in os.getenv("LENS_ALLOWED_DOMAINS", "colnect,mysticstamp,hipstamp").split(",")
    if d.strip()
]

# ============ UI SETTINGS ============
MIN_WINDOW_WIDTH = 800
MIN_WINDOW_HEIGHT = 800
DEFAULT_WINDOW_WIDTH = 1100
DEFAULT_WINDOW_HEIGHT = 800

# ============ UI PANEL SIZES ============
# Minimum widths for each resizable panel
PANEL_GALLERY_MIN_W  = 0     # gallery panel (0 = fully collapsible)
PANEL_DB_MIN_W       = 100   # database / country browser sidebar
PANEL_DB_MAX_W       = 400   # database sidebar maximum
PANEL_CONTENT_MIN_W  = 350   # main stamp info + search area
PANEL_THEMES_MIN_W   = 100   # themes list (left of stamp fields)
PANEL_FIELDS_INIT_W  = 580   # stamp info fields initial width
FIELD_INPUT_HEIGHT   = 22    # height of each text input row in the stamp fields
PANEL_HISTORY_MIN_W  = 80    # capture history sidebar (right edge)

# Initial splitter positions in pixels (can be dragged freely at runtime)
SPLITTER_MAIN_INIT       = [0, 100, 900]    # gallery | db | content
SPLITTER_INFO_H_INIT     = [PANEL_THEMES_MIN_W, PANEL_FIELDS_INIT_W]  # themes | stamp fields
SPLITTER_CONTENT_V_INIT  = [400, 280, 150]  # results | info rows | copies
SPLITTER_CONTENT_H_INIT  = [800, 120]       # main stack | history sidebar

# ============ CAMERA PREVIEW ============
CAMERA_FPS = 30  # ~33ms per frame

# ============ LOGGING ============
LOG_LEVEL = os.getenv("STAMP_LOG_LEVEL", "INFO")

# ============ COUNTRY OVERRIDES FILE ============
# Path to a JSON file that can contain custom abbreviation -> full name mappings.
from pathlib import Path
COUNTRY_OVERRIDES_FILE = os.getenv(
    "COUNTRY_OVERRIDES_FILE",
    str(Path(__file__).parent / "country_overrides.json")
)

# ============ TAG ALIASES FILE ============
# Path to a JSON file mapping Colnect tag names to local tag names.
TAG_ALIASES_FILE = os.getenv(
    "TAG_ALIASES_FILE",
    str(Path(__file__).parent / "tag_aliases.json")
)

# ============ THEME IMPLICATIONS FILE ============
# Path to a JSON file mapping a trigger theme to a list of auto-selected themes.
# Example content: { "Generals": ["Military", "Military Officers"] }
THEME_IMPLICATIONS_FILE = os.getenv(
    "THEME_IMPLICATIONS_FILE",
    str(Path(__file__).parent / "theme_implications.json")
)
