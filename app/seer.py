"""Seerr (Overseerr / Jellyseerr lineage) request integration.

Requests are made by TMDB id for both shows and movies via the /api/v1/request
endpoint, authenticated with an X-Api-Key header.
"""
from __future__ import annotations

import httpx

from .config import Settings


def _base(settings: Settings) -> tuple[str, str]:
    return settings.seer_url.strip().rstrip("/"), settings.seer_api_key.strip()


def is_configured(settings: Settings) -> bool:
    url, key = _base(settings)
    return bool(url and key)


async def check_health(settings: Settings) -> dict:
    if not is_configured(settings):
        return {"configured": False, "reachable": False}
    url, key = _base(settings)
    try:
        async with httpx.AsyncClient(timeout=8) as client:
            resp = await client.get(f"{url}/api/v1/status", headers={"X-Api-Key": key})
        return {"configured": True, "reachable": resp.status_code == 200}
    except httpx.HTTPError:
        return {"configured": True, "reachable": False}


async def library_ids(settings: Settings) -> list:
    """All TMDB ids already known to Seerr (requested or available), paginated."""
    if not is_configured(settings):
        return []
    url, key = _base(settings)
    headers = {"X-Api-Key": key}
    ids: list = []
    skip = 0
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            while True:
                resp = await client.get(f"{url}/api/v1/media", params={"take": 100, "skip": skip}, headers=headers)
                if resp.status_code != 200:
                    break
                data = resp.json()
                results = data.get("results", [])
                ids.extend(m["tmdbId"] for m in results if m.get("tmdbId"))
                total = (data.get("pageInfo") or {}).get("results", 0)
                skip += len(results)
                if not results or skip >= total or skip > 10000:  # safety cap
                    break
    except (httpx.HTTPError, ValueError):
        return ids
    return ids


async def add_media(settings: Settings, media: str, tmdb, title: str) -> dict:
    """Create a Seerr request. Shows request all seasons; both use the TMDB id."""
    url, key = _base(settings)
    if not tmdb:
        thing = "movie" if media == "movie" else "show"
        return {"ok": False, "error": f"This {thing} has no TMDB id, so Seerr can't request it."}
    payload = {"mediaType": "movie" if media == "movie" else "tv", "mediaId": int(tmdb)}
    if media != "movie":
        payload["seasons"] = "all"
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.post(
                f"{url}/api/v1/request",
                json=payload,
                headers={"X-Api-Key": key, "Content-Type": "application/json"},
            )
    except httpx.HTTPError as exc:
        return {"ok": False, "error": f"Could not reach Seerr: {exc}"}

    if resp.status_code in (200, 201):
        return {"ok": True, "message": f"Requested {title} on Seerr."}
    if resp.status_code == 409:  # already exists / requested
        return {"ok": True, "message": f"{title} is already on Seerr."}
    try:
        body = resp.json()
        msg = body.get("message") if isinstance(body, dict) else None
    except ValueError:
        msg = None
    return {"ok": False, "error": msg or f"HTTP {resp.status_code}"}
