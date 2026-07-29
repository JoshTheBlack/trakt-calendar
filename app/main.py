"""FastAPI application — served under Hypercorn.

Server-renders the same day-grouped poster grid as the original PHP app, plus a
JSON API for watch-state and front-end settings.
"""
from __future__ import annotations

import asyncio
import calendar
import dataclasses
import json
import logging
import os
import re
import time
from collections import Counter
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from math import ceil
from pathlib import Path
from urllib.parse import quote
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import anyio.to_thread
import httpx
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.gzip import GZipMiddleware

from . import admin_routes
from . import arr
from . import assets
from . import auth
from . import auth_routes
from . import authz
from . import cache
from . import calendar_cache
from . import calendar_state
from . import changelog
from . import db
from . import discord_fmt
from . import distrakt as distrakt_store
from . import distrakt_backfill
from . import encryption_flow
from . import encryption_routes
from . import logos
from . import artwork
from . import nav as nav_ctx
from . import plex_auth
from . import plex_routes
from . import posters
from . import ranker_routes
from . import secrets_backfill
from . import secrets_box
from . import seer
from . import share_links
from . import share_routes
from . import trakt_auth
from . import trakt_routes
from . import watch_history
from .auth import AuthLevel
from .perftrace import span
from .config import SECRET_FIELDS, Settings, apply_update, load_settings, public_base_url_error, save_settings
from .endpoints import DEFAULT_ENDPOINT, endpoint_choices, get_endpoint
from .timezones import build_options as build_timezone_options
from .trakt import (
    TraktError,
    TraktRateLimitError,
    fetch_details,
    fetch_season_detail,
    fetch_show_seasons,
    fetch_tile_info,
    fetch_watched_map,
    search_shows,
    search_titles,
)

logger = logging.getLogger(__name__)

# Configured here (not only in run.py) so `hypercorn app.main:app` — what the
# Docker image's CMD runs directly, bypassing run.py entirely — gets the same
# app.* diagnostics and Trakt-call tracing as the dev runner, instead of
# Python's silent WARNING-only default. LOG_LEVEL controls the app's own
# loggers (including "app.perf", which every outbound Trakt call logs a line
# to at DEBUG — see app/trakt.py, app/calendar_cache.py, app/trakt_auth.py);
# third-party libraries stay at WARNING regardless, since their own DEBUG
# output is rarely what anyone actually wants. basicConfig() only attaches a
# handler if the root logger doesn't already have one, so when run.py has
# already called it (the dev path) this is a no-op and run.py's config wins;
# in Docker, where nothing else calls it, this is the only config that fires.
logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logging.getLogger("app").setLevel(os.environ.get("LOG_LEVEL", "INFO").upper())

VERSION = "1.1.5"  # keep in sync with CHANGELOG.md
# Build metadata injected at Docker build time (GitHub Actions); "dev" for local runs.
BUILD = os.environ.get("APP_BUILD", "dev").strip() or "dev"
COMMIT = os.environ.get("APP_COMMIT", "").strip()
BUILD_LABEL = "dev" if BUILD == "dev" else f"build {BUILD}" + (f" · {COMMIT[:7]}" if COMMIT else "")

BASE_DIR = Path(__file__).resolve().parent
HEARTBEAT_SECONDS = 60

# How many of a month's day blocks the calendar page renders inline. The rest are
# fetched in one request once the page has painted, so a busy month's first
# response is a few dozen cards instead of a thousand — the document the browser
# has to parse before it can show anything is what made the page slow, not the
# cards further down it that nobody has scrolled to yet.
INITIAL_DAY_BLOCKS = 5


# Cache-busting token for every stylesheet and script. Lives in app/assets.py so
# the route modules imported below can use it too without importing this one back.
ASSET_VERSION = assets.ASSET_VERSION

# In-memory Sonarr/Radarr health, refreshed by a background heartbeat + on save.
INTEGRATION_HEALTH: dict[str, dict] = {
    "sonarr": {"configured": False, "reachable": False},
    "radarr": {"configured": False, "reachable": False},
    "seer": {"configured": False, "reachable": False},
}


async def refresh_integration_health() -> None:
    settings = load_settings()
    for kind in ("sonarr", "radarr"):
        INTEGRATION_HEALTH[kind] = await arr.check_health(kind, settings)
    INTEGRATION_HEALTH["seer"] = await seer.check_health(settings)


async def _warn_on_key_state(health: str) -> None:
    """One loud, actionable startup line for each unhealthy key state.

    The missing-key warning is worded as strongly as it is on purpose: the sealed
    values are intact and come back the instant the key is restored, but a well-meaning
    re-link or credential re-save overwrites them for good, so the one thing the
    operator must NOT do is exactly the thing that looks like the fix."""
    if health == encryption_flow.KEY_MISMATCH:
        logger.error(
            "ENCRYPTION_KEY does not match the stored secrets — the app is gated to the "
            "admin recovery screen. Restore the original key to get everything back, or "
            "run the recovery reset to discard the unrecoverable values.",
        )
        return
    if health == encryption_flow.KEY_MISSING:
        logger.error(
            "ENCRYPTION_KEY is not set but stored secrets are sealed. They are UNREADABLE "
            "but INTACT — restore ENCRYPTION_KEY to get them back. Do NOT re-link Trakt or "
            "re-save API keys while the key is missing: that overwrites the encrypted "
            "values and loses them for good.",
        )
        return
    warning = secrets_box.plaintext_storage_warning(await secrets_backfill.unsealed_present())
    if warning:
        logger.warning(warning)


# Cached "what's already in the library" id sets (TVDB for Sonarr, TMDB for Radarr/Seerr).
LIBRARY_CACHE: dict = {"sonarr": [], "radarr": [], "seer": [], "_ts": 0.0}
LIBRARY_TTL = 300  # seconds


async def refresh_library(force: bool = False) -> None:
    if not force and (time.time() - LIBRARY_CACHE["_ts"]) < LIBRARY_TTL:
        return
    settings = load_settings()
    LIBRARY_CACHE["sonarr"] = await arr.library_ids("sonarr", settings)
    LIBRARY_CACHE["radarr"] = await arr.library_ids("radarr", settings)
    LIBRARY_CACHE["seer"] = await seer.library_ids(settings)
    LIBRARY_CACHE["_ts"] = time.time()


async def _apply_new_trakt_token(settings: Settings, token: dict) -> Settings:
    """Write a fresh access/refresh token pair (from device-auth or a refresh
    call) into `settings` and persist it. Trakt issues a NEW refresh_token on
    every refresh — the old one stops working, so it must always be saved."""
    settings.trakt_access_token = token["access_token"]
    settings.trakt_refresh_token = token.get("refresh_token", "")
    settings.trakt_token_expires_at = int(token.get("created_at", time.time())) + int(token.get("expires_in", 0))
    save_settings(settings)
    return settings


async def _maybe_refresh_trakt_token() -> None:
    """Refresh the Trakt access token once it has actually expired.

    Runs on every heartbeat tick (cheap — just a timestamp comparison until the
    token is actually due), so the token renews itself in the background
    without the user having to notice or intervene.
    """
    settings = load_settings()
    if not (settings.trakt_client_id and settings.trakt_client_secret and settings.trakt_refresh_token):
        return
    if not settings.trakt_token_expires_at or time.time() < settings.trakt_token_expires_at:
        return
    try:
        token = await trakt_auth.refresh_access_token(
            settings.trakt_client_id, settings.trakt_client_secret, settings.trakt_refresh_token,
        )
    except (httpx.HTTPError, trakt_auth.TraktRateLimitError) as exc:
        # A rate-limited refresh is not an httpx error; catch it here too so the
        # background renewal just skips this cycle and tries again next tick rather
        # than letting the exception escape the heartbeat.
        logger.warning("Trakt token auto-refresh failed: %s", exc)
        return
    await _apply_new_trakt_token(settings, token)
    logger.info("Trakt token auto-refreshed (next expiry %s)", settings.trakt_token_expires_at)


async def _sweep_auth_rows() -> None:
    """Delete expired sessions, abandoned OAuth/PIN handshakes, and login/
    registration attempt rows old enough that no rate limiter still needs them.

    All three expire by a stored timestamp rather than by any self-expiring
    token, so without this sweep their rows would accumulate forever. Cheap
    indexed deletes.
    """
    now = db.now()
    await auth.sweep_expired_sessions(now)
    await db.execute("DELETE FROM auth_handshakes WHERE expires_at <= ?", (now,))
    await auth.sweep_login_attempts(now)
    # Age out expired cache windows and hold the shared blob table under its
    # size cap, evicting least-recently-stored first.
    settings = load_settings()
    await cache.sweep(now, settings.api_cache_max_bytes)
    # Drop poster-URL sightings past their retention window, and hold the
    # on-disk poster tile cache under its own size cap, oldest file first. The
    # tile sweep is filesystem walking, not a DB call, so it goes through a
    # worker thread rather than the event loop.
    await artwork.sweep(now)
    await anyio.to_thread.run_sync(posters.sweep, settings.poster_cache_max_bytes)


async def _heartbeat_loop() -> None:
    while True:
        try:
            await refresh_integration_health()
        except Exception:  # never let the heartbeat kill the loop
            pass
        try:
            await _maybe_refresh_trakt_token()
        except Exception:
            pass
        try:
            await _sweep_auth_rows()
        except Exception:
            pass
        try:
            await calendar_cache.prewarm_calendar_cache(load_settings())
        except Exception:
            pass
        await asyncio.sleep(HEARTBEAT_SECONDS)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    # Schema first — everything after this point may touch the database.
    await db.init()
    # The detail-lookup cache moved into the api_cache table; drop the old
    # data/cache/*.json directory now that the schema that replaces it is in place.
    cache.discard_legacy_dir()
    # Generated once and persisted: this UUID names the INSTALLATION to Plex,
    # not any particular user, and every PIN request needs it.
    await plex_auth.ensure_client_identifier()
    # Loud, once, at boot: a route nobody declared is being refused to every
    # caller, and the operator should hear about it here rather than from a user.
    authz.log_undeclared_routes(_app)
    # Turn encryption on non-interactively when the escape-hatch env var is set with
    # a valid key, then derive the key-health once so the request gate can steer an
    # administrator to recovery on a wrong key BEFORE any ordinary load hits a sealed
    # secret and raises. Both happen before the integration health check below, which
    # reads real secrets and would raise on a wrong key.
    try:
        await encryption_flow.run_env_escape_hatch()
    except Exception:
        logger.warning("Non-interactive encryption enable failed at startup", exc_info=True)
    health = await encryption_flow.refresh_health()
    await _warn_on_key_state(health)
    if health != encryption_flow.KEY_MISMATCH:
        await refresh_integration_health()
    task = asyncio.create_task(_heartbeat_loop())
    try:
        yield
    finally:
        task.cancel()
        import app.trakt as _trakt
        await _trakt.aclose_shared_client()


# Every load carries app.js/style.css/fonts plus (mostly) whatever asset_v was
# minted at the last deploy; a short max-age still saves a full refetch within a
# session without risking the "forgot to bump asset_v" staleness a long/immutable
# one would cause. ETags (StaticFiles' own default) still catch a change within
# that window.
_STATIC_CACHE_HEADERS = {"Cache-Control": "max-age=600"}

# Fonts are the one exception, and it is safe for a reason that does not hold for
# anything else under /static: a vendored woff2 cannot change without its FILENAME
# changing, because the name carries the version (inter-v20-latin-400). So there is
# no "forgot to bump asset_v" staleness to protect against — a new font is a new
# URL. At 600s a viewer who opens the calendar twice a day re-downloads ~86 KB both
# times and watches the text re-flow from the fallback face on each; a year makes
# that a true-cold-load-only event.
_FONT_CACHE_HEADERS = {"Cache-Control": "public, max-age=31536000, immutable"}


class _CachedStaticFiles(StaticFiles):
    def file_response(self, full_path, *args, **kwargs) -> Response:
        response = super().file_response(full_path, *args, **kwargs)
        headers = (_FONT_CACHE_HEADERS if Path(full_path).parent.name == "fonts"
                   else _STATIC_CACHE_HEADERS)
        response.headers.update(headers)
        return response


# The interactive API docs are off: they are a complete, unauthenticated
# inventory of every endpoint in the app, and nothing here is a public API that
# anyone consumes from a schema.
app = FastAPI(title="Trakt New Shows", lifespan=lifespan,
              docs_url=None, redoc_url=None, openapi_url=None)
# Added before authz.install() below, so it nests INSIDE the authz middleware
# stack (Starlette's registration order is reversed — see authz.install's own
# docstring) and compresses the actual route responses, including the
# multi-megabyte calendar HTML. authz's own short-circuit responses (redirects,
# 403s) are tiny, so shipping those uncompressed costs nothing.
app.add_middleware(GZipMiddleware)
app.mount("/static", _CachedStaticFiles(directory=BASE_DIR / "static"), name="static")
templates = Jinja2Templates(directory=BASE_DIR / "templates")

app.include_router(auth_routes.router)
app.include_router(trakt_routes.router)
app.include_router(plex_routes.router)
app.include_router(admin_routes.router)
app.include_router(encryption_routes.router)
app.include_router(share_routes.router)
app.include_router(ranker_routes.router)

# Every route below is registered through this, which requires an access level
# and refuses to register one without it.
guard = authz.Guard(app)
# Styles, scripts, images, and the easter egg's audio. Nothing here is derived
# from anyone's data.
authz.declare_mount(app, "/static", AuthLevel.PUBLIC)
authz.install(app)


# The status codes that get a rendered page rather than Starlette's bare
# {"detail": "Not Found"} JSON. Deliberately short: a wrong URL and a refused one
# are what a person typing in the address bar actually hits. Everything else
# keeps the default, because an unexpected status is worth seeing raw.
#
# The third field is the template. A mistyped address gets the intermission
# page — a dead end is the one place there is room to be charming. A 403 keeps
# the plain card: somebody who has just been told no is not in the mood, and the
# two pages stay deliberately similar in what they SAY (see below). Swapping
# error_lobby.html back to error.html here is the whole rollback.
_ERROR_PAGES: dict[int, tuple[str, str, str]] = {
    404: ("Not found", "There's nothing at this address. It may have moved, or the link may have been mistyped.", "error_lobby.html"),
    403: ("Not allowed", "Your account can't open this. If that seems wrong, ask an administrator to check your access.", "error.html"),
    405: ("Not found", "There's nothing at this address. It may have moved, or the link may have been mistyped.", "error_lobby.html"),
}


@app.exception_handler(StarletteHTTPException)
async def handle_http_exception(request: Request, exc: StarletteHTTPException) -> Response:
    """A themed page for a browser, the unchanged JSON for everything else.

    Split on Accept rather than on the path: /api/... is not the only thing a
    script calls, and a page is only ever useful to something that renders one.
    A 405 is folded into "not found" on purpose — telling a stranger that an
    address exists but takes a different method is an inventory of the app.
    """
    page = _ERROR_PAGES.get(exc.status_code)
    if page is None or "text/html" not in (request.headers.get("accept") or "").lower():
        detail = exc.detail if isinstance(exc.detail, dict) else {"error": str(exc.detail)}
        return JSONResponse({"ok": False, **detail}, status_code=exc.status_code,
                            headers=getattr(exc, "headers", None))
    title, message, template = page
    return templates.TemplateResponse(
        request, template,
        {"status": exc.status_code, "title": title, "message": message,
         "asset_v": ASSET_VERSION},
        status_code=exc.status_code,
    )


def _valid_month(value, fallback: int) -> int:
    try:
        m = int(value)
        return m if 1 <= m <= 12 else fallback
    except (TypeError, ValueError):
        return fallback


def _valid_year(value, fallback: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return fallback


def _nav(year: int, month: int) -> dict:
    prev_m, prev_y = (12, year - 1) if month == 1 else (month - 1, year)
    next_m, next_y = (1, year + 1) if month == 12 else (month + 1, year)
    return {"prev_month": prev_m, "prev_year": prev_y, "next_month": next_m, "next_year": next_y}


# Deliberately reachable by anyone: a container orchestrator's liveness probe
# carries no session, and the response says nothing about the instance.
@guard.get("/healthz", AuthLevel.PUBLIC)
async def healthz():
    return {"ok": True}


# SESSION rather than PUBLIC: release notes are not sensitive, but they are a
# feature inventory and a version history, and handing that to an unauthenticated
# visitor is a small gift to somebody probing the instance. Any signed-in account
# may read them, approved for anything or not — the menu entry sits on every page
# and gating it further would make the header a different shape per account.
@guard.get("/api/changelog", AuthLevel.SESSION)
async def api_changelog(request: Request):
    """The modal's body, as an HTML fragment. Fetched once on first open."""
    return templates.TemplateResponse(
        request, "_changelog.html", {"releases": changelog.releases()})


def _month_valid(value) -> bool:
    try:
        return 1 <= int(value) <= 12
    except (TypeError, ValueError):
        return False


def _picker_context(request: Request, settings, year: int, endpoint, user=None):
    today = date.today()
    return {
        "request": request,
        "year": year,
        "endpoint": endpoint,
        # The picker carries the same navigation the calendar does — it is a
        # landing page people arrive on directly, and having no way from here to
        # the account or admin screens made those reachable only by typing a URL.
        "endpoints": endpoint_choices(),
        **nav_ctx.nav_context(user),
        "months": [{"num": m, "name": calendar.month_name[m]} for m in range(1, 13)],
        "current_month": today.month if year == today.year else None,
        "today_month": today.month,
        "today_year": today.year,
        "version": VERSION,
        "build": BUILD_LABEL,
        "asset_v": ASSET_VERSION,
    }


def _resolve_viewer_tz(user, settings) -> ZoneInfo:
    """The viewer's saved timezone, falling back to the app-wide default and then
    UTC if either name turns out to be unusable (e.g. a stale settings.json value
    predating a tzdata rename)."""
    for name in (user.timezone, settings.timezone, "UTC"):
        if not name:
            continue
        try:
            return ZoneInfo(name)
        except (ZoneInfoNotFoundError, ValueError):
            continue
    return ZoneInfo("UTC")


# How many columns a packed day's grid may grow to, per card style: the poster
# wall is compact, "poster beside" cards are wide. Mirrored by updateCols() in
# app.js, which re-runs this per day after a toggle changes what is visible.
_COLUMN_CAPS = {"poster": 6, "horizontal": 2}
_COLUMN_CAP_DEFAULT = 5


def _apply_day_layout(grouped: list[dict], *, not_watching: set[str],
                      hide_not_watching: bool, card_style: str) -> None:
    """Annotate each day with the two presentation facts the client used to work
    out only AFTER the page had painted: how many columns that day's grid needs,
    and whether the day collapses entirely because everything on it is hidden.

    Both are decided from the cards the day contains, which the server knows
    before it writes them. Left to the client they land a frame late, and a day
    header that paints and then vanishes takes the rest of the month up the page
    with it — the "1 July, then suddenly 2 July" flicker on a hide-mode load.

    `rows` and `visible` are the same answer put to a different use: a day that
    has not been fetched yet is drawn as a placeholder, and it can only reserve
    the right amount of vertical space (so the scrollbar and the jump-to strip
    land where the day really is) if it knows how many rows of cards are coming."""
    cap = _COLUMN_CAPS.get(card_style, _COLUMN_CAP_DEFAULT)
    for group in grouped:
        visible = sum(1 for item in group["items"] if item["id"] not in not_watching)
        shown = visible if hide_not_watching else len(group["items"])
        group["cols"] = max(1, min(shown, cap))
        group["collapsed"] = hide_not_watching and visible == 0
        group["visible"] = visible
        group["rows"] = ceil(shown / group["cols"]) if shown else 1


def _filters_active(prefs: dict) -> bool:
    return bool(
        prefs["genres"] or prefs["countries"] or prefs["network_filter"]
        or prefs["show_certifications"] or prefs["movie_certifications"]
    )


def _filters_summary(prefs: dict) -> str:
    """"genre, country" — which dimensions are narrowing this calendar, for the
    header button's tooltip. Names the dimensions rather than the values, which
    can run to dozens of networks and would not fit. Show and movie
    certifications share one "certification" label — the endpoint decides which
    of the two specs is actually in play, but the tooltip is naming a dimension,
    not a value, so it does not need to distinguish them."""
    named = [
        label for label, value in (
            ("genre", prefs["genres"]),
            ("country", prefs["countries"]),
            ("certification", prefs["show_certifications"] or prefs["movie_certifications"]),
            ("network", prefs["network_filter"]),
        ) if value
    ]
    return ", ".join(named)


def _requested_endpoint(request: Request, prefs: dict, settings):
    """The calendar this request is about. Always resolved through get_endpoint,
    which falls back to the default for anything not in the calendar set, so a
    made-up key can never reach a cache row or a Trakt URL."""
    return get_endpoint(
        request.query_params.get("endpoint") or prefs["endpoint"] or settings.endpoint or DEFAULT_ENDPOINT
    )


@guard.get("/", AuthLevel.CALENDAR_APPROVED)
async def home(request: Request):
    """The month/year picker landing page (as the original front page was).

    A `month` in the query is an old calendar link — a bookmark, a shared URL, or
    a Discord post from when this one route served both the picker and the
    calendar. Forward it rather than asking someone to choose the month they
    already named."""
    settings = load_settings()
    # Already resolved and cached by the dependency that let this request in.
    user = await auth.current_user(request)
    prefs = await auth.get_user_prefs(user.user_id)
    year = _valid_year(request.query_params.get("year"), date.today().year)
    endpoint = _requested_endpoint(request, prefs, settings)
    if _month_valid(request.query_params.get("month")):
        month = _valid_month(request.query_params.get("month"), date.today().month)
        return RedirectResponse(
            f"/calendar?month={month}&year={year}&endpoint={endpoint.key}", status_code=302)
    return templates.TemplateResponse(
        request, "pick.html", _picker_context(request, settings, year, endpoint, user))


@guard.get("/calendar", AuthLevel.CALENDAR_APPROVED)
async def calendar_page(request: Request):
    """The calendar SHELL: the header, the stats bar, the jump-to strip, the
    modals — and the first few days of cards inline. Every day after those is
    announced as a placeholder that fetches itself from /calendar/day when the
    viewer reaches it, so the browser has a usable page long before a whole month
    of cards exists, and a day nobody scrolls to is never built at all.

    The month is still assembled in full here, because the numbers the shell
    states (the tiles, the per-day chip counts, which shows are new) are claims
    about the WHOLE month and would be wrong if they described only the days that
    happen to be rendered."""
    settings = load_settings()
    user = await auth.current_user(request)
    is_admin = bool(user and user.is_admin)
    prefs = await auth.get_user_prefs(user.user_id)
    today = date.today()
    year = _valid_year(request.query_params.get("year"), today.year)
    endpoint = _requested_endpoint(request, prefs, settings)
    month = _valid_month(request.query_params.get("month"), today.month)
    tz = _resolve_viewer_tz(user, settings)
    days = calendar.monthrange(year, month)[1]

    # This viewer's marks, read ONCE and handed to the assembly so the cards come
    # out of the template already carrying the class. The client used to add it
    # after the page had painted, which is what made hidden items visibly pop out.
    not_watching = await calendar_state.not_watching_ids(user.user_id)

    grouped: list[dict] = []
    total = 0
    watching = 0
    not_watching_count = 0
    partial = False
    new_ids: set[str] = set()
    delta = {"text": "", "kind": "none"}
    history: list[dict] = []
    show_counts: dict[str, int] = {}
    error: str | None = None
    if not settings.configured:
        error = "Trakt API credentials aren't set yet. Open ⚙️ Settings to add your Client ID and Access Token."
    else:
        try:
            # The whole month is fetched, filtered, and grouped before any HTML is
            # sent, so this span is the server-side "time to first byte" for the
            # calendar — dominated by the per-window Trakt fetch on a cold cache
            # (now concurrent across the windows, not one await at a time).
            with span("calendar.read_month", endpoint=endpoint.key, ym=f"{year}-{month:02d}") as sp:
                grouped, meta = await calendar_cache.assemble_range(
                    endpoint, settings, tz=tz,
                    start_date=date(year, month, 1), end_date=date(year, month, days),
                    genres=prefs["genres"], countries=prefs["countries"],
                    show_certifications=prefs["show_certifications"],
                    movie_certifications=prefs["movie_certifications"],
                    network_filter=prefs["network_filter"] or None,
                    not_watching_ids=not_watching,
                )
                sp.set(items=meta["total"])
            total = meta["total"]
            watching = meta["watching"]
            not_watching_count = meta["not_watching"]
            # A window Trakt couldn't supply is skipped rather than failing the
            # whole month; flag it so the page can say the month is incomplete
            # instead of silently showing a short one.
            partial = meta["partial"]
            # How many cards each show has this month. The stats tiles need it to
            # keep counting correctly when one toggle flips a show that airs on a
            # dozen days — without asking the DOM, which only ever knows about the
            # cards it currently holds.
            show_counts = Counter(item["id"] for group in grouped for item in group["items"])
            # The is-new diff and its baseline commit belong to whoever produced
            # the cards, over the SERVER's full id list. Skipped on the error
            # paths below: committing an empty month as the baseline would make
            # the whole month look new the next time it loads properly.
            view_state = await calendar_state.resolve_view(
                user.user_id, endpoint.key, year, month,
                show_ids=meta["show_ids"], total=total, now=datetime.now(tz),
            )
            new_ids = view_state["new_ids"]
            delta = view_state["delta"]
            history = view_state["history"]
        except TraktError as exc:
            error = str(exc)

    counts_by_date = {group["date"]: len(group["items"]) for group in grouped}
    # What each day will actually SHOW this viewer. With hide-not-watching on, a
    # day whose every item is marked renders nothing at all, so its chip must not
    # offer to scroll somewhere blank. app.js keeps this in step when the viewer
    # toggles hiding or marks a show without reloading.
    shown_by_date = {
        group["date"]: sum(1 for item in group["items"] if item["id"] not in not_watching)
        for group in grouped
    } if prefs["hide_not_watching"] else counts_by_date

    _apply_day_layout(grouped, not_watching=not_watching,
                      hide_not_watching=prefs["hide_not_watching"],
                      card_style=prefs["card_style"] or settings.card_style)

    # Only the first few days go out with the shell; every day after them is a
    # placeholder that fetches its own cards when it is scrolled to. So first
    # paint costs a handful of cards instead of a month of them, and a day nobody
    # ever scrolls to is never assembled, rendered, or shipped at all.
    #
    # The split is by DAY BLOCK rather than by date, because a month can open with
    # a run of empty days and "the first five dates" would then ship nothing.
    inline_groups = grouped[:INITIAL_DAY_BLOCKS]
    skeleton_groups = grouped[INITIAL_DAY_BLOCKS:]
    for group in skeleton_groups:
        group["url"] = _day_url(endpoint.key, date.fromisoformat(group["date"]))

    # Per-user view preferences (card style, day packing, hide-not-watching) —
    # distinct from `settings`, which stays the app-wide defaults new accounts
    # are seeded from and the admin Settings screen's own values.
    view = {
        "card_style": prefs["card_style"] or settings.card_style,
        "day_packing": prefs["day_packing"] or settings.day_packing,
        "hide_not_watching": prefs["hide_not_watching"],
        # Whether this viewer's month has been narrowed, and by what. The header
        # button reads both: a filter's only other evidence is the shows that
        # aren't there, which is indistinguishable from Trakt not listing them.
        "filters_active": _filters_active(prefs),
        "filters_summary": _filters_summary(prefs),
    }

    context = {
        "request": request,
        "settings": settings,
        "view": view,
        "endpoint": endpoint,
        "endpoints": endpoint_choices(),
        "timezone_groups": build_timezone_options(settings.timezone),
        "viewer_timezone_groups": build_timezone_options(tz.key),
        "year": year,
        "month": month,
        "month_label": calendar.month_name[month],
        # For the Share panel's "opens on" month picker, which names months
        # rather than numbering them.
        "month_names": [calendar.month_name[m] for m in range(1, 13)],
        "nav": _nav(year, month),
        # The days rendered INLINE. `grouped` (the whole month) is what every
        # number on the page is computed from; this is only what is painted now.
        "grouped": inline_groups,
        # The days that are announced but not yet fetched: header, chip target and
        # reserved height now, cards when the viewer reaches them.
        "skeletons": skeleton_groups,
        "total": total,
        # The stats tiles, the is-new marks, the "since last run" line and the
        # history log are all computed above and rendered with the page, so they
        # are right at first paint and stay right when only part of a month is on
        # screen. The card partial reads these two sets by membership.
        "not_watching": not_watching,
        "new_ids": new_ids,
        "stats": {"total": total, "watching": watching, "not_watching": not_watching_count},
        "delta": delta,
        "history": history,
        # The same numbers again as data rather than markup, so the client can
        # keep the tiles honest through a toggle (and so a per-day render can mark
        # is-new from the whole month's answer instead of recomputing it).
        "view_data": {
            "newIds": sorted(new_ids),
            "showCounts": dict(show_counts),
            "notWatching": sorted(nw for nw in not_watching if nw in show_counts),
            "watching": watching,
            "notWatchingCount": not_watching_count,
        },
        # One chip per day of the month for the jump-to strip. `count` is what the
        # day holds; `shown` is what this viewer will see of it, and a day showing
        # nothing has no section to scroll to, so its chip renders inert.
        "day_chips": [
            {"day": day,
             "date": f"{year}-{month:02d}-{day:02d}",
             "count": counts_by_date.get(f"{year}-{month:02d}-{day:02d}", 0),
             "shown": shown_by_date.get(f"{year}-{month:02d}-{day:02d}", 0)}
            for day in range(1, days + 1)
        ],
        "error": error,
        # A non-fatal warning distinct from `error`: the month rendered, but at
        # least one window's data couldn't be loaded, so it may be missing days.
        "partial": partial,
        "generated": datetime.now().strftime("%H:%M"),
        # Sonarr/Radarr/Seerr writes land in the operator's own shared libraries
        # and Seerr's requests all carry one app-wide API key, so they are an
        # administrator's affordance. The buttons and health state are left out
        # of the page entirely for everyone else rather than rendered into a
        # guaranteed 403.
        # is_admin, calendar_available and ranker_available for the shared header.
        **nav_ctx.nav_context(user),
        # The same two conditions the tracker's own access level enforces, asked
        # here so the easter egg knows whether it has anywhere to send this
        # person. Resolved from the session rather than probed over HTTP: an
        # endpoint answering "may I?" is itself a disclosure that there is
        # something to be allowed into. Note this gates the REVEAL, not the menu
        # item — see _nav.html.
        "distrakt_available": bool(user and user.distrakt_approved and user.has_trakt_identity),
        "integrations": INTEGRATION_HEALTH if is_admin else {},
        "version": VERSION,
        "build": BUILD_LABEL,
        "asset_v": ASSET_VERSION,
    }
    # Jinja renders eagerly when the response is built, so this span is the cost of
    # turning the shell's cards into HTML — the other half of the server's blocking
    # time before the browser gets anything. It is now bounded by the inline day
    # count rather than growing with the whole month.
    with span("calendar.render", cards=sum(len(g["items"]) for g in inline_groups)):
        response = templates.TemplateResponse(request, "index.html", context)
    return response


_YMD = re.compile(r"\d{4}-\d{2}-\d{2}")


def _month_date(value, year: int, month: int) -> date | None:
    """A YYYY-MM-DD query parameter, accepted ONLY if it is exactly that shape and
    falls inside {year, month}. Anything else is None, and the caller refuses the
    request: this value reaches date arithmetic and a cache read, so it is checked
    against the month being viewed rather than merely parsed."""
    # Matched before parsing rather than left to fromisoformat, which also accepts
    # ISO week dates ("2026-W28-1") — a second spelling of the same day is one more
    # shape of input reaching a cache key for no benefit.
    if not isinstance(value, str) or not _YMD.fullmatch(value):
        return None
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        return None
    if (parsed.year, parsed.month) != (year, month):
        return None
    return parsed


def _day_url(endpoint_key: str, day: date) -> str:
    """The content request for one day. Built in one place because the shell's
    placeholder and the retry button on a day that failed must ask for exactly the
    same thing."""
    return (f"/calendar/day?endpoint={quote(endpoint_key)}"
            f"&year={day.year}&month={day.month}&date={day.isoformat()}")


@guard.get("/calendar/day", AuthLevel.CALENDAR_APPROVED)
async def calendar_day(request: Request):
    """ONE day's block — the content half of the calendar, rendered through the
    same day-block and card partials the shell uses, so a day that arrives this
    way is byte-identical to one the shell rendered inline.

    Only that day is assembled: the covering window(s) are read and only their
    entries are normalized, so a month the viewer never scrolls through is never
    built. A viewer-local day can straddle two UTC windows, which assemble_range
    already accounts for.

    Everything that decides WHAT this viewer may see comes from their session: the
    per-user filters and their not-watching marks are read here, never taken from
    the query, so this cannot be asked for someone else's view or for an
    unfiltered day. The query only says WHICH day of WHICH calendar, and both are
    validated before they reach a cache key.

    is-new is deliberately NOT computed here. The diff and its baseline commit are
    a whole-month decision the shell already made and wrote; re-reading the
    baseline from a fragment would see the shell's own commit and conclude nothing
    is new. The shell embeds the ids it decided were new and the page marks these
    cards from that one answer."""
    settings = load_settings()
    user = await auth.current_user(request)
    prefs = await auth.get_user_prefs(user.user_id)
    today = date.today()
    year = _valid_year(request.query_params.get("year"), today.year)
    month = _valid_month(request.query_params.get("month"), today.month)
    endpoint = _requested_endpoint(request, prefs, settings)
    day = _month_date(request.query_params.get("date"), year, month)
    if day is None:
        return JSONResponse({"ok": False, "error": "Invalid date"}, status_code=400)
    if not settings.configured:
        return JSONResponse({"ok": False, "error": "Not configured"}, status_code=400)

    tz = _resolve_viewer_tz(user, settings)
    not_watching = await calendar_state.not_watching_ids(user.user_id)
    context = {
        "request": request, "not_watching": not_watching,
        # Empty on purpose: see the docstring — the shell owns the is-new answer.
        "new_ids": set(),
        "settings": settings, "is_admin": bool(user and user.is_admin),
        "date": day.isoformat(), "label": calendar_cache.day_label(day),
        "retry_url": _day_url(endpoint.key, day),
        "partial": False,
    }
    try:
        with span("calendar.day", endpoint=endpoint.key, day=day.isoformat()) as sp:
            grouped, meta = await calendar_cache.assemble_range(
                endpoint, settings, tz=tz, start_date=day, end_date=day,
                genres=prefs["genres"], countries=prefs["countries"],
                show_certifications=prefs["show_certifications"],
                movie_certifications=prefs["movie_certifications"],
                network_filter=prefs["network_filter"] or None,
                not_watching_ids=not_watching,
            )
            sp.set(items=meta["total"])
        # Same per-day presentation the shell's own blocks were rendered with, so a
        # day that arrives late is laid out correctly on arrival rather than being
        # re-packed (and, in hide mode, collapsed) a frame after it appears.
        _apply_day_layout(grouped, not_watching=not_watching,
                          hide_not_watching=prefs["hide_not_watching"],
                          card_style=prefs["card_style"] or settings.card_style)
    except TraktError as exc:
        # The shell is already on screen with the month's real numbers, so one day
        # failing is a gap in the month, not a broken page. It replaces itself with
        # a block that says so and offers to try again, rather than sitting there
        # as a placeholder that looks like it is still loading.
        return templates.TemplateResponse(
            request, "_day_fragment.html", {**context, "group": None, "error": str(exc)})

    return templates.TemplateResponse(
        request, "_day_fragment.html",
        # A day with nothing on it groups to nothing; the fragment then renders
        # empty and the placeholder it replaces simply disappears, which is what an
        # empty day should look like.
        {**context, "group": grouped[0] if grouped else None,
         "error": None, "partial": meta["partial"]},
    )


@guard.get("/distrakt", AuthLevel.DISTRAKT_APPROVED)
async def distrakt(request: Request):
    """Hidden Discord-tracker page, reached through an easter egg rather than any
    link in the UI.

    Renders the shell for the requested {year, month}; the page's JS fetches the
    computed month via /api/distrakt/month (which lazily rolls the month over).
    Month-nav prev/next mirror the main calendar's nav (see index.html)."""
    today = date.today()
    settings = load_settings()
    user = await auth.current_user(request)
    year = _valid_year(request.query_params.get("year"), today.year)
    month = _valid_month(request.query_params.get("month"), today.month)
    network_emojis, default_network_emoji = await distrakt_store.get_emoji_prefs(user.user_id)
    context = {
        "request": request,
        "year": year,
        "month": month,
        "nav": _nav(year, month),
        # For the announcement post's "which calendar view does the embedded link
        # open on" selector; the same list the calendar's endpoint picker uses.
        "endpoints": endpoint_choices(),
        **nav_ctx.nav_context(user),
        # This user's OWN map — it renders into their Discord posts and nobody
        # else's. Rendered in rather than fetched because the roster rows fall
        # back to these emoji whenever a network has no logo.
        "network_emojis": network_emojis,
        "default_network_emoji": default_network_emoji,
        "version": VERSION,
        "build": BUILD_LABEL,
        "asset_v": ASSET_VERSION,
    }
    return templates.TemplateResponse(request, "distrakt.html", context)


@guard.get("/pick", AuthLevel.CALENDAR_APPROVED)
async def pick(request: Request):
    """Month/year selector landing page (carried over from the original front page)."""
    settings = load_settings()
    user = await auth.current_user(request)
    prefs = await auth.get_user_prefs(user.user_id)
    year = _valid_year(request.query_params.get("year"), date.today().year)
    endpoint = get_endpoint(
        request.query_params.get("endpoint") or prefs["endpoint"] or settings.endpoint or DEFAULT_ENDPOINT
    )
    return templates.TemplateResponse(
        request, "pick.html", _picker_context(request, settings, year, endpoint, user))


def _season_param(value) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


@guard.get("/api/tile", AuthLevel.CALENDAR_APPROVED)
async def api_tile(request: Request):
    """Compact season info for a tile."""
    settings = load_settings()
    if not settings.configured:
        return JSONResponse({"ok": False, "error": "Not configured"}, status_code=400)
    media = request.query_params.get("media", "show")
    trakt_id = request.query_params.get("id")
    if not trakt_id:
        return JSONResponse({"ok": False, "error": "Missing id"}, status_code=400)
    try:
        info = await fetch_tile_info(settings, media, trakt_id, _season_param(request.query_params.get("season")))
    except TraktError as exc:
        # A transport failure (rate-limit or unreachable) now raises rather than
        # returning a benign empty tile, so a 429 can't render as "no episodes".
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=exc.status or 502)
    return JSONResponse({"ok": True, **info})


@guard.get("/api/details", AuthLevel.CALENDAR_APPROVED)
async def api_details(request: Request):
    """Full detail payload for the modal."""
    settings = load_settings()
    if not settings.configured:
        return JSONResponse({"ok": False, "error": "Not configured"}, status_code=400)
    media = request.query_params.get("media", "show")
    trakt_id = request.query_params.get("id")
    if not trakt_id:
        return JSONResponse({"ok": False, "error": "Missing id"}, status_code=400)
    try:
        details = await fetch_details(settings, media, trakt_id, _season_param(request.query_params.get("season")))
    except TraktError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=exc.status or 502)
    return JSONResponse({"ok": True, **details})


@guard.get("/api/state", AuthLevel.CALENDAR_APPROVED)
async def get_state(request: Request):
    user = await auth.current_user(request)
    today = date.today()
    year = _valid_year(request.query_params.get("year"), today.year)
    month = _valid_month(request.query_params.get("month"), today.month)
    endpoint = get_endpoint(request.query_params.get("endpoint"))
    return JSONResponse(await calendar_state.load_state(user.user_id, endpoint.key, year, month))


@guard.post("/api/state", AuthLevel.CALENDAR_APPROVED)
async def post_state(request: Request):
    """A DELTA endpoint, not a whole-document replace.

    Two independent payload shapes, dispatched on which keys are present:
    `{item_id, not_watching}` toggles a single SHOW for this user everywhere, and
    `{last_count, last_show_ids, history?}` records this load's change-detection
    baseline for the endpoint/year/month in the query string. Sending only the
    piece that actually changed — instead of the whole notWatching array — is
    what stops one open tab's save from clobbering a mark a second tab just made;
    each write is its own INSERT/DELETE or UPDATE, not a read-modify-write of a
    shared document.

    The endpoint/year/month params are read for the baseline only. A not-watching
    mark has no month in it: it is a statement about the show.
    """
    user = await auth.current_user(request)
    today = date.today()
    year = _valid_year(request.query_params.get("year"), today.year)
    month = _valid_month(request.query_params.get("month"), today.month)
    endpoint = get_endpoint(request.query_params.get("endpoint"))
    try:
        payload = await request.json()
    except ValueError:
        return JSONResponse({"ok": False, "error": "Invalid JSON body"}, status_code=400)
    if not isinstance(payload, dict):
        return JSONResponse({"ok": False, "error": "Invalid JSON body"}, status_code=400)

    if "item_id" in payload:
        item_id = str(payload.get("item_id") or "")
        if not item_id:
            return JSONResponse({"ok": False, "error": "Missing item_id"}, status_code=400)
        await calendar_state.set_not_watching(
            user.user_id, item_id, bool(payload.get("not_watching")),
        )
        return JSONResponse({"ok": True})

    if "last_count" in payload or "last_show_ids" in payload:
        last_count = payload.get("last_count")
        try:
            last_count = int(last_count) if last_count is not None else None
        except (TypeError, ValueError):
            return JSONResponse({"ok": False, "error": "last_count must be a whole number"}, status_code=400)
        last_show_ids = payload.get("last_show_ids")
        if last_show_ids is not None and not isinstance(last_show_ids, list):
            return JSONResponse({"ok": False, "error": "last_show_ids must be a list"}, status_code=400)
        history = payload.get("history")
        if history is not None and not isinstance(history, list):
            return JSONResponse({"ok": False, "error": "history must be a list"}, status_code=400)
        await calendar_state.set_view_state(
            user.user_id, endpoint.key, year, month,
            last_count=last_count, last_show_ids=last_show_ids, history=history,
        )
        return JSONResponse({"ok": True})

    return JSONResponse(
        {"ok": False, "error": "Expected item_id/not_watching or last_count/last_show_ids"},
        status_code=400,
    )


_CARD_STYLES = ("vertical", "horizontal", "poster")
_DAY_PACKINGS = ("stacked", "packed")


def _filter_spec(value) -> str:
    """Normalize a `-anime, drama` genre/country spec to comma-separated tokens.

    Only tidying — app/calendar_filter.py lowercases and splits on ',' itself, so
    this exists to keep what the user typed from being echoed back with the empty
    tokens and ragged spacing a half-edited list leaves behind.
    """
    return ", ".join(t for t in (s.strip() for s in str(value or "").split(",")) if t)


def _network_list(value) -> list[str]:
    """Networks as a de-duplicated list, from either a JSON array or the comma
    string the textarea produces. Names are matched exactly on the read path, so
    they keep their case; the duplicate check does not."""
    raw = [str(v) for v in value] if isinstance(value, list) else str(value or "").split(",")
    seen: set[str] = set()
    names: list[str] = []
    for name in (item.strip() for item in raw):
        if name and name.lower() not in seen:
            seen.add(name.lower())
            names.append(name)
    return names


@guard.get("/api/me/prefs", AuthLevel.CALENDAR_APPROVED)
async def get_me_prefs(request: Request):
    """The viewer's own view preferences, for the Filters panel to populate from.

    Separate from /api/settings, which is ADMIN-only and describes the instance.
    These are the values that actually filter this person's calendar, so every
    signed-in account has to be able to read them back.
    """
    user = await auth.current_user(request)
    prefs = await auth.get_user_prefs(user.user_id)
    return JSONResponse({"ok": True, "prefs": prefs})


@guard.post("/api/me/prefs", AuthLevel.CALENDAR_APPROVED)
async def post_me_prefs(request: Request):
    """Persist a partial update to the viewer's own calendar view preferences.

    Card style, day packing, and hide-not-watching used to write settings.json —
    an admin-only file — so anyone else's choice applied for one page load and
    was gone on the next. This writes user_prefs instead, so it sticks for every
    account. The genre/country/network filters are here for the same reason: they
    were only editable on the admin Settings screen, which wrote the app-wide
    seed and so changed nothing for the admin's own calendar and offered nobody
    else any way to filter at all.

    Also mirrors into share_links when the user has ever opened the Share panel:
    that table's owner-default columns are seeded from user_prefs at creation and
    otherwise have no editor of their own, so keeping them in sync here is what
    makes a public share page track the owner's own view without a second save
    action (see app/share_links.py's module docstring).
    """
    user = await auth.current_user(request)
    try:
        data = await request.json()
    except ValueError:
        return JSONResponse({"ok": False, "error": "Invalid JSON body"}, status_code=400)
    if not isinstance(data, dict):
        return JSONResponse({"ok": False, "error": "Invalid JSON body"}, status_code=400)

    updates: dict = {}
    if "card_style" in data and data["card_style"] in _CARD_STYLES:
        updates["card_style"] = data["card_style"]
    if "day_packing" in data and data["day_packing"] in _DAY_PACKINGS:
        updates["day_packing"] = data["day_packing"]
    if "hide_not_watching" in data:
        updates["hide_not_watching"] = bool(data["hide_not_watching"])
    # Present-but-empty is a real value here — it is how a filter is CLEARED —
    # so these key off presence rather than truthiness.
    if "genres" in data:
        updates["genres"] = _filter_spec(data["genres"])
    if "countries" in data:
        updates["countries"] = _filter_spec(data["countries"])
    if "show_certifications" in data:
        updates["show_certifications"] = _filter_spec(data["show_certifications"])
    if "movie_certifications" in data:
        updates["movie_certifications"] = _filter_spec(data["movie_certifications"])
    if "network_filter" in data:
        updates["network_filter"] = _network_list(data["network_filter"])
    if not updates:
        return JSONResponse({"ok": False, "error": "Nothing to update"}, status_code=400)
    await auth.update_user_prefs(user.user_id, **updates)
    if await share_links.get(user.user_id) is not None:
        await share_links.update_owner_defaults(user.user_id, **updates)
    return JSONResponse({"ok": True})


@guard.post("/api/me/timezone", AuthLevel.CALENDAR_APPROVED)
async def post_me_timezone(request: Request):
    """Persist the viewer's calendar timezone.

    No automatic browser detection — this is reached either by picking a
    zone from the header's dropdown, or by the "use my device timezone" button
    filling in Intl's resolved zone name before the same request fires.
    """
    user = await auth.current_user(request)
    try:
        data = await request.json()
    except ValueError:
        return JSONResponse({"ok": False, "error": "Invalid JSON body"}, status_code=400)
    tz_name = str((data or {}).get("timezone") or "").strip()
    if not tz_name:
        return JSONResponse({"ok": False, "error": "Missing timezone"}, status_code=400)
    try:
        ZoneInfo(tz_name)
    except (ZoneInfoNotFoundError, ValueError):
        return JSONResponse({"ok": False, "error": "Unknown timezone"}, status_code=400)
    await auth.set_user_timezone(user.user_id, tz_name)
    # Mirrored into share_links the same way post_me_prefs does, once the user
    # has a share row at all — see that route's docstring.
    if await share_links.get(user.user_id) is not None:
        await share_links.update_owner_defaults(user.user_id, timezone=tz_name)
    return JSONResponse({"ok": True})


# `private` rather than `public`: this response now requires a session, so a
# shared cache in front of the app has no business holding a copy.
_LOGO_CACHE_HEADERS = {"Cache-Control": "private, max-age=86400"}


@guard.get("/api/network-logo", AuthLevel.CALENDAR_APPROVED)
async def api_network_logo(request: Request):
    """A processed network-logo PNG tile for a network name (calendar + distrakt).

    Calendar-level, which is where these are overwhelmingly requested from. The
    distrakt page shows them too, so a user approved for distrakt but NOT for the
    calendar sees the emoji fallback there instead of logos — the same thing that
    happens for any network without a logo, and not worth a second access level.

    Generates it from TMDB on first request when `tmdb` is supplied and a TMDB key
    is set; serves the disk cache thereafter. 404 -> the caller falls back to the
    emoji/text tag."""
    name = (request.query_params.get("name") or "").strip()
    tmdb = request.query_params.get("tmdb")
    if not name:
        return Response(status_code=404)
    path = logos.cached_tile(name)
    if path is None and not logos.is_negative(name):
        path = await logos.ensure_logo(load_settings(), name, tmdb)
    if path is None or not path.exists():
        return Response(status_code=404, headers=_LOGO_CACHE_HEADERS)
    # ?download=1 -> attachment (for the emoji-map "download logo" button).
    filename = f"{logos._slug(name)}.png" if request.query_params.get("download") else None
    return FileResponse(path, media_type="image/png", filename=filename, headers=_LOGO_CACHE_HEADERS)


@guard.post("/api/network-logo/regenerate", AuthLevel.ADMIN)
async def api_network_logo_regenerate(request: Request):
    """Drop a single network's cached logo and re-resolve it from TMDB."""
    try:
        data = await request.json()
    except ValueError:
        data = {}
    name = (data.get("name") or "").strip()
    tmdb = data.get("tmdb")
    if not name:
        return JSONResponse({"ok": False, "error": "Missing network name"}, status_code=400)
    logos.delete(name)
    path = await logos.ensure_logo(load_settings(), name, tmdb)
    return JSONResponse({"ok": True, "network": name, "generated": bool(path and path.exists())})


@guard.get("/api/settings", AuthLevel.ADMIN)
async def get_settings(request: Request):
    """Configuration for the Settings screen, WITHOUT any credential in it.

    Credentials are write-only over this API: the response carries a flag per
    secret saying whether one is stored, never the value. This route used to hand
    the Trakt access token, the Trakt client secret, the TMDB key, and every
    Sonarr/Radarr/Seerr API key to whoever asked for it.
    """
    settings = load_settings()
    peer = (request.client.host if request.client else "") or ""
    admin = await auth.current_user(request)
    return JSONResponse({
        **settings.redacted(),
        # What `trusted_proxy_ips` has to cover, shown beside that field so the
        # operator can read the answer off the screen instead of guessing their
        # container network. This is the IMMEDIATE peer — the reverse proxy on a
        # real deployment — not the forwarded client address.
        "detected_peer_ip": peer,
        # Whether forwarded headers are actually arriving AND being honored. The
        # two disagreeing is the misconfiguration worth surfacing: headers
        # present but the peer untrusted means every user is collapsed onto one
        # address for rate limiting and the session list.
        "forwarded_headers_present": any(
            h in request.headers for h in ("x-forwarded-for", "x-real-ip", "forwarded")
        ),
        "peer_is_trusted_proxy": auth.peer_is_trusted_proxy(request, settings),
        # Raised at first-run setup when the Trakt token already in settings.json
        # could not be resolved to an account, so the Settings screen can prompt
        # the administrator to reconnect.
        #
        # DERIVED, not just read back: the stored flag records that setup failed,
        # but what the notice actually asks for is a linked Trakt identity, and
        # this caller either has one or does not. Trusting the flag alone left the
        # prompt up after somebody linked by a route that forgot to clear it —
        # a sticky "do this thing" that stayed after the thing was done.
        "trakt_reconnect_notice": bool(
            await db.get_meta(auth_routes.TRAKT_RECONNECT_NOTICE, "")
        ) and not (admin and admin.has_trakt_identity),
        # Whether the per-user "Log in with Trakt" button can be offered at all.
        "trakt_login_configured": settings.trakt_login_configured,
        "trakt_redirect_uri": (
            trakt_auth.redirect_uri(settings.public_base_url)
            if settings.public_base_url else ""
        ),
    })


_COOKIE_SECURE_MODES = ("always", "auto", "never")


def _cookie_secure_error(settings, request: Request) -> str | None:
    """Reject a cookie_secure change that is invalid or self-locking.

    The lockout: "always" makes the session cookie Secure, and a browser on plain
    http:// silently discards a Secure cookie — so the operator's next request
    arrives with no session and they can't get back to this screen to undo it,
    which is exactly why this used to be hand-edited only.

    Judged on the BROWSER's scheme (Origin/Referer), never the request's own,
    because behind a TLS-terminating proxy the app sees http while the browser is
    on https — that is the case "always" exists for and must stay allowed. The
    browser scheme also does not depend on trusted_proxy_ips being right, so the
    guard holds on a fresh instance whose proxy list is still the default. A save
    from this screen always carries an Origin (mutating + same-origin), so a
    missing scheme here means we genuinely can't tell, and we allow it.
    """
    mode = (settings.cookie_secure or "").strip().lower()
    if mode not in _COOKIE_SECURE_MODES:
        return f"Session cookie security must be one of: {', '.join(_COOKIE_SECURE_MODES)}."
    settings.cookie_secure = mode  # normalize what gets saved
    if mode == "always" and auth.browser_scheme(request) == "http":
        return (
            "You're viewing this over http://, so a Secure session cookie would be "
            "discarded by your browser and lock you out. Serve this over https:// "
            "(a reverse proxy in front counts), or choose Auto or Never."
        )
    return None


@guard.post("/api/settings", AuthLevel.ADMIN)
async def post_settings(request: Request):
    """Save a partial settings update.

    A secret that is absent or blank keeps its stored value, and an explicit null
    clears it — see config.apply_update. That is what lets the Settings screen
    render its credential inputs empty (it cannot read them back) without the
    first save wiping every credential the instance has.
    """
    try:
        data = await request.json()
    except ValueError:
        return JSONResponse({"ok": False, "error": "Invalid JSON body"}, status_code=400)
    if not isinstance(data, dict):
        return JSONResponse({"ok": False, "error": "Invalid JSON body"}, status_code=400)
    # A credential save while the key is missing or wrong would seal a fresh value
    # over ciphertext the original key could still recover. Refuse it loudly and send
    # the admin to recovery, rather than let it silently overwrite. A save that only
    # touches non-secret settings is fine — those are never sealed — so this checks
    # for a real secret change (a new value or an explicit clear), not a blank field.
    changes_secret = any(
        name in data and (data[name] is None or str(data[name]).strip())
        for name in SECRET_FIELDS
    )
    if changes_secret and encryption_flow.secret_writes_blocked():
        return JSONResponse({
            "ok": False,
            "reason": "key_unhealthy",
            "error": (
                "Encryption is unhealthy, so saving a credential is blocked to avoid "
                "overwriting a value the correct key could still recover. Restore the "
                "original ENCRYPTION_KEY, or run the recovery reset first."
            ),
            "recovery_url": encryption_routes.RECOVERY_PATH,
        }, status_code=409)
    settings = apply_update(load_settings(), data)
    # Rejected on save rather than on use: a base URL with a path or a trailing
    # slash builds a redirect URI that no longer matches the one registered on
    # the Trakt application, and Trakt compares the two exactly — so the failure
    # would otherwise surface much later as an unreadable error mid-sign-in.
    if err := public_base_url_error(settings.public_base_url):
        return JSONResponse({"ok": False, "error": err}, status_code=400)
    if "cookie_secure" in data and (err := _cookie_secure_error(settings, request)):
        return JSONResponse({"ok": False, "error": err}, status_code=400)
    save_settings(settings)
    # Re-check Sonarr/Radarr/Seerr immediately so buttons reflect the new config right away,
    # and invalidate the library cache so the next fetch re-pulls with the new credentials
    # (rather than serving the stale/empty cache until the TTL expires or a restart).
    await refresh_integration_health()
    LIBRARY_CACHE["_ts"] = 0.0
    return JSONResponse({"ok": True, "settings": settings.redacted()})


@guard.post("/api/auth/device/start", AuthLevel.ADMIN)
async def auth_device_start(request: Request):
    """Begin Trakt's OAuth device-code flow. Accepts an in-progress (unsaved)
    client_id from the Settings form, falling back to the saved one — same
    pattern as /api/integrations/options for Sonarr/Radarr."""
    try:
        data = await request.json()
    except ValueError:
        data = {}
    settings = load_settings()
    client_id = (data.get("client_id") or "").strip() or settings.trakt_client_id
    if not client_id:
        return JSONResponse({"ok": False, "error": "Enter a Trakt Client ID first."}, status_code=400)
    try:
        code = await trakt_auth.request_device_code(client_id)
    except httpx.HTTPError as exc:
        return JSONResponse({"ok": False, "error": f"Could not start device authorization: {exc}"}, status_code=502)
    return JSONResponse({"ok": True, **code})


@guard.post("/api/auth/device/poll", AuthLevel.ADMIN)
async def auth_device_poll(request: Request):
    """Check whether the user has approved the device code yet. On success,
    persists client_id/client_secret + the new token pair to settings.json so
    the background auto-refresh (heartbeat) can pick it up without the user
    separately clicking "Save & reload" on the main Settings form."""
    try:
        data = await request.json()
    except ValueError:
        return JSONResponse({"ok": False, "error": "Invalid JSON body"}, status_code=400)
    settings = load_settings()
    client_id = (data.get("client_id") or "").strip() or settings.trakt_client_id
    client_secret = (data.get("client_secret") or "").strip() or settings.trakt_client_secret
    device_code = data.get("device_code")
    if not (client_id and client_secret and device_code):
        return JSONResponse({"ok": False, "error": "Missing client_id, client_secret, or device_code."}, status_code=400)
    try:
        token = await trakt_auth.poll_device_token(client_id, client_secret, device_code)
    except trakt_auth.DevicePending:
        return JSONResponse({"ok": True, "status": "pending"})
    except trakt_auth.DeviceSlowDown:
        return JSONResponse({"ok": True, "status": "slow_down"})
    except trakt_auth.DeviceExpired as exc:
        return JSONResponse({"ok": False, "status": "expired", "error": str(exc)}, status_code=410)
    except trakt_auth.DeviceDenied as exc:
        return JSONResponse({"ok": False, "status": "denied", "error": str(exc)}, status_code=409)
    except httpx.HTTPError as exc:
        return JSONResponse({"ok": False, "status": "error", "error": f"Trakt error: {exc}"}, status_code=502)

    settings.trakt_client_id = client_id
    settings.trakt_client_secret = client_secret
    settings = await _apply_new_trakt_token(settings, token)
    await refresh_integration_health()
    # The token is known-good right now, so this is the best moment there will
    # ever be to resolve it to an account and link it to the administrator who
    # just authorized it. Without this the app-wide token renews while
    # `linked_identities` stays empty — which leaves the "reconnect your Trakt
    # account" notice up with nothing in the UI able to clear it, and leaves the
    # tracker refusing this account for want of a linked identity.
    admin = await auth.current_user(request)
    linked, link_error = await trakt_routes.adopt_app_token(admin.user_id, settings)
    # The token itself is not echoed back. It is already saved, so sending it to
    # the browser would put a Trakt bearer token in page memory for no purpose.
    return JSONResponse({
        "ok": True,
        "status": "authorized",
        "expires_at": settings.trakt_token_expires_at,
        # Lets the Settings screen take the reconnect notice down without a reload.
        "trakt_linked": linked,
        # And, when it can't, say so on the spot. A successful authorization that
        # silently failed to link is the exact state that leaves the reconnect
        # notice up looking like it ignored what was just done.
        "trakt_link_error": link_error,
    })


@guard.post("/api/auth/trakt/adopt", AuthLevel.ADMIN)
async def auth_trakt_adopt(request: Request):
    """Retry linking the saved app-wide Trakt token to the calling administrator.

    The reconnect notice asks for exactly this, and until now the only thing that
    performed it was a fresh device authorization — so an adoption that failed
    for a reason re-authorizing does not fix (the Trakt account already belonging
    to another login here) left the notice up no matter how many times the
    operator re-ran the flow. This is the same operation on its own, with the
    reason reported.
    """
    admin = await auth.current_user(request)
    linked, link_error = await trakt_routes.adopt_app_token(admin.user_id, load_settings())
    if not linked:
        return JSONResponse({"ok": False, "error": link_error}, status_code=409)
    return JSONResponse({"ok": True})


@guard.post("/api/auth/refresh", AuthLevel.ADMIN)
async def auth_refresh():
    """Manual "refresh now" button — uses whatever is already saved (the
    device-auth flow is what actually seeds client_secret/refresh_token)."""
    settings = load_settings()
    if not (settings.trakt_client_id and settings.trakt_client_secret and settings.trakt_refresh_token):
        return JSONResponse({"ok": False, "error": "Not authorized yet — use 'Authorize with Trakt' first."}, status_code=400)
    try:
        token = await trakt_auth.refresh_access_token(
            settings.trakt_client_id, settings.trakt_client_secret, settings.trakt_refresh_token,
        )
    except (httpx.HTTPError, trakt_auth.TraktRateLimitError) as exc:
        return JSONResponse({"ok": False, "error": f"Refresh failed: {exc}"}, status_code=502)
    settings = await _apply_new_trakt_token(settings, token)
    return JSONResponse({"ok": True, "expires_at": settings.trakt_token_expires_at})


@guard.get("/api/integrations/status", AuthLevel.ADMIN)
async def integrations_status():
    """Cached Sonarr/Radarr health (refreshed by the heartbeat + on save)."""
    return JSONResponse(INTEGRATION_HEALTH)


@guard.get("/api/integrations/library", AuthLevel.ADMIN)
async def integrations_library():
    """Ids already in each library, so the UI can mark added items (TTL-cached)."""
    await refresh_library()
    return JSONResponse({k: LIBRARY_CACHE[k] for k in ("sonarr", "radarr", "seer")})


@guard.post("/api/integrations/options", AuthLevel.ADMIN)
async def integrations_options(request: Request):
    """Quality profiles + root folders for the Settings dropdowns. Accepts the URL +
    API key from the (possibly unsaved) form, falling back to saved settings."""
    try:
        data = await request.json()
    except ValueError:
        data = {}
    kind = data.get("kind")
    if kind not in ("sonarr", "radarr"):
        return JSONResponse({"ok": False, "error": "Unknown service"}, status_code=400)
    url = (data.get("url") or "").strip()
    key = (data.get("api_key") or "").strip()
    if not (url and key):  # fall back to what's already saved
        url, key = arr._base(kind, load_settings())
    if not (url and key):
        return JSONResponse({"ok": False, "error": "Enter the URL and API key first."}, status_code=400)
    try:
        opts = await arr.fetch_options(url, key)
    except Exception:  # network / parse errors
        return JSONResponse({"ok": False, "error": f"Couldn't reach {kind.title()} at {url} — check the URL and API key."}, status_code=502)
    return JSONResponse({"ok": True, **opts})


@guard.post("/api/integrations/add", AuthLevel.ADMIN)
async def integrations_add(request: Request):
    """Add a title to Sonarr (show/TVDB), Radarr (movie/TMDB), or Seerr (request/TMDB).

    Routed by `target`; falls back to the arr service implied by `media`.
    """
    try:
        data = await request.json()
    except ValueError:
        return JSONResponse({"ok": False, "error": "Invalid JSON body"}, status_code=400)
    media = data.get("media")
    target = data.get("target") or ("radarr" if media == "movie" else "sonarr")
    settings = load_settings()
    title = data.get("title") or "This title"

    if target == "seer":
        if not seer.is_configured(settings):
            return JSONResponse({"ok": False, "error": "Seerr isn't configured."}, status_code=400)
        result = await seer.add_media(settings, media, data.get("tmdb"), title)
    elif target in ("sonarr", "radarr"):
        if not arr.is_configured(target, settings):
            return JSONResponse({"ok": False, "error": f"{target.title()} isn't configured."}, status_code=400)
        ids = {"tvdb": data.get("tvdb"), "tmdb": data.get("tmdb")}
        result = await arr.add_media(target, settings, ids, title)
    else:
        return JSONResponse({"ok": False, "error": "Unknown target."}, status_code=400)

    # Keep the library cache consistent so the button stays marked on the next load.
    if result.get("ok"):
        lib_id = data.get("tvdb") if target == "sonarr" else data.get("tmdb")
        if lib_id is not None:
            try:
                lib_id = int(lib_id)
                if lib_id not in LIBRARY_CACHE[target]:
                    LIBRARY_CACHE[target].append(lib_id)
            except (TypeError, ValueError):
                pass

    return JSONResponse(result, status_code=200 if result.get("ok") else 502)


# ---------------------------------------------------------------------------
# Distrakt (hidden tracker) API — the add-show flow and abandon toggle, plus the
# endpoint that computes the buckets and renders the two copy-paste POST blocks.
# ---------------------------------------------------------------------------

def _month_key(year: int, month: int) -> str:
    return f"{year:04d}-{month:02d}"


def _merge_live_show(rec: dict, watched_lookup: dict, detail: dict) -> dict:
    """Combine a stored record (identity + abandoned/abandoned_form) with its
    live Trakt-derived fields into the flat "LIVE SHOW SHAPE" discord_fmt
    expects (see app/discord_fmt.py's module docstring), plus the computed
    `bucket` for the UI to group by."""
    show = {
        **rec,
        "watched": watched_lookup.get((rec["trakt_id"], rec["season"]), 0),
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
    a row can still hold an empty token, in which case `configured` goes false
    and the handlers take their existing "not configured" path.

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


@guard.get("/api/distrakt/list", AuthLevel.DISTRAKT_APPROVED)
async def api_distrakt_list(request: Request):
    """Raw (unbucketed) shows stored for a month — the plain management list."""
    user_id = await _distrakt_user_id(request)
    today = date.today()
    year = _valid_year(request.query_params.get("year"), today.year)
    month = _valid_month(request.query_params.get("month"), today.month)
    doc = await distrakt_store.load_month(user_id, _month_key(year, month))
    return JSONResponse({"ok": True, "month": _month_key(year, month), "shows": (doc or {}).get("shows", [])})


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
        return str(s.get("slug") or "") in nw_ids or str(s.get("trakt_id")) in nw_ids

    if not committed:
        return [s for s in shows if not matched(s)]

    for show in shows:
        if show.get("abandoned") or not matched(show):
            continue
        form = discord_fmt.freeze_form(show)
        await distrakt_store.set_abandoned(user_id, month_key, show["trakt_id"], show["season"],
                                           True, abandoned_form=form)
        show["abandoned"] = True
        show["abandoned_form"] = form
        show["bucket"] = "abandoned"
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


async def _distrakt_month_payload(user_id: int, year: int, month: int, settings,
                                  force_fresh: bool = False) -> tuple[dict, int]:
    """Shared body for GET /api/distrakt/month + POST /api/distrakt/refresh, for
    ONE user's tracker.

    Lazily rolls the month over (ensure_month), then either renders a CLOSED
    month from its frozen snapshot (no Trakt) or computes the OPEN month live
    (or always when force_fresh). A never-tracked PAST/gap month (backward nav)
    is rendered empty + read-only and never created. Returns (json_payload,
    http_status)."""
    today = date.today()
    month_key = _month_key(year, month)
    link_url = await _distrakt_post_link(user_id, settings, year, month)
    # This user's own map, fetched once and handed to every render below. It is
    # not on `settings` any more — see _distrakt_settings.
    emojis, default_emoji = await distrakt_store.get_emoji_prefs(user_id)
    existing = await distrakt_store.load_month(user_id, month_key)
    if existing is None:
        blocked = await distrakt_store.is_backfill_blocked(user_id, month_key)
        if blocked or not settings.configured:
            # Backward/gap past month (blocked) OR no Trakt yet: empty, NOT
            # persisted, no Trakt call. `readonly` hides the add/edit affordances.
            return _empty_month_payload(
                month_key, emojis, default_emoji, readonly=blocked, link_url=link_url,
            ), 200

    # Everything below reaches Trakt (rollover init, premiere import, tmdb
    # backfill, watch-history sync, season totals). A 429 on a SHARED prerequisite
    # (anything but the per-show season fan-out, which degrades itself) can't be
    # attributed to one show, so rather than a false 0/0 or a 500 the whole month
    # falls back to its last-known stored totals plus a notice at HTTP 200 — the
    # user refreshes again once the window clears. A plain reachability failure
    # (TraktError) degrades the same way; only the notice wording differs.
    try:
        with span("payload.ensure_month", month=month_key, force=force_fresh):
            doc = await distrakt_store.ensure_month(user_id, year, month, settings, today=today)
        month_key = doc["month"]

        if doc.get("closed"):
            # Frozen past month: render straight from the snapshot, no Trakt calls.
            shows = distrakt_store.frozen_shows(doc)
            post1 = discord_fmt.render_post1(shows, emojis, default_emoji, link_url=link_url, month=month_key)
            post2 = discord_fmt.render_post2(shows, emojis, default_emoji, movies=doc.get("movies"))
            return {
                "ok": True, "month": month_key, "closed": True, "readonly": False, "shows": shows,
                "movies": doc.get("movies") or [],
                "post1": post1, "post2": post2, "generated_at": datetime.now(timezone.utc).isoformat(),
            }, 200

        committed = distrakt_store.month_committed(month_key, today)
        # A PREVIEW month (before the 1st) keeps auto-populating from premieres so the
        # roster tracks the calendar (and un-not-watching re-adds a previously excluded
        # premiere). A COMMITTED month is stable — premieres only re-import on demand.
        if not committed and settings.configured:
            await distrakt_store.import_premieres(user_id, month_key, settings)
            doc = await distrakt_store.load_month(user_id, month_key) or doc

        records = doc.get("shows", [])
        if records and not settings.configured:
            return {"ok": False, "error": "Not configured"}, 400
        # Backfill tmdb on records added before we stored it (one-time; self-limiting)
        # so the network-logo <img> gets a tmdb to generate from on this same load.
        if records and settings.configured:
            with span("payload.backfill_tmdb"):
                doc = await distrakt_store.backfill_tmdb(user_id, month_key, settings) or doc
            records = doc.get("shows", [])
        # Two INDEPENDENT freshness knobs (they were wrongly coupled, which made every
        # stale load re-baseline the whole watch history):
        #   season_fresh -> bypass the 24h season cache for `y`. Only on explicit
        #                   Refresh; routine loads let the 24h TTL refresh `y` daily.
        #   force        -> full watch-history re-baseline. ONLY on explicit Refresh;
        #                   normal loads rely on the last_activities gate + deltas.
        season_fresh = force_fresh

        # Sync the incremental watch-history cache ONCE (gated by /sync/last_activities).
        # Reuse it for both watched counts and the month's watched-movies list.
        watched_lookup: dict = {}
        completed_lookup: dict = {}
        movies: list[dict] = []
        if settings.configured:
            with span("payload.watch_history_sync", roster=len(records), force=force_fresh) as sp:
                state = await watch_history.sync_and_baseline(
                    settings, user_id, [r["trakt_id"] for r in records], force=force_fresh, today=today,
                )
                watched_lookup = watch_history.watched_map(state)
                # When each season was finished, for the "Completed means
                # completed THIS month" rule below.
                completed_lookup = watch_history.season_completed_map(state)
                mstart, mend = watch_history.month_bounds(month_key)
                movies = watch_history.movies_in_range(state, mstart, mend)
                sp.set(watched_keys=len(watched_lookup), movies=len(movies))

        with span("payload.compute_live_shows", n=len(records), fresh=season_fresh):
            # allow_degrade: a per-show season 429 marks THAT show unavailable and
            # renders the rest, instead of failing the whole roster.
            shows = await distrakt_store.compute_live_shows(user_id, records, settings, fresh=season_fresh, watched_lookup=watched_lookup, allow_degrade=True, completed_lookup=completed_lookup) if records else []
        shows = await _apply_not_watching(user_id, month_key, shows, committed)
        # A season finished before this month began belongs to the month it was
        # finished in, not to this one — see drop_seasons_finished_earlier.
        shows = await distrakt_store.drop_seasons_finished_earlier(user_id, month_key, shows)
        if records and season_fresh:
            await distrakt_store.stamp_refreshed(user_id, month_key)

        # Pre-warm the network-logo cache for the whole roster now that tmdb has been
        # backfilled, so a show manually added before logos existed doesn't depend on
        # some OTHER show requesting its network's logo first (see logos.ensure_logos).
        # Best-effort and self-limiting: a no-op once each network's tile is on disk.
        if shows and settings.configured:
            with span("payload.ensure_logos"):
                await logos.ensure_logos(settings, [(s.get("network"), s.get("tmdb")) for s in shows])

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
    year = _valid_year(request.query_params.get("year"), today.year)
    month = _valid_month(request.query_params.get("month"), today.month)
    with span("GET /api/distrakt/month", ym=f"{year}-{month:02d}"):
        payload, status = await _distrakt_month_payload(user_id, year, month, await _distrakt_settings(user_id))
    return JSONResponse(payload, status_code=status)


@guard.post("/api/distrakt/refresh", AuthLevel.DISTRAKT_APPROVED)
async def api_distrakt_refresh(request: Request):
    """Force a fresh totals refresh: bypass the 24h season cache + re-stamp
    totals_refreshed_at for the OPEN month, then return the same shape as GET
    /api/distrakt/month. CLOSED months are frozen (nothing to refresh)."""
    user_id = await _distrakt_user_id(request)
    try:
        data = await request.json()
    except ValueError:
        data = {}
    today = date.today()
    year = _valid_year(data.get("year"), today.year)
    month = _valid_month(data.get("month"), today.month)
    payload, status = await _distrakt_month_payload(user_id, year, month, await _distrakt_settings(user_id),
                                                    force_fresh=True)
    return JSONResponse(payload, status_code=status)


@guard.get("/api/distrakt/months", AuthLevel.DISTRAKT_APPROVED)
async def api_distrakt_months(request: Request):
    """This user's tracked YYYY-MM months for the history nav, plus the real
    current month (always navigable even before it has been initialized)."""
    user_id = await _distrakt_user_id(request)
    today = date.today()
    current = _month_key(today.year, today.month)
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
    if not settings.configured:
        return JSONResponse({"ok": False, "error": "Not configured"}, status_code=400)
    try:
        data = await request.json()
    except ValueError:
        data = {}
    today = date.today()
    year = _valid_year(data.get("year"), today.year)
    month = _valid_month(data.get("month"), today.month)
    month_key = _month_key(year, month)
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
    try:
        data = await request.json()
    except ValueError:
        data = {}
    today = date.today()
    year = _valid_year(data.get("year"), today.year)
    month = _valid_month(data.get("month"), today.month)
    doc = await distrakt_store.load_month(user_id, _month_key(year, month))
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

    The slug comes from the user's own roster row, not the query string, so the
    Trakt links this builds cannot be pointed somewhere else by the caller.
    """
    user_id = await _distrakt_user_id(request)
    settings = await _distrakt_settings(user_id)
    if not settings.configured:
        return JSONResponse({"ok": False, "error": "Not configured"}, status_code=400)
    trakt_id = request.query_params.get("trakt_id")
    season = _season_param(request.query_params.get("season"))
    if not trakt_id or season is None:
        return JSONResponse({"ok": False, "error": "Missing trakt_id/season"}, status_code=400)
    try:
        trakt_id_int = int(trakt_id)
    except (TypeError, ValueError):
        return JSONResponse({"ok": False, "error": "Invalid trakt_id"}, status_code=400)

    try:
        details = await fetch_details(settings, "show", trakt_id, season)
    except TraktError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=exc.status or 502)
    row = await db.fetch_one(
        "SELECT slug FROM distrakt_shows WHERE user_id = ? AND trakt_id = ? LIMIT 1",
        (user_id, trakt_id_int),
    )
    progress = await db.fetch_one(
        "SELECT watched_episodes_json FROM distrakt_show_progress "
        "WHERE user_id = ? AND trakt_id = ? AND season = ?",
        (user_id, trakt_id_int, season),
    )
    watched: list[int] = []
    if progress is not None:
        try:
            parsed = json.loads(progress["watched_episodes_json"] or "[]")
            watched = [int(n) for n in parsed if isinstance(n, (int, float))]
        except (TypeError, ValueError):
            watched = []
    return JSONResponse({
        "ok": True,
        **details,
        "slug": (row["slug"] if row else "") or "",
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
    try:
        data = await request.json()
    except ValueError:
        return JSONResponse({"ok": False, "error": "Invalid JSON body"}, status_code=400)
    if not isinstance(data, dict):
        return JSONResponse({"ok": False, "error": "Invalid JSON body"}, status_code=400)
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
    A row from before `source` was recorded is resolved by asking the calendar
    whether it would hand that show straight back (see is_calendar_premiere).

    A CLOSED month never writes that mark, whatever the row says. Correcting what
    a past month records is a statement about that month and nothing else — a
    season you finished years ago and re-watched one episode of does not belong
    on March's list, but it also is not something to start hiding from your
    calendar today. The row goes; the month stays closed.
    """
    user_id = await _distrakt_user_id(request)
    try:
        data = await request.json()
    except ValueError:
        return JSONResponse({"ok": False, "error": "Invalid JSON body"}, status_code=400)
    today = date.today()
    year = _valid_year(data.get("year"), today.year)
    month = _valid_month(data.get("month"), today.month)
    try:
        trakt_id = int(data["trakt_id"])
        season = int(data["season"])
    except (KeyError, TypeError, ValueError):
        return JSONResponse({"ok": False, "error": "Missing or invalid trakt_id/season"}, status_code=400)
    month_key = _month_key(year, month)
    doc = await distrakt_store.load_month(user_id, month_key)
    if doc is None:
        return JSONResponse({"ok": False, "error": "Show/season not found in that month"}, status_code=404)
    # Read before removing: both the provenance and the key the mark is written
    # under (slug, falling back to the trakt id, exactly as the calendar keys its
    # own items) live on the record that is about to go.
    record = next((s for s in (doc.get("shows") or [])
                   if int(s["trakt_id"]) == trakt_id and int(s["season"]) == season), None)
    if not await distrakt_store.remove_show(user_id, month_key, trakt_id, season):
        return JSONResponse({"ok": False, "error": "Show/season not found in that month"}, status_code=404)

    settings = await _distrakt_settings(user_id)
    closed = bool(doc.get("closed"))
    source = str((record or {}).get("source") or "")
    hide_on_calendar = not closed and source == distrakt_store.SOURCE_CALENDAR
    if record is not None and not source and not closed:
        hide_on_calendar = await distrakt_store.is_calendar_premiere(
            user_id, month_key, settings, trakt_id, season,
        )
    if hide_on_calendar:
        await calendar_state.set_not_watching(
            user_id, str(record.get("slug") or trakt_id), True,
        )
    payload, status = await _distrakt_month_payload(user_id, year, month, settings)  # recomputed month (1d)
    # So the toast can say what actually happened rather than guessing.
    if isinstance(payload, dict):
        payload["hidden_on_calendar"] = hide_on_calendar
    return JSONResponse(payload, status_code=status)


@guard.get("/api/distrakt/search", AuthLevel.DISTRAKT_APPROVED)
async def api_distrakt_search(request: Request):
    settings = await _distrakt_settings(await _distrakt_user_id(request))
    if not settings.configured:
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
    if not settings.configured:
        return JSONResponse({"ok": False, "error": "Not configured"}, status_code=400)
    q = request.query_params.get("q", "")
    try:
        found = await search_titles(settings, "movie", q)
    except TraktError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=exc.status or 502)
    results = [
        {"trakt_id": entry["ids"]["trakt"], "title": entry["title"],
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
    try:
        data = await request.json()
    except ValueError:
        return JSONResponse({"ok": False, "error": "Invalid JSON body"}, status_code=400)
    try:
        trakt_id = int(data["trakt_id"])
    except (KeyError, TypeError, ValueError):
        return JSONResponse({"ok": False, "error": "Missing or invalid trakt_id"}, status_code=400)
    day = str(data.get("watched_on") or "").strip()
    try:
        watched_on = date.fromisoformat(day)
    except ValueError:
        return JSONResponse({"ok": False, "error": "Pick the day you watched it."}, status_code=400)
    if watched_on > date.today():
        return JSONResponse({"ok": False, "error": "That day hasn't happened yet."}, status_code=400)

    await watch_history.record_movie_watches(user_id, [{
        "trakt_id": trakt_id,
        "title": data.get("title") or "",
        "year": data.get("year"),
        "watched_at": f"{watched_on.isoformat()}T12:00:00Z",
    }])
    # A CLOSED month renders films from its own snapshot, so it has to be told;
    # an open one recomputes them on every load and needs nothing.
    month_key = f"{watched_on.year:04d}-{watched_on.month:02d}"
    doc = await distrakt_store.load_month(user_id, month_key)
    if doc is not None and doc.get("closed"):
        state = await watch_history.load_state(user_id)
        mstart, mend = watch_history.month_bounds(month_key)
        await distrakt_store.set_month_movies(
            user_id, month_key, watch_history.movies_in_range(state, mstart, mend))

    today = date.today()
    year = _valid_year(data.get("year_view"), today.year)
    month = _valid_month(data.get("month_view"), today.month)
    payload, status = await _distrakt_month_payload(
        user_id, year, month, await _distrakt_settings(user_id))
    return JSONResponse(payload, status_code=status)


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
    try:
        data = await request.json()
    except ValueError:
        return JSONResponse({"ok": False, "error": "Invalid JSON body"}, status_code=400)
    try:
        trakt_id = int(data["trakt_id"])
    except (KeyError, TypeError, ValueError):
        return JSONResponse({"ok": False, "error": "Missing or invalid trakt_id"}, status_code=400)

    watched_at = await watch_history.forget_movie_watch(user_id, trakt_id)
    if watched_at is None:
        return JSONResponse({"ok": False, "error": "No such film on record."}, status_code=404)

    # The month it was filed under renders films from its own snapshot once
    # closed, so that snapshot is what has to be rebuilt — not necessarily the
    # month being looked at.
    month_key = watched_at[:7]
    doc = await distrakt_store.load_month(user_id, month_key) if month_key else None
    if doc is not None and doc.get("closed"):
        state = await watch_history.load_state(user_id)
        mstart, mend = watch_history.month_bounds(month_key)
        await distrakt_store.set_month_movies(
            user_id, month_key, watch_history.movies_in_range(state, mstart, mend))

    today = date.today()
    year = _valid_year(data.get("year"), today.year)
    month = _valid_month(data.get("month"), today.month)
    payload, status = await _distrakt_month_payload(
        user_id, year, month, await _distrakt_settings(user_id))
    return JSONResponse(payload, status_code=status)


@guard.get("/api/distrakt/seasons", AuthLevel.DISTRAKT_APPROVED)
async def api_distrakt_seasons(request: Request):
    """Aired seasons for a show (add-flow season picker) — required so the
    browser can call fetch_show_seasons()."""
    settings = await _distrakt_settings(await _distrakt_user_id(request))
    if not settings.configured:
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
    if not settings.configured:
        return JSONResponse({"ok": False, "error": "Not configured"}, status_code=400)
    try:
        data = await request.json()
    except ValueError:
        return JSONResponse({"ok": False, "error": "Invalid JSON body"}, status_code=400)
    today = date.today()
    year = _valid_year(data.get("year"), today.year)
    month = _valid_month(data.get("month"), today.month)
    try:
        show = {
            "trakt_id": int(data["trakt_id"]),
            "tmdb": data.get("tmdb"),
            "season": int(data["season"]),
            "slug": data.get("slug") or "",
            "title": data.get("title") or "",
            "network": data.get("network") or "",
            "media": "show",
            # Added by hand, so removing it later says nothing about the
            # calendar — see api_distrakt_remove.
            "source": distrakt_store.SOURCE_MANUAL,
        }
    except (KeyError, TypeError, ValueError):
        return JSONResponse({"ok": False, "error": "Missing or invalid trakt_id/season"}, status_code=400)
    month_key = _month_key(year, month)
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
        await watch_history.baseline_show(settings, user_id, show["trakt_id"])
    except Exception:  # never fail the add on a baseline hiccup — it self-heals on next load
        logger.warning("baseline_show failed for %s", show["trakt_id"], exc_info=True)
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
    if not settings.configured:
        return JSONResponse({"ok": False, "error": "Not configured"}, status_code=400)
    try:
        data = await request.json()
    except ValueError:
        return JSONResponse({"ok": False, "error": "Invalid JSON body"}, status_code=400)
    today = date.today()
    year = _valid_year(data.get("year"), today.year)
    month = _valid_month(data.get("month"), today.month)
    month_key = _month_key(year, month)
    if month_key >= _month_key(today.year, today.month):
        return JSONResponse(
            {"ok": False, "error": "Only a past month can be filled in by hand."},
            status_code=400)
    try:
        trakt_id = int(data["trakt_id"])
        season = int(data["season"])
    except (KeyError, TypeError, ValueError):
        return JSONResponse({"ok": False, "error": "Missing or invalid trakt_id/season"}, status_code=400)

    try:
        detail = await fetch_season_detail(settings, trakt_id, season)
    except TraktError as exc:
        return JSONResponse({"ok": False, "error": f"Trakt could not be read: {exc}"}, status_code=502)
    total = int((detail or {}).get("total") or 0)
    if not total:
        return JSONResponse(
            {"ok": False, "error": "Trakt lists no episodes for that season, so it cannot be recorded as finished."},
            status_code=400)

    await distrakt_store.add_show(user_id, month_key, {
        "trakt_id": trakt_id,
        "tmdb": data.get("tmdb"),
        "slug": data.get("slug") or "",
        "title": data.get("title") or "",
        "season": season,
        "network": data.get("network") or "",
        "media": "show",
        "watched": total,
        "total": total,
        "cadence": (detail or {}).get("cadence"),
        "premiere": (detail or {}).get("premiere"),
        "finale": (detail or {}).get("finale"),
        "started_airing": True,
        "finished_airing": True,
        "bucket": "completed",
        "source": distrakt_store.SOURCE_MANUAL,
    })
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
    if not settings.configured:
        return JSONResponse({"ok": False, "error": "Not configured"}, status_code=400)
    try:
        data = await request.json()
    except ValueError:
        data = {}
    if not isinstance(data, dict):
        data = {}
    tracked = await distrakt_store.list_months(user_id)
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
    try:
        data = await request.json()
    except ValueError:
        return JSONResponse({"ok": False, "error": "Invalid JSON body"}, status_code=400)
    today = date.today()
    year = _valid_year(data.get("year"), today.year)
    month = _valid_month(data.get("month"), today.month)
    try:
        trakt_id = int(data["trakt_id"])
        season = int(data["season"])
    except (KeyError, TypeError, ValueError):
        return JSONResponse({"ok": False, "error": "Missing or invalid trakt_id/season"}, status_code=400)
    abandoned = bool(data.get("abandoned"))
    month_key = _month_key(year, month)

    abandoned_form = None
    if abandoned:
        doc = await distrakt_store.load_month(user_id, month_key)
        rec = next(
            (r for r in (doc or {}).get("shows", []) if r.get("trakt_id") == trakt_id and r.get("season") == season),
            None,
        )
        settings = await _distrakt_settings(user_id)
        if rec is not None and settings.configured:
            try:
                watched_lookup, detail = await asyncio.gather(
                    fetch_watched_map(settings, [trakt_id]),
                    fetch_season_detail(settings, trakt_id, season),
                )
                abandoned_form = discord_fmt.freeze_form(_merge_live_show(rec, watched_lookup, detail))
            except TraktError:
                # Rate-limited or unreachable while snapshotting the abandon form:
                # skip the live freeze and leave abandoned_form=None. The renderer
                # recomputes a form from the stored record instead. Abandoning is a
                # user action and must still succeed — it is not a read to fail on.
                abandoned_form = None

    rec = await distrakt_store.set_abandoned(user_id, month_key, trakt_id, season, abandoned,
                                             abandoned_form=abandoned_form)
    if rec is None:
        return JSONResponse({"ok": False, "error": "Show/season not found in that month"}, status_code=404)
    payload, status = await _distrakt_month_payload(user_id, year, month, await _distrakt_settings(user_id))  # recomputed month (1d)
    return JSONResponse(payload, status_code=status)


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
    try:
        data = await request.json()
    except ValueError:
        return JSONResponse({"ok": False, "error": "Invalid JSON body"}, status_code=400)
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
    try:
        data = await request.json()
    except ValueError:
        return JSONResponse({"ok": False, "error": "Invalid JSON body"}, status_code=400)
    if not isinstance(data, dict):
        return JSONResponse({"ok": False, "error": "Invalid JSON body"}, status_code=400)
    kind = data["kind"] or None if "kind" in data else ...
    endpoint = data["endpoint"] or None if "endpoint" in data else ...
    if kind is ... and endpoint is ...:
        return JSONResponse({"ok": False, "error": "Nothing to update"}, status_code=400)
    try:
        await share_links.set_post_link(user_id, kind=kind, endpoint=endpoint)
    except ValueError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    return JSONResponse(await _share_link_payload(user_id))
