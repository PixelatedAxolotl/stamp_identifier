"""
Playwright browser worker — runs in a separate child process so that event
loop crashes (access violations from Playwright's Node.js subprocess pipes
interacting with asyncio IOCP) cannot bring down the main Qt application.

This file is imported both in the parent (to pickle browser_process_main)
and in the child (to execute it). Keep module-level code to imports only.
"""
import asyncio

from playwright.async_api import async_playwright

from lens_controller import AsyncGoogleLensController
from colnect_controller import AsyncColnectController
from logger import logger


# ------------------------------------------------------------------ task helpers

async def _do_lens(lens: AsyncGoogleLensController, cmd: dict, result_q):
    try:
        results = await lens.search(cmd["image_path"])
        # Compute derived values here so the main process doesn't need a _lens reference.
        result_q.put({
            "id":       cmd["id"],
            "results":  results,
            "scott":    lens.extract_scott_numbers(results),
            "country":  lens.extract_country(results),
            "filtered": lens.filter_results_by_domain(results),
            "error":    None,
        })
    except Exception as e:
        result_q.put({"id": cmd["id"], "results": None, "error": str(e)})


async def _do_colnect_search(colnect: AsyncColnectController, cmd: dict, result_q):
    try:
        if cmd.get("method") == "filter":
            result = await colnect.search_stamp_by_filter(
                cmd["country"], cmd.get("filters") or {})
        else:
            result = await colnect.search_stamp(cmd["scott_number"], cmd["country"])
        result_q.put({"type": "search_done", "id": cmd["id"], "result": result, "error": None})
    except Exception as e:
        result_q.put({"type": "search_done", "id": cmd["id"], "result": None, "error": str(e)})


async def _do_colnect_info(colnect: AsyncColnectController, cmd: dict, result_q):
    try:
        result = await colnect.get_colnect_info()
        result_q.put({"type": "get_info_done", "id": cmd["id"], "result": result, "error": None})
    except Exception as e:
        result_q.put({"type": "get_info_done", "id": cmd["id"], "result": None, "error": str(e)})


# ------------------------------------------------------------------ async entry

async def _run(cmd_conn, lens_result_q, colnect_result_q):
    pw = await async_playwright().start()

    lens = AsyncGoogleLensController()
    try:
        await lens.start(playwright=pw)
        logger.info("Lens browser started")
    except Exception as e:
        logger.error(f"Lens startup failed: {e}")
        lens = None

    colnect = AsyncColnectController()
    try:
        await colnect.start(playwright=pw)
        ok = await colnect.login()
        colnect_result_q.put({
            "type":  "login_done",
            "error": None if ok else "Login returned False",
        })
    except Exception as e:
        logger.error(f"Colnect startup/login failed: {e}")
        colnect_result_q.put({"type": "login_done", "error": str(e)})
        colnect = None

    # Poll the command pipe; sleep briefly when empty to yield to the event loop.
    # Track only processes own search tasks so it doesn't accidentally cancel Playwright's
    # internal Connection.run() task during shutdown (which would prevent browser close).
    our_tasks: set = set()

    while True:
        try:
            if cmd_conn.poll():
                cmd = cmd_conn.recv()
            else:
                await asyncio.sleep(0.02)
                continue
        except (EOFError, OSError):
            # Main process died or closed the pipe without sending the sentinel.
            # Break so it still attempts a clean browser shutdown.
            logger.warning("Command pipe closed unexpectedly; shutting down browser worker")
            break

        if cmd is None:
            break

        t = cmd.get("type")
        if t == "lens_search" and lens:
            task = asyncio.create_task(_do_lens(lens, cmd, lens_result_q))
            our_tasks.add(task)
            task.add_done_callback(our_tasks.discard)
        elif t == "colnect_search" and colnect:
            task = asyncio.create_task(_do_colnect_search(colnect, cmd, colnect_result_q))
            our_tasks.add(task)
            task.add_done_callback(our_tasks.discard)
        elif t == "colnect_info" and colnect:
            task = asyncio.create_task(_do_colnect_info(colnect, cmd, colnect_result_q))
            our_tasks.add(task)
            task.add_done_callback(our_tasks.discard)
        elif t == "open_url" and colnect:
            task = asyncio.create_task(colnect.navigate_to(cmd["url"]))
            our_tasks.add(task)
            task.add_done_callback(our_tasks.discard)

    # Cancel only the in-flight search tasks — NOT Playwright's internal tasks.
    # Cancelling Connection.run() before ctrl.shutdown() would sever the pipe to
    # Chrome, preventing the close command from being sent and leaving Chrome running.
    for task in list(our_tasks):
        task.cancel()
    if our_tasks:
        await asyncio.gather(*our_tasks, return_exceptions=True)

    for ctrl, name in [(colnect, "Colnect"), (lens, "Lens")]:
        if ctrl:
            try:
                await ctrl.shutdown()
            except Exception as e:
                logger.debug(f"{name} shutdown error: {e}")
    try:
        await pw.stop()
    except Exception as e:
        logger.debug(f"Playwright stop error: {e}")


# ------------------------------------------------------------------ process entry

def browser_process_main(cmd_conn, lens_result_q, colnect_result_q):
    """
    Invoked by AsyncBrowserWorker via multiprocessing.Process. Runs entirely
    in the child process. If the event loop crashes, only this process dies.
    """
    loop = asyncio.ProactorEventLoop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(_run(cmd_conn, lens_result_q, colnect_result_q))
    finally:
        loop.close()
