"""live.season_detail asks the ANSWERING service through its own DetailPort.

It used to choose between two packages' functions with an if on the service's
name — "Simkl, else Trakt" — so any third answerer's id was sent to Trakt's
season endpoint. What is held here is the routing alone: whoever `choose` names
is asked through `providers.get(that source).detail_port`, by the record's own
id for THAT service, with the Refresh button's `fresh` carried through.
"""
from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app.config import Settings
from app.distrakt import live
from app.providers import detail_source
from app.providers.base import Media, Source

REC = {"media": "show", "season": 2, "ids": {"trakt": 101, "simkl": 202, "tmdb": 303}}
SUMMARY = {"season": 2, "total": 8, "cadence": "Mon", "premiere": None, "finale": None,
           "started_airing": False, "finished_airing": False, "air_dates": []}


class TheAnswererIsAskedThroughItsOwnPortTests(unittest.IsolatedAsyncioTestCase):

    async def _ask(self, answering: Source, *, fresh: bool = False):
        ports = {}

        def get(source):
            port = ports.setdefault(Source(source), SimpleNamespace(
                fetch_season_summary=AsyncMock(return_value=dict(SUMMARY)),
                catalogue_configured=lambda settings: True))
            return SimpleNamespace(detail_port=port)

        chosen = detail_source.Answerer(answering, str(REC["ids"][str(answering)]), False)
        with patch("app.distrakt.live.detail_source", return_value=str(answering)), \
             patch.object(detail_source, "choose", return_value=chosen), \
             patch("app.providers.get", get):
            detail = await live.season_detail(Settings(), dict(REC), fresh=fresh)
        return detail, ports

    async def test_each_service_is_asked_by_its_own_id(self):
        for answering in (Source.TRAKT, Source.SIMKL):
            with self.subTest(answering=str(answering)):
                detail, ports = await self._ask(answering)
                self.assertEqual(list(ports), [answering])
                ports[answering].fetch_season_summary.assert_awaited_once_with(
                    unittest.mock.ANY, REC["ids"][str(answering)], 2, Media.SHOW, fresh=False)
                self.assertEqual(detail["described_by"], str(answering))

    async def test_the_refresh_button_reaches_the_port(self):
        _detail, ports = await self._ask(Source.TRAKT, fresh=True)
        self.assertTrue(ports[Source.TRAKT].fetch_season_summary.await_args.kwargs["fresh"])

    async def test_no_package_function_is_reached_by_name(self):
        """The two package functions the old branch called directly are never
        touched: only the port is."""
        with patch("app.providers.trakt.detail.fetch_season_detail") as trakt_fn, \
             patch("app.providers.simkl.detail.fetch_season_detail") as simkl_fn:
            await self._ask(Source.SIMKL)
        trakt_fn.assert_not_called()
        simkl_fn.assert_not_called()


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
