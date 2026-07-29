"""Per-user persistence and rollover for the hidden "distrakt" tracker.

Each distrakt user keeps their OWN independent roster: their own tracked shows,
their own Cleanup/Keepup/Completed/Abandoned buckets built from their own Trakt
watch history, and their own pair of Discord posts. Nothing here is shared
between users — every lookup and mutation is scoped by user_id.

Two storage shapes:
  - distrakt_months holds the month-level state (whether the month is frozen,
    when its totals were last refreshed, the movies snapshotted at freeze time),
    keyed (user_id, month).
  - distrakt_shows holds one row per tracked (user_id, month, trakt_id, season).

In memory a month is still the same `doc` dict the renderers and the pure
rollover logic have always consumed —
    {month, closed, totals_refreshed_at, movies?, shows: [record, ...]}
— so load_month assembles that shape from the two tables and save_month writes it
back; only the storage underneath changed, not the document the logic reasons about.

The Trakt reads (premieres, season detail, watch history) still authenticate with
the app-wide token carried on `settings`; user_id scopes the STORAGE. Handing a
user's own token in is a separate step — the only input to change is the
`settings`/token these functions and watch_history's are given.
"""
from __future__ import annotations

import asyncio
import json
import re
from datetime import date

from . import calendar_state, db
from .providers.base import Item, Media

_MONTH_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")

# Keys allowed on a stored show record, with the type-coercion used on write.
_INT_FIELDS = ("trakt_id", "season", "watched", "total")
_BOOL_FIELDS = ("abandoned",)

# The distrakt_shows columns beyond (user_id, month), in insert order. The bool
# columns are stored as 0/1; everything else passes through.
_SHOW_COLUMNS = (
    "trakt_id", "tmdb", "slug", "media", "title", "season", "network",
    "abandoned", "abandoned_form", "watched", "total", "cadence", "premiere",
    "finale", "bucket", "started_airing", "finished_airing", "source",
)

# Who put a row on the roster. Only CALENDAR rows are re-imported on every load
# of a preview month, and only they mean "the calendar says you're watching
# this" — which is why removing one marks the show not-watching there and
# removing any other kind does not. "" is a row written before the column
# existed; see MIGRATION_16 and api_distrakt_remove.
SOURCE_CALENDAR = "calendar"
SOURCE_HISTORY = "history"
SOURCE_MANUAL = "manual"
SOURCES = (SOURCE_CALENDAR, SOURCE_HISTORY, SOURCE_MANUAL)

# Columns an in-place update is allowed to touch (identity keys excluded).
# `source` is excluded with them: it records who FIRST put the row on the roster
# and must not be rewritten by the live-counts writer or by re-adding a show that
# is already there, or a calendar row could quietly become a manual one.
_UPDATABLE_COLUMNS = frozenset(_SHOW_COLUMNS) - {"trakt_id", "season", "source"}
_BOOL_COLUMNS = frozenset(("abandoned", "started_airing", "finished_airing"))

_INSERT_SHOW_SQL = (
    "INSERT INTO distrakt_shows (user_id, month, " + ", ".join(_SHOW_COLUMNS) + ") "
    "VALUES (" + ", ".join(["?"] * (2 + len(_SHOW_COLUMNS))) + ")"
)


def _validate_month(month: str) -> str:
    if not isinstance(month, str) or not _MONTH_RE.match(month):
        raise ValueError(f"month must be 'YYYY-MM', got {month!r}")
    return month


def new_month_doc(month: str) -> dict:
    """A fresh, empty month document (not persisted until save_month)."""
    return {
        "month": _validate_month(month),
        "closed": False,
        "totals_refreshed_at": None,
        "shows": [],
    }


def _normalize_show(show: dict) -> dict:
    """Build a full show record from `show`, filling schema defaults."""
    incoming = dict(show or {})
    return {
        "trakt_id": int(incoming["trakt_id"]),
        "tmdb": int(incoming["tmdb"]) if incoming.get("tmdb") not in (None, "") else None,
        "slug": str(incoming.get("slug") or ""),
        "media": str(incoming.get("media") or "show"),
        "title": str(incoming.get("title") or ""),
        "season": int(incoming["season"]),
        "network": str(incoming.get("network") or ""),
        "abandoned": bool(incoming.get("abandoned", False)),
        "abandoned_form": incoming.get("abandoned_form"),
        "watched": int(incoming.get("watched") or 0),
        "total": int(incoming.get("total") or 0),
        "cadence": incoming.get("cadence"),
        "premiere": incoming.get("premiere"),
        "finale": incoming.get("finale"),
        "bucket": incoming.get("bucket"),
        "source": str(incoming.get("source") or ""),
    }


def _coerce_update(fields: dict) -> dict:
    """Coerce the subset of updatable keys present in `fields` for an in-place
    update, dropping identity keys and anything not a real column."""
    out = {}
    for k, v in (fields or {}).items():
        if k not in _UPDATABLE_COLUMNS:
            continue  # identity key or not a stored column
        if k in _INT_FIELDS:
            out[k] = int(v) if v is not None else 0
        elif k in _BOOL_FIELDS:
            out[k] = bool(v)
        else:
            out[k] = v
    return out


def _show_params(user_id: int, month: str, rec: dict) -> tuple:
    """Positional values for _INSERT_SHOW_SQL from a record dict."""
    return (
        user_id, month,
        int(rec["trakt_id"]),
        int(rec["tmdb"]) if rec.get("tmdb") not in (None, "") else None,
        str(rec.get("slug") or ""),
        str(rec.get("media") or "show"),
        str(rec.get("title") or ""),
        int(rec["season"]),
        str(rec.get("network") or ""),
        1 if rec.get("abandoned") else 0,
        rec.get("abandoned_form"),
        int(rec.get("watched") or 0),
        int(rec.get("total") or 0),
        rec.get("cadence"),
        rec.get("premiere"),
        rec.get("finale"),
        rec.get("bucket"),
        1 if rec.get("started_airing") else 0,
        1 if rec.get("finished_airing") else 0,
        str(rec.get("source") or ""),
    )


def _row_to_show(row) -> dict:
    """A distrakt_shows row back into the record shape the renderers consume."""
    return {
        "trakt_id": row["trakt_id"],
        "tmdb": row["tmdb"],
        "slug": row["slug"] or "",
        "media": row["media"] or "show",
        "title": row["title"] or "",
        "season": row["season"],
        "network": row["network"] or "",
        "abandoned": bool(row["abandoned"]),
        "abandoned_form": row["abandoned_form"],
        "watched": row["watched"],
        "total": row["total"],
        "cadence": row["cadence"],
        "premiere": row["premiere"],
        "finale": row["finale"],
        "bucket": row["bucket"],
        "started_airing": bool(row["started_airing"]),
        "finished_airing": bool(row["finished_airing"]),
        "source": row["source"] or "",
    }


async def load_month(user_id: int, month: str) -> dict | None:
    """Return this user's stored month doc, or None if it has not been created.

    None — rather than an empty default — lets the rollover logic distinguish an
    uninitialized month from an initialized-but-empty one.
    """
    month = _validate_month(month)
    mrow = await db.fetch_one(
        "SELECT closed, totals_refreshed_at, movies_json FROM distrakt_months "
        "WHERE user_id = ? AND month = ?",
        (user_id, month),
    )
    if mrow is None:
        return None
    rows = await db.fetch_all(
        "SELECT * FROM distrakt_shows WHERE user_id = ? AND month = ? ORDER BY rowid",
        (user_id, month),
    )
    doc = {
        "month": month,
        "closed": bool(mrow["closed"]),
        "totals_refreshed_at": mrow["totals_refreshed_at"],
        "shows": [_row_to_show(r) for r in rows],
    }
    # `movies` only exists on a frozen month (snapshotted at freeze time). Mirror
    # the file model, where the key was simply absent until then.
    if mrow["movies_json"] is not None:
        doc["movies"] = json.loads(mrow["movies_json"])
    return doc


async def save_month(user_id: int, doc: dict) -> None:
    """Persist a whole month doc for `user_id` in one transaction: upsert the
    month-level row, then replace the month's show rows with the doc's.

    Whole-doc replace matches how the freeze pass and the lazy init build a doc
    (from scratch or by recomputing every record); the per-item mutators below
    write single rows instead.
    """
    month = _validate_month((doc or {}).get("month"))
    closed = 1 if doc.get("closed") else 0
    totals = doc.get("totals_refreshed_at")
    movies = doc.get("movies")
    movies_json = None if movies is None else json.dumps(movies)
    shows = doc.get("shows") or []

    def _work(conn: db.Connection) -> None:
        conn.execute(
            "INSERT INTO distrakt_months "
            "(user_id, month, closed, totals_refreshed_at, movies_json, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(user_id, month) DO UPDATE SET "
            "closed = excluded.closed, totals_refreshed_at = excluded.totals_refreshed_at, "
            "movies_json = excluded.movies_json",
            (user_id, month, closed, totals, movies_json, db.now()),
        )
        conn.execute("DELETE FROM distrakt_shows WHERE user_id = ? AND month = ?", (user_id, month))
        for rec in shows:
            conn.execute(_INSERT_SHOW_SQL, _show_params(user_id, month, rec))

    await db.transaction(_work)


async def months_with_shows(user_id: int) -> set[str]:
    """The months that actually HOLD something for this user.

    Distinct from list_months, which answers "has a month row", because the two
    diverge the moment a month is emptied: removing its last show leaves the
    month row behind, so the history backfill — which refuses to touch a month
    somebody is already keeping — saw a curated month where there was nothing at
    all, and could never refill it. What deserves protecting is the content, not
    the row.
    """
    rows = await db.fetch_all(
        "SELECT DISTINCT month FROM distrakt_shows WHERE user_id = ?", (user_id,))
    return {r["month"] for r in rows}


async def set_month_movies(user_id: int, month: str, movies: list[dict]) -> None:
    """Replace just the films a CLOSED month snapshotted, leaving its roster rows
    and its closed flag alone.

    Targeted rather than a save_month round trip because the caller (the history
    backfill) has learned about films for a month that was already frozen: the
    verdicts in it were settled and must not be rewritten, but the **Movies**
    section is a record of what was watched, and leaving it stale would make the
    month contradict the plan that just told the user what it found.
    """
    await db.execute(
        "UPDATE distrakt_months SET movies_json = ? WHERE user_id = ? AND month = ?",
        (json.dumps(list(movies or [])), user_id, _validate_month(month)),
    )


async def list_months(user_id: int) -> list[str]:
    """Sorted list of 'YYYY-MM' this user has a month row for."""
    rows = await db.fetch_all(
        "SELECT month FROM distrakt_months WHERE user_id = ? ORDER BY month",
        (user_id,),
    )
    return [r["month"] for r in rows]


async def add_show(user_id: int, month: str, show: dict) -> None:
    """Upsert a show+season into `user_id`'s `month`, creating the month row if
    needed. Keyed by (trakt_id, season): a new pair is inserted as a full record;
    an existing pair is updated in place with whatever updatable keys `show`
    carries (so this doubles as a live-counts writer)."""
    month = _validate_month(month)
    tid = int(show["trakt_id"])
    season = int(show["season"])

    def _work(conn: db.Connection) -> None:
        conn.execute(
            "INSERT INTO distrakt_months "
            "(user_id, month, closed, totals_refreshed_at, movies_json, created_at) "
            "VALUES (?, ?, 0, NULL, NULL, ?) ON CONFLICT(user_id, month) DO NOTHING",
            (user_id, month, db.now()),
        )
        existing = conn.execute(
            "SELECT trakt_id FROM distrakt_shows "
            "WHERE user_id = ? AND month = ? AND trakt_id = ? AND season = ?",
            (user_id, month, tid, season),
        ).fetchone()
        if existing is None:
            conn.execute(_INSERT_SHOW_SQL, _show_params(user_id, month, _normalize_show(show)))
            return
        updates = _coerce_update(show)
        if not updates:
            return
        cols = list(updates)
        set_clause = ", ".join(f"{c} = ?" for c in cols)
        params = [(1 if updates[c] else 0) if c in _BOOL_COLUMNS else updates[c] for c in cols]
        params += [user_id, month, tid, season]
        conn.execute(
            f"UPDATE distrakt_shows SET {set_clause} "
            "WHERE user_id = ? AND month = ? AND trakt_id = ? AND season = ?",
            params,
        )

    await db.transaction(_work)


async def set_abandoned(
    user_id: int,
    month: str,
    trakt_id: int,
    season: int,
    abandoned: bool,
    abandoned_form: str | None = None,
) -> dict | None:
    """Toggle a show's abandoned flag. Returns the updated record, or None if the
    month or the show+season isn't present for this user.

    When abandoning, `abandoned_form` freezes the rendered inline form so the
    Discord line stays stable; un-abandoning clears it.
    """
    month = _validate_month(month)
    tid, season = int(trakt_id), int(season)

    def _work(conn: db.Connection) -> dict | None:
        row = conn.execute(
            "SELECT trakt_id FROM distrakt_shows "
            "WHERE user_id = ? AND month = ? AND trakt_id = ? AND season = ?",
            (user_id, month, tid, season),
        ).fetchone()
        if row is None:
            return None
        conn.execute(
            "UPDATE distrakt_shows SET abandoned = ?, abandoned_form = ? "
            "WHERE user_id = ? AND month = ? AND trakt_id = ? AND season = ?",
            (1 if abandoned else 0, abandoned_form if abandoned else None,
             user_id, month, tid, season),
        )
        updated = conn.execute(
            "SELECT * FROM distrakt_shows "
            "WHERE user_id = ? AND month = ? AND trakt_id = ? AND season = ?",
            (user_id, month, tid, season),
        ).fetchone()
        return _row_to_show(updated)

    return await db.transaction(_work)


async def remove_show(user_id: int, month: str, trakt_id, season) -> bool:
    """Delete a show+season from a user's month. Returns True if a record was
    removed. Callers guard closed (frozen) months."""
    month = _validate_month(month)
    result = await db.execute(
        "DELETE FROM distrakt_shows "
        "WHERE user_id = ? AND month = ? AND trakt_id = ? AND season = ?",
        (user_id, month, int(trakt_id), int(season)),
    )
    return result.rowcount > 0


# ===========================================================================
# Lazy month rollover, prior-month freeze, and totals staleness. This is the
# orchestrator layer on top of the pure store above; it is the only part that
# reaches out to Trakt (via app/trakt.py) and reads the per-user main-calendar
# not-watching store (app/calendar_state.py).
# ===========================================================================

TOTALS_STALE_HOURS = 24        # auto-refresh open-month totals if stale >24h
WATCHED_RECENCY_DAYS = 60      # only seed genuinely active shows from history


def _prev_month_key(month_key: str) -> str:
    year, month = (int(x) for x in month_key.split("-"))
    return f"{year - 1:04d}-12" if month == 1 else f"{year:04d}-{month - 1:02d}"


async def can_initialize(user_id: int, month_key: str) -> bool:
    """No backfill of months earlier than a user's initial seed. Only a brand-new
    store (no months yet -> seed) or a month strictly AFTER their latest tracked
    month (forward rollover) may be initialized. This stops backward / gap
    month-nav from silently creating (and Trakt-seeding) historical months — a
    user's store only ever grows forward. YYYY-MM strings compare chronologically."""
    months = await list_months(user_id)  # sorted ascending
    return not months or month_key > months[-1]


async def is_backfill_blocked(user_id: int, month_key: str) -> bool:
    """True when `month_key` has no doc for this user AND may not be initialized (a
    past / gap month reached by navigating backward) — rendered read-only."""
    return (await load_month(user_id, month_key)) is None and not await can_initialize(user_id, month_key)


def month_committed(month_key: str, today: date | None = None) -> bool:
    """True once the calendar has reached (or passed) the 1st of `month_key` — the
    month has officially begun. BEFORE this a month is a "preview": it auto-
    populates from premieres and its main-calendar not-watching toggles only HIDE
    shows (reversibly). ON/AFTER it, not-watching promotes to Abandoned and the
    immediately-prior month freezes."""
    today = today or date.today()
    year, month = int(month_key[:4]), int(month_key[5:7])
    return (today.year, today.month) >= (year, month)


async def maybe_freeze_prior(user_id: int, month_key: str, settings, today: date | None = None) -> None:
    """Freeze `user_id`'s immediately-prior month, but ONLY once `month_key` has
    begun (first access on/after the 1st) and the prior is still open. This is
    what keeps a NEW month's pre-1st preview from freezing the still-current prior
    month. Idempotent — a closed/absent prior is left alone. Per user: one user
    reaching the 1st does not freeze anyone else's prior month."""
    if not month_committed(month_key, today):
        return
    prior = await load_month(user_id, _prev_month_key(month_key))
    if prior is not None and not prior.get("closed"):
        await _freeze_month(user_id, prior, settings)


def _matches_not_watching(rec: dict, nw_ids: set[str]) -> bool:
    return str(rec.get("slug") or "") in nw_ids or str(rec.get("trakt_id")) in nw_ids


def _identity_record(src: dict) -> dict:
    """Identity-only projection (no live counts/dates/bucket; abandoned reset) —
    used to carry a show forward into a new month (identity only; recompute live
    once the new month opens)."""
    return {
        "trakt_id": int(src["trakt_id"]),
        "tmdb": src.get("tmdb"),
        "slug": str(src.get("slug") or ""),
        "media": "show",
        "title": str(src.get("title") or ""),
        "season": int(src["season"]),
        "network": str(src.get("network") or ""),
        "abandoned": False,
        "abandoned_form": None,
        # Carried forward with the show: a premiere that rolls into next month is
        # still the calendar's row, and a hand-added one is still the user's.
        "source": str(src.get("source") or ""),
    }


async def compute_live_shows(user_id: int, records: list[dict], settings, fresh: bool = False,
                             watched_lookup: dict | None = None,
                             allow_degrade: bool = False,
                             completed_lookup: dict | None = None) -> list[dict]:
    """Merge each stored record with its live Trakt-derived fields into the flat
    "LIVE SHOW SHAPE" discord_fmt expects (+ computed `bucket`).

    Watched counts (`x`) come from `user_id`'s incremental watch-history cache
    (app/watch_history) — the caller may pass a pre-synced `watched_lookup`
    (avoids re-syncing when it also needs the movies from the same state); if
    omitted we sync here. Totals/dates (`y`, cadence, premiere/finale) come from
    one season call per record; `fresh=True` bypasses the 24h season cache.

    `allow_degrade` decides what a per-show season-detail failure does. Off (the
    default, used by the freeze pass and rollover bucketing) a failure propagates
    so the caller aborts and retries later — never persisting a rate-limited 0/0 as
    a permanent frozen total. On (the live open-month view) the season fan-out runs
    with return_exceptions and a failed show is marked `unavailable` and rendered
    from its LAST-KNOWN stored fields instead: a 429 on one show must not read as
    that show genuinely having 0 episodes, and it must not sink the rest of the
    roster. The shared-prerequisite sync (watched counts) is NOT degraded here —
    its failure still propagates for the caller's top-level handler, because it
    can't be pinned on any one show."""
    import logging

    from . import discord_fmt, watch_history
    from .perftrace import span
    from .trakt import fetch_season_detail, shared_client
    logger = logging.getLogger(__name__)
    if not records:
        return []

    async def _season_details():
        # The app-wide shared client for the whole fan-out (no per-call client).
        # return_exceptions only when degrading: otherwise the first failure must
        # propagate (see allow_degrade) instead of being captured as a result.
        client = shared_client()
        return await asyncio.gather(*(
            fetch_season_detail(settings, rec["trakt_id"], rec["season"], fresh=fresh, client=client)
            for rec in records
        ), return_exceptions=allow_degrade)

    if watched_lookup is None:
        with span("cls.sync+seasons", n=len(records), fresh=fresh):
            state, details = await asyncio.gather(
                watch_history.sync_and_baseline(settings, user_id, [rec["trakt_id"] for rec in records], force=fresh),
                _season_details(),
            )
        watched_lookup = watch_history.watched_map(state)
        completed_lookup = watch_history.season_completed_map(state)
    else:
        with span("cls.season_gather", n=len(records), fresh=fresh):
            details = await _season_details()
    shows = []
    matched = 0
    unavailable = 0
    for rec, detail in zip(records, details):
        key = (int(rec["trakt_id"]), int(rec["season"]))
        if key in watched_lookup:
            matched += 1
        if isinstance(detail, Exception):
            # allow_degrade path only (else the gather would have raised): this one
            # show's totals/dates could not be fetched. Render it from its stored
            # record's last-known fields and flag it so the UI shows "unavailable,
            # refresh to retry" rather than presenting a fabricated 0/0 as real.
            unavailable += 1
            show = {
                **rec,
                "watched": watched_lookup.get(key, int(rec.get("watched") or 0)),
                "total": int(rec.get("total") or 0),
                "cadence": rec.get("cadence"),
                "premiere": rec.get("premiere"),
                "finale": rec.get("finale"),
                "started_airing": bool(rec.get("started_airing")),
                "finished_airing": bool(rec.get("finished_airing")),
                "unavailable": True,
            }
        else:
            show = {
                **rec,
                "watched": watched_lookup.get(key, 0),
                "total": detail["total"],
                "cadence": detail["cadence"],
                "premiere": detail["premiere"],
                "finale": detail["finale"],
                "started_airing": detail["started_airing"],
                "finished_airing": detail["finished_airing"],
                "unavailable": False,
            }
        show["bucket"] = discord_fmt.bucket_of(show, show)
        # WHEN the season was finished, and only for a season that IS finished:
        # on a partly-watched season the same date is just "last time I watched
        # something", which must not read as a completion. "" = not finished, or
        # finished on a date the history cache cannot name.
        show["completed_on"] = (completed_lookup or {}).get(key, "") if show["bucket"] == "completed" else ""
        shows.append(show)

    if unavailable:
        logger.warning(
            "compute_live_shows: %d/%d show(s) rate-limited/unreachable this pass; "
            "rendered from last-known totals and flagged unavailable.",
            unavailable, len(records),
        )

    # X/Y diagnostic: distinguishes an EMPTY watched lookup (no progress returned)
    # from a NON-empty lookup that simply doesn't line up with the stored records
    # (an id/season key mismatch), by printing a small sample of each.
    logger.info(
        "compute_live_shows: %d record(s), watched-lookup has %d key(s), %d matched",
        len(records), len(watched_lookup), matched,
    )
    if records and matched == 0:
        sample_records = [(int(r["trakt_id"]), int(r["season"])) for r in records[:6]]
        sample_lookup = list(watched_lookup.items())[:6]
        logger.warning(
            "compute_live_shows: 0/%d records matched a watched count. "
            "sample record keys=%s ; sample watched-lookup=%s",
            len(records), sample_records, sample_lookup,
        )
    return shows


def frozen_shows(doc: dict) -> list[dict]:
    """LIVE SHOW SHAPE list for a CLOSED month, straight from the stored snapshot
    — NO Trakt calls. `_freeze_month` already persisted watched/total/cadence/
    premiere/finale/started_airing/finished_airing/bucket onto each record, so the
    discord_fmt renderers read them as-is."""
    out = []
    for rec in doc.get("shows") or []:
        show = dict(rec)
        show.setdefault("started_airing", False)
        show.setdefault("finished_airing", False)
        out.append(show)
    return out


async def _freeze_month(user_id: int, doc: dict, settings) -> dict:
    """Compute one final live snapshot for `doc`, persist counts/dates/bucket onto
    each stored record, mark it closed, stamp totals_refreshed_at, save. After
    this the month renders forever from the frozen snapshot with no Trakt calls."""
    from . import watch_history
    records = doc.get("shows") or []
    state = await watch_history.sync_and_baseline(settings, user_id, [r["trakt_id"] for r in records], force=True)
    watched_lookup = watch_history.watched_map(state)
    shows = await compute_live_shows(user_id, records, settings, fresh=True, watched_lookup=watched_lookup)
    by_key = {(int(s["trakt_id"]), int(s["season"])): s for s in shows}
    for rec in records:
        s = by_key.get((int(rec["trakt_id"]), int(rec["season"])))
        if not s:
            continue
        rec["watched"] = int(s["watched"])
        rec["total"] = int(s["total"])
        rec["cadence"] = s["cadence"]
        rec["premiere"] = s["premiere"]
        rec["finale"] = s["finale"]
        rec["started_airing"] = bool(s["started_airing"])
        rec["finished_airing"] = bool(s["finished_airing"])
        rec["bucket"] = s["bucket"]
    # Snapshot the movies watched during this month so the frozen POST 2 keeps its
    # **Movies** section offline forever.
    mstart, mend = watch_history.month_bounds(doc["month"])
    doc["movies"] = watch_history.movies_in_range(state, mstart, mend)
    doc["closed"] = True
    doc["totals_refreshed_at"] = db.now()
    await save_month(user_id, doc)
    return doc


def is_stale(doc: dict | None, max_age_hours: int = TOTALS_STALE_HOURS) -> bool:
    """True if the open month's totals have never been stamped or are older than
    `max_age_hours` (auto-refresh on load if stale >24h)."""
    ts = (doc or {}).get("totals_refreshed_at")
    if not ts:
        return True
    try:
        ts = int(ts)
    except (TypeError, ValueError):
        return True
    return (db.now() - ts) > max_age_hours * 3600


async def stamp_refreshed(user_id: int, month_key: str) -> None:
    """Record that a user's open month totals were just refreshed."""
    await db.execute(
        "UPDATE distrakt_months SET totals_refreshed_at = ? WHERE user_id = ? AND month = ?",
        (db.now(), user_id, _validate_month(month_key)),
    )


def _calendar_record(item: Item) -> dict:
    """Identity record from a normalized calendar item.

    The tracker's rows are keyed on Trakt's own id, so this reaches into
    `item.ids` for it rather than taking whatever the item's display id happens
    to be — those are different questions, and the display id is a slug.
    """
    return {
        "trakt_id": int(item.ids["trakt"]),
        "tmdb": item.ids.get("tmdb"),
        "slug": str(item.ids.get("slug") or ""),
        "media": Media.SHOW,
        "title": str(item.title or ""),
        "season": int(item.season or 1),
        "network": str(item.network or ""),
    }


async def _premiere_records(user_id: int, settings, year: int, month: int) -> list[dict]:
    """This month's calendar premieres split by rule: shows/new -> New (S01);
    shows/premieres minus shows/new -> Returning (S02+).

    Reads through the shared calendar cache (calendar_cache.read_month) rather
    than issuing a separate live Trakt call, so import stops duplicating a
    fetch the main calendar already made. Passing the importing user's own
    genre/country/show_certifications prefs into that read applies the same
    filters that already keep those shows off their calendar, so import can't
    hand back something they've personally filtered out and never got a chance
    to mark not-watching (it never appeared for them to mark in the first
    place). The instance-wide content floor still applies underneath this for
    free — it is enforced where the cache is populated, before any reader,
    including this one, ever sees the excluded show.
    """
    from zoneinfo import ZoneInfo

    from . import auth, calendar_cache
    from .endpoints import get_endpoint
    prefs = await auth.get_user_prefs(user_id)
    tz = ZoneInfo(settings.timezone)
    (new_items, _), (prem_items, _) = await asyncio.gather(
        calendar_cache.read_month(
            get_endpoint("shows/new"), settings, tz=tz, year=year, month=month,
            genres=prefs["genres"], countries=prefs["countries"],
            show_certifications=prefs["show_certifications"],
        ),
        calendar_cache.read_month(
            get_endpoint("shows/premieres"), settings, tz=tz, year=year, month=month,
            genres=prefs["genres"], countries=prefs["countries"],
            show_certifications=prefs["show_certifications"],
        ),
    )
    out: list[dict] = []
    new_keys: set[tuple[int, int]] = set()
    for item in new_items:
        tid, season = item.ids.get("trakt"), item.season
        if tid is None or season is None:
            continue
        new_keys.add((int(tid), int(season)))
        out.append(_calendar_record(item))
    for item in prem_items:
        tid, season = item.ids.get("trakt"), item.season
        if tid is None or season is None:
            continue
        if (int(tid), int(season)) in new_keys:
            continue  # this S01 premiere is already counted as a New Shows entry
        out.append(_calendar_record(item))
    return out


async def _add_premieres(doc: dict, present: set[tuple[int, int]], user_id: int, settings,
                         year: int, month: int, nw_ids: set[str]) -> int:
    """Append this month's premieres to `doc` (skip existing + not-watching).
    Mutates `doc['shows']`/`present`; returns the number added."""
    added = 0
    for rec in await _premiere_records(user_id, settings, year, month):
        key = (int(rec["trakt_id"]), int(rec["season"]))
        if key in present or _matches_not_watching(rec, nw_ids):
            continue
        doc["shows"].append(_normalize_show({**rec, "source": SOURCE_CALENDAR}))
        present.add(key)
        added += 1
    return added


async def import_premieres(user_id: int, month_key: str, settings) -> dict | None:
    """Merge this month's calendar premieres into `user_id`'s OPEN month (skip
    existing + not-watching). Powers the manual "Import from calendar" action and
    the preview-month auto-populate. No-op on a missing/closed month."""
    doc = await load_month(user_id, month_key)
    if doc is None or doc.get("closed"):
        return doc
    year, month = int(month_key[:4]), int(month_key[5:7])
    present = {(int(s["trakt_id"]), int(s["season"])) for s in doc.get("shows") or []}
    nw_ids = await calendar_state.not_watching_ids(user_id)
    if await _add_premieres(doc, present, user_id, settings, year, month, nw_ids):
        await save_month(user_id, doc)
    return doc


async def drop_seasons_finished_earlier(user_id: int, month_key: str,
                                        shows: list[dict]) -> list[dict]:
    """Take out the shows whose season was finished BEFORE this month began, and
    delete their roster rows.

    Completed means "completed this month". Rollover already refuses to carry a
    completed show into a new month, but it decides that once, when the month is
    created — so a show carried into August during the July preview and then
    finished in July stayed on August's roster and, being fully watched, sat in
    August's Completed for good. This closes that window on every load.

    Removed rather than re-bucketed: with the Completed rule applied, a fully
    watched season would otherwise fall through to Cleanup or Keepup and read as
    work outstanding, which is worse than not being on the month at all. It is
    still on the month it WAS finished in.

    Only acts on a date it actually has: a season the history cache cannot date
    (nothing dated for it, or a show that predates dated history and has not been
    re-baselined yet) is left exactly where it is.
    """
    from . import watch_history
    start, _ = watch_history.month_bounds(month_key)
    keep, stale = [], []
    for show in shows:
        done = str(show.get("completed_on") or "")
        (stale if done and done < start else keep).append(show)
    for show in stale:
        await remove_show(user_id, month_key, int(show["trakt_id"]), int(show["season"]))
    return keep


async def is_calendar_premiere(user_id: int, month_key: str, settings,
                               trakt_id: int, season: int) -> bool:
    """Whether this month's calendar premieres include this show+season.

    Only for rows written before `source` existed (see MIGRATION_16), where the
    question "did the calendar put this here" has no recorded answer: a show the
    premiere import would hand straight back is one the calendar is asserting,
    and removing it has to say something to the calendar or it cannot stick.
    Reads through the same cache import_premieres uses, which the page load that
    rendered the ✕ has just warmed.

    Answers False rather than raising if Trakt is unreachable: not knowing means
    not marking, which is the conservative half — the row simply comes back.
    """
    if not (settings and getattr(settings, "trakt_configured", False)):
        return False
    year, month = int(month_key[:4]), int(month_key[5:7])
    try:
        records = await _premiere_records(user_id, settings, year, month)
    except Exception:  # noqa: BLE001 — a hiccup here must not fail the removal
        return False
    return any(int(r["trakt_id"]) == trakt_id and int(r["season"]) == season for r in records)


async def backfill_tmdb(user_id: int, month_key: str, settings) -> dict | None:
    """Fill in `tmdb` for records added before it was stored (one-time per show).
    Resolves tmdb from the trakt_id via Trakt, dedup by show, persists if changed."""
    from .perftrace import span
    from .trakt import fetch_show_tmdb, shared_client
    doc = await load_month(user_id, month_key)
    if doc is None:
        return None
    missing = [r for r in (doc.get("shows") or []) if not r.get("tmdb")]
    if not missing:
        return doc
    uniq = list(dict.fromkeys(int(r["trakt_id"]) for r in missing))
    with span("distrakt.backfill_tmdb", n=len(uniq)):
        client = shared_client()
        tmdbs = await asyncio.gather(*(fetch_show_tmdb(settings, tid, client=client) for tid in uniq))
        by_tid = dict(zip(uniq, tmdbs))
        changed = False
        for rec in missing:
            tmdb = by_tid.get(int(rec["trakt_id"]))
            if tmdb:
                rec["tmdb"] = int(tmdb)
                changed = True
        if changed:
            await save_month(user_id, doc)
    return doc


async def _history_records(settings, present: set[tuple[int, int]]) -> list[dict]:
    """In-progress-but-unfinished shows from recent watch history not already in
    the roster. A candidate is dropped if its season is fully watched (completed)
    or has zero watched episodes (nothing in progress)."""
    from .trakt import fetch_season_detail, fetch_watched_progress
    progress = await fetch_watched_progress(settings, since_days=WATCHED_RECENCY_DAYS)
    candidates = [
        p for p in progress
        if (int(p["trakt_id"]), int(p["season"])) not in present and int(p.get("watched") or 0) > 0
    ]
    if not candidates:
        return []
    details = await asyncio.gather(*(
        fetch_season_detail(settings, c["trakt_id"], c["season"]) for c in candidates
    ))
    out = []
    for c, detail in zip(candidates, details):
        total = int(detail.get("total") or 0)
        watched = int(c.get("watched") or 0)
        if total > 0 and watched >= total:
            continue  # already completed -> not "in-progress-but-unfinished"
        out.append({
            "trakt_id": int(c["trakt_id"]),
            "tmdb": c.get("tmdb"),
            "slug": str(c.get("slug") or ""),
            "media": "show",
            "title": str(c.get("title") or ""),
            "season": int(c["season"]),
            "network": str(c.get("network") or ""),
        })
    return out


async def ensure_month(user_id: int, year: int, month: int, settings, today: date | None = None) -> dict:
    """Lazy, scheduler-free month rollover for one user. Returns the month doc.

    On EVERY access it first freezes the prior month IF the accessed month has
    begun (maybe_freeze_prior) — so a pre-1st preview of a new month leaves the
    still-current prior month open/editable, and the freeze only lands on first
    access on/after the 1st. Then, if the month doc doesn't exist yet and may be
    created (configured + not backfill-blocked), it initializes it:

      (b) Carry forward prior-month shows EXCEPT completed/abandoned (identity
          only; live fields recompute once this month opens). Prior buckets come
          from its frozen snapshot when closed, else computed live (preview case).
      (c) Add this month's calendar premieres (shows/new -> New, shows/premieres
          minus new -> Returning), EXCLUDING not-watching (before-commit).
      (d) Add in-progress-but-unfinished shows from recent history not present.

    An already-initialized month is returned untouched (aside from the prior-month
    freeze), so PAST months never re-run initialization.
    """
    today = today or date.today()
    month_key = f"{int(year):04d}-{int(month):02d}"
    existing = await load_month(user_id, month_key)

    # Freeze the prior month only once THIS month has actually begun (not during a
    # pre-1st preview). Skip when accessing an already-closed month (settled).
    if settings and getattr(settings, "trakt_configured", False) and (existing is None or not existing.get("closed")):
        await maybe_freeze_prior(user_id, month_key, settings, today)

    if existing is not None:
        return await load_month(user_id, month_key)
    if not (settings and getattr(settings, "trakt_configured", False)):
        # Initialization needs Trakt (premieres + history); without credentials
        # return a transient, UNPERSISTED empty doc so a proper init still happens
        # once Trakt is configured (rather than baking in an empty month).
        return new_month_doc(month_key)
    if not await can_initialize(user_id, month_key):
        # Backward / gap navigation to a never-tracked past month: DO NOT backfill
        # — return a transient, UNPERSISTED empty doc (rendered read-only).
        return new_month_doc(month_key)

    doc = new_month_doc(month_key)
    present: set[tuple[int, int]] = set()

    # (b) Carry forward everything except Completed / Abandoned. An open (not-yet-
    # frozen) prior is bucketed live so a preview rollover still drops the right
    # shows; a frozen prior reuses its stored buckets.
    prior = await load_month(user_id, _prev_month_key(month_key))
    if prior is not None:
        prior_shows = frozen_shows(prior) if prior.get("closed") \
            else await compute_live_shows(user_id, prior.get("shows") or [], settings)
        for s in prior_shows:
            if s.get("abandoned") or s.get("bucket") in ("completed", "abandoned"):
                continue
            key = (int(s["trakt_id"]), int(s["season"]))
            if key in present:
                continue
            doc["shows"].append(_normalize_show(_identity_record(s)))
            present.add(key)

    # (c) This month's premieres, minus not-watching (excluded before commit).
    nw_ids = await calendar_state.not_watching_ids(user_id)
    await _add_premieres(doc, present, user_id, settings, int(year), int(month), nw_ids)

    # (d) In-progress-but-unfinished shows from recent history.
    for rec in await _history_records(settings, present):
        key = (int(rec["trakt_id"]), int(rec["season"]))
        if key in present:
            continue
        doc["shows"].append(_normalize_show({**rec, "source": SOURCE_HISTORY}))
        present.add(key)

    doc["totals_refreshed_at"] = db.now()
    await save_month(user_id, doc)
    return doc


# ===========================================================================
# Per-user JSON export / restore. The export is one user's complete distrakt
# dataset — every month, show row, watch-state row, per-season progress, and
# movie watch — as a single document carrying a schema version. Restore is the
# inverse, REPLACE (not merge) in one transaction, scoped to the requesting user.
# ===========================================================================

# Bump only on an incompatible change to the exported shape. Restore refuses a
# version it doesn't understand rather than guessing at an older/newer layout.
# 2 adds distrakt_prefs (the network->emoji map). A version-1 document restores
# fine — see restore_user_data, which treats a table the file doesn't carry as
# "leave it alone" rather than "delete it".
EXPORT_SCHEMA = 2
SUPPORTED_EXPORT_SCHEMAS = (1, 2)

# (table, columns-excluding-user_id). The export lists rows verbatim so an
# export -> restore round trip is an identity; restore always writes user_id from
# the session, never from the file.
_EXPORT_TABLES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("distrakt_months", ("month", "closed", "totals_refreshed_at", "movies_json", "created_at")),
    ("distrakt_shows", ("month", *_SHOW_COLUMNS)),
    ("distrakt_watch_state", ("last_synced", "beacons_json")),
    ("distrakt_show_progress", ("trakt_id", "season", "watched_episodes_json")),
    ("distrakt_movie_watches", ("trakt_id", "watched_at", "title", "year")),
    # The emoji map travels with the backup: it is the only copy there is now
    # that nothing seeds it, so a restore that dropped it would lose work that
    # cannot be recovered from anywhere else.
    ("distrakt_prefs", ("network_emojis_json", "default_network_emoji", "updated_at")),
)


# ---------------------------------------------------------------------------
# per-user network -> emoji map
# ---------------------------------------------------------------------------
# This was app-wide in settings.json, which meant every tracker user shared one
# map: importing a roster on any account registered its networks into the
# operator's, and one person's emoji choices went out in everybody's Discord
# posts. It is per-user for the same reason the roster and the watch history are.
#
# THERE IS NO SEEDING. A new account starts with an empty map and the default
# emoji, and fills it in as its own roster registers networks. Inheriting the
# operator's map would be the same mistake in slower motion — one person's
# choices arriving in another person's posts, just once instead of continuously.
# The map travels with the tracker's own Backup export/restore instead, which is
# how a user moves it between instances or accounts.

DEFAULT_EMOJI = ":tv:"


async def get_emoji_prefs(user_id: int) -> tuple[dict, str]:
    """This user's (network_emojis, default_emoji).

    An account with no row yet gets an empty map — deliberately, not as a
    fallback to anything app-wide.
    """
    row = await db.fetch_one(
        "SELECT network_emojis_json, default_network_emoji FROM distrakt_prefs "
        "WHERE user_id = ?",
        (user_id,),
    )
    if row is None:
        return {}, DEFAULT_EMOJI
    try:
        emojis = json.loads(row["network_emojis_json"] or "{}")
    except ValueError:
        emojis = {}
    return (
        emojis if isinstance(emojis, dict) else {},
        row["default_network_emoji"] or DEFAULT_EMOJI,
    )


async def set_emoji_prefs(user_id: int, emojis: dict, default_emoji: str) -> None:
    """Replace this user's whole map. The editor sends every row it has, so a
    partial merge would make deleting an entry impossible."""
    await db.execute(
        "INSERT INTO distrakt_prefs (user_id, network_emojis_json, default_network_emoji, updated_at) "
        "VALUES (?, ?, ?, ?) "
        "ON CONFLICT(user_id) DO UPDATE SET "
        "network_emojis_json = excluded.network_emojis_json, "
        "default_network_emoji = excluded.default_network_emoji, "
        "updated_at = excluded.updated_at",
        (user_id, json.dumps({str(k): str(v) for k, v in (emojis or {}).items()}),
         (default_emoji or DEFAULT_EMOJI).strip() or DEFAULT_EMOJI, db.now()),
    )


async def register_networks(user_id: int, networks) -> dict:
    """Add any unmapped network to THIS user's map with the default emoji, so it
    shows up in their editor ready to customize. Returns the resulting map."""
    emojis, default_emoji = await get_emoji_prefs(user_id)
    changed = False
    for net in networks:
        net = (net or "").strip()
        if net and net not in emojis:
            emojis[net] = default_emoji
            changed = True
    if changed:
        await set_emoji_prefs(user_id, emojis, default_emoji)
    return emojis


class RestoreError(ValueError):
    """A restore document that cannot be applied (unknown schema, wrong shape)."""


async def export_user_data(user_id: int) -> dict:
    """The requesting user's complete distrakt dataset as one JSON-able document.
    Contains no tokens and no other user's data."""
    doc: dict = {"schema": EXPORT_SCHEMA, "exported_at": db.now()}
    for table, cols in _EXPORT_TABLES:
        rows = await db.fetch_all(
            f"SELECT {', '.join(cols)} FROM {table} WHERE user_id = ?",
            (user_id,),
        )
        doc[table] = [{c: row[c] for c in cols} for row in rows]
    return doc


async def restore_user_data(user_id: int, doc: dict) -> None:
    """Replace `user_id`'s distrakt data with the document's, in one transaction.

    REPLACE, not merge: the user's existing rows in the file's tables are deleted
    and the file's inserted. Any `user_id` present in the file is IGNORED — every
    row is written under the session user. Refuses an unknown schema version.

    A table the document does not carry at all is LEFT ALONE rather than emptied,
    which is what lets an older export restore onto a newer schema: a version-1
    backup predates the emoji map and says nothing about it, and reading that
    silence as "delete my map" would destroy data the file never claimed to
    describe.
    """
    if not isinstance(doc, dict):
        raise RestoreError("restore document must be an object")
    if doc.get("schema") not in SUPPORTED_EXPORT_SCHEMAS:
        raise RestoreError(f"unsupported distrakt export schema: {doc.get('schema')!r}")
    payload: dict[str, list] = {}
    for table, _cols in _EXPORT_TABLES:
        if table not in doc:
            continue
        rows = doc.get(table)
        if not isinstance(rows, list):
            raise RestoreError(f"{table} must be a list")
        payload[table] = rows

    def _work(conn: db.Connection) -> None:
        for table in payload:
            conn.execute(f"DELETE FROM {table} WHERE user_id = ?", (user_id,))
        for table, cols in _EXPORT_TABLES:
            if table not in payload:
                continue
            collist = ", ".join(("user_id", *cols))
            placeholders = ", ".join(["?"] * (1 + len(cols)))
            sql = f"INSERT INTO {table} ({collist}) VALUES ({placeholders})"
            for row in payload[table]:
                if not isinstance(row, dict):
                    raise RestoreError(f"{table} rows must be objects")
                conn.execute(sql, [user_id, *((row.get(c)) for c in cols)])

    await db.transaction(_work)
