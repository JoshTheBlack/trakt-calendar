"""Per-title lookups of PUBLIC Simkl data: a title's full episode list, the
season summary the tracker's tiles are drawn from, and the field set the detail
modal draws.

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
from . import titles, transport

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


async def fetch_episodes(settings: Settings, simkl_id, media: Media | str = Media.SHOW,
                         *, cache_only: bool = False) -> list[dict]:
    """Every episode Simkl knows for a title, aired and unaired, as it sends them.

    Returns [] for a movie, for a title with no id, and for an id Simkl does not
    know — the last of those is Simkl's own answer (it returns an empty list for
    an unknown id) rather than a failure, and the callers here cannot tell the
    three apart because none of them would do anything different.

    `cache_only=True` makes no outbound call and adds a fourth way to get [] —
    nothing cached — which the callers again treat the same. It is what the public
    share pages read with, so a visitor's click can never spend the instance's
    Simkl budget.
    """
    path = _EPISODE_PATHS.get(Media(media))
    if path is None or simkl_id in (None, ""):
        return []
    episodes = await transport.cached_get(
        transport.catalog_client(), settings, f"{path}/{simkl_id}", {},
        pool=transport.CATALOG_POOL, ttl_seconds=EPISODES_CACHE_TTL_SECONDS,
        cache_only=cache_only,
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


# ---------------------------------------------------------------------------
# The detail modal's field set — what a card opens on when Simkl is the only
# service that listed it.
# ---------------------------------------------------------------------------

# How an episode's air date is written in the modal's list. The same strftime the
# Trakt package uses for the same row, so two cards' episode lists read alike.
_AIR_DISPLAY = "%d %b %Y"


def _modal_episodes(episodes: list[dict], season: int) -> list[dict]:
    """Simkl's episode list reduced to the modal's rows, for `season` alone.

    THE SEASON FILTER IS THE ONE `_season_air_dates` ALREADY APPLIES one function
    up, for the same measured reason: Simkl maps an anime title to one canonical
    season and omits the field from its episodes, so an episode with no season
    number belongs to whichever season is being asked about.

    NO PER-EPISODE RATING, because Simkl publishes none — measured against the
    live endpoint, an episode carries a title, a description, a date, an `aired`
    flag and an image and nothing else. The key is still present and still None,
    so the one renderer both sources feed sees a field this source did not fill
    in rather than a field that does not exist.

    AN UNDATED EPISODE KEEPS ITS ROW with an empty display string, the same way
    the Trakt package's `_episodes_from` keeps one: an unscheduled episode still
    exists and dropping it would make a half-announced season look complete.
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
        air_date = _episode_date(entry)
        rows.append({
            "number": int(number),
            "title": str(entry.get("title") or f"Episode {number}"),
            # THE DATE AS SIMKL STATES IT, NOT CONVERTED. Simkl expresses a whole
            # file's times in one fixed offset rather than in each title's own
            # zone (see _episode_date), so converting the instant into the
            # viewer's zone would move a late-night episode a day for no reason
            # anybody could act on.
            "air_display": air_date.strftime(_AIR_DISPLAY) if air_date else "",
            "rating": None,
            "overview": str(entry.get("description") or "").strip(),
        })
    rows.sort(key=lambda row: row["number"])
    return rows


def _trailer_url(trailers) -> str:
    """The first trailer Simkl lists, as a watchable URL, or "".

    Simkl gives `[{"name": ..., "youtube": "<id>", "size": ...}]` — a bare
    YouTube id rather than a link — so the URL is built here. Building it in the
    provider rather than in the renderer is what lets the modal's `trailer` key
    mean the same thing whoever filled it in: Trakt sends a finished URL, and a
    renderer that had to know which source it was looking at would be a second
    place the two shapes are reconciled.

    THE FIRST ONE, because there is no basis for choosing another: the list is
    unordered as far as the payload says, and 2316 of 7698 titles on a live
    instance carry one at all.
    """
    for entry in trailers or []:
        if not isinstance(entry, dict):
            continue
        youtube = str(entry.get("youtube") or "").strip()
        if youtube:
            return f"https://www.youtube.com/watch?v={youtube}"
    return ""


async def fetch_details(settings: Settings, media: Media | str, simkl_id,
                        season: int | None, *, cache_only: bool = False) -> dict:
    """One title as the detail modal draws it — app/providers/base.py's DetailPort.

    THE SAME KEYS THE TRAKT PACKAGE'S fetch_details RETURNS, for the reason that
    protocol states: one client-side renderer draws both, so a key present on
    only one source's answer would read as a template bug rather than as
    something this source cannot say.

    `cast` IS ALWAYS EMPTY AND THAT IS DELIBERATE. Simkl publishes no cast on any
    endpoint this app can reach, and the app's TMDB key exists for network logos
    — pulling a third metadata service in to fill one section of one modal is a
    third source's failure modes, rate limit and staleness bought for a strip of
    headshots. The renderer already omits the section on an empty list, which is
    the same thing it does for a Trakt title served from a cold cache.

    TWO CALLS, NOT ONE, and they answer different questions: `titles.fetch_title`
    is the catalogue record (overview, genres, network, rating, trailers) and
    `fetch_episodes` is the season's list. Both are cached per URL, so a modal
    opened twice costs nothing the second time, and the first is the same URL the
    calendar's enrichment drain already warms — a title the drain has reached
    opens with no outbound call at all.
    """
    media = Media(media)
    fields = await titles.fetch_title(settings, simkl_id, media, cache_only=cache_only) or {}
    episodes = (await fetch_episodes(settings, simkl_id, media, cache_only=cache_only)
                if media is not Media.MOVIE else [])
    # WHICH SEASON WAS ANSWERED IS RETURNED, not assumed to be the one asked for.
    # 69 of 690 Simkl-only show entries measured on a live instance carry no
    # season at all — Simkl's calendar files omit it for anime — and a title whose
    # episode list holds exactly one season leaves nothing to choose between, so
    # answering that one is a reading of the data rather than a guess. Anything
    # else keeps the season it was asked about, including None, and the modal
    # draws no episode section for it.
    known = seasons_known(episodes)
    answered = season if season is not None else (known[0] if len(known) == 1 else None)
    runtime = fields.get("runtime")
    return {
        # EMPTY, AND NOT AN OVERSIGHT. What this reads is the enrichment
        # extraction (titles.py's `_extract`), which keeps no title: the calendar
        # already has one from the listing that put the card on the page. Adding
        # a field to that extraction means bumping its version and re-fetching
        # every stored row — 7698 of them on a live instance — to fill in a string
        # the modal is already showing in its own heading, taken from the card the
        # click came from.
        "title": "",
        "year": fields.get("year") or "",
        "overview": str(fields.get("overview") or "").strip(),
        "status": str(fields.get("status") or "").replace("_", " ").title(),
        "network": str(fields.get("network") or ""),
        "runtime": runtime,
        # SLUGS BACK INTO WORDS, exactly as the Trakt package does to its own.
        # `_extract` slugs a genre so a viewer's filter spec matches one spelling
        # across both services; the modal draws chips a person reads, and the two
        # sources' chips have to look alike.
        "genres": [str(g).replace("-", " ").title() for g in (fields.get("genres") or [])],
        "rating": round(float(fields["rating"]), 1) if fields.get("rating") else None,
        "certification": str(fields.get("certification") or "").upper(),
        "trailer": _trailer_url(fields.get("trailers")),
        # Simkl's catalogue record carries no homepage field; the card's own
        # outbound button already offers this title's Simkl page.
        "homepage": "",
        "season": answered,
        "cast": [],
        # NO SEASON MEANS NO EPISODE SECTION, rather than every season run
        # together. A list nobody can label is worse than none: the reader has no
        # way to tell which season's E01 they are looking at.
        "episodes": _modal_episodes(episodes, answered) if answered is not None else [],
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
