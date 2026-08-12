"""Incremental watch-history cache for the distrakt tracker, per user.

Fetching per-title progress on every page load was correct but slow (one call per
tracked title). This module caches each user's watch state and keeps it fresh
cheaply:

  - BASELINE: when a title first enters a user's roster it is baselined once from
    the source's per-show progress record -> the exact set of completed episode
    numbers per season, each with the date it was watched (authoritative +
    deduped).
  - INCREMENTAL: on each load we hit the source's activity beacon (a tiny,
    fixed-size "last changed at" blob). If nothing changed, we serve the cache
    with zero further calls. If it changed, we pull only NEW plays from the
    history feed since `last_synced` and fold them in (idempotent: an already-
    known episode is re-stamped with the same date, so day-granularity overlap is
    harmless).
  - MOVIES: the same history sweep carries movie plays, cached with their
    watched_at so a month can list the movies watched during it (POST 2's
    **Movies** section).
  - UNWATCH / FORCE: if the removed_at beacon changes (or the Refresh button
    forces it) we re-baseline from progress and re-seed movies.
    A REMOVAL THAT MOVES NO SUCH BEACON IS INFERRED INSTEAD, because at least one
    service does not move one for it — see _watched_changed, and _sync_one for
    what the inference costs and which sources need it at all.
    A SOURCE THAT CAN SWEEP ITS PER-TITLE PLAY COUNTS IS SWEPT ON EVERY PASS THAT
    GETS PAST THE GATE, because that sweep is a handful of calls for the whole
    library and names the titles whose counts moved in EITHER direction — so a
    removal is found without being inferred, including on an evening that also
    contained ordinary viewing. Only the titles it names are read properly. See
    _rebaseline_by_id and PlayCountPort.

WHICH SOURCES ANSWER is not this module's business: it asks the registry for
every port that can read one person's own viewing and that this account's
preferences admit (providers.for_tracker_ports) rather than naming a service.
What it does still need from a record is each service's OWN id for the title,
because that is what places the call — hence `ids` beside every cached entry.

TWO SOURCES ARE READ INDEPENDENTLY AND KEPT APART. Each has its own beacon and
its own cursor, so an unchanged history on one still costs one call while the
other is being pulled — two sources means two beacon calls, not two full syncs.
And each keeps its OWN episode set per season, because the whole point of asking
two services is that they can legitimately know different things: unioning them
would invent a viewing nobody reported, and picking one would throw away the
disagreement that is the only honest thing to show. A source that could not be
read this pass is named in `unreadable` and its slot is simply left as it was.

WHAT A CACHED ENTRY IS FILED UNDER is the shared title identity
(app/providers/base.py's ItemKey), in its flat string form, so plays reported by
two different services fold into ONE record instead of two that nothing can tell
apart. The flat form specifically because these dicts are serialized to JSON,
where a tuple cannot be a key.

AND THAT KEY IS ALSO HOW A SERVICE IS ASKED ABOUT A TITLE IT WAS NEVER NAMED IN.
Every record on this roster was created from one service, so its `ids` map holds
that service's id and no other's — which, if a baseline could only be placed with
the asked service's own id, would mean a second service was never asked about
anything and its numbers came out as a silent false agreement. A source that can
hand over a whole library at once (app/providers/base.py's LibraryPort) is asked
for it and the answers are matched on the key every row is already filed under,
so the second service's id arrives as a BY-PRODUCT of the match rather than as
its precondition. A source with no such read is still asked per title, with its
own id, exactly as before.

Storage: three per-user SQLite tables (distrakt_watch_state,
distrakt_show_progress, distrakt_movie_watches). In memory:

    {cursors: {source: 'YYYY-MM-DD'},
     beacons: {source: {...}},
     play_counts: {source: {source's own show id: plays}},
     shows:   {key: {ids: {...},
                     seasons: {season: {source: {episode: watched_at}}},
                     watched_all: [source, ...],
                     baselined: [source, ...]}},
     movies:  {key: {ids: {...}, title, year, watched_at, source}}}

`watched_all` NAMES THE SERVICES THAT REPORTED THE WHOLE TITLE WATCHED WITHOUT
ITEMIZING ANY OF IT. A service can file a finished show under a "completed" list
that carries counts instead of episodes — no episode numbers, no dates — and that
is a real answer, not an empty one. Read as "nothing watched" it inverts every
show somebody has finished, which is a silent wrong answer and worse than no
answer at all. It is held per TITLE because that is exactly what the service said,
and what it comes to per season is settled where the season totals are (see
watched_map and app/distrakt/counts.py); manufacturing episode numbers here to fit
the storage would be inventing a viewing history nobody reported.

WHETHER A TITLE HAS BEEN BASELINED IS A FACT PER (title, source), which is what
`baselined` is for. A service that has watches to report says so by having a slot
in `seasons`, or by being named in `watched_all`; a service that was asked and had
NOTHING to report leaves neither, and without a mark of its own that is
indistinguishable from a service that was never asked. Both mistakes are
expensive in opposite directions: read as "never asked" the whole roster is
re-fetched from that service on every load, and read as "asked" a service linked
later is never baselined at all and its counts come out of the month-bounded
history sweep, which is a fraction of what it knows. So `baselined` carries
exactly the services with nothing else to speak for them, and _baselined_sources
is the one place the three are added up.

A SEASON IS PER SOURCE AND A FILM IS NOT, and the asymmetry is deliberate. A
season is a count out of a total, so two services counting differently is a
disagreement somebody has to be shown; a film is a play on a day, and two
services reporting the same play is the same fact twice. Films therefore merge on
identity, keeping the latest play, and carry the name of whichever source
reported that one — which is all the storage needs to file the row.

A sync also leaves the episode plays it just folded in on the state it returns,
read back through episode_plays, and the sources it could not read this pass, as
`unreadable`. Neither is part of the four things above and neither is ever
stored — see _PLAYS.

Episodes carry their watch DATE (they were a bare list of numbers until the
Completed bucket needed to know which month a season was finished in — see
season_completed_map). A date is "" when it is genuinely unknown, which readers
treat as "no answer", never as an old date. _load accepts the old list shape and
reads it as dates-unknown, so a backup taken before the change still restores.

The provider calls authenticate with whatever token is carried on `settings`;
`user_id` scopes the STORAGE only.
"""
from __future__ import annotations

import json
import logging
from datetime import date, datetime, timezone
from typing import NamedTuple

from . import counts, store
from .store import ID_COLUMNS, IDENTITY_COLUMNS, record_key
from .. import clock, db, providers
from ..providers.base import (ItemKey, LibraryPort, Media, PlayCountPort,
                              SourceUnavailable, UnlistedSeasons, collect_ids,
                              item_key, resolve_key)
from ..sources import prefs as source_prefs

logger = logging.getLogger(__name__)
_perf = logging.getLogger("app.perf")

# The id columns these two cache tables carry: the ones a sync places a call
# with, and nothing else. The shared ids the waterfall did not pick live on the
# ROSTER row, which is where a later resolution pass would read them — a cache of
# one service's answers has no use for an id it never calls with.
_CACHE_ID_COLUMNS = {column: ID_COLUMNS[column] for column in ("trakt_id", "simkl_id")}

# Where a sync leaves the episode plays it has just folded in, on the state dict
# it returns. IN MEMORY ONLY: _save writes the four things _load rebuilds and this
# is not among them, deliberately. A play is a signal to act on once — a season
# that has grown, an episode nothing knows about — and a stored copy would have
# every later load replay a decision already taken.
_PLAYS = "plays"

# The season number a title is filed under when it HAS no seasons — when the
# viewer has watched none of it. distrakt_show_progress holds one row per season,
# so such a title would otherwise write no row at all and _load could not tell it
# from one that was never baselined; every load would then re-fetch it from the
# provider. Negative because season 0 is a real season (specials) and a negative
# one cannot be, so nothing can collide with it.
NO_SEASONS = -1

# The season number a title is filed under when a service reports the WHOLE TITLE
# watched without itemizing any of it. A service that files a finished show as
# "completed" and hands over counts instead of episodes has told the truth in a
# shape this table cannot hold: there are no episode numbers to write and no dates
# to write beside them, only the claim. So the claim gets a row of its own, the
# same way "asked, and had nothing" does, and how many episodes it comes to is
# worked out where the season's total is known (app/distrakt/counts.py).
#
# NEGATIVE FOR THE SAME REASON NO_SEASONS IS, and a different number because the
# two are different answers: one says this service has seen NONE of the title and
# the other says it has seen ALL of it, and a reader that confused them would
# invert every finished show.
ALL_SEASONS = -2

# Where an entry records the services that made that claim. A list of source
# names, held per title rather than per season, because that is exactly what the
# service said: it spoke about the title and named no seasons at all.
_WATCHED_ALL = "watched_all"


# Where a sync leaves the sources it could not read this pass, on the state dict
# it returns. IN MEMORY ONLY, for the same reason as _PLAYS: it describes THIS
# pass, and a stored copy would keep saying a service was unreachable long after
# it came back.
_UNREADABLE = "unreadable"

# Where a pass leaves a COMPLETE library read, per source, so the baseline that
# follows a sync does not pay for the same several megabytes twice in one
# request. IN MEMORY ONLY, for the same reason as _PLAYS and _UNREADABLE: it
# describes one pass, and a library is precisely the thing that is out of date the
# moment the person watches something. A PARTIAL read is never left here — it
# cannot answer "does this service hold this title", which is the only question
# the baseline asks it.
_LIBRARY = "library"

# The source a watch is filed under when nothing said which one reported it — a
# state restored from a backup taken before the state was per source, or one
# assembled by a caller that has only a play. It is the source every stored row
# already had, which is what the migration wrote, so reading an unlabelled watch
# as this one's is a statement of fact rather than a guess.
_LEGACY_SOURCE = "trakt"


# Where a source's last play-count sweep sits on the state, {source: {source's
# own show id: plays}}. STORED, unlike the plays and the unreadable list, because
# its whole job is to be compared against the NEXT pass — a copy that did not
# survive the request would answer "everything changed" every time and cost
# exactly what it exists to save.
_PLAY_COUNTS = "play_counts"


def _default_state() -> dict:
    return {"cursors": {}, "beacons": {}, _PLAY_COUNTS: {}, "shows": {}, "movies": {}}


def _stored_document(document: str | None) -> dict:
    """A stored {source: value} column as a dict, or an empty one.

    A document that will not parse reads as absent rather than raising: both
    columns are a CACHE of a service's own answer, so the cost of not
    understanding one is a single extra call on the next load, and refusing to
    load somebody's whole watch state over it would be far worse.
    """
    if not document:
        return {}
    try:
        parsed = json.loads(document)
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _season_slots(stored) -> dict[str, dict[str, str]]:
    """One season's watches as {source: {episode: watched_at}}.

    ACCEPTS THE FLAT SHAPE the state carried while only one service could answer
    — {episode: watched_at}, or the older bare list of episode numbers — and
    reads it as that service's. The two are told apart by their KEYS: an episode
    number is a number, a source name never is. This is not only for backups; it
    is what lets a caller that has only a play hand one in without knowing about
    sources at all.
    """
    if isinstance(stored, dict) and stored and all(
            not str(key).lstrip("-").isdigit() for key in stored):
        return {str(source): episode_watches(eps or {}) for source, eps in stored.items()}
    return {_LEGACY_SOURCE: episode_watches(stored or {})}


def _seasons_by_source(entry: dict) -> dict[str, dict[str, dict[str, str]]]:
    """One title's whole seasons map, every season per source."""
    return {str(season): _season_slots(stored)
            for season, stored in (entry.get("seasons") or {}).items()}


def _watched_all_sources(entry: dict) -> set[str]:
    """The services claiming every episode of this title is watched.

    One accessor because the claim is read in four places — the baseline that
    records it, the save that writes it, the load that reads it back and the map
    that renders it — and a second spelling of "which services claimed this" is
    exactly how one of them would come to disagree with the rest.
    """
    return {str(source) for source in (entry.get(_WATCHED_ALL) or [])}


def _baselined_sources(entry: dict) -> set[str]:
    """Which services have ANSWERED about this title — asked, and replied, whether
    or not they had anything watched to report.

    Three halves, because a baseline leaves three different traces. A service with
    watches is named in the season slots it filled; one claiming the whole title
    watched is named in `watched_all`; a service with nothing at all is named in
    `baselined`, which exists for precisely that case (see the module docstring).
    Adding them up in one accessor is what keeps every caller asking the same
    question — "has THIS service been asked about THIS title" — rather than each
    inventing its own approximation of it.
    """
    sources = {str(source) for source in (entry.get("baselined") or [])}
    sources |= _watched_all_sources(entry)
    for stored in (entry.get("seasons") or {}).values():
        sources.update(_season_slots(stored))
    return sources


def _mark_baselined(entry: dict, source: str) -> None:
    """Record that `source` has answered about this title, and keep the mark
    MINIMAL: a service with a season slot, or with a whole-title claim, is already
    accounted for by that, so listing it here as well would be the same fact stored
    twice and the two copies could then disagree. A title the service turns out to
    have watches for therefore loses its mark on the next baseline, which is
    correct — the slots now say what the mark was standing in for.
    """
    marks = {str(s) for s in (entry.get("baselined") or [])} | {str(source)}
    marks -= _watched_all_sources(entry)
    marks -= {slot_source
              for stored in (entry.get("seasons") or {}).values()
              for slot_source in _season_slots(stored)}
    if marks:
        entry["baselined"] = sorted(marks)
    else:
        entry.pop("baselined", None)


def _row_key(row) -> str:
    return item_key(row["media"], row["match_source"], row["match_id"])


def _row_ids(row) -> dict:
    return collect_ids({id_key: row[column] for column, id_key in _CACHE_ID_COLUMNS.items()})


def _insert_sql(table: str, own_columns: tuple[str, ...]) -> str:
    """An INSERT for one of the two cache tables: the user, the identity, the
    table's own columns, then the ids a call is placed with. Built from the column
    names rather than written out so the placeholder count cannot drift from
    them — miscounting `?` in a hand-written statement is silent until it isn't.
    """
    # `source` is written explicitly rather than left to the column's default.
    # The default is 'trakt' and was right while one service could write; with a
    # second one it would silently file Simkl's rows as Trakt's, and nothing
    # downstream could tell.
    columns = ("user_id", *IDENTITY_COLUMNS, "source", *own_columns, *_CACHE_ID_COLUMNS)
    return (f"INSERT INTO {table} ({', '.join(columns)}) "
            f"VALUES ({', '.join(['?'] * len(columns))})")


def _key_params(key: str) -> tuple:
    """The identity columns for a flat key string, in IDENTITY_COLUMNS order."""
    media, match_source, match_id = str(key).split(":", 2)
    return (media, match_source, match_id)


async def _load(user_id: int) -> dict:
    """Assemble this user's in-memory state dict from the three storage tables.

    A user with no rows yet gets the empty default, exactly as a missing file did.
    """
    state = _default_state()
    ws = await db.fetch_one(
        "SELECT cursors_json, beacons_json, play_counts_json "
        "FROM distrakt_watch_state WHERE user_id = ?",
        (user_id,),
    )
    if ws is not None:
        state["cursors"] = _stored_document(ws["cursors_json"])
        state["beacons"] = _stored_document(ws["beacons_json"])
        state[_PLAY_COUNTS] = _stored_document(ws["play_counts_json"])
    prog = await db.fetch_all(
        "SELECT * FROM distrakt_show_progress WHERE user_id = ?", (user_id,))
    shows: dict = {}
    for row in prog:
        entry = shows.setdefault(_row_key(row), {"ids": _row_ids(row), "seasons": {}})
        # A ROW MERGES ITS IDS INTO THE ENTRY rather than replacing them. Two
        # sources write their own row for one title, each carrying the id it
        # places its calls with, so reading only the first row's ids would leave
        # the other source unable to ask about a title it knows perfectly well.
        entry["ids"].update(_row_ids(row))
        # The NO_SEASONS row exists only to say that THE SERVICE ON IT was asked
        # about this title and had nothing watched to report. Creating the entry
        # and naming that service are its whole purpose, so it is not carried into
        # `seasons` — a caller counting seasons must not find one that does not
        # exist.
        if int(row["season"]) == NO_SEASONS:
            _mark_baselined(entry, str(row["source"] or _LEGACY_SOURCE))
            continue
        # The ALL_SEASONS row is not a season either: it is one service's claim
        # that the whole title has been watched, made without any episode to write
        # down. It becomes a count only where the season totals are (see
        # watched_map and app/distrakt/counts.py).
        if int(row["season"]) == ALL_SEASONS:
            entry[_WATCHED_ALL] = sorted(
                _watched_all_sources(entry) | {str(row["source"] or _LEGACY_SOURCE)})
            continue
        by_source = entry["seasons"].setdefault(str(int(row["season"])), {})
        by_source[str(row["source"] or _LEGACY_SOURCE)] = episode_watches(
            json.loads(row["watched_episodes_json"] or "{}")
        )
    state["shows"] = shows
    movie_rows = await db.fetch_all(
        "SELECT * FROM distrakt_movie_watches WHERE user_id = ?", (user_id,))
    movies: dict = {}
    for row in movie_rows:
        key = _row_key(row)
        watched_at = row["watched_at"] or ""
        previous = movies.get(key)
        # ONE FILM, WHOEVER REPORTED IT. Both services can hold a row for the
        # same play; the later of the two is the one kept, which is the same rule
        # a second play of the same film already takes.
        if previous is not None and (previous.get("watched_at") or "") >= watched_at:
            continue
        movies[key] = {
            "ids": {**(previous or {}).get("ids", {}), **_row_ids(row)},
            "title": row["title"] or "",
            "year": row["year"],
            "watched_at": watched_at,
            "source": str(row["source"] or _LEGACY_SOURCE),
        }
    state["movies"] = movies
    return state


async def _save(user_id: int, state: dict) -> None:
    """Persist a user's whole state back to the three tables in one transaction.

    The progress and movie tables are replaced wholesale for this user rather than
    diffed: a roster is small and bounded, and a full replace is the exact analogue
    of rewriting the single JSON document the state used to live in.

    THE DELETE IS STILL THE WHOLE USER'S ROWS, ACROSS EVERY SOURCE, and that is a
    decision rather than an oversight. `_load` reads every source's rows and this
    writes every source's rows, so the state passed in is always the COMPLETE
    picture for that account — there is no partial save to protect the other
    source's rows from. Scoping the delete by source would only pay off if some
    caller could save one source's half alone, and none can; it would also make
    forgetting a film need a delete per source to actually forget it. If a
    partial save is ever wanted, THAT is the change that has to bring a scoped
    delete with it.
    """
    beacons = state.get("beacons") or {}
    beacons_json = json.dumps(beacons) if beacons else None
    cursors = state.get("cursors") or {}
    cursors_json = json.dumps(cursors) if cursors else None
    play_counts = state.get(_PLAY_COUNTS) or {}
    play_counts_json = json.dumps(play_counts) if play_counts else None
    shows = state.get("shows") or {}
    movies = state.get("movies") or {}
    progress_sql = _insert_sql("distrakt_show_progress",
                               ("season", "watched_episodes_json"))
    movie_sql = _insert_sql("distrakt_movie_watches", ("watched_at", "title", "year"))

    def _ids_params(entry: dict) -> tuple:
        ids = entry.get("ids") or {}
        return tuple(ids.get(id_key) for id_key in _CACHE_ID_COLUMNS.values())

    def _work(conn: db.Connection) -> None:
        conn.execute(
            "INSERT INTO distrakt_watch_state "
            "(user_id, cursors_json, beacons_json, play_counts_json) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET "
            "cursors_json = excluded.cursors_json, beacons_json = excluded.beacons_json, "
            "play_counts_json = excluded.play_counts_json",
            (user_id, cursors_json, beacons_json, play_counts_json),
        )
        conn.execute("DELETE FROM distrakt_show_progress WHERE user_id = ?", (user_id,))
        for key, entry in shows.items():
            seasons = _seasons_by_source(entry)
            # A SERVICE WITH NOTHING WATCHED STILL HAS TO LEAVE A MARK. This table
            # holds one row per (season, service), so a service that was asked
            # about a title and had nothing to report writes no row at all — and
            # _load, which rebuilds the cache from these rows, then cannot tell
            # "asked about, nothing watched" from "never asked about". It read as
            # never-baselined on every load, so sync_and_baseline re-fetched that
            # title from that service every time, for ever. A brand-new premiere
            # is exactly that case, so a month of them cost a fetch each on every
            # page load.
            #
            # NO_SEASONS is not a season and is never rendered as one: _load drops
            # it after using its presence to reconstruct the entry and to name the
            # service that answered. A negative number is safe to reserve because
            # season 0 is real (it is where specials live) but a negative one
            # cannot be.
            #
            # THE MARK NAMES THE SERVICE, one row each, because the question it
            # answers is per service: two of them can be asked about one title and
            # they do not have to agree about having seen none of it. A service
            # with slots needs no mark — the slots already say it answered.
            rows = [(season_s, source, eps)
                    for season_s, by_source in seasons.items()
                    for source, eps in by_source.items()]
            # THE WHOLE-TITLE CLAIM GETS ITS OWN ROW, for the same reason the mark
            # does: it is an answer with no season to hang on, and a service that
            # made it would otherwise read back on the next load as one that was
            # never asked.
            whole = sorted(_watched_all_sources(entry))
            rows += [(ALL_SEASONS, source, {}) for source in whole]
            answered = {source for _season_s, source, _eps in rows}
            marks = [(NO_SEASONS, source, {})
                     for source in sorted(_baselined_sources(entry) - answered)]
            # The fallback covers a state assembled by a caller that never went
            # through a baseline — a restore of a backup taken before the state
            # was per service, say — where the only service that could have
            # written the rows is the one every stored row already carried.
            for season_s, source, eps in (rows + marks or
                                          [(NO_SEASONS, _LEGACY_SOURCE, {})]):
                conn.execute(progress_sql, (
                    user_id, *_key_params(key), str(source), int(season_s),
                    json.dumps(episode_watches(eps or {})), *_ids_params(entry),
                ))
        conn.execute("DELETE FROM distrakt_movie_watches WHERE user_id = ?", (user_id,))
        for key, movie in movies.items():
            conn.execute(movie_sql, (
                user_id, *_key_params(key),
                str((movie or {}).get("source") or _LEGACY_SOURCE),
                (movie or {}).get("watched_at") or "",
                (movie or {}).get("title") or "", (movie or {}).get("year"),
                *_ids_params(movie or {}),
            ))

    await db.transaction(_work)


# ---------------------------------------------------------------------------
# Pure state folders / readers (no I/O — unit-tested directly)
# ---------------------------------------------------------------------------

def episode_watches(stored) -> dict[str, str]:
    """Stored season episodes as {episode: watched_at}.

    Accepts the pre-dates shape (a bare list of episode numbers) and reads it as
    dates-unknown, so a backup taken before dates existed still restores rather
    than being refused or silently losing its counts.

    Public because it is the ONLY reader of what
    `distrakt_show_progress.watched_episodes_json` holds. Anything else that opens
    that column — the tracker's details route does — goes through here, so the
    column's shape has one place to change rather than one per call site.
    """
    if isinstance(stored, dict):
        return {str(int(k)): str(v or "") for k, v in stored.items()}
    return {str(int(n)): "" for n in (stored or [])}


# The four values a sync gates on. Named here because they are also how a stored
# beacon written before the whole blob was kept is recognized — see _beacons.
_GATE_KEYS = ("ep_watched", "ep_removed", "mv_watched", "mv_removed")


def _beacons(la: dict) -> dict:
    """The subset of the activity blob we gate on: episode + movie watched/
    removed timestamps.

    ACCEPTS A STORED GATE BLOB AS WELL AS A SOURCE'S WHOLE ANSWER, and reads the
    first as itself. What is stored is now the source's answer entire, because a
    source may carry detail beside these four that it needs handed back to it next
    time (which lists moved, say) — but a state written before that holds only the
    four. The two are told apart by their KEYS, the same way a season's slots are:
    a gate blob names these four and a source's answer never does. Without this a
    deploy would read every stored beacon as absent, and every account would pay
    for one full re-baseline it did not need.
    """
    la = la or {}
    if any(key in la for key in _GATE_KEYS):
        return {key: la.get(key) for key in _GATE_KEYS}
    ep = la.get("episodes") or {}
    mv = la.get("movies") or {}
    return {
        "ep_watched": ep.get("watched_at"),
        "ep_removed": ep.get("removed_at"),
        "mv_watched": mv.get("watched_at"),
        "mv_removed": mv.get("removed_at"),
    }


def _removed_changed(old: dict | None, new: dict) -> bool:
    """True if an unwatch happened (a *_removed_at beacon moved) — triggers a
    re-baseline since removals don't appear as new history events.

    IT IS THE HONEST SIGNAL AND IT IS NOT ALWAYS SENT. Measured against a live
    Trakt account: removing every play of a season moved `episodes.watched_at`
    and left `episodes.removed_at` null. So this stays the FIRST thing asked —
    a service that says outright that something was removed is believed without
    any inference — and _watched_changed is what covers the service that will
    not say it.
    """
    if not old:
        return False
    return old.get("ep_removed") != new.get("ep_removed") or old.get("mv_removed") != new.get("mv_removed")


def _watched_changed(old: dict | None, new: dict) -> bool:
    """True if a *_watched_at beacon moved since the last pass.

    ON ITS OWN THIS IS NOT A REMOVAL AND MUST NEVER BE READ AS ONE. The watched
    beacon moves every time anybody watches anything, which is the ordinary case
    and precisely the case the gate above it exists to keep cheap. It becomes
    evidence only in combination with a history pull that came back EMPTY, and
    _sync_one is the one place that combination is read — see the comment there
    for what it costs and why it is worth paying.

    THE INFERENCE, STATED ONCE: two things move a service's watch record, plays
    added and plays taken away. The history feed reports the first. So a record
    that moved while the feed accounts for none of the movement moved because
    something was taken away, and that is the removal _removed_changed was
    supposed to report and did not.

    IT IS THE FALLBACK, NOT THE MECHANISM, AND ONLY FOR A SOURCE THAT HAS NEITHER
    BETTER READ. Where a source can sweep its play counts (PlayCountPort) that
    sweep runs on every pass that got past the gate and answers the question
    directly, and this predicate is not consulted at all — see _sync_one.

    AND IT IS NOT SOUND ENOUGH TO GATE ONE. "The history came back empty" is not
    the same as "the history explains the movement": an evening in which somebody
    watches one episode and un-marks a season yields events for the first and
    silence for the second, so the feed is non-empty and the inference never
    fires while a removal has just happened. That is not a corner case; it is what
    an ordinary session looks like when somebody is tidying up. This is kept
    because for a source with no sweep and no library read it is still better than
    nothing, and its limits are written here so nobody mistakes it for a detector.
    """
    if not old:
        return False
    return (old.get("ep_watched") != new.get("ep_watched")
            or old.get("mv_watched") != new.get("mv_watched"))


def _event_key(payload: dict, media: Media) -> ItemKey | None:
    """The identity of the title an event is about, or None when the event names
    no shared id — in which case there is nothing to file it under and nothing
    the tracker could have been counting for it either."""
    return resolve_key(media, collect_ids(payload.get("ids") or {}))


def _set_show_baseline(state: dict, key, ids: dict, season_to_eps: dict,
                       source: str = _LEGACY_SOURCE, *,
                       unlisted: UnlistedSeasons = UnlistedSeasons.SILENT) -> None:
    """Replace ONE SOURCE's cached progress for one title with a fresh baseline.
    `season_to_eps` is what that port's progress read returns,
    {season: {episode: watched_at}}; a bare list of episode numbers is still
    accepted as dates-unknown.

    THE OTHER SOURCE'S SLOTS SURVIVE, including for a season this baseline says
    nothing about. Re-reading Trakt must not erase what Simkl reported, and a
    baseline is one service's complete answer about itself and no statement at
    all about the other. The ids MERGE for the same reason: each service names the
    title with its own id and both are needed to keep asking.

    `unlisted` SAYS WHAT THIS SERVICE'S SILENCE ABOUT A SEASON OF THIS TITLE MEANS
    — see UnlistedSeasons in app/providers/base.py, which is where a source
    declares it and the only place that knows whether it is true of its own
    payload. A service that lists only the seasons it has watches in is saying
    ZERO about the ones it left out; one that reports a finished title without
    itemizing it is saying every episode of it was WATCHED. Recording nothing for
    either leaves the other service's number standing alone, and a season the two
    of them agree about renders as a claim only one of them made.

    A ZERO IS ONLY RECORDED FOR A SEASON THE TRACKER IS ALREADY ASKING ABOUT —
    one that already has a slot from somebody. A library read cannot say how many
    seasons a title has and must not be made to guess: filling in every season of
    every held title would write rows for seasons nobody is watching and nothing
    would ever render. A season whose only slot was this service's, and which it
    now says nothing about, is still retired rather than zeroed; that is an
    unwatch, and it is the one case where silence means the season has gone.

    A WHOLE-TITLE CLAIM IS RECORDED AS ITSELF AND NOT AS A SET OF EPISODES. The
    service that made it handed over no episode numbers and no dates, so writing
    any would be inventing a viewing history to fit this table's shape. It is
    filed against the title, and how many episodes it comes to per season is
    settled where the totals are (see watched_map and app/distrakt/counts.py). It
    is bounded the same way a zero is, for the same reason: only a season somebody
    is already asking about can render at all.
    """
    shows = state.setdefault("shows", {})
    entry = shows.setdefault(str(key), {"ids": {}, "seasons": {}})
    entry["ids"] = {**(entry.get("ids") or {}), **collect_ids(ids or {})}
    seasons = _seasons_by_source(entry)
    for slots in seasons.values():
        slots.pop(str(source), None)
    for season, eps in (season_to_eps or {}).items():
        seasons.setdefault(str(int(season)), {})[str(source)] = episode_watches(eps)
    # THIS BASELINE REPLACES EVERYTHING THIS SERVICE PREVIOUSLY SAID, the claim
    # included: a title it called finished last time and itemizes today must not
    # keep both answers, or the itemized count would render against a claim
    # nothing renewed.
    claimed = _watched_all_sources(entry) - {str(source)}
    if unlisted == UnlistedSeasons.WATCHED:
        claimed.add(str(source))
    if claimed:
        entry[_WATCHED_ALL] = sorted(claimed)
    else:
        entry.pop(_WATCHED_ALL, None)
    if unlisted == UnlistedSeasons.ZERO:
        for slots in seasons.values():
            if slots:  # somebody else is still asking about this season
                slots.setdefault(str(source), {})
    # A season left with no source saying anything is not a season anybody is
    # watching; dropping it here is what keeps an unwatch actually removing it.
    entry["seasons"] = {season: slots for season, slots in seasons.items() if slots}
    # THIS SERVICE HAS NOW ANSWERED ABOUT THIS TITLE, and that has to be recorded
    # even when the answer was "nothing" — otherwise the next load reads it as
    # never asked and pays for the whole roster again. See _baselined_sources.
    _mark_baselined(entry, str(source))


def _apply_episode(state: dict, key, season, number, watched_at=None,
                   source: str = _LEGACY_SOURCE) -> None:
    """Fold one episode play into a cached title, under the source that reported
    it (idempotent). Untracked titles (never baselined) are ignored — only roster
    titles carry counts.

    Keeps the LATEST date seen for an episode, the same rule _apply_movie uses:
    re-watching a season's last episode this month is finishing it this month.
    A play with no date never overwrites one that has a date.
    """
    if key is None or season is None or number is None:
        return
    shows = state.setdefault("shows", {})
    entry = shows.get(str(key))
    if entry is None:  # not baselined -> not on the roster; skip
        return
    seasons = entry.setdefault("seasons", {})
    slots = _season_slots(seasons.get(str(int(season))))
    eps = slots.setdefault(str(source), {})
    n = str(int(number))
    when = str(watched_at or "")
    if when > eps.get(n, ""):
        eps[n] = when
    elif n not in eps:
        eps[n] = when
    seasons[str(int(season))] = slots


def _apply_movie(state: dict, key, ids: dict, title, year, watched_at,
                 source: str = _LEGACY_SOURCE) -> None:
    """Record a watched movie, keeping the latest watched_at (dedup by identity).

    The source rides along so the row can be filed, and it is whichever service
    reported the play being kept — not a claim that only that one knows the film.
    """
    if key is None:
        return
    movies = state.setdefault("movies", {})
    prev = movies.get(str(key))
    if not prev or (watched_at or "") > (prev.get("watched_at") or ""):
        movies[str(key)] = {"ids": {**(prev or {}).get("ids", {}), **collect_ids(ids or {})},
                            "title": title or "", "year": year,
                            "watched_at": watched_at or "", "source": str(source)}


def _apply_event(state: dict, event: dict, source: str = _LEGACY_SOURCE) -> None:
    etype = event.get("type")
    if etype == "episode":
        show = event.get("show") or {}
        ep = event.get("episode") or {}
        _apply_episode(state, _event_key(show, Media.SHOW), ep.get("season"),
                       ep.get("number"), event.get("watched_at"), source)
    elif etype == "movie":
        movie = event.get("movie") or {}
        _apply_movie(state, _event_key(movie, Media.MOVIE), movie.get("ids") or {},
                     movie.get("title"), movie.get("year"), event.get("watched_at"), source)


class EpisodePlay(NamedTuple):
    """One episode the history reported as watched, and nothing beyond what the
    event itself said.

    DELIBERATELY THIN. The shared identity of the title, the season, the episode
    number, the show's name as the event spelled it, and the ids the event
    carried — no stored record read, nothing fetched. That is exactly what makes
    it safe to raise a play for a season the tracker has never heard of: looking
    one of those up would cost a provider call for every unmatched episode of a
    viewing life, which is the cost the whole ask-the-viewer path exists to avoid.

    `ids` IS NOT A SECOND IDENTITY. `key` is what the tracker files a record
    under; these are the service ids that travel with it, and they are here for
    one reason — a season the viewer asks to be added has to be LOOKED UP, and a
    lookup needs the id of the service being asked. Carrying them costs nothing
    because the history event already spelled them out.
    """
    key: ItemKey
    season: int
    number: int
    title: str
    ids: dict


def _episode_play(event: dict) -> EpisodePlay | None:
    """One history event as a play, or None when it is not an episode, names no
    shared id, or does not say which episode it was.

    An event naming no shared id is dropped for the same reason _event_key
    returns None for it: there is no identity to match it against a record, so
    nothing could be said about it either way.
    """
    if event.get("type") != "episode":
        return None
    show = event.get("show") or {}
    episode = event.get("episode") or {}
    key = _event_key(show, Media.SHOW)
    if key is None or episode.get("season") is None or episode.get("number") is None:
        return None
    return EpisodePlay(key, int(episode["season"]), int(episode["number"]),
                       str(show.get("title") or ""),
                       collect_ids(show.get("ids") or {}))


def episode_plays(state: dict) -> list[EpisodePlay]:
    """The episode plays THIS sync folded in, in the order the history reported
    them.

    EMPTY IS THE ORDINARY ANSWER and it is what keeps a routine load a read: the
    activity beacon had not moved, no history was pulled, and there is nothing
    for a caller to reconcile. Empty as well for a state that came out of storage
    rather than out of a sync — see _PLAYS for why the plays are never stored.
    """
    return list(state.get(_PLAYS) or [])


def unreadable_sources(state: dict) -> list[str]:
    """The sources THIS pass could not read, in the order they were asked.

    Empty is the ordinary answer, and empty is also what a state that came out of
    storage says — the fact is about one sync attempt, not about the account, so
    it is never stored. A caller renders it as "that service could not be read"
    beside numbers that are therefore one service's alone; without it, a season
    the two services normally disagree about would silently render as agreement
    the moment one of them went down.
    """
    return list(state.get(_UNREADABLE) or [])


def watched_map(state: dict) -> dict[tuple[str, int], dict[str, int]]:
    """{(item key, season): {source: watched_episode_count}} from the cache.

    A DICT PER SEASON RATHER THAN A NUMBER, because with two services asked there
    may be two answers and neither is wrong — see app/distrakt/counts.py, which
    owns what to do with them. An account reading one service gets a
    single-entry dict and every reader collapses it to the one number, which is
    why this needed no second code path for the overwhelmingly common case.

    A SERVICE THAT REPORTED THE WHOLE TITLE WATCHED ANSWERS counts.ALL_EPISODES
    FOR EVERY SEASON, because that is the honest translation of what it said and
    the number it comes to is not knowable here — the season's total is catalogue
    data this state does not hold. A season this service DID itemize keeps its
    real count: an itemized answer is more specific than a claim about the title,
    and a play folded in since the claim was made is the newer fact.

    IT IS BOUNDED TO THE SEASONS ALREADY IN THE STATE, exactly as a recorded zero
    is: the service named no seasons, so there is no list of them to expand
    against, and a season nobody else is asking about would render nowhere anyway.
    """
    out: dict[tuple[str, int], dict[str, int]] = {}
    for key, entry in (state.get("shows") or {}).items():
        whole = _watched_all_sources(entry)
        for season_s, slots in _seasons_by_source(entry).items():
            season = {source: len(eps or {}) for source, eps in slots.items()}
            for source in whole:
                season.setdefault(source, counts.ALL_EPISODES)
            out[(key, int(season_s))] = season
    return out


def season_counts(state: dict, key, season: int, sources=()) -> dict[str, int]:
    """What each of `sources` says RIGHT NOW about one season of one title, ZERO
    included.

    A SECOND READER RATHER THAN A WIDER watched_map, because the two answer
    opposite questions about a season the state holds nothing for. watched_map is
    deliberately bounded to the seasons somebody is asking about: it feeds the
    rows of a month, and inventing a season nobody is watching would render
    nowhere and cost a lookup to find that out. This one is asked ABOUT a named
    season — one a settled verdict already named — and for that season "no slot"
    is a real answer, not an absence, provided the service has answered about the
    title at all. That distinction is the whole point: a season set back to
    unwatched at both services leaves no slot anywhere (the last source's slot
    going empty retires the season, see _set_show_baseline), so a reader that
    treated a missing season as "nothing known" could never see the retraction it
    was asked to look for.

    A SERVICE THAT HAS NOT ANSWERED ABOUT THE TITLE IS ABSENT rather than zero,
    and _baselined_sources is what says which have — asked and answered, whether
    or not they had anything to report. Zero and never-asked are the two answers
    that must never be confused here: one of them retracts a verdict and the other
    says nothing whatsoever.

    A WHOLE-TITLE CLAIM ANSWERS counts.ALL_EPISODES, exactly as watched_map has
    it, because how many episodes that comes to needs the season's total and the
    total is not in this state (see app/distrakt/counts.py).

    A SEASON WITH NO STORED SLOTS AT ALL IS READ AS NO SLOTS, which is why
    _season_slots is not asked about one. That helper exists to read a season the
    state HOLDS, in either of the shapes it has been stored in, and it resolves an
    empty one to the legacy service's empty set — a sound reading of "this season
    is here and that service has seen none of it", and a wrong one here, where the
    season is not here at all. Taken literally it would report a flat zero for a
    service that had claimed the WHOLE TITLE watched without itemizing a season of
    it, which is a retraction that never happened.
    """
    entry = (state.get("shows") or {}).get(str(key))
    if entry is None:
        return {}
    answered = _baselined_sources(entry)
    whole = _watched_all_sources(entry)
    stored = (entry.get("seasons") or {}).get(str(int(season)))
    slots = _season_slots(stored) if stored else {}
    out: dict[str, int] = {}
    for source in (sources or sorted(answered)):
        name = str(source)
        if name not in answered:
            continue
        if name in slots:
            out[name] = len(slots[name] or {})
        else:
            out[name] = counts.ALL_EPISODES if name in whole else 0
    return out


def season_completed_map(state: dict) -> dict[tuple[str, int], str]:
    """{(item key, season): 'YYYY-MM-DD'} — the day the season's LAST episode was
    watched, which is the day it was finished.

    Says nothing about whether the season is actually complete: that needs the
    episode total, which lives on the show record, so the caller decides (see
    compute_live_shows, which only keeps this for a season it has just bucketed
    as completed). A season with no dated episodes is absent rather than dated
    ""; "I don't know when" and "finished on the epoch" must never be confused.
    """
    out: dict[tuple[str, int], str] = {}
    for key, entry in (state.get("shows") or {}).items():
        for season_s, slots in _seasons_by_source(entry).items():
            # THE LATEST DAY ANY SOURCE REPORTED. Which service saw the last
            # episode go by does not change the day it was seen, and a season
            # finished in July is July's whichever of them said so.
            days = [str(w)[:10] for eps in slots.values() for w in eps.values() if w]
            if days:
                out[(key, int(season_s))] = max(days)
    return out


def movies_in_range(state: dict, start_date: str, end_date: str) -> list[dict]:
    """Movies whose watched_at date falls within [start_date, end_date]
    (YYYY-MM-DD, inclusive), as [{key, ids, title, year, watched_at}]."""
    out = []
    for key, m in (state.get("movies") or {}).items():
        day = (m.get("watched_at") or "")[:10]
        if day and start_date <= day <= end_date:
            # The identity travels with it: the page needs something to name when
            # a film has to be removed, and the title is not an identifier.
            out.append({"key": key, "ids": dict(m.get("ids") or {}),
                        "title": m.get("title") or "", "year": m.get("year"),
                        "watched_at": m.get("watched_at")})
    return out


def month_bounds(month_key: str) -> tuple[str, str]:
    """('YYYY-MM-01', 'YYYY-MM-<last>') for a 'YYYY-MM' key."""
    import calendar as _calendar
    year, month = store.parse_month_key(month_key)
    last = _calendar.monthrange(year, month)[1]
    return f"{year:04d}-{month:02d}-01", f"{year:04d}-{month:02d}-{last:02d}"


def _sweep_cursor(today: date) -> str:
    """The day the NEXT sweep may resume from, having swept up to now.

    TWO CLOCKS ARE IN PLAY AND NEITHER ONE IS SAFE ALONE. A play's `watched_at`
    comes back from every service in UTC, and the cursor is compared against it —
    so a cursor taken from the viewer's local date is ahead of the data wherever
    local time runs ahead of UTC (a morning in Tokyo is still yesterday in UTC).
    But the months this tracker files plays into are the viewer's local months and
    `today` is their local date, so a cursor taken from UTC is ahead of the day the
    viewer is living in wherever UTC runs ahead of local — through the last four
    or five hours of every US evening, which is when somebody is most likely to be
    watching something. Either way round, a cursor in the future means the next
    sweep asks for plays after the ones it is looking for, and those plays are
    never seen again until something forces a wider read.

    SO IT IS THE EARLIER OF THE TWO, AND THE ASYMMETRY IS THE WHOLE REASON. Being
    early costs one extra day of history on one call, and re-seeing a play already
    applied is harmless BY CONTRACT — both SyncPort.fetch_history and the library
    read say so, and the fold is idempotent. Being late loses a play silently and
    permanently. A future reader tempted to tighten this back to one clock is
    trading a free cost for an unrecoverable one.
    """
    return min(datetime.now(timezone.utc).date(), today).isoformat()


def _month_start_of(today: date) -> str:
    return f"{today.year:04d}-{today.month:02d}-01"


def _source_id(entry: dict, source) -> int | None:
    """The id THAT source places a call with, from a cached entry or a roster
    record.

    One accessor because there are several call sites and they must all reach for
    the same thing: the named service's own id, never the shared match id the
    entry is filed under, and never another service's — asking Simkl about a
    Trakt id would either 404 or, far worse, answer about a different title.
    See app/providers/base.py's SyncPort.
    """
    return (entry.get("ids") or {}).get(str(source))


# ---------------------------------------------------------------------------
# Orchestration (provider I/O)
# ---------------------------------------------------------------------------

async def load_state(user_id: int) -> dict:
    """This user's cached watch state, read-only. Public because the backfill
    needs the movies it holds to build a frozen month's **Movies** section, the
    same way the freeze pass does."""
    return await _load(user_id)


async def record_movie_watches(user_id: int, movies: list[dict]) -> int:
    """Fold movie plays into the cache and save; returns how many were recorded.

    For the backfill: the routine sync only ever looks back to the start of the
    CURRENT month, so movies watched before tracking began are recorded nowhere,
    and the ranker imports movies too. Uses the same fold as a normal sync, so a
    movie already known keeps whichever play is later. A film naming no shared id
    is skipped rather than filed under an invented one, and does not count.
    """
    if not movies:
        return 0
    state = await _load(user_id)
    recorded = 0
    for movie in movies:
        ids = collect_ids(movie.get("ids") or {})
        key = resolve_key(Media.MOVIE, ids)
        if key is None:
            continue
        _apply_movie(state, key, ids, movie.get("title"), movie.get("year"),
                     movie.get("watched_at"))
        recorded += 1
    await _save(user_id, state)
    return recorded


async def forget_movie_watch(user_id: int, key) -> str | None:
    """Drop one film from the cache. Returns the day it was recorded on (so the
    caller knows which month has to be re-snapshotted), or None if it was not
    there.

    A film is held once per identity, carrying its latest play, so there is no
    "remove it from March but keep it in May" — forgetting it forgets the watch.
    What this does NOT do is remember that it was forgotten: a later sweep of the
    same range will see the source still reporting it and offer it back, in a plan
    the user confirms.
    """
    state = await _load(user_id)
    movie = (state.get("movies") or {}).pop(str(key), None)
    if movie is None:
        return None
    await _save(user_id, state)
    return str(movie.get("watched_at") or "")


async def tracker_ports(settings, user_id: int) -> list:
    """Every (source, port) pair this account's tracker should read, in order.

    THE PREFERENCE IS READ HERE rather than threaded through every caller: it is
    one small query against the account this sync is already for, so asking for
    it here keeps the dozen call sites that just want "sync this person" exactly
    as they were — which is most of why an account with one linked service
    behaves identically to before.

    WHICH SERVICES COUNT AS LINKED, FOR THE TRACKER, IS WHICH ONES THIS SETTINGS
    OBJECT CARRIES A USABLE CREDENTIAL FOR. That is not a shortcut around auth:
    `_distrakt_settings` builds this object by replacing every source's token
    with THIS account's own, so a source configured on it is a source this
    account has an identity for, and a source it has not linked comes through
    empty. The tracker's half of "linked" and "can be asked" are the same fact
    arrived at from two directions, and reading the identity rows again would be
    a second query to learn something already in hand. (The CALENDAR half is
    where the two genuinely differ — Simkl's calendar needs no token at all — and
    that is where the linked set has to come from auth.)

    An empty list means the cache cannot be advanced; every caller here already
    treats an unanswered sync as "serve what we have", which is the same
    degradation an unreachable source produces.
    """
    prefs = await source_prefs.load(user_id)
    linked = frozenset(str(source) for source, provider in providers.registered().items()
                       if provider.is_configured(settings))
    return providers.for_tracker_ports(prefs, linked, settings)


async def tracker_sources(settings, user_id: int) -> list:
    """Just the source names of tracker_ports, for callers that need to know WHO
    answers rather than how to ask them — the season lookups, which are catalogue
    reads on a different seam entirely."""
    return [source for source, _port in await tracker_ports(settings, user_id)]


async def baseline_show(settings, user_id: int, record: dict) -> None:
    """Baseline one title from every admitted source's progress record. Takes the
    whole record rather than an id, because it needs both halves: each source's id
    to place its call, and the shared identity to file the answers under.

    A source that has no id for this title is skipped rather than asked with
    another service's, and a source that could not be read leaves its slot as it
    was — one service being down must not stop a title being baselined from the
    other. THAT INCLUDES A SOURCE THAT ANSWERED ABOUT NOTHING: an id missing from
    what came back means the service had nothing to say about this title (see
    SyncPort.fetch_progress_details), which is not the same as it saying the title
    has been watched by nobody, and only the second may overwrite a stored count.

    TWO CALLERS, ONE QUESTION. A title entering the roster has never been asked
    about at all; a settled verdict the viewer has just asked the app to reconsider
    has been asked about, but not recently enough to decide anything from — see
    routes.api_distrakt_verdict_readd, where a re-derive from the cache sent the
    row it had withdrawn straight back to completed. Both want the same thing:
    this service's current, complete answer about ONE title, at one call each. So
    both come here rather than the second inventing its own version of it, and
    neither reaches for the whole-roster re-baseline — that is the cost the
    incremental gate exists to avoid and neither caller is asking about the roster.

    THE UNIT IS THE TITLE BECAUSE THAT IS WHAT A SERVICE ANSWERS ABOUT. A progress
    record covers every season at once and there is no cheaper call for one of
    them, so a caller that cares about a single season pays exactly the same.
    """
    ports = await tracker_ports(settings, user_id)
    state = await _load(user_id)
    touched = False
    for source, port in ports:
        source_id = _source_id(record, source)
        if source_id is None:
            continue
        try:
            details = await port.fetch_progress_details(settings, [source_id])
        except SourceUnavailable as exc:
            logger.warning("baseline_show: %s could not be read: %s", source, exc)
            continue
        if int(source_id) not in details:
            continue
        _set_show_baseline(state, record_key(record), record.get("ids") or {},
                           details[int(source_id)], str(source))
        touched = True
    if touched:
        await _save(user_id, state)


async def sync(settings, user_id: int, force: bool = False, today: date | None = None,
               since_month: str | None = None) -> dict:
    """Gated incremental sync (see module docstring). Returns the (saved) state.

    Fast path: the activity beacon is unchanged -> return cache, no history pull.
    Change path: re-baseline on unwatch/force, then fold in new history events.

    `since_month` ("YYYY-MM") RE-READS THAT MONTH'S HISTORY FROM ITS FIRST DAY
    WITHOUT RE-ASKING ABOUT EVERY TITLE — the half of `force` a month being closed
    actually needs. Forcing does two separable things: it re-fetches the progress
    of every title in the cache, one provider call each and growing with
    everything ever tracked, and it winds the cursor back. A freeze needs a
    complete read of ITS OWN month, because its film list will never be recomputed
    afterwards, and almost none of the re-fetch: a settled record already holds the
    counts it settled on, and a premiere record carries no viewer progress at all.

    THE MONTH IS NAMED RATHER THAN INFERRED, and that is the whole point of the
    parameter. Winding the cursor back only moved the start to the month TODAY is
    in — so closing October during November read November's history and never
    touched October's, which is the one month that mattered. `force` has always had
    the same blind spot; it goes unnoticed there because a refresh is only ever
    asked for on the month under way, where the two coincide.
    """
    ports = await tracker_ports(settings, user_id)
    if not ports:
        return await _load(user_id)
    today = today or clock.today()
    state = await _load(user_id)
    plays: list[EpisodePlay] = []
    unreadable: list[str] = []
    failure: SourceUnavailable | None = None
    answered = 0
    touched = False

    for source, port in ports:
        try:
            touched |= await _sync_one(settings, state, source, port, plays,
                                       force=force, today=today, since_month=since_month)
        except SourceUnavailable as exc:
            # PER SOURCE, WHICH IS WHY IT IS CAUGHT AT ALL. One service being
            # unreachable must leave the other's numbers standing rather than
            # sinking the whole tracker — and the season then says so instead of
            # showing a fabricated agreement.
            logger.warning("wh.sync: %s could not be read: %s", source, exc)
            unreadable.append(str(source))
            failure = failure or exc
        else:
            answered += 1

    state[_PLAYS] = plays
    state[_UNREADABLE] = unreadable
    # WHEN NOTHING ANSWERED, THE FAILURE IS THE TRACKER'S, and it is raised the
    # way it always was — the route's own handler renders last-known totals plus
    # a notice at HTTP 200. That is what keeps an account with one linked service
    # behaving exactly as before: with one source, "that source failed" and "the
    # tracker failed" are the same sentence, and this says so without a special
    # case for the number of sources.
    if failure is not None and answered == 0:
        raise failure
    if touched:
        await _save(user_id, state)
    return state


async def _sync_one(settings, state: dict, source, port, plays: list, *,
                    force: bool, today: date, since_month: str | None) -> bool:
    """One source's half of a sync, folded into `state`. True if anything moved.

    Everything here is scoped to `source`: its own beacon, its own cursor, its own
    slot in every season. That is what makes two sources cost two beacon calls
    rather than two full syncs — an unchanged history on one still returns after
    a single call while the other is being pulled.
    """
    from ..perftrace import span
    name = str(source)
    cursors = state.setdefault("cursors", {})
    beacon_by_source = state.setdefault("beacons", {})
    with span("wh.last_activities", source=name):
        la = await port.fetch_last_activities(settings)
    # WHAT THIS SOURCE LAST SAID, kept before anything overwrites it. The gate
    # reads it, and so does a library read, which is handed it back so the source
    # can work out what has moved since — the beacon is not only a gate.
    previous = beacon_by_source.get(name)
    stored = _beacons(previous) if previous else None
    beacons = _beacons(la)

    if (not force and since_month is None
            and cursors.get(name) and stored == beacons):
        _perf.debug("wh.sync GATED for %s (beacon unchanged) — no history pull", name)
        return False

    rebaseline = force or _removed_changed(stored, beacons)

    # A named month is read from its own first day; everything else carries on
    # from the cursor, or from the start of the month today falls in when there is
    # no cursor to carry on from. store.month_first_day both validates the key and
    # spells the date, so a malformed month is refused here rather than reaching
    # the provider as a plausible-looking string. A force re-seeds movie history
    # from the month start, which is what winding the cursor back means.
    start_at = (store.month_first_day(since_month).isoformat() if since_month
                else (None if force else cursors.get(name)) or _month_start_of(today))
    if force:
        cursors[name] = None

    library = port if isinstance(port, LibraryPort) else None
    if library is not None:
        events = await _sync_from_library(
            settings, state, name, library, span,
            start_at=start_at, activities=la,
            # HANDED BACK ONLY WHEN THIS PULL CARRIES ON FROM THE LAST ONE. A
            # source may read only the lists that moved since `since`, which is
            # sound for an incremental pull and wrong for any pull that reaches
            # FURTHER BACK than the last one did — a re-read of an earlier month,
            # or a rebaseline — because a list that has not moved still holds the
            # older plays that pull is there to find.
            since=(None if rebaseline or since_month is not None
                   or not cursors.get(name) else previous))
    else:
        if rebaseline:
            await _rebaseline_by_id(settings, state, name, source, port, span,
                                    reason="force" if force else "unwatch")
        with span("wh.history", start_at=start_at, source=name) as sp:
            events = await port.fetch_history(settings, start_at=start_at)
            sp.set(events=len(events))
        # THE REMOVAL THIS SOURCE WOULD NOT ADMIT TO. Its watch record moved and
        # the feed of plays cannot say that anything was taken away — a removal is
        # not an event — so without something here the stored count keeps the
        # pre-removal number for ever: the sync runs (the beacon moved), the
        # history finds nothing to explain it, and nothing re-reads the progress.
        #
        # ONLY A SOURCE WHOSE INCREMENTAL READ IS AN APPEND-ONLY FEED NEEDS THIS,
        # which is why it sits on this branch alone rather than above the split. A
        # source that hands over its LIBRARY re-states what it currently holds for
        # every title it names, and folding that in replaces those slots outright —
        # so a removal inside a list it re-reads corrects itself, and a whole title
        # dropped from the library moves the removed beacon that path already gates
        # on. A feed of plays can only ever be folded forward and never subtracts,
        # and that is the defect.
        #
        # A SOURCE THAT CAN SWEEP ITS PLAY COUNTS IS ASKED EVERY PASS, and this is
        # the whole reason the sweep was worth building. It is a handful of calls
        # for the entire library, so it is cheap enough to run whenever anything
        # moved at all — and it answers the question directly rather than inferring
        # it, naming the titles whose counts went up OR down and leaving everything
        # else untouched.
        #
        # WHAT THIS REPLACED, AND WHY IT HAD TO. The gate here used to be "the
        # beacon moved AND the history came back EMPTY", on the reasoning that new
        # plays ACCOUNT for the movement and leave nothing to infer. They do not.
        # An evening in which somebody watches one episode and un-marks a season
        # produces events for the first and nothing at all for the second, so the
        # history is non-empty, the branch is skipped, and the removal is missed —
        # observed exactly that way, on a load whose history returned two events
        # while a season's plays had just been taken away. That reasoning was only
        # ever tolerable because the answer cost one call per tracked title; with a
        # sweep it buys nothing and hides the case it was written for.
        #
        # A SOURCE WITH NEITHER READ still falls back to the inference, which is the
        # best that can be done for one: see _watched_changed for what it can and
        # cannot say, and _removed_changed for the measurement that made it
        # necessary.
        if not rebaseline:
            if isinstance(port, PlayCountPort):
                await _rebaseline_by_id(settings, state, name, source, port, span,
                                        reason="changed")
            elif not events and _watched_changed(stored, beacons):
                await _rebaseline_by_id(settings, state, name, source, port, span,
                                        reason="unwatch-implied")

    for event in events:
        _apply_event(state, event, name)
        # Taken from the RAW event rather than from the fold above, because the
        # two answer different questions: the fold counts progress for titles the
        # tracker already has baselined and drops everything else, while a play
        # for a title it has never heard of is precisely the one a caller has to
        # ask the viewer about.
        play = _episode_play(event)
        if play is not None:
            plays.append(play)
    _forget_play_counts_for(state, name, source, events)

    cursors[name] = _sweep_cursor(today)
    # THE WHOLE BLOB, not the four values the gate compares. A source may carry
    # its own detail alongside them and be handed it back on the next pull; what
    # the gate needs is derived from it either way (see _beacons).
    beacon_by_source[name] = la
    return True


def _forget_play_counts_for(state: dict, name: str, source, events: list) -> None:
    """Drop the stored play count of every title this pass folded PLAYS into.

    THE STORED COUNT IS A CLAIM THAT THE APP'S PROGRESS FOR A TITLE MATCHES THAT
    NUMBER, and folding an event in breaks the claim. The two reads are not
    consistent with each other at the same instant: measured against the live
    service, marking a season watched appeared in the history feed IMMEDIATELY
    while that show's row in the whole-library listing still carried its old count
    for some seconds after. So a pass can advance a title's progress from the fast
    read while recording the slow read's number beside it, and from then on the
    two describe different moments.

    WHY THAT IS NOT MERELY UNTIDY. Marking a season watched and un-marking it
    returns the count to exactly where it started — 31 to 39 and back to 31, on
    the account this was found on, with the row's own "last updated" stamp
    reverting with it. If the intermediate number was never stored, the removal
    arrives at a count EQUAL to the stored one, nothing looks changed, and the
    season keeps the episodes the events had folded in. Nothing about the count is
    unreliable; it was compared against a baseline that had drifted.

    SO A TITLE THIS PASS TOUCHED KEEPS NO COUNT AT ALL, and the next sweep sees it
    as one it has never had a number for — which reads as changed, and buys one
    targeted re-read of a title the viewer has just been watching. That is the same
    title the events already named, so it is a call per title watched rather than
    per title tracked, and it is what makes the pair of reads honest with each
    other again.
    """
    stored = (state.get(_PLAY_COUNTS) or {}).get(name)
    if not stored:
        return
    for event in events:
        show_id = ((event.get("show") or {}).get("ids") or {}).get(str(source))
        if show_id is not None:
            stored.pop(str(show_id), None)


def _moved_ids(source, before: dict | None, sweep) -> int:
    """How many ids the sweep says moved, BEFORE they are narrowed to the titles
    this account tracks. Reported beside that narrowed count so the two can be
    told apart — see the span in _rebaseline_by_id.
    """
    if before is None or not sweep.complete:
        return len(sweep.counts)
    return len({show_id for show_id, plays in sweep.counts.items()
                if before.get(str(show_id)) != plays}
               | {show_id for show_id in before if str(show_id) not in sweep.counts})


def _changed_titles(cached: dict, source, before: dict | None, sweep) -> dict:
    """The subset of `cached` a play-count sweep says is worth re-reading.

    TWO WAYS A TITLE QUALIFIES, and the second is the one a naive comparison
    misses. Its count MOVED — in either direction, because a count that only ever
    rose would be blind to exactly the removals this is here to catch. Or it has
    VANISHED from the listing while the store still holds a count for it, which is
    what a title losing its last play looks like: the source stops listing it at
    all rather than listing it at zero, so an absence is a removal and not merely
    a title nobody has watched.

    NOTHING AT ALL STORED MEANS EVERYTHING QUALIFIES. There is no previous sweep
    to have moved against, so this can say nothing, and saying nothing has to mean
    "ask about all of them" — the alternative reads a first sweep as proof that
    nothing has changed, which is the one answer it cannot support. That is the
    first pass after this ships, and it costs exactly what every pass used to.

    ONE KNOWN FALSE POSITIVE, and it is a cost rather than a wrong answer: a
    RE-WATCH raises the count without changing the watched set, so it buys one
    needless re-read of a title the viewer has just been watching. Suppressing it
    would need the per-episode data this sweep does not carry, which is the whole
    reason the sweep is cheap.
    """
    if before is None or not sweep.complete:
        return cached
    moved = {show_id for show_id, plays in sweep.counts.items()
             if before.get(str(show_id)) != plays}
    moved |= {show_id for show_id in before if str(show_id) not in sweep.counts}
    return {key: entry for key, entry in cached.items()
            if str(_source_id(entry, source)) in moved}


async def _rebaseline_by_id(settings, state: dict, name: str, source, port, span, *,
                            reason: str) -> None:
    """Re-read cached titles' progress from a source that answers per title.

    Only titles this source has its OWN id for can be asked about at all, which is
    the limit this path has always had; a source that can hand over its whole
    library is not on it (see _sync_from_library), because for that one the
    question "which titles may I ask about" does not arise.

    WHICH OF THEM ARE ASKED ABOUT IS THE SOURCE'S OWN ANSWER WHERE IT HAS ONE.
    Asking about every cached title is one provider call each and grows with
    everything ever tracked — measured at 146 sequential calls and six and a half
    seconds on one real account, for a page that took eight and a half. A source
    that can sweep its per-title play counts for the whole library in a handful of
    calls (app/providers/base.py's PlayCountPort) is asked that first, and only
    the titles whose counts actually moved are read properly. A source with no
    such sweep is still asked about all of them, which is what this always did.
    """
    cached = {key: entry for key, entry in (state.get("shows") or {}).items()
              if _source_id(entry, source) is not None}
    stored = state.setdefault(_PLAY_COUNTS, {})
    unread: set[str] = set()
    sweep = None
    if isinstance(port, PlayCountPort):
        # NOT CAUGHT HERE. A sweep that could not be read raises, and this whole
        # source's pass is then degraded by the caller — named on the page, stored
        # rows left alone. Falling back to asking about every title would be
        # placing a hundred and forty-six calls on a credential that has just
        # refused one.
        with span("wh.play_counts", source=name) as sp:
            sweep = await port.fetch_play_counts(settings)
            before = stored.get(name)
            narrowed = _changed_titles(cached, source, before, sweep)
            # THE THREE NUMBERS THAT TELL THE THREE FAILURES APART, and they are
            # here because "n=0" on its own cannot: a sweep that saw nothing move,
            # a sweep that saw things move that this account does not track, and a
            # sweep whose ids do not line up with the ones the cache files titles
            # under all read identically without them. `moved` is what the source
            # says changed; `n` is what survives being intersected with the titles
            # the tracker actually holds.
            sp.set(swept=len(sweep.counts), stored=len(before or {}),
                   moved=_moved_ids(source, before, sweep), n=len(narrowed),
                   complete=sweep.complete)
        cached = narrowed
    # SPLIT, because the two halves fail slowly for unrelated reasons and the
    # combined number could not tell them apart: the fetch is one provider call
    # per show, paced by the outbound rate gate, and grows with the roster; the
    # apply is pure CPU on the event loop over whatever came back. A rebaseline
    # that is slow in the fetch is waiting on the provider; one that is slow in
    # the apply is blocking every other request while it runs.
    with span("wh.rebaseline", n=len(cached), source=name, reason=reason):
        with span("wh.rebaseline.fetch", n=len(cached)):
            details = await port.fetch_progress_details(
                settings, [_source_id(entry, source) for entry in cached.values()])
        with span("wh.rebaseline.apply", n=len(cached)):
            for key, entry in cached.items():
                # A TITLE THE ANSWER DID NOT NAME IS LEFT EXACTLY AS IT WAS. Its
                # id is missing from what came back, which means this service had
                # nothing to say about it (see SyncPort.fetch_progress_details) —
                # a call that failed, not a person who has watched none of it.
                # Read as the second, one flaky request retires a season's stored
                # episodes and nothing anywhere says why.
                source_id = int(_source_id(entry, source))
                if source_id not in details:
                    unread.add(str(source_id))
                    continue
                _set_show_baseline(
                    state, key, entry.get("ids") or {}, details[source_id], name)
    if sweep is not None and sweep.complete:
        # STORED ONLY NOW, AND WITHOUT THE TITLES THIS PASS FAILED TO READ. The
        # stored map is a claim that the app's counts are up to date with these
        # numbers, so recording a count for a title whose progress read failed
        # would say the next sweep has nothing to do about it — and the wrong
        # number would stand until something else happened to that title. Leaving
        # those ids out makes the next sweep see them as changed and try again.
        #
        # AN INCOMPLETE SWEEP IS NOT STORED AT ALL, for the reason PlayCounts
        # gives: it may say what it found and never what is missing, and a stored
        # partial map would have the next comparison read every title on a page it
        # never fetched as having lost its plays.
        stored[name] = {show_id: plays for show_id, plays in sweep.counts.items()
                        if show_id not in unread}


def _titles_with_evidence(shows: dict, source: str) -> set[str]:
    """The titles `source` currently has WATCH EVIDENCE for: a season slot of its
    own holding at least one episode, or a whole-title claim.

    THE "ASKED, AND IT HAD NOTHING" MARK IS DELIBERATELY NOT EVIDENCE, and neither
    is a recorded zero. Both already say the service has seen nothing of the
    title, so replacing them with the same statement destroys nothing — and
    counting them would make a roster of untouched titles look like a library
    worth protecting, which would jam the floor below permanently shut for the
    accounts that need it least.
    """
    held: set[str] = set()
    for key, entry in (shows or {}).items():
        if str(source) in _watched_all_sources(entry):
            held.add(str(key))
            continue
        for stored in (entry.get("seasons") or {}).values():
            if _season_slots(stored).get(str(source)):
                held.add(str(key))
                break
    return held


def _may_retire_rows(shows: dict, source: str, read) -> bool:
    """Whether a COMPLETE library read may be allowed to retire what it does not
    name. False refuses the destructive half of the fold.

    THIS IS A FLOOR UNDER THE DESTRUCTIVE WRITE, AND IT IS DELIBERATELY
    REDUNDANT. Do not remove it because the paths above it look correct — being
    redundant is its entire job. Retiring every one of a service's rows and
    replacing them with "asked, and it had nothing" is the single most damaging
    thing this module can do: watch history is not re-derivable from anything the
    app holds, the page goes on looking healthy because the OTHER service's rows
    are untouched, and nobody is told. It has already happened once, from a read
    that reported itself complete and successful while every underlying call was
    being refused. The provider-level fixes close that particular route; this
    exists to be standing there whatever the next one turns out to be.

    THE RULE IS A SHAPE, NOT A THRESHOLD, and that is what makes it right for a
    viewer with three titles as well as one with three hundred. What made the
    observed case obviously wrong was not the number of rows: it was that a
    source went from holding a great deal to holding NOTHING on the strength of
    one read. So a complete read that names not one of the titles this source has
    evidence for is not believed — it has demonstrated nothing about the library
    it claims to have read entirely — and it is treated as partial: what it did
    name is folded in, and nothing is retired.

    THE PRICE IS PAID KNOWINGLY. A viewer who genuinely empties their whole
    library at the service keeps stale counts until they watch one thing, because
    a read that names a single held title clears this and retires the rest
    correctly. That is a wrong answer the viewer caused and can see; the
    alternative is a wrong answer nobody caused and nobody can see, which costs
    them their history.
    """
    held = _titles_with_evidence(shows, source)
    if not held or held & set(read.entries):
        return True
    logger.error(
        "wh: refusing to retire %s's stored watch history for %d title(s) — a read "
        "claiming to cover the whole library named none of them. Their rows are "
        "left as they were and the read is treated as partial.", source, len(held))
    return False


def _fold_library(state: dict, name: str, read) -> bool:
    """Re-baseline the cached titles a library read speaks to, and say whether the
    read was treated as COMPLETE.

    A COMPLETE READ SPEAKS TO EVERY CACHED TITLE, including by silence: a title
    the whole library does not hold is a title this service has seen none of, and
    saying so is what retires slots it used to fill and what leaves the "asked,
    and it had nothing" mark behind. A PARTIAL read speaks only about the titles
    it actually named — it skipped lists that had not changed, so the absence of a
    title from it says nothing, and touching one on that basis would erase a
    perfectly good count.

    EITHER WAY THIS IS WHERE A MARK IS LIFTED. A title this service knew nothing
    about last time and holds today arrives in the read the moment the list it
    landed in moves, and folding it in replaces the mark with real counts. That is
    why the mark means "as of this library state" rather than "for ever": the
    author's Simkl library overlaps their roster only incidentally today, and an
    import that fills it in must not leave the tracker reporting an empty answer
    from before it.

    AND A COMPLETE READ IS ONLY BELIEVED THIS FAR IF IT CLEARS THE FLOOR — see
    _may_retire_rows, which is where the reason lives.
    """
    shows = state.get("shows") or {}
    if read.complete and not _may_retire_rows(shows, name, read):
        # Treated exactly as a partial read from here on: what it named is folded
        # in, nothing is retired, and the caller is told it was not complete so it
        # does not reuse this read to answer "does this service hold this title".
        read = read._replace(complete=False)
    if read.complete:
        for key, entry in list(shows.items()):
            found = read.entries.get(str(key))
            # A TITLE THE READ DID NOT NAME HAS NO ENTRY AND THEREFORE NO CLAIM
            # ABOUT ITS SEASONS. That is the whole reason the flag rides on the
            # entry: this service holding a title and having seen none of a season
            # is a zero, and this service not holding the title at all is silence,
            # and there is no way to reach for the first while looking at the
            # second.
            _set_show_baseline(state, key, found.ids if found else (entry.get("ids") or {}),
                               found.seasons if found else {}, name,
                               unlisted=(found.unlisted_seasons if found
                                         else UnlistedSeasons.SILENT))
        return True
    for key, found in read.entries.items():
        if str(key) in shows:
            _set_show_baseline(state, key, found.ids, found.seasons, name,
                               unlisted=found.unlisted_seasons)
    return False


async def _sync_from_library(settings, state: dict, name: str, library, span, *,
                             start_at, activities, since) -> list[dict]:
    """One source's half of a sync when it can hand over the whole library.

    ONE READ, THREE ANSWERS: which titles this service holds, what it has seen of
    each of them, and the plays inside that window. The alternative is a progress
    read and a history read over the same buckets, which doubles the most
    expensive call there is here for data the first read already carried.
    """
    with span("wh.library", source=name, start_at=start_at or "") as sp:
        read = await library.fetch_library(settings, start_at=start_at,
                                           activities=activities, since=since)
        sp.set(titles=len(read.entries), events=len(read.events),
               complete=read.complete)
    # THE FOLD DECIDES WHETHER THE READ COUNTS AS COMPLETE, not the read itself:
    # a complete read that fails the floor is downgraded there, and stashing it
    # here on its own say-so would hand the baseline that follows a read the fold
    # has just refused to believe.
    if _fold_library(state, name, read):
        state[_LIBRARY] = {**(state.get(_LIBRARY) or {}), name: read}
    return read.events


async def _baseline_from_library(settings, state: dict, name: str, library,
                                 missing: dict[str, dict], span) -> bool:
    """Baseline every title in `missing` for one source, out of ONE library read.

    THE READ HAS TO BE COMPLETE, because a title's absence from it is the answer
    for half of them: on the account this was measured against, 80 of 146 roster
    titles are genuinely not in the Simkl library, and each of those has to come
    away marked as asked-and-nothing or the whole roster is re-read on every page
    load. A partial read cannot say that, so one is never reused here.

    The sync that ran a moment ago may already have made a complete read; taking
    it from there rather than repeating it is the difference between one pass over
    a library and two in the same request.
    """
    read = (state.get(_LIBRARY) or {}).get(name)
    if read is None:
        try:
            with span("wh.baseline_library", n=len(missing), source=name):
                read = await library.fetch_library(settings)
        except SourceUnavailable as exc:
            # The same per-source degradation the sync takes: a title left
            # un-baselined for one service still gets the other's count, and the
            # next load tries again.
            logger.warning("wh.baseline_library: %s could not be read: %s", name, exc)
            state.setdefault(_UNREADABLE, [])
            if name not in state[_UNREADABLE]:
                state[_UNREADABLE].append(name)
            return False
        state[_LIBRARY] = {**(state.get(_LIBRARY) or {}), name: read}
    for key, record in missing.items():
        found = read.entries.get(key)
        # THE SERVICE'S OWN ID ARRIVES HERE, off the matched library entry, and it
        # is what lets the per-title paths ask about this title directly from now
        # on. It was never needed to place THIS call, which is the point.
        _set_show_baseline(state, key,
                           {**(record.get("ids") or {}), **(found.ids if found else {})},
                           found.seasons if found else {}, name,
                           unlisted=(found.unlisted_seasons if found
                                     else UnlistedSeasons.SILENT))
    return True


async def sync_and_baseline(settings, user_id: int, roster: list[dict], force: bool = False,
                            today: date | None = None,
                            since_month: str | None = None) -> dict:
    """`sync`, then guarantee every roster title has a baseline (so titles that
    entered via calendar/rollover/history — not the manual add flow — still get
    counts on first view). Returns the state; read counts via `watched_map` and
    movies via `movies_in_range`.

    Takes the roster RECORDS rather than a list of ids, because filing a baseline
    needs the shared identity and fetching one needs the source's id, and only the
    record carries both."""
    from ..perftrace import span
    state = await sync(settings, user_id, force=force, today=today,
                       since_month=since_month)
    ports = await tracker_ports(settings, user_id)
    # WHO HAS BEEN ASKED, PER (title, source) — never per title alone. A title is
    # baselined once per SERVICE, because a baseline is one service's complete
    # answer about itself: skipping a title merely because SOME service has filed
    # it means a service linked later is never baselined for anything the first
    # one already knew, and its counts then come only from the history sweep,
    # which reaches back to the start of the current month and no further. That
    # renders as a nearly-empty library rather than as a service nobody asked.
    #
    # SNAPSHOTTED BEFORE THE LOOP, not read live, so the answer is about the state
    # as previous sessions left it. Baselining a title for the first source adds
    # it to `state["shows"]` and marks that source on it; taking the snapshot up
    # front keeps this pass's own writes out of the predicate, so the loop cannot
    # come to depend on the order the sources happen to be asked in.
    already = {key: _baselined_sources(entry)
               for key, entry in (state.get("shows") or {}).items()}
    saved = False
    for source, port in ports:
        missing: dict[str, dict] = {}
        # A SOURCE THAT CAN HAND OVER ITS WHOLE LIBRARY IS NEVER SKIPPED FOR WANT
        # OF AN ID. Asking per title needs this service's own id for the title,
        # and a roster built from another service carries none — which is how a
        # second service came to be asked about nothing at all while every number
        # on the page quietly came from the first. Matching a library on the
        # shared identity removes the precondition instead of trying to satisfy
        # it, and the id is what comes back.
        library = port if isinstance(port, LibraryPort) else None
        for record in roster or []:
            key = str(record_key(record))
            if (str(source) in already.get(key, ()) or key in missing
                    or (library is None and _source_id(record, source) is None)):
                continue
            missing[key] = record
        if not missing:
            continue
        if library is not None:
            saved |= await _baseline_from_library(settings, state, str(source), library,
                                                  missing, span)
            continue
        try:
            with span("wh.baseline_missing", n=len(missing), source=str(source)):
                details = await port.fetch_progress_details(
                    settings, [_source_id(r, source) for r in missing.values()])
        except SourceUnavailable as exc:
            # The same per-source degradation the sync above takes: a title left
            # un-baselined for one service still gets the other's count, and the
            # next load tries again.
            logger.warning("wh.baseline_missing: %s could not be read: %s", source, exc)
            state.setdefault(_UNREADABLE, [])
            if str(source) not in state[_UNREADABLE]:
                state[_UNREADABLE].append(str(source))
            continue
        for key, record in missing.items():
            # A TITLE THE ANSWER DID NOT NAME IS LEFT ALONE, not baselined at zero
            # — the service could not be read about it, which says nothing (see
            # SyncPort.fetch_progress_details). It stays un-baselined for this
            # service and is asked about again on the next load.
            source_id = int(_source_id(record, source))
            if source_id not in details:
                continue
            _set_show_baseline(state, key, record.get("ids") or {},
                               details[source_id], str(source))
        saved = True
    if saved:
        await _save(user_id, state)
    return state
