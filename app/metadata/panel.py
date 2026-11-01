"""What a details panel says about a title: the describing service's answer, read
from the title store and asked of the service only when the store has none.

WHO DESCRIBES A TITLE IS app/providers/detail_source.py's question (`choose`, and
`for_season` for a season the first service lacks) — a reading of the registry,
the same on every surface. WHAT THEY SAID is this module's: the answer is kept in
the store (app/metadata/descriptions.py), so a panel opened twice, or opened on
a title a tracker row or a board row has already described, costs no request,
and every surface draws the same stored words.

A STALE ANSWER IS STILL AN ANSWER. A row past its clock is drawn as it is, and
the surface that drew it queues it for a refresh (app/metadata/refresh.py) —
which a public share page never does. Only a title the store has NO description
of from the describing service is asked for while the reader waits, and that
answer is kept.

THE ANSWER IS PRESENTED ONCE, HERE (`presented`): a service states origin
addresses and its own dates, and a page draws pictures through the image proxy
and days in the instance's zone. One step for every service, so no service's
package decides how a page draws.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from .. import clock, db, providers
from ..providers import detail_source, videos
from ..providers.base import AIR_DISPLAY, Media, Source, SourceUnavailable, Video, proxied_image
from . import descriptions

logger = logging.getLogger(__name__)


def _air_display(first_aired, tz: ZoneInfo) -> str:
    """An episode's day as a reader sees it: a bare day as itself, an instant in
    the instance's zone. "" for an episode nobody has dated."""
    text = str(first_aired or "").strip()
    if not text:
        return ""
    try:
        if "T" not in text:
            return datetime.fromisoformat(text[:10]).strftime(AIR_DISPLAY)
        moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return ""
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(tz).strftime(AIR_DISPLAY)


def presented(answer: dict | None, settings) -> dict | None:
    """A service's answer as a page draws it: its poster and every headshot
    through the image proxy (the only picture host the Content-Security-Policy
    admits, and the services' own terms ask that their CDNs not be hotlinked),
    and each episode's day written for a reader (`air_display`)."""
    if answer is None:
        return None
    tz = ZoneInfo(settings.timezone)
    return {
        **answer,
        "poster": proxied_image(answer.get("poster")) or "",
        "cast": [{**member, "headshot": proxied_image(member.get("headshot")) or None}
                 for member in answer.get("cast") or []],
        "episodes": [{**episode, "air_display": _air_display(episode.get("first_aired"), tz)}
                     for episode in answer.get("episodes") or []],
    }


def says_anything(details: dict | None) -> bool:
    """Whether an answer has enough in it to be worth drawing.

    A CACHE-ONLY READ THAT MISSED STILL ANSWERS THE FULL KEY SET, empty — that is
    DetailPort's own contract, so the renderer never sees a missing key — which
    means "nothing was stored" and "this title has nothing to say" arrive
    identically and the only way to tell is to look at the values. These three
    are what a panel is FOR: what it is called, what it is about, and what is in
    it. A panel with none of them is a blank the reader cannot act on.
    """
    return bool(details and (details.get("title") or details.get("overview")
                             or details.get("episodes")))


async def describe(settings, chosen: detail_source.Answerer, media, season: int | None, *,
                   ids=None, cache_only: bool = False) -> dict | None:
    """The chosen service's answer about a title (and a season of it), in its
    own terms — from the store, or asked and then kept. None when there is
    genuinely nothing to show.

    `cache_only` is the CALLER's restriction and `chosen.stored_only` a fact about
    the instance (nobody here can reach that service); either means no request,
    and then the answer is whatever the store or the service's own response
    cache holds — which is not kept, since a partial cached read written as a
    description would mark a title described that is not.
    """
    source = Source(chosen.source)
    stored = await descriptions.stored(source, media, chosen.source_id, season, ids=ids)
    if stored is not None:
        return stored
    port = providers.get(source).detail_port
    if cache_only or chosen.stored_only:
        answer = await port.fetch_details(settings, media, chosen.source_id, season,
                                          cache_only=True)
        if chosen.stored_only and not says_anything(answer):
            return None
        return answer
    answer = await port.fetch_details(settings, media, chosen.source_id, season)
    try:
        written = await descriptions.store(source, media, chosen.source_id, answer,
                                           asked_season=season, tz=ZoneInfo(settings.timezone))
    except db.DatabaseError:
        # The reader still gets the answer they waited for; the next view asks
        # again, which is all a failed write costs.
        logger.warning("title store: %s's answer about %s could not be kept",
                       source, chosen.source_id, exc_info=True)
        return answer
    if written is None:
        return answer
    return await descriptions.stored(source, media, chosen.source_id, season, ids=ids) or answer


async def season_facts(settings, chosen: detail_source.Answerer, media, season: int, *,
                       ids=None, fresh: bool = False) -> dict:
    """One season reduced to app/providers/season.py's summary — what a tracker
    row's total, cadence and dates are — from the season's SHAPE as the title
    store holds it (how many episodes, which days they air), the row a panel's
    season tab also reads.

    THE TRACKER'S COLD MONTH LOAD WAS THE REASON: every row asked its season of
    a service whenever the response cache had let the answer go — thirty
    lookups, 4.2 s, on a month nobody had opened that day — for a shape that
    changes when an episode is announced. Kept, a month of rows is a few indexed
    reads, and a season past its clock is re-read behind the page
    (app/metadata/refresh.py) like any other row the page drew.

    ASKED, AND KEPT, only where the store does not know the season's shape — or
    when `fresh` says the viewer pressed Refresh, which asks past every cache.
    THE ASK IS THE SERVICE'S SEASON SUMMARY (DetailPort.fetch_season_summary),
    one request: a row needs the shape, and the season's cast and episode titles
    are a panel's to ask for. A failure raises (SourceUnavailable), as a season
    lookup always has, so the row keeps its last-known numbers rather than
    recording a false zero.
    """
    source = Source(chosen.source)
    # "Has it started airing" is decided against today at read, never frozen at
    # write — the clock every tracker decision reads (app/clock.py).
    today = clock.today()
    if not fresh:
        stored = await db.run(lambda conn: descriptions.season_facts(
            conn, source, media, chosen.source_id, int(season), today=today, ids=ids))
        if stored is not None:
            return stored
    summary = await providers.get(source).detail_port.fetch_season_summary(
        settings, chosen.source_id, int(season), Media(media), fresh=fresh)
    if not descriptions.keeps_the_answer(summary, today):
        logger.debug("title store: %s's season %s of %s is answered, not kept: its "
                     "days do not give its summary back", source, season, chosen.source_id)
        return summary
    # AN EMPTY SEASON IS KEPT TOO: "this service holds none of it" is what hands
    # the season to the next service (detail_source.for_season), and keeping it
    # saves asking again until its clock says to.
    try:
        await db.transaction(lambda conn: descriptions.write_season_summary(
            conn, source, media, chosen.source_id, {**summary, "season": int(season)},
            ids=ids, now=db.now()))
    except db.DatabaseError:
        # The row still gets the answer it waited for; the next load asks again,
        # which is all a failed write costs.
        logger.warning("title store: %s's season %s of %s could not be kept",
                       source, season, chosen.source_id, exc_info=True)
    return summary


async def season_list(settings, chosen: detail_source.Answerer, media, *, ids=None,
                      cached_only: bool = False) -> list[dict]:
    """The chosen service's season list for a title — base.SeasonsAnswer's
    `seasons` — from the title store, asked of the service and kept only where
    the store holds none. What a board's season control offers and what a board
    row that is one season draws its count and name from (app/metadata/labels.py).

    `cached_only` is the caller's restriction, as for DetailPort.fetch_seasons: a
    moment that must not spend a request (a bulk arrival) asks the store and the
    service's response cache and nothing else. Raises SourceUnavailable where the
    service could not be asked.
    """
    source = Source(chosen.source)
    stored = await db.run(lambda conn: descriptions.listed_seasons(
        conn, source, media, chosen.source_id, ids=ids))
    if stored:
        return stored
    listed = (await detail_source.seasons(settings, chosen, media,
                                          cached_only=cached_only)).seasons
    if listed:
        try:
            await db.transaction(lambda conn: descriptions.write_season_list(
                conn, source, media, chosen.source_id, listed, now=db.now(), ids=ids))
        except db.DatabaseError:
            logger.warning("title store: %s's season list of %s could not be kept",
                           source, chosen.source_id, exc_info=True)
    return listed


async def keep_season_list(source, media, own_id, listed: list[dict], *, ids=None) -> None:
    """File a season list a caller already holds — one a search result's
    checklist was drawn from — in the title store, under the service that
    listed it, so the season rows it makes draw their counts and names from
    there. A failed write costs those words until the title's next description."""
    if not listed:
        return
    try:
        await db.transaction(lambda conn: descriptions.write_season_list(
            conn, str(source), str(media), own_id, listed, now=db.now(), ids=ids))
    except db.DatabaseError:
        logger.warning("title store: %s's season list of %s could not be kept",
                       source, own_id, exc_info=True)


async def _describe_season(settings, chosen, ids, media, season: int | None, *,
                           cache_only: bool = False):
    """(who answered, their answer) with the handover rule
    detail_source.for_season states, unpresented.

    WITH A SEASON, an answer HOLDS it when it lists the season's episodes. WITH
    NONE, an answer holds the title when it says anything at all
    (`says_anything`). The second is the same rule one level up: the ids on a
    card can name a title the first service does not have — a stale or merged
    id, answered with nothing — and with that service first in the order the
    panel would otherwise open blank on a title the next service describes.
    """
    if season is None:
        holds = says_anything
    else:
        holds = lambda details: bool(details and details.get("episodes"))  # noqa: E731
    return await detail_source.for_season(
        settings, ids, chosen,
        lambda answerer: describe(settings, answerer, media, season, ids=ids,
                                  cache_only=cache_only),
        holds)


async def describe_season(settings, chosen: detail_source.Answerer, ids, media,
                          season: int | None, *, cache_only: bool = False):
    """(who answered, the answer as a page draws it) for one season of a title —
    what a season tab draws. A panel draws the answering service's name and
    links, so it is told which one that was."""
    answerer, details = await _describe_season(settings, chosen, ids, media, season,
                                               cache_only=cache_only)
    return answerer, presented(details, settings)


async def describe_panel(settings, chosen: detail_source.Answerer, ids, media,
                         season: int | None, *, cache_only: bool = False,
                         with_seasons: bool = True):
    """`describe_season`, with `videos` replaced by EVERY service's videos
    pooled (app/providers/videos.py says why that one field is pooled when
    nothing else is), and — for a show, unless `with_seasons` is False —
    `seasons` widened to every season some service the ids name lists.
    What every surface calls to draw a WHOLE panel.

    THE STRIP IS WHAT THE SERVICES LIST (details_panel.panel draws `seasons`
    and the season open): the answering service's own list, stated in its
    answer, and every other service's the store or its response cache holds
    (`_cached_seasons`) — because a season tab is answered by
    whichever of them holds the season (detail_source.for_season), so each listed
    season is one a tab can draw and nothing else is. It used to be counted, a
    tab for every number up to the larger of a service's season count and the
    season open, and Simkl numbers a daily or talk show's seasons by year: a card
    of season 2026 drew 2,026 tabs, every phantom one a lookup that stored an
    empty season when pressed. A surface that draws no strip (a share page)
    passes `with_seasons=False` and reads no other service's list.

    `ids` IS WHAT THE CALLER MAY ASK ABOUT, and it is the same map the description
    is asked with — which is what keeps the share page's promise: it passes the
    ids its link's airings carry and nothing else, so a service asked for its
    videos is a service asked about a title the link shows. Each service is asked
    by its own id in that map, never by an id another service stated.

    ASKED BESIDE THE DESCRIPTION, not after it, so the panel waits for the
    slower of the two rather than their sum. Only where the season rule hands
    the description to a later service is the first service's list asked
    afterwards — its description was already read, so that is normally stored.

    A SERVICE THAT FAILS contributes nothing and never fails the panel — the
    videos are an addition to a description, not a reason to lose one.
    """
    every = detail_source.known_ids(ids)
    others = [(source, value) for source, value in every if source != chosen.source]
    listing = every if with_seasons and str(media) == str(Media.SHOW) else []
    described, *gathered = await asyncio.gather(
        _describe_season(settings, chosen, ids, media, season, cache_only=cache_only),
        *(_videos(settings, source, value, media, ids=ids, cache_only=cache_only)
          for source, value in others),
        *(_cached_seasons(settings, source, value, media, ids=ids)
          for source, value in listing))
    answerer, details = described
    if details is None:
        return answerer, None
    lists, numbers = gathered[:len(others)], gathered[len(others):]
    seasons = {"seasons": _widened(details.get("seasons"), numbers)} if listing else {}
    by_source = {source: listed for (source, _id), listed in zip(others, lists)}
    by_source[answerer.source] = list(details.get("videos") or [])
    if chosen.source not in by_source:
        by_source[chosen.source] = await _videos(
            settings, chosen.source, chosen.source_id, media, ids=ids, cache_only=cache_only)
    order = [source for source, _port in detail_source.ports()]
    pooled = videos.pool([by_source[source] for source in order if source in by_source],
                         str(answerer.source))
    return answerer, presented({**details, "videos": pooled, **seasons}, settings)


def _widened(own, others) -> list[dict]:
    """The answering service's season list with every season another service
    lists added — its own entry for a season both list."""
    by_number = {int(entry["season"]): entry for entry in own or ()
                 if entry.get("season") is not None}
    for listed in others:
        for entry in listed:
            if entry.get("season") is not None:
                by_number.setdefault(int(entry["season"]), entry)
    return [by_number[number] for number in sorted(by_number)]


async def _cached_seasons(settings, source: Source, source_id: str, media, *,
                          ids) -> list[dict]:
    """The season list one service has for a title (`season_list`); [] where it
    could not answer.

    NEVER A REQUEST: the store's copy, else what the service's response cache
    holds. Describing the title has just warmed what a describing service's list
    reads (TMDB's from the same /tv answer, Trakt's from the season list its
    description reads), while asking every service live cost a request each on
    every panel opened — two for Simkl, whose list reads a record its
    description does not. A list nobody has read yet offers no tabs until it
    is, rather than a guessed one."""
    answerer = detail_source.Answerer(source, source_id, stored_only=True)
    try:
        listed = await season_list(settings, answerer, media, ids=ids, cached_only=True)
    except SourceUnavailable:
        logger.warning("%s could not list the seasons of %s; the strip offers the others",
                       source, source_id)
        return []
    return list(listed)


async def _videos(settings, source: Source, source_id: str, media, *, ids,
                  cache_only: bool) -> list[Video]:
    """One service's videos, [] where it could not answer: its stored
    description's, where the store holds one, and otherwise its own list — read
    from its response cache alone where it cannot be reached."""
    stored = await descriptions.stored(source, media, source_id, None, ids=ids)
    if stored is not None:
        return list(stored.get("videos") or [])
    port = providers.get(source).detail_port
    try:
        return await port.fetch_videos(
            settings, Media(media), source_id,
            cache_only=cache_only or not port.catalogue_configured(settings))
    except SourceUnavailable:
        logger.warning("%s could not list videos for %s; the panel plays the others",
                       source, source_id)
        return []
