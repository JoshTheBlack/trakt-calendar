"""app/after_response.py — work a request owes, started once its answer is sent.

A stale calendar week's refill used to start while the request that noticed it
was still building its page, and competed with it. Held until the response has
gone, it competes with nothing anybody is waiting for; outside a request nobody
is waiting, and it starts at once.
"""
from __future__ import annotations

import asyncio
import unittest
from unittest.mock import patch

from app import after_response


def _run(app) -> list[str]:
    """Drive `app` through the middleware as a server would, recording when the
    last byte of its response went out."""
    events: list[str] = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        if message["type"] == "http.response.body" and not message.get("more_body"):
            events.append("sent")

    asyncio.run(after_response.AfterResponseMiddleware(app(events))(
        {"type": "http", "method": "GET", "path": "/"}, receive, send))
    return events


def _answering(owe):
    def app(events):
        async def inner(scope, receive, send):
            owe(events)
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"ok"})
            events.append("answered")
        return inner
    return app


class WorkOwedAfterAResponseTests(unittest.TestCase):

    def test_outside_a_request_it_starts_at_once(self):
        ran: list[int] = []
        self.assertFalse(after_response.later(lambda: ran.append(1)))
        self.assertEqual(ran, [1])

    def test_inside_one_it_starts_once_the_response_has_been_sent(self):
        events = _run(_answering(lambda events: after_response.later(
            lambda: events.append("started"))))
        self.assertEqual(events, ["sent", "answered", "started"])

    def test_what_it_starts_defers_nothing_further(self):
        """Started with nobody waiting, anything it owes in turn starts at once
        rather than into a list nobody reads again."""
        def owe(events):
            after_response.later(lambda: after_response.later(
                lambda: events.append("started in turn")))
        self.assertEqual(_run(_answering(owe)), ["sent", "answered", "started in turn"])

    def test_one_piece_failing_costs_the_others_nothing(self):
        def owe(events):
            after_response.later(lambda: 1 / 0)
            after_response.later(lambda: events.append("started"))
        with self.assertLogs("app.after_response", "ERROR"):
            self.assertEqual(_run(_answering(owe)), ["sent", "answered", "started"])

    def test_a_request_that_failed_still_starts_what_it_owed(self):
        """A refill a failed page noticed is still owed: the next page draws the
        same stale week."""
        def app(events):
            async def inner(scope, receive, send):
                after_response.later(lambda: events.append("started"))
                raise RuntimeError("the page failed")
            return inner
        events: list[str] = []
        with self.assertRaises(RuntimeError):
            asyncio.run(after_response.AfterResponseMiddleware(app(events))(
                {"type": "http"}, None, None))
        self.assertEqual(events, ["started"])


class WhatARequestStartsBehindItStartsOnceItHasAnsweredTests(unittest.TestCase):
    """Every task a request starts BEHIND itself — work the page does not wait
    for — is started once the page's answer has been sent, so none of it runs
    beside the page that noticed it was owed. Each starter is called while the
    request is being answered, its work replaced by nothing, and every task the
    loop is asked to create is recorded against the moment the answer went."""

    def _order(self, start) -> list[str]:
        """What happened first and second once `start` was called mid-answer:
        ["sent", "task"] when the task waited for the answer. (asyncio.run's
        own shutdown makes tasks of its own after those.)"""
        def app(events):
            async def inner(scope, receive, send):
                loop = asyncio.get_running_loop()

                def factory(loop, coro, **kwargs):
                    events.append("task")
                    return asyncio.Task(coro, loop=loop, **kwargs)

                loop.set_task_factory(factory)
                start(events)
                await send({"type": "http.response.start", "status": 200, "headers": []})
                await send({"type": "http.response.body", "body": b"ok"})
            return inner
        return _run(app)[:2]

    async def _nothing(self, *args, **kwargs):
        return None

    def test_a_network_logos_warm_up(self):
        from app.config import Settings
        from app.media import logos
        with patch.object(logos, "warm", self._nothing), patch.object(logos, "_warming", None):
            self.assertEqual(self._order(
                lambda events: logos.warm_later(Settings(), {"BBC One": (1,)})), ["sent", "task"])

    def test_a_viewed_pages_refresh(self):
        from app.config import Settings
        from app.metadata import refresh

        def started(work):
            work.close()
            asyncio.get_running_loop().create_task(self._nothing())

        with patch.object(refresh, "start", started):
            self.assertEqual(self._order(lambda events: refresh.viewed(
                Settings(), [refresh.Wanted("show", {"tmdb": 1})], budget=1)), ["sent", "task"])

    def test_the_enrichment_drain_a_fill_asks_for(self):
        from app.calendar import enrich
        from app.config import Settings
        with patch.object(enrich, "run_drain", self._nothing):
            self.assertEqual(self._order(lambda events: enrich.schedule_drain(Settings())),
                             ["sent", "task"])

    def test_a_lapsed_library_read(self):
        from app.integrations import routes as integrations
        with patch.object(integrations, "_read_all", self._nothing), \
                patch.object(integrations, "_refresh_task", None), \
                patch.dict(integrations.LIBRARY_CACHE, {"_ts": 0}):
            self.assertEqual(self._order(lambda events: integrations.library_snapshot()),
                             ["sent", "task"])

    def test_a_share_pages_poster_warm(self):
        from types import SimpleNamespace

        from app.calendar import share_routes
        from app.config import Settings
        view = SimpleNamespace(endpoint=SimpleNamespace(key="shows"), year=2026, month=10)
        with patch.object(share_routes, "_warm_job", self._nothing), \
                patch.object(share_routes, "select_tile_items", lambda visible: visible), \
                patch.object(share_routes, "_poster_refs", lambda items: [("item", "ref")]), \
                patch.object(share_routes, "_warming", set()):
            self.assertEqual(self._order(lambda events: share_routes._spawn_poster_warm(
                view, 1, [], Settings(), ("tmdb",))), ["sent", "task"])

    def test_tmdbs_season_premieres_worker(self):
        from datetime import date

        from app.config import Settings
        from app.providers.tmdb import premieres
        with patch.object(premieres, "_work_through", self._nothing), \
                patch.object(premieres, "_waiting", {}), \
                patch.object(premieres, "_worker", None), patch.object(premieres, "_loop", None):
            self.assertEqual(self._order(lambda events: premieres.work(
                Settings(), premieres.Span.of(date(2026, 10, 5), 7))), ["sent", "task"])

    def test_a_pending_answers_follow_up(self):
        from datetime import date

        from app.calendar import cache
        from app.config import Settings
        from app.endpoints import get_endpoint
        start = date(2026, 10, 5)
        held = {("shows", start.isoformat(), "tmdb"): cache._Pending(None, 0.0, None)}
        with patch.object(cache, "_follow_up", self._nothing), \
                patch.object(cache, "_pending", held):
            self.assertEqual(self._order(lambda events: cache._follow_pending(
                get_endpoint("shows"), Settings(), start)), ["sent", "task"])
