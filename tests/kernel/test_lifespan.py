"""Start-up, the heartbeat and shutdown — the code every other test skips.

Every route test builds its TestClient WITHOUT entering it, so the app's lifespan
never runs under test: that keeps the heartbeat's network-bound jobs out of
tests that have nothing to do with them. The cost was that start-up itself —
the order its steps run in, the background tasks it starts and the pools it
closes — was exercised by nothing but a real boot. These enter it once, with
the heartbeat's jobs replaced, and drive one tick and one sweep directly.
"""
from __future__ import annotations

import asyncio
import unittest
from unittest import mock

from fastapi.testclient import TestClient

from app import auth, db, http_pool, main
from tests.support import ORIGIN, PASSWORD, migrated_db


class StartAndStopTests(unittest.TestCase):
    def setUp(self):
        migrated_db("lifespan")

    def tearDown(self):
        db.close_thread_connection()

    def test_the_app_starts_serves_and_closes_its_pools_on_the_way_out(self):
        started = asyncio.Event()

        async def heartbeat():
            # The real loop's first tick reaches Trakt and Simkl; what is asserted
            # here is that start-up STARTS it, not what one tick does.
            started.set()
            await asyncio.Event().wait()

        # The REAL close, watched: shutting down really closes every pool, and a
        # stub here would leave that code as unexercised as it was before.
        closed = mock.AsyncMock(wraps=http_pool.aclose_all)
        with mock.patch.object(main, "_heartbeat_loop", heartbeat), \
             mock.patch.object(http_pool, "aclose_all", closed), \
             mock.patch.object(main.authz, "log_undeclared_routes",
                               wraps=main.authz.log_undeclared_routes) as audit, \
             TestClient(main.app, base_url=ORIGIN, headers={"Origin": ORIGIN}) as client:
            self.assertEqual(client.get("/healthz").status_code, 200)
            self.assertTrue(started.is_set())
            # Every route the app registers is declared, so the start-up audit
            # that refuses undeclared ones finds nothing to name.
            audit.assert_called_once_with(main.app)
            self.assertEqual(main.authz.undeclared_routes(main.app), [])
        closed.assert_awaited_once()


class HeartbeatTickTests(unittest.TestCase):
    """One tick runs every job, and one job failing does not stop the rest —
    this loop is the only thing renewing the Trakt token and sweeping rows, so
    an escaping exception would end both for the life of the process."""

    def setUp(self):
        migrated_db("tick")

    def tearDown(self):
        db.close_thread_connection()

    def test_every_job_runs_even_when_one_raises(self):
        ran: list[str] = []

        def job(name, fail=False):
            async def run(*_args):
                ran.append(name)
                if fail:
                    raise RuntimeError(f"{name} broke")
            return run

        with mock.patch.object(main.integrations_routes, "refresh_integration_health",
                               job("health", fail=True)), \
             mock.patch.object(main.settings_routes, "maybe_refresh_trakt_token", job("token")), \
             mock.patch.object(main, "_sweep_auth_rows", job("sweep", fail=True)), \
             mock.patch.object(main.calendar_cache, "refresh_months", job("months")), \
             mock.patch.object(main.calendar_enrich, "run_drain", job("drain")):
            asyncio.run(main._heartbeat_tick())
        self.assertEqual(ran, ["health", "token", "sweep", "months", "drain"])


class SweepTests(unittest.TestCase):
    """The sweep, run for real over a database holding one row on each side of
    its clocks: what has expired goes and nothing else does."""

    def setUp(self):
        migrated_db("sweep")

    def tearDown(self):
        db.close_thread_connection()

    def _count(self, sql, params=()):
        return asyncio.run(db.fetch_value(sql, params))

    def test_expired_sessions_and_handshakes_go_and_live_ones_stay(self):
        from app.config import Settings
        user_id = asyncio.run(auth.create_user(username="sweeper", password=PASSWORD,
                                               settings=Settings()))
        live = asyncio.run(auth.create_session(user_id))
        dead = asyncio.run(auth.create_session(user_id))
        now = db.now()
        asyncio.run(db.execute("UPDATE sessions SET expires_at = ? WHERE id = ?", (now - 1, dead)))
        for state, expires in (("old", now - 1), ("fresh", now + 600)):
            asyncio.run(db.execute(
                "INSERT INTO auth_handshakes (state, provider, purpose, created_at, expires_at) "
                "VALUES (?, 'trakt', 'login', ?, ?)", (state, now - 700, expires)))

        asyncio.run(main._sweep_auth_rows())

        self.assertEqual(self._count("SELECT COUNT(*) FROM sessions WHERE id = ?", (live,)), 1)
        self.assertEqual(self._count("SELECT COUNT(*) FROM sessions WHERE id = ?", (dead,)), 0)
        self.assertEqual(
            asyncio.run(db.fetch_all("SELECT state FROM auth_handshakes"))[0]["state"], "fresh")
        self.assertEqual(self._count("SELECT COUNT(*) FROM auth_handshakes"), 1)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
