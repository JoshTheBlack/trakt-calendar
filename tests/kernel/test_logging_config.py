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
import logging.handlers
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


class _FreshSetup(unittest.TestCase):
    """Each test configures logging from nothing: its own queue and listener in
    place of the ones app/main.py made at import, put back afterwards."""

    def setUp(self):
        import tempfile
        from unittest import mock
        from app import logging_setup
        self.logging_setup = logging_setup
        self.dir = Path(tempfile.mkdtemp())
        self.root = logging.getLogger()
        self.before = list(self.root.handlers)
        for name in ("_queued", "_listener"):
            patcher = mock.patch.object(logging_setup, name, None)
            patcher.start()
            self.addCleanup(patcher.stop)

    def tearDown(self):
        import shutil
        self.logging_setup.stop()
        for handler in self.root.handlers[:]:
            if handler not in self.before:
                self.root.removeHandler(handler)
                handler.close()
        shutil.rmtree(self.dir, ignore_errors=True)

    def configure(self, value, **patches):
        from unittest import mock
        with mock.patch.dict(os.environ, {"LOG_FILE": value}), \
                mock.patch.multiple(self.logging_setup, **patches) if patches else _nothing():
            return self.logging_setup.configure()

    def file_handlers(self):
        return [h for h in self.logging_setup._writers()
                if isinstance(h, logging.FileHandler) and h not in self.before]


class LogFileTests(_FreshSetup):
    """LOG_FILE: a rolling file beside the console, only when asked for."""

    def test_off_unless_asked(self):
        from app import logging_setup
        for value in ("", "0", "false", "Off", " no "):
            with self.subTest(value=value):
                self.assertIsNone(logging_setup.log_file_path(value))
                self.assertIsNone(self.configure(value))
                self.assertEqual(self.file_handlers(), [])

    def test_a_yes_means_the_data_directory_and_anything_else_is_a_path(self):
        from app import config, logging_setup
        self.assertEqual(logging_setup.log_file_path("1"),
                         Path(config.DATA_DIR) / "logs" / "app.log")
        self.assertEqual(logging_setup.log_file_path("TRUE"),
                         Path(config.DATA_DIR) / "logs" / "app.log")
        self.assertEqual(logging_setup.log_file_path(str(self.dir / "x.log")), self.dir / "x.log")

    def test_ten_megabytes_five_kept_and_one_handler_however_often_it_is_called(self):
        """Both entry points call configure(); a second handler would write every
        line twice and fight the first over the rollover."""
        path = self.dir / "logs" / "app.log"
        self.assertEqual(self.configure(str(path)), path)
        self.configure(str(path))
        handler, = self.file_handlers()
        self.assertEqual((handler.maxBytes, handler.backupCount), (10 * 1024 * 1024, 5))
        logging.getLogger("app.somewhere").warning("a line worth handing over")
        self.logging_setup.stop()
        self.assertEqual(path.read_text(encoding="utf-8").count(
            "WARNING app.somewhere: a line worth handing over"), 1)

    def test_it_rolls_over_and_keeps_five(self):
        path = self.dir / "app.log"
        self.configure(str(path), LOG_FILE_MAX_BYTES=300)
        log = logging.getLogger("app.rollover")
        for n in range(200):
            log.warning("line %03d of a log that outgrows its file", n)
        self.logging_setup.stop()
        for handler in self.file_handlers():
            handler.close()
        self.assertEqual(sorted(p.name for p in self.dir.iterdir()),
                         ["app.log", "app.log.1", "app.log.2", "app.log.3", "app.log.4",
                          "app.log.5"])


class LinesAreWrittenByAThreadOfTheirOwnTests(_FreshSetup):
    """A write lets go of the interpreter lock and must win it back, a switch
    interval each time beside a busy thread — so the thread that logs (the
    event loop, often) only queues the line, and the listener writes it."""

    def test_the_root_holds_one_queue_however_often_it_is_configured(self):
        path = self.dir / "app.log"
        for _ in range(3):
            self.configure(str(path))
        added = [h for h in self.root.handlers if h not in self.before]
        self.assertEqual(len(added), 1)
        self.assertIsInstance(added[0], logging.handlers.QueueHandler)
        self.assertEqual(len(self.file_handlers()), 1)
        self.assertNotIn(self.file_handlers()[0], self.root.handlers,
                         "the file is written by the listener, never from the root")

    def test_the_file_is_written_on_the_listeners_thread(self):
        import threading
        path = self.dir / "app.log"
        self.configure(str(path))
        handler, = self.file_handlers()
        writers = []
        real = handler.emit_batch

        def emit_batch(records):
            writers.append(threading.get_ident())
            real(records)

        handler.emit_batch = emit_batch
        logging.getLogger("app.somewhere").warning("queued, not written here")
        self.logging_setup.stop()
        # The setup's own "Writing logs to" line may be written after the patch too.
        self.assertTrue(writers)
        self.assertNotIn(threading.get_ident(), writers)

    def test_stopping_writes_everything_queued_and_a_later_line_too(self):
        path = self.dir / "app.log"
        self.configure(str(path))
        log = logging.getLogger("app.burst")
        for n in range(600):
            log.warning("row %03d refreshed", n)
        self.logging_setup.stop()
        log.warning("after the listener stopped")
        self.logging_setup.stop()   # again: nothing to do
        text = path.read_text(encoding="utf-8")
        self.assertEqual(sum(f"row {n:03d} refreshed" in text for n in range(600)), 600)
        self.assertIn("after the listener stopped", text)

    def test_a_full_queue_waits_for_room_and_loses_nothing(self):
        import threading
        path = self.dir / "app.log"
        self.configure(str(path), QUEUE_MAX=3)
        handler, = self.file_handlers()
        held = threading.Event()
        real = handler.emit_batch

        def emit_batch(records):
            held.wait(10)
            real(records)

        handler.emit_batch = emit_batch
        log = logging.getLogger("app.full")
        logger_done = threading.Event()

        def burst():
            for n in range(20):
                log.warning("line %02d", n)
            logger_done.set()

        thread = threading.Thread(target=burst)
        thread.start()
        self.assertFalse(logger_done.wait(0.3), "a full queue should hold the logger back")
        held.set()
        thread.join(10)
        self.assertTrue(logger_done.is_set())
        self.logging_setup.stop()
        lines = [line for line in path.read_text(encoding="utf-8").splitlines() if "app.full" in line]
        self.assertEqual([line[-7:] for line in lines], [f"line {n:02d}" for n in range(20)])

    def test_lines_waiting_together_are_written_together(self):
        """Beside a busy thread every write and flush waits for the interpreter
        lock, so the listener writes what is waiting in one go: a line at a time
        it managed 52 a second there."""
        import threading
        path = self.dir / "app.log"
        self.configure(str(path))
        handler, = self.file_handlers()
        self.logging_setup._listener.queue.join()   # the setup's own line, written
        held, batches = threading.Event(), []
        real = handler.emit_batch

        def emit_batch(records):
            held.wait(10)
            batches.append(len(records))
            real(records)

        handler.emit_batch = emit_batch
        log = logging.getLogger("app.burst")
        log.warning("the first line, which holds the writer")
        for n in range(500):
            log.warning("row %03d refreshed", n)
        held.set()
        self.logging_setup.stop()
        self.assertEqual(sum(batches), 501)
        self.assertLessEqual(len(batches), 2, batches)
        self.assertEqual(path.read_text(encoding="utf-8").count(" refreshed"), 500)

    def test_without_file_closes_the_file_and_keeps_the_rest(self):
        from unittest import mock
        path = self.dir / "app.log"
        self.configure(str(path))
        handler, = self.file_handlers()
        console = logging.StreamHandler()
        listener = self.logging_setup._listener
        listener.handlers = (console, *listener.handlers)
        # pytest's own capture file stays out of it: it is not this test's to close.
        with mock.patch.object(self.root, "handlers",
                               [h for h in self.root.handlers
                                if not isinstance(h, logging.FileHandler)]):
            self.logging_setup.without_file()
        self.assertEqual(listener.handlers, (console,))
        self.assertIsNone(handler.stream, "the dropped file should be closed")


class AProcessWritesItsLinesBeforeItExitsTests(unittest.TestCase):
    """The real shape, in a process of its own: configured with nothing set up
    before it, the console is the listener's, and a burst logged just before the
    interpreter exits is all in the file — `stop` runs at exit."""

    SCRIPT = (
        "import logging\n"
        "from app import logging_setup\n"
        "logging_setup.configure(); logging_setup.configure()\n"
        "root = logging.getLogger()\n"
        "assert len(root.handlers) == 1, root.handlers\n"
        "log = logging.getLogger('app.exit')\n"
        "for n in range(2000):\n"
        "    log.debug('line %04d', n)\n"
    )

    def test_a_burst_logged_at_exit_is_all_written_once(self):
        import subprocess
        import sys
        import tempfile
        logs = Path(tempfile.mkdtemp())
        self.addCleanup(__import__("shutil").rmtree, logs, True)
        log_file = logs / "app.log"
        done = subprocess.run(
            [sys.executable, "-c", self.SCRIPT], cwd=Path(__file__).resolve().parents[2],
            env={**os.environ, "LOG_FILE": str(log_file), "LOG_LEVEL": "DEBUG"},
            capture_output=True, text=True, encoding="utf-8", timeout=120)
        self.assertEqual(done.returncode, 0, done.stderr)
        written = [line for line in log_file.read_text(encoding="utf-8").splitlines()
                   if "app.exit" in line]
        self.assertEqual(len(written), 2000)
        self.assertEqual(done.stderr.count("DEBUG app.exit: line "), 2000,
                         "the console is the listener's too")


def _nothing():
    import contextlib
    return contextlib.nullcontext()


if __name__ == "__main__":
    unittest.main()


class AFactAboutTheDataIsSaidOnceTests(unittest.TestCase):
    """`logging_setup.once`: a line every calendar store would restate is said
    the first time this process meets it — on the development server two such
    messages were 291 of 381 INFO lines in eight minutes."""

    def setUp(self):
        from collections import OrderedDict
        from unittest.mock import patch

        from app import logging_setup
        self.logging_setup = logging_setup
        patcher = patch.object(logging_setup, "_said", OrderedDict())
        patcher.start()
        self.addCleanup(patcher.stop)
        self.logger = logging.getLogger("app.test_once")

    def _say(self, *args):
        self.logging_setup.once(self.logger, logging.INFO, "%s holds %s", *args)

    def test_the_same_line_is_said_once_and_another_line_is_said_too(self):
        with self.assertLogs(self.logger, logging.INFO) as said:
            for _ in range(3):
                self._say("show:tmdb:1", "tvdb=2")
            self._say("show:tmdb:1", "tvdb=3")
        self.assertEqual([record.getMessage() for record in said.records],
                         ["show:tmdb:1 holds tvdb=2", "show:tmdb:1 holds tvdb=3"])

    def test_what_it_remembers_is_bounded_and_the_oldest_goes_first(self):
        from unittest.mock import patch
        with patch.object(self.logging_setup, "_SAID_MAX", 2), \
                self.assertLogs(self.logger, logging.INFO) as said:
            for title in ("a", "b", "c", "a"):
                self._say(title, "x")
        self.assertEqual(len(self.logging_setup._said), 2)
        self.assertEqual([record.args[0] for record in said.records], ["a", "b", "c", "a"])
