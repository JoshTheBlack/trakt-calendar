"""FastAPI application (requirement A) — served under Hypercorn.

Server-renders the same day-grouped poster grid as the original PHP app, plus a
JSON API for watch-state and front-end settings.
"""
from __future__ import annotations

import asyncio
import calendar
import os
import time
from contextlib import asynccontextmanager
from datetime import date, datetime
from itertools import groupby
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import arr
from . import seer
from . import state as state_store
from .config import Settings, load_settings, save_settings
from .endpoints import DEFAULT_ENDPOINT, endpoint_choices, get_endpoint
from .timezones import build_options as build_timezone_options
from .trakt import TraktError, fetch_calendar, fetch_details, fetch_tile_info

VERSION = "1.0.0"  # keep in sync with CHANGELOG.md
# Build metadata injected at Docker build time (GitHub Actions); "dev" for local runs.
BUILD = os.environ.get("APP_BUILD", "dev").strip() or "dev"
COMMIT = os.environ.get("APP_COMMIT", "").strip()
BUILD_LABEL = "dev" if BUILD == "dev" else f"build {BUILD}" + (f" · {COMMIT[:7]}" if COMMIT else "")

BASE_DIR = Path(__file__).resolve().parent
HEARTBEAT_SECONDS = 60


def _asset_version() -> str:
    """Cache-busting token: newest mtime of the CSS/JS, recomputed each server start."""
    files = [BASE_DIR / "static/css/style.css", BASE_DIR / "static/js/app.js"]
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


async def _heartbeat_loop() -> None:
    while True:
        try:
            await refresh_integration_health()
        except Exception:  # never let the heartbeat kill the loop
            pass
        await asyncio.sleep(HEARTBEAT_SECONDS)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    await refresh_integration_health()
    task = asyncio.create_task(_heartbeat_loop())
    try:
        yield
    finally:
        task.cancel()


app = FastAPI(title="Trakt New Shows", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
templates = Jinja2Templates(directory=BASE_DIR / "templates")


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


@app.get("/healthz")
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


@app.get("/")
async def index(request: Request):
    settings = load_settings()
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
        "integrations": INTEGRATION_HEALTH,
        "version": VERSION,
        "build": BUILD_LABEL,
        "asset_v": ASSET_VERSION,
    }
    return templates.TemplateResponse(request, "index.html", context)


@app.get("/pick")
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


@app.get("/api/tile")
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


@app.get("/api/details")
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


@app.get("/api/state")
async def get_state(request: Request):
    today = date.today()
    year = _valid_year(request.query_params.get("year"), today.year)
    month = _valid_month(request.query_params.get("month"), today.month)
    endpoint = get_endpoint(request.query_params.get("endpoint"))
    return JSONResponse(state_store.load_state(endpoint.key, year, month))


@app.post("/api/state")
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


@app.get("/api/settings")
async def get_settings():
    return JSONResponse(load_settings().to_dict())


@app.post("/api/settings")
async def post_settings(request: Request):
    ctype = request.headers.get("content-type", "")
    if "application/json" in ctype:
        data = await request.json()
    else:
        form = await request.form()
        data = dict(form)
    # Merge onto current settings so partial saves keep untouched fields.
    current = load_settings().to_dict()
    current.update(data)
    settings = Settings.from_dict(current)
    save_settings(settings)
    # Re-check Sonarr/Radarr immediately so buttons reflect the new config right away.
    await refresh_integration_health()
    return JSONResponse({"ok": True, "settings": settings.to_dict()})


@app.get("/api/integrations/status")
async def integrations_status():
    """Cached Sonarr/Radarr health (refreshed by the heartbeat + on save)."""
    return JSONResponse(INTEGRATION_HEALTH)


@app.get("/api/integrations/library")
async def integrations_library():
    """Ids already in each library, so the UI can mark added items (TTL-cached)."""
    await refresh_library()
    return JSONResponse({k: LIBRARY_CACHE[k] for k in ("sonarr", "radarr", "seer")})


@app.post("/api/integrations/options")
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


@app.post("/api/integrations/add")
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
