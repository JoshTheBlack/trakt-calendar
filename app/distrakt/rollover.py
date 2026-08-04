"""Lazy month rollover, the freeze of a month the calendar has passed, and totals
staleness.

ONE JOB — decide what a month should CONTAIN when it is first reached, and when a
month stops being editable. There is no scheduler: every rule here fires on
access, which is why each of them has to be idempotent.

This is the orchestrator layer over the row store: it is the part that reaches
out to a provider (for premieres, watch history and season detail) and reads the
per-user main-calendar not-watching store (app/calendar/state.py).
"""
from __future__ import annotations

import asyncio
from datetime import date

from .. import clock, db
from ..calendar import state as calendar_state
from ..providers.base import Media, collect_ids
from . import calendar_import, lifecycle, store
from .live import compute_live_shows, live_key
from .store import (
    ADDED_BY_HISTORY,
    list_months,
    load_month,
    new_month_doc,
    record_key,
    save_month,
)

TOTALS_STALE_HOURS = 24        # auto-refresh open-month totals if stale >24h
WATCHED_RECENCY_DAYS = 60      # only seed genuinely active shows from history


def prev_month_key(month_key: str) -> str:
    year, month = store.parse_month_key(month_key)
    return store.month_key(year - 1, 12) if month == 1 else store.month_key(year, month - 1)


def month_committed(month_key: str, today: date | None = None) -> bool:
    """True once the calendar has reached (or passed) the 1st of `month_key` — the
    month has officially begun. BEFORE this a month is a "preview": it auto-
    populates from premieres and its main-calendar not-watching toggles only HIDE
    shows (reversibly). ON/AFTER it, not-watching promotes to Abandoned.

    Reads the standing rather than comparing the key again here: "has this month
    begun" and "is this month over" are the same comparison asked twice, and two
    copies of it drift silently because neither one looks wrong on its own."""
    return store.month_standing(month_key, today) is not store.MonthStanding.FUTURE


async def can_initialize(user_id: int, month_key: str, today: date | None = None) -> bool:
    """Whether the tracker may BUILD `month_key` for this user.

    ANY MONTH THE CALENDAR HAS NOT PASSED, AT ANY DISTANCE. The month under way
    and every month ahead of it are all fair game, and a gap between them is
    fine: nothing has to be built in order, and one skipped over can be filled in
    afterwards. Building a month ahead is a deliberate act — opening one gathers
    nothing (routes._distrakt_month_payload), and the Import control is what asks
    for it by name — so the ask is the safeguard, and a bound on how far ahead it
    may point was doing a job the ask already does. Bounded, it did harm instead:
    the store grew forward only, so a December built during August stranded
    September, October and November for good.

    A MONTH THE CALENDAR HAS ALREADY PASSED IS STILL REFUSED, so backward or gap
    month-nav cannot silently invent (and Trakt-seed) history for a month nobody
    was tracking at the time. Filling those in is what the watch-history backfill
    is for (app/distrakt/backfill.py) — it works them out from what was actually
    watched and writes them outright rather than coming through here — and titles
    can be put onto an old month by hand.

    Two long-standing exceptions to that refusal survive: a store with no months
    at all is a first seed and may start wherever the user starts it, and a month
    later than everything already tracked is the forward growth the store was
    built around. YYYY-MM strings compare chronologically.
    """
    if store.month_standing(month_key, today) is not store.MonthStanding.PAST:
        return True
    months = await list_months(user_id)  # sorted ascending
    return not months or month_key > months[-1]


async def is_backfill_blocked(user_id: int, month_key: str, today: date | None = None) -> bool:
    """True when `month_key` has no doc for this user AND may not be initialized (a
    past / gap month reached by navigating backward) — rendered read-only."""
    return ((await load_month(user_id, month_key)) is None
            and not await can_initialize(user_id, month_key, today))


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


def freeze_eligible(month_key: str, today: date | None = None) -> bool:
    """Whether `month_key` has settled and may be frozen: the calendar has left it
    behind.

    THE CLOCK DECIDES THIS AND NOTHING ELSE. A month stops being editable because
    its own dates are over, not because some later month happens to exist or to
    have been looked at. Asked the other way round — "has the month after it
    begun?" — a store nobody touched for three weeks kept July open and editable
    right through August, because the thing that was supposed to close it was a
    side effect of opening a month the user never opened.

    Being eligible is not the same as being frozen: taking the final snapshot
    costs a live read (see freeze_month), so it is taken the next time somebody
    looks. There is no scheduler in this app and this rule does not need one — a
    month is settled from the moment its date passes, and the snapshot merely
    writes down what was already true."""
    return store.month_standing(month_key, today) is store.MonthStanding.PAST


async def maybe_freeze(user_id: int, month_key: str, settings, today: date | None = None) -> None:
    """Freeze `user_id`'s `month_key` if the calendar has passed it and it is still
    open — the lazy half of freeze_eligible. Idempotent: a closed, absent or
    still-running month is left alone. Per user, because one user reaching the 1st
    says nothing about anyone else's roster."""
    if not freeze_eligible(month_key, today):
        return
    doc = await load_month(user_id, month_key)
    if doc is not None and not doc.get("closed"):
        await freeze_month(user_id, doc, settings)


async def maybe_freeze_prior(user_id: int, month_key: str, settings, today: date | None = None) -> None:
    """Freeze the month immediately BEFORE `month_key` if it has settled.

    The month being looked at freezes itself (maybe_freeze); this is here so the
    one before it does not have to wait to be visited. On the 1st of August the
    user opens August, not July, and taking July's snapshot then records its
    counts while they are still the counts July ended on rather than whatever they
    have drifted to by the time somebody next opens it."""
    await maybe_freeze(user_id, prev_month_key(month_key), settings, today)


async def freeze_month(user_id: int, doc: dict, settings) -> dict:
    """Take one final snapshot for `doc`: correct what its records say about the
    shows, snapshot the films watched during it, mark it closed and stamp
    totals_refreshed_at. After this the month renders from its own rows with no
    provider calls.

    WHAT A FREEZE MAY REWRITE HAS NARROWED, because the records themselves now
    carry their own answers. A settled record — completed, abandoned — already
    holds the counts it settled on, written at the moment the verdict was reached,
    and re-deriving them here from today's history would let a rewatch months
    later rewrite what a month recorded. A PREMIERE record carries no viewer
    progress at all, so the only thing a lookup can tell us about it is the
    catalogue half: how many episodes the season turned out to have, and when it
    started and ended. That much is corrected, here and afterwards, because it is
    a statement about the show rather than about the viewer.
    """
    from . import watch_history
    records = doc.get("shows") or []
    premieres = [rec for rec in records if rec["kind"] in store.PREMIERE_KINDS]
    # THIS MONTH'S history is re-read from its own first day, and every title's
    # progress is NOT re-fetched. The film list has to be complete, because a
    # closed month never recomputes it. Re-asking what the viewer has seen of every
    # title ever tracked buys nothing here: a settled record already holds the
    # counts it settled on, and a premiere record carries no viewer progress to
    # correct. The two used to be one switch, so a freeze cost a provider call per
    # tracked title to write down a dozen films — and read the wrong month's
    # history while doing it, because the only start it knew was today's.
    state = await watch_history.sync_and_baseline(
        settings, user_id, records, since_month=doc["month"])
    if premieres:
        shows = await compute_live_shows(
            user_id, premieres, settings, fresh=True,
            watched_lookup=watch_history.watched_map(state),
            # The freeze did its own sync above, so it is the one that knows
            # which services answered for this account.
            sources_read=await watch_history.tracker_sources(settings, user_id))
        by_key = {live_key(s): s for s in shows}
        for rec in premieres:
            live = by_key.get(live_key(rec))
            if live is None:
                continue
            rec.update({field: live[field] for field in lifecycle.CATALOGUE_FIELDS})
    # Snapshot the movies watched during this month so the frozen second notice
    # keeps its **Movies** section offline forever.
    mstart, mend = watch_history.month_bounds(doc["month"])
    doc["movies"] = watch_history.movies_in_range(state, mstart, mend)
    doc["closed"] = True
    doc["totals_refreshed_at"] = db.now()
    await save_month(user_id, doc)
    return doc


async def history_records(user_id: int, settings, present: set[tuple[str, int]]) -> list[dict]:
    """Seasons from recent viewing that the viewer is part-way through, as records
    for their OWN LIST rather than for any month.

    A season somebody is in the middle of is a fact about them and about no
    particular month, so this is the one source that seeds the viewer's list
    directly. A candidate is dropped if its season is fully watched (there is
    nothing left to get through) or has no episodes watched at all (nothing has
    been started).

    The kind — keepup or catchup — comes from the same season lookup that decides
    whether the season is finished, so it costs nothing extra and is right from
    the first write instead of being corrected on the next load.
    """
    from . import live, watch_history
    # THE PRIMARY SOURCE ALONE. This seeds the viewer's OWN LIST with seasons they
    # are part-way through, and a season is on that list or it is not — there is
    # no half-listed. Two services proposing overlapping sets of "you seem to be
    # watching this" would add the union — the one collapse the counts rule
    # refuses everywhere a disagreement can actually be shown. The ordinary sync
    # then counts every admitted source against whatever ends up listed, so the
    # second service still contributes its numbers to every row here.
    ports = await watch_history.tracker_ports(settings, user_id)
    if not ports:
        return []
    _source, port = ports[0]
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
    details = await live.fetch_season_details(
        settings, [rec for rec, _ in candidates], fresh=False, allow_degrade=False)
    out = []
    for (rec, entry), detail in zip(candidates, details):
        total = int(detail.get("total") or 0)
        watched = int(entry.get("watched") or 0)
        if total > 0 and watched >= total:
            continue  # already finished -> nothing left on the pile
        out.append({
            **rec,
            "kind": lifecycle.listed_kind(detail),
            "watched": watched,
            "total": total,
            "cadence": detail.get("cadence"),
            "premiere": detail.get("premiere"),
            "finale": detail.get("finale"),
            "started_airing": bool(detail.get("started_airing")),
            "finished_airing": bool(detail.get("finished_airing")),
            "added_by": ADDED_BY_HISTORY,
        })
    return out


async def ensure_month(user_id: int, year: int, month: int, settings, today: date | None = None) -> dict:
    """Lazy, scheduler-free month rollover for one user. Returns the month doc.

    On EVERY access it first freezes whichever of the accessed month and the one
    before it the calendar has already passed (maybe_freeze / maybe_freeze_prior),
    so a pre-1st preview of a new month leaves the still-current prior month
    open/editable. Then, if the month doc doesn't exist yet and may be created
    (configured + not backfill-blocked), it initializes it — see _initialize_month
    for what goes in and in what order.

    An already-initialized month is returned untouched (aside from those freezes),
    so PAST months never re-run initialization.

    BUILDING A MONTH THAT HAS NOT BEGUN IS SOMETHING SOMEBODY ASKS FOR. This
    function will do it, however far ahead the month is and whatever gap it leaves
    behind it (see can_initialize), but the page load does not call it for such a
    month; the Import control does. The rule and the reason are at the one place
    that decides it, routes._distrakt_month_payload — which is also why there is
    no bound here on how far ahead a month may be: nothing reaches this by
    accident.
    """
    today = today or clock.today()
    month_key = store.month_key(year, month)
    existing = await load_month(user_id, month_key)
    configured = bool(settings and getattr(settings, "trakt_configured", False))

    # A month settles by the calendar alone, so the one being read here freezes on
    # this very access if its own dates are over — leaving the tracker alone for
    # weeks no longer leaves a finished month editable. The month before it is
    # frozen too, so its snapshot is taken while its counts are still the ones it
    # ended on rather than whenever somebody next thinks to open it.
    if configured:
        await maybe_freeze(user_id, month_key, settings, today)
        await maybe_freeze_prior(user_id, month_key, settings, today)

    if existing is not None:
        return await load_month(user_id, month_key)
    if not configured:
        # Initialization needs Trakt (premieres + history); without credentials
        # return a transient, UNPERSISTED empty doc so a proper init still happens
        # once Trakt is configured (rather than baking in an empty month).
        return new_month_doc(month_key)
    if not await can_initialize(user_id, month_key, today):
        # Backward / gap navigation to a never-tracked past month: DO NOT backfill
        # — return a transient, UNPERSISTED empty doc (rendered read-only).
        return new_month_doc(month_key)

    doc = await _initialize_month(user_id, month_key, settings, today)
    doc["totals_refreshed_at"] = db.now()
    await save_month(user_id, doc)
    return doc


async def _initialize_month(user_id: int, month_key: str, settings,
                            today: date | None = None) -> dict:
    """A brand-new month's contents: the calendar's premieres, and nothing else.

    A MONTH HOLDS WHAT BEGAN IN IT. What the viewer is behind on and what they are
    keeping up with are facts about the VIEWER, true of no particular month, and
    they live on their own list (store.user_records) — so there is nothing here for
    a new month to inherit from the one before it. Copying them forward is what
    made a month claim a title that premiered somewhere else, gave a month built
    ahead of time a list frozen at build time, and read a calendar turn-away made
    during that wait as giving up on a show that had never started. None of the
    three can be stated in a model where a month holds only its own premieres.

    ONCE THE MONTH HAS BEGUN, recent viewing also seeds the VIEWER'S LIST — the
    one way a season nobody's calendar announced is ever noticed at all. Those
    records go onto the list rather than onto this month: being part-way through
    something is a statement about now, and a month that has not been reached has
    nothing to say about it. Filed into one anyway, such a title was announced as
    that month's premiere because it had not aired YET by that month's reckoning,
    so a season that began in August was announced as new in October.
    """
    doc = new_month_doc(month_key)
    present: set[tuple[str, int]] = set()
    year, month = store.parse_month_key(month_key)
    begun = month_committed(month_key, today)

    # Read ONCE and applied to both sources below: a title the viewer has turned
    # away on their calendar is not built in at all. A month that opens with it
    # listed as given up on is announcing a verdict about a show they had already
    # said they were not following, and there is no record here yet for such a
    # verdict to be about.
    nw_ids = await calendar_state.not_watching_ids(user_id)

    # This month's premieres, minus not-watching.
    await calendar_import.add_premieres(doc, present, user_id, settings, year, month, nw_ids)

    if begun:
        present |= {(str(store.record_key(rec)), int(rec["season"]))
                    for rec in await store.user_records(user_id)}
        for rec in await history_records(user_id, settings, present):
            if calendar_import.matches_not_watching(rec, nw_ids):
                continue
            await store.add_user_record(user_id, rec)
    return doc
