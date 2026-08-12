"""The Sources screen: which services answer for this account, and whose value
wins when two of them disagree.

THE SCREEN IS THE ONLY PLACE THESE ARE STATED. Everything it writes is read at
READ time over already-cached rows (app/calendar/resolve.py), so saving here
refetches nothing, expires nothing and invalidates nothing — which is why the
save route below touches no cache and must never be made to. A control that
quietly cost a month's refetch would be a control nobody could afford to try.

THREE THINGS ARE CHOSEN HERE AND THEY ARE NOT THE SAME QUESTION:

  WHICH SERVICES FILL THE CALENDAR — account-wide, and again per calendar. The
  per-calendar override exists because one service's movie listing is a global
  release calendar and its show listing is coverage worth having, which is two
  opposite answers about one service (app/sources/prefs.py's
  `calendar_selection` carries the measurement).
  WHICH SERVICES BACK THE TRACKER — separate, because reading somebody's own
  history needs their own token and the calendar needs nobody's.
  WHOSE VALUE WINS PER FIELD — a reordering, never a filter, so nothing chosen
  here can make a card emptier than the services made it.

THE FIELD NAMES ARE IMPORTED, NOT SPELLED. `app/calendar/resolve.py` owns the
vocabulary a precedence document is written in; a second list here would be a
second thing to keep in step with the record. The import is function-local, and
that placement is load-bearing rather than tidy — see the note beside it.
"""
from __future__ import annotations

from dataclasses import replace

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from . import prefs as source_prefs
from .. import auth, authz, chrome, providers
from ..auth import AuthLevel
from ..endpoints import endpoint_choices
from ..templating import templates

router = APIRouter()
guard = authz.Guard(router)

# The two group names a precedence entry may carry that are not a Record field
# spelled out. Everything else derives its label from the field name, so adding a
# field to the vocabulary gives it a row here without anybody editing a table —
# and a table of labels is exactly the thing that goes stale silently.
_FIELD_LABELS = {
    "coordinate": "Season and episode",
    "source": "Which service the card is from",
}

# The two vocabulary entries the screen deliberately does NOT offer, each with
# the reason it would be a control nobody can act on:
#   `genres` is a UNION, so a preference over it only reorders a list that keeps
#     every entry either way — there is no losing value to prefer away.
#   `airing` is the one field where the two services are not equally credible:
#     one of them dates an airing from a per-file fixed offset, which is only
#     right for titles airing in that file's own zone. Offering it would invite
#     somebody to choose the answer that is wrong for most of their calendar.
# They stay valid in a stored document — resolve reads them — so an account that
# has one keeps it; this screen simply does not hand anybody a new one.
_UNOFFERED_FIELDS = ("genres", "airing")


def _fields():
    """The precedence vocabulary, as (name, label) in the order it is offered.

    DEFERRED IMPORT, AND THE PLACEMENT IS THE POINT. The calendar reads this
    package (it asks a viewer's preference before it asks any source), so this
    package naming a calendar module at load time would close an import cycle.
    Inside the function it has not happened yet. tests/kernel/test_layering.py
    declares the edge as deferred and fails if it is ever hoisted.
    """
    from ..calendar import resolve

    return [(name, _FIELD_LABELS.get(name, name.replace("_", " ").capitalize()))
            for name in resolve.FIELDS if name not in _UNOFFERED_FIELDS]


def _selection_state(selection: str | None) -> dict:
    """One stored selection as the three things a control has to draw.

    `mode` is what the radio group is on: "inherit" (nothing stored for this
    calendar), "auto", or "named". `names` is the set of ticks, and it is
    populated even under `auto` so that turning the radio to "named" starts from
    something sensible rather than from nothing.

    A STORED `both` COMES BACK AS TWO TICKS, which is the whole reason this goes
    through `named_sources` rather than comparing strings: `both` is a legacy
    spelling of exactly {trakt, simkl} and reads as those two services forever,
    never as "all". Saving the screen afterwards writes the named set, so the
    value quietly becomes "trakt+simkl" with no change in what it does.
    """
    if selection is None:
        return {"mode": "inherit", "names": frozenset()}
    named = source_prefs.named_sources(selection)
    if named is None:
        return {"mode": "auto", "names": frozenset()}
    return {"mode": "named", "names": named}


def _by_name() -> dict:
    """The registry keyed by the name a preference stores, in declared order.

    Declared order and not alphabetical: it is the app's one statement of which
    service comes first, and every list on this screen has to agree with the
    order resolution actually reads them in.
    """
    return {str(source): provider for source, provider in providers.registered().items()}


def _reach(days: int | None) -> str:
    """How far a calendar reaches, in the unit somebody would say it in.

    None is "no declared bound", which is a different statement from a large
    number and must not be rendered as one — it is the answer for a source that
    publishes no window at all, and it is the reason this is not a subtraction.
    """
    if days is None:
        return "as far as you ask"
    if days >= 365:
        years = round(days / 365)
        return f"about {years} year{'' if years == 1 else 's'}"
    months = max(1, round(days / 30))
    return f"about {months} month{'' if months == 1 else 's'}"


def _source_rows(stored: source_prefs.SourcePrefs, linked: dict) -> list[dict]:
    """One row per registered service: what it is called, how far its calendar
    reaches, whether it can back the tracker, and who this account is linked to
    it as.

    Read off the registry rather than written out, so a third service appears
    here by being registered. `days_before`/`days_after` are the source's own
    declared reach — the honest answer to "why does this month look empty" — and
    None means it publishes no bound, which is a different statement from a big
    number.
    """
    calendar = _selection_state(stored.calendar_source)
    tracker = _selection_state(stored.tracker_source)
    rows = []
    for source, provider in providers.registered().items():
        name = str(source)
        rows.append({
            "name": name,
            "label": provider.label,
            "reach_before": _reach(provider.capabilities.days_before),
            "reach_after": _reach(provider.capabilities.days_after),
            "has_calendar": bool(provider.capabilities.endpoints),
            "tracks": provider.capabilities.private_user_data,
            "linked_as": linked.get(name),
            "calendar_ticked": name in calendar["names"],
            "tracker_ticked": name in tracker["names"],
        })
    return rows


@guard.get("/sources", AuthLevel.SESSION)
async def sources_page(request: Request):
    """The screen itself, rendered from what is stored rather than fetched from
    anywhere — nothing on this page costs a call to any service."""
    user = await auth.require_session(request)
    stored = await source_prefs.load(user.user_id)
    identities = await auth.list_identities(user.user_id)
    linked = {row["provider"]: row["display_name"] for row in identities}
    document = stored.precedence if isinstance(stored.precedence, dict) else {}
    stated = document.get("fields")
    stated = stated if isinstance(stated, dict) else {}
    return templates.TemplateResponse(request, "sources.html", {
        "request": request,
        **chrome.page_context(user),
        "sources": _source_rows(stored, linked),
        "calendar": _selection_state(stored.calendar_source),
        "tracker": _selection_state(stored.tracker_source),
        # WHICH SERVICES EACH CONTROL MAY OFFER. Naming a service for something
        # it cannot answer is legal and simply yields nothing — which from the
        # screen is indistinguishable from a service that answered and had
        # nothing to say, so the tick is withheld rather than offered and inert.
        "calendar_choices": [name for name, provider in _by_name().items()
                             if provider.capabilities.endpoints],
        "tracker_choices": [name for name, provider in _by_name().items()
                            if provider.capabilities.private_user_data],
        # Every calendar, each with what this account said about it — `None`
        # meaning "nothing", which the screen draws as "same as my calendar
        # source" and which saves nothing at all.
        "endpoints": [{
            "key": endpoint.key,
            "label": endpoint.label,
            "state": _selection_state((stored.endpoint_sources or {}).get(endpoint.key)),
            "sources": [name for name, provider in _by_name().items()
                        if provider.capabilities.answers(endpoint.key)],
        } for endpoint in endpoint_choices()],
        "precedence_default": document.get("default") or "",
        "precedence_fields": [
            {"name": name, "label": label, "chosen": stated.get(name) or ""}
            for name, label in _fields()
        ],
    })


def _selection_from(value, what: str) -> str:
    """One selection out of the request body, refused rather than coerced.

    A selection nobody can satisfy is a bug in the screen, and quietly rewriting
    it to `auto` would leave somebody looking at ticks that were never stored.
    The same rule `app/sources/prefs.py` applies to the column, said at the edge
    so the message can name the control instead of the column.
    """
    if not isinstance(value, str) or not source_prefs.is_selection(value):
        raise ValueError(f"{what} is not a source selection this app knows.")
    return value


def _precedence_from(data, valid_sources: set[str]) -> dict:
    """The precedence document out of the request body.

    An EMPTY STRING IS "no preference" AND REMOVES THE ENTRY, which is what the
    screen's own "leave it to the app" option posts. That is the difference
    between this and a validated selection: every other control here is a
    choice, and this one has a legitimate way to say nothing.

    An unknown field or an unknown service is REFUSED. `field_order` would
    ignore either at read — a document written by a newer version must not stop
    an older one rendering — but tolerating them on the way IN would let this
    screen store a preference that silently does nothing forever.
    """
    document = data.get("precedence")
    if document is None:
        return {}
    if not isinstance(document, dict):
        raise ValueError("The precedence settings are not shaped like settings.")
    fields = document.get("fields") or {}
    if not isinstance(fields, dict):
        raise ValueError("The per-field precedence settings are not shaped like settings.")
    known = {name for name, _label in _fields()}
    out: dict = {}
    default = document.get("default") or ""
    if default:
        if default not in valid_sources:
            raise ValueError(f"{default!r} is not a service this app has.")
        out["default"] = default
    chosen = {}
    for name, value in fields.items():
        if name not in known:
            raise ValueError(f"{name!r} is not something a preference can be stated about.")
        if not value:
            continue
        if value not in valid_sources:
            raise ValueError(f"{value!r} is not a service this app has.")
        chosen[str(name)] = str(value)
    if chosen:
        out["fields"] = chosen
    return out


@guard.post("/api/sources", AuthLevel.SESSION)
async def save_sources(request: Request):
    """Save whichever of the three controls the screen sent.

    A SECTION THE BODY DOES NOT MENTION IS LEFT ALONE, so the screen can save one
    part of itself without restating the rest — and so a control added later
    cannot be blanked by an older page that does not know about it.

    NOTHING HERE TOUCHES A CACHE, and that is the design's central claim rather
    than an omission. Resolution runs at read over rows filled without knowing
    who would read them, so a preference change is free: the next page load shows
    it and no service is asked anything. Clearing a window here would quietly
    turn every one of these controls into a refetch.
    """
    data = await authz.json_body(request)
    user = await auth.require_session(request)
    stored = await source_prefs.load(user.user_id)
    valid_sources = {str(source) for source in providers.registered()}
    try:
        updated = stored
        if "calendar_source" in data:
            updated = replace(updated, calendar_source=_selection_from(
                data["calendar_source"], "The calendar source"))
        if "tracker_source" in data:
            updated = replace(updated, tracker_source=_selection_from(
                data["tracker_source"], "The tracker source"))
        if "endpoint_sources" in data:
            overrides = data["endpoint_sources"]
            if not isinstance(overrides, dict):
                raise ValueError("The per-calendar sources are not shaped like settings.")
            keys = {endpoint.key for endpoint in endpoint_choices()}
            narrowed = {}
            for key, value in overrides.items():
                if key not in keys:
                    raise ValueError(f"{key!r} is not a calendar this app has.")
                # An absent value is how the screen says "same as my calendar
                # source": the entry is dropped rather than stored as a copy of
                # the account-wide answer, so changing that answer later moves
                # this calendar with it.
                if value:
                    narrowed[str(key)] = _selection_from(value, f"The source for {key}")
            updated = replace(updated, endpoint_sources=narrowed)
        if "precedence" in data:
            updated = replace(updated, precedence=_precedence_from(data, valid_sources))
        saved = await source_prefs.save(updated)
    except ValueError as exc:
        return authz.error(str(exc))
    # What was STORED, not what was sent: `both` arrives from an older page as
    # two ticks and leaves as "trakt+simkl", and a screen that re-rendered from
    # its own request would keep showing the value it no longer has.
    return JSONResponse({
        "ok": True,
        "calendar_source": saved.calendar_source,
        "tracker_source": saved.tracker_source,
        "endpoint_sources": saved.endpoint_sources,
        "precedence": saved.precedence,
    })
