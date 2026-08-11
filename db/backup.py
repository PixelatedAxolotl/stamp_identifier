"""
Database backup helpers.

Creates compressed pg_dump (custom-format, -Fc) snapshots of the PostgreSQL
database into config.BACKUP_DIR. Custom format is used so pg_restore can restore
either the whole database or individual tables (see restore_db.py).

`backup_on_launch()` is called from the GUI entry points (backend.py,
stamp_identifier_v3.py). It is deliberately defensive: it throttles frequent
launches, prunes old backups, and never raises — a backup failure must not stop
the app from starting. Every failure is logged instead.
"""
import os
import subprocess
from datetime import datetime
from pathlib import Path

from sqlalchemy.engine import make_url

from config import (
    DATABASE_URL,
    BACKUP_DIR,
    BACKUP_KEEP,
    BACKUP_MIN_INTERVAL_MIN,
    PG_DUMP,
)
from logger import logger

_STAMP_FMT = "%Y-%m-%d_%H-%M-%S"
_PREFIX = "stamps_"
_SUFFIX = ".dump"

# Suppress the console window pg_dump would otherwise flash when the app is
# launched from a GUI (pythonw) on Windows.
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _conn(url):
    """Return (pg_dump connection args, env with PGPASSWORD) from a DB URL."""
    args = []
    if url.host:
        args += ["-h", url.host]
    if url.port:
        args += ["-p", str(url.port)]
    if url.username:
        args += ["-U", url.username]
    args += ["-d", url.database]

    env = os.environ.copy()
    if url.password:
        env["PGPASSWORD"] = url.password
    return args, env


def list_backups():
    """All existing backup files, newest first."""
    d = Path(BACKUP_DIR)
    if not d.exists():
        return []
    files = [p for p in d.glob(f"{_PREFIX}*{_SUFFIX}") if p.is_file()]
    return sorted(files, key=lambda p: p.stat().st_mtime, reverse=True)


def _prune(keep):
    """Delete all but the `keep` most recent backups."""
    for old in list_backups()[keep:]:
        try:
            old.unlink()
            logger.info(f"Pruned old backup: {old.name}")
        except OSError as e:
            logger.warning(f"Could not prune backup {old.name}: {e}")


def run_backup():
    """Create one pg_dump snapshot. Returns the Path on success, else None."""
    if not DATABASE_URL:
        logger.warning("Backup skipped: DATABASE_URL is not set.")
        return None

    url = make_url(DATABASE_URL)
    if not url.database:
        logger.warning("Backup skipped: no database name in DATABASE_URL.")
        return None

    Path(BACKUP_DIR).mkdir(parents=True, exist_ok=True)
    outfile = Path(BACKUP_DIR) / f"{_PREFIX}{datetime.now().strftime(_STAMP_FMT)}{_SUFFIX}"

    conn_args, env = _conn(url)
    cmd = [PG_DUMP, "-Fc", *conn_args, "-f", str(outfile)]

    logger.info(f"Backing up database '{url.database}' -> {outfile.name}")
    try:
        result = subprocess.run(
            cmd,
            env=env,
            capture_output=True,
            text=True,
            timeout=600,
            creationflags=_NO_WINDOW,
        )
    except FileNotFoundError:
        logger.error(
            f"Backup failed: pg_dump not found at '{PG_DUMP}'. "
            "Set STAMP_PG_BIN to your PostgreSQL bin directory."
        )
        return None
    except subprocess.TimeoutExpired:
        logger.error("Backup failed: pg_dump timed out.")
        _cleanup_partial(outfile)
        return None

    if result.returncode != 0:
        logger.error(f"Backup failed (pg_dump exit {result.returncode}): {result.stderr.strip()}")
        _cleanup_partial(outfile)
        return None

    size_kb = outfile.stat().st_size / 1024 if outfile.exists() else 0
    logger.info(f"Backup complete: {outfile.name} ({size_kb:.0f} KB)")
    _prune(BACKUP_KEEP)
    return outfile


def _cleanup_partial(outfile):
    """Remove a truncated dump left behind by a failed pg_dump run."""
    try:
        if outfile.exists():
            outfile.unlink()
    except OSError:
        pass


def backup_on_launch():
    """
    Create a backup at app startup, unless a recent one already exists.

    Never raises: any failure is logged so it cannot block app startup.
    """
    try:
        if BACKUP_MIN_INTERVAL_MIN > 0:
            existing = list_backups()
            if existing:
                age_min = (datetime.now().timestamp() - existing[0].stat().st_mtime) / 60
                if age_min < BACKUP_MIN_INTERVAL_MIN:
                    logger.info(
                        f"Skipping launch backup: newest is {age_min:.0f} min old "
                        f"(< {BACKUP_MIN_INTERVAL_MIN} min threshold)."
                    )
                    return None
        return run_backup()
    except Exception as e:  # pragma: no cover - defensive; startup must not fail
        logger.error(f"Launch backup errored (ignored): {e}")
        return None
