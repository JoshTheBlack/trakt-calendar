"""Which source's values one viewer sees for a title two sources may describe.

THE SECOND HALF OF A SPLIT THE CACHE FORCED. Two operations sit between "what a
source said" and "what a person sees", and they have different scopes, which is
why they happen in different places:

  MATCHING    is this record and that record the same airing? User-independent,
              so it runs at FILL and is stored (app/calendar/cache.py groups the
              records it stores).
  RESOLUTION  which source's value does THIS VIEWER see for this field? Per
              account by definition, so it runs at READ, over already-cached
              data, and changing a preference therefore invalidates nothing.

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

from dataclasses import replace
from typing import Any

from .. import providers
from ..providers.base import Record, Source

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
    "title", "overview", "poster", "rating", "network", "country", "language",
    "runtime", "status", "certification", "year", "episode_title",
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

# WHEN IT AIRS, and the flag saying whether that instant is really an instant.
# One unit because `date_only` changes how `air_ts` is read: a timestamp from one
# source under the other's flag renders a film a day early for half the world
# (see Record.date_only).
AIRING_FIELD = "airing"
AIRING_FIELDS = ("air_ts", "date_only")

# The union field. Never a contest: two services listing different genres for one
# title have both told the truth, and picking a winner would throw away a genre a
# viewer filters on.
GENRES_FIELD = "genres"

# Everything a stored preference may name. `ids` is deliberately absent — the
# group's id union is the MATCH RESULT and belongs to nobody, so there is nothing
# to prefer.
FIELDS = (*SCALAR_FIELDS, GENRES_FIELD, COORDINATE_FIELD, AIRING_FIELD, SOURCE_FIELD)


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

    `endpoint` IS THE CALENDAR THIS GROUP CAME OUT OF, and it is passed because a
    selection can be stated per calendar: one service's movie listing can be a
    global release calendar while its show listing is the coverage somebody
    wanted (app/sources/prefs.py's `calendar_selection` has the measurement).
    None asks the account-wide question, which is what a caller with no endpoint
    in hand should get.

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
    MISS that refills (app/calendar/cache.py's `load_window`).
    """
    order = source_order(group)
    instance = instance_sources(settings)
    if instance is not None:
        order = [name for name in order if name in instance]
    if prefs is None:
        return order
    return [name for name in order if prefs.admits_calendar(name, endpoint)]


def admitted_records(group, prefs=None, endpoint=None, settings=None) -> list[Record]:
    """Every admitted source's record for `group`, parsed, in declared order.

    SEPARATE FROM `resolve_records` BELOW SO THAT SOMETHING CAN HAPPEN BETWEEN
    THEM, and one thing does: app/calendar/enrich.py's read-time overlay fills in
    the fields one source's calendar files do not carry. It has to run on the
    PER-SOURCE records, before anything picks between them — a merged group whose
    other source won the card would otherwise never have its enrichment
    considered at all, and a field only that source knows could never win however
    the viewer set their preference.

    A row that will not parse is skipped rather than raised over: one unreadable
    record in a stored window is not a reason to fail a month.
    """
    by_source = group.get("by_source") or {}
    out: list[Record] = []
    for name in admitted_order(group, prefs, endpoint, settings):
        try:
            out.append(Record.from_dict(by_source[name]))
        except (ValueError, TypeError, KeyError):
            continue
    return out


def _present(value) -> bool:
    """Whether a source actually filled this field in.

    None, "" and [] are the defaults `Record` uses for "this source does not
    carry it", so they are absences rather than answers and a source offering one
    does not get to win a field from a source that has something to say. Zero is
    NOT an absence: a runtime of 0 would be nonsense, but the rule that says so
    belongs to whoever normalized it, not to a comparison.
    """
    return value is not None and value != "" and value != []


def _order(prefs, field_name: str, names: list[str]) -> list[str]:
    """`names` in the order this viewer wants for `field_name`.

    `prefs=None` — nobody is asking — is the declared order, which is also what a
    viewer who has stated no preference gets. The two are the same answer and
    deliberately share a code path: the default table is the declared order, so
    there is nothing to seed and nothing that can drift out of step with it.
    """
    if prefs is None:
        return names
    return prefs.field_order(field_name, names)


def _winner(records: list[Record], order: list[str], field_name: str,
            present) -> Record | None:
    """The first record in `order` that `present` says has an answer."""
    by_name = {str(r.source): r for r in records}
    for name in order:
        record = by_name.get(name)
        if record is not None and present(record):
            return record
    return None


def _coordinate_rank(record: Record) -> int:
    """How completely `record` states the episode coordinate: 2 both, 1 one, 0
    neither."""
    return int(_present(record.season)) + int(_present(record.episode_number))


def _resolve_coordinate(records: list[Record], order: list[str]) -> Record | None:
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
    of what the services between them knew.
    """
    ranked = [r for r in records if _coordinate_rank(r) > 0]
    if not ranked:
        return None
    best = max(_coordinate_rank(r) for r in ranked)
    return _winner([r for r in ranked if _coordinate_rank(r) == best], order,
                   COORDINATE_FIELD, lambda _r: True)


def _merged_genres(records: list[Record], order: list[str]) -> list[str]:
    """Every genre any admitted source gave, de-duplicated, preferred source
    first. A union rather than a contest — see GENRES_FIELD."""
    by_name = {str(r.source): r for r in records}
    out: list[str] = []
    for name in order:
        record = by_name.get(name)
        for genre in (record.genres if record is not None else ()):
            if genre not in out:
                out.append(str(genre))
    return out


def _provenance(records: list[Record]) -> tuple[dict, dict]:
    """(field_sources, alternatives) — who filled each scalar field in, and where
    they filled it in differently, every value they gave.

    SCALARS ONLY. These exist for a card that wants to show two ratings side by
    side, or a poster with a logo that swaps it; a union field has no second
    value to show and a coordinate's second value is the same fact said worse.

    THE WINNER IS IN `alternatives` TOO, not only the loser. A caller rendering
    "8.1 (Trakt) · 7.9 (Simkl)" needs both halves and should not have to
    reconstruct one of them from the record it was handed beside a map of the
    other — that is two sources of truth for one line of text.
    """
    field_sources: dict[str, list[str]] = {}
    alternatives: dict[str, dict[str, Any]] = {}
    for field_name in SCALAR_FIELDS:
        stated = {str(r.source): getattr(r, field_name) for r in records
                  if _present(getattr(r, field_name))}
        if not stated:
            continue
        field_sources[field_name] = list(stated)
        if len(set(map(repr, stated.values()))) > 1:
            alternatives[field_name] = stated
    return field_sources, alternatives


def resolve_records(group, records: list[Record], prefs=None) -> Record | None:
    """The one record this viewer sees for `group`, built out of `records`.

    NONE IS A REAL ANSWER HERE, NOT ONLY A MALFORMED ONE. A group every source
    behind it is one this viewer excluded has nothing to show them, and dropping
    it is how "show me only this service" narrows a shared window down to the
    titles that service actually listed.

    THE SHAPE OF THE ANSWER: one source is the card's — see SOURCE_FIELD — and
    every other field is filled from the first source this viewer's order names
    that actually stated it. A preference REORDERS and never excludes, so a field
    only one source carries comes from that source whatever anybody prefers, and
    a viewer cannot make a card emptier than the services made it.

    THE IDS ARE THE GROUP'S, NOT THE RECORD'S. `ids` is hoisted onto the group at
    fill because it is the MATCH RESULT — the union of every id space any source
    named this title in — and the arr/Seerr buttons, the tracker and the ranker
    all read it. Handing back the one source's own ids would quietly narrow that
    union to whichever service happened to win.
    """
    if not records:
        return None
    names = [str(r.source) for r in records]
    primary = _winner(records, _order(prefs, SOURCE_FIELD, names), SOURCE_FIELD,
                      lambda _r: True)
    if primary is None:  # unreachable while `names` comes from `records`
        return None
    resolved = replace(primary, ids=dict(primary.ids or {}),
                       genres=list(primary.genres or []))

    for field_name in SCALAR_FIELDS:
        winner = _winner(records, _order(prefs, field_name, names), field_name,
                         lambda r, f=field_name: _present(getattr(r, f)))
        if winner is not None:
            setattr(resolved, field_name, getattr(winner, field_name))

    resolved.genres = _merged_genres(records, _order(prefs, GENRES_FIELD, names))

    coordinate = _resolve_coordinate(records, _order(prefs, COORDINATE_FIELD, names))
    for field_name in COORDINATE_FIELDS:
        setattr(resolved, field_name,
                getattr(coordinate, field_name) if coordinate is not None
                else getattr(primary, field_name))

    airing = _winner(records, _order(prefs, AIRING_FIELD, names), AIRING_FIELD,
                     lambda r: _present(r.air_ts))
    for field_name in AIRING_FIELDS:
        setattr(resolved, field_name, getattr(airing or primary, field_name))

    # ENRICHED IS A PROPERTY OF THE GROUP, NOT OF THE CARD'S SOURCE. The flag
    # exists so app/calendar/filter.py can tell "empty because there is nothing to
    # say" from "empty because nobody has looked yet" and exempt the second rather
    # than judging it. If ANY source behind this group has looked, the values
    # above are real answers and the group is judgeable — reading the flag off the
    # card's source alone would exempt a merged card whose genres came, fully
    # filled in, from the other service.
    resolved.enriched = any(r.enriched for r in records)

    ids = group.get("ids")
    if isinstance(ids, dict) and ids:
        resolved.ids = dict(ids)

    # Empty for a single-source group, which is every group on an instance that
    # has only ever had one source — so the provenance machinery costs nothing at
    # all for the accounts that will never see a merged card.
    if len(records) > 1:
        resolved.field_sources, resolved.alternatives = _provenance(records)
        # WHERE EACH SERVICE'S OWN PAGE FOR THIS TITLE IS, in declared order.
        # NOT a resolved value and not something a preference can order: the card
        # is attributed to one service and `detail_url` travels with that
        # attribution, but both services really do have a page and a reader who
        # wants the other one is not disagreeing with anybody. So this is
        # collected rather than won — nothing above chooses between these, and
        # nothing may be added here that a viewer could state a preference about,
        # because that is what SCALAR_FIELDS is for.
        resolved.source_links = {str(r.source): r.detail_url
                                 for r in records if r.detail_url}
    return resolved


def resolve(group, prefs=None, endpoint=None, settings=None) -> Record | None:
    """`admitted_records` and `resolve_records` in one call, for a caller with
    nothing to do in between.

    The read path in app/calendar/cache.py does have something to do in between —
    the enrichment overlay — and calls the two halves itself. Everything else
    wants this.
    """
    return resolve_records(group, admitted_records(group, prefs, endpoint, settings), prefs)
