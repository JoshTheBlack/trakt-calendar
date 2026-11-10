"""Unit tests for the SQLite foundation (app/db).

Covers the migration runner applying cleanly from empty and being idempotent,
the connection pragmas (foreign key enforcement in particular is ASSERTED rather
than assumed — it is per-connection and defaults off, so every cascade in the
schema is inert without it), and the async helpers' transaction semantics.

No network.
"""
from __future__ import annotations

import asyncio
import json
import re
import threading
import time
import unittest
from unittest.mock import patch

from app import db
from tests.support import APP_DIR, TMP, new_db_path

EXPECTED_TABLES = {
    "users", "user_prefs", "linked_identities", "sessions", "login_attempts",
    "auth_handshakes", "invites", "invite_redemptions", "retired_identifiers",
    "app_meta", "schema_version",
    # Migration 2 — the calendar data model. (calendar_not_watching was folded
    # into not_watching_shows by migration 10 and dropped.)
    "api_cache", "calendar_view_state",
    # Migration 3 — public share links.
    "share_links",
    # Migration 4 — the per-user distrakt tracker data model. (distrakt_shows was
    # split into the two record tables below by migration 19 and dropped.)
    "distrakt_months", "distrakt_watch_state",
    "distrakt_show_progress", "distrakt_movie_watches",
    # Migration 9 / 10 — per-user emoji map, and show-level not-watching.
    "distrakt_prefs", "not_watching_shows",
    # Migration 11 — configuration consolidated out of settings.json.
    "app_secrets", "app_settings",
    # Migration 19 — month facts and viewer facts, stored apart.
    "distrakt_month_records", "distrakt_user_seasons", "distrakt_prompt_dismissals",
    # Migration 22 — which services answer for an account.
    "source_prefs",
}


class DbTestCase(unittest.IsolatedAsyncioTestCase):
    """Each test gets its own database file so nothing leaks between them."""
    async def asyncSetUp(self):
        new_db_path("test")
        await db.migrate()

    async def asyncTearDown(self):
        db.close_thread_connection()


class MigrationTests(DbTestCase):
    async def test_applies_cleanly_from_empty(self):
        names = {r["name"] for r in await db.fetch_all(
            "SELECT name FROM sqlite_master WHERE type = 'table'")}
        self.assertTrue(EXPECTED_TABLES <= names, f"missing: {EXPECTED_TABLES - names}")
        self.assertEqual(await db.schema_version(), max(v for v, _ in db.MIGRATIONS))

    async def test_is_idempotent(self):
        before = await db.schema_version()
        self.assertEqual(await db.migrate(), before)
        self.assertEqual(await db.migrate(), before)
        # Re-running must not have duplicated the single version row.
        self.assertEqual(await db.fetch_value("SELECT COUNT(*) FROM schema_version"), 1)

    async def test_migration_12_adds_certification_columns_to_user_prefs(self):
        """A fresh install gets the columns via CREATE TABLE; an upgrading one
        gets them via the ALTER TABLE migration. Both must default to '' so an
        existing row with no opinion on certification keeps behaving as
        unfiltered, the same way genres/countries already do."""
        now = db.now()
        await db.execute(
            "INSERT INTO users (username, created_at, updated_at) VALUES ('cert-user', ?, ?)",
            (now, now))
        user_id = await db.fetch_value("SELECT id FROM users WHERE username = 'cert-user'")
        await db.execute(
            "INSERT INTO user_prefs (user_id, endpoint, card_style, day_packing) "
            "VALUES (?, 'shows/new', 'vertical', 'stacked')", (user_id,))
        row = await db.fetch_one(
            "SELECT show_certifications, movie_certifications FROM user_prefs WHERE user_id = ?",
            (user_id,))
        self.assertEqual(row["show_certifications"], "")
        self.assertEqual(row["movie_certifications"], "")

    async def test_only_one_bootstrap_account_can_exist(self):
        """The database half of the first-run race guard."""
        now = db.now()
        await db.execute(
            "INSERT INTO users (username, is_bootstrap, created_at, updated_at) "
            "VALUES ('one', 1, ?, ?)", (now, now))
        with self.assertRaises(db.IntegrityError):
            await db.execute(
                "INSERT INTO users (username, is_bootstrap, created_at, updated_at) "
                "VALUES ('two', 1, ?, ?)", (now, now))
        # The index is partial: ordinary accounts are unconstrained.
        await db.execute(
            "INSERT INTO users (username, is_bootstrap, created_at, updated_at) "
            "VALUES ('three', 0, ?, ?)", (now, now))
        await db.execute(
            "INSERT INTO users (username, is_bootstrap, created_at, updated_at) "
            "VALUES ('four', 0, ?, ?)", (now, now))

    async def test_migration_10_folds_per_view_marks_into_one_per_show(self):
        """An upgrading instance keeps every not-watching mark it had, and the
        same show marked in three different views becomes one row carrying the
        EARLIEST of the three timestamps — the moment the user first said it."""
        import sqlite3

        from unittest.mock import patch

        path = TMP / "fold-test.db"
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        conn.isolation_level = None  # the runner issues its own BEGIN IMMEDIATE
        try:
            with patch.object(db, "MIGRATIONS", [m for m in db.MIGRATIONS if m[0] <= 9]):
                db.migrate_sync(conn)
            now = db.now()
            conn.execute(
                "INSERT INTO users (id, username, created_at, updated_at) "
                "VALUES (1, 'operator', ?, ?)", (now, now))
            for endpoint, month, item, ts in (
                ("shows/new", 7, "the-show", now + 50),
                ("shows", 7, "the-show", now + 10),
                ("shows/premieres", 8, "the-show", now + 30),
                ("shows/new", 7, "other-show", now),
            ):
                conn.execute(
                    "INSERT INTO calendar_not_watching "
                    "(user_id, endpoint, year, month, item_id, created_at) "
                    "VALUES (1, ?, 2026, ?, ?, ?)", (endpoint, month, item, ts))
            conn.commit()

            db.migrate_sync(conn)

            rows = {r["item_id"]: r["created_at"] for r in
                    conn.execute("SELECT item_id, created_at FROM not_watching_shows")}
            self.assertEqual(rows, {"the-show": now + 10, "other-show": now})
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM sqlite_master WHERE type = 'table' "
                             "AND name = 'calendar_not_watching'").fetchone()[0], 0)
        finally:
            conn.close()

    async def test_username_is_case_insensitive(self):
        """Without NOCASE, `Admin` and `admin` would be two separate accounts."""
        now = db.now()
        await db.execute(
            "INSERT INTO users (username, created_at, updated_at) VALUES ('admin', ?, ?)",
            (now, now))
        with self.assertRaises(db.IntegrityError):
            await db.execute(
                "INSERT INTO users (username, created_at, updated_at) VALUES ('ADMIN', ?, ?)",
                (now, now))

    async def test_migration_14_widens_key_type_and_keeps_existing_rows(self):
        """The rebuild that adds 'ranker_search'/'ranker_export' must carry an
        in-progress lockout across, the same guarantee migration 7 made for
        'handshake_ip' — a rebuild that silently forgot one would be a way to
        clear it."""
        import sqlite3

        from unittest.mock import patch

        path = TMP / "migration-14-test.db"
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        conn.isolation_level = None
        try:
            with patch.object(db, "MIGRATIONS", [m for m in db.MIGRATIONS if m[0] <= 13]):
                db.migrate_sync(conn)
            now = db.now()
            conn.execute(
                "INSERT INTO login_attempts (key_type, key_value, attempted_at, succeeded) "
                "VALUES ('ip', '1.2.3.4', ?, 0)", (now,))
            conn.commit()

            db.migrate_sync(conn)

            rows = conn.execute(
                "SELECT key_type, key_value, attempted_at FROM login_attempts").fetchall()
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["key_type"], "ip")
            self.assertEqual(rows[0]["key_value"], "1.2.3.4")
            self.assertEqual(rows[0]["attempted_at"], now)

            conn.execute(
                "INSERT INTO login_attempts (key_type, key_value, attempted_at, succeeded) "
                "VALUES ('ranker_search', '7', ?, 1)", (now,))
            conn.execute(
                "INSERT INTO login_attempts (key_type, key_value, attempted_at, succeeded) "
                "VALUES ('ranker_export', '7', ?, 1)", (now,))
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO login_attempts (key_type, key_value, attempted_at, succeeded) "
                    "VALUES ('bogus_type', '7', ?, 1)", (now,))
        finally:
            conn.close()

    async def test_migration_38_gives_the_show_calendar_todays_filters_and_the_film_one_none(self):
        """The split, over data that already exists.

        `genres` and `countries` were one answer applied to whichever calendar
        was open, and the upgrade has to decide what that answer MEANT. It keeps
        it for shows and starts the film columns empty, which is a real
        behaviour change: an account that filtered genres sees a WIDER film
        calendar the first time it loads one. Copying both ways would look like
        nothing changed while silently asserting that a filter written for shows
        was meant for films too — which is the conflation the split exists to
        end.

        The source selection goes the other way and is copied, because there is
        no "empty" for it: no service showing is the one state the app refuses,
        so the only answer that preserves what somebody chose is their own.
        """
        import sqlite3

        from unittest.mock import patch

        path = TMP / "migration-38-test.db"
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        conn.isolation_level = None
        try:
            with patch.object(db, "MIGRATIONS", [m for m in db.MIGRATIONS if m[0] <= 37]):
                db.migrate_sync(conn)
            now = db.now()
            conn.execute(
                "INSERT INTO users (id, username, created_at, updated_at) "
                "VALUES (1, 'filtered', ?, ?)", (now, now))
            conn.execute(
                "INSERT INTO users (id, username, created_at, updated_at) "
                "VALUES (2, 'unfiltered', ?, ?)", (now, now))
            conn.execute(
                "INSERT INTO user_prefs (user_id, endpoint, card_style, day_packing, "
                "genres, countries, show_certifications, movie_certifications, "
                "network_filter_json) "
                "VALUES (1, 'shows/new', 'vertical', 'grid', '-reality,drama', "
                "'us,-jp', '-TV-MA', '-R', '[\"HBO\"]')")
            conn.execute(
                "INSERT INTO user_prefs (user_id, endpoint, card_style, day_packing) "
                "VALUES (2, 'shows/new', 'vertical', 'grid')")
            conn.execute(
                "INSERT INTO source_prefs (user_id, calendar_source) VALUES (1, 'trakt')")
            conn.commit()

            db.migrate_sync(conn)

            row = conn.execute("SELECT * FROM user_prefs WHERE user_id = 1").fetchone()
            self.assertEqual(row["tv_genres"], "-reality,drama")
            self.assertEqual(row["tv_countries"], "us,-jp")
            self.assertEqual(row["movie_genres"], "")
            self.assertEqual(row["movie_countries"], "")
            # The dimensions that were ALREADY per-medium are untouched — this
            # migration finishes a split the schema had started, it does not
            # redo it.
            self.assertEqual(row["show_certifications"], "-TV-MA")
            self.assertEqual(row["movie_certifications"], "-R")
            self.assertEqual(row["network_filter_json"], '["HBO"]')
            # And the stash starts off, so nobody's calendar changes because a
            # switch they have never seen defaulted the wrong way.
            self.assertEqual(row["filters_paused"], 0)

            blank = conn.execute("SELECT * FROM user_prefs WHERE user_id = 2").fetchone()
            self.assertEqual(blank["tv_genres"], "")
            self.assertEqual(blank["movie_genres"], "")

            sources = conn.execute("SELECT * FROM source_prefs WHERE user_id = 1").fetchone()
            self.assertEqual(sources["calendar_source"], "trakt")
            self.assertEqual(sources["movie_calendar_source"], "trakt")

            # The columns the two specs used to share are gone rather than left
            # behind to be read by mistake.
            columns = {c["name"] for c in conn.execute("PRAGMA table_info(user_prefs)")}
            self.assertNotIn("genres", columns)
            self.assertNotIn("countries", columns)
        finally:
            conn.close()
            path.unlink(missing_ok=True)

    async def test_migration_39_adds_the_repair_log_and_touches_no_existing_row(self):
        """A log whose GROWTH is the alarm, so it must arrive empty.

        The table records where a per-service name was recovered from when a
        stored record turned out not to have one. Rows dated to the first run
        are the historical backlog; a row dated later means a writer is still
        dropping names. That reading only works if the migration itself writes
        nothing — a backfill here would date the whole backlog to the upgrade and
        make the first genuine regression indistinguishable from it.
        """
        import sqlite3

        from unittest.mock import patch

        path = TMP / "migration-39-test.db"
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        conn.isolation_level = None
        try:
            with patch.object(db, "MIGRATIONS", [m for m in db.MIGRATIONS if m[0] <= 38]):
                db.migrate_sync(conn)
            now = db.now()
            conn.execute(
                "INSERT INTO users (id, username, created_at, updated_at) "
                "VALUES (1, 'someone', ?, ?)", (now, now))
            conn.execute(
                "INSERT INTO distrakt_month_records "
                "(user_id, month, kind, media, match_source, match_id, season, "
                " slug, simkl_slug, trakt_id, simkl_id, title, created_at) "
                "VALUES (1, '2026-07', 'premiere', 'show', 'tmdb', '134421', 1, "
                "'the-rookie-2018', 'the-rookie', 134421, 555, 'The Rookie', ?)", (now,))
            conn.commit()

            # Up to 39 and no further: a later migration drops the shared column
            # this reads, and what is under test is what 39 itself did.
            with patch.object(db, "MIGRATIONS", [m for m in db.MIGRATIONS if m[0] <= 39]):
                db.migrate_sync(conn)

            self.assertEqual(
                conn.execute("SELECT COUNT(*) c FROM distrakt_slug_repairs").fetchone()["c"],
                0, "the migration must not date the backlog to the upgrade")
            # The record it will later repair is untouched by the migration
            # itself: recovering a name is the background pass's job, where it
            # can be logged, not a silent UPDATE nobody can audit.
            row = conn.execute("SELECT * FROM distrakt_month_records").fetchone()
            self.assertEqual(row["slug"], "the-rookie-2018")
            self.assertIsNone(row["trakt_slug"])

            columns = {c["name"] for c in
                       conn.execute("PRAGMA table_info(distrakt_slug_repairs)")}
            self.assertEqual(
                columns,
                {"id", "user_id", "media", "match_source", "match_id", "column_name",
                 "value", "evidence", "rows_changed", "title", "repaired_at"})
            indexes = {i["name"] for i in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index' "
                "AND tbl_name='distrakt_slug_repairs'")}
            self.assertIn("ix_distrakt_slug_repairs_at", indexes,
                          "the log is always read by date")
        finally:
            conn.close()
            path.unlink(missing_ok=True)

    def _at_45_with_listed_sets(self, path):
        """A database at schema 45 holding one account, a tracker row with the
        shared slug beside both namespaced ones, and three cached listed sets:
        one readable, one for an account that no longer exists, one that cannot
        be decoded."""
        import sqlite3

        from unittest.mock import patch

        from app import cache

        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        conn.isolation_level = None
        with patch.object(db, "MIGRATIONS", [m for m in db.MIGRATIONS if m[0] <= 45]):
            db.migrate_sync(conn)
        now = db.now()
        conn.execute("INSERT INTO users (id, username, created_at, updated_at) "
                     "VALUES (1, 'someone', ?, ?)", (now, now))
        conn.execute(
            "INSERT INTO distrakt_user_seasons "
            "(user_id, kind, media, match_source, match_id, season, slug, trakt_slug, "
            " simkl_slug, trakt_id, simkl_id, title, created_at) "
            "VALUES (1, 'keepup', 'show', 'tmdb', '900', 1, 'shared', 'a-show-2019', "
            "'a-show', 11, 22, 'A Show', ?)", (now,))
        for key, blob in (("distrakt_listed:v1:1:simkl", cache._encode([22, 5, 7])),
                          ("distrakt_listed:v1:99:simkl", cache._encode([1])),
                          ("distrakt_listed:v1:1:trakt", b"not zlib")):
            conn.execute("INSERT INTO api_cache (cache_key, payload, cached_at, "
                         "ttl_seconds, byte_size) VALUES (?, ?, ?, ?, ?)",
                         (key, blob, now, 400 * 86400, len(blob)))
        # A key that only LOOKS like one under LIKE's wildcard, which must survive.
        conn.execute("INSERT INTO api_cache (cache_key, payload, cached_at, "
                     "ttl_seconds, byte_size) VALUES ('distraktXlisted:v1:1:simkl', "
                     "?, ?, NULL, 1)", (cache._encode([3]), now))
        conn.commit()
        return conn

    async def test_migration_46_moves_every_readable_listed_set_into_its_table(self):
        """What a removal is judged against leaves the evictable cache for a
        table, with every id carried across and the cache rows gone; a set for an
        account that no longer exists, or one that cannot be read, is dropped."""
        path = TMP / "migration-46-listed.db"
        conn = self._at_45_with_listed_sets(path)
        try:
            db.migrate_sync(conn)
            listed = conn.execute(
                "SELECT user_id, source, service_id FROM distrakt_listed_ids "
                "ORDER BY service_id").fetchall()
            self.assertEqual([tuple(r) for r in listed],
                             [(1, "simkl", 5), (1, "simkl", 7), (1, "simkl", 22)])
            left = [r["cache_key"] for r in conn.execute("SELECT cache_key FROM api_cache")]
            self.assertEqual(left, ["distraktXlisted:v1:1:simkl"])
        finally:
            conn.close()
            path.unlink(missing_ok=True)

    async def test_migration_46_drops_the_shared_slug_and_keeps_both_namespaced_ones(self):
        path = TMP / "migration-46-slug.db"
        conn = self._at_45_with_listed_sets(path)
        try:
            db.migrate_sync(conn)
            for table in ("distrakt_user_seasons", "distrakt_month_records",
                          "distrakt_unsettled_rows"):
                columns = {c["name"] for c in conn.execute(f"PRAGMA table_info({table})")}
                self.assertNotIn("slug", columns, table)
                self.assertTrue({"trakt_slug", "simkl_slug"} <= columns, table)
            row = conn.execute("SELECT * FROM distrakt_user_seasons").fetchone()
            self.assertEqual((row["trakt_slug"], row["simkl_slug"], row["title"]),
                             ("a-show-2019", "a-show", "A Show"))
        finally:
            conn.close()
            path.unlink(missing_ok=True)

    async def test_migration_46_adds_the_watchlist_tables_and_they_cascade(self):
        path = TMP / "migration-46-watchlist.db"
        conn = self._at_45_with_listed_sets(path)
        try:
            db.migrate_sync(conn)
            conn.execute("PRAGMA foreign_keys = ON")
            columns = {table: [c["name"] for c in conn.execute(f"PRAGMA table_info({table})")]
                       for table in ("watchlist_targets", "watchlist_writes")}
            self.assertEqual(columns["watchlist_targets"], ["user_id", "source", "enabled_at"])
            self.assertEqual(columns["watchlist_writes"],
                             ["user_id", "source", "match_source", "match_id", "service_id",
                              "status_before", "state", "updated_at"])
            now = db.now()
            conn.execute("INSERT INTO watchlist_targets VALUES (1, 'simkl', ?)", (now,))
            conn.execute("INSERT INTO watchlist_writes VALUES "
                         "(1, 'simkl', 'tmdb', '900', '22', 'none', 'written', ?)", (now,))
            conn.execute("DELETE FROM users WHERE id = 1")
            for table in ("watchlist_targets", "watchlist_writes", "distrakt_listed_ids"):
                self.assertEqual(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0],
                                 0, f"{table} must go with its account")
        finally:
            conn.close()
            path.unlink(missing_ok=True)

    async def test_migration_44_gives_a_board_seasons_and_keeps_every_row_whole(self):
        """tier_items is rebuilt to add `season`, NULL meaning the whole show, and
        its uniqueness moves to an index over IFNULL(season, -1). Every existing
        row must arrive as the whole show with its id, tier and rank; the old
        rule (one whole show per board) must still hold; a season must be able
        to sit beside it; and the foreign keys the rebuild re-declared must
        still act."""
        import sqlite3

        from unittest.mock import patch

        path = TMP / "migration-44-test.db"
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        conn.isolation_level = None
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            with patch.object(db, "MIGRATIONS", [m for m in db.MIGRATIONS if m[0] <= 43]):
                db.migrate_sync(conn)
            now = db.now()
            conn.execute("INSERT INTO users (id, username, created_at, updated_at) "
                         "VALUES (1, 'someone', ?, ?)", (now, now))
            conn.execute("INSERT INTO tier_boards (id, user_id, uid, name, created_at, updated_at) "
                         "VALUES (7, 1, 'b7', 'Shows', ?, ?)", (now, now))
            conn.execute("INSERT INTO tier_categories (id, board_id, uid, label, created_at) "
                         "VALUES (70, 7, 'c70', 'A', ?)", (now,))
            conn.execute(
                "INSERT INTO tier_items (id, board_id, category_id, media, match_source, "
                "match_id, title, season_count, rank_in_category, created_at) VALUES "
                "(501, 7, 70, 'show', 'tmdb', '95396', 'Severance', 2, 3, ?), "
                "(502, 7, NULL, 'movie', 'tmdb', '550', 'Fight Club', NULL, 0, ?)",
                (now, now))
            conn.execute(
                "INSERT INTO tier_item_removals (board_id, media, match_source, match_id, "
                "removed_at, item_json) VALUES (7, 'show', 'tmdb', '1399', ?, '{}')", (now,))
            conn.commit()

            db.migrate_sync(conn)

            rows = {r["id"]: dict(r) for r in conn.execute("SELECT * FROM tier_items")}
            self.assertEqual(set(rows), {501, 502}, "a row was lost or renumbered")
            self.assertEqual((rows[501]["category_id"], rows[501]["rank_in_category"],
                              rows[501]["season_count"], rows[501]["season"]),
                             (70, 3, 2, None), "a ranked row did not arrive whole")
            self.assertIsNone(rows[502]["season"])

            insert = ("INSERT INTO tier_items (board_id, media, match_source, match_id, "
                      "season, created_at) VALUES (7, 'show', 'tmdb', '95396', ?, ?)")
            with self.assertRaises(sqlite3.IntegrityError, msg="the whole show twice"):
                conn.execute(insert, (None, now))
            conn.execute(insert, (2, now))
            conn.execute(insert, (0, now))  # the specials are a season like any other
            with self.assertRaises(sqlite3.IntegrityError, msg="one season twice"):
                conn.execute(insert, (2, now))
            # A bare DO NOTHING is what the board's writer relies on.
            cur = conn.execute(insert + " ON CONFLICT DO NOTHING",
                               (None, now))
            self.assertEqual(cur.rowcount, 0)

            conn.execute("DELETE FROM tier_categories WHERE id = 70")
            self.assertIsNone(conn.execute(
                "SELECT category_id FROM tier_items WHERE id = 501").fetchone()[0],
                "deleting a tier must return its titles to the pool")
            conn.execute("DELETE FROM tier_boards WHERE id = 7")
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM tier_items").fetchone()[0], 0,
                             "deleting a board must take its titles with it")

            indexes = {i["name"] for i in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='tier_items'")}
            self.assertTrue({"ux_tier_items_title", "ix_tier_items_category",
                             "ix_tier_items_pool"} <= indexes, indexes)
            self.assertIn("season", {c["name"] for c in
                                     conn.execute("PRAGMA table_info(tier_item_removals)")})
            self.assertEqual(
                {c["name"] for c in conn.execute("PRAGMA table_info(season_posters)")},
                {"media", "tmdb", "season", "source", "url", "first_seen_at", "last_seen_at",
                 "last_ok_at", "last_failed_at", "fail_count"})
            self.assertIn("covered_json", {c["name"] for c in conn.execute(
                "PRAGMA table_info(distrakt_prompt_dismissals)")})
            self.assertEqual(conn.execute("PRAGMA foreign_key_check").fetchall(), [])
        finally:
            conn.close()
            path.unlink(missing_ok=True)

    async def test_migration_45_names_a_season_row_and_leaves_every_row_unnamed(self):
        """tier_items gains `season_name`, NULL meaning no name of its own. Every
        existing row — a whole show, a season and a film — keeps its id, season,
        tier and counts and arrives unnamed."""
        import sqlite3

        from unittest.mock import patch

        path = TMP / "migration-45-test.db"
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        conn.isolation_level = None
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            with patch.object(db, "MIGRATIONS", [m for m in db.MIGRATIONS if m[0] <= 44]):
                db.migrate_sync(conn)
            now = db.now()
            conn.execute("INSERT INTO users (id, username, created_at, updated_at) "
                         "VALUES (1, 'someone', ?, ?)", (now, now))
            conn.execute("INSERT INTO tier_boards (id, user_id, uid, name, created_at, updated_at) "
                         "VALUES (7, 1, 'b7', 'Shows', ?, ?)", (now, now))
            conn.execute(
                "INSERT INTO tier_items (id, board_id, media, match_source, match_id, season, "
                "title, episode_count, rank_in_category, created_at) VALUES "
                "(1, 7, 'show', 'tmdb', '31911', NULL, 'True Detective', 30, 0, ?), "
                "(2, 7, 'show', 'tmdb', '31911', 4, 'True Detective', 6, 1, ?), "
                "(3, 7, 'movie', 'tmdb', '550', NULL, 'Fight Club', NULL, 2, ?)",
                (now, now, now))
            before = [dict(r) for r in conn.execute("SELECT * FROM tier_items ORDER BY id")]

            db.migrate_sync(conn)

            after = [dict(r) for r in conn.execute("SELECT * FROM tier_items ORDER BY id")]
            self.assertEqual([row.pop("season_name") for row in after], [None, None, None])
            self.assertEqual(after, before, "a row changed on the way through")
            conn.execute("UPDATE tier_items SET season_name = 'Night Country' WHERE id = 2")
            self.assertEqual(conn.execute(
                "SELECT season_name FROM tier_items WHERE id = 2").fetchone()[0], "Night Country")
        finally:
            conn.close()
            path.unlink(missing_ok=True)

    def _at_version_with_titles(self, path, version: int):
        """A database at schema `version` holding one service's title row, one
        episode row and the airing that names them — the shape every instance
        upgrading to 47 has."""
        import sqlite3

        from unittest.mock import patch

        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        conn.isolation_level = None
        conn.execute("PRAGMA foreign_keys=ON")
        with patch.object(db, "MIGRATIONS", [m for m in db.MIGRATIONS if m[0] <= version]):
            db.migrate_sync(conn)
        conn.execute(
            "INSERT INTO calendar_titles (source, media, source_id, title_key, title, ids_json, "
            "network, enriched, fetched_at, stale_after) VALUES ('simkl', 'show', '55', "
            "'show:tvdb:4321', 'A Show', '{\"simkl\": 55, \"tvdb\": 4321}', 'HBO', 1, 10, 20)")
        conn.execute(
            "INSERT INTO calendar_episodes (source, media, source_id, season, number, title, "
            "fetched_at, stale_after) VALUES ('simkl', 'show', '55', 1, 2, 'Second', 10, 20)")
        conn.execute(
            "INSERT INTO calendar_airings (source, media, source_id, endpoint, title_key, air_ts, "
            "air_date, season, episode_number, stored_at) VALUES ('simkl', 'show', '55', "
            "'shows', 'show:tvdb:4321', 1783540800, '2026-07-08', 1, 2, 10)")
        return conn

    def _assert_the_store_after_47(self, conn):
        title = dict(conn.execute("SELECT * FROM title_metadata").fetchone())
        self.assertEqual((title["title_key"], title["title"], title["network"], title["enriched"]),
                         ("show:tvdb:4321", "A Show", "HBO", 1))
        # The columns later readers need, empty until their writers exist.
        self.assertEqual((title["keywords_json"], title["cast_json"], title["videos_json"],
                          title["collection_json"], title["next_episode_json"],
                          title["first_aired"], title["logo"], title["season_count"]),
                         ("[]", "[]", "[]", "{}", "{}", "", "", None))
        self.assertEqual(conn.execute("SELECT title FROM episode_metadata").fetchone()[0], "Second")
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertFalse({"calendar_titles", "calendar_episodes"} & tables)
        # 47's identity table is kept aside by 49 (`titles_v47`), still empty.
        self.assertTrue({"titles_v47", "title_aliases", "season_metadata", "metadata_refresh",
                         "title_posters", "network_logos"} <= tables)
        for table in ("titles_v47", "title_aliases", "season_metadata", "metadata_refresh",
                      "title_posters", "network_logos"):
            self.assertEqual(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0], 0, table)
        # And 49 made the service's row its title's row in the one store.
        self.assertEqual(tuple(conn.execute(
            "SELECT title_key, simkl_title, simkl_network FROM titles").fetchone()),
            ("show:tvdb:4321", "A Show", "HBO"))
        indexes = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")}
        self.assertTrue({"ix_title_metadata_fold", "ix_title_metadata_key",
                         "ix_title_metadata_due", "ix_title_metadata_fetched",
                         "ix_episode_metadata_due", "ix_episode_metadata_fetched",
                         "ix_titles_tvdb", "ix_titles_imdb", "ix_titles_mal"} <= indexes)
        self.assertFalse([name for name in indexes if "calendar_titles" in name
                          or "calendar_episodes" in name])
        # The airing still joins its title and episode under the new names.
        joined = conn.execute(
            "SELECT t.title, e.title FROM calendar_airings a "
            "JOIN title_metadata t ON t.source = a.source AND t.source_id = a.source_id "
            "JOIN episode_metadata e ON e.source = a.source AND e.source_id = a.source_id "
            "AND e.season = a.season AND e.number = a.episode_number").fetchone()
        self.assertEqual(tuple(joined), ("A Show", "Second"))
        self.assertIn("season_totals_order_json",
                      [c[1] for c in conn.execute("PRAGMA table_info(source_prefs)")])
        # Every later migration ran after it, the way an upgrading instance's do.
        self.assertEqual(conn.execute("SELECT version FROM schema_version").fetchone()[0],
                         max(version for version, _step in db.MIGRATIONS))

    async def test_migration_47_from_the_development_schema_renames_the_store_in_place(self):
        """The development database is at 46: the calendar's title and episode
        rows become the title store's, with every row and every index carried."""
        path = TMP / "migration-47-from-46.db"
        conn = self._at_version_with_titles(path, 46)
        try:
            db.migrate_sync(conn)
            self._assert_the_store_after_47(conn)
        finally:
            conn.close()
            path.unlink(missing_ok=True)

    async def test_migration_47_from_the_production_schema_runs_everything_after_it(self):
        """Production lags at 39, so 40-46 run first; the store's rows come
        through all eight unchanged."""
        path = TMP / "migration-47-from-39.db"
        conn = self._at_version_with_titles(path, 39)
        try:
            db.migrate_sync(conn)
            self._assert_the_store_after_47(conn)
            self.assertEqual(conn.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        finally:
            conn.close()
            path.unlink(missing_ok=True)

    # The columns migration 48 adds, with the value every existing row reads.
    _AFTER_48 = {
        "user_prefs": {"tv_languages": "", "movie_languages": "", "tv_keywords_json": "[]",
                       "movie_keywords_json": "[]", "tv_show_types": "",
                       "tv_single_service": 0, "movie_single_service": 0,
                       "tv_sparse_no_network": 0, "tv_missing": "", "movie_missing": ""},
        "title_metadata": {"show_type": "", "facts_known": 0},
    }

    def _assert_after_48(self, conn):
        for table, columns in self._AFTER_48.items():
            declared = {c[1]: c for c in conn.execute(f"PRAGMA table_info({table})")}
            for column, value in columns.items():
                with self.subTest(table=table, column=column):
                    self.assertIn(column, declared)
                    self.assertEqual(declared[column][3], 1, "NOT NULL")
                    row = conn.execute(f"SELECT {column} FROM {table}").fetchone()
                    self.assertEqual(row[0], value)

    async def test_migration_48_adds_the_filter_columns_and_keeps_every_row(self):
        """Additive only: a viewer's row and a title row come through untouched,
        each new column reading the value that means "nothing narrowed" / "not
        described". From the development schema before the store (46) and from
        production's (39), so 47 and everything before it run first."""
        for version in (46, 39):
            with self.subTest(version=version):
                path = TMP / f"migration-48-from-{version}.db"
                conn = self._at_version_with_titles(path, version)
                try:
                    conn.execute("INSERT INTO users (username, created_at, updated_at) "
                                 "VALUES ('viewer', 1, 1)")
                    user_id = conn.execute("SELECT id FROM users").fetchone()[0]
                    conn.execute("INSERT INTO user_prefs (user_id, endpoint, card_style, "
                                 "day_packing, tv_genres) VALUES (?, 'shows/new', 'vertical', "
                                 "'stacked', ?)", (user_id, "-anime"))
                    db.migrate_sync(conn)
                    self._assert_after_48(conn)
                    self.assertEqual(conn.execute("SELECT tv_genres FROM user_prefs").fetchone()[0],
                                     "-anime")
                    self.assertEqual(conn.execute("SELECT network FROM title_metadata")
                                     .fetchone()[0], "HBO")
                    self.assertEqual(conn.execute("PRAGMA integrity_check").fetchone()[0], "ok")
                finally:
                    conn.close()
                    path.unlink(missing_ok=True)

    async def test_the_identity_indexes_find_an_id_only_by_its_stored_type(self):
        """Why the store keeps ids canonical: SQLite never finds the text '4321'
        equal to the integer 4321, so an index lookup is only as good as the type
        it was written in."""
        await db.execute("INSERT INTO titles (title_key, media, ids_json, created_at) "
                         "VALUES ('show:tmdb:1', 'show', '{\"tmdb\": 1, \"tvdb\": 4321}', 0)")
        found = await db.fetch_value("SELECT COUNT(*) FROM titles WHERE media = 'show' AND "
                                     "json_extract(ids_json, '$.tvdb') = ?", (4321,))
        missed = await db.fetch_value("SELECT COUNT(*) FROM titles WHERE media = 'show' AND "
                                      "json_extract(ids_json, '$.tvdb') = ?", ("4321",))
        self.assertEqual((found, missed), (1, 0))
        plan = " ".join(r["detail"] for r in await db.fetch_all(
            "EXPLAIN QUERY PLAN SELECT title_key FROM titles WHERE media = ? AND "
            "json_extract(ids_json, '$.tvdb') = ?", ("show", 1)))
        self.assertIn("ix_titles_tvdb", plan)

    async def test_migration_21_opens_the_provider_column_and_keeps_every_row(self):
        """Both provider tables carried `CHECK (provider IN ('plex','trakt'))`,
        which made admitting a third service a table rebuild.

        Two things have to hold at once. The name set must genuinely be open
        afterwards — that is the whole point of the rebuild — and every existing
        row must come across, because a linked identity is how somebody signs in
        and an account created through a provider has no password to fall back
        on. `purpose` stays constrained: that set is this app's own and really
        is closed.
        """
        import sqlite3

        from unittest.mock import patch

        path = TMP / "migration-21-test.db"
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        conn.isolation_level = None
        try:
            with patch.object(db, "MIGRATIONS", [m for m in db.MIGRATIONS if m[0] <= 20]):
                db.migrate_sync(conn)
            now = db.now()
            conn.execute(
                "INSERT INTO users (id, username, created_at, updated_at) "
                "VALUES (1, 'viewer', ?, ?)", (now, now))
            conn.execute(
                "INSERT INTO linked_identities (user_id, provider, provider_user_id, "
                "display_name, access_token, created_at, last_login_at) "
                "VALUES (1, 'trakt', 'uuid-1', 'Viewer', 'tok', ?, ?)", (now, now))
            conn.execute(
                "INSERT INTO auth_handshakes (state, provider, purpose, created_at, expires_at) "
                "VALUES ('st-1', 'plex', 'login', ?, ?)", (now, now + 600))
            # The constraint this migration exists to remove, still in force.
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO linked_identities (user_id, provider, provider_user_id, "
                    "created_at) VALUES (1, 'simkl', 'acct-9', ?)", (now,))
            conn.commit()

            db.migrate_sync(conn)

            identity = conn.execute("SELECT * FROM linked_identities").fetchall()
            self.assertEqual(len(identity), 1)
            self.assertEqual(identity[0]["provider"], "trakt")
            self.assertEqual(identity[0]["provider_user_id"], "uuid-1")
            self.assertEqual(identity[0]["display_name"], "Viewer")
            self.assertEqual(identity[0]["access_token"], "tok")
            self.assertEqual(identity[0]["created_at"], now)
            handshake = conn.execute("SELECT * FROM auth_handshakes").fetchall()
            self.assertEqual(len(handshake), 1)
            self.assertEqual(handshake[0]["state"], "st-1")
            self.assertEqual(handshake[0]["provider"], "plex")

            # A third service now writes to both tables with no schema change.
            conn.execute(
                "INSERT INTO linked_identities (user_id, provider, provider_user_id, "
                "created_at) VALUES (1, 'simkl', 'acct-9', ?)", (now,))
            conn.execute(
                "INSERT INTO auth_handshakes (state, provider, purpose, created_at, expires_at) "
                "VALUES ('st-2', 'simkl', 'link', ?, ?)", (now, now + 600))
            # ...but the identity is still unique per (provider, account).
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO linked_identities (user_id, provider, provider_user_id, "
                    "created_at) VALUES (1, 'simkl', 'acct-9', ?)", (now,))
            # ...and `purpose` is still a closed set, because that one is ours.
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO auth_handshakes (state, provider, purpose, created_at, "
                    "expires_at) VALUES ('st-3', 'simkl', 'sideways', ?, ?)", (now, now + 600))

            # The rebuild drops the old table's indexes with it. A missing one
            # here is invisible until every account page full-scans.
            indexes = {r["name"] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index'")}
            self.assertIn("ix_linked_identities_user", indexes)
            self.assertIn("ix_auth_handshakes_expires", indexes)
        finally:
            conn.close()

    async def test_migration_21_refuses_rather_than_dropping_an_orphaned_identity(self):
        """An identity row whose account is gone cannot be carried across, and
        the migration says which rows and why instead of failing on a bare
        foreign key error — or, far worse, quietly copying fewer rows than it
        found. Nothing is changed when it refuses."""
        import sqlite3

        from unittest.mock import patch

        path = TMP / "migration-21-orphan-test.db"
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        conn.isolation_level = None
        try:
            with patch.object(db, "MIGRATIONS", [m for m in db.MIGRATIONS if m[0] <= 20]):
                db.migrate_sync(conn)
            now = db.now()
            # Written with the enforcement off, which is how a hand-repaired or
            # partially restored database acquires one in the first place.
            conn.execute("PRAGMA foreign_keys = OFF")
            conn.execute(
                "INSERT INTO linked_identities (user_id, provider, provider_user_id, created_at) "
                "VALUES (404, 'trakt', 'uuid-orphan', ?)", (now,))
            conn.commit()
            conn.execute("PRAGMA foreign_keys = ON")

            with self.assertRaises(RuntimeError) as caught:
                db.migrate_sync(conn)
            self.assertIn("no longer exists", str(caught.exception))

            # Rolled back whole: the row is still there and so is the old version.
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM linked_identities").fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT version FROM schema_version").fetchone()[0], 20)
        finally:
            conn.close()


    async def test_migration_22_gives_the_tracker_s_cached_rows_a_source(self):
        """The two cache tables gain `source` INSIDE their primary key, so two
        services' answers about one season are two rows.

        Everything already stored came from the one service that has ever written
        these tables, and the migration must say so on every row: a progress row
        that came out with no source, or with the wrong one, is somebody's viewing
        history filed under a service that never reported it. The rebuild is also
        the moment rows can silently vanish, which is why the counts are checked
        rather than the shape alone.
        """
        import sqlite3

        from unittest.mock import patch

        path = TMP / "migration-22-test.db"
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        conn.isolation_level = None
        try:
            with patch.object(db, "MIGRATIONS", [m for m in db.MIGRATIONS if m[0] <= 21]):
                db.migrate_sync(conn)
            now = db.now()
            conn.execute(
                "INSERT INTO users (id, username, created_at, updated_at) "
                "VALUES (1, 'viewer', ?, ?)", (now, now))
            conn.execute(
                "INSERT INTO distrakt_show_progress (user_id, media, match_source, "
                "match_id, season, watched_episodes_json, trakt_id) "
                "VALUES (1, 'show', 'tmdb', '1399', 2, '{\"1\": \"2026-03-04\"}', 601)")
            conn.execute(
                "INSERT INTO distrakt_movie_watches (user_id, media, match_source, "
                "match_id, watched_at, title, year, trakt_id) "
                "VALUES (1, 'movie', 'tmdb', '55', '2026-03-04T00:00:00Z', 'A Film', 1999, 55)")
            conn.execute(
                "INSERT INTO distrakt_watch_state (user_id, last_synced, beacons_json) "
                "VALUES (1, '2026-03-20', '{\"ep_watched\": \"2026-03-20T00:00:00Z\"}')")
            conn.commit()

            db.migrate_sync(conn)

            progress = conn.execute("SELECT * FROM distrakt_show_progress").fetchall()
            self.assertEqual(len(progress), 1)
            self.assertEqual(progress[0]["source"], "trakt")
            self.assertEqual(progress[0]["match_id"], "1399")
            self.assertEqual(progress[0]["watched_episodes_json"], '{"1": "2026-03-04"}')
            self.assertEqual(progress[0]["trakt_id"], 601)
            movie = conn.execute("SELECT * FROM distrakt_movie_watches").fetchone()
            self.assertEqual(movie["source"], "trakt")
            self.assertEqual(movie["title"], "A Film")
            self.assertEqual(movie["watched_at"], "2026-03-04T00:00:00Z")

            # The cursor and the beacon are nested under the service that set them.
            state = conn.execute("SELECT * FROM distrakt_watch_state").fetchone()
            self.assertEqual(json.loads(state["cursors_json"]), {"trakt": "2026-03-20"})
            self.assertEqual(json.loads(state["beacons_json"]),
                             {"trakt": {"ep_watched": "2026-03-20T00:00:00Z"}})
            self.assertNotIn("last_synced", state.keys())

            # A second service's answer about the SAME season is a second row, not
            # a conflict — which is the whole reason `source` is in the key.
            conn.execute(
                "INSERT INTO distrakt_show_progress (user_id, media, match_source, "
                "match_id, season, source, watched_episodes_json, simkl_id) "
                "VALUES (1, 'show', 'tmdb', '1399', 2, 'simkl', '{\"1\": \"\"}', 77)")
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM distrakt_show_progress").fetchone()[0], 2)
            # ...but one service still cannot say two things about it.
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO distrakt_show_progress (user_id, media, match_source, "
                    "match_id, season, source, watched_episodes_json) "
                    "VALUES (1, 'show', 'tmdb', '1399', 2, 'simkl', '{}')")

            # The frozen-month columns are additive: the single numbers stay.
            columns = {r["name"] for r in conn.execute(
                "PRAGMA table_info(distrakt_month_records)")}
            self.assertTrue({"watched", "total", "watched_by_source", "total_by_source"}
                            <= columns)
        finally:
            conn.close()

    async def test_migration_22_carries_a_beacon_it_cannot_read_no_further(self):
        """A stored beacon is a cache of one service's "something changed" marker.
        One that will not parse cannot be nested by source, and the honest answer
        is to drop it — the next tracker load fetches a fresh one — rather than
        wrap an unreadable string in a shape the loader would have to guess about.
        Nothing about what was watched lives in that column."""
        import sqlite3

        from unittest.mock import patch

        path = TMP / "migration-22-beacon-test.db"
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        conn.isolation_level = None
        try:
            with patch.object(db, "MIGRATIONS", [m for m in db.MIGRATIONS if m[0] <= 21]):
                db.migrate_sync(conn)
            now = db.now()
            conn.execute(
                "INSERT INTO users (id, username, created_at, updated_at) "
                "VALUES (1, 'viewer', ?, ?)", (now, now))
            conn.execute(
                "INSERT INTO distrakt_watch_state (user_id, last_synced, beacons_json) "
                "VALUES (1, NULL, 'not json at all')")
            conn.commit()

            db.migrate_sync(conn)

            state = conn.execute("SELECT * FROM distrakt_watch_state").fetchall()
            # The ROW survives — it is one per user and dropping it would be a
            # different thing entirely — with both columns empty.
            self.assertEqual(len(state), 1)
            self.assertIsNone(state[0]["cursors_json"])
            self.assertIsNone(state[0]["beacons_json"])
        finally:
            conn.close()

    async def test_migration_36_carries_the_stated_order_out_of_the_precedence_map(self):
        """The one thing migration 36 is not free to drop.

        `precedence_json` held a per-FIELD map AND a `default` — and the default
        is exactly what the new column means, so a stated one has to survive. The
        rest of the document does not: naming a service for the overview and
        another for the poster was a question with a screen of its own and almost
        no askers.

        A BARE STRING IS A ONE-ELEMENT ORDER, which is how that document was
        written before an order was possible; a document naming only fields
        stated no default at all and correctly comes out empty. Verified against
        a copy of a real database as well as here.
        """
        import sqlite3

        from unittest.mock import patch

        path = TMP / "precedence-carry.db"
        path.unlink(missing_ok=True)
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        conn.isolation_level = None  # the runner issues its own BEGIN IMMEDIATE
        try:
            with patch.object(db, "MIGRATIONS", [m for m in db.MIGRATIONS if m[0] <= 35]):
                db.migrate_sync(conn)
            now = db.now()
            shapes = {
                901: '{"default": "simkl"}',
                902: '{"default": ["simkl", "trakt"]}',
                903: '{"default": "simkl", "fields": {"poster": "trakt"}}',
                904: '{"fields": {"poster": "trakt"}}',
                905: 'not json at all',
                906: '{}',
            }
            for uid, document in shapes.items():
                conn.execute(
                    "INSERT INTO users (id, username, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?)", (uid, f"seed{uid}", now, now))
                conn.execute(
                    "INSERT INTO source_prefs (user_id, calendar_source, tracker_source, "
                    "precedence_json, endpoint_sources_json) "
                    "VALUES (?, 'trakt+simkl', 'auto', ?, '{\"movies\": \"trakt\"}')",
                    (uid, document))

            db.migrate_sync(conn)

            carried = {r["user_id"]: json.loads(r["metadata_order_json"]) for r in
                       conn.execute("SELECT user_id, metadata_order_json FROM source_prefs")}
            self.assertEqual(carried, {
                901: ["simkl"],          # a bare name is a one-element order
                902: ["simkl", "trakt"],  # an order survives whole
                903: ["simkl"],          # the default carries, the field map does not
                904: [],                 # fields only stated no default
                905: [],                 # unreadable degrades rather than raising
                906: [],
            })
            # WHICH SERVICES SHOW IS UNTOUCHED, and it is the one preference on
            # this row that people actually state.
            self.assertEqual(
                {r["calendar_source"] for r in
                 conn.execute("SELECT calendar_source FROM source_prefs")},
                {"trakt+simkl"})
            columns = {r["name"] for r in conn.execute("PRAGMA table_info(source_prefs)")}
            self.assertNotIn("precedence_json", columns)
            self.assertNotIn("tracker_source", columns)
            self.assertNotIn("endpoint_sources_json", columns)
        finally:
            conn.close()

    async def test_migration_22_source_prefs_default_to_having_no_opinion(self):
        """An account that has never stated a preference shows every service and
        has no metadata order, so the app's declared order stands. The row is
        only written when somebody states something, so the defaults are what
        almost every account will ever read."""
        now = db.now()
        await db.execute(
            "INSERT INTO users (username, created_at, updated_at) "
            "VALUES ('prefs-user', ?, ?)", (now, now))
        user_id = await db.fetch_value("SELECT id FROM users WHERE username = 'prefs-user'")
        await db.execute("INSERT INTO source_prefs (user_id) VALUES (?)", (user_id,))
        row = await db.fetch_one("SELECT * FROM source_prefs WHERE user_id = ?", (user_id,))
        self.assertEqual(row["calendar_source"], "auto")
        self.assertEqual(row["metadata_order_json"], "[]")

        # It belongs to the account and goes with it.
        await db.execute("DELETE FROM users WHERE id = ?", (user_id,))
        self.assertEqual(
            await db.fetch_value("SELECT COUNT(*) FROM source_prefs WHERE user_id = ?",
                                 (user_id,)), 0)


    async def test_migration_23_adds_a_place_for_the_play_count_sweep(self):
        """The column is additive and NULLABLE, and NULL is what every existing
        row gets — which reads as "no sweep has been stored", falls back to asking
        about every title once, and fills itself in on that pass. Nothing about
        what was watched lives here: it is a cache of one service's per-title play
        counts, kept only to work out which of them have moved."""
        import sqlite3

        from unittest.mock import patch

        path = TMP / "migration-23-test.db"
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        conn.isolation_level = None
        try:
            with patch.object(db, "MIGRATIONS", [m for m in db.MIGRATIONS if m[0] <= 22]):
                db.migrate_sync(conn)
            now = db.now()
            conn.execute(
                "INSERT INTO users (id, username, created_at, updated_at) "
                "VALUES (1, 'viewer', ?, ?)", (now, now))
            conn.execute(
                "INSERT INTO distrakt_watch_state (user_id, cursors_json, beacons_json) "
                "VALUES (1, '{\"trakt\": \"2026-08-01\"}', '{\"trakt\": {}}')")
            conn.commit()

            db.migrate_sync(conn)

            row = conn.execute("SELECT * FROM distrakt_watch_state").fetchone()
            self.assertEqual(row["cursors_json"], '{"trakt": "2026-08-01"}')
            self.assertIsNone(row["play_counts_json"],
                              "an existing row must read as having no stored sweep")
            conn.execute("UPDATE distrakt_watch_state SET play_counts_json = ? "
                         "WHERE user_id = 1", (json.dumps({"trakt": {"202341": 20}}),))
            self.assertEqual(
                json.loads(conn.execute(
                    "SELECT play_counts_json FROM distrakt_watch_state").fetchone()[0]),
                {"trakt": {"202341": 20}})
        finally:
            conn.close()

    async def test_migration_23_leaves_the_tracker_s_export_alone(self):
        """The sweep is deliberately NOT exported. It costs one sweep to rebuild,
        and a map restored beside older progress rows could only claim that
        nothing had changed when everything might have — so a restore starts with
        no stored sweep and asks about every title once, which is correct."""
        from app.distrakt import backup

        columns = dict(backup._EXPORT_TABLES)["distrakt_watch_state"]
        self.assertNotIn("play_counts_json", columns)


    async def test_migration_28_readdresses_a_cached_answer_off_its_credential(self):
        """A stored Simkl answer keyed by the client id that fetched it is
        unreachable the moment that credential changes — and it never leaves on
        its own, because these rows carry no per-row TTL and the size sweep does
        not fire until a gigabyte. So they are re-addressed rather than left, and
        rather than deleted: the payloads are good answers about titles whose
        content never depended on the credential.

        THE KEY IT LANDS ON IS THE ONE THE TRANSPORT WILL ASK FOR, asserted
        against that function rather than against a spelled-out string — a
        migration that produced a plausible-looking key nothing reads would pass
        every test written the other way.
        """
        import sqlite3

        from unittest.mock import patch

        from app.providers.simkl import transport

        path = TMP / "migration-28-test.db"
        path.unlink(missing_ok=True)
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        conn.isolation_level = None
        try:
            with patch.object(db, "MIGRATIONS", [m for m in db.MIGRATIONS if m[0] <= 27]):
                db.migrate_sync(conn)
            now = db.now()
            rows = [
                ("https://api.simkl.com/tv/10009?client_id=abc", b"a"),
                ("https://api.simkl.com/tv/55?extended=full&client_id=abc", b"b"),
                # Two parameters in an order urlencode would not have chosen, so
                # the sort is exercised rather than merely described.
                ("https://api.simkl.com/search/tv?q=silo&extended=full&client_id=abc", b"c"),
                # Not this migration's business, and proof it is not a blanket
                # rewrite: Trakt sends its key as a header, so its URLs carry no
                # credential at all.
                ("https://api.trakt.tv/shows/7?extended=full", b"d"),
            ]
            for key, payload in rows:
                conn.execute(
                    "INSERT INTO api_cache (cache_key, payload, cached_at, ttl_seconds, "
                    "byte_size) VALUES (?, ?, ?, NULL, ?)", (key, payload, now, len(payload)))
            conn.commit()

            db.migrate_sync(conn)

            keys = {r["cache_key"] for r in conn.execute("SELECT cache_key FROM api_cache")}
            self.assertEqual(keys, {
                transport.cache_key("tv/10009"),
                transport.cache_key("tv/55", {"extended": "full"}),
                transport.cache_key("search/tv", {"q": "silo", "extended": "full"}),
                "https://api.trakt.tv/shows/7?extended=full",
            })
            # The ANSWER travelled with the address — a rekey that lost the
            # payload would be a delete wearing a better name.
            self.assertEqual(
                conn.execute("SELECT payload FROM api_cache WHERE cache_key = ?",
                             (transport.cache_key("tv/10009"),)).fetchone()[0], b"a")
        finally:
            conn.close()

    async def test_migration_28_collapses_two_credentials_answers_onto_one_row(self):
        """`cache_key` is the primary key, so an instance that rotated its client
        id holds two rows for one question and they cannot both survive. Both are
        answers to the same public question and neither is more this instance's
        than the other, so the collision resolves rather than raising."""
        import sqlite3

        from unittest.mock import patch

        from app.providers.simkl import transport

        path = TMP / "migration-28-collision-test.db"
        path.unlink(missing_ok=True)
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        conn.isolation_level = None
        try:
            with patch.object(db, "MIGRATIONS", [m for m in db.MIGRATIONS if m[0] <= 27]):
                db.migrate_sync(conn)
            now = db.now()
            for key in ("https://api.simkl.com/tv/10009?client_id=old",
                        "https://api.simkl.com/tv/10009?client_id=new"):
                conn.execute(
                    "INSERT INTO api_cache (cache_key, payload, cached_at, ttl_seconds, "
                    "byte_size) VALUES (?, ?, ?, NULL, 1)", (key, b"x", now))
            conn.commit()

            db.migrate_sync(conn)

            self.assertEqual(
                [r["cache_key"] for r in conn.execute("SELECT cache_key FROM api_cache")],
                [transport.cache_key("tv/10009")])
        finally:
            conn.close()

    async def test_migration_29_gives_a_stored_record_the_id_its_state_knows(self):
        """The live pass writes a service's own id onto the records it renders,
        but it never sees a settled verdict or anything in a frozen month — so
        those keep the gap for ever, and a row nobody can ask about is the whole
        thing the id exists to prevent.

        ADD-ONLY AND THE IDENTITY UNTOUCHED, both asserted: a record already
        naming a service keeps its own value, and gaining an id does not move the
        row (MATCH_SOURCES excludes `simkl` for exactly that reason).
        """
        import sqlite3

        from unittest.mock import patch

        path = TMP / "migration-29-test.db"
        path.unlink(missing_ok=True)
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        conn.isolation_level = None
        try:
            with patch.object(db, "MIGRATIONS", [m for m in db.MIGRATIONS if m[0] <= 28]):
                db.migrate_sync(conn)
            now = db.now()
            conn.execute("INSERT INTO users (id, username, created_at, updated_at) "
                         "VALUES (1, 'viewer', ?, ?)", (now, now))
            conn.execute("INSERT INTO distrakt_watch_state (user_id) VALUES (1)")
            # What the baseline learned and left on the watch state.
            for season in (1, 2):
                conn.execute(
                    "INSERT INTO distrakt_show_progress (user_id, media, match_source, "
                    "match_id, season, source, watched_episodes_json, trakt_id, simkl_id) "
                    "VALUES (1, 'show', 'tmdb', '900', ?, 'simkl', '{}', 111, 222)", (season,))
            conn.execute("INSERT INTO distrakt_months (user_id, month, closed, created_at) "
                         "VALUES (1, '2026-01', 1, ?)", (now,))
            # A settled verdict on a frozen month: the record the live pass never
            # touches, and the reason this migration exists.
            conn.execute(
                "INSERT INTO distrakt_month_records (user_id, month, kind, media, "
                "match_source, match_id, season, trakt_id, simkl_id, tmdb, title, "
                "network, watched, total, started_airing, finished_airing, added_by, "
                "created_at) VALUES (1, '2026-01', 'completed', 'show', 'tmdb', '900', 1, "
                "111, NULL, 900, 'Settled', '', 8, 8, 1, 1, 'manual', ?)", (now,))
            # And one that already names Simkl, with a DIFFERENT id.
            conn.execute(
                "INSERT INTO distrakt_user_seasons (user_id, media, match_source, match_id, "
                "season, kind, trakt_id, simkl_id, tmdb, title, network, watched, total, "
                "started_airing, finished_airing, came_back, added_by, created_at) "
                "VALUES (1, 'show', 'tmdb', '900', 2, 'keepup', 111, 999, 900, 'Listed', "
                "'', 3, 8, 1, 0, 0, 'manual', ?)", (now,))
            conn.commit()

            db.migrate_sync(conn)

            settled = conn.execute(
                "SELECT * FROM distrakt_month_records WHERE month = '2026-01'").fetchone()
            self.assertEqual(settled["simkl_id"], 222)
            # The identity is the WHERE clause, never the SET.
            self.assertEqual(
                (settled["media"], settled["match_source"], settled["match_id"]),
                ("show", "tmdb", "900"))
            self.assertEqual(settled["watched"], 8, "a verdict's counts are not this "
                                                    "migration's to touch")

            listed = conn.execute("SELECT * FROM distrakt_user_seasons").fetchone()
            self.assertEqual(listed["simkl_id"], 999,
                             "an id the record already carried must not be overwritten")
        finally:
            conn.close()

    async def test_migration_29_leaves_a_record_no_state_can_name_alone(self):
        """Silence is not an id. A title no service's library holds has nothing to
        learn, and must come away exactly as it arrived rather than gaining
        something derived from a neighbouring row."""
        import sqlite3

        from unittest.mock import patch

        path = TMP / "migration-29-silent-test.db"
        path.unlink(missing_ok=True)
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        conn.isolation_level = None
        try:
            with patch.object(db, "MIGRATIONS", [m for m in db.MIGRATIONS if m[0] <= 28]):
                db.migrate_sync(conn)
            now = db.now()
            conn.execute("INSERT INTO users (id, username, created_at, updated_at) "
                         "VALUES (1, 'viewer', ?, ?)", (now, now))
            conn.execute(
                "INSERT INTO distrakt_user_seasons (user_id, media, match_source, match_id, "
                "season, kind, trakt_id, simkl_id, tmdb, title, network, watched, total, "
                "started_airing, finished_airing, came_back, added_by, created_at) "
                "VALUES (1, 'show', 'tmdb', '901', 1, 'keepup', 111, NULL, 901, 'Alone', "
                "'', 0, 8, 1, 0, 0, 'manual', ?)", (now,))
            conn.commit()

            db.migrate_sync(conn)

            row = conn.execute("SELECT * FROM distrakt_user_seasons").fetchone()
            self.assertIsNone(row["simkl_id"])
            self.assertEqual(row["trakt_id"], 111)
        finally:
            conn.close()


class PragmaTests(DbTestCase):
    async def test_foreign_keys_are_actually_on(self):
        """Asserted, not assumed: the setting is per-connection and defaults off,
        and every ON DELETE CASCADE in the schema depends on it."""
        self.assertEqual(await db.fetch_value("PRAGMA foreign_keys"), 1)

    async def test_foreign_keys_are_enforced_and_cascade(self):
        now = db.now()
        with self.assertRaises(db.IntegrityError):
            await db.execute(
                "INSERT INTO sessions (id, user_id, created_at, expires_at, "
                "absolute_expires_at, last_seen_at) VALUES ('x', 9999, ?, ?, ?, ?)",
                (now, now + 60, now + 60, now))

        user_id = (await db.execute(
            "INSERT INTO users (username, created_at, updated_at) VALUES ('u', ?, ?)",
            (now, now))).lastrowid
        await db.execute(
            "INSERT INTO sessions (id, user_id, created_at, expires_at, "
            "absolute_expires_at, last_seen_at) VALUES ('s1', ?, ?, ?, ?, ?)",
            (user_id, now, now + 60, now + 60, now))
        await db.execute("DELETE FROM users WHERE id = ?", (user_id,))
        self.assertEqual(await db.fetch_value("SELECT COUNT(*) FROM sessions"), 0)

    async def test_connection_policy(self):
        self.assertEqual(str(await db.fetch_value("PRAGMA journal_mode")).lower(), "wal")
        self.assertEqual(await db.fetch_value("PRAGMA busy_timeout"), db.BUSY_TIMEOUT_MS)
        self.assertEqual(await db.fetch_value("PRAGMA synchronous"), 1)  # NORMAL

    async def test_every_thread_gets_the_pragmas(self):
        """The helpers run on a worker thread pool and each thread opens its own
        connection, so a fresh thread must come up with foreign keys on too.
        That is why the pragmas live in the connection factory rather than in a
        migration."""
        results = []
        for _ in range(8):
            results.append(await db.run(
                lambda conn: (conn.execute("PRAGMA foreign_keys").fetchone()[0],
                              conn.execute("PRAGMA journal_mode").fetchone()[0])))
        self.assertTrue(all(fk == 1 and str(jm).lower() == "wal" for fk, jm in results))


class AggregatedReadTests(unittest.TestCase):
    """db.aggregated reads every row as one: SQLite's JSON spells a REAL with 15
    significant digits, so the columns a reader names `exact` come back as a
    plain SELECT gives them — and only those cost the spelling."""

    def setUp(self):
        self.conn = __import__("sqlite3").connect(":memory:")
        self.conn.execute("CREATE TABLE t (rating REAL, name TEXT, n INTEGER, at REAL)")
        self.rows = [(8.893616676330566, "one", 3, 1791673200.0), (0.1, "two", None, None),
                     (None, "8.25", 7, -2.5)]
        self.conn.executemany("INSERT INTO t VALUES (?, ?, ?, ?)", self.rows)
        self.columns = ["rating", "name", "n", "at"]

    def test_a_plain_read_spells_a_rating_with_fifteen_digits(self):
        read = db.aggregated(self.conn, self.columns, "FROM t ORDER BY rowid")
        self.assertEqual(read[0][0], 8.89361667633057)

    def test_the_exact_columns_come_back_as_a_select_gives_them(self):
        read = db.aggregated(self.conn, self.columns, "FROM t ORDER BY rowid", exact=[0, 3])
        self.assertEqual([tuple(row) for row in read], self.rows)

    def test_a_text_column_is_never_read_as_a_number(self):
        read = db.aggregated(self.conn, self.columns, "FROM t ORDER BY rowid", exact=[0, 1])
        self.assertEqual(read[2][1], "8.25")

    def test_nothing_found_is_no_rows(self):
        self.assertEqual(db.aggregated(self.conn, self.columns, "FROM t WHERE 0", exact=[0]), [])


class HelperTests(DbTestCase):
    async def test_execute_reports_lastrowid_and_rowcount(self):
        now = db.now()
        inserted = await db.execute(
            "INSERT INTO users (username, created_at, updated_at) VALUES ('a', ?, ?)",
            (now, now))
        self.assertIsNotNone(inserted.lastrowid)
        updated = await db.execute("UPDATE users SET timezone = 'UTC' WHERE id = ?",
                                   (inserted.lastrowid,))
        self.assertEqual(updated.rowcount, 1)

    async def test_fetch_one_and_all(self):
        now = db.now()
        await db.executemany(
            "INSERT INTO users (username, created_at, updated_at) VALUES (?, ?, ?)",
            [("a", now, now), ("b", now, now)])
        self.assertEqual(len(await db.fetch_all("SELECT * FROM users")), 2)
        row = await db.fetch_one("SELECT * FROM users WHERE username = 'b'")
        self.assertEqual(row["username"], "b")
        self.assertIsNone(await db.fetch_one("SELECT * FROM users WHERE username = 'zz'"))

    async def test_transaction_rolls_back_on_error(self):
        now = db.now()

        def _boom(conn):
            conn.execute("INSERT INTO users (username, created_at, updated_at) "
                         "VALUES ('kept?', ?, ?)", (now, now))
            raise RuntimeError("nope")

        with self.assertRaises(RuntimeError):
            await db.transaction(_boom)
        self.assertEqual(await db.fetch_value("SELECT COUNT(*) FROM users"), 0)

    async def test_a_long_hold_of_the_write_lock_is_logged_naming_the_holder(self):
        """Every other writer waits behind a held lock and gives up at the busy
        timeout, so the hold that causes "database is locked" names itself."""
        def slow_body(conn):
            time.sleep(0.05)

        def quick_body(conn):
            pass

        with patch.object(db, "LOCK_HELD_WARN_SECONDS", 0.01), \
                self.assertLogs("app.db", level="WARNING") as logged:
            await db.transaction(quick_body)
            await db.transaction(slow_body)
        self.assertEqual(len(logged.records), 1)
        self.assertIn("slow_body", logged.output[0])

    async def test_a_body_that_fails_after_a_long_hold_is_logged_too(self):
        def failing_body(conn):
            time.sleep(0.05)
            raise RuntimeError("nope")

        with patch.object(db, "LOCK_HELD_WARN_SECONDS", 0.01), \
                self.assertLogs("app.db", level="WARNING") as logged, \
                self.assertRaises(RuntimeError):
            await db.transaction(failing_body)
        self.assertIn("failing_body", logged.output[0])

    async def test_what_prepare_read_before_the_lock_reaches_the_body(self):
        calls = []

        def prepare(conn):
            calls.append("prepare")
            return conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]

        def body(conn, prepared):
            calls.append(("body", prepared, conn.in_transaction))
            return prepared

        self.assertEqual(await db.transaction(body, prepare=prepare), 0)
        self.assertEqual(calls, ["prepare", ("body", 0, True)])

    async def test_another_connections_commit_after_prepare_makes_it_prepare_again(self):
        """`prepare` read before the lock; once another writer has committed, what
        it read may be stale, so it is read again under the lock."""
        now = db.now()
        prepared = []

        def other_writer():
            db.connection().execute(
                "INSERT INTO users (username, created_at, updated_at) VALUES ('w', ?, ?)",
                (now, now))
            db.close_thread_connection()

        def prepare(conn):
            prepared.append(conn.execute("SELECT COUNT(*) FROM users").fetchone()[0])
            if len(prepared) == 1:  # then another connection commits, once
                writer = threading.Thread(target=other_writer)
                writer.start()
                writer.join()
            return prepared[-1]

        seen = await db.transaction(lambda conn, users: users, prepare=prepare)
        self.assertEqual((prepared, seen), ([0, 1], 1))

    async def test_a_transaction_waits_its_turn_however_long_the_one_ahead_takes(self):
        """SQLite does not queue writers: one that finds the lock taken sleeps and
        tries again until the busy timeout, and a run of other writers can outlast
        it. Queued in the process instead, a transaction waits for the one ahead of
        it, and reads and writes only once that one has finished."""
        order = []

        def slow(conn):
            order.append("slow begins")
            time.sleep(0.3)
            order.append("slow ends")

        def quick_prepare(conn):
            order.append("quick prepares")

        def quick(conn, _prepared):
            order.append("quick writes")

        # Connections opened under the patch wait 50 ms at a taken lock, far less
        # than the slow body holds it; moving the generation makes every worker
        # open a new one.
        with patch.object(db, "BUSY_TIMEOUT_MS", 50):
            db.set_db_path(db.db_path())
            first = asyncio.ensure_future(db.transaction(slow))
            await asyncio.sleep(0.05)
            await db.transaction(quick, prepare=quick_prepare)
            await first
        self.assertEqual(order, ["slow begins", "slow ends", "quick prepares", "quick writes"])

    async def test_transaction_commits(self):
        now = db.now()

        def _work(conn):
            conn.execute("INSERT INTO users (username, created_at, updated_at) "
                         "VALUES ('x', ?, ?)", (now, now))
            return conn.execute("SELECT last_insert_rowid()").fetchone()[0]

        user_id = await db.transaction(_work)
        self.assertEqual(await db.fetch_value("SELECT COUNT(*) FROM users"), 1)
        self.assertEqual(
            await db.fetch_value("SELECT id FROM users WHERE username = 'x'"), user_id)

    async def test_app_meta_roundtrip_and_upsert(self):
        self.assertIsNone(await db.get_meta("plex_client_id"))
        self.assertEqual(await db.get_meta("plex_client_id", "fallback"), "fallback")
        await db.set_meta("plex_client_id", "abc")
        await db.set_meta("plex_client_id", "def")
        self.assertEqual(await db.get_meta("plex_client_id"), "def")


class DriverIsolationTests(unittest.TestCase):
    def test_only_the_db_module_imports_sqlite3(self):
        """app/db.py is the only module allowed to touch the driver directly.

        Everything else goes through its async helpers, which push the blocking
        call onto a worker thread — an `import sqlite3` anywhere else is how a
        blocking query ends up stalling the event loop from inside a route.
        """
        pattern = re.compile(r"^\s*(import sqlite3|from sqlite3 import)", re.MULTILINE)
        # Walked, not globbed: app/ holds packages now (auth, distrakt,
        # providers), and a top-level glob stopped seeing most of the app the
        # moment the first one was created.
        offenders = sorted(
            str(path.relative_to(APP_DIR)) for path in APP_DIR.rglob("*.py")
            if path.name != "db.py" and pattern.search(path.read_text(encoding="utf-8"))
        )
        self.assertEqual(offenders, [], f"modules importing sqlite3 directly: {offenders}")

    def test_only_the_db_module_takes_a_connection_itself(self):
        """The same rule, one step on: `db.connection()` is this thread's
        connection, so calling it from a route runs the query on the event loop —
        which config.py did for every settings read and save until both went
        through db.run and db.transaction. A body handed to one of those is given
        its connection; nothing else asks for one."""
        pattern = re.compile(r"\bdb\.connection\(")
        offenders = sorted(
            str(path.relative_to(APP_DIR)) for path in APP_DIR.rglob("*.py")
            if path.name != "db.py" and pattern.search(path.read_text(encoding="utf-8"))
        )
        self.assertEqual(offenders, [], f"modules taking a connection directly: {offenders}")


if __name__ == "__main__":
    unittest.main()
