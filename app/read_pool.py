"""The calendar's reads, in worker processes of their own: what one interpreter
lock cannot run side by side.

WHY A PROCESS AND NOT A THREAD. A read is Python over thousands of rows — a
month's cards made, placed and judged — and threads share one interpreter lock,
so reads asked at once run one after another however many threads hold them.
MEASURED on a copy of dev: six of a month's late days asked at once, as a page
scrolled to them asks them, took 2.6 s each where one alone took 0.4 s; and while
a restart's refills ran, a month's read took 2.2-2.4 s against 1.4 s idle. A
worker process has an interpreter of its own, and the pool's workers run as many
reads at once as there are workers.

WHAT RUNS HERE IS A READ, AND ONLY A READ (`read`): a function of a connection
and values, handed values and returning values. A worker's connection is
read-only (db.worker_read), so nothing it runs can write — every write stays in
the web process's one writer queue — and a read touches none of the in-process
state the web process keeps (a page's filed month, the refill latches, the
pacers, the month-changed clocks): the latch list a second process would have to
move is empty for this, which is what made it the shape chosen. Everything a
read is handed and returns crosses the pipe pickled: a filtered month's kept
cards are 0.8 MB, a day's well under that.

HOW MANY: `READ_PROCESSES`, by default one fewer than the machine's cores, at most
four — each worker is a Python process of its own (its own imports, its own
memory) — and 0 runs every read on a worker thread of the web process, as
before: the test suite runs that way, and a machine of one core has nothing to
spare. SPAWNED, NEVER FORKED, on every platform: the web process has threads and
an event loop by the time a pool starts, and a fork copies whatever lock one of
them held into a child that can never release it.

STARTED WITH THE APP AND WARMED BEHIND IT (`start`): each worker is started and
its read modules imported then, so the first month a viewer opens after a
restart does not wait for a process to start. A worker that dies (a crash, a
kill) breaks the pool; the next read builds a new one and asks again, once.
"""
from __future__ import annotations

import asyncio
import logging
import multiprocessing
import os
import signal
from collections.abc import Callable
from concurrent.futures import ProcessPoolExecutor
from concurrent.futures.process import BrokenProcessPool
from typing import Any, TypeVar

from . import db

logger = logging.getLogger(__name__)

T = TypeVar("T")

# The most workers a pool starts by default, whatever the machine.
DEFAULT_MOST = 4

_pool: ProcessPoolExecutor | None = None
# What `start` was asked to warm each worker with, for a pool built again.
_warm: Callable[[], None] | None = None


def processes() -> int:
    """How many workers a pool runs: `READ_PROCESSES`, else one fewer than the
    cores (at most `DEFAULT_MOST`); 0 means no pool."""
    raw = os.environ.get("READ_PROCESSES", "").strip()
    if raw:
        try:
            return max(0, int(raw))
        except ValueError:
            logger.warning("READ_PROCESSES=%r is not a number; using the default", raw)
    return max(0, min(DEFAULT_MOST, (os.cpu_count() or 1) - 1))


def _worker_started() -> None:
    """A worker's set-up, before its first read: it writes no log file — the web
    process owns the one it rotates, and a second writer could not rotate it —
    and a console's Ctrl+C is the web process's to act on."""
    db.become_read_only()
    root = logging.getLogger()
    for handler in [h for h in root.handlers if isinstance(h, logging.FileHandler)]:
        root.removeHandler(handler)
        handler.close()
    signal.signal(signal.SIGINT, signal.SIG_IGN)


def _build(count: int) -> ProcessPoolExecutor:
    pool = ProcessPoolExecutor(count, mp_context=multiprocessing.get_context("spawn"),
                               initializer=_worker_started)
    if _warm is not None:
        # One task a worker, submitted together: a pool starts a worker for each
        # task no idle worker is free for, so this starts them all now.
        for _ in range(count):
            pool.submit(_warm)
    return pool


def start(warm: Callable[[], None] | None = None) -> None:
    """Build the pool, if this instance runs one, and start its workers behind
    the start-up — each running `warm` (a module-level function), which imports
    what a read needs."""
    global _pool, _warm
    _warm = warm
    count = processes()
    if _pool is None and count:
        _pool = _build(count)
        logger.info("calendar reads run in %d worker process(es)", count)


def stop() -> None:
    """Let the pool go: its workers finish what they hold and exit."""
    global _pool
    pool, _pool = _pool, None
    if pool is not None:
        pool.shutdown(wait=False, cancel_futures=True)


async def read(fn: Callable[..., T], *args: Any, **kwargs: Any) -> T:
    """`fn(conn, *args, **kwargs)` — a READ, `fn` a module-level function and
    every argument a value — in a worker process with its own read-only
    connection, or on a worker thread of this process where there is no pool."""
    global _pool
    pool = _pool
    if pool is None:
        return await db.run(lambda conn: fn(conn, *args, **kwargs))
    path = str(db.db_path())
    loop = asyncio.get_running_loop()
    try:
        return await loop.run_in_executor(pool, db.worker_read, path, fn, args, kwargs)
    except BrokenProcessPool:
        # A worker died under it. Built again once — by the first read to find
        # it broken; a read finding a pool already rebuilt uses that one, and one
        # finding the pool stopped reads on a thread.
        if _pool is pool:
            logger.warning("a calendar read worker stopped; starting the pool again")
            pool.shutdown(wait=False, cancel_futures=True)
            _pool = _build(processes() or 1)
        rebuilt = _pool
        if rebuilt is None:
            return await db.run(lambda conn: fn(conn, *args, **kwargs))
        return await loop.run_in_executor(rebuilt, db.worker_read, path, fn, args, kwargs)
