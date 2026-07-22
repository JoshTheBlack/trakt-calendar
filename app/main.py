"""FastAPI application (requirement A) — served under Hypercorn.

Server-renders the same day-grouped poster grid as the original PHP app, plus a
JSON API for watch-state and front-end settings.
"""
from __future__ import annotations

import asyncio
import calendar
import logging
import os
import time
from contextlib import asynccontextmanager
from datetime import date, datetime, timezone
from itertools import groupby
from pathlib import Path

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import arr
from . import auth
from . import auth_routes
from . import authz
from . import db
from . import discord_fmt
from . import distrakt as distrakt_store
from . import logos
from . import seer
from . import state as state_store
from . import trakt_auth
from . import trakt_routes
from . import watch_history
from .auth import AuthLevel
from .perftrace import span
from .config import Settings, apply_update, load_settings, public_base_url_error, save_settings
from .endpoints import DEFAULT_ENDPOINT, endpoint_choices, get_endpoint
from .timezones import build_options as build_timezone_options
from .trakt import (
    TraktError,
    fetch_calendar,
    fetch_details,
    fetch_season_detail,
    fetch_show_seasons,
    fetch_tile_info,
    fetch_watched_map,
    search_shows,
)

logger = logging.getLogger(__name__)

VERSION = "1.0.0"  # keep in sync with CHANGELOG.md
# Build metadata injected at Docker build time (GitHub Actions); "dev" for local runs.
BUILD = os.environ.get("APP_BUILD", "dev").strip() or "dev"
COMMIT = os.environ.get("APP_COMMIT", "").strip()
BUILD_LABEL = "dev" if BUILD == "dev" else f"build {BUILD}" + (f" · {COMMIT[:7]}" if COMMIT else "")

BASE_DIR = Path(__file__).resolve().parent
HEARTBEAT_SECONDS = 60


def _asset_version() -> str:
    """Cache-busting token: newest mtime of the CSS/JS, recomputed each server start."""
    files = [
        BASE_DIR / "static/css/style.css",
        BASE_DIR / "static/css/distrakt.css",
        BASE_DIR / "static/js/app.js",
        BASE_DIR / "static/js/distrakt.js",
    ]
    try:
        return str(int(max(f.stat().st_mtime for f in files)))
    except OSError:
        return VERSION


ASSET_VERSION = _asset_version()

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
    except httpx.HTTPError as exc:
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
        await asyncio.sleep(HEARTBEAT_SECONDS)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    # Schema first — everything after this point may touch the database.
    await db.init()
    # Loud, once, at boot: a route nobody declared is being refused to every
    # caller, and the operator should hear about it here rather than from a user.
    authz.log_undeclared_routes(_app)
    await refresh_integration_health()
    task = asyncio.create_task(_heartbeat_loop())
    try:
        yield
    finally:
        task.cancel()
        import app.trakt as _trakt
        await _trakt.aclose_shared_client()


# The interactive API docs are off: they are a complete, unauthenticated
# inventory of every endpoint in the app, and nothing here is a public API that
# anyone consumes from a schema.
app = FastAPI(title="Trakt New Shows", lifespan=lifespan,
              docs_url=None, redoc_url=None, openapi_url=None)
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
templates = Jinja2Templates(directory=BASE_DIR / "templates")

app.include_router(auth_routes.router)
app.include_router(trakt_routes.router)

# Every route below is registered through this, which requires an access level
# and refuses to register one without it.
guard = authz.Guard(app)
# Styles, scripts, images, and the easter egg's audio. Nothing here is derived
# from anyone's data.
authz.declare_mount(app, "/static", AuthLevel.PUBLIC)
authz.install(app)


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


def _month_valid(value) -> bool:
    try:
        return 1 <= int(value) <= 12
    except (TypeError, ValueError):
        return False


def _picker_context(request: Request, settings, year: int, endpoint):
    today = date.today()
    return {
        "request": request,
        "year": year,
        "endpoint": endpoint,
        "months": [{"num": m, "name": calendar.month_name[m]} for m in range(1, 13)],
        "current_month": today.month if year == today.year else None,
        "today_month": today.month,
        "today_year": today.year,
        "version": VERSION,
        "build": BUILD_LABEL,
        "asset_v": ASSET_VERSION,
    }


@guard.get("/", AuthLevel.CALENDAR_APPROVED)
async def index(request: Request):
    settings = load_settings()
    # Already resolved and cached by the dependency that let this request in.
    user = await auth.current_user(request)
    is_admin = bool(user and user.is_admin)
    today = date.today()
    year = _valid_year(request.query_params.get("year"), today.year)
    endpoint_key = request.query_params.get("endpoint") or settings.endpoint or DEFAULT_ENDPOINT
    endpoint = get_endpoint(endpoint_key)

    # No month specified -> show the month/year picker landing page (like the original front page).
    if not _month_valid(request.query_params.get("month")):
        return templates.TemplateResponse(request, "pick.html", _picker_context(request, settings, year, endpoint))

    month = _valid_month(request.query_params.get("month"), today.month)

    items: list[dict] = []
    error: str | None = None
    if not settings.configured:
        error = "Trakt API credentials aren't set yet. Open ⚙️ Settings to add your Client ID and Access Token."
    else:
        try:
            items = await fetch_calendar(endpoint, settings, year, month)
        except TraktError as exc:
            error = str(exc)

    grouped = [
        {"date": day, "label": datetime.strptime(day, "%Y-%m-%d").strftime("%A, %d %B"), "items": list(rows)}
        for day, rows in groupby(items, key=lambda i: i["air_date"])
    ]

    context = {
        "request": request,
        "settings": settings,
        "endpoint": endpoint,
        "endpoints": endpoint_choices(),
        "timezone_groups": build_timezone_options(settings.timezone),
        "year": year,
        "month": month,
        "month_label": calendar.month_name[month],
        "nav": _nav(year, month),
        "grouped": grouped,
        "total": len(items),
        "error": error,
        "generated": datetime.now().strftime("%H:%M"),
        # Sonarr/Radarr/Seerr writes land in the operator's own shared libraries
        # and Seerr's requests all carry one app-wide API key, so they are an
        # administrator's affordance. The buttons and health state are left out
        # of the page entirely for everyone else rather than rendered into a
        # guaranteed 403.
        "is_admin": is_admin,
        "integrations": INTEGRATION_HEALTH if is_admin else {},
        "version": VERSION,
        "build": BUILD_LABEL,
        "asset_v": ASSET_VERSION,
    }
    return templates.TemplateResponse(request, "index.html", context)


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
    context = {
        "request": request,
        "year": year,
        "month": month,
        "nav": _nav(year, month),
        # The network -> emoji map is app-wide configuration shared by every
        # user's Discord posts, so EDITING it is an administrator's job. Reading
        # it is not: the roster rows on this page fall back to these emoji when a
        # network has no logo, so they are rendered in rather than fetched from
        # the admin-only settings endpoint.
        "is_admin": bool(user and user.is_admin),
        "network_emojis": settings.network_emojis,
        "default_network_emoji": settings.default_network_emoji,
        "version": VERSION,
        "build": BUILD_LABEL,
        "asset_v": ASSET_VERSION,
    }
    return templates.TemplateResponse(request, "distrakt.html", context)


@guard.get("/pick", AuthLevel.CALENDAR_APPROVED)
async def pick(request: Request):
    """Month/year selector landing page (carried over from the original front page)."""
    settings = load_settings()
    year = _valid_year(request.query_params.get("year"), date.today().year)
    endpoint = get_endpoint(request.query_params.get("endpoint") or settings.endpoint or DEFAULT_ENDPOINT)
    return templates.TemplateResponse(request, "pick.html", _picker_context(request, settings, year, endpoint))


def _season_param(value) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


@guard.get("/api/tile", AuthLevel.CALENDAR_APPROVED)
async def api_tile(request: Request):
    """Compact season info for a tile (requirement F)."""
    settings = load_settings()
    if not settings.configured:
        return JSONResponse({"ok": False, "error": "Not configured"}, status_code=400)
    media = request.query_params.get("media", "show")
    trakt_id = request.query_params.get("id")
    if not trakt_id:
        return JSONResponse({"ok": False, "error": "Missing id"}, status_code=400)
    info = await fetch_tile_info(settings, media, trakt_id, _season_param(request.query_params.get("season")))
    return JSONResponse({"ok": True, **info})


@guard.get("/api/details", AuthLevel.CALENDAR_APPROVED)
async def api_details(request: Request):
    """Full detail payload for the modal (requirement G)."""
    settings = load_settings()
    if not settings.configured:
        return JSONResponse({"ok": False, "error": "Not configured"}, status_code=400)
    media = request.query_params.get("media", "show")
    trakt_id = request.query_params.get("id")
    if not trakt_id:
        return JSONResponse({"ok": False, "error": "Missing id"}, status_code=400)
    details = await fetch_details(settings, media, trakt_id, _season_param(request.query_params.get("season")))
    return JSONResponse({"ok": True, **details})


@guard.get("/api/state", AuthLevel.CALENDAR_APPROVED)
async def get_state(request: Request):
    today = date.today()
    year = _valid_year(request.query_params.get("year"), today.year)
    month = _valid_month(request.query_params.get("month"), today.month)
    endpoint = get_endpoint(request.query_params.get("endpoint"))
    return JSONResponse(state_store.load_state(endpoint.key, year, month))


@guard.post("/api/state", AuthLevel.CALENDAR_APPROVED)
async def post_state(request: Request):
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
    try:
        state_store.save_state(endpoint.key, year, month, payload)
    except OSError as exc:
        return JSONResponse({"ok": False, "error": f"Could not write state: {exc}"}, status_code=500)
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
async def get_settings():
    """Configuration for the Settings screen, WITHOUT any credential in it.

    Credentials are write-only over this API: the response carries a flag per
    secret saying whether one is stored, never the value. This route used to hand
    the Trakt access token, the Trakt client secret, the TMDB key, and every
    Sonarr/Radarr/Seerr API key to whoever asked for it.
    """
    settings = load_settings()
    return JSONResponse({
        **settings.redacted(),
        # Raised at first-run setup when the Trakt token already in settings.json
        # could not be resolved to an account id, so the Settings screen can
        # prompt the administrator to reconnect. Cleared when they do.
        "trakt_reconnect_notice": bool(
            await db.get_meta(auth_routes.TRAKT_RECONNECT_NOTICE, "")
        ),
        # Whether the per-user "Log in with Trakt" button can be offered at all.
        "trakt_login_configured": settings.trakt_login_configured,
        "trakt_redirect_uri": (
            trakt_auth.redirect_uri(settings.public_base_url)
            if settings.public_base_url else ""
        ),
    })


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
    settings = apply_update(load_settings(), data)
    # Rejected on save rather than on use: a base URL with a path or a trailing
    # slash builds a redirect URI that no longer matches the one registered on
    # the Trakt application, and Trakt compares the two exactly — so the failure
    # would otherwise surface much later as an unreadable error mid-sign-in.
    if err := public_base_url_error(settings.public_base_url):
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
    # The token itself is not echoed back. It is already saved, so sending it to
    # the browser would put a Trakt bearer token in page memory for no purpose.
    return JSONResponse({
        "ok": True,
        "status": "authorized",
        "expires_at": settings.trakt_token_expires_at,
    })


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
    except httpx.HTTPError as exc:
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
# Distrakt (hidden tracker) API — add-show flow + abandon toggle (CHAT 3) plus
# the bucketing/rendering endpoint (CHAT 4).
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


@guard.get("/api/distrakt/list", AuthLevel.DISTRAKT_APPROVED)
async def api_distrakt_list(request: Request):
    """Raw (unbucketed) shows stored for a month — the plain management list."""
    today = date.today()
    year = _valid_year(request.query_params.get("year"), today.year)
    month = _valid_month(request.query_params.get("month"), today.month)
    doc = distrakt_store.load_month(_month_key(year, month))
    return JSONResponse({"ok": True, "month": _month_key(year, month), "shows": (doc or {}).get("shows", [])})


def _apply_not_watching(month_key: str, year: int, month: int, shows: list[dict], committed: bool) -> list[dict]:
    """§5 main-calendar not-watching, date-gated on the month's 1st (committed):

      - PREVIEW (before the 1st): not-watching HIDES the show from the tracker —
        excluded from the list + both posts, but KEPT in the doc so un-toggling
        brings it straight back ("not-watching removes from distrakt prior to the
        1st").
      - COMMITTED (on/after the 1st): not-watching promotes the roster show to
        Abandoned (persisted, form frozen). One-directional — never un-abandons;
        the dedicated /distrakt toggle + Delete stay the source of truth. The
        `abandoned` guard means a steady-state read does no extra writes."""
    nw_ids = distrakt_store.not_watching_ids(year, month)
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
        distrakt_store.set_abandoned(month_key, show["trakt_id"], show["season"], True, abandoned_form=form)
        show["abandoned"] = True
        show["abandoned_form"] = form
        show["bucket"] = "abandoned"
    return shows


def _empty_month_payload(month_key: str, settings, readonly: bool = False) -> dict:
    """Headers-only render for a month with no roster + no Trakt call: an
    unconfigured/uninitialized month (readonly=False) or a never-tracked past
    month reached by navigating backward (readonly=True, §6 no-backfill)."""
    return {
        "ok": True, "month": month_key, "closed": False, "readonly": readonly, "shows": [],
        "post1": discord_fmt.render_post1([], settings.network_emojis, settings.default_network_emoji),
        "post2": discord_fmt.render_post2([], settings.network_emojis, settings.default_network_emoji),
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


async def _distrakt_month_payload(year: int, month: int, settings, force_fresh: bool = False) -> tuple[dict, int]:
    """Shared body for GET /api/distrakt/month + POST /api/distrakt/refresh.

    Lazily rolls the month over (ensure_month), then either renders a CLOSED
    month from its frozen snapshot (no Trakt) or computes the OPEN month live
    (auto-refreshing totals when stale >24h, §3; or always when force_fresh).
    A never-tracked PAST/gap month (backward nav) is rendered empty + read-only
    and never created (§6 no-backfill). Returns (json_payload, http_status)."""
    today = date.today()
    month_key = _month_key(year, month)
    existing = distrakt_store.load_month(month_key)
    if existing is None:
        blocked = distrakt_store.is_backfill_blocked(month_key)
        if blocked or not settings.configured:
            # Backward/gap past month (blocked) OR no Trakt yet: empty, NOT
            # persisted, no Trakt call. `readonly` hides the add/edit affordances.
            return _empty_month_payload(month_key, settings, readonly=blocked), 200

    with span("payload.ensure_month", month=month_key, force=force_fresh):
        doc = await distrakt_store.ensure_month(year, month, settings, today=today)
    month_key = doc["month"]

    if doc.get("closed"):
        # Frozen past month: render straight from the snapshot, no Trakt calls (§3).
        shows = distrakt_store.frozen_shows(doc)
        post1 = discord_fmt.render_post1(shows, settings.network_emojis, settings.default_network_emoji)
        post2 = discord_fmt.render_post2(shows, settings.network_emojis, settings.default_network_emoji,
                                         movies=doc.get("movies"))
        return {
            "ok": True, "month": month_key, "closed": True, "readonly": False, "shows": shows,
            "post1": post1, "post2": post2, "generated_at": datetime.now(timezone.utc).isoformat(),
        }, 200

    committed = distrakt_store.month_committed(month_key, today)
    # A PREVIEW month (before the 1st) keeps auto-populating from premieres so the
    # roster tracks the calendar (and un-not-watching re-adds a previously excluded
    # premiere). A COMMITTED month is stable — premieres only re-import on demand.
    if not committed and settings.configured:
        await distrakt_store.import_premieres(month_key, settings)
        doc = distrakt_store.load_month(month_key) or doc

    records = doc.get("shows", [])
    if records and not settings.configured:
        return {"ok": False, "error": "Not configured"}, 400
    # Backfill tmdb on records added before we stored it (one-time; self-limiting)
    # so the network-logo <img> gets a tmdb to generate from on this same load.
    if records and settings.configured:
        with span("payload.backfill_tmdb"):
            doc = await distrakt_store.backfill_tmdb(month_key, settings) or doc
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
    movies: list[dict] = []
    if settings.configured:
        with span("payload.watch_history_sync", roster=len(records), force=force_fresh) as sp:
            state = await watch_history.sync_and_baseline(
                settings, [r["trakt_id"] for r in records], force=force_fresh, today=today,
            )
            watched_lookup = watch_history.watched_map(state)
            mstart, mend = watch_history.month_bounds(month_key)
            movies = watch_history.movies_in_range(state, mstart, mend)
            sp.set(watched_keys=len(watched_lookup), movies=len(movies))

    with span("payload.compute_live_shows", n=len(records), fresh=season_fresh):
        shows = await distrakt_store.compute_live_shows(records, settings, fresh=season_fresh, watched_lookup=watched_lookup) if records else []
    shows = _apply_not_watching(month_key, year, month, shows, committed)
    if records and season_fresh:
        distrakt_store.stamp_refreshed(month_key)

    with span("payload.render"):
        post1 = discord_fmt.render_post1(shows, settings.network_emojis, settings.default_network_emoji)
        post2 = discord_fmt.render_post2(shows, settings.network_emojis, settings.default_network_emoji, movies=movies)
    return {
        "ok": True,
        "month": month_key,
        "closed": False,
        "readonly": False,
        "shows": shows,
        "post1": post1,
        "post2": post2,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }, 200


@guard.get("/api/distrakt/month", AuthLevel.DISTRAKT_APPROVED)
async def api_distrakt_month(request: Request):
    """Computed buckets + the two copy-paste POST 1/POST 2 markdown blocks.

    OPEN month: live x/y + cadence/dates recomputed (1x /sync/watched/shows + 1x
    season call per show), auto-refreshed if totals are stale >24h (§3). CLOSED /
    past month: rendered from the frozen snapshot with NO Trakt calls. Opening an
    uninitialized month lazily rolls it over first (§6, see ensure_month)."""
    today = date.today()
    year = _valid_year(request.query_params.get("year"), today.year)
    month = _valid_month(request.query_params.get("month"), today.month)
    with span("GET /api/distrakt/month", ym=f"{year}-{month:02d}"):
        payload, status = await _distrakt_month_payload(year, month, load_settings())
    return JSONResponse(payload, status_code=status)


@guard.post("/api/distrakt/refresh", AuthLevel.DISTRAKT_APPROVED)
async def api_distrakt_refresh(request: Request):
    """Force a fresh totals refresh (§3): bypass the 24h season cache + re-stamp
    totals_refreshed_at for the OPEN month, then return the same shape as GET
    /api/distrakt/month. CLOSED months are frozen (nothing to refresh)."""
    try:
        data = await request.json()
    except ValueError:
        data = {}
    today = date.today()
    year = _valid_year(data.get("year"), today.year)
    month = _valid_month(data.get("month"), today.month)
    payload, status = await _distrakt_month_payload(year, month, load_settings(), force_fresh=True)
    return JSONResponse(payload, status_code=status)


@guard.get("/api/distrakt/months", AuthLevel.DISTRAKT_APPROVED)
async def api_distrakt_months():
    """Available YYYY-MM docs for the history nav, plus the real current month
    (always navigable even before it has been initialized)."""
    today = date.today()
    current = _month_key(today.year, today.month)
    months = sorted(set(distrakt_store.list_months()) | {current})
    return JSONResponse({"ok": True, "months": months, "current": current})


@guard.post("/api/distrakt/import", AuthLevel.DISTRAKT_APPROVED)
async def api_distrakt_import(request: Request):
    """Pull this month's calendar premieres into the OPEN month (shows/new -> New,
    shows/premieres minus new -> Returning; skips existing + not-watching). The
    manual "Import from calendar" action — e.g. to seed the current month when its
    doc already exists (so lazy-init's one-shot premiere seeding was skipped).
    Returns the same shape as GET /api/distrakt/month."""
    settings = load_settings()
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
    if distrakt_store.is_backfill_blocked(month_key):
        return JSONResponse({"ok": False, "error": "Can't import into a past month that was never tracked."}, status_code=400)
    doc = await distrakt_store.ensure_month(year, month, settings, today=today)
    if doc.get("closed"):
        return JSONResponse({"ok": False, "error": "Past month is frozen (read-only)."}, status_code=400)
    doc = await distrakt_store.import_premieres(month_key, settings)
    _register_networks([s.get("network") for s in (doc or {}).get("shows", [])])
    payload, status = await _distrakt_month_payload(year, month, settings)
    return JSONResponse(payload, status_code=status)


@guard.post("/api/distrakt/backfill-networks", AuthLevel.DISTRAKT_APPROVED)
async def api_distrakt_backfill_networks(request: Request):
    """Register every network used by this month's roster into the emoji map
    (with the default emoji) so they all show up in the editor. Returns the map."""
    try:
        data = await request.json()
    except ValueError:
        data = {}
    today = date.today()
    year = _valid_year(data.get("year"), today.year)
    month = _valid_month(data.get("month"), today.month)
    doc = distrakt_store.load_month(_month_key(year, month))
    _register_networks([s.get("network") for s in (doc or {}).get("shows", [])])
    settings = load_settings()
    return JSONResponse({
        "ok": True,
        "network_emojis": settings.network_emojis,
        "default_network_emoji": settings.default_network_emoji,
    })


@guard.post("/api/distrakt/remove", AuthLevel.DISTRAKT_APPROVED)
async def api_distrakt_remove(request: Request):
    """Delete a show+season from an OPEN month (cleanup mistakes / abandons).
    Frozen past months are read-only."""
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
    doc = distrakt_store.load_month(month_key)
    if doc is None:
        return JSONResponse({"ok": False, "error": "Show/season not found in that month"}, status_code=404)
    if doc.get("closed"):
        return JSONResponse({"ok": False, "error": "Past month is frozen (read-only)."}, status_code=400)
    if not distrakt_store.remove_show(month_key, trakt_id, season):
        return JSONResponse({"ok": False, "error": "Show/season not found in that month"}, status_code=404)
    payload, status = await _distrakt_month_payload(year, month, load_settings())  # recomputed month (1d)
    return JSONResponse(payload, status_code=status)


@guard.get("/api/distrakt/search", AuthLevel.DISTRAKT_APPROVED)
async def api_distrakt_search(request: Request):
    settings = load_settings()
    if not settings.configured:
        return JSONResponse({"ok": False, "error": "Not configured"}, status_code=400)
    q = request.query_params.get("q", "")
    try:
        results = await search_shows(settings, q)
    except TraktError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=exc.status or 502)
    return JSONResponse({"ok": True, "results": results})


@guard.get("/api/distrakt/seasons", AuthLevel.DISTRAKT_APPROVED)
async def api_distrakt_seasons(request: Request):
    """Aired seasons for a show (add-flow season picker) — not in BUILD_PLAN's
    route list, but required so the browser can call fetch_show_seasons()."""
    settings = load_settings()
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


def _register_networks(networks) -> None:
    """Auto-populate the network->emoji map (backlog item 3): any network not yet
    mapped gets the default emoji as a placeholder so it appears in the editor,
    ready to be customized. No-op for blank / already-mapped networks."""
    settings = load_settings()
    changed = False
    for net in networks:
        net = (net or "").strip()
        if net and net not in settings.network_emojis:
            settings.network_emojis[net] = settings.default_network_emoji
            changed = True
    if changed:
        save_settings(settings)


@guard.post("/api/distrakt/add", AuthLevel.DISTRAKT_APPROVED)
async def api_distrakt_add(request: Request):
    """Persist a show+season into the {year,month} doc (identity), baseline its
    watch history, and register its network in the emoji map."""
    settings = load_settings()
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
        }
    except (KeyError, TypeError, ValueError):
        return JSONResponse({"ok": False, "error": "Missing or invalid trakt_id/season"}, status_code=400)
    month_key = _month_key(year, month)
    if distrakt_store.is_backfill_blocked(month_key):
        # §6 no-backfill: refuse to create a never-tracked past/gap month even via
        # a manual add (keeps the store growing forward-only, consistent with the
        # read path's read-only rendering of such months).
        return JSONResponse(
            {"ok": False, "error": "Can't add shows to a past month that was never tracked."},
            status_code=400,
        )
    distrakt_store.add_show(month_key, show)
    _register_networks([show["network"]])
    try:  # baseline the show's watch history now so its counts are correct immediately
        await watch_history.baseline_show(settings, show["trakt_id"])
    except Exception:  # never fail the add on a baseline hiccup — it self-heals on next load
        logger.warning("baseline_show failed for %s", show["trakt_id"], exc_info=True)
    payload, status = await _distrakt_month_payload(year, month, settings)  # return the recomputed month (1d)
    return JSONResponse(payload, status_code=status)


@guard.post("/api/distrakt/abandon", AuthLevel.DISTRAKT_APPROVED)
async def api_distrakt_abandon(request: Request):
    """Toggle a show+season's abandoned flag. On abandon, freezes
    `abandoned_form` = the current live inline form minus premiere/finale dates
    (§4/§5), via discord_fmt.freeze_form — so the Discord line stays stable even
    after the show would otherwise have moved buckets. Un-abandoning clears it
    (distrakt_store.set_abandoned's job). If Trakt isn't configured (or the show
    isn't found), abandoned_form falls back to None, same as pre-Chat-4."""
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
        doc = distrakt_store.load_month(month_key)
        rec = next(
            (r for r in (doc or {}).get("shows", []) if r.get("trakt_id") == trakt_id and r.get("season") == season),
            None,
        )
        settings = load_settings()
        if rec is not None and settings.configured:
            watched_lookup, detail = await asyncio.gather(
                fetch_watched_map(settings, [trakt_id]),
                fetch_season_detail(settings, trakt_id, season),
            )
            abandoned_form = discord_fmt.freeze_form(_merge_live_show(rec, watched_lookup, detail))

    rec = distrakt_store.set_abandoned(month_key, trakt_id, season, abandoned, abandoned_form=abandoned_form)
    if rec is None:
        return JSONResponse({"ok": False, "error": "Show/season not found in that month"}, status_code=404)
    payload, status = await _distrakt_month_payload(year, month, load_settings())  # recomputed month (1d)
    return JSONResponse(payload, status_code=status)
