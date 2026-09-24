"""What a record owes before it is filed, whichever way it reached the tracker.

A TITLE REACHES THE TRACKER SIX WAYS — a calendar import (and the month build that
runs one), a search add, a finished season stated by hand, a history prompt, the
history backfill and the month rollover — and each builds its record from what
its own source handed it. What they were handed differs: a calendar record and a
Trakt search hit state a network; a history event, a Simkl search hit and a Simkl
calendar record not yet enriched do not. A fact every record should carry cannot
be left to whichever path happens to have it, because the path that does not is
the row that renders without one for ever — nothing learns a network after the
row is written. So the filling is here, once, and every path calls it.

THE NETWORK, CHEAPEST ANSWER FIRST. The stored calendar holds a network for most
titles anybody has tracked — MEASURED on a live instance, 71 of the 80 titles its
backfill had filed with none — and reading it is one indexed query. Only what it
cannot answer costs `live.network_for`, one outbound call per show, which is the
call a history prompt already paid per click.
"""
from __future__ import annotations

import asyncio
import logging

from ..calendar import entries as calendar_entries
from . import live
from .store import record_key

logger = logging.getLogger(__name__)


async def fill_networks(settings, records: list[dict]) -> list[dict]:
    """`records` with every blank network answered where anything can answer it.

    A record that already states one keeps it: the path that built it had it
    from the title's own source. The rest are asked of the stored calendar first
    and of `live.network_for` for whatever it could not answer; a lookup that
    fails leaves the network blank rather than failing the write, because a row
    with no badge is worse than a row with one and better than no row.

    ONE ANSWER PER TITLE, so a backfill naming six seasons of one show asks once.
    Returns new dicts; the caller's are not mutated.
    """
    out = [dict(rec) for rec in records]
    blank = [rec for rec in out if not str(rec.get("network") or "").strip()]
    if not blank:
        return out
    by_key: dict[str, list[dict]] = {}
    for rec in blank:
        try:
            by_key.setdefault(str(record_key(rec)), []).append(rec)
        except ValueError:
            continue  # unkeyable: the write will refuse it anyway
    stored = await calendar_entries.known_about(by_key)
    answers = {key: stored_network(settings, recs[0], stored.get(key) or {})
               for key, recs in by_key.items()}
    residue = [key for key, network in answers.items() if not network]
    asked = await asyncio.gather(
        *(live.network_for(settings, by_key[key][0]) for key in residue),
        return_exceptions=True)
    for key, got in zip(residue, asked):
        # BaseException, not Exception: gather hands a cancelled lookup back as a
        # CancelledError, which is not an Exception and must not become a network.
        if isinstance(got, BaseException):
            logger.info("no network for %s: the lookup failed (%r)", key, got)
            continue
        answers[key] = got
    logger.info("tracker networks: %d title(s) owed one, %d answered from the stored "
                "calendar, %d asked for", len(by_key), len(by_key) - len(residue),
                len(residue))
    for key, recs in by_key.items():
        for rec in recs:
            rec["network"] = answers.get(key) or ""
    return out


def stored_network(settings, rec: dict, by_source: dict) -> str:
    """The stored calendar's network for `rec` (`by_source` is
    calendar.entries.known_about's answer for its title), from the service
    `network_for` would have asked where that service states one — the two can
    legitimately disagree (Simkl names who aired a show, Trakt who carries it),
    and the row should say what the rest of it is built from — else from
    whichever does. Also how naming.py repairs a row stored without one."""
    preferred = by_source.get(live.detail_source(rec, settings) or "")
    if preferred is not None and preferred.network:
        return preferred.network
    return next((title.network for title in by_source.values() if title.network), "")
