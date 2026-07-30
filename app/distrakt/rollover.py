"""Lazy month rollover, prior-month freeze, and totals staleness.

ONE JOB — decide what a month should CONTAIN when it is first reached, and when a
month stops being editable. There is no scheduler: every rule here fires on
access, which is why each of them has to be idempotent.

This is the orchestrator layer over the row store: it is the part that reaches
out to a provider (for premieres, watch history and season detail) and reads the
per-user main-calendar not-watching store (app/calendar_state.py).
"""
from __future__ import annotations

import asyncio
from datetime import date

from .. import calendar_state, db, discord_fmt
from ..providers.base import Media, collect_ids
from . import calendar_import, store
from .live import compute_live_shows, live_key
from .store import (
    ADDED_BY_HISTORY,
    frozen_shows,
    list_months,
    load_month,
    new_month_doc,
    normalize_show,
    record_key,
    remove_show,
    save_month,
)

TOTALS_STALE_HOURS = 24        # auto-refresh open-month totals if stale >24h
WATCHED_RECENCY_DAYS = 60      # only seed genuinely active shows from history


def prev_month_key(month_key: str) -> str:
    year, month = (int(x) for x in month_key.split("-"))
    return store.month_key(year - 1, 12) if month == 1 else store.month_key(year, month - 1)


def month_committed(month_key: str, today: date | None = None) -> bool:
    """True once the calendar has reached (or passed) the 1st of `month_key` — the
    month has officially begun. BEFORE this a month is a "preview": it auto-
    populates from premieres and its main-calendar not-watching toggles only HIDE
    shows (reversibly). ON/AFTER it, not-watching promotes to Abandoned and the
    immediately-prior month freezes."""
    today = today or date.today()
    year, month = int(month_key[:4]), int(month_key[5:7])
    return (today.year, today.month) >= (year, month)


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


async def maybe_freeze_prior(user_id: int, month_key: str, settings, today: date | None = None) -> None:
    """Freeze `user_id`'s immediately-prior month, but ONLY once `month_key` has
    begun (first access on/after the 1st) and the prior is still open. This is
    what keeps a NEW month's pre-1st preview from freezing the still-current prior
    month. Idempotent — a closed/absent prior is left alone. Per user: one user
    reaching the 1st does not freeze anyone else's prior month."""
    if not month_committed(month_key, today):
        return
    prior = await load_month(user_id, prev_month_key(month_key))
    if prior is not None and not prior.get("closed"):
        await freeze_month(user_id, prior, settings)


def identity_record(src: dict) -> dict:
    """Identity-only projection (no live counts/dates/bucket; abandoned reset) —
    used to carry a title forward into a new month (identity only; recompute live
    once the new month opens)."""
    return {
        "media": src.get("media") or Media.SHOW,
        "match_source": src.get("match_source"),
        "match_id": src.get("match_id"),
        "ids": collect_ids(src.get("ids") or {}),
        "title": str(src.get("title") or ""),
        "season": int(src["season"]),
        "network": str(src.get("network") or ""),
        "abandoned": False,
        "abandoned_form": None,
        # Carried forward with the show: a premiere that rolls into next month is
        # still the calendar's row, and a hand-added one is still the user's.
        "added_by": str(src.get("added_by") or ""),
    }


async def freeze_month(user_id: int, doc: dict, settings) -> dict:
    """Compute one final live snapshot for `doc`, persist counts/dates/bucket onto
    each stored record, mark it closed, stamp totals_refreshed_at, save. After
    this the month renders forever from the frozen snapshot with no Trakt calls."""
    from .. import watch_history
    records = doc.get("shows") or []
    state = await watch_history.sync_and_baseline(settings, user_id, records, force=True)
    watched_lookup = watch_history.watched_map(state)
    shows = await compute_live_shows(user_id, records, settings, fresh=True,
                                    watched_lookup=watched_lookup)
    by_key = {live_key(s): s for s in shows}
    for rec in records:
        s = by_key.get(live_key(rec))
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
    (nothing dated for it, or a title that predates dated history and has not been
    re-baselined yet) is left exactly where it is.
    """
    from .. import watch_history
    start, _ = watch_history.month_bounds(month_key)
    keep, stale = [], []
    for show in shows:
        done = str(show.get("completed_on") or "")
        (stale if done and done < start else keep).append(show)
    for show in stale:
        await remove_show(user_id, month_key, record_key(show), int(show["season"]))
    return keep


async def history_records(settings, present: set[tuple[str, int]]) -> list[dict]:
    """In-progress-but-unfinished shows from recent watch history not already in
    the roster. A candidate is dropped if its season is fully watched (completed)
    or has zero watched episodes (nothing in progress)."""
    from .. import providers
    from ..providers.trakt.detail import fetch_season_detail
    port = providers.for_tracker()
    if port is None:
        return []
    progress = await port.fetch_watched_progress(settings, since_days=WATCHED_RECENCY_DAYS)
    candidates = []
    for entry in progress:
        rec = {
            "media": Media.SHOW,
            "ids": collect_ids(entry.get("ids") or {}),
            "title": str(entry.get("title") or ""),
            "season": int(entry["season"]),
            "network": str(entry.get("network") or ""),
        }
        try:
            key = record_key(rec)
        except ValueError:
            continue  # nothing shared to file it under; not this pass's problem
        if (str(key), int(rec["season"])) in present or int(entry.get("watched") or 0) <= 0:
            continue
        candidates.append((rec, entry))
    if not candidates:
        return []
    details = await asyncio.gather(*(
        fetch_season_detail(settings, (rec["ids"]).get("trakt"), rec["season"])
        for rec, _ in candidates
    ))
    out = []
    for (rec, entry), detail in zip(candidates, details):
        total = int(detail.get("total") or 0)
        watched = int(entry.get("watched") or 0)
        if total > 0 and watched >= total:
            continue  # already completed -> not "in-progress-but-unfinished"
        out.append(rec)
    return out


async def ensure_month(user_id: int, year: int, month: int, settings, today: date | None = None) -> dict:
    """Lazy, scheduler-free month rollover for one user. Returns the month doc.

    On EVERY access it first freezes the prior month IF the accessed month has
    begun (maybe_freeze_prior) — so a pre-1st preview of a new month leaves the
    still-current prior month open/editable, and the freeze only lands on first
    access on/after the 1st. Then, if the month doc doesn't exist yet and may be
    created (configured + not backfill-blocked), it initializes it — see
    _initialize_month for what goes in and in what order.

    An already-initialized month is returned untouched (aside from the prior-month
    freeze), so PAST months never re-run initialization.
    """
    today = today or date.today()
    month_key = store.month_key(year, month)
    existing = await load_month(user_id, month_key)
    configured = bool(settings and getattr(settings, "trakt_configured", False))

    # Freeze the prior month only once THIS month has actually begun (not during a
    # pre-1st preview). Skip when accessing an already-closed month (settled).
    if configured and (existing is None or not existing.get("closed")):
        await maybe_freeze_prior(user_id, month_key, settings, today)

    if existing is not None:
        return await load_month(user_id, month_key)
    if not configured:
        # Initialization needs Trakt (premieres + history); without credentials
        # return a transient, UNPERSISTED empty doc so a proper init still happens
        # once Trakt is configured (rather than baking in an empty month).
        return new_month_doc(month_key)
    if not await can_initialize(user_id, month_key):
        # Backward / gap navigation to a never-tracked past month: DO NOT backfill
        # — return a transient, UNPERSISTED empty doc (rendered read-only).
        return new_month_doc(month_key)

    doc = await _initialize_month(user_id, month_key, settings)
    doc["totals_refreshed_at"] = db.now()
    await save_month(user_id, doc)
    return doc


async def _initialize_month(user_id: int, month_key: str, settings) -> dict:
    """A brand-new month's contents, in the order the three sources are allowed to
    contribute: carried-forward titles first (they are the ones with history), then
    the calendar's premieres, then whatever recent watch history suggests. Each
    later source only adds what the earlier ones did not, so being carried forward
    beats being re-imported, and `added_by` records the truth about who put a row
    there rather than the last writer to touch it.
    """
    doc = new_month_doc(month_key)
    present: set[tuple[str, int]] = set()
    year, month = int(month_key[:4]), int(month_key[5:7])

    # Carry forward everything except Completed / Abandoned. An open (not-yet-
    # frozen) prior is bucketed live so a preview rollover still drops the right
    # titles; a frozen prior reuses its stored buckets.
    prior = await load_month(user_id, prev_month_key(month_key))
    if prior is not None:
        prior_shows = frozen_shows(prior) if prior.get("closed") \
            else await compute_live_shows(user_id, prior.get("shows") or [], settings)
        for s in prior_shows:
            if s.get("abandoned") or s.get("bucket") in (
                    discord_fmt.Bucket.COMPLETED, discord_fmt.Bucket.ABANDONED):
                continue
            key = live_key(s)
            if key in present:
                continue
            doc["shows"].append(normalize_show(identity_record(s)))
            present.add(key)

    # This month's premieres, minus not-watching (excluded before commit).
    nw_ids = await calendar_state.not_watching_ids(user_id)
    await calendar_import.add_premieres(doc, present, user_id, settings, year, month, nw_ids)

    # In-progress-but-unfinished titles from recent history.
    for rec in await history_records(settings, present):
        key = (str(record_key(rec)), int(rec["season"]))
        if key in present:
            continue
        doc["shows"].append(normalize_show({**rec, "added_by": ADDED_BY_HISTORY}))
        present.add(key)
    return doc
