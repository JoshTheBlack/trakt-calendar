"""app/distrakt/arrival.py — what a record owes before it is filed.

MEASURED BEFORE IT EXISTED, on a copy of the owner's database: every month record
the history backfill had written — 94 of them across two runs — was filed with no
network, while every calendar and search add had one. A settled row is never
revisited, so the blank was permanent. 71 of the 80 titles involved were already
answered by the stored calendar, which is why that is asked first.

No network: the stored calendar is seeded the way a fill leaves it, and the one
outbound lookup, live.network_for, is stood in for.
"""
from __future__ import annotations

from dataclasses import replace
from unittest.mock import AsyncMock, patch

from app.config import Settings
from app.distrakt import arrival
from app.providers.base import Media
from tests.distrakt.test_naming import a_calendar_record, store_calendar
from tests.distrakt.test_store import DistraktTestCase

SETTINGS = Settings(trakt_client_id="cid", simkl_client_id="scid")


def a_filing(*, network="", ids=None, season=1) -> dict:
    """A record as a history event builds it: ids, title, season, and no network."""
    return {"media": Media.SHOW, "ids": dict(ids or {"tmdb": 900, "trakt": 111}),
            "title": "A Show", "season": season, "network": network}


def _lookup(answer="Asked Network"):
    return patch("app.distrakt.live.network_for", AsyncMock(return_value=answer))


class FillNetworksTests(DistraktTestCase):
    async def test_a_record_that_states_a_network_keeps_it_and_costs_nothing(self):
        with _lookup() as asked:
            got, = await arrival.fill_networks(SETTINGS, [a_filing(network="HBO")])
        self.assertEqual(got["network"], "HBO")
        asked.assert_not_awaited()

    async def test_the_stored_calendar_answers_first_and_no_call_is_made(self):
        await store_calendar([replace(a_calendar_record("trakt", {"tmdb": 900, "trakt": 111}),
                                      network="The CW")])
        with _lookup() as asked:
            got, = await arrival.fill_networks(SETTINGS, [a_filing()])
        self.assertEqual(got["network"], "The CW")
        asked.assert_not_awaited()

    async def test_where_the_services_disagree_the_one_the_row_is_described_by_wins(self):
        """Simkl names who AIRED a show and Trakt who CARRIES it, both true. The
        row answers with the service its own ids select — the one network_for
        would have asked — so the badge agrees with the rest of the row."""
        await store_calendar([
            replace(a_calendar_record("trakt", {"tmdb": 900, "trakt": 111}), network="Netflix"),
            replace(a_calendar_record("simkl", {"tmdb": 900, "simkl": 222}), network="Fuji TV"),
        ])
        got, = await arrival.fill_networks(
            SETTINGS, [a_filing(ids={"tmdb": 900, "simkl": 222})])
        self.assertEqual(got["network"], "Fuji TV")
        got, = await arrival.fill_networks(
            SETTINGS, [a_filing(ids={"tmdb": 900, "trakt": 111, "simkl": 222})])
        self.assertEqual(got["network"], "Netflix")

    async def test_what_the_calendar_cannot_answer_is_asked_once_per_title(self):
        """A backfill names several seasons of one show; the network is a fact
        about the SHOW, so it costs one lookup however many seasons ride on it."""
        with _lookup("Paramount+") as asked:
            got = await arrival.fill_networks(
                SETTINGS, [a_filing(season=1), a_filing(season=2), a_filing(season=3)])
        self.assertEqual([r["network"] for r in got], ["Paramount+"] * 3)
        asked.assert_awaited_once()

    async def test_a_lookup_that_fails_leaves_the_network_blank_rather_than_the_write(self):
        from app.providers.base import SourceUnavailable
        with patch("app.distrakt.live.network_for",
                   AsyncMock(side_effect=SourceUnavailable("down"))):
            got, = await arrival.fill_networks(SETTINGS, [a_filing()])
        self.assertEqual(got["network"], "")

    async def test_the_callers_records_are_not_mutated(self):
        mine = a_filing()
        with _lookup():
            await arrival.fill_networks(SETTINGS, [mine])
        self.assertEqual(mine["network"], "")


class EveryWayOntoTheTrackerFillsItTests(DistraktTestCase):
    """The paths that measured blank. Each is asked for its records with the
    lookup stood in for, so what is asserted is that the path goes through the
    filling at all — which is the thing that was not true."""

    async def test_the_backfill_plan_carries_networks(self):
        from app.distrakt import backfill
        months = {"2026-01": [a_filing(season=1), a_filing(season=2)],
                  "2026-02": [a_filing(ids={"tmdb": 901, "trakt": 112})]}
        with _lookup("Hulu"):
            got = await backfill._with_networks(SETTINGS, months)
        self.assertEqual(list(got), ["2026-01", "2026-02"])
        self.assertEqual([len(rows) for rows in got.values()], [2, 1])
        self.assertTrue(all(r["network"] == "Hulu" for rows in got.values() for r in rows))

    async def test_the_rollover_seeds_the_list_with_networks(self):
        from app.distrakt import rollover
        progress = [{"ids": {"tmdb": 900, "trakt": 111}, "title": "A Show",
                     "season": 2, "watched": 3, "network": ""}]
        port = AsyncMock()
        port.fetch_watched_progress = AsyncMock(return_value=progress)
        detail = {"total": 10, "cadence": "w", "started_airing": True}
        with _lookup("AMC"), \
             patch("app.distrakt.watch_history.tracker_ports",
                   AsyncMock(return_value=[("trakt", port)])), \
             patch("app.distrakt.live.fetch_season_details",
                   AsyncMock(return_value=[detail])):
            got = await rollover.history_records(self.user_id, SETTINGS, set())
        self.assertEqual([r["network"] for r in got], ["AMC"])
