"""The calls the sign-in flows make to Trakt and plex.tv, answered by a fake
transport rather than patched out.

Every flow test elsewhere replaces these functions whole, which is right for a
test about what a flow DOES with an answer and leaves the functions themselves
unread: which URL each one posts, what it sends, and — for the device poll —
which of Trakt's seven documented statuses becomes which exception. A poll that
read 410 as "still waiting" would spin until its own deadline instead of telling
the operator to start over, and nothing in the flow tests would notice.

WHY httpx.MockTransport HERE. What is asserted is the request as it leaves and
the response as the function reads it, and a real client over a mock transport
is the whole httpx path with only the socket missing. The pool's client is
swapped for one per test, never closed by the code under test (pools are shared).
"""
from __future__ import annotations

import asyncio
import json
import unittest
from unittest import mock

import httpx

from app.auth import plex, trakt as trakt_auth


class _Recorder:
    """A MockTransport handler answering from a fixed status and body, and
    keeping every request it saw."""

    def __init__(self, status: int = 200, body=None):
        self.status = status
        self.body = {} if body is None else body
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return httpx.Response(self.status, json=self.body)

    @property
    def sent(self) -> dict:
        return json.loads(self.requests[-1].content or b"{}")


def _on_pool(pool, handler):
    """Hand `pool`'s callers a client over `handler` for the length of a test.

    Patched on the CLASS, because a Pool has __slots__ and its instances take no
    attribute; every pool answering with this client is harmless inside a test
    that calls one function."""
    async def _run(coro_fn):
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        try:
            with mock.patch.object(type(pool), "client", lambda self: client):
                return await coro_fn()
        finally:
            await client.aclose()
    return _run


class TraktTokenCallTests(unittest.TestCase):
    def _call(self, handler, coro_fn):
        return asyncio.run(_on_pool(trakt_auth.POOL, handler)(coro_fn))

    def test_exchanging_a_code_posts_it_with_the_registered_redirect(self):
        rec = _Recorder(body={"access_token": "a", "refresh_token": "r"})
        token = self._call(rec, lambda: trakt_auth.exchange_code(
            "cid", "secret", "the-code", "https://shows.example.com"))
        self.assertEqual(token["access_token"], "a")
        self.assertEqual(str(rec.requests[-1].url), trakt_auth.TOKEN_URL)
        self.assertEqual(rec.sent, {
            "code": "the-code", "client_id": "cid", "client_secret": "secret",
            "redirect_uri": trakt_auth.redirect_uri("https://shows.example.com"),
            "grant_type": "authorization_code"})

    def test_a_refused_exchange_raises_rather_than_reading_as_a_token(self):
        with self.assertRaises(httpx.HTTPStatusError):
            self._call(_Recorder(status=401), lambda: trakt_auth.exchange_code(
                "cid", "secret", "bad", "https://shows.example.com"))

    def test_refreshing_spends_the_refresh_token(self):
        rec = _Recorder(body={"access_token": "new", "refresh_token": "next"})
        token = self._call(rec, lambda: trakt_auth.refresh_access_token("cid", "secret", "old"))
        self.assertEqual(token["refresh_token"], "next")
        self.assertEqual(rec.sent["grant_type"], "refresh_token")
        self.assertEqual(rec.sent["refresh_token"], "old")

    def test_revoking_names_the_token_and_raises_on_failure(self):
        rec = _Recorder()
        self._call(rec, lambda: trakt_auth.revoke_token("cid", "secret", "tok"))
        self.assertEqual(str(rec.requests[-1].url), trakt_auth.REVOKE_URL)
        self.assertEqual(rec.sent["token"], "tok")
        # An unlink that could not reach Trakt has to be able to say so.
        with self.assertRaises(httpx.HTTPStatusError):
            self._call(_Recorder(status=503),
                       lambda: trakt_auth.revoke_token("cid", "secret", "tok"))

    def test_a_device_session_starts_with_the_client_id_alone(self):
        rec = _Recorder(body={"device_code": "d", "user_code": "ABCD", "interval": 5})
        payload = self._call(rec, lambda: trakt_auth.request_device_code("cid"))
        self.assertEqual(payload["user_code"], "ABCD")
        self.assertEqual(rec.sent, {"client_id": "cid"})


class DevicePollTests(unittest.TestCase):
    """Each documented status becomes the one exception the caller acts on."""

    def _poll(self, status: int, body=None):
        rec = _Recorder(status=status, body=body)
        return asyncio.run(_on_pool(trakt_auth.POOL, rec)(
            lambda: trakt_auth.poll_device_token("cid", "secret", "dev")))

    def test_an_approved_code_answers_with_the_token(self):
        self.assertEqual(self._poll(200, {"access_token": "a"})["access_token"], "a")

    def test_every_other_documented_status_names_what_to_do_next(self):
        for status, exc in ((400, trakt_auth.DevicePending),
                            (404, trakt_auth.DeviceExpired),
                            (409, trakt_auth.DeviceDenied),
                            (410, trakt_auth.DeviceExpired),
                            (418, trakt_auth.DeviceDenied),
                            (429, trakt_auth.DeviceSlowDown)):
            with self.subTest(status=status), self.assertRaises(exc):
                self._poll(status)

    def test_an_undocumented_status_is_an_error_not_a_wait(self):
        with self.assertRaises(httpx.HTTPStatusError):
            self._poll(500)


class PlexAccountTests(unittest.TestCase):
    def _fetch(self, handler):
        return asyncio.run(_on_pool(plex.POOL, handler)(
            lambda: plex.fetch_account("plex-token", "client-1")))

    def test_the_numeric_id_is_the_identity_and_the_name_is_display(self):
        rec = _Recorder(body={"id": 4242, "username": "josh", "thumb": "https://plex.tv/a.png"})
        account = self._fetch(rec)
        self.assertEqual(account, {"id": 4242, "name": "josh", "avatar": "https://plex.tv/a.png"})
        self.assertEqual(rec.requests[-1].headers["X-Plex-Token"], "plex-token")

    def test_every_failure_is_one_lookup_error(self):
        """A missing or non-numeric id, a refusal, a body that is not JSON and a
        connection that never answered all raise the one error the flow handles."""
        def not_json(request):
            return httpx.Response(200, content=b"<html>")

        def unreachable(request):
            raise httpx.ConnectError("down", request=request)

        for handler in (_Recorder(body={"username": "josh"}),
                        _Recorder(body={"id": "4242"}),
                        _Recorder(status=401), not_json, unreachable):
            with self.subTest(handler=handler), self.assertRaises(plex.AccountLookupError):
                self._fetch(handler)
