"""Async Trakt API client + response normalizer.

Fetches a month of calendar items for the selected endpoint and normalizes the
(differently-shaped) show/movie responses into one uniform `Item` dict the
template can render regardless of endpoint (requirement D).
"""
from __future__ import annotations

import asyncio
import calendar
from datetime import datetime
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

import httpx

from . import cache
from .config import Settings
from .endpoints import Endpoint

API_BASE = "https://api.trakt.tv"


class TraktError(Exception):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


def _headers(settings: Settings) -> dict:
    return {
        "Authorization": f"Bearer {settings.trakt_access_token}",
        "trakt-api-version": "2",
        "trakt-api-key": settings.trakt_client_id,
        "Content-Type": "application/json",
        "User-Agent": "trakt-new-shows-py/2.0",
        "X-Pagination-Page": "1",
        "X-Pagination-Limit": str(settings.pagination_limit),
    }


def _build_url(endpoint: Endpoint, settings: Settings, start_date: str, days: int) -> str:
    path = f"{API_BASE}/calendars/all/{endpoint.path}/{start_date}/{days}"
    params = {"extended": "full,images"}
    if settings.genres:
        params["genres"] = settings.genres
    if settings.countries:
        params["countries"] = settings.countries
    return f"{path}?{urlencode(params)}"


async def fetch_calendar(endpoint: Endpoint, settings: Settings, year: int, month: int) -> list[dict]:
    """Fetch and normalize a month of calendar items for the given endpoint."""
    days = calendar.monthrange(year, month)[1]
    start_date = f"{year:04d}-{month:02d}-01"
    end_date = f"{year:04d}-{month:02d}-{days:02d}"
    url = _build_url(endpoint, settings, start_date, days)

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(url, headers=_headers(settings))
    if resp.status_code == 401:
        raise TraktError("Trakt rejected the credentials (401). Check Client ID / Access Token in Settings.", 401)
    if resp.status_code != 200:
        raise TraktError(f"Trakt API returned HTTP {resp.status_code}.", resp.status_code)

    try:
        raw = resp.json()
    except ValueError:
        raise TraktError("Trakt API returned an unreadable response.")
    if not isinstance(raw, list):
        raw = []

    tz = ZoneInfo(settings.timezone)
    items = [normalize(entry, endpoint, tz) for entry in raw]
    items = [i for i in items if i and start_date <= i["air_date"] <= end_date]

    # Network filter (requirement C: configurable) — case-sensitive match Trakt naming.
    if settings.network_filter:
        allow = set(settings.network_filter)
        items = [i for i in items if i["network"] in allow]

    items.sort(key=lambda i: i["air_ts"])
    return items


def _poster(media: dict) -> str | None:
    imgs = media.get("images") or {}
    posters = imgs.get("poster") or []
    if posters:
        url = posters[0]
        if not url.startswith("http"):
            url = "https://" + url
        return url
    return None


def normalize(entry: dict, endpoint: Endpoint, tz: ZoneInfo) -> dict | None:
    """Turn a raw Trakt calendar entry into the uniform Item shape."""
    media = entry.get(endpoint.media) or {}
    aired_raw = entry.get("first_aired") or entry.get("released")
    if not aired_raw or not media:
        return None

    # `released` (movies) is a plain date; `first_aired` is an ISO UTC timestamp.
    try:
        if "T" in str(aired_raw):
            dt = datetime.fromisoformat(str(aired_raw).replace("Z", "+00:00")).astimezone(tz)
        else:
            dt = datetime.fromisoformat(f"{aired_raw}T00:00:00+00:00").astimezone(tz)
    except ValueError:
        return None

    ids = media.get("ids") or {}
    episode = entry.get("episode") or {}
    ep_label = None
    ep_season = episode.get("season") if episode else None
    ep_number = episode.get("number") if episode else None
    if ep_season is not None and ep_number is not None:
        ep_label = f"S{int(ep_season):02d}E{int(ep_number):02d}"

    # Full overview is sent; cards clamp it via CSS, the poster-only panel scrolls it.
    overview = (media.get("overview") or "").strip()

    return {
        "media": endpoint.media,
        "id": ids.get("slug") or str(ids.get("trakt") or ""),
        "trakt_slug": ids.get("slug") or "",
        "trakt_id": ids.get("trakt"),
        "tvdb": ids.get("tvdb"),
        "tmdb": ids.get("tmdb"),
        "title": media.get("title") or "Untitled",
        "year": media.get("year") or "",
        "network": media.get("network") or "",
        "country": (media.get("country") or "").upper(),
        "language": (media.get("language") or "").upper(),
        "runtime": media.get("runtime"),
        "status": media.get("status") or "",
        "rating": round(float(media["rating"]), 1) if media.get("rating") else None,
        "genres": [g.replace("-", " ").title() for g in (media.get("genres") or [])],
        "overview": overview,
        "poster": _poster(media),
        "air_date": dt.strftime("%Y-%m-%d"),
        "air_ts": dt.timestamp(),
        "air_display": dt.strftime("%d %b %Y"),
        "air_time": dt.strftime("%H:%M"),
        "day_of_week": dt.strftime("%A"),
        "episode_label": ep_label,
        "episode_title": episode.get("title") or "",
        "season": int(ep_season) if ep_season is not None else None,
        "episode_number": int(ep_number) if ep_number is not None else None,
        "trakt_url": (
            f"https://trakt.tv/{'movies' if endpoint.media == 'movie' else 'shows'}/{ids.get('slug')}"
            if ids.get("slug") else "https://trakt.tv"
        ),
    }


# ---------------------------------------------------------------------------
# Phase 2: per-show detail lookups (cast, episodes) for the tile chip + modal.
# ---------------------------------------------------------------------------

def _headshot(person: dict) -> str | None:
    imgs = (person.get("images") or {}).get("headshot") or []
    if imgs:
        url = imgs[0]
        return url if url.startswith("http") else "https://" + url
    return None


async def _cached_get(client: httpx.AsyncClient, settings: Settings, path: str, params: dict):
    """GET a Trakt path (with disk caching keyed by path+params). Returns parsed JSON or None."""
    url = f"{API_BASE}/{path}?{urlencode(params)}"
    ttl = settings.cache_ttl_minutes * 60
    cached = cache.get(url, ttl)
    if cached is not None:
        return cached
    try:
        resp = await client.get(url, headers=_headers(settings))
    except httpx.HTTPError:
        return None
    if resp.status_code != 200:
        return None
    try:
        data = resp.json()
    except ValueError:
        return None
    cache.set(url, data)
    return data


def _summarize_season(episodes: list[dict], tz: ZoneInfo) -> dict:
    """Reduce a season's episode list to the tile summary: count + first/last/next air dates."""
    aired, upcoming, total = [], [], 0
    now = datetime.now(tz)
    for ep in episodes or []:
        total += 1
        fa = ep.get("first_aired")
        if not fa:
            continue
        try:
            dt = datetime.fromisoformat(str(fa).replace("Z", "+00:00")).astimezone(tz)
        except ValueError:
            continue
        (aired if dt <= now else upcoming).append(dt)
    return {
        "episode_count": total,
        "first_aired": min(aired).strftime("%d %b %Y") if aired else None,
        "last_aired": max(aired).strftime("%d %b %Y") if aired else None,
        "next_aired": min(upcoming).strftime("%d %b %Y") if upcoming else None,
    }


async def fetch_tile_info(settings: Settings, media: str, trakt_id: str, season: int | None) -> dict:
    """Compact season info for a tile (requirement F). Movies have no seasons."""
    if media == "movie" or season is None:
        return {"episode_count": None, "first_aired": None, "last_aired": None, "next_aired": None}
    tz = ZoneInfo(settings.timezone)
    async with httpx.AsyncClient(timeout=30) as client:
        episodes = await _cached_get(client, settings, f"shows/{trakt_id}/seasons/{season}", {"extended": "full"})
    if not isinstance(episodes, list):
        return {"episode_count": None, "first_aired": None, "last_aired": None, "next_aired": None}
    return {"season": season, **_summarize_season(episodes, tz)}


async def fetch_details(settings: Settings, media: str, trakt_id: str, season: int | None) -> dict:
    """Full detail payload for the modal (requirement G): overview, cast, episode list."""
    tz = ZoneInfo(settings.timezone)
    base = "movies" if media == "movie" else "shows"
    async with httpx.AsyncClient(timeout=30) as client:
        tasks = {
            "info": _cached_get(client, settings, f"{base}/{trakt_id}", {"extended": "full"}),
            "people": _cached_get(client, settings, f"{base}/{trakt_id}/people", {"extended": "full"}),
        }
        if media != "movie" and season is not None:
            tasks["episodes"] = _cached_get(client, settings, f"shows/{trakt_id}/seasons/{season}", {"extended": "full"})
        results = dict(zip(tasks.keys(), await asyncio.gather(*tasks.values())))

    info = results.get("info") or {}
    people = results.get("people") or {}
    episodes_raw = results.get("episodes") or []

    cast = []
    for member in (people.get("cast") or [])[:16]:
        person = member.get("person") or {}
        character = member.get("character") or (member.get("characters") or [""])[0]
        cast.append({
            "name": person.get("name") or "",
            "character": character,
            "headshot": _headshot(person),
        })

    episodes = []
    for ep in episodes_raw if isinstance(episodes_raw, list) else []:
        fa = ep.get("first_aired")
        air_display = ""
        if fa:
            try:
                air_display = datetime.fromisoformat(str(fa).replace("Z", "+00:00")).astimezone(tz).strftime("%d %b %Y")
            except ValueError:
                air_display = ""
        episodes.append({
            "number": ep.get("number"),
            "title": ep.get("title") or f"Episode {ep.get('number')}",
            "air_display": air_display,
            "rating": round(float(ep["rating"]), 1) if ep.get("rating") else None,
            "overview": (ep.get("overview") or "").strip(),
        })

    return {
        "title": info.get("title") or "",
        "year": info.get("year") or "",
        "overview": (info.get("overview") or "").strip(),
        "status": (info.get("status") or "").replace("_", " ").title(),
        "network": info.get("network") or "",
        "runtime": info.get("runtime"),
        "genres": [g.replace("-", " ").title() for g in (info.get("genres") or [])],
        "rating": round(float(info["rating"]), 1) if info.get("rating") else None,
        "trailer": info.get("trailer") or "",
        "homepage": info.get("homepage") or "",
        "season": season,
        "cast": cast,
        "episodes": episodes,
    }
