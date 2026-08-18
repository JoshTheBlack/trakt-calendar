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
    """
    ids: dict
    season: int | None
    network: str


EMPTY = Naming(ids={}, season=None, network="")


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
    )


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
        pool=transport.CATALOG_POOL, raise_errors=True,
    )
    if not isinstance(payload, dict) or not isinstance(payload.get("ids"), dict):
        return EMPTY
    return read(payload)
