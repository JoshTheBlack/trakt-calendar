"""app/read_pool.py — the calendar's reads in worker processes of their own.

THE PROMISES: a read run in a worker answers exactly what it answers on a
thread, from the web process's own database; a worker cannot write; the web
process can never be made a reader by running a worker's entry point; a worker
that dies is replaced by the next read, which still answers; a worker leaves
when its web process is killed; and how many workers there are is
`READ_PROCESSES`, else a share of the cores.

THESE START REAL PROCESSES (spawned, as the app's are), so they are few: the
rest of the suite reads on threads (tests/conftest.py's READ_PROCESSES=0), which
runs the very functions a worker runs.
"""
from __future__ import annotations

import asyncio
import multiprocessing.connection
import os
import signal
import subprocess
import sys
import time
import unittest
from datetime import date, datetime, timezone
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

from app import db, read_pool
from app.calendar import query, vocab
from app.endpoints import get_endpoint
from app.providers.base import Media, Record, Source
from tests.support import CardTestCase, seed_records

AIRS = datetime(2026, 7, 8, 20, tzinfo=timezone.utc).timestamp()
ROOT = Path(__file__).resolve().parents[2]


def _records() -> list[Record]:
    return [Record(source=Source.TRAKT, media=Media.SHOW, id=f"show-{n}",
                   ids={"trakt": 100 + n, "tmdb": 700 + n}, detail_url="", title=f"Show {n}",
                   air_ts=AIRS + 3600 * n, season=1, episode_number=n + 1)
            for n in range(6)]


def _write(conn) -> None:
    """A read that tries to write — for a worker to refuse."""
    conn.execute("INSERT INTO app_meta (key, value) VALUES ('x', 'y')")


class HowManyWorkersTests(unittest.TestCase):

    def test_the_variable_decides(self):
        for raw, count in (("0", 0), ("3", 3), (" 2 ", 2), ("-1", 0)):
            with self.subTest(raw=raw), mock.patch.dict(os.environ, {"READ_PROCESSES": raw}):
                self.assertEqual(read_pool.processes(), count)

    def test_otherwise_a_share_of_the_cores(self):
        with mock.patch.dict(os.environ, {"READ_PROCESSES": ""}):
            for cores, count in ((1, 0), (2, 1), (8, read_pool.DEFAULT_MOST), (None, 0)):
                with self.subTest(cores=cores), mock.patch.object(os, "cpu_count", lambda: cores):
                    self.assertEqual(read_pool.processes(), count)

    def test_a_word_is_the_default(self):
        with mock.patch.dict(os.environ, {"READ_PROCESSES": "many"}), \
                mock.patch.object(os, "cpu_count", lambda: 3):
            self.assertEqual(read_pool.processes(), 2)


class WhichReadsGoToAWorkerTests(unittest.TestCase):
    """A month whose viewer filters nothing answers with the whole calendar,
    which costs more to hand across than to read in place; every other span
    goes to a worker."""

    def _viewer(self, **specs) -> query.Viewer:
        return query.Viewer(get_endpoint("shows"), None, None, ZoneInfo("UTC"),
                            vocab.complete_specs(specs))

    def test_a_month_nobody_filters_is_read_in_place(self):
        self.assertTrue(query._answered_in_place(self._viewer(), date(2026, 9, 30),
                                                 date(2026, 11, 2)))

    def test_a_filtered_month_and_any_day_go_to_a_worker(self):
        self.assertFalse(query._answered_in_place(self._viewer(genres="-drama"),
                                                  date(2026, 9, 30), date(2026, 11, 2)))
        self.assertFalse(query._answered_in_place(self._viewer(single_service=True),
                                                  date(2026, 9, 30), date(2026, 11, 2)))
        self.assertFalse(query._answered_in_place(self._viewer(), date(2026, 10, 13),
                                                  date(2026, 10, 16)))


class TheWebProcessIsNeverAReaderTests(unittest.TestCase):

    def test_a_workers_entry_point_refuses_to_run_here(self):
        with self.assertRaises(RuntimeError):
            db.worker_read(str(db.db_path()), _write, (), {})


class AReadInAWorkerTests(CardTestCase):
    """One real worker, spawned, against this test's own database."""

    def setUp(self):
        super().setUp()
        seed_records(db.connection(), _records())
        self.viewer = query.Viewer(get_endpoint("shows"), None, None, ZoneInfo("UTC"),
                                   vocab.complete_specs({}))
        env = mock.patch.dict(os.environ, {"READ_PROCESSES": "1"})
        env.start()
        self.addCleanup(env.stop)
        read_pool.start(warm=query.ready)
        self.addCleanup(read_pool.stop)

    def _span(self) -> query.Placed:
        return asyncio.run(query.span(self.viewer, date(2026, 7, 6), date(2026, 7, 13),
                                      held=query.Held(), near=(date(2026, 7, 6), date(2026, 7, 12)),
                                      days=(date(2026, 7, 6), date(2026, 7, 12))))

    def test_it_answers_what_a_thread_answers(self):
        in_worker = self._span()
        read_pool.stop()
        on_thread = self._span()
        self.assertEqual(len(in_worker.views), 6)
        self.assertEqual([(v.card.title_key, v.air_date, v.air_ts, v.mark_key)
                          for v in in_worker.views],
                         [(v.card.title_key, v.air_date, v.air_ts, v.mark_key)
                          for v in on_thread.views])
        items = asyncio.run(query.present(self.viewer, in_worker.views))
        self.assertEqual([item.title for item in items], [f"Show {n}" for n in range(6)])

    def test_a_worker_cannot_write(self):
        with self.assertRaises(Exception) as raised:
            asyncio.run(read_pool.read(_write))
        self.assertIn("readonly", str(raised.exception).replace(" ", "").lower())

    def test_a_worker_that_dies_is_replaced_by_the_next_read(self):
        self.assertEqual(len(self._span().views), 6)
        for process in list(read_pool._pool._processes.values()):
            process.kill()
            process.join(10)
        self.assertEqual(len(self._span().views), 6)


class _Watched:
    """Another process's lifetime, held from before anything is killed so a
    reused pid cannot answer for it; `end` takes it down if it is still there."""

    def __init__(self, pid: int):
        self.pid = pid
        self._handle = None
        if sys.platform == "win32":
            import _winapi
            # SYNCHRONIZE to wait on it, PROCESS_TERMINATE to clean it up.
            self._handle = _winapi.OpenProcess(_winapi.SYNCHRONIZE | 0x0001, False, pid)

    def gone_within(self, seconds: float) -> bool:
        if self._handle is not None:
            return bool(multiprocessing.connection.wait([self._handle], seconds))
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            try:
                os.kill(self.pid, 0)
            except ProcessLookupError:
                return True
            time.sleep(0.1)
        return False

    def end(self) -> None:
        if self._handle is not None:
            import _winapi
            if not self.gone_within(0):
                _winapi.TerminateProcess(self._handle, 1)
            _winapi.CloseHandle(self._handle)
        elif not self.gone_within(0):
            os.kill(self.pid, signal.SIGKILL)


class AWorkerLeavesWithItsWebProcessTests(unittest.TestCase):
    """A web process stopped by force tells its workers nothing; each must
    leave anyway rather than run on with nobody to ask it anything. The web
    process here is a stand-in that starts a pool of one, names its worker and
    waits to be killed."""

    STAND_IN = (
        "import os, time\n"
        "os.environ['READ_PROCESSES'] = '1'\n"
        "from app import read_pool\n"
        "read_pool.start()\n"
        "print(read_pool._pool.submit(os.getpid).result(60), flush=True)\n"
        "time.sleep(600)\n"
    )

    def test_a_worker_leaves_when_its_web_process_is_killed(self):
        web = subprocess.Popen([sys.executable, "-c", self.STAND_IN], cwd=ROOT,
                               stdout=subprocess.PIPE, text=True)
        self.addCleanup(web.wait, 10)
        self.addCleanup(web.kill)
        self.addCleanup(web.stdout.close)
        worker = _Watched(int(web.stdout.readline()))
        self.addCleanup(worker.end)
        self.assertFalse(worker.gone_within(0.5))
        web.kill()
        web.wait(10)
        self.assertTrue(worker.gone_within(15), "the worker outlived its web process")


if __name__ == "__main__":
    unittest.main()
