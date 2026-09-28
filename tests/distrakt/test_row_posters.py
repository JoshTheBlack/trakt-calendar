"""A picture on every row of the tracker's month.

WHAT THIS FILE HOLDS, and why each matters:
  - THE ADDRESS, NEVER THE IMAGE. A row draws its picture's address through the
    image proxy; nothing is fetched to disk.
  - AT MOST ONE LOOKUP PER TITLE, EVER. What is stored answers first — the
    calendar's own record of the title, then the poster registry — and only a
    title neither knows costs one lookup, made while the month is computed (never
    while it is drawn) and recorded for every later load. A title found to have
    no picture is recorded too, so it is not asked about again.
  - A ROW IS A SEASON, so a season's own artwork is drawn where it is known, and
    its show's otherwise.

No network: the month's provider reads are stubbed by MonthFragmentTestCase, and
tests/distrakt/conftest.py answers the poster lookup with nothing unless a test
says otherwise.
"""
from __future__ import annotations

import asyncio
import re
from html import unescape
from unittest.mock import patch

from app import distrakt as distrakt_store
from app.distrakt import watch_history
from app.media import artwork, posters
from app.providers.base import Media, proxied_image
from tests.distrakt.test_month_fragment import MonthFragmentTestCase
from tests.ranker.test_search import a_record, store

_POSTER = re.compile(r'<(img|span) class="distrakt-poster[^"]*"(?: src="([^"]*)")?')


class RowPosterTests(MonthFragmentTestCase):

    def pictures(self, year=None, month=None) -> list[str]:
        """Every show row's picture as the browser would ask for it ("" for an
        empty slot)."""
        year, month = (year, month) if year else self.current
        outer, season, calendar = self.offline()
        with outer, season, calendar:
            html = self.client.get(f"/distrakt/fragments/month?year={year}&month={month}").text
        return [unescape(src or "") for _tag, src in _POSTER.findall(html)]

    def lookups(self, answer=None) -> tuple:
        """The one lookup, counted, answering `answer` (an origin address or None)."""
        calls = []

        async def _lookup(settings, media, tmdb):
            calls.append((media, tmdb))
            if answer:
                await artwork.record_poster_url(media, tmdb, "trakt", answer)
            return answer

        return calls, patch.object(posters, "_fresh_provider_lookup", _lookup)

    def test_the_calendars_own_picture_is_drawn_with_no_lookup(self):
        store(a_record("Listed Show", 500, poster="https://media.trakt.tv/cal.jpg"))
        self.list_one()
        calls, patched = self.lookups()
        with patched:
            self.assertEqual(self.pictures(),
                             [proxied_image("https://media.trakt.tv/cal.jpg")])
        self.assertEqual(calls, [])

    def test_a_registry_address_is_drawn_with_no_lookup(self):
        asyncio.run(artwork.record_poster_url("show", 500, "trakt", "https://x/reg.jpg"))
        self.list_one()
        calls, patched = self.lookups()
        with patched:
            self.assertEqual(self.pictures(), [proxied_image("https://x/reg.jpg")])
        self.assertEqual(calls, [])

    def test_an_unknown_title_costs_exactly_one_lookup_ever(self):
        self.list_one()
        calls, patched = self.lookups("https://x/found.jpg")
        with patched:
            first = self.pictures()
            second = self.pictures()
        self.assertEqual(calls, [("show", 500)])
        self.assertEqual(first, [proxied_image("https://x/found.jpg")])
        self.assertEqual(second, first)
        self.assertEqual(asyncio.run(artwork.urls_for("show", 500)),
                         {"trakt": "https://x/found.jpg"})

    def test_a_title_with_no_picture_is_remembered_and_not_asked_again(self):
        self.list_one()
        calls, patched = self.lookups(None)
        with patched:
            self.assertEqual(self.pictures(), [""])
            self.assertEqual(self.pictures(), [""])
        self.assertEqual(calls, [("show", 500)])
        self.assertEqual(asyncio.run(artwork.known([("show", 500, None)])).missed,
                         {("show", 500)})

    def test_a_seasons_own_artwork_is_drawn_before_its_shows(self):
        asyncio.run(artwork.record_poster_url("show", 500, "trakt", "https://x/show.jpg"))
        asyncio.run(artwork.record_season_poster_urls(
            [("show", 500, 2, "trakt", "https://x/s2.jpg")]))
        self.list_one(title="Season Two", season=2)
        self.list_one(title="Season One", season=1)
        self.assertEqual(sorted(self.pictures()),
                         sorted([proxied_image("https://x/s2.jpg"),
                                 proxied_image("https://x/show.jpg")]))

    def test_the_viewers_own_order_picks_the_picture(self):
        import dataclasses

        from app.sources import prefs as source_prefs
        for source in ("trakt", "simkl"):
            asyncio.run(artwork.record_poster_url("show", 500, source, f"https://x/{source}.jpg"))
        prefs = asyncio.run(source_prefs.load(self.user_id))
        asyncio.run(source_prefs.save(dataclasses.replace(prefs, metadata_order=["simkl"])))
        self.list_one()
        self.assertEqual(self.pictures(), [proxied_image("https://x/simkl.jpg")])

    def test_nothing_is_written_to_disk(self):
        self.list_one()
        _calls, patched = self.lookups("https://x/found.jpg")
        with patched:
            self.pictures()
        self.assertFalse(list(posters.POSTER_DIR.glob("show/500.*")),
                         "a tracker row's picture is an address, never a tile")

    def test_a_frozen_month_draws_what_is_known_and_asks_nobody(self):
        year, month = self.past
        key = distrakt_store.month_key(year, month)
        asyncio.run(distrakt_store.add_month_record(self.user_id, key, {
            "media": Media.SHOW, "ids": {"trakt": 500, "tmdb": 500}, "season": 1,
            "title": "Frozen Show", "network": "HBO",
            "kind": distrakt_store.RecordKind.COMPLETED, "watched": 10, "total": 10}))
        doc = asyncio.run(distrakt_store.load_month(self.user_id, key))
        asyncio.run(distrakt_store.save_month(self.user_id, {**doc, "closed": True}))
        calls, patched = self.lookups("https://x/found.jpg")
        with patched:
            self.assertEqual(self.pictures(year, month), [""])
        self.assertEqual(calls, [])

    def test_the_network_badge_stays(self):
        asyncio.run(artwork.record_poster_url("show", 500, "trakt", "https://x/reg.jpg"))
        self.list_one()
        outer, season, calendar = self.offline()
        with outer, season, calendar:
            html = self.client.get("/distrakt/fragments/month?year=%d&month=%d" % self.current).text
        row = re.search(r'<div class="distrakt-show-row.*?</div>', html, re.S).group(0)
        self.assertIn('class="distrakt-poster"', row)
        self.assertIn('class="distrakt-badge"', row)
        self.assertLess(row.index("distrakt-poster"), row.index("distrakt-badge"))


class FilmRowPosterTests(MonthFragmentTestCase):
    """A watched film's row draws a picture by the same rules as a show's.

    WHAT A FILM ROW HOLDS: the ids the reporting service gave, which the watch
    store keeps only as that service's own id — the tmdb id lives in the KEY
    (`movie:tmdb:N`), the identity the watch was filed under. So these films are
    recorded with a tmdb id that comes back from the store without one, which is
    the shape every real film row has."""

    pictures = RowPosterTests.pictures
    lookups = RowPosterTests.lookups

    def watch_one(self):
        year, month = self.current
        asyncio.run(watch_history.record_movie_watches(self.user_id, [{
            "ids": {"tmdb": 603, "trakt": 481}, "title": "The Matrix", "year": 1999,
            "watched_at": f"{year:04d}-{month:02d}-01T12:00:00Z"}]))

    def film_row(self, year=None, month=None) -> str:
        year, month = (year, month) if year else self.current
        outer, season, calendar = self.offline()
        with outer, season, calendar:
            html = self.client.get(f"/distrakt/fragments/month?year={year}&month={month}").text
        return re.search(r'<div class="distrakt-show-row distrakt-film-row.*?</div>',
                         html, re.S).group(0)

    def test_a_film_row_keeps_no_tmdb_id_of_its_own(self):
        # The premise the rest of this class stands on, measured rather than
        # assumed: if the store starts keeping a film's tmdb id, the key fallback
        # in routes._row_tmdb is no longer what these tests exercise.
        self.watch_one()
        state = asyncio.run(watch_history._load(self.user_id))
        self.assertEqual(list(state["movies"]), ["movie:tmdb:603"])
        self.assertNotIn("tmdb", state["movies"]["movie:tmdb:603"]["ids"])

    def test_the_calendars_own_picture_is_drawn_with_no_lookup(self):
        store(a_record("The Matrix", 603, Media.MOVIE, poster="https://media.trakt.tv/m.jpg"),
              endpoint="movies/releases")
        self.watch_one()
        calls, patched = self.lookups()
        with patched:
            self.assertEqual(self.pictures(), [proxied_image("https://media.trakt.tv/m.jpg")])
        self.assertEqual(calls, [])

    def test_a_registry_address_is_drawn_with_no_lookup(self):
        asyncio.run(artwork.record_poster_url("movie", 603, "trakt", "https://x/reg.jpg"))
        self.watch_one()
        calls, patched = self.lookups()
        with patched:
            self.assertEqual(self.pictures(), [proxied_image("https://x/reg.jpg")])
        self.assertEqual(calls, [])

    def test_an_unknown_film_costs_exactly_one_lookup_ever_by_its_keys_tmdb_id(self):
        self.watch_one()
        calls, patched = self.lookups("https://x/found.jpg")
        with patched:
            first = self.pictures()
            second = self.pictures()
        self.assertEqual(calls, [("movie", 603)])
        self.assertEqual(first, [proxied_image("https://x/found.jpg")])
        self.assertEqual(second, first)
        self.assertEqual(asyncio.run(artwork.urls_for("movie", 603)),
                         {"trakt": "https://x/found.jpg"})

    def test_a_film_with_no_picture_is_remembered_and_not_asked_again(self):
        self.watch_one()
        calls, patched = self.lookups(None)
        with patched:
            self.assertEqual(self.pictures(), [""])
            self.assertEqual(self.pictures(), [""])
        self.assertEqual(calls, [("movie", 603)])
        self.assertEqual(asyncio.run(artwork.known([("movie", 603, None)])).missed,
                         {("movie", 603)})

    def test_a_film_and_a_show_with_one_tmdb_number_are_two_titles(self):
        # The registry is keyed by (media, tmdb): TMDB numbers films and shows
        # separately, so the same number names two unrelated titles.
        asyncio.run(artwork.record_poster_url("show", 603, "trakt", "https://x/show.jpg"))
        asyncio.run(artwork.record_poster_url("movie", 603, "trakt", "https://x/film.jpg"))
        self.watch_one()
        self.assertEqual(self.pictures(), [proxied_image("https://x/film.jpg")])

    def test_the_viewers_own_order_picks_the_picture(self):
        import dataclasses

        from app.sources import prefs as source_prefs
        for source in ("trakt", "simkl"):
            asyncio.run(artwork.record_poster_url("movie", 603, source, f"https://x/{source}.jpg"))
        prefs = asyncio.run(source_prefs.load(self.user_id))
        asyncio.run(source_prefs.save(dataclasses.replace(prefs, metadata_order=["simkl"])))
        self.watch_one()
        self.assertEqual(self.pictures(), [proxied_image("https://x/simkl.jpg")])

    def test_nothing_is_written_to_disk(self):
        self.watch_one()
        _calls, patched = self.lookups("https://x/found.jpg")
        with patched:
            self.pictures()
        self.assertFalse(list(posters.POSTER_DIR.glob("movie/603.*")),
                         "a tracker row's picture is an address, never a tile")

    def test_a_frozen_month_draws_what_is_known_and_asks_nobody(self):
        year, month = self.past
        key = distrakt_store.month_key(year, month)
        asyncio.run(distrakt_store.save_month(self.user_id, {
            "month": key, "closed": True, "shows": [],
            "movies": [{"key": "movie:tmdb:603", "ids": {"trakt": 481},
                        "title": "The Matrix", "year": 1999,
                        "watched_at": f"{year:04d}-{month:02d}-02T12:00:00Z"}]}))
        calls, patched = self.lookups("https://x/found.jpg")
        with patched:
            self.assertEqual(self.pictures(year, month), [""])
        self.assertEqual(calls, [])
        asyncio.run(artwork.record_poster_url("movie", 603, "trakt", "https://x/reg.jpg"))
        self.assertEqual(self.pictures(year, month), [proxied_image("https://x/reg.jpg")])

    def test_the_film_badge_stays_after_the_picture(self):
        asyncio.run(artwork.record_poster_url("movie", 603, "trakt", "https://x/reg.jpg"))
        self.watch_one()
        row = self.film_row()
        self.assertIn('class="distrakt-poster"', row)
        self.assertIn("🎬", row)
        self.assertLess(row.index("distrakt-poster"), row.index("🎬"))
