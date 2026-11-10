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
from app.calendar import cache as calendar_cache
from app.calendar import entries as calendar_entries, enrich as calendar_enrich
from app.calendar import filter as calendar_filter
from app.config import Settings
from app.endpoints import get_endpoint
from app.providers.base import Media, Record, Source
from tests import support
from tests.support import migrated_db

MOVIES = get_endpoint("movies")
SHOWS = get_endpoint("shows")

# 2026-07-07T12:00:00Z — inside the aligned window starting 2026-07-06 that the
# end-to-end tests store under, and inside their [7/7, 7/7] read span.
_AIR_TS = 1783425600.0

# TMDB's numbering, which Simkl reproduces; spelled out here so a test reads as
# the sentence it is checking rather than as arithmetic.
PREMIERE, LIMITED, THEATRICAL, DIGITAL, PHYSICAL, TV = 1, 2, 3, 4, 5, 6


def _film(simkl_id: int, *, title="A Film", releases=None, tmdb=None) -> Record:
    """One Simkl film as the calendar normalizer produces it, optionally with
    the release map enrichment would have overlaid onto it."""
    return Record(
        source=Source.SIMKL, media=Media.MOVIE, id=str(simkl_id),
        ids={"simkl": simkl_id, "tmdb": tmdb or 9000 + simkl_id}, detail_url="https://simkl.com",
        title=title, air_ts=_AIR_TS, date_only=True,
        release_types_by_country=dict(releases or {}),
        enriched=releases is not None,
    )


def _kept(records, countries="", types="", endpoint="movies") -> list[str]:
    """The ids of the cards `records` draw for a viewer narrowed to `countries`
    and `types` — judged by the calendar's own statement over a store holding
    them (support.read_kept)."""
    kept, _filtered = support.read_kept(
        records, endpoint=endpoint,
        specs={"movie_release_countries": countries, "movie_release_types": types})
    return [record.id for record in kept]


class ReleaseBlocksTests(support.CardTestCase):
    """The rule over one film's schedule — what a block is, and what a filter
    asks of it."""

    def _one(self, releases, countries="", types=""):
        return bool(_kept([_film(1, releases=releases)], countries, types))

    def test_a_country_and_a_type_must_meet_on_one_block(self):
        """THE RULE. A film that premiered in Brazil and opened in Japanese
        cinemas has a BR block and a theatrical block, and a viewer asking for
        Brazilian theatrical releases did not ask for it. Asking the two
        dimensions independently would keep it."""
        film = {"BR": [PREMIERE], "JP": [THEATRICAL]}
        self.assertFalse(self._one(film, "br", "3"))
        self.assertTrue(self._one(film, "jp", "3"))

    def test_one_matching_block_is_enough(self):
        self.assertTrue(self._one({"BR": [PREMIERE], "US": [THEATRICAL, DIGITAL]}, "us", "4"))

    def test_a_country_alone_narrows_without_a_type(self):
        self.assertTrue(self._one({"US": [PHYSICAL]}, "us"))
        self.assertFalse(self._one({"BR": [PHYSICAL]}, "us"))

    def test_a_type_alone_narrows_without_a_country(self):
        self.assertTrue(self._one({"BR": [THEATRICAL]}, types="3"))
        self.assertFalse(self._one({"BR": [TV]}, types="3"))

    def test_an_exclude_disqualifies_the_block_not_the_film(self):
        """The difference from every scalar dimension: a film has several
        releases, so "-br" means a Brazilian release is not one that counts — a
        film out in Brazil AND America still survives on its American block, and
        only a Brazil-only film goes."""
        self.assertTrue(self._one({"BR": [THEATRICAL], "US": [THEATRICAL]}, "-br"))
        self.assertFalse(self._one({"BR": [THEATRICAL]}, "-br"))

    def test_a_type_exclude_works_the_same_way(self):
        self.assertFalse(self._one({"US": [PREMIERE]}, types="-1"))
        self.assertTrue(self._one({"US": [PREMIERE, THEATRICAL]}, types="-1"))

    def test_an_empty_schedule_is_no_answer(self):
        """A film whose schedule says nothing cannot be judged by it, so it is
        kept — the same answer as a film nobody has described — while one whose
        schedule names only other markets goes."""
        self.assertTrue(self._one({}, "us"))
        self.assertFalse(self._one({"BR": [PREMIERE]}, "us"))


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


class ReleaseGroupsTests(support.CardTestCase):
    """The card-level half: which TITLES a viewer sees, asked once per card."""

    @staticmethod
    def _trakt(tmdb=None):
        return Record(source=Source.TRAKT, media=Media.MOVIE, id="77",
                      ids={"trakt": 77, **({"tmdb": tmdb} if tmdb else {})},
                      detail_url="", title="A Film", air_ts=_AIR_TS)

    def test_a_show_endpoint_is_untouched_whatever_the_spec_says(self):
        """A release format is not a fact about an episode, and the show
        calendars must not quietly lose titles to a films control."""
        show = Record(source=Source.SIMKL, media=Media.SHOW, id="1", ids={"simkl": 1, "tmdb": 1},
                      detail_url="", title="A Show", air_ts=_AIR_TS, season=1, episode_number=1,
                      release_types_by_country={"BR": [PREMIERE]}, enriched=True)
        self.assertEqual(_kept([show], "us", "3", endpoint="shows"), ["1"])

    def test_an_empty_spec_is_a_pass_through(self):
        self.assertEqual(_kept([_film(1, releases={"BR": [PREMIERE]})]), ["1"])

    def test_a_film_with_no_matching_release_is_removed(self):
        films = [_film(1, releases={"BR": [PREMIERE]}), _film(2, releases={"US": [THEATRICAL]})]
        self.assertEqual(_kept(films, "us"), ["2"])

    def test_a_group_no_record_can_judge_is_kept(self):
        """The promise that survives: a film nothing carries a release map for is
        never dropped. Trakt's calendar publishes no release schedule at all, so
        a film only Trakt lists, with no releases read, is unjudgeable and
        stays."""
        self.assertEqual(_kept([self._trakt()], "us"), ["77"])

    def test_a_record_that_cannot_answer_no_longer_keeps_the_group(self):
        """THE CORRECTION, AND IT WAS FOUND IN A BROWSER. On a film both services
        listed the Trakt record was always silent — and while silence counted as
        survival, the filter could not drop any film Trakt also listed. Measured
        on one real August: 19 of 29 films surviving a filter for an empty market
        were merged films whose Simkl record named their countries and matched
        none of them.

        A film's release schedule is a fact about the TITLE. Where one record
        holds it, the other's silence is an absence rather than a second
        opinion."""
        films = [self._trakt(tmdb=501), _film(1, tmdb=501, releases={"BR": [PREMIERE]})]
        self.assertEqual(_kept(films, "us"), [])

    def test_a_group_that_survives_keeps_both_its_records(self):
        """The half of the old rule that stands. The question is which TITLES a
        viewer sees, so it is asked once per card — dropping the silent record
        and keeping the informed one would quietly rewrite what a card says about
        its own provenance to enforce a rule about release formats."""
        films = [self._trakt(tmdb=501), _film(1, tmdb=501, releases={"US": [THEATRICAL]})]
        placed, _viewer = support.read_views(
            films, endpoint="movies", specs={"movie_release_countries": "us"})
        self.assertEqual([sorted(view.card.by_source) for view in placed.views],
                         [["simkl", "trakt"]])


class ReleaseFilterThroughAssembleRangeTests(unittest.IsolatedAsyncioTestCase):
    """End to end, over a real stored window: the shape a viewer actually
    produces by ticking a box on the Filters panel."""

    async def asyncSetUp(self):
        migrated_db("release-filter")
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
        self.assertEqual(meta["filtered"], 1)

        grouped, meta = await self._read(movie_release_countries="jp")
        self.assertEqual(grouped, [])
        self.assertEqual(meta["filtered"], 2)

    async def test_nothing_is_removed_and_nothing_is_reported_with_no_spec(self):
        await self._enrich(1, {"BR": [PREMIERE]})
        await self._stored([_film(1, title="Brazil Only")])
        grouped, meta = await self._read()
        self.assertEqual([i.title for g in grouped for i in g["items"]], ["Brazil Only"])
        self.assertEqual(meta["filtered"], 0)

    async def test_a_film_enrichment_has_not_reached_yet_still_renders(self):
        """The same deliberate, temporary gap the film prune and the genre
        exemption already accept: the release schedule arrives only once the
        background drain has looked the title up, which is strictly after the
        window was stored."""
        await self._stored([_film(1, title="Not Looked Up Yet")])
        grouped, meta = await self._read(movie_release_countries="us")
        self.assertEqual([i.title for g in grouped for i in g["items"]],
                         ["Not Looked Up Yet"])
        self.assertEqual(meta["filtered"], 0)

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
        self.assertEqual(meta["filtered"], 0)

    async def test_the_release_map_is_stored_with_the_entry(self):
        """THIS TEST USED TO ASSERT THE OPPOSITE, and the reversal is the design.

        It read: only enrichment sets this, enrichment is a READ-time overlay, so
        a window carrying one moment's release schedule would be a stored row
        whose contents depend on how far the drain had got when it was filled.
        That was a true objection to storing a value inside a BLOB, where a
        refill replaced everything wholesale and nothing could update one title.

        A row can be updated. The drain writes the release map onto the title it
        belongs to, a refill is forbidden to demote it, and the value is
        therefore never older than the last drain pass rather than frozen at
        whatever moment the window happened to be filled. Storing it is what lets
        the read stop rebuilding it per viewer."""
        await self._enrich(1, {"US": [THEATRICAL]})
        await self._stored([_film(1, title="American")])
        # READ BACK THE STORED ROWS, which is what the assertion is actually
        # about. This used to inflate the whole calendar to reach them, through a
        # helper that no longer has any caller in the app.
        stored, _asked, _answered, _at = await support.read_span(
            MOVIES.key, date(2026, 7, 6), date(2026, 7, 13))
        simkl = [r for r in stored if r.source is Source.SIMKL]
        self.assertTrue(simkl)
        for record in simkl:
            self.assertEqual(record.release_types_by_country, {"US": [THEATRICAL]})
