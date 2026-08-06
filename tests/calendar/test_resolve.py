"""What a viewer is shown, after the calendar started storing records instead of
payloads.

THE POINT OF THIS FILE IS THAT NOTHING CHANGED. The stored shape and the whole
read path moved underneath the calendar, with no user-visible difference
intended — which is precisely the kind of claim a passing suite is worst at
supporting, because every test can be rewritten alongside the code and still
agree with it. So the values below are TRANSCRIBED FROM THE PREVIOUS
IMPLEMENTATION rather than read off the new one: the exact strings the old
normalizer produced for the same entry in the same timezone, written out as
literals. A rewrite that renders a month differently fails here.

The one deliberate exception has its own class at the end, and it is a bug fix: a
release DATE is not an instant, and converting one through a viewer's timezone
moved a film to the previous day for anybody west of UTC.
"""
from __future__ import annotations

import unittest
from datetime import date
from unittest.mock import patch
from zoneinfo import ZoneInfo

from app import db
from app.calendar import cache as calendar_cache, resolve as calendar_resolve
from app.config import Settings
from app.endpoints import get_endpoint
from app.providers.base import Record, Source, render
from app.providers.trakt import calendar as trakt_calendar
from app.sources import prefs as source_prefs
from tests.support import calendar_records, new_db_path, window_fetch

SHOWS = get_endpoint("shows")
MOVIES = get_endpoint("movies")

# One rich entry, in the shape Trakt really returns it, carrying every field a
# card draws. The literals asserted against it below came from the normalizer
# this phase replaced.
RICH = {
    "first_aired": "2026-07-15T20:00:00.000Z",
    "episode": {"season": 2, "number": 5, "title": "The One"},
    "show": {
        "title": "Rich Show", "year": 2026, "network": "HBO", "country": "us",
        "language": "en", "runtime": 50, "status": "returning series",
        "rating": 8.456, "genres": ["drama", "game-show"],
        "overview": "An overview.", "certification": "TV-14",
        "ids": {"slug": "rich-show", "trakt": 123, "tvdb": 456, "tmdb": 789,
                "imdb": "tt42"},
        "images": {"poster": ["img.tmdb.example/poster.jpg"]},
    },
}


class ACardRendersExactlyAsItDidTests(unittest.TestCase):
    """Field by field, against literals from the previous implementation."""

    def item(self, tz_name="America/New_York", entry=None, endpoint=SHOWS):
        return render(trakt_calendar.to_record(entry or RICH, endpoint), ZoneInfo(tz_name))

    def test_every_display_field_is_what_it_always_was(self):
        item = self.item()
        self.assertEqual(item.id, "rich-show")
        self.assertEqual(item.title, "Rich Show")
        self.assertEqual(item.year, 2026)
        self.assertEqual(item.network, "HBO")
        self.assertEqual(item.country, "US")
        self.assertEqual(item.language, "EN")
        self.assertEqual(item.runtime, 50)
        self.assertEqual(item.status, "returning series")
        self.assertEqual(item.rating, 8.5)
        self.assertEqual(item.certification, "TV-14")
        self.assertEqual(item.overview, "An overview.")
        self.assertEqual(item.poster, "https://img.tmdb.example/poster.jpg")
        self.assertEqual(item.detail_url, "https://trakt.tv/shows/rich-show")
        self.assertEqual(item.episode_label, "S02E05")
        self.assertEqual(item.episode_title, "The One")
        self.assertEqual(item.season, 2)
        self.assertEqual(item.episode_number, 5)
        self.assertEqual(item.source, Source.TRAKT)

    def test_the_genres_a_card_shows_are_still_title_cased(self):
        """The display form is unchanged; what moved is WHERE it is produced.
        The slugs now survive as far as the filter, and the casing happens on the
        far side of it."""
        self.assertEqual(self.item().genres, ["Drama", "Game Show"])

    def test_the_four_air_fields_are_the_viewers_and_unchanged(self):
        for tz_name, expected in (
            ("UTC", ("2026-07-15", "15 Jul 2026", "20:00", "Wednesday")),
            ("America/New_York", ("2026-07-15", "15 Jul 2026", "16:00", "Wednesday")),
            # UTC+14: the same instant is the NEXT local day, which is the case
            # the whole per-viewer render exists for.
            ("Pacific/Kiritimati", ("2026-07-16", "16 Jul 2026", "10:00", "Thursday")),
        ):
            with self.subTest(tz=tz_name):
                item = self.item(tz_name)
                self.assertEqual(
                    (item.air_date, item.air_display, item.air_time, item.day_of_week),
                    expected)
                # The instant itself is the same fact for all three, which is why
                # one stored record can serve them.
                self.assertEqual(item.air_ts, 1784145600.0)

    def test_an_item_is_a_record_and_answers_to_both_names(self):
        """Everything that reads a card reads it unchanged, and anything that
        only wants the source's own facts can take either."""
        item = self.item()
        self.assertIsInstance(item, Record)


class AMonthRendersExactlyAsItDidTests(unittest.IsolatedAsyncioTestCase):
    """The same claim, made end to end through the cache rather than about one
    record: a whole month read for a Trakt-only instance, day by day."""

    async def asyncSetUp(self):
        new_db_path("calresolve")
        await db.migrate()
        self.settings = Settings()

    async def asyncTearDown(self):
        db.close_thread_connection()

    ENTRIES = [
        {"first_aired": "2026-07-15T20:00:00.000Z",
         "episode": {"season": 2, "number": 5, "title": "The One"},
         "show": {"title": "Beta", "country": "us", "genres": ["drama"],
                  "network": "HBO", "ids": {"slug": "beta", "trakt": 2, "tmdb": 22}}},
        {"first_aired": "2026-07-15T18:00:00.000Z",
         "episode": {"season": 1, "number": 1, "title": "Pilot"},
         "show": {"title": "Alpha", "country": "gb", "genres": ["game-show"],
                  "network": "BBC One", "ids": {"slug": "alpha", "trakt": 1, "tmdb": 11}}},
        {"first_aired": "2026-07-16T02:00:00.000Z",
         "episode": {"season": 3, "number": 9, "title": "Later"},
         "show": {"title": "Gamma", "country": "us", "genres": ["comedy"],
                  "network": "AMC", "ids": {"slug": "gamma", "trakt": 3, "tmdb": 33}}},
    ]

    async def read(self, tz_name="UTC", **kwargs):
        with patch("app.calendar.cache.fetch_window_records", window_fetch(self.ENTRIES)):
            return await calendar_cache.assemble_range(
                SHOWS, self.settings, tz=ZoneInfo(tz_name),
                start_date=date(2026, 7, 1), end_date=date(2026, 7, 31), **kwargs)

    async def test_the_days_and_their_order_are_unchanged(self):
        """Grouped by local day, each day's items in air order, the days
        themselves ascending."""
        grouped, meta = await self.read()
        self.assertEqual([g["date"] for g in grouped], ["2026-07-15", "2026-07-16"])
        self.assertEqual([g["label"] for g in grouped],
                         ["Wednesday, 15 July", "Thursday, 16 July"])
        self.assertEqual([i.id for i in grouped[0]["items"]], ["alpha", "beta"])
        self.assertEqual([i.id for i in grouped[1]["items"]], ["gamma"])
        self.assertEqual(meta["total"], 3)
        self.assertEqual(meta["show_ids"], ["alpha", "beta", "gamma"])
        self.assertFalse(meta["partial"])

    async def test_the_same_month_regroups_for_a_viewer_further_west(self):
        """The 16th's 02:00 UTC airing is the 15th in New York, so it moves —
        which is the read-time behaviour the shared cache exists to allow."""
        grouped, _ = await self.read("America/New_York")
        self.assertEqual([g["date"] for g in grouped], ["2026-07-15"])
        self.assertEqual([i.id for i in grouped[0]["items"]], ["alpha", "beta", "gamma"])

    async def test_a_multi_word_genre_filter_still_filters(self):
        """THE ONE THAT WOULD HAVE GONE SILENTLY WRONG. The spec matches the
        hyphenated slug, so a stored record holding the display form would leave
        "drama" working and "game-show" quietly matching nothing."""
        grouped, _ = await self.read(genres="game-show")
        self.assertEqual([i.id for g in grouped for i in g["items"]], ["alpha"])
        grouped, _ = await self.read(genres="-game-show")
        self.assertEqual([i.id for g in grouped for i in g["items"]], ["beta", "gamma"])
        # And the card still shows the display form of the genre it was kept for.
        grouped, _ = await self.read(genres="game-show")
        self.assertEqual(grouped[0]["items"][0].genres, ["Game Show"])

    async def test_the_country_and_network_filters_still_filter(self):
        grouped, _ = await self.read(countries="gb")
        self.assertEqual([i.id for g in grouped for i in g["items"]], ["alpha"])
        grouped, _ = await self.read(network_filter=["HBO"])
        self.assertEqual([i.id for g in grouped for i in g["items"]], ["beta"])

    async def test_two_viewers_read_one_stored_window_and_neither_poisons_it(self):
        """The cache stores the UNFILTERED window once and filtering happens at
        READ, so one viewer excluding a genre cannot change what another sees
        from the same rows."""
        with patch("app.calendar.cache.fetch_window_records",
                   window_fetch(self.ENTRIES)) as _:
            first, _ = await calendar_cache.assemble_range(
                SHOWS, self.settings, tz=ZoneInfo("UTC"),
                start_date=date(2026, 7, 1), end_date=date(2026, 7, 31),
                genres="-game-show", now=1000)
        # Second viewer, no filter, and NOTHING may be fetched — the window is
        # already stored, and it has to have been stored whole.
        never = window_fetch(lambda endpoint, start: (_ for _ in ()).throw(
            AssertionError("the stored window was not complete")))
        with patch("app.calendar.cache.fetch_window_records", never):
            second, _ = await calendar_cache.assemble_range(
                SHOWS, self.settings, tz=ZoneInfo("UTC"),
                start_date=date(2026, 7, 1), end_date=date(2026, 7, 31), now=1000)
        self.assertEqual([i.id for g in first for i in g["items"]], ["beta", "gamma"])
        self.assertEqual([i.id for g in second for i in g["items"]],
                         ["alpha", "beta", "gamma"])


class AReleaseDateIsNotAnInstantTests(unittest.TestCase):
    """THE ONE DELIBERATE CHANGE, and it is a fix.

    A film released on the 6th is released on the 6th wherever you are. The old
    read turned `released` into a UTC-midnight timestamp and then converted it
    into the viewer's timezone, which moved every release to the previous day for
    anybody west of UTC — a UTC-8 viewer saw a film dated the 5th, in the wrong
    month whenever the 1st was a release date.
    """

    ENTRY = {"released": "2026-07-06",
             "movie": {"title": "A Film", "ids": {"slug": "a-film", "trakt": 9}}}

    def item(self, tz_name):
        return render(trakt_calendar.to_record(self.ENTRY, MOVIES), ZoneInfo(tz_name))

    def test_every_viewer_sees_the_release_on_the_day_it_was_released(self):
        for tz_name in ("UTC", "America/Los_Angeles", "Pacific/Apia", "Asia/Tokyo"):
            with self.subTest(tz=tz_name):
                item = self.item(tz_name)
                self.assertEqual(item.air_date, "2026-07-06")
                self.assertEqual(item.air_display, "06 Jul 2026")
                self.assertEqual(item.day_of_week, "Monday")

    def test_the_old_answer_is_the_one_that_is_gone(self):
        """Named explicitly rather than left as the absence of a bug: a UTC-8
        viewer used to be shown 05 Jul for this film."""
        self.assertNotEqual(self.item("America/Los_Angeles").air_date, "2026-07-05")

    def test_an_episode_is_still_converted_because_it_really_is_an_instant(self):
        """The flag is not "never convert" — a broadcast happens at a moment, and
        two viewers in different zones are right to see different local times for
        it."""
        record = trakt_calendar.to_record(RICH, SHOWS)
        self.assertFalse(record.date_only)
        self.assertEqual(render(record, ZoneInfo("America/New_York")).air_time, "16:00")

    def test_the_flag_travels_through_storage(self):
        """It decides how a card is drawn at READ, so a window that lost it would
        put every film back a day for half the world until its TTL expired."""
        stored = trakt_calendar.to_record(self.ENTRY, MOVIES).to_dict()
        self.assertTrue(Record.from_dict(stored).date_only)


class ResolvingOneSourceTests(unittest.TestCase):
    """With one source behind a group there is one answer, and the seam still has
    to hand it back with the group's own ids rather than that source's."""

    def group(self):
        records = calendar_records([RICH], SHOWS)
        return calendar_cache.group_records(records)[0]

    def test_the_record_comes_back_whole(self):
        record = calendar_resolve.resolve(self.group())
        self.assertEqual(record.title, "Rich Show")
        self.assertEqual(record.source, Source.TRAKT)

    def test_the_ids_are_the_groups_union_not_one_sources_own(self):
        """`ids` is the MATCH RESULT and is what the arr/Seerr buttons, the
        tracker and the ranker read. Narrowing it to the winning source's own map
        is how a merged card would quietly lose the other service's ids."""
        group = self.group()
        group["ids"]["mal"] = 4242
        self.assertEqual(calendar_resolve.resolve(group).ids["mal"], 4242)

    def test_a_group_holding_nothing_readable_resolves_to_nothing(self):
        """Skipped, not raised over: one unreadable row in a stored window is not
        a reason to fail a month."""
        self.assertIsNone(calendar_resolve.resolve({"key": "x", "ids": {}, "by_source": {}}))
        self.assertIsNone(calendar_resolve.resolve(
            {"key": "x", "ids": {}, "by_source": {"trakt": {"title": "no air time"}}}))


def _record(source, slug, *, tmdb=None, title=None, air_ts=1784145600.0):
    """One source's record for an airing, as the cache stores them."""
    ids = {"slug": slug, str(source): slug}
    if tmdb is not None:
        ids["tmdb"] = tmdb
    return Record(
        source=source, media="show", id=slug, ids=ids,
        detail_url=f"https://example.test/{slug}", title=title or slug.title(),
        air_ts=air_ts, season=1, episode_number=1, episode_label="S01E01",
    )


class WhichSourceAViewerReadsTests(unittest.TestCase):
    """The per-viewer half: one stored window, several viewers, different
    answers out of the same rows.

    A group naming several sources is one title both services listed; a group
    naming one is a title only that service had. Which of those a person is shown
    is their own choice, and it is applied HERE — the window itself was filled by
    asking everybody, so the choice can narrow what one person reads without
    touching what the next person gets.
    """

    LINKED = frozenset({"trakt", "simkl"})

    def groups(self):
        """Three groups: one both services listed, one only Trakt, one only
        Simkl — which is the shape a real window has (measured on the author's
        own cache: 4 of 35 entries in one week carried both)."""
        return calendar_cache.group_records([
            _record(Source.TRAKT, "shared", tmdb=100, title="Shared"),
            _record(Source.SIMKL, "shared-simkl", tmdb=100, title="Shared"),
            _record(Source.TRAKT, "trakt-only"),
            _record(Source.SIMKL, "simkl-only"),
        ])

    def read(self, selection, linked=None):
        prefs = source_prefs.SourcePrefs(user_id=1, calendar_source=selection)
        linked = self.LINKED if linked is None else linked
        return [(r.title, str(r.source))
                for r in (calendar_resolve.resolve(g, prefs, linked) for g in self.groups())
                if r is not None]

    def test_naming_one_source_drops_the_groups_only_the_other_describes(self):
        """The reported defect, stated as a test: every `source=` value used to
        render the same cards, because nothing filtered by source at read."""
        self.assertEqual(self.read("trakt"), [("Shared", "trakt"), ("Trakt-Only", "trakt")])
        self.assertEqual(self.read("simkl"), [("Shared", "simkl"), ("Simkl-Only", "simkl")])

    def test_both_reads_every_group_and_a_shared_one_once(self):
        """A title two services listed is one card, not two — they merged into
        one group on the shared tmdb id at fill — and the declared order picks
        which side of it is shown."""
        self.assertEqual(
            self.read("both"),
            [("Shared", "trakt"), ("Trakt-Only", "trakt"), ("Simkl-Only", "simkl")])

    def test_auto_follows_the_links(self):
        self.assertEqual([s for _, s in self.read("auto", frozenset({"simkl"}))],
                         ["simkl", "simkl"])
        self.assertEqual([s for _, s in self.read("auto", frozenset({"trakt"}))],
                         ["trakt", "trakt"])

    def test_an_account_that_has_linked_nothing_still_has_a_calendar(self):
        """'auto' follows the links, and there are none to follow — but a
        calendar needs no link to be worth reading, and blanking it for everybody
        who only ever signs in would be a strange reading of "no opinion"."""
        self.assertEqual(len(self.read("auto", frozenset())), 3)

    def test_a_stated_selection_is_honoured_with_nothing_linked(self):
        """The absence of a link is the absence of a statement; naming a service
        IS a statement and stays one whatever is linked."""
        self.assertEqual(self.read("simkl", frozenset()),
                         [("Shared", "simkl"), ("Simkl-Only", "simkl")])

    def test_no_account_asking_reads_everything(self):
        """A public share page has no viewer to have a preference."""
        titles = [calendar_resolve.resolve(g).title for g in self.groups()]
        self.assertEqual(titles, ["Shared", "Trakt-Only", "Simkl-Only"])


class TwoViewersOneWindowTests(unittest.IsolatedAsyncioTestCase):
    """The invariant this fix restores, end to end: the stored window is the
    union of what every source said, and two viewers who chose differently read
    that one row without either of them changing it."""

    async def asyncSetUp(self):
        new_db_path("calsources")
        await db.migrate()
        self.settings = Settings()

    async def asyncTearDown(self):
        db.close_thread_connection()

    LINKED = frozenset({"trakt", "simkl"})

    async def fill_and_read(self, selection, *, allow_fetch=True):
        """One span read as `selection`, with the fill answering for both
        services. `allow_fetch=False` proves the read came out of the stored
        window rather than out of a fetch shaped by the viewer."""
        async def fetch(endpoint, settings, start):
            if start != calendar_cache.window_start(date(2026, 7, 15)):
                return [], ["trakt", "simkl"]
            return [_record(Source.TRAKT, "trakt-only"),
                    _record(Source.SIMKL, "simkl-only")], ["trakt", "simkl"]

        prefs = source_prefs.SourcePrefs(user_id=1, calendar_source=selection)
        with patch("app.calendar.cache.fetch_window_records", fetch):
            grouped, meta = await calendar_cache.assemble_range(
                SHOWS, self.settings, tz=ZoneInfo("UTC"),
                start_date=date(2026, 7, 15), end_date=date(2026, 7, 15),
                prefs=prefs, linked=self.LINKED, allow_fetch=allow_fetch, now=1000)
        return [i.id for g in grouped for i in g["items"]], meta

    async def test_a_viewer_naming_one_source_does_not_narrow_what_is_stored(self):
        """The poisoning half of the defect. A `?source=simkl` load used to write
        a Simkl-only window that every other viewer then read as the truth for
        that week."""
        ids, _ = await self.fill_and_read("simkl")
        self.assertEqual(ids, ["simkl-only"])
        window, _ = await calendar_cache.read_cached_window(
            SHOWS.key, calendar_cache.window_start(date(2026, 7, 15)))
        self.assertEqual(sorted(window.sources), ["simkl", "trakt"])
        self.assertEqual(
            sorted(s for g in window.groups for s in g["by_source"]), ["simkl", "trakt"])

    async def test_the_next_viewer_reads_their_own_answer_out_of_that_window(self):
        """No fetch is permitted for the second read, so whatever it finds was
        put there by the first viewer's fill."""
        await self.fill_and_read("simkl")
        ids, _ = await self.fill_and_read("trakt", allow_fetch=False)
        self.assertEqual(ids, ["trakt-only"])
        ids, _ = await self.fill_and_read("both", allow_fetch=False)
        self.assertEqual(ids, ["trakt-only", "simkl-only"])

    async def test_a_source_selection_never_makes_a_span_partial(self):
        """Not reading a source is not a source failing. The banner says data
        could not be loaded, which is untrue of a service the viewer asked not to
        see."""
        for selection in ("auto", "both", "trakt", "simkl"):
            with self.subTest(source=selection):
                _, meta = await self.fill_and_read(selection)
                self.assertFalse(meta["partial"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
