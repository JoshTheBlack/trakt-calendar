"""Which source's values one viewer sees for a title two sources may describe.

THE SECOND HALF OF A SPLIT THE CACHE FORCED. Two operations sit between "what a
source said" and "what a person sees", and they have different scopes, which is
why they happen in different places:

  MATCHING    is this record and that record the same card? User-independent:
              one episode is one card whichever services list it
              (app/calendar/cache.py's `match_keys` and `join_listings`, run at
              read over the stored rows).
  RESOLUTION  which source's value does THIS VIEWER see for this field — and so
              on which DAY is the card drawn? Per account by definition, so it
              runs at READ, over already-cached data, and changing a preference
              therefore invalidates nothing.

THAT SECOND SENTENCE IS THE DESIGN'S CENTRAL CLAIM AND IT CONSTRAINS THIS FILE.
Nothing here may be moved to the fill path and nothing here may be written back
into a window: a stored resolved value would be one account's answer sitting in a
row served to everybody, and the next preference change would have to invalidate
it. If a change to this module starts to need the stored payload's version
bumped, the change is on the wrong side of the split.

`resolve` returns a RECORD, not a rendered Item, and that is deliberate: the
per-viewer genre/country/certification filter matches on the raw genre slugs, so
it has to run between resolution and rendering. Rendering is
`app/providers/base.py`'s `render`, the one place a stored instant becomes a
viewer's local day.

RESOLUTION IS ALSO EXCLUSION, and both halves are here for the same reason. The
window a group came out of was filled by asking every source the instance admits
and is served to every viewer from one row, so "I only want this service" cannot
be honoured while filling it without deciding for everybody else too. It is
honoured here instead: a group naming no source this viewer admits resolves to
nothing and never reaches their page, while the same row still gives the next
viewer everything they asked for.

TWO EXCLUSIONS MEET HERE AND THEY ARE DIFFERENT IN KIND. The one above is a
VIEWER'S, and it may only ever be applied here. The other is the OPERATOR'S —
whether a service contributes to this instance's calendar at all
(app/providers/__init__.py's `calendar_sources`, given a Settings) — and it is
applied at the fill too, deliberately, because it decides for every viewer at
once exactly as the instance-wide content floor does. It is applied HERE as well
so that turning it off takes effect on the windows already sitting in the cache,
instead of the operator watching a service they have just switched off go on
rendering until every window's TTL runs out.
"""
from __future__ import annotations

import copy
from collections.abc import Sequence
from datetime import date, datetime, timedelta, timezone
from typing import Any, NamedTuple

from .. import providers
from ..providers import detail_source
from ..providers.base import Record, Source, epoch_moment

# ---------------------------------------------------------------------------
# the vocabulary a preference is written in
# ---------------------------------------------------------------------------
# THE NAMES A PER-FIELD PRECEDENCE ENTRY MAY USE. This is the app's answer to
# "what can a viewer state a preference about", and it lives here rather than in
# app/sources/prefs.py because every one of them is a `Record` field or a named
# group of them — a second copy beside the storage would be a second thing to
# keep in step with the record.

# One value each, one source each, and the ordinary case: the winner is the first
# admitted source in this viewer's order that actually filled the field in.
SCALAR_FIELDS = (
    "title", "overview", "poster", "rating", "imdb_rating", "network", "country",
    "language", "runtime", "status", "certification", "year", "episode_title",
)

# WHICH SERVICE THE CARD IS ATTRIBUTED TO, and it carries the three fields that
# are meaningless apart from it: `id` and `detail_url` are that service's own
# spellings, and `anime_type` is that service's own answer about its own listing.
# Taking `detail_url` from one source and `source` from another would render a
# badge for one service linking out to the other.
#
# `anime_type` TRAVELS WITH IT RATHER THAN BEING RESOLVED, and the consequence is
# deliberate: app/calendar/filter.py's `prune_disguised_films` drops a series-
# endpoint record that one source's own catalogue calls a film. On a MERGED
# group, the other source listed the same title on a series endpoint, so letting
# one service's "this is a film" reach the resolved record would delete a card
# the other service stands behind. It is a statement about one listing, not a
# fact about the title.
SOURCE_FIELD = "source"
SOURCE_CARRIES = ("id", "detail_url", "anime_type")

# THE EPISODE COORDINATE, RESOLVED AS ONE THING. `season`, `episode_number` and
# the `episode_label` derived from them are a single answer, and the reason they
# cannot be three fields is measured rather than tidy: a merged group really does
# hold a record stating (season 1, episode 1) beside one stating (no season,
# episode 1) — the same airing, said at two resolutions. Resolving them
# separately would pair one source's season with another's episode number; taking
# the label separately would print a label from one coordinate beside another.
COORDINATE_FIELD = "coordinate"
COORDINATE_FIELDS = ("season", "episode_number", "episode_label")

# WHEN IT AIRS — `air_ts` and the flag saying whether that instant is really an
# instant, resolved as one by `_resolve_airing`. One unit because `date_only`
# changes how `air_ts` is read: a timestamp from one source under the other's
# flag renders a film a day early for half the world (see Record.date_only).
AIRING_FIELD = "airing"

# The union field. Never a contest: two services listing different genres for one
# title have both told the truth, and picking a winner would throw away a genre a
# viewer filters on.
GENRES_FIELD = "genres"

# Everything a stored preference may name. `ids` is deliberately absent — the
# group's id union is the MATCH RESULT and belongs to nobody, so there is nothing
# to prefer.
FIELDS = (*SCALAR_FIELDS, GENRES_FIELD, COORDINATE_FIELD, AIRING_FIELD, SOURCE_FIELD)


# Each service's place in the declared order — the order every record a
# resolution compares is put in (`resolve_records`).
_DECLARED = {str(source): rank for rank, source in enumerate(Source)}


def source_order(group) -> list[str]:
    """The sources in `group`, in DECLARED order.

    Declared rather than stored order because the payload is JSON and a dict
    that came back from it holds whatever order it was written in — which would
    make "the first source" depend on which service answered first during some
    fill weeks ago.
    """
    by_source = group.get("by_source") or {}
    return [str(s) for s in Source if str(s) in by_source]


def instance_sources(settings=None) -> frozenset[str] | None:
    """The names this INSTANCE puts on its calendar at all, or None for "nobody
    is asking that question".

    THE OPERATOR'S HALF OF ADMISSION, asked of the one function that owns it
    rather than restated here — `providers.calendar_sources` is where a source
    being switched off for this whole instance is decided, and a second copy of
    that rule living in the read path is a copy that would answer differently the
    first time either one is edited. Neither `prefs` nor `endpoint` is passed
    down with it: this is the instance-wide question only, and the viewer's own
    narrowing is applied separately below, where it can be seen to be per viewer.

    `settings=None` MEANS THE SAME "DON'T NARROW" IT MEANS THERE. Every caller
    that has no Settings in hand — a test resolving one group, a caller with
    nothing but a stored row — keeps reading every source the row holds.
    """
    if settings is None:
        return None
    return frozenset(str(p.source) for p in providers.calendar_sources(settings=settings))


def sparse_sources() -> frozenset[str]:
    """Every registered service whose calendar listings arrive undescribed
    (Capabilities.describes_sparse_listings) — what the no-network rule means by
    "only a catalogue lists it" (filter.keep_title), named through the registry
    so no service is named where the rule is applied."""
    return frozenset(str(source) for source, provider in providers.registered().items()
                     if provider.capabilities.describes_sparse_listings)


def describing_sources(settings=None) -> tuple[str, ...]:
    """Every registered service whose row in the title store joins every card of
    a title it does not list (Capabilities.describes_every_card), and that this
    INSTANCE puts on its calendar (`instance_sources`), in declared order — the
    services app/calendar/cache.py's `with_title_only` reads rows from. Named
    through the registry, so the read names no service."""
    admitted = instance_sources(settings)
    return tuple(str(source) for source, provider in providers.registered().items()
                 if provider.capabilities.describes_every_card
                 and (admitted is None or str(source) in admitted))


def sparse_labels() -> str:
    """Those services' own labels ("TMDB"), joined — how a control offering the
    rule names them; "" when no registered service lists sparse."""
    return " or ".join(provider.label for provider in providers.registered().values()
                       if provider.capabilities.describes_sparse_listings)


def answering_services(endpoint, settings) -> list[tuple[str, str]]:
    """(name, label) for every service that could actually fill THIS calendar on
    THIS instance, in declared order — or an empty list when there is nothing to
    choose between. Every source control is built out of this — the calendar
    toolbar's, the Filters panel's and a share link's — so none of them can come
    to a different answer about what a calendar has to offer.

    IT IS THREE QUESTIONS AND NONE OF THEM IS ASKED HERE. Whether a service is
    on this INSTANCE's calendar at all belongs to `providers.calendar_sources`,
    and it is asked through `instance_sources` so that an operator switching a
    service off empties these controls by the same rule that empties the calendar
    underneath them. Whether it publishes THIS calendar is
    `capabilities.answers`. Whether this instance can READ its calendar is its
    port's `calendar_configured` — the question the fill asks before asking the
    service anything (cache.py's `_window_sources`): a service whose credential
    is not filled in has nothing on the calendar, and a switch for it, or a
    "that one only", would draw an empty month. No service is named in any of
    the three, so a fourth one is offered the day it is registered.

    AND WITH FEWER THAN TWO THERE IS NOTHING TO OFFER, which is returned as an
    empty list rather than as one entry. One service that can answer means every
    label a control could carry names the same outcome, and drawing it makes the
    page look as though a choice is available when none is.
    """
    admitted = instance_sources(settings)
    services = [(str(source), provider.label)
                for source, provider in providers.registered().items()
                if provider.capabilities.answers(endpoint.key)
                and (admitted is None or str(source) in admitted)
                and (settings is None or provider.calendar_port.calendar_configured(settings))]
    return services if len(services) >= 2 else []


def admitted_order(group, prefs=None, endpoint=None, settings=None) -> list[str]:
    """The sources in `group` this viewer will read, in declared order.

    THE PER-VIEWER EXCLUSION, AND IT HAS TO HAPPEN HERE. The window this group
    came out of is stored once per (endpoint, week) and served to every viewer,
    so a selection applied while filling it would write one person's exclusion
    into what everybody else reads. Applied at READ, over the union every source
    contributed, one viewer asking for one service and another asking for several
    get different answers out of the identical row — and changing the preference
    invalidates nothing, because nothing was ever stored per viewer.

    `prefs=None` means no account is asking — a public share page — and admits
    every source the group holds.

    WHAT SOMEBODY HAS LINKED DOES NOT ENTER INTO IT, and this function takes no
    `linked` so that it cannot start to. A calendar is fetched with the
    instance's own credentials or with none at all, so no viewer's identity is
    spent on one and there is no credential a link could supply — the account's
    preference is the only narrowing there is. `app/sources/prefs.py`'s
    `admits_calendar` owns that reasoning; linkage governs the tracker, where it
    is correct because reading somebody's history needs their token.

    `settings` IS THE OPERATOR'S EXCLUSION AND IT IS NOT THE SAME THING. A source
    this instance no longer puts on its calendar is gone for everybody, so a
    window filled while it was still in play holds records nobody should now be
    shown — see `instance_sources`. Applying it here is what makes switching a
    source off take effect on the spot rather than a TTL later; the fill applies
    it too, so the instance stops paying for records it will never show. Switching
    it back ON needs no cache clearing either: a stored window records which
    sources were ASKED for it, and one that never asked a source now in play is a
    MISS that refills (app/calendar/cache.py's `ensure_window`).
    """
    order = source_order(group)
    instance = instance_sources(settings)
    if instance is not None:
        order = [name for name in order if name in instance]
    if prefs is None:
        return order
    return [name for name in order if prefs.admits_calendar(name)]


def admitted_records(group, prefs=None, endpoint=None, settings=None) -> list[Record]:
    """Every admitted source's record for `group`, in declared order.

    SEPARATE FROM `resolve_records` BELOW SO THAT SOMETHING CAN HAPPEN BETWEEN
    THEM, and one thing does: app/calendar/enrich.py's read-time overlay fills in
    the fields one source's calendar files do not carry. It has to run on the
    PER-SOURCE records, before anything picks between them — a merged group whose
    other source won the card would otherwise never have its enrichment
    considered at all, and a field only that source knows could never win however
    the viewer set their preference.
    """
    by_source = group.get("by_source") or {}
    return [by_source[name] for name in admitted_order(group, prefs, endpoint, settings)]


def instance_admitted(groups, settings=None) -> list[tuple[dict, list[Record]]]:
    """`[(group, admitted_records(group, settings=settings))]` for every group of
    a read — the operator's admission asked once for all of them, where asking it
    per group walked the registry eighteen thousand times a month."""
    instance = instance_sources(settings)
    names = tuple(name for name in _DECLARED if instance is None or name in instance)
    return [(group, [by_source[name] for name in names if name in by_source])
            for group in groups for by_source in (group.get("by_source") or {},)]


def _present(value) -> bool:
    """Whether a source actually filled this field in.

    None, "" and [] are the defaults `Record` uses for "this source does not
    carry it", so they are absences rather than answers and a source offering one
    does not get to win a field from a source that has something to say. Zero is
    NOT an absence: a runtime of 0 would be nonsense, but the rule that says so
    belongs to whoever normalized it, not to a comparison.
    """
    return value is not None and value != "" and value != []


class Orders:
    """One viewer's order over each set of services a card names — THE ORDER
    EVERY FIELD IS RESOLVED IN, worked out once per set for a whole read.

    ONE ORDER ANSWERS FOR EVERY FIELD (app/sources/prefs.py's `source_order` is
    the whole of what a metadata preference does), and it depends on nothing but
    the viewer's preference and which services the card names — of which a
    calendar holds a handful of combinations. It used to be worked out per field
    per card: MEASURED, 438,231 calls for one October's All Episodes, a third of
    the month's resolution time. A read makes one of these (cache.narrow_for_viewer)
    and every card asks it; it lives as long as the read and no longer.

    `prefs=None` — nobody is asking — is the declared order, which is also what a
    viewer who has stated no preference gets. The two are the same answer and
    deliberately share a code path: there is nothing to seed and nothing that can
    drift out of step with the declared order.
    """

    def __init__(self, prefs=None):
        self._prefs = prefs
        self._memo: dict[tuple[str, ...], tuple[str, ...]] = {}

    def __call__(self, names: tuple[str, ...]) -> tuple[str, ...]:
        found = self._memo.get(names)
        if found is None:
            found = self._memo[names] = (names if self._prefs is None
                                         else tuple(self._prefs.source_order(names)))
        return found


def _first_present(ordered: Sequence[Record], field_name: str) -> Record | None:
    """The first of `ordered` that filled `field_name` in."""
    for record in ordered:
        if _present(getattr(record, field_name)):
            return record
    return None


# WHERE A CALENDAR DATE IS CURRENT SOMEWHERE ON EARTH. The zones in use run from
# UTC-12 to UTC+14, so a date D begins at D 00:00 in UTC+14 — 10:00 UTC the day
# before — and ends at D 24:00 in UTC-12, 12:00 UTC the day after.
_DATE_BEGINS_BEFORE_UTC = timedelta(hours=14)
_DATE_ENDS_AFTER_UTC = timedelta(hours=12)


def falls_on(air_ts: float, day: date) -> bool:
    """Whether the instant `air_ts` is on calendar date `day` somewhere on earth
    — the test a date-only description puts to another service's time before
    borrowing it (`_resolve_airing`)."""
    midnight = datetime(day.year, day.month, day.day, tzinfo=timezone.utc)
    return (midnight - _DATE_BEGINS_BEFORE_UTC
            <= epoch_moment(air_ts)
            < midnight + timedelta(days=1) + _DATE_ENDS_AFTER_UTC)


def _resolve_airing(listed: Sequence[Record]) -> tuple[float, bool]:
    """(air_ts, date_only) for the card: THE PRIMARY'S DATE AND TIME — the first
    service in the viewer's order, the one the card is attributed to.

    WHEN IT AIRS IS THE PRIMARY'S TO SAY, because it decides WHERE THE CARD IS
    DRAWN. One episode is one card however many services list it (cache.py's
    `may_join`), and services disagree about days by more than a timezone:
    Yellowjackets S04E01 is a Friday evening on Simkl and a Sunday evening on
    Trakt. Taking the day from one service and the description from another
    would draw a card nobody's service stands behind; taking it from the
    viewer's own service is the one answer that cannot surprise them. So a
    viewer who flips their order sees the same episode, still one card, on
    their new first service's day. The other services' days are not thrown
    away — `listed_airings` keeps each one, and the card says where they differ.

    ONE THING IS BORROWED, AND ONLY WHEN IT AGREES. A date-only primary (TMDB
    lists no times; a film is a date everywhere) leaves the time unsaid, so the
    first other service in the viewer's order whose INSTANT falls on the
    primary's date somewhere on earth (`falls_on`) lends its time — the same
    airing said more precisely, which is the case that used to make a timed
    source beat a date-only one outright: MEASURED, Dexter: Original Sin's card
    lost Trakt's 22:00 when Simkl's bare "2024-12-15" joined it. A time from an
    instant on another day is a different service's claim about a different
    day, and borrowing it would move the card off the primary's date; nothing
    agrees, and the card shows the primary's date with no time. `listed` is the
    card's listings in the viewer's order.
    """
    primary = listed[0]
    if not primary.date_only:
        return primary.air_ts, False
    day = epoch_moment(primary.air_ts).date()
    for other in listed[1:]:
        if not other.date_only and falls_on(other.air_ts, day):
            return other.air_ts, False
    return primary.air_ts, True


def _coordinate_rank(record: Record) -> int:
    """How completely `record` states the episode coordinate: 2 both, 1 one, 0
    neither."""
    return int(_present(record.season)) + int(_present(record.episode_number))


def _resolve_coordinate(listed: Sequence[Record]) -> Record | None:
    """The source whose episode coordinate the card shows, COMPLETENESS FIRST and
    the viewer's preference only breaking ties.

    THE ONE FIELD WHERE PRECEDENCE IS NOT THE FIRST QUESTION, and it is the one
    that would ship as a visible defect if it were. A merged group holds records
    like (season 1, episode 1) and (no season, episode 1) for one airing: the
    second does not CONTRADICT the first, it says the same thing less precisely.
    Every other field on a record is two services making competing claims, where
    whose claim you trust is genuinely a preference; this one is one service
    saying less than the other, and there is no preference under which "less" is
    the right answer. A viewer who prefers that service would get a card with no
    S01E01 chip at all — not a different label, no label.

    So the most complete coordinate wins and the preference chooses only among
    equally complete ones. A source stating none is never chosen while one
    stating some exists, which leaves a card labelled exactly as well as the best
    of what the services between them knew. `listed` is the card's listings in
    the viewer's order.
    """
    best = max(_coordinate_rank(r) for r in listed)
    if best == 0:
        return None
    return next(r for r in listed if _coordinate_rank(r) == best)


def _merged_genres(ordered: Sequence[Record]) -> list[str]:
    """Every genre any admitted source gave, de-duplicated, preferred source
    first. A union rather than a contest — see GENRES_FIELD."""
    out: list[str] = []
    for record in ordered:
        for genre in record.genres:
            if genre not in out:
                out.append(str(genre))
    return out


def _title_facts(ordered: Sequence[Record]) -> tuple[list[str] | None, str | None]:
    """(keywords, show_type) for the card — what the services that STATE them
    said, and None for each when none of them does (Record.keywords).

    NOT A PREFERENCE FIELD, deliberately outside SCALAR_FIELDS: one catalogue
    states these today, so there is nothing for a viewer's order to choose
    between, and naming them there would make them something a stored
    preference could name. The keywords are a union, as genres are; the type is
    the first stated one in the viewer's order, an empty one (a description that
    gave none) still counting as stated. `ordered` is every record of the card in
    the viewer's order.
    """
    stating = [r for r in ordered if r.keywords is not None]
    keywords = None
    if stating:
        keywords = []
        for record in stating:
            keywords.extend(k for k in record.keywords if k not in keywords)
    typed = [r.show_type for r in ordered if r.show_type is not None]
    show_type = next((t for t in typed if t), typed[0] if typed else None)
    return keywords, show_type


# THE FIELDS WHOSE EVERY SERVICE IS SHOWN, never folded into agreement. Two
# services' ratings are two audiences answering, and the same number from both is
# still two answers — a card draws each beside its service's mark, and a viewer
# reading one of them alone could not tell whose it is.
SIDE_BY_SIDE = frozenset({"rating"})


def _meaning(field_name: str, source: str, value) -> frozenset:
    """What `value`, as `source` states `field_name`, may MEAN — the spellings it
    cannot be told apart from — so two services that said one thing two ways
    agree (`_distinct`). One meaning for every field but a status, where a
    service's word may be too coarse to name just one (Provider.status_means).

    Text is folded: case, runs of whitespace and the no-break spaces one
    service's catalogue writes ("Tokyo MX" and "TOKYO MX" are one network). A
    runtime is whole minutes and a year its digits. A picture is its address,
    exactly — a URL's path is case-sensitive, and two services' pictures are two
    pictures however alike they look.
    """
    if field_name == "status":
        return providers.get(source).status_means(str(value))
    if field_name == "poster":
        return frozenset({str(value)})
    if field_name in ("runtime", "imdb_rating", "rating"):
        try:
            number = float(value)
        except (TypeError, ValueError):
            pass
        else:
            return frozenset({round(number) if field_name == "runtime" else round(number, 1)})
    return frozenset({" ".join(str(value).split()).casefold()})


def _distinct(field_name: str, stated: dict[str, Any], order: list[str]) -> dict[str, Any]:
    """`stated` — {service: its value for one field}, in declared order — less
    every value another one already says: one entry per thing the services
    really disagree about, each under the first service in the viewer's `order`
    that said it, still in declared order so a service's mark sits in the same
    place on every card. One entry left means they agree.

    A COARSER WORD AGREES WITH A FINER ONE IT COVERS, and steps aside for it —
    "ended" from a service that does not tell ending from cancelling beside
    "canceled" from one that does is one statement, said more precisely by the
    second, which is what is shown. That is the only way a value can be covered,
    so for every field with one meaning per value the first statement of each
    meaning in the viewer's order — the winner's among them — is the one kept.
    """
    if field_name in SIDE_BY_SIDE:
        return dict(stated)
    meanings = {name: _meaning(field_name, name, value) for name, value in stated.items()}
    precise = {meaning for meaning in meanings.values() if len(meaning) == 1}
    kept: dict[frozenset, str] = {}
    for name in order:
        meaning = meanings[name]
        if len(meaning) > 1 and any(finer <= meaning for finer in precise):
            continue
        kept.setdefault(meaning, name)
    shown = set(kept.values())
    return {name: value for name, value in stated.items() if name in shown}


def _provenance(every: list[Record], order: tuple[str, ...]) -> tuple[dict, dict]:
    """(field_sources, alternatives) — who filled each scalar field in, and where
    they REALLY filled it in differently, a value per thing they disagree about.

    SCALARS ONLY. These exist for a card that wants to show two ratings side by
    side, or a poster with a logo that swaps it; a union field has no second
    value to show and a coordinate's second value is the same fact said worse.

    THE WINNER IS IN `alternatives` TOO, not only the loser. A caller rendering
    "8.1 (Trakt) · 7.9 (Simkl)" needs both halves and should not have to
    reconstruct one of them from the record it was handed beside a map of the
    other — that is two sources of truth for one line of text.

    A DIFFERENCE OF SPELLING IS AGREEMENT (`_distinct`), so a card offers a flip
    or a pair of chips only where its services say different things. MEASURED on
    one October's All Episodes before this rule: of 1,610 cards with any such
    control, 1,603 disagreed about a status, 1,480 of them only in how each
    service words one, and dozens about a title or a network only in case. Where
    two spellings agree, `field_sources` lists the WINNER FIRST — its spelling is
    the one on the card, and Record.shown_source names it.

    `every` is the card's records in declared order, `order` their names in the
    viewer's.

    THE SAME VALUE FROM EVERY SERVICE IS ONE STATEMENT, WITHOUT ASKING WHAT IT
    MEANS: identical values fold identically, so only where they differ does
    `_distinct` decide — most fields of most merged cards, a few million meaning
    tests a month. A status is the exception and always asks, since what one word
    means is each service's own (Provider.status_means).
    """
    field_sources: dict[str, list[str]] = {}
    alternatives: dict[str, dict[str, Any]] = {}
    for field_name in SCALAR_FIELDS:
        stated = {str(r.source): value for r in every
                  if _present(value := getattr(r, field_name))}
        if not stated:
            continue
        stating = [name for name in order if name in stated]
        field_sources[field_name] = stating
        if len(stated) < 2 or (field_name not in SIDE_BY_SIDE and field_name != "status"
                               and len(set(stated.values())) == 1):
            continue
        distinct = _distinct(field_name, stated, stating)
        if len(distinct) > 1:
            alternatives[field_name] = distinct
    return field_sources, alternatives


class _Arranged(NamedTuple):
    """One card's records as every rule below reads them, put in order once."""
    every: list[Record]          # listings and title-only records, declared order
    ordered: list[Record]        # the same, in the viewer's order
    order: tuple[str, ...]       # their names, in the viewer's order
    listed: list[Record]         # the listings alone, in the viewer's order
    primary: Record              # whose card it is: the first listing in that order


def _arrange(records: Sequence[Record], describing: Sequence[Record],
             orders: Orders) -> _Arranged:
    """IN DECLARED ORDER FIRST, the title-only records among the listings rather
    than after them: a viewer who has stated no order reads the declared one, and
    a describing service declared first must win a field for them as its listing
    would have. One record per service — a listing and a title-only record of
    one service never meet (cache.with_title_only)."""
    every = sorted([*records, *describing], key=lambda r: _DECLARED[str(r.source)])
    by_name = {str(r.source): r for r in every}
    order = orders(tuple(by_name))
    listing = {str(r.source) for r in records}
    listed = [by_name[name] for name in orders(tuple(n for n in by_name if n in listing))]
    return _Arranged(every, [by_name[name] for name in order], order, listed, listed[0])


# What the filters judge a card by (filter.keep_title, filter.prune_disguised_films),
# each the field resolve_records gives the card.
_JUDGED_SCALARS = ("network", "country", "certification", "language", "poster")


class Facts(NamedTuple):
    """WHAT A CARD'S FILTERS JUDGE IT BY, and nothing else — the fields
    `resolve_records` would give the card, by the same rules (`_arrange`,
    `_first_present`, `_merged_genres`, `_title_facts`), worked out without the
    rest of it.

    FOR THE READ, WHICH JUDGES EVERY CARD AND DRAWS FEW (cache.narrow_for_viewer):
    MEASURED, one October's All Episodes resolved 18,759 cards in full to draw
    1,263 of them under one viewer's filters, the provenance, the buttons and the
    airing of every card thrown away. The cards that survive are resolved in full.
    """
    media: Any
    anime_type: str
    network: str
    country: str
    certification: str
    language: str
    poster: str | None
    genres: list[str]
    keywords: list[str] | None
    show_type: str | None
    enriched: bool


def judged_facts(records: Sequence[Record], describing: Sequence[Record] = (), *,
                 orders: Orders) -> Facts | None:
    """`Facts` for the card `records` (its listings) and `describing` (its
    title-only records) make — None for no listing, as `resolve_records` gives."""
    if not records:
        return None
    if len(records) == 1 and not describing:
        # ONE LISTING AND NOTHING DESCRIBING IT — about half a month's cards — is
        # its own answer to every rule below, whatever the viewer's order.
        only = records[0]
        keywords, show_type = _title_facts(records)
        return Facts(only.media, only.anime_type, only.network, only.country,
                     only.certification, only.language, only.poster,
                     genres=_merged_genres(records), keywords=keywords,
                     show_type=show_type, enriched=only.enriched)
    card = _arrange(records, describing, orders)
    scalars = {}
    for field_name in _JUDGED_SCALARS:
        winner = _first_present(card.ordered, field_name)
        scalars[field_name] = getattr(winner if winner is not None else card.primary,
                                      field_name)
    keywords, show_type = _title_facts(card.ordered)
    return Facts(card.primary.media, card.primary.anime_type, **scalars,
                 genres=_merged_genres(card.ordered), keywords=keywords,
                 show_type=show_type, enriched=any(r.enriched for r in card.every))


def resolve_records(group, records: list[Record], prefs=None,
                    describing: Sequence[Record] = (), *,
                    orders: Orders | None = None) -> Record | None:
    """The one record this viewer sees for `group`, built out of `records` — the
    services that LIST it — and `describing`, the title-only records of services
    that describe the title without listing this airing (cache.with_title_only).

    NONE IS A REAL ANSWER HERE, NOT ONLY A MALFORMED ONE. A group every source
    behind it is one this viewer excluded has nothing to show them, and dropping
    it is how "show me only this service" narrows a shared window down to the
    titles that service actually listed.

    THE SHAPE OF THE ANSWER: one source is the card's — see SOURCE_FIELD — and
    every other field is filled from the first source this viewer's order names
    that actually stated it. A preference REORDERS and never excludes, so a field
    only one source carries comes from that source whatever anybody prefers, and
    a viewer cannot make a card emptier than the services made it.

    A TITLE-ONLY RECORD IS AN OPINION ON THE CARD'S FACTS AND NOTHING ELSE, and
    every choice below that names a listing is made among `records` alone:
      - it never makes a card: with no listing there is nothing to draw, however
        much a describing service says — a viewer who reads only that service
        sees only what it lists;
      - it never wins SOURCE, AIRING or COORDINATE — the card's attribution, its
        day and time, its episode — because none of them is a fact about the
        title, and the first record in a viewer's order would otherwise be the
        description, moving the card to a day no service that lists it gave;
      - it competes for every scalar field and joins the genres, keywords and
        show type like any record, which is what it is for: a certification or
        a language the listing services never state reaches the card, and the
        filters judge it;
      - its ids join the ones the card's buttons address, as a service that
        knows the title without listing this airing (`links`).

    THE IDS ARE THE GROUP'S, NOT THE RECORD'S. `ids` is hoisted onto the group at
    fill because it is the MATCH RESULT — the union of every id space any source
    named this title in — and the arr/Seerr buttons, the tracker and the ranker
    all read it. Handing back the one source's own ids would quietly narrow that
    union to whichever service happened to win.

    `orders` is the read's `Orders` for this viewer, so a month works each set of
    services' order out once; a caller resolving one card may leave it out.
    """
    if not records:
        return None
    card = _arrange(records, describing, orders if orders is not None else Orders(prefs))
    primary = card.primary
    # A COPY OF THE PRIMARY, every field the card is then given set on it — its
    # own containers left shared with the record, as `dataclasses.replace` would
    # leave them, because nothing writes into a record it is handed; `ids` and
    # `genres` are replaced below.
    resolved = copy.copy(primary)
    resolved.ids = dict(primary.ids or {})

    for field_name in SCALAR_FIELDS:
        winner = _first_present(card.ordered, field_name)
        if winner is not None:
            setattr(resolved, field_name, getattr(winner, field_name))

    resolved.genres = _merged_genres(card.ordered)
    resolved.keywords, resolved.show_type = _title_facts(card.ordered)

    coordinate = _resolve_coordinate(card.listed)
    for field_name in COORDINATE_FIELDS:
        setattr(resolved, field_name,
                getattr(coordinate, field_name) if coordinate is not None
                else getattr(primary, field_name))

    resolved.air_ts, resolved.date_only = _resolve_airing(card.listed)
    every = card.every

    # ENRICHED IS A PROPERTY OF THE GROUP, NOT OF THE CARD'S SOURCE. The flag
    # exists so app/calendar/filter.py can tell "empty because there is nothing to
    # say" from "empty because nobody has looked yet" and exempt the second rather
    # than judging it. If ANY source behind this group has looked, the values
    # above are real answers and the group is judgeable — reading the flag off the
    # card's source alone would exempt a merged card whose genres came, fully
    # filled in, from the other service. A described title-only record counts:
    # its words are on the card.
    resolved.enriched = any(r.enriched for r in every)

    ids = group.get("ids")
    if isinstance(ids, dict) and ids:
        resolved.ids = dict(ids)

    # WHERE EACH SERVICE'S OWN PAGE FOR THIS TITLE IS, in declared order — every
    # service that LISTS this airing, and every one the title store knows the
    # title at though it does not (Record.known_ids), on every card, one service
    # or several. NOT a resolved value and not something a preference can order:
    # the card is attributed to one service and `detail_url` travels with that
    # attribution, but each service really does have a page and a reader who
    # wants another one is not disagreeing with anybody. So this is collected
    # rather than won — nothing above chooses between these, and nothing may be
    # added here that a viewer could state a preference about, because that is
    # what SCALAR_FIELDS is for.
    known: dict[str, Any] = {}
    for record in every:
        for name, value in (record.known_ids or {}).items():
            known.setdefault(name, value)
    resolved.known_ids = known
    resolved.links = detail_source.service_links(
        resolved.media, {**known, **resolved.ids},
        {str(r.source): r.detail_url for r in records})

    # Empty for a single-source group, which is every group on an instance that
    # has only ever had one source — so the provenance machinery costs nothing at
    # all for the accounts that will never see a merged card.
    if len(every) > 1:
        resolved.field_sources, resolved.alternatives = _provenance(every, card.order)
    if len(records) > 1:
        # WHEN EACH SERVICE THAT LISTS IT SAYS IT AIRS, in declared order — a
        # title-only record states no airing. The card is drawn
        # on the primary's day (`_resolve_airing`), and a reader whose services
        # disagree about the day deserves to see that they do — the card draws
        # every service's day and time beside its mark wherever they differ for
        # this viewer, which only `render` can judge, since a difference is
        # about the local day. Collected, not won, for the reason the links are.
        resolved.listed_airings = {str(r.source): {"air_ts": r.air_ts,
                                                   "date_only": r.date_only}
                                   for r in records}
    return resolved


def resolve(group, prefs=None, endpoint=None, settings=None) -> Record | None:
    """`admitted_records` and `resolve_records` in one call, for a caller with
    nothing to do in between.

    The read path in app/calendar/cache.py does have something to do in between —
    the enrichment overlay, and the title-only records it reads from the store
    (`with_title_only`) — and calls the two halves itself. Everything else wants
    this, and is answered from the group's listings alone.
    """
    return resolve_records(group, admitted_records(group, prefs, endpoint, settings), prefs)
