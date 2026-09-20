"""
Pull new photos off a USB-connected iPhone into INCOMING_DIR.

Why this exists
---------------
The phone app's capture button hands off to iOS's native picker, which always
demands a "Use Photo" confirmation — that view controller runs outside the web
page and nothing in the browser can reach it. Photos taken with the stock
Camera app have no such step: one shutter press and the file is saved. So the
laptop pulls from the camera roll instead of the phone pushing, and capture
costs no interaction beyond the shutter itself.

The camera roll is read over Windows Portable Devices (MTP) through the Shell
COM API. No iTunes, no Apple ID, no software on the phone. Files are copied
byte-for-byte — nothing is re-encoded or resized — and land in INCOMING_DIR via
an atomic rename so the History panel's folder watcher
(ui/panels/history.py) only ever sees complete files.

Phone settings that matter
--------------------------
    Settings > Camera > Formats > Most Compatible
        Captures native JPEG. On High Efficiency you get HEIC, which the
        desktop app cannot read; those files are skipped with a warning.
    Settings > Photos > Transfer to Mac or PC > Keep Originals
        Stops Windows transcoding on copy, which would cost a lossy generation.

Usage
-----
    python phone_import.py --list              # show attached devices
    python phone_import.py --once              # one pass, then exit
    python phone_import.py --watch             # poll until interrupted
    python phone_import.py --watch --device 1  # pick a specific device
    python phone_import.py --once --dry-run    # report without copying

Nothing is ever deleted from the phone; clearing the camera roll stays a manual
decision.
"""
import argparse
import json
import os
import shutil
import sys
import tempfile
import time

import autocrop
from config import INCOMING_DIR, PHONE_IMPORT_STATE, AUTOCROP_ON_ARRIVAL
from logger import logger

# Shell special folder for "This PC".
_THIS_PC = 17

# CopyHere flags: no progress dialog, yes-to-all, no new-directory prompt,
# no error UI. Without these a background poll can pop a modal on the desktop.
_COPY_FLAGS = 4 | 16 | 512 | 1024

# Extensions worth importing. The History panel lists only .jpg
# (ui/panels/history.py), so that is what we produce.
_JPEG_EXT = {".jpg", ".jpeg"}
# Recognised but deliberately not imported: HEIC needs Most Compatible turned
# on instead of a lossy server-side transcode, and .aae files are just edit
# sidecars.
_SKIP_EXT = {".heic", ".heif", ".aae", ".mov", ".mp4", ".png", ".dng"}

# How many date buckets to scan. The phone groups the camera roll into folders
# named YYYYMM__, so scanning the newest two covers a month rollover without
# walking the entire roll on every poll.
_BUCKETS_TO_SCAN = 2

# CopyHere is asynchronous, so a copied file is polled until its size stops
# changing before it is published.
_COPY_TIMEOUT_S = 120
_COPY_POLL_S = 0.25


# ----------------------------------------------------------------------
# State — which files have already been pulled
# ----------------------------------------------------------------------

def _load_seen() -> set[str]:
    try:
        with open(PHONE_IMPORT_STATE, "r", encoding="utf-8") as fh:
            return set(json.load(fh).get("seen", []))
    except FileNotFoundError:
        return set()
    except Exception as e:
        logger.warning(f"phone_import: could not read state, starting fresh: {e}")
        return set()


def _save_seen(seen: set[str]) -> None:
    os.makedirs(os.path.dirname(PHONE_IMPORT_STATE) or ".", exist_ok=True)
    tmp = PHONE_IMPORT_STATE + ".part"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump({"seen": sorted(seen)}, fh, indent=1)
    os.replace(tmp, PHONE_IMPORT_STATE)


# ----------------------------------------------------------------------
# Filesystem helpers
# ----------------------------------------------------------------------

def _unique_jpg_path(directory: str, stem: str) -> str:
    """A free path in `directory` for `stem`.jpg, suffixing on collision."""
    candidate = os.path.join(directory, f"{stem}.jpg")
    i = 1
    while os.path.exists(candidate):
        candidate = os.path.join(directory, f"{stem}_{i}.jpg")
        i += 1
    return candidate


def _wait_for_stable(path: str, timeout: float = _COPY_TIMEOUT_S) -> bool:
    """Block until `path` exists and has stopped growing.

    Shell.CopyHere returns immediately and copies in the background, so the
    file has to be watched rather than assumed complete.
    """
    deadline = time.monotonic() + timeout
    last_size = -1
    stable_for = 0
    while time.monotonic() < deadline:
        try:
            size = os.path.getsize(path)
        except OSError:
            time.sleep(_COPY_POLL_S)
            continue
        if size > 0 and size == last_size:
            stable_for += 1
            if stable_for >= 2:      # two consecutive identical reads
                return True
        else:
            stable_for = 0
        last_size = size
        time.sleep(_COPY_POLL_S)
    return False


# ----------------------------------------------------------------------
# Device access
# ----------------------------------------------------------------------

def _shell():
    import win32com.client
    return win32com.client.Dispatch("Shell.Application")


def list_devices() -> list[str]:
    """Names of the portable devices currently attached."""
    try:
        items = _shell().NameSpace(_THIS_PC).Items()
    except Exception as e:
        logger.error(f"phone_import: cannot reach the Shell namespace: {e}")
        return []
    names = []
    for item in items:
        # Portable devices have no drive letter; that is what separates an
        # iPhone from "Windows (C:)" or a USB stick.
        if item.IsFolder and ":" not in item.Name:
            names.append(item.Name)
    return names


def _find_device(index: int | None, match: str | None):
    """Pick a portable device, by index or by name substring."""
    items = [i for i in _shell().NameSpace(_THIS_PC).Items()
             if i.IsFolder and ":" not in i.Name]
    if not items:
        return None
    if index is not None:
        return items[index] if 0 <= index < len(items) else None
    if match:
        for i in items:
            if match.lower() in i.Name.lower():
                return i
        return None
    return items[0]


def _camera_buckets(device):
    """The newest date-bucket folders on the device, newest first.

    Modern iOS groups the camera roll into YYYYMM__ folders rather than the old
    100APPLE scheme, so sorting the names descending finds the current month
    without reading any file metadata.
    """
    storage = None
    for item in device.GetFolder.Items():
        if item.IsFolder:
            if item.Name.lower().startswith("internal"):
                storage = item
                break
            storage = storage or item
    if storage is None:
        return []

    # The buckets sit under DCIM, not at the storage root — the root holds only
    # DCIM itself, which contains no files, so scanning it finds nothing.
    # Descend when DCIM is present and fall back to the root otherwise, since
    # not every device nests the same way.
    root = storage
    for item in storage.GetFolder.Items():
        if item.IsFolder and item.Name.upper() == "DCIM":
            root = item
            break

    folders = [i for i in root.GetFolder.Items() if i.IsFolder]
    folders.sort(key=lambda f: f.Name, reverse=True)
    return folders[:_BUCKETS_TO_SCAN]


# ----------------------------------------------------------------------
# Import
# ----------------------------------------------------------------------

def import_once(index: int | None = None, match: str | None = None,
                dry_run: bool = False, baseline: bool = False) -> dict:
    """One pass over the newest camera-roll buckets.

    With `baseline` set, everything currently on the phone is recorded as
    already-seen and nothing is copied — so a first run against a phone that
    already holds photos does not dump the lot into INCOMING_DIR. Only pictures
    taken afterwards get imported.

    Returns {'copied': int, 'skipped': int, 'device': str|None}.
    """
    result = {"copied": 0, "skipped": 0, "device": None}

    device = _find_device(index, match)
    if device is None:
        logger.info("phone_import: no portable device attached")
        return result
    result["device"] = device.Name

    seen = _load_seen()
    newly_seen: set[str] = set()
    os.makedirs(INCOMING_DIR, exist_ok=True)

    for bucket in _camera_buckets(device):
        folder = bucket.GetFolder
        for item in folder.Items():
            if item.IsFolder:
                continue
            key = f"{bucket.Name}/{item.Name}"
            if key in seen:
                continue

            if baseline:
                newly_seen.add(key)
                result["skipped"] += 1
                continue

            ext = os.path.splitext(item.Name)[1].lower()
            if ext in _SKIP_EXT:
                if ext in (".heic", ".heif"):
                    logger.warning(
                        f"phone_import: skipping {item.Name} — HEIC. Set the phone to "
                        f"Settings > Camera > Formats > Most Compatible."
                    )
                newly_seen.add(key)     # don't re-report it every poll
                result["skipped"] += 1
                continue
            if ext not in _JPEG_EXT:
                newly_seen.add(key)
                result["skipped"] += 1
                continue

            if dry_run:
                logger.info(f"phone_import: would copy {key}")
                result["copied"] += 1
                continue

            if _copy_one(folder, item, key):
                newly_seen.add(key)
                result["copied"] += 1
            else:
                result["skipped"] += 1

    if newly_seen and not dry_run:
        _save_seen(seen | newly_seen)
    return result


def _copy_one(folder, item, key: str) -> bool:
    """Copy one photo off the device and publish it into INCOMING_DIR."""
    staging = tempfile.mkdtemp(prefix="stamp_phone_")
    try:
        _shell().NameSpace(staging).CopyHere(item, _COPY_FLAGS)
        staged = os.path.join(staging, item.Name)
        if not _wait_for_stable(staged):
            logger.error(f"phone_import: timed out copying {key}")
            return False

        stem = f"phone_{os.path.splitext(item.Name)[0]}"
        dest = _unique_jpg_path(INCOMING_DIR, stem)
        # Rename inside the staging dir first so the move into INCOMING_DIR is a
        # same-volume atomic replace where possible; the History watcher must
        # never observe a partial file.
        shutil.move(staged, dest)
        # Auto-crop after the publish, not before it. Cropping in the staging
        # directory would put the snapshot in the wrong bucket (it is keyed by
        # the folder the image lives in), and the rewrite here is a modify of a
        # file that already exists rather than a partial one appearing — which
        # is the thing the atomic move above is protecting the watcher from.
        if AUTOCROP_ON_ARRIVAL:
            autocrop.apply_to(dest)
        logger.info(f"phone_import: imported {key} -> {os.path.basename(dest)}")
        return True
    except Exception as e:
        logger.error(f"phone_import: failed on {key}: {e}")
        return False
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def watch(interval: float = 5.0, index: int | None = None,
          match: str | None = None) -> None:
    """Poll until interrupted. Survives the phone being unplugged."""
    logger.info(f"phone_import: watching every {interval}s — Ctrl+C to stop")
    while True:
        try:
            r = import_once(index=index, match=match)
            if r["copied"]:
                logger.info(f"phone_import: {r['copied']} new photo(s) from {r['device']}")
        except KeyboardInterrupt:
            raise
        except Exception as e:
            # A disconnect mid-scan raises from COM; keep polling so replugging
            # the phone resumes on its own.
            logger.warning(f"phone_import: poll failed ({e}) — retrying")
        try:
            time.sleep(interval)
        except KeyboardInterrupt:
            break


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Import photos from a USB-connected iPhone.")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--list", action="store_true", help="list attached portable devices")
    g.add_argument("--once", action="store_true", help="run a single import pass")
    g.add_argument("--watch", action="store_true", help="poll continuously")
    g.add_argument("--baseline", action="store_true",
                   help="mark everything already on the phone as seen and copy nothing; "
                        "run this once first so only new photos get imported")
    p.add_argument("--device", type=int, default=None,
                   help="device index from --list (use when two phones are attached)")
    p.add_argument("--name", default=None, help="match a device by name substring")
    p.add_argument("--interval", type=float, default=5.0, help="poll seconds (default 5)")
    p.add_argument("--dry-run", action="store_true", help="report without copying")
    args = p.parse_args(argv)

    try:
        import pythoncom
        pythoncom.CoInitialize()
    except Exception:
        pass   # already initialised, or running somewhere it is not needed

    if args.list:
        names = list_devices()
        if not names:
            print("No portable devices found. Plug the phone in, unlock it, and tap Trust.")
            return 1
        print("Attached portable devices:")
        for i, n in enumerate(names):
            print(f"  [{i}] {n}")
        print("\nIf more than one phone is listed, pass --device N to choose.")
        return 0

    if args.watch:
        watch(interval=args.interval, index=args.device, match=args.name)
        return 0

    r = import_once(index=args.device, match=args.name,
                    dry_run=args.dry_run, baseline=args.baseline)
    if r["device"] is None:
        print("No portable device attached.")
        return 1
    if args.baseline:
        print(f"{r['device']}: marked {r['skipped']} existing photo(s) as seen. "
              f"Only new pictures will import from now on.")
        return 0
    verb = "would copy" if args.dry_run else "copied"
    print(f"{r['device']}: {verb} {r['copied']}, skipped {r['skipped']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
