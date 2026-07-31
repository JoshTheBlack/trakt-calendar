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

import asyncio
import logging
import time

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from . import arr, authz, seer
from .auth import AuthLevel
from .config import load_settings

logger = logging.getLogger(__name__)

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

# How long an id stays believed-present after the last successful read that saw
# it. LONGER THAN THE TTL ON PURPOSE, and the gap between the two numbers is the
# whole idea: the TTL says how often we ask, this says how long an answer keeps
# counting when a later ask fails or comes back short. A title in Sonarr today is
# still in Sonarr in an hour — additions are routine, removals are rare — so the
# expensive mistake is not "held a stale id", it is "dropped every id because one
# request timed out" and told the operator their library was empty.
LIBRARY_MEMORY_SECONDS = 3600

# id -> the last time a successful read saw it, per service. This is what makes a
# failed refresh degrade to STALE rather than to WRONG: a read that raises leaves
# these untouched, and the served list is whatever is still inside the window.
_seen_at: dict[str, dict] = {"sonarr": {}, "radarr": {}, "seer": {}}

# The refresh in flight right now, or None — see _spawn_refresh for why a
# timestamp alone could not answer the question this holds.
_refresh_task = None


def _remember(kind: str, ids, now: float) -> None:
    """Fold a successful read into the memory, stamping everything it saw."""
    seen = _seen_at[kind]
    for value in ids:
        seen[value] = now


def _remembered(kind: str, now: float) -> list:
    """What this service is believed to hold, dropping anything not seen inside
    the memory window. Sorted so the payload is stable between calls and a
    diffing client is not handed a reshuffle as a change."""
    seen = _seen_at[kind]
    for value, at in list(seen.items()):
        if now - at > LIBRARY_MEMORY_SECONDS:
            del seen[value]
    return sorted(seen)


async def _read_all(now: float) -> None:
    """One pass over all three services, each independent of the others.

    PER SERVICE, NOT ALL-OR-NOTHING: Seerr being down must not discard Sonarr's
    answer, so each read is caught on its own and only a SUCCESSFUL one updates
    the memory. A failed one still rebuilds the served list from the memory, so
    that a service which has been unreachable for longer than
    LIBRARY_MEMORY_SECONDS empties out gradually instead of being served forever.

    NOTHING ESCAPES THIS. It runs as a bare task with nobody awaiting it, so an
    exception here would surface only as asyncio's "task exception was never
    retrieved" at some unrelated moment.
    """
    settings = load_settings()
    reads = (
        ("sonarr", lambda: arr.library_ids("sonarr", settings)),
        ("radarr", lambda: arr.library_ids("radarr", settings)),
        ("seer", lambda: seer.library_ids(settings)),
    )
    for kind, read in reads:
        try:
            _remember(kind, await read(), now)
        except (arr.LibraryUnavailable, seer.LibraryUnavailable) as exc:
            # Not a warning: an unconfigured or briefly unreachable service is an
            # ordinary state for a self-hosted box, and this runs whenever anyone
            # opens the calendar. The served list simply stays as it was.
            logger.info("Library read skipped for %s: %s", kind, exc)
        except Exception:
            # Anything else is a bug rather than a sulking service, so it is loud
            # — but it still must not cost the other two services their read.
            logger.warning("Library read for %s failed unexpectedly", kind, exc_info=True)
        LIBRARY_CACHE[kind] = _remembered(kind, now)
    LIBRARY_CACHE["_ts"] = now


def library_snapshot() -> dict:
    """What the add buttons should show, RIGHT NOW, without ever waiting.

    Serves whatever is cached and starts a refresh BEHIND the response when the
    TTL has lapsed. That is the difference between the calendar's add-state
    costing a page load nothing and costing it a three-service round trip against
    services this app does not control and cannot hurry.

    REFRESHING FROM HERE — off a request — RATHER THAN OFF THE HEARTBEAT IS
    DELIBERATE. The heartbeat runs every minute forever, so owning the refresh
    there would mean reading three libraries around the clock on an instance
    nobody has open. Driven from the read, the work happens when somebody is
    actually looking and stops when they are not, and no request ever pays for it.
    """
    if (time.time() - LIBRARY_CACHE["_ts"]) >= LIBRARY_TTL:
        _spawn_refresh()
    return {k: LIBRARY_CACHE[k] for k in ("sonarr", "radarr", "seer")}


# asyncio keeps only a weak reference to a running task, so one nobody else holds
# can be collected mid-flight. This is that reference.
_background: set = set()


def _spawn_refresh() -> None:
    """Start a refresh unless one is already running — the single flight.

    THE GUARD IS THE POINT. The TTL check above is a timestamp, and a timestamp
    cannot say "somebody is already fetching this": several requests arriving in
    the same second after it lapses all read it as stale and all start their own
    three-service read. That is what two overlapping four-second refreshes in the
    logs were.
    """
    global _refresh_task
    if _refresh_task is not None and not _refresh_task.done():
        return
    _refresh_task = asyncio.create_task(_read_all(time.time()))
    _background.add(_refresh_task)
    _refresh_task.add_done_callback(_background.discard)


def invalidate_library_cache() -> None:
    """Force the next library read to re-pull rather than serve what is held.

    Called after credentials change: the cached ids were fetched with the old URL
    and API key, so keeping them until the TTL expires would leave the add buttons
    marked from a library this instance can no longer even reach.

    THE MEMORY GOES, THE SERVED LISTS STAY. New credentials mean a DIFFERENT
    library, so an id remembered from the old one is not stale — it is about
    somewhere else, and unioning it into the next read would keep it alive for an
    hour. But emptying what is currently SERVED would un-mark every add button
    between the save and the next successful read, and that window got longer when
    the route stopped waiting for the refresh. So the lists stand until something
    replaces them, which is the next successful read.
    """
    LIBRARY_CACHE["_ts"] = 0.0
    for seen in _seen_at.values():
        seen.clear()


@guard.get("/api/integrations/status", AuthLevel.ADMIN)
async def integrations_status():
    """Cached Sonarr/Radarr health (refreshed by the heartbeat + on save)."""
    return JSONResponse(INTEGRATION_HEALTH)


@guard.get("/api/integrations/library", AuthLevel.ADMIN)
async def integrations_library():
    """Ids already in each library, so the UI can mark added items.

    Answers from cache immediately and refreshes behind the response when it has
    aged out — this route is polled on a timer by every open calendar page, so a
    caller that WAITED for the refresh would hand one unlucky poll in each cycle
    the full cost of reading three services.
    """
    return JSONResponse(library_snapshot())


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
        opts = await arr.fetch_options(kind, url, key)
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
    # THE MEMORY IS STAMPED TOO, not just the served list: the served list is
    # rebuilt from the memory on every successful read, so an id appended here and
    # nowhere else would survive exactly until the next refresh and then vanish —
    # the button un-marking itself minutes after a successful add.
    if result.get("ok"):
        lib_id = data.get("tvdb") if target == "sonarr" else data.get("tmdb")
        if lib_id is not None:
            try:
                lib_id = int(lib_id)
            except (TypeError, ValueError):
                pass
            else:
                _seen_at[target][lib_id] = time.time()
                if lib_id not in LIBRARY_CACHE[target]:
                    LIBRARY_CACHE[target].append(lib_id)

    return JSONResponse(result, status_code=200 if result.get("ok") else 502)
