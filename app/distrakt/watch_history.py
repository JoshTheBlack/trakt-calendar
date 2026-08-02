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

WHICH SOURCE ANSWERS is not this module's business: it asks the registry for the
port that can read one person's own viewing (providers.for_tracker) rather than
naming a service. What it does still need from a record is that service's OWN id
for the title, because that is what places the call — hence `ids` beside every
cached entry.

WHAT A CACHED ENTRY IS FILED UNDER is the shared title identity
(app/providers/base.py's ItemKey), in its flat string form, so plays reported by
two different services fold into ONE record instead of two that nothing can tell
apart. The flat form specifically because these dicts are serialized to JSON,
where a tuple cannot be a key.

Storage: three per-user SQLite tables (distrakt_watch_state,
distrakt_show_progress, distrakt_movie_watches). In memory:

    {last_synced, beacons,
     shows:  {key: {ids: {...}, seasons: {season: {episode: watched_at}}}},
     movies: {key: {ids: {...}, title, year, watched_at}}}

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

from .store import ID_COLUMNS, IDENTITY_COLUMNS, record_key
from .. import clock, db, providers
from ..providers.base import ItemKey, Media, collect_ids, item_key, resolve_key

logger = logging.getLogger(__name__)
_perf = logging.getLogger("app.perf")

# The id columns these two cache tables carry: the ones a sync places a call
# with, and nothing else. The shared ids the waterfall did not pick live on the
# ROSTER row, which is where a later resolution pass would read them — a cache of
# one service's answers has no use for an id it never calls with.
_CACHE_ID_COLUMNS = {column: ID_COLUMNS[column] for column in ("trakt_id", "simkl_id")}


def _default_state() -> dict:
    return {"last_synced": None, "beacons": None, "shows": {}, "movies": {}}


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
    columns = ("user_id", *IDENTITY_COLUMNS, *own_columns, *_CACHE_ID_COLUMNS)
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
        "SELECT last_synced, beacons_json FROM distrakt_watch_state WHERE user_id = ?",
        (user_id,),
    )
    if ws is not None:
        state["last_synced"] = ws["last_synced"]
        state["beacons"] = json.loads(ws["beacons_json"]) if ws["beacons_json"] else None
    prog = await db.fetch_all(
        "SELECT * FROM distrakt_show_progress WHERE user_id = ?", (user_id,))
    shows: dict = {}
    for row in prog:
        entry = shows.setdefault(_row_key(row), {"ids": _row_ids(row), "seasons": {}})
        entry["seasons"][str(int(row["season"]))] = episode_watches(
            json.loads(row["watched_episodes_json"] or "{}")
        )
    state["shows"] = shows
    movie_rows = await db.fetch_all(
        "SELECT * FROM distrakt_movie_watches WHERE user_id = ?", (user_id,))
    state["movies"] = {
        _row_key(row): {
            "ids": _row_ids(row),
            "title": row["title"] or "",
            "year": row["year"],
            "watched_at": row["watched_at"] or "",
        }
        for row in movie_rows
    }
    return state


async def _save(user_id: int, state: dict) -> None:
    """Persist a user's whole state back to the three tables in one transaction.

    The progress and movie tables are replaced wholesale for this user rather than
    diffed: a roster is small and bounded, and a full replace is the exact analogue
    of rewriting the single JSON document the state used to live in.
    """
    beacons = state.get("beacons")
    beacons_json = None if beacons is None else json.dumps(beacons)
    last_synced = state.get("last_synced")
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
            "INSERT INTO distrakt_watch_state (user_id, last_synced, beacons_json) "
            "VALUES (?, ?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET "
            "last_synced = excluded.last_synced, beacons_json = excluded.beacons_json",
            (user_id, last_synced, beacons_json),
        )
        conn.execute("DELETE FROM distrakt_show_progress WHERE user_id = ?", (user_id,))
        for key, entry in shows.items():
            for season_s, eps in (entry.get("seasons") or {}).items():
                conn.execute(progress_sql, (
                    user_id, *_key_params(key), int(season_s),
                    json.dumps(episode_watches(eps or {})), *_ids_params(entry),
                ))
        conn.execute("DELETE FROM distrakt_movie_watches WHERE user_id = ?", (user_id,))
        for key, movie in movies.items():
            conn.execute(movie_sql, (
                user_id, *_key_params(key), (movie or {}).get("watched_at") or "",
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


def _set_show_baseline(state: dict, key, ids: dict, season_to_eps: dict) -> None:
    """Replace one title's cached progress with a fresh baseline. `season_to_eps`
    is what the port's progress read returns, {season: {episode: watched_at}}; a
    bare list of episode numbers is still accepted as dates-unknown."""
    state.setdefault("shows", {})[str(key)] = {
        "ids": collect_ids(ids or {}),
        "seasons": {
            str(int(season)): episode_watches(eps)
            for season, eps in (season_to_eps or {}).items()
        },
    }


def _apply_episode(state: dict, key, season, number, watched_at=None) -> None:
    """Fold one episode play into a cached title (idempotent). Untracked titles
    (never baselined) are ignored — only roster titles carry counts.

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
    eps = entry.setdefault("seasons", {}).setdefault(str(int(season)), {})
    n = str(int(number))
    when = str(watched_at or "")
    if when > eps.get(n, ""):
        eps[n] = when
    elif n not in eps:
        eps[n] = when


def _apply_movie(state: dict, key, ids: dict, title, year, watched_at) -> None:
    """Record a watched movie, keeping the latest watched_at (dedup by identity)."""
    if key is None:
        return
    movies = state.setdefault("movies", {})
    prev = movies.get(str(key))
    if not prev or (watched_at or "") > (prev.get("watched_at") or ""):
        movies[str(key)] = {"ids": collect_ids(ids or {}), "title": title or "",
                            "year": year, "watched_at": watched_at or ""}


def _apply_event(state: dict, event: dict) -> None:
    etype = event.get("type")
    if etype == "episode":
        show = event.get("show") or {}
        ep = event.get("episode") or {}
        _apply_episode(state, _event_key(show, Media.SHOW), ep.get("season"),
                       ep.get("number"), event.get("watched_at"))
    elif etype == "movie":
        movie = event.get("movie") or {}
        _apply_movie(state, _event_key(movie, Media.MOVIE), movie.get("ids") or {},
                     movie.get("title"), movie.get("year"), event.get("watched_at"))


def watched_map(state: dict) -> dict[tuple[str, int], int]:
    """{(item key, season): watched_episode_count} from the cache."""
    out: dict[tuple[str, int], int] = {}
    for key, entry in (state.get("shows") or {}).items():
        for season_s, eps in (entry.get("seasons") or {}).items():
            out[(key, int(season_s))] = len(eps or [])
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
        for season_s, eps in (entry.get("seasons") or {}).items():
            days = [str(w)[:10] for w in (eps or {}).values() if w] if isinstance(eps, dict) else []
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
    year, month = int(month_key[:4]), int(month_key[5:7])
    last = _calendar.monthrange(year, month)[1]
    return f"{year:04d}-{month:02d}-01", f"{year:04d}-{month:02d}-{last:02d}"


def _now_date_iso() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def _month_start_of(today: date) -> str:
    return f"{today.year:04d}-{today.month:02d}-01"


def _source_id(entry: dict) -> int | None:
    """The id the port places a call with, from a cached entry or a roster record.

    One accessor because there are three call sites and they must all reach for
    the same thing: the SOURCE's own id, never the shared match id the entry is
    filed under. See app/providers/base.py's SyncPort.
    """
    ids = entry.get("ids") or {}
    return ids.get("trakt") or ids.get("simkl")


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


def _port():
    """The registry's private-read port, or None when no registered source can
    read one person's own viewing. None means the cache simply cannot be advanced
    — every caller here already treats an unanswered sync as "serve what we have",
    which is the same degradation an unreachable source produces."""
    return providers.for_tracker()


async def baseline_show(settings, user_id: int, record: dict) -> None:
    """Baseline one title from the source's progress record (called when it enters
    the roster). Takes the whole record rather than an id, because it needs both
    halves: the source's id to place the call, and the shared identity to file the
    answer under."""
    port = _port()
    source_id = _source_id(record)
    if port is None or source_id is None:
        return
    details = await port.fetch_progress_details(settings, [source_id])
    state = await _load(user_id)
    _set_show_baseline(state, record_key(record), record.get("ids") or {},
                       details.get(int(source_id)) or {})
    await _save(user_id, state)


async def sync(settings, user_id: int, force: bool = False, today: date | None = None) -> dict:
    """Gated incremental sync (see module docstring). Returns the (saved) state.

    Fast path: the activity beacon is unchanged -> return cache, no history pull.
    Change path: re-baseline on unwatch/force, then fold in new history events.
    """
    from ..perftrace import span
    port = _port()
    if port is None:
        return await _load(user_id)
    today = today or clock.today()
    state = await _load(user_id)
    with span("wh.last_activities"):
        la = await port.fetch_last_activities(settings)
    beacons = _beacons(la)

    if not force and state.get("last_synced") and state.get("beacons") == beacons:
        _perf.debug("wh.sync GATED (beacon unchanged) — no history pull")
        return state  # nothing changed since last sync -> serve cache

    if force or _removed_changed(state.get("beacons"), beacons):
        cached = {key: entry for key, entry in (state.get("shows") or {}).items()
                  if _source_id(entry) is not None}
        # SPLIT, because the two halves fail slowly for unrelated reasons and the
        # combined number could not tell them apart: the fetch is one provider
        # call per show, paced by the outbound rate gate, and grows with the
        # roster; the apply is pure CPU on the event loop over whatever came back.
        # A rebaseline that is slow in the fetch is waiting on the provider; one
        # that is slow in the apply is blocking every other request while it runs.
        with span("wh.rebaseline", n=len(cached), reason="force" if force else "unwatch"):
            with span("wh.rebaseline.fetch", n=len(cached)):
                details = await port.fetch_progress_details(
                    settings, [_source_id(entry) for entry in cached.values()])
            with span("wh.rebaseline.apply", n=len(cached)):
                for key, entry in cached.items():
                    _set_show_baseline(state, key, entry.get("ids") or {},
                                       details.get(int(_source_id(entry))) or {})
        if force:
            state["last_synced"] = None  # re-seed movie history from the month start

    start_at = state.get("last_synced") or _month_start_of(today)
    with span("wh.history", start_at=start_at) as sp:
        events = await port.fetch_history(settings, start_at=start_at)
        for event in events:
            _apply_event(state, event)
        sp.set(events=len(events))

    state["last_synced"] = _now_date_iso()
    state["beacons"] = beacons
    await _save(user_id, state)
    return state


async def sync_and_baseline(settings, user_id: int, roster: list[dict], force: bool = False,
                            today: date | None = None) -> dict:
    """`sync`, then guarantee every roster title has a baseline (so titles that
    entered via calendar/rollover/history — not the manual add flow — still get
    counts on first view). Returns the state; read counts via `watched_map` and
    movies via `movies_in_range`.

    Takes the roster RECORDS rather than a list of ids, because filing a baseline
    needs the shared identity and fetching one needs the source's id, and only the
    record carries both."""
    from ..perftrace import span
    state = await sync(settings, user_id, force=force, today=today)
    port = _port()
    if port is None:
        return state
    cached = state.get("shows") or {}
    missing: dict[str, dict] = {}
    for record in roster or []:
        key = str(record_key(record))
        if key in cached or key in missing or _source_id(record) is None:
            continue
        missing[key] = record
    if missing:
        with span("wh.baseline_missing", n=len(missing)):
            details = await port.fetch_progress_details(
                settings, [_source_id(r) for r in missing.values()])
            for key, record in missing.items():
                _set_show_baseline(state, key, record.get("ids") or {},
                                   details.get(int(_source_id(record))) or {})
        await _save(user_id, state)
    return state
