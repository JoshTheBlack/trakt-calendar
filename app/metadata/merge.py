"""Two of the store's titles becoming one, and one service's entry moving from
one title to another — each service's group decided on its own.

A RE-KEY MERGES ROWS. When a title learns an id that names a title the store
already holds (app/metadata/identity.py's `settle`), everything filed under the
old key becomes the other title's: its row's service groups, its seasons, its
episodes and every one of its services' own ids. Two rows of one table for one
title cannot both stay, so each SERVICE'S GROUP is decided on its own:

  - one title has the service's group: it is the merged title's;
  - both do: the two are two of the service's ENTRIES for one title, and the
    title's group is the entry the store's conversion would have chosen had the
    two always been one title (app/db.py's MIGRATION_49 and
    app/metadata/rows.py's `_entry_rank`): the one with THE MOST LISTINGS, then
    the more complete, then the lowest id. The other becomes a `service_ids`
    row of the title — still found by its id — and its words are logged, not
    kept: a group is one entry's, never a patchwork of two, which is the rule
    every season and episode slot follows too (only the title's own entry's
    words) because another entry numbers its own seasons.

A LISTING IS NEVER LOST IN A MERGE: two services' listings of one episode are
one row whatever the two titles were called, one service's two listings of one
episode become one listing and its repeats, and the listings that name no full
episode are re-decided against the merged title's numbered ones (app/metadata/
episodes.py's `Book.refold`).

AN ENTRY MOVES WITH ITS OWN ID. A service re-points its shared ids and never its
own, so when a listing or an answer files an entry under a different title than
it was — with no re-key behind it — the entry's group (where it was its title's),
its seasons' and episodes' words, its listings and its ids go with it, and the
title it lands on keeps its own group if it has one (a group is kept while its
entry exists).

RARE, AND WRITTEN ROW BY ROW: a re-key or a move is a handful of titles in a
fill of thousands, and its own statements are excluded from the store's fixed
statement count the way a re-key's repointing always has been.
"""
from __future__ import annotations

import json
import logging
from collections.abc import Iterable, Mapping
from dataclasses import replace
from typing import Any

from .. import db
from . import episodes, groups, tables

logger = logging.getLogger(__name__)


def _id_order(own: str) -> tuple:
    return (len(own), own) if own.isdigit() else (float("inf"), own)


def _rows(conn: db.Connection, table: str, where: str, params) -> list[dict]:
    cursor = conn.execute(f"SELECT * FROM {table} WHERE {where}", params)
    names = [column[0] for column in cursor.description]
    return [dict(zip(names, found)) for found in cursor.fetchall()]


def _title(conn: db.Connection, key: str) -> dict | None:
    found = _rows(conn, tables.TITLES, "title_key = ?", (key,))
    return found[0] if found else None


def _own(row: Mapping[str, Any] | None, source: str) -> str | None:
    """The entry a title row's group of `source` is, or None."""
    if row is None or row.get(f"{source}_id") is None:
        return None
    return str(row[f"{source}_id"])


def _group(row: Mapping[str, Any] | None, table: str, source: str, *,
           listing: bool = True) -> dict:
    """`source`'s group on a whole row, by its prefixed column names — its empty
    values where there is no row. `listing` False leaves out an episode's
    listing columns, which a merge restates from the instants."""
    return {f"{source}_{name}": (row[f"{source}_{name}"] if row is not None
                                 else groups.EMPTY[name])
            for name in groups.every(table, source)
            if listing or name not in groups.LISTING}


def _present(row: Mapping[str, Any] | None, table: str, source: str) -> bool:
    name = groups.PRESENT[table]
    return (row is not None and groups.has(table, source, name)
            and row.get(f"{source}_{name}") is not None)


def _listings(book: episodes.Book, key: str, source: str, own: str | None) -> int:
    """How many of the title's listings — an instant on each of its calendars —
    are entry `own`'s; an instant with no entry is the title's own group's."""
    return sum(bin(instant.calendars).count("1")
               for row in book.of_title(key) for instant in row.instants.get(source, ())
               if (instant.entry or own) == own)


def _rank(row: Mapping[str, Any], source: str, listings: int) -> tuple:
    """`rows._entry_rank` over a stored group: the most listings, then answered,
    then more words, then the newer — the larger wins."""
    words = sum(1 for name in groups.WORDS[tables.TITLES]
                if groups.has(tables.TITLES, source, name)
                and row[f"{source}_{name}"] not in (None, "", "[]", "{}"))
    return (listings, int(row[f"{source}_enriched"] or 0), words,
            int(row[f"{source}_fetched_at"] or 0))


def _dropped(source: str, key: str, row: Mapping[str, Any]) -> None:
    """A group that is no longer its title's, logged in full — the log is the
    only record of it left."""
    said = {name: value for name, value in _group(row, tables.TITLES, source).items()
            if value not in (None, "", "[]", "{}", 0)}
    logger.warning("title store: %s's entry %s is no longer the group of %s; it said: %s",
                   source, row.get(f"{source}_id"), key,
                   json.dumps(said, default=str, sort_keys=True))


def _slot(preferred: Mapping[str, Any] | None, other: Mapping[str, Any] | None,
          table: str, source: str, *, other_is_loser: bool) -> dict:
    """One season's or episode's description by `source`, as the merged title
    keeps it: the winning side's; the other side's only where the winner says
    nothing and the other is no other entry's (a description nobody's group
    still stands behind); else nothing."""
    if _present(preferred, table, source):
        return _group(preferred, table, source, listing=False)
    if _present(other, table, source) and not other_is_loser:
        return _group(other, table, source, listing=False)
    return {name: groups.EMPTY[name.removeprefix(f"{source}_")]
            for name in _group(None, table, source, listing=False)}


def _restate(book: episodes.Book, key: str, source: str, owner: str | None,
             winner: str | None, *, belongs=lambda actual: True,
             into: str | None = None) -> set[tuple[int, str]]:
    """Each of `source`'s instants on `key`'s rows whose entry `belongs` —
    moved to the same unit of `into` where one is named — with its entry
    restated against the title's group (`winner`): none where it is that
    entry's. An instant with no entry is `owner`'s, the group it sat under.
    Returns every (calendar, day) a moved listing that names no full episode
    sits on."""
    folds: set[tuple[int, str]] = set()
    for row in book.of_title(key):
        for instant in list(row.instants.get(source, ())):
            actual = instant.entry or owner
            if not belongs(actual):
                continue
            restated = None if actual == winner else actual
            if into is None:
                if instant.entry != restated:
                    instant.entry = restated
                    row.changed = True
                continue
            row.instants[source].remove(instant)
            row.changed = True
            book.row(into, row.unit, row.media, row.season, row.number).add(
                source, replace(instant, entry=restated))
            if instant.unstated:
                folds.update((bit, instant.day) for bit in episodes.bits(instant.calendars))
    return folds


def _write_episodes(conn: db.Connection, book: episodes.Book, keys: Iterable[str],
                    descriptions: Mapping[tuple[str, str], dict]) -> None:
    """Every episode row of `keys` written back whole from the book — each
    service's listing from its instants, its description as `descriptions`
    says — and a row nothing lists or describes removed."""
    keys = list(dict.fromkeys(keys))
    conn.execute(f"DELETE FROM {tables.EPISODES} WHERE title_key IN "
                 f"({', '.join('?' * len(keys))})", keys)
    names = episodes.columns()
    for key in keys:
        for row in book.of_title(key):
            values = {"title_key": row.key, "unit": row.unit, "media": row.media,
                      "season": row.season, "number": row.number}
            # Every service's group empty, then what `descriptions` says — which
            # may name one service's alone (an entry's words moving on their own).
            values.update({name: groups.EMPTY[name.split("_", 1)[1]] for name in names
                           if name not in values})
            values.update(descriptions.get((row.key, row.unit)) or {})
            for source in groups.sources():
                values.update({f"{source}_{name}": value for name, value in
                               episodes.listing_columns(source, row.instants[source]).items()})
            described = any(values.get(f"{source}_fetched_at") is not None
                            for source in groups.sources())
            if row.listed or described:
                conn.execute(f"INSERT INTO {tables.EPISODES} ({', '.join(names)}) "
                             f"VALUES ({', '.join('?' * len(names))})",
                             tuple(values.get(name) for name in names))


def titles(conn: db.Connection, old: str, new: str, *, now: int) -> dict[str, int]:
    """Everything the store holds under `old` merged into `new`, inside the
    caller's transaction — see the module docstring for each service's group.
    The identity row of `old` is left for app/metadata/identity.py to retire.
    Returns {table: rows of `old`'s moved or merged}."""
    old_row, new_row = _title(conn, old), _title(conn, new)
    book = episodes.Book()
    book.hold(episodes.read_titles(conn, (old, new)))
    counts = {tables.TITLES: 0, tables.SEASONS: 0, tables.EPISODES: 0, tables.SERVICE_IDS: 0}
    if old_row is not None and new_row is None:
        # NOTHING TO MERGE WITH: the row is copied under the new key whole; its
        # identity is identity.py's to write once the re-key is.
        names = [name for name in old_row if name != "title_key"]
        conn.execute(f"INSERT INTO {tables.TITLES} (title_key, {', '.join(names)}) "
                     f"SELECT ?, {', '.join(names)} FROM {tables.TITLES} WHERE title_key = ?",
                     (new, old))
        counts[tables.TITLES] = 1

    winners: dict[str, str | None] = {}
    old_wins: dict[str, bool] = {}
    written: dict[str, Any] = {}
    for source in groups.sources():
        mine, theirs = _own(old_row, source), _own(new_row, source)
        if mine is not None and theirs is not None:
            first = _rank(old_row, source, _listings(book, old, source, mine))
            second = _rank(new_row, source, _listings(book, new, source, theirs))
            wins = first > second if first != second else _id_order(mine) < _id_order(theirs)
            _dropped(source, new, new_row if wins else old_row)
        else:
            wins = mine is not None
        old_wins[source] = wins
        winners[source] = mine if wins else theirs
        if wins and new_row is not None:
            written.update(_group(old_row, tables.TITLES, source))
            counts[tables.TITLES] += 1
    if written:
        conn.execute(f"UPDATE {tables.TITLES} SET "
                     + ", ".join(f"{name} = ?" for name in written) + " WHERE title_key = ?",
                     (*written.values(), new))

    # ---- every own id under the new key, the title's group's entry primary
    for source in groups.sources():
        if _own(old_row, source) is not None and _own(new_row, source) is not None:
            conn.execute(f"UPDATE {tables.SERVICE_IDS} SET is_primary = 0 "
                         "WHERE source = ? AND title_key = ?",
                         (source, new if old_wins[source] else old))
    counts[tables.SERVICE_IDS] = conn.execute(
        f"UPDATE {tables.SERVICE_IDS} SET title_key = ? WHERE title_key = ?", (new, old)).rowcount

    # ---- seasons: each service's slot from the title's group's entry
    old_seasons = {int(r["season"]): r for r in _rows(conn, tables.SEASONS, "title_key = ?", (old,))}
    new_seasons = {int(r["season"]): r for r in _rows(conn, tables.SEASONS, "title_key = ?", (new,))}
    conn.execute(f"DELETE FROM {tables.SEASONS} WHERE title_key IN (?, ?)", (old, new))
    for season in sorted(set(old_seasons) | set(new_seasons)):
        values: dict[str, Any] = {"title_key": new, "season": season}
        for source in groups.sources():
            sides = (old_seasons.get(season), new_seasons.get(season))
            preferred, other = sides if old_wins[source] else sides[::-1]
            loser = _own(new_row if old_wins[source] else old_row, source) is not None
            values.update(_slot(preferred, other, tables.SEASONS, source, other_is_loser=loser))
        if any(values.get(f"{source}_fetched_at") is not None for source in groups.sources()):
            conn.execute(f"INSERT INTO {tables.SEASONS} ({', '.join(values)}) "
                         f"VALUES ({', '.join('?' * len(values))})", tuple(values.values()))
    counts[tables.SEASONS] = len(old_seasons)

    # ---- episodes: descriptions from the group's entry, every listing kept
    stored_old = {row.unit: dict(row.stored) for row in book.of_title(old) if row.stored}
    stored_new = {row.unit: dict(row.stored) for row in book.of_title(new) if row.stored}
    for row in book.of_title(old):
        book.row(new, row.unit, row.media, row.season, row.number)
    folds: set[tuple[int, str]] = set()
    for source in groups.sources():
        # THE NEW TITLE'S OWN INSTANTS FIRST: an instant with no entry is the
        # entry its row's group was, which only holds until the old title's
        # instants — restated against the winner — arrive beside them.
        _restate(book, new, source, _own(new_row, source), winners[source])
        folds |= _restate(book, old, source, _own(old_row, source), winners[source], into=new)
    for bit, day in sorted(folds | book.days(new, ~0)):
        book.refold(new, bit, day)
    descriptions: dict[tuple[str, str], dict] = {}
    for row in book.of_title(new):
        values = {}
        for source in groups.sources():
            sides = (stored_old.get(row.unit), stored_new.get(row.unit))
            preferred, other = sides if old_wins[source] else sides[::-1]
            loser = _own(new_row if old_wins[source] else old_row, source) is not None
            values.update(_slot(preferred, other, tables.EPISODES, source, other_is_loser=loser))
        descriptions[(new, row.unit)] = values
    counts[tables.EPISODES] = len(stored_old)
    _write_episodes(conn, book, (old, new), descriptions)
    return counts


def entry(conn: db.Connection, source: str, media: str, own: str, old: str, new: str, *,
          now: int) -> None:
    """One service's entry `own` taken from title `old` to title `new`, inside
    the caller's transaction — see the module docstring."""
    source, own = str(source), str(own)
    conn.execute(f"INSERT INTO {tables.TITLES} (title_key, media, created_at) VALUES (?, ?, ?) "
                 "ON CONFLICT(title_key) DO NOTHING", (new, str(media), now))
    old_row, new_row = _title(conn, old), _title(conn, new)
    was_group = _own(old_row, source) == own
    theirs = _own(new_row, source)
    winner = theirs if theirs is not None else (own if was_group else None)
    if was_group:
        if theirs is None:
            conn.execute(f"UPDATE {tables.TITLES} SET "
                         + ", ".join(f"{name} = ?" for name in _group(old_row, tables.TITLES, source))
                         + " WHERE title_key = ?",
                         (*_group(old_row, tables.TITLES, source).values(), new))
        else:
            _dropped(source, new, old_row)
        conn.execute(f"UPDATE {tables.TITLES} SET "
                     + ", ".join(f"{name} = ?" for name in _group(None, tables.TITLES, source))
                     + " WHERE title_key = ?", (*_group(None, tables.TITLES, source).values(), old))

    # ---- its ids: the spelling it is, and every spelling of the group it was
    spellings = [own]
    if was_group:
        spellings += [str(spelled) for (spelled,) in conn.execute(
            f"SELECT service_id FROM {tables.SERVICE_IDS} WHERE source = ? AND media = ? "
            "AND title_key = ? AND is_primary = 1", (source, str(media), old))]
    spellings = list(dict.fromkeys(spellings))
    conn.execute(f"UPDATE {tables.SERVICE_IDS} SET title_key = ?, is_primary = ? "
                 "WHERE source = ? AND media = ? AND service_id IN "
                 f"({', '.join('?' * len(spellings))})",
                 (new, int(winner == own), source, str(media), *spellings))

    # ---- its seasons' and episodes' words go where its group goes
    book = episodes.Book()
    book.hold(episodes.read_titles(conn, (old, new)))
    old_seasons = _rows(conn, tables.SEASONS, "title_key = ?", (old,))
    if was_group:
        for row in old_seasons:
            if not _present(row, tables.SEASONS, source):
                continue
            if winner == own:
                group = _group(row, tables.SEASONS, source)
                conn.execute(f"INSERT INTO {tables.SEASONS} (title_key, season) VALUES (?, ?) "
                             "ON CONFLICT(title_key, season) DO NOTHING", (new, row["season"]))
                conn.execute(f"UPDATE {tables.SEASONS} SET "
                             + ", ".join(f"{name} = ?" for name in group)
                             + " WHERE title_key = ? AND season = ?",
                             (*group.values(), new, row["season"]))
            empty = _group(None, tables.SEASONS, source)
            conn.execute(f"UPDATE {tables.SEASONS} SET "
                         + ", ".join(f"{name} = ?" for name in empty)
                         + " WHERE title_key = ? AND season = ?",
                         (*empty.values(), old, row["season"]))
    stored = {(row.key, row.unit): dict(row.stored or {}) for key in (old, new)
              for row in book.of_title(key)}
    if was_group:
        for row in book.of_title(old):
            moved = _group(row.stored, tables.EPISODES, source, listing=False)
            if winner == own and _present(stored.get((old, row.unit)), tables.EPISODES, source):
                book.row(new, row.unit, row.media, row.season, row.number)
                stored.setdefault((new, row.unit), {}).update(moved)
            stored[(old, row.unit)].update(_group(None, tables.EPISODES, source, listing=False))

    # The landing title's own instants first, for the reason `titles` gives.
    _restate(book, new, source, theirs, winner)
    folds = _restate(book, old, source, _own(old_row, source), winner,
                     belongs=lambda actual: actual == own, into=new)
    for key in (old, new):
        for bit, day in sorted(book.days(key, ~0) | (folds if key == new else set())):
            book.refold(key, bit, day)
    names = episodes.columns()
    descriptions = {ref: {name: values[name] for name in names
                          if name in values and name not in groups.IDENTITY[tables.EPISODES]
                          and name.split("_", 1)[1] not in groups.LISTING}
                    for ref, values in stored.items()}
    _write_episodes(conn, book, (old, new), descriptions)
    logger.info("title store: %s's entry %s moved from %s to %s", source, own, old, new)
