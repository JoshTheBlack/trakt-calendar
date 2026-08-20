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

from typing import NamedTuple

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


class Answerer(NamedTuple):
    """Who to ask about one title, and whether they may only be asked of what is
    already stored here.

    `stored_only` is not a caller's policy — the share pages have one of those
    and pass `cache_only` themselves. It is a fact about this instance: no
    registered source that knows this title can be REACHED right now, so the only
    answer available is the one already on disk.
    """
    source: Source
    source_id: str
    stored_only: bool


def choose(settings, ids) -> Answerer | None:
    """Who to ask about a title carrying `ids`, and the id to ask them by.

    `ids` is {namespace: value}, straight off the card. A source is asked by ITS
    OWN name in that map — `ids["trakt"]`, `ids["simkl"]` — because a service
    cannot look a title up by an id it does not issue.

    A SOURCE THAT CANNOT BE REACHED IS STILL WORTH ASKING OF THE LOCAL STORE,
    which is the second pass below. Every field a modal draws was cached the last
    time anybody opened that title, and reading it back needs no credential and
    makes no request — so an instance that has lost a client id can still
    describe what it has already described once, exactly as a public share page
    does for a visitor who may never spend anything. Blanking the modal instead
    threw away data this instance was holding, which is the same answer 13.2's
    roster rows now refuse to give.
    THE PREFERENCE ORDER IS STILL WHOLE-PASS, not per source: a source that can
    be reached beats a stored answer from ANY source, because a live answer is
    complete and a stored one may be a fragment.

    None means nobody can answer at all: the card carries no id any registered
    source recognises. A caller that gets an Answerer with `stored_only` and then
    finds nothing behind it is the other empty case, and only it can tell —
    reading the store is the only way to find out.
    """
    known = [(source, value) for source, port in ports()
             if (value := str((ids or {}).get(str(source)) or "").strip())]
    for source, value in known:
        if providers.get(source).detail_port.catalogue_configured(settings):
            return Answerer(source, value, stored_only=False)
    for source, value in known:
        return Answerer(source, value, stored_only=True)
    return None


async def describe(settings, chosen: Answerer, media, season: int | None, *,
                   cache_only: bool = False) -> dict | None:
    """The chosen source's answer, or None when there is genuinely nothing to
    show — the whole "who answers and did they" step, in one call.

    IT EXISTS SO THE EMPTY CASE IS DECIDED ONCE. A stored-only answerer is a
    guess that this instance has described the title before; whether it actually
    has can only be found out by reading, and what a caller does about an empty
    read — refuse, rather than open a modal with nothing in it — is a rule both
    modals have to follow identically or the same title behaves differently
    depending on which page it was clicked from.

    `cache_only` is the CALLER's own restriction (the public pages never spend
    the owner's budget) and is ORed with the answerer's, which is a fact about
    the instance. Neither overrides the other; both mean "do not make a call".
    """
    details = await fetch(settings, chosen.source, media, chosen.source_id, season,
                          cache_only=cache_only or chosen.stored_only)
    if chosen.stored_only and not _says_anything(details):
        return None
    return details


def _says_anything(details: dict) -> bool:
    """Whether a stored answer has enough in it to be worth drawing.

    A CACHE-ONLY READ THAT MISSED STILL ANSWERS THE FULL KEY SET, empty — that is
    DetailPort's own contract, so the renderer never sees a missing key — which
    means "nothing was stored" and "this title has nothing to say" arrive
    identically and the only way to tell is to look at the values. These three
    are what a modal is FOR: what it is called, what it is about, and what is in
    it. A card with none of them is a blank the reader cannot act on.
    """
    return bool(details.get("title") or details.get("overview") or details.get("episodes"))


async def fetch(settings, source: Source, media, source_id: str,
                season: int | None, *, cache_only: bool = False) -> dict:
    """The chosen source's answer, in the one field set the modal renders.

    A separate step from `choose` so the public share route can decide who would
    answer and then ask them with `cache_only=True`, which is the only thing that
    differs between the two callers.
    """
    return await providers.get(source).detail_port.fetch_details(
        settings, media, source_id, season, cache_only=cache_only)
