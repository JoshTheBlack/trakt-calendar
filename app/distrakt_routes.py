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
import dataclasses
import json
import logging
from datetime import date, datetime, timezone

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from . import auth, authz, calendar_state, chrome, db, discord_fmt
from . import distrakt as distrakt_store
from . import distrakt_backfill, logos, route_params, share_links, trakt_routes, watch_history
from .auth import AuthLevel
from .config import load_settings
from .endpoints import endpoint_choices
from .perftrace import span
from .providers.base import ID_KEYS, ItemKey, Media, collect_ids, parse_item_key
from .providers.trakt import TraktError, TraktRateLimitError
from .providers.trakt.detail import (
    fetch_details,
    fetch_season_detail,
    fetch_show_seasons,
    search_shows,
    search_titles,
)
from .providers.trakt.sync import fetch_watched_map
from .templating import templates

logger = logging.getLogger(__name__)

router = APIRouter()
guard = authz.Guard(router)

# Which ids arriving in a request body are read as integers. imdb ids are not
# numbers and slugs are words, so those stay text; everything else is a numeric id
# in its own namespace and is coerced rather than stored as whatever the client
# happened to send, or the same title would key differently on two adds.
_NUMERIC_ID_KEYS = frozenset(ID_KEYS) - {"imdb", "slug"}


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
    expects (see app/discord_fmt.py's module docstring), plus the computed
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


@guard.get("/distrakt", AuthLevel.DISTRAKT_APPROVED)
async def distrakt(request: Request):
    """Hidden Discord-tracker page, reached through an easter egg rather than any
    link in the UI.

    Renders the shell for the requested {year, month}; the page's JS fetches the
    computed month via /api/distrakt/month (which lazily rolls the month over).
    Month-nav prev/next mirror the main calendar's nav (see index.html)."""
    today = date.today()
    user = await auth.current_user(request)
    year = route_params.valid_year(request.query_params.get("year"), today.year)
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
    today = date.today()
    year = route_params.valid_year(request.query_params.get("year"), today.year)
    month = route_params.valid_month(request.query_params.get("month"), today.month)
    doc = await distrakt_store.load_month(user_id, distrakt_store.month_key(year, month))
    return JSONResponse({"ok": True, "month": distrakt_store.month_key(year, month), "shows": (doc or {}).get("shows", [])})


async def _apply_not_watching(user_id: int, month_key: str,
                              shows: list[dict], committed: bool) -> list[dict]:
    """This user's own main-calendar not-watching marks, date-gated on the month's
    1st (committed):

      - PREVIEW (before the 1st): not-watching HIDES the show from the tracker —
        excluded from the list + both posts, but KEPT in the roster so un-toggling
        brings it straight back.
      - COMMITTED (on/after the 1st): not-watching promotes the roster show to
        Abandoned (persisted, form frozen). One-directional — never un-abandons;
        the dedicated /distrakt toggle + Delete stay the source of truth. The
        `abandoned` guard means a steady-state read does no extra writes.

    The marks read here are the user's whole set, not one month's: "not watching"
    is a fact about the show, so a show they turned off on the calendar is one
    the tracker should not be counting whichever month they said it in."""
    nw_ids = await calendar_state.not_watching_ids(user_id)
    if not nw_ids:
        return shows

    def matched(s: dict) -> bool:
        return distrakt_store.matches_not_watching(s, nw_ids)

    if not committed:
        return [s for s in shows if not matched(s)]

    for show in shows:
        if show.get("abandoned") or not matched(show):
            continue
        form = discord_fmt.freeze_form(show)
        await distrakt_store.set_abandoned(
            user_id, month_key, distrakt_store.record_key(show), show["season"],
            True, abandoned_form=form,
        )
        show["abandoned"] = True
        show["abandoned_form"] = form
        show["bucket"] = discord_fmt.Bucket.ABANDONED
    return shows


def _empty_month_payload(month_key: str, emojis: dict, default_emoji: str,
                         readonly: bool = False, link_url: str | None = None) -> dict:
    """Headers-only render for a month with no roster + no Trakt call: an
    unconfigured/uninitialized month (readonly=False) or a never-tracked past
    month reached by navigating backward (readonly=True). The tracker only
    ever rolls a month's snapshot forward, never backfills one after the fact,
    so an old month nobody was tracking at the time stays permanently empty
    and read-only rather than retroactively populating from Trakt."""
    return {
        "ok": True, "month": month_key, "closed": False, "readonly": readonly, "shows": [],
        "movies": [],
        "post1": discord_fmt.render_post1([], emojis, default_emoji, link_url=link_url, month=month_key),
        "post2": discord_fmt.render_post2([], emojis, default_emoji),
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


async def _stale_month_payload(user_id: int, month_key: str, emojis: dict, default_emoji: str,
                               link_url: str | None, rate_limited: bool) -> dict:
    """Render a month WITHOUT any Trakt call, from whatever is last persisted — the
    top-level fallback when a shared refresh prerequisite hit Trakt's rate limit or
    was unreachable. Stored records already carry each show's last-known
    watched/total/cadence/dates, so this projects them offline (frozen_shows) and
    re-buckets, attaching a visible notice so stale-but-real beats a false 0/0 or a
    500. `rate_limited` only chooses the notice wording; both cases degrade
    identically and return HTTP 200."""
    doc = await distrakt_store.load_month(user_id, month_key)
    shows = distrakt_store.frozen_shows(doc) if doc else []
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
        "post1": discord_fmt.render_post1(shows, emojis, default_emoji, link_url=link_url, month=month_key),
        "post2": discord_fmt.render_post2(shows, emojis, default_emoji, movies=(doc or {}).get("movies")),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        # Reports the ACTUAL cause: True only when Trakt rate-limited us, False when
        # it was simply unreachable. The client shows the banner off `notice`
        # (present only on this degraded payload), not off this flag, so both cases
        # still surface — this stays accurate metadata rather than a banner trigger.
        "rate_limited": rate_limited,
        "notice": notice,
    }


def _closed_month_payload(doc: dict, month_key: str, emojis: dict,
                          default_emoji: str, link_url: str | None) -> dict:
    """A frozen past month, rendered straight from its own snapshot with NO Trakt
    calls. The snapshot is the record of what that month WAS — recomputing it
    against today's watch history would rewrite history every time it was opened."""
    shows = distrakt_store.frozen_shows(doc)
    return {
        "ok": True, "month": month_key, "closed": True, "readonly": False, "shows": shows,
        "movies": doc.get("movies") or [],
        "post1": discord_fmt.render_post1(shows, emojis, default_emoji, link_url=link_url, month=month_key),
        "post2": discord_fmt.render_post2(shows, emojis, default_emoji, movies=doc.get("movies")),
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


async def _live_month_payload(user_id: int, doc: dict, month_key: str, settings,
                              emojis: dict, default_emoji: str, link_url: str | None,
                              force_fresh: bool, today: date) -> tuple[dict, int]:
    """Compute an OPEN month against Trakt: refresh the roster from premieres,
    read the watch history, work out each show's live x/y and bucket, and render
    the two Discord posts.

    Everything here may reach Trakt, and the caller is what turns a failure into
    the stale-but-real fallback — this function does not degrade, so that decision
    lives in exactly one place.
    """
    committed = distrakt_store.month_committed(month_key, today)
    # A PREVIEW month (before the 1st) keeps auto-populating from premieres so the
    # roster tracks the calendar (and un-not-watching re-adds a previously excluded
    # premiere). A COMMITTED month is stable — premieres only re-import on demand.
    if not committed and settings.trakt_configured:
        await distrakt_store.import_premieres(user_id, month_key, settings)
        doc = await distrakt_store.load_month(user_id, month_key) or doc

    records = doc.get("shows", [])
    if records and not settings.trakt_configured:
        return {"ok": False, "error": "Not configured"}, 400

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
            settings, user_id, records, month_key, force_fresh, today)

    with span("payload.compute_live_shows", n=len(records), fresh=season_fresh):
        # allow_degrade: a per-show season 429 marks THAT show unavailable and
        # renders the rest, instead of failing the whole roster.
        shows = await distrakt_store.compute_live_shows(
            user_id, records, settings, fresh=season_fresh, watched_lookup=watched_lookup,
            allow_degrade=True, completed_lookup=completed_lookup) if records else []
    shows = await _apply_not_watching(user_id, month_key, shows, committed)
    # A season finished before this month began belongs to the month it was
    # finished in, not to this one — see drop_seasons_finished_earlier.
    shows = await distrakt_store.drop_seasons_finished_earlier(user_id, month_key, shows)
    if records and season_fresh:
        await distrakt_store.stamp_refreshed(user_id, month_key)

    # Pre-warm the network-logo cache for the whole roster, so a show manually
    # added before logos existed doesn't depend on some OTHER show requesting its
    # network's logo first (see logos.ensure_logos). Best-effort and
    # self-limiting: a no-op once each network's tile is on disk.
    if shows and settings.trakt_configured:
        with span("payload.ensure_logos"):
            await logos.ensure_logos(settings, [
                (s.get("network"), (s.get("ids") or {}).get("tmdb")) for s in shows
            ])

    with span("payload.render"):
        post1 = discord_fmt.render_post1(shows, emojis, default_emoji, link_url=link_url, month=month_key)
        post2 = discord_fmt.render_post2(shows, emojis, default_emoji, movies=movies)
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
    is rendered empty + read-only and never created. Returns (json_payload,
    http_status).

    This is also where Trakt failing is turned into an answer. A 429 on a SHARED
    prerequisite (anything but the per-show season fan-out, which degrades itself)
    can't be attributed to one show, so rather than a false 0/0 or a 500 the whole
    month falls back to its last-known stored totals plus a notice at HTTP 200 —
    the user refreshes again once the window clears. A plain reachability failure
    degrades the same way; only the notice wording differs.
    """
    today = date.today()
    month_key = distrakt_store.month_key(year, month)
    link_url = await _distrakt_post_link(user_id, settings, year, month)
    # This user's own map, fetched once and handed to every render below. It is
    # not on `settings` any more — see _distrakt_settings.
    emojis, default_emoji = await distrakt_store.get_emoji_prefs(user_id)
    existing = await distrakt_store.load_month(user_id, month_key)
    if existing is None:
        blocked = await distrakt_store.is_backfill_blocked(user_id, month_key)
        if blocked or not settings.trakt_configured:
            # Backward/gap past month (blocked) OR no Trakt yet: empty, NOT
            # persisted, no Trakt call. `readonly` hides the add/edit affordances.
            return _empty_month_payload(
                month_key, emojis, default_emoji, readonly=blocked, link_url=link_url,
            ), 200

    try:
        with span("payload.ensure_month", month=month_key, force=force_fresh):
            doc = await distrakt_store.ensure_month(user_id, year, month, settings, today=today)
        month_key = doc["month"]
        if doc.get("closed"):
            return _closed_month_payload(doc, month_key, emojis, default_emoji, link_url), 200
        return await _live_month_payload(
            user_id, doc, month_key, settings, emojis, default_emoji, link_url,
            force_fresh, today)
    except TraktRateLimitError as exc:
        logger.warning("distrakt month %s degraded to stale (Trakt rate-limited): %s", month_key, exc)
        return await _stale_month_payload(user_id, month_key, emojis, default_emoji, link_url, rate_limited=True), 200
    except TraktError as exc:
        logger.warning("distrakt month %s degraded to stale (Trakt unreachable): %s", month_key, exc)
        return await _stale_month_payload(user_id, month_key, emojis, default_emoji, link_url, rate_limited=False), 200


@guard.get("/api/distrakt/month", AuthLevel.DISTRAKT_APPROVED)
async def api_distrakt_month(request: Request):
    """Computed buckets + the two copy-paste POST 1/POST 2 markdown blocks.

    OPEN month: live x/y + cadence/dates recomputed (1x /sync/watched/shows + 1x
    season call per show), auto-refreshed if totals are stale >24h. CLOSED /
    past month: rendered from the frozen snapshot with NO Trakt calls. Opening an
    uninitialized month lazily rolls it over first (see ensure_month)."""
    user_id = await _distrakt_user_id(request)
    today = date.today()
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
    today = date.today()
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
    today = date.today()
    current = distrakt_store.month_key(today.year, today.month)
    months = sorted(set(await distrakt_store.list_months(user_id)) | {current})
    return JSONResponse({"ok": True, "months": months, "current": current})


@guard.post("/api/distrakt/import", AuthLevel.DISTRAKT_APPROVED)
async def api_distrakt_import(request: Request):
    """Pull this month's calendar premieres into the OPEN month (shows/new -> New,
    shows/premieres minus new -> Returning; skips existing + not-watching). The
    manual "Import from calendar" action — e.g. to seed the current month when its
    doc already exists (so lazy-init's one-shot premiere seeding was skipped).
    Returns the same shape as GET /api/distrakt/month."""
    user_id = await _distrakt_user_id(request)
    settings = await _distrakt_settings(user_id)
    if not settings.trakt_configured:
        return JSONResponse({"ok": False, "error": "Not configured"}, status_code=400)
    data = await authz.json_body(request)
    today = date.today()
    year = route_params.valid_year(data.get("year"), today.year)
    month = route_params.valid_month(data.get("month"), today.month)
    month_key = distrakt_store.month_key(year, month)
    if await distrakt_store.is_backfill_blocked(user_id, month_key):
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
    today = date.today()
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


@guard.post("/api/distrakt/remove", AuthLevel.DISTRAKT_APPROVED)
async def api_distrakt_remove(request: Request):
    """Delete a show+season from a month (cleanup mistakes / abandons), and for a
    row the CALENDAR put there in an OPEN month, mark the show not-watching on
    the calendar as well.

    That mark is not a bonus, it is what makes such a removal STICK. A preview
    month (before the 1st) re-imports the month's premieres on every load, and
    the not-watching set is the only thing import_premieres skips — so deleting
    the row alone put it straight back in the same response and the ✕ looked
    broken.

    It is deliberately NOT written for a row the user added by hand or one that
    came from their watch history: neither is re-imported, so removing them
    already sticks, and hiding a show on the calendar because someone undid a
    manual add would take away something they never said they weren't watching.
    A row from before provenance was recorded is resolved by asking the calendar
    whether it would hand that show straight back (see is_calendar_premiere).

    A CLOSED month never writes that mark, whatever the row says. Correcting what
    a past month records is a statement about that month and nothing else — a
    season you finished years ago and re-watched one episode of does not belong
    on March's list, but it also is not something to start hiding from your
    calendar today. The row goes; the month stays closed.
    """
    user_id = await _distrakt_user_id(request)
    data = await authz.json_body(request)
    today = date.today()
    year = route_params.valid_year(data.get("year"), today.year)
    month = route_params.valid_month(data.get("month"), today.month)
    try:
        key, season = _row_target(data)
    except RequestError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    month_key = distrakt_store.month_key(year, month)
    doc = await distrakt_store.load_month(user_id, month_key)
    if doc is None:
        return JSONResponse({"ok": False, "error": "Show/season not found in that month"}, status_code=404)
    # Read before removing: both the provenance and the id the mark is written
    # under (slug, falling back to the source's own id, exactly as the calendar
    # keys its own items) live on the record that is about to go.
    record = next((s for s in (doc.get("shows") or [])
                   if s["key"] == str(key) and int(s["season"]) == season), None)
    if not await distrakt_store.remove_show(user_id, month_key, key, season):
        return JSONResponse({"ok": False, "error": "Show/season not found in that month"}, status_code=404)

    settings = await _distrakt_settings(user_id)
    closed = bool(doc.get("closed"))
    added_by = str((record or {}).get("added_by") or "")
    hide_on_calendar = not closed and added_by == distrakt_store.ADDED_BY_CALENDAR
    if record is not None and not added_by and not closed:
        hide_on_calendar = await distrakt_store.is_calendar_premiere(
            user_id, month_key, settings, key, season,
        )
    if hide_on_calendar:
        ids = (record or {}).get("ids") or {}
        await calendar_state.set_not_watching(
            user_id, str(ids.get("slug") or ids.get("trakt") or key.match_id), True,
        )
    payload, status = await _distrakt_month_payload(user_id, year, month, settings)  # recomputed month (1d)
    # So the toast can say what actually happened rather than guessing.
    if isinstance(payload, dict):
        payload["hidden_on_calendar"] = hide_on_calendar
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
    if watched_on > date.today():
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

    today = date.today()
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

    today = date.today()
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
    today = date.today()
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
    if await distrakt_store.is_backfill_blocked(user_id, month_key):
        # No backfill: refuse to create a never-tracked past/gap month even via a
        # manual add (keeps a user's store growing forward-only, consistent with
        # the read path's read-only rendering of such months).
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
    today = date.today()
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
            "bucket": discord_fmt.Bucket.COMPLETED,
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
    start, end = distrakt_backfill.default_range()
    return JSONResponse({"ok": True, "start": start, "end": end, "tracked": tracked})


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
    default_start, default_end = distrakt_backfill.default_range()
    start = distrakt_backfill.valid_month(data.get("start")) or default_start
    end = distrakt_backfill.valid_month(data.get("end")) or default_end
    if not distrakt_backfill.month_range(start, end):
        return JSONResponse(
            {"ok": False, "error": "That range covers no months — check the order."},
            status_code=400)
    try:
        plan = await distrakt_backfill.survey(user_id, settings, start, end)
    except TraktError as exc:
        return JSONResponse({"ok": False, "error": f"Trakt could not be read: {exc}"}, status_code=502)
    return JSONResponse({"ok": True, **distrakt_backfill.summarize(plan)})


@guard.post("/api/distrakt/backfill/apply", AuthLevel.DISTRAKT_APPROVED)
async def api_distrakt_backfill_apply(request: Request):
    """Write the surveyed plan. Refuses if there isn't one — the survey is the
    only thing that can produce records, and it expires."""
    user_id = await _distrakt_user_id(request)
    try:
        written = await distrakt_backfill.apply(user_id)
    except distrakt_backfill.BackfillExpired as exc:
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
    isn't found), abandoned_form falls back to None."""
    user_id = await _distrakt_user_id(request)
    data = await authz.json_body(request)
    today = date.today()
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
    if rec is None:
        return JSONResponse({"ok": False, "error": "Show/season not found in that month"}, status_code=404)
    payload, status = await _distrakt_month_payload(user_id, year, month, await _distrakt_settings(user_id))  # recomputed month (1d)
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
