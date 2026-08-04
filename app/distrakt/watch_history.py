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
    forces it) we re-baseline every cached title from progress and re-seed movies.

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

Storage: three per-user SQLite tables (distrakt_watch_state,
distrakt_show_progress, distrakt_movie_watches). In memory:

    {cursors: {source: 'YYYY-MM-DD'},
     beacons: {source: {...}},
     shows:   {key: {ids: {...}, seasons: {season: {source: {episode: watched_at}}}}},
     movies:  {key: {ids: {...}, title, year, watched_at, source}}}

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

from . import store
from .store import ID_COLUMNS, IDENTITY_COLUMNS, record_key
from .. import clock, db, providers
from ..providers.base import (ItemKey, Media, SourceUnavailable, collect_ids, item_key,
                              resolve_key)
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


# Where a sync leaves the sources it could not read this pass, on the state dict
# it returns. IN MEMORY ONLY, for the same reason as _PLAYS: it describes THIS
# pass, and a stored copy would keep saying a service was unreachable long after
# it came back.
_UNREADABLE = "unreadable"

# The source a watch is filed under when nothing said which one reported it — a
# state restored from a backup taken before the state was per source, or one
# assembled by a caller that has only a play. It is the source every stored row
# already had, which is what the migration wrote, so reading an unlabelled watch
# as this one's is a statement of fact rather than a guess.
_LEGACY_SOURCE = "trakt"


def _default_state() -> dict:
    return {"cursors": {}, "beacons": {}, "shows": {}, "movies": {}}


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
        "SELECT cursors_json, beacons_json FROM distrakt_watch_state WHERE user_id = ?",
        (user_id,),
    )
    if ws is not None:
        state["cursors"] = _stored_document(ws["cursors_json"])
        state["beacons"] = _stored_document(ws["beacons_json"])
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
        # The NO_SEASONS row exists only to say the title WAS baselined and has
        # nothing watched. Creating the entry above is its whole purpose, so it is
        # not carried into `seasons` — a caller counting seasons must not find one
        # that does not exist.
        if int(row["season"]) == NO_SEASONS:
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
            "INSERT INTO distrakt_watch_state (user_id, cursors_json, beacons_json) "
            "VALUES (?, ?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET "
            "cursors_json = excluded.cursors_json, beacons_json = excluded.beacons_json",
            (user_id, cursors_json, beacons_json),
        )
        conn.execute("DELETE FROM distrakt_show_progress WHERE user_id = ?", (user_id,))
        for key, entry in shows.items():
            seasons = _seasons_by_source(entry)
            # A TITLE WITH NOTHING WATCHED STILL HAS TO LEAVE A MARK. This table
            # holds one row per SEASON, so a title the viewer has seen none of
            # writes no rows at all — and _load, which rebuilds the cache from
            # these rows, then cannot tell "asked about, nothing watched" from
            # "never asked about". It read as never-baselined on every load, so
            # sync_and_baseline re-fetched it from the provider every time, for
            # ever. A brand-new premiere is exactly that case, so a month of them
            # cost a fetch each on every page load.
            #
            # NO_SEASONS is not a season and is never rendered as one: _load drops
            # it after using its presence to reconstruct the entry. A negative
            # number is safe to reserve because season 0 is real (it is where
            # specials live) but a negative one cannot be.
            #
            # THE MARK IS PER SOURCE TOO, because the question it answers is per
            # source: with a title nobody has watched any of, `seasons` is empty
            # and there is no source name to file the mark under, so it is
            # written for the account's primary — one row, exactly as before.
            rows = [(season_s, source, eps)
                    for season_s, by_source in seasons.items()
                    for source, eps in by_source.items()]
            for season_s, source, eps in (rows or [(NO_SEASONS, _LEGACY_SOURCE, {})]):
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


def _beacons(la: dict) -> dict:
    """The subset of the activity blob we gate on: episode + movie watched/
    removed timestamps."""
    la = la or {}
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
    re-baseline since removals don't appear as new history events."""
    if not old:
        return False
    return old.get("ep_removed") != new.get("ep_removed") or old.get("mv_removed") != new.get("mv_removed")


def _event_key(payload: dict, media: Media) -> ItemKey | None:
    """The identity of the title an event is about, or None when the event names
    no shared id — in which case there is nothing to file it under and nothing
    the tracker could have been counting for it either."""
    return resolve_key(media, collect_ids(payload.get("ids") or {}))


def _set_show_baseline(state: dict, key, ids: dict, season_to_eps: dict,
                       source: str = _LEGACY_SOURCE) -> None:
    """Replace ONE SOURCE's cached progress for one title with a fresh baseline.
    `season_to_eps` is what that port's progress read returns,
    {season: {episode: watched_at}}; a bare list of episode numbers is still
    accepted as dates-unknown.

    THE OTHER SOURCE'S SLOTS SURVIVE, including for a season this baseline says
    nothing about. Re-reading Trakt must not erase what Simkl reported, and a
    baseline is one service's complete answer about itself and no statement at
    all about the other. The ids MERGE for the same reason: each service names the
    title with its own id and both are needed to keep asking.
    """
    shows = state.setdefault("shows", {})
    entry = shows.setdefault(str(key), {"ids": {}, "seasons": {}})
    entry["ids"] = {**(entry.get("ids") or {}), **collect_ids(ids or {})}
    seasons = _seasons_by_source(entry)
    for slots in seasons.values():
        slots.pop(str(source), None)
    for season, eps in (season_to_eps or {}).items():
        seasons.setdefault(str(int(season)), {})[str(source)] = episode_watches(eps)
    # A season left with no source saying anything is not a season anybody is
    # watching; dropping it here is what keeps an unwatch actually removing it.
    entry["seasons"] = {season: slots for season, slots in seasons.items() if slots}


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
    """
    out: dict[tuple[str, int], dict[str, int]] = {}
    for key, entry in (state.get("shows") or {}).items():
        for season_s, slots in _seasons_by_source(entry).items():
            out[(key, int(season_s))] = {
                source: len(eps or {}) for source, eps in slots.items()}
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


def _now_date_iso() -> str:
    return datetime.now(timezone.utc).date().isoformat()


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
    """Baseline one title from every admitted source's progress record (called
    when it enters the roster). Takes the whole record rather than an id, because
    it needs both halves: each source's id to place its call, and the shared
    identity to file the answers under.

    A source that has no id for this title is skipped rather than asked with
    another service's, and a source that could not be read leaves its slot as it
    was — one service being down must not stop a title being baselined from the
    other.
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
        _set_show_baseline(state, record_key(record), record.get("ids") or {},
                           details.get(int(source_id)) or {}, str(source))
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
    beacons = _beacons(la)

    if (not force and since_month is None
            and cursors.get(name) and beacon_by_source.get(name) == beacons):
        _perf.debug("wh.sync GATED for %s (beacon unchanged) — no history pull", name)
        return False

    if force or _removed_changed(beacon_by_source.get(name), beacons):
        cached = {key: entry for key, entry in (state.get("shows") or {}).items()
                  if _source_id(entry, source) is not None}
        # SPLIT, because the two halves fail slowly for unrelated reasons and the
        # combined number could not tell them apart: the fetch is one provider
        # call per show, paced by the outbound rate gate, and grows with the
        # roster; the apply is pure CPU on the event loop over whatever came back.
        # A rebaseline that is slow in the fetch is waiting on the provider; one
        # that is slow in the apply is blocking every other request while it runs.
        with span("wh.rebaseline", n=len(cached), source=name,
                  reason="force" if force else "unwatch"):
            with span("wh.rebaseline.fetch", n=len(cached)):
                details = await port.fetch_progress_details(
                    settings, [_source_id(entry, source) for entry in cached.values()])
            with span("wh.rebaseline.apply", n=len(cached)):
                for key, entry in cached.items():
                    _set_show_baseline(
                        state, key, entry.get("ids") or {},
                        details.get(int(_source_id(entry, source))) or {}, name)
    if force:
        cursors[name] = None  # re-seed movie history from the month start

    # A named month is read from its own first day; everything else carries on
    # from the cursor, or from the start of the month today falls in when there is
    # no cursor to carry on from. store.month_first_day both validates the key and
    # spells the date, so a malformed month is refused here rather than reaching
    # the provider as a plausible-looking string.
    start_at = (store.month_first_day(since_month).isoformat() if since_month
                else cursors.get(name) or _month_start_of(today))
    with span("wh.history", start_at=start_at, source=name) as sp:
        events = await port.fetch_history(settings, start_at=start_at)
        for event in events:
            _apply_event(state, event, name)
            # Taken from the RAW event rather than from the fold above, because
            # the two answer different questions: the fold counts progress for
            # titles the tracker already has baselined and drops everything else,
            # while a play for a title it has never heard of is precisely the one
            # a caller has to ask the viewer about.
            play = _episode_play(event)
            if play is not None:
                plays.append(play)
        sp.set(events=len(events))

    cursors[name] = _now_date_iso()
    beacon_by_source[name] = beacons
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
    # SNAPSHOTTED BEFORE THE LOOP, not read live. Baselining a title for the
    # first source adds it to `state["shows"]`, and reading the live dict would
    # then make the second source skip every title the first had just filled in —
    # leaving it permanently blank for that service.
    already = set(state.get("shows") or {})
    saved = False
    for source, port in ports:
        missing: dict[str, dict] = {}
        for record in roster or []:
            key = str(record_key(record))
            if key in already or key in missing or _source_id(record, source) is None:
                continue
            missing[key] = record
        if not missing:
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
            _set_show_baseline(state, key, record.get("ids") or {},
                               details.get(int(_source_id(record, source))) or {},
                               str(source))
        saved = True
    if saved:
        await _save(user_id, state)
    return state
