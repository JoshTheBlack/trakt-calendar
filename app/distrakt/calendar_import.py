"""Bringing the calendar's premieres onto the roster.

ONE JOB — turn this month's calendar items into roster records, and decide which
of them the user has already said they are not watching. The month document it
merges into is the store's; what a premiere MEANS for rollover is the rollover's.
"""
from __future__ import annotations

import asyncio

from ..calendar import state as calendar_state
from ..providers.base import Item, Media, collect_ids
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

    The marks are keyed exactly as the calendar keys its own cards — the slug when
    the source gave one, else the source's own id — so this asks the same question
    in the same vocabulary rather than translating.
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
    """Append this month's premieres to `doc` (skip existing + not-watching).
    Mutates `doc['shows']`/`present`; returns the number added."""
    added = 0
    for rec in await premiere_records(user_id, settings, year, month):
        key = _present_key(rec)
        if key in present or matches_not_watching(rec, nw_ids):
            continue
        doc["shows"].append(normalize_show({**rec, "added_by": ADDED_BY_CALENDAR}))
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
    year, month = int(month_key[:4]), int(month_key[5:7])
    present = {_present_key(s) for s in doc.get("shows") or []}
    nw_ids = await calendar_state.not_watching_ids(user_id)
    if await add_premieres(doc, present, user_id, settings, year, month, nw_ids):
        await save_month(user_id, doc)
    return doc


async def is_calendar_premiere(user_id: int, month_key: str, settings,
                               key, season: int) -> bool:
    """Whether this month's calendar premieres include this title+season.

    Only for rows written before provenance was recorded (see MIGRATION_16 in
    app/db.py), where the question "did the calendar put this here" has no
    recorded answer: a show the premiere import would hand straight back is one
    the calendar is asserting, and removing it has to say something to the
    calendar or it cannot stick. Reads through the same cache import_premieres
    uses, which the page load that rendered the ✕ has just warmed.

    Answers False rather than raising if Trakt is unreachable: not knowing means
    not marking, which is the conservative half — the row simply comes back.
    """
    if not (settings and getattr(settings, "trakt_configured", False)):
        return False
    year, month = int(month_key[:4]), int(month_key[5:7])
    try:
        records = await premiere_records(user_id, settings, year, month)
    except Exception:  # noqa: BLE001 — a hiccup here must not fail the removal
        return False
    wanted = (str(key), int(season))
    return any(_present_key(r) == wanted for r in records)
