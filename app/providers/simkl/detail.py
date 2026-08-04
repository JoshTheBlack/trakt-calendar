"""Per-title lookups of PUBLIC Simkl data: a title's full episode list, and the
season summary the tracker's tiles are drawn from.

EVERYTHING HERE IS THE SAME FOR EVERYBODY, which is why all of it caches and all
of it goes through CATALOG_POOL — the pool for the Cloudflare-cached half of
Simkl, where parallel requests are explicitly allowed. The reads that depend on
WHOSE token asked live in sync.py, go through SYNC_POOL, and never touch the
shared cache. That is the line between the two modules, and it is the same line
the Trakt package beside this one draws: a season's episode list is a fact about
the show and is identical for every viewer, while a progress record is one
person's and must never reach a URL-keyed cache.

These endpoints take a client id and NO bearer token, so this half of Simkl
spends nobody's personal quota and works on an instance where nobody has linked
an account at all.
"""
from __future__ import annotations

import logging
from datetime import date, datetime

from ...config import Settings
from .. import season as season_rules
from ..base import Media
from . import transport

logger = logging.getLogger(__name__)

# Episode lists are catalogue data and barely move — an episode gains a date, a
# season gains an episode — so they are held far longer than the app's default
# response TTL. A day is short enough that a newly announced date is picked up
# without anybody asking, and long enough that a roster of fifty seasons does not
# re-fetch fifty lists on every page load. The same reasoning, and the same
# number, as the Trakt package's season cache.
EPISODES_CACHE_TTL_SECONDS = 24 * 60 * 60

# Which path answers for which kind of title. Anime is a separate catalogue in
# Simkl with its own episode endpoint, so a title we only know as a "show" is
# asked about at the TV path — an anime id asked there comes back as an empty
# list rather than as wrong data, and the season summary degrades to "nothing
# known", which is what an unanswerable lookup should look like.
_EPISODE_PATHS = {Media.SHOW: "tv/episodes", Media.MOVIE: None}

# Simkl marks a special with type "special"; specials have no place in a season's
# episode COUNT, exactly as the Trakt reads ask for specials to be excluded.
_REGULAR_EPISODE = "episode"


def _episode_date(entry: dict) -> date | None:
    """One episode's air date as a plain calendar date, or None when Simkl has
    not dated it yet.

    THE DATE IS TAKEN AND THE TIME IS DROPPED, deliberately. Simkl expresses a
    whole file's times in one fixed offset rather than in each title's own zone,
    so the instant is approximate while the calendar day is reliable; a cadence
    derived from the day is right, and one derived from a converted instant would
    be a coin flip for anything airing near midnight.
    """
    raw = entry.get("date")
    if not raw:
        return None
    try:
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00")).date()
    except ValueError:
        return None


async def fetch_episodes(settings: Settings, simkl_id, media: Media | str = Media.SHOW) -> list[dict]:
    """Every episode Simkl knows for a title, aired and unaired, as it sends them.

    Returns [] for a movie, for a title with no id, and for an id Simkl does not
    know — the last of those is Simkl's own answer (it returns an empty list for
    an unknown id) rather than a failure, and the callers here cannot tell the
    three apart because none of them would do anything different.
    """
    path = _EPISODE_PATHS.get(Media(media))
    if path is None or simkl_id in (None, ""):
        return []
    episodes = await transport.cached_get(
        transport.catalog_client(), settings, f"{path}/{simkl_id}", {},
        pool=transport.CATALOG_POOL, ttl_seconds=EPISODES_CACHE_TTL_SECONDS,
    )
    return episodes if isinstance(episodes, list) else []


def _season_air_dates(episodes: list[dict], season: int) -> list[date | None]:
    """One entry per regular episode of `season`, in episode order: its air date,
    or None when it has none.

    ANIME HAS NO SEASON NUMBERS. Simkl treats an anime title as one canonical
    season and omits `season` from its episodes entirely, so an episode with no
    season number is read as belonging to the season being asked about. That is
    correct for anime and harmless for TV, where the field is always present.
    """
    rows = []
    for entry in episodes or []:
        if str(entry.get("type") or _REGULAR_EPISODE) != _REGULAR_EPISODE:
            continue
        number = entry.get("episode")
        if number is None:
            continue
        entry_season = entry.get("season")
        if entry_season is not None and int(entry_season) != int(season):
            continue
        rows.append((int(number), _episode_date(entry)))
    rows.sort(key=lambda pair: pair[0])
    return [air_date for _number, air_date in rows]


async def fetch_season_detail(settings: Settings, simkl_id, season: int,
                              media: Media | str = Media.SHOW,
                              today: date | None = None) -> dict:
    """One season reduced to what a tracker tile shows: the episode total (y),
    the cadence, the premiere and finale dates, and whether either has passed.

    THE SAME KEYS THE TRAKT PACKAGE'S fetch_season_detail RETURNS, because the
    tracker merges whichever of them answered into one row and a key present on
    only one source would read as a template bug rather than as an unanswered
    lookup. The derivation itself is shared (app/providers/season.py); what this
    function owns is knowing how Simkl spells an episode and its date.

    `fresh` has no counterpart here on purpose: the Trakt call takes one because
    the tracker's Refresh button re-reads a viewer's progress, and progress is
    not what this returns. An episode list is catalogue data on a day-long TTL
    and re-fetching it on a button press would spend the instance's Simkl budget
    to learn nothing.
    """
    episodes = await fetch_episodes(settings, simkl_id, media)
    if not episodes:
        return season_rules.empty_season(int(season))
    return {
        "season": int(season),
        **season_rules.derive_season(
            _season_air_dates(episodes, int(season)), today or date.today()),
    }


def seasons_known(episodes: list[dict]) -> list[int]:
    """The season numbers a title's episode list actually contains, in order.

    An episode with no season number counts as season 1, which is how anime
    arrives: Simkl maps an anime title to one canonical season and omits the
    field. Naming that here rather than at each caller keeps "what season is this
    anime episode in" answered once.
    """
    seasons = set()
    for entry in episodes or []:
        if str(entry.get("type") or _REGULAR_EPISODE) != _REGULAR_EPISODE:
            continue
        seasons.add(int(entry.get("season") if entry.get("season") is not None else 1))
    return sorted(seasons)
