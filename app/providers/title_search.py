"""Asking every catalogue this instance has for a title, folding however many of
them answer into one list, and reading a chosen result back out of that answer.

WHY THIS LIVES IN providers/ AND NOT IN A FEATURE. The calendar's search, the
tracker's add and the rankings board all ask the same services the same
question, and the rules for folding their answers together carry measured
findings (anime season-titles colliding on one identity, a source deduped
against itself only where the season is known, whole shows absorbing their
seasons) that a second implementation would get wrong from memory. Every
feature may depend on providers/, which already owns the ports this asks
(SearchPort, DetailPort.fetch_seasons) and the one function that decides who
is asked (`providers.for_search`); a feature owning it made every other
searcher depend on that feature instead. What a result DOES once chosen stays
each feature's own — this module ends at the chosen title.

WHICH SEARCH A PAGE DREW TRAVELS AS A REFERENCE (`SearchRef`): the services the
viewer added to it and how many pages it shows — names and a number, never a
result. `search` asks with it, every result row and widening control sends it
back, and `resolve_hit` re-reads with it, so the same reference always names the
same list and a result is named by its place in it.

TWO THINGS HAPPEN HERE, DELIBERATELY KEPT APART. `search_catalogue` is the I/O
edge: it fans a query out over whichever search ports its caller hands it,
tolerating a source that could not be asked. `merge_search_hits` is the pure
transformation: it takes the raw per-source answers and folds them into one
deduplicated list, with no settings, no network and no provider object in
reach of it. That split is what lets the dedupe rules below be tested with
nothing but hand-built `SearchHit`s — which is the only way to trust them,
given how easy the wrong dedupe unit is to get wrong (see `merge_search_hits`).

`search_catalogue` DOES NOT CALL THE REGISTRY. It takes the list it is handed,
so the merge only ever sees the answer to "who is asked", never the question.
`search` and `resolve_hit` are where the question is asked — the same one, of
the same reference, which is what makes a re-read land on the row the page drew.
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, NamedTuple

from .. import providers
from .base import ItemKey, Media, SearchHit, SearchPage, SearchPort, Source, resolve_key

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

    `media` IS THE LEADER'S, not the caller's requested media, and the two can
    genuinely differ: Simkl's anime search mixes TV and film under one
    endpoint (`entry.get("anime_type")`, read in app/providers/simkl/search.py),
    so a "shows" search can turn up a hit that is really a film — measured,
    "Shingeki no Kyojin: The Last Attack" (a 2024 recap movie) answers a show
    query alongside its parent series' TV seasons. Every hit in one slot
    necessarily agrees on media already, because the dedupe key itself is
    `resolve_key(hit.media, hit.ids)` — a show and a film can never resolve to
    the same key — so taking it from whichever hit calls `start()` is safe
    rather than a guess.

    `title_key` IS WHERE A HIT READ FROM THIS INSTANCE'S STORED CALENDAR CAME
    FROM, and None for a hit a service answered just now. It is the one field
    that differs between the two halves of a search, and it is what a pick
    NAMES each by: a stored hit by that identity, which the server reads the
    stored rows back by (app/calendar/entries.py's `stored_title`); a service
    hit by its query and its place in the answer (`resolve_pick`), because a
    service hit can have no shared identity at all until it is looked up.
    Carried rather than re-derived from `key`, because a stored title the
    identity waterfall could not key still has a stored identity — a per-source
    one — and re-deriving it would be a second statement of that rule.

    `poster` FOLLOWS THE SAME LEADER-THEN-GAPS RULE as the fields above, and is
    the service's own address — see `SearchHit.poster` for why it is proxied only
    where it is drawn. `poster_source` is WHICH service's, because a picture
    this app keeps is filed under the service that published it
    (app/media/artwork.py's registry, read in each viewer's own order) — and a
    title only one service lists has no other way into that registry: the one
    lookup a missing picture gets asks TMDB and Trakt, by tmdb id.
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
    media: Media
    title_key: str | None = None
    poster: str = ""
    poster_source: str = ""


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

    `more_from` names each catalogue (base.SearchCatalogue.key) that holds a
    later page than the last one read — its own word for it (SearchPage.more).
    """
    hits: list[MergedSearchHit]
    failed: frozenset[Source]
    more_from: frozenset[str] = frozenset()

    @property
    def more(self) -> bool:
        """Whether any catalogue asked holds a later page."""
        return bool(self.more_from)


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
    media: Media
    poster: str = ""
    poster_source: str = ""
    source_ids: dict[Source, str] = field(default_factory=dict)

    @classmethod
    def start(cls, hit: SearchHit, key: ItemKey | None) -> "_Merging":
        slot = cls(key=key, season=hit.season, title=hit.title, year=hit.year,
                   network=hit.network, runtime=hit.runtime, overview=hit.overview,
                   ids=dict(hit.ids), media=hit.media, poster=hit.poster,
                   poster_source=str(hit.source) if hit.poster else "")
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
        if _blank(self.poster) and not _blank(hit.poster):
            self.poster, self.poster_source = hit.poster, str(hit.source)
        if self.year is None:
            self.year = hit.year
        if self.runtime is None:
            self.runtime = hit.runtime

    def finish(self, title_key: str | None = None) -> MergedSearchHit:
        return MergedSearchHit(key=self.key, season=self.season,
                               source_ids=dict(self.source_ids), ids=dict(self.ids),
                               title=self.title, year=self.year, network=self.network,
                               runtime=self.runtime, overview=self.overview, media=self.media,
                               title_key=title_key, poster=self.poster,
                               poster_source=self.poster_source)


def fold_one_title(hits: Sequence[SearchHit], key: ItemKey | None, *,
                   title_key: str | None = None) -> MergedSearchHit:
    """Hits ALREADY KNOWN to name one title, at most one per source and in
    registry order, folded into one hit by the same rules a merge folds a slot
    with — the first leads, the rest fill what it left blank, the ids union.

    FOR A CALLER THAT HAS DECIDED IDENTITY ITSELF, which is why it takes `key`
    rather than resolving one: the stored calendar groups its rows by the
    identity it wrote them under (`calendar_titles.title_key`), and asking the
    waterfall again of ids enrichment has since added to could split one stored
    title in two. What this shares with `merge_search_hits` is how a row's
    FIELDS are chosen, so a title reads the same whichever half of a search
    found it.
    """
    slot = _Merging.start(hits[0], key)
    for hit in hits[1:]:
        slot.absorb(hit)
    return slot.finish(title_key)


def merge_search_hits(
        per_source: Sequence[tuple[Source, Sequence[SearchHit]]],
        *, failed: frozenset[Source] = frozenset(),
        more_from: frozenset[str] = frozenset()) -> SearchMergeResult:
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

    `failed` AND `more_from` PASS THROUGH UNCHANGED. Which sources could not be
    asked, and whether any holds a later page, are facts about the fan-out that
    produced `per_source`, not about the fold —
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
                if _titles_differ_materially(slot.title, hit.title):
                    # The season unit is what tells apart titles that share one
                    # match id, and a mapping this thin (Simkl's own `season` /
                    # `mapped_tvdb_seasons`, or simply None on every source
                    # measured so far) is where a wrong collapse would show up
                    # first. This is an operator's signal, not the viewer's —
                    # nothing here refuses the merge over it. It lives HERE,
                    # where the dedupe is decided, and not in the fold every
                    # caller shares: a caller folding rows it already knows are
                    # one title (fold_one_title) has decided nothing to check.
                    logger.warning(
                        "Catalogue search merged %r (%s) and %r (%s) onto one row "
                        "(%s, season %s) — check whether the dedupe unit actually "
                        "matched.", slot.title, next(iter(slot.source_ids)),
                        hit.title, hit.source, slot.key, slot.season)
                slot.absorb(hit)
                continue
            slot = _Merging.start(hit, key)
            slots.append(slot)
            if dedupe_key is not None:
                by_dedupe_key.setdefault(dedupe_key, []).append(slot)

    return SearchMergeResult(hits=[slot.finish() for slot in _whole_shows_absorb_seasons(slots)],
                             failed=failed, more_from=more_from)


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


def _asking(entry) -> tuple[Source, SearchPort, str | None]:
    """An asked entry as (source, port, catalogue key) — a SearchPlan's
    AskedCatalogue, or a bare (source, port) pair asking the port's first
    catalogue, which is how a caller holding no plan names one."""
    if isinstance(entry, providers.AskedCatalogue):
        return entry.source, entry.port, entry.catalogue.key
    source, port = entry
    return source, port, None


async def search_catalogue(asked: Sequence, settings, media: Media, query: str, *,
                           pages: int | Mapping[str, int] = 1,
                           cached_only: bool = False) -> SearchMergeResult:
    """Ask every catalogue in `asked` for its pages 1 to n of `query` — the
    catalogues at once, each one's pages in turn — and merge whatever answers
    come back. `asked` is a SearchPlan's (`search` below); this function does
    not call the registry itself. `pages` is each catalogue's page count by key
    (SearchRef.page_counts), or one count for every catalogue.

    EVERY PAGE UP TO n, NOT ONLY THE LAST, AND MERGED AS ONE ANSWER. A title one
    service lists on page 1 and another on page 2 is one row, and the rows are
    numbered in the list a page actually draws — so "more from <service>"
    redraws the whole list one page longer rather than appending a second list
    beside it. The earlier pages were stored by the requests that drew them, so
    they cost nothing to read again. A catalogue stops at the first page that
    says it has no more.

    EACH CATALOGUE'S ROWS FOLLOW THE ONE BEFORE IT, in the plan's order: the
    default's, then each service added. No service carries a relevance a merge
    could rank across services by (TMDB and Simkl answer in their own order
    with no score), so what a service adds is appended below what was being
    read, never shuffled in above it.

    ONE SOURCE FAILING IS NOT THE SEARCH FAILING, the same bargain the
    calendar's fill envelope makes: `asyncio.gather(..., return_exceptions=True)`
    means a source that raises loses only its own half of the answer, logged
    and recorded in the result's `failed` set, while whoever else answered
    still renders. A search that 502s because a service the viewer was not
    even thinking about is down is exactly the failure this avoids.

    `cached_only=True` MAKES NO CALL AT ALL and re-reads what this exact search
    already stored, merged again by the same rules. It is how an add acts on a
    result the page NAMED: the picker searched moments ago, so the title, the
    ids and the network that get written are the services' own answer rather
    than fields a browser handed back — and a search the instance no longer
    holds answers with nothing, which the caller reads as "search for it again"
    rather than quietly spending a search nobody counted.
    """
    if not asked:
        return SearchMergeResult(hits=[], failed=frozenset())
    entries = [_asking(entry) for entry in asked]

    def count(key: str | None, source: Source) -> int:
        if isinstance(pages, int):
            return pages
        return int(pages.get(key or str(source), 1))

    async def pages_of(source: Source, port: SearchPort, key: str | None) -> SearchPage:
        hits: list[SearchHit] = []
        more = True
        named = {"catalogue": key} if key is not None else {}
        for page in range(1, count(key, source) + 1):
            if not more:
                break
            answer = await port.search_titles(settings, media, query, page=page,
                                              cached_only=cached_only, **named)
            hits.extend(answer.hits)
            more = answer.more
        return SearchPage(hits, more)

    answers = await asyncio.gather(*(pages_of(*entry) for entry in entries),
                                   return_exceptions=True)
    per_source: list[tuple[Source, Sequence[SearchHit]]] = []
    failed: set[Source] = set()
    more_from: set[str] = set()
    for (source, _port, key), answer in zip(entries, answers):
        if isinstance(answer, BaseException):
            logger.warning("Catalogue search failed for %s.", key or source, exc_info=answer)
            failed.add(source)
            continue
        per_source.append((source, answer.hits))
        if answer.more:
            more_from.add(key or str(source))
    return merge_search_hits(per_source, failed=frozenset(failed),
                             more_from=frozenset(more_from))


# How far "more from <service>" may page one catalogue. Far short of TMDB's own
# ceiling (page 500): a list this long is no longer a picker, and a narrower
# query is the better answer than a longer list.
MAX_PAGES = 10


@dataclass(frozen=True)
class SearchRef:
    """WHICH search a page drew, said as references: the catalogues the viewer
    ADDED to the default (`also`, base.SearchCatalogue keys) and how many pages
    each catalogue shows (`pages`, (key, count) for every count past the first).

    A REFERENCE, NEVER DATA, so it may ride in an address: the server resolves
    the keys against what this viewer may add (providers.for_search, which
    refuses any other) and re-reads every page from what the search stored. It is
    what every result row and every widening control sends back, which is how
    an add re-reads EXACTLY the list the row was drawn in.

    BOTH ARE SETS, kept sorted, because the catalogues asked are put in declared
    order by the registry whatever order the viewer added them in.
    """
    also: tuple[str, ...] = ()
    pages: tuple[tuple[str, int], ...] = ()

    @classmethod
    def named(cls, params: Mapping[str, Any]) -> "SearchRef":
        """The reference a request carries — a GET's query or a POST's body,
        which name it the same way: `also=trakt,simkl-anime`,
        `pages=tmdb:2,trakt:3`. A count that is not one is left out (the first
        page); a key is checked where it is resolved, not here."""
        names = {name.strip() for name in str(params.get("also") or "").split(",")
                 if name.strip()}
        counts: dict[str, int] = {}
        for part in str(params.get("pages") or "").split(","):
            key, _, number = part.strip().partition(":")
            try:
                value = int(number)
            except ValueError:
                continue
            if key and value > 1:
                counts[key] = min(value, MAX_PAGES)
        return cls(tuple(sorted(names)), tuple(sorted(counts.items())))

    def page_count(self, key: str) -> int:
        """How many pages catalogue `key` shows."""
        return dict(self.pages).get(key, 1)

    def page_counts(self) -> dict[str, int]:
        return dict(self.pages)

    def fields(self) -> dict[str, str]:
        """The reference as request fields: nothing for the default first page,
        so a plain search's rows carry exactly what they always did."""
        out: dict[str, str] = {}
        if self.also:
            out["also"] = ",".join(self.also)
        if self.pages:
            out["pages"] = ",".join(f"{key}:{count}" for key, count in self.pages)
        return out

    def adding(self, key: str) -> "SearchRef":
        """This search with catalogue `key` added, on its first page; what it
        adds is appended below the rows already there (search_catalogue)."""
        return SearchRef(tuple(sorted({*self.also, key})), self.pages)

    def longer(self, key: str) -> "SearchRef":
        """This search with catalogue `key` one page longer, or as it is at the
        ceiling."""
        counts = {**dict(self.pages), key: min(self.page_count(key) + 1, MAX_PAGES)}
        return SearchRef(self.also, tuple(sorted(counts.items())))


class SearchOutcome(NamedTuple):
    """One search, answered: what the services found, the reference it was
    asked with, and who was and was not asked — everything a box draws below
    its results (`controls`)."""
    result: SearchMergeResult
    ref: SearchRef
    plan: providers.SearchPlan

    def controls(self) -> dict[str, Any]:
        """What `_title_search.html`'s `widen` draws, ONE BUTTON PER CATALOGUE in
        the plan's order: "More from <service>" for a catalogue asked that holds
        a later page (short of the ceiling), and "Also search <service>" for one
        this viewer could add — which, once pressed, is asked and so becomes its
        "More from". Each names the search it would ask by its own reference.
        And the default services this instance has no credential for, which the
        box names rather than replacing."""
        asked = {entry.catalogue.key for entry in self.plan.asked}
        offered = {catalogue.key for catalogue in self.plan.offered}
        buttons = []
        for catalogue in self.plan.order:
            if catalogue.key in asked:
                if (catalogue.key in self.result.more_from
                        and self.ref.page_count(catalogue.key) < MAX_PAGES):
                    buttons.append({"label": f"More from {catalogue.label}",
                                    "fields": self.ref.longer(catalogue.key).fields()})
            elif catalogue.key in offered:
                buttons.append({"label": f"Also search {catalogue.label}",
                                "fields": self.ref.adding(catalogue.key).fields()})
        return {"buttons": buttons, "unconfigured": list(self.plan.unconfigured),
                "asked": [entry.catalogue.key for entry in self.plan.asked]}


async def search(settings, media: Media, query: str, ref: SearchRef = SearchRef(), *,
                 cached_only: bool = False) -> SearchOutcome:
    """THE ONE SEARCH every box makes: the catalogues `ref` names for this viewer
    (providers.for_search, which raises UnknownSearchService for a key the
    viewer may not add), each asked for its own page count and merged.
    `settings` is the viewer's own (auth.viewer.settings_for_user) — whom they
    have linked is what may be added. `cached_only` re-reads the stored answer
    and asks nobody, which is how an add redraws the list it came from."""
    plan = providers.for_search(settings, ref.also)
    result = await search_catalogue(plan.asked, settings, media, query,
                                    pages=ref.page_counts(), cached_only=cached_only)
    return SearchOutcome(result, ref, plan)


class Pick(NamedTuple):
    """A search result somebody chose, resolved the only way it may be resolved:
    from answers a SERVICE gave — the stored search and the season lookup.

    NOTHING HERE CAME OFF THE PAGE. The page names a result; the title, the
    network and every id are the services' own words for it. That closes the
    route through which a caller's ids could reach a write — and it matters
    more than almost anywhere, because a wrong Trakt id SHADOWS a correct Simkl
    one on a tracker roster row for ever, so an id a browser could influence is
    an id that can permanently mis-file somebody's watch record.

    WHETHER THE TITLE CAN BE FILED IS NOT ANSWERED HERE. That is a question
    about the feature doing the filing — the tracker's refusal names the
    tracker — so each caller asks it of `ids` itself; `resolve_key` is the
    shared half of the answer, and it is already public.

    `media` IS THE HIT'S OWN, which a show search cannot always assume — Simkl's
    anime search mixes films in with TV (see app/providers/simkl/search.py's
    _FILM_SEARCH_TYPE), so a result found from a SHOW box can still turn out to
    be a film. A caller reads this to say so rather than filing a film as a
    zero-progress show.
    """
    title: str
    network: str
    year: int | None
    ids: dict
    seasons: list[dict]
    named_season: int | None
    media: Media


async def pick_from_hit(settings, hit: MergedSearchHit, *, cached_only: bool) -> Pick | None:
    """`hit` as a Pick: for a show, its season list and every shared id the
    per-title lookup adds; for a film, the hit alone. None when the source that
    would be asked cannot be.

    `cached_only` IS THE CALLER'S BECAUSE THE TWO KINDS OF CALLER DIFFER. A
    picker may make the per-title lookup, because that lookup is what the click
    is FOR; an add may not SEARCH, because an add that can search is an add that
    spends a budget nobody is counting. The one lookup an add may make is this
    one, for a result the search left with no id to key it on — the rankings
    board's add does, see app/ranker/sources.py's board_item for the measurement.

    THE FIRST SOURCE IN THE HIT'S OWN ORDER THAT THIS INSTANCE CAN DESCRIBE WITH
    IS WHO IS ASKED, and that order is the registry's — whoever found a title is
    who can be asked about it, and a Simkl-only instance has no Trakt entry to
    reach for. For a hit a service answered just now the first source always
    qualifies: it was searchable, and describing asks strictly less of an
    instance than searching does. A hit read from the stored calendar is where
    the difference shows: its rows were written by whichever services filled a
    month, perhaps long before this instance stopped being able to ask one of
    them, and a title another source also holds should still be addable.
    Describing is a public per-title read on every source (DetailPort
    .catalogue_configured), so a viewer who never linked the service that
    leads still gets its season list.
    """
    # THE HIT'S OWN MEDIA DECIDES, NOT THE SEARCH THAT FOUND IT. Simkl's anime
    # search mixes TV and film under one endpoint (see
    # app/providers/simkl/search.py's _FILM_SEARCH_TYPE), so a "shows" search
    # can turn up a hit that is really a film — MergedSearchHit.media already
    # carries that answer, resolved from whichever hit led the merge.
    if hit.media is Media.MOVIE:
        # A film has no season to pick and so no second lookup: the search hit
        # is the whole answer, which is also why an unfileable one is refused on
        # the row it is drawn as rather than on the click.
        return Pick(hit.title, hit.network, hit.year, dict(hit.ids), [], None, hit.media)
    asked = next(((provider, source_id) for source, source_id in hit.source_ids.items()
                  if (provider := providers.get(source)) is not None
                  and provider.detail_port is not None
                  and provider.detail_port.catalogue_configured(settings)), None)
    if asked is None:
        return None
    provider, source_id = asked
    answer = await provider.detail_port.fetch_seasons(settings, source_id, hit.media,
                                                      cached_only=cached_only)
    # THE LOOKUP WINS A CONFLICT and the hit's own ids fill in the rest. This
    # answer's `season` was read off the per-title record `answer.ids` came from,
    # so those two describe the same title by construction; an id that disagrees
    # describes a different one, and keeping it would file the record with one
    # title's season beside another title's id — which is exactly what a source
    # listing one series as several titles produces, and a record nothing can
    # then count. The hit's ids still travel because a lookup that surfaces
    # nothing (Trakt, always — see SeasonsAnswer) must not read as "this hit had
    # no ids after all".
    return Pick(
        title=hit.title,
        year=hit.year,
        # ONLY WHERE THE HIT HAD NONE. The hit's network came from whichever
        # source led the merge; this one is the per-title record's, and it fills
        # a gap rather than overriding a source that already answered — a Simkl
        # search hit carries no network, so on a Simkl-only instance this is the
        # only place a title added by hand ever gets one.
        network=hit.network or answer.network or "",
        ids={**hit.ids, **answer.ids},
        seasons=answer.seasons,
        named_season=answer.named_season,
        media=hit.media,
    )


async def resolve_pick(settings, media: Media, query: str, index,
                       ref: SearchRef = SearchRef(), *, cached_only: bool) -> Pick | None:
    """The result at `index` of the search for `query` that `ref` names, or None
    when nothing stored answers to that name.

    A RESULT IS NAMED BY ITS QUERY AND ITS PLACE IN THE ANSWER, not by an
    identity, and that is not a shortcut. A show hit CAN have no shared id at
    all — that is the ordinary shape of a Simkl season-title before the per-title
    lookup fills one in, and refusing those is exactly what a search-to-add
    exists not to do — so there is no key to name it by until the pick has
    already happened. The merge is a pure function of the stored answers, so the
    same query lands on the same row.

    TWO CACHED READS, AND BOTH OF THEM ARE ANSWERS THE CLICK ALREADY PAID FOR.
    The merged search names the title, the network and the ids the hit carried;
    the per-title season lookup (`pick_from_hit`) names the season list, the
    season the hit IS where its source files seasons as titles, and every shared
    id the search left out. Neither is a new question — the box searched a moment
    ago and the picker looked the title up — so re-asking them is a read of the
    shared response cache and no request at all. The SEARCH is re-read from the
    store whatever `cached_only` says: the box's own request stored it seconds
    earlier, and a result the store no longer holds is one to search for again
    rather than one to guess at.
    """
    hit = await resolve_hit(settings, media, query, index, ref)
    if hit is None:
        return None
    return await pick_from_hit(settings, hit, cached_only=cached_only)


async def resolve_hit(settings, media: Media, query: str, index,
                      ref: SearchRef = SearchRef()) -> MergedSearchHit | None:
    """The merged hit at `index` of the stored search for `query` that `ref`
    names, or None when nothing stored answers to that name — the first half of
    `resolve_pick`, for a caller that needs the hit itself rather than its
    season list.

    CACHE ONLY, ALWAYS: the search is re-read from where the caller's own
    earlier request stored it, merged again by the same pure rules, so the same
    query lands on the same row and nothing is asked. THE SAME REFERENCE ASKS
    THE SAME SERVICES — `search` puts `ref` to providers.for_search exactly as the
    box's own request did — and the same pages, so the same place names the same
    title. `settings` must be the viewer's that search was made with, for the
    same reason. A reference naming a service this viewer may no longer add
    (they unlinked it since) names nothing, and is answered as an expired
    search.
    """
    try:
        outcome = await search(settings, media, query, ref, cached_only=True)
    except providers.UnknownSearchService:
        return None
    result = outcome.result
    try:
        position = int(index)
    except (TypeError, ValueError):
        return None
    # A negative index would read from the end of the list rather than name a
    # row, so it is not a place in the answer.
    if not 0 <= position < len(result.hits):
        return None
    return result.hits[position]
