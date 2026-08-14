"""Trakt's DetailPort.fetch_seasons implementation (app/providers/trakt/
__init__.py's `_TraktDetailPort.fetch_seasons`).

Guards the one thing that makes Trakt's answer different from Simkl's:
`named_season` and `ids` are always empty/None, because Trakt's catalogue has
no concept of a search hit that IS a season of a larger show and a Trakt
search hit already carries every shared id `search_titles` found — see
SeasonsAnswer's own docstring in app/providers/base.py.

No network — app.providers.trakt.detail.fetch_show_seasons is patched
directly, matching test_search.py's own pattern for this port.
"""
from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app.providers.base import Media, SeasonsAnswer
from app.providers.trakt import detail
from app.providers.trakt import _TraktDetailPort

SETTINGS = SimpleNamespace(trakt_client_id="id", trakt_access_token="", cache_ttl_minutes=10)

SEASONS = [{"season": 1, "episode_count": 10}, {"season": 2, "episode_count": 8}]


class TraktSeasonsTests(unittest.IsolatedAsyncioTestCase):
    async def test_delegates_to_fetch_show_seasons(self):
        spy = AsyncMock(return_value=SEASONS)
        with patch.object(detail, "fetch_show_seasons", spy):
            answer = await _TraktDetailPort().fetch_seasons(SETTINGS, "123", Media.SHOW)
        spy.assert_awaited_once_with(SETTINGS, "123")
        self.assertEqual(answer.seasons, SEASONS)

    async def test_named_season_is_always_none(self):
        """Trakt never offers a hit that is one season of a larger show, so
        there is never a season to skip the picker for."""
        with patch.object(detail, "fetch_show_seasons", AsyncMock(return_value=SEASONS)):
            answer = await _TraktDetailPort().fetch_seasons(SETTINGS, "123", Media.SHOW)
        self.assertIsNone(answer.named_season)

    async def test_ids_are_always_empty(self):
        """A Trakt search hit already carries every shared id search found —
        this per-title call has nothing new to surface."""
        with patch.object(detail, "fetch_show_seasons", AsyncMock(return_value=SEASONS)):
            answer = await _TraktDetailPort().fetch_seasons(SETTINGS, "123", Media.SHOW)
        self.assertEqual(answer.ids, {})

    async def test_the_answer_is_a_seasonsanswer(self):
        with patch.object(detail, "fetch_show_seasons", AsyncMock(return_value=[])):
            answer = await _TraktDetailPort().fetch_seasons(SETTINGS, "123", Media.SHOW)
        self.assertEqual(answer, SeasonsAnswer(seasons=[], named_season=None, ids={}))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
