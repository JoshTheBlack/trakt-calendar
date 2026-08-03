"""Bringing the calendar's premieres onto a month.

ONE JOB — turn this month's calendar items into that month's PREMIERE records,
and decide which of them the user has already said they are not watching. The
month document it merges into is the store's; what a premiere BECOMES afterwards
is the lifecycle's.

IT ALSO OWNS THE TRACKER'S HALF OF THE CALENDAR'S TURN-AWAY VOCABULARY — how a
mark is recognised (matches_not_watching) and what id one is written under
(calendar_mark_id). Those two are the same fact read in opposite directions, and
they are stated together here so they cannot come to disagree about what a
calendar card is called.

WHICH PREMIERE A RECORD IS — a series premiere (a first season) or a season
premiere (a later one) — is decided HERE, once, by store.premiere_kind, at the
moment the record is made. They are two distinct sections of the month's first
notice, and deriving the split from the season number every time something renders
is how the two sections come to disagree.
"""
from __future__ import annotations

import asyncio

from ..calendar import state as calendar_state
from ..providers.base import Item, Media, collect_ids
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


def calendar_mark_id(rec: dict) -> str:
    """The id the MAIN CALENDAR would key this title's card by: the slug when the
    source gave one, else the source's own id. "" when it can name neither.

    THIS IS NOT THE ID THE TRACKER FILES THE RECORD UNDER. A record is keyed by
    whichever shared id the identity waterfall picked (store.record_key), and a
    turn-away written in those terms would silently match no card at all. Both
    directions of the calendar's marks are stated here, once, so the id a mark is
    written under and the ids a mark is recognised by cannot drift apart.
    """
    ids = rec.get("ids") or {}
    return str(ids.get("slug") or "") or str(ids.get("trakt") or "")


def matches_not_watching(rec: dict, nw_ids: set[str]) -> bool:
    """Whether the user has marked this title not-watching on the calendar.

    BOTH ids are asked about rather than just calendar_mark_id's answer, because a
    mark already in the store was written under whichever id the card carried at
    the time: a title that has since gained a slug would stop matching a mark made
    before it had one, and the show would quietly come back.
    """
    ids = rec.get("ids") or {}
    return str(ids.get("slug") or "") in nw_ids or str(ids.get("trakt") or "") in nw_ids


async def premiere_records(user_id: int, settings, year: int, month: int) -> list[dict]:
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
    """
    from zoneinfo import ZoneInfo

    from .. import auth
    from ..calendar import cache as calendar_cache
    from ..endpoints import get_endpoint
    prefs = await auth.get_user_prefs(user_id)
    tz = ZoneInfo(settings.timezone)
    (new_items, _), (prem_items, _) = await asyncio.gather(
        calendar_cache.read_month(
            get_endpoint("shows/new"), settings, tz=tz, year=year, month=month,
            genres=prefs["genres"], countries=prefs["countries"],
            show_certifications=prefs["show_certifications"],
        ),
        calendar_cache.read_month(
            get_endpoint("shows/premieres"), settings, tz=tz, year=year, month=month,
            genres=prefs["genres"], countries=prefs["countries"],
            show_certifications=prefs["show_certifications"],
        ),
    )
    out: list[dict] = []
    new_keys: set[tuple[str, int]] = set()
    for item in new_items:
        record = _keyable(item)
        if record is None:
            continue
        new_keys.add(_present_key(record))
        out.append(record)
    for item in prem_items:
        record = _keyable(item)
        if record is None:
            continue
        if _present_key(record) in new_keys:
            continue  # this S01 premiere is already counted as a New Shows entry
        out.append(record)
    return out


def _keyable(item: Item) -> dict | None:
    """The record for `item`, or None when it cannot go on a roster: no season to
    file it under, or no shared id to file it by. Skipped rather than raised —
    a calendar month is a list somebody else assembled, and one unusable entry in
    it is not a reason to fail the import."""
    if item.season is None:
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
    for rec in await premiere_records(user_id, settings, year, month):
        key = _present_key(rec)
        if key in present or matches_not_watching(rec, nw_ids):
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
