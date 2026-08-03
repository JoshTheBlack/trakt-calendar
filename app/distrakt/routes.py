"""The hidden Discord tracker: its page shell and the whole /api/distrakt/* API.

Every route here is DISTRAKT_APPROVED, and every one of them reads ONE person's
private Trakt history — their progress, their plays, their films. That is why
`_distrakt_settings` exists and why nothing here uses the app-wide token: the
operator's token would hand every user the operator's viewing instead of their
own.

Most of the mutating routes end by returning the whole recomputed month, so the
page never has to guess what its change did. That shared body is
`_distrakt_month_payload`, and it comes in three shapes depending on what can be
reached: a frozen past month rendered from its own snapshot, an open month
computed live against Trakt, and — when a shared Trakt prerequisite fails — the
last-known stored totals plus a notice, at HTTP 200. The third one is why the
tracker stays usable through a rate-limit window instead of showing 0/0.
"""
from __future__ import annotations

import asyncio
import calendar as _calendar
import dataclasses
import json
import logging
from datetime import date, datetime, timezone

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from . import backfill, discord_fmt, watch_history
# The data layer is reached through this package's own public surface, the same
# names an outside caller uses, rather than through the six modules those names
# are defined in: a route handler has no business knowing which half of the
# tracker `load_month` or `compute_live_shows` lives in.
from .. import distrakt as distrakt_store
from .. import auth, authz, chrome, clock, db, route_params
from ..auth import trakt_routes
from ..auth import AuthLevel
from ..calendar import share_links, state as calendar_state
from ..config import load_settings
from ..endpoints import endpoint_choices
from ..media import logos
from ..perftrace import span
from ..providers.base import ID_KEYS, ItemKey, Media, collect_ids, parse_item_key
from ..providers.trakt import TraktError, TraktRateLimitError
from ..providers.trakt.detail import (
    fetch_details,
    fetch_season_detail,
    fetch_show_seasons,
    search_shows,
    search_titles,
)
from ..providers.trakt.sync import fetch_watched_map
from ..templating import templates

logger = logging.getLogger(__name__)

router = APIRouter()
guard = authz.Guard(router)

# Which ids arriving in a request body are read as integers. imdb ids are not
# numbers and slugs are words, so those stay text; everything else is a numeric id
# in its own namespace and is coerced rather than stored as whatever the client
# happened to send, or the same title would key differently on two adds.
_NUMERIC_ID_KEYS = frozenset(ID_KEYS) - {"imdb", "slug"}

# What an empty month that has not begun says for itself. There is only ever one
# sentence for it now, at any distance ahead: every future month is waiting to be
# asked, and the ask is a button on the page saying this.
MONTH_AWAITS_IMPORT = ("This month hasn't begun yet, so nothing has been gathered for it. "
                       "Press ⤓ Import to build it from what premieres in it.")


class RequestError(ValueError):
    """A request body the caller has to fix. Its message is what the client is
    told, so it says what is wrong rather than naming a field type."""


def _client_ids(raw) -> dict:
    """An id map out of a request body: known namespaces only, numbers as numbers.

    The client supplies these on the add flows, which it always has — what is new
    is that they decide which row the record becomes, so they are narrowed and
    coerced here at the boundary rather than trusted into a key.
    """
    if not isinstance(raw, dict):
        return {}
    cleaned = {}
    for key, value in raw.items():
        if key not in ID_KEYS or value in (None, ""):
            continue
        if key in _NUMERIC_ID_KEYS:
            try:
                cleaned[key] = int(value)
            except (TypeError, ValueError):
                continue  # a numeric namespace with a non-number in it names nothing
        else:
            cleaned[key] = str(value)
    return collect_ids(cleaned)


def _row_target(data: dict) -> tuple[ItemKey, int]:
    """The (identity, season) a mutating request names, or a RequestError.

    ONE parser for every route that changes a roster row, because they all address
    a row the same way — the flat item key the month payload handed the client,
    plus the season. The key is validated against the same closed sets the ranker's
    is (see providers.base.parse_item_key), so a malformed one is refused here
    rather than reaching a query.
    """
    try:
        key = parse_item_key(data.get("key"))
    except ValueError as exc:
        raise RequestError(str(exc)) from None
    try:
        season = int(data["season"])
    except (KeyError, TypeError, ValueError):
        raise RequestError("Missing or invalid season.") from None
    return key, season


def _merge_live_show(rec: dict, watched_lookup: dict, detail: dict) -> dict:
    """Combine a stored record (identity + abandoned/abandoned_form) with its
    live Trakt-derived fields into the flat "LIVE SHOW SHAPE" discord_fmt
    expects (see discord_fmt.py's module docstring), plus the computed
    `bucket` for the UI to group by."""
    show = {
        **rec,
        "watched": watched_lookup.get(distrakt_store.live_key(rec), 0),
        "total": detail["total"],
        "cadence": detail["cadence"],
        "premiere": detail["premiere"],
        "finale": detail["finale"],
        "started_airing": detail["started_airing"],
        "finished_airing": detail["finished_airing"],
    }
    show["bucket"] = discord_fmt.bucket_of(show, show)
    return show


async def _distrakt_user_id(request: Request) -> int:
    """The signed-in user whose tracker this request is for. Every distrakt route
    is gated DISTRAKT_APPROVED, so a user is always present by the time a handler
    runs; current_user is cached on the request by the dependency that gated it."""
    user = await auth.current_user(request)
    return user.user_id


async def _distrakt_settings(user_id: int):
    """The app-wide settings with the Trakt credential swapped for `user_id`'s own.

    The tracker reads one person's private watch history — their progress, their
    plays, their movies — so every Trakt call it makes has to authenticate as
    THEM. The token in settings.json belongs to the operator and would hand every
    user the operator's viewing instead of their own. Everything else on the
    object (the network emoji map, the TMDB key, the genre/country strings) is
    genuinely app-wide and is carried through untouched.

    The refresh token is cleared as well: nothing downstream refreshes, and
    leaving the operator's beside somebody else's access token would be a pairing
    that means nothing. The access level guarantees a linked Trakt identity, but
    a row can still hold an empty token, in which case `trakt_configured` goes
    false and the handlers take their existing "not configured" path.

    The network->emoji map is per-user too, but it is NOT on this object: it was
    removed from Settings entirely when it stopped being app-wide, so the
    renderers take it as an argument (see _distrakt_month_payload). Keeping it off
    `settings` is deliberate — there is no longer an app-wide value for a caller
    to reach for by mistake.
    """
    token = await trakt_routes.access_token_for_user(user_id)
    return dataclasses.replace(
        load_settings(), trakt_access_token=token or "", trakt_refresh_token="",
    )


async def _distrakt_post_link(user_id: int, settings, year: int, month: int) -> str | None:
    """The public calendar link this user's announcement post embeds, or None when
    they have nothing publishable — no configured public base URL, or every link
    form switched off. Omitted rather than rendered empty in that case.

    Pinned to the month the post announces, so a post read in September still
    opens on the August it is about."""
    row = await share_links.get_or_create(user_id)
    user = await auth.get_user(user_id)
    return share_links.post_link_with_view(
        row, user["username"] if user else None, settings.public_base_url,
        year=year, month=month,
    )


def _chooser_months(year: int, tracked: set[str]) -> list[dict]:
    """The twelve tiles the chooser draws for `year`: which month each one is, and
    whether it is one the user already has.

    EVERY month is offered, in every year. Opening one costs nothing on its own —
    a month behind the calendar that was never tracked renders empty and
    read-only, and one ahead renders empty with the ⤓ Import control that is the
    only thing that builds it — so there is no month the grid can advertise and
    the month view then refuse to show. The tiles used to be bounded by how far
    ahead the tracker would build, which meant a month the user could legitimately
    ask for was drawn as unavailable.
    """
    return [
        {
            "num": month,
            "name": _calendar.month_name[month],
            "tracked": distrakt_store.month_key(year, month) in tracked,
        }
        for month in range(1, 13)
    ]


@guard.get("/distrakt", AuthLevel.DISTRAKT_APPROVED)
async def distrakt(request: Request):
    """Hidden Discord-tracker page, reached through an easter egg rather than any
    link in the UI.

    WITH NO MONTH IN THE QUERY THIS IS A CHOOSER, and that is the whole point of
    it. Opening a month is expensive — it can pull a month of premieres, sweep
    recent viewing and write a roster's worth of rows — and the page used to
    inherit its month from whichever view the easter egg was triggered on, so
    which month paid that cost was an accident of where somebody happened to be.
    Naming a month is now a deliberate act, exactly as it is on the calendar's own
    landing page (see calendar.routes.home, whose picker shape this follows).
    Nothing here loads or builds a month: the chooser reads which months this user
    already has and renders a grid.

    WITH a month it renders the shell for the requested {year, month}; the page's
    JS then fetches the computed month via /api/distrakt/month (which lazily rolls
    the month over). Month-nav prev/next mirror the main calendar's nav."""
    today = clock.today()
    user = await auth.current_user(request)
    year = route_params.valid_year(request.query_params.get("year"), today.year)
    if not route_params.month_given(request.query_params.get("month")):
        tracked = set(await distrakt_store.list_months(user.user_id))
        return templates.TemplateResponse(request, "distrakt_pick.html", {
            "request": request,
            "year": year,
            "months": _chooser_months(year, tracked),
            "current_month": today.month if year == today.year else None,
            "today_month": today.month,
            "today_year": today.year,
            **chrome.page_context(user),
        })
    month = route_params.valid_month(request.query_params.get("month"), today.month)
    network_emojis, default_network_emoji = await distrakt_store.get_emoji_prefs(user.user_id)
    context = {
        "request": request,
        "year": year,
        "month": month,
        "nav": route_params.adjacent_months(year, month),
        # For the announcement post's "which calendar view does the embedded link
        # open on" selector; the same list the calendar's endpoint picker uses.
        "endpoints": endpoint_choices(),
        **chrome.page_context(user),
        # This user's OWN map — it renders into their Discord posts and nobody
        # else's. Rendered in rather than fetched because the roster rows fall
        # back to these emoji whenever a network has no logo.
        "network_emojis": network_emojis,
        "default_network_emoji": default_network_emoji,
    }
    return templates.TemplateResponse(request, "distrakt.html", context)


@guard.get("/api/distrakt/list", AuthLevel.DISTRAKT_APPROVED)
async def api_distrakt_list(request: Request):
    """Raw (unbucketed) shows stored for a month — the plain management list."""
    user_id = await _distrakt_user_id(request)
    today = clock.today()
    year = route_params.valid_year(request.query_params.get("year"), today.year)
    month = route_params.valid_month(request.query_params.get("month"), today.month)
    doc = await distrakt_store.load_month(user_id, distrakt_store.month_key(year, month))
    return JSONResponse({"ok": True, "month": distrakt_store.month_key(year, month), "shows": (doc or {}).get("shows", [])})


async def _abandon_in_month(user_id: int, month_key: str, show: dict) -> None:
    """Record `show` as given up on IN `month_key`, putting it on that month's
    roster first if it is not there yet, and mark the in-memory copy to match.

    The roster write is what makes this work for a title the user holds on some
    OTHER month. Their live lists are drawn from every month at once, so a row on
    the page can perfectly well be stored elsewhere — but giving up on it is a
    fact about THIS month ("I was following this and stopped, in August"), so
    that is where it has to land. Writing it into the month the row happens to be
    stored under would edit a month that has already settled.
    """
    form = discord_fmt.freeze_form(show)
    key, season = distrakt_store.record_key(show), int(show["season"])
    if await distrakt_store.set_abandoned(
            user_id, month_key, key, season, True, abandoned_form=form) is None:
        await distrakt_store.add_show(user_id, month_key, show)
        await distrakt_store.set_abandoned(
            user_id, month_key, key, season, True, abandoned_form=form)
    show["abandoned"] = True
    show["abandoned_form"] = form
    show["bucket"] = distrakt_store.Bucket.ABANDONED


async def _apply_not_watching(user_id: int, month_key: str, own: list[dict],
                              live: list[dict]) -> tuple[list[dict], list[dict]]:
    """This user's own main-calendar not-watching marks, split by WHEN each mark
    was made relative to the 1st of `month_key`.

      - marked BEFORE the month opened: the show never appears in this month, and
        can never be abandoned in it. That mark says "I never intended to watch
        this", so the month has no verdict to announce about it. The row is not
        deleted — un-marking brings it straight back.
      - marked ON OR AFTER the 1st: the show is promoted to Abandoned in this
        month, persisted with its form frozen. That mark says "I was following
        this and stopped", which is exactly what a month records. One-directional
        — never un-abandons; the tracker's own toggle and ✕ stay the source of
        truth, and the `abandoned` guard means a steady-state read does no extra
        writes.

    Reading the timestamp is what removed the need to notice the moment a month
    opens: a month built ahead of time exists for weeks before it begins, so
    measuring "later" against the month's EXISTENCE read every mark made during
    that wait as giving up on a show that had never started.

    `own` is the month's own roster and `live` is the user's own list (drawn from
    every month), and they are handled together because the second statement
    moves a row from one to the other: a title the user turns away part-way
    through the month stops being a live concern and becomes this month's record
    of having dropped it. Returns the two lists as they stand afterwards.
    """
    before = await calendar_state.not_watching_marked_before(
        user_id, distrakt_store.month_first_day(month_key))
    after = await calendar_state.not_watching_ids(user_id) - before

    def matched(s: dict, marks: set[str]) -> bool:
        return bool(marks) and distrakt_store.matches_not_watching(s, marks)

    kept_own = []
    for show in own:
        if matched(show, before):
            continue
        if not show.get("abandoned") and matched(show, after):
            await _abandon_in_month(user_id, month_key, show)
        kept_own.append(show)

    kept_live = []
    for show in live:
        if matched(show, before):
            continue
        if matched(show, after):
            await _abandon_in_month(user_id, month_key, show)
            kept_own.append(show)  # now one of the month's own verdicts
            continue
        kept_live.append(show)
    return kept_own, kept_live


def _empty_month_payload(month_key: str, emojis: dict, default_emoji: str,
                         standing: distrakt_store.MonthStanding,
                         readonly: bool = False, link_url: str | None = None,
                         empty_note: str = "") -> dict:
    """Headers-only render for a month with no roster + no Trakt call: an
    unconfigured/uninitialized month (readonly=False) or a never-tracked past
    month reached by navigating backward (readonly=True). The tracker only
    ever rolls a month's snapshot forward, never backfills one after the fact,
    so an old month nobody was tracking at the time stays permanently empty
    and read-only rather than retroactively populating from Trakt.

    `empty_note` replaces the page's generic "nothing here yet" line. An empty
    month is not self-explanatory — the same blank list means "waiting for you to
    ask", "the calendar isn't there yet" and "nobody was tracking that far back" —
    so when the reason is known it is said, in the sentence the state owns."""
    return {
        "ok": True, "month": month_key, "closed": False, "readonly": readonly, "shows": [],
        "movies": [],
        "empty_note": empty_note,
        "post1": discord_fmt.render_post1([], emojis, default_emoji, link_url=link_url, month=month_key),
        "post2": discord_fmt.render_post2([], emojis, default_emoji, standing=standing),
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


async def _stale_month_payload(user_id: int, month_key: str, emojis: dict, default_emoji: str,
                               link_url: str | None, rate_limited: bool,
                               standing: distrakt_store.MonthStanding) -> dict:
    """Render a month WITHOUT any Trakt call, from whatever is last persisted — the
    top-level fallback when a shared refresh prerequisite hit Trakt's rate limit or
    was unreachable. Stored records already carry each show's last-known
    watched/total/cadence/dates, so this projects them offline (frozen_shows) and
    re-buckets, attaching a visible notice so stale-but-real beats a false 0/0 or a
    500. `rate_limited` only chooses the notice wording; both cases degrade
    identically and return HTTP 200."""
    doc = await distrakt_store.load_month(user_id, month_key)
    roster = distrakt_store.frozen_shows(doc) if doc else []
    shows = discord_fmt.month_view(roster, standing, month_key)
    notice = (
        "Trakt is rate-limiting us right now — showing last-known totals. Refresh again in a moment."
        if rate_limited else
        "Couldn't reach Trakt just now — showing last-known totals. Try refreshing again shortly."
    )
    return {
        "ok": True,
        "month": month_key,
        "closed": bool(doc and doc.get("closed")),
        "readonly": False,
        "shows": shows,
        # The films watched that month travel WITH the month, not just inside the
        # POST 2 text: they were being recorded, counted and imported while never
        # appearing anywhere on the page, which reads as them not being there.
        "movies": (doc or {}).get("movies") or [],
        # POST 1 gets the WHOLE roster, not the standing-filtered view: it
        # announces what premiered in the month, and that is true of a month in
        # any standing. See discord_fmt.render_post1.
        "post1": discord_fmt.render_post1(roster, emojis, default_emoji, link_url=link_url, month=month_key),
        "post2": discord_fmt.render_post2(shows, emojis, default_emoji,
                                          movies=(doc or {}).get("movies"), standing=standing),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        # Reports the ACTUAL cause: True only when Trakt rate-limited us, False when
        # it was simply unreachable. The client shows the banner off `notice`
        # (present only on this degraded payload), not off this flag, so both cases
        # still surface — this stays accurate metadata rather than a banner trigger.
        "rate_limited": rate_limited,
        "notice": notice,
    }


def _closed_month_payload(doc: dict, month_key: str, emojis: dict, default_emoji: str,
                          link_url: str | None,
                          standing: distrakt_store.MonthStanding) -> dict:
    """A frozen past month, rendered straight from its own snapshot with NO Trakt
    calls. The snapshot is the record of what that month WAS — recomputing it
    against today's watch history would rewrite history every time it was opened."""
    roster = distrakt_store.frozen_shows(doc)
    shows = discord_fmt.month_view(roster, standing, month_key)
    return {
        "ok": True, "month": month_key, "closed": True, "readonly": False, "shows": shows,
        "movies": doc.get("movies") or [],
        # The whole roster, not the standing-filtered view: a closed month's
        # announcement is still every premiere it had, whatever each one has
        # since become. See discord_fmt.render_post1.
        "post1": discord_fmt.render_post1(roster, emojis, default_emoji, link_url=link_url, month=month_key),
        "post2": discord_fmt.render_post2(shows, emojis, default_emoji,
                                          movies=doc.get("movies"), standing=standing),
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


async def _sync_watch_history(settings, user_id: int, records: list[dict],
                              month_key: str, force_fresh: bool,
                              today: date) -> tuple[dict, dict, list[dict]]:
    """Bring this user's incremental watch-history cache up to date ONCE, and take
    the three answers the month needs out of it: how much of each season they have
    watched, when each season was finished, and which films they watched in the
    month.

    One sync for all three because they all read the same history: doing it per
    answer would re-baseline the whole history three times. `force_fresh` is a full
    re-baseline and belongs ONLY to an explicit Refresh — a routine load relies on
    the /sync/last_activities gate plus deltas.
    """
    with span("payload.watch_history_sync", roster=len(records), force=force_fresh) as sp:
        state = await watch_history.sync_and_baseline(
            settings, user_id, records, force=force_fresh, today=today,
        )
        watched_lookup = watch_history.watched_map(state)
        # When each season was finished, for the "Completed means completed THIS
        # month" rule compute_live_shows applies.
        completed_lookup = watch_history.season_completed_map(state)
        mstart, mend = watch_history.month_bounds(month_key)
        movies = watch_history.movies_in_range(state, mstart, mend)
        sp.set(watched_keys=len(watched_lookup), movies=len(movies))
    return watched_lookup, completed_lookup, movies


# The two buckets that describe WORK IN HAND rather than something a month
# settled: what has finished airing and is waiting to be caught up on, and what
# is still airing and being kept up with. They are the user's own lists, which is
# why they are the only ones a title stored on some other month contributes to —
# a season that premiered in June, or that June recorded as finished, is June's
# business, and only the fact that it is still on the pile is today's.
_WORK_IN_HAND = (distrakt_store.Bucket.CLEANUP, distrakt_store.Bucket.KEEPUP)


def _work_in_hand(shows: list[dict]) -> list[dict]:
    """The rows of `shows` that belong to the user's own lists."""
    return [s for s in shows if s.get("bucket") in _WORK_IN_HAND]


async def _mark_returns(user_id: int, shows: list[dict]) -> None:
    """Flag each row that is back on the pile after having been finished.

    A season completed at 16 of 16 stops being complete the day the provider
    learns of an 18th, and the live counts put it back on the user's lists by
    themselves — no detection, no manual step. That is the behaviour that was
    wanted, but arriving unannounced it reads as a bug: the user remembers
    finishing it. The months already hold the record that answers it, so this
    asks them rather than inferring anything.
    """
    finished_before = await distrakt_store.completed_seasons(user_id)
    for show in shows:
        show["returned"] = (show.get("bucket") in _WORK_IN_HAND
                            and distrakt_store.live_key(show) in finished_before)


async def _live_month_payload(user_id: int, doc: dict, month_key: str, settings,
                              emojis: dict, default_emoji: str, link_url: str | None,
                              force_fresh: bool, today: date) -> tuple[dict, int]:
    """Compute an OPEN month against Trakt: refresh the roster from premieres,
    read the watch history, work out each show's live x/y and bucket, and render
    the two Discord posts.

    Two different kinds of fact come out of this. The MONTH's — what premiered in
    it, what was finished or given up on in it, what films were watched in it —
    come from its own roster. The USER's — what they are behind on and what they
    are keeping up with — are true of no particular month and are read across all
    of them, and only the month in progress shows them.

    Everything here may reach Trakt, and the caller is what turns a failure into
    the stale-but-real fallback — this function does not degrade, so that decision
    lives in exactly one place.
    """
    committed = distrakt_store.month_committed(month_key, today)
    standing = distrakt_store.month_standing(month_key, today)
    # A PREVIEW month (before the 1st) keeps auto-populating from premieres so the
    # roster tracks the calendar (and un-not-watching re-adds a previously excluded
    # premiere). A COMMITTED month is stable — premieres only re-import on demand.
    if not committed and settings.trakt_configured:
        await distrakt_store.import_premieres(user_id, month_key, settings)
        doc = await distrakt_store.load_month(user_id, month_key) or doc

    records = doc.get("shows", [])
    if records and not settings.trakt_configured:
        return {"ok": False, "error": "Not configured"}, 400

    # WHAT THE USER IS BEHIND ON AND WHAT THEY ARE KEEPING UP WITH ARE FACTS ABOUT
    # THE USER, not about a month, so they are read across every month at once
    # rather than from this one's roster. Only the month actually in progress
    # shows them: a month that is over settled its own verdicts and a month that
    # has not begun has no work in hand to report. The rows this adds are the ones
    # the viewed month does not already hold — a season on both is the month's own
    # copy, which carries this month's counts and its provenance.
    elsewhere: list[dict] = []
    if standing is distrakt_store.MonthStanding.CURRENT and settings.trakt_configured:
        held = {distrakt_store.live_key(r) for r in records}
        elsewhere = [r for r in await distrakt_store.user_roster(user_id)
                     if distrakt_store.live_key(r) not in held]
    # One pass over both, because they share every provider read: the watch-history
    # sync baselines against the whole set, and splitting them would fetch each
    # season twice and baseline the history twice for no gain.
    everything = records + elsewhere

    # Two INDEPENDENT freshness knobs (they were wrongly coupled, which made every
    # stale load re-baseline the whole watch history):
    #   season_fresh -> bypass the 24h season cache for `y`. Only on explicit
    #                   Refresh; routine loads let the 24h TTL refresh `y` daily.
    #   force        -> full watch-history re-baseline. ONLY on explicit Refresh;
    #                   normal loads rely on the last_activities gate + deltas.
    season_fresh = force_fresh

    watched_lookup: dict = {}
    completed_lookup: dict = {}
    movies: list[dict] = []
    if settings.trakt_configured:
        watched_lookup, completed_lookup, movies = await _sync_watch_history(
            settings, user_id, everything, month_key, force_fresh, today)

    with span("payload.compute_live_shows", n=len(everything), fresh=season_fresh):
        # allow_degrade: a per-show season 429 marks THAT show unavailable and
        # renders the rest, instead of failing the whole roster.
        computed = await distrakt_store.compute_live_shows(
            user_id, everything, settings, fresh=season_fresh, watched_lookup=watched_lookup,
            allow_degrade=True, completed_lookup=completed_lookup) if everything else []
    # compute_live_shows answers in the order it was asked, so the split is where
    # the two inputs were joined.
    shows, live_rows = computed[:len(records)], computed[len(records):]
    # THE USER'S OWN LIST IS CLEANUP AND KEEPUP AND NOTHING ELSE, AND IT IS CUT
    # DOWN TO THOSE HERE — before anything can read from it or write off it.
    # This is the only position that works. A row taken from another month
    # carries no bucket until the live pass above has resolved its watched count
    # and its air dates, so the question cannot be asked any earlier; and the very
    # next step, _apply_not_watching, WRITES — a calendar turn-away on a row it is
    # handed materialises that row onto the month being viewed and records it as
    # given up on there.
    # What that cost: a title that has not started airing buckets as New or
    # Returning, so premieres imported into a month still ahead sat on this list,
    # were turned away, and were abandoned against the month under way. The
    # standing filter below then hid them from the page — long after they had been
    # written. New and Returning for the viewed month come from its OWN roster;
    # they must never arrive by this route.
    live_rows = _work_in_hand(live_rows)
    shows, live_rows = await _apply_not_watching(user_id, month_key, shows, live_rows)
    # A season finished before this month began belongs to the month it was
    # finished in, not to this one — see drop_seasons_finished_earlier.
    roster = await distrakt_store.drop_seasons_finished_earlier(user_id, month_key, shows)
    # What this month is allowed to present, decided once for the page's row list
    # and POST 2 — see discord_fmt.MONTH_BUCKETS. Applied after the roster
    # bookkeeping above, so a row that is merely not shown here is still stored,
    # still refreshed, and still there when the calendar reaches its month.
    # POST 1 keeps the unfiltered `roster`: it announces the month's premieres,
    # which no standing changes.
    # `live_rows` needs no filtering of its own: it was reduced to the user's own
    # two buckets before anything acted on it, above.
    shows = discord_fmt.month_view(roster, standing, month_key) + live_rows
    await _mark_returns(user_id, shows)
    if records and season_fresh:
        await distrakt_store.stamp_refreshed(user_id, month_key)

    # Pre-warm the network-logo cache for the whole roster, so a show manually
    # added before logos existed doesn't depend on some OTHER show requesting its
    # network's logo first (see logos.ensure_logos). Best-effort and
    # self-limiting: a no-op once each network's tile is on disk.
    # The user's own list is warmed with the month's: those rows draw a logo too,
    # and a title stored on some other month has no reason to be the one that
    # renders a placeholder.
    if settings.trakt_configured and (roster or live_rows):
        with span("payload.ensure_logos"):
            await logos.ensure_logos(settings, [
                (s.get("network"), (s.get("ids") or {}).get("tmdb"))
                for s in (*roster, *live_rows)
            ])

    with span("payload.render"):
        post1 = discord_fmt.render_post1(roster, emojis, default_emoji, link_url=link_url, month=month_key)
        post2 = discord_fmt.render_post2(shows, emojis, default_emoji, movies=movies,
                                         standing=standing)
    return {
        "ok": True,
        "month": month_key,
        "closed": False,
        "readonly": False,
        "shows": shows,
        "movies": movies,
        "post1": post1,
        "post2": post2,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }, 200


async def _distrakt_month_payload(user_id: int, year: int, month: int, settings,
                                  force_fresh: bool = False) -> tuple[dict, int]:
    """Shared body for GET /api/distrakt/month + POST /api/distrakt/refresh, for
    ONE user's tracker.

    Lazily rolls the month over (ensure_month), then either renders a CLOSED
    month from its frozen snapshot (no Trakt) or computes the OPEN month live
    (or always when force_fresh). A never-tracked PAST/gap month (backward nav)
    is rendered empty + read-only and never created; a month that has not BEGUN is
    rendered empty and not created either, because building that one is something
    the user asks for with ⤓ Import — see the branch below. Returns
    (json_payload, http_status).

    This is also where Trakt failing is turned into an answer. A 429 on a SHARED
    prerequisite (anything but the per-show season fan-out, which degrades itself)
    can't be attributed to one show, so rather than a false 0/0 or a 500 the whole
    month falls back to its last-known stored totals plus a notice at HTTP 200 —
    the user refreshes again once the window clears. A plain reachability failure
    degrades the same way; only the notice wording differs.
    """
    today = clock.today()
    month_key = distrakt_store.month_key(year, month)
    # Where this month stands relative to the calendar decides which sections it
    # may show at all (discord_fmt.MONTH_BUCKETS), so every shape below is handed
    # the same answer rather than working one out for itself.
    standing = distrakt_store.month_standing(month_key, today)
    link_url = await _distrakt_post_link(user_id, settings, year, month)
    # This user's own map, fetched once and handed to every render below. It is
    # not on `settings` any more — see _distrakt_settings.
    emojis, default_emoji = await distrakt_store.get_emoji_prefs(user_id)
    existing = await distrakt_store.load_month(user_id, month_key)
    if existing is None:
        blocked = await distrakt_store.is_backfill_blocked(user_id, month_key, today)
        if blocked or not settings.trakt_configured:
            # Backward/gap past month (blocked) OR no Trakt yet: empty, NOT
            # persisted, no Trakt call. `readonly` hides the add/edit affordances.
            return _empty_month_payload(
                month_key, emojis, default_emoji, standing,
                readonly=blocked, link_url=link_url,
            ), 200
        if standing is distrakt_store.MonthStanding.FUTURE:
            # A MONTH THAT HAS NOT BEGUN IS NOT BUILT BY BEING LOOKED AT. Building
            # one is the expensive act — a month of premieres read, a roster's
            # worth of rows written — and out here it is also a guess: nothing has
            # aired, so there is no state to catch up with. Opening it therefore
            # shows an empty month that says what it is waiting for, and ⤓ Import
            # is what builds it (api_distrakt_import). The month UNDER WAY is the
            # opposite case and still fills itself in on sight: it is the month
            # being asked about, and an empty one there would be wrong rather than
            # merely early. Not persisted and no Trakt call either way.
            return _empty_month_payload(
                month_key, emojis, default_emoji, standing, link_url=link_url,
                empty_note=MONTH_AWAITS_IMPORT,
            ), 200

    try:
        with span("payload.ensure_month", month=month_key, force=force_fresh):
            doc = await distrakt_store.ensure_month(user_id, year, month, settings, today=today)
        month_key = doc["month"]
        if doc.get("closed"):
            return _closed_month_payload(doc, month_key, emojis, default_emoji,
                                         link_url, standing), 200
        return await _live_month_payload(
            user_id, doc, month_key, settings, emojis, default_emoji, link_url,
            force_fresh, today)
    except TraktRateLimitError as exc:
        logger.warning("distrakt month %s degraded to stale (Trakt rate-limited): %s", month_key, exc)
        return await _stale_month_payload(user_id, month_key, emojis, default_emoji, link_url,
                                          rate_limited=True, standing=standing), 200
    except TraktError as exc:
        logger.warning("distrakt month %s degraded to stale (Trakt unreachable): %s", month_key, exc)
        return await _stale_month_payload(user_id, month_key, emojis, default_emoji, link_url,
                                          rate_limited=False, standing=standing), 200


@guard.get("/api/distrakt/month", AuthLevel.DISTRAKT_APPROVED)
async def api_distrakt_month(request: Request):
    """Computed buckets + the two copy-paste POST 1/POST 2 markdown blocks.

    OPEN month: live x/y + cadence/dates recomputed (1x /sync/watched/shows + 1x
    season call per show), auto-refreshed if totals are stale >24h. CLOSED /
    past month: rendered from the frozen snapshot with NO Trakt calls. Opening an
    uninitialized month lazily rolls it over first (see ensure_month)."""
    user_id = await _distrakt_user_id(request)
    today = clock.today()
    year = route_params.valid_year(request.query_params.get("year"), today.year)
    month = route_params.valid_month(request.query_params.get("month"), today.month)
    with span("GET /api/distrakt/month", ym=f"{year}-{month:02d}"):
        payload, status = await _distrakt_month_payload(user_id, year, month, await _distrakt_settings(user_id))
    return JSONResponse(payload, status_code=status)


@guard.post("/api/distrakt/refresh", AuthLevel.DISTRAKT_APPROVED)
async def api_distrakt_refresh(request: Request):
    """Force a fresh totals refresh: bypass the 24h season cache + re-stamp
    totals_refreshed_at for the OPEN month, then return the same shape as GET
    /api/distrakt/month. CLOSED months are frozen (nothing to refresh)."""
    user_id = await _distrakt_user_id(request)
    data = await authz.json_body(request)
    today = clock.today()
    year = route_params.valid_year(data.get("year"), today.year)
    month = route_params.valid_month(data.get("month"), today.month)
    payload, status = await _distrakt_month_payload(user_id, year, month, await _distrakt_settings(user_id),
                                                    force_fresh=True)
    return JSONResponse(payload, status_code=status)


@guard.get("/api/distrakt/months", AuthLevel.DISTRAKT_APPROVED)
async def api_distrakt_months(request: Request):
    """This user's tracked YYYY-MM months for the history nav, plus the real
    current month (always navigable even before it has been initialized)."""
    user_id = await _distrakt_user_id(request)
    today = clock.today()
    current = distrakt_store.month_key(today.year, today.month)
    months = sorted(set(await distrakt_store.list_months(user_id)) | {current})
    return JSONResponse({"ok": True, "months": months, "current": current})


@guard.post("/api/distrakt/import", AuthLevel.DISTRAKT_APPROVED)
async def api_distrakt_import(request: Request):
    """Pull this month's calendar premieres into the OPEN month (shows/new -> New,
    shows/premieres minus new -> Returning; skips existing + not-watching). The
    manual "Import from calendar" action — e.g. to seed the current month when its
    doc already exists (so lazy-init's one-shot premiere seeding was skipped).
    Returns the same shape as GET /api/distrakt/month.

    THIS IS ALSO HOW A MONTH THAT HAS NOT BEGUN GETS BUILT AT ALL. Opening one no
    longer builds it (see _distrakt_month_payload), so for the month ahead this
    button is the ask: it creates the month, which takes that month's premieres
    and nothing carried in from before it (see rollover._initialize_month).

    ANY month ahead, however far, and it does not matter whether the months
    between have been built: this is somebody pressing a button on the month they
    are looking at, which is the deliberate act a bound on how far ahead it may
    point was standing in for. What it gathers is only what this instance already
    holds for that month's calendar, so a month nothing is known about yet builds
    empty rather than expensively. A PAST month never tracked is the one refusal
    left — see rollover.can_initialize for why that one stays."""
    user_id = await _distrakt_user_id(request)
    settings = await _distrakt_settings(user_id)
    if not settings.trakt_configured:
        return JSONResponse({"ok": False, "error": "Not configured"}, status_code=400)
    data = await authz.json_body(request)
    today = clock.today()
    year = route_params.valid_year(data.get("year"), today.year)
    month = route_params.valid_month(data.get("month"), today.month)
    month_key = distrakt_store.month_key(year, month)
    if await distrakt_store.is_backfill_blocked(user_id, month_key, today):
        return JSONResponse({"ok": False, "error": "Can't import into a past month that was never tracked."}, status_code=400)
    doc = await distrakt_store.ensure_month(user_id, year, month, settings, today=today)
    if doc.get("closed"):
        return JSONResponse({"ok": False, "error": "Past month is frozen (read-only)."}, status_code=400)
    doc = await distrakt_store.import_premieres(user_id, month_key, settings)
    await _register_networks(user_id, [s.get("network") for s in (doc or {}).get("shows", [])])
    payload, status = await _distrakt_month_payload(user_id, year, month, settings)
    return JSONResponse(payload, status_code=status)


@guard.post("/api/distrakt/backfill-networks", AuthLevel.DISTRAKT_APPROVED)
async def api_distrakt_backfill_networks(request: Request):
    """Register every network used by this month's roster into the emoji map
    (with the default emoji) so they all show up in the editor. Returns the map."""
    user_id = await _distrakt_user_id(request)
    data = await authz.json_body(request)
    today = clock.today()
    year = route_params.valid_year(data.get("year"), today.year)
    month = route_params.valid_month(data.get("month"), today.month)
    doc = await distrakt_store.load_month(user_id, distrakt_store.month_key(year, month))
    emojis = await _register_networks(
        user_id, [s.get("network") for s in (doc or {}).get("shows", [])],
    )
    _, default_emoji = await distrakt_store.get_emoji_prefs(user_id)
    return JSONResponse({
        "ok": True,
        "network_emojis": emojis,
        "default_network_emoji": default_emoji,
    })


@guard.get("/api/distrakt/details", AuthLevel.DISTRAKT_APPROVED)
async def api_distrakt_details(request: Request):
    """The calendar's detail payload for one roster show, plus what THIS user has
    watched of that season.

    A separate route from /api/details rather than a parameter on it: that one is
    CALENDAR_APPROVED, and a distrakt-approved account need not be calendar
    approved. It also answers a different question — which episodes this
    particular person has seen — that the calendar has no business knowing.

    THE SOURCE ID AND THE SLUG BOTH COME FROM THE USER'S OWN ROSTER ROW, not from
    the query string: the caller names a row it can already see, and everything
    this looks up follows from that row. So the Trakt links it builds — and the
    title it fetches — cannot be pointed somewhere else by the caller.
    """
    user_id = await _distrakt_user_id(request)
    settings = await _distrakt_settings(user_id)
    if not settings.trakt_configured:
        return JSONResponse({"ok": False, "error": "Not configured"}, status_code=400)
    season = route_params.season(request.query_params.get("season"))
    try:
        key = parse_item_key(request.query_params.get("key"))
    except ValueError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    if season is None:
        return JSONResponse({"ok": False, "error": "Missing season"}, status_code=400)

    row = await db.fetch_one(
        "SELECT slug, trakt_id FROM distrakt_shows "
        "WHERE user_id = ? AND media = ? AND match_source = ? AND match_id = ? LIMIT 1",
        (user_id, key.media, key.match_source, key.match_id),
    )
    if row is None or row["trakt_id"] is None:
        return JSONResponse({"ok": False, "error": "Not on your roster"}, status_code=404)
    try:
        details = await fetch_details(settings, Media.SHOW, row["trakt_id"], season)
    except TraktError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=exc.status or 502)
    progress = await db.fetch_one(
        "SELECT watched_episodes_json FROM distrakt_show_progress WHERE user_id = ? "
        "AND media = ? AND match_source = ? AND match_id = ? AND season = ?",
        (user_id, key.media, key.match_source, key.match_id, season),
    )
    # watch_history owns what that column holds — {episode: watched_at} now, a
    # bare list of numbers before dates were stored — so the shape is read there
    # rather than guessed at again here. Guessing at it here is exactly how this
    # route came to answer "nothing watched" for everyone: it read the dated
    # mapping as a list and dropped every entry.
    watched: list[int] = []
    if progress is not None:
        try:
            stored = json.loads(progress["watched_episodes_json"] or "{}")
            watched = sorted(int(ep) for ep in watch_history.episode_watches(stored))
        except (TypeError, ValueError):
            watched = []
    return JSONResponse({
        "ok": True,
        **details,
        "slug": row["slug"] or "",
        "season": season,
        "watched_episodes": watched,
    })


@guard.get("/api/distrakt/emojis", AuthLevel.DISTRAKT_APPROVED)
async def api_distrakt_emojis(request: Request):
    """This user's network->emoji map."""
    user_id = await _distrakt_user_id(request)
    emojis, default_emoji = await distrakt_store.get_emoji_prefs(user_id)
    return JSONResponse({
        "ok": True, "network_emojis": emojis, "default_network_emoji": default_emoji,
    })


@guard.post("/api/distrakt/emojis", AuthLevel.DISTRAKT_APPROVED)
async def api_distrakt_set_emojis(request: Request):
    """Replace this user's network->emoji map.

    DISTRAKT_APPROVED rather than ADMIN: the map is this account's own now, and
    it decides how this account's Discord posts render. It used to be saved
    through the admin-only settings endpoint, which is why only an administrator
    could edit what every user's posts looked like.
    """
    user_id = await _distrakt_user_id(request)
    data = await authz.json_body(request)
    emojis = data.get("network_emojis")
    if not isinstance(emojis, dict):
        return JSONResponse(
            {"ok": False, "error": "network_emojis must be an object"}, status_code=400,
        )
    default_emoji = str(data.get("default_network_emoji") or "").strip() or distrakt_store.DEFAULT_EMOJI
    clean = {
        str(k).strip(): str(v).strip()
        for k, v in emojis.items() if str(k).strip()
    }
    await distrakt_store.set_emoji_prefs(user_id, clean, default_emoji)
    return JSONResponse({
        "ok": True, "network_emojis": clean, "default_network_emoji": default_emoji,
    })


async def _calendar_item_a_row_speaks_for(user_id: int, month_key: str, doc: dict | None,
                                          record: dict | None, key: ItemKey, season: int,
                                          settings) -> str | None:
    """The calendar item this tracker row is allowed to speak for, or None when
    acting on the row must say nothing to the calendar at all.

    ONE answer for both of the controls that end a title's run on a month — the
    ✕ and Abandon. They ask the identical question ("may this touch the
    calendar, and under which id?"), and a second copy of the rule beside the
    second control would drift out of step with the first in silence: the copies
    are only ever read one at a time, so nothing would ever put them side by
    side and notice.

    ONLY A ROW THE CALENDAR PUT ON THE MONTH. For such a row the mark is what
    makes the action stick at all: a month before its 1st re-imports its
    premieres on every load, and the not-watching set is the only thing
    import_premieres skips, so without it the row is handed straight back in the
    same response and the control looks broken. A row the user added by hand, or
    one their watch history produced, is neither re-imported nor necessarily on
    the calendar in the first place — hiding a show there because somebody undid
    a manual add would take away something they never said they were not
    watching. A row written before provenance was recorded has no stored answer,
    so the calendar is asked directly whether it would hand that show back (see
    is_calendar_premiere).

    A CLOSED MONTH NEVER SPEAKS OUTWARD, whatever the row says. Correcting what
    a past month records is a statement about that month and nothing else — a
    season you finished years ago and re-watched one episode of does not belong
    on March's list, but it is also not something to start hiding from your
    calendar today.

    The id is the slug, falling back to the source's own id, exactly as the
    calendar keys its own cards. That is NOT the id the tracker row is filed
    under, and a mark written in the tracker's terms would silently match
    nothing.
    """
    if record is None or (doc or {}).get("closed"):
        return None
    added_by = str(record.get("added_by") or "")
    from_calendar = (added_by == distrakt_store.ADDED_BY_CALENDAR if added_by
                     else await distrakt_store.is_calendar_premiere(
                         user_id, month_key, settings, key, season))
    if not from_calendar:
        return None
    ids = record.get("ids") or {}
    return str(ids.get("slug") or ids.get("trakt") or key.match_id)


@guard.post("/api/distrakt/remove", AuthLevel.DISTRAKT_APPROVED)
async def api_distrakt_remove(request: Request):
    """Delete a show+season from a month (cleanup mistakes / abandons), and for a
    row the calendar put there, mark the show not-watching on the calendar as
    well — which rows those are, and under what id, is
    _calendar_item_a_row_speaks_for's to say.

    That mark is not a bonus, it is what makes such a removal STICK: without it
    a preview month's next load re-imports the premiere and the ✕ looks broken.
    The row goes either way.

    A row the viewed month does not hold is one of the user's own lists, drawn
    from every month at once, and removing it takes the season off all of them.
    See the branch below for why anything narrower does not stick.
    """
    user_id = await _distrakt_user_id(request)
    data = await authz.json_body(request)
    today = clock.today()
    year = route_params.valid_year(data.get("year"), today.year)
    month = route_params.valid_month(data.get("month"), today.month)
    try:
        key, season = _row_target(data)
    except RequestError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    month_key = distrakt_store.month_key(year, month)
    doc = await distrakt_store.load_month(user_id, month_key)
    # Read before removing: both the provenance and the id the mark is written
    # under (slug, falling back to the source's own id, exactly as the calendar
    # keys its own items) live on the record that is about to go.
    record = next((s for s in ((doc or {}).get("shows") or [])
                   if s["key"] == str(key) and int(s["season"]) == season), None)
    if record is not None:
        removed = await distrakt_store.remove_show(user_id, month_key, key, season)
    else:
        # The row is on the page because the user's own lists are drawn from every
        # month at once, so it can be stored somewhere else entirely. ✕ there means
        # the season should not be on their list AT ALL, and taking it off only the
        # month being viewed would leave the copy that produced it — the row would
        # come straight back on the next load with the ✕ looking broken.
        record = await distrakt_store.find_user_row(user_id, key, season)
        removed = bool(await distrakt_store.remove_show_everywhere(user_id, key, season))
    if not removed:
        return JSONResponse({"ok": False, "error": "Show/season not found in that month"}, status_code=404)

    settings = await _distrakt_settings(user_id)
    item_id = await _calendar_item_a_row_speaks_for(
        user_id, month_key, doc, record, key, season, settings)
    if item_id is not None:
        await calendar_state.set_not_watching(user_id, item_id, True)
    payload, status = await _distrakt_month_payload(user_id, year, month, settings)  # recomputed month (1d)
    # So the toast can say what actually happened rather than guessing.
    if isinstance(payload, dict):
        payload["hidden_on_calendar"] = item_id is not None
    return JSONResponse(payload, status_code=status)


@guard.get("/api/distrakt/search", AuthLevel.DISTRAKT_APPROVED)
async def api_distrakt_search(request: Request):
    settings = await _distrakt_settings(await _distrakt_user_id(request))
    if not settings.trakt_configured:
        return JSONResponse({"ok": False, "error": "Not configured"}, status_code=400)
    q = request.query_params.get("q", "")
    try:
        results = await search_shows(settings, q)
    except TraktError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=exc.status or 502)
    return JSONResponse({"ok": True, "results": results})


@guard.get("/api/distrakt/search-movie", AuthLevel.DISTRAKT_APPROVED)
async def api_distrakt_search_movie(request: Request):
    """Film search for the add-a-film flow. Its own route rather than a media
    flag on the show search, because what comes back is a different shape with
    no seasons in it and the caller does something else entirely with it."""
    settings = await _distrakt_settings(await _distrakt_user_id(request))
    if not settings.trakt_configured:
        return JSONResponse({"ok": False, "error": "Not configured"}, status_code=400)
    q = request.query_params.get("q", "")
    try:
        found = await search_titles(settings, Media.MOVIE, q)
    except TraktError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=exc.status or 502)
    results = [
        {"ids": entry["ids"], "title": entry["title"],
         "year": entry["year"], "runtime": entry.get("runtime")}
        for entry in found if (entry.get("ids") or {}).get("trakt")
    ]
    return JSONResponse({"ok": True, "results": results})


@guard.post("/api/distrakt/add-movie", AuthLevel.DISTRAKT_APPROVED)
async def api_distrakt_add_movie(request: Request):
    """Record a film as watched on a day, by hand.

    Films are not a roster the way shows are — there is nothing to bucket, no
    progress to follow and no season to finish. One is simply a play that
    happened on a date, so this writes the same watch-history record the sweep
    writes and nothing else. The month it shows up in follows from the date,
    exactly as it does for a film Trakt reported itself.

    The date is required rather than defaulted to today: a film added while
    looking at March is a film watched in March, and quietly stamping it with
    today's date would file it under a month the user is not even looking at.
    """
    user_id = await _distrakt_user_id(request)
    data = await authz.json_body(request)
    ids = _client_ids(data.get("ids"))
    if not ids:
        return JSONResponse({"ok": False, "error": "Missing or invalid film ids"}, status_code=400)
    day = str(data.get("watched_on") or "").strip()
    try:
        watched_on = date.fromisoformat(day)
    except ValueError:
        return JSONResponse({"ok": False, "error": "Pick the day you watched it."}, status_code=400)
    if watched_on > clock.today():
        return JSONResponse({"ok": False, "error": "That day hasn't happened yet."}, status_code=400)

    recorded = await watch_history.record_movie_watches(user_id, [{
        "ids": ids,
        "title": data.get("title") or "",
        "year": data.get("year"),
        "watched_at": f"{watched_on.isoformat()}T12:00:00Z",
    }])
    if not recorded:
        # The film named no shared id, so there is no identity to file the play
        # under — see app/providers/base.py's MATCH_SOURCES.
        return JSONResponse(
            {"ok": False, "error": "That film has no id the tracker can file it under."},
            status_code=400)
    await _resnapshot_if_closed(user_id, distrakt_store.month_key(watched_on.year, watched_on.month))

    today = clock.today()
    year = route_params.valid_year(data.get("year_view"), today.year)
    month = route_params.valid_month(data.get("month_view"), today.month)
    payload, status = await _distrakt_month_payload(
        user_id, year, month, await _distrakt_settings(user_id))
    return JSONResponse(payload, status_code=status)


async def _resnapshot_if_closed(user_id: int, month_key: str) -> None:
    """Rebuild a CLOSED month's stored film list from watch history.

    A closed month renders its films from its own snapshot and is never
    recomputed, so a film added to or removed from that month has to be written
    into the snapshot or it simply will not appear there. An OPEN month recomputes
    its films on every load and needs nothing.
    """
    doc = await distrakt_store.load_month(user_id, month_key) if month_key else None
    if doc is None or not doc.get("closed"):
        return
    state = await watch_history.load_state(user_id)
    mstart, mend = watch_history.month_bounds(month_key)
    await distrakt_store.set_month_movies(
        user_id, month_key, watch_history.movies_in_range(state, mstart, mend))


@guard.post("/api/distrakt/remove-movie", AuthLevel.DISTRAKT_APPROVED)
async def api_distrakt_remove_movie(request: Request):
    """Forget a watched film — from a past month as readily as the current one.

    Trakt's history is not always right about what was watched, and a film it
    reports wrongly has nowhere else to be corrected: unlike a show there is no
    roster row to take off, only the watch record itself. So this removes that,
    and re-snapshots whichever CLOSED month had been carrying it.

    A film is held once per id with its latest play, so this forgets the watch
    rather than one month's share of it. It does not remember the decision: a
    later backfill sweep over the same range will see Trakt still reporting the
    film and offer it back — in a plan that has to be confirmed, not silently.
    """
    user_id = await _distrakt_user_id(request)
    data = await authz.json_body(request)
    try:
        key = parse_item_key(data.get("key"))
    except ValueError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)

    watched_at = await watch_history.forget_movie_watch(user_id, key)
    if watched_at is None:
        return JSONResponse({"ok": False, "error": "No such film on record."}, status_code=404)

    # The month it was filed under is the one whose snapshot has to be rebuilt —
    # not necessarily the month being looked at.
    await _resnapshot_if_closed(user_id, watched_at[:7])

    today = clock.today()
    year = route_params.valid_year(data.get("year"), today.year)
    month = route_params.valid_month(data.get("month"), today.month)
    payload, status = await _distrakt_month_payload(
        user_id, year, month, await _distrakt_settings(user_id))
    return JSONResponse(payload, status_code=status)


@guard.get("/api/distrakt/seasons", AuthLevel.DISTRAKT_APPROVED)
async def api_distrakt_seasons(request: Request):
    """Aired seasons for a show (add-flow season picker) — required so the
    browser can call fetch_show_seasons()."""
    settings = await _distrakt_settings(await _distrakt_user_id(request))
    if not settings.trakt_configured:
        return JSONResponse({"ok": False, "error": "Not configured"}, status_code=400)
    trakt_id = request.query_params.get("id")
    if not trakt_id:
        return JSONResponse({"ok": False, "error": "Missing id"}, status_code=400)
    try:
        seasons = await fetch_show_seasons(settings, trakt_id)
    except TraktError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=exc.status or 502)
    return JSONResponse({"ok": True, "seasons": seasons})


async def _register_networks(user_id: int, networks) -> dict:
    """Auto-populate THIS user's network->emoji map: any network not yet mapped
    gets the default emoji as a placeholder so it appears in their editor, ready
    to customize. No-op for blank / already-mapped networks.

    Per-user because it used to write settings.json, so importing a roster on any
    tracker account registered its networks into the operator's map.
    """
    return await distrakt_store.register_networks(user_id, networks)


@guard.post("/api/distrakt/add", AuthLevel.DISTRAKT_APPROVED)
async def api_distrakt_add(request: Request):
    """Persist a show+season into this user's {year,month} roster (identity),
    baseline their watch history, and register its network in the emoji map."""
    user_id = await _distrakt_user_id(request)
    settings = await _distrakt_settings(user_id)
    if not settings.trakt_configured:
        return JSONResponse({"ok": False, "error": "Not configured"}, status_code=400)
    data = await authz.json_body(request)
    today = clock.today()
    year = route_params.valid_year(data.get("year"), today.year)
    month = route_params.valid_month(data.get("month"), today.month)
    try:
        show = {
            "media": Media.SHOW,
            "ids": _client_ids(data.get("ids")),
            "season": int(data["season"]),
            "title": data.get("title") or "",
            "network": data.get("network") or "",
            # Added by hand, so removing it later says nothing about the
            # calendar — see api_distrakt_remove.
            "added_by": distrakt_store.ADDED_BY_MANUAL,
        }
        # Resolved before anything is written, so a title with no shared id is
        # refused here rather than at the insert, where the answer would be a 500.
        key = distrakt_store.record_key(show)
    except distrakt_store.UnkeyableRecord as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    except (KeyError, TypeError, ValueError):
        return JSONResponse({"ok": False, "error": "Missing or invalid ids/season"}, status_code=400)
    month_key = distrakt_store.month_key(year, month)
    if await distrakt_store.is_backfill_blocked(user_id, month_key, today):
        # No backfill: refuse to create a never-tracked PAST month even via a
        # manual add, consistent with the read path's read-only rendering of such
        # months. api_distrakt_add_completed is the way to state one outright.
        return JSONResponse(
            {"ok": False, "error": "Can't add shows to a past month that was never tracked."},
            status_code=400,
        )
    await distrakt_store.add_show(user_id, month_key, show)
    await _register_networks(user_id, [show["network"]])
    try:  # baseline the show's watch history now so its counts are correct immediately
        await watch_history.baseline_show(settings, user_id, show)
    # Deliberately broad: this is a nicety, not the add. Whatever went wrong —
    # Trakt refusing, a malformed history row, a database hiccup — the row is
    # already stored and the next month load re-baselines it, so failing the
    # user's add over it would be the worse outcome.
    except Exception:
        logger.warning("baseline_show failed for %s", key, exc_info=True)
    payload, status = await _distrakt_month_payload(user_id, year, month, settings)  # recomputed month (1d)
    return JSONResponse(payload, status_code=status)


@guard.post("/api/distrakt/add-completed", AuthLevel.DISTRAKT_APPROVED)
async def api_distrakt_add_completed(request: Request):
    """Record a show+season as FINISHED in a past month, whatever watch history says.

    The manual counterpart to the history backfill, for what a sweep cannot know:
    a season watched somewhere that never reached Trakt, a play logged against
    the wrong date, a show the history call simply did not return. It states
    outright that it is not consulting history — the caller is the authority
    here, which is why this is its own route rather than a flag on the ordinary
    add, whose whole job is to let the buckets work the answer out.

    Deliberately allowed to create a month that `can_initialize` would refuse.
    That guard exists to stop month-nav from silently inventing history; this is a
    person saying "January had this in it", which is the case it was never meant
    to cover. Past months only: the current month is the tracker's own to bucket.

    The episode total comes from Trakt's season detail, not from the caller — a
    frozen month's counts are never recomputed, so a wrong one is wrong forever
    and would reach the ranker import as a wrong episode count.
    """
    user_id = await _distrakt_user_id(request)
    settings = await _distrakt_settings(user_id)
    if not settings.trakt_configured:
        return JSONResponse({"ok": False, "error": "Not configured"}, status_code=400)
    data = await authz.json_body(request)
    today = clock.today()
    year = route_params.valid_year(data.get("year"), today.year)
    month = route_params.valid_month(data.get("month"), today.month)
    month_key = distrakt_store.month_key(year, month)
    if month_key >= distrakt_store.month_key(today.year, today.month):
        return JSONResponse(
            {"ok": False, "error": "Only a past month can be filled in by hand."},
            status_code=400)
    ids = _client_ids(data.get("ids"))
    try:
        season = int(data["season"])
    except (KeyError, TypeError, ValueError):
        return JSONResponse({"ok": False, "error": "Missing or invalid season"}, status_code=400)
    if not ids.get("trakt"):
        return JSONResponse({"ok": False, "error": "Missing or invalid ids"}, status_code=400)

    try:
        detail = await fetch_season_detail(settings, ids["trakt"], season)
    except TraktError as exc:
        return JSONResponse({"ok": False, "error": f"Trakt could not be read: {exc}"}, status_code=502)
    total = int((detail or {}).get("total") or 0)
    if not total:
        return JSONResponse(
            {"ok": False, "error": "Trakt lists no episodes for that season, so it cannot be recorded as finished."},
            status_code=400)

    try:
        await distrakt_store.add_show(user_id, month_key, {
            "media": Media.SHOW,
            "ids": ids,
            "title": data.get("title") or "",
            "season": season,
            "network": data.get("network") or "",
            "watched": total,
            "total": total,
            "cadence": (detail or {}).get("cadence"),
            "premiere": (detail or {}).get("premiere"),
            "finale": (detail or {}).get("finale"),
            "started_airing": True,
            "finished_airing": True,
            "bucket": distrakt_store.Bucket.COMPLETED,
            "added_by": distrakt_store.ADDED_BY_MANUAL,
        })
    except distrakt_store.UnkeyableRecord as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    # add_show leaves an existing month's `closed` alone and creates a new one
    # open; either way a hand-filled past month ends up closed, like every other
    # past month, so it renders and imports with no further Trakt calls.
    doc = await distrakt_store.load_month(user_id, month_key)
    if doc is not None and not doc.get("closed"):
        doc["closed"] = True
        doc["totals_refreshed_at"] = db.now()
        await distrakt_store.save_month(user_id, doc)
    await _register_networks(user_id, [data.get("network") or ""])
    payload, status = await _distrakt_month_payload(user_id, year, month, settings)
    return JSONResponse(payload, status_code=status)


@guard.get("/api/distrakt/backfill", AuthLevel.DISTRAKT_APPROVED)
async def api_distrakt_backfill_range(request: Request):
    """The range the backfill dialog opens on, and what is already tracked."""
    user_id = await _distrakt_user_id(request)
    tracked = await distrakt_store.list_months(user_id)
    start, end = backfill.default_range()
    return JSONResponse({"ok": True, "start": start, "end": end, "tracked": tracked})


def _requested_month(value, fallback: str) -> str | None:
    """A "YYYY-MM" out of the backfill form: `fallback` when nothing was sent,
    the value when it is a real month key, and None when it is neither.

    ABSENT AND MALFORMED ARE DIFFERENT ANSWERS, and collapsing them is the bug
    this exists to stop. Substituting the default for a value somebody actually
    typed means a request for two months quietly runs as eight — the user sees a
    plausible result and no sign that what they asked for was discarded. Nothing
    typed at all is the only case a default belongs in.
    """
    if value is None or not str(value).strip():
        return fallback
    return backfill.valid_month(value)


@guard.post("/api/distrakt/backfill/survey", AuthLevel.DISTRAKT_APPROVED)
async def api_distrakt_backfill_survey(request: Request):
    """Work out what a backfill of {start, end} would write, and write nothing.

    The expensive half: one history sweep plus a lookup per show and per season.
    The plan it produces is kept server-side for /apply, so the summary the user
    confirms is the same set of records that gets written, and the client never
    hands records back to be stored.
    """
    user_id = await _distrakt_user_id(request)
    settings = await _distrakt_settings(user_id)
    if not settings.trakt_configured:
        return JSONResponse({"ok": False, "error": "Not configured"}, status_code=400)
    data = await authz.json_body(request)
    default_start, default_end = backfill.default_range()
    start = _requested_month(data.get("start"), default_start)
    end = _requested_month(data.get("end"), default_end)
    if start is None or end is None:
        return JSONResponse(
            {"ok": False, "error": "Months go in as YYYY-MM, with the month padded — 2026-07, not 2026-7."},
            status_code=400)
    if not backfill.month_range(start, end):
        return JSONResponse(
            {"ok": False, "error": "That range covers no months — check the order."},
            status_code=400)
    try:
        plan = await backfill.survey(user_id, settings, start, end)
    except TraktError as exc:
        return JSONResponse({"ok": False, "error": f"Trakt could not be read: {exc}"}, status_code=502)
    return JSONResponse({"ok": True, **backfill.summarize(plan)})


@guard.post("/api/distrakt/backfill/apply", AuthLevel.DISTRAKT_APPROVED)
async def api_distrakt_backfill_apply(request: Request):
    """Write the surveyed plan. Refuses if there isn't one — the survey is the
    only thing that can produce records, and it expires."""
    user_id = await _distrakt_user_id(request)
    try:
        written = await backfill.apply(user_id)
    except backfill.BackfillExpired as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=409)
    months = await distrakt_store.list_months(user_id)
    return JSONResponse({"ok": True, **written, "tracked": months})


@guard.post("/api/distrakt/abandon", AuthLevel.DISTRAKT_APPROVED)
async def api_distrakt_abandon(request: Request):
    """Toggle a show+season's abandoned flag. On abandon, freezes
    `abandoned_form` = the current live inline form minus premiere/finale dates,
    via discord_fmt.freeze_form — so the Discord line stays stable even after the
    show would otherwise have moved buckets. Un-abandoning clears it
    (distrakt_store.set_abandoned's job). If Trakt isn't configured (or the show
    isn't found), abandoned_form falls back to None.

    ABANDONING PUTS THE TITLE ON THIS MONTH'S ROSTER IF IT IS NOT THERE ALREADY.
    The user's own lists are drawn from every month at once, so the row being
    abandoned may well be stored elsewhere — but "I stopped following this" is a
    fact about the month it happened in, and the month it happens to be stored
    under has usually already settled. Un-abandoning has no such fallback: there
    is nothing to un-say about a month with no such row.

    IT ALSO TURNS THE TITLE AWAY ON THE CALENDAR, AND TAKING IT BACK UN-TURNS
    IT, for the rows _calendar_item_a_row_speaks_for allows — the same rule and
    the same helper the ✕ uses. Giving up on a title the calendar is still
    offering and leaving it sitting there is the two views disagreeing about a
    decision just made.

    BOTH DIRECTIONS OR NEITHER, and the second is not symmetry for its own sake.
    A calendar mark made after a month opened is read on the next load as having
    given up on the title (see _apply_not_watching), so a mark left standing
    behind an un-abandon gives the row up again the moment anybody looks at the
    month: the button reports success and the row comes back abandoned, with
    nothing in between to say why.
    """
    user_id = await _distrakt_user_id(request)
    data = await authz.json_body(request)
    today = clock.today()
    year = route_params.valid_year(data.get("year"), today.year)
    month = route_params.valid_month(data.get("month"), today.month)
    try:
        key, season = _row_target(data)
    except RequestError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    abandoned = bool(data.get("abandoned"))
    month_key = distrakt_store.month_key(year, month)

    abandoned_form = None
    if abandoned:
        abandoned_form = await _freeze_abandon_form(user_id, month_key, key, season)

    rec = await distrakt_store.set_abandoned(user_id, month_key, key, season, abandoned,
                                             abandoned_form=abandoned_form)
    if rec is None and abandoned:
        held = await distrakt_store.find_user_row(user_id, key, season)
        if held is not None:
            await distrakt_store.add_show(user_id, month_key, held)
            rec = await distrakt_store.set_abandoned(user_id, month_key, key, season, True,
                                                     abandoned_form=abandoned_form)
    if rec is None:
        return JSONResponse({"ok": False, "error": "Show/season not found in that month"}, status_code=404)

    settings = await _distrakt_settings(user_id)
    # Read AFTER the writes above, so a row that reached this month through the
    # held-elsewhere fallback is judged on the month it has just landed on.
    doc = await distrakt_store.load_month(user_id, month_key)
    item_id = await _calendar_item_a_row_speaks_for(
        user_id, month_key, doc, rec, key, season, settings)
    if item_id is not None:
        await calendar_state.set_not_watching(user_id, item_id, abandoned)
    payload, status = await _distrakt_month_payload(user_id, year, month, settings)  # recomputed month (1d)
    return JSONResponse(payload, status_code=status)


async def _freeze_abandon_form(user_id: int, month_key: str,
                               key: ItemKey, season: int) -> str | None:
    """The show's inline Discord form as it stands right now, so an abandoned line
    stays stable even after the show would otherwise have moved buckets.

    None when there is nothing to freeze from — no such record, no Trakt, or Trakt
    unreachable. The renderer then recomputes a form from the stored record
    instead. Abandoning is a user ACTION and must still succeed: it is not a read
    to fail on, which is why a Trakt failure here is swallowed rather than
    returned.
    """
    doc = await distrakt_store.load_month(user_id, month_key)
    rec = next(
        (r for r in (doc or {}).get("shows", [])
         if r["key"] == str(key) and r.get("season") == season),
        None,
    )
    if rec is None:
        # Not on this month, but the user's own lists show titles stored on other
        # months, so the row being abandoned may be one of those.
        rec = await distrakt_store.find_user_row(user_id, key, season)
    if rec is None:
        return None
    settings = await _distrakt_settings(user_id)
    if not settings.trakt_configured:
        return None
    source_id = (rec.get("ids") or {}).get("trakt")
    if source_id is None:
        return None
    try:
        watched, detail = await asyncio.gather(
            fetch_watched_map(settings, [source_id]),
            fetch_season_detail(settings, source_id, season),
        )
    except TraktError:
        return None
    # fetch_watched_map answers in Trakt's own (id, season) terms; the merge below
    # reads the shared identity, so this one record's count is re-filed under it.
    watched_lookup = {distrakt_store.live_key(rec): watched.get((int(source_id), season), 0)}
    return discord_fmt.freeze_form(_merge_live_show(rec, watched_lookup, detail))


@guard.get("/api/distrakt/export", AuthLevel.DISTRAKT_APPROVED)
async def api_distrakt_export(request: Request):
    """Download the REQUESTING user's complete distrakt dataset as one JSON
    document — every month, show row, watch state, per-season progress, and movie
    watch. Contains no tokens and no other user's data, and doubles as the input
    POST /api/distrakt/restore takes back."""
    user_id = await _distrakt_user_id(request)
    doc = await distrakt_store.export_user_data(user_id)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d")
    return JSONResponse(doc, headers={
        "Content-Disposition": f'attachment; filename="distrakt-export-{stamp}.json"',
    })


@guard.post("/api/distrakt/restore", AuthLevel.DISTRAKT_APPROVED)
async def api_distrakt_restore(request: Request):
    """Replace the requesting user's distrakt data with an exported document.

    REPLACE, not merge, in one transaction: a merge would need conflict rules for
    every field and has no clear use case. The restoring user comes from the
    session — any user_id in the file is ignored, so a document can never write
    into someone else's tracker. This is deliberately NOT the same thing as
    POST /api/distrakt/import, which pulls premieres in from the calendar."""
    user_id = await _distrakt_user_id(request)
    data = await authz.json_body(request)
    try:
        await distrakt_store.restore_user_data(user_id, data)
    except distrakt_store.RestoreError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    except db.DatabaseError as exc:
        logger.warning("distrakt restore failed for user %s", user_id, exc_info=True)
        return JSONResponse({"ok": False, "error": f"Could not restore this file: {exc}"}, status_code=400)
    months = await distrakt_store.list_months(user_id)
    return JSONResponse({"ok": True, "months": months})


async def _share_link_payload(user_id: int) -> dict:
    """What the announcement post's link controls need: the resolved URL plus
    which of the three forms are actually publishable right now, so the selector
    can offer only the ones that would produce a working link."""
    settings = load_settings()
    row = await share_links.get_or_create(user_id)
    user = await auth.get_user(user_id)
    username = user["username"] if user else None
    urls = share_links.share_urls(row, username, settings.public_base_url)
    return {
        "ok": True,
        "base_url_missing": not bool(settings.public_base_url),
        "url": share_links.post_link_with_view(row, username, settings.public_base_url),
        # None means "whatever the share panel prefers" rather than a fixed form,
        # which is a different state from having picked that same form outright.
        "kind": row["post_link_kind"],
        "preferred_kind": row["preferred_kind"],
        "endpoint": row["post_link_endpoint"],
        "available": {kind: bool(url) for kind, url in urls.items()},
    }


@guard.get("/api/distrakt/share-link", AuthLevel.DISTRAKT_APPROVED)
async def api_distrakt_share_link(request: Request):
    """This user's current announcement-post link settings."""
    return JSONResponse(await _share_link_payload(await _distrakt_user_id(request)))


@guard.post("/api/distrakt/share-link", AuthLevel.DISTRAKT_APPROVED)
async def api_distrakt_set_share_link(request: Request):
    """Choose which share-link form the announcement post embeds and which
    calendar view it opens on.

    Both fields are optional and each is only written when present, so the two
    controls save independently. An empty string clears the choice: the link form
    goes back to following the share panel's preferred kind, and the view goes
    back to whatever the owner's share defaults already resolve to.
    """
    user_id = await _distrakt_user_id(request)
    data = await authz.json_body(request)
    kind = data["kind"] or None if "kind" in data else ...
    endpoint = data["endpoint"] or None if "endpoint" in data else ...
    if kind is ... and endpoint is ...:
        return JSONResponse({"ok": False, "error": "Nothing to update"}, status_code=400)
    try:
        await share_links.set_post_link(user_id, kind=kind, endpoint=endpoint)
    except ValueError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    return JSONResponse(await _share_link_payload(user_id))
