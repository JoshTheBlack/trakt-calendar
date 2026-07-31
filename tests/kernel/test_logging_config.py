"""The logging levels app/main.py sets at import, and the one decision it cannot
make.

WHY THIS IS PINNED. There are two ways this app starts — run.py for development,
and `hypercorn app.main:app` for the Docker image, which never executes run.py at
all. So every logging decision has to be made somewhere both paths reach, and the
access log had drifted: the Docker CMD passed --access-logfile, the dev runner
left it unset, and the container logged a line per static file, per poll and per
health check while development stayed clean. The app.perf warnings that exist to
surface a slow request had to be found inside that.

THE ACCESS LOG IS NOT SETTABLE FROM PYTHON, which is the trap worth recording.
Hypercorn constructs its Logger AFTER importing the app and calls setLevel on
"hypercorn.access" from its own config, so a level set at import is overwritten
before the first request. Only the invocation decides it. That is why the test
below reads the Dockerfile: the behaviour lives in a CMD, no runtime assertion in
this process can observe it, and a wrong answer there is invisible until a deploy.
"""
from __future__ import annotations

import logging
import os
import re
import unittest
from pathlib import Path

# Imported for its import-time side effects, which are the subject of the tests.
from app import main  # noqa: F401

DOCKERFILE = Path(__file__).resolve().parents[2] / "Dockerfile"


class AccessLogInvocationTests(unittest.TestCase):
    """Reading a config file is not this suite's habit, and it is right here: the
    switch is a command-line flag, so there is nothing else to assert against."""

    def test_the_image_does_not_enable_the_access_log(self):
        cmd = [line for line in DOCKERFILE.read_text(encoding="utf-8").splitlines()
               if re.match(r"\s*CMD\b", line)]
        self.assertTrue(cmd, "Dockerfile has no CMD to check.")
        self.assertNotIn(
            "--access-logfile", cmd[-1],
            "The image's CMD must not enable Hypercorn's access log: it logs a "
            "line per static file and per health check, and it CANNOT be quieted "
            "from Python (Hypercorn overwrites the level after import). Append the "
            "flag by hand for an investigation instead.",
        )


class LoggingConfigTests(unittest.TestCase):
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
