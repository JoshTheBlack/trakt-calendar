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

import asyncio
import logging
from datetime import date, datetime

from ...config import Settings
from .. import season as season_rules
from ..base import Media, SeasonsAnswer
from . import _naming, titles, transport

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

    THE SEASON ASKED FOR IS THE TRACKER'S, WHICH IS NOT ALWAYS THIS TITLE'S OWN.
    Simkl files each anime season as a separate title numbering its episodes from
    season 1, so `show:tmdb:1429` season 3 has to be asked of simkl 694485's own
    season 1 — see `_translated_season`, which is where that lookup is decided
    and where the reason it is not paid on every call is written.
    """
    episodes, local = await _episodes_holding(settings, simkl_id, int(season), media)
    if not episodes:
        return season_rules.empty_season(int(season))
    return {
        # THE SEASON THE CALLER ASKED ABOUT, always. `local` is Simkl's spelling
        # of it and belongs to this module; the tracker files its record under
        # the season both services agree names the same thing.
        "season": int(season),
        **season_rules.derive_season(
            _season_air_dates(episodes, local), today or date.today()),
    }


async def _episodes_holding(settings: Settings, simkl_id, season: int,
                            media: Media) -> tuple[list[dict], int]:
    """The episode list that actually holds `season` of the series `simkl_id`
    belongs to, and the number THAT list calls it.

    Simkl files each anime season as its own title numbering its episodes from
    1, so the season a record names and the title a record carries are two
    different things — `_naming.title_for_season` is where that is reconciled,
    and its docstring carries the reasoning.

    PAID ONLY WHERE THE ANSWER WOULD OTHERWISE BE NOTHING. A title already
    holding the season it was asked about needs no lookup and gets none, which
    is every ordinary show and every anime title asked for its own season — so
    the extra per-title GETs land exactly on the case this exists for, and never
    on a roster of rows that were already answerable.

    A LOOKUP THAT CANNOT BE MADE LEAVES THE ANSWER ALONE. `fetch_season_detail`
    has never raised — an unanswerable season reads as an empty one and the next
    load asks again — so a Simkl outage must not start failing a whole roster
    render through a refinement of the answer rather than the answer itself.
    """
    episodes = await fetch_episodes(settings, simkl_id, media)
    if not episodes or season in seasons_known(episodes):
        return episodes, season
    try:
        holder = await _naming.title_for_season(settings, simkl_id, season)
    except transport.SimklError:
        logger.warning("simkl could not be asked which of its titles holds season %s "
                       "of simkl id %s; reading that season as this title's own",
                       season, simkl_id)
        return episodes, season
    if holder is None:
        return episodes, season
    if str(holder) != str(simkl_id):
        episodes = await fetch_episodes(settings, holder, media)
    # The holder IS this season, so whatever single season its own list uses is
    # the one to read. A list spanning several is one that already numbers its
    # seasons the way the show does, and needs no translation.
    known = seasons_known(episodes)
    return episodes, (known[0] if len(known) == 1 else season)


# ---------------------------------------------------------------------------
# The detail modal's field set — what a card opens on when Simkl is the only
# service that listed it.
# ---------------------------------------------------------------------------

# How an episode's air date is written in the modal's list. The same strftime the
# Trakt package uses for the same row, so two cards' episode lists read alike.
_AIR_DISPLAY = "%d %b %Y"


# Deferred for the reason the Trakt package gives for the same import: the
# poster reading belongs beside the feed that parses it.
from . import calendar as simkl_calendar  # noqa: E402

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


async def _filled_from_series(settings: Settings, simkl_id, media: Media,
                              fields: dict) -> dict:
    """`fields` with every empty value taken from this title's series root.

    A BLANK IS THE ONLY THING REPLACED. `year` and `status` are the season's own
    and stay that way where it states them — Frieren's third season is 2027 and
    `tba`, and reading 2023 and `ended` off season 1 would be worse than the gap
    this exists to close.

    Costs nothing for a title that already describes itself (the caller only
    asks when the overview is empty), nothing for a title with no series behind
    it, and one cached record otherwise. A lookup that fails leaves the gaps —
    an under-described modal is what this is improving on, not a state worth
    failing the modal over.
    """
    try:
        naming = await _naming.fetch(settings, simkl_id)
        root = await _naming.series_root(settings, simkl_id, naming)
    except transport.SimklError:
        logger.warning("simkl could not be asked what series simkl id %s belongs to; "
                       "the modal draws what this title alone says", simkl_id)
        return fields
    if not root:
        return fields
    parent = await titles.fetch_title(settings, root, media) or {}
    filled = dict(fields)
    for key, value in parent.items():
        if not filled.get(key):
            filled[key] = value
    return filled


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
    # THE SAME RESOLUTION THE SEASON SUMMARY MAKES, for the same reason: the
    # season a record NAMES and the Simkl title a record CARRIES are two
    # different things, so a modal opened on `show:tmdb:90937` season 3 while
    # the record holds season 1's title would filter its episode list to a
    # season that list does not contain and say there is none. The tile beside
    # it would be showing twelve.
    # SKIPPED ENTIRELY UNDER `cache_only`, which is the public share pages'
    # promise that a stranger's click spends no Simkl budget — the walk makes
    # live calls, and a modal with no episode list is the degrade those pages
    # already accept everywhere else.
    # WHAT THIS TITLE LEFT BLANK, TAKEN FROM ITS SERIES. Simkl writes a
    # description and trailers per season-title, and for the newest entries it
    # has not written them yet — measured, Beastars' 2026 season and Frieren's
    # 2027 one carry a 0-character overview and no trailers while their earlier
    # seasons carry both. That left the modal drawing an episode list and a row
    # of genre chips and nothing else.
    # FILLING FROM THE SERIES MATCHES THE OTHER SOURCE RATHER THAN INVENTING A
    # RULE: the Trakt package reads `shows/{id}` for overview, trailer, genres
    # and rating and lets the season choose only the episode list, so EVERY
    # Trakt modal already shows the series' description. A gap filled this way
    # makes one renderer's two sources agree, which is what DetailPort's
    # contract asks for; a blank does not.
    # ONLY WHAT IS EMPTY IS FILLED, so a season that does describe itself keeps
    # its own words — which is better than the series' and is what Simkl offers
    # that Trakt does not.
    # `fields` EMPTY MEANS SIMKL DOES NOT KNOW THIS TITLE, which is a different
    # thing from a title it knows and has not described — there is no series
    # behind it to ask about, so asking would spend a request to learn nothing.
    # THE OVERVIEW AND THE NETWORK ARE BOTH TRIGGERS, because they go missing
    # for different reasons and either one alone leaves the modal disagreeing
    # with something: an overview Simkl has not written yet empties the card,
    # and a network it only ever states on the series root would leave the modal
    # blank beside a roster row that shows one (`fetch_seasons` fills the same
    # gap for the add).
    if (media is not Media.MOVIE and not cache_only and fields
            and not (str(fields.get("overview") or "").strip()
                     and str(fields.get("network") or "").strip())):
        fields = await _filled_from_series(settings, simkl_id, media, fields)
    local = None if season is None else int(season)
    if (media is not Media.MOVIE and season is not None and not cache_only
            and episodes and int(season) not in seasons_known(episodes)):
        episodes, local = await _episodes_holding(settings, simkl_id, int(season), media)
    # WHICH SEASON WAS ANSWERED IS RETURNED, not assumed to be the one asked for.
    # 69 of 690 Simkl-only show entries measured on a live instance carry no
    # season at all — Simkl's calendar files omit it for anime — and a title whose
    # episode list holds exactly one season leaves nothing to choose between, so
    # answering that one is a reading of the data rather than a guess. Anything
    # else keeps the season it was asked about, including None, and the modal
    # draws no episode section for it.
    known = seasons_known(episodes)
    answered = season if season is not None else (known[0] if len(known) == 1 else None)
    # `answered` IS THE TRACKER'S SEASON AND `local` IS SIMKL'S SPELLING OF IT.
    # They differ only for a season-title, whose episodes are numbered from 1
    # whatever season of the show they are — so the modal must SAY 3 while
    # FILTERING on 1, and conflating the two is what made it say there was no
    # episode list at all.
    if local is None:
        local = answered
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
        # THE COUNTRY, UPPERCASED, "" when the service does not say. `_extract`
        # has always kept it and this projection dropped it, which made the
        # calendar search's country filter a no-op: a record carrying no country
        # cannot be excluded BY country, so a viewer excluding a dozen of them
        # was still offered every one — and the month such a row linked to could
        # never draw the title. Costs nothing: it is already in the row this
        # reads.
        "country": str(fields.get("country") or "").upper(),
        # UPPERCASED to match what the calendar feed stores, for the reason the
        # Trakt package gives for the same field.
        "language": str(fields.get("language") or "").upper(),
        # THE TWO FIELDS READ-TIME FILTERS ACT ON, and the reason they are worth
        # carrying even though their absence is survivable. `anime_type` is what
        # `filter.prune_disguised_films` keys on to keep a film off a series
        # calendar; `release_types_by_country` is what the movie release
        # narrowing judges. A record missing them is SHOWN rather than hidden —
        # the rule that a filter must not hide what it has not learned yet — so
        # nothing breaks without them, and a title the calendar would have
        # narrowed away is instead drawn. `_extract` has held both since it was
        # written; only this projection dropped them.
        "anime_type": str(fields.get("anime_type") or ""),
        "release_types_by_country": dict(fields.get("release_types_by_country") or {}),
        # THE SHARED IDS, for the reason the Trakt package gives for the same
        # field: the calendar search's jump route knows only a source and that
        # source's id, and asking the SERVICE who a title is keeps a hand-made
        # URL from deciding what a shared calendar row is about. `_extract` has
        # always kept these.
        "ids": dict(fields.get("ids") or {}),
        # THE POSTER, AS A FULL URL. `_extract` keeps Simkl's partial path and
        # this projection dropped it, so a card the calendar SEARCH wrote had no
        # picture. Built by this package's own one reading of that path -- the
        # same one the calendar feed uses -- because Simkl states a poster as a
        # fragment and two places turning it into a URL would be two chances to
        # get the size suffix wrong.
        "poster": simkl_calendar.poster_url(fields.get("poster")) or "",
        "runtime": runtime,
        # SLUGS BACK INTO WORDS, exactly as the Trakt package does to its own.
        # `_extract` slugs a genre so a viewer's filter spec matches one spelling
        # across both services; the modal draws chips a person reads, and the two
        # sources' chips have to look alike.
        "genres": [str(g).replace("-", " ").title() for g in (fields.get("genres") or [])],
        # THE GENRES AS THE SOURCE SPELLS THEM, beside the display form above.
        # TWO CONSUMERS WANT TWO DIFFERENT THINGS and only one of them was being
        # served: the modal draws chips a person reads ("Science Fiction"), and a
        # RECORD stores slugs, because `render` derives the display form from
        # them and every genre FILTER matches against them. `keep_values`
        # lowercases but does not slugify, so a record holding "Science Fiction"
        # is never matched by a `science-fiction` spec -- a filter that silently
        # stops acting, which is the same defect the country field had.
        "genre_slugs": [str(g) for g in (fields.get("genres") or [])],
        "rating": round(float(fields["rating"]), 1) if fields.get("rating") else None,
        # A THIRD PARTY'S SCORE, UNDER ITS OWN KEY. It reaches the modal beside
        # Simkl's rather than instead of it — see titles._imdb_rating for why the
        # two are separate facts and not two spellings of one.
        "imdb_rating": (round(float(fields["imdb_rating"]), 1)
                        if fields.get("imdb_rating") else None),
        "certification": str(fields.get("certification") or "").upper(),
        "trailer": _trailer_url(fields.get("trailers")),
        # Kept by `_extract` already; passed through for the calendar search,
        # which needs a month to point a catalogue hit at — see the same key on
        # the Trakt package's own payload.
        "first_aired": str(fields.get("first_aired") or ""),
        # Simkl's catalogue record carries no homepage field; the card's own
        # outbound button already offers this title's Simkl page.
        "homepage": "",
        "season": answered,
        "cast": [],
        # NO SEASON MEANS NO EPISODE SECTION, rather than every season run
        # together. A list nobody can label is worse than none: the reader has no
        # way to tell which season's E01 they are looking at.
        "episodes": _modal_episodes(episodes, local) if local is not None else [],
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


# ---------------------------------------------------------------------------
# The catalogue search's season picker (app/providers/base.py's
# DetailPort.fetch_seasons) — a different consumer from everything above, and a
# different per-title lookup from titles.py's `fetch_title`.
# ---------------------------------------------------------------------------


def _season_counts(episodes: list[dict]) -> list[dict]:
    """[{season, episode_count, first_aired}] over `episodes`, one entry per
    season the episode list actually contains, in order — the picker's
    candidate list.

    GROUPED THE SAME WAY `seasons_known` GROUPS SEASON NUMBERS: an episode with
    no season number counts as season 1, which is how anime arrives (Simkl
    omits the field for a title it maps to one canonical season).

    `first_aired` IS THE EARLIEST DATED EPISODE IN THE SEASON, "" when none of
    them carries a date, and it is the same field Trakt's own season list
    returns — the calendar search reads one shape whichever service answered.
    Derived rather than fetched: the episode list being grouped here already
    carries every date, so a season's premiere costs no extra call.
    """
    counts: dict[int, int] = {}
    earliest: dict[int, date] = {}
    for entry in episodes or []:
        if str(entry.get("type") or _REGULAR_EPISODE) != _REGULAR_EPISODE:
            continue
        if entry.get("episode") is None:
            continue
        season = int(entry.get("season") if entry.get("season") is not None else 1)
        counts[season] = counts.get(season, 0) + 1
        when = _episode_date(entry)
        if when is not None and (season not in earliest or when < earliest[season]):
            earliest[season] = when
    return [{"season": season, "episode_count": count,
             "first_aired": earliest[season].isoformat() if season in earliest else ""}
            for season, count in sorted(counts.items())]


async def fetch_seasons(settings: Settings, simkl_id, media: Media | str = Media.SHOW) -> SeasonsAnswer:
    """app/providers/base.py's DetailPort.fetch_seasons.

    TWO CALLS, BOTH PAID BY ONE CLICK. `fetch_episodes` answers the picker's
    candidate list — the episode list already answers it, so no new endpoint is
    needed for it — and `_naming.fetch` answers the three things only the
    per-title record carries: this hit's own season, every shared id Simkl knows
    it by, and the network. Both run together because neither depends on the
    other's answer.

    THE NETWORK IS HERE BECAUSE THE ADD FLOW HAS NOWHERE ELSE TO GET IT. A Simkl
    SEARCH hit carries none, so on an instance with no second catalogue to fill
    the gap from, a show added by hand reached the roster with an empty network
    and drew no emoji. This record carries it and the click already pays for the
    record — and where a season-title's own record leaves it null, which is
    every season but the first, it is read off the series (see
    `_naming.network_of_series`).
    """
    media = Media(media)
    if media is not Media.SHOW or not simkl_id:
        return SeasonsAnswer(seasons=[], named_season=None, ids={}, network="")
    episodes, naming = await asyncio.gather(
        fetch_episodes(settings, simkl_id, media),
        _naming.fetch(settings, simkl_id),
    )
    return SeasonsAnswer(
        seasons=_season_counts(episodes),
        named_season=naming.season,
        ids=naming.ids,
        network=await _naming.network_of_series(settings, simkl_id, naming),
    )
