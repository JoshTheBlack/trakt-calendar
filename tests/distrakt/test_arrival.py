"""app/distrakt/arrival.py — what a record IS, decided once, whichever way it
reached the tracker.

MEASURED BEFORE THIS EXISTED, on a copy of the owner's database: of the titles
two different paths had both filed, history-written rows were missing tvdb and
imdb on 13 of 14, the network on 4, and a service id on 1 — the same title,
described differently by the door it came through. And every month record the
history backfill wrote had no network at all.

TWO KINDS OF TEST. The rules `describe` applies, one at a time; and — the one
that holds the design in place — every one of the six ways onto the tracker
writing EXACTLY the description `describe` gave it, so a path that decides a
field for itself fails here rather than drifting quietly.

No network: the stored calendar is seeded the way a fill leaves it, the season
lookup is stood in for, and tests/distrakt/conftest.py answers the two catalogue
lookups with "nothing more" unless a test says otherwise.
"""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
from dataclasses import replace
from unittest.mock import AsyncMock, patch

from app.config import Settings
from app.distrakt import arrival, backfill, calendar_import, rollover, store
from app.metadata import ids as id_rules
from app.providers.base import ItemKey, Media, SourceUnavailable, TitleFacts
from tests.distrakt.test_naming import a_calendar_record, store_calendar
from tests.distrakt.test_rewatch import KEY, _RosterAddCase
from tests.distrakt.test_store import DistraktTestCase

# A VIEWER'S OWN TOKEN, NOT JUST A CLIENT ID: arrival.describe's callers
# (calendar_import, rollover, the tracker's own routes) are always processing
# one account, and Simkl's v2 search/redirect now require that account's own
# token (see providers.for_shared_id_lookup's docstring) — a client id alone
# is no longer enough to make find_by_shared_id askable.
SETTINGS = Settings(trakt_client_id="cid", simkl_client_id="scid",
                   simkl_access_token="viewer-simkl-token")
SEASON = {"total": 10, "cadence": "w", "premiere": "2026-01-05", "finale": "2026-03-09",
          "started_airing": True, "finished_airing": True}


def a_filing(*, ids=None, season=1, title="Path Title", network="") -> dict:
    """A record as a path hands one over: an identity, a season, and whatever
    that path happened to be told."""
    return {"media": Media.SHOW, "ids": dict(ids or {"tmdb": 900, "trakt": 111}),
            "title": title, "season": season, "network": network}


def _season(answer=None):
    return patch("app.distrakt.live.season_detail",
                 AsyncMock(return_value=dict(answer or SEASON)))


def _facts(title="", network=""):
    return patch("app.providers.trakt.detail.fetch_title_facts",
                 AsyncMock(return_value=TitleFacts(title, network)))


class TheIdsTests(DistraktTestCase):
    async def test_the_stored_calendar_teaches_the_other_service_and_the_shared_ids(self):
        await store_calendar([a_calendar_record(
            "simkl", {"tmdb": 900, "simkl": 222, "simkl_slug": "a-show", "imdb": "tt9"})])
        with _season():
            arrived, = await arrival.describe(SETTINGS, [a_filing()])
        ids = arrived.record["ids"]
        self.assertEqual((ids["simkl"], ids["simkl_slug"], ids["imdb"]), (222, "a-show", "tt9"))
        self.assertEqual(ids["trakt"], 111)

    async def test_a_service_nothing_local_knows_is_asked_by_shared_id(self):
        """Nothing on disk knows Simkl's id for it, so Simkl is asked — by the
        shared id, an exact lookup, never by title."""
        found = AsyncMock(return_value={"simkl": 555, "simkl_slug": "found"})
        with _season(), patch("app.providers.simkl.by_id.find_by_shared_id", found):
            arrived, = await arrival.describe(SETTINGS, [a_filing()])
        self.assertEqual(arrived.record["ids"]["simkl"], 555)
        self.assertEqual(found.await_args.args[2]["tmdb"], 900)

    async def test_an_answer_that_names_no_id_of_that_service_is_no_answer(self):
        with _season(), patch("app.providers.simkl.by_id.find_by_shared_id",
                              AsyncMock(return_value={"imdb": "tt-other"})):
            arrived, = await arrival.describe(SETTINGS, [a_filing()])
        self.assertNotIn("simkl", arrived.record["ids"])
        self.assertNotIn("imdb", arrived.record["ids"])

    async def test_a_service_that_could_not_be_asked_changes_nothing(self):
        with _season(), patch("app.providers.simkl.by_id.find_by_shared_id",
                              AsyncMock(side_effect=SourceUnavailable("down"))):
            arrived, = await arrival.describe(SETTINGS, [a_filing()])
        self.assertEqual(arrived.record["ids"], {"tmdb": 900, "trakt": 111})

    async def test_learning_a_higher_id_files_the_title_there_with_its_earlier_rows(self):
        """A title filed under tvdb that learns its tmdb id is filed under tmdb —
        and the row already filed under tvdb moves with it in the same
        transaction, so the title does not split from its own earlier rows."""
        await store.add_user_record(self.user_id, {
            "ids": {"tvdb": 4321, "simkl": 1}, "season": 1, "kind": "keepup",
            "title": "Earlier"})
        with _season(), patch("app.providers.trakt.detail.find_by_shared_id",
                              AsyncMock(return_value={"trakt": 7, "tmdb": 900})):
            arrived, = await arrival.describe(
                SETTINGS, [a_filing(ids={"tvdb": 4321, "simkl": 1}, season=2)])
        self.assertEqual(store.record_key(arrived.record), ItemKey("show", "tmdb", "900"))
        self.assertEqual(arrived.record["ids"]["tvdb"], 4321)
        earlier = await store.find_user_record(self.user_id, ItemKey("show", "tmdb", "900"), 1)
        self.assertEqual((earlier["title"], earlier["ids"]["tmdb"]), ("Earlier", 900))
        self.assertIsNone(
            await store.find_user_record(self.user_id, ItemKey("show", "tvdb", "4321"), 1))

    async def test_a_title_the_store_already_keys_higher_is_filed_there_without_asking(self):
        """A play naming a show only by its tvdb id is the title another service
        already stated a tmdb id for: it is filed under that title's key."""
        await store_calendar([a_calendar_record("trakt", {"tmdb": 900, "tvdb": 4321,
                                                          "trakt": 111})])
        with _season():
            arrived, = await arrival.describe(
                SETTINGS, [a_filing(ids={"tvdb": 4321, "simkl": 1})])
        self.assertEqual(store.record_key(arrived.record), ItemKey("show", "tmdb", "900"))

    async def test_a_held_service_id_is_never_replaced(self):
        await store_calendar([a_calendar_record("trakt", {"tmdb": 900, "trakt": 999})])
        with _season():
            arrived, = await arrival.describe(SETTINGS, [a_filing()])
        self.assertEqual(arrived.record["ids"]["trakt"], 111)

    async def test_where_the_services_disagree_about_a_shared_id_the_describer_wins(self):
        """MEASURED: Trakt and Simkl state different imdb ids for Little House on
        the Prairie (2026), so the title arrived with either, by door. The
        describing service's own stored record decides — never the filing id."""
        await store_calendar([
            a_calendar_record("trakt", {"tmdb": 900, "trakt": 111, "imdb": "tt-trakt"}),
            a_calendar_record("simkl", {"tmdb": 900, "simkl": 222, "imdb": "tt-simkl"})])
        with _season():
            by_simkl_door, = await arrival.describe(
                SETTINGS, [a_filing(ids={"tmdb": 900, "simkl": 222, "imdb": "tt-simkl"})])
            by_trakt_door, = await arrival.describe(
                SETTINGS, [a_filing(ids={"tmdb": 900, "trakt": 111})])
        self.assertEqual(by_simkl_door.record["ids"], by_trakt_door.record["ids"])
        self.assertEqual(by_simkl_door.record["ids"]["imdb"], "tt-trakt")
        self.assertEqual(by_simkl_door.record["ids"]["tmdb"], 900)

    def test_ids_are_filed_in_one_shape_whatever_door_they_came_through(self):
        """The bare `slug` is ambiguous — each service calls its own name that —
        and Simkl states numeric ids as text where Trakt states numbers."""
        self.assertEqual(
            id_rules.canonical({"slug": "the-rookie", "trakt_slug": "the-rookie-2018",
                                "tmdb": "79744", "tvdb": 350665, "imdb": "tt7587890",
                                "unknown": 1}),
            {"trakt_slug": "the-rookie-2018", "tmdb": 79744, "tvdb": 350665,
             "imdb": "tt7587890"})


class TheWordsTests(DistraktTestCase):
    async def test_the_describing_service_names_the_title_and_the_network(self):
        with _season(), _facts("Service Title", "HBO"):
            arrived, = await arrival.describe(SETTINGS, [a_filing(network="Other")])
        self.assertEqual((arrived.record["title"], arrived.record["network"]),
                         ("Service Title", "HBO"))

    async def test_the_stored_calendar_answers_first_when_it_holds_exactly_that_title(self):
        await store_calendar([replace(
            a_calendar_record("trakt", {"tmdb": 900, "trakt": 111}),
            title="Stored Title", network="The CW")])
        with _season(), _facts("Asked", "Asked") as asked:
            arrived, = await arrival.describe(SETTINGS, [a_filing()])
        self.assertEqual((arrived.record["title"], arrived.record["network"]),
                         ("Stored Title", "The CW"))
        asked.assert_not_awaited()

    async def test_a_stored_record_of_another_id_is_not_taken_for_this_one(self):
        """Simkl files each anime season as its own title under the series' key;
        a stored row for a different id is asked about instead, so a stored
        answer and a fetched one can never be two different things."""
        await store_calendar([replace(
            a_calendar_record("trakt", {"tmdb": 900, "trakt": 999}),
            title="Other Title", network="Other")])
        with _season(), _facts("Asked", "Asked Net") as asked:
            arrived, = await arrival.describe(SETTINGS, [a_filing()])
        self.assertEqual(arrived.record["network"], "Asked Net")
        asked.assert_awaited_once()

    async def test_a_season_only_the_second_service_holds_is_named_by_it(self):
        """MEASURED: Simkl files its anthology "Monster" under the ids of Trakt's
        "DAHMER – Monster: The Jeffrey Dahmer Story", which has one season. Trakt,
        asked first, holds nothing of season 4, so Simkl — which holds all eight
        episodes — answers the season AND names the row."""
        empty = {"season": 4, "total": 0, "cadence": None, "premiere": None, "finale": None,
                 "started_airing": False, "finished_airing": False, "air_dates": []}
        held = {**SEASON, "season": 4, "total": 8, "air_dates": []}
        with patch("app.providers.trakt.detail.fetch_season_detail", AsyncMock(return_value=empty)), \
                patch("app.providers.simkl.detail.fetch_season_detail",
                      AsyncMock(return_value=held)), \
                _facts("DAHMER - Monster", "Netflix"), \
                patch("app.providers.simkl.detail.fetch_title_facts",
                      AsyncMock(return_value=TitleFacts("Monster", "Netflix"))):
            arrived, = await arrival.describe(SETTINGS, [a_filing(
                ids={"tmdb": 900, "trakt": 111, "simkl": 222}, season=4)])
        self.assertEqual(arrived.record["title"], "Monster")
        self.assertEqual(arrived.record["total"], 8)
        self.assertEqual(arrived.detail["described_by"], "simkl")

    async def test_a_season_the_describing_service_holds_is_named_by_it_as_before(self):
        with patch("app.providers.trakt.detail.fetch_season_detail",
                   AsyncMock(return_value={**SEASON, "air_dates": []})), \
                _facts("Trakt Title", "HBO"), \
                patch("app.providers.simkl.detail.fetch_season_detail") as simkl_season:
            arrived, = await arrival.describe(SETTINGS, [a_filing(
                ids={"tmdb": 900, "trakt": 111, "simkl": 222})])
        self.assertEqual(arrived.record["title"], "Trakt Title")
        simkl_season.assert_not_called()

    async def test_a_blank_answer_keeps_the_paths_words(self):
        with _season(), _facts("", ""):
            arrived, = await arrival.describe(SETTINGS, [a_filing(network="AMC")])
        self.assertEqual((arrived.record["title"], arrived.record["network"]),
                         ("Path Title", "AMC"))


class TheSeasonTests(DistraktTestCase):
    async def test_the_season_facts_come_from_the_describing_services_lookup(self):
        with _season() as lookup:
            arrived, = await arrival.describe(SETTINGS, [a_filing(season=2)])
        for field in ("total", "cadence", "premiere", "finale", "started_airing"):
            self.assertEqual(arrived.record[field], SEASON[field])
        self.assertEqual(arrived.detail["total"], 10)
        self.assertEqual(lookup.await_args.args[1]["season"], 2)

    async def test_a_failed_lookup_degrades_to_no_detail(self):
        with patch("app.distrakt.live.season_detail",
                   AsyncMock(side_effect=SourceUnavailable("down"))):
            arrived, = await arrival.describe(SETTINGS, [a_filing()])
        self.assertEqual(arrived.detail, {})
        self.assertNotIn("total", arrived.record)

    async def test_strict_raises_rather_than_filing_a_guess(self):
        with patch("app.distrakt.live.season_detail",
                   AsyncMock(side_effect=SourceUnavailable("down"))):
            with self.assertRaises(SourceUnavailable):
                await arrival.describe(SETTINGS, [a_filing()], strict=True)

    async def test_one_title_is_asked_about_once_however_many_seasons_arrive(self):
        found = AsyncMock(return_value={})
        with _season() as lookup, _facts("T", "N") as facts, \
             patch("app.providers.simkl.by_id.find_by_shared_id", found):
            got = await arrival.describe(SETTINGS, [a_filing(season=n) for n in (1, 2, 3)])
        self.assertEqual(len(got), 3)
        found.assert_awaited_once()
        facts.assert_awaited_once()
        self.assertEqual(lookup.await_count, 3)


class AskingForMissingNetworksTests(DistraktTestCase):
    """What the stored calendar cannot answer about a stored row is asked — once
    per title per day. MEASURED: Jury Duty Presents: Company Retreat and Quiet on
    Set stayed blank through every local repair, because no stored calendar
    record names either."""

    async def _row(self, **fields):
        from app import distrakt
        await distrakt.add_month_record(self.user_id, "2026-04", {
            "ids": {"tmdb": 312697, "trakt": 315568}, "season": 1,
            "kind": distrakt.RecordKind.COMPLETED, "title": "Company Retreat", **fields})

    async def _network(self):
        from app import distrakt
        rec = await distrakt.find_month_record(
            self.user_id, "2026-04", distrakt.RecordKind.COMPLETED,
            ItemKey("show", "tmdb", "312697"), 1)
        return rec["network"]

    async def test_a_blank_network_is_asked_of_the_describing_service(self):
        await self._row()
        with _facts("Jury Duty Presents: Company Retreat", "Amazon Prime Video") as asked:
            await arrival.ask_for_missing_networks(self.user_id, SETTINGS)
        self.assertEqual(await self._network(), "Amazon Prime Video")
        self.assertEqual(asked.await_args.args[1], 315568)

    async def test_it_is_asked_once_a_day_not_on_every_load(self):
        await self._row()
        with _facts("", "") as asked:
            await arrival.ask_for_missing_networks(self.user_id, SETTINGS)
            await arrival.ask_for_missing_networks(self.user_id, SETTINGS)
        asked.assert_awaited_once()

    async def test_a_service_that_could_not_be_asked_is_asked_again(self):
        await self._row()
        with patch("app.providers.trakt.detail.fetch_title_facts",
                   AsyncMock(side_effect=SourceUnavailable("down"))) as asked:
            await arrival.ask_for_missing_networks(self.user_id, SETTINGS)
            await arrival.ask_for_missing_networks(self.user_id, SETTINGS)
        self.assertEqual(asked.await_count, 2)

    async def test_a_row_with_a_network_is_not_asked_about(self):
        await self._row(network="HBO")
        with _facts("", "Other") as asked:
            await arrival.ask_for_missing_networks(self.user_id, SETTINGS)
        asked.assert_not_awaited()


class AskingForMissingIdsTests(DistraktTestCase):
    """A stored row's missing service id, asked by shared id. MEASURED: 54 rows
    on the owner's copy owed one after the stored calendar had taught all it
    could — Groomed: The Truth Behind the Lie among them."""

    async def _row(self, ids):
        from app import distrakt
        await distrakt.add_month_record(self.user_id, "2026-08", {
            "ids": ids, "season": 1, "kind": distrakt.RecordKind.COMPLETED,
            "title": "Groomed", "network": "Peacock"})

    async def _ids(self):
        from app import distrakt
        rec = await distrakt.find_month_record(
            self.user_id, "2026-08", distrakt.RecordKind.COMPLETED,
            ItemKey("show", "tmdb", "330599"), 1)
        return rec["ids"]

    async def test_the_missing_service_is_asked_and_its_answer_learned(self):
        await self._row({"tmdb": 330599, "simkl": 2618044})
        found = AsyncMock(return_value={"trakt": 326227,
                                        "trakt_slug": "groomed-the-truth-behind-the-lie"})
        with patch("app.providers.trakt.detail.find_by_shared_id", found):
            wrote = await arrival.ask_for_missing_ids(self.user_id, SETTINGS)
        self.assertGreater(wrote, 0)
        ids = await self._ids()
        self.assertEqual((ids["trakt"], ids["trakt_slug"]),
                         (326227, "groomed-the-truth-behind-the-lie"))
        self.assertEqual(found.await_args.args[2]["tmdb"], 330599)

    async def test_a_load_asks_only_about_the_rows_it_is_drawing(self):
        """An account's rows across every month grow without end, and a title a
        service does not list is asked again each day — so a load asks only about
        what it draws, and an ordinary load costs its page, not its history."""
        await self._row({"tmdb": 330599, "simkl": 2618044})
        found = AsyncMock(return_value={})
        with patch("app.providers.trakt.detail.find_by_shared_id", found):
            await arrival.ask_for_missing_ids(self.user_id, SETTINGS,
                                              within={"show:tmdb:1"})
            await arrival.ask_for_missing_networks(self.user_id, SETTINGS,
                                                   within={"show:tmdb:1"})
        found.assert_not_awaited()
        with patch("app.providers.trakt.detail.find_by_shared_id", found):
            await arrival.ask_for_missing_ids(self.user_id, SETTINGS,
                                              within={"show:tmdb:330599"})
        found.assert_awaited()

    async def test_an_answer_not_naming_that_services_id_is_not_written(self):
        await self._row({"tmdb": 330599, "simkl": 2618044})
        with patch("app.providers.trakt.detail.find_by_shared_id",
                   AsyncMock(return_value={"imdb": "tt1"})):
            self.assertEqual(await arrival.ask_for_missing_ids(self.user_id, SETTINGS), 0)
        self.assertNotIn("imdb", await self._ids())

    async def test_a_service_that_could_not_be_asked_writes_nothing(self):
        await self._row({"tmdb": 330599, "simkl": 2618044})
        with patch("app.providers.trakt.detail.find_by_shared_id",
                   AsyncMock(side_effect=SourceUnavailable("down"))):
            self.assertEqual(await arrival.ask_for_missing_ids(self.user_id, SETTINGS), 0)

    async def test_a_row_holding_both_services_is_not_asked_about(self):
        await self._row({"tmdb": 330599, "simkl": 2618044, "trakt": 326227})
        found = AsyncMock(return_value={})
        with patch("app.providers.trakt.detail.find_by_shared_id", found), \
             patch("app.providers.simkl.by_id.find_by_shared_id", found):
            await arrival.ask_for_missing_ids(self.user_id, SETTINGS)
        found.assert_not_awaited()


class WritingTheMissingPremiereTests(DistraktTestCase):
    """A title added by hand gets its premiere written into the calendar for each
    service that has the title and whose calendar does not draw it. MEASURED:
    Groomed's only stored airing was Simkl's though Trakt lists the title, and
    Trakt's calendar feed omits all three of its dated episodes."""

    REC = {"media": Media.SHOW, "ids": {"tmdb": 330599, "trakt": 326227, "simkl": 2618044},
           "season": 1, "title": "Groomed"}

    async def test_each_service_not_yet_drawn_is_written_from_its_own_id(self):
        fill = AsyncMock(return_value="show:tmdb:330599")
        with patch("app.calendar.search.fill_one_gap", fill):
            written = await arrival.write_missing_premieres(SETTINGS, dict(self.REC))
        self.assertEqual(written, 2)
        asked = {(call.kwargs["source"], call.kwargs["source_id"]) for call in fill.await_args_list}
        self.assertEqual(asked, {("trakt", "326227"), ("simkl", "2618044")})
        self.assertTrue(all(call.kwargs["endpoint_key"] == "shows/premieres"
                            and call.kwargs["season"] == 1 for call in fill.await_args_list))

    async def test_a_service_whose_calendar_already_draws_it_is_left_alone(self):
        await store_calendar([a_calendar_record(
            "simkl", {"tmdb": 330599, "simkl": 2618044})], endpoint="shows/premieres")
        fill = AsyncMock(return_value="x")
        with patch("app.calendar.search.fill_one_gap", fill):
            await arrival.write_missing_premieres(SETTINGS, dict(self.REC))
        self.assertEqual([call.kwargs["source"] for call in fill.await_args_list], ["trakt"])

    async def test_a_service_the_title_has_no_id_for_is_not_asked(self):
        fill = AsyncMock(return_value="x")
        with patch("app.calendar.search.fill_one_gap", fill):
            await arrival.write_missing_premieres(
                SETTINGS, {**self.REC, "ids": {"tmdb": 330599, "simkl": 2618044}})
        self.assertEqual([call.kwargs["source"] for call in fill.await_args_list], ["simkl"])

    async def test_a_write_that_fails_costs_the_add_nothing(self):
        with patch("app.calendar.search.fill_one_gap",
                   AsyncMock(side_effect=SourceUnavailable("down"))):
            self.assertEqual(await arrival.write_missing_premieres(SETTINGS, dict(self.REC)), 0)


# ---------------------------------------------------------------------------
# every way onto the tracker files what describe decided
# ---------------------------------------------------------------------------

CANON_IDS = {"tmdb": 1396, "trakt": 1396, "simkl": 42, "simkl_slug": "canon",
             "tvdb": 81189, "imdb": "tt0903747"}
CANON = {"title": "Canonical Title", "network": "Canonical Net", **SEASON}


async def _canonical_describe(settings, records, *, strict=False, order=None):
    """`describe` as the one authority: whatever a path hands in, the
    description comes back canonical. A path that writes anything else for a
    description field has decided that field for itself."""
    return [arrival.Arrival({**rec, **CANON, "ids": {**(rec.get("ids") or {}), **CANON_IDS}},
                            dict(SEASON))
            for rec in records]


@contextmanager
def _canonical():
    with patch("app.distrakt.arrival.describe", _canonical_describe):
        yield


class _FilesTheDescription:
    def assert_filed_as_described(self, record: dict):
        self.assertIsNotNone(record, "nothing was filed")
        for field in arrival.DESCRIPTION_FIELDS:
            if field == "ids":
                for name, value in CANON_IDS.items():
                    self.assertEqual(record["ids"].get(name), value, f"ids[{name}]")
            elif field in ("started_airing", "finished_airing"):
                self.assertEqual(bool(record.get(field)), bool(CANON[field]), field)
            else:
                self.assertEqual(record.get(field), CANON[field], field)


class EveryPathOfItsOwnFilesTheDescriptionTests(_FilesTheDescription, DistraktTestCase):
    """The three paths that are functions rather than routes."""

    FILING = {"media": Media.SHOW, "ids": {"tmdb": 1396, "trakt": 1396},
              "title": "What the path had", "season": 1, "network": ""}

    async def test_the_calendar_import(self):
        await store.save_month(self.user_id, store.new_month_doc("2026-09"))
        with _canonical(), patch.object(calendar_import, "premiere_records",
                                        AsyncMock(return_value=calendar_import.Premieres(
                                            [dict(self.FILING)]))):
            added = await calendar_import.import_premieres(
                self.user_id, "2026-09", SETTINGS,
                {calendar_import.candidate_ref(self.FILING)})
        self.assertEqual(added, 1)
        doc = await store.load_month(self.user_id, "2026-09")
        self.assert_filed_as_described(doc["shows"][0])

    async def test_the_rollover(self):
        progress = [{**self.FILING, "watched": 3}]
        port = AsyncMock()
        port.fetch_watched_progress = AsyncMock(return_value=progress)
        with _canonical(), patch("app.distrakt.watch_history.tracker_ports",
                                 AsyncMock(return_value=[("trakt", port)])):
            got, = await rollover.history_records(self.user_id, SETTINGS, set())
        self.assert_filed_as_described(got)

    async def test_the_backfill(self):
        port = AsyncMock()
        port.fetch_history = AsyncMock(return_value=[])
        port.movie_plays_from = lambda events: []
        port.watched_progress_from = lambda events: [dict(self.FILING)]
        port.fetch_progress_details = AsyncMock(return_value={
            1396: {1: {n: f"2026-01-{n + 10:02d}T20:00:00Z" for n in range(1, 11)}}})
        from datetime import date
        with _canonical(), patch("app.distrakt.watch_history.tracker_ports",
                                 AsyncMock(return_value=[("trakt", port)])):
            plan = await backfill.survey(self.user_id, SETTINGS, "2026-01", "2026-01",
                                         today=date(2026, 9, 1))
        self.assert_filed_as_described(plan["months"]["2026-01"][0])


class EveryRouteFilesTheDescriptionTests(_FilesTheDescription, _RosterAddCase):
    """The three that are routes: a search add, a finished season stated by
    hand, and saying yes to a history prompt."""

    def test_the_search_add(self):
        with _canonical():
            resp = self._search_add()
        self.assertEqual(resp.status_code, 200, resp.text[:300])
        self.assert_filed_as_described(self._row())

    def test_the_prompt(self):
        with _canonical():
            resp = self._prompt_add()
        self.assertEqual(resp.status_code, 200, resp.text[:300])
        self.assert_filed_as_described(self._row())

    def test_a_finished_season_stated_by_hand(self):
        with _canonical(), self._services(), self._picked():
            resp = self.client.post("/api/distrakt/add-completed?year=2020&month=1",
                                    json={"q": "breaking bad", "i": 0, "season": 1})
        self.assertEqual(resp.status_code, 200, resp.text[:300])
        self.assert_filed_as_described(asyncio.run(store.find_month_record(
            self.user_id, "2020-01", store.RecordKind.COMPLETED, KEY, 1)))


class OnlyAHandAddWritesTheCalendarTests(_RosterAddCase):
    """The three one-title adds write the missing premiere; nothing else does —
    see arrival.write_missing_premieres for why the bulk paths do not."""

    def _spy(self):
        return patch("app.distrakt.arrival.write_missing_premieres", AsyncMock(return_value=0))

    def test_the_search_add_the_prompt_and_a_stated_finish_all_write_it(self):
        for name, act in (
                ("search add", lambda: self._search_add()),
                ("prompt", lambda: self._prompt_add()),
                ("stated finish", lambda: self._post_completed())):
            with self.subTest(path=name), self._spy() as spy:
                act()
                spy.assert_awaited()
                self.assertEqual(spy.await_args.args[1]["season"], 1)

    def _post_completed(self):
        with self._services(), self._picked():
            return self.client.post("/api/distrakt/add-completed?year=2020&month=1",
                                    json={"q": "breaking bad", "i": 0, "season": 1})


class TheBulkPathsWriteNoCalendarTests(DistraktTestCase):
    async def test_the_import_the_rollover_and_the_backfill_do_not(self):
        with patch("app.distrakt.arrival.write_missing_premieres", AsyncMock()) as spy:
            await EveryPathOfItsOwnFilesTheDescriptionTests.test_the_calendar_import(self)
            await EveryPathOfItsOwnFilesTheDescriptionTests.test_the_rollover(self)
            await EveryPathOfItsOwnFilesTheDescriptionTests.test_the_backfill(self)
        spy.assert_not_awaited()

    FILING = EveryPathOfItsOwnFilesTheDescriptionTests.FILING
    assert_filed_as_described = _FilesTheDescription.assert_filed_as_described
