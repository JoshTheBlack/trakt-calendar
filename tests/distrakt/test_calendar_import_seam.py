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

        stored = {"genres": "", "countries": "", "show_certifications": ""}
        stored.update(prefs)
        with patch("app.auth.get_user_prefs", return_value=stored):
            return await calendar_import.premiere_records(
                1, self.settings, 2026, 7)

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

        narrowed = await self._import(countries="us")
        self.assertEqual({r["ids"]["tmdb"] for r in narrowed}, {1})

        # ...and the rows themselves are untouched by either read.
        self.assertEqual(
            await db.fetch_value("SELECT COUNT(*) FROM calendar_airings"), 2)

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
