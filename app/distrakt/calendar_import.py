"""Bringing the calendar's premieres onto a month.

ONE JOB — turn this month's calendar items into that month's PREMIERE records,
and decide which of them the user has already said they are not watching. The
month document it merges into is the store's; what a premiere BECOMES afterwards
is the lifecycle's.

IT ALSO OWNS HOW A TURN-AWAY MARK IS RECOGNISED (matches_not_watching), and that
is now the ONLY direction there is. The tracker used to write marks too — giving
up on a season marked the show on the calendar, and a mark read back here became
a verdict — and both halves of that mirror are gone. A season is ended on the
tracker by the row's own controls, and a calendar mark reaches the tracker at
exactly one moment: a month being BUILT skips a title the viewer has turned away.
Reading a mark is an import-time question and nothing else.

WHICH PREMIERE A RECORD IS — a series premiere (a first season) or a season
premiere (a later one) — is decided HERE, once, by store.premiere_kind, at the
moment the record is made. They are two distinct sections of the month's first
notice, and deriving the split from the season number every time something renders
is how the two sections come to disagree.
"""
from __future__ import annotations

import asyncio

from ..calendar import state as calendar_state
from ..providers.base import Item, Media, collect_ids, resolve_key
from . import store
from .store import ADDED_BY_CALENDAR, load_month, normalize_show, record_key, save_month


def calendar_record(item: Item) -> dict:
    """An identity record from a normalized calendar item.

    The whole id map travels, not just the one the row ends up keyed on: an id
    dropped here is one a later cross-service match cannot use, and it was already
    paid for. `item.id` is deliberately not read — it is the calendar's DISPLAY id
    (a slug, usually), which is a different question from what identifies a title.
    """
    return {
        "media": Media.SHOW,
        "ids": collect_ids(item.ids),
        "title": str(item.title or ""),
        "season": int(item.season or 1),
        "network": str(item.network or ""),
    }


def matches_not_watching(rec: dict, nw_ids: set[str]) -> bool:
    """Whether the user has marked this title not-watching on the calendar.

    THE CALENDAR'S OWN RULE, CALLED RATHER THAN RESTATED. This used to be a
    second implementation — the record's slug and its Trakt id — and it was
    narrower than the one the grid uses in two ways that both showed up on a real
    instance: a mark spelled with Trakt's slug missed a card that had resolved to
    Simkl's description, and a mark spelled `lazarus` missed a title whose only
    match for it was Simkl's `tvdbslug`. Both were hidden on the calendar and
    imported onto the month anyway, which is the one direction a mark must never
    travel.

    THE KEY IS RESOLVED HERE because the record is not a card: `calendar_record`
    deliberately drops the display id, so there is nothing to pass as one, and
    the identity has to be computed from the id map the record does carry.
    """
    ids = rec.get("ids") or {}
    key = resolve_key(rec.get("media") or Media.SHOW, ids)
    return calendar_state.marked_by_ids(
        nw_ids, mark_key=str(key) if key is not None else "", ids=ids)


async def premiere_records(user_id: int, settings, year: int, month: int,
                           nw_ids: set[str] | None = None) -> list[dict]:
    """This month's calendar premieres split by rule: shows/new -> New (S01);
    shows/premieres minus shows/new -> Returning (S02+).

    Reads through the shared calendar cache (calendar_cache.read_month) rather
    than issuing a separate live Trakt call, so import stops duplicating a
    fetch the main calendar already made. Passing the importing user's own
    genre/country/show_certifications prefs into that read applies the same
    filters that already keep those shows off their calendar, so import can't
    hand back something they've personally filtered out and never got a chance
    to mark not-watching (it never appeared for them to mark in the first
    place). The instance-wide content floor still applies underneath this for
    free — it is enforced where the cache is populated, before any reader,
    including this one, ever sees the excluded show.

    `settings` IS THE INSTANCE'S, NOT THE IMPORTING VIEWER'S, and every caller
    owes that. A calendar window is fetched under the instance's own client id,
    stored once and served to everybody, so whose token is on the object decides
    nothing about what a month holds — while a viewer's token on it decides
    whether the fetch can happen at all. Handing this the per-viewer Settings the
    tracker builds for its history reads made importing a month hinge on whether
    that particular person had linked Trakt, which is how an account signed in
    with Simkl alone came to be refused a calendar the instance was reading
    perfectly well. The PREFS above are the importer's and stay so: which titles
    they may keep is theirs, who is asked is not.
    """
    from zoneinfo import ZoneInfo

    from .. import auth
    from ..calendar import cache as calendar_cache
    from ..calendar import vocab
    from ..endpoints import get_endpoint
    from ..providers.base import Media
    prefs = await auth.get_user_prefs(user_id)
    tz = ZoneInfo(settings.timezone)
    # BOTH ENDPOINTS ARE SHOW CALENDARS, so both read the importer's TV answers
    # — asked through the one function that knows which stored column governs
    # which medium rather than by naming columns here. The tracker holds shows
    # and nothing else, so the film side never comes into it.
    # `honour_pause` IS FALSE, AND THE ASYMMETRY IS THE POINT. Switching filters
    # off is a temporary look at a calendar — nothing is written, and turning
    # them back on undoes it completely. An import WRITES ROWS onto a month, and
    # a row does not come back off when the switch does: importing while paused
    # produced 85 rows of Italian game shows and Japanese anime this viewer
    # filters out, every one of which then had to be deleted by hand. So the
    # import reads the filters as they are STATED rather than as they are
    # currently being applied, which is the only reading where "show me
    # everything for a moment" cannot leave a mess behind.
    specs = vocab.active_specs(prefs, Media.SHOW, honour_pause=False)
    (new_items, _), (prem_items, _) = await asyncio.gather(
        calendar_cache.read_month(
            get_endpoint("shows/new"), settings, tz=tz, year=year, month=month,
            genres=specs["genres"], countries=specs["countries"],
            show_certifications=specs["show_certifications"],
        ),
        calendar_cache.read_month(
            get_endpoint("shows/premieres"), settings, tz=tz, year=year, month=month,
            genres=specs["genres"], countries=specs["countries"],
            show_certifications=specs["show_certifications"],
        ),
    )
    # A CALLER THAT DOES NOT PASS MARKS IS READING THE MONTH, NOT IMPORTING IT.
    # The roster this returns is also what the preview and the diff are built
    # from, and those describe the calendar rather than write to it; only the ADD
    # path owes the viewer their marks, and it passes them.
    marks = nw_ids or set()
    out: list[dict] = []
    new_keys: set[tuple[str, int]] = set()
    for item in new_items:
        record = _keyable(item, marks)
        if record is None:
            continue
        new_keys.add(_present_key(record))
        out.append(record)
    for item in prem_items:
        record = _keyable(item, marks)
        if record is None:
            continue
        if _present_key(record) in new_keys:
            continue  # this S01 premiere is already counted as a New Shows entry
        out.append(record)
    return out


def _keyable(item: Item, nw_ids: set[str]) -> dict | None:
    """The record for `item`, or None when it cannot go on a roster: no season to
    file it under, no shared id to file it by, or a mark saying the viewer has
    turned this title away. Skipped rather than raised — a calendar month is a
    list somebody else assembled, and one unusable entry in it is not a reason to
    fail the import.

    THE MARK IS ASKED OF THE ITEM AND NOT OF THE RECORD, and that is the whole
    repair. A mark may be stored under any spelling a title has ever been known
    by — the calendar accepts six of them — while a record's id map is
    `collect_ids`, an ALLOWLIST that keeps the ids the tracker files rows under
    and drops the rest. Three of those six (`traktslug`, `tvdbslug`, `mdlslug`)
    are among the dropped, so a record simply cannot answer this question:
    measured here, a viewer's Nocturne mark reads `lazarus`, which the card
    carries as Simkl's `tvdbslug` and the record does not carry at all. The
    calendar hid the show and the import added it, and no amount of comparing
    records could have found each other.

    So it is asked at the last moment the full id map exists, through the
    calendar's own predicate.
    """
    if item.season is None:
        return None
    if calendar_state.marked(nw_ids, item):
        return None
    record = calendar_record(item)
    try:
        record_key(record)
    except ValueError:
        return None
    return record


def _present_key(rec: dict) -> tuple[str, int]:
    return (str(record_key(rec)), int(rec["season"]))


async def add_premieres(doc: dict, present: set[tuple[str, int]], user_id: int, settings,
                        year: int, month: int, nw_ids: set[str]) -> int:
    """Append this month's premieres to `doc` as premiere records (skip existing +
    not-watching). Mutates `doc['shows']`/`present`; returns the number added."""
    added = 0
    # THE MARKS TRAVEL DOWN rather than being applied to what comes back: the
    # question needs the card's whole id map and a record no longer has one.
    for rec in await premiere_records(user_id, settings, year, month, nw_ids):
        key = _present_key(rec)
        if key in present:
            continue
        doc["shows"].append(normalize_show({
            **rec,
            "kind": store.premiere_kind(rec["season"]),
            "added_by": ADDED_BY_CALENDAR,
        }))
        present.add(key)
        added += 1
    return added


async def import_premieres(user_id: int, month_key: str, settings) -> dict | None:
    """Merge this month's calendar premieres into `user_id`'s OPEN month (skip
    existing + not-watching). Powers the manual "Import from calendar" action and
    the preview-month auto-populate. No-op on a missing/closed month."""
    doc = await load_month(user_id, month_key)
    if doc is None or doc.get("closed"):
        return doc
    year, month = store.parse_month_key(month_key)
    present = {_present_key(s) for s in doc.get("shows") or []}
    # Putting a title the user has turned away back onto a month is never the
    # right answer, so every mark filters this ADD path. What a turn-away means
    # for a row that ALREADY exists is a different question — it is a verdict on
    # that row rather than a reason never to write one — and it is answered where
    # such a row is acted on, not here where there is no row yet.
    nw_ids = await calendar_state.not_watching_ids(user_id)
    if await add_premieres(doc, present, user_id, settings, year, month, nw_ids):
        await save_month(user_id, doc)
    return doc
