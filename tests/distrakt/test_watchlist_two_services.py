"""The write step with BOTH services switched on: Simkl's Plan to Watch and
Trakt's watchlist, written from one tracker, each answering for itself
(app/distrakt/watchlist_writes.py).

Simkl is the port-level fake the step's own tests use. Trakt is the REAL port
(app/providers/trakt/watchlist.py) over a stateful stand-in for Trakt's API —
a watchlist that pages, adds, counts what already existed, removes, and can
be full or down — so what is exercised here is the step and Trakt's port
together, as a pass really runs them.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, patch
from urllib.parse import parse_qs, urlparse

import httpx

from app import distrakt
from app.config import Settings
from app.distrakt import lifecycle, watchlist_writes as ww
from app.providers import get as get_provider
from app.providers.base import Source
from app.providers.trakt import TraktError, transport as trakt_transport
from tests.distrakt.test_store import DistraktTestCase, a_record, month_back
from tests.distrakt.watchlist_fake import FakeList

SIMKL, TRAKT = str(Source.SIMKL), str(Source.TRAKT)


class FakeTraktApi:
    """Trakt's watchlist endpoints, holding one person's list.

    `catalogue` is {tmdb: trakt} for every show Trakt knows; a show named by an
    id outside it comes back under `not_found`, echoed as sent, which is what
    the live service did with an id it had no show for."""

    PAGE = 100

    def __init__(self, catalogue: dict[int, int], listed=()):
        self.catalogue = dict(catalogue)
        self.listed: set[int] = set(listed)
        self.fail = False
        self.full = False
        self.posts: list[tuple[str, list[dict]]] = []

    def _resolve(self, ids: dict) -> int | None:
        if "trakt" in ids:
            trakt = int(ids["trakt"])
            return trakt if trakt in self.catalogue.values() else None
        tmdb = ids.get("tmdb")
        return self.catalogue.get(int(tmdb)) if tmdb is not None else None

    def _item(self, trakt: int) -> dict:
        tmdb = next(t for t, k in self.catalogue.items() if k == trakt)
        return {"type": "show", "show": {"title": f"Show {tmdb}", "ids": {
            "trakt": trakt, "tmdb": tmdb, "slug": f"show-{tmdb}"}}}

    async def send_or_unavailable(self, client, method, url, *, headers=None, json=None,
                                  timeout=None):
        if self.fail:
            raise TraktError("Could not reach Trakt: the fake is down.")
        parsed = urlparse(url)
        path = parsed.path.lstrip("/")
        if method == "GET":
            query = {k: v[0] for k, v in parse_qs(parsed.query).items()}
            page, limit = int(query["page"]), int(query["limit"])
            items = [self._item(trakt) for trakt in sorted(self.listed)]
            pages = max(1, -(-len(items) // limit))
            return httpx.Response(200, json=items[(page - 1) * limit:page * limit],
                                  headers={"x-pagination-page-count": str(pages)})
        shows = json["shows"]
        self.posts.append((path, shows))
        if self.full:
            return httpx.Response(420, json={}, headers={"X-Account-Limit": "100"})
        counts = {"shows": 0}
        existing = {"shows": 0}
        missing = []
        for show in shows:
            trakt = self._resolve(show["ids"])
            if trakt is None:
                missing.append({"ids": show["ids"]})
            elif path == "sync/watchlist":
                if trakt in self.listed:
                    existing["shows"] += 1
                else:
                    self.listed.add(trakt)
                    counts["shows"] += 1
            elif trakt in self.listed:
                self.listed.discard(trakt)
                counts["shows"] += 1
        if path == "sync/watchlist":
            body = {"added": counts, "existing": existing, "not_found": {"shows": missing}}
            return httpx.Response(201, json=body)
        return httpx.Response(200, json={"deleted": counts, "not_found": {"shows": missing}})

    def sent(self, path: str) -> list[dict]:
        return [ids for called, shows in self.posts if called == path
                for ids in (show["ids"] for show in shows)]


class TwoServicesTestCase(DistraktTestCase):
    KEY = "show:tmdb:1"

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.simkl = FakeList()
        self.trakt = FakeTraktApi({1: 501, 2: 502, 3: 503})
        port = get_provider(Source.TRAKT).watchlist_port
        settings = Settings(trakt_client_id="cid", trakt_access_token="tok",
                            simkl_client_id="sid", simkl_access_token="stok")
        self._patches = [
            patch("app.providers.for_watchlist",
                  lambda _settings: [(Source.SIMKL, self.simkl), (Source.TRAKT, port)]),
            patch("app.auth.viewer.settings_for_user", AsyncMock(return_value=settings)),
            patch.object(trakt_transport, "send_or_unavailable",
                         new=self.trakt.send_or_unavailable),
        ]
        for started in self._patches:
            started.start()
        self._reset()
        await ww.switch(self.user_id, SIMKL, True)
        await ww.switch(self.user_id, TRAKT, True)

    async def asyncTearDown(self):
        for started in self._patches:
            started.stop()
        self._reset()
        await super().asyncTearDown()

    @staticmethod
    def _reset():
        ww._locks.clear()
        ww._running.clear()
        ww._again.clear()
        ww._departures.clear()
        ww._trouble.clear()

    async def listed(self, tmdb: int, season: int = 1):
        await distrakt.add_user_record(self.user_id, a_record(
            tmdb, season=season, kind=distrakt.RecordKind.KEEPUP, title=f"Show {tmdb}",
            ids={"tmdb": tmdb, "trakt": 500 + tmdb}))

    async def states(self, source: str) -> dict[str, tuple[str, str]]:
        return {key: (str(row.state), row.status_before)
                for key, row in (await ww.rows(self.user_id, source)).items()}

    async def depart(self, departure):
        pending = ww.expect(self.user_id, self.KEY, departure)
        key = distrakt.record_key(a_record(1))
        if departure is ww.Departure.ABANDON:
            record = await distrakt.find_user_record(self.user_id, key, 1)
            await lifecycle.give_up(self.user_id, record, month=month_back(0))
        else:
            await distrakt.remove_user_record(self.user_id, key, 1)
        with patch.object(ww, "nudge", lambda user_id: None):
            await ww.after_request(self.user_id, [pending])
        await ww.run(self.user_id)


class ArrivalAtBothTests(TwoServicesTestCase):
    async def test_one_add_writes_both(self):
        await self.listed(1)
        await ww.run(self.user_id)
        self.assertEqual(self.simkl.held, {self.KEY: "plantowatch"})
        self.assertEqual(self.trakt.listed, {501})
        self.assertEqual(self.trakt.sent("sync/watchlist"), [{"trakt": 501}])
        self.assertEqual(await self.states(SIMKL), {self.KEY: ("written", "none")})
        self.assertEqual(await self.states(TRAKT), {self.KEY: ("written", "none")})
        row = (await ww.rows(self.user_id, TRAKT))[self.KEY]
        self.assertEqual(row.service_id, "501")

    async def test_a_show_already_on_the_watchlist_is_the_viewers_own(self):
        self.trakt.listed.add(501)
        await self.listed(1)
        await ww.run(self.user_id)
        self.assertEqual(self.trakt.posts, [])
        self.assertEqual(await self.states(TRAKT), {self.KEY: ("written", "listed")})

    async def test_a_show_trakt_does_not_know_is_settled_as_not_found(self):
        await distrakt.add_user_record(self.user_id, a_record(
            9, kind=distrakt.RecordKind.KEEPUP, title="Unknown", ids={"tmdb": 9}))
        await ww.run(self.user_id)
        self.assertEqual(await self.states(TRAKT), {"show:tmdb:9": ("not_found", "none")})
        self.assertEqual(self.trakt.sent("sync/watchlist"), [{"tmdb": 9}])
        await ww.run(self.user_id)
        self.assertEqual(len(self.trakt.posts), 1, "a settled show was sent again")

    async def test_a_played_show_is_never_put_back_while_it_stays(self):
        """Trakt takes a show off the watchlist at its first play. That is
        expected, and the tracker does not fight it."""
        await self.listed(1)
        await ww.run(self.user_id)
        self.trakt.listed.clear()
        await ww.run(self.user_id)
        self.assertEqual(len(self.trakt.sent("sync/watchlist")), 1)


class DepartureAtBothTests(TwoServicesTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        await self.listed(1)
        await ww.run(self.user_id)

    async def test_abandon_drops_at_both(self):
        await self.depart(ww.Departure.ABANDON)
        self.assertEqual(self.simkl.held, {self.KEY: "dropped"})
        self.assertEqual(self.trakt.listed, set())
        # The departed show is named by the Trakt id the write recorded.
        self.assertEqual(self.trakt.sent("sync/watchlist/remove"), [{"trakt": 501}])
        self.assertEqual((await self.states(TRAKT))[self.KEY][0], "dropped")
        self.assertEqual((await self.states(SIMKL))[self.KEY][0], "dropped")

    async def test_the_x_undoes_at_both(self):
        await self.depart(ww.Departure.REMOVE)
        self.assertEqual(self.simkl.held, {})
        self.assertEqual(self.trakt.listed, set())
        self.assertEqual((await self.states(TRAKT))[self.KEY][0], "undone")
        self.assertEqual((await self.states(SIMKL))[self.KEY][0], "undone")

    async def test_the_x_leaves_a_show_the_viewer_had_listed_on_trakt(self):
        await ww.switch(self.user_id, TRAKT, False)
        await ww._forget(self.user_id, TRAKT, [self.KEY])
        await ww.switch(self.user_id, TRAKT, True)
        self.trakt.listed = {501}
        await ww.run(self.user_id)
        self.assertEqual(await self.states(TRAKT), {self.KEY: ("written", "listed")})
        await self.depart(ww.Departure.REMOVE)
        self.assertEqual(self.trakt.listed, {501})
        self.assertEqual(self.trakt.sent("sync/watchlist/remove"), [])


class TraktFailingTests(TwoServicesTestCase):
    async def test_a_trakt_that_is_down_leaves_simkl_and_the_tracker_working(self):
        self.trakt.fail = True
        await self.listed(1)
        await ww.run(self.user_id)
        self.assertEqual(await self.states(SIMKL), {self.KEY: ("written", "none")})
        self.assertEqual(await self.states(TRAKT), {self.KEY: ("failed", "none")})
        self.assertIsNone(ww.trouble(self.user_id, SIMKL))
        self.assertIn("fake is down", ww.trouble(self.user_id, TRAKT).message)
        records = await distrakt.user_records(self.user_id)
        self.assertEqual(len(records), 1, "the tracker's own record was touched")

    async def test_the_failed_show_is_written_once_trakt_is_back(self):
        self.trakt.fail = True
        await self.listed(1)
        await ww.run(self.user_id)
        self.trakt.fail = False
        ww._trouble.clear()                        # the quiet spell has passed
        await ww.run(self.user_id)
        self.assertEqual(await self.states(TRAKT), {self.KEY: ("written", "none")})
        self.assertEqual(self.trakt.listed, {501})

    async def test_an_abandon_trakt_cannot_hear_waits_and_simkls_goes_through(self):
        await self.listed(1)
        await ww.run(self.user_id)
        self.trakt.fail = True
        await self.depart(ww.Departure.ABANDON)
        self.assertEqual(self.simkl.held, {self.KEY: "dropped"})
        self.assertEqual((await self.states(TRAKT))[self.KEY][0], "written")
        self.assertIn(self.KEY, ww._departures.get(self.user_id, {}))
        self.trakt.fail = False
        ww._trouble.clear()
        await ww.run(self.user_id)
        self.assertEqual(self.trakt.listed, set())
        self.assertEqual((await self.states(TRAKT))[self.KEY][0], "dropped")


class FullListTests(TwoServicesTestCase):
    async def test_a_full_watchlist_is_refused_settled_and_shown(self):
        self.trakt.full = True
        await self.listed(1)
        await self.listed(2)
        await ww.run(self.user_id)
        self.assertEqual(await self.states(TRAKT), {"show:tmdb:1": ("refused", "none"),
                                                    "show:tmdb:2": ("refused", "none")})
        self.assertEqual((await self.states(SIMKL))["show:tmdb:1"][0], "written")
        await ww.run(self.user_id)
        self.assertEqual(len(self.trakt.posts), 1, "a refused write was tried again")
        switches = {s.source: s for s in await ww.switches(self.user_id, Settings())}
        # switches() asks the registry itself; the patched for_watchlist offers both.
        self.assertEqual(switches[TRAKT].refused, 2)
        self.assertEqual(switches[TRAKT].waiting, 0)
        self.assertEqual(switches[SIMKL].refused, 0)


if __name__ == "__main__":
    import unittest
    unittest.main()
