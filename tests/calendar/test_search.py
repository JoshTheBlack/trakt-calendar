"""Finding a title on the calendar.

THE RULE THIS FILE EXISTS TO HOLD: a jump is a promise that there is something
to jump to, and it may only ever be offered for an airing THIS viewer's own
calendar read would actually draw. A title airs on a date and still does not
appear for six independent reasons — wrong endpoint, the instance content floor,
the viewer's own filters, their source selection, the source's declared reach,
or nothing stored for that month — and an answer derived from air dates alone
lands a reader on a day whose grid draws nothing, with nothing on the page able
to explain itself.

So the stored half is confirmed by `assemble_range` rather than by this module
deciding for itself what a filter would have done.

No network: the autouse guard in tests/conftest.py fails any test that reaches
for one, which is exactly the assertion the typing path needs.
"""
from __future__ import annotations

import unittest
from datetime import date
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

from app import db
from app.calendar import cache as calendar_cache, entries as calendar_entries
from app.calendar import search as calendar_search
from app.config import Settings
from app.endpoints import get_endpoint
from app.providers.base import Media, Record, Source
from app.sources import prefs as source_prefs
from tests.support import migrated_db

SHOWS = "shows/premieres"
# 2026-07-08 20:00Z, mid-window and mid-month so no test is about a boundary.
AIR = 1783540800.0

NO_FILTERS = {
    "genres": "", "countries": "", "show_certifications": "",
    "movie_certifications": "", "movie_release_countries": "",
    "movie_release_types": "", "network_filter": [],
}


def _record(title: str, *, source=Source.TRAKT, air_ts=AIR, country="us",
            source_id=None, tmdb=None) -> Record:
    name = str(source)
    return Record(
        source=source, media=Media.SHOW, id=source_id or title.lower().replace(" ", "-"),
        ids={"tmdb": tmdb or abs(hash(title)) % 9999, name: title.lower()},
        detail_url=f"https://{name}.test/show", title=title, air_ts=air_ts,
        season=1, episode_number=1, episode_label="S01E01",
        country=country, genres=["drama"], enriched=True)


class SearchTestCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        migrated_db(f"calsearch-{id(self)}")
        self.settings = Settings(simkl_public_calendar_enabled=False)
        self.tz = ZoneInfo("UTC")

    async def asyncTearDown(self):
        db.close_thread_connection()

    async def store(self, *records, endpoint=SHOWS, start=date(2026, 7, 6)):
        await calendar_cache.store_window(
            endpoint, calendar_cache.window_start(start), list(records), 600, 1000,
            sources=["trakt"], asked=["trakt"])

    async def find(self, query, *, prefs=None, marks=frozenset(), endpoints=None):
        """Searched on ONE endpoint by default, which is what the route does: a
        search is nearly always about the calendar in front of somebody, and the
        five are five different questions."""
        return await calendar_search.stored(
            query, settings=self.settings, prefs=prefs or dict(NO_FILTERS),
            tz=self.tz, source_selection=source_prefs.SourcePrefs(user_id=1),
            marks=marks, endpoints=endpoints or [get_endpoint(SHOWS)])


class StoredResultsTests(SearchTestCase):
    async def test_it_finds_a_title_and_names_the_day_it_airs(self):
        await self.store(_record("Severance"))
        found = await self.find("severance")
        self.assertEqual([a.item.title for a in found.airings], ["Severance"])
        hit = found.airings[0]
        self.assertEqual((hit.year, hit.month, hit.day), (2026, 7, "2026-07-08"))
        self.assertEqual(hit.endpoint_key, SHOWS)

    async def test_the_jump_url_carries_the_month_the_day_and_the_card(self):
        """All three, because all three are needed: the month is what the
        calendar route reads, the anchor is what scrolls, and the card id is what
        the page highlights once the day has painted."""
        await self.store(_record("Severance"))
        url = (await self.find("severance")).airings[0].url
        self.assertIn("year=2026", url)
        self.assertIn("month=7", url)
        self.assertIn("endpoint=shows%2Fpremieres", url)
        self.assertIn("#day-2026-07-08", url)
        self.assertIn("highlight=", url)

    async def test_it_matches_on_a_fold_rather_than_on_the_exact_spelling(self):
        """The same folding a stored title was written with — case-folded and
        accent-stripped — so a title typed from memory still finds it."""
        await self.store(_record("Pokémon Horizons"))
        self.assertEqual(len((await self.find("pokemon")).airings), 1)
        self.assertEqual(len((await self.find("POKEMON HORIZONS")).airings), 1)

    async def test_a_substring_matches_the_middle_of_a_title(self):
        await self.store(_record("The Last of Us"))
        self.assertEqual(len((await self.find("last of")).airings), 1)

    async def test_a_query_too_short_to_mean_anything_answers_nothing(self):
        await self.store(_record("Severance"))
        self.assertEqual((await self.find("s")).airings, ())

    async def test_every_airing_of_one_title_is_its_own_row(self):
        """The question is "when does this air", so three airings are three
        answers rather than one title with a footnote."""
        # EACH IN ITS OWN WINDOW, because storage trims an airing that falls
        # outside the span it was stored under — a source handing back entries
        # past the bound it was given is the reason that trim exists.
        await self.store(_record("Severance"))
        await self.store(_record("Severance", air_ts=AIR + 7 * 86400),
                         start=date(2026, 7, 13))
        await self.store(_record("Severance", air_ts=AIR + 14 * 86400),
                         start=date(2026, 7, 20))
        found = await self.find("severance")
        self.assertEqual(len(found.airings), 3)
        # Newest first: a search is usually about what is coming.
        self.assertEqual([a.day for a in found.airings],
                         ["2026-07-22", "2026-07-15", "2026-07-08"])


class AJumpIsOnlyOfferedForACardThatWouldBeDrawnTests(SearchTestCase):
    """THE RULE THE WHOLE DESIGN RESTS ON. Every one of these titles is stored,
    airs on a real date, and would be found by any search built on air dates —
    and every one of them is invisible to the viewer asking, so offering a jump
    would land them on a grid that draws nothing."""

    async def test_a_title_the_viewers_country_filter_removes_is_not_offered(self):
        await self.store(_record("Severance", country="th"))
        prefs = dict(NO_FILTERS, countries="-th")
        self.assertEqual((await self.find("severance", prefs=prefs)).airings, ())
        # ...and the same search without that filter does find it, so the test is
        # about the filter rather than about the title being unfindable.
        self.assertEqual(len((await self.find("severance")).airings), 1)

    async def test_a_title_the_genre_filter_removes_is_not_offered(self):
        await self.store(_record("Severance"))
        prefs = dict(NO_FILTERS, genres="-drama")
        self.assertEqual((await self.find("severance", prefs=prefs)).airings, ())

    async def test_a_source_this_viewer_does_not_admit_is_not_offered(self):
        """Resolving to nobody is a real answer on the read path, and a search
        must inherit it rather than reaching past it into storage."""
        await self.store(_record("Severance", source=Source.SIMKL))
        narrowed = source_prefs.SourcePrefs(user_id=1, calendar_source="trakt")
        found = await calendar_search.stored(
            "severance", settings=self.settings, prefs=dict(NO_FILTERS),
            tz=self.tz, source_selection=narrowed, marks=frozenset(),
            endpoints=[get_endpoint(SHOWS)])
        self.assertEqual(found.airings, ())

    async def test_nothing_stored_for_a_month_is_simply_no_answer(self):
        """Not an error and not an empty jump — the catalogue half is what
        answers for a month nobody has opened, and only when asked."""
        self.assertEqual((await self.find("severance")).airings, ())


class ItSpendsNoRequestsTests(SearchTestCase):
    async def test_the_stored_half_makes_no_outbound_call(self):
        """The reason it is safe to run while somebody is still typing. The
        autouse network guard would fail this test if it did, so this asserts
        the intent as well: `allow_fetch=False` on every read."""
        await self.store(_record("Severance"))
        with patch("app.calendar.cache.fetch_window_records",
                   side_effect=AssertionError("a search fetched a window")):
            found = await self.find("severance")
        self.assertEqual(len(found.airings), 1)


class TheLocalDayIsTheViewersOwnTests(SearchTestCase):
    async def test_two_viewers_can_be_sent_to_different_days(self):
        """An airing at 01:00 UTC is the previous evening in New York, and a jump
        has to land on the day THAT viewer's own calendar drew it on."""
        # 2026-07-09 01:00Z
        await self.store(_record("Severance", air_ts=1783558800.0))

        east = await self.find("severance")
        self.assertEqual(east.airings[0].day, "2026-07-09")

        self.tz = ZoneInfo("America/New_York")
        west = await self.find("severance")
        self.assertEqual(west.airings[0].day, "2026-07-08")
        self.assertEqual((west.airings[0].year, west.airings[0].month), (2026, 7))


class TheNeedleIsEscapedTests(unittest.TestCase):
    """A search box is untrusted input and `%` is a character in titles.

    Left unescaped, a query of `%` matches the whole table and `100%` silently
    matches far more than it should. Both are ordinary things to type."""

    def test_a_wildcard_typed_by_a_person_is_a_literal(self):
        self.assertEqual(calendar_entries.like_needle("100%"), r"%100\%%")
        self.assertEqual(calendar_entries.like_needle("a_b"), r"%a\_b%")

    def test_the_escape_character_cannot_smuggle_itself(self):
        self.assertEqual(calendar_entries.like_needle("a\\b"), r"%a\\b%")

    def test_it_folds_the_needle_the_same_way_a_stored_title_was_folded(self):
        self.assertEqual(calendar_entries.like_needle("Pokémon"), "%pokemon%")


class TheCatalogueHalfIsADifferentPromiseTests(SearchTestCase):
    """It links to a MONTH, never to a day, because visiting a month FILLS it —
    and whether a card then appears depends on whether any source lists that
    title on that calendar, which nothing here can promise."""

    def _hit(self, title="Unseen Show", simkl_id="4242", also=None):
        """A REAL `MergedSearchHit`, and the reason that matters is a bug this
        file did not catch.

        `search_catalogue` returns MERGED hits — one row per title, assembled
        from every source that answered — and a merged hit has no single
        `source` or `source_id`: it carries `source_ids`, a source-to-id map
        ordered so its first entry is the leader. The double here used to return
        a per-source `SearchHit` instead, which DOES have those two fields, so
        every test passed while the live route raised AttributeError on the
        first real search. A double that reports a different type than the
        function it stands in for tests nothing about the caller.
        """
        from app.distrakt.search import MergedSearchHit

        sources = {Source.SIMKL: simkl_id}
        sources.update(also or {})
        return MergedSearchHit(key=None, season=None, source_ids=sources,
                               ids={"simkl": simkl_id, "tmdb": 777}, title=title,
                               year=2026, network="", runtime=None, overview="")

    async def _catalogue(self, described, known=frozenset()):
        # ANSWERS FOR SHOWS AND NOT FOR FILMS, because the real thing asks
        # both and gets different titles back. A double that returned the
        # same hit to each would report one title twice and hide that.
        async def _search(asked, settings, media, query):
            if media is Media.SHOW:
                return SimpleNamespace(hits=[self._hit()], failed=frozenset())
            return SimpleNamespace(hits=[], failed=frozenset())

        with patch("app.distrakt.search.search_catalogue", new=_search), \
             patch("app.providers.for_catalogue_search",
                   return_value=[(Source.SIMKL, object())]), \
             patch("app.providers.get", return_value=SimpleNamespace(
                 detail_port=SimpleNamespace(
                     fetch_details=AsyncMock(return_value=described)))):
            return await calendar_search.catalogue(
                "unseen", settings=self.settings, tz=self.tz, known=known)

    async def test_it_links_to_the_month_and_not_to_a_day(self):
        found = await self._catalogue({"first_aired": "2026-11-04T20:00:00Z",
                                       "title": "Unseen Show"})
        self.assertEqual(len(found.elsewhere), 1)
        url = found.elsewhere[0].url
        self.assertIn("year=2026", url)
        self.assertIn("month=11", url)
        self.assertNotIn("#day-", url)
        self.assertNotIn("highlight=", url)

    async def test_a_title_with_no_date_is_not_offered_at_all(self):
        """The whole offer is "go to where this should be", and a title nobody
        can date has no where. Refusing beats sending a reader to a guess."""
        found = await self._catalogue({"title": "Unseen Show", "first_aired": ""})
        self.assertEqual(found.elsewhere, ())

    async def test_an_unreadable_date_is_refused_rather_than_guessed(self):
        found = await self._catalogue({"first_aired": "sometime next year"})
        self.assertEqual(found.elsewhere, ())

    async def test_a_title_the_calendar_already_draws_is_not_offered_twice(self):
        """The stored answer names the day, which is strictly better than naming
        the month; offering both would be two rows for one answer."""
        first = await self._catalogue({"first_aired": "2026-11-04T20:00:00Z"})
        already = frozenset({first.elsewhere[0].item.mark_key})
        again = await self._catalogue({"first_aired": "2026-11-04T20:00:00Z"},
                                      known=already)
        self.assertEqual(again.elsewhere, ())

    async def test_the_leading_source_is_the_one_asked_to_describe_it(self):
        """A title two services both found is described by ONE of them, and
        which one is not a choice this module gets to make again: `source_ids`
        is ordered by the registry, and its leader is the same source the
        tracker's own pick calls a season lookup with. Asking a different one
        here would draw a row from one service and resolve it against another.
        """
        seen = []

        async def _search(asked, settings, media, query):
            if media is not Media.SHOW:
                return SimpleNamespace(hits=[], failed=frozenset())
            return SimpleNamespace(
                hits=[self._hit(also={Source.TRAKT: "trakt-one"})],
                failed=frozenset())

        async def _details(settings, media, source_id, _):
            seen.append(source_id)
            return {"first_aired": "2026-11-04T20:00:00Z", "title": "Unseen Show"}

        with patch("app.distrakt.search.search_catalogue", new=_search),              patch("app.providers.for_catalogue_search",
                   return_value=[(Source.SIMKL, object())]),              patch("app.providers.get", return_value=SimpleNamespace(
                 detail_port=SimpleNamespace(fetch_details=_details))):
            await calendar_search.catalogue(
                "unseen", settings=self.settings, tz=self.tz, known=frozenset())

        self.assertEqual(seen, ["4242"], "the lookup did not use the leader's id")

    async def test_a_hit_no_source_can_address_is_skipped_rather_than_raising(self):
        """`source_ids` empty means nobody named this title in a way a lookup
        can call back with. There is nothing to describe and nothing to link
        to, so it is dropped — quietly, because it is not an error."""
        async def _search(asked, settings, media, query):
            from app.distrakt.search import MergedSearchHit
            return SimpleNamespace(hits=[MergedSearchHit(
                key=None, season=None, source_ids={}, ids={}, title="Nameless",
                year=None, network="", runtime=None, overview="")],
                failed=frozenset())

        with patch("app.distrakt.search.search_catalogue", new=_search),              patch("app.providers.for_catalogue_search",
                   return_value=[(Source.SIMKL, object())]):
            found = await calendar_search.catalogue(
                "nameless", settings=self.settings, tz=self.tz, known=frozenset())
        self.assertEqual(found.elsewhere, ())

    async def test_a_row_carries_what_the_lookup_described(self):
        """It renders through the same shape a stored row does, so a reader is
        not asked to learn a second kind of result."""
        found = await self._catalogue({
            "first_aired": "2026-11-04T20:00:00Z", "network": "Apple TV",
            "overview": "A description.", "poster": "https://img/p.jpg",
            "genres": ["Drama"], "rating": 8.2, "imdb_rating": 7.9})
        item = found.elsewhere[0].item
        self.assertEqual(item.network, "Apple TV")
        self.assertEqual(item.overview, "A description.")
        self.assertEqual(item.imdb_rating, 7.9)
        self.assertEqual(found.elsewhere[0].endpoint_key, "shows/premieres")


if __name__ == "__main__":
    unittest.main()
