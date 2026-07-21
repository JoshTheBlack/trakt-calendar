"""Per-month persistence store for the hidden "distrakt" tracker (BUILD_PLAN §6).

One JSON doc per month at data/distrakt/YYYY-MM.json. This module is a PURE store:
load / save / list, plus add-show and set-abandoned mutations. It deliberately
contains NO rollover logic (Chat 5) and NO bucket computation (Chat 4) — it only
persists and reads back the month document.

Mirrors app/state.py's file-IO conventions (DATA_DIR, ensure-dir, plain JSON),
with atomic writes so an in-place update can't leave a truncated doc behind.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import tempfile
from datetime import date, datetime, timedelta, timezone

from .config import DATA_DIR, _ensure_data_dir

DISTRAKT_DIR = DATA_DIR / "distrakt"

_MONTH_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")

# Keys allowed on a stored show record, with the type-coercion used on write.
_INT_FIELDS = ("trakt_id", "season", "watched", "total")
_BOOL_FIELDS = ("abandoned",)


def _validate_month(month: str) -> str:
    if not isinstance(month, str) or not _MONTH_RE.match(month):
        raise ValueError(f"month must be 'YYYY-MM', got {month!r}")
    return month


def _month_path(month: str):
    return DISTRAKT_DIR / f"{_validate_month(month)}.json"


def _ensure_distrakt_dir() -> None:
    _ensure_data_dir()
    DISTRAKT_DIR.mkdir(parents=True, exist_ok=True)


def _atomic_write(path, text: str) -> None:
    """Write via a temp file in the same dir + os.replace so readers never see a
    half-written doc (state.py writes in place; the tracker updates docs in place
    far more often, so the atomicity matters here)."""
    _ensure_distrakt_dir()
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def new_month_doc(month: str) -> dict:
    """A fresh, empty month document (not persisted until save_month)."""
    return {
        "month": _validate_month(month),
        "closed": False,
        "totals_refreshed_at": None,
        "shows": [],
    }


def _normalize_show(show: dict) -> dict:
    """Build a full show record from `show`, filling schema defaults (§6)."""
    incoming = dict(show or {})
    record = {
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
    }
    return record


def _coerce_update(fields: dict) -> dict:
    """Coerce the subset of keys present in `fields` for an in-place update."""
    out = {}
    for k, v in (fields or {}).items():
        if k in ("trakt_id", "season"):
            continue  # identity keys — never rewritten by an update
        if k in _INT_FIELDS:
            out[k] = int(v) if v is not None else 0
        elif k in _BOOL_FIELDS:
            out[k] = bool(v)
        else:
            out[k] = v
    return out


def _find_show(doc: dict, trakt_id: int, season: int) -> dict | None:
    for rec in doc.get("shows") or []:
        if rec.get("trakt_id") == trakt_id and rec.get("season") == season:
            return rec
    return None


def load_month(month: str) -> dict | None:
    """Return the stored month doc, or None if it has not been created yet.

    (None — rather than an empty default — lets Chat 5's rollover distinguish an
    uninitialized month from an initialized-but-empty one.)
    """
    path = _month_path(month)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def save_month(doc: dict) -> None:
    """Persist a month doc. `doc['month']` must be a valid 'YYYY-MM'."""
    month = _validate_month((doc or {}).get("month"))
    _atomic_write(_month_path(month), json.dumps(doc, indent=2))


def list_months() -> list[str]:
    """Sorted list of 'YYYY-MM' for which a doc exists on disk."""
    if not DISTRAKT_DIR.exists():
        return []
    months = []
    for entry in DISTRAKT_DIR.iterdir():
        if entry.suffix == ".json" and _MONTH_RE.match(entry.stem):
            months.append(entry.stem)
    return sorted(months)


def add_show(month: str, show: dict) -> dict:
    """Upsert a show+season into `month`, creating the month doc if needed.

    Keyed by (trakt_id, season): a new pair is appended as a full record; an
    existing pair is updated in place with whatever keys `show` carries (so this
    doubles as the live-counts writer for Chat 4/5). Returns the saved doc.
    """
    doc = load_month(month) or new_month_doc(month)
    tid = int(show["trakt_id"])
    season = int(show["season"])
    existing = _find_show(doc, tid, season)
    if existing is None:
        doc["shows"].append(_normalize_show(show))
    else:
        existing.update(_coerce_update(show))
    save_month(doc)
    return doc


def set_abandoned(
    month: str,
    trakt_id: int,
    season: int,
    abandoned: bool,
    abandoned_form: str | None = None,
) -> dict | None:
    """Toggle a show's abandoned flag (§4/§5). Returns the updated record, or
    None if the month or the show+season isn't present.

    When abandoning, `abandoned_form` freezes the rendered inline form (the
    renderer that produces it lands in Chat 4; callers may pass None until then).
    Un-abandoning clears the frozen form.
    """
    doc = load_month(month)
    if doc is None:
        return None
    rec = _find_show(doc, int(trakt_id), int(season))
    if rec is None:
        return None
    rec["abandoned"] = bool(abandoned)
    rec["abandoned_form"] = abandoned_form if abandoned else None
    save_month(doc)
    return rec


# ===========================================================================
# CHAT 5 — lazy month rollover, prior-month freeze, and totals staleness
# (BUILD_PLAN §3 refresh + §6 rollover). This is the ORCHESTRATOR layer on top
# of the pure store above; it is the only part that reaches out to Trakt (via
# app/trakt.py) + reads the main-calendar not-watching store (app/state.py).
# ===========================================================================

TOTALS_STALE_HOURS = 24        # §3: auto-refresh open-month totals if stale >24h
WATCHED_RECENCY_DAYS = 60      # §6 step (d): only seed genuinely active shows


def _prev_month_key(month_key: str) -> str:
    year, month = (int(x) for x in month_key.split("-"))
    return f"{year - 1:04d}-12" if month == 1 else f"{year:04d}-{month - 1:02d}"


def can_initialize(month_key: str) -> bool:
    """§6 "no backfill of months earlier than the initial seed". Only a brand-new
    store (no docs yet -> seed) or a month strictly AFTER the latest tracked month
    (forward rollover) may be initialized. This stops backward / gap month-nav
    from silently creating (and Trakt-seeding) historical month docs — the store
    only ever grows forward. YYYY-MM strings compare chronologically."""
    months = list_months()  # sorted ascending
    return not months or month_key > months[-1]


def is_backfill_blocked(month_key: str) -> bool:
    """True when `month_key` has no doc AND may not be initialized (a past / gap
    month reached by navigating backward) — the caller renders it read-only."""
    return load_month(month_key) is None and not can_initialize(month_key)


def month_committed(month_key: str, today: date | None = None) -> bool:
    """True once the calendar has reached (or passed) the 1st of `month_key` — the
    month has officially begun. BEFORE this a month is a "preview": it auto-
    populates from premieres and its main-calendar not-watching toggles only HIDE
    shows (reversibly). ON/AFTER it, not-watching promotes to Abandoned and the
    immediately-prior month freezes."""
    today = today or date.today()
    year, month = int(month_key[:4]), int(month_key[5:7])
    return (today.year, today.month) >= (year, month)


async def maybe_freeze_prior(month_key: str, settings, today: date | None = None) -> None:
    """Freeze the immediately-prior month, but ONLY once `month_key` has begun
    (first access on/after the 1st) and the prior is still open. This is what
    keeps a NEW month's pre-1st preview from freezing the still-current prior
    month. Idempotent — a closed/absent prior is left alone."""
    if not month_committed(month_key, today):
        return
    prior = load_month(_prev_month_key(month_key))
    if prior is not None and not prior.get("closed"):
        await _freeze_month(prior, settings)


def not_watching_ids(year: int, month: int) -> set[str]:
    """Union of main-calendar not-watching ids for the roster's source endpoints
    (shows/new + shows/premieres) for this year/month (§5). Ids are the calendar
    card's `data-id` = slug (preferred) or str(trakt_id) — matched against a
    stored record's slug / trakt_id."""
    from . import state as state_store
    ids: set[str] = set()
    for endpoint_key in ("shows/new", "shows/premieres"):
        st = state_store.load_state(endpoint_key, int(year), int(month))
        for x in st.get("notWatching") or []:
            ids.add(str(x))
    return ids


def _matches_not_watching(rec: dict, nw_ids: set[str]) -> bool:
    return str(rec.get("slug") or "") in nw_ids or str(rec.get("trakt_id")) in nw_ids


def _identity_record(src: dict) -> dict:
    """Identity-only projection (no live counts/dates/bucket; abandoned reset) —
    used to carry a show forward into a new month (Chat 4 handoff: identity only,
    recompute live once the new month opens)."""
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
    }


async def compute_live_shows(records: list[dict], settings, fresh: bool = False,
                             watched_lookup: dict | None = None) -> list[dict]:
    """Merge each stored record with its live Trakt-derived fields into the flat
    "LIVE SHOW SHAPE" discord_fmt expects (+ computed `bucket`).

    Watched counts (`x`) come from the incremental watch-history cache
    (app/watch_history) — the caller may pass a pre-synced `watched_lookup`
    (avoids re-syncing when it also needs the movies from the same state); if
    omitted we sync here. Totals/dates (`y`, cadence, premiere/finale) come from
    one season call per record; `fresh=True` bypasses the 24h season cache."""
    import logging

    from . import discord_fmt, watch_history
    from .perftrace import span
    from .trakt import fetch_season_detail, shared_client
    logger = logging.getLogger(__name__)
    if not records:
        return []

    async def _season_details():
        # The app-wide shared client for the whole fan-out (no per-call client).
        client = shared_client()
        return await asyncio.gather(*(
            fetch_season_detail(settings, rec["trakt_id"], rec["season"], fresh=fresh, client=client)
            for rec in records
        ))

    if watched_lookup is None:
        with span("cls.sync+seasons", n=len(records), fresh=fresh):
            state, details = await asyncio.gather(
                watch_history.sync_and_baseline(settings, [rec["trakt_id"] for rec in records], force=fresh),
                _season_details(),
            )
        watched_lookup = watch_history.watched_map(state)
    else:
        with span("cls.season_gather", n=len(records), fresh=fresh):
            details = await _season_details()
    shows = []
    matched = 0
    for rec, detail in zip(records, details):
        key = (int(rec["trakt_id"]), int(rec["season"]))
        if key in watched_lookup:
            matched += 1
        show = {
            **rec,
            "watched": watched_lookup.get(key, 0),
            "total": detail["total"],
            "cadence": detail["cadence"],
            "premiere": detail["premiere"],
            "finale": detail["finale"],
            "started_airing": detail["started_airing"],
            "finished_airing": detail["finished_airing"],
        }
        show["bucket"] = discord_fmt.bucket_of(show, show)
        shows.append(show)

    # X/Y diagnostic (BUILD_PLAN "0/Y" audit): distinguishes an EMPTY watched
    # lookup (no progress returned — see fetch_watched_map) from a NON-empty
    # lookup that simply doesn't line up with the stored records (an id/season
    # key mismatch), by printing a small sample of each.
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
    — NO Trakt calls (§3). `_freeze_month` already persisted watched/total/
    cadence/premiere/finale/started_airing/finished_airing/bucket onto each
    record, so the discord_fmt renderers read them as-is."""
    out = []
    for rec in doc.get("shows") or []:
        show = dict(rec)
        show.setdefault("started_airing", False)
        show.setdefault("finished_airing", False)
        out.append(show)
    return out


async def _freeze_month(doc: dict, settings) -> dict:
    """Compute one final live snapshot for `doc`, persist counts/dates/bucket onto
    each stored record, mark it closed, stamp totals_refreshed_at, save. After
    this the month renders forever from the frozen snapshot with no Trakt (§3)."""
    from . import watch_history
    records = doc.get("shows") or []
    state = await watch_history.sync_and_baseline(settings, [r["trakt_id"] for r in records], force=True)
    watched_lookup = watch_history.watched_map(state)
    shows = await compute_live_shows(records, settings, fresh=True, watched_lookup=watched_lookup)
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
    # Snapshot the movies watched during this month so the frozen POST 2 keeps
    # its **Movies** section offline forever.
    mstart, mend = watch_history.month_bounds(doc["month"])
    doc["movies"] = watch_history.movies_in_range(state, mstart, mend)
    doc["closed"] = True
    doc["totals_refreshed_at"] = datetime.now(timezone.utc).isoformat()
    save_month(doc)
    return doc


def is_stale(doc: dict | None, max_age_hours: int = TOTALS_STALE_HOURS) -> bool:
    """True if the open month's totals have never been stamped or are older than
    `max_age_hours` (§3: auto-refresh on load if stale >24h)."""
    ts = (doc or {}).get("totals_refreshed_at")
    if not ts:
        return True
    try:
        dt = datetime.fromisoformat(ts)
    except (TypeError, ValueError):
        return True
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - dt > timedelta(hours=max_age_hours)


def stamp_refreshed(month_key: str) -> dict | None:
    """Record that the open month's live totals were just refreshed (§3)."""
    doc = load_month(month_key)
    if doc is None:
        return None
    doc["totals_refreshed_at"] = datetime.now(timezone.utc).isoformat()
    save_month(doc)
    return doc


def _calendar_record(item: dict) -> dict:
    """Identity record from a normalized calendar item (app/trakt.normalize)."""
    return {
        "trakt_id": int(item["trakt_id"]),
        "tmdb": item.get("tmdb"),
        "slug": str(item.get("trakt_slug") or ""),
        "media": "show",
        "title": str(item.get("title") or ""),
        "season": int(item.get("season") or 1),
        "network": str(item.get("network") or ""),
    }


async def _premiere_records(settings, year: int, month: int) -> list[dict]:
    """This month's calendar premieres split by §2a: shows/new -> New (S01);
    shows/premieres minus shows/new -> Returning (S02+)."""
    from .endpoints import get_endpoint
    from .trakt import fetch_calendar
    new_items, prem_items = await asyncio.gather(
        fetch_calendar(get_endpoint("shows/new"), settings, year, month),
        fetch_calendar(get_endpoint("shows/premieres"), settings, year, month),
    )
    out: list[dict] = []
    new_keys: set[tuple[int, int]] = set()
    for item in new_items:
        tid, season = item.get("trakt_id"), item.get("season")
        if tid is None or season is None:
            continue
        new_keys.add((int(tid), int(season)))
        out.append(_calendar_record(item))
    for item in prem_items:
        tid, season = item.get("trakt_id"), item.get("season")
        if tid is None or season is None:
            continue
        if (int(tid), int(season)) in new_keys:
            continue  # this S01 premiere is already counted as a New Shows entry
        out.append(_calendar_record(item))
    return out


async def _add_premieres(doc: dict, present: set[tuple[int, int]], settings,
                         year: int, month: int, nw_ids: set[str]) -> int:
    """Append this month's premieres to `doc` (skip existing + not-watching, §5).
    Mutates `doc['shows']`/`present`; returns the number added."""
    added = 0
    for rec in await _premiere_records(settings, year, month):
        key = (int(rec["trakt_id"]), int(rec["season"]))
        if key in present or _matches_not_watching(rec, nw_ids):
            continue
        doc["shows"].append(_normalize_show(rec))
        present.add(key)
        added += 1
    return added


async def import_premieres(month_key: str, settings) -> dict | None:
    """Merge this month's calendar premieres into an OPEN month doc (skip existing
    + not-watching). Powers the manual "Import from calendar" action and the
    preview-month auto-populate. No-op on a missing/closed month."""
    doc = load_month(month_key)
    if doc is None or doc.get("closed"):
        return doc
    year, month = int(month_key[:4]), int(month_key[5:7])
    present = {(int(s["trakt_id"]), int(s["season"])) for s in doc.get("shows") or []}
    nw_ids = not_watching_ids(year, month)
    if await _add_premieres(doc, present, settings, year, month, nw_ids):
        save_month(doc)
    return doc


async def backfill_tmdb(month_key: str, settings) -> dict | None:
    """Fill in `tmdb` for records added before it was stored (one-time per show).
    Resolves tmdb from the trakt_id via Trakt, dedup by show, persists if changed."""
    from .perftrace import span
    from .trakt import fetch_show_tmdb, shared_client
    doc = load_month(month_key)
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
            save_month(doc)
    return doc


def remove_show(month: str, trakt_id, season) -> bool:
    """Delete a show+season from a month doc (cleanup of mistakes / abandons).
    Returns True if a record was removed. Pure store op — callers guard closed
    (frozen) months."""
    doc = load_month(month)
    if doc is None:
        return False
    tid, season = int(trakt_id), int(season)
    shows = doc.get("shows") or []
    kept = [s for s in shows if not (s.get("trakt_id") == tid and s.get("season") == season)]
    if len(kept) == len(shows):
        return False
    doc["shows"] = kept
    save_month(doc)
    return True


async def _history_records(settings, present: set[tuple[int, int]]) -> list[dict]:
    """§6 step (d): in-progress-but-unfinished shows from recent watch history not
    already in the roster. A candidate is dropped if its season is fully watched
    (completed) or has zero watched episodes (nothing in progress)."""
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


async def ensure_month(year: int, month: int, settings, today: date | None = None) -> dict:
    """Lazy, scheduler-free month rollover (§6). Returns the month doc.

    On EVERY access it first freezes the prior month IF the accessed month has
    begun (maybe_freeze_prior) — so a pre-1st preview of a new month leaves the
    still-current prior month open/editable, and the freeze only lands on first
    access on/after the 1st. Then, if the month doc doesn't exist yet and may be
    created (configured + not backfill-blocked), it initializes it:

      (b) Carry forward prior-month shows EXCEPT completed/abandoned (identity
          only; live fields recompute once this month opens). Prior buckets come
          from its frozen snapshot when closed, else computed live (preview case).
      (c) Add this month's calendar premieres (shows/new -> New, shows/premieres
          minus new -> Returning), EXCLUDING not-watching (§5 before-commit).
      (d) Add in-progress-but-unfinished shows from recent history not present.

    An already-initialized month is returned untouched (aside from the prior-month
    freeze), so PAST months never re-run initialization.
    """
    today = today or date.today()
    month_key = f"{int(year):04d}-{int(month):02d}"
    existing = load_month(month_key)

    # Freeze the prior month only once THIS month has actually begun (not during a
    # pre-1st preview). Skip when accessing an already-closed month (settled).
    if settings and getattr(settings, "configured", False) and (existing is None or not existing.get("closed")):
        await maybe_freeze_prior(month_key, settings, today)

    if existing is not None:
        return load_month(month_key)
    if not (settings and getattr(settings, "configured", False)):
        # Initialization needs Trakt (premieres + history); without credentials
        # return a transient, UNPERSISTED empty doc so a proper init still happens
        # once Trakt is configured (rather than baking in an empty month).
        return new_month_doc(month_key)
    if not can_initialize(month_key):
        # Backward / gap navigation to a never-tracked past month: DO NOT backfill
        # (§6) — return a transient, UNPERSISTED empty doc (rendered read-only).
        return new_month_doc(month_key)

    doc = new_month_doc(month_key)
    present: set[tuple[int, int]] = set()

    # (b) Carry forward everything except Completed / Abandoned. An open (not-yet-
    # frozen) prior is bucketed live so a preview rollover still drops the right
    # shows; a frozen prior reuses its stored buckets.
    prior = load_month(_prev_month_key(month_key))
    if prior is not None:
        prior_shows = frozen_shows(prior) if prior.get("closed") \
            else await compute_live_shows(prior.get("shows") or [], settings)
        for s in prior_shows:
            if s.get("abandoned") or s.get("bucket") in ("completed", "abandoned"):
                continue
            key = (int(s["trakt_id"]), int(s["season"]))
            if key in present:
                continue
            doc["shows"].append(_normalize_show(_identity_record(s)))
            present.add(key)

    # (c) This month's premieres, minus not-watching (excluded before commit, §5).
    nw_ids = not_watching_ids(int(year), int(month))
    await _add_premieres(doc, present, settings, int(year), int(month), nw_ids)

    # (d) In-progress-but-unfinished shows from recent history.
    for rec in await _history_records(settings, present):
        key = (int(rec["trakt_id"]), int(rec["season"]))
        if key in present:
            continue
        doc["shows"].append(_normalize_show(rec))
        present.add(key)

    doc["totals_refreshed_at"] = datetime.now(timezone.utc).isoformat()
    save_month(doc)
    return doc
