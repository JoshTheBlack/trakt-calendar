"""A calendar a service lists WITHOUT PUBLISHING IT (app/providers/base.py's
`DrawnFrom`): Simkl's Season Finales are its All Episodes listings whose episode
TMDB's season list marks a finale or a mid-season finale — read from the title
store at read (app/calendar/query.py's `read_rows`), made current through Simkl's
All Episodes windows (app/calendar/cache.py's `drawn_sources`), and kept owed
their marks by the heartbeat (app/metadata/refresh.py's `queue_marks`).

Every card here is stored and read back through the app's own read. No network.
"""
from __future__ import annotations

import asyncio
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

from app import db, providers
from app.calendar import cache as calendar_cache
from app.calendar import query, vocab
from app.config import Settings
from app.endpoints import get_endpoint
from app.metadata import refresh, tables
from app.providers.base import Media, Record, Source
from tests.support import CardTestCase, put_listing, put_title, seed_records, title_key_of

FINALES = "shows/finales"
TMDB_TOKEN = "eyJhbGciOiJIUzI1NiJ9.eyJhdWQiOiJtYXRjaGluZyJ9.c2lnbmF0dXJl"
TMDB_ID = 4242


def _at(*parts) -> float:
    return datetime(*parts, tzinfo=timezone.utc).timestamp()


def simkl_episode(number: int, **fields) -> Record:
    """Simkl's All Episodes listing of S02E{number}, 10 October + number."""
    values = dict(source=Source.SIMKL, media=Media.SHOW, id="777",
                  ids={"simkl": 777, "tmdb": str(TMDB_ID)}, title="The Show",
                  detail_url="https://simkl.test/tv/777",
                  air_ts=_at(2026, 10, 10 + number, 1), season=2, episode_number=number,
                  episode_label=f"S02E{number:02d}", enriched=True)
    values.update(fields)
    return Record(**values)


def _typed(record: Record, source: str, word: str) -> None:
    """`source`'s word for the episode's place in its season, on its row."""
    db.connection().execute(
        f"UPDATE {tables.EPISODES} SET {source}_episode_type = ? "
        "WHERE title_key = ? AND season = ? AND number = ?",
        (word, title_key_of(record), record.season, record.episode_number))


def _trakt_finale(record: Record, word: str = "season_finale") -> None:
    """Trakt's own Season Finales listing of the same episode, with its word."""
    conn = db.connection()
    key = title_key_of(record)
    put_title(conn, "trakt", "show", "the-show", key, ids={"trakt": 99, "tmdb": TMDB_ID},
              title="The Show")
    put_listing(conn, "trakt", "show", "99", FINALES, key, record.air_ts + 3600,
                season=record.season, number=record.episode_number)
    _typed(record, "trakt", word)


def _cards(record: Record, endpoint: str = FINALES) -> list:
    """The cards one viewer's read draws of `record`'s title, from the store as
    it stands — tests.support's read_cards stores its records afresh, which
    would take the marks these tests set."""
    conn = db.connection()
    viewer = query.Viewer(get_endpoint(endpoint), None, None, ZoneInfo("UTC"),
                          vocab.complete_specs({}))
    listings = query.read_rows(conn, endpoint, titles={title_key_of(record)})
    placed = query.judge(conn, query.views_of(listings, viewer), viewer)
    views = sorted(placed.views, key=lambda view: (view.air_date, view.air_ts))
    return query.present_rows(conn, views, viewer)


def _span(endpoint: str = FINALES) -> list:
    return query.read_rows(db.connection(), endpoint, first=date(2026, 10, 1),
                           after=date(2026, 11, 1))


class SimklsFinalesAreItsListingsTmdbMarksTests(CardTestCase):

    def setUp(self):
        super().setUp()
        self.finale, self.middle, self.plain = (simkl_episode(10), simkl_episode(5),
                                                simkl_episode(3))
        seed_records(db.connection(), [self.finale, self.middle, self.plain], endpoint="shows")
        _typed(self.finale, "tmdb", "finale")
        _typed(self.middle, "tmdb", "mid_season")
        _typed(self.plain, "tmdb", "standard")

    def test_a_marked_episode_is_on_the_finales_calendar_and_no_other(self):
        on = {(listing.source, listing.episode_number, listing.mark) for listing in _span()}
        self.assertEqual(on, {("simkl", 10, "finale"), ("simkl", 5, "mid_season_finale")})

    def test_the_whole_titles_read_agrees_with_the_span(self):
        found = query.read_rows(db.connection(), FINALES,
                                titles={title_key_of(self.finale)})
        self.assertEqual(sorted(listing.episode_number for listing in found), [5, 10])

    def test_all_episodes_marks_nothing(self):
        """The tag is the finales calendar's alone (endpoints.Endpoint.marked)."""
        self.assertEqual({listing.mark for listing in _span("shows")}, {""})
        self.assertEqual(len(_span("shows")), 3)

    def test_the_card_says_mid_season(self):
        cards = _cards(self.finale)
        self.assertEqual([(c.episode_number, c.episode_mark) for c in cards],
                         [(5, "mid_season_finale"), (10, "finale")])

    def test_a_mark_learned_later_puts_the_card_on_the_next_read(self):
        self.assertNotIn(3, [listing.episode_number for listing in _span()])
        _typed(self.plain, "tmdb", "finale")
        self.assertIn(3, [listing.episode_number for listing in _span()])

    def test_an_episode_trakt_lists_too_is_one_card(self):
        _trakt_finale(self.finale)
        found = [listing for listing in _span() if listing.episode_number == 10]
        self.assertEqual({listing.source for listing in found}, {"simkl", "trakt"})
        cards = [c for c in _cards(self.finale) if c.episode_number == 10]
        self.assertEqual(len(cards), 1)

    def test_trakts_own_listing_is_marked_by_its_own_word(self):
        _trakt_finale(self.plain, "mid_season_finale")
        trakt = [listing for listing in _span() if listing.source == "trakt"]
        self.assertEqual([(listing.episode_number, listing.mark) for listing in trakt],
                         [(3, "mid_season_finale")])

    def test_trakts_listing_needs_no_mark_to_stay(self):
        """Trakt publishes its finales: its listing is the calendar's, whatever
        TMDB says of the episode."""
        _trakt_finale(self.plain, "")
        self.assertIn(("trakt", 3), {(listing.source, listing.episode_number)
                                     for listing in _span()})


class WhoListsTheCalendarTests(CardTestCase):

    def test_simkl_draws_finales_and_publishes_none(self):
        simkl = providers.registered()[Source.SIMKL]
        self.assertFalse(simkl.capabilities.answers(FINALES))
        self.assertEqual(simkl.capabilities.drawn(FINALES).listings_of, "shows")
        self.assertIs(simkl.capabilities.drawn(FINALES).marked_by, Source.TMDB)

    def test_simkl_is_offered_on_finales_only_where_tmdb_can_be_read(self):
        simkl = providers.registered()[Source.SIMKL]
        self.assertTrue(providers.lists_calendar(simkl, FINALES, Settings(tmdb_api_key=TMDB_TOKEN)))
        self.assertFalse(providers.lists_calendar(simkl, FINALES, Settings()))

    def test_a_finales_read_makes_simkls_all_episodes_current(self):
        drawn = calendar_cache.drawn_sources(get_endpoint(FINALES),
                                             Settings(tmdb_api_key=TMDB_TOKEN), date.today())
        self.assertEqual([(source, of.key) for source, of in drawn], [("simkl", "shows")])
        self.assertEqual(calendar_cache.drawn_sources(get_endpoint(FINALES), Settings(),
                                                      date.today()), [])
        self.assertEqual(calendar_cache.drawn_sources(get_endpoint("shows"),
                                                      Settings(tmdb_api_key=TMDB_TOKEN),
                                                      date.today()), [])


class TheSeasonsAMarkNeedsAreQueuedTests(CardTestCase):

    def setUp(self):
        super().setUp()
        self.settings = Settings(tmdb_api_key=TMDB_TOKEN)
        self.episode = simkl_episode(4)
        seed_records(db.connection(), [self.episode], endpoint="shows")
        db.connection().execute(
            f"UPDATE {tables.TITLES} SET ids_json = json_set(ids_json, '$.tmdb', ?) "
            "WHERE title_key = ?", (TMDB_ID, title_key_of(self.episode)))

    def _listed_by_tmdb(self, episode_count: int = 10) -> None:
        """TMDB's description of the title lists season 2, with `episode_count`
        episodes — a season list leaves the season's clock at the schema's 0."""
        db.connection().execute(
            f"INSERT INTO {tables.SEASONS} (title_key, season, tmdb_fetched_at, "
            "tmdb_episode_count) VALUES (?, 2, 1, ?) "
            "ON CONFLICT (title_key, season) DO UPDATE SET tmdb_fetched_at = 1, "
            "tmdb_episode_count = excluded.tmdb_episode_count",
            (title_key_of(self.episode), episode_count))

    def _read_typing_nothing(self, stale_after: int) -> None:
        """TMDB's read of season 2 answered and typed no episode — its season
        group is written with the clock the answer set, and nothing else."""
        db.connection().execute(f"UPDATE {tables.SEASONS} SET tmdb_stale_after = ?",
                                (stale_after,))
        db.connection().execute("DELETE FROM metadata_refresh")

    def _queue(self) -> list[tuple]:
        asyncio.run(refresh.queue_marks(self.settings, date(2026, 10, 1), date(2026, 12, 1),
                                        today=date(2026, 10, 14)))
        return [tuple(row) for row in db.connection().execute(
            "SELECT source, media, source_id, season, reason FROM metadata_refresh")]

    def test_a_season_tmdb_lists_and_has_not_typed_is_queued(self):
        self._listed_by_tmdb()
        self.assertEqual(self._queue(), [("tmdb", "show", str(TMDB_ID), 2, refresh.REASON_MARK)])

    def test_a_season_tmdb_does_not_list_is_never_asked(self):
        self.assertEqual(self._queue(), [])

    def test_a_season_typed_inside_its_clock_is_not_asked_again(self):
        self._listed_by_tmdb()
        _typed(self.episode, "tmdb", "standard")
        db.connection().execute(f"UPDATE {tables.EPISODES} SET tmdb_stale_after = ?",
                                (db.now() + 3600,))
        self.assertEqual(self._queue(), [])

    def test_a_season_typed_past_its_clock_is_read_again(self):
        self._listed_by_tmdb()
        _typed(self.episode, "tmdb", "standard")
        db.connection().execute(f"UPDATE {tables.EPISODES} SET tmdb_stale_after = 1")
        self.assertEqual(len(self._queue()), 1)

    def test_a_season_whose_read_typed_nothing_waits_its_own_clock(self):
        """The heartbeat once read the same seasons every minute: an answer that
        types nothing leaves the season untyped, and only its clock says when to
        ask again."""
        self._listed_by_tmdb()
        self.assertEqual(len(self._queue()), 1)
        self._read_typing_nothing(db.now() + 86400)
        self.assertEqual(self._queue(), [])
        self._read_typing_nothing(db.now() - 1)
        self.assertEqual(len(self._queue()), 1)

    def test_a_season_tmdb_holds_with_no_episodes_is_never_asked(self):
        """What an empty answer leaves (TMDB has no such season — Simkl numbers
        some by year — or lists none of its episodes): nothing to mark."""
        self._listed_by_tmdb(episode_count=0)
        self.assertEqual(self._queue(), [])

    def test_a_season_tmdb_gave_up_on_is_left_alone(self):
        self._listed_by_tmdb()
        db.connection().execute(f"UPDATE {tables.SEASONS} SET tmdb_fail_count = ?",
                                (refresh.LISTED_GIVE_UP_AFTER,))
        self.assertEqual(self._queue(), [])


class TheCardSaysMidSeasonTests(CardTestCase):
    """The tag is drawn from the card's mark and nothing else, so it can only
    appear where a read marked the card: the finales calendar."""

    def _html(self, mark: str) -> str:
        from app.providers.base import render
        from app.templating import templates
        record = simkl_episode(5, episode_mark=mark)
        return templates.env.get_template("_card.html").render(
            item=render(record, ZoneInfo("UTC")), not_watching=set(), new_ids=set(),
            is_admin=False, month=10, year=2026, endpoint=get_endpoint(FINALES),
            settings=Settings())

    def test_a_mid_season_finale_is_tagged(self):
        self.assertIn("MID-SEASON FINALE", self._html("mid_season_finale"))

    def test_a_season_finale_and_an_unmarked_card_are_not(self):
        self.assertNotIn("MID-SEASON FINALE", self._html("finale"))
        self.assertNotIn("MID-SEASON FINALE", self._html(""))
