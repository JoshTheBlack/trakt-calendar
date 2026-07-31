"""Public calendar sharing: the read-only /s/, /u/, /c/ pages, and the
owner-facing API the calendar's Share panel calls to manage them.

The three public routes do the SAME read as the authenticated calendar —
calendar_cache.read_month, the per-(endpoint, 7-day window) cache, normalized
into a viewer's timezone the same way — with the fetch branch permanently
switched off: a public request serves whatever is cached — even stale, even
nothing — and never spends the instance's Trakt rate limit. A visitor is never
given a session, and nothing here writes anything on their behalf.

A miss is identical whatever the reason — an unknown token, a disabled
account, or a retired username/slug — so a share link can never be used to
probe which of those three it is.
"""
from __future__ import annotations

import calendar as _calendar
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime
from itertools import groupby
from urllib.parse import quote, urlencode
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, PlainTextResponse, RedirectResponse, Response

from . import auth, calendar_cache, calendar_state, route_params, share_code, share_links
from .providers.trakt import detail as trakt_detail
from .auth import AuthLevel
from . import authz
from .authz import Guard
from .config import load_settings
from .endpoints import DEFAULT_ENDPOINT, ENDPOINTS, Endpoint, endpoint_choices, get_endpoint
from .timezones import build_options as build_timezone_options
from .templating import templates

router = APIRouter()
guard = Guard(router)

# One definition, in the module that also builds the links carrying these
# values, so a link can never offer a view this page would reject.
_CARD_STYLES = share_links.CARD_STYLES
_DAY_PACKINGS = share_links.DAY_PACKINGS

# Purely anti-scrape — these pages never touch Trakt, so this is not protecting
# a rate-limited upstream, just keeping a bot from hammering the cache reads.
SHARE_RATE_MAX_ATTEMPTS = 120
SHARE_RATE_WINDOW_SECONDS = 60

# Query params a share request may carry, kept here so the view-option resolvers
# below and the "carry these into the month-nav links" helper agree on the set.
_CARRY_PARAMS = ("card", "packing", "hidenw", "tz", "networks", "endpoint")


# ---------------------------------------------------------------------------
# small page-local helpers
# ---------------------------------------------------------------------------

def _carry_query(request: Request) -> str:
    parts = [
        f"{key}={quote(value)}" for key in _CARRY_PARAMS
        if (value := request.query_params.get(key))
    ]
    return ("&" + "&".join(parts)) if parts else ""


# ---------------------------------------------------------------------------
# view-option resolution — request param -> owner's share_links default -> app
# default, whitelisted at every tier, never erroring on an invalid value
# ---------------------------------------------------------------------------
# EVERY RESOLVER BELOW TAKES A PLAIN MAPPING OF PARAMS RATHER THAN THE REQUEST,
# because the page and the share card do not read them from the same place: the
# page reads the query string, while the card reads a compact `p=` code expanded
# in place into a dict (a page REDIRECTS to the expanded form instead, which is
# the wrong move for an image an unfurler fetches once). Taking the mapping is
# what lets both surfaces ask these functions the same question and get the same
# answer — see `resolve_view`.

_Params = Mapping[str, str]


def _resolve_endpoint(params: _Params, share_row, settings) -> Endpoint:
    def _valid(key):
        return key if key in ENDPOINTS else None

    key = (
        _valid(params.get("endpoint"))
        or _valid(share_row["endpoint"])
        or _valid(settings.endpoint)
        or DEFAULT_ENDPOINT
    )
    return get_endpoint(key)


def _resolve_choice(value, share_default, app_default, choices):
    for candidate in (value, share_default, app_default):
        if candidate in choices:
            return candidate
    return choices[0]


def _resolve_hide_not_watching(params: _Params, share_row, settings) -> bool:
    raw = params.get("hidenw")
    if raw in ("0", "1"):
        return raw == "1"
    if share_row["hide_not_watching"] is not None:
        return bool(share_row["hide_not_watching"])
    return bool(settings.hide_not_watching)


def _resolve_networks(params: _Params, share_row, settings) -> list[str] | None:
    if "networks" in params:
        names = [n.strip() for n in (params.get("networks") or "").split(",") if n.strip()]
        return names or None
    stored = json.loads(share_row["network_filter_json"] or "[]")
    if stored:
        return stored
    return list(settings.network_filter or []) or None


def _resolve_owner_tz(share_row, settings) -> ZoneInfo:
    """The owner's default timezone: their share-specific override if they set
    one, else their account's own saved timezone, else the app-wide default."""
    for name in (share_row["timezone"], share_row["owner_account_timezone"], settings.timezone, "UTC"):
        if not name:
            continue
        try:
            return ZoneInfo(name)
        except (ZoneInfoNotFoundError, ValueError):
            continue
    return ZoneInfo("UTC")


def _resolve_tz(params: _Params, share_row, settings) -> ZoneInfo:
    requested = (params.get("tz") or "").strip()
    if requested:
        try:
            return ZoneInfo(requested)
        except (ZoneInfoNotFoundError, ValueError):
            pass  # invalid -> owner default, never an error
    return _resolve_owner_tz(share_row, settings)


@dataclass(frozen=True)
class ShareView:
    """Which view of a shared calendar a request is asking for, after every
    fallback tier has been applied.

    A VALUE, NOT A REQUEST. Once this exists, "what is being looked at" is
    settled and nothing downstream needs the query string again — which is the
    point, because the same shared month is rendered by two different surfaces
    (the HTML page and its preview card) and a card advertising a count the page
    does not show is worse than no card at all. One resolver, one answer, nothing
    to drift.

    `card_style` and `day_packing` are page layout and mean nothing to the card;
    everything else changes WHICH AIRINGS are in the view and therefore changes
    both.
    """
    year: int
    month: int
    endpoint: Endpoint
    tz: ZoneInfo
    hide_not_watching: bool
    network_filter: list[str] | None
    card_style: str
    day_packing: str

    @property
    def month_label(self) -> str:
        return _calendar.month_name[self.month]


def resolve_view(params: _Params, share_row, settings) -> ShareView:
    """The one place a share request's view options are worked out.

    `params` is whatever the caller has already established the request means —
    the raw query string for a page, a `p=` code expanded in place for the card.
    Every value is whitelisted on the way through and an unusable one falls back
    rather than raising, because these arrive from strangers editing URLs.
    """
    today = date.today()
    return ShareView(
        year=route_params.valid_year(params.get("year"), today.year),
        month=route_params.valid_month(params.get("month"), today.month),
        endpoint=_resolve_endpoint(params, share_row, settings),
        tz=_resolve_tz(params, share_row, settings),
        hide_not_watching=_resolve_hide_not_watching(params, share_row, settings),
        network_filter=_resolve_networks(params, share_row, settings),
        card_style=_resolve_choice(
            params.get("card"), share_row["card_style"], settings.card_style, _CARD_STYLES,
        ),
        day_packing=_resolve_choice(
            params.get("packing"), share_row["day_packing"], settings.day_packing, _DAY_PACKINGS,
        ),
    )


# ---------------------------------------------------------------------------
# the never-fetch read + render
# ---------------------------------------------------------------------------

def _not_found(request: Request) -> Response:
    return templates.TemplateResponse(request, "share_not_found.html", {"request": request}, status_code=404)


def _expanded_from_code(request: Request) -> str | None:
    """Where a `?p=` request should go instead, or None when there is no code.

    A code is only how a link is HANDED OUT. On arrival it is expanded back into
    the ordinary query params this page has always taken and the visitor is sent
    there, so from the first paint the URL they can edit, bookmark and re-share
    is the plain one — and the month arrows, the view controls and every other
    param-carrying thing on the page need know nothing about the short form.

    Anything the visitor spelled out themselves wins over the same param in the
    code, and `p` never survives the redirect, even when it decoded to nothing.
    """
    if "p" not in request.query_params:
        return None
    params = share_code.decode(request.query_params.get("p"))
    for key, value in request.query_params.multi_items():
        if key != "p":
            params[key] = value
    query = urlencode(params)
    return f"{request.url.path}?{query}" if query else request.url.path


async def _render(request: Request, share_row) -> Response:
    if share_row is None:
        return _not_found(request)

    # After the miss above, so an unusable link is still a 404 on the spot rather
    # than a redirect that then 404s.
    if target := _expanded_from_code(request):
        return RedirectResponse(target, status_code=302)

    settings = load_settings()
    owner_id = int(share_row["user_id"])
    owner_prefs = await auth.get_user_prefs(owner_id)

    view = resolve_view(request.query_params, share_row, settings)

    items: list[dict] = []
    as_of: int | None = None
    if settings.calendar_source_configured:
        # allow_fetch=False is the whole point of a public share page: a
        # visitor is served whatever is already cached, even stale, even
        # nothing, and never triggers a Trakt call that would spend the
        # owner's rate-limit budget on an anonymous request.
        items, as_of = await calendar_cache.read_month(
            view.endpoint, settings, tz=view.tz, year=view.year, month=view.month,
            genres=owner_prefs["genres"], countries=owner_prefs["countries"],
            show_certifications=owner_prefs["show_certifications"],
            movie_certifications=owner_prefs["movie_certifications"],
            network_filter=view.network_filter, allow_fetch=False,
        )

    # The owner's marks travel to the template as a SET, the same way the
    # signed-in calendar's own card partial reads them, rather than being copied
    # onto each item as a field. One less per-item copy, and one answer to "is
    # this marked" instead of two spellings of it.
    nw_ids = await calendar_state.not_watching_ids(owner_id)
    visible = [i for i in items if not (view.hide_not_watching and i.id in nw_ids)]

    grouped = [
        {"date": day, "label": datetime.strptime(day, "%Y-%m-%d").strftime("%A, %d %B"), "items": list(rows)}
        for day, rows in groupby(visible, key=lambda i: i.air_date)
    ]
    as_of_label = datetime.fromtimestamp(as_of, tz=view.tz).strftime("%Y-%m-%d %H:%M %Z") if as_of else None

    # Open Graph tags for link unfurlers (Discord/Slack/etc.). Both URLs are
    # absolute and built only from the configured public_base_url — never the
    # request Host — since an unauthenticated crawler resolves relative paths
    # unreliably and a spoofed Host header must not become the advertised
    # origin. Absent a configured base there is nothing safe to advertise, so
    # the tags are simply omitted and the link falls back to a bare text preview.
    base = _public_base(settings)
    og_image = f"{base}/static/images/tvbanner.png" if base else None
    og_url = None
    if base:
        urls = share_links.share_urls(share_row, share_row["owner_username"], base)
        og_url = urls.get(share_row["preferred_kind"]) or next((u for u in urls.values() if u), None)

    context = {
        "request": request,
        "owner_username": share_row["owner_username"],
        "og_image": og_image,
        "og_url": og_url,
        "year": view.year,
        "month": view.month,
        "month_label": view.month_label,
        "nav": route_params.adjacent_months(view.year, view.month),
        "grouped": grouped,
        "not_watching": nw_ids,
        "total": len(visible),
        "view": {"card_style": view.card_style, "day_packing": view.day_packing},
        "as_of": as_of_label,
        "query_extra": _carry_query(request),
        # The visitor's own view controls. Everything they drive is a GET with
        # the same whitelisted params a hand-edited URL already carries, so they
        # add no write surface and need no session — they just save the visitor
        # from editing the query string by hand.
        "endpoints": endpoint_choices(),
        "endpoint_key": view.endpoint.key,
        "card_styles": _CARD_STYLES,
        "day_packings": _DAY_PACKINGS,
        "hide_not_watching": view.hide_not_watching,
        "timezone_groups": build_timezone_options(view.tz.key),
    }
    return templates.TemplateResponse(request, "share_calendar.html", context)


def _too_many_requests() -> Response:
    return PlainTextResponse("Too many requests.", status_code=429)


async def _share_rate_limited(request: Request, settings) -> bool:
    ip = auth.client_ip(request, settings)
    limited = await auth.rate_limited(
        "share_ip", ip, max_attempts=SHARE_RATE_MAX_ATTEMPTS, window_seconds=SHARE_RATE_WINDOW_SECONDS,
    )
    # Volume-only counter (like registration/invite redemption) — there is no
    # notion of a "failed" share-page request to distinguish.
    await auth.record_attempt("share_ip", ip, True)
    return limited


@guard.get("/s/{token}", AuthLevel.PUBLIC)
async def share_by_token(request: Request, token: str):
    settings = load_settings()
    if await _share_rate_limited(request, settings):
        return _too_many_requests()
    return await _render(request, await share_links.resolve_by_token(token))


@guard.get("/u/{username}", AuthLevel.PUBLIC)
async def share_by_username(request: Request, username: str):
    settings = load_settings()
    if await _share_rate_limited(request, settings):
        return _too_many_requests()
    return await _render(request, await share_links.resolve_by_username(username))


@guard.get("/c/{slug}", AuthLevel.PUBLIC)
async def share_by_slug(request: Request, slug: str):
    settings = load_settings()
    if await _share_rate_limited(request, settings):
        return _too_many_requests()
    return await _render(request, await share_links.resolve_by_slug(slug))


# ---------------------------------------------------------------------------
# details for a card on a public page — same modal content as the calendar
# ---------------------------------------------------------------------------
# CACHE-ONLY, same as the calendar view above: this never calls Trakt. The
# owner's own calendar views already fetch and cache each show's detail (cast,
# trailer, episodes);
# this serves that cache back to visitors. A show the owner has not viewed comes
# back with empty fields and the modal renders around them — no public request
# ever spends the owner's rate limit. Rate-limited per IP like every other share
# request; no membership gate is needed because there is no fetch to amplify.

def _season_param(value) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


async def _details(request: Request, share_row) -> Response:
    settings = load_settings()
    if await _share_rate_limited(request, settings):
        return _too_many_requests()
    if share_row is None:
        return JSONResponse({"ok": False, "error": "Not found"}, status_code=404)
    media = request.query_params.get("media", "show")
    trakt_id = (request.query_params.get("id") or "").strip()
    if not trakt_id:
        return JSONResponse({"ok": False, "error": "Missing id"}, status_code=400)
    season = _season_param(request.query_params.get("season"))
    details = await trakt_detail.fetch_details(settings, media, trakt_id, season, cache_only=True)
    return JSONResponse({"ok": True, **details})


@guard.get("/s/{token}/details", AuthLevel.PUBLIC)
async def share_details_by_token(request: Request, token: str):
    return await _details(request, await share_links.resolve_by_token(token))


@guard.get("/u/{username}/details", AuthLevel.PUBLIC)
async def share_details_by_username(request: Request, username: str):
    return await _details(request, await share_links.resolve_by_username(username))


@guard.get("/c/{slug}/details", AuthLevel.PUBLIC)
async def share_details_by_slug(request: Request, slug: str):
    return await _details(request, await share_links.resolve_by_slug(slug))


# ---------------------------------------------------------------------------
# owner-facing API — the Share panel on the logged-in calendar
# ---------------------------------------------------------------------------

def _public_base(settings) -> str:
    return (settings.public_base_url or "").rstrip("/")


def _share_payload(row, username: str | None, settings) -> dict:
    base = _public_base(settings)
    return {
        "ok": True,
        # Every URL below is None without a configured base — there is no
        # request-derived fallback (the request Host isn't trustworthy enough
        # to advertise as the public origin), so the panel needs to say why the
        # link boxes are empty rather than just rendering them blank.
        "base_url_missing": not bool(base),
        "token": row["token"],
        "custom_slug": row["custom_slug"],
        "preferred_kind": row["preferred_kind"],
        "enabled": {
            "token": bool(row["enabled_token"]),
            "username": bool(row["enabled_username"]),
            "slug": bool(row["enabled_slug"]),
        },
        # The links as handed out: carrying the owner's chosen view params, which
        # is the ONLY thing those params affect — not the owner's own calendar,
        # and not the share page's fallback for a link that omits them.
        "urls": share_links.generated_urls(row, username, base),
        # None == "use my current display", i.e. hand out a bare link and let the
        # page resolve the owner's defaults.
        "link_view": share_links.link_view(row),
    }


@guard.get("/api/me/share", AuthLevel.CALENDAR_APPROVED)
async def get_share(request: Request):
    user = await auth.current_user(request)
    row = await share_links.get_or_create(user.user_id)
    return JSONResponse(_share_payload(row, user.username, load_settings()))


@guard.post("/api/me/share/enabled", AuthLevel.CALENDAR_APPROVED)
async def post_share_enabled(request: Request):
    user = await auth.current_user(request)
    data = await authz.json_body(request)
    if data.get("kind") not in share_links.PREFERRED_KINDS:
        return JSONResponse({"ok": False, "error": "Expected {kind, enabled}"}, status_code=400)
    await share_links.set_enabled(user.user_id, data["kind"], bool(data.get("enabled")))
    row = await share_links.get(user.user_id)
    return JSONResponse(_share_payload(row, user.username, load_settings()))


@guard.post("/api/me/share/active", AuthLevel.CALENDAR_APPROVED)
async def post_share_active(request: Request):
    """Publish exactly one of the three link forms and retire the other two.

    What the Share panel's single dropdown writes. The granular
    /enabled + /preferred pair is still there for a caller that wants several
    forms live at once; this is the one-link-at-a-time shape the UI presents.
    """
    user = await auth.current_user(request)
    data = await authz.json_body(request)
    if data.get("kind") not in share_links.PREFERRED_KINDS:
        return JSONResponse({"ok": False, "error": "Expected {kind}"}, status_code=400)
    await share_links.set_active_kind(user.user_id, data["kind"])
    row = await share_links.get(user.user_id)
    return JSONResponse(_share_payload(row, user.username, load_settings()))


@guard.post("/api/me/share/preferred", AuthLevel.CALENDAR_APPROVED)
async def post_share_preferred(request: Request):
    user = await auth.current_user(request)
    data = await authz.json_body(request)
    if data.get("kind") not in share_links.PREFERRED_KINDS:
        return JSONResponse({"ok": False, "error": "Expected {kind}"}, status_code=400)
    await share_links.set_preferred_kind(user.user_id, data["kind"])
    row = await share_links.get(user.user_id)
    return JSONResponse(_share_payload(row, user.username, load_settings()))


@guard.post("/api/me/share/slug", AuthLevel.CALENDAR_APPROVED)
async def post_share_slug(request: Request):
    user = await auth.current_user(request)
    data = await authz.json_body(request)
    if "slug" not in data:
        return JSONResponse({"ok": False, "error": "Expected {slug}"}, status_code=400)
    err = await share_links.set_custom_slug(user.user_id, data.get("slug"))
    if err:
        return JSONResponse({"ok": False, "error": err}, status_code=400)
    row = await share_links.get(user.user_id)
    return JSONResponse(_share_payload(row, user.username, load_settings()))


@guard.get("/api/me/share/slug-check", AuthLevel.CALENDAR_APPROVED)
async def get_share_slug_check(request: Request):
    """Live availability for the slug field, called as the owner types."""
    user = await auth.current_user(request)
    slug = (request.query_params.get("slug") or "").strip()
    if not slug:
        return JSONResponse({"ok": True, "available": False, "error": "Enter a slug."})
    err = await share_links.slug_error(slug, exclude_user_id=user.user_id)
    return JSONResponse({"ok": True, "available": err is None, "error": err})


@guard.post("/api/me/share/view", AuthLevel.CALENDAR_APPROVED)
async def post_share_view(request: Request):
    """Set (or clear) the display options the generated link carries.

    `{"view": null}` hands out a bare link, so whoever opens it sees the owner's
    current display. `{"view": {...}}` pins those options into the URL instead.
    Either way nothing about the owner's own calendar changes — this writes the
    link and only the link.
    """
    user = await auth.current_user(request)
    data = await authz.json_body(request)
    if "view" not in data:
        return JSONResponse({"ok": False, "error": "Expected {view}"}, status_code=400)
    view = data["view"]
    if view is not None and not isinstance(view, dict):
        return JSONResponse({"ok": False, "error": "Expected {view}"}, status_code=400)
    err = await share_links.set_link_view(user.user_id, view)
    if err:
        return JSONResponse({"ok": False, "error": err}, status_code=400)
    row = await share_links.get(user.user_id)
    return JSONResponse(_share_payload(row, user.username, load_settings()))


@guard.post("/api/me/share/rotate", AuthLevel.CALENDAR_APPROVED)
async def post_share_rotate(request: Request):
    user = await auth.current_user(request)
    await share_links.rotate_token(user.user_id)
    row = await share_links.get(user.user_id)
    return JSONResponse(_share_payload(row, user.username, load_settings()))
