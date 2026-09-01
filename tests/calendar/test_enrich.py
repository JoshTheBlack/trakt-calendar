"""app/calendar/enrich.py: the read-time overlay of `simkl_titles` onto Simkl
records, the heartbeat drain that derives its own work from the stored
calendar cache, and the retention sweep.

THE CENTRAL CLAIM UNDER TEST: a Simkl record with no genres/country/
certification is exempt from a viewer's filter on those dimensions until the
drain has actually looked it up — not hidden, and not treated as though it had
already answered "none". Everything else here is the machinery that makes that
claim true without ever making a network call from the read path, and without
ever silently dropping a title the stored calendar names.
"""
from __future__ import annotations

import asyncio
import unittest
from datetime import date
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

from app import db
from app.calendar import cache as calendar_cache, enrich as calendar_enrich
from app.calendar import entries as calendar_entries
from app.config import Settings
from app.endpoints import get_endpoint
from app.providers.base import Media, Record, Source
from app.providers.simkl import titles as simkl_titles
from app.providers.simkl import transport as simkl_transport
from tests.support import migrated_db

SHOWS = get_endpoint("shows")
MOVIES = get_endpoint("movies")

# 2026-07-07T12:00:00Z — inside the aligned window starting 2026-07-06 that
# the end-to-end tests below store under, and inside their [7/7, 7/7] read
# span. Everything in this file that needs "an ordinary airing" uses this.
_AIR_TS = 1783425600.0

# A fetch_title answer with every key present but nothing to say — the shape
# titles.py always returns even for a title with no runtime or genres. Used
# wherever a test only cares that a call SUCCEEDED, not what it found.
_OK_FIELDS = {
    "extract_version": simkl_titles.EXTRACT_VERSION,
    "genres": [], "network": "", "country": "", "certification": "",
    "runtime": None, "status": "", "overview": "", "ids": {},
    "type": "", "anime_type": "", "total_episodes": None, "poster": "",
    "first_aired": "", "trailers": [],
}


def _simkl_record(simkl_id, *, title="Moonshadow", genres=(), country="", certification=""):
    """One Simkl record exactly as app/providers/simkl/calendar.py's
    to_show_record produces it: enriched=False, none of the filter fields set —
    unless a test asks for pre-filled ones to prove overlay overwrites them."""
    return Record(
        source=Source.SIMKL, media=Media.SHOW, id=str(simkl_id),
        ids={"simkl": simkl_id, "slug": f"title-{simkl_id}"},
        detail_url="https://simkl.com", title=title, air_ts=_AIR_TS,
        season=1, episode_number=1, episode_label="S01E01",
        genres=list(genres), country=country, certification=certification,
        enriched=False,
    )


class EnrichTestCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        migrated_db("enrich")

    async def asyncTearDown(self):
        db.close_thread_connection()

    async def _stored(self, records, *, start=date(2026, 7, 6), now=1000):
        """Store one calendar window naming `records`, exactly as FILL would —
        the only thing `_owed_titles`/`drain` read from, and deliberately never
        given any enrichment in these tests unless a test says so."""
        await calendar_cache.store_window(
            SHOWS.key, start, records, 600, now,
            sources=["simkl"], asked=["simkl"])


class EnrichmentReachesTheStoredRowTests(EnrichTestCase):
    """What a lookup learned, as the calendar READ hands it back.

    THESE ARE THE OVERLAY'S TESTS, ASKED OF THE NEW PATH. The claims have not
    changed -- a title nobody has looked up stays unenriched, an answered one
    comes back filled, a narrower answer leaves the fields it never carried at
    their defaults, ids are added but never overwritten, and one row answers
    twenty airings. What changed is where the answer lives: the drain writes it
    onto `calendar_titles` and the read serves the row, instead of every read
    joining `simkl_titles` back in.

    So each of these stores a window FIRST and enriches after, which is the real
    order of events -- a fill lands, the drain answers later -- and reads through
    `read_span`, the same call the calendar itself makes.
    """

    async def _one(self, simkl_id=1):
        records, *_ = await calendar_entries.read_span(
            SHOWS.key, date(2026, 7, 6), date(2026, 7, 13))
        return records[0]

    async def test_a_title_nobody_has_looked_up_is_left_unenriched(self):
        await self._stored([_simkl_record(1)])
        got = await self._one()
        self.assertFalse(got.enriched)
        self.assertEqual(got.genres, [])

    async def test_an_answered_title_comes_back_filled_and_marked(self):
        await self._stored([_simkl_record(1)])
        await calendar_enrich._upsert_success(1, "show", {
            "genres": ["drama", "game-show"], "network": "AMC", "country": "US",
            "certification": "TV-MA", "runtime": 45, "status": "ended",
            "overview": "An overview.", "ids": {"tvdb": "999"},
        }, now=1000)
        got = await self._one()
        self.assertTrue(got.enriched)
        self.assertEqual(got.genres, ["drama", "game-show"])
        self.assertEqual(got.country, "US")
        self.assertEqual(got.certification, "TV-MA")
        self.assertEqual(got.network, "AMC")
        self.assertEqual(got.runtime, 45)
        self.assertEqual(got.ids["tvdb"], "999")

    async def test_the_movie_fields_reach_the_record_the_card_draws(self):
        """Simkl's calendar CDN entries carry no language and no year, so those
        chips were blank on every Simkl card — the fields existed on the Record
        and nothing filled them in."""
        await self._stored([_simkl_record(1)])
        await calendar_enrich._upsert_success(1, "show", {
            "language": "EN", "year": 2026,
        }, now=1000)
        got = await self._one()
        self.assertEqual((got.language, got.year), ("EN", 2026))

    async def test_a_narrower_answer_leaves_the_fields_it_never_had_at_defaults(self):
        """A row the narrower extraction wrote has no key for any of them, and
        must read as "nothing to say" rather than as a value — it is owed a
        re-fetch and still applies everything it does know in the meantime."""
        await self._stored([_simkl_record(1)])
        await calendar_enrich._upsert_success(1, "show", {
            "genres": ["drama"], "network": "AMC",
        }, now=1000)
        got = await self._one()
        self.assertEqual((got.language, got.year), ("", ""))
        self.assertEqual(got.genres, ["drama"])

    async def test_enrichment_upgrades_ids_without_overwriting_ones_already_there(self):
        """The calendar file's own ids must survive; enrichment only ADDS a
        namespace the fill never had (tvdb, mal, anidb). A lookup overwriting one
        would be re-identifying the title under cover of enriching it."""
        record = _simkl_record(1)
        record.ids["mal"] = "already-there"
        await self._stored([record])
        await calendar_enrich._upsert_success(1, "show", {
            "genres": [], "ids": {"tvdb": "999", "mal": "should-not-override"},
        }, now=1000)
        got = await self._one()
        self.assertEqual(got.ids["tvdb"], "999")
        self.assertEqual(got.ids["mal"], "already-there")
        self.assertEqual(got.ids["simkl"], 1)

    async def test_a_twenty_airing_show_is_answered_from_one_row(self):
        """One title airing twenty times is twenty airings and ONE title row, so
        the answer arrives on all of them from a single write. Under the overlay
        this was a claim about one query serving twenty records; it is now a
        claim about there being one row to write at all.

        TWENTY DISTINCT AIR TIMES, because that is what twenty airings ARE. The
        air instant is part of an airing's identity, so twenty records naming the
        same one are not twenty airings — they are one, listed twenty times, and
        `dedupe_records` removes exactly that before a fill ever reaches storage.
        """
        airings = []
        for hour in range(20):
            record = _simkl_record(1)
            record.air_ts = _AIR_TS + hour * 3600
            airings.append(record)
        await self._stored(airings)
        await calendar_enrich._upsert_success(1, "show", {"genres": ["drama"]}, now=1000)
        records, *_ = await calendar_entries.read_span(
            SHOWS.key, date(2026, 7, 6), date(2026, 7, 13))
        self.assertEqual(len(records), 20)
        self.assertTrue(all(r.enriched for r in records))
        self.assertTrue(all(r.genres == ["drama"] for r in records))
        self.assertEqual(
            await db.fetch_value("SELECT COUNT(*) FROM calendar_titles"), 1)

    async def test_a_trakt_record_is_left_alone(self):
        """Trakt's calendar carries these fields already, so its records arrive
        enriched and no lookup is owed for them."""
        await self._stored([Record(
            source=Source.TRAKT, media=Media.SHOW, id="x", ids={"trakt": 1},
            detail_url="https://trakt.tv", title="X", air_ts=_AIR_TS)])
        got = await self._one()
        self.assertTrue(got.enriched)

    async def test_the_read_makes_no_outbound_call(self):
        """The read path may never enrich — see app/calendar/enrich.py's module
        docstring. A patched fetch_title that is never awaited is the proof, and
        it matters more now than it did: the read serves stored columns, so
        there is no join left that could tempt anyone to fall back to a fetch."""
        await self._stored([_simkl_record(1)])
        spy = AsyncMock(side_effect=AssertionError("must not fetch"))
        with patch("app.providers.simkl.titles.fetch_title", spy):
            await calendar_entries.read_span(
                SHOWS.key, date(2026, 7, 6), date(2026, 7, 13))
        spy.assert_not_awaited()


class OwedTitlesTests(EnrichTestCase):
    """`_owed_titles`, in isolation: what the drain derives straight off the
    stored calendar cache, with no read and no queue involved at all."""

    async def test_a_stored_simkl_record_is_owed_by_id_media_and_title(self):
        await self._stored([_simkl_record(9001, title="Moonshadow")])
        owed = await calendar_enrich._owed_titles()
        self.assertEqual(owed, {(9001, "show"): "Moonshadow"})

    async def test_a_trakt_only_group_names_nothing_owed(self):
        trakt_record = Record(
            source=Source.TRAKT, media=Media.SHOW, id="x", ids={"trakt": 1},
            detail_url="https://trakt.tv", title="X", air_ts=_AIR_TS,
        )
        await self._stored([trakt_record])
        self.assertEqual(await calendar_enrich._owed_titles(), {})

    async def test_an_already_answered_title_is_still_returned(self):
        """Filtering OUT what is already answered is `drain`'s job, not this
        function's — it stays a pure "what does the cache name" question."""
        await calendar_enrich._upsert_success(9001, "show", {
            "genres": ["drama"], "network": "", "country": "", "certification": "",
            "runtime": None, "status": "", "overview": "", "ids": {},
        }, now=1000)
        await self._stored([_simkl_record(9001, title="Moonshadow")])
        owed = await calendar_enrich._owed_titles()
        self.assertEqual(owed, {(9001, "show"): "Moonshadow"})

    async def test_nothing_cached_is_nothing_owed(self):
        self.assertEqual(await calendar_enrich._owed_titles(), {})


class DrainTests(EnrichTestCase):
    SETTINGS = SimpleNamespace(simkl_client_id="cid", simkl_access_token="",
                               simkl_catalogue_configured=True)

    async def test_nothing_owed_fetches_nothing(self):
        self.assertEqual(await calendar_enrich.drain(self.SETTINGS), 0)

    async def test_a_blocked_instance_records_nothing_against_its_titles(self):
        """The defect this closes, observed on a live deployment: a 412 is an
        instance-wide refusal, `fetch_title` answers None for every failure it
        meets, and the drain wrote that None down as "this title failed" — 372 of
        one instance's 388 rows, every one a title Simkl would have answered for,
        each carrying a backoff it had not earned. A blocked call never reached
        Simkl to have an opinion about any id."""
        await self._stored([_simkl_record(9001, title="Moonshadow")])
        simkl_transport._open_breaker("/tv/9001")
        self.addCleanup(simkl_transport._close_breaker)

        with patch("app.providers.simkl.titles.fetch_title", AsyncMock(return_value=None)):
            self.assertEqual(await calendar_enrich.drain(self.SETTINGS), 0)

        row = await db.fetch_one(
            "SELECT failed_at FROM simkl_titles WHERE simkl_id = 9001 AND media = 'show'")
        self.assertIsNone(row, "a blocked pass wrote a failure row against a good title")

    async def test_an_instance_with_no_client_id_asks_for_nothing(self):
        """Simkl's calendar takes no credential, so an instance can hold a full
        queue of Simkl titles and no client id at all. Firing that queue anyway
        sends an empty id, and Simkl answers 412 — which this app honours as an
        instance-wide refusal by refusing EVERY Simkl call for 900 seconds, the
        detail modals and signing in with Simkl included. A missing credential
        has to degrade to "cannot enrich", not to a quarter-hour outage.

        The fetch is patched to fail loudly rather than asserted about
        afterwards: the point is that nothing is called, not that nothing is
        stored."""
        await self._stored([_simkl_record(9001, title="Moonshadow")])
        unconfigured = SimpleNamespace(simkl_client_id="", simkl_access_token="",
                                       simkl_catalogue_configured=False)

        def _explode(*args, **kwargs):
            raise AssertionError("the drain called Simkl with no client id")

        with patch("app.providers.simkl.titles.fetch_title", _explode):
            self.assertEqual(await calendar_enrich.drain(unconfigured), 0)

    async def test_owed_work_survives_a_restart_with_nothing_in_memory(self):
        """The stored calendar window and simkl_titles are the only things
        that decide what is owed — no read ever happens here,
        which is the point: nothing in memory has to survive a restart for the
        drain to find its work."""
        await self._stored([_simkl_record(9001, title="Moonshadow")])
        with patch("app.providers.simkl.titles.fetch_title", AsyncMock(return_value=_OK_FIELDS)):
            fetched = await calendar_enrich.drain(self.SETTINGS)
        self.assertEqual(fetched, 1)
        row = await db.fetch_one(
            "SELECT payload FROM simkl_titles WHERE simkl_id = 9001 AND media = 'show'")
        self.assertIsNotNone(row)

    async def test_a_title_surfaced_while_the_old_queue_would_have_been_full_is_still_fetched(self):
        """Regression for the reported defect: `simkl_titles` grew 100 -> 260
        while two titles that had aired and rendered many times were never
        attempted, because the old in-memory queue capped at 500 entries and
        silently dropped anything offered while full. Here the calendar cache
        alone names more distinct titles than that old cap — and, crucially,
        no read (the only thing that ever fed the old queue) is
        called at all — yet every single one is still found and fetched within
        a bounded number of drain ticks. That is impossible for a design whose
        drain can only ever see what a read happened to queue."""
        # 505 TITLES, AND THE BATCH SHRUNK TO REACH THEM OVER MANY TICKS. Both
        # numbers are chosen against what the defect was rather than against the
        # production batch: the old queue held 500 ENTRIES, so the stored window
        # has to name more than 500 distinct titles for a dropped one to be
        # possible at all, and the drain has to run more than once for "still
        # found on a later tick" to mean anything. Sizing the roster off the real
        # DRAIN_BATCH_SIZE instead named 7,505 titles and fetched every one of
        # them, which cost 48 seconds — most of this file, and a tenth of the
        # whole suite — to demonstrate nothing the 505 do not.
        #
        # The real batch size is not left untested: test_the_per_tick_bound_
        # still_holds below is exactly that assertion, and it is the only thing
        # here that should depend on the constant's value.
        total = 505
        batch = 25
        records = [_simkl_record(n, title=f"Title {n}") for n in range(1, total + 1)]
        await self._stored(records)
        fetched_total = 0
        with patch("app.providers.simkl.titles.fetch_title", AsyncMock(return_value=_OK_FIELDS)), \
                patch.object(calendar_enrich, "DRAIN_BATCH_SIZE", batch):
            for _ in range(total // batch + 5):  # ceil(total / batch) with margin
                fetched = await calendar_enrich.drain(self.SETTINGS)
                fetched_total += fetched
                if fetched == 0:
                    break
        self.assertEqual(fetched_total, total)
        rows = await db.fetch_all("SELECT COUNT(*) AS n FROM simkl_titles")
        self.assertEqual(rows[0]["n"], total)

    async def test_the_per_tick_bound_still_holds(self):
        records = [_simkl_record(n, title=f"T{n}")
                  for n in range(1, calendar_enrich.DRAIN_BATCH_SIZE + 6)]
        await self._stored(records)
        with patch("app.providers.simkl.titles.fetch_title", AsyncMock(return_value=_OK_FIELDS)):
            fetched = await calendar_enrich.drain(self.SETTINGS)
        self.assertEqual(fetched, calendar_enrich.DRAIN_BATCH_SIZE)
        rows = await db.fetch_all("SELECT COUNT(*) AS n FROM simkl_titles")
        self.assertEqual(rows[0]["n"], calendar_enrich.DRAIN_BATCH_SIZE)

    async def test_a_title_inside_its_backoff_is_not_refetched(self):
        await self._stored([_simkl_record(1, title="X")])
        await calendar_enrich._upsert_failure(1, "show", now=1000)
        spy = AsyncMock(return_value=_OK_FIELDS)
        with patch("app.providers.simkl.titles.fetch_title", spy):
            fetched = await calendar_enrich.drain(self.SETTINGS, now=1000 + 10)
        self.assertEqual(fetched, 0)
        spy.assert_not_awaited()

    async def test_a_title_past_its_backoff_is_refetched(self):
        await self._stored([_simkl_record(1, title="X")])
        await calendar_enrich._upsert_failure(1, "show", now=1000)
        with patch("app.providers.simkl.titles.fetch_title", AsyncMock(return_value=_OK_FIELDS)):
            fetched = await calendar_enrich.drain(
                self.SETTINGS, now=1000 + calendar_enrich._BACKOFF_BASE_SECONDS + 1)
        self.assertEqual(fetched, 1)

    async def test_a_repeated_failure_backs_off_longer(self):
        await self._stored([_simkl_record(1, title="X")])
        await calendar_enrich._upsert_failure(1, "show", now=1000)
        await calendar_enrich._upsert_failure(1, "show", now=1000)  # fail_count -> 2
        spy = AsyncMock(return_value=_OK_FIELDS)
        # Past the first-failure backoff but not the second's.
        with patch("app.providers.simkl.titles.fetch_title", spy):
            fetched = await calendar_enrich.drain(
                self.SETTINGS, now=1000 + calendar_enrich._BACKOFF_BASE_SECONDS + 1)
        self.assertEqual(fetched, 0)
        spy.assert_not_awaited()

    async def test_an_already_answered_title_is_not_refetched(self):
        await self._stored([_simkl_record(1, title="X")])
        await calendar_enrich._upsert_success(1, "show", {
            "extract_version": simkl_titles.EXTRACT_VERSION,
            "genres": ["drama"], "network": "", "country": "", "certification": "",
            "runtime": None, "status": "", "overview": "", "ids": {},
        }, now=1000)
        spy = AsyncMock(return_value=_OK_FIELDS)
        with patch("app.providers.simkl.titles.fetch_title", spy):
            fetched = await calendar_enrich.drain(self.SETTINGS, now=2000)
        self.assertEqual(fetched, 0)
        spy.assert_not_awaited()

    async def test_a_row_from_the_older_extraction_is_re_fetched_not_treated_as_complete(self):
        """A row written before ids/type/anime_type/trailers existed carries
        no `extract_version` key at all — the shape every one of the ~260
        rows already in a live simkl_titles table is in. It must be treated
        as OWED, not as already answered, so the author does not wait a full
        30-day retention cycle for the fields that make matching cheap."""
        await self._stored([_simkl_record(1, title="X")])
        await calendar_enrich._upsert_success(1, "show", {
            # The exact shape the narrower extraction wrote: no
            # extract_version, no type/anime_type/trailers, ids limited to
            # tvdb/mal/anidb.
            "genres": ["drama"], "network": "AMC", "country": "US",
            "certification": "", "runtime": None, "status": "", "overview": "",
            "ids": {"tvdb": "999"},
        }, now=1000)
        spy = AsyncMock(return_value=_OK_FIELDS)
        with patch("app.providers.simkl.titles.fetch_title", spy):
            fetched = await calendar_enrich.drain(self.SETTINGS, now=1000)
        self.assertEqual(fetched, 1)
        spy.assert_awaited_once()

    async def test_a_stale_row_still_applies_its_old_fields_while_owed(self):
        """Applying stored enrichment is not gated on extract_version — it uses
        whatever an old row already carries, so a title enriched under the
        narrower extraction does not regress to unenriched while it waits for the
        drain to re-fetch it under the wider shape."""
        await calendar_enrich._upsert_success(1, "show", {
            "genres": ["drama"], "network": "AMC", "country": "US",
            "certification": "", "runtime": None, "status": "", "overview": "",
            "ids": {"tvdb": "999"},
        }, now=1000)
        await self._stored([_simkl_record(1)])
        records, *_ = await calendar_entries.read_span(
            SHOWS.key, date(2026, 7, 6), date(2026, 7, 13))
        got = records[0]
        self.assertTrue(got.enriched)
        self.assertEqual(got.network, "AMC")
        self.assertEqual(got.anime_type, "")  # not carried by the old shape

    async def test_a_failed_lookup_still_writes_a_row_so_it_is_not_requeued_forever(self):
        await self._stored([_simkl_record(1, title="X")])
        with patch("app.providers.simkl.titles.fetch_title", AsyncMock(return_value=None)):
            fetched = await calendar_enrich.drain(self.SETTINGS)
        self.assertEqual(fetched, 0)
        row = await db.fetch_one(
            "SELECT fail_count FROM simkl_titles WHERE simkl_id = 1 AND media = 'show'")
        self.assertEqual(row["fail_count"], 1)

    async def test_one_failing_id_does_not_stop_the_rest_of_the_batch(self):
        await self._stored([_simkl_record(1, title="A"), _simkl_record(2, title="B")])

        async def fake_fetch(settings, simkl_id, media):
            if simkl_id == 1:
                raise RuntimeError("boom")
            return _OK_FIELDS

        with patch("app.providers.simkl.titles.fetch_title", side_effect=fake_fetch):
            fetched = await calendar_enrich.drain(self.SETTINGS)
        self.assertEqual(fetched, 1)

    async def test_a_titles_name_reaches_the_debug_log(self):
        """Author-requested: the drain must say WHICH title it enriched, by
        name, not only by id — an id alone is unreadable in a log."""
        await self._stored([_simkl_record(9001, title="Moonshadow")])
        with patch("app.providers.simkl.titles.fetch_title", AsyncMock(return_value=_OK_FIELDS)):
            with self.assertLogs("app.calendar.enrich", level="DEBUG") as captured:
                await calendar_enrich.drain(self.SETTINGS)
        self.assertTrue(any("Moonshadow" in line for line in captured.output))


class AnAnswerThatArrivedAfterTheRowStillReachesItTests(EnrichTestCase):
    """THE CASE A DELETED MECHANISM LEFT BEHIND, found on a live calendar.

    A calendar row starts unenriched and only a write marks it otherwise. The
    drain SKIPS any title `simkl_titles` already answers — so a row whose answer
    landed a moment after it was written, or was stored by a version of this app
    that did not yet project onto the calendar, is answered in one table and
    blank in the other, and never revisited. It stays blank for as long as it
    exists.

    THIS WAS BUILT, THEN REMOVED ON A WRONG ARGUMENT: that applying enrichment at
    FILL made it unreachable. Fill-time application covers rows a fill CREATES,
    not rows that already exist and are waiting — and a span that has been filled
    is not due another fill for a day. Five films on one real August calendar sat
    unenriched with complete stored answers beside them.

    NO FILL HAPPENS IN THESE TESTS, deliberately. That is the whole gap: the
    other class below covers the fill path, and it passing is exactly what made
    the removal look safe.
    """

    SETTINGS = SimpleNamespace(simkl_client_id="cid", simkl_access_token="",
                               simkl_catalogue_configured=True)

    async def _one(self):
        records, *_ = await calendar_entries.read_span(
            SHOWS.key, date(2026, 7, 6), date(2026, 7, 13))
        return records[0]

    async def test_a_stored_answer_reaches_a_row_that_never_got_it(self):
        # The row first, unenriched, exactly as a fill leaves one.
        await self._stored([_simkl_record(1)])
        self.assertFalse((await self._one()).enriched)
        # Then the answer, written WITHOUT the projection — the state a lookup
        # that crossed with the row leaves behind.
        await calendar_enrich._upsert_success(1, "show", {
            "extract_version": simkl_titles.EXTRACT_VERSION,
            "genres": ["drama"], "network": "AMC",
        }, now=1000)
        await db.execute("UPDATE calendar_titles SET enriched = 0, genres_json = '[]', "
                         "network = '' WHERE source = 'simkl'")
        self.assertFalse((await self._one()).enriched)

        never = AsyncMock(side_effect=AssertionError("a stored answer was refetched"))
        with patch("app.providers.simkl.titles.fetch_title", never):
            await calendar_enrich.drain(self.SETTINGS, now=2000)
        got = await self._one()
        self.assertTrue(got.enriched)
        self.assertEqual(got.genres, ["drama"])
        self.assertEqual(got.network, "AMC")
        never.assert_not_awaited()

    async def test_a_stored_failure_is_not_mistaken_for_an_answer(self):
        """A title Simkl returns nothing for has a row with an EMPTY payload, and
        that is not something to project. Its record stays unenriched, which is
        what keeps the filter exempting it rather than judging it on defaults it
        never had."""
        await self._stored([_simkl_record(1)])
        await calendar_enrich._upsert_failure(1, "show", now=1000)
        with patch("app.providers.simkl.titles.fetch_title",
                   AsyncMock(return_value=_OK_FIELDS)):
            await calendar_enrich.drain(self.SETTINGS, now=1000 + 10)
        self.assertFalse((await self._one()).enriched)

    async def test_a_second_pass_finds_nothing_left_to_hand_over(self):
        """Bounded work, not work every tick: `enriched` is what says the row has
        its answer."""
        await self._stored([_simkl_record(1)])
        await calendar_enrich._upsert_success(1, "show", {
            "extract_version": simkl_titles.EXTRACT_VERSION, "genres": ["drama"],
        }, now=1000)
        await db.execute("UPDATE calendar_titles SET enriched = 0 WHERE source = 'simkl'")
        with patch("app.providers.simkl.titles.fetch_title",
                   AsyncMock(return_value=_OK_FIELDS)):
            await calendar_enrich.drain(self.SETTINGS, now=2000)
        self.assertEqual(
            await calendar_entries.unenriched("simkl", "simkl"), set())


class AnAnswerAlreadyPaidForGoesInWithTheFillTests(EnrichTestCase):
    """A REFILL MUST NOT BLANK WHAT IS ALREADY KNOWN, and this is the case that
    would have made it look like enrichment never worked.

    A fresh calendar payload is unenriched by construction. Stored as-is, every
    title the drain had already answered for would render bare until some later
    drain tick noticed — and on the machine this was written for that was 13,634
    titles the moment the calendar moved to rows, since every stored answer
    predates every stored row. The read-time overlay used to hide the whole
    problem by reapplying the answer on every read; there is no overlay now, so
    the answer has to go in WITH the records.
    """

    SETTINGS = SimpleNamespace(simkl_client_id="cid", simkl_access_token="",
                               simkl_catalogue_configured=True)

    async def _one(self):
        records, *_ = await calendar_entries.read_span(
            SHOWS.key, date(2026, 7, 6), date(2026, 7, 13))
        return records[0]

    async def test_a_refill_carries_the_stored_answer_in_with_it(self):
        await calendar_enrich._upsert_success(1, "show", {
            "extract_version": simkl_titles.EXTRACT_VERSION,
            "genres": ["drama"], "network": "AMC", "country": "US",
            "certification": "TV-MA", "runtime": 45, "status": "ended",
            "overview": "An overview.",
        }, now=1000)
        # The refill: a fresh, unenriched payload for a title already answered.
        await self._stored([_simkl_record(1)])
        got = await self._one()
        self.assertTrue(got.enriched)
        self.assertEqual(got.genres, ["drama"])
        self.assertEqual(got.network, "AMC")

    async def test_it_costs_no_lookup(self):
        """These are answers the instance already paid for. Reaching for the
        network here would turn every refill into a burst of lookups for titles
        nothing had forgotten."""
        await calendar_enrich._upsert_success(1, "show", {
            "extract_version": simkl_titles.EXTRACT_VERSION, "genres": ["drama"],
        }, now=1000)
        never = AsyncMock(side_effect=AssertionError("a stored answer was refetched"))
        with patch("app.providers.simkl.titles.fetch_title", never):
            await self._stored([_simkl_record(1)])
        never.assert_not_awaited()
        self.assertTrue((await self._one()).enriched)

    async def test_a_title_with_no_answer_is_stored_unenriched(self):
        """The other side of the branch: nothing known means nothing claimed, so
        the filter goes on exempting it rather than judging it on defaults."""
        await self._stored([_simkl_record(2)])
        got = await self._one()
        self.assertFalse(got.enriched)
        self.assertEqual(got.genres, [])


class SweepTests(EnrichTestCase):
    SETTINGS = SimpleNamespace(simkl_client_id="cid", simkl_access_token="",
                               simkl_catalogue_configured=True)

    async def test_a_row_past_retention_is_deleted(self):
        await calendar_enrich._upsert_success(1, "show", {
            "genres": [], "network": "", "country": "", "certification": "",
            "runtime": None, "status": "", "overview": "", "ids": {},
        }, now=1000)
        removed = await calendar_enrich.sweep(now=1000 + calendar_enrich.RETENTION_SECONDS + 1)
        self.assertEqual(removed, 1)
        rows = await db.fetch_all("SELECT COUNT(*) AS n FROM simkl_titles")
        self.assertEqual(rows[0]["n"], 0)

    async def test_a_fresh_row_survives_the_sweep(self):
        await calendar_enrich._upsert_success(1, "show", {
            "genres": [], "network": "", "country": "", "certification": "",
            "runtime": None, "status": "", "overview": "", "ids": {},
        }, now=1000)
        removed = await calendar_enrich.sweep(now=1000 + 10)
        self.assertEqual(removed, 0)

    async def test_a_swept_row_is_simply_re_owed_on_the_next_drain(self):
        """Retention is not data loss — it is a forced recheck. Deleting the
        row must make the id look unattempted again, not permanently failed."""
        await calendar_enrich._upsert_success(1, "show", {
            "genres": ["drama"], "network": "", "country": "", "certification": "",
            "runtime": None, "status": "", "overview": "", "ids": {},
        }, now=1000)
        await calendar_enrich.sweep(now=1000 + calendar_enrich.RETENTION_SECONDS + 1)
        await self._stored([_simkl_record(1, title="X")])
        with patch("app.providers.simkl.titles.fetch_title", AsyncMock(return_value=_OK_FIELDS)) as spy:
            fetched = await calendar_enrich.drain(self.SETTINGS)
        self.assertEqual(fetched, 1)
        spy.assert_awaited_once()


class FilterExemptionThroughAssembleRangeTests(unittest.IsolatedAsyncioTestCase):
    """The end-to-end case: a real viewer read, over a real stored window,
    with a real per-viewer genre filter — the shape the Moonshadow/You Maniac
    report actually took."""

    async def asyncSetUp(self):
        migrated_db("enrich-assemble")
        self.settings = Settings()

    async def asyncTearDown(self):
        db.close_thread_connection()

    async def _stored(self, records, *, now=1000):
        await calendar_cache.store_window(
            SHOWS.key, date(2026, 7, 6), records, 600, now,
            sources=["trakt", "simkl"], asked=["trakt", "simkl"])

    async def test_an_unenriched_simkl_record_survives_an_include_only_genre_filter(self):
        """Read BEFORE the drain has ever looked at this title: the record
        must not be silently hidden by a filter it cannot answer for yet."""
        await self._stored([_simkl_record(1, title="Moonshadow")])
        grouped, meta = await calendar_cache.assemble_range(
            SHOWS, self.settings, tz=ZoneInfo("UTC"),
            start_date=date(2026, 7, 7), end_date=date(2026, 7, 7),
            genres="drama", now=1000)
        titles = [i.title for g in grouped for i in g["items"]]
        self.assertIn("Moonshadow", titles)
        self.assertEqual(meta["unenriched"], 1)

    async def test_once_enriched_the_same_filter_actually_applies(self):
        """The other half of the same claim: enrichment must not become a
        permanent bypass — once the drain has answered, the ordinary filter
        judges the record like any other."""
        await calendar_enrich._upsert_success(1, "show", {
            "genres": ["comedy"], "network": "", "country": "", "certification": "",
            "runtime": None, "status": "", "overview": "", "ids": {},
        }, now=999)
        await self._stored([_simkl_record(1, title="Moonshadow")])
        grouped, meta = await calendar_cache.assemble_range(
            SHOWS, self.settings, tz=ZoneInfo("UTC"),
            start_date=date(2026, 7, 7), end_date=date(2026, 7, 7),
            genres="drama", now=1000)
        titles = [i.title for g in grouped for i in g["items"]]
        self.assertNotIn("Moonshadow", titles)
        self.assertEqual(meta["unenriched"], 0)

    async def test_a_country_filter_no_longer_depends_on_which_source_won_the_group(self):
        """THE REPORTED DEFECT, reproduced and closed: under source=both a group
        naming both services used to resolve to Trakt's record (which HAS a
        country and gets filtered) while source=simkl resolved to Simkl's own
        record (no country, exempt) — the same title, filtered or not, by
        which service happened to answer. With enrichment, Simkl's own record
        carries a country too and is judged the same way Trakt's is."""
        from app.sources import prefs as source_prefs

        await calendar_enrich._upsert_success(2, "show", {
            "genres": [], "network": "", "country": "KR", "certification": "",
            "runtime": None, "status": "", "overview": "", "ids": {},
        }, now=999)
        trakt_record = Record(
            source=Source.TRAKT, media=Media.SHOW, id="moonshadow", ids={"tmdb": 100},
            detail_url="https://trakt.tv", title="Moonshadow", air_ts=_AIR_TS,
            season=1, episode_number=1, country="KR",
        )
        simkl_record = _simkl_record(2, title="Moonshadow")
        simkl_record.ids["tmdb"] = 100  # same waterfall id: one group, two sources
        await self._stored([trakt_record, simkl_record])

        for selection in ("trakt", "simkl", "both"):
            prefs = source_prefs.SourcePrefs(user_id=1, calendar_source=selection)
            grouped, _meta = await calendar_cache.assemble_range(
                SHOWS, self.settings, tz=ZoneInfo("UTC"),
                start_date=date(2026, 7, 7), end_date=date(2026, 7, 7),
                countries="-kr", now=1000, prefs=prefs)
            titles = [i.title for g in grouped for i in g["items"]]
            self.assertNotIn("Moonshadow", titles, f"selection={selection}")


class FilmPruneThroughAssembleRangeTests(unittest.IsolatedAsyncioTestCase):
    """End-to-end: the worked example from the report (Shiranuhi, `anime_type:
    "movie"`, `total_episodes: 1`) reaches a series endpoint's read exactly
    as it did live, and the prune closes it — including the transitional
    state where enrichment has not landed yet."""

    async def asyncSetUp(self):
        migrated_db("enrich-film-prune")
        self.settings = Settings()

    async def asyncTearDown(self):
        db.close_thread_connection()

    async def _stored(self, records, *, now=1000):
        await calendar_cache.store_window(
            SHOWS.key, date(2026, 7, 6), records, 600, now,
            sources=["trakt", "simkl"], asked=["trakt", "simkl"])

    async def test_an_unenriched_film_still_renders(self):
        """THE STATED, ACCEPTABLE GAP: the truth only arrives with
        enrichment, so a film that has not been looked up yet renders as an
        ordinary series entry until its simkl_titles row lands."""
        await self._stored([_simkl_record(1, title="Shiranuhi")])
        grouped, _meta = await calendar_cache.assemble_range(
            SHOWS, self.settings, tz=ZoneInfo("UTC"),
            start_date=date(2026, 7, 7), end_date=date(2026, 7, 7), now=1000)
        titles = [i.title for g in grouped for i in g["items"]]
        self.assertIn("Shiranuhi", titles)

    async def test_the_same_film_is_pruned_once_enriched(self):
        await calendar_enrich._upsert_success(1, "show", {
            "genres": ["anime"], "network": "", "country": "", "certification": "",
            "runtime": None, "status": "", "overview": "", "ids": {},
            "anime_type": "movie",
        }, now=999)
        await self._stored([_simkl_record(1, title="Shiranuhi")])
        grouped, _meta = await calendar_cache.assemble_range(
            SHOWS, self.settings, tz=ZoneInfo("UTC"),
            start_date=date(2026, 7, 7), end_date=date(2026, 7, 7), now=1000)
        titles = [i.title for g in grouped for i in g["items"]]
        self.assertNotIn("Shiranuhi", titles)

    async def test_a_one_episode_ona_series_is_not_pruned(self):
        """The Ribbon Hero worked example: total_episodes=1 on a genuine ONA
        SERIES must survive — only anime_type == "movie" is a film."""
        await calendar_enrich._upsert_success(2, "show", {
            "genres": ["anime"], "network": "", "country": "", "certification": "",
            "runtime": None, "status": "", "overview": "", "ids": {},
            "anime_type": "ona",
        }, now=999)
        await self._stored([_simkl_record(2, title="The Ribbon Hero")])
        grouped, _meta = await calendar_cache.assemble_range(
            SHOWS, self.settings, tz=ZoneInfo("UTC"),
            start_date=date(2026, 7, 7), end_date=date(2026, 7, 7), now=1000)
        titles = [i.title for g in grouped for i in g["items"]]
        self.assertIn("The Ribbon Hero", titles)


class RoutedAnimeFilmThroughAssembleRangeTests(unittest.IsolatedAsyncioTestCase):
    """The other end of the fix the prune above left half-done: an anime film
    Simkl lists on its ANIME calendar is now filled into the MOVIES window
    (app/providers/simkl/calendar.py's `is_anime_film`), so the title the
    prune takes off Series Premieres has somewhere to land instead of
    disappearing.

    These read the movies endpoint the way a viewer does, over a stored
    window, and they never fetch — the routing itself is tested at the fill,
    in tests/providers/simkl/test_calendar.py.
    """

    async def asyncSetUp(self):
        migrated_db("enrich-anime-film-routing")
        self.settings = Settings()

    async def asyncTearDown(self):
        db.close_thread_connection()

    def _film_record(self, simkl_id=3157124, title="Shiranuhi"):
        """What to_anime_film_record produces: MOVIE media, no episode
        coordinate, unenriched, dated from the anime file's own instant."""
        return Record(
            source=Source.SIMKL, media=Media.MOVIE, id=str(simkl_id),
            ids={"simkl": simkl_id, "slug": "shiranuhi"},
            detail_url="https://simkl.com", title=title, air_ts=_AIR_TS,
            enriched=False,
        )

    async def _stored(self, records, *, endpoint=MOVIES, now=1000):
        await calendar_cache.store_window(
            endpoint.key, date(2026, 7, 6), records, 600, now,
            sources=["trakt", "simkl"], asked=["trakt", "simkl"])

    async def _read(self, endpoint, **kwargs):
        grouped, meta = await calendar_cache.assemble_range(
            endpoint, self.settings, tz=ZoneInfo("UTC"),
            start_date=date(2026, 7, 7), end_date=date(2026, 7, 7),
            now=1000, **kwargs)
        return [(g["date"], i.title) for g in grouped for i in g["items"]], meta

    async def _enrich_as_film(self, simkl_id=3157124, media="movie"):
        await calendar_enrich._upsert_success(simkl_id, media, {
            **_OK_FIELDS, "genres": ["anime"], "anime_type": "movie",
        }, now=999)

    async def test_an_enriched_anime_film_renders_on_the_movies_calendar_on_its_own_date(self):
        await self._enrich_as_film()
        await self._stored([self._film_record()])
        items, _meta = await self._read(MOVIES)
        self.assertEqual(items, [("2026-07-07", "Shiranuhi")])

    async def test_the_prune_never_touches_it_there(self):
        """`prune_disguised_films` drops a film from a SERIES endpoint only.
        A film on the movies endpoint is a film where it belongs, so the same
        enrichment that removes it from one calendar must leave it on this
        one — otherwise routing it would only move where it vanishes."""
        await self._enrich_as_film()
        await self._stored([self._film_record()])
        items, _meta = await self._read(MOVIES)
        self.assertIn(("2026-07-07", "Shiranuhi"), items)

    async def test_the_same_film_is_absent_from_every_series_endpoint(self):
        """Nothing filled it into a show window, and the prune removes it from
        a window filled before the split existed. Either way no series
        calendar shows it."""
        await self._enrich_as_film(media="show")
        for endpoint in (SHOWS, get_endpoint("shows/new"), get_endpoint("shows/premieres")):
            with self.subTest(endpoint=endpoint.key):
                await self._stored([_simkl_record(3157124, title="Shiranuhi")],
                                   endpoint=endpoint)
                items, _meta = await self._read(endpoint)
                self.assertNotIn("Shiranuhi", [t for _d, t in items])

    async def test_an_unenriched_film_still_renders_on_the_movies_calendar(self):
        """Enrichment decides how a card is DESCRIBED, never whether it is
        drawn. The routing already happened at the fill, off the calendar
        file's own field, so a film the drain has not reached yet is still on
        the right calendar — just without genres or an overview."""
        await self._stored([self._film_record()])
        items, meta = await self._read(MOVIES)
        self.assertEqual(items, [("2026-07-07", "Shiranuhi")])
        self.assertEqual(meta["unenriched"], 1)

    async def test_a_trakt_movie_is_unaffected(self):
        """Nothing here may reach a record from another source: `anime_type`
        is empty on every non-Simkl record and the routing happens inside the
        Simkl provider."""
        trakt_film = Record(
            source=Source.TRAKT, media=Media.MOVIE, id="a-trakt-film",
            ids={"trakt": 5, "tmdb": 999}, detail_url="https://trakt.tv",
            title="A Trakt Film", air_ts=_AIR_TS, date_only=True,
        )
        await self._stored([trakt_film, self._film_record()])
        await self._enrich_as_film()
        items, _meta = await self._read(MOVIES)
        self.assertIn(("2026-07-07", "A Trakt Film"), items)
        self.assertIn(("2026-07-07", "Shiranuhi"), items)

    async def test_the_share_path_renders_what_it_has_without_reaching_a_source(self):
        """allow_fetch=False is the public share page's promise. A routed film
        already in the stored window draws exactly as it does for a signed-in
        viewer, and no fill runs to put it there."""
        await self._enrich_as_film()
        await self._stored([self._film_record()])
        with patch.object(calendar_cache, "fetch_window_records",
                          new=AsyncMock(side_effect=AssertionError(
                              "the share path must never fill a window"))) as never:
            items, _meta = await self._read(MOVIES, allow_fetch=False)
        never.assert_not_awaited()
        self.assertEqual(items, [("2026-07-07", "Shiranuhi")])


class RunDrainLatchTests(EnrichTestCase):
    """The coalescing latch app/calendar/cache.py's load_window (a fill) and
    app/main.py's heartbeat both go through: at most one pass runs, at most
    one more is remembered to run after it — see the module note above
    `run_drain` in app/calendar/enrich.py for why a queue is the wrong shape
    given what `drain` itself costs to run redundantly."""
    SETTINGS = SimpleNamespace(simkl_client_id="cid", simkl_access_token="",
                               simkl_catalogue_configured=True)

    async def asyncSetUp(self):
        await super().asyncSetUp()
        # Module-level latch state outlives any one test's instance; start
        # every test from the same clean slate rather than trusting whatever
        # the previous test left behind.
        calendar_enrich._drain_active = False
        calendar_enrich._drain_rerun_requested = False
        calendar_enrich._drain_tasks.clear()

    async def asyncTearDown(self):
        for task in list(calendar_enrich._drain_tasks):
            task.cancel()
        calendar_enrich._drain_active = False
        calendar_enrich._drain_rerun_requested = False
        await super().asyncTearDown()

    async def test_a_single_call_runs_one_pass(self):
        spy = AsyncMock(return_value=3)
        with patch("app.calendar.enrich.drain", spy):
            fetched = await calendar_enrich.run_drain(self.SETTINGS)
        self.assertEqual(fetched, 3)
        spy.assert_awaited_once()
        self.assertFalse(calendar_enrich._drain_active)

    async def test_a_call_while_one_is_running_does_not_start_a_second_pass_but_causes_one_more(self):
        gate = asyncio.Event()
        calls: list[int] = []

        async def fake_drain(settings):
            calls.append(len(calls) + 1)
            if len(calls) == 1:
                await gate.wait()
            return 0

        with patch("app.calendar.enrich.drain", fake_drain):
            first = asyncio.create_task(calendar_enrich.run_drain(self.SETTINGS))
            await asyncio.sleep(0)  # let the first pass start and block on the gate
            second_result = await calendar_enrich.run_drain(self.SETTINGS)
            self.assertEqual(second_result, 0)  # coalesced — ran nothing of its own
            self.assertEqual(len(calls), 1)  # no concurrent second pass started
            self.assertTrue(calendar_enrich._drain_rerun_requested)
            gate.set()
            await first
        self.assertEqual(len(calls), 2)  # exactly one rerun happened
        self.assertFalse(calendar_enrich._drain_active)
        self.assertFalse(calendar_enrich._drain_rerun_requested)

    async def test_three_requests_during_one_run_still_cause_exactly_one_more(self):
        gate = asyncio.Event()
        calls: list[int] = []

        async def fake_drain(settings):
            calls.append(len(calls) + 1)
            if len(calls) == 1:
                await gate.wait()
            return 0

        with patch("app.calendar.enrich.drain", fake_drain):
            first = asyncio.create_task(calendar_enrich.run_drain(self.SETTINGS))
            await asyncio.sleep(0)
            for _ in range(3):
                result = await calendar_enrich.run_drain(self.SETTINGS)
                self.assertEqual(result, 0)
            self.assertEqual(len(calls), 1)  # still only the original pass
            gate.set()
            await first
        # Three requests arriving during one run still cause exactly one more
        # pass, not three — the flag is a bool, not a counter.
        self.assertEqual(len(calls), 2)

    async def test_a_raising_pass_still_releases_the_latch(self):
        with patch("app.calendar.enrich.drain", AsyncMock(side_effect=RuntimeError("boom"))):
            with self.assertRaises(RuntimeError):
                await calendar_enrich.run_drain(self.SETTINGS)
        self.assertFalse(calendar_enrich._drain_active)
        self.assertFalse(calendar_enrich._drain_rerun_requested)
        # A later caller is not wedged behind the failure — it starts fresh.
        spy = AsyncMock(return_value=1)
        with patch("app.calendar.enrich.drain", spy):
            fetched = await calendar_enrich.run_drain(self.SETTINGS)
        self.assertEqual(fetched, 1)
        spy.assert_awaited_once()

    async def test_schedule_drain_runs_the_drain_in_the_background(self):
        spy = AsyncMock(return_value=5)
        with patch("app.calendar.enrich.drain", spy):
            calendar_enrich.schedule_drain(self.SETTINGS)
            self.assertEqual(len(calendar_enrich._drain_tasks), 1)
            [task] = list(calendar_enrich._drain_tasks)
            result = await task
        self.assertEqual(result, 5)
        spy.assert_awaited_once()
        self.assertEqual(calendar_enrich._drain_tasks, set())  # discarded by its own done callback

    async def test_a_fill_triggered_pass_and_a_heartbeat_call_do_not_run_concurrently(self):
        """The same latch covers both callers: a fill's schedule_drain and
        the heartbeat's own run_drain call must not run two passes at once —
        the heartbeat call folds into the rerun flag instead."""
        gate = asyncio.Event()
        calls: list[int] = []

        async def fake_drain(settings):
            calls.append(len(calls) + 1)
            if len(calls) == 1:
                await gate.wait()
            return 0

        with patch("app.calendar.enrich.drain", fake_drain):
            calendar_enrich.schedule_drain(self.SETTINGS)  # the fill
            [background] = list(calendar_enrich._drain_tasks)
            await asyncio.sleep(0)  # let the background pass start and block on the gate
            heartbeat_result = await calendar_enrich.run_drain(self.SETTINGS)  # the heartbeat tick
            self.assertEqual(heartbeat_result, 0)  # coalesced, not a concurrent pass
            self.assertEqual(len(calls), 1)
            gate.set()
            await background
        self.assertEqual(len(calls), 2)

    async def test_a_scheduled_drain_that_raises_is_logged_and_releases_the_latch(self):
        with patch("app.calendar.enrich.drain", AsyncMock(side_effect=RuntimeError("boom"))):
            with self.assertLogs("app.calendar.enrich", level="ERROR") as captured:
                calendar_enrich.schedule_drain(self.SETTINGS)
                [task] = list(calendar_enrich._drain_tasks)
                with self.assertRaises(RuntimeError):
                    await task
                await asyncio.sleep(0)  # let the done callback finish logging
        self.assertTrue(any("drain failed" in line.lower() for line in captured.output))
        self.assertFalse(calendar_enrich._drain_active)
        # The latch is free for a later caller — nothing here left it stuck.
        spy = AsyncMock(return_value=2)
        with patch("app.calendar.enrich.drain", spy):
            fetched = await calendar_enrich.run_drain(self.SETTINGS)
        self.assertEqual(fetched, 2)

    async def test_schedule_drain_is_a_noop_with_no_running_event_loop(self):
        """Degrades to "not scheduled" rather than raising — a script, a
        synchronous test path, or shutdown all lack a running loop to
        schedule onto. Simulated here since this test itself runs inside
        one: get_running_loop is made to fail exactly as it would outside
        one."""
        with patch("asyncio.get_running_loop", side_effect=RuntimeError("no running loop")):
            calendar_enrich.schedule_drain(self.SETTINGS)  # must not raise
        self.assertEqual(calendar_enrich._drain_tasks, set())


if __name__ == "__main__":  # pragma: no cover
    unittest.main()


class BothWritersReadThePayloadTheSameWayTests(EnrichTestCase):
    """THE DRIFT GUARD, and it exists because the drift already happened.

    A lookup's answer is written in two places: onto a RECORD on the way into a
    fill (`_apply`), and onto a stored ROW when the drain answers for a title
    whose row already exists (`entries.apply_enrichment`). Each used to read the
    payload itself, and they disagreed — one looked for `ratings` where the
    payload says `rating`. Measured on a live instance: 1,505 titles kept their
    score and 225 lost it, decided by nothing but which path happened to reach
    them.

    Neither path's own tests could catch that: each was right about itself. What
    catches it is asking both to write the SAME payload and comparing the rows.
    """

    SETTINGS = SimpleNamespace(simkl_client_id="cid", simkl_access_token="",
                               simkl_catalogue_configured=True)

    PAYLOAD = {
        "extract_version": simkl_titles.EXTRACT_VERSION,
        "genres": ["drama", "game-show"], "network": "AMC", "country": "US",
        "language": "EN", "certification": "TV-MA", "runtime": 45,
        "status": "ended", "overview": "An overview.", "year": 2026,
        "rating": 8.4, "anime_type": "", "ids": {"tvdb": "999"},
        "release_types_by_country": {"US": [3]},
    }

    COLUMNS = ("network", "country", "language", "certification", "status",
               "overview", "runtime", "year", "genres_json", "ratings_json",
               "anime_type", "release_types_json", "enriched")

    async def _row(self):
        row = await db.fetch_one(
            f"SELECT {', '.join(self.COLUMNS)} FROM calendar_titles WHERE source = 'simkl'")
        return tuple(row[name] for name in self.COLUMNS)

    async def test_the_fill_path_and_the_row_path_store_the_same_thing(self):
        # PATH A — the answer is known before the fill, so `apply_stored_enrichment`
        # puts it on the record and the row is written carrying it.
        await calendar_enrich._upsert_success(1, "show", self.PAYLOAD, now=1000)
        await self._stored([_simkl_record(1)])
        through_the_fill = await self._row()

        # PATH B — the row exists first and the drain updates it in place.
        await db.execute("DELETE FROM calendar_titles")
        await db.execute("DELETE FROM simkl_titles")
        await self._stored([_simkl_record(1)])
        await calendar_enrich._upsert_success(1, "show", self.PAYLOAD, now=1000)
        through_the_row = await self._row()

        self.assertEqual(through_the_fill, through_the_row)

    async def test_the_rating_survives_both_of_them(self):
        """The field the drift actually cost. Named on its own because a tuple
        comparison failing says "these differ" and this says which."""
        await self._stored([_simkl_record(1)])
        await calendar_enrich._upsert_success(1, "show", self.PAYLOAD, now=1000)
        self.assertEqual(
            await db.fetch_value("SELECT ratings_json FROM calendar_titles"),
            '{"simkl": 8.4}')
        records, *_ = await calendar_entries.read_span(
            SHOWS.key, date(2026, 7, 6), date(2026, 7, 13))
        self.assertEqual(records[0].rating, 8.4)
