"""app/calendar/filter.py's release rule: narrowing a films calendar to the
markets and formats one viewer cares about.

WHY IT EXISTS, MEASURED RATHER THAN ASSUMED. One service's movie calendar is a
GLOBAL release calendar — every release in every market. On the author's live
instance, August 2026 held 1322 film groups, of which 1314 came from that
service and 25 from the other, and the calendar payload carries nothing to
narrow them with: no genre, no country, no release type. The per-title
catalogue does, as a per-country schedule with TMDB's numeric type, and this is
the rule that spends it.

THE TWO CLAIMS THIS FILE IS HERE TO KEEP TRUE:
  - A COUNTRY AND A TYPE ARE JUDGED ON THE SAME RELEASE BLOCK. "US, theatrical"
    is a film in American cinemas, not a film that premiered in Brazil and
    opened in cinemas in Japan. Asking the two questions separately passes
    almost every example and is wrong on exactly the ones a viewer would
    notice.
  - A RECORD THAT CANNOT ANSWER IS KEPT. A source with no release schedule at
    all, a film enrichment has not reached yet, and a row written before the
    schedule was extracted must all survive, or a filter deletes titles it
    knows nothing about.
  - BUT A GROUP IS JUDGED ON THE RECORDS THAT CAN. Only a Simkl record ever
    carries a release map, so while a silent record kept its whole group, the
    filter could not drop any film the other service also listed — 19 of 29
    survivors on one real August. A release schedule is a fact about the TITLE,
    so where one record holds it the other's silence is an absence rather than
    a second opinion. A group NO record can judge is still kept whole.
"""
from __future__ import annotations

import unittest
from datetime import date
from zoneinfo import ZoneInfo

from app import db
from app.calendar import cache as calendar_cache, enrich as calendar_enrich
from app.calendar import filter as calendar_filter
from app.config import Settings
from app.endpoints import get_endpoint
from app.providers.base import Media, Record, Source
from tests.support import new_db_path

MOVIES = get_endpoint("movies")
SHOWS = get_endpoint("shows")

# 2026-07-07T12:00:00Z — inside the aligned window starting 2026-07-06 that the
# end-to-end tests store under, and inside their [7/7, 7/7] read span.
_AIR_TS = 1783425600.0

# TMDB's numbering, which Simkl reproduces; spelled out here so a test reads as
# the sentence it is checking rather than as arithmetic.
PREMIERE, LIMITED, THEATRICAL, DIGITAL, PHYSICAL, TV = 1, 2, 3, 4, 5, 6


def _film(simkl_id: int, *, title="A Film", releases=None) -> Record:
    """One Simkl film as the calendar normalizer produces it, optionally with
    the release map enrichment would have overlaid onto it."""
    return Record(
        source=Source.SIMKL, media=Media.MOVIE, id=str(simkl_id),
        ids={"simkl": simkl_id}, detail_url="https://simkl.com",
        title=title, air_ts=_AIR_TS, date_only=True,
        release_types_by_country=dict(releases or {}),
        enriched=releases is not None,
    )


class KeepReleaseBlocksTests(unittest.TestCase):
    """The predicate alone, over values — no database, no records, no read."""

    def test_a_country_and_a_type_must_meet_on_one_block(self):
        """THE RULE. A film that premiered in Brazil and opened in Japanese
        cinemas has a BR block and a theatrical block, and a viewer asking for
        Brazilian theatrical releases did not ask for it. Asking the two
        dimensions independently would keep it."""
        film = {"BR": [PREMIERE], "JP": [THEATRICAL]}
        self.assertFalse(calendar_filter.keep_release_blocks(
            film, {"br"}, set(), {THEATRICAL}, set()))
        self.assertTrue(calendar_filter.keep_release_blocks(
            film, {"jp"}, set(), {THEATRICAL}, set()))

    def test_one_matching_block_is_enough(self):
        film = {"BR": [PREMIERE], "US": [THEATRICAL, DIGITAL]}
        self.assertTrue(calendar_filter.keep_release_blocks(
            film, {"us"}, set(), {DIGITAL}, set()))

    def test_a_country_alone_narrows_without_a_type(self):
        self.assertTrue(calendar_filter.keep_release_blocks(
            {"US": [PHYSICAL]}, {"us"}, set(), set(), set()))
        self.assertFalse(calendar_filter.keep_release_blocks(
            {"BR": [PHYSICAL]}, {"us"}, set(), set(), set()))

    def test_a_type_alone_narrows_without_a_country(self):
        self.assertTrue(calendar_filter.keep_release_blocks(
            {"BR": [THEATRICAL]}, set(), set(), {THEATRICAL}, set()))
        self.assertFalse(calendar_filter.keep_release_blocks(
            {"BR": [TV]}, set(), set(), {THEATRICAL}, set()))

    def test_an_exclude_disqualifies_the_block_not_the_film(self):
        """The difference from every scalar dimension in this module: a film has
        several releases, so "-br" means a Brazilian release is not one that
        counts — a film out in Brazil AND America still survives on its American
        block, and only a Brazil-only film goes."""
        both = {"BR": [THEATRICAL], "US": [THEATRICAL]}
        self.assertTrue(calendar_filter.keep_release_blocks(
            both, set(), {"br"}, set(), set()))
        self.assertFalse(calendar_filter.keep_release_blocks(
            {"BR": [THEATRICAL]}, set(), {"br"}, set(), set()))

    def test_a_type_exclude_works_the_same_way(self):
        self.assertFalse(calendar_filter.keep_release_blocks(
            {"US": [PREMIERE]}, set(), set(), set(), {PREMIERE}))
        self.assertTrue(calendar_filter.keep_release_blocks(
            {"US": [PREMIERE, THEATRICAL]}, set(), set(), set(), {PREMIERE}))

    def test_an_empty_map_never_matches_here(self):
        """This function is asked only about a film that HAS something to say —
        whether "nothing to say" is kept is keep_release's decision, one level
        up, and deliberately not buried in the predicate."""
        self.assertFalse(calendar_filter.keep_release_blocks(
            {}, {"us"}, set(), set(), set()))
        self.assertFalse(calendar_filter.keep_release_blocks(
            None, {"us"}, set(), set(), set()))


class KeepReleaseTests(unittest.TestCase):
    def test_a_record_with_no_release_map_is_always_kept(self):
        """Three real situations produce one and none of them is "released
        nowhere": a source whose calendar payload has no release schedule (the
        other service's does not), a film enrichment has not reached, and a row
        written before the schedule was extracted."""
        trakt = Record(source=Source.TRAKT, media=Media.MOVIE, id="x", ids={},
                       detail_url="", title="A Film", air_ts=_AIR_TS)
        self.assertTrue(calendar_filter.keep_release(
            trakt, {"us"}, set(), {THEATRICAL}, set()))
        self.assertTrue(calendar_filter.keep_release(
            _film(1), {"us"}, set(), {THEATRICAL}, set()))

    def test_a_record_that_can_answer_is_judged(self):
        self.assertFalse(calendar_filter.keep_release(
            _film(1, releases={"BR": [PREMIERE]}), {"us"}, set(), set(), set()))


class ParseReleaseTypeSpecTests(unittest.TestCase):
    def test_it_reads_numbers_and_the_same_exclude_convention(self):
        self.assertEqual(calendar_filter.parse_release_type_spec("3, 4, -1"),
                         ({THEATRICAL, DIGITAL}, {PREMIERE}))

    def test_a_token_that_is_not_a_number_is_dropped_rather_than_raising(self):
        """A stored preference a later version wrote must not stop a page
        rendering — the read rule everywhere in this package. The write path is
        where a bad value is refused."""
        self.assertEqual(calendar_filter.parse_release_type_spec("3, theatrical, -"),
                         ({THEATRICAL}, set()))

    def test_an_empty_spec_is_no_filter(self):
        self.assertEqual(calendar_filter.parse_release_type_spec(""), (set(), set()))


class FilterReleaseGroupsTests(unittest.TestCase):
    """The group-level half: which TITLES a viewer sees, asked once per group."""

    def _pairs(self, *record_lists):
        return [({"key": f"movie:tmdb:{i}"}, list(rs))
                for i, rs in enumerate(record_lists)]

    def test_a_show_endpoint_is_untouched_whatever_the_spec_says(self):
        """A release format is not a fact about an episode, and the show
        calendars must not quietly lose titles to a films control."""
        pairs = self._pairs([_film(1, releases={"BR": [PREMIERE]})])
        self.assertEqual(
            calendar_filter.filter_release_groups(pairs, SHOWS.media, "us", "3"),
            pairs)

    def test_an_empty_spec_is_a_pass_through(self):
        pairs = self._pairs([_film(1, releases={"BR": [PREMIERE]})])
        self.assertEqual(
            calendar_filter.filter_release_groups(pairs, MOVIES.media, "", ""),
            pairs)

    def test_a_film_with_no_matching_release_is_removed(self):
        pairs = self._pairs([_film(1, releases={"BR": [PREMIERE]})],
                            [_film(2, releases={"US": [THEATRICAL]})])
        kept = calendar_filter.filter_release_groups(pairs, MOVIES.media, "us", "")
        self.assertEqual([r.id for _, rs in kept for r in rs], ["2"])

    def test_a_group_no_record_can_judge_is_kept(self):
        """The promise that survives: a film nothing carries a release map for is
        never dropped. Trakt's calendar publishes no release schedule at all, so
        a film only Trakt lists is genuinely unjudgeable and stays."""
        trakt = Record(source=Source.TRAKT, media=Media.MOVIE, id="t", ids={},
                       detail_url="", title="A Film", air_ts=_AIR_TS)
        pairs = self._pairs([trakt])
        kept = calendar_filter.filter_release_groups(pairs, MOVIES.media, "us", "")
        self.assertEqual(len(kept), 1)

    def test_a_record_that_cannot_answer_no_longer_keeps_the_group(self):
        """THE CORRECTION, AND IT WAS FOUND IN A BROWSER. Only a Simkl record
        ever carries a release map, so on a film both services listed the Trakt
        record is always silent — and while silence counted as survival, the
        filter could not drop any film Trakt also listed. Measured on one real
        August: 19 of 29 films surviving a filter for an empty market were merged
        films whose Simkl record named their countries and matched none of them.

        A film's release schedule is a fact about the TITLE. Where one record
        holds it, the other's silence is an absence rather than a second
        opinion."""
        trakt = Record(source=Source.TRAKT, media=Media.MOVIE, id="t", ids={},
                       detail_url="", title="A Film", air_ts=_AIR_TS)
        pairs = self._pairs([trakt, _film(1, releases={"BR": [PREMIERE]})])
        kept = calendar_filter.filter_release_groups(pairs, MOVIES.media, "us", "")
        self.assertEqual(kept, [])

    def test_a_group_that_survives_keeps_both_its_records(self):
        """The half of the old rule that stands. The question is which TITLES a
        viewer sees, so it is asked once per group — dropping the silent record
        and keeping the informed one would quietly rewrite what a card says about
        its own provenance to enforce a rule about release formats."""
        trakt = Record(source=Source.TRAKT, media=Media.MOVIE, id="t", ids={},
                       detail_url="", title="A Film", air_ts=_AIR_TS)
        pairs = self._pairs([trakt, _film(1, releases={"US": [THEATRICAL]})])
        kept = calendar_filter.filter_release_groups(pairs, MOVIES.media, "us", "")
        self.assertEqual(len(kept), 1)
        self.assertEqual(len(kept[0][1]), 2)


class ReleaseFilterThroughAssembleRangeTests(unittest.IsolatedAsyncioTestCase):
    """End to end, over a real stored window: the shape a viewer actually
    produces by ticking a box on the Filters panel."""

    async def asyncSetUp(self):
        new_db_path("release-filter")
        await db.migrate()
        self.settings = Settings()

    async def asyncTearDown(self):
        db.close_thread_connection()

    async def _stored(self, records, *, now=1000):
        await calendar_cache.store_window(
            MOVIES.key, date(2026, 7, 6), records, 600, now,
            sources=["trakt", "simkl"], asked=["trakt", "simkl"])

    async def _read(self, **kwargs):
        return await calendar_cache.assemble_range(
            MOVIES, self.settings, tz=ZoneInfo("UTC"),
            start_date=date(2026, 7, 7), end_date=date(2026, 7, 7),
            now=1000, **kwargs)

    async def _enrich(self, simkl_id: int, releases: dict) -> None:
        await calendar_enrich._upsert_success(
            simkl_id, "movie", {"release_types_by_country": releases}, now=999)

    async def test_the_narrowing_applies_and_says_how_much_it_removed(self):
        """An empty month that is empty BECAUSE OF A FILTER has to say so — a
        release country rule removes films rather than re-dating them, so it can
        empty a calendar outright, and a blank page with nothing to explain it
        reads as the app being broken."""
        await self._enrich(1, {"BR": [PREMIERE]})
        await self._enrich(2, {"US": [THEATRICAL]})
        await self._stored([_film(1, title="Brazil Only"), _film(2, title="US Only")])

        grouped, meta = await self._read(movie_release_countries="us")
        self.assertEqual([i.title for g in grouped for i in g["items"]], ["US Only"])
        self.assertEqual(meta["release_filtered"], 1)

        grouped, meta = await self._read(movie_release_countries="jp")
        self.assertEqual(grouped, [])
        self.assertEqual(meta["release_filtered"], 2)

    async def test_nothing_is_removed_and_nothing_is_reported_with_no_spec(self):
        await self._enrich(1, {"BR": [PREMIERE]})
        await self._stored([_film(1, title="Brazil Only")])
        grouped, meta = await self._read()
        self.assertEqual([i.title for g in grouped for i in g["items"]], ["Brazil Only"])
        self.assertEqual(meta["release_filtered"], 0)

    async def test_a_film_enrichment_has_not_reached_yet_still_renders(self):
        """The same deliberate, temporary gap the film prune and the genre
        exemption already accept: the release schedule arrives only once the
        background drain has looked the title up, which is strictly after the
        window was stored."""
        await self._stored([_film(1, title="Not Looked Up Yet")])
        grouped, meta = await self._read(movie_release_countries="us")
        self.assertEqual([i.title for g in grouped for i in g["items"]],
                         ["Not Looked Up Yet"])
        self.assertEqual(meta["release_filtered"], 0)

    async def test_the_country_and_type_meet_on_one_block_end_to_end(self):
        await self._enrich(1, {"BR": [PREMIERE], "JP": [THEATRICAL]})
        await self._enrich(2, {"US": [THEATRICAL]})
        await self._stored([_film(1, title="Split"), _film(2, title="American")])
        grouped, _ = await self._read(movie_release_countries="br",
                                      movie_release_types="3")
        self.assertEqual([i.title for g in grouped for i in g["items"]], [])

    async def test_it_changes_nothing_on_a_show_calendar(self):
        """The specs travel with every read, including the show endpoints'; the
        rule has to be inert there rather than merely unused."""
        show = Record(source=Source.SIMKL, media=Media.SHOW, id="9",
                      ids={"simkl": 9}, detail_url="", title="A Series",
                      air_ts=_AIR_TS, season=1, episode_number=1)
        await calendar_cache.store_window(
            SHOWS.key, date(2026, 7, 6), [show], 600, 1000,
            sources=["trakt", "simkl"], asked=["trakt", "simkl"])
        grouped, meta = await calendar_cache.assemble_range(
            SHOWS, self.settings, tz=ZoneInfo("UTC"),
            start_date=date(2026, 7, 7), end_date=date(2026, 7, 7),
            movie_release_countries="us", movie_release_types="3", now=1000)
        self.assertEqual([i.title for g in grouped for i in g["items"]], ["A Series"])
        self.assertEqual(meta["release_filtered"], 0)

    async def test_the_release_map_never_reaches_a_stored_window(self):
        """Only enrichment sets it, and enrichment is a READ-time overlay — a
        window carrying one moment's release schedule would be a stored row
        whose contents depend on how far the drain had got when it was filled."""
        await self._enrich(1, {"US": [THEATRICAL]})
        await self._stored([_film(1, title="American")])
        groups = await calendar_cache.cached_calendar_groups()
        stored = [g for g in groups if (g.get("by_source") or {}).get("simkl")]
        self.assertTrue(stored)
        for group in stored:
            self.assertNotIn("release_types_by_country", group["by_source"]["simkl"])
