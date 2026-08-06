"""app/calendar/enrich.py: the read-time overlay of `simkl_titles` onto Simkl
records, the in-memory drain queue it feeds, the heartbeat drain itself, and
the retention sweep.

THE CENTRAL CLAIM UNDER TEST: a Simkl record with no genres/country/
certification is exempt from a viewer's filter on those dimensions until the
drain has actually looked it up — not hidden, and not treated as though it had
already answered "none". Everything else here is the machinery that makes that
claim true without ever making a network call from the read path.
"""
from __future__ import annotations

import json
import unittest
import zlib
from datetime import date
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

from app import db
from app.calendar import cache as calendar_cache, enrich as calendar_enrich
from app.config import Settings
from app.endpoints import get_endpoint
from app.providers.base import Media, Record, Source
from tests.support import new_db_path

SHOWS = get_endpoint("shows")


# 2026-07-07T12:00:00Z — inside the aligned window starting 2026-07-06 that
# the end-to-end tests below store under, and inside their [7/7, 7/7] read
# span. Everything in this file that needs "an ordinary airing" uses this.
_AIR_TS = 1783425600.0


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
        new_db_path("enrich")
        await db.migrate()
        calendar_enrich._pending.clear()

    async def asyncTearDown(self):
        calendar_enrich._pending.clear()
        db.close_thread_connection()


class OverlayTests(EnrichTestCase):
    async def test_a_record_with_no_row_is_left_unenriched_and_queued(self):
        record = _simkl_record(1)
        [got] = await calendar_enrich.overlay_records([record])
        self.assertFalse(got.enriched)
        self.assertEqual(calendar_enrich.pending_count(), 1)

    async def test_a_row_that_answered_fills_the_record_and_marks_it_enriched(self):
        await calendar_enrich._upsert_success(1, "show", {
            "genres": ["drama", "game-show"], "network": "AMC", "country": "US",
            "certification": "TV-MA", "runtime": 45, "status": "ended",
            "overview": "An overview.", "ids": {"tvdb": "999"},
        }, now=1000)
        record = _simkl_record(1)
        [got] = await calendar_enrich.overlay_records([record])
        self.assertTrue(got.enriched)
        self.assertEqual(got.genres, ["drama", "game-show"])
        self.assertEqual(got.country, "US")
        self.assertEqual(got.certification, "TV-MA")
        self.assertEqual(got.network, "AMC")
        self.assertEqual(got.runtime, 45)
        self.assertEqual(got.ids["tvdb"], "999")
        self.assertEqual(calendar_enrich.pending_count(), 0)

    async def test_enrichment_upgrades_ids_without_overwriting_ones_already_there(self):
        """The calendar file's own tmdb must survive; enrichment only ADDS a
        namespace the fill never had (tvdb, mal, anidb)."""
        await calendar_enrich._upsert_success(1, "show", {
            "genres": [], "network": "", "country": "", "certification": "",
            "runtime": None, "status": "", "overview": "",
            "ids": {"tvdb": "999", "mal": "should-not-override"},
        }, now=1000)
        record = _simkl_record(1)
        record.ids["mal"] = "already-there"
        [got] = await calendar_enrich.overlay_records([record])
        self.assertEqual(got.ids["tvdb"], "999")
        self.assertEqual(got.ids["mal"], "already-there")

    async def test_a_twenty_airing_show_queues_exactly_once(self):
        """One title airing many times in a window is one row to fetch, not
        twenty — the whole reason simkl_titles is keyed on the title."""
        records = [_simkl_record(1) for _ in range(20)]
        await calendar_enrich.overlay_records(records)
        self.assertEqual(calendar_enrich.pending_count(), 1)

    async def test_a_trakt_record_is_never_queried_or_queued(self):
        trakt_record = Record(
            source=Source.TRAKT, media=Media.SHOW, id="x", ids={"trakt": 1},
            detail_url="https://trakt.tv", title="X", air_ts=1784145600.0,
        )
        await calendar_enrich.overlay_records([trakt_record])
        self.assertEqual(calendar_enrich.pending_count(), 0)
        self.assertTrue(trakt_record.enriched)

    async def test_overlay_makes_no_outbound_call(self):
        """The read path may never enrich — see app/calendar/enrich.py's module
        docstring. A patched fetch_title that is never awaited is the proof."""
        spy = AsyncMock(side_effect=AssertionError("must not fetch"))
        with patch("app.providers.simkl.titles.fetch_title", spy):
            await calendar_enrich.overlay_records([_simkl_record(1)])
        spy.assert_not_awaited()

    async def test_a_stored_failure_within_its_backoff_is_not_requeued(self):
        await calendar_enrich._upsert_failure(1, "show", now=1000)
        record = _simkl_record(1)
        await calendar_enrich.overlay_records([record], now=1000 + 10)
        self.assertEqual(calendar_enrich.pending_count(), 0)
        self.assertFalse(record.enriched)

    async def test_a_stored_failure_past_its_backoff_is_requeued(self):
        await calendar_enrich._upsert_failure(1, "show", now=1000)
        record = _simkl_record(1)
        await calendar_enrich.overlay_records([record], now=1000 + calendar_enrich._BACKOFF_BASE_SECONDS + 1)
        self.assertEqual(calendar_enrich.pending_count(), 1)

    async def test_a_repeated_failure_backs_off_longer(self):
        await calendar_enrich._upsert_failure(1, "show", now=1000)
        await calendar_enrich._upsert_failure(1, "show", now=1000)  # fail_count -> 2
        record = _simkl_record(1)
        # Past the first-failure backoff but not the second's.
        await calendar_enrich.overlay_records(
            [record], now=1000 + calendar_enrich._BACKOFF_BASE_SECONDS + 1)
        self.assertEqual(calendar_enrich.pending_count(), 0)

    async def test_a_failure_does_not_erase_a_previous_success(self):
        """A title that once answered and then fails a later attempt keeps its
        last good data — a transient failure is not "this title has nothing"."""
        await calendar_enrich._upsert_success(1, "show", {
            "genres": ["drama"], "network": "", "country": "US", "certification": "",
            "runtime": None, "status": "", "overview": "", "ids": {},
        }, now=1000)
        await calendar_enrich._upsert_failure(1, "show", now=2000)
        record = _simkl_record(1)
        [got] = await calendar_enrich.overlay_records([record], now=2000)
        self.assertTrue(got.enriched)
        self.assertEqual(got.genres, ["drama"])

    async def test_records_of_different_media_do_not_share_a_row(self):
        """Simkl's own id spaces are per media kind, so the same numeric id for
        a show and a movie must not read each other's enrichment."""
        await calendar_enrich._upsert_success(1, "show", {
            "genres": ["drama"], "network": "", "country": "", "certification": "",
            "runtime": None, "status": "", "overview": "", "ids": {},
        }, now=1000)
        movie = Record(
            source=Source.SIMKL, media=Media.MOVIE, id="1", ids={"simkl": 1},
            detail_url="https://simkl.com", title="Movie One", air_ts=1784145600.0,
            date_only=True, enriched=False,
        )
        [got] = await calendar_enrich.overlay_records([movie])
        self.assertFalse(got.enriched)
        self.assertEqual(calendar_enrich.pending_count(), 1)

    async def test_the_pending_queue_is_bounded(self):
        records = [_simkl_record(n) for n in range(calendar_enrich._PENDING_MAX + 50)]
        await calendar_enrich.overlay_records(records)
        self.assertEqual(calendar_enrich.pending_count(), calendar_enrich._PENDING_MAX)


class DrainTests(EnrichTestCase):
    SETTINGS = SimpleNamespace(simkl_client_id="cid", simkl_access_token="")

    async def test_an_empty_queue_fetches_nothing(self):
        self.assertEqual(await calendar_enrich.drain(self.SETTINGS), 0)

    async def test_a_bounded_batch_is_drained_and_stored(self):
        for n in range(calendar_enrich.DRAIN_BATCH_SIZE + 5):
            calendar_enrich._enqueue(n, "show")
        answers = {
            n: {"genres": [], "network": "", "country": "", "certification": "",
                "runtime": None, "status": "", "overview": "", "ids": {}}
            for n in range(calendar_enrich.DRAIN_BATCH_SIZE + 5)
        }

        async def fake_fetch(settings, simkl_id, media):
            return answers.get(simkl_id)

        with patch("app.providers.simkl.titles.fetch_title", side_effect=fake_fetch):
            fetched = await calendar_enrich.drain(self.SETTINGS)
        self.assertEqual(fetched, calendar_enrich.DRAIN_BATCH_SIZE)
        self.assertEqual(calendar_enrich.pending_count(), 5)
        rows = await db.fetch_all("SELECT COUNT(*) AS n FROM simkl_titles")
        self.assertEqual(rows[0]["n"], calendar_enrich.DRAIN_BATCH_SIZE)

    async def test_a_failed_lookup_still_writes_a_row_so_it_is_not_requeued_forever(self):
        calendar_enrich._enqueue(1, "show")
        with patch("app.providers.simkl.titles.fetch_title", AsyncMock(return_value=None)):
            fetched = await calendar_enrich.drain(self.SETTINGS)
        self.assertEqual(fetched, 0)
        row = await db.fetch_one("SELECT fail_count FROM simkl_titles WHERE simkl_id = 1 AND media = 'show'")
        self.assertEqual(row["fail_count"], 1)

    async def test_one_failing_id_does_not_stop_the_rest_of_the_batch(self):
        calendar_enrich._enqueue(1, "show")
        calendar_enrich._enqueue(2, "show")

        async def fake_fetch(settings, simkl_id, media):
            if simkl_id == 1:
                raise RuntimeError("boom")
            return {"genres": [], "network": "", "country": "", "certification": "",
                   "runtime": None, "status": "", "overview": "", "ids": {}}

        with patch("app.providers.simkl.titles.fetch_title", side_effect=fake_fetch):
            fetched = await calendar_enrich.drain(self.SETTINGS)
        self.assertEqual(fetched, 1)


class SweepTests(EnrichTestCase):
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

    async def test_a_swept_row_is_simply_re_queued_on_the_next_read(self):
        """Retention is not data loss — it is a forced recheck. Deleting the row
        must make the id look unattempted again, not permanently failed."""
        await calendar_enrich._upsert_success(1, "show", {
            "genres": ["drama"], "network": "", "country": "", "certification": "",
            "runtime": None, "status": "", "overview": "", "ids": {},
        }, now=1000)
        await calendar_enrich.sweep(now=1000 + calendar_enrich.RETENTION_SECONDS + 1)
        record = _simkl_record(1)
        [got] = await calendar_enrich.overlay_records([record])
        self.assertFalse(got.enriched)
        self.assertEqual(calendar_enrich.pending_count(), 1)


class FilterExemptionThroughAssembleRangeTests(unittest.IsolatedAsyncioTestCase):
    """The end-to-end case: a real viewer read, over a real stored window,
    with a real per-viewer genre filter — the shape the Moonshadow/You Maniac
    report actually took."""

    async def asyncSetUp(self):
        new_db_path("enrich-assemble")
        await db.migrate()
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
                countries="-kr", now=1000, prefs=prefs, linked=("trakt", "simkl"))
            titles = [i.title for g in grouped for i in g["items"]]
            self.assertNotIn("Moonshadow", titles, f"selection={selection}")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
