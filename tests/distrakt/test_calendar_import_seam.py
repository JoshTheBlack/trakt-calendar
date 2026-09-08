"""The tracker's monthly import, across the calendar's move to rows.

THE CHEAPEST GUARANTEE IN THE WHOLE STORAGE CHANGE, and the one the plan asked
for by name: import a month and diff the roster. `app/distrakt/calendar_import.py`
reads the calendar through `cache.read_month` and consumes `Item` objects —
`_keyable` reads `item.season`, `calendar_record(item)` reads the rest — so the
import keeps working IF AND ONLY IF read_month goes on returning `list[Item]`
with the same filtering semantics. Nothing else about the tracker was touched by
the storage change, which is exactly why a silent break here would be plausible:
every test in this file would have to be rewritten to notice it, and none of the
calendar's own tests look at the tracker at all.

WHAT IS ASSERTED IS THE ROSTER, not the plumbing. The identities, the seasons and
the ids a month yields are what the tracker files rows under; if those survive,
the import survives.
"""
from __future__ import annotations

import unittest
from datetime import date
from unittest.mock import patch
from zoneinfo import ZoneInfo

from app import db
from app.calendar import cache as calendar_cache
from app.config import Settings
from app.distrakt import calendar_import
from app.endpoints import get_endpoint
from app.providers.base import Media, Record, Source
from tests.support import migrated_db

# 2026-07-15 12:00Z, inside the aligned window the fixtures are stored under.
AIR = 1784131200.0
NEW = get_endpoint("shows/new")
PREMIERES = get_endpoint("shows/premieres")


def _record(source, slug, *, tmdb, season=1, title=None, country="us",
            genres=("drama",), certification="TV-14"):
    return Record(
        source=source, media=Media.SHOW, id=slug,
        ids={"tmdb": tmdb, str(source): slug, f"{source}_slug": slug},
        detail_url=f"https://{source}.test/{slug}", title=title or slug.title(),
        air_ts=AIR, season=season, episode_number=1, episode_label=f"S{season:02d}E01",
        country=country, genres=list(genres), certification=certification,
        network="A Network")


class TheMonthlyImportSurvivesTheStorageChangeTests(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        migrated_db("importseam")
        self.settings = Settings(simkl_public_calendar_enabled=False)

    async def asyncTearDown(self):
        db.close_thread_connection()

    async def _fill(self, endpoint, records):
        await calendar_cache.store_window(
            endpoint.key, calendar_cache.window_start(date(2026, 7, 15)),
            records, 600, 1000, sources=["trakt"], asked=["trakt"])

    async def _import(self, **prefs):
        """The roster `premiere_records` yields for July 2026."""
        from app.distrakt import calendar_import

        # THE TV COLUMNS, because both endpoints the import reads are show
        # calendars. Which stored column answers for which medium is
        # app/calendar/vocab.py's; naming them here rather than passing
        # `genres=` is what makes this test notice if that mapping changes.
        stored = {"tv_genres": "", "tv_countries": "", "show_certifications": "",
                  "movie_genres": "", "movie_countries": "",
                  "movie_certifications": "", "movie_release_countries": "",
                  "movie_release_types": "", "network_filter": [],
                  "filters_paused": False}
        marks = prefs.pop("marks", None)
        stored.update(prefs)
        with patch("app.auth.get_user_prefs", return_value=stored):
            return await calendar_import.premiere_records(
                1, self.settings, 2026, 7, marks)

    async def test_a_month_yields_the_roster_the_tracker_files_rows_under(self):
        await self._fill(NEW, [_record(Source.TRAKT, "severance", tmdb=95396)])
        await self._fill(PREMIERES, [_record(Source.TRAKT, "andor", tmdb=83867, season=2)])
        roster = await self._import()
        self.assertEqual(
            sorted((r["title"], r["season"], r["ids"].get("tmdb")) for r in roster),
            [("Andor", 2, 83867), ("Severance", 1, 95396)])

    async def test_it_still_returns_items_and_not_rows(self):
        """`read_month` is the compatibility seam and it returns `list[Item]`.
        The import reads `item.season` and hands the whole object to
        `calendar_record`, so a row or a dict arriving here would break it in a
        way no calendar test would ever see."""
        from app.providers.base import Item

        await self._fill(NEW, [_record(Source.TRAKT, "severance", tmdb=95396)])
        items, _as_of = await calendar_cache.read_month(
            NEW, self.settings, tz=ZoneInfo("UTC"), year=2026, month=7)
        self.assertTrue(items)
        self.assertTrue(all(isinstance(i, Item) for i in items))
        self.assertEqual(items[0].season, 1)

    async def test_the_importers_own_filters_still_narrow_the_roster(self):
        """The import passes the IMPORTER's genre/country/certification prefs, and
        those are read-time filters over shared rows. A storage change that
        applied them at the fill instead would narrow what every other viewer
        sees — the invariant this whole model exists for."""
        await self._fill(NEW, [
            _record(Source.TRAKT, "us-drama", tmdb=1, country="us", genres=("drama",)),
            _record(Source.TRAKT, "jp-anime", tmdb=2, country="jp", genres=("anime",)),
        ])
        everything = await self._import()
        self.assertEqual({r["ids"]["tmdb"] for r in everything}, {1, 2})

        narrowed = await self._import(tv_countries="us")
        self.assertEqual({r["ids"]["tmdb"] for r in narrowed}, {1})

        # ...and the rows themselves are untouched by either read.
        self.assertEqual(
            await db.fetch_value("SELECT COUNT(*) FROM calendar_airings"), 2)

    async def test_a_mark_in_a_namespace_the_record_cannot_carry_still_excludes(self):
        """THE FAILURE THE RECORD COULD NOT ANSWER. A mark may be stored under any
        spelling a title has ever been known by, and the calendar accepts six of
        them. A RECORD's id map is `collect_ids`, an allowlist built for the ids
        the tracker files rows under, and three of those six are not in it — so
        asking the question of a record was unanswerable however carefully it was
        asked. Measured: a viewer's Nocturne mark reads `lazarus`, which the card
        carries as Simkl's `tvdbslug`; the calendar hid the show and the import
        added it.

        So the mark is applied to the ITEM, at the last moment the whole id map
        exists, and this is the test that fails if it ever moves back.
        """
        record = _record(Source.TRAKT, "nocturne", tmdb=292741)
        record.ids = {**record.ids, "tvdbslug": "lazarus"}
        await self._fill(NEW, [record])
        self.assertEqual({r["title"] for r in await self._import()}, {"Nocturne"})
        kept = await self._import(marks={"lazarus"})
        self.assertEqual(kept, [], "a mark the record cannot carry still has to exclude")

    async def test_a_mark_in_the_stable_key_excludes_too(self):
        """The ordinary case, so the fix above is not the only path that works."""
        await self._fill(NEW, [_record(Source.TRAKT, "nocturne", tmdb=292741)])
        self.assertEqual(await self._import(marks={"show:tmdb:292741"}), [])

    async def test_a_paused_filter_still_narrows_what_is_imported(self):
        """THE ASYMMETRY IS DELIBERATE. Switching filters off is a temporary look
        at a calendar and nothing is written; an import WRITES ROWS, and a row
        does not come back off when the switch does. Importing while paused
        produced 85 rows of titles the viewer filters out, every one of which had
        to be deleted by hand — so the import reads the filters as they are
        STATED rather than as they are currently being applied."""
        await self._fill(NEW, [
            _record(Source.TRAKT, "us-drama", tmdb=1, country="us", genres=("drama",)),
            _record(Source.TRAKT, "jp-anime", tmdb=2, country="jp", genres=("anime",)),
        ])
        narrowed = await self._import(tv_countries="us", filters_paused=True)
        self.assertEqual({r["ids"]["tmdb"] for r in narrowed}, {1})

    async def test_a_title_both_endpoints_list_is_imported_once(self):
        """shows/new and shows/premieres overlap, and the import reads both. The
        de-duplication is the importer's and must survive whatever storage
        hands back."""
        show = _record(Source.TRAKT, "severance", tmdb=95396)
        await self._fill(NEW, [show])
        await self._fill(PREMIERES, [_record(Source.TRAKT, "severance", tmdb=95396)])
        roster = await self._import()
        self.assertEqual(len(roster), 1, [r["title"] for r in roster])

    async def test_an_empty_month_imports_nothing_rather_than_raising(self):
        self.assertEqual(await self._import(), [])

class TheImportReadsTheMarkTheCalendarWroteTests(unittest.IsolatedAsyncioTestCase):
    """A mark travels in exactly one direction — calendar to tracker — and this
    is the whole of that direction. If the import cannot recognise a mark, the
    calendar hides a title and the tracker adds it anyway.

    THE BUG THIS EXISTS FOR: a mark is stored under `Record.mark_key`, which is
    the identity waterfall's answer ("show:tmdb:231801"), and this function used
    to ask only about the raw slug and Trakt id. Measured on a real instance, 78
    of 987 marks were in that spelling and not one of them matched — so two
    shows the viewer had turned away were imported onto their month.
    """

    def test_a_mark_written_under_the_cards_own_key_is_recognised(self):
        rec = {"media": "show", "ids": {"slug": "get-jiro", "simkl": 2347649,
                                        "tmdb": "231801", "tvdb": "444631"}}
        self.assertTrue(calendar_import.matches_not_watching(rec, {"show:tmdb:231801"}))

    def test_the_waterfall_decides_which_id_the_key_uses(self):
        """A title with no TMDB id keys on whatever the waterfall reaches next,
        and the import has to follow it there rather than assuming one space."""
        rec = {"media": "show", "ids": {"slug": "tyrolean-blood", "simkl": 2894015,
                                        "tvdb": "468888"}}
        self.assertTrue(calendar_import.matches_not_watching(rec, {"show:tvdb:468888"}))

    def test_a_mark_spelled_by_the_other_service_still_counts(self):
        """MEASURED, on a real instance. The mark reads `grey-s-anatomy` — Trakt's
        slug — while the card resolved to Simkl's description and carries
        `greys-anatomy`. The calendar hid the show; the import's own narrower copy
        of this rule did not, and added it."""
        rec = {"media": "show", "ids": {
            "slug": "greys-anatomy", "simkl": 3296, "simkl_slug": "greys-anatomy",
            "tmdb": "1416", "imdb": "tt0413573", "tvdb": "73762",
            "tvdbslug": "greys-anatomy", "traktslug": "grey-s-anatomy"}}
        self.assertTrue(calendar_import.matches_not_watching(rec, {"grey-s-anatomy"}))

    def test_a_mark_in_a_namespace_the_card_only_has_from_one_service(self):
        """The second measured case: Nocturne was marked under `lazarus`, which
        appears on the card only as Simkl's `tvdbslug`. A copy that looked at the
        slug and the Trakt id never saw that namespace at all."""
        rec = {"media": "show", "ids": {
            "slug": "nocturne", "simkl": 2089449, "tmdb": "292741",
            "tvdb": "430654", "tvdbslug": "lazarus", "traktslug": "nocturne"}}
        self.assertTrue(calendar_import.matches_not_watching(rec, {"lazarus"}))

    def test_it_asks_the_same_question_the_calendar_asks(self):
        """THE POINT OF THE REPAIR, not just its symptoms. A rule every surface
        has to agree on cannot have two implementations — a copy that asks half
        the question makes a show hidden on one screen and visible on the next,
        which is exactly what happened. Any spelling the grid accepts, this
        accepts."""
        ids = {"slug": "a-show", "simkl_slug": "b-show", "trakt_slug": "c-show",
               "traktslug": "d-show", "tvdbslug": "e-show", "mdlslug": "f-show",
               "tmdb": "999"}
        rec = {"media": "show", "ids": ids}
        for namespace, spelling in ids.items():
            if namespace == "tmdb":
                continue
            with self.subTest(namespace=namespace):
                self.assertTrue(calendar_import.matches_not_watching(rec, {spelling}))

    def test_a_bare_service_number_is_not_a_mark(self):
        """The grid deliberately refuses numeric id namespaces — one real account
        has a mark spelled `1670`, which is a show's slug, and tmdb 1670 is a
        different programme. A false match HIDES something nobody marked, and the
        import must not be laxer than the screen it follows."""
        rec = {"media": "show", "ids": {"slug": "sixteen-seventy", "tmdb": "1670"}}
        self.assertFalse(calendar_import.matches_not_watching(rec, {"1670"}))

    def test_a_mark_in_an_older_spelling_still_counts(self):
        """Most marks on a long-running instance predate the waterfall. Dropping
        them would bring hundreds of turned-away shows back at once."""
        rec = {"media": "show", "ids": {"slug": "get-jiro", "tmdb": "231801"}}
        self.assertTrue(calendar_import.matches_not_watching(rec, {"get-jiro"}))

    def test_an_unmarked_title_is_not_matched_by_any_of_them(self):
        rec = {"media": "show", "ids": {"slug": "get-jiro", "tmdb": "231801"}}
        self.assertFalse(calendar_import.matches_not_watching(rec, {"show:tmdb:999999"}))
        self.assertFalse(calendar_import.matches_not_watching(rec, {"some-other-show"}))

    def test_a_title_with_nothing_to_key_on_is_not_matched_by_accident(self):
        """The waterfall answers None, and a None key must not be compared as the
        string "None" — which would match a mark of that name and would be the
        kind of bug nobody finds."""
        self.assertFalse(calendar_import.matches_not_watching(
            {"media": "show", "ids": {}}, {"None", "show:None:None", ""}))

