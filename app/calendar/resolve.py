"""Which source's values one viewer sees for a title two sources may describe.

THE SECOND HALF OF A SPLIT THE CACHE FORCED. Two operations sit between "what a
source said" and "what a person sees", and they have different scopes, which is
why they happen in different places:

  MATCHING    is this record and that record the same card? User-independent:
              one episode is one card whichever services list it — the title
              store's episode row, decided as each listing is written
              (app/metadata/episodes.py) and read back by app/calendar/cache.py's
              `group_records`.
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
from collections.abc import Mapping, Sequence
from datetime import date, datetime, timedelta, timezone
from typing import Any

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
# instant, resolved as one by `resolve_airing`. One unit because `date_only`
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
                if providers.lists_calendar(provider, endpoint.key, settings)
                and (admitted is None or str(source) in admitted)
                and (settings is None or provider.calendar_port.calendar_configured(settings))]
    return services if len(services) >= 2 else []


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


# WHERE A CALENDAR DATE IS CURRENT SOMEWHERE ON EARTH. The zones in use run from
# UTC-12 to UTC+14, so a date D begins at D 00:00 in UTC+14 — 10:00 UTC the day
# before — and ends at D 24:00 in UTC-12, 12:00 UTC the day after.
_DATE_BEGINS_BEFORE_UTC = timedelta(hours=14)
_DATE_ENDS_AFTER_UTC = timedelta(hours=12)


def falls_on(air_ts: float, day: date) -> bool:
    """Whether the instant `air_ts` is on calendar date `day` somewhere on earth
    — the test a date-only description puts to another service's time before
    borrowing it (`resolve_airing`)."""
    midnight = datetime(day.year, day.month, day.day, tzinfo=timezone.utc)
    return (midnight - _DATE_BEGINS_BEFORE_UTC
            <= epoch_moment(air_ts)
            < midnight + timedelta(days=1) + _DATE_ENDS_AFTER_UTC)


def resolve_airing(listed: Sequence) -> tuple[float, bool]:
    """(air_ts, date_only) for the card: THE PRIMARY'S DATE AND TIME — the first
    service in the viewer's order, the one the card is attributed to.

    WHEN IT AIRS IS THE PRIMARY'S TO SAY, because it decides WHERE THE CARD IS
    DRAWN. One episode is one card however many services list it and however far
    apart they date it (cache.py's `group_records`), and services disagree about
    days by more than a timezone:
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


def resolve_coordinate(listed: Sequence):
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


def _keywords(ordered: Sequence[Record]) -> list[str] | None:
    """The card's keywords — every keyword the services that STATE them gave, a
    union as genres are, and None when none of them states any (Record.keywords).

    NOT A PREFERENCE FIELD, deliberately outside SCALAR_FIELDS: one catalogue
    states these today, so there is nothing for a viewer's order to choose
    between. The show type beside them is the first stated in the viewer's
    order, an empty one (a description that gave none) still counting as stated
    — decided by the query with every other field's winner."""
    stating = [r for r in ordered if r.keywords is not None]
    if not stating:
        return None
    keywords: list[str] = []
    for record in stating:
        keywords.extend(k for k in record.keywords if k not in keywords)
    return keywords


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


def compose(listed: Sequence[Record], describing: Sequence[Record],
            winners: Mapping[str, str | None], *, ids, orders: Orders,
            airing: tuple[float, bool], coordinate: tuple) -> Record:
    """The one record a viewer's card shows, composed from `listed` — the
    services of theirs that LIST it, in their order — and `describing`, the
    title-only records of services that describe the title without listing this
    airing (app/calendar/query.py), with `winners` the query's decision of whose
    value each field shows (query's `_winner`: the first of the viewer's services
    that states it — a preference REORDERS and never excludes, so a viewer cannot
    make a card emptier than the services made it).

    A TITLE-ONLY RECORD IS AN OPINION ON THE CARD'S FACTS AND NOTHING ELSE:
      - it never makes a card — with no listing there is nothing to draw, and a
        viewer who reads only that service sees only what it lists;
      - it never wins SOURCE, AIRING or COORDINATE — the card's attribution, its
        day and time, its episode, all chosen among the listings (the query's
        placement: `resolve_airing`, `resolve_coordinate`) — because none of
        them is a fact about the title, and the first record in a viewer's order
        would otherwise be the description, moving the card to a day no service
        that lists it gave;
      - it competes for every scalar field and joins the genres, keywords and
        show type like any record, which is what it is for: a certification or a
        language the listing services never state reaches the card;
      - its ids join the ones the card's buttons address, as a service that
        knows the title without listing this airing (`links`).

    THE IDS ARE THE CARD'S, NOT THE RECORD'S (`ids`): the union of every id
    space any listing named the title in, which the arr/Seerr buttons, the
    tracker and the ranker read — handing back one service's own ids would
    narrow it to whichever happened to win.
    """
    every = sorted([*listed, *describing], key=lambda r: _DECLARED[str(r.source)])
    by_name = {str(r.source): r for r in every}
    order = orders(tuple(by_name))
    ordered = [by_name[name] for name in order]
    primary = listed[0]
    # A COPY OF THE PRIMARY, every field the card is then given set on it — its
    # own containers left shared with the record, as `dataclasses.replace` would
    # leave them, because nothing writes into a record it is handed.
    resolved = copy.copy(primary)
    resolved.ids = dict(primary.ids or {})
    for field_name in SCALAR_FIELDS:
        winner = winners.get(field_name)
        if winner is not None:
            setattr(resolved, field_name, getattr(by_name[winner], field_name))
    resolved.genres = _merged_genres(ordered)
    resolved.keywords = _keywords(ordered)
    stated = winners.get("show_type")
    resolved.show_type = (by_name[stated].show_type if stated is not None
                          else "" if any(r.show_type is not None for r in ordered) else None)
    resolved.season, resolved.episode_number, resolved.episode_label = coordinate
    resolved.air_ts, resolved.date_only = airing
    # WHICH FINALE, as the first of the card's listings in the viewer's order
    # that says. Where two services both mark one episode they have agreed every
    # time it was measured (82 of 82 on a copy of dev), so this is never a
    # contest, and a listing the calendar marks nothing on says nothing.
    resolved.episode_mark = next((r.episode_mark for r in listed if r.episode_mark), "")

    # ENRICHED IS A PROPERTY OF THE CARD, NOT OF ITS SOURCE. The flag exists so
    # a filter can tell "empty because there is nothing to say" from "empty
    # because nobody has looked yet"; if ANY service behind the card has looked,
    # the values above are real answers. A described title-only record counts:
    # its words are on the card.
    resolved.enriched = any(r.enriched for r in every)
    if isinstance(ids, dict) and ids:
        resolved.ids = dict(ids)

    # WHERE EACH SERVICE'S OWN PAGE FOR THIS TITLE IS, in declared order — every
    # service that LISTS this airing, and every one the title store knows the
    # title at though it does not (Record.known_ids), on every card. Collected
    # rather than won: each service really does have a page, and a reader who
    # wants another one is not disagreeing with anybody.
    known: dict[str, Any] = {}
    for record in every:
        for name, value in (record.known_ids or {}).items():
            known.setdefault(name, value)
    resolved.known_ids = known
    resolved.links = detail_source.service_links(
        resolved.media, {**known, **resolved.ids},
        {str(r.source): r.detail_url for r in listed})

    # Empty for a card one service makes, which is every card on an instance
    # that has only ever had one source — so the provenance costs nothing for the
    # accounts that will never see a merged card.
    if len(every) > 1:
        resolved.field_sources, resolved.alternatives = _provenance(every, order)
    if len(listed) > 1:
        # WHEN EACH SERVICE THAT LISTS IT SAYS IT AIRS, in declared order — a
        # title-only record states no airing. The card is drawn on its first
        # service's day, and a reader whose services disagree about the day sees
        # that they do: the card draws every service's day beside its mark
        # wherever they differ for this viewer, which only `render` can judge.
        resolved.listed_airings = {str(r.source): {"air_ts": r.air_ts,
                                                   "date_only": r.date_only}
                                   for r in sorted(listed, key=lambda r: _DECLARED[str(r.source)])}
    return resolved
