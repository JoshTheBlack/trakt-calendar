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
from datetime import date
from types import SimpleNamespace
from unittest.mock import patch
from zoneinfo import ZoneInfo

import httpx
import pytest

from app.endpoints import get_endpoint
from app.providers.trakt import TraktError
from app.providers.trakt import calendar as trakt_calendar
from app.providers.trakt import sync as trakt_sync
from app.providers.trakt import transport
from app.providers.trakt.detail import _cast_from, _episodes_from

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

class _Unreadable:
    """Stands in for a body that is not JSON at all."""

class _WindowResponse:
    def __init__(self, body, status, headers):
        self._body = body
        self.status_code = status
        self.headers = headers

    def json(self):
        if isinstance(self._body, _Unreadable):
            raise ValueError("not json")
        return self._body

class _CaptureClient:
    """A client stand-in that records the request it was given and replies with a
    canned body — what the window fetch builds is half of what it is asked to do."""

    def __init__(self, body, status: int = 200, response_headers: dict | None = None):
        self._body = body
        self._status = status
        self._response_headers = response_headers or {}
        self.url = None
        self.sent_headers: dict = {}

    async def get(self, url, headers=None, timeout=None):
        self.url = url
        self.sent_headers = headers or {}
        return _WindowResponse(self._body, self._status, self._response_headers)


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
            result = asyncio.run(transport.cached_get(client, SETTINGS, "x", {}, **kwargs))
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


def _cached_get(*answers):
    """A stand-in for transport.cached_get that serves `answers` in order.

    AN ANSWER THAT IS AN EXCEPTION IS SERVED THE WAY THE REAL FUNCTION SERVES A
    FAILURE: raised when the caller passed `raise_errors=True`, and handed back as
    None otherwise. That flag is the whole mechanism under test — a double that
    raised regardless would pass just as happily against the swallowing code these
    tests exist to forbid, which would make every one of them worthless.
    """
    served = list(answers)
    calls: list[dict] = []

    async def _get(_client, _settings, path, params=None, **kwargs):
        calls.append({"path": path, "params": params, **kwargs})
        answer = served.pop(0) if len(served) > 1 else served[0]
        if isinstance(answer, Exception):
            if kwargs.get("raise_errors"):
                raise answer
            return None
        return answer

    _get.calls = calls
    return _get


class TestThePrivateReadsRefuseToLookEmpty:
    """A FAILURE IS NEVER NORMALIZED INTO AN EMPTY ANSWER, asked of the three
    reads a person's own viewing comes through.

    Each of these was a swallowed refusal, and each swallowed one differently:
    the beacon reported "nothing has changed", the history reported "nothing was
    watched", and the progress fan-out reported "this show has been watched by
    nobody" once per show. All three read as ordinary answers, all three are
    acted on, and the third overwrites stored counts — so with one refused
    credential the page rendered healthy on a reload and unavailable on a
    refresh, with nothing anywhere naming the service that had stopped
    answering.
    """

    def test_a_refused_beacon_raises_rather_than_answering_empty(self):
        """An empty beacon is not a missing one. It claims all four stamps are
        absent, which compares EQUAL to a stored empty one and gates the next
        sync as unchanged — so a refused token has the source report itself up to
        date for as long as it stays refused."""
        get = _cached_get(TraktError("nope", 401))
        with patch.object(transport, "cached_get", get):
            with pytest.raises(TraktError):
                asyncio.run(trakt_sync.fetch_last_activities(SETTINGS))
        assert get.calls[0]["raise_errors"] is True

    def test_a_beacon_that_answered_with_nothing_is_still_an_empty_blob(self):
        """The distinction the fix rests on: Trakt answering with a body that
        held nothing is a real, successful, empty answer."""
        with patch.object(transport, "cached_get", _cached_get(None)):
            assert asyncio.run(trakt_sync.fetch_last_activities(SETTINGS)) == {}

    def test_a_show_that_could_not_be_read_is_absent_rather_than_empty(self):
        """The one that overwrites data. An empty map against an id means "this
        person has watched none of it" and retires the stored seasons; a show
        whose own call failed has said nothing, so its id is left out entirely
        and the caller keeps what it had."""
        with patch.object(transport, "cached_get", _cached_get(TraktError("gone", 404))), \
             patch.object(transport, "shared_client", lambda: None):
            assert asyncio.run(trakt_sync.fetch_progress_details(SETTINGS, [7])) == {}

    def test_a_show_with_nothing_watched_is_present_and_empty(self):
        """The other side of the same rule, and the reason absence had to be
        reserved: a real "none of it" still has to reach the caller."""
        with patch.object(transport, "cached_get", _cached_get({"seasons": []})), \
             patch.object(transport, "shared_client", lambda: None):
            assert asyncio.run(trakt_sync.fetch_progress_details(SETTINGS, [7])) == {7: {}}

    def test_a_refused_credential_is_not_one_show_failing(self):
        """It is true of every request this token will make, so tolerating it per
        show composes a whole roster of refusals into a library nobody watched."""
        with patch.object(transport, "cached_get", _cached_get(TraktError("nope", 401))), \
             patch.object(transport, "shared_client", lambda: None):
            with pytest.raises(TraktError):
                asyncio.run(trakt_sync.fetch_progress_details(SETTINGS, [7, 8]))

    def test_a_403_is_a_credential_failure_too(self):
        """Both say the request was refused over WHO asked rather than WHAT was
        asked for, so the same token on any other path gets the same answer."""
        assert transport.is_credential_failure(TraktError("x", 403))
        assert not transport.is_credential_failure(TraktError("x", 404))

    def _history(self, response):
        async def _send(_client, _method, _url, **_kwargs):
            return response
        with patch.object(transport, "send", _send), \
             patch.object(transport, "shared_client", lambda: None):
            return asyncio.run(trakt_sync.fetch_history(SETTINGS))

    def test_a_refused_history_page_raises_rather_than_ending_the_sweep(self):
        """It used to log, stop, and return what had arrived — so a refused read
        was reported as "0 event(s) over 1 page(s)", and the caller advanced its
        cursor past a window nothing had read."""
        with pytest.raises(TraktError):
            self._history(_response(401))

    def test_a_page_that_answered_with_no_events_ends_the_sweep_normally(self):
        """Trakt saying there is nothing more, which is the ordinary answer for
        an account that has watched nothing since the cursor."""
        assert self._history(_response(200, body="[]")) == []


class TestFetchWindow:
    """The one place the app asks Trakt what airs.

    It had no direct coverage while it was two private copies assembled at their
    call sites — each was only ever reached through a whole calendar fetch or a
    whole cache fill, so its own refusals were only ever asserted second-hand.
    """

    def _fetch(self, client, headers=None):
        with patch.object(transport, "shared_client", return_value=client):
            return asyncio.run(trakt_calendar.fetch_window(
                get_endpoint("shows"), SETTINGS, date(2026, 7, 6), 7,
            ))

    def test_the_window_is_asked_for_by_start_date_and_day_count(self):
        client = _CaptureClient(body=[])
        self._fetch(client)
        assert "/calendars/all/shows/2026-07-06/7" in client.url
        assert "extended=full%2Cimages" in client.url

    def test_no_pagination_headers_are_sent(self):
        """Sending them switches some Trakt endpoints into a paginated shape.
        The calendar ignores them, so asking is a way to be silently truncated."""
        client = _CaptureClient(body=[])
        self._fetch(client)
        assert "X-Pagination-Page" not in client.sent_headers
        assert "X-Pagination-Limit" not in client.sent_headers

    def test_the_entries_come_back_untouched(self):
        """Raw, unfiltered and unnormalized: the two callers filter differently
        and one of them stores the result, so deciding here would make one wrong."""
        entries = [{"show": {"title": "A"}}, {"show": {"title": "B"}}]
        assert self._fetch(_CaptureClient(body=entries)) == entries

    def test_a_401_names_the_credentials_to_check(self):
        with pytest.raises(TraktError) as exc:
            self._fetch(_CaptureClient(body=[], status=401))
        assert exc.value.status == 401
        assert "Client ID" in str(exc.value)

    def test_any_other_non_200_carries_its_status(self):
        with pytest.raises(TraktError) as exc:
            self._fetch(_CaptureClient(body=[], status=503))
        assert exc.value.status == 503

    def test_an_unreadable_body_raises_rather_than_reading_as_empty(self):
        """An empty window and a broken response are different facts: the first
        gets cached as the answer, and the second must not be."""
        with pytest.raises(TraktError):
            self._fetch(_CaptureClient(body=_Unreadable()))

    def test_a_body_that_is_not_a_list_is_an_empty_window(self):
        """There is nothing to salvage from it and nothing to say about it."""
        assert self._fetch(_CaptureClient(body={"error": "nope"})) == []

    def test_pagination_headers_are_reported_rather_than_ignored(self):
        """If Trakt ever starts paginating the calendar, the symptom is a short
        window and no other sign of it — so the warning is the only tell."""
        client = _CaptureClient(body=[], response_headers={"x-pagination-page-count": "3"})
        with patch.object(trakt_calendar.logger, "warning") as warn:
            self._fetch(client)
        assert warn.called


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


class TestATokenlessCallStillGoesOut:
    """Trakt without a bearer, which is most of what this app asks it for.

    The public catalogue endpoints — a title's summary, its cast, a season's
    episode list, /search — authenticate with the `trakt-api-key` header, which
    carries the INSTANCE's client id. Only the per-person reads under /sync/ want
    an Authorization header at all.

    THE FAILURE THIS PINS was not a refusal from Trakt; it never reached Trakt.
    An account with no Trakt token produced the literal header value "Bearer ",
    the HTTP layer refused to put that on the wire, and every call the app made
    died locally — including the season episode counts, which are the same
    number for everybody and need no token. A whole roster rendered
    "unavailable" because of it.
    """

    def _headers(self, token):
        settings = SimpleNamespace(trakt_access_token=token, trakt_client_id="id",
                                   pagination_limit=100, cache_ttl_minutes=10)
        return transport.api_headers(settings)

    def test_a_token_is_still_sent_as_a_bearer(self):
        assert self._headers("token")["Authorization"] == "Bearer token"

    def test_no_token_means_no_authorization_header_at_all(self):
        """Omitted, not empty. An empty bearer is not an anonymous request."""
        assert "Authorization" not in self._headers("")

    def test_a_blank_token_is_no_token(self):
        """A pasted credential that is only whitespace is the same nothing, and
        it used to be the same illegal header."""
        assert "Authorization" not in self._headers("   ")

    def test_the_api_key_is_what_carries_a_tokenless_call(self):
        assert self._headers("")["trakt-api-key"] == "id"

    def test_the_headers_are_ones_the_wire_will_actually_accept(self):
        """THE REGRESSION, at the layer that raised it. h11 validates header
        values as it frames the request, and it is what rejected b'Bearer ' —
        so asserting the header is absent is only half the claim, and this is
        the other half. Building the same request with a token proves the check
        is real rather than vacuous.
        """
        import h11
        for token in ("", "token"):
            headers = [("host", "api.trakt.tv")] + list(self._headers(token).items())
            h11.Request(method="GET", target="/shows/1/seasons/2", headers=headers)

    def test_a_tokenless_get_comes_back_with_its_body(self):
        client = _CaptureClient(body={"aired_episodes": 8})
        settings = SimpleNamespace(trakt_access_token="", trakt_client_id="id",
                                   pagination_limit=100, cache_ttl_minutes=10)
        fake_cache = _FakeCache()
        with patch.object(transport, "cache", fake_cache):
            body = asyncio.run(transport.cached_get(client, settings, "shows/1", {}))
        assert body == {"aired_episodes": 8}
        assert "Authorization" not in client.sent_headers
        assert client.sent_headers["trakt-api-key"] == "id"
