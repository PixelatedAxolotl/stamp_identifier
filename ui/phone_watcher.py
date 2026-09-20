# ui/phone_watcher.py
#
# PhoneWatcher — polls a USB-connected iPhone for new camera-roll photos and
# copies them into INCOMING_DIR, where the History panel's existing folder
# watcher picks them up. The Qt-side wrapper around phone_import.import_once().
#
# Why a thread: a poll blocks. Shell.CopyHere returns immediately and copies in
# the background, so every file is watched until its size stops changing
# (up to _COPY_TIMEOUT_S in phone_import). Running that on the GUI thread would
# freeze the window for the duration of each import.
#
# phone_import.py deliberately knows nothing about Qt — it stays runnable as a
# plain CLI. This module is the only place the two meet.
#
# Signals emitted:  status_changed(str)  — human-readable state for the panel

import threading

from PySide6.QtCore import QObject, Signal

import phone_import
from logger import logger

# Matches the CLI's --interval default.
_DEFAULT_INTERVAL_S = 5.0

# How long stop() waits for the poll thread before giving up on it. A poll
# already in progress can be mid-copy, so this is generous enough to let a
# normal one finish rather than orphaning a half-written staging directory.
_STOP_TIMEOUT_S = 5.0


class PhoneWatcher(QObject):
    """Start/stop wrapper around a background phone-import poll loop.

    Note this has the same semantics as `phone_import.py --watch`: it imports
    whatever it has not seen before. On a phone whose state file
    (PHONE_IMPORT_STATE) does not exist yet, the first poll will therefore pull
    everything in the newest camera-roll buckets. Run `--baseline` once first if
    that is not what you want.
    """

    status_changed = Signal(str)

    def __init__(self, interval: float = _DEFAULT_INTERVAL_S, parent=None):
        super().__init__(parent)
        self._interval = interval
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    # ------------------------------------------------------------------
    # Public
    # ------------------------------------------------------------------

    def start(self):
        if self.running:
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, name="phone-watch", daemon=True
        )
        self._thread.start()
        self.status_changed.emit("Looking for phone…")

    def stop(self):
        """Signal the poll loop down and wait for it. Safe to call when idle."""
        if not self.running:
            self.status_changed.emit("Off")
            return
        self._stop.set()
        self._thread.join(timeout=_STOP_TIMEOUT_S)
        if self._thread.is_alive():
            # Daemon thread, so it cannot block process exit; it is just still
            # finishing a copy. Dropping the reference is enough.
            logger.warning("phone_watcher: poll thread did not exit within timeout")
        self._thread = None
        self.status_changed.emit("Off")

    # ------------------------------------------------------------------
    # Worker thread
    # ------------------------------------------------------------------

    def _loop(self):
        # COM apartments are per-thread — the Shell API phone_import uses is
        # unusable here unless it is initialised on this thread specifically.
        com_ready = False
        try:
            import pythoncom
            pythoncom.CoInitialize()
            com_ready = True
        except Exception as e:
            logger.warning(f"phone_watcher: CoInitialize failed: {e}")

        last_status = None
        try:
            while not self._stop.is_set():
                try:
                    result = phone_import.import_once()
                except Exception as e:
                    # A disconnect mid-scan raises from COM. Keep polling so
                    # replugging the phone resumes on its own, exactly as the
                    # CLI's watch() does.
                    logger.warning(f"phone_watcher: poll failed ({e}) — retrying")
                    result = {"copied": 0, "device": None}

                if result["device"] is None:
                    status = "No phone connected"
                elif result["copied"]:
                    status = f"Imported {result['copied']} from {result['device']}"
                else:
                    status = f"Watching {result['device']}"

                # Only publish transitions. Emitting every poll would repaint
                # the label twelve times a minute to say the same thing.
                if status != last_status:
                    last_status = status
                    self.status_changed.emit(status)

                # wait() rather than sleep() so stop() takes effect immediately
                # instead of after the rest of the interval.
                self._stop.wait(self._interval)
        finally:
            if com_ready:
                try:
                    import pythoncom
                    pythoncom.CoUninitialize()
                except Exception:
                    pass
