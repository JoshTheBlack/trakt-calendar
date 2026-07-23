"""Public calendar sharing: the read-only /s/, /u/, /c/ pages, and the
owner-facing API the calendar's Share panel calls to manage them.

The three public routes do the SAME read as the authenticated calendar (the
per-(endpoint, 7-day window) cache chat G built, normalized into a viewer's
timezone by chat H's read path), with the fetch branch permanently switched
off: a public request serves whatever is cached — even stale, even nothing —
and never spends the instance's Trakt rate limit. A visitor is never given a
session, and nothing here writes anything on their behalf.

A miss is identical whatever the reason — an unknown token, a disabled
account, or a retired username/slug — so a share link can never be used to
probe which of those three it is.
"""
from __future__ import annotations

import calendar as _calendar
import json
from datetime import date, datetime
from itertools import groupby
from pathlib import Path
from urllib.parse import quote
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, PlainTextResponse, Response
from fastapi.templating import Jinja2Templates

from . import auth, calendar_cache, calendar_state, share_links, trakt
from .auth import AuthLevel
from .authz import Guard
from .config import load_settings
from .endpoints import DEFAULT_ENDPOINT, ENDPOINTS, endpoint_choices, get_endpoint
from .timezones import build_options as build_timezone_options

router = APIRouter()
guard = Guard(router)
templates = Jinja2Templates(directory=Path(__file__).resolve().parent / "templates")

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
# small page-local helpers (deliberately not shared with app/main.py's private
# versions — these render a page with no session, and duplicating five lines
# is cheaper than coupling this module to main.py's internals)
# ---------------------------------------------------------------------------

def _valid_year(value, fallback: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return fallback


def _valid_month(value, fallback: int) -> int:
    try:
        m = int(value)
        return m if 1 <= m <= 12 else fallback
    except (TypeError, ValueError):
        return fallback


def _nav(year: int, month: int) -> dict:
    prev_m, prev_y = (12, year - 1) if month == 1 else (month - 1, year)
    next_m, next_y = (1, year + 1) if month == 12 else (month + 1, year)
    return {"prev_month": prev_m, "prev_year": prev_y, "next_month": next_m, "next_year": next_y}


def _carry_query(request: Request) -> str:
    parts = [
        f"{key}={quote(value)}" for key in _CARRY_PARAMS
        if (value := request.query_params.get(key))
    ]
    return ("&" + "&".join(parts)) if parts else ""


# ---------------------------------------------------------------------------
# view-option resolution — query param -> owner's share_links default -> app
# default, whitelisted at every tier, never erroring on an invalid value
# ---------------------------------------------------------------------------

def _resolve_endpoint(request: Request, share_row, settings):
    def _valid(key):
        return key if key in ENDPOINTS else None

    key = (
        _valid(request.query_params.get("endpoint"))
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


def _resolve_hide_not_watching(request: Request, share_row, settings) -> bool:
    raw = request.query_params.get("hidenw")
    if raw in ("0", "1"):
        return raw == "1"
    if share_row["hide_not_watching"] is not None:
        return bool(share_row["hide_not_watching"])
    return bool(settings.hide_not_watching)


def _resolve_networks(request: Request, share_row, settings) -> list[str] | None:
    if "networks" in request.query_params:
        names = [n.strip() for n in (request.query_params.get("networks") or "").split(",") if n.strip()]
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


def _resolve_tz(request: Request, share_row, settings) -> ZoneInfo:
    requested = (request.query_params.get("tz") or "").strip()
    if requested:
        try:
            return ZoneInfo(requested)
        except (ZoneInfoNotFoundError, ValueError):
            pass  # invalid -> owner default, never an error
    return _resolve_owner_tz(share_row, settings)


# ---------------------------------------------------------------------------
# the never-fetch read + render
# ---------------------------------------------------------------------------

def _not_found(request: Request) -> Response:
    return templates.TemplateResponse(request, "share_not_found.html", {"request": request}, status_code=404)


async def _render(request: Request, share_row) -> Response:
    if share_row is None:
        return _not_found(request)

    settings = load_settings()
    owner_id = int(share_row["user_id"])
    owner_prefs = await auth.get_user_prefs(owner_id)

    today = date.today()
    year = _valid_year(request.query_params.get("year"), today.year)
    month = _valid_month(request.query_params.get("month"), today.month)
    endpoint = _resolve_endpoint(request, share_row, settings)
    tz = _resolve_tz(request, share_row, settings)
    card_style = _resolve_choice(
        request.query_params.get("card"), share_row["card_style"], settings.card_style, _CARD_STYLES,
    )
    day_packing = _resolve_choice(
        request.query_params.get("packing"), share_row["day_packing"], settings.day_packing, _DAY_PACKINGS,
    )
    hide_not_watching = _resolve_hide_not_watching(request, share_row, settings)
    network_filter = _resolve_networks(request, share_row, settings)

    items: list[dict] = []
    as_of: int | None = None
    if settings.configured:
        # allow_fetch=False is the whole point (§1.9): a public visitor is
        # served whatever is already cached, even stale, even nothing, and
        # never triggers a Trakt call.
        items, as_of = await calendar_cache.read_month(
            endpoint, settings, tz=tz, year=year, month=month,
            genres=owner_prefs["genres"], countries=owner_prefs["countries"],
            network_filter=network_filter, allow_fetch=False,
        )

    nw_ids = await calendar_state.not_watching_ids(owner_id)
    visible: list[dict] = []
    for item in items:
        item = dict(item)
        item["not_watching"] = item["id"] in nw_ids
        if hide_not_watching and item["not_watching"]:
            continue
        visible.append(item)

    grouped = [
        {"date": day, "label": datetime.strptime(day, "%Y-%m-%d").strftime("%A, %d %B"), "items": list(rows)}
        for day, rows in groupby(visible, key=lambda i: i["air_date"])
    ]
    as_of_label = datetime.fromtimestamp(as_of, tz=tz).strftime("%Y-%m-%d %H:%M %Z") if as_of else None

    # Open Graph tags for link unfurlers (Discord/Slack/etc.). Both URLs are
    # absolute and built only from the configured public_base_url — never the
    # request Host (§1.16b) — since an unauthenticated crawler resolves relative
    # paths unreliably and a spoofed Host must not become the advertised origin.
    # Absent a configured base there is nothing safe to advertise, so the tags
    # are simply omitted and the link falls back to a bare text preview.
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
        "year": year,
        "month": month,
        "month_label": _calendar.month_name[month],
        "nav": _nav(year, month),
        "grouped": grouped,
        "total": len(visible),
        "view": {"card_style": card_style, "day_packing": day_packing},
        "as_of": as_of_label,
        "query_extra": _carry_query(request),
        # The visitor's own view controls. Everything they drive is a GET with
        # the same whitelisted params a hand-edited URL already carries, so they
        # add no write surface and need no session — they just save the visitor
        # from editing the query string by hand.
        "endpoints": endpoint_choices(),
        "endpoint_key": endpoint.key,
        "card_styles": _CARD_STYLES,
        "day_packings": _DAY_PACKINGS,
        "hide_not_watching": hide_not_watching,
        "timezone_groups": build_timezone_options(tz.key),
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
# CACHE-ONLY, so §1.9 holds: this never calls Trakt. The owner's own calendar
# views already fetch and cache each show's detail (cast, trailer, episodes);
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
    details = await trakt.fetch_details(settings, media, trakt_id, season, cache_only=True)
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
        # request-derived fallback (§1.16b), so the panel needs to say why the
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


async def _json_body(request: Request) -> dict | None:
    try:
        data = await request.json()
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


@guard.get("/api/me/share", AuthLevel.CALENDAR_APPROVED)
async def get_share(request: Request):
    user = await auth.current_user(request)
    row = await share_links.get_or_create(user.user_id)
    return JSONResponse(_share_payload(row, user.username, load_settings()))


@guard.post("/api/me/share/enabled", AuthLevel.CALENDAR_APPROVED)
async def post_share_enabled(request: Request):
    user = await auth.current_user(request)
    data = await _json_body(request)
    if data is None or data.get("kind") not in share_links.PREFERRED_KINDS:
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
    data = await _json_body(request)
    if data is None or data.get("kind") not in share_links.PREFERRED_KINDS:
        return JSONResponse({"ok": False, "error": "Expected {kind}"}, status_code=400)
    await share_links.set_active_kind(user.user_id, data["kind"])
    row = await share_links.get(user.user_id)
    return JSONResponse(_share_payload(row, user.username, load_settings()))


@guard.post("/api/me/share/preferred", AuthLevel.CALENDAR_APPROVED)
async def post_share_preferred(request: Request):
    user = await auth.current_user(request)
    data = await _json_body(request)
    if data is None or data.get("kind") not in share_links.PREFERRED_KINDS:
        return JSONResponse({"ok": False, "error": "Expected {kind}"}, status_code=400)
    await share_links.set_preferred_kind(user.user_id, data["kind"])
    row = await share_links.get(user.user_id)
    return JSONResponse(_share_payload(row, user.username, load_settings()))


@guard.post("/api/me/share/slug", AuthLevel.CALENDAR_APPROVED)
async def post_share_slug(request: Request):
    user = await auth.current_user(request)
    data = await _json_body(request)
    if data is None or "slug" not in data:
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
    data = await _json_body(request)
    if data is None or "view" not in data:
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
