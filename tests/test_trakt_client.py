"""Unit tests for the pieces of the Trakt client that had no direct coverage
until they became separately reachable.

The transport's one-GET step is where "Trakt said no" is told apart from "Trakt
never answered", and the two are NOT interchangeable: the first is a legitimate
empty result and the second must never be allowed to look like one. The modal's
cast and episode builders are pure shape translation over two response shapes
Trakt still mixes.

No network — a stub client returns the responses.
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from zoneinfo import ZoneInfo

import httpx
import pytest

os.environ.setdefault("TRAKT_DATA_DIR", tempfile.mkdtemp(prefix="tns-trakt-client-test-"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.providers.trakt import TraktError  # noqa: E402
from app.providers.trakt import transport  # noqa: E402
from app.providers.trakt.detail import _cast_from, _episodes_from  # noqa: E402

SETTINGS = SimpleNamespace(
    trakt_access_token="token", trakt_client_id="id", pagination_limit=100,
    cache_ttl_minutes=10,
)


class _Client:
    """An httpx.AsyncClient stand-in that answers with one canned response, or
    raises the transport error it was given."""

    def __init__(self, response=None, error=None):
        self._response = response
        self._error = error

    async def get(self, url, headers=None, timeout=None):
        if self._error is not None:
            raise self._error
        return self._response


def _response(status: int, *, body: str = "{}") -> httpx.Response:
    return httpx.Response(status, text=body, request=httpx.Request("GET", "https://api.trakt.tv/x"))


def _fetch(client, raise_errors: bool = False):
    return asyncio.run(transport._fetch_json(
        client, SETTINGS, "https://api.trakt.tv/x", "x", fresh=False, raise_errors=raise_errors,
    ))


class TestFetchJson:
    def test_a_body_comes_back_parsed(self):
        assert _fetch(_Client(_response(200, body='{"ok": true}'))) == {"ok": True}

    def test_a_refused_call_is_none_by_default(self):
        """The historical contract: callers that treat None as "nothing there"
        depend on a 404 not raising at them."""
        assert _fetch(_Client(_response(404))) is None

    def test_raise_errors_turns_the_same_refusal_into_a_trakt_error(self):
        """What search and the season picker ask for, because a swallowed 401
        there was indistinguishable from a genuinely empty result."""
        with pytest.raises(TraktError) as exc:
            _fetch(_Client(_response(404)), raise_errors=True)
        assert exc.value.status == 404

    def test_a_401_says_which_credentials_to_check(self):
        with pytest.raises(TraktError) as exc:
            _fetch(_Client(_response(401)), raise_errors=True)
        assert exc.value.status == 401
        assert "Client ID" in str(exc.value)

    def test_an_unreadable_body_is_not_a_result(self):
        assert _fetch(_Client(_response(200, body="not json"))) is None

    def test_an_unreachable_trakt_raises_even_when_errors_are_swallowed(self):
        """THE ONE THAT MATTERS. A transport failure is not an answer, so it must
        never collapse into the None that means "Trakt says there's nothing" —
        that is how a season renders a false 0 episodes."""
        with pytest.raises(TraktError):
            _fetch(_Client(error=httpx.ConnectError("no route")), raise_errors=False)


class _FakeCache:
    """app.cache with the database taken out, so what got WRITTEN is observable.
    Every read misses, which is the state a fetch is made from."""

    def __init__(self):
        self.writes: list[tuple] = []

    async def get(self, url, ttl):
        return None

    async def get_stale(self, url):
        return None

    async def set(self, url, data):
        self.writes.append((url, data))


class TestCachedGetStoresOnlyRealAnswers:
    """The caching half, now that it is separable from the call."""

    def _run(self, client, **kwargs):
        fake_cache = _FakeCache()
        with patch.object(transport, "cache", fake_cache):
            result = asyncio.run(transport._cached_get(client, SETTINGS, "x", {}, **kwargs))
        return result, fake_cache.writes

    def test_a_good_response_is_stored(self):
        result, writes = self._run(_Client(_response(200, body='{"ok": true}')))
        assert result == {"ok": True}
        assert len(writes) == 1

    def test_a_refusal_stores_nothing(self):
        """A cached failure would be served back as though it were the answer
        for the whole TTL."""
        result, writes = self._run(_Client(_response(500)))
        assert result is None
        assert writes == []

    def test_a_private_response_is_never_stored(self):
        """The cache is keyed by URL and shared by the instance: two accounts
        asking about the same show send the identical key."""
        result, writes = self._run(_Client(_response(200, body='{"seasons": []}')), private=True)
        assert result == {"seasons": []}
        assert writes == []


class TestModalShaping:
    def test_cast_accepts_both_of_trakts_character_shapes(self):
        """`characters` (a list) is the newer response and `character` (a
        string) the older one; both still arrive, so both have to read."""
        cast = _cast_from({"cast": [
            {"person": {"name": "A"}, "character": "Solo"},
            {"person": {"name": "B"}, "characters": ["Duo", "Alias"]},
        ]})
        assert [c["character"] for c in cast] == ["Solo", "Duo"]

    def test_cast_is_capped_at_the_top_billed_sixteen(self):
        cast = _cast_from({"cast": [{"person": {"name": str(i)}} for i in range(40)]})
        assert len(cast) == 16

    def test_an_undated_episode_keeps_its_place_in_the_list(self):
        """An announced-but-unscheduled episode still exists. Dropping it would
        renumber the modal's list against the season it describes."""
        episodes = _episodes_from([
            {"number": 1, "title": "One", "first_aired": "2026-07-15T20:00:00.000Z"},
            {"number": 2, "title": "Two"},
            {"number": 3, "title": "Three", "first_aired": "not a date"},
        ], ZoneInfo("UTC"))
        assert [e["number"] for e in episodes] == [1, 2, 3]
        assert [bool(e["air_display"]) for e in episodes] == [True, False, False]

    def test_a_missing_season_response_is_an_empty_list_not_a_crash(self):
        """cache_only can leave the episodes lookup with nothing at all, and the
        modal renders around it."""
        assert _episodes_from(None, ZoneInfo("UTC")) == []
