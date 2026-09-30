"""The one hook: every request to the tracker ends with a watchlist pass, after
its answer, for a viewer who switched a service on — and a request that put a
show on the tracker leaves that show owed a write.

The pass itself is tests/distrakt/test_watchlist_writes.py's. Here `nudge` is
recorded rather than run, because what is asserted is that the request ASKED
for a pass and did not wait for one.
"""
from __future__ import annotations

import asyncio
import unittest
from unittest.mock import patch

from fastapi.routing import APIRoute

from app.distrakt import routes as distrakt_routes, watchlist_writes as ww
from app.providers.base import Source
from tests.distrakt.test_rewatch import KEY, _RosterAddCase

SIMKL = str(Source.SIMKL)


class EveryTrackerRouteEndsWithAPassTests(unittest.TestCase):
    """The hook is on the ROUTER, so a route cannot be written without it."""

    def test_every_route_the_tracker_registers_carries_it(self):
        tracker = [route for route in distrakt_routes.router.routes
                   if isinstance(route, APIRoute)]
        self.assertGreater(len(tracker), 30)
        for route in tracker:
            with self.subTest(path=route.path):
                self.assertIn(distrakt_routes._owe_watchlist_pass,
                              [dep.dependency for dep in route.dependencies])


class ARequestAsksForAPassTests(_RosterAddCase):

    def setUp(self):
        super().setUp()
        self.nudged: list[int] = []
        started = patch.object(ww, "nudge", self.nudged.append)
        started.start()
        self.addCleanup(started.stop)
        asyncio.run(ww.switch(self.user_id, SIMKL, True))

    def owed(self) -> set[str]:
        async def _owed():
            held = await ww.rows(self.user_id, SIMKL)
            return {key for key in await ww.on_tracker(self.user_id)
                    if key not in held or held[key].state in ww.OWED_STATES}
        return asyncio.run(_owed())

    def test_a_search_add_asks_for_a_pass_and_leaves_the_show_owed(self):
        self.assertEqual(self.owed(), set())
        answer = self._search_add()
        self.assertEqual(answer.status_code, 200)
        self.assertEqual(self.nudged, [self.user_id])
        self.assertEqual(self.owed(), {str(KEY)})

    def test_a_history_add_asks_for_a_pass_and_leaves_the_show_owed(self):
        answer = self._prompt_add()
        self.assertEqual(answer.status_code, 200)
        self.assertEqual(self.nudged, [self.user_id])
        self.assertEqual(self.owed(), {str(KEY)})

    def test_a_read_asks_too(self):
        """Opening a month can seed the viewer's list, so reads are not exempt;
        a pass with nothing owed costs two queries and no call."""
        self.client.get("/distrakt/fragments/day-label?day=2026-09-01")
        self.assertEqual(self.nudged, [self.user_id])

    def test_a_viewer_who_switched_nothing_on_starts_nothing(self):
        asyncio.run(ww.switch(self.user_id, SIMKL, False))
        self._search_add()
        self.assertEqual(self.nudged, [])

    def test_a_refused_request_starts_nothing(self):
        self.sign_out()
        answer = self.client.post("/api/distrakt/refresh", json={})
        self.assertNotEqual(answer.status_code, 200)
        self.assertEqual(self.nudged, [])
