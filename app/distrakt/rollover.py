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
from . import calendar_import, store
from .live import compute_live_shows, live_key
from .store import (
    ADDED_BY_HISTORY,
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
    """Compute one final live snapshot for `doc`, persist counts/dates/bucket onto
    each stored record, mark it closed, stamp totals_refreshed_at, save. After
    this the month renders forever from the frozen snapshot with no Trakt calls."""
    from . import watch_history
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

    Completed means "completed this month". A month gets its roster once, when it
    is created, and a title on it can be finished at any point afterwards — a
    premiere imported into August that turns out to have been finished off in
    July would otherwise sit in August's Completed for good, being fully watched.
    This closes that window on every load.

    Removed rather than re-bucketed: with the Completed rule applied, a fully
    watched season would otherwise fall through to Cleanup or Keepup and read as
    work outstanding, which is worse than not being on the month at all. It is
    still on the month it WAS finished in, which is also where the user's own
    lists read it from (store.user_roster), so nothing is lost by taking it off
    this one.

    Only acts on a date it actually has: a season the history cache cannot date
    (nothing dated for it, or a title that predates dated history and has not been
    re-baselined yet) is left exactly where it is.
    """
    from . import watch_history
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
    """A brand-new month's contents: the calendar's premieres, plus — once the
    month has begun — whatever a season part-way through in recent viewing adds
    that the premieres did not. The second source only adds what the first did
    not, so `added_by` records the truth about who put a row there rather than the
    last writer to touch it.

    A NEW MONTH TAKES NOTHING FROM THE MONTH BEFORE IT. What the user is behind on
    and what they are keeping up with are facts about the USER — true of no
    particular month — and they are read live from every month at once
    (store.user_roster), so there is nothing here for a new month to inherit.
    Copying them forward is what made a month claim a title that premiered
    somewhere else, gave a month built ahead of time a roster frozen at build
    time, and read a calendar turn-away made during that wait as giving up on a
    show that had never started. None of the three can be stated in a model where
    a month holds only its own premieres.

    A MONTH THAT HAS NOT BEGUN TAKES ITS PREMIERES AND NOTHING ELSE. The recent-
    viewing sweep is a statement about NOW, and it has nothing to say about a
    month nobody has reached; filed into one anyway its titles were bucketed by
    whether they had aired YET, which they had not, so a season that began in
    August was announced as new in October.
    """
    doc = new_month_doc(month_key)
    present: set[tuple[str, int]] = set()
    year, month = store.parse_month_key(month_key)
    begun = month_committed(month_key, today)

    # Read ONCE and applied to both sources below: a title the user has turned
    # away on their calendar does not get built into a month at all. A month that
    # opens with it listed as Abandoned is announcing a verdict on a show they had
    # already said they were not following, and there is no row here yet for such
    # a verdict to be about. WHEN the mark was made is what separates that from "I
    # was following this and stopped", and it is read against a row that already
    # exists — see routes._apply_not_watching.
    nw_ids = await calendar_state.not_watching_ids(user_id)

    # This month's premieres, minus not-watching.
    await calendar_import.add_premieres(doc, present, user_id, settings, year, month, nw_ids)

    # In-progress-but-unfinished titles from recent history, only once the month
    # has begun: what somebody is part-way through today is THIS month's material
    # whichever month is being built, and it was the sweep that filed it under a
    # month that has not happened. This is also the one way a title nobody's
    # calendar announced gets onto the roster at all.
    if begun:
        for rec in await history_records(settings, present):
            if calendar_import.matches_not_watching(rec, nw_ids):
                continue
            key = (str(record_key(rec)), int(rec["season"]))
            if key in present:
                continue
            doc["shows"].append(normalize_show({**rec, "added_by": ADDED_BY_HISTORY}))
            present.add(key)
    return doc
