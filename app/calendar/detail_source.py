"""Which service answers the detail modal for one card, and whether it can.

TWO ROUTES ASK THIS and they must agree: the signed-in calendar's /api/details
and the public share pages' own details endpoint. They differ in exactly one
thing — the public one never makes an outbound call — and everything else about
"who describes this title" is the same question. Written out at both, the two
would drift the first time either was touched, and the drift would be invisible:
each renders a modal that looks right on its own page.

THE CARD IS IDENTIFIED BY IDS, NOT BY A SERVICE NAME. A calendar group carries
the UNION of every id its records had (app/calendar/resolve.py hoists it), so a
title both services listed arrives here with both. The caller hands over what the
card carried and this decides who to ask; the alternative — the client naming a
service — puts the choice on the one side that cannot see whether that service's
credentials are filled in.

TRAKT IS PREFERRED WHEREVER IT HAS AN ID, INCLUDING ON A CARD ATTRIBUTED TO
SIMKL, and the asymmetry is deliberate rather than left over. The modal is about
the TITLE; the card's attribution is about whose listing put it on the page.
Trakt's per-title answer is strictly the larger one — it carries a cast, a
per-episode air date and a per-episode rating, none of which Simkl publishes
anywhere this app can reach — so preferring the card's own service would take
those away from a merged title for no gain. It DOES mean a viewer who set Simkl's
overview to win on the card can read Trakt's in the modal; that is a real
inconsistency and the smaller of the two, because the modal is one click away
from a source-flip control the card already has and the missing cast would not be
recoverable at all.
"""
from __future__ import annotations

from .. import providers
from ..providers.base import Source


def ports():
    """Every registered source that can describe a title, in DECLARED order.

    Declared order is the preference order — Trakt, then Simkl — for the reason
    the module docstring gives, and it comes from the registry rather than from a
    list here so a third source is a registration and not an edit.
    """
    return [(source, provider.detail_port)
            for source, provider in providers.registered().items()
            if provider.detail_port is not None]


def ids_from_query(params) -> dict[str, str]:
    """The id map a details request carried, read out of its query string.

    ONE PARAMETER PER SOURCE, NAMED FOR THAT SOURCE — `?trakt=203330&simkl=2601798`
    — rather than the `?id=` a single-source app could get away with. A card
    two services described has two ids and neither is "the" id; sending one plus
    a service name would put the choice on the client (see the module docstring),
    and sending one alone would throw away the fallback that makes a merged card
    open when one service's credentials are missing.

    Only names the registry knows are read, so a query string cannot ask this app
    to look a title up under a namespace no source issues.
    """
    return {str(source): value.strip()
            for source, _port in ports()
            if (value := params.get(str(source)) or "")}


def choose(settings, ids) -> tuple[Source, str] | None:
    """Who to ask about a title carrying `ids`, and the id to ask them by.

    `ids` is {namespace: value}, straight off the card. A source is asked by ITS
    OWN name in that map — `ids["trakt"]`, `ids["simkl"]` — because a service
    cannot look a title up by an id it does not issue.

    None means nobody can answer: either the card carries no id any registered
    source recognises, or the ones it carries belong to sources whose credentials
    this instance has not filled in. THE TWO ARE DELIBERATELY ONE ANSWER here and
    are told apart by the caller's own message, because the route wants to say
    "nothing here can describe this" either way and a modal is not the place to
    explain an operator's configuration.
    """
    for source, port in ports():
        value = str((ids or {}).get(str(source)) or "").strip()
        if not value:
            continue
        if not port.catalogue_configured(settings):
            continue
        return source, value
    return None


async def fetch(settings, source: Source, media, source_id: str,
                season: int | None, *, cache_only: bool = False) -> dict:
    """The chosen source's answer, in the one field set the modal renders.

    A separate step from `choose` so the public share route can decide who would
    answer and then ask them with `cache_only=True`, which is the only thing that
    differs between the two callers.
    """
    return await providers.get(source).detail_port.fetch_details(
        settings, media, source_id, season, cache_only=cache_only)
