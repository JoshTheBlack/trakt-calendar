"""The conversion and the live writers agree: on the same input, migration 49's
frozen fold and the store's own writers produce the same rows.

WHY THIS IS ITS OWN TEST. Migration 49 decides, once, for every listing an
instance already holds, which title row and which episode row it is, which of a
service's entries is a title's group and which id that entry is filed under. The
live writers (app/calendar/entries.py over app/metadata/) decide the same things
for every listing after it — with code of their own, because a migration is
frozen and the app is not. If the two ever disagree, a title converted at
start-up and the same title written by the next fill are two different rows, or
one row whose listings are filed two ways, and nothing fails: the calendar draws
a card twice, or a listing stands beside its own repeat.

So the same input goes both ways — as rows in the retired tables, written the way
the store wrote them at 48, then converted; and as records handed to the live
writers on a database already at 49 — and the rows must be identical: every
episode row's listings (instants, calendars, searched bits, the halves a listing
left out, which entry listed it), every title's service groups (the primary
entry's words, its canonical own id), every spelling in `service_ids`, and each
episode's description.

Covered: a service with two entries for one title, listed together; an entry
spelled by its slug; a listing naming no full episode that folds, one refused as
ambiguous, one refused because its own service lists the episode; one instant on
two calendars and two instants of one episode; a film listed twice; a searched
listing its feed stands down and one it does not.

No network.
"""
from __future__ import annotations

import json
import sqlite3
import unittest
from datetime import date, datetime, timezone
from unittest.mock import patch

from app import db
from app.calendar import entries
from app.metadata import descriptions, groups, rows, tables
from app.providers.base import Media, Record, Source
from tests.support import TMP, DatabaseTestCase

NOW = 1_800_000_000
STALE = NOW + 86_400
SPAN = (date(2024, 1, 1), date(2027, 1, 1))


def at(day: str, hour: int = 20) -> float:
    return datetime.fromisoformat(day).replace(hour=hour, tzinfo=timezone.utc).timestamp()


def _listing(source: Source, own: str, ids: dict, title: str, day: str, *, hour: int = 20,
             season: int | None = None, number: int | None = None,
             media: Media = Media.SHOW, episode_title: str = "", **words) -> Record:
    return Record(source=source, media=media, id=own, ids=ids, detail_url=f"https://{own}",
                  title=title, air_ts=at(day, hour), season=season, episode_number=number,
                  episode_label=(f"S{season:02d}E{number:02d}"
                                 if season is not None and number is not None else ""),
                  episode_title=episode_title, **words)


TRAKT_K1 = {"trakt": 500, "tmdb": 100, "trakt_slug": "the-show"}
SIMKL_A = {"simkl": 1001, "tmdb": "100"}
SIMKL_B = {"simkl": 1002, "tmdb": "100"}

# {calendar: [records its feed listed]} — every case, in the order the fills ran.
FEEDS: dict[str, list[Record]] = {
    "shows": [
        # K1: Trakt's premiere, spelled by its slug, named by the feed; Simkl's
        # two entries for the title — the show, listed once, and its second
        # season filed as a title of its own, listed three times.
        _listing(Source.TRAKT, "the-show", TRAKT_K1, "The Show", "2026-10-07", season=2,
                 number=1, episode_title="Premiere", network="HBO", year=2024),
        # Folds into Trakt's S2E1 that day: one numbered episode agrees with it.
        _listing(Source.SIMKL, "1002", SIMKL_B, "The Show Season 2", "2026-10-07", hour=21,
                 number=1, anime_type="tv", enriched=False),
        # Two episodes one day nobody else lists: two rows.
        _listing(Source.SIMKL, "1002", SIMKL_B, "The Show Season 2", "2026-10-14", number=2,
                 anime_type="tv", enriched=False),
        _listing(Source.SIMKL, "1002", SIMKL_B, "The Show Season 2", "2026-10-14", number=3,
                 anime_type="tv", enriched=False),
        _listing(Source.SIMKL, "1001", SIMKL_A, "The Show", "2024-01-05", number=1,
                 enriched=False),
        # K2: a day with two episodes Simkl's listing could be, and a listing whose
        # own service already lists the one episode it could be.
        _listing(Source.TRAKT, "two", {"trakt": 2, "tmdb": 200}, "Two", "2026-10-01",
                 season=1, number=1),
        _listing(Source.TRAKT, "two", {"trakt": 2, "tmdb": 200}, "Two", "2026-10-01",
                 season=2, number=1),
        _listing(Source.SIMKL, "2001", {"simkl": 2001, "tmdb": 200}, "Two", "2026-10-01",
                 number=1, enriched=False),
        _listing(Source.TRAKT, "two", {"trakt": 2, "tmdb": 200}, "Two", "2026-10-02",
                 season=1, number=5),
        _listing(Source.SIMKL, "2001", {"simkl": 2001, "tmdb": 200}, "Two", "2026-10-02",
                 season=1, number=5, enriched=False),
        _listing(Source.SIMKL, "2001", {"simkl": 2001, "tmdb": 200}, "Two", "2026-10-02",
                 hour=22, number=5, enriched=False),
        # K3: one episode an hour apart on two calendars, and another listed twice
        # a week apart.
        _listing(Source.SIMKL, "3001", {"simkl": 3001, "tmdb": 300}, "Three", "2026-10-03",
                 hour=21, season=1, number=1, enriched=False),
        _listing(Source.SIMKL, "3001", {"simkl": 3001, "tmdb": 300}, "Three", "2026-10-10",
                 season=1, number=2, enriched=False),
        _listing(Source.SIMKL, "3001", {"simkl": 3001, "tmdb": 300}, "Three", "2026-10-17",
                 season=1, number=2, enriched=False),
    ],
    "shows/premieres": [
        # K1 again: the same instant on a second calendar, and TMDB dating it two
        # months later.
        _listing(Source.TRAKT, "the-show", TRAKT_K1, "The Show", "2026-10-07", season=2,
                 number=1, episode_title="Premiere", network="HBO", year=2024),
        _listing(Source.TMDB, "100", {"tmdb": 100}, "The Show", "2026-12-20", season=2,
                 number=1),
        _listing(Source.SIMKL, "3001", {"simkl": 3001, "tmdb": 300}, "Three", "2026-10-03",
                 season=1, number=1, enriched=False),
        # K5: the feed's listing of an episode a search had already written.
        _listing(Source.TRAKT, "five", {"trakt": 5, "tmdb": 500}, "Five", "2026-10-05",
                 hour=7, season=1, number=1),
    ],
    "movies": [
        # K4: a film Simkl lists twice and TMDB once.
        _listing(Source.SIMKL, "4001", {"simkl": 4001, "tmdb": 400}, "Four", "2026-11-04",
                 hour=0, media=Media.MOVIE, enriched=False),
        _listing(Source.SIMKL, "4001", {"simkl": 4001, "tmdb": 400}, "Four", "2026-12-25",
                 hour=0, media=Media.MOVIE, enriched=False),
        _listing(Source.TMDB, "400", {"tmdb": 400}, "Four", "2026-11-04", hour=0,
                 media=Media.MOVIE),
    ],
}

# {calendar: [records a search wrote]}: K5's premiere, before its feed listed it
# (it stands down), and an episode no feed lists (it stays).
SEARCHED: dict[str, list[Record]] = {
    "shows/premieres": [
        _listing(Source.TRAKT, "five", {"trakt": 5, "tmdb": 500}, "Five", "2026-10-05",
                 hour=0, season=1, number=1)],
    "shows/new": [
        _listing(Source.TRAKT, "five", {"trakt": 5, "tmdb": 500}, "Five", "2026-10-06",
                 season=1, number=2)],
}

# What Trakt's own episode list says about K1's premiere, once the drain read it.
DESCRIBED = {"title": "Premiere", "overview": "It begins.", "runtime": 45, "rating": 8.1,
             "votes": 100, "episode_type": "season_premiere", "first_aired": "2026-10-07"}

# Season summaries, as (source, own id, ids, summary): TMDB's of K1's season 2,
# and one from Simkl's OTHER entry for K1 — its own season 1, another numbering
# than the title's, which neither way files on the title.
SUMMARIES = (
    ("tmdb", "100", {"tmdb": 100},
     {"season": 2, "total": 10, "air_dates": ["2026-10-07", "2026-10-14"]}),
    ("simkl", "1001", SIMKL_A, {"season": 1, "total": 3, "air_dates": ["2024-01-05"]}),
)


def _key(record: Record) -> str:
    return f"{record.media}:tmdb:{int(record.ids['tmdb'])}"


# ---------------------------------------------------------------------------
# the input, as the store wrote it at 48
# ---------------------------------------------------------------------------

def _as_written_at_48(conn) -> None:
    """Every listing as the store at 48 filed it: a title row per service entry
    as its listing spelled it, an airing per listing and calendar, and the
    episode stub a numbered listing wrote — then the drain's description."""
    titles, airings, stubs = {}, [], {}
    every = [(endpoint, record, 0) for endpoint, listed in FEEDS.items() for record in listed]
    every += [(endpoint, record, 1) for endpoint, listed in SEARCHED.items() for record in listed]
    for endpoint, record, searched in every:
        source, media, spelled = str(record.source), str(record.media), str(record.id)
        titles[(source, media, spelled)] = {
            "source": source, "media": media, "source_id": spelled, "title_key": _key(record),
            **rows._title_values(record), "fetched_at": NOW, "stale_after": STALE}
        airings.append((source, media, spelled, endpoint, _key(record), record.air_ts,
                        datetime.fromtimestamp(record.air_ts, timezone.utc).date().isoformat(),
                        -1 if record.season is None else record.season,
                        -1 if record.episode_number is None else record.episode_number,
                        record.episode_label or "", NOW, searched))
        if record.season is not None and record.episode_number is not None:
            stubs[(source, media, spelled, record.season, record.episode_number)] = \
                record.episode_title or ""
    for row in titles.values():
        conn.execute(f"INSERT INTO title_metadata ({', '.join(row)}) "
                     f"VALUES ({', '.join('?' * len(row))})", tuple(row.values()))
    conn.executemany(
        "INSERT INTO calendar_airings (source, media, source_id, endpoint, title_key, air_ts, "
        "air_date, date_only, season, episode_number, episode_label, stored_at, from_search) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?)", airings)
    conn.executemany(
        "INSERT INTO episode_metadata (source, media, source_id, season, number, title, "
        "fetched_at) VALUES (?, ?, ?, ?, ?, ?, 0)",
        [(*slot, title) for slot, title in stubs.items()])
    # THE DRAIN'S ANSWER, over the stub of the spelling the listing used.
    conn.execute(
        "UPDATE episode_metadata SET title = ?, overview = ?, runtime = ?, rating = ?, "
        "votes = ?, episode_type = ?, first_aired = ?, fetched_at = ?, stale_after = ? "
        "WHERE source = 'trakt' AND source_id = 'the-show' AND season = 2 AND number = 1",
        (DESCRIBED["title"], DESCRIBED["overview"], DESCRIBED["runtime"], DESCRIBED["rating"],
         DESCRIBED["votes"], DESCRIBED["episode_type"], DESCRIBED["first_aired"], NOW,
         rows.episode_stale_after(at("2026-10-07"), NOW)))
    for source, own, _ids, summary in SUMMARIES:
        shape = descriptions.summary_shape(summary)
        days = [date.fromisoformat(day) if day else None for day in shape]
        conn.execute(
            "INSERT INTO season_metadata (source, media, source_id, season, episode_count, "
            "premiere, air_dates_json, fetched_at, stale_after) "
            "VALUES (?, 'show', ?, ?, ?, ?, ?, ?, ?)",
            (source, own, summary["season"], len(shape), next(filter(None, shape), ""),
             json.dumps(shape), NOW, descriptions.season_stale_after(days, NOW)))


def _converted() -> sqlite3.Connection:
    path = TMP / "conversion-agreement.db"
    path.unlink(missing_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.isolation_level = None
    conn.execute("PRAGMA foreign_keys=ON")
    with patch.object(db, "MIGRATIONS", [m for m in db.MIGRATIONS if m[0] <= 48]):
        db.migrate_sync(conn)
    _as_written_at_48(conn)
    conn.execute("BEGIN IMMEDIATE")
    db.MIGRATION_49(conn, now=NOW)
    conn.execute("COMMIT")
    return conn


# ---------------------------------------------------------------------------
# what both ways are compared on
# ---------------------------------------------------------------------------

def _group_columns(table: str) -> list[str]:
    return [f"{source}_{name}" for source in groups.sources()
            for name in groups.every(table, source)]


def _rows_of(fetch, table: str, order: str, identity: tuple[str, ...]) -> list[dict]:
    columns = [*identity, *_group_columns(table)]
    return [dict(zip(columns, row)) for row in fetch(
        f"SELECT {', '.join(columns)} FROM {table} ORDER BY {order}")]


# A title row's identity columns are the identity's, which settle writes and the
# migration carries; what is compared here is each service's group.
_COMPARED = {
    tables.TITLES: ("title_key", ("title_key", "media")),
    tables.SEASONS: ("title_key, season", ("title_key", "season")),
    tables.EPISODES: ("title_key, unit", ("title_key", "unit", "media", "season", "number")),
}


def _snapshot(fetch) -> dict[str, list]:
    out = {table: _rows_of(fetch, table, order, identity)
           for table, (order, identity) in _COMPARED.items()}
    out[tables.SERVICE_IDS] = [tuple(row) for row in fetch(
        f"SELECT source, media, service_id, title_key, is_primary FROM {tables.SERVICE_IDS} "
        "ORDER BY source, media, service_id")]
    return out


class TheConversionAndTheWritersAgreeTests(DatabaseTestCase):

    @classmethod
    def setUpClass(cls):
        conn = _converted()
        try:
            cls.converted = _snapshot(lambda sql: conn.execute(sql).fetchall())
        finally:
            conn.close()
            (TMP / "conversion-agreement.db").unlink(missing_ok=True)

    async def asyncSetUp(self):
        await super().asyncSetUp()
        # A search wrote K5's premiere before its feed listed it.
        await entries.store_loose_airings("shows/premieres", SEARCHED["shows/premieres"],
                                          now=NOW, stale_after=STALE)
        for endpoint, records in FEEDS.items():
            await entries.store_span(
                endpoint, *SPAN, records, sources=sorted({str(r.source) for r in records}),
                asked=sorted({str(r.source) for r in records}), now=NOW, stale_after=STALE)
        await entries.store_loose_airings("shows/new", SEARCHED["shows/new"], now=NOW,
                                          stale_after=STALE)
        await entries.store_episodes("trakt", 500, "show", 2, [{"number": 1, **DESCRIBED}],
                                     now=NOW)
        for source, own, ids, summary in SUMMARIES:
            await db.transaction(lambda conn, s=source, o=own, i=ids, m=summary:
                                 descriptions.write_season_summary(conn, s, "show", o, m,
                                                                   ids=i, now=NOW))
        self.written = await db.run(
            lambda conn: _snapshot(lambda sql: conn.execute(sql).fetchall()))

    def test_every_episode_row_and_its_listings(self):
        self.assertEqual(self.written[tables.EPISODES], self.converted[tables.EPISODES])

    def test_every_titles_service_groups(self):
        self.assertEqual(self.written[tables.TITLES], self.converted[tables.TITLES])

    def test_every_spelling_of_every_entry_and_which_is_the_group(self):
        self.assertEqual(self.written[tables.SERVICE_IDS], self.converted[tables.SERVICE_IDS])

    def test_every_season_row(self):
        self.assertEqual(self.written[tables.SEASONS], self.converted[tables.SEASONS])

    def test_the_input_reaches_every_case(self):
        """A comparison of two empty stores proves nothing: the cases are there."""
        units = {(row["title_key"], row["unit"]) for row in self.written[tables.EPISODES]}
        self.assertLessEqual({("show:tmdb:100", "S2E1"), ("show:tmdb:100", "D2026-10-14E2"),
                              ("show:tmdb:200", "D2026-10-01E1"),
                              ("show:tmdb:200", "D2026-10-02E5"), ("movie:tmdb:400", "film"),
                              ("show:tmdb:500", "S1E2")}, units)
        primary = {(row[0], row[2]): row[4] for row in self.written[tables.SERVICE_IDS]}
        self.assertEqual((primary[("simkl", "1002")], primary[("simkl", "1001")]), (1, 0))
        self.assertEqual([(row["title_key"], row["season"]) for row in self.written[tables.SEASONS]],
                         [("show:tmdb:100", 2)])


class TheCanonicalOwnIdIsOneRuleTests(unittest.TestCase):
    """`rows.own_id` and the migration's `_m49_own_id` are one rule spelled twice
    — the app's and the frozen copy — and must answer alike."""

    CASES = (
        ("trakt", "the-show", {"trakt": 500, "trakt_slug": "the-show"}),
        ("trakt", "500", {"trakt": 500}),
        ("trakt", "a-slug", {}),
        ("simkl", "1002", {"simkl": 1002, "tmdb": "100"}),
        ("simkl", "1002", {"simkl": "1002"}),
        ("tmdb", "100", {"tmdb": 100}),
        ("tmdb", "100", '{"tmdb": 100}'),
        ("tmdb", "100", "not json"),
        ("simkl", "77", None),
    )

    def test_the_two_spellings_of_the_rule_agree(self):
        for source, source_id, ids in self.CASES:
            with self.subTest(source=source, source_id=source_id, ids=ids):
                stored = ids if ids is None or isinstance(ids, str) else json.dumps(ids)
                self.assertEqual(rows.own_id(source, source_id, ids),
                                 db._m49_own_id(source, source_id, stored))


if __name__ == "__main__":
    unittest.main()
