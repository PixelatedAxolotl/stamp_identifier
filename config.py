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

# ============ IMAGE STORAGE ============
IMAGE_DIR = os.getenv("STAMP_IMAGE_DIR", "storage/images")
os.makedirs(IMAGE_DIR, exist_ok=True)

# ============ CAMERA ============
# Camera index (0 = default, 1 = secondary, etc.)
# Set via STAMP_CAMERA_INDEX env var to override
CAMERA_INDEX = int(os.getenv("STAMP_CAMERA_INDEX", "0"))

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

# ============ CAMERA PREVIEW ============
CAMERA_FPS = 30  # ~33ms per frame

# ============ LOGGING ============
LOG_LEVEL = os.getenv("STAMP_LOG_LEVEL", "INFO")

