"""Migration 49: one row per title, per season and per episode, every service's
facts side by side, converted from the per-service tables — which are kept whole.

Every case the measured copies held is a fixture here, on a database brought to
48 by the migrations themselves: a service with two entries for one title, one
entry spelled two ways, a listing that names no full episode (folded, refused as
ambiguous, refused because its own service already lists the episode, two on one
day), one service listing an episode twice, two services dating it far apart,
one instant on two calendars and two instants on two calendars, a searched
listing its feed stands down and one it does not, and a listing filed under a
key its title row has since left. Then what the conversion must keep: the
retired tables untouched, a re-run that does nothing, production's road from
46, and the tables holding exactly the columns each provider declares.

No network.
"""
from __future__ import annotations

import json
import sqlite3
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from app import db, providers
from app.endpoints import ENDPOINTS
from tests.support import TMP

NOW = 1_800_000_000

TITLE_IDENTITY = {"title_key", "media", "ids_json", "created_at", "last_viewed_at",
                  "refreshed_at"}
SEASON_IDENTITY = {"title_key", "season"}
EPISODE_IDENTITY = {"title_key", "unit", "media", "season", "number"}

K1, K2, K3, K4, K5, K6 = ("show:tmdb:100", "show:tmdb:200", "show:tmdb:300",
                          "movie:tmdb:400", "show:tmdb:500", "show:tmdb:600")


def at(day: str, hour: int = 20) -> float:
    return datetime.fromisoformat(day).replace(hour=hour, tzinfo=timezone.utc).timestamp()


def _connect(name: str) -> sqlite3.Connection:
    path = TMP / name
    path.unlink(missing_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.isolation_level = None  # the runner issues its own BEGIN IMMEDIATE
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _title(conn, source: str, source_id: str, key: str, *, media: str = "show",
           ids: dict | None = None, title: str = "", enriched: int = 1, fetched_at: int = 100,
           table: str = "title_metadata", **words) -> None:
    columns = {"source": source, "media": media, "source_id": source_id, "title_key": key,
               "title": title, "ids_json": json.dumps(ids or {source: source_id}),
               "enriched": enriched, "fetched_at": fetched_at, **words}
    conn.execute(f"INSERT INTO {table} ({', '.join(columns)}) "
                 f"VALUES ({', '.join('?' * len(columns))})", tuple(columns.values()))


def _airing(conn, source: str, source_id: str, key: str, endpoint: str, day: str, *,
            hour: int = 20, season: int = -1, number: int = -1, media: str = "show",
            from_search: int = 0, stored_at: int = 100, label: str = "") -> None:
    conn.execute(
        "INSERT INTO calendar_airings (source, media, source_id, endpoint, title_key, air_ts, "
        "air_date, date_only, season, episode_number, episode_label, stored_at, from_search) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?)",
        (source, media, source_id, endpoint, key, at(day, hour), day, season, number, label,
         stored_at, from_search))


def _through(version: int) -> list:
    """The migrations up to `version`, the conversion as 49 whether or not the
    app's list carries it yet."""
    return [m for m in [*(m for m in db.MIGRATIONS if m[0] <= 48), (49, db.MIGRATION_49)]
            if m[0] <= version]


def _convert(conn, now: int = NOW) -> None:
    """Migration 49, applied the way the runner applies one."""
    conn.execute("BEGIN IMMEDIATE")
    db.MIGRATION_49(conn, now=now)
    conn.execute("UPDATE schema_version SET version = 49")
    conn.execute("COMMIT")


def _snapshot(conn, tables) -> dict:
    return {table: sorted(tuple(r) for r in conn.execute(f"SELECT * FROM {table}"))
            for table in tables}


RETIRED = ("title_metadata", "season_metadata", "episode_metadata", "calendar_airings")


def _seed(conn) -> None:
    """Every case, on a database at 48."""
    conn.execute("INSERT INTO users (id, username, created_at, updated_at) "
                 "VALUES (1, 'someone', 1, 1)")
    conn.execute("INSERT INTO titles (title_key, media, ids_json, created_at) "
                 "VALUES (?, 'show', ?, 5)", (K1, json.dumps({"tmdb": 100, "tvdb": 9})))
    # K1: TMDB once; Trakt one entry spelled twice (its slug and its number);
    # Simkl two entries — the show, listed once, and its second season filed as
    # a title of its own, listed three times.
    _title(conn, "tmdb", "100", K1, title="The Show", keywords_json='["anime"]',
           show_type="scripted", facts_known=1, cast_json='[{"name": "A"}]')
    _title(conn, "trakt", "the-show", K1, ids={"trakt": 500, "tmdb": 100, "trakt_slug": "the-show"},
           title="The Show", homepage="https://example.test")
    _title(conn, "trakt", "500", K1, ids={"trakt": 500, "tmdb": 100}, title="The Show",
           fetched_at=50)
    _title(conn, "simkl", "1001", K1, ids={"simkl": 1001, "tmdb": "100"}, title="The Show")
    _title(conn, "simkl", "1002", K1, ids={"simkl": 1002, "tmdb": "100"},
           title="The Show Season 2", anime_type="tv")
    _airing(conn, "trakt", "the-show", K1, "shows", "2026-10-07", season=2, number=1, label="S02E01")
    _airing(conn, "trakt", "the-show", K1, "shows/premieres", "2026-10-07", season=2, number=1,
            label="S02E01")
    # Folds into Trakt's S2E1 that day: one numbered episode agrees with it.
    _airing(conn, "simkl", "1002", K1, "shows", "2026-10-07", hour=21, number=1)
    # Two episodes one day that nobody else lists: two rows.
    _airing(conn, "simkl", "1002", K1, "shows", "2026-10-14", number=2)
    _airing(conn, "simkl", "1002", K1, "shows", "2026-10-14", number=3)
    _airing(conn, "simkl", "1001", K1, "shows", "2024-01-05", number=1)
    # TMDB dates the same episode two months later: still the one row.
    _airing(conn, "tmdb", "100", K1, "shows/premieres", "2026-12-20", season=2, number=1)
    conn.execute("INSERT INTO episode_metadata (source, media, source_id, season, number, title, "
                 "runtime, rating, votes, episode_type, fetched_at, stale_after) "
                 "VALUES ('trakt', 'show', 'the-show', 2, 1, 'Premiere', 45, 8.1, 100, "
                 "'season_premiere', 100, 200)")
    conn.execute("INSERT INTO season_metadata (source, media, source_id, season, name, "
                 "episode_count, premiere, poster, fetched_at) "
                 "VALUES ('tmdb', 'show', '100', 2, 'Season Two', 10, '2026-10-07', '/s2.jpg', 100)")

    # K2: an ambiguous day, and a listing whose own service already lists the one
    # episode it could be.
    _title(conn, "trakt", "two", K2, ids={"trakt": 2, "tmdb": 200})
    _title(conn, "simkl", "2001", K2, ids={"simkl": 2001, "tmdb": 200})
    _airing(conn, "trakt", "two", K2, "shows", "2026-10-01", season=1, number=1)
    _airing(conn, "trakt", "two", K2, "shows", "2026-10-01", season=2, number=1)
    _airing(conn, "simkl", "2001", K2, "shows", "2026-10-01", number=1)
    _airing(conn, "trakt", "two", K2, "shows", "2026-10-02", season=1, number=5)
    _airing(conn, "simkl", "2001", K2, "shows", "2026-10-02", season=1, number=5)
    _airing(conn, "simkl", "2001", K2, "shows", "2026-10-02", hour=22, number=5)

    # K3: one service's episode at an hour apart on two calendars, and another
    # episode it lists twice a week apart.
    _title(conn, "simkl", "3001", K3, ids={"simkl": 3001, "tmdb": 300})
    _airing(conn, "simkl", "3001", K3, "shows/premieres", "2026-10-03", season=1, number=1)
    _airing(conn, "simkl", "3001", K3, "shows", "2026-10-03", hour=21, season=1, number=1)
    _airing(conn, "simkl", "3001", K3, "shows", "2026-10-10", season=1, number=2)
    _airing(conn, "simkl", "3001", K3, "shows", "2026-10-17", season=1, number=2)

    # K4: a film Simkl lists twice and TMDB once.
    _title(conn, "simkl", "4001", K4, media="movie", ids={"simkl": 4001, "tmdb": 400})
    _title(conn, "tmdb", "400", K4, media="movie", ids={"tmdb": 400})
    _airing(conn, "simkl", "4001", K4, "movies", "2026-11-04", hour=0, media="movie")
    _airing(conn, "simkl", "4001", K4, "movies", "2026-12-25", hour=0, media="movie")
    _airing(conn, "tmdb", "400", K4, "movies", "2026-11-04", hour=0, media="movie")

    # K5: a searched listing its feed also lists (stands down), and one it does not.
    _title(conn, "trakt", "five", K5, ids={"trakt": 5, "tmdb": 500})
    _airing(conn, "trakt", "five", K5, "shows/premieres", "2026-10-05", hour=0, season=1,
            number=1, from_search=1)
    _airing(conn, "trakt", "five", K5, "shows/premieres", "2026-10-05", hour=7, season=1,
            number=1)
    _airing(conn, "trakt", "five", K5, "shows/new", "2026-10-06", season=1, number=2,
            from_search=1)

    # K6: an airing still filed under the key its title row has left.
    _title(conn, "simkl", "6001", K6, ids={"simkl": 6001, "tmdb": 600, "tvdb": 66})
    _airing(conn, "simkl", "6001", "show:tvdb:66", "shows", "2026-10-09", season=1, number=1)


class _Converted(unittest.TestCase):
    """A database at 48 with every case seeded, then converted."""

    @classmethod
    def setUpClass(cls):
        cls.conn = _connect("title-store-49.db")
        with patch.object(db, "MIGRATIONS", _through(48)):
            db.migrate_sync(cls.conn)
        _seed(cls.conn)
        cls.retired_before = _snapshot(cls.conn, RETIRED)
        cls.identity_before = _snapshot(cls.conn, ("titles",))["titles"]
        _convert(cls.conn)

    @classmethod
    def tearDownClass(cls):
        cls.conn.close()
        (TMP / "title-store-49.db").unlink(missing_ok=True)

    def title(self, key):
        return self.conn.execute("SELECT * FROM titles WHERE title_key = ?", (key,)).fetchone()

    def episode(self, key, unit):
        return self.conn.execute("SELECT * FROM episodes WHERE title_key = ? AND unit = ?",
                                 (key, unit)).fetchone()

    def units(self, key):
        return [r[0] for r in self.conn.execute(
            "SELECT unit FROM episodes WHERE title_key = ? ORDER BY unit", (key,))]


class AServiceWithSeveralEntriesTests(_Converted):

    def test_the_entry_with_the_most_listings_is_the_titles_group(self):
        row = self.title(K1)
        self.assertEqual((row["simkl_id"], row["simkl_title"], row["simkl_anime_type"]),
                         ("1002", "The Show Season 2", "tv"))

    def test_every_entry_still_finds_the_title_and_only_one_is_primary(self):
        found = {r["service_id"]: (r["title_key"], r["is_primary"]) for r in self.conn.execute(
            "SELECT * FROM service_ids WHERE source = 'simkl' AND title_key = ?", (K1,))}
        self.assertEqual(found, {"1001": (K1, 0), "1002": (K1, 1)})

    def test_a_listing_from_the_other_entry_says_which(self):
        self.assertEqual(self.episode(K1, "D2024-01-05E1")["simkl_entry"], "1001")
        self.assertIsNone(self.episode(K1, "S2E1")["simkl_entry"])

    def test_one_entry_spelled_two_ways_is_one_entry_under_its_number(self):
        row = self.title(K1)
        self.assertEqual(row["trakt_id"], "500")
        # The more listed spelling's words: its homepage, the clock it was read at.
        self.assertEqual((row["trakt_homepage"], row["trakt_fetched_at"]),
                         ("https://example.test", 100))
        found = {r["service_id"]: r["is_primary"] for r in self.conn.execute(
            "SELECT * FROM service_ids WHERE source = 'trakt' AND title_key = ?", (K1,))}
        self.assertEqual(found, {"the-show": 1, "500": 1})


class TheEpisodeRuleTests(_Converted):

    def test_a_numbered_episode_is_one_row_whoever_lists_it_and_however_far_apart(self):
        row = self.episode(K1, "S2E1")
        self.assertEqual((row["season"], row["number"], row["media"]), (2, 1, "show"))
        self.assertEqual(row["trakt_air_ts"], at("2026-10-07"))
        self.assertEqual(row["tmdb_air_ts"], at("2026-12-20"))

    def test_a_listing_naming_no_full_episode_joins_the_one_episode_it_can_be(self):
        row = self.episode(K1, "S2E1")
        self.assertEqual((row["simkl_air_ts"], row["simkl_calendars"]),
                         (at("2026-10-07", 21), ENDPOINTS["shows"].bit))

    def test_two_episodes_on_one_day_nobody_numbers_are_two_rows(self):
        self.assertIn("D2026-10-14E2", self.units(K1))
        self.assertIn("D2026-10-14E3", self.units(K1))
        row = self.episode(K1, "D2026-10-14E3")
        self.assertEqual((row["season"], row["number"]), (None, 3))

    def test_a_day_with_two_episodes_it_could_be_joins_neither(self):
        self.assertIn("D2026-10-01E1", self.units(K2))
        self.assertIsNone(self.episode(K2, "S1E1")["simkl_air_ts"])
        self.assertIsNone(self.episode(K2, "S2E1")["simkl_air_ts"])

    def test_a_listing_never_joins_its_own_services_listing_of_the_episode(self):
        self.assertEqual(self.episode(K2, "S1E5")["simkl_air_ts"], at("2026-10-02"))
        self.assertEqual(self.episode(K2, "D2026-10-02E5")["simkl_air_ts"], at("2026-10-02", 22))

    def test_a_film_is_its_titles_film_row(self):
        row = self.episode(K4, "film")
        self.assertEqual((row["media"], row["season"], row["number"]), ("movie", None, None))
        self.assertEqual(self.units(K4), ["film"])


class OneServiceListingTwiceTests(_Converted):

    def test_the_earliest_instant_is_the_listing_and_the_next_a_repeat(self):
        row = self.episode(K3, "S1E2")
        self.assertEqual(row["simkl_air_ts"], at("2026-10-10"))
        repeats = json.loads(row["simkl_repeats_json"])
        self.assertEqual([(r["at"], r["calendars"]) for r in repeats],
                         [(at("2026-10-17"), ENDPOINTS["shows"].bit)])

    def test_each_instant_carries_only_the_calendars_it_is_on(self):
        """An hour apart on Season Premieres and All Episodes: each instant draws
        on its own calendar, never the other's."""
        row = self.episode(K3, "S1E1")
        self.assertEqual((row["simkl_air_ts"], row["simkl_calendars"]),
                         (at("2026-10-03"), ENDPOINTS["shows/premieres"].bit))
        (repeat,) = json.loads(row["simkl_repeats_json"])
        self.assertEqual((repeat["at"], repeat["calendars"]),
                         (at("2026-10-03", 21), ENDPOINTS["shows"].bit))

    def test_one_instant_on_two_calendars_is_one_instant_with_both_bits(self):
        row = self.episode(K1, "S2E1")
        self.assertEqual(row["trakt_calendars"],
                         ENDPOINTS["shows"].bit | ENDPOINTS["shows/premieres"].bit)
        self.assertEqual(row["trakt_repeats_json"], "[]")
        self.assertEqual(row["trakt_label"], "S02E01")

    def test_a_film_listed_twice_keeps_both_dates(self):
        row = self.episode(K4, "film")
        self.assertEqual(row["simkl_air_ts"], at("2026-11-04", 0))
        self.assertEqual([r["at"] for r in json.loads(row["simkl_repeats_json"])],
                         [at("2026-12-25", 0)])
        self.assertEqual(row["tmdb_air_ts"], at("2026-11-04", 0))
        # Its own entry's: a repeat names an entry only where it is another one.
        self.assertIsNone(json.loads(row["simkl_repeats_json"])[0]["entry"])


class SearchedListingsTests(_Converted):

    def test_a_searched_listing_its_feed_lists_stands_down(self):
        row = self.episode(K5, "S1E1")
        self.assertEqual((row["trakt_air_ts"], row["trakt_searched"], row["trakt_repeats_json"]),
                         (at("2026-10-05", 7), 0, "[]"))

    def test_a_searched_listing_no_feed_lists_is_kept_and_marked(self):
        row = self.episode(K5, "S1E2")
        bit = ENDPOINTS["shows/new"].bit
        self.assertEqual((row["trakt_calendars"], row["trakt_searched"]), (bit, bit))


class WhichTitleTests(_Converted):

    def test_a_listing_goes_under_its_title_rows_key(self):
        self.assertEqual(self.units(K6), ["S1E1"])
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM episodes WHERE title_key = 'show:tvdb:66'").fetchone()[0], 0)

    def test_the_identity_is_carried_and_a_title_it_lacked_starts_empty(self):
        row = self.title(K1)
        self.assertEqual((json.loads(row["ids_json"]), row["created_at"]),
                         ({"tmdb": 100, "tvdb": 9}, 5))
        row = self.title(K2)
        self.assertEqual((row["media"], row["ids_json"], row["created_at"]), ("show", "{}", NOW))


class DescriptionsTests(_Converted):

    def test_a_title_groups_words_are_copied(self):
        row = self.title(K1)
        self.assertEqual((row["tmdb_title"], row["tmdb_keywords_json"], row["tmdb_show_type"],
                          row["tmdb_facts_known"], row["tmdb_cast_json"]),
                         ("The Show", '["anime"]', "scripted", 1, '[{"name": "A"}]'))
        self.assertIsNone(row["tmdb_popularity"])

    def test_a_service_with_no_entry_has_no_group(self):
        row = self.title(K3)
        self.assertIsNone(row["tmdb_id"])
        self.assertIsNone(row["tmdb_fetched_at"])
        self.assertEqual((row["tmdb_title"], row["tmdb_genres_json"]), ("", "[]"))

    def test_an_episodes_description_is_copied_beside_its_listings(self):
        row = self.episode(K1, "S2E1")
        self.assertEqual((row["trakt_title"], row["trakt_runtime"], row["trakt_rating"],
                          row["trakt_votes"], row["trakt_episode_type"], row["trakt_fetched_at"],
                          row["trakt_stale_after"]),
                         ("Premiere", 45, 8.1, 100, "season_premiere", 100, 200))

    def test_a_seasons_description_is_copied(self):
        row = self.conn.execute("SELECT * FROM seasons WHERE title_key = ? AND season = 2",
                                (K1,)).fetchone()
        self.assertEqual((row["tmdb_name"], row["tmdb_episode_count"], row["tmdb_poster"],
                          row["tmdb_fetched_at"]), ("Season Two", 10, "/s2.jpg", 100))
        self.assertIsNone(row["simkl_id"])
        self.assertEqual(row["simkl_named"], 0)


class NothingIsDeletedTests(_Converted):

    def test_the_retired_tables_are_kept_whole(self):
        self.assertEqual(_snapshot(self.conn, RETIRED), self.retired_before)

    def test_the_old_identity_table_is_renamed_aside_whole(self):
        self.assertEqual(_snapshot(self.conn, ("titles_v47",))["titles_v47"],
                         self.identity_before)

    def test_only_the_renamed_tables_id_indexes_went_and_the_new_table_has_them(self):
        on = dict(self.conn.execute("SELECT name, tbl_name FROM sqlite_master WHERE type = 'index'"))
        for space in ("tvdb", "imdb", "mal"):
            self.assertEqual(on[f"ix_titles_{space}"], "titles")
        self.assertEqual(on["ix_calendar_airings_key"], "calendar_airings")
        self.assertEqual(on["ix_title_metadata_fold"], "title_metadata")

    def test_the_conversion_records_when_it_ran(self):
        self.assertEqual(self.conn.execute("SELECT value FROM app_meta WHERE key = ?",
                                           (db.TITLE_STORE_CONVERTED_AT,)).fetchone()[0], str(NOW))

    def test_the_database_is_whole(self):
        self.assertEqual(self.conn.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        self.assertEqual(self.conn.execute("PRAGMA foreign_key_check").fetchall(), [])


class WhatElseItAddsTests(_Converted):

    def test_a_highlight_goes_with_its_owner(self):
        self.conn.execute("INSERT INTO share_highlights (user_id, year, month, title_key, "
                          "position, created_at) VALUES (1, 2026, 11, ?, 0, 1)", (K1,))
        self.conn.execute("DELETE FROM users WHERE id = 1")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM share_highlights").fetchone()[0], 0)

    def test_a_linked_account_can_hold_a_session_and_holds_none_yet(self):
        columns = [c["name"] for c in self.conn.execute("PRAGMA table_info(linked_identities)")]
        self.assertIn("session_token", columns)


class TheTablesTests(_Converted):
    """The tables hold exactly what each provider declares
    (Capabilities.store_columns), and are the shape that was measured best."""

    def columns(self, table):
        return {c["name"] for c in self.conn.execute(f"PRAGMA table_info({table})")}

    def test_each_table_holds_exactly_the_declared_columns(self):
        for table, identity, declared in (
                ("titles", TITLE_IDENTITY, lambda caps: caps.titles),
                ("seasons", SEASON_IDENTITY, lambda caps: caps.seasons),
                ("episodes", EPISODE_IDENTITY, lambda caps: caps.episodes)):
            expected = set(identity)
            for source, provider in providers.registered().items():
                expected |= {f"{source}_{column}"
                             for column in declared(provider.capabilities.store_columns)}
            with self.subTest(table=table):
                self.assertEqual(self.columns(table), expected)

    def test_the_wide_tables_are_rowid_tables_and_the_id_map_is_not(self):
        sql = dict(self.conn.execute("SELECT name, sql FROM sqlite_master WHERE type = 'table'"))
        for table in ("titles", "seasons", "episodes"):
            self.assertNotIn("WITHOUT ROWID", sql[table], table)
        self.assertIn("WITHOUT ROWID", sql["service_ids"])

    def test_the_calendar_bits_are_the_endpoints_and_distinct(self):
        bits = {key: endpoint.bit for key, endpoint in ENDPOINTS.items()}
        self.assertEqual(bits, db._M49_CALENDAR_BITS)
        self.assertEqual(len(set(bits.values())), len(bits))
        for key, bit in bits.items():
            self.assertEqual(bit & (bit - 1), 0, key)


class ARerunTests(unittest.TestCase):

    def test_running_the_migrations_again_changes_nothing(self):
        conn = _connect("title-store-49-rerun.db")
        try:
            with patch.object(db, "MIGRATIONS", _through(48)):
                db.migrate_sync(conn)
            _seed(conn)
            with patch.object(db, "MIGRATIONS", _through(49)):
                self.assertEqual(db.migrate_sync(conn), 49)
                tables = [r[0] for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name")]
                before = _snapshot(conn, tables)
                self.assertEqual(db.migrate_sync(conn), 49)
                self.assertEqual(_snapshot(conn, tables), before)
        finally:
            conn.close()
            (TMP / "title-store-49-rerun.db").unlink(missing_ok=True)


class FromProductionsSchemaTests(unittest.TestCase):

    def test_an_instance_at_46_runs_47_48_and_the_conversion_in_one_start(self):
        """Production holds the per-service rows under their calendar names, and
        47 renames them; its identity table is empty until the start-up
        registration, which settles the titles the conversion created."""
        conn = _connect("title-store-49-from-46.db")
        try:
            with patch.object(db, "MIGRATIONS", _through(46)):
                db.migrate_sync(conn)
            _title(conn, "simkl", "55", "show:tvdb:4321", table="calendar_titles",
                   ids={"simkl": 55, "tvdb": 4321}, title="A Show", network="HBO")
            conn.execute("INSERT INTO calendar_episodes (source, media, source_id, season, number, "
                         "title, fetched_at) VALUES ('simkl', 'show', '55', 1, 2, 'Second', 0)")
            _airing(conn, "simkl", "55", "show:tvdb:4321", "shows", "2026-07-08", season=1, number=2)
            with patch.object(db, "MIGRATIONS", _through(49)):
                self.assertEqual(db.migrate_sync(conn), 49)
            row = conn.execute("SELECT * FROM titles").fetchone()
            self.assertEqual((row["title_key"], row["ids_json"], row["simkl_id"],
                              row["simkl_title"], row["simkl_network"]),
                             ("show:tvdb:4321", "{}", "55", "A Show", "HBO"))
            row = conn.execute("SELECT * FROM episodes").fetchone()
            self.assertEqual((row["unit"], row["simkl_title"], row["simkl_fetched_at"],
                              row["simkl_air_ts"]), ("S1E2", "Second", 0, at("2026-07-08")))
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM titles_v47").fetchone()[0], 0)
            self.assertEqual(conn.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        finally:
            conn.close()
            (TMP / "title-store-49-from-46.db").unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
