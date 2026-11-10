"""app/providers/tmdb/calendar.py and discover.py — TMDB's Series and Movie
Premieres, read from its discover listings.

`transport.cached_get` is replaced by a small discover service (`FakeTmdb`) that
pages a list of entries the way TMDB does, twenty a page, so what is held is how
a listing is asked for and read: every page, each title once, the page-500 cap
answered by halving the span, adult titles never asked for, the region and the
release types on the film query, and each entry built into a DATE-ONLY, SPARSE
record by the one record builder.
"""
from __future__ import annotations

import math
import unittest
from datetime import date
from unittest import mock

from app.config import Settings
from app.endpoints import get_endpoint
from app.providers.base import Media, Source
from app.providers.tmdb import calendar as tmdb_calendar
from app.providers.tmdb import discover, transport

SETTINGS = Settings(tmdb_api_key="eyJa.eyJb.c", tmdb_region="GB")
WEEK = date(2026, 10, 26)


def show(tmdb_id: int, first_aired: str, **extra) -> dict:
    """A discover/tv entry as TMDB lists it — no network, status or ids beyond
    its own."""
    return {"id": tmdb_id, "name": f"Show {tmdb_id}", "first_air_date": first_aired,
            "origin_country": ["US"], "original_language": "en", "genre_ids": [18, 10765],
            "overview": "A show.", "poster_path": f"/p{tmdb_id}.jpg", "vote_average": 7.5,
            "vote_count": 40, **extra}


def film(tmdb_id: int, released: str, **extra) -> dict:
    return {"id": tmdb_id, "title": f"Film {tmdb_id}", "release_date": released,
            "original_language": "en", "genre_ids": [27], "overview": "A film.",
            "poster_path": None, "vote_average": 0, "vote_count": 0, **extra}


class FakeTmdb:
    """`cached_get` for discover: pages `listing[kind]` filtered to the query's
    span on its own date field, twenty a page, reporting every page it would
    take (as TMDB does) and refusing a page past 500 (as TMDB does). Any other
    path answers from `titles`, or is TMDB's 404. Records every question."""

    def __init__(self, listing=None, titles=None, *, per_page=20, down=False):
        self.listing = listing or {}
        self.titles = titles or {}
        self.per_page = per_page
        self.down = down
        self.asked: list[tuple[str, dict]] = []

    async def __call__(self, settings, path, params=None, **kw):
        params = dict(params or {})
        self.asked.append((path, params))
        if self.down:
            raise transport.TmdbError("unreachable")
        if not path.startswith("discover/"):
            if path not in self.titles:
                raise transport.TmdbNotFound(path)
            return self.titles[path]
        kind = path.split("/", 1)[1]
        (field,) = {name[:-4] for name in params if name.endswith(".gte")}
        first, last = params[f"{field}.gte"], params[f"{field}.lte"]
        matched = [e for e in self.listing.get(kind, ()) if first <= str(e.get(f"_{field}",
                   e.get(field)) or "") <= last]
        page = int(params["page"])
        if page > discover.PAGE_CAP:
            raise transport.TmdbError("page must be less than or equal to 500", 400)
        pages = math.ceil(len(matched) / self.per_page)
        chunk = matched[(page - 1) * self.per_page:page * self.per_page]
        return {"page": page, "total_pages": pages, "total_results": len(matched),
                "results": chunk}

    def pages_asked(self, kind: str) -> list[tuple[str, str, int]]:
        out = []
        for path, params in self.asked:
            if path == f"discover/{kind}":
                (field,) = {name[:-4] for name in params if name.endswith(".gte")}
                out.append((params[f"{field}.gte"], params[f"{field}.lte"], params["page"]))
        return out


def serving(tmdb: FakeTmdb):
    return mock.patch.object(transport, "cached_get", tmdb)


class TheSeriesPremieresTests(unittest.IsolatedAsyncioTestCase):

    async def fetch(self, tmdb):
        with serving(tmdb):
            return await tmdb_calendar.fetch_window(get_endpoint("shows/new"), SETTINGS, WEEK, 7)

    async def test_every_page_is_read_and_each_show_is_its_s01e01_on_its_day(self):
        tmdb = FakeTmdb({"tv": [show(n, "2026-10-27") for n in range(1, 46)]})
        records = await self.fetch(tmdb)
        self.assertEqual(len(records), 45)
        self.assertEqual(tmdb.pages_asked("tv"), [("2026-10-26", "2026-11-01", p)
                                                  for p in (1, 2, 3)])
        first = records[0]
        self.assertEqual((first.source, first.media, first.id), (Source.TMDB, Media.SHOW, "1"))
        self.assertEqual((first.season, first.episode_number, first.episode_label),
                         (1, 1, "S01E01"))
        self.assertTrue(first.date_only)
        self.assertEqual(first.air_ts, 1793059200.0)   # 2026-10-27 00:00 UTC

    async def test_a_listing_is_sparse_and_says_so(self):
        """A discover entry names no network, status or certification: the record
        says it is not described yet (so filters exempt it rather than judging
        it) and carries what the entry did state — genres by TMDB's ids."""
        record = (await self.fetch(FakeTmdb({"tv": [show(7, "2026-10-28")]})))[0]
        self.assertFalse(record.enriched)
        self.assertEqual(record.genres, ["drama", "science-fiction", "fantasy"])
        self.assertEqual((record.country, record.language), ("US", "EN"))
        self.assertEqual((record.network, record.status, record.certification), ("", "", ""))
        self.assertEqual(record.ids, {"tmdb": 7})
        self.assertEqual(record.detail_url, "https://www.themoviedb.org/tv/7")
        self.assertEqual(record.rating, 7.5)

    async def test_the_query_asks_for_no_adult_titles(self):
        tmdb = FakeTmdb({"tv": [show(1, "2026-10-27")]})
        await self.fetch(tmdb)
        _path, params = tmdb.asked[0]
        self.assertEqual(params["include_adult"], "false")

    async def test_a_title_on_two_pages_is_one_record(self):
        """Measured: a listing still being edited can move one title between two
        pages of one read."""
        tmdb = FakeTmdb({"tv": [show(1, "2026-10-27"), show(1, "2026-10-27"),
                                show(2, "2026-10-27")]}, per_page=1)
        self.assertEqual(sorted(r.id for r in await self.fetch(tmdb)), ["1", "2"])

    async def test_a_tmdb_that_cannot_be_asked_raises_rather_than_answering_empty(self):
        with self.assertRaises(transport.TmdbError):
            await self.fetch(FakeTmdb(down=True))


class TheMoviePremieresTests(unittest.IsolatedAsyncioTestCase):

    async def fetch(self, tmdb):
        with serving(tmdb):
            return await tmdb_calendar.fetch_window(get_endpoint("movies"), SETTINGS, WEEK, 7)

    async def test_the_region_and_the_release_types_narrow_the_query(self):
        tmdb = FakeTmdb({"movie": [film(1, "2026-10-30")]})
        await self.fetch(tmdb)
        _path, params = tmdb.asked[0]
        self.assertEqual((params["region"], params["with_release_type"]), ("GB", "2|3|4"))
        self.assertEqual((params["release_date.gte"], params["release_date.lte"]),
                         ("2026-10-26", "2026-11-01"))

    async def test_a_film_is_dated_by_its_own_release_date_and_has_no_episode(self):
        """The entry's `release_date` is the film's primary release — the day the
        other services' film calendars give. A later release in the region (a
        re-release, a digital release) is listed with an EARLIER day, which the
        calendar's trim to the window then drops: the film was listed in its own
        first week."""
        tmdb = FakeTmdb({"movie": [film(1, "2026-10-30"),
                                   film(2, "1994-05-11", _release_date="2026-10-31")]})
        records = await self.fetch(tmdb)
        by_id = {r.id: r for r in records}
        self.assertEqual(by_id["1"].air_ts, 1793318400.0)    # 2026-10-30
        self.assertEqual(by_id["2"].air_ts, 768614400.0)     # 1994-05-11 — trimmed later
        self.assertTrue(all(r.date_only and r.media is Media.MOVIE for r in records))
        self.assertTrue(all(r.season is None and r.episode_label is None for r in records))
        self.assertFalse(by_id["1"].enriched)
        self.assertEqual(by_id["1"].genres, ["horror"])


class ThePageCapTests(unittest.IsolatedAsyncioTestCase):
    """TMDB stops a discover listing at page 500. A span that would pass it is
    asked again as two halves, so every title in it is read."""

    async def test_a_span_past_the_cap_is_asked_as_two_halves(self):
        days = ["2026-10-26", "2026-10-27", "2026-10-28", "2026-10-29"]
        tmdb = FakeTmdb({"tv": [show(n, days[n % 4]) for n in range(1, 41)]}, per_page=1)
        with serving(tmdb), mock.patch.object(discover, "PAGE_CAP", 25):
            found = await discover.entries(SETTINGS, "tv", "first_air_date",
                                           date(2026, 10, 26), date(2026, 10, 29))
        self.assertEqual(len(found), 40)
        spans = {(first, last) for first, last, _page in tmdb.pages_asked("tv")}
        self.assertEqual(spans, {("2026-10-26", "2026-10-29"), ("2026-10-26", "2026-10-27"),
                                 ("2026-10-28", "2026-10-29")})
        self.assertLessEqual(max(page for *_span, page in tmdb.pages_asked("tv")), 25)

    async def test_one_day_past_the_cap_reads_what_tmdb_will_give_and_says_so(self):
        tmdb = FakeTmdb({"tv": [show(n, "2026-10-26") for n in range(1, 31)]}, per_page=1)
        with serving(tmdb), mock.patch.object(discover, "PAGE_CAP", 20), \
                self.assertLogs("app.providers.tmdb.discover", "WARNING"):
            found = await discover.entries(SETTINGS, "tv", "first_air_date",
                                           date(2026, 10, 26), date(2026, 10, 26))
        self.assertEqual(len(found), 20)


class WhatItAnswersTests(unittest.IsolatedAsyncioTestCase):

    async def test_a_calendar_tmdb_does_not_list_is_refused_not_answered_empty(self):
        """The fill never asks it (Capabilities.endpoints), so being asked is a
        caller's mistake, and an empty answer would read as "nothing airs"."""
        for key in ("shows/finales", "shows"):
            with self.subTest(endpoint=key), self.assertRaises(ValueError):
                await tmdb_calendar.fetch_window(get_endpoint(key), SETTINGS, WEEK, 7)

    def test_it_declares_exactly_the_calendars_it_answers(self):
        from app.providers import tmdb
        self.assertEqual(tmdb._TmdbProvider.capabilities.endpoints, tmdb_calendar.ENDPOINTS)
