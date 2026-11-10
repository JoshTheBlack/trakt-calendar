"""A TITLE-ONLY record: what a service that describes every card says about a
title it does not list on this calendar (app/calendar/cache.py's
`with_title_only`), and each rule about what one may and may not do.

END TO END THROUGH THE REAL READ PATH. The fixture is the shape the calendar
really holds: TMDB lists a show's season premiere on Season Premieres — which
is what files its description in the title store — and only Simkl lists the
show's later episodes on All Episodes, stating no certification, language or
network for it. Every card here is read the way the calendar reads one.

No network.
"""
from __future__ import annotations

import json
import unittest
from datetime import date, datetime, timezone
from unittest import mock
from zoneinfo import ZoneInfo

from app import db
from app.calendar import cache as calendar_cache
from app.calendar import entries
from app.calendar import query
from app.calendar import resolve as calendar_resolve
from app.calendar import search as calendar_search
from app.calendar import vocab
from app.config import Settings
from app.endpoints import get_endpoint
from app.providers.base import Media, Record, ServiceLink, Source
from app.sources import prefs as source_prefs
from tests.support import migrated_db, put_title

SHOWS = get_endpoint("shows")
PREMIERES = get_endpoint("shows/premieres")
MOVIES = get_endpoint("movies")
NEW_YORK = ZoneInfo("America/New_York")
TMDB_ID = 4242
TMDB_PAGE = f"https://www.themoviedb.org/tv/{TMDB_ID}"


def _at(*parts) -> float:
    return datetime(*parts, tzinfo=timezone.utc).timestamp()


def simkl_episode(number: int = 5, **fields) -> Record:
    """Simkl's listing of one episode on All Episodes — E05 at 14 October 01:00
    UTC, 21:00 the evening before in New York — described, and silent on the
    facts TMDB states."""
    values = dict(
        source=Source.SIMKL, media=Media.SHOW, id="777", ids={"simkl": 777, "tmdb": str(TMDB_ID)},
        detail_url="https://simkl.test/tv/777", title="The Show",
        air_ts=_at(2026, 10, 9 + number, 1), season=2, episode_number=number,
        episode_label=f"S02E{number:02d}", overview="Simkl's words.",
        poster="https://simkl.test/p.jpg", genres=["drama"], enriched=True)
    values.update(fields)
    return Record(**values)


def tmdb_premiere(**fields) -> Record:
    """TMDB's Season Premieres listing of S02E01, as a fill built from the
    title's own answer stores it — keywords and show type stated."""
    values = dict(
        source=Source.TMDB, media=Media.SHOW, id=str(TMDB_ID), ids={"tmdb": TMDB_ID},
        detail_url=TMDB_PAGE, title="The Show", air_ts=_at(2026, 10, 7), date_only=True,
        season=2, episode_number=1, episode_label="S02E01", network="HBO", country="US",
        language="EN", certification="TV-MA", status="returning series", genres=["drama"],
        keywords=["anime", "based on manga"], show_type="scripted", overview="TMDB's words.",
        poster="https://image.tmdb.org/t/p/w500/x.jpg", enriched=True)
    values.update(fields)
    return Record(**values)


def _prefs(*order, calendar="auto") -> source_prefs.SourcePrefs:
    return source_prefs.SourcePrefs(user_id=1, metadata_order=list(order),
                                    calendar_source=calendar, movie_calendar_source=calendar)


DEFAULT = _prefs()                       # TMDB, Trakt, Simkl — declared
SIMKL_FIRST = _prefs("simkl", "trakt", "tmdb")


class TitleOnlyTestCase(unittest.IsolatedAsyncioTestCase):
    """Rows stored as a fill stores them, read back with no fetch allowed."""

    async def asyncSetUp(self):
        migrated_db(type(self).__name__.lower())
        self.settings = Settings()

    async def asyncTearDown(self):
        db.close_thread_connection()

    async def store(self, endpoint, records: list[Record]) -> None:
        by_window: dict[date, list[Record]] = {}
        for record in records:
            start = calendar_cache.window_start(calendar_cache.record_utc_date(record))
            by_window.setdefault(start, []).append(record)
        for start, held in by_window.items():
            names = sorted({str(r.source) for r in held})
            await calendar_cache.store_window(endpoint.key, start, held, 10**9, 1000,
                                              sources=names, asked=names)

    async def october(self, prefs=DEFAULT, endpoint=SHOWS, **specs):
        grouped, _meta = await calendar_cache.assemble_range(
            endpoint, self.settings, tz=NEW_YORK, start_date=date(2026, 10, 1),
            end_date=date(2026, 10, 31), prefs=prefs, allow_fetch=False, now=2000, **specs)
        return [item for day in grouped for item in day["items"]]

    async def card(self, prefs=DEFAULT, **specs):
        items = await self.october(prefs, **specs)
        return items[0] if len(items) == 1 else (None if not items else items)


class ACardTmdbDescribesTests(TitleOnlyTestCase):
    """What TMDB states reaches a card only Simkl lists."""

    async def asyncSetUp(self):
        await super().asyncSetUp()
        await self.store(PREMIERES, [tmdb_premiere()])
        await self.store(SHOWS, [simkl_episode()])

    async def test_tmdbs_certification_language_and_network_reach_it(self):
        item = await self.card()
        self.assertEqual((item.certification, item.language, item.network, item.country),
                         ("TV-MA", "EN", "HBO", "US"))
        self.assertEqual(item.field_sources["certification"], ["tmdb"])

    async def test_it_adds_tmdbs_button_as_a_service_that_does_not_list_it(self):
        item = await self.card()
        self.assertIn(ServiceLink("tmdb", TMDB_PAGE, False), item.links)
        self.assertIn(ServiceLink("simkl", "https://simkl.test/tv/777", True), item.links)

    async def test_an_include_certification_filter_judges_it_by_tmdbs(self):
        self.assertIsNotNone(await self.card(show_certifications="TV-MA"))
        self.assertIsNone(await self.card(show_certifications="TV-14"))

    async def test_the_keyword_and_show_type_filters_now_judge_it(self):
        self.assertIsNone(await self.card(keywords=["-anime"]))
        self.assertIsNone(await self.card(show_types="-scripted"))
        self.assertIsNotNone(await self.card(show_types="scripted"))

    async def test_only_one_service_still_lists_it(self):
        """TMDB describes the card and does not list it, so it is still the
        one-service card the filter hides."""
        self.assertIsNone(await self.card(single_service=True))


class NeverTheCardsPrimaryTests(TitleOnlyTestCase):
    """TMDB comes first in the declared order, and still gives a card it only
    describes no attribution, no day, no time and no episode."""

    async def asyncSetUp(self):
        await super().asyncSetUp()
        await self.store(PREMIERES, [tmdb_premiere(title="The Show (2024)")])
        await self.store(SHOWS, [simkl_episode()])

    async def test_a_tmdb_first_viewer_sees_simkls_day_time_and_episode(self):
        item = await self.card(DEFAULT)
        self.assertEqual((str(item.source), item.air_date, item.air_time, item.episode_label,
                          item.detail_url),
                         ("simkl", "2026-10-13", "21:00", "S02E05", "https://simkl.test/tv/777"))

    async def test_it_still_wins_a_field_in_their_order(self):
        """A third opinion on the facts: first in the order, its title wins the
        field, and the flip offers Simkl's."""
        item = await self.card(DEFAULT)
        self.assertEqual(item.title, "The Show (2024)")
        self.assertEqual(item.shown_source("title"), "tmdb")
        self.assertEqual(set(item.alternatives["title"]), {"simkl", "tmdb"})

    async def test_it_states_no_airing_so_the_card_says_no_other_day(self):
        self.assertEqual((await self.card(DEFAULT)).air_choices, [])


class ItNeverAdmitsACardTests(TitleOnlyTestCase):

    async def asyncSetUp(self):
        await super().asyncSetUp()
        await self.store(PREMIERES, [tmdb_premiere()])
        await self.store(SHOWS, [simkl_episode()])

    async def test_a_viewer_reading_only_tmdb_sees_only_what_tmdb_lists(self):
        self.assertEqual(await self.october(_prefs(calendar="tmdb")), [])

    async def test_a_viewer_who_left_tmdb_off_draws_none_of_its_words_but_is_judged_by_them(self):
        """Their card is Simkl's alone; their filters still judge the title as
        every service the instance admits describes it, TMDB's TV-MA included."""
        simkl_only = _prefs(calendar="simkl")
        item = await self.card(simkl_only)
        self.assertEqual((item.certification, item.network), ("", ""))
        self.assertNotIn("certification", item.field_sources)
        self.assertIsNone(await self.card(simkl_only, show_certifications="TV-14"))


class WhatItSaysAboutKeywordsAndTypeTests(TitleOnlyTestCase):
    """Keywords and a show's type are an answer only where a description stated
    them (rows.facts_of)."""

    async def test_a_row_no_description_stated_them_on_is_not_judged_by_them(self):
        await self.store(PREMIERES, [tmdb_premiere(keywords=None, show_type=None, enriched=False)])
        await self.store(SHOWS, [simkl_episode()])
        item = await self.card()
        self.assertIsNone(item.keywords)
        self.assertIsNone(item.show_type)
        self.assertIsNotNone(await self.card(keywords=["anime"], show_types="reality"))

    async def test_a_described_row_states_them(self):
        await self.store(PREMIERES, [tmdb_premiere()])
        await self.store(SHOWS, [simkl_episode()])
        item = await self.card()
        self.assertEqual((item.keywords, item.show_type), (["anime", "based on manga"], "scripted"))


class WhetherTheCardIsDescribedTests(TitleOnlyTestCase):
    """`enriched` is the group's: a described title-only record makes a card
    nobody else has described judgeable, and an undescribed one does not."""

    async def test_an_undescribed_row_leaves_an_undescribed_card_unjudged(self):
        await self.store(PREMIERES, [tmdb_premiere(enriched=False, keywords=None, show_type=None,
                                                   certification="", network="")])
        await self.store(SHOWS, [simkl_episode(enriched=False, genres=[])])
        item = await self.card()
        self.assertFalse(item.enriched)
        self.assertIsNotNone(await self.card(genres="comedy"))

    async def test_a_described_row_makes_it_judgeable(self):
        await self.store(PREMIERES, [tmdb_premiere()])
        await self.store(SHOWS, [simkl_episode(enriched=False, genres=[])])
        item = await self.card()
        self.assertTrue(item.enriched)
        self.assertEqual(item.genres, ["Drama"])
        self.assertIsNone(await self.card(genres="comedy"))


class OneReadAndNothingWrittenTests(TitleOnlyTestCase):

    async def asyncSetUp(self):
        await super().asyncSetUp()
        await self.store(PREMIERES, [tmdb_premiere()])
        await self.store(SHOWS, [simkl_episode(5), simkl_episode(6), simkl_episode(7)])

    async def test_a_span_is_one_read_of_the_store(self):
        """Every card of the span is judged by ONE statement, and the drawn cards'
        words — the describing group's among them — are ONE more read."""
        with mock.patch.object(query, "_verdicts", wraps=query._verdicts) as judged, \
                mock.patch.object(query, "_words_sql", wraps=query._words_sql) as words:
            items = await self.october()
        self.assertEqual([item.episode_label for item in items], ["S02E05", "S02E06", "S02E07"])
        self.assertEqual((judged.call_count, words.call_count), (1, 1))
        self.assertTrue(all(item.certification == "TV-MA" for item in items))

    async def test_two_viewers_read_the_same_rows_and_nothing_is_written(self):
        async def snapshot():
            return [tuple(row) for table in ("titles", "episodes",
                                             "calendar_coverage", "metadata_refresh")
                    for row in await db.fetch_all(f"SELECT * FROM {table} ORDER BY 1, 2, 3")]
        before = await snapshot()
        tmdb_first, simkl_first = await self.october(DEFAULT), await self.october(SIMKL_FIRST)
        self.assertEqual(await snapshot(), before)
        self.assertEqual({item.overview for item in tmdb_first}, {"TMDB's words."})
        self.assertEqual({item.overview for item in simkl_first}, {"Simkl's words."})


class AFilmTests(TitleOnlyTestCase):
    """The same record joins a film card, and the release narrowing still
    judges only the records that list it."""

    async def asyncSetUp(self):
        await super().asyncSetUp()
        film = dict(media=Media.MOVIE, season=None, episode_number=None, episode_label=None)
        await self.store(MOVIES, [
            simkl_episode(**film, ids={"simkl": 5, "tmdb": "99"}, id="5",
                          air_ts=_at(2026, 10, 16), date_only=True),
        ])
        # TMDB DESCRIBES THE FILM AND DOES NOT LIST IT: a listing of it, at any
        # date, would be this card's (one film, one card), so its words reach the
        # card as a description alone.
        await db.run(lambda conn: put_title(
            conn, "tmdb", "movie", 99, "movie:tmdb:99", title="The Show", certification="R",
            enriched=1, release_types_json=json.dumps({"BR": [3]}),
            detail_url="https://www.themoviedb.org/movie/99"))

    async def test_a_simkl_only_film_shows_tmdbs_certification(self):
        item = await self.card(endpoint=MOVIES)
        self.assertEqual((str(item.source), item.certification), ("simkl", "R"))

    async def test_tmdbs_release_schedule_is_not_this_cards(self):
        """A US-only release filter keeps the film: the one listing has no
        schedule to judge, and TMDB's (Brazil only) belongs to no listing."""
        self.assertIsNotNone(await self.card(endpoint=MOVIES, movie_release_countries="us"))

    async def test_the_record_carries_no_airing_episode_or_release(self):
        row = await db.run(lambda conn: conn.execute(
            query._words_sql(), {"rows": json.dumps([["movie:tmdb:99", "film"]])}).fetchone())
        record = query._title_only_record(row, "tmdb", "movie")
        self.assertEqual((record.air_ts, record.season, record.episode_number,
                          record.release_types_by_country, record.anime_type),
                         (0.0, None, None, {}, ""))
        self.assertEqual(record.certification, "R")


class WhoDescribesEveryCardTests(unittest.TestCase):

    def test_the_services_that_declare_it(self):
        self.assertEqual(calendar_resolve.describing_sources(Settings()), ("tmdb",))

    def test_a_service_the_instance_keeps_off_its_calendar_describes_none(self):
        with mock.patch.object(calendar_resolve, "instance_sources",
                               return_value=frozenset({"trakt", "simkl"})):
            self.assertEqual(calendar_resolve.describing_sources(Settings()), ())


class TheSearchPickerJudgesTheSameWayTests(TitleOnlyTestCase):
    """A season the calendar does not hold yet is judged as the card the jump
    would write — with TMDB's description of the title beside it."""

    def described(self) -> Record:
        return Record(source=Source.TRAKT, media=Media.SHOW, id="the-show",
                      ids={"trakt": 1, "tmdb": TMDB_ID}, detail_url="https://trakt.test/the-show",
                      title="The Show", air_ts=_at(2026, 10, 7), season=2, episode_number=1)

    async def test_tmdbs_certification_decides(self):
        specs = vocab.complete_specs({"show_certifications": "TV-MA"})
        self.assertFalse(await calendar_search._kept_by_filters(
            self.described(), Media.SHOW, specs, self.settings))
        await self.store(PREMIERES, [tmdb_premiere(air_ts=_at(2025, 3, 1))])
        self.assertTrue(await calendar_search._kept_by_filters(
            self.described(), Media.SHOW, specs, self.settings))
