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
  - THE TITLE is the title store's (app/metadata): the key a row is filed under
    is the key the store answers for the path's ids, so a play naming a show by
    its tvdb id lands on the row its tmdb id already filed.
  - THE IDS: what the path had, plus what the store knows of the title, plus each
    service's own stored record of it, plus — for a service still missing — that
    service asked by shared id through the store (metadata.ensure: an exact match
    on a shared id, never a title search). Add-only throughout.
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

from .. import cache, db, metadata, providers
from ..calendar import entries as calendar_entries
from ..calendar import search as calendar_search
from ..metadata import ids as id_rules
from ..metadata import rows as title_rows
from ..providers.base import MATCH_SOURCES, Media, Source, SourceUnavailable, TitleFacts
from . import live, store
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
    out = [{**rec, "ids": id_rules.canonical(rec.get("ids") or {})} for rec in records]
    by_path: dict[str, list[dict]] = {}
    for rec in out:
        try:
            key = record_key(rec)
        except ValueError:
            continue  # unkeyable: the write will refuse it, with its own sentence
        by_path.setdefault(str(key), []).append(rec)
    if not by_path:
        return [Arrival(rec, {}) for rec in out]

    # WHICH TITLE EACH IS, IS THE TITLE STORE'S ANSWER (metadata.ensure), not the
    # path's. A history play naming a show only by its tvdb id is the title the
    # store already knows by its tmdb id, and it is filed there — one title, one
    # key, whichever door it came through. Asked twice: first with no service
    # asked, so the stored calendar can teach what it knows against the right
    # key; then asking each service the title still has no id from.
    registered = await metadata.ensure_many(
        [(recs[0].get("media") or Media.SHOW, _merged_ids(recs), path_key)
         for path_key, recs in by_path.items()])
    groups: dict[str, list[dict]] = {}
    held: dict[str, dict] = {}
    for (path_key, recs), title in zip(by_path.items(), registered):
        key = title.key if title is not None else path_key
        groups.setdefault(key, []).extend(recs)
        merged = held.setdefault(key, {})
        _fill(merged, _merged_ids(recs))
        if title is not None:
            _fill(merged, title.ids)
    known = await title_rows.known_about(groups)
    for key in groups:
        held[key].update(calendar_teaches(held[key], known.get(key) or {}))
    asked = await metadata.ensure_many(
        [(groups[key][0].get("media") or Media.SHOW, held[key], key) for key in groups],
        settings=settings)
    for key, title in zip(list(groups), asked):
        recs = groups[key]
        if title is not None:
            _fill(held[key], title.ids)
        # THE KEY IS PINNED TO WHAT THE STORE ANSWERED, and a learned id that
        # moved it moved every row already holding the old one in the same
        # transaction (app/metadata/holders.py) — so this row joins the title's
        # earlier rows rather than splitting from them.
        filed = title.triple if title is not None else None
        _describing_service_states_the_shared_ids(settings, recs[0], held[key],
                                                  known.get(key) or {}, filed)
        for rec in recs:
            if filed is not None:
                rec.update(media=filed.media, match_source=filed.match_source,
                           match_id=filed.match_id)
            else:
                path = record_key(rec)
                rec.update(media=path.media, match_source=path.match_source,
                           match_id=path.match_id)
            rec["ids"] = id_rules.canonical(held[key])

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

    # THE SEASON IS LOOKED UP BEFORE THE NAME IS ASKED FOR, because the service
    # that answered a record's season is the one that names it. Usually that is
    # the describing service either way; where it is not, the chosen service
    # had no episodes of the season (detail_source.for_season) and its name is
    # another show's — Trakt's "DAHMER – Monster: The Jeffrey Dahmer Story" on
    # Simkl's "Monster" season 4, which only Simkl holds. So the names are
    # asked per title AND per answering service; a record with no season
    # answer is named by the describing service, as before.
    named: dict[tuple[str, str | None], list[dict]] = {}
    for key, recs in groups.items():
        for rec in recs:
            answered = (found.get(id(rec)) or {}).get("described_by")
            named.setdefault((key, answered), []).append(rec)
    facts = await _title_facts(settings, named, known)
    for group, recs in named.items():
        title, network = facts.get(group) or TitleFacts("", "")
        for rec in recs:
            # A BLANK ANSWER KEEPS THE PATH'S VALUE: the service could not be
            # asked or has nothing to say, and a row with the path's words is
            # better than a row with none.
            rec["title"] = title or str(rec.get("title") or "")
            rec["network"] = network or str(rec.get("network") or "")
    logger.info("tracker arrival: described %d record(s) of %d title(s)",
                len(keyed), len(groups))
    return [Arrival(rec, found.get(id(rec), {})) for rec in out]


# How long a title whose describing service stated no network waits before it is
# asked again. A day, like every other catalogue re-ask here.
NETWORK_REASK_SECONDS = 24 * 60 * 60


async def ask_for_missing_networks(user_id: int, settings, *,
                                   within: set[str] | None = None) -> int:
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

    `within` NARROWS IT TO THE TITLES A LOAD IS DRAWING (their item keys), which
    is how the tracker's sync calls it — see watch_history.sync_and_baseline.
    None asks about every row, for a caller that means the whole account.
    """
    owed = [debt for debt in await store.identities_owing(user_id)
            if store.NETWORK in debt.names and not debt.network
            and (within is None or str(debt.key) in within)]
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


# Where a title added to a tracker has its premiere written: the same calendar a
# followed search result is written to (calendar.search's `_endpoint_for`).
PREMIERES = "shows/premieres"


async def write_missing_premieres(settings, record: dict) -> int:
    """Write this season's premiere into the shared calendar for every service
    that has the title and whose calendar does not yet draw it. Returns how many
    airings were written.

    A TITLE ON SOMEBODY'S TRACKER IS A TITLE THIS INSTANCE CARES ABOUT — the same
    standing a clicked search result has — so it earns the same repair. A service
    can list a title in its catalogue and omit it from its own calendar feed:
    MEASURED, Trakt's show record dates Groomed: The Truth Behind the Lie's three
    episodes and none of its /calendars/all/ endpoints lists any of them, so the
    calendar held only Simkl's airing and a reload could never change that.

    THROUGH calendar.search.fill_one_gap, whose contract is what makes a second
    caller sound: it is handed a service and THAT SERVICE'S OWN id and nothing
    else, and asks the service for the title, the date and every other field —
    so this can no more invent a calendar row than a hand-made URL can. A
    service with no premieres calendar of its own (TMDB, today) writes nothing
    there — fill_one_gap's own rule.

    ONLY FOR A TITLE ADDED ONE AT A TIME, BY HAND. The history backfill and the
    month rollover add in bulk, often old seasons, and writing each of their
    premieres into the calendar every viewer shares would be dozens of calendar
    decisions nobody made; the calendar import's airings come from the calendar
    already. A failure writes nothing and costs the add nothing.
    """
    if (record.get("media") or Media.SHOW) != Media.SHOW or record.get("season") is None:
        return 0
    key = record_key(record)
    drawn = await calendar_entries.sources_airing(PREMIERES, str(key), int(record["season"]))
    ids = record.get("ids") or {}
    written = 0
    for source, _port in providers.for_shared_id_lookup(settings):
        name = str(source)
        if name in drawn or ids.get(name) in (None, ""):
            continue
        try:
            wrote = await calendar_search.fill_one_gap(
                settings, source=Source(name), source_id=str(ids[name]), media=Media.SHOW,
                season=int(record["season"]), endpoint_key=PREMIERES, now=db.now(),
                # THIS SEASON'S PREMIERE OR NOTHING: a service that cannot date
                # the season must not write the show's own first-air date into
                # the shared calendar in its place.
                exact_season=True)
        except SourceUnavailable as exc:
            logger.info("tracker arrival: %s premiere for %s not written (%r)", name, key, exc)
            continue
        if wrote:
            logger.info("tracker arrival: wrote %s's premiere of %s S%s into the calendar",
                        name, key, record["season"])
            written += 1
    return written


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


def _fill(held: dict, ids: dict) -> None:
    """Add what `ids` knows that `held` does not — never replacing a value."""
    for name, value in (ids or {}).items():
        if value not in (None, "") and held.get(name) in (None, ""):
            held[name] = value


def _describing_service_states_the_shared_ids(settings, rec: dict, held: dict,
                                               by_source: dict, filed=None) -> None:
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
    filed_under = (filed or record_key(rec)).match_source
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


async def ask_for_missing_ids(user_id: int, settings, *,
                              within: set[str] | None = None) -> int:
    """Ask, by shared id, for every service id this viewer's STORED rows still
    lack — the stored half of what `describe` does for a title arriving. Returns
    how many row values were written.

    WHAT THE STORED CALENDAR CANNOT TEACH. naming.fill_from_calendar learns a
    service's id from that service's own stored record; a title no stored record
    of that service names stays half-linked — MEASURED on the owner's copy, 54
    rows after that pass, Groomed: The Truth Behind the Lie among them, whose
    Trakt id exists but whose Trakt airing no calendar feed carries. Asked through
    the title store (metadata.ensure — once per title a day, the ports' own
    clocks, exact matches only), and written add-only through store.learn_ids,
    every learned id logged.

    A TITLE THE ANSWER RE-KEYED IS WRITTEN UNDER ITS NEW KEY: the re-key moved
    this viewer's rows there in the same transaction it happened in.

    `within`, as for ask_for_missing_networks: the titles a load is drawing.
    """
    owed = [debt for debt in await store.identities_owing(user_id)
            if any(str(source) in debt.names
                   for source, _port in providers.for_shared_id_lookup(settings))
            and (within is None or str(debt.key) in within)]
    if not owed:
        return 0
    found = await metadata.ensure_many(
        [(debt.key.media, dict(debt.ids), str(debt.key)) for debt in owed], settings=settings)
    changed = 0
    for debt, known in zip(owed, found):
        if known is None or known.triple is None:
            continue
        learned = {name: value for name, value in known.ids.items()
                   if debt.ids.get(name) in (None, "")}
        if not learned:
            continue
        wrote = await store.learn_ids(user_id, known.triple, learned)
        if wrote:
            logger.info("tracker arrival: user %s learned %s for %s by asking "
                        "(%d row value(s))", user_id, learned, known.key, wrote)
        changed += wrote
    return changed


def stored_facts(settings, rec: dict, by_source: dict, *,
                 service: str | None = None) -> TitleFacts | None:
    """The describing service's title and network for `rec` from the stored
    calendar (`by_source` is metadata/rows.py's known_about answer for its
    title), or None when disk does not hold exactly what a lookup would answer —
    see `_title_facts`. Also how naming.py repairs a row stored without a network,
    so a repaired row and a newly arrived one carry the same words. `service`
    names the service to read instead, where one answered the season (see
    `describe`)."""
    service = service or live.detail_source(rec, settings)
    if not service:
        return None
    own = (rec.get("ids") or {}).get(service)
    row = by_source.get(service)
    if (row is None or own in (None, "") or str(row.ids.get(service)) != str(own)
            or not row.title or not row.network):
        return None
    return TitleFacts(row.title, row.network)


async def _title_facts(settings, groups: dict, known: dict) -> dict[tuple, TitleFacts]:
    """{(title key, service): that service's title and network}, for `groups`
    keyed the same way — the service being the one that answered those records'
    season, or None for the describing service (see `describe`).

    FROM DISK ONLY WHEN DISK HOLDS EXACTLY THE ANSWER A LOOKUP WOULD GIVE: the
    describing service's own stored record, for the very id the row carries, and
    stating both. Anything less — another of that service's titles (an anime
    season stored under the series' key), a record with no network yet — is
    asked, so a stored answer and a fetched one can never be two different
    things. MEASURED on a live instance, the stored calendar answered most of the
    titles a history backfill had filed blank.
    """
    out: dict[tuple, TitleFacts] = {}
    lookups: list[tuple] = []
    for group, recs in groups.items():
        key, service = group
        stored = stored_facts(settings, recs[0], known.get(key) or {}, service=service)
        if stored is not None:
            out[group] = stored
        else:
            lookups.append(group)
    answers = await asyncio.gather(
        *(live.title_facts(settings, groups[group][0], service=group[1]) for group in lookups),
        return_exceptions=True)
    for group, got in zip(lookups, answers):
        if isinstance(got, BaseException):
            logger.info("tracker arrival: no title facts for %s (%r)", group[0], got)
            continue
        out[group] = got
    logger.info("tracker arrival: title facts for %d title(s), %d from the stored "
                "calendar, %d asked for", len(groups), len(groups) - len(lookups), len(lookups))
    return out
