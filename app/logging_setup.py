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
"""
from __future__ import annotations

import logging
import logging.handlers
import os
import threading
from collections import OrderedDict
from pathlib import Path

FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"

LOG_FILE_MAX_BYTES = 10 * 1024 * 1024
LOG_FILE_BACKUPS = 5

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


def configure() -> Path | None:
    """Set this process's logging up, once. Returns the log file, if any.

    IDEMPOTENT ON PURPOSE: both entry points call it (see the module
    docstring). `basicConfig` already attaches its console handler only once;
    the file handler is recognised by the file it writes, so a second call —
    or a test calling it again — never writes every line twice.
    """
    logging.basicConfig(level=logging.WARNING, format=FORMAT)
    logging.getLogger("app").setLevel(os.environ.get("LOG_LEVEL", "INFO").upper())
    path = log_file_path()
    if path is None:
        return None
    root = logging.getLogger()
    target = str(path.resolve())
    for handler in root.handlers:
        if isinstance(handler, logging.FileHandler) and handler.baseFilename == target:
            return path
    path.parent.mkdir(parents=True, exist_ok=True)
    handler = logging.handlers.RotatingFileHandler(
        path, maxBytes=LOG_FILE_MAX_BYTES, backupCount=LOG_FILE_BACKUPS, encoding="utf-8")
    handler.setFormatter(logging.Formatter(FORMAT))
    root.addHandler(handler)
    logging.getLogger(__name__).info(
        "Writing logs to %s as well as the console (rolled over at %d MB, %d kept).",
        path, LOG_FILE_MAX_BYTES // (1024 * 1024), LOG_FILE_BACKUPS)
    return path
