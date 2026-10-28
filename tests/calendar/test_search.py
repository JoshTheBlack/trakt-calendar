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

import dataclasses
import unittest
from datetime import date
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from app import db, providers
from app.calendar import cache as calendar_cache, entries as calendar_entries
from app.calendar import search as calendar_search
from app.metadata import typed_search
from app.providers import title_search
from app.config import Settings
from app.endpoints import get_endpoint
from app.providers.base import SourceUnavailable, Media, Record, Source
from app.sources import prefs as source_prefs
from tests.support import migrated_db

def publishing(**ports):
    """A stand-in provider that publishes every calendar, so calendar/search.py's
    rule that a service writes only to a calendar it publishes lets it write."""
    return SimpleNamespace(calendar_port=object(),
                           capabilities=SimpleNamespace(answers=lambda _key: True), **ports)


SHOWS = "shows/premieres"
# 2026-07-08 20:00Z, mid-window and mid-month so no test is about a boundary.
AIR = 1783540800.0

NO_FILTERS = {
    "tv_genres": "", "tv_countries": "", "movie_genres": "", "movie_countries": "",
    "show_certifications": "", "movie_certifications": "",
    "movie_release_countries": "", "movie_release_types": "", "network_filter": [],
    "filters_paused": False,
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
        # The viewer whose filters apply. Both halves of a search read these,
        # which is the point -- the catalogue half used to read none of them.
        self.prefs = dict(NO_FILTERS)
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

    async def test_the_jump_url_anchors_on_the_card_and_not_on_the_day(self):
        """The month is what the calendar route reads, `highlight=` is what the
        route marks and ships the day for, and `#jump-target` is what the
        BROWSER scrolls to with no script involved.

        ANCHORED ON THE CARD BECAUSE THE DAY IS TOO COARSE. `#day-` was not
        wrong, it just landed at the top of the right day — which on a day
        holding thirty titles is not where the one somebody searched for is.
        """
        await self.store(_record("Severance"))
        url = (await self.find("severance")).airings[0].url
        self.assertIn("year=2026", url)
        self.assertIn("month=7", url)
        self.assertIn("endpoint=shows%2Fpremieres", url)
        self.assertIn("highlight=", url)
        self.assertTrue(url.endswith("#jump-target"), url)
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
        prefs = dict(NO_FILTERS, tv_countries="-th")
        self.assertEqual((await self.find("severance", prefs=prefs)).airings, ())
        # ...and the same search without that filter does find it, so the test is
        # about the filter rather than about the title being unfindable.
        self.assertEqual(len((await self.find("severance")).airings), 1)

    async def test_a_title_the_genre_filter_removes_is_not_offered(self):
        await self.store(_record("Severance"))
        prefs = dict(NO_FILTERS, tv_genres="-drama")
        self.assertEqual((await self.find("severance", prefs=prefs)).airings, ())

    async def test_a_title_the_network_filter_removes_is_not_offered(self):
        """THE FILTER THE SEARCH USED TO SKIP. The network lists judge a
        rendered card, and only the month read applied them — so a search
        offered a title on a network this viewer filters out, and its jump
        opened a day that could not draw it."""
        await self.store(dataclasses.replace(_record("Severance"), network="Apple TV"))
        prefs = dict(NO_FILTERS, network_filter=["-Apple TV"])
        self.assertEqual((await self.find("severance", prefs=prefs)).airings, ())
        self.assertEqual(len((await self.find("severance")).airings), 1)

    async def test_a_title_the_operators_network_list_removes_is_not_offered(self):
        await self.store(dataclasses.replace(_record("Severance"), network="Apple TV"))
        self.settings = dataclasses.replace(self.settings, network_filter=["-Apple TV"])
        self.assertEqual((await self.find("severance")).airings, ())

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
        self.assertEqual(typed_search.like_needle("100%"), r"%100\%%")
        self.assertEqual(typed_search.like_needle("a_b"), r"%a\_b%")

    def test_the_escape_character_cannot_smuggle_itself(self):
        self.assertEqual(typed_search.like_needle("a\\b"), r"%a\\b%")

    def test_it_folds_the_needle_the_same_way_a_stored_title_was_folded(self):
        self.assertEqual(typed_search.like_needle("Pokémon"), "%pokemon%")


class WhichGroupsAQueryLoadsTests(SearchTestCase):
    """How the stored half finds its groups changed shape for speed — matches
    first, through the fold index — and WHAT it finds must not have: every
    matching group when there are fewer than the limit, and, when there are
    more, the same earliest-airing ones the endpoint-first read always chose."""

    async def titles_of(self, keys, endpoint=SHOWS):
        records = await calendar_entries.read_groups(endpoint, keys)
        return sorted({r.title for r in records})

    async def test_fewer_matches_than_the_limit_are_every_match(self):
        await self.store(_record("Severance"), _record("Severed Ties"),
                         _record("Unsevered"), _record("Andor"))
        keys = await calendar_entries.groups_matching(SHOWS, "sever", 40)
        self.assertEqual(await self.titles_of(keys),
                         ["Severance", "Severed Ties", "Unsevered"])

    async def test_a_broad_query_loads_the_earliest_airing_groups_as_it_always_has(self):
        """Stored latest-airing first, so the titles' own order is the opposite
        of their air dates: a matches-first read cut at the limit would keep A
        and B, and the endpoint-first read this search has always made keeps C
        and B."""
        await self.store(_record("Sev A", air_ts=AIR + 86400),
                         _record("Sev B", air_ts=AIR),
                         _record("Sev C", air_ts=AIR - 86400))
        keys = await calendar_entries.groups_matching(SHOWS, "sev", 2)
        self.assertEqual(await self.titles_of(keys), ["Sev B", "Sev C"])

    async def test_a_title_airing_only_on_another_calendar_is_not_a_match(self):
        await self.store(_record("Severance"), endpoint="shows")
        self.assertEqual(await calendar_entries.groups_matching(SHOWS, "severance", 40), [])
        self.assertEqual(len(await calendar_entries.groups_matching("shows", "severance", 40)), 1)

    async def test_a_group_is_read_from_the_calendar_asked_about_only(self):
        await self.store(_record("Severance"))
        await self.store(_record("Severance", air_ts=AIR + 86400), endpoint="shows")
        keys = await calendar_entries.groups_matching(SHOWS, "severance", 40)
        records = await calendar_entries.read_groups(SHOWS, keys)
        self.assertEqual([r.air_ts for r in records], [AIR])

    async def plan(self, sql, params):
        rows = await db.run(lambda conn: conn.execute("EXPLAIN QUERY PLAN " + sql,
                                                      params).fetchall())
        return " | ".join(str(row["detail"]) for row in rows)

    async def test_the_matches_are_found_in_the_fold_index_before_any_airing_is_read(self):
        """THE SHAPE IS THE SPEED. Measured on a copy of a real instance, the
        planner left to itself walked the endpoint's every airing — 38-112ms a
        keystroke for a query naming one title — and this order is 2ms."""
        plan = await self.plan(calendar_entries._SEARCH_SQL, (SHOWS, "%x%", 40))
        self.assertIn("ix_title_metadata_fold", plan)
        self.assertNotIn("ix_calendar_airings_month", plan)
        self.assertLess(plan.index("ix_title_metadata_fold"),
                        plan.index("sqlite_autoindex_calendar_airings_1"))

    async def test_a_group_is_read_through_its_key_and_not_the_endpoints_month(self):
        sql = calendar_entries._BY_KEY_SQL.replace("__KEYS__", "?, ?")
        plan = await self.plan(sql, (SHOWS, "a", "b"))
        self.assertIn("ix_calendar_airings_key", plan)
        self.assertNotIn("ix_calendar_airings_month", plan)


class WhichGroupsABigCalendarLoadsTests(WhichGroupsAQueryLoadsTests):
    """The same answers when the calendar is big enough to be read matches-first.
    A test calendar holds a handful of airings, so it is SMALL and read
    endpoint-first; lowering the threshold is what puts the other read, and its
    fall back to the endpoint-first read for a broad query, under the same
    assertions."""

    async def asyncSetUp(self):
        await super().asyncSetUp()
        patcher = patch.object(calendar_entries, "_SMALL_CALENDAR", 1)
        patcher.start()
        self.addCleanup(patcher.stop)

    async def test_a_big_calendar_asks_the_fold_index_first(self):
        await self.store(_record("Severance"))
        with patch.object(calendar_entries.db, "fetch_all",
                          wraps=calendar_entries.db.fetch_all) as spy:
            await calendar_entries.groups_matching(SHOWS, "severance", 40)
        self.assertEqual([c.args[0] for c in spy.call_args_list],
                         [calendar_entries._SEARCH_SQL])


class TheCatalogueHalfIsADifferentPromiseTests(SearchTestCase):
    """A catalogue result's season PICKER (calendar/search.py's `picker`) — where
    a title is described, its seasons dated, the viewer's filters read and what
    the calendar already draws marked. Its jump links to a MONTH, because
    visiting a month FILLS it — and whether a card then appears depends on
    whether any source lists that title on that calendar."""

    def _hit(self, title="Unseen Show", simkl_id="4242", also=None, media=Media.SHOW):
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
        from app.providers.title_search import MergedSearchHit

        sources = {Source.SIMKL: simkl_id}
        sources.update(also or {})
        return MergedSearchHit(key=None, season=None, source_ids=sources,
                               ids={"simkl": simkl_id, "tmdb": 777}, title=title,
                               year=2026, network="", runtime=None, overview="",
                               media=media)

    def _port(self, described, seasons=None):
        """A detail port that answers all three halves of what a row needs.

        `seasons=None` MEANS "THIS SOURCE WILL NOT LIST THEM", which is a real
        state and the one most of these tests want: it makes the hit fall back
        to the show's own first-air date, so assertions about placement stay
        about placement.

        `records_for` BUILDS REAL RECORDS, because the real one does. A provider
        answers this by running the payload through its OWN calendar record
        builder — that is the entire point of the port — so a double that
        returned some other shape, or that skipped the fields a filter reads,
        would test a pipeline this app does not have. This is the lesson the
        merged-hit double taught once already.

        `enriched=True` BECAUSE THIS STANDS IN FOR A SOURCE WHOSE CALENDAR ROWS
        ARE COMPLETE. Simkl's are not, and its records come back needing the
        enrichment pass; saying so here would send these tests at the network,
        which the autouse guard in tests/conftest.py would rightly stop.
        """
        async def _seasons(settings, source_id, media):
            if seasons is None:
                raise SourceUnavailable("no season list")
            return SimpleNamespace(seasons=list(seasons))

        async def _records(settings, source_id, media, moments):
            from app.providers.base import Record

            out = []
            for season, when in moments:
                bare = len(str(when)) <= 10
                text = f"{when}T00:00:00+00:00" if bare else str(when).replace("Z", "+00:00")
                moment = datetime.fromisoformat(text)
                out.append(Record(
                    source=Source.SIMKL, media=media, id=str(source_id),
                    ids={"simkl": str(source_id), "tmdb": 777},
                    detail_url="", title=described.get("title") or "Unseen Show",
                    air_ts=moment.timestamp(), date_only=bare, season=season,
                    year=described.get("year") or 2026,
                    network=str(described.get("network") or ""),
                    country=str(described.get("country") or ""),
                    certification=str(described.get("certification") or ""),
                    genres=[str(g).lower().replace(" ", "-")
                            for g in (described.get("genres") or [])],
                    poster=str(described.get("poster") or ""),
                    overview=str(described.get("overview") or ""),
                    runtime=described.get("runtime"),
                    status=str(described.get("status") or ""),
                    rating=described.get("rating"),
                    imdb_rating=described.get("imdb_rating"),
                    enriched=True))
            return out

        return SimpleNamespace(fetch_details=AsyncMock(return_value=described),
                               fetch_seasons=_seasons, records_for=_records)

    async def _picker(self, described, seasons=None, *, hit=None, media=Media.SHOW,
                      port=None):
        """The season picker for one catalogue result — where a title is
        described now that the search lists titles alone."""
        with patch("app.providers.get", return_value=publishing(
                detail_port=port or self._port(described, seasons))):
            return await calendar_search.picker(
                self.settings, hit or self._hit(), media, prefs=self.prefs, tz=self.tz)

    def _offered(self, chosen):
        """The seasons a viewer can pick: neither refused nor already drawn."""
        return [option for option in chosen.options if option.pickable]

    async def test_a_season_older_than_the_epoch_does_not_crash_the_picker(self):
        """THE CRASH, EXACTLY. A catalogue answers for whatever a service knows,
        and services know about television older than 1970 — so a season list
        came back with a premiere in 1969, and converting it with
        `datetime.fromtimestamp` raised OSError [Errno 22] on Windows, where
        that call is handed to the platform C library. The whole search 500'd
        because one row was old.

        WHICH YEARS THIS APP CAN DISPLAY MUST NOT DEPEND ON ITS OPERATING
        SYSTEM, which is what `providers.base.epoch_moment` is for.
        """
        chosen = await self._picker(
            {"first_aired": "1969-06-01T00:00:00Z", "title": "Old Show"},
            seasons=[{"season": 1, "first_aired": "1969-06-01"},
                     {"season": 2, "first_aired": "1970-06-01"}])
        self.assertEqual(sorted(row.year for row in chosen.rows), [1969, 1970])
        self.assertEqual([option.season for option in chosen.options], [1, 2])

    async def test_a_show_older_than_the_epoch_is_placed_in_its_real_month(self):
        """Not merely "does not crash": the row has to land where the title
        actually aired, or refusing would have been the better answer."""
        chosen = await self._picker({"first_aired": "1963-11-23T17:16:00Z"})
        row, = chosen.rows
        self.assertEqual((row.year, row.month, row.day), (1963, 11, "1963-11-23"))

    async def test_a_premiere_with_a_time_keeps_the_day_that_time_falls_on(self):
        """THE OFF-BY-ONE, EXACTLY. Trakt dates a premiere to the moment it airs
        — Half Man season 1 came back as `2026-04-28T20:00:00.000Z` — and this
        used to truncate that to a bare day and re-read it as UTC MIDNIGHT.
        Pushed into a zone behind Greenwich, midnight on the 28th is the 27th,
        so the row offered 27 April for a season airing on the 28th and the link
        landed a day early.
        """
        self.tz = ZoneInfo("America/New_York")
        chosen = await self._picker(
            {"first_aired": "2026-04-28T20:00:00Z"},
            seasons=[{"season": 1, "first_aired": "2026-04-28T20:00:00.000Z"}])
        self.assertEqual(chosen.rows[0].day, "2026-04-28")

    async def test_a_premiere_given_as_a_bare_day_is_not_moved_by_a_zone(self):
        """The other source's shape, and the opposite rule. Simkl's season list
        carries a calendar DAY — its episode reader drops the time deliberately,
        because Simkl states a whole file in one fixed offset — and a day is the
        same day everywhere. Converting it would move it for exactly the viewers
        the fix above was for."""
        for zone in ("America/New_York", "Asia/Tokyo"):
            with self.subTest(zone=zone):
                self.tz = ZoneInfo(zone)
                chosen = await self._picker(
                    {"first_aired": "2026-04-28T00:00:00Z"},
                    seasons=[{"season": 1, "first_aired": "2026-04-28"}])
                self.assertEqual(chosen.rows[0].day, "2026-04-28")

    async def test_opening_a_title_writes_nothing_new_to_the_calendar(self):
        """A picker offers; it does not decide. Following it — the jump — is
        the act that names what is written."""
        wrote = []

        async def _store(endpoint_key, records, *, now, stale_after):
            wrote.append(endpoint_key)
            return len(records)

        with patch("app.calendar.entries.store_loose_airings", _store):
            chosen = await self._picker(
                {"first_aired": "2026-11-04T20:00:00Z", "title": "Unseen Show"})
        self.assertEqual(len(chosen.options), 1)
        self.assertEqual(wrote, [], "opening a title wrote to the shared calendar")

    async def test_a_show_is_offered_once_per_season_premiere(self):
        """WHAT THE SHOW-SHAPED ROW COULD NOT DO. A long-running title has one
        first-air date and many premieres, so keying the link on the show sent
        every search for a recent season to the month the show began — years
        before the season anybody was looking for. EVERY dated season is
        offered: the picker is a list to choose from, not a search's budget."""
        chosen = await self._picker(
            {"first_aired": "2010-01-01T00:00:00Z", "title": "Long Runner"},
            seasons=[{"season": n, "first_aired": f"20{10 + n:02d}-05-01"}
                     for n in range(1, 9)])
        self.assertEqual([option.season for option in chosen.options], list(range(1, 9)))
        self.assertEqual(sorted((row.year, row.month) for row in chosen.rows)[0], (2011, 5))
        # The latest aired is the suggestion, as in every picker.
        self.assertEqual([o.season for o in chosen.options if o.suggested], [8])

    async def test_a_season_with_no_date_is_not_offered(self):
        """The offer is "go to where this should be", and an undated season has
        no where."""
        chosen = await self._picker(
            {"first_aired": "2020-01-01T00:00:00Z"},
            seasons=[{"season": 1, "first_aired": "2020-01-01"},
                     {"season": 2, "first_aired": ""},
                     {"season": 3, "first_aired": None}])
        self.assertEqual([option.season for option in chosen.options], [1])

    async def test_a_source_that_will_not_list_seasons_still_offers_the_show(self):
        """Losing the season list costs precision, not the result. A reader who
        searched a title by name should not get nothing because one lookup of
        two was refused."""
        chosen = await self._picker({"first_aired": "2026-11-04T20:00:00Z"}, seasons=None)
        row, = chosen.rows
        self.assertEqual((row.year, row.month), (2026, 11))

    async def _stored_airing(self, source: Source, source_id: str, season: int = 1,
                             **fields):
        """One source's premiere of a season of the title these tests open
        (tmdb 777), as the calendar holds it — `fields` being what that service
        said about it, which is what the calendar's filters read."""
        await calendar_entries.store_loose_airings("shows/premieres", [Record(
            source=source, media=Media.SHOW, id=source_id,
            ids={str(source): source_id, "tmdb": 777}, detail_url="", title="Unseen Show",
            air_ts=datetime(2020, 1, 1, tzinfo=timezone.utc).timestamp(),
            season=season, enriched=True, **fields)], now=0, stale_after=10**10)

    async def _open_known_season_one(self):
        # SIMKL'S CALENDAR SWITCHED ON: the stored airing is Simkl's, and the
        # picker asks whether this instance's calendar would draw it.
        self.settings = Settings(simkl_public_calendar_enabled=True)
        chosen = await self._picker(
            {"first_aired": "2020-01-01T00:00:00Z"},
            seasons=[{"season": 1, "first_aired": "2020-01-01"},
                     {"season": 2, "first_aired": "2021-03-04"}])
        return chosen, await calendar_entries.sources_airing(
            "shows/premieres", "show:tmdb:777", 1)

    async def test_a_season_already_on_the_calendar_links_to_its_day_alone(self):
        """THE TRAP IN KEYING THIS ON THE TITLE. `mark_key` is the title's
        identity and carries no season, so matching on it alone would mark every
        season of a show the calendar happens to hold one airing of — which is
        precisely the title whose other seasons are the useful answer."""
        await self._stored_airing(Source.SIMKL, "4242")
        chosen, _sources = await self._open_known_season_one()
        one, two = chosen.options
        self.assertTrue(one.href.startswith("/calendar?year=2020&month=1"), one.href)
        self.assertIn("on your calendar", one.notes)
        self.assertEqual((two.href, two.pickable), ("", True))

    async def test_a_stored_season_the_filters_hide_is_refused_not_linked(self):
        """A link to its day would open a day that cannot draw it — measured in
        the owner's browser: the month opened with nothing to find."""
        self.settings = Settings(simkl_public_calendar_enabled=True)
        await self._stored_airing(Source.SIMKL, "4242", genres=["reality"])
        self.prefs = {**self.prefs, "tv_genres": "-reality"}
        chosen = await self._picker({"first_aired": "2020-01-01T00:00:00Z",
                                     "genres": ["Reality"]},
                                    seasons=[{"season": 1, "first_aired": "2020-01-01"}])
        option, = chosen.options
        self.assertEqual(option.href, "")
        self.assertIn("your filters hide it", option.notes)
        self.assertFalse(option.pickable)

    async def test_a_stored_season_is_judged_by_the_record_the_calendar_draws(self):
        """MEASURED on the owner's calendar: Sever (2026) is stored from Simkl with
        no country, and their US-only country filter hides that card — while
        TMDB's description says US. Judged by the description, the picker linked
        to a day that drew nothing."""
        self.settings = Settings(simkl_public_calendar_enabled=True)
        await self._stored_airing(Source.SIMKL, "4242")
        self.prefs = {**self.prefs, "tv_countries": "us"}
        chosen = await self._picker({"first_aired": "2020-01-01T00:00:00Z", "country": "US"},
                                    seasons=[{"season": 1, "first_aired": "2020-01-01"}])
        option, = chosen.options
        self.assertEqual(option.href, "")
        self.assertIn("your filters hide it", option.notes)

    async def test_a_season_not_held_is_judged_by_the_title_the_calendar_holds(self):
        """MEASURED on the owner's calendar: TMDB names Yellowjackets' network
        "Showtime", both services' listings "Paramount+ with Showtime". With that
        network filtered out, the season on the calendar was refused and the ones
        not on it were offered — judged by TMDB's word rather than by the title
        the calendar holds, whose card a written season would be."""
        self.settings = Settings(simkl_public_calendar_enabled=True)
        await self._stored_airing(Source.SIMKL, "4242", network="Paramount+ with Showtime")
        described = {"first_aired": "2020-01-01T00:00:00Z", "network": "Showtime"}
        seasons = [{"season": 1, "first_aired": "2020-01-01"},
                   {"season": 2, "first_aired": "2021-03-04"}]
        self.prefs = {**self.prefs, "network_filter": ["-Paramount+ with Showtime"]}
        one, two = (await self._picker(described, seasons=seasons)).options
        for option in (one, two):
            with self.subTest(season=option.season):
                self.assertFalse(option.pickable)
                self.assertIn("your filters hide it", option.notes)
        # ...and without the filter the season not held is offered, so the test
        # is about the filter.
        self.prefs = {**self.prefs, "network_filter": []}
        _one, two = (await self._picker(described, seasons=seasons)).options
        self.assertTrue(two.pickable)

    async def test_a_known_season_another_service_airs_learns_this_ones_airing(self):
        """THE SUPPRESSION WAS KEYED ON TITLE AND SEASON, NOT SOURCE. MEASURED:
        Groomed was searched for, answered by Trakt, and dropped as a duplicate
        of the Simkl airing the calendar held — so the only write that could
        have taught the calendar Trakt's airing never happened. The season is
        still not offered twice; it just gains the source it was missing."""
        await self._stored_airing(Source.TRAKT, "326227")
        chosen, sources = await self._open_known_season_one()
        self.assertTrue(chosen.options[0].href)
        self.assertEqual(sources, {"trakt", "simkl"})

    async def test_a_known_season_this_service_already_airs_writes_nothing(self):
        await self._stored_airing(Source.SIMKL, "4242")
        wrote = []

        async def _store(endpoint_key, records, *, now, stale_after):
            wrote.append(endpoint_key)
            return len(records)

        with patch("app.calendar.entries.store_loose_airings", _store):
            chosen, _sources = await self._open_known_season_one()
        self.assertTrue(chosen.options[0].href)
        self.assertEqual(wrote, [])

    async def test_a_film_is_never_asked_for_seasons(self):
        """Films have none, and asking would spend a call to be told so. Its one
        choice is its release."""
        asked = []

        async def _seasons(settings, source_id, media):
            asked.append(source_id)
            return SimpleNamespace(seasons=[])

        port = SimpleNamespace(
            fetch_details=AsyncMock(return_value={"first_aired": "2026-11-04T20:00:00Z"}),
            fetch_seasons=_seasons,
            records_for=self._port({"first_aired": "2026-11-04T20:00:00Z"}).records_for)
        chosen = await self._picker({}, hit=self._hit(title="A Film", media=Media.MOVIE),
                                    media=Media.MOVIE, port=port)
        option, = chosen.options
        self.assertEqual((option.label, option.season), ("Release", None))
        self.assertEqual(asked, [], "a film was asked for its seasons")

    async def test_a_country_this_viewer_excludes_is_refused_with_the_reason(self):
        """THE FILTER THAT LOOKED APPLIED AND WAS NOT.

        Filtering was not enough on its own: the record it filtered carried no
        `country`, because both packages' detail projections dropped the field
        their own payloads return. A record with no country cannot be excluded BY
        country, so a viewer excluding a dozen of them was still offered every one
        — and the month such a row linked to could never draw the title, which is
        exactly the report: "The Traitors S02, Prime Video, and it isn't on that
        page". It is listed and REFUSED, saying so, so "not there" is never
        confused with "nothing found".
        """
        self.prefs = {**self.prefs, "tv_countries": "-in"}
        chosen = await self._picker({"first_aired": "2026-08-12T00:00:00Z", "country": "IN"})
        option, = chosen.options
        self.assertFalse(option.pickable)
        self.assertIn("your filters hide it", option.notes)

    async def test_a_network_this_viewer_excludes_is_refused_with_the_reason(self):
        """Both network lists judge the card the calendar would draw, so a season
        on a network the viewer filters out is listed and refused like any other
        filtered season — not offered as somewhere the month cannot show it."""
        self.prefs = {**self.prefs, "network_filter": ["-Apple TV"]}
        chosen = await self._picker({"first_aired": "2026-08-12T00:00:00Z",
                                     "network": "Apple TV"})
        option, = chosen.options
        self.assertFalse(option.pickable)
        self.assertIn("your filters hide it", option.notes)

    async def test_a_country_this_viewer_keeps_is_still_offered(self):
        self.prefs = {**self.prefs, "tv_countries": "-in"}
        chosen = await self._picker({"first_aired": "2026-08-12T00:00:00Z", "country": "US"})
        self.assertEqual(len(self._offered(chosen)), 1)

    async def test_the_row_carries_the_country_it_was_filtered_on(self):
        """It is the field the filter reads — one value, so a reader cannot be
        shown a country the filter did not use."""
        chosen = await self._picker({"first_aired": "2026-08-12T00:00:00Z", "country": "IN"})
        self.assertEqual(chosen.rows[0].item.country, "IN")

    async def test_a_title_this_viewer_filters_out_is_refused(self):
        """THE ONE SURFACE THAT USED TO OFFER SOMEWHERE YOU CANNOT GET TO. A
        title excluded by genre was offered, and the month it linked to could
        never draw it."""
        self.prefs = {**self.prefs, "tv_genres": "-reality"}
        chosen = await self._picker({"first_aired": "2026-11-04T20:00:00Z",
                                     "genres": ["Reality"]})
        self.assertEqual(self._offered(chosen), [])

    async def test_a_title_that_passes_the_filters_is_still_offered(self):
        """The other side of the same rule, so the filter cannot pass by
        rejecting everything."""
        self.prefs = {**self.prefs, "tv_genres": "-reality"}
        chosen = await self._picker({"first_aired": "2026-11-04T20:00:00Z",
                                     "genres": ["Drama"]})
        self.assertEqual(len(self._offered(chosen)), 1)

    async def test_a_film_is_asked_about_the_film_vocabulary(self):
        """Certifications are two different vocabularies — TV Parental
        Guidelines against MPA ratings — so a film checked against the SHOW
        spec passes anything. Which one applies follows the media, as it does
        everywhere else that asks."""
        self.prefs = {**self.prefs, "movie_certifications": "-r",
                      "show_certifications": "-tv-ma"}
        chosen = await self._picker(
            {"first_aired": "2026-11-04T20:00:00Z", "certification": "R"},
            hit=self._hit(title="Unseen Film", media=Media.MOVIE), media=Media.MOVIE)
        self.assertEqual(self._offered(chosen), [])

    async def test_a_title_with_no_date_is_not_offered_at_all(self):
        """The whole offer is "go to where this should be", and a title nobody
        can date has no where. Refusing beats sending a reader to a guess."""
        chosen = await self._picker({"title": "Unseen Show", "first_aired": ""})
        self.assertEqual(chosen.options, [])

    async def test_an_unreadable_date_is_refused_rather_than_guessed(self):
        chosen = await self._picker({"first_aired": "sometime next year"})
        self.assertEqual(chosen.options, [])

    async def test_the_leading_source_is_the_one_asked_to_describe_it(self):
        """A title two services both found is described by ONE of them, and
        which one is not a choice this module gets to make again: `source_ids`
        is ordered by the registry, and its leader is the same source the
        tracker's own pick calls a season lookup with. Asking a different one
        here would draw a row from one service and resolve it against another.
        """
        seen = []

        async def _details(settings, media, source_id, _):
            seen.append(source_id)
            return {"first_aired": "2026-11-04T20:00:00Z", "title": "Unseen Show"}

        port = SimpleNamespace(fetch_details=_details,
                               fetch_seasons=AsyncMock(side_effect=SourceUnavailable("no")),
                               records_for=AsyncMock(return_value=[]))
        await self._picker({}, hit=self._hit(also={Source.TRAKT: "trakt-one"}), port=port)
        self.assertEqual(seen, ["4242"], "the lookup did not use the leader's id")

    async def test_a_hit_no_source_can_address_offers_nothing_rather_than_raising(self):
        """`source_ids` empty means nobody named this title in a way a lookup
        can call back with. There is nothing to describe and nowhere to go."""
        from app.providers.title_search import MergedSearchHit
        nameless = MergedSearchHit(key=None, season=None, source_ids={}, ids={},
                                   title="Nameless", year=None, network="", runtime=None,
                                   overview="", media=Media.SHOW)
        chosen = await self._picker({}, hit=nameless)
        self.assertEqual(chosen.options, [])

    async def test_a_row_carries_what_the_lookup_described(self):
        """It renders through the same shape a stored row does."""
        chosen = await self._picker({
            "first_aired": "2026-11-04T20:00:00Z", "network": "Apple TV",
            "overview": "A description.", "poster": "https://img/p.jpg",
            "genres": ["Drama"], "rating": 8.2, "imdb_rating": 7.9})
        item = chosen.rows[0].item
        self.assertEqual((item.network, item.overview, item.imdb_rating),
                         ("Apple TV", "A description.", 7.9))
        self.assertEqual(chosen.rows[0].endpoint_key, "shows/premieres")


class TheCatalogueHalfListsTitlesTests(SearchTestCase):
    """The search's catalogue half: the same rows every box draws — every title
    each asked catalogue answered, both media, an added service's below the
    default's — and nothing described, so a search costs its search answers."""

    _hit = TheCatalogueHalfIsADifferentPromiseTests._hit

    def _led_by(self, source, n, *, also=None, media=Media.SHOW):
        from app.providers.title_search import MergedSearchHit

        ids = {**{source: str(n)}, **(also or {})}
        return MergedSearchHit(key=None, season=None, source_ids=ids,
                               ids={"tmdb": 9000 + n}, title=f"{source} {n}", year=2026,
                               network="", runtime=None, overview="", media=media)

    async def _found(self, shows=(), films=(), ref=title_search.SearchRef(),
                     more_from=frozenset()):
        async def _search(asked, settings, media, query, *, pages=1, cached_only=False):
            hits = list(shows if media is Media.SHOW else films)
            return title_search.SearchMergeResult(hits=hits, failed=frozenset(),
                                                  more_from=more_from)

        cat = {key: providers.SearchCatalogue(key, label) for key, label in (
            ("tmdb", "TMDB"), ("trakt", "Trakt"), ("simkl", "Simkl"))}
        plan = providers.SearchPlan(
            asked=[providers.AskedCatalogue(Source.TMDB, object(), cat["tmdb"]),
                   providers.AskedCatalogue(Source.TRAKT, object(), cat["trakt"])],
            offered=(cat["simkl"],), unconfigured=(), order=tuple(cat.values()))
        get = AsyncMock(side_effect=AssertionError("a search described a title"))
        with patch("app.providers.title_search.search_catalogue", new=_search), \
             patch("app.providers.for_search", return_value=plan), \
             patch("app.providers.get", side_effect=get):
            return await calendar_search.catalogue(
                "unseen", settings=self.settings, ref=ref,
                media=(Media.SHOW, Media.MOVIE))

    async def test_every_title_is_listed_and_nothing_is_described(self):
        found = await self._found(shows=[self._led_by(Source.TMDB, n) for n in range(20)],
                                  films=[self._led_by(Source.TMDB, 99, media=Media.MOVIE)])
        self.assertEqual(len(found.found), 21)
        self.assertEqual(found.found[-1].media, "movie")

    async def test_each_row_names_itself_by_reference(self):
        found = await self._found(shows=[self._led_by(Source.TMDB, 1)],
                                  ref=title_search.SearchRef(("trakt",), (("tmdb", 2),)))
        self.assertEqual(found.found[0].ref, {"q": "unseen", "media": "show",
                                              "also": "trakt", "pages": "tmdb:2", "i": "0"})

    async def test_an_added_services_rows_come_after_the_defaults_across_both_media(self):
        found = await self._found(
            shows=[self._led_by(Source.TMDB, 1), self._led_by(Source.TRAKT, 2)],
            films=[self._led_by(Source.TMDB, 3, media=Media.MOVIE)],
            ref=title_search.SearchRef(("trakt",)))
        self.assertEqual([row.hit.title for row in found.found],
                         ["tmdb 1", "tmdb 3", "trakt 2"])

    async def test_its_controls_are_every_boxs(self):
        found = await self._found(shows=[self._led_by(Source.TMDB, 1)],
                                  ref=title_search.SearchRef(("trakt",)),
                                  more_from=frozenset({"tmdb"}))
        self.assertEqual([b["label"] for b in found.controls["buttons"]],
                         ["More from TMDB", "Also search Simkl"])

    async def test_one_calendar_searches_its_own_medium_alone(self):
        found = await self._found(shows=[self._led_by(Source.TMDB, 1)],
                                  films=[self._led_by(Source.TMDB, 2, media=Media.MOVIE)])
        both = len(found.found)
        async def _search(asked, settings, media, query, *, pages=1, cached_only=False):
            return title_search.SearchMergeResult(
                hits=[self._led_by(Source.TMDB, 1, media=media)], failed=frozenset())
        with patch("app.providers.title_search.search_catalogue", new=_search):
            one = await calendar_search.catalogue("unseen", settings=self.settings,
                                                  media=(Media.SHOW,))
        self.assertEqual((both, [row.media for row in one.found]), (2, ["show"]))

    async def test_a_query_too_short_asks_nobody(self):
        self.assertEqual(await calendar_search.catalogue("u", settings=self.settings),
                         calendar_search.Results())


class TheJumpWritesWhatWasTickedTests(SearchTestCase):
    """calendar/search.py's `jump`: the ticked seasons' airings, written by a
    service that lists the calendar, and the first one's month to land on."""

    _hit = TheCatalogueHalfIsADifferentPromiseTests._hit
    _port = TheCatalogueHalfIsADifferentPromiseTests._port

    async def _jump(self, seasons, *, provider=None, hit=None):
        provider = provider or publishing(detail_port=self._port(
            {"first_aired": "2020-01-01T00:00:00Z"},
            seasons=[{"season": 1, "first_aired": "2020-01-01"},
                     {"season": 2, "first_aired": "2021-03-04"}]))
        with patch("app.providers.get", return_value=provider):
            return await calendar_search.jump(self.settings, hit or self._hit(), Media.SHOW,
                                              seasons, tz=self.tz, now=db.now())

    async def test_it_writes_each_ticked_season_and_lands_on_the_first(self):
        where = await self._jump([2, 1])
        self.assertTrue(where.startswith("/calendar?year=2020&month=1"), where)
        # The card and its DAY: a card past the days the page draws inline does
        # not exist when the browser looks for #jump-target, so a mark alone
        # landed at the top of the month.
        self.assertIn("&day=2020-01-01", where)
        self.assertTrue(where.endswith("#jump-target"), where)
        self.assertEqual(await calendar_entries.sources_airing(
            "shows/premieres", "show:tmdb:777", 2), {"simkl"})

    async def test_a_catalogue_with_no_calendar_writes_nothing_and_still_names_the_card(self):
        """The month may draw the title from a service that lists it there, under
        the identity this description keys, so the landing names that card;
        naming one the month does not draw marks nothing."""
        silent = SimpleNamespace(detail_port=self._port(
            {"first_aired": "2020-01-01T00:00:00Z"},
            seasons=[{"season": 1, "first_aired": "2020-01-01"}]),
            calendar_port=None, capabilities=SimpleNamespace(answers=lambda _key: False))
        where = await self._jump([1], provider=silent)
        self.assertEqual(where, "/calendar?year=2020&month=1&endpoint=shows%2Fpremieres"
                                "&highlight=show%3Atmdb%3A777&day=2020-01-01#jump-target")
        self.assertEqual(await calendar_entries.sources_airing(
            "shows/premieres", "show:tmdb:777", 1), set())

    async def test_nothing_ticked_lands_nowhere(self):
        self.assertEqual(await self._jump([]), "")


class AskingTheServiceACardIsMissingTests(SearchTestCase):
    """A card only one service's calendar draws, opened: the other service is
    asked once, by shared id, and its own premiere stored if it lists the title.

    MEASURED, the reason it is on the click and not a schedule: 17,205 of 18,342
    stored titles on the owner's instance are one service's, most of them
    genuinely so, and asking about all of them would spend thousands of lookups
    a day to make a handful of cards two-sided."""

    KEY = "show:tmdb:777"

    async def asyncSetUp(self):
        await super().asyncSetUp()
        # Simkl's calendar in play, so a Trakt-only card has a service to ask.
        self.settings = Settings(simkl_public_calendar_enabled=True)
        # A STORED WEEK holding Trakt's premiere alone, the way a card is drawn:
        # a loose airing is read inside the coverage its week already has.
        await calendar_cache.store_window(
            SHOWS, calendar_cache.window_start(date(2020, 1, 1)),
            [Record(source=Source.TRAKT, media=Media.SHOW, id="326227",
                    ids={"trakt": "326227", "tmdb": 777}, detail_url="",
                    title="Unseen Show",
                    air_ts=datetime(2020, 1, 1, tzinfo=timezone.utc).timestamp(),
                    season=1, episode_number=1, episode_label="S01E01", enriched=True)],
            600, 1000, sources=["trakt", "simkl"], asked=["trakt", "simkl"])

    def _services(self, answer):
        """The Simkl catalogue answering `answer` by shared id (a dict, or an
        exception to raise), and its detail port building its own records."""
        async def _find(settings, media, ids):
            if isinstance(answer, BaseException):
                raise answer
            return answer

        built = TheCatalogueHalfIsADifferentPromiseTests._port(
            self, {"first_aired": "2020-01-01T00:00:00Z"},
            seasons=[{"season": 1, "first_aired": "2020-01-01"}])

        async def _records(settings, source_id, media, moments):
            # EPISODE 1 STATED, as both real builders state it for a premiere
            # (see each provider's records_for), so it pairs with the card's.
            return [dataclasses.replace(r, episode_number=1, episode_label="S01E01")
                    for r in await built.records_for(settings, source_id, media, moments)]

        port = SimpleNamespace(fetch_details=built.fetch_details,
                               fetch_seasons=built.fetch_seasons, records_for=_records)
        return (patch("app.providers.for_shared_id_lookup", return_value=[
                    (Source.SIMKL, SimpleNamespace(find_by_shared_id=_find))]),
                patch("app.providers.get", return_value=publishing(detail_port=port)))

    async def _ask(self, answer, endpoint=SHOWS):
        search_patch, get_patch = self._services(answer)
        with search_patch, get_patch:
            source = await calendar_search.other_service_to_ask(
                self.settings, endpoint_key=endpoint, title_key=self.KEY, season=1)
            if not source:
                return source, False
            return source, await calendar_search.complete_from_other_service(
                self.settings, source=source, endpoint_key=endpoint,
                title_key=self.KEY, season=1, now=1000)

    async def test_a_one_service_premiere_gains_the_others_airing(self):
        source, stored = await self._ask({"simkl": "4242", "tmdb": 777})
        self.assertEqual((source, stored), ("simkl", True))
        self.assertEqual(await calendar_entries.sources_airing(SHOWS, self.KEY, 1),
                         {"trakt", "simkl"})
        again, _ = await self._ask({"simkl": "4242", "tmdb": 777})
        self.assertEqual(again, "", "a card two services draw was asked about again")

    async def test_not_listed_is_an_answer_kept_for_a_week(self):
        _, stored = await self._ask({})
        self.assertFalse(stored)
        again, _ = await self._ask({"simkl": "4242", "tmdb": 777})
        self.assertEqual(again, "", "the same question was asked twice in a week")

    async def test_a_service_that_could_not_be_asked_is_asked_next_time(self):
        _, stored = await self._ask(SourceUnavailable("offline"))
        self.assertFalse(stored)
        again, stored = await self._ask({"simkl": "4242", "tmdb": 777})
        self.assertEqual((again, stored), ("simkl", True))

    async def test_only_a_premiere_or_a_release_can_be_completed(self):
        """An All Episodes or Finales card is some other episode, and a
        service's own record can only be built for a season's premiere."""
        for endpoint in ("shows", "shows/finales"):
            with self.subTest(endpoint=endpoint):
                source, _ = await self._ask({"simkl": "4242", "tmdb": 777}, endpoint)
                self.assertEqual(source, "")

    async def test_the_next_read_of_the_month_draws_the_new_airing(self):
        """The month is held in memory between reads, and a held month that is
        behind is still served while its replacement builds. The redraw that
        answers the panel has to see the airing it just stored."""
        async def read(*, page_load: bool):
            # THE PAGE'S READ MAY FILL and is the one held in memory; a redraw may
            # not, and is served from what the page's read held. One day, inside
            # the stored week, read at a moment that week is fresh, so the page's
            # read has nothing to fetch.
            grouped, _meta = await calendar_cache.assemble_range(
                get_endpoint(SHOWS), self.settings, tz=self.tz,
                start_date=date(2020, 1, 1), end_date=date(2020, 1, 1),
                allow_fetch=page_load, now=1100)
            return [item for day in grouped for item in day["items"]
                    if item.mark_key == self.KEY]

        before = await read(page_load=True)
        self.assertTrue(before)
        self.assertNotIn("simkl", before[0].ids)
        _, stored = await self._ask({"simkl": "4242", "tmdb": 777})
        self.assertTrue(stored)
        after = await read(page_load=False)
        self.assertEqual(len(after), 1, "the two services' premieres drew two cards")
        self.assertEqual(after[0].ids.get("simkl"), "4242",
                         "the read after the write was served the month from before it")


class CompletingASeasonNeverWritesTheShowsStartTests(AskingTheServiceACardIsMissingTests):
    """MEASURED on a live copy: asked to complete a season-6 premiere card, a
    service that could not date season 6 had its SHOW's first-air date (2021)
    written into the shared premieres calendar instead, with no season. A season
    the other service cannot date writes nothing."""

    def _services(self, answer):
        search_patch, _get_patch = super()._services(answer)
        built = TheCatalogueHalfIsADifferentPromiseTests._port(
            self, {"first_aired": "2016-05-01T00:00:00Z"},
            # Seasons the service CAN date, and season 1 is not among them.
            seasons=[{"season": 2, "first_aired": "2021-07-13"}])
        return search_patch, patch("app.providers.get",
                                   return_value=publishing(detail_port=built))

    async def test_a_season_the_other_service_cannot_date_writes_nothing(self):
        source, stored = await self._ask({"simkl": "4242", "tmdb": 777})
        self.assertEqual((source, stored), ("simkl", False))
        self.assertEqual(await calendar_entries.sources_airing(SHOWS, self.KEY, None),
                         {"trakt"}, "the show's own start was written in the season's place")

    # The parent's cases assume the season can be dated; they are not this class's.
    test_a_one_service_premiere_gains_the_others_airing = None
    test_a_service_that_could_not_be_asked_is_asked_next_time = None
    test_the_next_read_of_the_month_draws_the_new_airing = None
    test_not_listed_is_an_answer_kept_for_a_week = None
    test_only_a_premiere_or_a_release_can_be_completed = None


class AServiceWritesOnlyACalendarItPublishesTests(SearchTestCase):
    """calendar/search.py's `_publishes`: what is written is a service's own
    airing as its calendar feed would have stored it, so a catalogue with no
    feed for that calendar writes nothing — following its search result opens
    the month instead."""

    async def test_following_a_result_from_a_catalogue_with_no_calendar_writes_nothing(self):
        describer = SimpleNamespace(
            fetch_seasons=AsyncMock(side_effect=AssertionError("it was described")),
            fetch_details=AsyncMock(side_effect=AssertionError("it was described")),
            records_for=AsyncMock(side_effect=AssertionError("it was built")))
        catalogue_only = SimpleNamespace(
            detail_port=describer, calendar_port=None,
            capabilities=SimpleNamespace(answers=lambda _key: False))
        with patch("app.providers.get", return_value=catalogue_only):
            landing = await calendar_search.fill_one_gap(
                self.settings, source=Source.TMDB, source_id="777", media=Media.SHOW,
                season=1, endpoint_key=SHOWS, now=db.now())
        self.assertFalse(landing)
        self.assertEqual(await calendar_entries.sources_airing(SHOWS, "show:tmdb:777", 1),
                         set())

    def test_tmdb_writes_the_three_calendars_it_lists_and_no_others(self):
        tmdb = providers.get(Source.TMDB)
        for endpoint in ("shows/new", "shows/premieres", "movies"):
            with self.subTest(endpoint=endpoint):
                self.assertTrue(calendar_search._publishes(tmdb, endpoint))
        for endpoint in ("shows/finales", "shows"):
            with self.subTest(endpoint=endpoint):
                self.assertFalse(calendar_search._publishes(tmdb, endpoint))
