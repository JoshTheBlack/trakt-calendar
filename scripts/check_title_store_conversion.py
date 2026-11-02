"""Account for every row the title store's conversion retired, on a COPY.

Migration 49 (app/db.py) built one row per title, per season and per episode —
`titles`, `seasons`, `episodes`, `service_ids` — from the per-service tables,
and dropped none of them: title_metadata, season_metadata, episode_metadata,
calendar_airings and the old identity table, renamed `titles_v47`, are kept
whole so that THIS can check, row by row, that the conversion lost nothing
before a later release drops them. It is the check that release's drop waits on.

WHAT IT DOES:
  1. Copies SOURCE through SQLite's backup API, opened read-only, into a working
     copy (a temporary directory unless --keep names one). SOURCE is never
     written.
  2. Converts the copy if it has not been converted: the app's own migrations up
     to the newest it knows, and migration 49 on top while it is not yet in that
     list. Reports how long each took and the file's size before and after. A
     copy taken after an instance has run the conversion is only read.
  3. Accounts, counting per table and per service:
       - every title_metadata row: its own id is a `service_ids` row naming a
         title, and a row of the entry that title's group is was copied word for
         word — or the group has been rewritten since the conversion;
       - every season_metadata and episode_metadata row: its title's season or
         episode row holds that service's description, word for word, unless it
         has been rewritten since or another entry of the service described it;
       - EVERY WORD WITH NO COLUMN TO GO TO — a value a retired row holds in a
         column the service's group was not given — is LOST, and listed;
       - every calendar_airings row: its title (found through `service_ids`, so
         a title re-keyed since is still found) has exactly one row for that
         episode holding the service's instant on that calendar, as its listing
         or a repeat — or the service's calendar has been refilled since;
       - and the other way: every instant and every group in the new tables
         comes from a retired row, or was written after the conversion;
       - every old identity row, and every key a holder table holds, resolves to
         a title (directly or through `title_aliases`) — no key that resolved
         against the old identity table may fail against the new one;
       - PRAGMA integrity_check and foreign_key_check.
Exit status 1 when anything is MISSING, LOST, INVENTED or newly unresolved, or
a check fails; 0 otherwise.

WHAT IS EXPECTED, AND NOT A LOSS: words of a service's SECOND entry for a title
(Simkl files some later seasons as titles of their own) are not copied into the
title's group — the entry stays findable as a `service_ids` row — and a searched
listing its own feed also lists stands down, as the calendar's read already
dropped it. Both are counted and named.

    ./.venv/Scripts/python.exe scripts/check_title_store_conversion.py data/proddb/app.db
    ./.venv/Scripts/python.exe scripts/check_title_store_conversion.py SOURCE --keep DIR
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import time
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

SOURCES = ("tmdb", "trakt", "simkl")
# The key app_meta records the conversion's moment under (app/db.py,
# TITLE_STORE_CONVERTED_AT) — spelled here too so this reads a copy without the
# app's code having to still define it.
CONVERTED_AT = "title_store_converted_at"
UNSTATED = -1
RETIRED_IDENTITY = "titles_v47"

# Columns of the retired tables that say WHICH row it is rather than what the
# service said — carried as the new rows' keys, never as group columns.
ROW_KEYS = {
    "title_metadata": {"source", "media", "source_id", "title_key"},
    "season_metadata": {"source", "media", "source_id", "season"},
    "episode_metadata": {"source", "media", "source_id", "season", "number"},
}

# EVERY TABLE THAT HOLDS A TITLE KEY, and the keys it holds — the holders a
# re-key repoints (app/metadata/holders.py), spelled here so a copy is checked
# against what was there whatever the app's list says by then. A table missing
# from a copy is skipped.
_TRIPLE = "media || ':' || match_source || ':' || match_id"
HOLDER_KEYS = {
    **{table: f"SELECT {_TRIPLE} FROM {table}" for table in (
        "distrakt_user_seasons", "distrakt_month_records", "distrakt_unsettled_rows",
        "distrakt_movie_watches", "distrakt_open_prompts", "distrakt_prompt_dismissals",
        "distrakt_show_progress", "distrakt_slug_repairs", "tier_items",
        "tier_item_removals")},
    "watchlist_writes": "SELECT 'show:' || match_source || ':' || match_id FROM watchlist_writes",
    "not_watching_shows": "SELECT item_id FROM not_watching_shows",
    "calendar_view_state": ("SELECT j.value FROM calendar_view_state, "
                            "json_each(calendar_view_state.last_show_ids_json) j "
                            "WHERE json_valid(last_show_ids_json)"),
    "tier_category_removals": ("SELECT j.value FROM tier_category_removals, "
                               "json_each(tier_category_removals.items_json) j "
                               "WHERE json_valid(items_json)"),
    "distrakt_months": ("SELECT json_extract(j.value, '$.key') FROM distrakt_months, "
                        "json_each(distrakt_months.movies_json) j WHERE json_valid(movies_json)"),
    "title_posters": "SELECT title_key FROM title_posters",
    "title_aliases": "SELECT title_key FROM title_aliases",
    "service_ids": "SELECT title_key FROM service_ids",
    "seasons": "SELECT title_key FROM seasons",
    "episodes": "SELECT title_key FROM episodes",
    "share_highlights": "SELECT title_key FROM share_highlights",
}
# Holders that are the store's own: every key must resolve to a title outright.
STORE_HOLDERS = {"title_aliases", "service_ids", "seasons", "episodes", "share_highlights"}


def held(value) -> bool:
    return value is not None and value not in ("", "[]", "{}")


class Report:
    """Counts by (section, outcome, detail), and the examples of anything bad."""
    BAD = ("MISSING", "LOST", "INVENTED", "NEWLY UNRESOLVED", "FAILED")

    def __init__(self) -> None:
        self.counts: Counter = Counter()
        self.examples: dict[tuple, list] = defaultdict(list)

    def add(self, section: str, outcome: str, detail: str = "", example=None) -> None:
        self.counts[(section, outcome, detail)] += 1
        if example is not None and outcome in self.BAD and len(self.examples[(section, outcome, detail)]) < 5:
            self.examples[(section, outcome, detail)].append(example)

    def bad(self) -> int:
        return sum(n for (_s, outcome, _d), n in self.counts.items() if outcome in self.BAD)

    def print(self) -> None:
        section = None
        for (name, outcome, detail), n in sorted(self.counts.items()):
            if name != section:
                print(f"\n{name}")
                section = name
            flag = "  <-- " if outcome in self.BAD else ""
            print(f"  {n:>9,}  {outcome}{' / ' + detail if detail else ''}{flag}")
            for example in self.examples.get((name, outcome, detail), ()):
                print(f"               e.g. {example}")


# ---------------------------------------------------------------------------
# the copy, and converting it
# ---------------------------------------------------------------------------

def copy_of(source: Path, work: Path) -> Path:
    target = work / "app.db"
    if target.exists():
        target.unlink()
    reader = sqlite3.connect(f"file:{source.resolve().as_posix()}?mode=ro", uri=True)
    writer = sqlite3.connect(target)
    try:
        reader.backup(writer)
    finally:
        writer.close()
        reader.close()
    return target


def version_of(conn: sqlite3.Connection) -> int:
    return int(conn.execute("SELECT version FROM schema_version").fetchone()[0])


def convert(path: Path) -> None:
    """Bring a copy that has not been converted through the conversion, with the
    app's own migrations — never a second copy of them — and NOTHING PAST IT: a
    later release's migrations include the drop of the very tables this checks,
    so a copy is taken up to 48, then 49, and no further. A copy already at 49 or
    later is only read."""
    from app import db  # the app's migrations, run against the working copy only

    conn = sqlite3.connect(path, isolation_level=None)
    try:
        before = version_of(conn)
        print(f"copy at schema {before}, {path.stat().st_size / 1e6:,.1f} MB")
        if before >= 49:
            return
        listed = db.MIGRATIONS
        db.MIGRATIONS = [step for step in listed if step[0] <= 48]
        try:
            started = time.perf_counter()
            db.migrate_sync(conn)
        finally:
            db.MIGRATIONS = listed
        if version_of(conn) != before:
            print(f"  migrations {before + 1}..{version_of(conn)}: "
                  f"{time.perf_counter() - started:.2f} s, {path.stat().st_size / 1e6:,.1f} MB")
        # The app's own step 49 — in its list once the code reading these tables
        # landed with it, and the module's definition before that.
        conversion = dict(listed).get(49, db.MIGRATION_49)
        size = path.stat().st_size
        started = time.perf_counter()
        conn.execute("BEGIN IMMEDIATE")
        try:
            conversion(conn)
            conn.execute("UPDATE schema_version SET version = 49")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        conn.execute("COMMIT")
        print(f"  migration 49: {time.perf_counter() - started:.2f} s, "
              f"{size / 1e6:,.1f} MB -> {path.stat().st_size / 1e6:,.1f} MB")
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# accounting
# ---------------------------------------------------------------------------

def rows(conn: sqlite3.Connection, sql: str, params=()) -> list[sqlite3.Row]:
    cursor = conn.cursor()
    cursor.row_factory = sqlite3.Row
    return cursor.execute(sql, params).fetchall()


def columns_of(conn: sqlite3.Connection, table: str) -> list[str]:
    return [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]


def defaults_of(conn: sqlite3.Connection, table: str) -> dict:
    """{column: the value a row holds when nothing was said} — its declared
    default, as the value it reads back as."""
    out = {}
    for _cid, name, _type, _notnull, default, _pk in conn.execute(f"PRAGMA table_info({table})"):
        out[name] = None if default is None else conn.execute(f"SELECT {default}").fetchone()[0]
    return out


def says_something(value, default) -> bool:
    """A value a service stated — not nothing, and not the column's default."""
    return held(value) and value != default


def has_table(conn: sqlite3.Connection, table: str) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
                        (table,)).fetchone() is not None


class Store:
    """The converted tables, read once: how a retired row finds its title now."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn
        self.converted_at = int((conn.execute(
            "SELECT value FROM app_meta WHERE key = ?", (CONVERTED_AT,)).fetchone() or [0])[0])
        self.aliases = dict(conn.execute("SELECT old_key, title_key FROM title_aliases"))
        self.titles = {r["title_key"]: r for r in rows(conn, "SELECT * FROM titles")}
        self.ids = {(r["source"], r["media"], r["service_id"]): (r["title_key"], r["is_primary"])
                    for r in rows(conn, "SELECT * FROM service_ids")}
        self.title_columns = set(columns_of(conn, "titles"))
        self.season_columns = set(columns_of(conn, "seasons"))
        self.episode_columns = set(columns_of(conn, "episodes"))

    def follow(self, key: str) -> str:
        return self.aliases.get(key, key)

    def title_of(self, source: str, media: str, source_id) -> tuple[str | None, int]:
        found = self.ids.get((source, media, str(source_id)))
        return (None, 0) if found is None else (self.follow(found[0]), found[1])

    def since(self, value) -> bool:
        """Written after the conversion — a group or listing the live code has
        rewritten, which no retired row is expected to match."""
        return value is not None and self.converted_at and int(value) > self.converted_at


def account_titles(conn, store: Store, report: Report) -> None:
    section = "title_metadata -> titles / service_ids"
    old_columns = columns_of(conn, "title_metadata")
    defaults = defaults_of(conn, "title_metadata")
    by_group: dict[tuple, list] = defaultdict(list)
    for row in rows(conn, "SELECT * FROM title_metadata"):
        source = row["source"]
        key, primary = store.title_of(source, row["media"], row["source_id"])
        if key is None or key not in store.titles:
            report.add(section, "MISSING", source, (source, row["source_id"], row["title_key"]))
            continue
        for column in old_columns:
            if column not in ROW_KEYS["title_metadata"] and f"{source}_{column}" not in store.title_columns \
                    and says_something(row[column], defaults[column]):
                report.add("words with no column", "LOST", f"title_metadata.{column} ({source})",
                           (row["source_id"], row[column]))
        by_group[(source, key)].append((row, primary))
    primary_spellings: dict[tuple, set] = defaultdict(set)
    for (source, _media, spelled), (key, primary) in store.ids.items():
        if primary:
            primary_spellings[(source, store.follow(key))].add(spelled)
    for (source, key), members in by_group.items():
        new = store.titles[key]
        if new[f"{source}_id"] is None:
            for row, _p in members:
                report.add(section, "MISSING", f"{source}: its title has no {source} group",
                           (source, row["source_id"], key))
            continue
        if new[f"{source}_id"] not in primary_spellings[(source, key)]:
            report.add(section, "MISSING", f"{source}: the group's id is not its primary entry's",
                       (key, new[f"{source}_id"], sorted(primary_spellings[(source, key)])))
        rewritten = store.since(new[f"{source}_fetched_at"])
        copied = None
        for row, primary in members:
            if primary and not rewritten and all(
                    row[c] == new[f"{source}_{c}"] for c in row.keys()
                    if c not in ROW_KEYS["title_metadata"] and f"{source}_{c}" in store.title_columns):
                copied = row
                break
        for row, primary in members:
            if row is copied:
                report.add(section, "found", f"{source}: the title's group, word for word")
            elif primary and rewritten:
                report.add(section, "found", f"{source}: the title's group, rewritten since")
            elif primary and copied is not None:
                report.add(section, "found", f"{source}: a second spelling of the group's entry")
            elif primary:
                report.add(section, "MISSING", f"{source}: the group matches no row of its entry",
                           (source, row["source_id"], key))
            else:
                report.add(section, "found", f"{source}: another entry (service_ids row; "
                           "its words stay in title_metadata)")


def account_descriptions(conn, store: Store, report: Report, table: str) -> None:
    """season_metadata -> seasons, episode_metadata -> episodes."""
    episode = table == "episode_metadata"
    target = "episodes" if episode else "seasons"
    new_columns = store.episode_columns if episode else store.season_columns
    section = f"{table} -> {target}"
    old_columns = columns_of(conn, table)
    defaults = defaults_of(conn, table)
    if episode:
        new_rows = {(r["title_key"], r["unit"]): r for r in rows(conn, "SELECT * FROM episodes")}
    else:
        new_rows = {(r["title_key"], r["season"]): r for r in rows(conn, "SELECT * FROM seasons")}
    # Each (service, slot)'s retired rows, then judged together: one slot can be
    # described by two of a service's rows — two entries, or one entry spelled
    # two ways — and only one of them is what the slot holds.
    slots: dict[tuple, list] = defaultdict(list)
    for row in rows(conn, f"SELECT * FROM {table}"):
        source = row["source"]
        for column in old_columns:
            if column not in ROW_KEYS[table] and f"{source}_{column}" not in new_columns \
                    and says_something(row[column], defaults[column]):
                report.add("words with no column", "LOST", f"{table}.{column} ({source})",
                           (row["source_id"], row["season"], row[column]))
        key, primary = store.title_of(source, row["media"], row["source_id"])
        if key is None:
            report.add(section, "MISSING", f"{source}: no title for its own id",
                       (source, row["source_id"]))
            continue
        slot = (key, f"S{row['season']}E{row['number']}") if episode else (key, row["season"])
        slots[(source, slot)].append((row, primary))
    for (source, slot), members in slots.items():
        new = new_rows.get(slot)
        if new is None or new[f"{source}_fetched_at"] is None:
            for row, _primary in members:
                report.add(section, "MISSING", source, (source, row["source_id"], slot))
            continue
        same = [all(row[c] == new[f"{source}_{c}"] for c in row.keys()
                    if c not in ROW_KEYS[table] and f"{source}_{c}" in new_columns)
                for row, _primary in members]
        for (row, primary), copied in zip(members, same):
            if copied:
                report.add(section, "found", f"{source}: word for word")
            elif store.since(new[f"{source}_fetched_at"]):
                report.add(section, "found", f"{source}: rewritten since")
            elif any(same):
                report.add(section, "found", f"{source}: another of the service's rows for it was "
                           f"copied ({'a second spelling of the entry' if primary else 'another entry'}"
                           f"; its words stay in {table})")
            else:
                report.add(section, "MISSING", f"{source}: the row holds other words",
                           (source, row["source_id"], slot))


def utc_day(at: float) -> str:
    return (datetime(1970, 1, 1, tzinfo=timezone.utc) + timedelta(seconds=float(at))).strftime("%Y-%m-%d")


def account_listings(conn, store: Store, report: Report, bits: dict[str, int]) -> None:
    section = "calendar_airings -> episodes"
    instants: dict[tuple, list] = defaultdict(list)
    for r in rows(conn, "SELECT * FROM episodes"):
        for source in SOURCES:
            if f"{source}_air_ts" not in store.episode_columns or r[f"{source}_air_ts"] is None:
                continue
            first = {"at": r[f"{source}_air_ts"], "date_only": r[f"{source}_date_only"],
                     "calendars": r[f"{source}_calendars"], "searched": r[f"{source}_searched"],
                     "label": r[f"{source}_label"], "listed_at": r[f"{source}_listed_at"]}
            for at, instant in enumerate([first, *json.loads(r[f"{source}_repeats_json"] or "[]")]):
                instants[(source, r["title_key"])].append((r["unit"], at, instant))
    airings = rows(conn, "SELECT * FROM calendar_airings")
    fed = {(a["source"], a["media"], a["source_id"], a["endpoint"], a["season"],
            a["episode_number"]) for a in airings if not a["from_search"]}
    backed: set[tuple] = set()
    refilled = defaultdict(list)
    for c in rows(conn, "SELECT endpoint, source, span_start, stored_at FROM calendar_coverage"):
        refilled[(c["endpoint"], c["source"])].append((c["span_start"], c["stored_at"]))
    for a in airings:
        source, bit = a["source"], bits.get(a["endpoint"])
        if bit is None:
            report.add(section, "MISSING", f"an unknown calendar {a['endpoint']}", dict(a))
            continue
        if a["from_search"] and (source, a["media"], a["source_id"], a["endpoint"], a["season"],
                                 a["episode_number"]) in fed:
            report.add(section, "stood down for its feed", source)
            continue
        key, _primary = store.title_of(source, a["media"], a["source_id"])
        if key is None:
            report.add(section, "MISSING", f"{source}: no title for its own id",
                       (source, a["source_id"]))
            continue
        season, number = a["season"], a["episode_number"]
        if a["media"] == "movie":
            def fits(unit): return unit == "film"
        elif season != UNSTATED and number != UNSTATED:
            def fits(unit, s=season, n=number): return unit == f"S{s}E{n}"
        else:
            day = "D" + a["air_date"] + (f"S{season}" if season != UNSTATED else "") \
                + (f"E{number}" if number != UNSTATED else "")

            def fits(unit, day=day, s=season, n=number):
                if unit == day:
                    return True
                if not unit.startswith("S"):
                    return False
                us, un = (int(part) for part in unit[1:].split("E"))
                return (s == UNSTATED or us == s) and (n == UNSTATED or un == n)
        hits = [(unit, at, instant) for unit, at, instant in instants.get((source, key), ())
                if instant["at"] == a["air_ts"] and instant["calendars"] & bit and fits(unit)]
        if len(hits) == 1:
            unit, at, instant = hits[0]
            backed.add((source, key, unit, instant["at"], bit))
            searched = bool(instant["searched"] & bit)
            if searched != bool(a["from_search"]) or int(instant["date_only"]) != int(a["date_only"]):
                report.add(section, "MISSING", f"{source}: found, but its searched mark or "
                           "date-only flag differs", dict(a))
            else:
                report.add(section, "found", f"{source}: {'its listing' if at == 0 else 'a repeat'}"
                           f"{' (folded into a numbered episode)' if unit.startswith('S') and season == UNSTATED else ''}")
            continue
        if len(hits) > 1:
            report.add(section, "MISSING", f"{source}: on {len(hits)} rows", dict(a))
            continue
        since = any(span <= a["air_date"] < (date.fromisoformat(span) + timedelta(days=7)).isoformat()
                    and store.since(stored) for span, stored in refilled[(a["endpoint"], source)])
        if since:
            report.add(section, "found", f"{source}: gone, its calendar refilled since")
        else:
            report.add(section, "MISSING", source, dict(a))
    # THE OTHER WAY: no instant appears that no retired listing put there.
    for (source, key), held_instants in instants.items():
        for unit, _at, instant in held_instants:
            for name, bit in bits.items():
                if not instant["calendars"] & bit:
                    continue
                if (source, key, unit, instant["at"], bit) in backed:
                    continue
                if store.since(instant["listed_at"]):
                    report.add("episodes -> calendar_airings", "listed since", source)
                else:
                    report.add("episodes -> calendar_airings", "INVENTED", source,
                               (key, unit, instant["at"], name))


def account_groups(store: Store, report: Report, conn) -> None:
    """Every group in `titles` comes from a retired row, or was written since."""
    retired = {(r["source"], store.follow(store.ids.get((r["source"], r["media"], r["source_id"]),
                                                        (r["title_key"],))[0]))
               for r in rows(conn, "SELECT source, media, source_id, title_key FROM title_metadata")}
    for key, row in store.titles.items():
        for source in SOURCES:
            if f"{source}_id" not in store.title_columns or row[f"{source}_id"] is None:
                continue
            if (source, key) in retired:
                report.add("titles -> title_metadata", "backed", source)
            elif store.since(row[f"{source}_fetched_at"]):
                report.add("titles -> title_metadata", "written since", source)
            else:
                report.add("titles -> title_metadata", "INVENTED", source, (key, row[f"{source}_id"]))


def account_identity(conn, store: Store, report: Report) -> None:
    section = f"{RETIRED_IDENTITY} -> titles"
    for row in rows(conn, f"SELECT * FROM {RETIRED_IDENTITY}"):
        key = store.follow(row["title_key"])
        new = store.titles.get(key)
        if new is None:
            report.add(section, "MISSING", "", row["title_key"])
            continue
        old_ids = json.loads(row["ids_json"] or "{}")
        new_ids = json.loads(new["ids_json"] or "{}")
        if key != row["title_key"]:
            report.add(section, "found", "re-keyed since")
        elif new_ids == old_ids:
            report.add(section, "found", "ids as they were")
        elif all(new_ids.get(k) == v for k, v in old_ids.items()):
            report.add(section, "found", "ids widened since")
        else:
            report.add(section, "MISSING", "ids changed", (key, old_ids, new_ids))


def account_holders(conn, store: Store, report: Report) -> None:
    section = "holders' keys"
    old_titles = {r[0] for r in conn.execute(f"SELECT title_key FROM {RETIRED_IDENTITY}")}

    def resolves(key: str, table: str) -> bool:
        key = ":".join(key.split(":")[:3]) if table == "tier_category_removals" else key
        return key in store.titles or store.aliases.get(key) in store.titles

    def resolved_before(key: str, table: str) -> bool:
        key = ":".join(key.split(":")[:3]) if table == "tier_category_removals" else key
        return key in old_titles or store.aliases.get(key) in old_titles

    for table, sql in HOLDER_KEYS.items():
        if not has_table(conn, table):
            continue
        for (key,) in conn.execute(sql):
            if key is None:
                continue
            key = str(key)
            if resolves(key, table):
                report.add(section, "resolve", table)
            elif table in STORE_HOLDERS:
                report.add(section, "NEWLY UNRESOLVED", table, key)
            elif resolved_before(key, table):
                report.add(section, "NEWLY UNRESOLVED", table, key)
            else:
                report.add(section, "unresolved, as before the conversion", table)


def account_counts(conn, report: Report) -> None:
    for table in ("title_metadata", "season_metadata", "episode_metadata", "calendar_airings"):
        for source, n in conn.execute(f"SELECT source, COUNT(*) FROM {table} GROUP BY source"):
            report.counts[("rows retired", table, source)] += n
    report.counts[("rows retired", RETIRED_IDENTITY, "")] += conn.execute(
        f"SELECT COUNT(*) FROM {RETIRED_IDENTITY}").fetchone()[0]
    for table in ("titles", "service_ids", "seasons", "episodes", "share_highlights"):
        report.counts[("rows now", table, "")] += conn.execute(
            f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    for source in SOURCES:
        report.counts[("groups now", "titles", source)] += conn.execute(
            f"SELECT COUNT(*) FROM titles WHERE {source}_id IS NOT NULL").fetchone()[0]


def account(path: Path) -> int:
    from app.endpoints import ENDPOINTS
    bits = {key: endpoint.bit for key, endpoint in ENDPOINTS.items()}
    conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    try:
        report = Report()
        for check, outcome in (("integrity_check", conn.execute("PRAGMA integrity_check").fetchone()[0]),):
            report.add("PRAGMA", "ok" if outcome == "ok" else "FAILED", check, outcome)
        broken = conn.execute("PRAGMA foreign_key_check").fetchall()
        report.add("PRAGMA", "FAILED" if broken else "ok", "foreign_key_check",
                   broken[:5] if broken else None)
        store = Store(conn)
        print(f"converted at {datetime.fromtimestamp(store.converted_at, timezone.utc):%Y-%m-%d %H:%M} UTC"
              if store.converted_at else "converted at: not recorded")
        account_counts(conn, report)
        account_titles(conn, store, report)
        account_groups(store, report, conn)
        account_descriptions(conn, store, report, "season_metadata")
        account_descriptions(conn, store, report, "episode_metadata")
        account_listings(conn, store, report, bits)
        account_identity(conn, store, report)
        account_holders(conn, store, report)
        report.print()
        bad = report.bad()
        print(f"\n{'NOTHING LOST' if not bad else f'{bad:,} PROBLEM(S)'}")
        return 1 if bad else 0
    finally:
        conn.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("source", type=Path, help="the database to check (opened read-only)")
    parser.add_argument("--keep", type=Path, help="keep the working copy in this directory")
    args = parser.parse_args()
    if not args.source.exists():
        parser.error(f"{args.source} does not exist")
    work = args.keep or Path(tempfile.mkdtemp(prefix="title-store-check-"))
    work.mkdir(parents=True, exist_ok=True)
    if work.resolve() == args.source.resolve().parent:
        parser.error("the working copy must not be written beside its source")
    # The app's modules read their data directory as they are imported: pointed
    # at the working directory before any of them is, so nothing here can reach
    # a real instance's settings or database.
    os.environ["TRAKT_DATA_DIR"] = str(work / "appdata")
    try:
        path = copy_of(args.source, work)
        convert(path)
        return account(path)
    finally:
        if args.keep is None:
            shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
