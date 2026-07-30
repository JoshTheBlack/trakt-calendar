"""Sonarr / Radarr / Seerr: the admin-only status, options and add endpoints,
plus the two in-memory caches they answer from.

The caches live here rather than beside the clients in app/arr.py and
app/seer.py because they are a fact about THIS PROCESS's view of those services —
refreshed by the heartbeat and after a settings save, read by the calendar page's
add buttons — while arr.py and seer.py are stateless callers that hold nothing.
Anything that wants either cache asks this module; that is what keeps "when was
this last refreshed" a question with one answer.

Every route here is ADMIN: these writes land in the operator's own shared
libraries and Seerr's requests all carry one app-wide API key, so they are an
administrator's affordance rather than a per-user one.
"""
from __future__ import annotations

import time

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from . import arr, authz, seer
from .auth import AuthLevel
from .config import load_settings

router = APIRouter()
guard = authz.Guard(router)

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


def invalidate_library_cache() -> None:
    """Force the next library read to re-pull rather than serve what is held.

    Called after credentials change: the cached ids were fetched with the old
    URL and API key, so keeping them until the TTL expires would leave the add
    buttons marked from a library this instance can no longer even reach.
    """
    LIBRARY_CACHE["_ts"] = 0.0


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
    data = await authz.json_body(request)
    kind = data.get("kind")
    if kind not in ("sonarr", "radarr"):
        return JSONResponse({"ok": False, "error": "Unknown service"}, status_code=400)
    url = (data.get("url") or "").strip()
    key = (data.get("api_key") or "").strip()
    if not (url and key):  # fall back to what's already saved
        url, key = arr.credentials(kind, load_settings())
    if not (url and key):
        return JSONResponse({"ok": False, "error": "Enter the URL and API key first."}, status_code=400)
    try:
        opts = await arr.fetch_options(url, key)
    # The four things an unreachable or wrong-service instance actually raises:
    # a transport failure, a body that is not JSON, and — when the URL turns out
    # to be some other web app answering 200 with JSON of its own — a profile
    # list whose entries have neither the keys nor the shape Sonarr's do.
    except (httpx.HTTPError, ValueError, KeyError, TypeError):
        return JSONResponse({"ok": False, "error": f"Couldn't reach {kind.title()} at {url} — check the URL and API key."}, status_code=502)
    return JSONResponse({"ok": True, **opts})


@guard.post("/api/integrations/add", AuthLevel.ADMIN)
async def integrations_add(request: Request):
    """Add a title to Sonarr (show/TVDB), Radarr (movie/TMDB), or Seerr (request/TMDB).

    Routed by `target`; falls back to the arr service implied by `media`.
    """
    data = await authz.json_body(request)
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
