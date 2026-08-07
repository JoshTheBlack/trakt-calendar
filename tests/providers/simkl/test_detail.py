"""Simkl's PUBLIC per-title lookups: the episode list, and the season summary a
tracker tile is drawn from.

The line this file guards is the one between this module and sync.py beside it.
An episode list is the same for everybody and takes no token, so it caches and
travels on the pool where Simkl allows parallel requests; a progress record is
one person's and does neither. Both halves are asserted here, because "does this
module cache?" having one answer per module is only true while something checks.
"""
from __future__ import annotations

import unittest
from datetime import date
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app.providers.base import Media
from app.providers.simkl import detail, transport

SETTINGS = SimpleNamespace(simkl_client_id="cid", simkl_access_token="", cache_ttl_minutes=10)

# A weekly season with its last episode not yet dated, which is the shape the
# cadence and finale rules are actually interesting for.
EPISODES = [
    {"episode": 1, "season": 1, "type": "episode", "date": "2026-07-07T01:00:00Z"},
    {"episode": 2, "season": 1, "type": "episode", "date": "2026-07-14T01:00:00Z"},
    {"episode": 3, "season": 1, "type": "episode", "date": "2026-07-21T01:00:00Z"},
    {"episode": 1, "season": 2, "type": "episode", "date": "2026-09-01T01:00:00Z"},
    {"episode": 0, "season": 1, "type": "special", "date": "2026-07-01T01:00:00Z"},
]


def _cached_get(*answers):
    return AsyncMock(side_effect=list(answers))


class PublicLookupTests(unittest.IsolatedAsyncioTestCase):
    async def test_the_episode_list_is_cached_and_travels_on_the_catalog_pool(self):
        """The opposite of every call in sync.py, and deliberately so: this
        answer is identical for every viewer, so caching it is right and the pool
        that allows parallel requests is the one it belongs on."""
        spy = _cached_get(EPISODES)
        with patch("app.providers.simkl.transport.cached_get", new=spy):
            await detail.fetch_episodes(SETTINGS, 55)
        call = spy.await_args
        self.assertEqual(call.args[2], "tv/episodes/55")
        self.assertIs(call.kwargs["pool"], transport.CATALOG_POOL)
        self.assertNotIn("private", call.kwargs)
        self.assertEqual(call.kwargs["ttl_seconds"], detail.EPISODES_CACHE_TTL_SECONDS)

    async def test_a_movie_and_a_missing_id_cost_no_request(self):
        spy = _cached_get()
        with patch("app.providers.simkl.transport.cached_get", new=spy):
            self.assertEqual(await detail.fetch_episodes(SETTINGS, 55, Media.MOVIE), [])
            self.assertEqual(await detail.fetch_episodes(SETTINGS, None), [])
        spy.assert_not_awaited()

    async def test_an_id_simkl_does_not_know_is_an_empty_list_not_a_failure(self):
        """Simkl's own answer for an unknown id is `[]`, and a caller could do
        nothing different with a distinction anyway."""
        with patch("app.providers.simkl.transport.cached_get", new=_cached_get(None)):
            self.assertEqual(await detail.fetch_episodes(SETTINGS, 55), [])


class SeasonSummaryTests(unittest.IsolatedAsyncioTestCase):
    """The same keys the Trakt package's fetch_season_detail returns, because the
    tracker merges whichever answered into one row."""

    async def _season(self, episodes, season=1, **kwargs):
        with patch("app.providers.simkl.transport.cached_get", new=_cached_get(episodes)):
            return await detail.fetch_season_detail(SETTINGS, 55, season,
                                                    today=date(2026, 7, 20), **kwargs)

    async def test_a_season_is_counted_without_its_specials(self):
        got = await self._season(EPISODES)
        self.assertEqual((got["season"], got["total"], got["cadence"]), (1, 3, "Tue"))
        self.assertEqual(got["premiere"], "7/7")
        self.assertTrue(got["started_airing"])

    async def test_another_seasons_episodes_are_not_this_seasons(self):
        got = await self._season(EPISODES, season=2)
        self.assertEqual((got["total"], got["premiere"]), (1, "9/1"))

    async def test_a_title_with_no_episode_list_answers_with_every_key(self):
        """A missing key would read as a template bug rather than as a lookup
        nobody could answer."""
        empty = await self._season([])
        self.assertEqual(set(empty), {"season", "total", "cadence", "premiere",
                                      "finale", "started_airing", "finished_airing",
                                      "air_dates"})
        self.assertEqual((empty["total"], empty["cadence"]), (0, None))

    async def test_anime_episodes_carry_no_season_and_are_read_as_this_one(self):
        """Simkl treats an anime title as one canonical season and omits the
        field entirely. Reading an episode with no season number as belonging to
        the season being asked about is correct for anime and harmless for TV,
        where the field is always there."""
        anime = [{"episode": 1, "type": "episode", "date": "2026-07-05T01:00:00Z"},
                 {"episode": 2, "type": "episode", "date": "2026-07-12T01:00:00Z"}]
        got = await self._season(anime)
        self.assertEqual(got["total"], 2)
        self.assertEqual(detail.seasons_known(anime), [1])

    async def test_an_undated_tail_leaves_the_finale_unknown(self):
        """The same rule the Trakt side takes: a season that is not fully
        scheduled has no finale date to claim yet."""
        got = await self._season([*EPISODES, {"episode": 4, "season": 1, "type": "episode"}])
        self.assertEqual((got["total"], got["finale"]), (4, None))

    async def test_the_time_is_dropped_and_the_date_is_kept(self):
        """Simkl expresses a whole file's times in one fixed offset rather than
        in each title's own zone, so the instant is approximate while the
        calendar day is reliable. A cadence derived from a converted instant
        would be a coin flip for anything airing near midnight."""
        late = [{"episode": n, "season": 1, "type": "episode",
                 "date": f"2026-07-{6 + 7 * n:02d}T23:30:00-04:00"} for n in (1, 2)]
        got = await self._season(late)
        self.assertEqual(got["premiere"], "7/13")


class TheModalsFieldSetTests(unittest.IsolatedAsyncioTestCase):
    """What a card opens on when Simkl is the only service that listed it.

    The route-level behaviour lives in tests/calendar/test_detail_modal.py; what
    is here is the part that belongs to this module — that the two lookups behind
    one modal both stay on the cached, tokenless half of Simkl. A modal that
    quietly read through SYNC_POOL would spend one person's quota to draw a fact
    that is identical for everybody.
    """

    TITLE = {"title": "A Show", "ids": {"simkl": 55}, "overview": "Words.",
             "genres": ["Game Show"], "trailers": [{"youtube": "abc123"}]}

    async def _details(self, season=1):
        calls = []

        async def _get(client, settings, path, params=None, **kwargs):
            calls.append((path, kwargs))
            return self.TITLE if path.startswith("tv/5") else EPISODES

        with patch("app.providers.simkl.transport.cached_get", new=AsyncMock(side_effect=_get)):
            got = await detail.fetch_details(SETTINGS, Media.SHOW, 55, season)
        return got, calls

    async def test_both_lookups_cache_and_neither_is_private(self):
        _got, calls = await self._details()
        self.assertEqual([path for path, _ in calls], ["tv/55", "tv/episodes/55"])
        for path, kwargs in calls:
            with self.subTest(path=path):
                self.assertIs(kwargs["pool"], transport.CATALOG_POOL)
                self.assertNotIn("private", kwargs)

    async def test_the_cast_is_empty_because_simkl_publishes_none(self):
        """Not a lookup that failed. Simkl has no cast on any endpoint this app
        can reach, and no third metadata service is pulled in to fill it."""
        got, _calls = await self._details()
        self.assertEqual(got["cast"], [])

    async def test_a_bare_youtube_id_becomes_a_url_here_and_not_in_the_renderer(self):
        got, _calls = await self._details()
        self.assertEqual(got["trailer"], "https://www.youtube.com/watch?v=abc123")

    async def test_a_title_simkl_cannot_place_still_answers_with_every_key(self):
        """Simkl's "not found" is a 200 with a body that is not a title, so the
        modal has to render around an answer that says nothing rather than error.
        """
        with patch("app.providers.simkl.transport.cached_get",
                   new=AsyncMock(side_effect=[[], []])):
            got = await detail.fetch_details(SETTINGS, Media.SHOW, 55, 1)
        self.assertEqual(got["overview"], "")
        self.assertEqual(got["episodes"], [])
        self.assertIn("certification", got)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
