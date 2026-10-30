"""The tracker's own settings: what its dialog draws and what each control saves.

ONE JOB — the preferences that are about the TRACKER, in one place the dialog
(templates/_distrakt_settings.html) and its routes share. They used to sit on the
account page beside the account's own settings, which is the wrong neighbourhood:
a viewer who wants to change which service decides a season is looking at the
tracker when they want to, and an account page that mentions the tracker at all
is one an account without it should not read (the tracker is not advertised).
What stays on /me is what is about the ACCOUNT — the metadata order, whether
share links publish a watch history, and linking.

FOUR PREFERENCES, THREE STORES:
  - the default emoji (distrakt/prefs.py), which has a field of its own;
  - which linked tracker decides a season, whose stored numbers still count, and
    which catalogue a season's episode total is asked of (sources/prefs.py) —
    three lists of service names, each a REORDERING or a SET and never a filter.

EVERY CONTROL SAVES ITSELF AND IS ANSWERED WITH ITS OWN SECTION, drawn from the
same context the dialog is (`panel`), so what an answer draws is what is stored
and a refused save puts the control back without anything remembering which way
it was. I/O is the three verbs at the bottom; everything above them is a function
of values the caller holds.
"""
from __future__ import annotations

import dataclasses
from dataclasses import dataclass

from .. import auth, providers
from ..sources import prefs as source_prefs

# The two lists a press moves, by the name the dialog's buttons post and the field
# of SourcePrefs each one is stored in. A name that is not here is refused rather
# than read as one of them: a stale page posting a list this version no longer
# has must say so, not reorder something else.
ORDER_FIELDS = {
    "tracker_order": "tracker_priority",
    "season_totals": "season_totals_order",
}


@dataclass(frozen=True)
class Panel:
    """What the dialog draws, read once per answer."""
    prefs: source_prefs.SourcePrefs
    # Each list as the dialog offers it — {"source", "label"} entries in the order
    # they are currently asked in. EMPTY WHEN THERE IS NO ORDER TO STATE: with one
    # service answering, the control could only confuse.
    orders: dict[str, list[dict]]
    # Every tracker service, linked or not, with whether its stored numbers count.
    retirable: list[dict]


def _entries(names) -> list[dict]:
    return [{"source": name, "label": providers.registered()[name].label} for name in names]


def _linked_trackers(linked: set[str]) -> list[str]:
    """The tracker services this account has linked, in declared order. Only
    these are offered in the "which decides" list: the choice is between services
    that actually answer, and a service with no identity here decides nothing."""
    return [str(source) for source in providers.registered()
            if str(source) in providers.tracker_sources() and str(source) in linked]


def _askable_catalogues(settings) -> list[str]:
    """The catalogues this instance can be asked a season's length of, in declared
    order — a service whose catalogue credential is missing is not offered
    because naming it would be a preference nothing could act on."""
    return [str(source) for source, provider in providers.registered().items()
            if provider.detail_port is not None
            and provider.detail_port.catalogue_configured(settings)]


def build(prefs: source_prefs.SourcePrefs, linked: set[str], settings) -> Panel:
    """The dialog's context from the account's preferences, the services it has
    linked and the settings its catalogues are asked under.

    EACH LIST IS ORDERED THROUGH THE FUNCTION THAT ACTS ON IT
    (`SourcePrefs.tracker_order`, `totals_order`) rather than by reading the
    stored list here: a screen with its own reading of a preference could
    disagree with the code acting on it, and a viewer would have no way to tell
    which was lying. A press reorders what was drawn.

    THE RETIRED LIST IS NOT NARROWED TO WHAT IS LINKED, and narrowing it would
    remove the only row that matters: the service somebody wants to retire is
    almost always one they have ALREADY unlinked, which is what left its numbers
    stranded and every row amber in the first place. NOR TO SERVICES THAT HAVE
    CONTRIBUTED — that fact is the tracker's own watch state, and a switch over a
    service that contributed nothing simply does nothing.
    """
    trackers = _linked_trackers(linked)
    catalogues = _askable_catalogues(settings)
    return Panel(
        prefs=prefs,
        orders={
            "tracker_order": _entries(prefs.tracker_order(trackers)) if len(trackers) > 1 else [],
            "season_totals": (_entries(prefs.totals_order(catalogues))
                              if len(catalogues) > 1 else []),
        },
        retirable=[
            {"source": str(source), "label": provider.label,
             "linked": str(source) in linked, "counted": prefs.counts_tracker(source)}
            for source, provider in providers.registered().items()
            if str(source) in providers.tracker_sources()
        ],
    )


async def load(user_id: int, settings) -> Panel:
    """The dialog's context for `user_id`, from what is stored now."""
    linked = {row["provider"] for row in await auth.list_identities(user_id)}
    return build(await source_prefs.load(user_id), linked, settings)


async def move(user_id: int, settings, kind: str, source: str, direction: str) -> str:
    """Move `source` one place up or down in the list `kind` names, and save the
    whole list. Returns the sentence a refusal says, or "" when it moved.

    A MOVE, NOT THE WHOLE LIST FROM THE BROWSER. The server takes the list it
    would draw, moves the named service and stores the result, so the order a
    press starts from is the stored one rather than whatever another tab's page
    was left showing.

    STORED WHOLE rather than as "promote this one", because that is what it means:
    with a third service registered, "Simkl first" says nothing about the other
    two. `source_prefs.save` is what refuses a name this app has never heard of
    and one named twice.
    """
    field = ORDER_FIELDS.get(kind)
    if field is None:
        return "There is no such list."
    panel = await load(user_id, settings)
    names = [entry["source"] for entry in panel.orders[kind]]
    step = {"up": -1, "down": 1}.get(direction)
    if source not in names or step is None or not 0 <= names.index(source) + step < len(names):
        return "That service cannot move that way."
    at = names.index(source)
    names[at], names[at + step] = names[at + step], names[at]
    try:
        await source_prefs.save(dataclasses.replace(panel.prefs, **{field: names}))
    except ValueError as exc:
        return str(exc)
    return ""


async def set_retired(user_id: int, counted: dict[str, bool]) -> str:
    """State which services' stored numbers this account still counts. Returns the
    sentence a refusal says, or "" when it was saved.

    THE WHOLE SET IS THE ANSWER, like the orders and for the same reason: "retire
    this one" says nothing about the others. Every tracker service has to be
    mentioned — one the body does not name is a page that did not draw it, not a
    person who unticked it, and is refused rather than read as retired.

    NOTHING IS DELETED, HERE OR ANYWHERE THIS REACHES. The numbers stay where they
    are and the rows mark them "retired, not counted", because un-retiring has to
    be able to put them straight back. A FROZEN MONTH IS NOT TOUCHED AT ALL: what
    a month recorded is not a live claim.
    """
    trackers = [str(source) for source in providers.registered()
                if str(source) in providers.tracker_sources()]
    unknown = sorted(set(counted) - set(trackers))
    if unknown:
        return f"Unknown service: {', '.join(unknown)}."
    if any(name not in counted for name in trackers):
        return "Every service has to be stated. Reload the page and try again."
    prefs = await source_prefs.load(user_id)
    try:
        await source_prefs.save(dataclasses.replace(
            prefs, tracker_retired=[name for name in trackers if not counted[name]]))
    except ValueError as exc:
        return str(exc)
    return ""
