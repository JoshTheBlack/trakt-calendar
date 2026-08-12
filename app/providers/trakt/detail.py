"""Per-title lookups of PUBLIC Trakt data: summaries, cast, episode lists,
season cadence and search.

Everything here is the same for everybody, which is why all of it caches. The
reads that depend on WHOSE token asked live in sync.py and never touch the
shared cache — that is the line between these two modules, not "detail vs
tracker": the tracker's season cadence is public show data and belongs here.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime
from zoneinfo import ZoneInfo

import httpx

from ...config import Settings
from .. import season as season_rules
from ..base import Media
from . import transport

logger = logging.getLogger(__name__)


def _headshot(person: dict) -> str | None:
    imgs = (person.get("images") or {}).get("headshot") or []
    if imgs:
        url = imgs[0]
        return url if url.startswith("http") else "https://" + url
    return None


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
    """Compact season info for a tile. Movies have no seasons."""
    if media == "movie" or season is None:
        return {"episode_count": None, "first_aired": None, "last_aired": None, "next_aired": None}
    tz = ZoneInfo(settings.timezone)
    episodes = await transport.cached_get(
        transport.shared_client(), settings, f"shows/{trakt_id}/seasons/{season}", {"extended": "full"},
    )
    if not isinstance(episodes, list):
        return {"episode_count": None, "first_aired": None, "last_aired": None, "next_aired": None}
    return {"season": season, **_summarize_season(episodes, tz)}


def _cast_from(people: dict) -> list[dict]:
    """The modal's cast list from a /people response: top-billed 16, name +
    character + headshot. `characters` (a list) is Trakt's newer shape and
    `character` (a string) the older one; both still arrive."""
    cast = []
    for member in (people.get("cast") or [])[:16]:
        person = member.get("person") or {}
        character = member.get("character") or (member.get("characters") or [""])[0]
        cast.append({
            "name": person.get("name") or "",
            "character": character,
            "headshot": _headshot(person),
        })
    return cast


def _episodes_from(episodes_raw, tz: ZoneInfo) -> list[dict]:
    """The modal's episode list from a season response. An episode with no or an
    unparseable air date gets an empty display string rather than being dropped —
    an unscheduled episode still exists and still belongs in the list."""
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
    return episodes


async def fetch_details(settings: Settings, media: str, trakt_id: str, season: int | None,
                        cache_only: bool = False) -> dict:
    """Full detail payload for the modal: overview, cast, episode list.

    `cache_only=True` serves purely from cache and never calls Trakt — the mode a
    public share page uses so a visitor's click reuses what the owner's own views
    already cached rather than spending the owner's rate limit. Fields with no
    cached source come back empty, and the modal renders around them."""
    tz = ZoneInfo(settings.timezone)
    base = "movies" if media == "movie" else "shows"
    client = transport.shared_client()
    tasks = {
        "info": transport.cached_get(
            client, settings, f"{base}/{trakt_id}", {"extended": "full"}, cache_only=cache_only),
        "people": transport.cached_get(
            client, settings, f"{base}/{trakt_id}/people", {"extended": "full"}, cache_only=cache_only),
    }
    if media != "movie" and season is not None:
        tasks["episodes"] = transport.cached_get(
            client, settings, f"shows/{trakt_id}/seasons/{season}", {"extended": "full"},
            cache_only=cache_only)
    results = dict(zip(tasks.keys(), await asyncio.gather(*tasks.values())))

    info = results.get("info") or {}

    return {
        "title": info.get("title") or "",
        "year": info.get("year") or "",
        "overview": (info.get("overview") or "").strip(),
        "status": (info.get("status") or "").replace("_", " ").title(),
        "network": info.get("network") or "",
        "runtime": info.get("runtime"),
        "genres": [g.replace("-", " ").title() for g in (info.get("genres") or [])],
        "rating": round(float(info["rating"]), 1) if info.get("rating") else None,
        "certification": (info.get("certification") or "").upper(),
        "trailer": info.get("trailer") or "",
        "homepage": info.get("homepage") or "",
        "season": season,
        "cast": _cast_from(results.get("people") or {}),
        "episodes": _episodes_from(results.get("episodes") or [], tz),
    }


# ---------------------------------------------------------------------------
# Season cadence/date derivation — what the tracker's tiles show. Shows only.
# ---------------------------------------------------------------------------

# Season calls get a SHORT TTL: totals grow over time, so a day-old total is
# fine, but we don't want to hold a season's episode list for the 12h detail TTL.
SEASON_CACHE_TTL_SECONDS = 24 * 60 * 60

def _parse_air_date(first_aired, tz: ZoneInfo) -> date | None:
    """Trakt's ISO-UTC `first_aired` -> local calendar date, or None if missing."""
    if not first_aired:
        return None
    try:
        return datetime.fromisoformat(str(first_aired).replace("Z", "+00:00")).astimezone(tz).date()
    except ValueError:
        return None


def _derive_season(episodes: list[dict], tz: ZoneInfo, now: datetime | None = None) -> dict:
    """A season's cadence/date fields from Trakt's raw episode list. No I/O —
    unit-tested directly.

    The rule itself lives in app/providers/season.py, because it is about air
    dates rather than about Trakt: what belongs here is knowing that Trakt spells
    an episode's air date `first_aired`, in ISO-UTC. One episode in, one date out
    — INCLUDING None for an episode Trakt has not dated yet, since the count of
    entries is the season's episode total and dropping the undated ones would
    make a half-announced season look fully scheduled.
    """
    return season_rules.derive_season(
        [_parse_air_date(ep.get("first_aired"), tz) for ep in (episodes or [])],
        (now or datetime.now(tz)).date(),
    )


def _empty_season(season: int) -> dict:
    return season_rules.empty_season(season)


async def fetch_season_detail(settings: Settings, trakt_id, season: int, fresh: bool = False,
                              client: httpx.AsyncClient | None = None) -> dict:
    """One /shows/{id}/seasons/{season}?extended=full call (short TTL) reduced to
    the fields: total (y), cadence, premiere, finale, started/finished. Pass a
    shared `client` when batching (else a throwaway one is created)."""
    tz = ZoneInfo(settings.timezone)
    c = client or transport.shared_client()
    episodes = await transport.cached_get(
        c, settings, f"shows/{trakt_id}/seasons/{season}", {"extended": "full"},
        ttl_seconds=SEASON_CACHE_TTL_SECONDS, fresh=fresh,
    )
    if not isinstance(episodes, list):
        return _empty_season(season)
    return {"season": season, **_derive_season(episodes, tz)}


async def fetch_show_seasons(settings: Settings, trakt_id) -> list[dict]:
    """/shows/{id}/seasons?extended=full -> [{season, episode_count}] for
    seasons Trakt has actually populated with episodes (skips season 0/
    specials and any season with zero KNOWN episodes at all). Powers the
    add-show flow's season picker.

    Filters on `episode_count` (Trakt's total planned/known episode count for
    the season), NOT `aired_episodes`. A season that hasn't premiered yet has
    aired_episodes=0 but a real episode_count once Trakt has announced it —
    filtering on aired_episodes wrongly hid every not-yet-aired season from
    the picker, which is exactly a season 1 that has not started airing yet.
    Fixed once manual add-show on an unaired season turned out to be broken."""
    results = await transport.cached_get(
        transport.shared_client(), settings, f"shows/{trakt_id}/seasons", {"extended": "full"}, raise_errors=True,
    )
    out = []
    for entry in results if isinstance(results, list) else []:
        num = entry.get("number")
        episode_count = entry.get("episode_count") or 0
        if num is None or num == 0 or episode_count <= 0:
            continue
        out.append({"season": int(num), "episode_count": int(episode_count)})
    out.sort(key=lambda s: s["season"])
    logger.info("fetch_show_seasons(%s) -> %d usable season(s)", trakt_id, len(out))
    return out


SEARCH_MEDIA = tuple(Media)


def ids_map(media: dict) -> dict:
    """Every id Trakt knows for a title, with the empty ones dropped.

    The whole map travels rather than the one id a given caller happens to want:
    an id we discard here is one a future match against another service cannot
    use, and re-fetching it costs a call we have already paid for.
    """
    ids = media.get("ids") or {}
    return {key: value for key, value in ids.items() if value not in (None, "")}


async def search_titles(settings: Settings, media: str, query: str) -> list[dict]:
    """/search/{show|movie}?query=... -> [{media, ids, title, year, network,
    runtime, overview}], newest-match-first as Trakt orders it.

    ONE implementation for both media types. The two searches differ only in the
    path segment and in which key the result object hangs under, so a second
    copy shaped for movies would drift from this one the first time either is
    touched. Empty query returns [] without a call.
    """
    if media not in SEARCH_MEDIA:
        raise ValueError(f"Unknown media type {media!r}.")
    q = (query or "").strip()
    if not q:
        return []
    results = await transport.cached_get(
        transport.shared_client(), settings, f"search/{media}", {"query": q, "extended": "full"},
        raise_errors=True,
    )

    out = []
    for entry in results if isinstance(results, list) else []:
        item = entry.get(media) or {}
        ids = ids_map(item)
        if not ids:
            # Nothing to identify it by, so nothing downstream could store,
            # dedupe or look up artwork for it.
            continue
        out.append({
            "media": media,
            "ids": ids,
            "title": item.get("title") or "",
            "year": item.get("year"),
            # Movies have no network and shows no runtime worth showing, so each
            # simply comes back empty for the other — the caller decides which
            # it renders.
            "network": item.get("network") or "",
            "runtime": item.get("runtime"),
            "overview": (item.get("overview") or "").strip(),
        })
    logger.info("search_titles(%s, %r) -> %d raw / %d usable result(s)", media, q,
                len(results) if isinstance(results, list) else 0, len(out))
    return out


async def search_shows(settings: Settings, query: str) -> list[dict]:
    """/search/show?query=... -> compact [{ids, title, year, network}] for the
    add-show flow. Empty query returns [].

    Carries the whole id map rather than the two ids the add flow used to need:
    the tracker files a row under whichever shared id it can, so a result that had
    been flattened to a Trakt id could not be stored at all.
    """
    return [
        {"ids": entry["ids"], "title": entry["title"], "year": entry["year"],
         "network": entry["network"]}
        for entry in await search_titles(settings, "show", query)
        if entry["ids"].get("trakt") is not None
    ]


async def fetch_movie_summary(settings: Settings, trakt_id) -> dict | None:
    """/movies/{id}?extended=full,images -> the raw movie object, or None.

    The id resolution step for a movie known only by its Trakt id: ids never
    change, so this caches like any other detail lookup. `images` is asked for
    because the same response then answers "what is this movie's poster URL"
    without a second call.
    """
    data = await transport.cached_get(
        transport.shared_client(), settings, f"movies/{trakt_id}", {"extended": "full,images"},
    )
    return data if isinstance(data, dict) else None
