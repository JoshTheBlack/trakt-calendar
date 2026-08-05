"""The modal behind a tracker row: GET /api/distrakt/details.

WHAT THIS FILE IS FOR is the one row the route could not answer about. A season
the month under way has already SETTLED — finished, or given up on — is drawn on
the page from that month's own record, and opening it read "Could not load
details from Trakt". The route was searching the way a watch-history
reconciliation searches, and that search deliberately steps over the month under
way's verdicts, so a season that completed this month missed all three of its
places: it had left the viewer's list when it settled, it is not a premiere, and
the settled walk starts one month earlier.

That is not a two-service problem and never was. Any season completed in the
month being looked at has always had it.

No network: the transport's pooled client is replaced with a stub that answers
every catalogue path, exactly as the modal's other tests do.
"""
from __future__ import annotations

import asyncio
from unittest.mock import patch

from app import db, distrakt as distrakt_store
from app.clock import today
from app.config import Settings
from app.providers.trakt import transport
from tests.support import AppTestCase, ORIGIN


class _CatalogueClient:
    """An httpx.AsyncClient stand-in answering the three public catalogue paths
    the modal reads. It carries no credential and needs none."""

    def __init__(self, bodies: dict):
        self._bodies = bodies

    async def get(self, url, headers=None, timeout=None):
        path = url.split("?", 1)[0].split("api.trakt.tv/", 1)[-1]
        return _Response(self._bodies.get(path, {}))


class _Response:
    status_code = 200
    text = ""
    headers: dict = {}

    def __init__(self, body):
        self._body = body

    def json(self):
        return self._body


SHOW = {"title": "The Agency", "year": 2026, "overview": "Spies.",
        "status": "returning series", "network": "Paramount+", "runtime": 50,
        "genres": ["drama"], "rating": 8.0, "certification": "tv-ma"}
PEOPLE = {"cast": [{"person": {"name": "Michael"}, "character": "Martian"}]}
EPISODES = [{"number": n, "title": f"Ep {n}", "first_aired": "2026-08-01T20:00:00.000Z"}
            for n in range(1, 11)]

KEY = "show:tmdb:1"


def _month_back(count: int) -> str:
    """The key of the month `count` months before the one in progress, derived
    from the clock — a written-out month would be a different distance from today
    every month and would test the wrong rule most of them."""
    day = today()
    total = day.year * 12 + (day.month - 1) - count
    return distrakt_store.month_key(total // 12, total % 12 + 1)


def _settled(kind, tid: int = 1, season: int = 2) -> dict:
    return {"ids": {"trakt": 7, "tmdb": tid, "slug": "the-agency"},
            "season": season, "title": "The Agency", "network": "Paramount+",
            "media": "show", "kind": kind, "watched": 10, "total": 10,
            "started_airing": True, "finished_airing": True}


class ASeasonSettledInTheMonthBeingViewedTests(AppTestCase):
    """The regression. Everything here is one HTTP call against a roster holding
    exactly one record, which is what the failing page had."""

    def make_settings(self):
        # The catalogue half needs a client id and nothing else — the modal's
        # answer is public data plus this account's own stored progress.
        return Settings(public_base_url=ORIGIN, trakt_client_id="cid")

    def setUp(self):
        super().setUp()
        self.user_id = self.make_user("viewer", distrakt_approved=True,
                                      calendar_approved=True)
        self.link_identity(self.user_id, "trakt", 900, "user-token")
        self.sign_in_as(self.user_id)

    def _settle(self, month: str, kind=distrakt_store.RecordKind.COMPLETED,
                season: int = 2) -> None:
        asyncio.run(distrakt_store.add_month_record(
            self.user_id, month, _settled(kind, season=season)))

    def _details(self, season: int = 2):
        client = _CatalogueClient({
            "shows/7": SHOW, "shows/7/people": PEOPLE,
            f"shows/7/seasons/{season}": EPISODES,
        })
        with patch.object(transport, "shared_client", return_value=client):
            return self.client.get(f"/api/distrakt/details?key={KEY}&season={season}")

    def test_a_season_this_month_completed_opens(self):
        """The report, exactly: a season finished while the month was under way,
        answering 404 with the modal reading "Could not load details from
        Trakt"."""
        self._settle(_month_back(0))
        resp = self._details()
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["title"], "The Agency")

    def test_a_season_this_month_was_given_up_on_opens_too(self):
        """The same row from the other verdict. Both are drawn by the page and
        both were unreachable for the same reason."""
        self._settle(_month_back(0), distrakt_store.RecordKind.ABANDONED)
        self.assertEqual(self._details().status_code, 200)

    def test_a_season_an_earlier_month_settled_still_opens(self):
        """What already worked has to go on working: the search it was already
        doing is still the first thing asked."""
        self._settle(_month_back(1))
        self.assertEqual(self._details().status_code, 200)

    def test_a_season_still_on_the_viewers_list_still_opens(self):
        asyncio.run(distrakt_store.add_user_record(self.user_id, {
            **_settled(distrakt_store.RecordKind.KEEPUP), "watched": 3}))
        self.assertEqual(self._details().status_code, 200)

    def test_a_season_nothing_anywhere_knows_about_is_still_a_404(self):
        """The refusal is the honest answer for a row this account cannot see,
        and it is what keeps the route from answering about somebody else's
        roster."""
        self.assertEqual(self._details().status_code, 404)

    def test_the_watched_episodes_come_back_with_it(self):
        """The private half of the modal, read from this app's own storage — the
        reason the row being findable matters at all is that the settled row is
        where somebody looks to see what they watched."""
        self._settle(_month_back(0))
        asyncio.run(db.execute(
            "INSERT OR REPLACE INTO distrakt_show_progress "
            "(user_id, media, match_source, match_id, season, source, "
            "watched_episodes_json, trakt_id) VALUES (?,?,?,?,?,?,?,?)",
            (self.user_id, "show", "tmdb", "1", 2, "trakt",
             '{"1": "2026-08-01T00:00:00Z", "2": "2026-08-02T00:00:00Z"}', 7)))
        self.assertEqual(self._details().json()["watched_episodes"], [1, 2])
