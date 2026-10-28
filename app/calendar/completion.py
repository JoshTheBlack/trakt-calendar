"""Matching the calendar's titles across services in the background, a few a
minute, on the heartbeat.

TWO QUESTIONS, EACH ASKED OF THE ONE SERVICE THAT CAN ANSWER IT, AT THAT
SERVICE'S PACE:

  - A TITLE WITH NO TMDB ID — one Trakt or Simkl listed by its tvdb or imdb id
    alone — is asked of TMDB by those ids (`/find`). An answer re-keys the title
    up the waterfall (identity.settle), so where TMDB lists the same premiere the
    two cards become one, and the card is described by TMDB like every other.
    MEASURED on the development copy: 159 such cards on the three premiere
    calendars over three months, among them Under Her Rules, drawn twice beside
    TMDB's own card for it; a live run asked about October's and November's 85
    in two passes (95 requests) and TMDB knew three, that one included.
  - A PREMIERE OR FILM ONLY TMDB LISTS is asked of Trakt by its TMDB id, and
    where Trakt lists the title its own airing of that season (or film) is
    written beside TMDB's — the completion a panel's open already makes
    (calendar/search.py's `complete_from_other_service`), on a schedule. MEASURED
    on a sample of 40 October TMDB-only season premieres: Trakt knew 19 of them,
    and its premieres calendar listed none; a live run of six passes found
    about three in five known to Trakt, most films among them with no release
    date Trakt could give, so their cards keep TMDB's day and gain only Trakt's
    (greyed) button. TMDB's derived Season Premieres
    are the bulk — 1,158 of 1,442 TMDB-only cards over three months — and the
    arrival rate falls to a couple of hundred a week once a month is matched.

WHY NOT EVERY ONE-SERVICE CARD, WHICH IS WHAT A PANEL OPEN WOULD ASK: most of
them are one service's alone (calendar/search.py's `other_service_to_ask`
measures it), and Simkl lists thousands of films nobody else does — 5,744
film cards over two months Trakt does not list. Simkl itself is never asked
here: its lookup needs a viewer's own token, and the heartbeat has none.

TRAKT IS PACED BY THE REQUESTS A PASS SENDS, NOT BY CARDS. A card costs nothing
when the response cache holds Trakt's answers and up to five reads when it does
not — the lookup tries each shared id in turn, and a match reads the title's
seasons — so a pass counts what it actually sent (http_pool.tally) and stops at
`TRAKT_READS_PER_MINUTE`. Those reads go out under this instance's client id,
whose allowance is 500 GETs every five minutes — Trakt publishes 1,000, and
MEASURED, two instances sharing one client id were refused (a 300-second
Retry-After, every read for five minutes) about 495 reads into the five minutes
before the refusal. So a pass also yields
to everything else spending it: it sends nothing once the whole app has sent
`TRAKT_APP_CEILING` reads in the last five minutes (http_pool.sent_within).
Six cards a pass, as a fixed count, measured 3 to 21 requests, which is why the
count is of requests; MEASURED live at thirty a pass, a TMDB-only card costs
about three (9 cards in 32 requests, then 11 in 30), so a pass takes some
fifteen cards. TMDB's `/find` is one or two cheap requests on a client measured
at forty a second with no 429.

EVERY ANSWER IS REMEMBERED FOR A WEEK, "not listed" included, in the response
cache: a Trakt answer under the same key a panel's open remembers it by
(search.asked_key), so neither asks again what the other has. A service that
cannot be asked is left alone for `REST_SECONDS` and asked again after, and
nothing is remembered for it — an outage is not an answer.

THE SPAN IS THE MONTHS THE SCHEDULE KEEPS CURRENT — this one and the next
(cache.REFRESH_TIERS) — nearest day first: those are the cards a viewer is
looking at, and older months' cards are matched when a panel is opened on them.
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import date
from typing import NamedTuple

from .. import cache, clock, db, http_pool, metadata, providers
from ..metadata import identity as title_identity
from ..providers.base import Media, Source
from . import entries
from . import search as calendar_search

logger = logging.getLogger(__name__)

# What each question may spend in a minute — the heartbeat's interval, so in a
# pass: TMDB's in titles asked, Trakt's in REQUESTS SENT. The module docstring
# says why each is counted the way it is.
TMDB_PER_MINUTE = 60
TRAKT_READS_PER_MINUTE = 50

# AND WHAT THE WHOLE APP MAY HAVE SENT TRAKT before a pass sends anything: 350
# reads in the last five minutes, so the pages always keep 150 of the 500 —
# a details panel is about five, a month's windows a handful. A pass takes the
# lesser of its own allowance and what is left under this: a backlog drain or a
# viewer's busy minute beside it is spending the same allowance
# (http_pool.sent_within says why that is measured).
TRAKT_APP_CEILING = 350

# How many cards a pass may weigh for Trakt, whatever they cost. A card whose
# answers the response cache holds sends nothing, so the reads alone would not
# bound a pass; this does, at a few seconds of database work.
TRAKT_CARDS_PER_PASS = 60

# TMDB's finds side by side, a few of its client's twenty in flight, the rest
# left to the pages. Trakt's go one at a time: its client gates every call
# behind one semaphore anyway (app/calendar/enrich.py's drain_releases).
TMDB_IN_FLIGHT = 4

# How long a service that could not be asked is left alone.
REST_SECONDS = 15 * 60

# How long an answer stands: a title a service does not list this week is
# usually one it has not got round to, and a week is the panel's own clock.
ASK_AGAIN_SECONDS = calendar_search.OTHER_SERVICE_ASK_SECONDS

# Where "TMDB was asked about this title this week" is remembered. Title-level,
# unlike Trakt's, because the question is the title's ids, not one card's season.
_FIND_PREFIX = "calendar_tmdb_find_asked:"

# {service: monotonic moment it may be asked again}, after it could not be.
# Two keys at most, so nothing here needs pruning.
_resting_until: dict[str, float] = {}


def _find_key(title_key: str) -> str:
    return f"{_FIND_PREFIX}{title_key}"


def span(today: date) -> tuple[date, date]:
    """[first, last): this month and the next, the months the schedule keeps
    current."""
    first = today.replace(day=1)
    month = first.month + 2
    return first, date(first.year + (month - 1) // 12, (month - 1) % 12 + 1, 1)


def _resting(name: str) -> bool:
    return time.monotonic() < _resting_until.get(name, 0.0)


def _rest(name: str) -> None:
    _resting_until[name] = time.monotonic() + REST_SECONDS
    logger.info("calendar completion: %s could not be asked; leaving it for %d minutes",
                name, REST_SECONDS // 60)


async def run(settings, *, today: date | None = None) -> int:
    """One pass: TMDB asked about the span's titles with no TMDB id, and Trakt
    about its TMDB-only premieres and films, each up to its minute's allowance.
    Returns how many titles and cards were asked about."""
    today = clock.today() if today is None else today
    first, last = span(today)
    cards = await entries.listed_cards(calendar_search.COMPLETABLE_ENDPOINTS, first, last)
    if not cards:
        return 0
    cards.sort(key=lambda card: abs((card.day - today).days))
    askable = {str(source) for source, _port in providers.for_shared_id_lookup(settings)}
    found = await _ask_tmdb(settings, cards, askable)
    listed = await _ask_trakt(settings, cards, askable)
    if found[0] or listed.asked:
        logger.info(
            "calendar completion: TMDB asked about %d title(s) with no TMDB id (%d found); "
            "Trakt asked about %d TMDB-only card(s) in %d request(s) (%d airing(s) "
            "stored); %d card(s) still to ask Trakt about", found[0], found[1],
            listed.asked, listed.reads, listed.stored, listed.owed)
    return found[0] + listed.asked


async def _ask_tmdb(settings, cards, askable) -> tuple[int, int]:
    """(asked, found) — TMDB asked by shared id about each due title holding no
    TMDB id, nearest first."""
    name = str(Source.TMDB)
    if name not in askable or _resting(name):
        return 0, 0
    titles = {}
    for card in cards:
        if (card.title_key not in titles and title_identity.tmdb_of(card.title_key) is None
                and not card.known_ids.get(name)):
            titles[card.title_key] = card
    if not titles:
        return 0, 0
    asked_already = await cache.fresh_keys(map(_find_key, titles), ASK_AGAIN_SECONDS)
    due = [card for key, card in titles.items() if _find_key(key) not in asked_already]
    gate = asyncio.Semaphore(TMDB_IN_FLIGHT)

    async def _one(card) -> bool | None:
        """True found, False not (remembered either way), None not asked."""
        async with gate:
            held = await metadata.group_identity(card.title_key)
            answer = None
            if held is not None:
                media, ids, title = held
                answer = await metadata.ask(Media(media), ids, settings=settings,
                                            filed_under=card.title_key, asking=(name,))
                if name in answer.unreachable:
                    return None
            await cache.set(_find_key(card.title_key), 1, retain_seconds=ASK_AGAIN_SECONDS)
            if answer is not None and answer.identity is not None \
                    and answer.identity.ids.get(name):
                logger.info("calendar completion: TMDB knows %s (%s) as %s", title,
                            card.title_key, answer.identity.key)
                return True
            return False

    answers = await asyncio.gather(*(_one(card) for card in due[:TMDB_PER_MINUTE]))
    if None in answers:
        _rest(name)
    return sum(1 for a in answers if a is not None), sum(1 for a in answers if a)


class TraktPass(NamedTuple):
    """What one pass asked of Trakt: cards asked, Trakt requests they sent,
    airings stored, and cards still owed an ask."""
    asked: int = 0
    reads: int = 0
    stored: int = 0
    owed: int = 0


async def _ask_trakt(settings, cards, askable) -> TraktPass:
    """Trakt asked about each due card only TMDB lists, nearest first, one at a
    time, until the pass has sent its minute's reads."""
    name = str(Source.TRAKT)
    # Only where Trakt's calendar is this instance's to write to: a service
    # writes an airing only onto a calendar it publishes (search._publishes),
    # and an operator may have switched Trakt's calendar off.
    trakt = next((p for p in providers.calendar_sources(settings=settings)
                  if str(p.source) == name and p.calendar_port.calendar_configured(settings)),
                 None)
    if name not in askable or trakt is None:
        return TraktPass()
    tmdb_only = [card for card in cards if card.sources == {str(Source.TMDB)}
                 and trakt.capabilities.answers(card.endpoint)]
    if not tmdb_only:
        return TraktPass()
    keyed = [(card, calendar_search.asked_key(name, card.endpoint, card.title_key,
                                              card.season)) for card in tmdb_only]
    asked_already = await cache.fresh_keys((key for _card, key in keyed), ASK_AGAIN_SECONDS)
    due = [(card, key) for card, key in keyed if key not in asked_already]
    if _resting(name):
        return TraktPass(owed=len(due))
    allowance = min(TRAKT_READS_PER_MINUTE, TRAKT_APP_CEILING - http_pool.sent_within(
        name, http_pool.RECENT_SECONDS))
    if allowance <= 0:
        return TraktPass(owed=len(due))
    asked = stored = 0
    # Trakt's own pool is declared under the service's name
    # (providers/trakt/transport.py's POOL), which is what the tally counts by.
    with http_pool.tally() as sent:
        # A CARD THE PANEL'S RULE TURNS DOWN costs no ask, but a check, and so
        # does one the cache answers: both count towards the cards a pass weighs.
        # The worklist already counts a listing at any date, so what is turned
        # down here is a race with a panel's own ask.
        for card, key in due[:TRAKT_CARDS_PER_PASS]:
            # The check comes BEFORE a card, so a pass may end one card's reads
            # past the allowance — at most five — rather than guess what the next
            # card will cost.
            if sent.get(name, 0) >= allowance:
                break
            # THE PANEL'S OWN RULE DECIDES, re-read now: only TMDB draws this
            # season anywhere on the calendar (a listing outside the span counts),
            # Trakt publishes it here and can be asked, and nobody asked this week.
            if await calendar_search.other_service_to_ask(
                    settings, endpoint_key=card.endpoint, title_key=card.title_key,
                    season=card.season) != name:
                continue
            wrote = await calendar_search.complete_from_other_service(
                settings, source=name, endpoint_key=card.endpoint, title_key=card.title_key,
                season=None if card.media == str(Media.MOVIE) else card.season,
                now=db.now(), ask_every_service=False)
            if not wrote and await cache.get(key, ASK_AGAIN_SECONDS) is None:
                # NOT REMEMBERED: Trakt could not be asked, which is no answer (or
                # the card's title left the store between the read and the ask,
                # which costs the same quarter hour and nothing else).
                _rest(name)
                break
            asked += 1
            stored += bool(wrote)
    return TraktPass(asked, sent.get(name, 0), stored, max(0, len(due) - asked))
