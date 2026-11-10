"""How this process logs: the one setup both ways of starting the app call.

TWO ENTRY POINTS, ONE CONFIGURATION. `python run.py` (development) and
`hypercorn app.main:app` (the Docker image, which never executes run.py) both
reach `configure()` — run.py before it imports the app, app/main.py at import —
and the second call changes nothing. The same lines used to be written out in
both files, which is how a logging decision drifts between dev and a container.

WHAT GOES WHERE. Third-party libraries at WARNING, since their own DEBUG output
is rarely what anyone wants; the app's own `app.*` loggers at LOG_LEVEL
(default INFO, DEBUG for per-call tracing — see run.py's docstring). Everything
goes to the console. Hypercorn's access log is not here and cannot be: see the
note in app/main.py.

A FILE AS WELL, ONLY WHEN ASKED (`LOG_FILE`). It exists so that when something
misbehaves — a search that took nine seconds — the lines around it can be
handed over afterwards instead of being scrolled back to, or lost with the
terminal. Off by default, because a container's logs are already collected and
a disk that fills with them is its own outage:
  - unset, empty, `0`, `false` or `off`: console only;
  - `1`, `true`, `yes` or `on`: `<data dir>/logs/app.log`, beside everything else
    this instance keeps (config.DATA_DIR, so a Docker volume holds it);
  - anything else: that path.
The file rolls over at LOG_FILE_MAX_BYTES, keeping LOG_FILE_BACKUPS old ones
(`app.log.1` newest ... `app.log.5` oldest), so it can never grow past about
60 MB however long the instance runs.

EVERY LINE IS WRITTEN BY A THREAD OF ITS OWN, never by the thread that logs it.
The root logger holds ONE handler, a queue (`_Queued`); the console and the file
hang off a listener (`_Listener`) whose thread writes what is waiting a batch at
a time. A write and its flush
let go of the interpreter lock, and the thread that let go must win it back —
up to a switch interval (5 ms) each time while another thread is busy — so a
line logged on the event loop was a stall of the loop whenever a week store or
a read was running on a worker thread. MEASURED on the development server: a
refresh pass's 558 per-row DEBUG lines held the loop 2,061 ms; 558 lines to a
RotatingFileHandler took 89 ms idle and 11,224 ms beside one busy thread, and
through a queue, a listener writing them, 17 ms idle and 15 ms beside it. What
the logging thread still pays is spelling the line and putting it on the queue.
The queue is bounded (`QUEUE_MAX`): a console nobody reads can stop the listener,
and then a logger waits for room — what every line cost before — rather than the
process growing without end. `stop` (at exit) writes out whatever is still
queued, and hands the console and the file back to the root so a line logged
after it is written too.
"""
from __future__ import annotations

import atexit
import logging
import logging.handlers
import os
import queue
import threading
from collections import OrderedDict
from pathlib import Path

FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"

LOG_FILE_MAX_BYTES = 10 * 1024 * 1024
LOG_FILE_BACKUPS = 5

# How many lines may wait for the listener before a logger waits for room. A
# refresh pass logs ~600 at DEBUG in one go; a line is about a kilobyte queued,
# so the bound is ~10 MB.
QUEUE_MAX = 10_000

# A FACT ABOUT THE DATA IS SAID ONCE. Some lines describe what a service states —
# one title's ids disagreeing between two of its answers, a listing naming
# another title than its entry is filed under — and every calendar store that
# meets the same listing again used to say it again: on the development server,
# 291 of 381 INFO lines in eight minutes after a restart were two such messages
# restated. Production logs at INFO and rolls its file at LOG_FILE_MAX_BYTES, so
# the repeats pushed out what mattered. `once` says each distinct line the first
# time this process meets it, keeping the last `_SAID_MAX` it has said (the
# process runs for weeks, so the memory is bounded, oldest forgotten first).
_SAID_MAX = 4096
_said: OrderedDict[tuple, None] = OrderedDict()
_said_lock = threading.Lock()

_OFF = {"", "0", "false", "no", "off"}
_DEFAULT = {"1", "true", "yes", "on"}


def log_file_path(value: str | None = None) -> Path | None:
    """Where LOG_FILE says to write, or None for console only."""
    raw = (os.environ.get("LOG_FILE", "") if value is None else value).strip()
    if raw.lower() in _OFF:
        return None
    if raw.lower() in _DEFAULT:
        # Read at call time and through the module: DATA_DIR is the one answer
        # to where this instance keeps its files, and a test points it elsewhere.
        from . import config
        return Path(config.DATA_DIR) / "logs" / "app.log"
    return Path(raw)


def once(logger: logging.Logger, level: int, message: str, *args) -> None:
    """Log `message` % `args` at `level`, unless this process has already said
    that exact line on `logger`. Safe from a worker thread."""
    key = (logger.name, level, message, args)
    with _said_lock:
        if key in _said:
            _said.move_to_end(key)
            return
        _said[key] = None
        if len(_said) > _SAID_MAX:
            _said.popitem(last=False)
    logger.log(level, message, *args)


class _Queued(logging.handlers.QueueHandler):
    """The root's one handler: each line onto the bounded queue, waiting for room
    when it is full (the standard handler's `put_nowait` would report a full
    queue as a logging error and lose the line)."""

    def enqueue(self, record: logging.LogRecord) -> None:
        self.queue.put(record)


def _lines(handler: logging.Handler, records: list[logging.LogRecord]) -> list[str]:
    return [handler.format(record) + handler.terminator for record in records]


class _Console(logging.StreamHandler):
    """The console, written a batch at a time (`_Listener`)."""

    def emit_batch(self, records: list[logging.LogRecord]) -> None:
        try:
            text = "".join(_lines(self, records))
            with self.lock:
                self.stream.write(text)
                self.flush()
        except Exception:
            self.handleError(records[0])


class _File(logging.handlers.RotatingFileHandler):
    """The log file, written a batch at a time (`_Listener`) — still rolled over
    exactly where a line at a time would roll it: before the line that would
    take a non-empty file to `maxBytes`."""

    def emit_batch(self, records: list[logging.LogRecord]) -> None:
        try:
            lines = _lines(self, records)
            with self.lock:
                if self.stream is None:
                    self.stream = self._open()
                # A path that is not a regular file (os.devnull) never rolls, as
                # RotatingFileHandler.shouldRollover says.
                rolls = self.maxBytes > 0 and os.path.isfile(self.baseFilename)
                at = self.stream.tell() if rolls else 0
                chunk: list[str] = []
                for line in lines:
                    if rolls and at and at + len(line) >= self.maxBytes:
                        self.stream.write("".join(chunk))
                        self.doRollover()
                        chunk, at = [], 0
                    chunk.append(line)
                    at += len(line)
                self.stream.write("".join(chunk))
                self.flush()
        except Exception:
            self.handleError(records[0])


# What `_Listener.stop` puts on the queue to end the thread.
_STOP = object()

# The most lines one batch writes; a longer queue is written in several.
BATCH_MAX = 1000


class _Listener:
    """The thread writing what `_Queued` queues, A BATCH AT A TIME: everything
    waiting is taken at once and each handler writes it with one write and one
    flush. A line at a time, the listener paid the same toll the loop did —
    every write, flush and stat waiting a switch interval for the interpreter
    lock beside a busy thread — and MEASURED 52 lines a second beside one, so a
    DEBUG stream filled the queue and the loop waited for room again; batched,
    17,000 a second beside one (30,000 idle, from 5,600). A handler with no
    `emit_batch` is handed the lines one by one."""

    def __init__(self, lines: queue.Queue, *handlers: logging.Handler) -> None:
        self.queue = lines
        self.handlers = tuple(handlers)
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="log-writer", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """Write everything queued before this call, then end the thread. The
        marker waits for room as a line does."""
        if self._thread is not None:
            self.queue.put(_STOP)
            self._thread.join()
            self._thread = None

    def _run(self) -> None:
        while True:
            batch = [self.queue.get()]
            while len(batch) < BATCH_MAX and batch[-1] is not _STOP:
                try:
                    batch.append(self.queue.get_nowait())
                except queue.Empty:
                    break
            stopping = batch[-1] is _STOP
            records = batch[:-1] if stopping else batch
            if records:
                self._write(records)
            for _ in batch:
                self.queue.task_done()
            if stopping:
                return

    def _write(self, records: list[logging.LogRecord]) -> None:
        for handler in self.handlers:
            kept = [record for record in records if record.levelno >= handler.level]
            if not kept:
                continue
            write = getattr(handler, "emit_batch", None)
            if write is not None:
                write(kept)
            else:
                for record in kept:
                    handler.handle(record)


# The root's queue handler and the listener writing what it queues, once
# `configure` has made them; None before, and again after `stop`.
_queued: _Queued | None = None
_listener: _Listener | None = None
_setup_lock = threading.Lock()


def _writers() -> tuple[logging.Handler, ...]:
    """Every handler that writes this process's lines: the listener's, and any
    the root holds itself (after `stop`, or put there by a host)."""
    root = logging.getLogger()
    held = _listener.handlers if _listener is not None else ()
    return (*held, *(h for h in root.handlers if h is not _queued))


def _start() -> None:
    """The queue, its listener and — as `basicConfig` would, only where nothing
    has set the root up already (a host's own handlers, pytest's capture) — the
    console. Called once, under `_setup_lock`."""
    global _queued, _listener
    root = logging.getLogger()
    writers: list[logging.Handler] = []
    if not root.handlers:
        root.setLevel(logging.WARNING)
        console = _Console()
        console.setFormatter(logging.Formatter(FORMAT))
        writers.append(console)
    lines: queue.Queue = queue.Queue(QUEUE_MAX)
    _queued = _Queued(lines)
    _listener = _Listener(lines, *writers)
    _listener.start()
    root.addHandler(_queued)
    atexit.unregister(stop)   # once, however many times a test starts afresh
    atexit.register(stop)


def configure() -> Path | None:
    """Set this process's logging up, once. Returns the log file, if any.

    IDEMPOTENT ON PURPOSE: both entry points call it (see the module
    docstring). The queue and its listener are made once; the file handler is
    recognised by the file it writes, so a second call — or a test calling it
    again — never writes every line twice.
    """
    logging.getLogger("app").setLevel(os.environ.get("LOG_LEVEL", "INFO").upper())
    path = log_file_path()
    with _setup_lock:
        if _listener is None:
            _start()
        if path is None:
            return None
        target = str(path.resolve())
        if any(isinstance(h, logging.FileHandler) and h.baseFilename == target
               for h in _writers()):
            return path
        path.parent.mkdir(parents=True, exist_ok=True)
        handler = _File(
            path, maxBytes=LOG_FILE_MAX_BYTES, backupCount=LOG_FILE_BACKUPS, encoding="utf-8")
        handler.setFormatter(logging.Formatter(FORMAT))
        # The listener reads its handlers afresh for every line it writes, so
        # handing it a new tuple is all adding one takes.
        _listener.handlers = (*_listener.handlers, handler)
    logging.getLogger(__name__).info(
        "Writing logs to %s as well as the console (rolled over at %d MB, %d kept).",
        path, LOG_FILE_MAX_BYTES // (1024 * 1024), LOG_FILE_BACKUPS)
    return path


def without_file() -> None:
    """Close and drop every log file this process writes, keeping the console:
    a calendar read worker's set-up (app/read_pool.py), since the web process
    owns the file and rotates it, and a second writer could not."""
    with _setup_lock:
        if _listener is not None:
            kept = tuple(h for h in _listener.handlers if not isinstance(h, logging.FileHandler))
            dropped = [h for h in _listener.handlers if h not in kept]
            _listener.handlers = kept
        else:
            dropped = []
        root = logging.getLogger()
        for handler in [h for h in root.handlers if isinstance(h, logging.FileHandler)]:
            root.removeHandler(handler)
            dropped.append(handler)
    for handler in dropped:
        handler.close()


def stop() -> None:
    """Write out every queued line, end the listener and hand its handlers back
    to the root, so a line logged after this — by a later exit handler, say — is
    written on the thread that logs it, with no loop left to keep waiting. Runs
    at exit; safe to call again."""
    global _queued, _listener
    with _setup_lock:
        listener, queued = _listener, _queued
        _listener = _queued = None
        if listener is None:
            return
        root = logging.getLogger()
        root.removeHandler(queued)
        listener.stop()   # writes everything queued before it returns
        for handler in listener.handlers:
            root.addHandler(handler)
