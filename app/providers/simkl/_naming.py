"""What Simkl's own per-title record says a title IS: the shared ids it knows it
by, the season of a larger show it stands for, and the network that carried it.

WHY THIS EXISTS AS ITS OWN MODULE — SIMKL MODELS EACH ANIME SEASON AS ITS OWN
CATALOGUE TITLE. "Shingeki no Kyojin Season 3" (simkl 694485) is a different
title from "Shingeki no Kyojin" (simkl 39687), and it numbers its OWN episodes
from season 1 while carrying the PARENT series' tmdb/tvdb/imdb — measured
2026-08-18, all three titles of that series resolve to `show:tmdb:1429`. So
"which season is this" has two answers for one title, and BOTH halves of this
package need the translation between them:

  detail.py, so a season list asked for the parent's season 3 reads the
  season-title's own season 1 instead of finding nothing;
  sync.py, so a viewer's plays on such a title are filed under the season the
  tracker knows rather than under Simkl's local numbering.

Two call sites, one rule, so the rule is here rather than in either of them.
`GET /tv/{id}?extended=full` is the one call that answers all of it.

DELIBERATELY NOT `titles.fetch_title`, even though both read the same endpoint.
That function's extraction is a versioned STORAGE shape the calendar enrichment
drain owns (see its own EXTRACT_VERSION), and none of these fields is in it —
adding them there would bump that version and re-fetch every stored row in
`simkl_titles` for facts only these two lookups need.

Package-internal: the underscore names the MODULE, not any name inside it — the
same shape as `_ids` beside it, and CLAUDE.md's stated convention for this.
"""
from __future__ import annotations

from typing import NamedTuple, Sequence

from ...config import Settings
from . import _ids, transport


class Naming(NamedTuple):
    """One title's per-title record, reduced to what this package asks of it.

    `ids` is collect_ids()-filtered — every shared id Simkl knows the title by,
    which for a season-title is the PARENT series' id map and is the whole
    reason a search hit left bare can be resolved on the click that picks it.

    `season` is the season of that larger show this title IS, or None. None
    covers three cases that all want the same answer: the record names no
    season, the record maps to SEVERAL seasons of the show (a long-running
    series filed as one title — One Piece maps to seasons 1-23), or the payload
    was unreadable. AN AMBIGUOUS MAPPING IS NOT GUESSED AT: a title that maps
    onto many seasons is already numbering its episodes the way the show does,
    so there is nothing to translate, and picking one of its seasons would
    invent a fact.

    `network` is "" where Simkl does not say. It is here because it rides the
    same record and the add flow has nowhere else to get it: a Simkl SEARCH hit
    carries no network at all, so on a Simkl-only instance a show added by hand
    reached the roster with an empty one and drew no emoji.

    `siblings` is the other Simkl titles of the same series, in the order the
    record lists them — the `relations` block, filtered to the kinds that can
    be a SEASON. It is what makes "which title holds season 3" answerable from
    any title of the series; see `title_for_season`.
    """
    ids: dict
    season: int | None
    network: str
    siblings: tuple[int, ...] = ()


EMPTY = Naming(ids={}, season=None, network="", siblings=())

# Which `relations` entries could be another SEASON of the same series, by
# Simkl's own `anime_type`. Measured 2026-08-18 against Attack on Titan, whose
# twelve relations include four films (`summary`), an OVA (`side story`) and an
# `alternative setting` spin-off alongside the real sequels: side material is
# never a season the tracker files episodes under, and reading each one's record
# to discover that would be a request spent to reject it.
_SEASON_KINDS = frozenset({"tv", "ona", "special"})

# A title's own ids, the season it maps to and the network that carried it are
# about as static as catalogue data gets — a mapping changes when somebody
# corrects it, which is rarer than an episode gaining an air date. So this is
# held for a day rather than for the app's default response TTL, which is
# measured in minutes and would make a repeated search pay the same lookups
# over again. The same number as detail.py's episode-list TTL, arrived at
# separately: these two records go stale for different reasons and each states
# its own.
CACHE_TTL_SECONDS = 24 * 60 * 60


def read(record: dict | None) -> Naming:
    """One raw `GET /tv/{id}?extended=full` payload as a `Naming`.

    `mapped_tvdb_seasons` LEADS AND `season` IS THE FALLBACK ONLY WHEN THE
    FIRST IS ABSENT — not consulted when it is present but names more than one
    season. A season-title states itself through `mapped_tvdb_seasons`; when
    that list holds anything other than exactly one entry, falling back to
    `season` (Simkl's own, un-mapped numbering) would answer a question the
    mapping deliberately did not.
    """
    if not isinstance(record, dict):
        return EMPTY
    return Naming(
        ids=_ids.normalize(record.get("ids") or {}),
        season=_season(record),
        network=str(record.get("network") or "").strip(),
        siblings=_siblings(record),
    )


def _siblings(record: dict) -> tuple[int, ...]:
    """The other Simkl titles of this series, season-capable ones only.

    THE LIST IS TRANSITIVE, NOT JUST THE NEIGHBOURS. Measured across three
    series and eleven titles, every member's `relations` reached every other
    member — the block carries `is_direct: false` entries precisely so one
    record can name the whole family, which is what makes a single lookup
    enough to start from any title and find any season.
    """
    out = []
    for relation in record.get("relations") or []:
        if not isinstance(relation, dict):
            continue
        if str(relation.get("anime_type") or "") not in _SEASON_KINDS:
            continue
        simkl_id = (relation.get("ids") or {}).get("simkl")
        if simkl_id is not None:
            out.append(int(simkl_id))
    return tuple(out)


def _season(record: dict) -> int | None:
    mapped = record.get("mapped_tvdb_seasons")
    if mapped is not None:
        if isinstance(mapped, list) and len(mapped) == 1 and isinstance(mapped[0], (int, float)):
            return int(mapped[0])
        return None
    season = record.get("season")
    return int(season) if isinstance(season, (int, float)) else None


def translation(naming: Naming, own_seasons: Sequence[int]) -> tuple[int, int] | None:
    """(the season number this title uses for its own episodes, the season it
    really is), or None when nothing needs translating.

    THE TITLE HAS TO USE EXACTLY ONE SEASON NUMBER OF ITS OWN for this to mean
    anything. A season-title's whole episode list is one season — measured, every
    one of them numbers it 1 — so a title spanning several is one whose numbering
    already IS the show's, and translating it would move episodes that were
    already in the right place.

    None is also the answer when the record names no season, when it names the
    season the title already uses, and when it is ambiguous. All four are "leave
    this alone", which is what makes the caller's fallback the same in every case
    the data cannot speak to.
    """
    if naming.season is None or len(own_seasons) != 1:
        return None
    local = int(own_seasons[0])
    return None if local == naming.season else (local, naming.season)


async def fetch(settings: Settings, simkl_id) -> Naming:
    """`GET /tv/{id}?extended=full`, read as a `Naming`.

    Returns EMPTY for a title with no id and for one Simkl could not answer for
    — the same "not found or unparseable, indistinguishable" reading
    `titles.fetch_title`'s own module docstring gives, for the reason stated
    there. RAISES the transport's own SimklError for a genuine failure (a
    rejected credential, an unreadable body), because a caller resolving a bare
    search hit needs that to fail honestly rather than read as "this title has
    no ids after all".
    """
    if not simkl_id:
        return EMPTY
    payload = await transport.cached_get(
        transport.catalog_client(), settings, f"tv/{simkl_id}", {"extended": "full"},
        pool=transport.CATALOG_POOL, ttl_seconds=CACHE_TTL_SECONDS, raise_errors=True,
    )
    if not isinstance(payload, dict) or not isinstance(payload.get("ids"), dict):
        return EMPTY
    return read(payload)


async def series_root(settings: Settings, simkl_id, start: Naming) -> int | None:
    """The title that is season 1 of `start`'s series, or None when `start` is
    already it or belongs to no series.

    WHERE SIMKL STATES A SERIES' FACTS ONCE. Some of a title's record is about
    the SEASON (its own overview, its own trailers, its air dates) and some is
    about the SHOW — and the show-level half is filled in on the season-1 title
    and left null on the rest. Measured 2026-08-18: `network` and `country` are
    populated on Beastars simkl 1034467 and null on all three of its sequels,
    with Attack on Titan and Frieren identical. So "ask the series" has one
    address, and this is it.
    """
    if start.season == 1 or not start.siblings:
        return None
    return await title_for_season(settings, simkl_id, 1)


async def network_of_series(settings: Settings, simkl_id, start: Naming) -> str:
    """`start.network`, or the network of the first title of its series that
    names one — "" when none does.

    SIMKL POPULATES THIS ONCE PER SERIES, ON THE ROOT. Measured 2026-08-18
    across three series: `network` (and `country` with it) is filled in on the
    season-1 title and is null on every later season-title — Beastars answers
    "Fuji TV" for simkl 1034467 and null for all three of its sequels, and
    Attack on Titan and Frieren behave identically. That is a gap in Simkl's
    own record rather than a statement that a later season aired nowhere, so
    reading it off the series is the honest answer and an empty string is not.

    WHAT THIS IS AND IS NOT SAYING: it is the SERIES' network, which is what
    the tracker's field means — the roster groups and draws an emoji per
    service, a show-level fact. It is not a claim that this particular season
    was distributed there, and for a season Simkl reclassifies (Beastars' third
    is an `ona` where its first was `tv`) the two can genuinely differ. An
    empty network drew no emoji at all and registered "" in the viewer's map,
    which is worse than the series' answer.
    """
    if start.network:
        return start.network
    ours = str(start.ids.get("tmdb") or "")
    for sibling_id in start.siblings:
        sibling = await fetch(settings, sibling_id)
        if not sibling.network:
            continue
        # The same cross-identity guard `title_for_season` needs: `relations`
        # reaches titles that are their own tracker row, and their network is
        # not this row's to borrow.
        if ours and str(sibling.ids.get("tmdb") or "") != ours:
            continue
        return sibling.network
    return ""


async def title_for_season(settings: Settings, simkl_id, season: int) -> int | None:
    """Which Simkl title holds `season` of the series `simkl_id` belongs to, or
    None when no title of it does.

    THE PROBLEM THIS SOLVES, STATED ONCE. A tracker record is keyed by the
    identity both services share — `show:tmdb:1429` season 3 — while Simkl
    files that season as its OWN title (simkl 694485) which numbers its episodes
    from 1. So the Simkl id a record happens to carry need not be the title that
    holds the season the record names: an add made from a merged search row
    stores whichever id led the row, and a row that offers a season picker can
    file any season under it. Asking that id for the season directly answers
    nothing, and the row reads as 0 episodes for ever.
    RESOLVING IT RATHER THAN PREVENTING IT is what makes this general. Guarding
    the one path that stores a mismatched id would fix rows made after the guard
    and leave every row made before it, and every path that has not been thought
    of, still wrong. Answering the question properly fixes all of them.

    ONE HOP AT MOST, FROM ANY MEMBER OF THE SERIES. `relations` is transitive
    (see `_siblings`), so the starting title names every other, and the search
    stops at the first sibling that both NAMES the season and shares the
    starting title's tmdb id.

    THE TMDB CHECK IS NOT BELT-AND-BRACES. `relations` crosses tracker
    identities: Attack on Titan's "The Final Season" (simkl 1120029) is tmdb
    313599, a different row entirely, and it names a season number of its own.
    Without the check, asking `show:tmdb:1429` for a season it does not have
    would answer with another show's episodes.

    Costs one cached lookup when the starting title already IS the season asked
    for, and one per candidate sibling otherwise — bounded by the family's size
    and held for a day. Raises the transport's SimklError, like `fetch`.
    """
    if not simkl_id:
        return None
    start = await fetch(settings, simkl_id)
    if start.season == season:
        return simkl_id
    ours = str(start.ids.get("tmdb") or "")
    for sibling_id in start.siblings:
        if str(sibling_id) == str(simkl_id):
            continue
        sibling = await fetch(settings, sibling_id)
        if sibling.season != season:
            continue
        if ours and str(sibling.ids.get("tmdb") or "") != ours:
            continue
        return sibling_id
    return None
