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
import re
from datetime import date, datetime
from zoneinfo import ZoneInfo

import httpx

from ...config import Settings
from .. import season as season_rules
from ..base import MATCH_SOURCES, Media, TitleFacts, proxied_image
from . import _ids, transport
from .transport import TraktError

logger = logging.getLogger(__name__)


def _headshot(person: dict) -> str | None:
    """A cast member's headshot, THROUGH THE SHARED IMAGE PROXY -- the same one
    every poster this app draws addresses (see app/providers/base.py's
    proxied_image). Trakt's own address was reaching the browser directly here,
    which both hotlinks their CDN (against the terms proxied_image's docstring
    quotes) and is unproxied by construction: a browser CSP that admits only the
    proxy host, as this app's does, blocked every headshot until this wrapped it."""
    imgs = (person.get("images") or {}).get("headshot") or []
    if imgs:
        url = imgs[0]
        return proxied_image(url if url.startswith("http") else "https://" + url)
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


# TWENTY, BECAUSE THE GRID IS FIVE WIDE. The cap is this app's own — a season's
# cast runs to 118 on some titles and the panel is not a filmography — and it was
# 16, which left the last row three short of full at the default width. Four rows
# of five fills the grid; anything under a full row reads as the list having been
# cut off mid-thought rather than deliberately bounded.
_CAST_SHOWN = 20


def _cast_from(people: dict) -> list[dict]:
    """The modal's cast list from a /people response: the top-billed
    `_CAST_SHOWN`, name + character + headshot. `characters` (a list) is Trakt's
    newer shape and `character` (a string) the older one; both still arrive."""
    cast = []
    for member in (people.get("cast") or [])[:_CAST_SHOWN]:
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


async def fetch_title_payload(settings: Settings, media: str, trakt_id: str,
                              *, cache_only: bool = False) -> dict | None:
    """One title's raw `extended=full` object, exactly as Trakt returns it.

    RAW, AND THAT IS THE POINT. `fetch_details` beside this projects the same
    payload down to the fields a detail MODAL draws; a caller building a calendar
    RECORD needs the object itself, because this package's own `calendar.to_record`
    already knows how to read it — a Trakt calendar entry's `show` is this very
    shape. Handing the projection to a record builder instead is what produced a
    second builder, and then a slow drip of fields the projection had quietly
    dropped: first_aired, country, poster, ids, language, and the genre slugs a
    filter matches on.
    """
    path = f"{'movies' if media == 'movie' else 'shows'}/{trakt_id}"
    payload = await transport.cached_get(
        transport.shared_client(), settings, path, {"extended": "full"},
        cache_only=cache_only)
    return payload if isinstance(payload, dict) else None


# "Season 3" and its translations are the NUMBER restated, not a name. Only the
# English form is dropped here because that is the form this app asks for; a
# localized catalogue would need its own reading, and rendering a real name that
# happened to look like this one is the harmless direction of the error.
_NUMBERED_SEASON = re.compile(r"(?i)season\s+\d+")


async def fetch_details(settings: Settings, media: str, trakt_id: str, season: int | None,
                        cache_only: bool = False) -> dict:
    """Full detail payload for the modal: overview, cast, episode list.

    ASKED ABOUT A SEASON, IT ANSWERS ABOUT THAT SEASON TOO. Where a season is
    named, the CAST is that season's rather than the title's, and the season's own
    name and its own description travel BESIDE the title's rather than replacing
    them — `overview` stays the show's and `season_overview` is the season's, so a
    panel can show the premise and then what this particular run of it is. That is a correction rather than an
    embellishment, and it was measured before it was built: the panel used to
    draw the show's cast whatever season it was open on, and across 14 seasons of
    six titles only 85 of the 224 people it listed were actually in the season
    being viewed — 37%. On an anthology it is worse and it is visible: the
    `true-detective` season 1 panel listed Jodie Foster and Kali Reis, who are in
    Night Country, four seasons later. Trakt states all of it per season, and
    every one of the 40 seasons probed had its own overview differing from the
    show's.

    WHAT STAYS SHOW-LEVEL, and the split is deliberate: genres, network,
    certification, status, trailer, country, language and the rating are facts
    about the TITLE, and Trakt has no per-season answer for most of them. So the
    chip row describes the show and the season pane describes the season.

    `cache_only=True` serves purely from cache and never calls Trakt — the mode a
    public share page uses so a visitor's click reuses what the owner's own views
    already cached rather than spending the owner's rate limit. Fields with no
    cached source come back empty, and the modal renders around them."""
    tz = ZoneInfo(settings.timezone)
    base = "movies" if media == "movie" else "shows"
    client = transport.shared_client()
    per_season = media != "movie" and season is not None
    tasks = {
        "info": transport.cached_get(
            client, settings, f"{base}/{trakt_id}", {"extended": "full"}, cache_only=cache_only),
        # THE CAST OF THE SEASON WHERE ONE WAS NAMED, and of the title otherwise.
        # Both answer the same shape, so only the path differs; a season that
        # answers nothing falls back to the show's below rather than drawing an
        # empty grid.
        "people": transport.cached_get(
            client, settings,
            f"shows/{trakt_id}/seasons/{season}/people" if per_season
            else f"{base}/{trakt_id}/people",
            {"extended": "full"}, cache_only=cache_only),
    }
    if per_season:
        tasks["episodes"] = transport.cached_get(
            client, settings, f"shows/{trakt_id}/seasons/{season}", {"extended": "full"},
            cache_only=cache_only)
        # EVERY SEASON IN ONE CALL, which is where the season's own name and
        # overview come from and how many seasons the picker offers.
        #
        # THE SAME URL `fetch_show_seasons` ALREADY ASKS, deliberately: same path,
        # same params, so the same cache key, so a title the tracker's add flow or
        # the calendar search has been through answers this for free. It is also
        # small — 3-13 KB across a 4-season and a 14-season show — where
        # `extended=full,episodes` would carry every episode of every season
        # (348 KB for one long-running title) and defeat the point of loading a
        # season's rows only when somebody opens that season.
        tasks["seasons"] = transport.cached_get(
            client, settings, f"shows/{trakt_id}/seasons", {"extended": "full"},
            cache_only=cache_only)
    results = dict(zip(tasks.keys(), await asyncio.gather(*tasks.values())))

    info = results.get("info") or {}
    seasons = results.get("seasons")
    this_season = _season_object(seasons, season)
    cast = _cast_from(results.get("people") or {})
    if per_season and not cast:
        cast = _cast_from(await transport.cached_get(
            client, settings, f"{base}/{trakt_id}/people", {"extended": "full"},
            cache_only=cache_only) or {})

    # Deferred so this module stays importable from calendar.py's side of the
    # package: the poster reading lives there because that is where the feed's
    # own images are parsed, and a module-level import would close a cycle.
    # `_ids` needs no such deferral — it reads only the shared value types — so
    # it is imported at the top like `transport`.
    from . import calendar as trakt_calendar

    return {
        "title": info.get("title") or "",
        "year": info.get("year") or "",
        "overview": (info.get("overview") or "").strip(),
        # THE SEASON'S OWN WORDS, BESIDE THE TITLE'S RATHER THAN INSTEAD OF THEM.
        # They answer different questions — "what is this show" and "what is this
        # season about" — and an anthology needs both on screen at once: the
        # premise, and then what THIS run of it is. Empty where the season has no
        # separate blurb, which is the panel's signal to draw nothing rather than
        # to repeat the paragraph above it.
        "season_overview": (this_season.get("overview") or "").strip(),
        "status": (info.get("status") or "").replace("_", " ").title(),
        # THE COUNTRY, UPPERCASED, "" when the service does not say. It has
        # always been in this response and this projection dropped it, which
        # made the calendar search's country filter a no-op: a record with no
        # country cannot be excluded BY country, so every viewer's exclusions
        # passed everything through. The calendar's own path reads the same
        # field from the same payload — see this package's calendar.py — so the
        # two now agree about what a title's country is.
        "country": (info.get("country") or "").upper(),
        # THE LANGUAGE, which this package's own calendar.py has always set on a
        # Record and this projection dropped. A record built from a per-title
        # lookup should be able to say everything a record built from the feed
        # says about the same title, or the two disagree about what is known
        # depending only on which path produced the row.
        # UPPERCASED, because that is what this package's calendar.py stores and
        # a record must not depend on which path produced it.
        "language": (info.get("language") or "").upper(),
        # THE SHARED IDS, SO A CALLER NEED NOT BE TOLD THEM. Everything that
        # reached this function used to arrive holding a search hit that already
        # carried them, so the projection dropped them; the calendar search's
        # jump route has only a source and that source's own id, and the ids are
        # what decide a title's cross-source identity. Taking them from the
        # SERVICE rather than from the request is also what stops a hand-made
        # URL deciding what a shared calendar row is about.
        "ids": _ids.normalize(info.get("ids") or {}),
        # THE POSTER, THROUGH THIS PACKAGE'S OWN ONE READING OF IT, THEN THROUGH
        # THE SHARED PROXY. A per-title lookup returns the same images the
        # calendar feed does and this projection dropped them, so a card the
        # calendar SEARCH wrote had no picture -- reported as "Half Man is
        # missing the poster". Reusing the calendar module's reading rather than
        # re-reading the field is what keeps its documented rule about these
        # addresses in one place; `trakt_calendar.poster()` deliberately returns
        # Trakt's own address unproxied (see its docstring) because most of its
        # callers are server-side downloads, not a browser -- this caller IS a
        # browser (the details panel), so it proxies here rather than asking
        # every such caller to remember to.
        "poster": proxied_image(trakt_calendar.poster(info)) or "",
        "network": info.get("network") or "",
        "runtime": info.get("runtime"),
        "genres": [g.replace("-", " ").title() for g in (info.get("genres") or [])],
        # THE GENRES AS THE SOURCE SPELLS THEM, beside the display form above.
        # TWO CONSUMERS WANT TWO DIFFERENT THINGS and only one of them was being
        # served: the modal draws chips a person reads ("Science Fiction"), and a
        # RECORD stores slugs, because `render` derives the display form from
        # them and every genre FILTER matches against them. `keep_values`
        # lowercases but does not slugify, so a record holding "Science Fiction"
        # is never matched by a `science-fiction` spec -- a filter that silently
        # stops acting, which is the same defect the country field had.
        "genre_slugs": [str(g) for g in (info.get("genres") or [])],
        "rating": round(float(info["rating"]), 1) if info.get("rating") else None,
        # THREE KEYS TRAKT HAS NOTHING TO SAY FOR, PRESENT AND EMPTY. A detail
        # answer's key set is the same whoever gives it — see DetailPort in
        # app/providers/base.py — because ONE renderer draws the modal, so a key
        # only one source answers reads as a template bug rather than as a lookup
        # that source cannot make. All three are on the Simkl package's answer
        # and were simply absent from this one, which is the whole explanation
        # for a chip that could never light on a Trakt-answered title.
        #
        # EMPTY IS THE HONEST ANSWER HERE, NOT A SHORTCUT, and it is a measured
        # one rather than an assumption. TRAKT PUBLISHES NO EXTERNAL RATING BY
        # ANY ROUTE: probed across 18 titles at show, film, season and episode
        # level under extended=full, full,metadata, metadata and full,images plus
        # eight speculative extended values; `rating` and `votes` are Trakt's own
        # throughout, /shows/{id}/ratings answers Trakt's own 1-10 distribution
        # and /shows/{id}/stats answers play counts. `anime_type` and
        # `release_types_by_country` are likewise facts Trakt's catalogue does
        # not state at all — the other service exposes both structurally, which
        # is where their value comes from. A source that does state them fills
        # these in; nothing reachable in Trakt's API can, so do not go looking
        # for a parameter that would.
        "anime_type": "",
        "imdb_rating": None,
        "release_types_by_country": {},
        "certification": (info.get("certification") or "").upper(),
        "trailer": info.get("trailer") or "",
        # WHEN THE TITLE ITSELF FIRST AIRED, which `extended=full` already
        # returns and which nothing used to keep. Its reader is the calendar
        # search: a catalogue hit carries no date on either source, so the month
        # a title should be looked for in has to come from somewhere, and this
        # is the lookup that already had it.
        "first_aired": info.get("first_aired") or "",
        "homepage": info.get("homepage") or "",
        "season": season,
        # WHAT THIS SEASON IS CALLED, where that is a name rather than a number.
        # Trakt titles an anthology's seasons properly — Murder House, Asylum,
        # Coven, Night Country, Candle Cove — and calls the rest "Season 3",
        # which says nothing the number does not. `_season_name` drops the latter,
        # so a caller can render this whenever it is non-empty without having to
        # know the difference.
        "season_title": _season_name(this_season),
        # HOW MANY SEASONS THIS TITLE HAS, for the panel's season picker. Counted
        # from the same one call the season's own words come from. 0 where nothing
        # was asked about a season, which is the film case and the case of a show
        # nobody named a season for.
        "season_count": _usable_season_count(seasons),
        "cast": cast,
        "episodes": _episodes_from(results.get("episodes") or [], tz),
    }


def _season_object(seasons, number: int | None) -> dict:
    """The one season `number` names, out of `shows/{id}/seasons`, or {}."""
    if number is None or not isinstance(seasons, list):
        return {}
    for entry in seasons:
        if isinstance(entry, dict) and entry.get("number") == number:
            return entry
    return {}


def _season_name(season: dict) -> str:
    """A season's own name, or "" where Trakt only restates the number.

    "Season 3" is not a name — it is the number spelled out, and rendering it
    beside the number gives a reader nothing. Anthologies are where this field
    earns its place, and there it holds the real thing.
    """
    title = " ".join(str(season.get("title") or "").split())
    if not title or _NUMBERED_SEASON.fullmatch(title):
        return ""
    return title


def _usable_season_count(seasons) -> int:
    """How many seasons a picker should offer.

    SPECIALS ARE NOT A SEASON somebody pages back through, and a season with no
    episodes is one Trakt is holding a place for — both are excluded, which is
    the same filter `fetch_show_seasons` applies for the same reason.
    """
    if not isinstance(seasons, list):
        return 0
    return sum(1 for entry in seasons
               if isinstance(entry, dict) and (entry.get("number") or 0) > 0
               and (entry.get("episode_count") or 0) > 0)


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
    shared `client` when batching (else a throwaway one is created).

    "TRAKT SAYS THIS SEASON HAS NO EPISODES" AND "TRAKT DID NOT ANSWER" ARE NOT
    THE SAME ANSWER, and this used to give both of them as a season of zero
    episodes with no dates. That is a FABRICATED NUMBER: the caller cannot tell it
    from a real one, so it renders as fact, moves the row into a different bucket
    (a total of 0 is not a season anybody is keeping up with) and can be written
    down by the transitions that run afterwards. Measured with a corrupted client
    id, which is the ordinary way to reach it — Trakt answers 401, `cached_get`
    without `raise_errors` reports that as None, and a roster lost most of its
    rows to seasons it had been counting correctly a minute earlier.
    SO A FAILURE RAISES and the caller degrades the row to its last-known counts
    (app/distrakt/live.py), while the one status that genuinely MEANS "there is
    nothing here" — 404, no such show or season — keeps answering an empty
    season, which is what it has always meant.
    """
    tz = ZoneInfo(settings.timezone)
    c = client or transport.shared_client()
    try:
        episodes = await transport.cached_get(
            c, settings, f"shows/{trakt_id}/seasons/{season}", {"extended": "full"},
            ttl_seconds=SEASON_CACHE_TTL_SECONDS, fresh=fresh, raise_errors=True,
        )
    except TraktError as exc:
        if getattr(exc, "status", None) == 404:
            return _empty_season(season)
        raise
    if not isinstance(episodes, list):
        return _empty_season(season)
    return {"season": season, **_derive_season(episodes, tz)}


async def fetch_season_episodes(settings: Settings, trakt_id, season: int,
                                client: httpx.AsyncClient | None = None,
                                *, only_if_cached: bool = False) -> list[dict] | None:
    """Every episode of one season, with the per-episode facts a card and a modal
    can draw: title, overview, runtime, rating, votes, first_aired, type.

    `extended=full` ON THE SEASON'S EPISODE LIST, and the parameter matters more
    than it looks. Measured against the live API: `extended=episodes` alone
    returns the episode objects WITHOUT `first_aired`, so a caller asking for the
    cheap form gets a list that looks complete and silently cannot say when
    anything aired. `full` is what carries the dates, the runtimes and the
    ratings together.

    ONE CALL PER SEASON, WHICH IS WHAT MAKES THIS AFFORDABLE. The alternative —
    /shows/{id}/seasons/{season}/episodes/{n} per episode — is one request per
    episode of every season on the calendar, and the drain that feeds this is
    bounded by seconds per pass rather than by a daily budget.

    RETURNS AN EMPTY LIST FOR A SEASON TRAKT HAS NOTHING FOR (404), and RAISES
    for anything else, on exactly the reasoning `fetch_season_detail` above
    spells out: "no episodes" and "could not ask" are different answers, and
    handing back the first for the second writes a fabricated blank over facts
    this app already had.

    AND None FOR A THIRD ANSWER, reachable only under `only_if_cached`: "not
    without a request". A caller pacing itself against Trakt's limits asks that
    first, so the seasons already in the response cache cost it nothing and its
    budget is spent on the ones that genuinely need the network. It is a
    separate value from `[]` for the same reason `[]` is separate from a raise —
    three different facts, and collapsing any two of them writes one of the
    others down as something it is not.
    """
    c = client or transport.shared_client()
    try:
        episodes = await transport.cached_get(
            c, settings, f"shows/{trakt_id}/seasons/{season}", {"extended": "full"},
            ttl_seconds=SEASON_CACHE_TTL_SECONDS, raise_errors=True,
            only_if_cached=only_if_cached,
        )
        if only_if_cached and episodes is None:
            return None
    except TraktError as exc:
        if getattr(exc, "status", None) == 404:
            return []
        raise
    if not isinstance(episodes, list):
        return []
    out: list[dict] = []
    for episode in episodes:
        if not isinstance(episode, dict):
            continue
        number = episode.get("number")
        if number is None:
            continue
        out.append({
            "number": int(number),
            "title": str(episode.get("title") or ""),
            "overview": str(episode.get("overview") or ""),
            "first_aired": str(episode.get("first_aired") or ""),
            "episode_type": str(episode.get("episode_type") or ""),
            "runtime": episode.get("runtime"),
            "rating": episode.get("rating"),
            "votes": episode.get("votes"),
        })
    return out


async def fetch_show_seasons(settings: Settings, trakt_id, *,
                             cached_only: bool = False) -> list[dict]:
    """/shows/{id}/seasons?extended=full -> [{season, episode_count, first_aired}] for
    seasons Trakt has actually populated with episodes (skips season 0/
    specials and any season with zero KNOWN episodes at all). Powers the
    add-show flow's season picker.

    Filters on `episode_count` (Trakt's total planned/known episode count for
    the season), NOT `aired_episodes`. A season that hasn't premiered yet has
    aired_episodes=0 but a real episode_count once Trakt has announced it —
    filtering on aired_episodes wrongly hid every not-yet-aired season from
    the picker, which is exactly a season 1 that has not started airing yet.
    Fixed once manual add-show on an unaired season turned out to be broken.

    `first_aired` IS THE SEASON'S PREMIERE, AS A FULL INSTANT, "" when Trakt has
    not dated the season. `extended=full` has always returned it and this
    projection used to drop it, which cost the calendar search the only thing it
    needed to offer a SEASON rather than a show: without a per-season date every
    season of a long-running title resolves to the month the show first aired,
    years before the season somebody was looking for. It arrives in the same
    response as the episode counts, so carrying it costs nothing.

    THE TIME IS KEPT AND THAT IS NOT A DETAIL. Trakt dates a premiere to the
    moment it airs — `2026-04-28T20:00:00.000Z` for a British show — and
    truncating that to a bare day and re-reading it as UTC midnight moves it
    BACKWARDS for every viewer west of Greenwich. Measured: a search offered
    27 April for a season the calendar draws on the 28th, so the link landed a
    day early on a day that had nothing on it.
    """
    results = await transport.cached_get(
        transport.shared_client(), settings, f"shows/{trakt_id}/seasons", {"extended": "full"},
        raise_errors=True,
        # `cache_only` is the one both transports spell the same way and
        # document the same way — never call, answer what is stored. The add
        # flow's refusal therefore means "nothing was ever stored for this",
        # which is the honest thing to say to somebody naming a result from a
        # search the instance no longer holds.
        cache_only=cached_only,
    )
    out = []
    for entry in results if isinstance(results, list) else []:
        num = entry.get("number")
        episode_count = entry.get("episode_count") or 0
        if num is None or num == 0 or episode_count <= 0:
            continue
        out.append({"season": int(num), "episode_count": int(episode_count),
                    "first_aired": str(entry.get("first_aired") or "")})
    out.sort(key=lambda s: s["season"])
    logger.info("fetch_show_seasons(%s) -> %d usable season(s)", trakt_id, len(out))
    return out


SEARCH_MEDIA = tuple(Media)


def ids_map(media: dict) -> dict:
    """Every id Trakt knows for a title, in this app's own id vocabulary.

    The whole map travels rather than the one id a given caller happens to want:
    an id we discard here is one a future match against another service cannot
    use, and re-fetching it costs a call we have already paid for.

    IT READS THE IDS THROUGH `_ids.normalize` LIKE EVERY OTHER PATH IN THIS
    PACKAGE, and doing that here rather than dropping the raw block on callers is
    the whole point of the function. `normalize` is what adds `trakt_slug`
    alongside `slug`, because both services call a title's readable name `slug`
    and disagree about it — so a map carrying only the bare one is a name nothing
    downstream can attribute to a service.

    THIS FUNCTION USED TO RETURN TRAKT'S RAW BLOCK, and it was the only reading
    of a source's ids in either provider package that did. The consequence was
    not theoretical: a title added to the tracker by hand stored the shared
    `slug` and no `trakt_slug`, so its episode links fell back to the numeric id
    for ever, while the same title arriving from a calendar window — which does
    normalize — carried both. Two readings of one service's ids is one reading
    too many, and the one that skipped the correction was invisible precisely
    because the other three did it properly.
    """
    return _ids.normalize(media.get("ids") or {})


async def search_titles(settings: Settings, media: str, query: str, *,
                        cache_only: bool = False) -> list[dict]:
    """/search/{show|movie}?query=... -> [{media, ids, title, year, network,
    runtime, overview, poster}], newest-match-first as Trakt orders it.

    `images` IS ASKED FOR, so a result row can show the title's poster from
    this one answer rather than a lookup per row — measured live 2026-09-27,
    `extended=full,images` on a search carries the same `images` block the
    calendar's does. `poster` is Trakt's own address (see
    calendar.poster), "" when there is none.

    ONE implementation for both media types. The two searches differ only in the
    path segment and in which key the result object hangs under, so a second
    copy shaped for movies would drift from this one the first time either is
    touched. Empty query returns [] without a call.

    `cache_only=True` reads back the answer this exact search already stored and
    never asks Trakt. It is how a caller that searched a moment ago names one of
    the results — by the query and the result — and gets the service's own record
    of it again without a second request.
    """
    if media not in SEARCH_MEDIA:
        raise ValueError(f"Unknown media type {media!r}.")
    q = (query or "").strip()
    if not q:
        return []
    # Deferred for the reason `fetch_details`'s own import of it gives.
    from . import calendar as trakt_calendar

    results = await transport.cached_get(
        transport.shared_client(), settings, f"search/{media}",
        {"query": q, "extended": "full,images"},
        raise_errors=True, cache_only=cache_only,
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
            "poster": trakt_calendar.poster(item) or "",
        })
    logger.info("search_titles(%s, %r) -> %d raw / %d usable result(s)", media, q,
                len(results) if isinstance(results, list) else 0, len(out))
    return out


# The shared ids Trakt's id lookup accepts, in MATCH_SOURCES order. Trakt has no
# `mal` lookup, so a title known only by that cannot be found here.
SHARED_ID_LOOKUPS = tuple(source for source in MATCH_SOURCES if source != "mal")

# How long an answer — "not listed" included — stands before Trakt is asked
# again: a catalogue that has not listed a title today will not have listed it
# within the hour, and a day is short enough that a title Trakt adds tomorrow is
# linked tomorrow. The response cache's own age IS the re-ask clock.
REASK_SECONDS = 24 * 60 * 60


async def find_by_shared_id(settings: Settings, media: str, ids: dict) -> dict:
    """Trakt's own ids for the title `ids` names, by the first shared id Trakt
    recognises — {} when none of them is listed. /search/{tmdb|tvdb|imdb}/{id}.

    AN EXACT MATCH OR NOTHING. Trakt's id search answers a list, and a hit is
    taken only when its type is the media asked for AND its own id in that space
    equals the one asked about. A title search would be a guess, and a wrong
    Trakt id shadows a correct Simkl one wherever the details layer prefers
    Trakt — for good, because nothing overwrites an id once stored.

    EVERY SHARED ID IS TRIED, because Trakt can map a title by one space and not
    another. Raises TraktError when Trakt could not be asked, which the caller
    must not remember as "not listed".
    """
    if media not in SEARCH_MEDIA:
        raise ValueError(f"Unknown media type {media!r}.")
    for id_type in SHARED_ID_LOOKUPS:
        value = ids.get(id_type)
        if value in (None, "", 0):
            continue
        results = await transport.cached_get(
            transport.shared_client(), settings, f"search/{id_type}/{value}",
            {"type": media}, ttl_seconds=REASK_SECONDS, raise_errors=True)
        for entry in results if isinstance(results, list) else []:
            if not isinstance(entry, dict) or entry.get("type") != media:
                continue
            found = ids_map(entry.get(media) or {})
            if str(found.get(id_type) or "") == str(value) and found.get("trakt"):
                return found
    return {}


async def fetch_show_summary(settings: Settings, trakt_id) -> dict | None:
    """/shows/{id}?extended=full -> the raw show object, or None.

    THE SHOW-LEVEL FACTS A SEASON LOOKUP DOES NOT CARRY, and the network is the
    one with a caller: `fetch_season_detail` answers how long a season is and
    when it aired, which is a question about the SEASON, so nothing in its reply
    says who broadcast the thing. A row added from a history prompt has no search
    hit behind it to have brought one, and so reached the roster with no network
    and drew no emoji.

    `extended=full` RATHER THAN `full,images`, unlike the movie helper beside
    this: the caller wants one string, the poster is answered elsewhere for a
    show, and the image block is the larger half of the response.

    Caches like every other public per-title lookup — a show's network is the
    same for everybody and changes about never.
    """
    data = await transport.cached_get(
        transport.shared_client(), settings, f"shows/{trakt_id}", {"extended": "full"},
    )
    return data if isinstance(data, dict) else None


async def fetch_title_facts(settings: Settings, trakt_id, media: str) -> TitleFacts:
    """DetailPort.fetch_title_facts: Trakt's own title and network for a show or
    a film (a film has no network), from the same cached summary lookups the
    rest of this module uses. Empty when Trakt answers nothing for the id."""
    info = (await fetch_movie_summary(settings, trakt_id) if media == Media.MOVIE
            else await fetch_show_summary(settings, trakt_id)) or {}
    return TitleFacts(str(info.get("title") or "").strip(),
                      str(info.get("network") or "").strip())


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
