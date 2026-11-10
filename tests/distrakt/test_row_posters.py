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
from urllib.parse import parse_qs, urlsplit

from app import db
from app import distrakt as distrakt_store
from app.distrakt import watch_history
from app.media import artwork, posters
from app.metadata import pictures
from app.metadata import refresh as metadata_refresh
from app.providers.base import Media, proxied_image, proxied_origin
from tests.distrakt.test_month_fragment import MonthFragmentTestCase
from tests.ranker.test_search import a_record, store


def _report(src: str):
    """The report a drawn picture carries behind it, read back, or None."""
    default = parse_qs(urlsplit(src).query).get("default", [""])[0]
    token = parse_qs(urlsplit(default).query).get("t", [""])[0]
    return pictures.read(token, now=db.now()) if token else None


_POSTER = re.compile(r'<(img|span) class="distrakt-poster[^"]*"(?: src="([^"]*)")?')


class RowPosterTests(MonthFragmentTestCase):

    def drawn(self, year=None, month=None) -> list[str]:
        """Every row's picture exactly as the browser would ask for it ("" for an
        empty slot)."""
        year, month = (year, month) if year else self.current
        outer, season, calendar = self.offline()
        with outer, season, calendar:
            html = self.client.get(f"/distrakt/fragments/month?year={year}&month={month}").text
        return [unescape(src or "") for _tag, src in _POSTER.findall(html)]

    def pictures(self, year=None, month=None) -> list[str]:
        """Every row's picture as the ORIGIN address it draws ("" for an empty
        slot) — what a row shows, whatever the proxy is asked around it."""
        return [proxied_origin(src) for src in self.drawn(year, month)]

    def reports(self, year=None, month=None) -> list:
        """What each row's report address says (app/metadata/pictures.py)."""
        return [_report(src) for src in self.drawn(year, month)]

    def lookups(self, answer=None) -> tuple:
        """The one lookup, counted, answering `answer` (an origin address or None)."""
        calls = []

        async def _lookup(settings, media, tmdb):
            calls.append((media, tmdb))
            return answer

        return calls, patch.dict(posters._ADDRESS_LOOKUPS, {"trakt": _lookup})

    def test_the_calendars_own_picture_is_drawn_with_no_lookup(self):
        store(a_record("Listed Show", 500, poster="https://media.trakt.tv/cal.jpg"))
        self.list_one()
        calls, patched = self.lookups()
        with patched:
            self.assertEqual(self.pictures(),
                             ["https://media.trakt.tv/cal.jpg"])
        self.assertEqual(calls, [])

    def test_a_registry_address_is_drawn_with_no_lookup(self):
        asyncio.run(artwork.record_poster_url("show", 500, "trakt", "https://x/reg.jpg"))
        self.list_one()
        calls, patched = self.lookups()
        with patched:
            self.assertEqual(self.pictures(), ["https://x/reg.jpg"])
        self.assertEqual(calls, [])

    def test_the_registrys_address_for_a_service_wins_over_the_stores_copy(self):
        """The calendar's record of the title keeps the address it was listed
        with; the registry is where a service's address is kept current. The
        copy drawn over the registry put back a picture Trakt had replaced."""
        store(a_record("Listed Show", 500, poster="https://media.trakt.tv/old.jpg"))
        asyncio.run(artwork.record_poster_url("show", 500, "trakt", "https://media.trakt.tv/new.jpg"))
        self.list_one()
        self.assertEqual(self.pictures(), ["https://media.trakt.tv/new.jpg"])

    def test_an_address_the_registry_gave_up_on_is_not_drawn_from_the_copy(self):
        """A failing address is left out of what the registry answers; the
        store's copy of the same address must not put it back in front."""
        dead = "https://media.trakt.tv/dead.jpg"
        store(a_record("Listed Show", 500, poster=dead))
        asyncio.run(artwork.record_poster_url("show", 500, "trakt", dead))
        asyncio.run(artwork.record_poster_url("show", 500, "simkl", "https://x/simkl.jpg"))
        for _ in range(artwork.MAX_FAIL_COUNT):
            asyncio.run(artwork.record_failure("show", 500, "trakt"))
        self.list_one()
        self.assertEqual(self.pictures(), ["https://x/simkl.jpg"])

    def test_an_unknown_title_costs_exactly_one_lookup_ever(self):
        self.list_one()
        calls, patched = self.lookups("https://x/found.jpg")
        with patched:
            first = self.pictures()
            second = self.pictures()
        self.assertEqual(calls, [("show", 500)])
        self.assertEqual(first, ["https://x/found.jpg"])
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
        # The season's own first — and its show's is what a report of it is
        # answered with, in case it is dead.
        self.assertEqual(sorted(self.pictures()), ["https://x/s2.jpg", "https://x/show.jpg"])
        reported = next(r for r in self.reports() if r.title.season == 2)
        self.assertEqual((reported.source, reported.season), ("trakt", 2))
        self.assertEqual(asyncio.run(pictures.next_choice(reported)).url, "https://x/show.jpg")

    def test_the_viewers_own_order_picks_the_picture(self):
        import dataclasses

        from app.sources import prefs as source_prefs
        for source in ("trakt", "simkl"):
            asyncio.run(artwork.record_poster_url("show", 500, source, f"https://x/{source}.jpg"))
        prefs = asyncio.run(source_prefs.load(self.user_id))
        asyncio.run(source_prefs.save(dataclasses.replace(prefs, metadata_order=["simkl"])))
        self.list_one()
        # Simkl's first, as the viewer ordered, and Trakt's what a report of it
        # is answered with, in case the first address has died at its host.
        self.assertEqual(self.pictures(), ["https://x/simkl.jpg"])
        reported, = self.reports()
        self.assertEqual(reported.source, "simkl")
        self.assertEqual(asyncio.run(pictures.next_choice(reported)).url, "https://x/trakt.jpg")

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

    def test_the_badge_sits_between_the_season_and_the_network(self):
        """Beside the network it stands for, not ahead of the title."""
        self.list_one()
        outer, season, calendar = self.offline()
        with outer, season, calendar:
            html = self.client.get("/distrakt/fragments/month?year=%d&month=%d" % self.current).text
        row = re.search(r'<div class="distrakt-show-row.*?</div>', html, re.S).group(0)
        order = [row.index(f'class="{cell}') for cell in
                 ("distrakt-title", "distrakt-season", "distrakt-badge", "distrakt-network")]
        self.assertEqual(order, sorted(order))


class FilmRowPosterTests(MonthFragmentTestCase):
    """A watched film's row draws a picture by the same rules as a show's.

    WHAT A FILM ROW HOLDS: the ids the reporting service gave, which the watch
    store keeps only as that service's own id — the tmdb id lives in the KEY
    (`movie:tmdb:N`), the identity the watch was filed under. So these films are
    recorded with a tmdb id that comes back from the store without one, which is
    the shape every real film row has."""

    drawn = RowPosterTests.drawn
    pictures = RowPosterTests.pictures
    reports = RowPosterTests.reports
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
              endpoint="movies")
        self.watch_one()
        calls, patched = self.lookups()
        with patched:
            self.assertEqual(self.pictures(), ["https://media.trakt.tv/m.jpg"])
        self.assertEqual(calls, [])

    def test_a_registry_address_is_drawn_with_no_lookup(self):
        asyncio.run(artwork.record_poster_url("movie", 603, "trakt", "https://x/reg.jpg"))
        self.watch_one()
        calls, patched = self.lookups()
        with patched:
            self.assertEqual(self.pictures(), ["https://x/reg.jpg"])
        self.assertEqual(calls, [])

    def test_an_unknown_film_costs_exactly_one_lookup_ever_by_its_keys_tmdb_id(self):
        self.watch_one()
        calls, patched = self.lookups("https://x/found.jpg")
        with patched:
            first = self.pictures()
            second = self.pictures()
        self.assertEqual(calls, [("movie", 603)])
        self.assertEqual(first, ["https://x/found.jpg"])
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
        self.assertEqual(self.pictures(), ["https://x/film.jpg"])

    def test_the_viewers_own_order_picks_the_picture(self):
        import dataclasses

        from app.sources import prefs as source_prefs
        for source in ("trakt", "simkl"):
            asyncio.run(artwork.record_poster_url("movie", 603, source, f"https://x/{source}.jpg"))
        prefs = asyncio.run(source_prefs.load(self.user_id))
        asyncio.run(source_prefs.save(dataclasses.replace(prefs, metadata_order=["simkl"])))
        self.watch_one()
        # Simkl's first, as the viewer ordered, and Trakt's what a report of it
        # is answered with, in case the first address has died at its host.
        self.assertEqual(self.pictures(), ["https://x/simkl.jpg"])
        reported, = self.reports()
        self.assertEqual(reported.source, "simkl")
        self.assertEqual(asyncio.run(pictures.next_choice(reported)).url, "https://x/trakt.jpg")

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
        self.assertEqual(self.pictures(year, month), ["https://x/reg.jpg"])

    def test_a_month_no_service_answers_still_draws_its_films_and_their_pictures(self):
        """The degraded page draws last-known everything, films included: a month
        under way keeps its films in the stored watch state, not in a snapshot, so
        reading them from the snapshot alone emptied its Films on every refresh a
        service could not answer."""
        from unittest.mock import AsyncMock

        from app.providers.base import SourceUnavailable
        asyncio.run(artwork.record_poster_url("movie", 603, "trakt", "https://x/reg.jpg"))
        self.watch_one()
        down = patch.object(watch_history, "sync_and_baseline",
                            AsyncMock(side_effect=SourceUnavailable("Could not reach Trakt")))
        calls, patched = self.lookups("https://x/found.jpg")
        with down, patched:
            row = self.film_row()
        self.assertIn("The Matrix", row)
        self.assertIn(proxied_image("https://x/reg.jpg").replace("&", "&amp;"), row)
        self.assertEqual(calls, [])

    def test_the_film_badge_stays_after_the_picture(self):
        asyncio.run(artwork.record_poster_url("movie", 603, "trakt", "https://x/reg.jpg"))
        self.watch_one()
        row = self.film_row()
        self.assertIn('class="distrakt-poster"', row)
        self.assertIn("🎬", row)
        self.assertLess(row.index("distrakt-poster"), row.index("🎬"))


class AFilmRowOpensItsPanelTests(MonthFragmentTestCase):
    """The film row names itself to the details route by its key and nothing
    else, as a show row does; the route reads the rest from the viewer's watch."""

    watch_one = FilmRowPosterTests.watch_one
    film_row = FilmRowPosterTests.film_row

    def test_the_row_carries_the_opener(self):
        self.watch_one()
        year, month = self.current
        row = self.film_row()
        self.assertIn(f'data-details-open="/distrakt/fragments/details?year={year}&amp;month={month}'
                      f'&amp;key=movie%3Atmdb%3A603"', row)
        self.assertIn('data-details-heading="The Matrix (1999)"', row)


class RowsDrawnAreTitlesViewedTests(MonthFragmentTestCase):
    """A month's rows name their titles, and the season each row is, to the
    title store's refresh queue (app/metadata/refresh.py) — behind the page."""

    def test_every_row_is_named_with_its_season(self):
        self.list_one()
        seen = []
        outer, season, calendar = self.offline()
        with outer, season, calendar, patch.object(
                metadata_refresh, "viewed",
                lambda settings, wanted, **kw: seen.append((list(wanted), kw))):
            year, month = self.current
            self.client.get(f"/distrakt/fragments/month?year={year}&month={month}")
        (wanted, kw), = seen
        self.assertEqual(kw["budget"], metadata_refresh.ROWS_BUDGET)
        self.assertEqual([(w.media, w.season, w.ids.get("tmdb")) for w in wanted],
                         [("show", 1, 500)])


class EveryRowReportsADeadPictureTests(MonthFragmentTestCase):
    """A row's picture carries this app's report address as the proxy's default,
    naming the row's title, its season, the service whose picture it is and the
    viewer's order (app/metadata/pictures.py)."""

    def test_the_report_names_the_row(self):
        asyncio.run(artwork.record_poster_url("show", 500, "trakt", "https://x/reg.jpg"))
        RowPosterTests.list_one(self)
        src, = RowPosterTests.drawn(self)
        self.assertIn("&default=" + "https%3A%2F%2Ftestserver%2Fapi%2Fposters%2Fmissed", src)
        reported = _report(src)
        self.assertEqual((reported.title.media, reported.title.tmdb, reported.title.season,
                          reported.source, reported.digest),
                         ("show", 500, 1, "trakt", pictures.digest("https://x/reg.jpg")))
