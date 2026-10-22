"""TMDB's answers about ONE title: the details panel's field set, its videos, a
season's summary for the tracker, the season picker's list, the words a tracker
row is filed with, and calendar records for a search.

EVERYTHING HERE IS PUBLIC CATALOGUE DATA, the same for every viewer, read with
the instance's token and cached in the shared response cache (transport.py).

ONE CALL PER TITLE AND ONE PER SEASON. `/tv/{id}` with `append_to_response`
carries the ids, the ratings, the videos and the season list in one answer
(measured 8.5 KB), so the panel, the picker, the facts and the record builder
all read the SAME cached object — `title_payload` is the only place that asks
for it, so its URL, and therefore its cache key, is one string.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import date

from ...config import Settings
from .. import season as season_rules
from ..base import (Media, Record, SeasonsAnswer, TitleFacts, Video, proxied_image)
from . import normalize, transport

logger = logging.getLogger(__name__)

# What `/tv/{id}` and `/movie/{id}` are asked to carry beside the title itself.
# One list per medium because the two answers differ in where a rating lives
# (a show's `content_ratings`, a film's `release_dates`).
_APPENDS = {
    Media.SHOW: "external_ids,content_ratings,videos",
    Media.MOVIE: "external_ids,release_dates,videos",
}
_PATHS = {Media.SHOW: "tv", Media.MOVIE: "movie"}

# A season's episode list moves — an episode gains a date, a season gains an
# episode — so it is revalidated sooner than a title. A revalidation of an
# unchanged season is a 304 and costs TMDB no body.
SEASON_TTL_SECONDS = 60 * 60


async def title_payload(settings: Settings, tmdb_id, media: Media | str, *,
                        cache_only: bool = False) -> dict | None:
    """The raw `/tv/{id}` or `/movie/{id}` answer, or None when TMDB has no such
    title (404) or, under `cache_only`, nothing is stored. Raises TmdbError when
    TMDB could not be asked."""
    media = Media(media)
    if tmdb_id in (None, ""):
        return None
    try:
        payload = await transport.cached_get(
            settings, f"{_PATHS[media]}/{tmdb_id}",
            {"append_to_response": _APPENDS[media]}, cache_only=cache_only)
    except transport.TmdbNotFound:
        return None
    return payload if isinstance(payload, dict) else None


async def season_payload(settings: Settings, tmdb_id, season: int, *,
                         cache_only: bool = False, fresh: bool = False) -> dict | None:
    """The raw `/tv/{id}/season/{n}` answer, or None when TMDB holds no such
    season (404) or nothing is stored under `cache_only`. `fresh` revalidates
    whatever is stored — still a conditional GET, so unchanged costs nothing."""
    if tmdb_id in (None, ""):
        return None
    try:
        payload = await transport.cached_get(
            settings, f"tv/{tmdb_id}/season/{int(season)}",
            ttl_seconds=0 if fresh else SEASON_TTL_SECONDS, cache_only=cache_only)
    except transport.TmdbNotFound:
        return None
    return payload if isinstance(payload, dict) else None


async def fetch_videos(settings: Settings, media, tmdb_id, *,
                       cache_only: bool = False) -> list[Video]:
    """Every video TMDB lists for the title — from the same cached `/tv/{id}`
    answer the panel reads, so asking for the list alone costs nothing more."""
    payload = await title_payload(settings, tmdb_id, media, cache_only=cache_only)
    return normalize.videos_from(payload or {})


async def fetch_details(settings: Settings, media, tmdb_id, season: int | None, *,
                        cache_only: bool = False) -> dict:
    """One title as the details panel draws it — DetailPort's one key set.

    A SEASON'S OWN WORDS TRAVEL BESIDE THE TITLE'S, as the Trakt package's do:
    `overview` is the show's and `season_overview` the season's, from the
    `seasons` entry `/tv/{id}` already carries, and the episode rows come from
    that season's own call. Asked of a season TMDB holds nothing of, the answer
    has no episodes — detail_source.for_season then hands the season to the next
    service the title names.

    NO CAST. TMDB publishes credits, but the panel's cast is one more call per
    title and season that this answer does not make yet; the key is present and
    empty, which the renderer already draws around (a Simkl title's is too).
    """
    media = Media(media)
    per_season = media is Media.SHOW and season is not None
    payload, season_body = await asyncio.gather(
        title_payload(settings, tmdb_id, media, cache_only=cache_only),
        season_payload(settings, tmdb_id, int(season), cache_only=cache_only)
        if per_season else _nothing())
    info = payload or {}
    entry = normalize.season_entry(info, season if per_season else None)
    return {
        "title": normalize.title_of(info, media),
        "year": normalize.year_of(info, media),
        "overview": str(info.get("overview") or "").strip(),
        "season_overview": str(entry.get("overview") or "").strip(),
        # TMDB's status in Trakt's words, then title-cased for reading, the same
        # spelling the other packages hand the panel.
        "status": normalize.status_word(info.get("status")).title(),
        "network": normalize.network_of(info) if media is Media.SHOW else "",
        "country": normalize.country_code(info),
        "language": normalize.language_code(info),
        "anime_type": "",
        "release_types_by_country": (normalize.release_types_by_country(info)
                                     if media is Media.MOVIE else {}),
        "ids": normalize.ids_of(info),
        # THROUGH THE SHARED PROXY: this answer goes straight to a browser.
        "poster": proxied_image(normalize.poster_of(info)) or "",
        "runtime": normalize.runtime(info, media),
        "genres": [slug.replace("-", " ").title()
                   for slug in normalize.genre_slugs(info.get("genres"))],
        "genre_slugs": normalize.genre_slugs(info.get("genres")),
        "rating": normalize.rating(info),
        # TMDB publishes no IMDb score.
        "imdb_rating": None,
        "certification": normalize.certification(info, media),
        "videos": normalize.videos_from(info),
        "first_aired": normalize.first_aired(info, media),
        "homepage": str(info.get("homepage") or ""),
        "season": season,
        "season_title": normalize.season_name(entry),
        "season_count": len(normalize.picker_seasons(info)),
        "cast": [],
        "episodes": normalize.panel_episodes(season_body or {}),
    }


async def _nothing():
    return None


async def fetch_season_summary(settings: Settings, tmdb_id, season: int, media,
                               *, fresh: bool = False, today: date | None = None) -> dict:
    """One season reduced to app/providers/season.py's summary — what a tracker
    row's total, cadence and dates are drawn from.

    A SEASON TMDB DOES NOT HOLD IS AN EMPTY SEASON, NOT A FAILURE (its 404), and
    that difference is load-bearing: an empty answer is what lets
    detail_source.for_season hand the season to the next service the title
    names. MEASURED on a real roster: three seasons TMDB does not list (a
    numbering Simkl splits differently) are held by Simkl and keep its totals
    that way. A TMDB that could not be asked RAISES, so the row keeps its
    last-known numbers rather than recording a false zero.
    """
    if Media(media) is Media.MOVIE:
        return season_rules.empty_season(int(season))
    body = await season_payload(settings, tmdb_id, int(season), fresh=fresh)
    return normalize.season_summary(body or {}, int(season), today or date.today())


async def fetch_seasons(settings: Settings, tmdb_id, media, *,
                        cached_only: bool = False) -> SeasonsAnswer:
    """The season picker's answer, from `/tv/{id}`'s own `seasons` list. TMDB
    never says a title IS one season of a larger show, so `named_season` is
    None; `ids` and `network` are what this one answer surfaced."""
    if Media(media) is not Media.SHOW or not tmdb_id:
        return SeasonsAnswer(seasons=[], named_season=None, ids={}, network="")
    info = await title_payload(settings, tmdb_id, Media.SHOW, cache_only=cached_only) or {}
    return SeasonsAnswer(seasons=normalize.picker_seasons(info), named_season=None,
                         ids=normalize.ids_of(info), network=normalize.network_of(info))


async def fetch_title_facts(settings: Settings, tmdb_id, media) -> TitleFacts:
    """TMDB's own name for the title and, for a show, its network. Empty when
    TMDB does not know the id."""
    media = Media(media)
    info = await title_payload(settings, tmdb_id, media) or {}
    return TitleFacts(normalize.title_of(info, media),
                      normalize.network_of(info) if media is Media.SHOW else "")


async def records_for(settings: Settings, tmdb_id, media, moments) -> list[Record]:
    """Calendar records for one title at the given days, built by
    normalize.to_record — this source's one record builder — from the same
    cached title answer."""
    media = Media(media)
    info = await title_payload(settings, tmdb_id, media)
    if not info:
        return []
    out = []
    for season, when in moments:
        record = normalize.to_record(info, media, season, str(when or ""))
        if record is not None:
            out.append(record)
    return out
