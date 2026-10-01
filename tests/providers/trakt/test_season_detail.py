"""`fetch_season_detail`, and the one distinction its answer has to keep.

A SEASON OF ZERO EPISODES IS A CLAIM, NOT A SHRUG. Every caller treats this
function's answer as measured — the roster's x/y, the bucket a row sits in, the
totals a month freezes — so a failure that came back as an empty season was a
fabricated number nobody downstream could tell from a real one.

FOUND FROM A BROWSER, 2026-08-19, WITH A CORRUPTED CLIENT ID: Trakt answered
401, `cached_get` reported that as None because this call did not ask for
errors, and the empty season it fell back to moved most of a roster out of the
buckets it belonged in — differently on each refresh, depending on which
lookups happened to be served from cache.

No network: the transport's own `cached_get` is patched, which is the seam
between "what Trakt said" and "what this function makes of it".
"""
from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app.providers.trakt import detail
from app.providers.trakt.transport import TraktError, TraktRateLimitError

SETTINGS = SimpleNamespace(trakt_client_id="id", trakt_access_token="",
                           cache_ttl_minutes=10, timezone="UTC")

EPISODES = [{"number": n, "first_aired": f"2026-07-0{n}T01:00:00.000Z"}
            for n in (1, 2, 3)]

# The REAL function, taken at import. tests/conftest.py's autouse
# `no_background_episode_lookups` replaces it for every test so the per-episode
# drain cannot reach the network, which also meant its own body never ran under
# test at all. Collection imports this module before any fixture runs, so this
# name still holds the original.
REAL_FETCH_SEASON_EPISODES = detail.fetch_season_episodes


class SeasonDetailTells0FromUnknownTests(unittest.IsolatedAsyncioTestCase):
    async def _detail(self, answer):
        """`answer` is either a return value or an exception to raise."""
        stub = (AsyncMock(side_effect=answer) if isinstance(answer, Exception)
                else AsyncMock(return_value=answer))
        with patch.object(detail.transport, "cached_get", stub):
            return await detail.fetch_season_detail(SETTINGS, 7, 1, client=object())

    async def test_a_real_episode_list_is_counted(self):
        self.assertEqual((await self._detail(EPISODES))["total"], 3)

    async def test_a_rejected_credential_raises_rather_than_answering_zero(self):
        """THE REPORTED BUG. 401 is Trakt declining to answer, and answering
        "this season has no episodes" on its behalf is what emptied a roster."""
        with self.assertRaises(TraktError):
            await self._detail(TraktError("Trakt rejected the credentials", 401))

    async def test_so_does_a_server_error(self):
        with self.assertRaises(TraktError):
            await self._detail(TraktError("Trakt API returned HTTP 503.", 503))

    async def test_and_a_rate_limit_still_reaches_the_caller_as_one(self):
        """The distrakt fan-out degrades this type deliberately (see its own
        docstring), so it must not be flattened into a TraktError here."""
        with self.assertRaises(TraktRateLimitError):
            await self._detail(TraktRateLimitError("rate limited", 429))

    async def test_but_a_404_is_still_an_empty_season(self):
        """The one status that genuinely means "there is nothing here": no such
        show, or no such season. That has always answered an empty season and
        still does — a row for a title Trakt has dropped is not a row whose
        counts could not be read."""
        answer = await self._detail(TraktError("Trakt API returned HTTP 404.", 404))
        self.assertEqual(answer["total"], 0)
        self.assertEqual(answer["season"], 1)

    async def test_a_body_that_is_not_a_list_is_an_empty_season_too(self):
        """Trakt answered, with something this function cannot count. That is
        its answer, not a failure to get one."""
        self.assertEqual((await self._detail({"unexpected": "shape"}))["total"], 0)


class SeasonEpisodesTests(unittest.IsolatedAsyncioTestCase):
    """`fetch_season_episodes` keeps three answers apart: the episodes, "Trakt
    has none" ([]), and "not without a request" (None), and raises for anything
    that is not an answer at all."""

    async def _episodes(self, answer, **kwargs):
        stub = (AsyncMock(side_effect=answer) if isinstance(answer, Exception)
                else AsyncMock(return_value=answer))
        with patch.object(detail.transport, "cached_get", stub):
            return await REAL_FETCH_SEASON_EPISODES(SETTINGS, 7, 1, client=object(), **kwargs), stub

    async def test_each_episode_keeps_the_facts_a_card_draws(self):
        raw = [{"number": 1, "title": "Pilot", "overview": "It begins.", "runtime": 50,
                "first_aired": "2026-07-01T01:00:00.000Z", "episode_type": "series_premiere",
                "rating": 8.1, "votes": 12},
               {"number": 2}]
        episodes, stub = await self._episodes(raw)
        self.assertEqual(episodes[0], {
            "number": 1, "title": "Pilot", "overview": "It begins.",
            "first_aired": "2026-07-01T01:00:00.000Z", "episode_type": "series_premiere",
            "runtime": 50, "rating": 8.1, "votes": 12})
        self.assertEqual(episodes[1]["title"], "")
        # `extended=full` is what carries first_aired; `episodes` alone does not.
        self.assertEqual(stub.await_args.args[3], {"extended": "full"})

    async def test_an_entry_with_no_number_or_no_shape_is_skipped(self):
        episodes, _ = await self._episodes([{"title": "no number"}, "junk", {"number": 3}])
        self.assertEqual([e["number"] for e in episodes], [3])

    async def test_a_404_or_a_body_that_is_not_a_list_is_no_episodes(self):
        for answer in (TraktError("Trakt API returned HTTP 404.", 404), {"unexpected": 1}):
            with self.subTest(answer=answer):
                self.assertEqual((await self._episodes(answer))[0], [])

    async def test_any_other_failure_raises(self):
        with self.assertRaises(TraktError):
            await self._episodes(TraktError("Trakt rejected the credentials", 401))

    async def test_not_yet_cached_is_none_rather_than_an_empty_season(self):
        episodes, stub = await self._episodes(None, only_if_cached=True)
        self.assertIsNone(episodes)
        self.assertTrue(stub.await_args.kwargs["only_if_cached"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
