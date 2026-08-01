"""Sonarr / Radarr integration — health checks, option lookup, and add-to-library.

Shows are added to Sonarr by TVDB id, movies to Radarr by TMDB id (both provided
by Trakt). Requires the instance URL + API key, plus a quality profile and root
folder, all configured from the Settings panel.
"""
from __future__ import annotations

import json

import anyio.to_thread
import httpx

from .. import http_pool
from ..config import Settings
from ..perftrace import span

# (base_url_attr, api_key_attr) per service
_SERVICE = {
    "sonarr": ("sonarr_url", "sonarr_api_key"),
    "radarr": ("radarr_url", "radarr_api_key"),
}

# ONE POOL PER SERVICE, not one for this module. Sonarr and Radarr are two
# separate instances on two hosts that happen to speak the same API, and this
# module is a single implementation of that API rather than a single service —
# so the pools are keyed the same way the credentials are.
#
# WHY THIS MODULE HAS POOLS AT ALL: it used to build a fresh httpx.AsyncClient
# for every call, and building one loads the system trust store INLINE, on the
# event loop. The library read below runs on a five-minute cycle whether or not
# anyone is using the app, so that cost was landing on unrelated requests
# forever. Small pools because these are one or two boxes on a LAN answering a
# handful of calls, not an API being fanned out to.
_POOLS = {kind: http_pool.Pool(kind, max_connections=4, timeout=20) for kind in _SERVICE}


def credentials(kind: str, settings: Settings) -> tuple[str, str]:
    url_attr, key_attr = _SERVICE[kind]
    return getattr(settings, url_attr).strip().rstrip("/"), getattr(settings, key_attr).strip()


def is_configured(kind: str, settings: Settings) -> bool:
    url, key = credentials(kind, settings)
    return bool(url and key)


async def check_health(kind: str, settings: Settings) -> dict:
    """Ping the instance; returns {configured, reachable}."""
    if not is_configured(kind, settings):
        return {"configured": False, "reachable": False}
    url, key = credentials(kind, settings)
    try:
        with span("arr.health", service=kind):
            resp = await _POOLS[kind].client().get(
                f"{url}/api/v3/system/status", headers={"X-Api-Key": key}, timeout=8)
        return {"configured": True, "reachable": resp.status_code == 200}
    except httpx.HTTPError:
        return {"configured": True, "reachable": False}


class LibraryUnavailable(Exception):
    """This service could not be read — as distinct from having nothing in it.

    THE WHOLE REASON THIS TYPE EXISTS: `library_ids` used to answer an
    unreachable Sonarr with an empty list, which its caller then cached as the
    truth. One timeout and the calendar quietly stopped marking anything as
    already-added, for as long as the cache held — the app confidently reporting
    an empty library it had never actually seen. An empty list must mean "this
    library is empty", so every other outcome has to be able to say so.
    """


def _ids_from(raw: bytes, field: str) -> list:
    """SYNCHRONOUS. The library's ids out of one raw JSON body.

    ITS OWN FUNCTION BECAUSE IT RUNS ON A WORKER THREAD. `/api/v3/movie` returns
    the FULL object for every title — images, ratings, the lot — so a library of a
    couple of thousand is megabytes of JSON, and parsing it is hundreds of
    milliseconds of CPU. Left on the event loop that is a stall for every other
    request in flight, which is exactly what the loop watchdog kept catching a few
    milliseconds after this read finished.
    """
    return [item[field] for item in json.loads(raw) if item.get(field)]


async def library_ids(kind: str, settings: Settings) -> list:
    """All ids already in the library — TVDB ids for Sonarr, TMDB ids for Radarr.

    Raises LibraryUnavailable when the answer is unknown rather than empty; an
    unconfigured service is a real, knowable empty.
    """
    if not is_configured(kind, settings):
        return []
    url, key = credentials(kind, settings)
    path = "series" if kind == "sonarr" else "movie"
    field = "tvdbId" if kind == "sonarr" else "tmdbId"
    try:
        with span("arr.library", service=kind) as sp:
            resp = await _POOLS[kind].client().get(
                f"{url}/api/v3/{path}", headers={"X-Api-Key": key})
            if resp.status_code != 200:
                raise LibraryUnavailable(f"{kind} returned HTTP {resp.status_code}")
            sp.set(bytes=len(resp.content))
            ids = await anyio.to_thread.run_sync(_ids_from, resp.content, field)
            sp.set(ids=len(ids))
            return ids
    except (httpx.HTTPError, ValueError) as exc:
        raise LibraryUnavailable(f"{kind} could not be read: {exc}") from exc


async def fetch_options(kind: str, url: str, key: str) -> dict:
    """Quality profiles + root folders, for the Settings dropdowns (explicit creds).

    Takes `kind` only to pick the right pool — the URL and key are the caller's,
    and may be a not-yet-saved value being tested. httpx pools per host, so
    probing an unsaved address costs this service's pool one host entry and
    nothing else.
    """
    url = url.strip().rstrip("/")
    headers = {"X-Api-Key": key.strip()}
    client = _POOLS[kind].client()
    with span("arr.options", service=kind):
        qp = await client.get(f"{url}/api/v3/qualityprofile", headers=headers, timeout=10)
        rf = await client.get(f"{url}/api/v3/rootfolder", headers=headers, timeout=10)
    profiles = [{"id": p["id"], "name": p["name"]} for p in qp.json()] if qp.status_code == 200 else []
    folders = [{"path": f["path"]} for f in rf.json()] if rf.status_code == 200 else []
    return {"profiles": profiles, "folders": folders}


def _error_text(resp: httpx.Response) -> str:
    try:
        body = resp.json()
        if isinstance(body, list) and body:
            return body[0].get("errorMessage") or body[0].get("message") or f"HTTP {resp.status_code}"
        if isinstance(body, dict):
            return body.get("message") or f"HTTP {resp.status_code}"
    except ValueError:
        pass
    return f"HTTP {resp.status_code}"


async def add_media(kind: str, settings: Settings, ids: dict, title: str) -> dict:
    """Look up the title in Sonarr/Radarr and add it to the library."""
    url, key = credentials(kind, settings)
    headers = {"X-Api-Key": key, "Content-Type": "application/json"}

    if kind == "sonarr":
        if not settings.sonarr_quality_profile_id or not settings.sonarr_root_folder.strip():
            return {"ok": False, "error": "Set a Sonarr quality profile and root folder in Settings first."}
        tvdb = ids.get("tvdb")
        if not tvdb:
            return {"ok": False, "error": "This show has no TVDB id, so Sonarr can't add it."}
        lookup_url, term = f"{url}/api/v3/series/lookup", f"tvdb:{tvdb}"
    else:
        if not settings.radarr_quality_profile_id or not settings.radarr_root_folder.strip():
            return {"ok": False, "error": "Set a Radarr quality profile and root folder in Settings first."}
        tmdb = ids.get("tmdb")
        if not tmdb:
            return {"ok": False, "error": "This movie has no TMDB id, so Radarr can't add it."}
        lookup_url, term = f"{url}/api/v3/movie/lookup", f"tmdb:{tmdb}"

    client = _POOLS[kind].client()
    try:
        with span("arr.add", service=kind):
            look = await client.get(lookup_url, params={"term": term}, headers={"X-Api-Key": key})
            if look.status_code != 200:
                return {"ok": False, "error": f"Lookup failed ({_error_text(look)})."}
            results = look.json()
            if not results:
                return {"ok": False, "error": f"{title} wasn't found in {kind.title()}."}
            found = results[0]
            if found.get("id"):
                return {"ok": True, "message": f"{title} is already in {kind.title()}."}

            if kind == "sonarr":
                payload = {
                    "title": found.get("title"),
                    "tvdbId": found.get("tvdbId"),
                    "titleSlug": found.get("titleSlug"),
                    "year": found.get("year"),
                    "images": found.get("images", []),
                    "seasons": found.get("seasons", []),
                    "qualityProfileId": settings.sonarr_quality_profile_id,
                    "languageProfileId": settings.sonarr_language_profile_id or 1,
                    "rootFolderPath": settings.sonarr_root_folder.strip(),
                    "monitored": True,
                    "seasonFolder": True,
                    "addOptions": {"searchForMissingEpisodes": True},
                }
                post_url = f"{url}/api/v3/series"
            else:
                payload = {
                    "title": found.get("title"),
                    "tmdbId": found.get("tmdbId"),
                    "titleSlug": found.get("titleSlug"),
                    "year": found.get("year"),
                    "images": found.get("images", []),
                    "qualityProfileId": settings.radarr_quality_profile_id,
                    "rootFolderPath": settings.radarr_root_folder.strip(),
                    "monitored": True,
                    "minimumAvailability": settings.radarr_minimum_availability or "released",
                    "addOptions": {"searchForMovie": True},
                }
                post_url = f"{url}/api/v3/movie"

            resp = await client.post(post_url, json=payload, headers=headers)
    except httpx.HTTPError as exc:
        return {"ok": False, "error": f"Could not reach {kind.title()}: {exc}"}

    if resp.status_code in (200, 201):
        return {"ok": True, "message": f"Added {title} to {kind.title()}."}
    return {"ok": False, "error": _error_text(resp)}
