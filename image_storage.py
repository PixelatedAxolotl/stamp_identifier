"""
Filesystem helpers for stamp images.

Images live in one of two folders under storage/ (see config.py):

    INCOMING_DIR (storage/incoming) — freshly captured, not yet associated
    IMAGE_DIR    (storage/images)   — associated with a stamp in the database

A capture is written to INCOMING_DIR. When the image is attached to a stamp,
associate_image() moves the file into IMAGE_DIR and returns its new path, which
is what gets stored on the StampImage record. deassociate_image() is the inverse
and is used to roll a move back if the save that triggered it fails.
"""
import os
import shutil

from send2trash import send2trash

from config import IMAGE_DIR, INCOMING_DIR
from logger import logger


def _unique_dest(directory: str, filename: str) -> str:
    """Return a path inside `directory` for `filename`, adding a numeric suffix
    if a file of that name already exists (so a move never clobbers)."""
    base, ext = os.path.splitext(filename)
    candidate = os.path.join(directory, filename)
    i = 1
    while os.path.exists(candidate):
        candidate = os.path.join(directory, f"{base}_{i}{ext}")
        i += 1
    return candidate


def _in_dir(path: str, directory: str) -> bool:
    return os.path.normpath(os.path.dirname(path)) == os.path.normpath(directory)


def duplicate_incoming(path: str) -> str:
    """Copy an incoming image alongside itself, returning the new path.

    Used to enter one photo as two separate stamps. shutil.copy is deliberate
    where the rest of this module uses move: copy2 would carry the original's
    mtime across, and the strip sorts by mtime — the duplicate would file itself
    next to its source instead of arriving at the top like any other new image.
    """
    if not path or not os.path.exists(path):
        raise FileNotFoundError(f"Cannot duplicate missing image: {path}")

    os.makedirs(INCOMING_DIR, exist_ok=True)
    dest = _unique_dest(INCOMING_DIR, os.path.basename(path))
    shutil.copy(path, dest)
    logger.info(f"Duplicated incoming image: {path} -> {dest}")
    return dest


def trash_image(path: str) -> None:
    """Send an image to the OS recycle bin.

    Recoverable by design — the caller does not prompt before this, so the
    recycle bin is the undo. send2trash needs a real absolute path; the storage
    directories are configured relative to the working directory, so the path
    arriving here usually is not one.
    """
    if not path or not os.path.exists(path):
        raise FileNotFoundError(f"Cannot delete missing image: {path}")

    send2trash(os.path.abspath(path))
    logger.info(f"Sent image to recycle bin: {path}")


def associate_image(path: str) -> str:
    """Move an incoming image into the associated-images folder.

    Returns the file's new path inside IMAGE_DIR. If the file already lives in
    IMAGE_DIR, or the source no longer exists, the path is returned unchanged.
    Idempotent — safe to call more than once on the same image.
    """
    if not path:
        return path
    if _in_dir(path, IMAGE_DIR):
        return path  # already associated
    if not os.path.exists(path):
        logger.warning(f"associate_image: source missing, leaving path unchanged: {path}")
        return path

    os.makedirs(IMAGE_DIR, exist_ok=True)
    dest = _unique_dest(IMAGE_DIR, os.path.basename(path))
    shutil.move(path, dest)
    logger.info(f"Associated image: {path} -> {dest}")
    return dest


def deassociate_image(path: str) -> str:
    """Move an associated image back into the incoming folder.

    Inverse of associate_image(); used to revert a move when the save that
    triggered the association fails. Returns the file's new incoming path.
    """
    if not path:
        return path
    if _in_dir(path, INCOMING_DIR):
        return path
    if not os.path.exists(path):
        logger.warning(f"deassociate_image: source missing, leaving path unchanged: {path}")
        return path

    os.makedirs(INCOMING_DIR, exist_ok=True)
    dest = _unique_dest(INCOMING_DIR, os.path.basename(path))
    shutil.move(path, dest)
    logger.info(f"De-associated image (reverted): {path} -> {dest}")
    return dest
