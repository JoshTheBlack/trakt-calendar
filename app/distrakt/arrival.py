"""What a record IS, decided once, whichever way it reached the tracker.

A TITLE REACHES THE TRACKER SIX WAYS — a calendar import (and the month build that
runs one), a search add, a finished season stated by hand, a history prompt, the
history backfill and the month rollover — and each used to build its record from
whatever its own door handed it. The same title then arrived differently by
different doors: a history row with no network and only the surveying service's
ids, a calendar row with the listing service's ids, a search row with both. A
settled row is never revisited, so the difference stayed for good.

SO THE DESCRIPTION IS NOT THE PATH'S TO WRITE. Every path hands `describe` the
identity and season it has, plus whatever it happened to be handed, and gets back
one answer built by one rule:
  - THE IDS: what the path had, plus each service's own stored calendar record of
    the title, plus — for a service still missing — that service asked by shared
    id (SearchPort.find_by_shared_id: an exact match on a shared id, never a title
    search). Add-only throughout.
  - THE DESCRIBING SERVICE is then `live.detail_source` over those ids — the one
    service a row's season lookups and counts already come from.
  - TITLE AND NETWORK are that service's words for that service's own id: its
    stored calendar record when the stored calendar holds exactly that title, and
    `live.title_facts` otherwise. The same question, answered from disk or asked,
    so the answer cannot depend on which.
  - THE SEASON — total, cadence, premiere, finale and the airing flags — is that
    service's season lookup.
What stays the path's is what is genuinely about the viewer or the act: which
kind of record it becomes, how much has been watched, and who added it.

WHAT CANNOT BE MADE IDENTICAL, stated rather than hidden: a lookup that fails
leaves the path's own value in place (a row is better than no row), and a service
that has not catalogued a title yet cannot be asked about it — which is a
difference in TIME, not in path, and is re-asked on the lookup's own cadence.
"""
from __future__ import annotations

import asyncio
import logging
from typing import NamedTuple

from .. import cache, providers
from ..calendar import entries as calendar_entries
from ..providers.base import MATCH_SOURCES, Media, TitleFacts, collect_ids
from . import live, prefs, store
from .lifecycle import CATALOGUE_FIELDS
from .store import record_key

logger = logging.getLogger(__name__)

# What a record's DESCRIPTION is: the fields `describe` decides and no path may
# decide for itself. The conformance test holds every path to exactly this set.
DESCRIPTION_FIELDS = ("title", "network", "ids", *CATALOGUE_FIELDS)


class Arrival(NamedTuple):
    """One described record, and the season lookup it was described from.

    `detail` is the describing service's whole season summary — the paths make
    decisions on it (has it started airing, how many episodes, when) — and is {}
    when no service could be asked or the lookup failed, which is what every
    path already reads as "no answer".
    """
    record: dict
    detail: dict


async def describe(settings, records: list[dict], *, strict: bool = False) -> list[Arrival]:
    """`records` described, in their own order — see the module docstring.

    `strict` is for the one caller that must not file a guess: a season stated
    as finished on a month that is over, whose episode total is never recomputed.
    It makes a failed season lookup RAISE (SourceUnavailable) instead of
    degrading to {}.

    ONE TITLE IS DESCRIBED ONCE however many seasons of it arrive together, so a
    backfill naming six seasons of a show asks its lookups once.
    """
    out = [{**rec, "ids": canonical_ids(rec.get("ids") or {})} for rec in records]
    groups: dict[str, list[dict]] = {}
    for rec in out:
        try:
            key = record_key(rec)
        except ValueError:
            continue  # unkeyable: the write will refuse it, with its own sentence
        # THE FILING IDENTITY IS PINNED BEFORE ANY ID IS LEARNED. A learned tmdb
        # on a title filed under tvdb would otherwise resolve to a different key
        # and split the title from its own earlier rows — learning an id is a
        # repair, never a re-identification.
        rec.update(media=key.media, match_source=key.match_source, match_id=key.match_id)
        groups.setdefault(str(key), []).append(rec)
    if not groups:
        return [Arrival(rec, {}) for rec in out]
    known = await calendar_entries.known_about(groups)

    held = {key: _merged_ids(recs) for key, recs in groups.items()}
    for key in groups:
        held[key].update(calendar_teaches(held[key], known.get(key) or {}))
    await _ask_missing_services(settings, groups, held)
    for key, recs in groups.items():
        _describing_service_states_the_shared_ids(settings, recs[0], held[key],
                                                  known.get(key) or {})
    for key, recs in groups.items():
        for rec in recs:
            rec["ids"] = canonical_ids(held[key])

    facts = await _title_facts(settings, groups, known)
    for key, recs in groups.items():
        title, network = facts.get(key) or TitleFacts("", "")
        for rec in recs:
            # A BLANK ANSWER KEEPS THE PATH'S VALUE: the service could not be
            # asked or has nothing to say, and a row with the path's words is
            # better than a row with none.
            rec["title"] = title or str(rec.get("title") or "")
            rec["network"] = network or str(rec.get("network") or "")

    keyed = [rec for recs in groups.values() for rec in recs]
    details = await live.fetch_season_details(
        settings, [{**rec, "media": rec.get("media") or Media.SHOW} for rec in keyed],
        fresh=False, allow_degrade=not strict)
    found: dict[int, dict] = {}
    for rec, detail in zip(keyed, details):
        if isinstance(detail, BaseException) or not detail:
            if isinstance(detail, BaseException):
                logger.info("tracker arrival: no season answer for %s S%s (%r)",
                            record_key(rec), rec.get("season"), detail)
            continue
        rec.update({field: detail.get(field) for field in CATALOGUE_FIELDS})
        found[id(rec)] = dict(detail)
    logger.info("tracker arrival: described %d record(s) of %d title(s)",
                len(keyed), len(groups))
    return [Arrival(rec, found.get(id(rec), {})) for rec in out]


# How long a title whose describing service stated no network waits before it is
# asked again. A day, like every other catalogue re-ask here.
NETWORK_REASK_SECONDS = 24 * 60 * 60


async def ask_for_missing_networks(user_id: int, settings) -> int:
    """Ask the describing service for the network of every title this viewer
    holds rows for that still has none — once per title per day. Returns how many
    row values were written.

    THE HALF OF THE REPAIR THE STORED CALENDAR CANNOT DO. naming.fill_from_calendar
    answers blank networks from disk and asks nobody; a title no stored calendar
    record names (an older show, one watched off-calendar) stayed blank for good —
    MEASURED on the owner's copy, 10 rows after that pass, among them Jury Duty
    Presents: Company Retreat and Quiet on Set. This asks `live.title_facts`, the
    same question a newly arriving row is asked, so a repaired row carries the
    same network a new one would.

    A PER-TITLE CLOCK, NOT A SIGNATURE: whether a service states a network is a
    fact that can change upstream with nothing local moving, so every title
    carries its own day-long marker (global — a network is a fact about the
    title, not the viewer) rather than one verdict over everything owed.
    """
    owed = [debt for debt in await store.identities_owing(user_id)
            if store.NETWORK in debt.names and not debt.network]
    due = []
    for debt in owed:
        if not await cache.get(_network_marker(debt.key), NETWORK_REASK_SECONDS):
            due.append(debt)
    if not due:
        return 0
    answers = await asyncio.gather(
        *(live.title_facts(settings, {"media": debt.key.media, "ids": debt.ids})
          for debt in due),
        return_exceptions=True)
    changed = 0
    for debt, got in zip(due, answers):
        if isinstance(got, BaseException):
            # Not remembered: "could not ask" is not an answer to wait a day on.
            logger.info("tracker arrival: no network for %s (%r)", debt.key, got)
            continue
        await cache.set(_network_marker(debt.key), True)
        if got.network:
            changed += await store.learn_network(user_id, debt.key, got.network)
    if changed:
        logger.info("tracker arrival: learned networks for user %s by asking "
                    "(%d row value(s), %d title(s) asked)", user_id, changed, len(due))
    return changed


def _network_marker(key) -> str:
    return f"tracker_network_asked:{key}"


async def register(user_id: int, records: list[dict]) -> None:
    """Put every network these records carry into the viewer's emoji map, so a
    network any path filed appears in their editor ready to customize — one call
    for every path, where it used to be made by some paths and not others."""
    await prefs.register_networks(user_id, [rec.get("network") for rec in records])


def calendar_teaches(held: dict, by_source: dict) -> dict:
    """The ids the stored calendar can add to `held`, and only additions.

    Each service's stored record may teach: that SERVICE'S OWN id and name for
    the title, where `held` has no id for that service — both together, so a row
    never names a service it cannot ask — or its name where `held` already has
    that very id; and any SHARED id `held` lacks. A service's record for a
    DIFFERENT id than the one held (Simkl files each anime season as its own
    title) teaches nothing about that service, since it is about another title.

    WHY A JOIN IS TRUSTED: the record sits under this identity because its
    service states the same shared id the row is filed under — an exact match on
    a shared id, the same one the calendar merges cards on. The row's own filing
    id is never touched: it is held by definition, and additions only fill gaps.
    Services are read in registry order, so where two disagree about a shared id
    the answer is the same every time.
    """
    out: dict = {}
    for source in providers.registered():
        service = str(source)
        row = by_source.get(service)
        if row is None:
            continue
        own = row.ids.get(service)
        if own in (None, ""):
            continue
        mine = held.get(service)
        if mine in (None, ""):
            out[service] = own
        elif str(mine) != str(own):
            continue
        name = row.ids.get(f"{service}_slug") or row.ids.get("slug")
        if name not in (None, "") and held.get(f"{service}_slug") in (None, ""):
            out.setdefault(f"{service}_slug", name)
        for shared in MATCH_SOURCES:
            if held.get(shared) in (None, "") and row.ids.get(shared) not in (None, ""):
                out.setdefault(shared, row.ids[shared])
    return out


# The id spaces whose ids are numbers. A service may state one as text — Simkl's
# ids block carries tmdb as "315142" where Trakt's carries 315142 — and the same
# title must not be filed with a different TYPE of id by a different door.
_NUMERIC_IDS = frozenset({"trakt", "simkl", "tmdb", "tvdb", "mal"})


def canonical_ids(ids: dict) -> dict:
    """`ids` as every path files them: ID_KEYS only, numeric spaces as numbers,
    and WITHOUT the bare `slug`.

    THE BARE SLUG IS AMBIGUOUS BY DEFINITION — both services call their readable
    name `slug` and disagree about it (`the-rookie` against `the-rookie-2018`) —
    so it depended on which service the path happened to read. Each service's
    name travels namespaced (`trakt_slug`, `simkl_slug`), which is what every
    link is built from; the bare one survives only on rows written before the
    split, as their readers' fallback.
    """
    out = {}
    for name, value in collect_ids(ids).items():
        if name == "slug":
            continue
        if name in _NUMERIC_IDS and isinstance(value, str) and value.strip().isdigit():
            value = int(value.strip())
        out[name] = value
    return out


def _describing_service_states_the_shared_ids(settings, rec: dict, held: dict,
                                               by_source: dict) -> None:
    """Where the two services DISAGREE about a shared id, the describing
    service's word is the one filed — the same authority the title and network
    come from. MEASURED on a live instance: Trakt states one imdb id for Little
    House on the Prairie (2026) and Simkl another, so the same title arrived with
    either depending on which door it came through.

    ONLY WHERE THAT SERVICE'S OWN RECORD OF THIS EXACT TITLE IS STORED, which is
    what makes the answer the same whoever arrives; and never the id the row is
    FILED under, which addresses it. This decides a record not yet written —
    stored rows are only ever added to.
    """
    service = live.detail_source({**rec, "ids": held}, settings)
    row = by_source.get(service) if service else None
    if row is None or str(row.ids.get(service)) != str(held.get(service)):
        return
    filed_under = rec.get("match_source")
    for shared in MATCH_SOURCES:
        stated = row.ids.get(shared)
        if shared == filed_under or stated in (None, ""):
            continue
        if held.get(shared) not in (None, "") and str(held[shared]) != str(stated):
            logger.info("tracker arrival: %s states %s=%s for %s where the path had %s",
                        service, shared, stated, rec.get("match_id"), held[shared])
        held[shared] = stated


def _merged_ids(recs: list[dict]) -> dict:
    """The union of the id maps several records of one title carry, first
    stated value winning — they are one title's ids, so they agree."""
    merged: dict = {}
    for rec in recs:
        for name, value in (rec.get("ids") or {}).items():
            merged.setdefault(name, value)
    return merged


async def _ask_missing_services(settings, groups: dict, held: dict) -> None:
    """Ask each catalogue this instance can search for the titles it has no id
    for, by shared id, and add what an exact match answers. Every learned id is
    logged by name; a service that could not be asked is logged and skipped,
    never remembered as "not listed" — its own cache holds real answers only."""
    asked = [(key, source, port)
             for key in groups
             for source, port in providers.for_catalogue_search(settings)
             if held[key].get(str(source)) in (None, "")]
    if not asked:
        return
    answers = await asyncio.gather(
        *(port.find_by_shared_id(settings, Media(groups[key][0].get("media") or Media.SHOW),
                                 dict(held[key]))
          for key, _source, port in asked),
        return_exceptions=True)
    for (key, source, _port), got in zip(asked, answers):
        if isinstance(got, BaseException):
            logger.info("tracker arrival: %s could not be asked about %s (%r)", source, key, got)
            continue
        learned = {name: value for name, value in collect_ids(got or {}).items()
                   if held[key].get(name) in (None, "")}
        if learned.get(str(source)) in (None, ""):
            continue  # an answer that does not name this service's own id is no answer
        logger.info("tracker arrival: %s answered %s for %s by shared id",
                    source, learned, key)
        held[key].update(learned)


def stored_facts(settings, rec: dict, by_source: dict) -> TitleFacts | None:
    """The describing service's title and network for `rec` from the stored
    calendar (`by_source` is calendar.entries.known_about's answer for its
    title), or None when disk does not hold exactly what a lookup would answer —
    see `_title_facts`. Also how naming.py repairs a row stored without a network,
    so a repaired row and a newly arrived one carry the same words."""
    service = live.detail_source(rec, settings)
    if not service:
        return None
    own = (rec.get("ids") or {}).get(service)
    row = by_source.get(service)
    if (row is None or own in (None, "") or str(row.ids.get(service)) != str(own)
            or not row.title or not row.network):
        return None
    return TitleFacts(row.title, row.network)


async def _title_facts(settings, groups: dict, known: dict) -> dict[str, TitleFacts]:
    """{title key: the describing service's title and network}.

    FROM DISK ONLY WHEN DISK HOLDS EXACTLY THE ANSWER A LOOKUP WOULD GIVE: the
    describing service's own stored record, for the very id the row carries, and
    stating both. Anything less — another of that service's titles (an anime
    season stored under the series' key), a record with no network yet — is
    asked, so a stored answer and a fetched one can never be two different
    things. MEASURED on a live instance, the stored calendar answered most of the
    titles a history backfill had filed blank.
    """
    out: dict[str, TitleFacts] = {}
    lookups: list[str] = []
    for key, recs in groups.items():
        stored = stored_facts(settings, recs[0], known.get(key) or {})
        if stored is not None:
            out[key] = stored
        else:
            lookups.append(key)
    answers = await asyncio.gather(
        *(live.title_facts(settings, groups[key][0]) for key in lookups),
        return_exceptions=True)
    for key, got in zip(lookups, answers):
        if isinstance(got, BaseException):
            logger.info("tracker arrival: no title facts for %s (%r)", key, got)
            continue
        out[key] = got
    logger.info("tracker arrival: title facts for %d title(s), %d from the stored "
                "calendar, %d asked for", len(groups), len(groups) - len(lookups), len(lookups))
    return out
