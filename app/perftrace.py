"""Lightweight timing/trace spans for the distrakt request path.

Logs to the "app.perf" logger (INFO), which run.py already enables. Use as a
context manager around awaited blocks — the elapsed time covers the awaits:

    from .perftrace import span
    with span("phase", n=len(records)):
        await do_work()

Set fields after the fact with the returned handle:

    with span("phase") as sp:
        rows = await fetch()
        sp.set(rows=len(rows))
"""
from __future__ import annotations

import logging
import time
from contextlib import contextmanager

logger = logging.getLogger("app.perf")


class _Span:
    __slots__ = ("fields",)

    def __init__(self, fields: dict):
        self.fields = fields

    def set(self, **kw) -> None:
        self.fields.update(kw)


@contextmanager
def span(label: str, **fields):
    sp = _Span(dict(fields))
    t0 = time.perf_counter()
    try:
        yield sp
    finally:
        dt_ms = (time.perf_counter() - t0) * 1000.0
        extra = " ".join(f"{k}={v}" for k, v in sp.fields.items())
        # DEBUG: per-request timers are diagnostic; enable app.perf=DEBUG to see them.
        logger.debug("⏱ %-26s %7.1fms  %s", label, dt_ms, extra)
