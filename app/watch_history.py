"""Incremental watch-history cache for the distrakt tracker, per user.

Fetching per-show progress on every page load was correct but slow (one call per
tracked show). This module caches each user's watch state and keeps it fresh
cheaply:

  - BASELINE: when a show first enters a user's roster it is baselined once from
    /shows/{id}/progress/watched -> the exact set of completed episode numbers
    per season (authoritative + deduped).
  - INCREMENTAL: on each load we hit /sync/last_activities (a tiny, fixed-size
    "last changed at" beacon). If nothing changed, we serve the cache with zero
    further calls. If it changed, we pull only NEW plays via
    /users/me/history?start_at=<last_synced> and fold them in (idempotent: adding
    an already-known episode number to a set is a no-op, so day-granularity
    `start_at` overlap is harmless).
  - MOVIES: the same history sweep carries movie plays, cached with their
    watched_at so a month can list the movies watched during it (POST 2's
    **Movies** section).
  - UNWATCH / FORCE: if the removed_at beacon changes (or the Refresh button
    forces it) we re-baseline every cached show from progress and re-seed movies.

Storage: three per-user SQLite tables (distrakt_watch_state,
distrakt_show_progress, distrakt_movie_watches). In memory the state is the same
dict shape it always was — {last_synced, beacons, shows: {tid: {season: [eps]}},
movies: {tid: {title, year, watched_at}}} — so the pure folders and readers
(watched_map, movies_in_range, month_bounds, the _apply_* folders) are unchanged;
only _load / _save around them talk to the database instead of a JSON file.

The Trakt calls still authenticate with the app-wide token carried on `settings`;
`user_id` scopes the STORAGE only. Pointing a user's own token at the fetches is a
separate step — the one input to change is what `settings` (and thus the token)
these functions are handed.
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import date, datetime, timezone

from . import db

logger = logging.getLogger(__name__)
_perf = logging.getLogger("app.perf")


def _default_state() -> dict:
    return {"last_synced": None, "beacons": None, "shows": {}, "movies": {}}


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
        "SELECT trakt_id, season, watched_episodes_json FROM distrakt_show_progress "
        "WHERE user_id = ?",
        (user_id,),
    )
    shows: dict = {}
    for row in prog:
        shows.setdefault(str(int(row["trakt_id"])), {})[str(int(row["season"]))] = list(
            json.loads(row["watched_episodes_json"] or "[]")
        )
    state["shows"] = shows
    movies_rows = await db.fetch_all(
        "SELECT trakt_id, watched_at, title, year FROM distrakt_movie_watches WHERE user_id = ?",
        (user_id,),
    )
    state["movies"] = {
        str(int(row["trakt_id"])): {
            "title": row["title"] or "",
            "year": row["year"],
            "watched_at": row["watched_at"] or "",
        }
        for row in movies_rows
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

    def _work(conn: db.Connection) -> None:
        conn.execute(
            "INSERT INTO distrakt_watch_state (user_id, last_synced, beacons_json) "
            "VALUES (?, ?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET "
            "last_synced = excluded.last_synced, beacons_json = excluded.beacons_json",
            (user_id, last_synced, beacons_json),
        )
        conn.execute("DELETE FROM distrakt_show_progress WHERE user_id = ?", (user_id,))
        for tid_s, seasons in shows.items():
            for season_s, eps in (seasons or {}).items():
                conn.execute(
                    "INSERT INTO distrakt_show_progress "
                    "(user_id, trakt_id, season, watched_episodes_json) VALUES (?, ?, ?, ?)",
                    (user_id, int(tid_s), int(season_s), json.dumps(list(eps or []))),
                )
        conn.execute("DELETE FROM distrakt_movie_watches WHERE user_id = ?", (user_id,))
        for tid_s, movie in movies.items():
            conn.execute(
                "INSERT INTO distrakt_movie_watches "
                "(user_id, trakt_id, watched_at, title, year) VALUES (?, ?, ?, ?, ?)",
                (
                    user_id, int(tid_s), (movie or {}).get("watched_at") or "",
                    (movie or {}).get("title") or "", (movie or {}).get("year"),
                ),
            )

    await db.transaction(_work)


# ---------------------------------------------------------------------------
# Pure state folders / readers (no I/O — unit-tested directly)
# ---------------------------------------------------------------------------

def _beacons(la: dict) -> dict:
    """The subset of /sync/last_activities we gate on: episode + movie watched/
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


def _set_show_baseline(state: dict, trakt_id, season_to_eps: dict) -> None:
    state.setdefault("shows", {})[str(int(trakt_id))] = {
        str(int(season)): sorted({int(n) for n in eps})
        for season, eps in (season_to_eps or {}).items()
    }


def _apply_episode(state: dict, trakt_id, season, number) -> None:
    """Fold one episode play into a cached show (idempotent). Untracked shows
    (never baselined) are ignored — only roster shows carry counts."""
    if trakt_id is None or season is None or number is None:
        return
    shows = state.setdefault("shows", {})
    key = str(int(trakt_id))
    if key not in shows:  # not baselined -> not on the roster; skip
        return
    lst = shows[key].setdefault(str(int(season)), [])
    n = int(number)
    if n not in lst:
        lst.append(n)
        lst.sort()


def _apply_movie(state: dict, trakt_id, title, year, watched_at) -> None:
    """Record a watched movie, keeping the latest watched_at (dedup by id)."""
    if trakt_id is None:
        return
    movies = state.setdefault("movies", {})
    key = str(int(trakt_id))
    prev = movies.get(key)
    if not prev or (watched_at or "") > (prev.get("watched_at") or ""):
        movies[key] = {"title": title or "", "year": year, "watched_at": watched_at or ""}


def _apply_event(state: dict, event: dict) -> None:
    etype = event.get("type")
    if etype == "episode":
        show = event.get("show") or {}
        ep = event.get("episode") or {}
        tid = ((show.get("ids") or {}).get("trakt"))
        _apply_episode(state, tid, ep.get("season"), ep.get("number"))
    elif etype == "movie":
        movie = event.get("movie") or {}
        tid = ((movie.get("ids") or {}).get("trakt"))
        _apply_movie(state, tid, movie.get("title"), movie.get("year"), event.get("watched_at"))


def watched_map(state: dict) -> dict[tuple[int, int], int]:
    """{(trakt_id, season): watched_episode_count} from the cache."""
    out: dict[tuple[int, int], int] = {}
    for tid_s, seasons in (state.get("shows") or {}).items():
        for season_s, eps in (seasons or {}).items():
            out[(int(tid_s), int(season_s))] = len(eps or [])
    return out


def movies_in_range(state: dict, start_date: str, end_date: str) -> list[dict]:
    """Movies whose watched_at date falls within [start_date, end_date]
    (YYYY-MM-DD, inclusive), as [{title, year, watched_at}]."""
    out = []
    for m in (state.get("movies") or {}).values():
        day = (m.get("watched_at") or "")[:10]
        if day and start_date <= day <= end_date:
            out.append({"title": m.get("title") or "", "year": m.get("year"), "watched_at": m.get("watched_at")})
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


# ---------------------------------------------------------------------------
# Orchestration (Trakt I/O)
# ---------------------------------------------------------------------------

async def baseline_show(settings, user_id: int, trakt_id) -> None:
    """Baseline one show from progress/watched (called when it enters the roster)."""
    from .trakt import fetch_show_progress_detail
    detail = await fetch_show_progress_detail(settings, trakt_id)
    state = await _load(user_id)
    _set_show_baseline(state, trakt_id, detail)
    await _save(user_id, state)


async def sync(settings, user_id: int, force: bool = False, today: date | None = None) -> dict:
    """Gated incremental sync (see module docstring). Returns the (saved) state.

    Fast path: last_activities unchanged -> return cache, no history pull.
    Change path: re-baseline on unwatch/force, then fold in new history events.
    """
    from .perftrace import span
    from .trakt import fetch_history, fetch_last_activities, fetch_show_progress_detail
    today = today or datetime.now(timezone.utc).date()
    state = await _load(user_id)
    with span("wh.last_activities"):
        la = await fetch_last_activities(settings)
    beacons = _beacons(la)

    if not force and state.get("last_synced") and state.get("beacons") == beacons:
        _perf.debug("wh.sync GATED (beacon unchanged) — no history pull")
        return state  # nothing changed since last sync -> serve cache

    if force or _removed_changed(state.get("beacons"), beacons):
        from .trakt import shared_client
        cached_ids = [int(t) for t in (state.get("shows") or {}).keys()]
        with span("wh.rebaseline", n=len(cached_ids), reason="force" if force else "unwatch"):
            client = shared_client()
            details = await asyncio.gather(*(
                fetch_show_progress_detail(settings, tid, client=client) for tid in cached_ids
            ))
            for tid, detail in zip(cached_ids, details):
                _set_show_baseline(state, tid, detail)
        if force:
            state["last_synced"] = None  # re-seed movie history from the month start

    start_at = state.get("last_synced") or _month_start_of(today)
    with span("wh.history", start_at=start_at) as sp:
        events = await fetch_history(settings, start_at=start_at)
        for event in events:
            _apply_event(state, event)
        sp.set(events=len(events))

    state["last_synced"] = _now_date_iso()
    state["beacons"] = beacons
    await _save(user_id, state)
    return state


async def sync_and_baseline(settings, user_id: int, roster_trakt_ids, force: bool = False,
                            today: date | None = None) -> dict:
    """`sync`, then guarantee every roster show has a baseline (so shows that
    entered via calendar/rollover/history — not the manual add flow — still get
    counts on first view). Returns the state; read counts via `watched_map` and
    movies via `movies_in_range`."""
    from .perftrace import span
    from .trakt import fetch_show_progress_detail, shared_client
    state = await sync(settings, user_id, force=force, today=today)
    missing = list(dict.fromkeys(
        int(t) for t in roster_trakt_ids if t is not None and str(int(t)) not in (state.get("shows") or {})
    ))
    if missing:
        with span("wh.baseline_missing", n=len(missing)):
            client = shared_client()
            details = await asyncio.gather(*(
                fetch_show_progress_detail(settings, tid, client=client) for tid in missing
            ))
            for tid, detail in zip(missing, details):
                _set_show_baseline(state, tid, detail)
        await _save(user_id, state)
    return state
