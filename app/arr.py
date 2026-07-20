"""Sonarr / Radarr integration — health checks, option lookup, and add-to-library.

Shows are added to Sonarr by TVDB id, movies to Radarr by TMDB id (both provided
by Trakt). Requires the instance URL + API key, plus a quality profile and root
folder, all configured from the Settings panel.
"""
from __future__ import annotations

import httpx

from .config import Settings

# (base_url_attr, api_key_attr) per service
_SERVICE = {
    "sonarr": ("sonarr_url", "sonarr_api_key"),
    "radarr": ("radarr_url", "radarr_api_key"),
}


def _base(kind: str, settings: Settings) -> tuple[str, str]:
    url_attr, key_attr = _SERVICE[kind]
    return getattr(settings, url_attr).strip().rstrip("/"), getattr(settings, key_attr).strip()


def is_configured(kind: str, settings: Settings) -> bool:
    url, key = _base(kind, settings)
    return bool(url and key)


async def check_health(kind: str, settings: Settings) -> dict:
    """Ping the instance; returns {configured, reachable}."""
    if not is_configured(kind, settings):
        return {"configured": False, "reachable": False}
    url, key = _base(kind, settings)
    try:
        async with httpx.AsyncClient(timeout=8) as client:
            resp = await client.get(f"{url}/api/v3/system/status", headers={"X-Api-Key": key})
        return {"configured": True, "reachable": resp.status_code == 200}
    except httpx.HTTPError:
        return {"configured": True, "reachable": False}


async def library_ids(kind: str, settings: Settings) -> list:
    """All ids already in the library — TVDB ids for Sonarr, TMDB ids for Radarr."""
    if not is_configured(kind, settings):
        return []
    url, key = _base(kind, settings)
    path = "series" if kind == "sonarr" else "movie"
    field = "tvdbId" if kind == "sonarr" else "tmdbId"
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.get(f"{url}/api/v3/{path}", headers={"X-Api-Key": key})
        if resp.status_code != 200:
            return []
        return [item[field] for item in resp.json() if item.get(field)]
    except (httpx.HTTPError, ValueError):
        return []


async def fetch_options(url: str, key: str) -> dict:
    """Quality profiles + root folders, for the Settings dropdowns (explicit creds)."""
    url = url.strip().rstrip("/")
    headers = {"X-Api-Key": key.strip()}
    async with httpx.AsyncClient(timeout=10) as client:
        qp = await client.get(f"{url}/api/v3/qualityprofile", headers=headers)
        rf = await client.get(f"{url}/api/v3/rootfolder", headers=headers)
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
    url, key = _base(kind, settings)
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

    try:
        async with httpx.AsyncClient(timeout=20) as client:
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
