"""The tracker's manual add flow asking every catalogue this instance has, and
folding however many of them answer into one list.

TWO THINGS HAPPEN HERE, DELIBERATELY KEPT APART. `search_catalogue` is the I/O
edge: it fans a query out over whichever search ports its caller hands it,
tolerating a source that could not be asked. `merge_search_hits` is the pure
transformation: it takes the raw per-source answers and folds them into one
deduplicated list, with no settings, no network and no provider object in
reach of it. That split is what lets the dedupe rules below be tested with
nothing but hand-built `SearchHit`s — which is the only way to trust them,
given how easy the wrong dedupe unit is to get wrong (see `merge_search_hits`).

THIS MODULE DOES NOT CALL THE REGISTRY. `search_catalogue` takes the list
`providers.for_catalogue_search(settings)` already returns rather than asking
for it itself, so a caller owns the one place that decides which sources are
reachable and this module only ever sees the answer to that question, never
the question itself.
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, NamedTuple

from ..providers.base import ItemKey, Media, SearchHit, SearchPort, Source, resolve_key

logger = logging.getLogger(__name__)


class MergedSearchHit(NamedTuple):
    """One title, after every source's answer for it has been folded together.

    `key` IS None FOR A HIT NO SOURCE COULD NAME IN A SHARED ID SPACE — see
    `merge_search_hits` for why such a hit is carried rather than dropped. It
    is resolved on click, not here; this type only has to say that resolving
    it is still open.

    `source_ids` IS BOTH THE SOURCE MARK LIST AND THE CALLBACK ADDRESS. Its key
    set is exactly the sources that contributed to this row — whether to DRAW
    a mark for it is a rendering decision (`_source_logo.html`'s rule: only
    when there is something to disambiguate), so a row built from one source
    still carries a one-entry map rather than something that must be
    special-cased. Its values are that source's own id for the title, which is
    what a pick calls the per-title lookup with — `next(iter(source_ids))` is
    the registry-order leader, the one whose season list answers for a title
    two sources both found (see `SearchHit`'s own docstring for why the id
    travels per-source rather than being re-derived from `ids`).

    `ids` IS THE UNION OF EVERY CONTRIBUTING SOURCE'S IDS. A hit Trakt named by
    tmdb and Simkl named by mal comes out carrying both — the reason a merge is
    worth doing at all beyond deduplication, see `merge_search_hits`.

    `title`, `year`, `network`, `runtime` AND `overview` COME FROM THE LEADER,
    WITH GAPS FILLED FROM WHOEVER ELSE ANSWERED. Simkl's search hit carries no
    network, runtime or overview (measured — see `SearchHit`'s own docstring);
    a merge with Trakt is what fills them in, on a Simkl-only instance they
    simply stay at the empty default `SearchHit` already uses.
    """
    key: ItemKey | None
    season: int | None
    source_ids: dict[Source, str]
    ids: dict[str, Any]
    title: str
    year: int | None
    network: str
    runtime: int | None
    overview: str


class SearchMergeResult(NamedTuple):
    """A merge's whole answer: what was found, and who could not be asked.

    `failed` BEING NON-EMPTY IS NOT `hits` BEING WRONG. A route reading this
    can still render whatever `hits` holds — that is the entire point of
    gathering with `return_exceptions=True` rather than letting one source's
    failure fail the search. What `failed` is for is telling "nothing matched"
    apart from "something could not be asked": the first is `failed == frozenset()`
    with `hits == []`, the second is `failed` covering every source that was
    asked, and a caller that only looked at `hits` would render the two
    identically as an honest-looking empty results list.
    """
    hits: list[MergedSearchHit]
    failed: frozenset[Source]


def _blank(value: Any) -> bool:
    """Whether a `SearchHit` scalar field is the empty default rather than a
    real answer — the same reading `Record.enriched` exists for on the
    calendar side, restated here because a search hit's optional fields use
    plain "" / None defaults rather than a flag of their own."""
    return value is None or value == ""


def _titles_differ_materially(a: str, b: str) -> bool:
    """A cheap, operator-facing signal that a dedupe may have collapsed two
    different titles onto one key — not a fuzzy-matching system, because
    logging every capitalization difference would bury the case that matters.
    Exact after stripping and case-folding is enough to catch "the titles are
    not even the same string", which is what a bad collapse looks like."""
    return a.strip().casefold() != b.strip().casefold()


@dataclass
class _Merging:
    """One dedupe slot, accumulating hits until `finish` freezes it into a
    `MergedSearchHit`. Mutable where the NamedTuple it produces is not, because
    folding N hits into one field set needs somewhere to fold them.
    """
    key: ItemKey | None
    season: int | None
    title: str
    year: int | None
    network: str
    runtime: int | None
    overview: str
    ids: dict[str, Any]
    source_ids: dict[Source, str] = field(default_factory=dict)

    @classmethod
    def start(cls, hit: SearchHit, key: ItemKey | None) -> "_Merging":
        slot = cls(key=key, season=hit.season, title=hit.title, year=hit.year,
                   network=hit.network, runtime=hit.runtime, overview=hit.overview,
                   ids=dict(hit.ids))
        slot.source_ids[hit.source] = hit.source_id
        return slot

    def absorb(self, hit: SearchHit) -> None:
        """Fold another hit for the same (key, season) into this slot —
        registry order picks the leader (whichever hit called `start`), and
        this only ever fills what the leader left blank or adds what neither
        had. EVERY field below upholds that, `source_ids` included.

        THE HIT IS ALWAYS A SOURCE THIS SLOT DOES NOT YET HOLD —
        `merge_search_hits` opens a new slot rather than calling this when a
        source is already present, for the reason written there. `source_ids`
        still refuses to overwrite rather than trusting that: it is the one
        field whose value a pick sends back to a service, so a slot addressing
        a different title from the one it draws is a wrong ADD rather than a
        wrong-looking row, and the guard belongs beside the field it protects.
        """
        if _titles_differ_materially(self.title, hit.title):
            # See risk-3-shaped reasoning in the module docstring: the season
            # unit is what tells apart titles that share one match id, and a
            # mapping this thin (Simkl's own `season`/`mapped_tvdb_seasons`, or
            # simply None on every source measured so far) is where that could
            # go wrong first. This is an operator's signal, not the viewer's —
            # nothing here refuses the merge over it.
            logger.warning(
                "Catalogue search merged %r (%s) and %r (%s) onto one row (%s, "
                "season %s) — check whether the dedupe unit actually matched.",
                self.title, next(iter(self.source_ids)), hit.title, hit.source,
                self.key, self.season)
        # THE LEADER'S ID SURVIVES, exactly as its title and its ids do — see
        # the docstring for why this one is guarded rather than assigned.
        # Observed before it was: a "frieren" search drew "Sousou no Frieren
        # (2023)" carrying the season-1 title's ids while addressing the
        # season-3 title, so clicking it filed season 3 under the season-1 id
        # and the record could never be counted.
        self.source_ids.setdefault(hit.source, hit.source_id)
        for id_key, id_value in hit.ids.items():
            self.ids.setdefault(id_key, id_value)
        if _blank(self.network):
            self.network = hit.network
        if _blank(self.overview):
            self.overview = hit.overview
        if self.year is None:
            self.year = hit.year
        if self.runtime is None:
            self.runtime = hit.runtime

    def finish(self) -> MergedSearchHit:
        return MergedSearchHit(key=self.key, season=self.season,
                                source_ids=dict(self.source_ids), ids=dict(self.ids),
                                title=self.title, year=self.year, network=self.network,
                                runtime=self.runtime, overview=self.overview)


def merge_search_hits(
        per_source: Sequence[tuple[Source, Sequence[SearchHit]]],
        *, failed: frozenset[Source] = frozenset()) -> SearchMergeResult:
    """Fold every source's hits into one deduplicated list. Pure — no settings,
    no network, no provider object — so the dedupe rules below can be trusted
    on nothing but hand-built `SearchHit`s.

    THE DEDUPE UNIT IS (ItemKey, season), NOT ItemKey ALONE. `resolve_key` is
    the app's one statement of "the same title", but the key alone is not the
    row: Simkl follows the anime-database convention of giving each season its
    own title, so Attack on Titan's S2 and S3 both resolve to the identical
    `show:tmdb:1429` while naming two different things a viewer searched for.
    Deduping on the key alone would silently collapse them into one row. A hit
    with no season of its own (every hit measured from either source today)
    dedupes on `(key, None)` — the whole show — which is also exactly the unit
    `app/distrakt/store.py`'s single-record verbs file rows under, so this is
    not a rule invented for search.

    AND A SOURCE IS DEDUPED AGAINST ITSELF ONLY WHERE THE SEASON IS KNOWN.
    Two hits from one source that BOTH name a season are two descriptions of
    one (key, season) — the same unit the tracker files a record under — so
    merging them is right, and leaving them apart would offer two rows that add
    the identical record. Two that do NOT name one cannot be told apart by
    anything this function is willing to call an identity, and collapsing them
    is how results get lost: Simkl returns three titles for "frieren", all
    resolving to `show:tmdb:209867` and none carrying a season, and under a
    single slot per (key, None) two of the three vanished. Measured 2026-08-18,
    Beastars season 2 (simkl 1231401) could not be surfaced by ANY query,
    because whichever title Simkl happened to rank first for a given query
    string absorbed the rest — so which results existed depended on how the
    query was spelled.
    SO A SLOT WITH NO SEASON HOLDS AT MOST ONE HIT PER SOURCE, and a second
    opens a new slot beside it. A title two SERVICES both returned merges
    either way, which is the whole point of merging at all.
    THE SEASON COMES FROM THE SOURCE, not from here — a source that lists one
    series as several titles is the only thing that can say which season each
    of them is, and app/providers/simkl/search.py fills it in for exactly the
    hits where it settles something. This function does not care how; it only
    distinguishes "named" from "not named".

    A HIT THAT RESOLVES TO NO KEY AT ALL IS NOT DEDUPED AGAINST ANYTHING. It
    cannot be told apart from a second keyless hit of a different title by
    anything this function is willing to call an identity, so it stands alone
    in the output — under-described, not unkeyable; a caller resolves it with
    a per-title lookup on click, using the one `(source, source_id)` pair such
    a hit carries.

    MERGING TWO HITS OF ONE TITLE prefers whichever hit is first in
    `per_source` for every scalar field, and fills a field the leader left
    blank from whoever answered next — see `_Merging.absorb`. THE ID MAPS ARE
    UNIONED UNCONDITIONALLY, leader or not: a hit Trakt named by tmdb and Simkl
    named by mal comes out carrying both, which is a better record than either
    source returned alone.

    `failed` PASSES THROUGH UNCHANGED. Which sources could not be asked is a
    fact about the fan-out that produced `per_source`, not about the fold —
    threading it through rather than recomputing it is what keeps this
    function honest about being pure.
    """
    slots: list[_Merging] = []
    # Several slots may share one (key, None): one per hit a single source
    # returned for a title it did not name a season for. They are kept in
    # arrival order so a later source's hit joins the earliest slot still
    # willing to take it, which keeps registry order deciding who leads a row
    # two services both found.
    by_dedupe_key: dict[tuple[ItemKey, int | None], list[_Merging]] = {}

    for source, hits in per_source:
        for hit in hits:
            key = resolve_key(hit.media, hit.ids)
            dedupe_key = (key, hit.season) if key is not None else None
            open_slots = by_dedupe_key.get(dedupe_key, ()) if dedupe_key is not None else ()
            # A named season makes any slot under this dedupe key the same
            # (key, season) as the hit, whoever is already in it; an unnamed
            # one only matches a slot this source is not already in.
            slot = next((s for s in open_slots
                         if hit.season is not None or hit.source not in s.source_ids), None)
            if slot is not None:
                slot.absorb(hit)
                continue
            slot = _Merging.start(hit, key)
            slots.append(slot)
            if dedupe_key is not None:
                by_dedupe_key.setdefault(dedupe_key, []).append(slot)

    return SearchMergeResult(hits=[slot.finish() for slot in _whole_shows_absorb_seasons(slots)],
                             failed=failed)


def _whole_shows_absorb_seasons(slots: list[_Merging]) -> list[_Merging]:
    """`slots` with a series' per-season rows folded into the row that is the
    WHOLE SHOW, where another source supplied one.

    WHY A SEASON-LESS ROW WINS. A source that models a series as one show can
    answer for EVERY season of it — clicking it opens a picker — so its row does
    everything the per-season rows do and more. Leaving both shapes standing
    offers "Beastars" beside "Beastars Season 2" beside "Beastars Final Season"
    as if they were alternatives, when one of them reaches all three.

    ONLY WHEN IT BRINGS A SOURCE THE SEASON ROW DOES NOT HAVE, which is what
    tells the two meanings of a missing season apart. Trakt returning no season
    means "this is the show, ask me for any of them". Simkl returning no season
    can mean that too — or that a per-title lookup failed and nobody knows what
    this title is (see app/providers/simkl/search.py). A row absorbing another
    row from its own source alone would be that second case swallowing real
    results, which is the collapse the dedupe rules above exist to prevent.

    THE ABSORBED IDS TRAVEL. The season rows' ids and marks merge in, so a
    viewer still sees that both services hold the title and the record still
    carries both ids — and a Simkl id that names a DIFFERENT season from the one
    finally added is harmless, because `app/providers/simkl/_naming.py`'s
    `title_for_season` resolves the season to whichever title holds it rather
    than trusting the id to be the right one.
    """
    whole: dict[ItemKey, _Merging] = {}
    for slot in slots:
        if slot.key is not None and slot.season is None:
            whole.setdefault(slot.key, slot)
    kept = []
    for slot in slots:
        host = whole.get(slot.key) if slot.key is not None else None
        if (host is None or host is slot or slot.season is None
                or not (set(host.source_ids) - set(slot.source_ids))):
            kept.append(slot)
            continue
        for source, source_id in slot.source_ids.items():
            host.source_ids.setdefault(source, source_id)
        for id_key, id_value in slot.ids.items():
            host.ids.setdefault(id_key, id_value)
    return kept


async def search_catalogue(asked: Sequence[tuple[Source, SearchPort]], settings,
                            media: Media, query: str) -> SearchMergeResult:
    """Ask every `(Source, SearchPort)` in `asked` for `query` at once, and
    merge whatever answers come back. `asked` is
    `providers.for_catalogue_search(settings)`'s own return — this function
    does not call the registry itself, so a caller owns the one decision about
    which sources are reachable and this only ever sees the answer.

    ONE SOURCE FAILING IS NOT THE SEARCH FAILING, the same bargain the
    calendar's fill envelope makes: `asyncio.gather(..., return_exceptions=True)`
    means a source that raises loses only its own half of the answer, logged
    and recorded in the result's `failed` set, while whoever else answered
    still renders. A search that 502s because a service the viewer was not
    even thinking about is down is exactly the failure this avoids.
    """
    if not asked:
        return SearchMergeResult(hits=[], failed=frozenset())
    answers = await asyncio.gather(
        *(port.search_titles(settings, media, query) for _source, port in asked),
        return_exceptions=True)
    per_source: list[tuple[Source, Sequence[SearchHit]]] = []
    failed: set[Source] = set()
    for (source, _port), answer in zip(asked, answers):
        if isinstance(answer, BaseException):
            logger.warning("Catalogue search failed for %s.", source, exc_info=answer)
            failed.add(source)
            continue
        per_source.append((source, answer))
    return merge_search_hits(per_source, failed=frozenset(failed))
