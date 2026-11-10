"""app/perftrace.py's request breakdown, and the write line app/db.py adds to it.

THE PROMISES: a slow request's line says how much of it no span covers, counting
overlapping and nested spans once; a block timed without a span can still be
put into the breakdown (`attribute`) and logged (`report`); a step taken many
times — every read — is one summed entry that still counts toward what is
covered (`tally`); and every write says
how long it waited for its turn and how long the turn took, under the request
that made it, as one line naming the writer by its body or its statement and
never by its parameters.
"""
from __future__ import annotations

import asyncio
import logging
import unittest
import unittest.mock

from fastapi import Request  # module level: the annotations are strings (future import)

from app import db, perftrace
from tests.support import DatabaseTestCase


def _request() -> perftrace._Request:
    return perftrace._Request("r00001", "GET", "/calendar")


class TheUnspannedTimeTests(unittest.TestCase):

    def test_nothing_spanned_is_the_whole_request(self):
        req = _request()
        self.assertAlmostEqual(req.unspanned_ms(req.t0 + 2.0), 2000.0, places=3)

    def test_nested_and_overlapping_spans_count_once(self):
        req = _request()
        t0 = req.t0
        # 0-1 s outer, 0.2-0.6 s nested inside it, 0.8-1.4 s overlapping its end,
        # 2-2.5 s apart: covered 1.4 + 0.5 of 3 s.
        req.record("outer", 1000.0, t0 + 1.0)
        req.record("inner", 400.0, t0 + 0.6)
        req.record("overlap", 600.0, t0 + 1.4)
        req.record("apart", 500.0, t0 + 2.5)
        self.assertAlmostEqual(req.unspanned_ms(t0 + 3.0), 1100.0, places=3)

    def test_the_breakdown_names_the_gap(self):
        req = _request()
        req.record("calendar.read_month", 3000.0, req.t0 + 3.0)
        line = req.breakdown(req.t0 + 13.0)
        self.assertIn("calendar.read_month=3000ms", line)
        self.assertIn("unspanned=10000ms", line)

    def test_the_longest_gap_is_placed_between_what_bounds_it(self):
        req = _request()
        t0 = req.t0
        req.record("query.titles", 100.0, t0 + 0.2)
        req.tally("db.run", 100.0, t0 + 0.3)
        req.record("templating.drawn", 100.0, t0 + 2.4)
        line = req.breakdown(t0 + 2.5)
        self.assertIn("unspanned=2200ms (longest 2000ms, after db.run, before templating.drawn)",
                      line)

    def test_a_gap_at_either_edge_is_placed_there(self):
        req = _request()
        req.record("calendar.read_month", 100.0, req.t0 + 1.1)
        self.assertIn("after the start, before calendar.read_month", req.breakdown(req.t0 + 1.2))
        req = _request()
        req.record("calendar.read_month", 100.0, req.t0 + 0.1)
        self.assertIn("after calendar.read_month, before the end", req.breakdown(req.t0 + 1.2))

    def test_a_short_gap_is_not_placed(self):
        req = _request()
        req.record("calendar.read_month", 1000.0, req.t0 + 1.01)
        line = req.breakdown(req.t0 + 1.02)
        self.assertIn("unspanned=20ms", line)
        self.assertNotIn("longest", line)

    def test_spans_past_the_cap_are_counted_not_kept(self):
        req = _request()
        for at in range(perftrace.MAX_SPANS_PER_REQUEST + 3):
            req.record(f"s{at}", 1.0, req.t0 + 0.001 * (at + 1))
        self.assertEqual(len(req.spans), perftrace.MAX_SPANS_PER_REQUEST)
        self.assertIn("+3 spans", req.breakdown())


class ATalliedStepTests(unittest.TestCase):
    """A step a request takes many times is one entry in its breakdown, summed,
    and every time it ran still counts toward what the request accounts for."""

    def test_its_times_are_one_entry(self):
        req = _request()
        for n in range(5):
            req.tally("db.run", 100.0, req.t0 + 0.1 * (n + 1))
        line = req.breakdown(req.t0 + 1.0)
        self.assertIn("db.run=500ms×5", line)
        self.assertEqual(line.count("db.run="), 1)
        self.assertEqual(req.spans, [])

    def test_it_counts_toward_what_is_covered_with_the_spans(self):
        req = _request()
        t0 = req.t0
        req.record("render", 500.0, t0 + 0.5)
        # 0.4-0.7 s overlaps the span's end; 1.0-1.2 s stands apart.
        req.tally("db.run", 300.0, t0 + 0.7)
        req.tally("db.run", 200.0, t0 + 1.2)
        self.assertAlmostEqual(req.unspanned_ms(t0 + 2.0), 1100.0, places=3)

    def test_past_the_cap_it_is_still_summed(self):
        req = _request()
        for n in range(perftrace.MAX_TALLIED_PER_REQUEST + 4):
            req.tally("db.run", 1.0, req.t0 + 0.000001 * (n + 1))
        self.assertEqual(len(req.tallied), perftrace.MAX_TALLIED_PER_REQUEST)
        self.assertEqual(req.tallies["db.run"], [perftrace.MAX_TALLIED_PER_REQUEST + 4.0,
                                                 perftrace.MAX_TALLIED_PER_REQUEST + 4])

    def test_a_tally_alone_is_a_breakdown(self):
        req = _request()
        req.tally("db.run", 40.0, req.t0 + 0.04)
        self.assertNotEqual(req.breakdown(req.t0 + 0.1), "(no spans)")

    def test_outside_a_request_it_does_nothing(self):
        perftrace.tally("db.run", 5.0, ended=0.0)


class AttributeAndReportTests(unittest.TestCase):

    def test_attribute_lands_in_the_request_and_logs_nothing(self):
        req = _request()
        token = perftrace._current.set(req)
        try:
            with self.assertNoLogs("app.perf", level="DEBUG"):
                perftrace.attribute("db.queue", 12.0)
        finally:
            perftrace._current.reset(token)
        self.assertEqual([(label, dt) for label, dt, _ended in req.spans], [("db.queue", 12.0)])

    def test_report_is_debug_until_slow(self):
        with self.assertLogs("app.perf", level="DEBUG") as logs:
            perftrace.report("db.write", 3.0, writer="x")
            perftrace.report("db.write", perftrace.SLOW_SPAN_MS + 1, writer="x")
        self.assertEqual([record.levelno for record in logs.records],
                         [logging.DEBUG, logging.WARNING])
        self.assertIn("writer=x", logs.records[0].getMessage())

    def test_report_spells_nothing_when_its_level_is_off(self):
        class Loud:
            def __str__(self):
                raise AssertionError("spelled a field nobody prints")
        logger = logging.getLogger("app.perf")
        before = logger.level
        logger.setLevel(logging.INFO)
        try:
            perftrace.report("quiet", 1.0, field=Loud())
        finally:
            logger.setLevel(before)


class EveryWriteSaysWhereItsTimeWentTests(DatabaseTestCase):

    def _in_request(self, coro):
        req = _request()

        async def run():
            token = perftrace._current.set(req)
            try:
                return await coro
            finally:
                perftrace._current.reset(token)
        return req, asyncio.run(run())

    def test_an_execute_reports_its_wait_and_its_turn(self):
        with self.assertLogs("app.perf", level="DEBUG") as logs:
            req, _ = self._in_request(db.execute(
                "INSERT INTO app_meta (key, value) VALUES (?, ?)", ("probe", "secret-value")))
        labels = [label for label, _dt, _ended in req.spans]
        self.assertEqual(labels, ["db.queue", "db.turn"])
        (line,) = [r.getMessage() for r in logs.records if "db.write" in r.getMessage()]
        self.assertIn("[r00001]", line)
        self.assertIn("writer=INSERT app_meta", line)
        self.assertIn("queued=", line)
        self.assertNotIn("secret-value", line)

    def test_a_transaction_is_named_by_its_body(self):
        def stamp(conn):
            conn.execute("INSERT INTO app_meta (key, value) VALUES ('t', '1')")
        with self.assertLogs("app.perf", level="DEBUG") as logs:
            self._in_request(db.transaction(stamp))
        self.assertTrue(any("writer=tests.kernel.test_perftrace_breakdown" in r.getMessage()
                            and "stamp" in r.getMessage() for r in logs.records))

    def test_a_read_is_tallied_and_a_writes_statement_is_not(self):
        """A read's wait for a thread is the request's to account for; a write's
        own statement is already its turn."""
        async def both():
            await db.run(lambda conn: conn.execute("SELECT 1").fetchone())
            await db.fetch_one("SELECT 2")
            await db.execute("INSERT INTO app_meta (key, value) VALUES ('w', '1')")
        req, _ = self._in_request(both())
        self.assertEqual(list(req.tallies), ["db.run"])
        self.assertEqual(req.tallies["db.run"][1], 2)
        self.assertEqual([label for label, _dt, _ended in req.spans], ["db.queue", "db.turn"])

    def test_a_write_behind_another_reports_the_wait(self):
        async def both():
            gate = asyncio.Event()

            def held(conn):
                conn.execute("INSERT INTO app_meta (key, value) VALUES ('a', '1')")

            async def first():
                async with db._writer_queue():
                    gate.set()
                    await asyncio.sleep(0.2)

            holder = asyncio.create_task(first())
            await gate.wait()
            await db.transaction(held)
            await holder
        req, _ = self._in_request(both())
        queued = dict((label, dt) for label, dt, _ended in req.spans)["db.queue"]
        self.assertGreaterEqual(queued, 150.0)


class TheCrossingIsTimedTests(unittest.TestCase):
    """The middleware a request crosses on its way to the routes, each read of
    its body and its answer's way out are in its breakdown, from a marker that
    sits inside every middleware."""

    def test_the_marker_is_innermost_and_the_timer_outside_the_gates(self):
        from app.main import app
        names = [entry.cls.__name__ if hasattr(entry.cls, "__name__") else repr(entry.cls)
                 for entry in app.user_middleware]
        self.assertEqual(names[-1], "_StackCrossing")
        self.assertLess(names.index("RequestTimingMiddleware"), names.index("BaseHTTPMiddleware"))

    def test_a_request_through_a_layer_names_all_three(self):
        from fastapi import FastAPI
        from fastapi.responses import PlainTextResponse
        from fastapi.testclient import TestClient

        app = FastAPI()

        @app.post("/echo")
        async def echo(request: Request):
            return PlainTextResponse(str(len(await request.body())))

        @app.middleware("http")
        async def layer(request, call_next):
            return await call_next(request)

        perftrace.install(app)
        with unittest.mock.patch.object(perftrace, "SLOW_REQUEST_MS", 0.0), \
                self.assertLogs("app.perf", level="WARNING") as logs, TestClient(app) as client:
            self.assertEqual(client.post("/echo", content=b"12345").text, "5")
        (line,) = [r.getMessage() for r in logs.records if "SLOW POST /echo" in r.getMessage()]
        for label in ("asgi.in=", "asgi.receive=", "asgi.out="):
            self.assertIn(label, line)


class StatementNamesTests(unittest.TestCase):

    def test_a_statement_is_named_by_its_verb_and_table(self):
        for sql, name in (
                ("INSERT OR REPLACE INTO api_cache(key, value) VALUES (?, ?)", "INSERT api_cache"),
                ("UPDATE sessions SET last_seen = ?", "UPDATE sessions"),
                ("UPDATE OR IGNORE titles SET x = 1 FROM json_each(?)", "UPDATE titles"),
                ("DELETE FROM api_cache WHERE key = ?", "DELETE api_cache"),
                ("WITH due AS (SELECT 1) INSERT INTO titles SELECT * FROM due", "WITH titles"),
                ("VACUUM", "VACUUM"), ("   ", "?")):
            with self.subTest(sql=sql):
                self.assertEqual(db._statement(sql), name)


if __name__ == "__main__":
    unittest.main()
