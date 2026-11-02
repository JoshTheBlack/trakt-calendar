"""Keywords and show type in the title store: who writes them, and how a reader
tells "described, none" from "nobody has said".

THE PROMISE (app/metadata/rows.py's `facts_values`): a row's keywords and show
type are an ANSWER only once a description — or a listing built from one —
stated them, and every reader of the row gets None for both otherwise. A filter
shows a title it cannot judge, so the difference decides whether an include
filter hides a title or waits for it.

No network.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

from app.calendar import entries
from app.metadata import descriptions
from app.providers.base import Record, Source
from tests.support import DatabaseTestCase, title_groups

UTC = ZoneInfo("UTC")
AIR_TS = datetime(2026, 10, 7, 12, tzinfo=timezone.utc).timestamp()
SPAN = (date(2026, 10, 5), date(2026, 10, 12))


def _answer(source: str, own_id: int, **fields) -> dict:
    """A whole-title answer in DetailPort's key set."""
    return {
        "title": "A Show", "year": 2026, "overview": "o", "season_overview": "",
        "status": "Returning Series", "network": "HBO", "country": "US", "language": "EN",
        "anime_type": "", "release_types_by_country": {}, "ids": {source: own_id},
        "poster": "", "runtime": 50, "genres": ["Drama"], "genre_slugs": ["drama"],
        "rating": None, "imdb_rating": None, "certification": "", "videos": [],
        "first_aired": "", "homepage": "", "season": None, "season_title": "",
        "season_count": 1, "cast": [], "episodes": [], "keywords": None, "show_type": None,
        **fields}


def _listing(source=Source.TMDB, own_id="5", **fields) -> Record:
    return Record(source=source, media="show", id=own_id, ids={str(source): int(own_id)},
                  detail_url="", title="A Show", air_ts=AIR_TS, season=1, episode_number=1,
                  episode_label="S01E01", **fields)


class ADescriptionTests(DatabaseTestCase):

    async def test_a_description_that_states_them_is_read_back_as_its_answer(self):
        await descriptions.store("tmdb", "show", 5, _answer(
            "tmdb", 5, keywords=["anime", "based on manga"], show_type="scripted"),
            asked_season=None, tz=UTC)
        got = await descriptions.stored("tmdb", "show", 5, None)
        self.assertEqual((got["keywords"], got["show_type"]),
                         (["anime", "based on manga"], "scripted"))

    async def test_none_stated_is_an_answer_too(self):
        await descriptions.store("tmdb", "show", 5, _answer("tmdb", 5, keywords=[],
                                                            show_type=""),
                                 asked_season=None, tz=UTC)
        got = await descriptions.stored("tmdb", "show", 5, None)
        self.assertEqual((got["keywords"], got["show_type"]), ([], ""))

    async def test_a_service_that_states_neither_leaves_them_unknown(self):
        await descriptions.store("trakt", "show", 7, _answer("trakt", 7),
                                 asked_season=None, tz=UTC)
        got = await descriptions.stored("trakt", "show", 7, None)
        self.assertEqual((got["keywords"], got["show_type"]), (None, None))
        self.assertEqual((await title_groups("trakt"))[0]["facts_known"], 0)


class AListingTests(DatabaseTestCase):

    async def store(self, *records):
        await entries.store_span("shows/new", *SPAN, list(records), sources=["tmdb"],
                                 asked=["tmdb"], now=1000, stale_after=2000)
        return await entries.read_span_whole("shows/new", *SPAN)

    async def test_a_sparse_listing_says_nothing_and_its_read_says_so(self):
        [record] = await self.store(_listing(enriched=False))
        self.assertIsNone(record.keywords)
        self.assertIsNone(record.show_type)

    async def test_a_listing_built_from_a_description_states_them(self):
        """TMDB's season premieres are built from the title's own answer."""
        [record] = await self.store(_listing(keywords=["office"], show_type="scripted"))
        self.assertEqual((record.keywords, record.show_type), (["office"], "scripted"))

    async def test_a_sparse_refill_never_erases_what_a_description_stated(self):
        await self.store(_listing(keywords=["office"], show_type="scripted"))
        [record] = await self.store(_listing(enriched=False))
        self.assertEqual((record.keywords, record.show_type), (["office"], "scripted"))
