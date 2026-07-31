"""The logging levels app/main.py sets at import.

WHY THIS IS PINNED. There are two ways this app starts — run.py for development,
and `hypercorn app.main:app` for the Docker image, which never executes run.py at
all. Every logging decision therefore has to be made in main.py to reach both,
and the one that was NOT had gone unnoticed: run.py quieted Hypercorn's
per-request access log, main.py did not, and the Docker CMD passes
--access-logfile explicitly — so the container logged a line per static file, per
poll and per health check while the dev runner stayed clean. The app.perf
warnings that exist to surface a slow request had to be found inside that.

These assertions are about PARITY, not about the numbers being sacred. If a level
here should change, change it in main.py and here together — the point is that
the change is deliberate rather than a path nobody ran locally.
"""
from __future__ import annotations

import logging
import os
import unittest

# Imported for its import-time side effects, which are the subject of the tests.
from app import main  # noqa: F401


class LoggingConfigTests(unittest.TestCase):
    def test_hypercorn_access_log_is_quiet(self):
        """The firehose is off by default. perftrace's request timer reports any
        request over SLOW_REQUEST_MS with its phases named, and every request at
        DEBUG, so nothing diagnostic is lost by silencing this."""
        self.assertEqual(
            logging.getLogger("hypercorn.access").level, logging.WARNING,
            "app/main.py must quiet hypercorn.access — the Docker CMD turns the "
            "access log on explicitly, and only main.py runs on that path.",
        )

    def test_the_app_logger_follows_LOG_LEVEL(self):
        """The app's own diagnostics are the thing LOG_LEVEL is for, and they
        must reach the Docker path too."""
        expected = getattr(logging, os.environ.get("LOG_LEVEL", "INFO").upper(), logging.INFO)
        self.assertEqual(logging.getLogger("app").level, expected)

    def test_perf_is_not_silenced_independently_of_the_app_logger(self):
        """app.perf carries the slow-request and loop-stall lines and inherits
        from "app" on purpose: one knob, not two. A level set directly on it
        would mean LOG_LEVEL no longer governed the diagnostics it advertises."""
        self.assertEqual(logging.getLogger("app.perf").level, logging.NOTSET)


if __name__ == "__main__":
    unittest.main()
