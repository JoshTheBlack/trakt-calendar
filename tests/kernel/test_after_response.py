"""app/after_response.py — work a request owes, started once its answer is sent.

A stale calendar week's refill used to start while the request that noticed it
was still building its page, and competed with it. Held until the response has
gone, it competes with nothing anybody is waiting for; outside a request nobody
is waiting, and it starts at once.
"""
from __future__ import annotations

import asyncio
import unittest

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
