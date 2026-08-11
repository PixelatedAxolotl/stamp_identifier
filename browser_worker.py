"""
AsyncBrowserWorker — launches the Playwright browser worker in a separate
child process (browser_subprocess.py) so that event loop crashes in the
child (access violations from Playwright's Node.js subprocess pipes
interacting with asyncio IOCP) cannot bring down the main Qt application.

Commands from the Qt main thread travel over a multiprocessing.Pipe instead
of a multiprocessing.Queue.  Queue.put() spawns a background _feed thread in
the calling process that holds Python Condition/Lock objects; those objects
are crash victims when heap corruption occurs anywhere in the process.  Pipe
writes are synchronous, happen directly in the calling thread, and leave no
background thread behind.

Results come back via the same mp.Queue result queues the Qt poll timers
already read.  No changes are needed to the existing poll-timer code.
"""
import multiprocessing as mp

from browser_subprocess import browser_process_main
from logger import logger


class AsyncBrowserWorker:

    def __init__(self, lens_result_q: mp.Queue, colnect_result_q: mp.Queue):
        # Pipe(duplex=False) → (readable_end, writable_end)
        # Child reads commands; parent writes commands.
        self._child_cmd_conn, self._parent_cmd_conn = mp.Pipe(duplex=False)
        self._lens_result_q    = lens_result_q
        self._colnect_result_q = colnect_result_q
        self._proc: mp.Process | None = None

    # ------------------------------------------------------------------ lifecycle

    def start(self):
        self._proc = mp.Process(
            target=browser_process_main,
            args=(self._child_cmd_conn, self._lens_result_q, self._colnect_result_q),
            daemon=True,
            name="BrowserWorker",
        )
        self._proc.start()
        # The child now has its own copy of the read end; close the parent's
        # copy so that EOF propagates correctly if the child exits.
        self._child_cmd_conn.close()
        logger.info(f"Browser worker process started (pid {self._proc.pid})")

    def shutdown(self):
        """Signal the child to shut down cleanly via the sentinel value."""
        try:
            self._parent_cmd_conn.send(None)
        except Exception:
            pass

    def join(self, timeout: float | None = None):
        if self._proc and self._proc.is_alive():
            self._proc.join(timeout=timeout)
            if self._proc.is_alive():
                # Subprocess didn't exit cleanly. Kill the entire process tree so
                # Chrome (a grandchild via Node.js) doesn't become an orphan —
                # Windows TerminateProcess only kills the target, not its children.
                logger.warning("Browser worker did not exit cleanly; killing process tree")
                try:
                    import psutil
                    parent = psutil.Process(self._proc.pid)
                    for child in parent.children(recursive=True):
                        try:
                            child.kill()
                        except psutil.NoSuchProcess:
                            pass
                    parent.kill()
                except Exception:
                    self._proc.terminate()

    # ------------------------------------------------------------------ schedule

    def _send(self, cmd: dict):
        """Send a command dict to the child process (synchronous, no background thread)."""
        try:
            self._parent_cmd_conn.send(cmd)
        except Exception as e:
            logger.error(f"Failed to send command to browser worker: {e}")

    def schedule_lens_search(self, image_path: str, request_id: int):
        self._send({"type": "lens_search", "image_path": image_path, "id": request_id})

    def schedule_colnect_search(self, scott_number: str, country: str, request_id: int,
                                method: str = "scott", filters: dict | None = None):
        self._send({"type": "colnect_search", "method": method,
                    "scott_number": scott_number, "country": country,
                    "filters": filters or {}, "id": request_id})

    def schedule_colnect_info(self, request_id: int):
        self._send({"type": "colnect_info", "id": request_id})

    def schedule_open_url(self, url: str):
        self._send({"type": "open_url", "url": url})

    def is_alive(self) -> bool:
        return self._proc is not None and self._proc.is_alive()
