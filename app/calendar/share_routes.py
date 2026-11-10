"""Public calendar sharing: the read-only /s/, /u/, /c/ pages, and the
owner-facing API the calendar's Share panel calls to manage them.

The three public routes do the SAME read as the authenticated calendar —
calendar_cache.read_month, the per-(endpoint, 7-day window) cache, normalized
into a viewer's timezone the same way — with the CALENDAR's fetch branch
permanently switched off: a public request serves whatever month is cached —
even stale, even nothing — and can never make this instance fill a window. A
visitor is never given a session, and nothing here writes anything on their
behalf.

TWO PUBLIC SURFACES MAY MAKE A BOUNDED OUTBOUND CALL, and both are bounded to
what the link ALREADY SHOWS rather than to a flag their callers pass: the
preview picture warms artwork for the titles its own grid draws (`assemble_card`),
and the details modal describes a title the owner's cached month contains
(`_describe_shared_title`). Neither can be reached with an identifier the link
does not already publish, which is the property that keeps a public URL from
turning a stranger's input into a call on the owner's credential.

A miss is identical whatever the reason — an unknown token, a disabled
account, or a retired username/slug — so a share link can never be used to
probe which of those three it is.
"""
from __future__ import annotations

import asyncio
import calendar as _calendar
import dataclasses
import json
import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from itertools import groupby
from typing import NamedTuple
from urllib.parse import quote, urlencode
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import anyio
import anyio.to_thread
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, PlainTextResponse, RedirectResponse, Response

from . import (cache as calendar_cache, resolve as calendar_resolve,
               share_card, share_card_cache, share_code, share_links,
               state as calendar_state, vocab)
from .. import (after_response, auth, authz, chrome, clock, details_panel, perftrace,
                route_params, templating)
from ..auth import AuthLevel
from ..authz import Guard
from ..config import load_settings
from ..endpoints import DEFAULT_ENDPOINT, ENDPOINTS, Endpoint, endpoint_choices, get_endpoint
from ..media import artwork, logos, posters, user_images
from ..perftrace import span
from ..metadata import identity as title_identity
from ..metadata import panel as metadata_panel
from ..providers import detail_source
from ..providers.base import (Item, Media, Source, SourceUnavailable,
                              parse_item_key, parse_media)
from ..sources import prefs as source_prefs
from ..timezones import build_options as build_timezone_options
from ..templating import templates

logger = logging.getLogger(__name__)

router = APIRouter()
guard = Guard(router)

# One definition, in the module that also builds the links carrying these
# values, so a link can never offer a view this page would reject.
_CARD_STYLES = share_links.CARD_STYLES
_DAY_PACKINGS = share_links.DAY_PACKINGS

# Purely anti-scrape — these pages never touch Trakt, so this is not protecting
# a rate-limited upstream, just keeping a bot from hammering the cache reads.
SHARE_RATE_MAX_ATTEMPTS = 120
SHARE_RATE_WINDOW_SECONDS = 60

# Query params a share request may carry, kept here so the view-option resolvers
# below and the "carry these into the month-nav links" helper agree on the set.
_CARRY_PARAMS = ("card", "packing", "hidenw", "tz", "networks", "endpoint", "source")


# ---------------------------------------------------------------------------
# small page-local helpers
# ---------------------------------------------------------------------------

def _carry_query(request: Request) -> str:
    parts = [
        f"{key}={quote(value)}" for key in _CARRY_PARAMS
        if (value := request.query_params.get(key))
    ]
    return ("&" + "&".join(parts)) if parts else ""


# ---------------------------------------------------------------------------
# view-option resolution — request param -> owner's share_links default -> app
# default, whitelisted at every tier, never erroring on an invalid value
# ---------------------------------------------------------------------------
# EVERY RESOLVER BELOW TAKES A PLAIN MAPPING OF PARAMS RATHER THAN THE REQUEST,
# because the page and the share card do not read them from the same place: the
# page reads the query string, while the card reads a compact `p=` code expanded
# in place into a dict (a page REDIRECTS to the expanded form instead, which is
# the wrong move for an image an unfurler fetches once). Taking the mapping is
# what lets both surfaces ask these functions the same question and get the same
# answer — see `resolve_view`.

_Params = Mapping[str, str]


def _resolve_endpoint(params: _Params, share_row, settings) -> Endpoint:
    def _valid(key):
        return key if key in ENDPOINTS else None

    key = (
        _valid(params.get("endpoint"))
        or _valid(share_row["endpoint"])
        or _valid(settings.endpoint)
        or DEFAULT_ENDPOINT
    )
    return get_endpoint(key)


def _resolve_choice(value, share_default, app_default, choices):
    for candidate in (value, share_default, app_default):
        if candidate in choices:
            return candidate
    return choices[0]


def _resolve_hide_not_watching(params: _Params, share_row, settings) -> bool:
    raw = params.get("hidenw")
    if raw in ("0", "1"):
        return raw == "1"
    if share_row["hide_not_watching"] is not None:
        return bool(share_row["hide_not_watching"])
    return bool(settings.hide_not_watching)


def _resolve_networks(params: _Params, share_row, settings) -> list[str] | None:
    if "networks" in params:
        names = [n.strip() for n in (params.get("networks") or "").split(",") if n.strip()]
        return names or None
    stored = json.loads(share_row["network_filter_json"] or "[]")
    if stored:
        return stored
    return list(settings.network_filter or []) or None


def _resolve_source(params: _Params) -> str | None:
    """Which services this request asks the month be filled from, or None for
    "whatever the owner's own preference says".

    WHITELISTED HERE, HONOURED ELSEWHERE, and the split is deliberate. All this
    can say is that the value is a selection the app can act on at all
    (`is_selection` — the same predicate the calendar's own `?source=` passes
    through, and the same one that guarded it on the way into the link). Whether
    it may actually be acted on for THIS share depends on two things a resolver
    taking nothing but params does not have: what the owner's preference already
    admits, and what the instance still puts on its calendar. `_narrowed_prefs`
    owns that, once, where both surfaces reach it.

    Anything unusable reads as None rather than as an error, like every other
    resolver here: these arrive from strangers editing URLs.
    """
    requested = (params.get("source") or "").strip()
    return requested if source_prefs.is_selection(requested) else None


def _resolve_owner_tz(share_row, settings) -> ZoneInfo:
    """The owner's default timezone: their share-specific override if they set
    one, else their account's own saved timezone, else the app-wide default."""
    return clock.first_zone(share_row["timezone"], share_row["owner_account_timezone"],
                            settings.timezone)


def _resolve_tz(params: _Params, share_row, settings) -> ZoneInfo:
    requested = (params.get("tz") or "").strip()
    if requested:
        try:
            return ZoneInfo(requested)
        except (ZoneInfoNotFoundError, ValueError):
            pass  # invalid -> owner default, never an error
    return _resolve_owner_tz(share_row, settings)


@dataclass(frozen=True)
class ShareView:
    """Which view of a shared calendar a request is asking for, after every
    fallback tier has been applied.

    A VALUE, NOT A REQUEST. Once this exists, "what is being looked at" is
    settled and nothing downstream needs the query string again — which is the
    point, because the same shared month is rendered by two different surfaces
    (the HTML page and its preview card) and a card advertising a count the page
    does not show is worse than no card at all. One resolver, one answer, nothing
    to drift.

    `card_style` and `day_packing` are page layout and mean nothing to the card;
    everything else changes WHICH AIRINGS are in the view and therefore changes
    both.

    `source` IS THE ONE FIELD THAT IS A REQUEST RATHER THAN AN ANSWER, and it
    says so here so nobody reads it as one. It is the selection this request
    NAMES, already whitelisted; whether the month is actually narrowed to it is
    settled by `_narrowed_prefs` inside `_read_month`, because that answer needs
    the owner's own preference and the instance's admitted set. Carrying the
    request on the view is still what makes the two surfaces agree: both reach
    that one decision through this one value.
    """
    year: int
    month: int
    endpoint: Endpoint
    tz: ZoneInfo
    hide_not_watching: bool
    network_filter: list[str] | None
    card_style: str
    day_packing: str
    source: str | None = None

    @property
    def month_label(self) -> str:
        return _calendar.month_name[self.month]


def resolve_view(params: _Params, share_row, settings) -> ShareView:
    """The one place a share request's view options are worked out.

    `params` is whatever the caller has already established the request means —
    the raw query string for a page, a `p=` code expanded in place for the card.
    Every value is whitelisted on the way through and an unusable one falls back
    rather than raising, because these arrive from strangers editing URLs.
    """
    today = clock.today()
    return ShareView(
        year=route_params.valid_year(params.get("year"), today.year),
        month=route_params.valid_month(params.get("month"), today.month),
        endpoint=_resolve_endpoint(params, share_row, settings),
        tz=_resolve_tz(params, share_row, settings),
        hide_not_watching=_resolve_hide_not_watching(params, share_row, settings),
        network_filter=_resolve_networks(params, share_row, settings),
        card_style=_resolve_choice(
            params.get("card"), share_row["card_style"], settings.card_style, _CARD_STYLES,
        ),
        day_packing=_resolve_choice(
            params.get("packing"), share_row["day_packing"], settings.day_packing, _DAY_PACKINGS,
        ),
        source=_resolve_source(params),
    )


# ---------------------------------------------------------------------------
# the never-fetch read + render
# ---------------------------------------------------------------------------

def _not_found(request: Request) -> Response:
    return templates.TemplateResponse(request, "share_not_found.html", {"request": request}, status_code=404)


def _narrowed_prefs(prefs, view: ShareView, settings):
    """The owner's source preference, narrowed to the services this request
    named — or the preference untouched, whenever that narrowing cannot be
    honoured.

    IT CAN ONLY EVER NARROW, NEVER WIDEN, and that is the whole of how a
    per-link source sits with the rule that a share page renders the OWNER's
    source preference. A link is the owner saying "open this one on Simkl", the
    same kind of statement as "open this one on the premieres calendar" — but
    the set it picks from is the set their own calendar already shows. Honouring
    a widening would put back exactly the defect that rule was written to fix:
    an owner who narrowed their calendar to one service handing strangers a link
    showing the titles they narrowed out. It is also what makes the value's
    arrival by query string harmless, and it does arrive that way — a `p=` code
    expands into the address bar, so `source` is a param a visitor can retype,
    and the most a retyped one can do is show them a subset of a page they were
    already being shown.

    THE INTERSECTION IS TAKEN AGAINST BOTH ADMISSIONS, the owner's and the
    operator's, and the second is what answers "the instance switched that
    service off after the link went out". The named services simply fall out of
    the intersection, nothing is left to narrow to, and the month renders on the
    owner's preference — a link degrading to the default rather than to an error
    page, which is how an unreadable `p=` and an unknown timezone already behave
    on these routes. NEVER A REFUSAL AND NEVER AN EMPTY MONTH: narrowing to a
    service the calendar cannot fill would render a blank page that says nothing
    about why.

    `auto` names no services at all — it is "whatever there is" — so it is not a
    narrowing of anything and is left alone here, resolving as the owner's
    preference does.
    """
    named = source_prefs.named_sources(view.source or "")
    if named is None:
        return prefs
    instance = calendar_resolve.instance_sources(settings)
    surviving = [
        str(source) for source in Source
        if str(source) in named
        and prefs.admits_calendar(source)
        and (instance is None or str(source) in instance)
    ]
    if not surviving:
        return prefs
    # The per-endpoint overrides go with it, for the reason the calendar's own
    # `?source=` clears them: a link that says "open this on Simkl" has to mean
    # that on the calendar it opens, and a stored override for that endpoint
    # would quietly win over the thing the link asked for.
    return dataclasses.replace(
        prefs, calendar_source=source_prefs.SEPARATOR.join(surviving),
    )


async def _read_month(view: ShareView, settings, owner_prefs,
                      owner_id: int) -> tuple[list[Item], int | None]:
    """The month a share request is asking for, out of the local cache and only
    the local cache.

    `allow_fetch=False` is the whole point of every public share surface: a
    visitor is served whatever is already cached — even stale, even nothing —
    and never triggers a Trakt call that would spend the owner's rate-limit
    budget on an anonymous request. An instance with no calendar source has
    nobody to ask at all, and reads as an empty month rather than raising.

    The prefs come from the OWNER's account rather than from the URL, exactly as
    they do for the owner's own calendar: genres, countries, certifications and
    the films-calendar release narrowing are the owner's editorial choices about
    their calendar, not view options a visitor gets to set.

    WHICH SERVICES ANSWER IS THE OWNER'S TOO, and it is read HERE rather than by
    each caller, which is what keeps the count invariant. A share link's page and
    its preview picture both come through this function, so they cannot resolve a
    month under different preferences however either of them changes — a card
    advertising a count the page does not show is exactly the failure a second
    read of the same fact would produce. Without this the two surfaces admitted
    every source the stored rows held while the owner's own calendar admitted
    what they asked for, so a link could show a stranger titles the owner had
    narrowed their calendar to exclude.

    A LINK MAY NARROW THAT FURTHER — see `_narrowed_prefs`, which is applied here
    for the same one-place reason the preference is loaded here.
    """
    if not settings.calendar_source_configured:
        return [], None
    return await calendar_cache.read_month(
        view.endpoint, settings, tz=view.tz, year=view.year, month=view.month,
        # THE OWNER'S FILTERS FOR THIS CALENDAR'S MEDIUM, and `honour_pause` is
        # False on purpose: the stash is a private look at your own calendar, so
        # an owner who switched their filters off for the afternoon does not
        # silently publish a wider calendar to everyone holding their link.
        **{k: v for k, v in vocab.active_specs(
            owner_prefs, view.endpoint.media, honour_pause=False).items()
           if k != "network_filter"},
        # The view's own networks rather than the owner's, because a link may
        # narrow further than the calendar it was made from.
        network_filter=view.network_filter,
        prefs=_narrowed_prefs(
            (await source_prefs.load(owner_id)).for_media(view.endpoint.media),
            view, settings),
        allow_fetch=False,
    )


def _visible_items(items: Sequence[Item], view: ShareView, not_watching: set[str]) -> list[Item]:
    """The airings this view actually shows.

    The owner's marks are a SET alongside the items rather than a field copied
    onto each one — one answer to "is this marked" instead of two spellings of
    it, and no per-item copy of a dataclass that is deliberately awkward to copy.

    THE SET IS EXPANDED HERE because this page does not go through
    `assemble_range`'s meta: a stored mark may name a title by whichever id the
    owner's card carried when they made it, and `marked_keys` is the one rule
    that translates that. Both the page and its preview picture read through
    this function, so they cannot come to different answers about what the owner
    is not watching — which is the same guarantee `_read_month` gives them about
    what the month contains.
    """
    marks = calendar_state.marked_keys(not_watching, items)
    return [item for item in items
            if not (view.hide_not_watching and item.mark_key in marks)]


def _effective_params(request: Request) -> dict[str, str]:
    """What a share request is actually asking for: a `?p=` code expanded back
    into ordinary params, with anything the visitor spelled out themselves
    winning over the same param inside the code.

    ONE implementation of that precedence, because two surfaces need the answer
    and they do different things with it. The PAGE redirects to the expanded URL,
    so from the first paint the URL a visitor can edit, bookmark and re-share is
    the plain one and the month arrows need know nothing about the short form.
    The PICTURE decodes in place and gets on with it: an unfurler fetches it once
    and never edits it, so a redirect would be a wasted round trip at best.
    Two copies of a precedence rule drift; this is the rule.
    """
    params = share_code.decode(request.query_params.get("p"))
    for key, value in request.query_params.multi_items():
        if key != "p":
            params[key] = value
    return params


def _expanded_from_code(request: Request) -> str | None:
    """Where a `?p=` request should go instead, or None when there is no code.

    A code is only how a link is HANDED OUT; on arrival the page sends the
    visitor to the plain form of the same request (see `_effective_params`).
    `p` never survives the redirect, even when it decoded to nothing.
    """
    if "p" not in request.query_params:
        return None
    query = urlencode(_effective_params(request))
    return f"{request.url.path}?{query}" if query else request.url.path


def _owner_label(share_row) -> str:
    """Who this page's <title> and its link-preview text say the calendar
    belongs to.

    THE OWNER'S CHOSEN NAME (`users.display_name`), OR NOBODY. That is one
    specific one of the three things this app calls a display name: not the admin
    screen's label for an account, which is the USERNAME on purpose because an
    admin types it back to confirm a deletion and it therefore has to be unique
    (see app/auth/admin.py), and not a linked provider's name either. This is the
    free text the owner typed on their own account page — the only one of the
    three they chose in order to be seen under, and the only one it is theirs to
    publish.

    THE USERNAME IS NOT A FALLBACK, deliberately. It is a sign-in identifier the
    owner never chose to publish, and everything else about a share is built to
    name nobody who did not ask to be named: the picture draws no name at all,
    and its URL is the token form even from a /u/ or /c/ link so the markup
    carries no identifier. Embed text is the one artefact that gets pasted into
    other people's channels, so repeating the username there would undo that in
    the one place it counts.

    NO CHOSEN NAME MEANS NO NAME, NOT A GAP. The nameless branch gives the page a
    subject of its own rather than leaving the title to trail off into a dangling
    possessive or a stray separator — both strings below stand on their own, and
    which branch produced which is meant to be readable from here.

    A blank or whitespace-only name counts as unset: `auth.normalize_display_name`
    is the same trimming the account page stores through, so a value written
    before that rule existed reads here exactly as one written after it.
    """
    chosen = auth.normalize_display_name(share_row["owner_display_name"])
    if chosen is None:
        return "Shared calendar"
    return chosen


async def _render(request: Request, share_row) -> Response:
    if share_row is None:
        return _not_found(request)

    # After the miss above, so an unusable link is still a 404 on the spot rather
    # than a redirect that then 404s.
    if target := _expanded_from_code(request):
        return RedirectResponse(target, status_code=302)

    settings = await load_settings()
    owner_id = int(share_row["user_id"])
    owner_prefs = await auth.get_user_prefs(owner_id)

    view = resolve_view(request.query_params, share_row, settings)

    # The page's own three phases, timed separately because they fail slowly for
    # three unrelated reasons — a large month to inflate, a database under
    # contention, and a template proportional to the number of airings — and a
    # single total for the request cannot tell them apart.
    with span("share.read_month", ym=f"{view.year}-{view.month:02d}",
              endpoint=view.endpoint.key) as sp:
        items, as_of = await _read_month(view, settings, owner_prefs, owner_id)
        sp.set(items=len(items))
    with span("share.not_watching"):
        nw_ids = await calendar_state.not_watching_ids(owner_id)
    visible = _visible_items(items, view, nw_ids)

    # Start resolving the artwork this month's preview picture will want, and do
    # not wait for it: a crawler fetches the page first and the picture second,
    # so by the time anyone asks for the card the posters are usually already on
    # disk.
    _spawn_poster_warm(view, owner_id, visible, settings,
                       _poster_order(await source_prefs.load(owner_id)))

    grouped = [
        {"date": day, "label": datetime.strptime(day, "%Y-%m-%d").strftime("%A, %d %B"), "items": list(rows)}
        for day, rows in groupby(visible, key=lambda i: i.air_date)
    ]
    as_of_label = datetime.fromtimestamp(as_of, tz=view.tz).strftime("%Y-%m-%d %H:%M %Z") if as_of else None

    # Open Graph tags for link unfurlers (Discord/Slack/etc.). Both URLs are
    # absolute and built only from the configured public_base_url — never the
    # request Host — since an unauthenticated crawler resolves relative paths
    # unreliably and a spoofed Host header must not become the advertised
    # origin. Absent a configured base there is nothing safe to advertise, so
    # the tags are simply omitted and the link falls back to a bare text preview.
    base = _public_base(settings)
    og_image = _card_url(view, share_row, base)
    og_url = None
    if base:
        urls = share_links.share_urls(share_row, share_row["owner_username"], base)
        og_url = urls.get(share_row["preferred_kind"]) or next((u for u in urls.values() if u), None)

    context = {
        "request": request,
        # TWO NAMES FOR THE OWNER, AND THEY ARE NOT INTERCHANGEABLE. The label is
        # what the <head> is allowed to say — see `_owner_label` for why the
        # username is not a candidate there. The username itself is still what
        # the page's own body shows a visitor who deliberately opened the link.
        "owner_label": _owner_label(share_row),
        "owner_username": share_row["owner_username"],
        "og_image": og_image,
        # Told to the unfurler rather than left to be guessed: without them Slack
        # and Discord size the picture themselves, and one that guesses wrong
        # renders a thumbnail instead of the wide card the tags claim. Read off
        # the renderer, so a canvas that changes size cannot leave the markup
        # advertising the old one.
        "og_image_w": share_card.CARD_W,
        "og_image_h": share_card.CARD_H,
        "og_url": og_url,
        "year": view.year,
        "month": view.month,
        "month_label": view.month_label,
        "nav": route_params.adjacent_months(view.year, view.month),
        "grouped": grouped,
        # AS MARK KEYS, matching what the template asks — the raw stored set
        # may spell a title by whichever id the owner's card carried when
        # they marked it. Same translation `_visible_items` made above.
        "not_watching": calendar_state.marked_keys(nw_ids, items),
        "total": len(visible),
        "view": {"card_style": view.card_style, "day_packing": view.day_packing},
        # Carried through this page's own controls rather than offered by one:
        # the visitor sets how the month is DRAWN, never which services fill it.
        "source": view.source,
        "as_of": as_of_label,
        "query_extra": _carry_query(request),
        # WHERE A CARD ASKS FOR ITS DETAILS PANEL, and what it has to say about
        # the view to be allowed an answer. The endpoint lives beside whichever
        # share URL this page was reached by (/s/<token>, /u/<name>, /c/<slug>),
        # so it is derived from the path rather than named three times; and the
        # page's own query string rides along verbatim because the bound is to
        # what THIS view shows and that cannot be evaluated without the view.
        # Verbatim rather than picked apart: a view option added later then needs
        # no second edit here, and a `?p=` code has already been expanded by the
        # redirect on arrival, so what is echoed is the plain form either way.
        "details_base": request.url.path.rstrip("/") + "/details",
        "view_query": ("&" + request.url.query) if request.url.query else "",
        # The visitor's own view controls. Everything they drive is a GET with
        # the same whitelisted params a hand-edited URL already carries, so they
        # add no write surface and need no session — they just save the visitor
        # from editing the query string by hand.
        "endpoints": endpoint_choices(),
        "endpoint_key": view.endpoint.key,
        "card_styles": _CARD_STYLES,
        "day_packings": _DAY_PACKINGS,
        "hide_not_watching": view.hide_not_watching,
        "timezone_groups": build_timezone_options(view.tz.key),
        # The shared footer states the version and the TMDB attribution, and a
        # public page owes that attribution exactly as a signed-in one does — its
        # cards draw the same TMDB poster art. `None` is "nobody is signed in",
        # which is what this page always is: the nav flags come back all-false and
        # go unread, while version and build are what the footer wants. Asked of
        # chrome rather than restated here, so a page cannot drift from what every
        # other page says about itself.
        **chrome.page_context(None),
        # STATED RATHER THAN LEFT UNDEFINED. The share page draws the calendar's
        # own cards, and those offer to filter from a badge — which needs filters
        # of one's own to add to, and a visitor here has none. Relying on the name
        # simply being absent would work and would say nothing.
        "can_filter": False,
        # WHICH BADGE EACH NETWORK HAS. Same question the signed-in calendar asks,
        # and it has to be asked here too or every network badge on a public page
        # would be drawn for a logo that may not exist. `logos.known` WITHOUT the
        # warm-up its sibling does: a share page makes no outbound call it is not
        # forced to (see this module's own allow_fetch=False rule), so it draws the
        # logos that are recorded and leaves resolving the rest to the owner's own
        # calendar.
        "logo_networks": await logos.known(item.network for item in visible if item.network),
    }
    # This template is one block per airing — a busy month is a lot of string
    # building — so it is drawn off the event loop (templating.drawn), where no
    # other request waits behind it.
    with span("share.render_page", airings=len(visible)):
        return await templating.drawn(request, "share_calendar.html", context)


# ---------------------------------------------------------------------------
# the preview picture behind a shared link
# ---------------------------------------------------------------------------
# WHAT THIS HALF DOES THAT THE PAGE DOES NOT: it may make an outbound call. That
# is the one deliberate exception to the rule stated at the top of this module,
# and it is narrow on purpose. The rule exists so an anonymous request cannot
# spend the instance's Trakt budget on the owner's behalf, unboundedly, per hit.
# What happens here is at most a handful of ARTWORK lookups for titles the
# calendar cache already holds, through the app's one poster resolution chain,
# into a cache shared by every user on the instance, with a negative marker so a
# failure does not repeat — and the finished picture is then cached on disk, so
# the whole cost is paid once per (share, month, content) rather than once per
# crawl. The CALENDAR itself is still never fetched (see `_read_month`).

def _card_view_params(view: ShareView) -> dict[str, str]:
    """The resolved view, spelled back out as the params the picture's own URL
    carries.

    FROM THE RESOLVED VIEW, NEVER FROM THE QUERY STRING THAT ARRIVED. Most of
    what decides which airings a month shows is defaulted rather than typed — a
    visitor who spelled out nothing still gets an endpoint, a timezone and a
    marks filter from the owner's share row — so a URL rebuilt from what they
    happened to type would leave the picture resolving those defaults again,
    later, at whatever they are when a crawler arrives. Pinning them here is what
    makes the card and the page it previews the same view rather than two views
    that usually agree.

    ONLY WHAT CHANGES WHICH AIRINGS ARE IN IT: `card_style` and `day_packing` are
    page layout and mean nothing to a picture, and carrying them would mint a
    second address for a byte-identical card.
    """
    params = {
        "endpoint": view.endpoint.key,
        "year": str(view.year),
        "month": str(view.month),
        "hidenw": "1" if view.hide_not_watching else "0",
        "tz": view.tz.key,
    }
    if view.source:
        # Pinned like everything else here: the picture must be filled from the
        # services the page it previews was filled from, and leaving it to be
        # resolved again from whatever an unfurler happened to fetch is how a
        # card ends up advertising a count its page does not show.
        params["source"] = view.source
    if view.network_filter:
        # The one view param the compact code has no field for, which is why
        # share_links.link_query decides between the short and long spellings
        # rather than this caller assuming one.
        params["networks"] = ",".join(view.network_filter)
    return params


def _card_url(view: ShareView, share_row, base: str) -> str | None:
    """Where this page's preview picture lives, absolutely, or None when the
    instance has no configured public origin to build a URL from.

    ALWAYS THE TOKEN FORM, from all three kinds of share link, because the token
    is the most anonymous of the three identifiers this app has: a username or a
    slug printed into markup that gets pasted into other people's channels names
    the owner, which is exactly what the picture itself is designed not to do
    (it carries an avatar and no name). There is one picture route for the same
    reason — see `share_card_image` and share_links.resolve_for_card, which
    answers whenever the share is published by ANY form, so a slug-only share
    still has a preview.
    """
    if not base:
        return None
    return f"{base}/s/{share_row['token']}/og.jpg{share_links.link_query(_card_view_params(view))}"


# How many titles ever get a tile, whatever the month holds. Read from the
# renderer's own layout bound rather than restated here: the strip is laid out
# for exactly this many, so resolving more would be work nothing draws — and a
# thirty-airing August must not become thirty lookups.
MAX_CARD_TILES = share_card.MAX_TILES

# How far down the ranking a card may look for a title it can actually DRAW.
#
# WHY IT IS MORE THAN THE GRID, which is the whole of this number's reason to
# exist. Selection ranks titles by how strongly they represent the month; a tile
# also needs ARTWORK, and those are different questions asked at different times.
# Taking exactly a grid's worth and then dropping whatever had no poster spends a
# slot on a title that cannot be drawn — so a month with a hundred candidates
# rendered a strip of six, while titles with artwork on disk sat just below the
# cut. Measured on a real August: four of the top ten had no poster.
#
# IT IS A CEILING, NOT A MULTIPLE OF THE MONTH. The bound that matters is that a
# busy month must not become a month's worth of lookups, and this is a fixed
# thirty however many airings there are. What it costs is a stat each for the
# twenty beyond the grid: only the first grid's worth is ever WARMED (see
# `_resolve_tiles`), so the outbound work is exactly what it was.
TILE_CANDIDATES = MAX_CARD_TILES * 3

# A wall clock on resolving artwork, applied ONLY where a request is waiting on
# the answer. An unfurler gives up in a handful of seconds, and a card that
# arrives after Discord stopped listening is not a slower embed, it is no embed
# at all — which is worse than a card with three tiles instead of five. When
# this expires we draw what landed.
POSTER_BUDGET_SECONDS = 4.0

# How strong a preview a title is, lowest first. TIER BEATS AIR DATE: a series
# premiere on the 30th says more about a month than a mid-season episode on the
# 1st, and "earliest first" — which is what a reader expects — holds within a
# tier.
# TIERS RATHER THAN A FILTER, because the card has to work on all five calendar
# views. On the premieres view every item is already the first tier and the sort
# collapses to pure date order; on All Episodes the tiers do real work; on movies
# everything is one tier and it is date order again. One rule, no per-view
# branching, and no view that renders an empty strip.
_TIER_SERIES_PREMIERE = 0
_TIER_SEASON_PREMIERE = 1
_TIER_MOVIE = 2
_TIER_OTHER = 3


def tile_tier(item: Item) -> int:
    """Which tier `item` belongs to for the purposes of leading a card.

    `Item.season` and `Item.episode_number` are `int | None` and are absent
    entirely on movies, so the movie test comes first and nothing below ever
    compares None with an integer.
    """
    if item.media == Media.MOVIE:
        return _TIER_MOVIE
    if item.episode_number == 1:
        return _TIER_SERIES_PREMIERE if item.season == 1 else _TIER_SEASON_PREMIERE
    return _TIER_OTHER


def _tile_date_label(item: Item) -> str:
    """When this airing airs, spelled the way the card draws it.

    THE FORMATTING HAPPENS HERE, not in the renderer: share_card is handed a
    finished string because it has no calendar vocabulary and no business
    knowing what an ISO date is (see its module docstring). This is the one
    place the card's date format lives.

    Weekday, day, month — "Fri 14 Aug". The month is redundant with the card's
    own heading in the ordinary case and is kept anyway: it costs a caption four
    characters, and a date reading "Fri 14" beside a heading saying August would
    be actively wrong for an entry the calendar window carried in from either
    side of the month. An air date this app could not parse is drawn as nothing
    rather than as a guess.
    """
    try:
        when = date.fromisoformat(item.air_date)
    except (TypeError, ValueError):
        return ""
    return f"{when:%a} {when.day} {when:%b}"


def _air_moment(item: Item) -> float | None:
    """When this airing airs, as a number that sorts, or None when there isn't
    one this app can make sense of.

    `Item.air_ts` is declared a float and is one for every airing any provider
    here produces, but the display order below is the one place a bad value
    would surface as an exception on an anonymous route rather than as a
    slightly odd picture, so it is checked rather than trusted.
    """
    return item.air_ts if isinstance(item.air_ts, (int, float)) else None


def _air_sort_key(pair: tuple[share_card.Tile, float | None]) -> tuple[bool, float]:
    """Earliest first, and everything undated after everything dated.

    An airing with no usable timestamp has no place in a date order, so it goes
    to the END of the grid — the last cells of the bottom row — rather than to
    the front, where a zero would put it. Among themselves the undated ones keep
    the order the selection gave them, because the sort is stable. Deterministic
    either way, and never an exception.
    """
    _tile, when = pair
    return (when is None, when if when is not None else 0.0)


def arrange_tiles(pairs: Sequence[tuple[share_card.Tile, float | None]]) -> tuple[share_card.Tile, ...]:
    """The order the card DRAWS its tiles in: one date order across the whole
    grid, filled in reading order — left to right, top row then bottom.

    SELECTION AND DISPLAY ARE TWO DIFFERENT ORDERS, and this is the second one.
    `select_tile_items` decides WHICH titles get a tile, by strength — series
    premieres first, then season premieres, and so on. Once that is decided the
    tier has said everything it has to say: what a reader gets is a month laid
    out as a month, scanned the way any calendar is scanned, and a card whose
    dates ran 8/3, 8/5, 8/7 then jumped BACK to 8/4 on the second line reads as
    a bug even to someone who knows why. The tier picks the titles; the date
    places them. A series premiere sitting in the second row because it airs
    late in the month is the correct picture, not a loss.

    NO ROW ARITHMETIC HERE. Where the break falls is the renderer's business —
    `share_card.tile_rows` balances a partial grid as four-then-three rather
    than five-then-two — and a single sorted sequence lands correctly under any
    split it chooses: earliest four on top, next three below.

    EACH TILE CARRIES ITS OWN CAPTION, so re-ordering moves a title, its date
    and its artwork together by construction; the timestamp rides alongside
    rather than inside `Tile` because the renderer is handed a preformatted date
    STRING ("Fri 14 Aug") that does not sort chronologically, and sorting on what
    is drawn would produce an order that looks almost right.
    """
    return tuple(tile for tile, _when in sorted(pairs, key=_air_sort_key))


def select_tile_items(items: Sequence[Item], limit: int = MAX_CARD_TILES) -> list[Item]:
    """Which titles lead the card: one stable sort by (tier, air time), then the
    first `limit` of them.

    A PURE FUNCTION, on its own, because "which shows represent this month" is a
    judgement that will still be argued about long after the plumbing around it
    has settled, and it should be arguable — and testable — without a request, a
    database or a renderer.

    Stable, so two titles in the same tier at the same moment keep the order the
    calendar itself gave them. NOT sorted by rating or popularity: that would
    make the card a different promise ("here are the good ones") than the page it
    previews makes, and it would change which titles appear as ratings drift,
    re-rendering the picture for no reason a reader can see.
    """
    return sorted(items, key=lambda item: (tile_tier(item), item.air_ts))[:max(0, limit)]


def _item_order(item: Item, order: tuple[str, ...]) -> tuple[str, ...]:
    """The artwork order for ONE item: the service actually describing it first,
    then the owner's preference behind that.

    THE OWNER'S ORDER IS NOT ENOUGH ON ITS OWN, and this is the bug that taught
    it. That order is resolved against the `show_posters` REGISTRY, which
    remembers every service that has ever mentioned a tmdb id — including
    services that do not describe this title on this calendar at all. Halloween
    Wars is Simkl-only in the stored calendar, so the page drew Simkl's picture;
    the registry also held a Trakt row from some other read, the owner's order
    put Trakt first, and the card advertised a different picture of a
    different-looking show. Exactly the fault the source-keyed cache was built
    to end, arrived at from the other side.

    THE ITEM'S OWN SOURCE IS THE ANSWER because it IS what the page resolved:
    `Item.source` is the service whose record won the card, so leading with it
    makes the two agree by construction rather than by both happening to prefer
    the same service. The owner's order still decides everything behind it —
    which matters when that service has no artwork and something has to.
    """
    lead = str(item.source)
    return (lead, *(name for name in order if name != lead))


def _poster_refs(items: Sequence[Item]) -> list[tuple[Item, tuple[str, object]]]:
    """Each item paired with the (media, tmdb) ref its poster is filed under.

    A title carrying no tmdb id is DROPPED rather than looked up. posters.py's
    chain already falls back to a live id-lookup for the ids it does have, and
    going hunting for an id a calendar entry never carried is the one lookup a
    link preview is not worth making.
    """
    return [(item, (item.media, tmdb)) for item in items if (tmdb := item.ids.get("tmdb"))]


def _poster_order(source_preferences) -> tuple[str, ...]:
    """Whose artwork this owner's link should draw, most preferred first.

    THE SAME ANSWER THE PAGE ITSELF USES. `poster` is one of the fields
    app/calendar/resolve.py resolves per source, so asking `source_order` is
    asking the identical question the calendar asks when it decides which
    service's picture a card shows — which is what makes the preview and the
    page agree instead of merely tending to. Resolving artwork TMDB-first was
    how they came to disagree: the card advertised a picture of a show the page
    was not showing a picture of.

    TMDB GOES BEHIND THEM (posters.order_for, the one spelling of that rule,
    shared with the rankings board and the tracker).
    """
    return posters.order_for(source_preferences.source_order([s for s in Source]))


async def _warm_posters(settings, refs, order, *, budget: float | None) -> None:
    """Resolve poster artwork for `refs`, best-effort.

    Everything about the bound lives in the caller: `refs` is already capped, and
    `budget` is wall clock in seconds, or None for "nobody is waiting, let it
    finish". posters.ensure_posters dedupes, skips anything already cached or
    given up on with a single stat each, and fans the rest out under its own
    semaphore, so this is the whole of the outbound work.
    """
    if not refs:
        return
    if budget is None:
        await posters.ensure_posters(settings, refs, order)
        return
    # Whatever landed inside the budget stays landed: the tiles are read back off
    # disk afterwards, so cutting this short costs the card a tile, never the
    # work already done.
    with anyio.move_on_after(budget):
        await posters.ensure_posters(settings, refs, order)


async def _resolve_tiles(settings, visible: Sequence[Item], order: tuple[str, ...], *,
                         budget: float | None) -> tuple[tuple[share_card.Tile, ...], bool]:
    """The card's poster tiles, and whether anything it wanted is still coming.

    The second half of the pair is what decides whether the finished picture may
    be cached. It is False when a chosen title has no poster on disk AND has not
    been given up on — the artwork is still on its way, or the budget above cut
    the wait short. Not caching that render is what makes the degradation
    self-healing: the next request resolves again, by which time the earlier warm
    has usually landed, and stores the complete one. No retry mechanism needed.

    A title that will never have a poster — no tmdb id at all, or a negative
    marker recording that every source came up empty — is SETTLED rather than
    pending, and does not hold the cache open forever on a picture that is
    already the best this month can look.

    THE TILE CARRIES MORE THAN A PICTURE. Its air date is formatted here and its
    "is this a series premiere" flag is read off `tile_tier` — the same function
    that decided the title was worth a tile in the first place, rather than a
    second spelling of the same rule. The renderer is handed the answers.

    THE GRID IS FILLED FROM THE RANKING, NOT FROM A SLICE OF IT. Candidates run
    `TILE_CANDIDATES` deep and the first grid's worth that HAVE artwork are the
    ones drawn, in ranking order — so a title with no poster costs itself a place
    rather than costing the card a tile. Only the leading grid's worth is warmed,
    so this reaches further without asking for more.

    THE ORDER THE TILES COME BACK IN IS THE DISPLAY ORDER, not the selection
    order — see `arrange_tiles`. The two are sorted separately and the
    arrangement happens LAST, after the titles with no artwork have dropped out,
    so the dates a reader sees run in order across whatever tiles survived
    rather than across the ones that were hoped for.
    """
    candidates = _poster_refs(select_tile_items(visible, limit=TILE_CANDIDATES))
    wanted = candidates[:MAX_CARD_TILES]
    # The one place a card request can wait on the network. `budget` is the wall
    # clock it is allowed — a span at or just under it means the budget FIRED and
    # the card that follows is a tile short on purpose, which is a very different
    # story from the same span reading 30ms.
    # PER ITEM, because the order's first entry is the item's own source. Warmed
    # one order at a time so a month whose titles come from two services asks
    # each one for its own artwork rather than one of them for both.
    with span("share.warm_posters", refs=len(wanted), budget=budget):
        by_order: dict[tuple[str, ...], list] = {}
        for item, ref in wanted:
            by_order.setdefault(_item_order(item, order), []).append(ref)
        for item_order, refs in by_order.items():
            await _warm_posters(settings, refs, item_order, budget=budget)

    drawn: list[tuple[share_card.Tile, float | None]] = []
    for item, ref in candidates:
        if len(drawn) >= MAX_CARD_TILES:
            break
        path = posters.cached_poster(*ref, _item_order(item, order))
        if path is not None:
            drawn.append((share_card.Tile(
                title=item.title, poster=path,
                date_label=_tile_date_label(item),
                is_premiere=tile_tier(item) == _TIER_SERIES_PREMIERE,
            ), _air_moment(item)))
    # A FULL GRID IS SETTLED whatever is still resolving behind it: the card is
    # already the best this month can look, and holding the cache open for a
    # poster that would only displace an equally good tile would re-render it on
    # every crawl for nothing. Short of full, anything still on its way is a
    # reason to render again later — which is what makes a thin card heal.
    # A title whose one lookup found no picture at all (artwork.record_miss) is
    # SETTLED, not pending. The negative marker alone could not say so: it is
    # written against an order's first source, and is_negative asks whether every
    # source has one — so a title nothing had a picture for held its card open,
    # re-rendered on every crawl.
    missed = (await artwork.known((media, int(tmdb), None) for _, (media, tmdb) in wanted)).missed
    pending = any(
        posters.cached_poster(*ref, _item_order(item, order)) is None
        and not posters.is_negative(*ref, _item_order(item, order))
        and (ref[0], int(ref[1])) not in missed
        for item, ref in wanted)
    return arrange_tiles(drawn), len(drawn) >= MAX_CARD_TILES or not pending


def _avatar_bytes(owner_id: int) -> bytes | None:
    """SYNCHRONOUS. This account's avatar, or None.

    Absent is the ordinary case rather than an error — most accounts have none —
    and an unreadable file is treated identically, because the card has no
    placeholder to draw either way: the layout simply closes up. Reading the
    bytes rather than passing a path is what lets the renderer stay a pure
    function of what it was handed.
    """
    try:
        return user_images.avatar_path(owner_id).read_bytes()
    except OSError:
        return None


async def assemble_card(view: ShareView, share_row, settings, *,
                        budget: float | None) -> tuple[share_card.Card, bool]:
    """Everything behind one preview picture: the read, the marks, the tiles and
    the avatar, assembled into the renderer's value object.

    Returns the card and whether it is COMPLETE — see `_resolve_tiles`. Renders
    nothing itself; the pixels are share_card's job and the caching is
    share_card_cache's, which is what keeps "which shows", "what it looks like"
    and "how long we keep it" as three things that change for three reasons.
    """
    owner_id = int(share_row["user_id"])
    owner_prefs = await auth.get_user_prefs(owner_id)
    with span("card.read_month", ym=f"{view.year}-{view.month:02d}",
              endpoint=view.endpoint.key) as sp:
        items, _as_of = await _read_month(view, settings, owner_prefs, owner_id)
        sp.set(items=len(items))
    nw_ids = await calendar_state.not_watching_ids(owner_id)
    visible = _visible_items(items, view, nw_ids)

    tiles, complete = await _resolve_tiles(
        settings, visible, _poster_order(await source_prefs.load(owner_id)),
        budget=budget)
    avatar = await anyio.to_thread.run_sync(_avatar_bytes, owner_id)
    card = share_card.Card(
        month_label=view.month_label, year=view.year, count=len(visible),
        tiles=tiles, avatar=avatar,
    )
    return card, complete


# The page pre-warms in flight right now, keyed by the view each one is warming.
# The set does two jobs at once. It stops the same (share, month) being warmed
# twice concurrently — a crawler fetching a page, then its picture, then the
# same page again is the ordinary shape — and its own size is the ceiling on how
# many a burst of crawls can spawn at all.
# OVER THE CEILING THE WARM IS DROPPED, not queued: the request that actually
# wants the artwork does its own bounded, time-boxed warm, so a dropped one costs
# one render one tile, while a queue of them would still be running long after
# anybody wanted the result.
_WARM_CEILING = 4
_warming: set[tuple] = set()

# asyncio keeps only a weak reference to a running task, so a task nobody else
# holds can be collected mid-flight. This is that reference.
_warm_tasks: set[asyncio.Task] = set()


def _spawn_poster_warm(view: ShareView, owner_id: int, visible: Sequence[Item],
                       settings, order: tuple[str, ...]) -> None:
    """Start resolving the artwork this month's card will want once the page
    has been answered (after_response), and return immediately.

    A REAL TASK with its exceptions swallowed, not a bare un-awaited coroutine —
    that is a RuntimeWarning and no warm at all — because nothing here may affect
    the page that spawned it.

    NOT TIME-BOXED, unlike the request path's own warm: nothing is waiting on
    this, so cutting it off would only throw away work that was about to land and
    make the budget on the request path fire more often. It is still bounded in
    COUNT by the tile selection, which is the bound that matters for outbound
    load, and in CONCURRENCY by the ceiling above.
    """
    key = (owner_id, view.endpoint.key, view.year, view.month)
    if key in _warming or len(_warming) >= _WARM_CEILING:
        return
    refs = [(item, ref) for item, ref in _poster_refs(select_tile_items(visible))]
    if refs:
        after_response.later(lambda: _start_poster_warm(key, refs, settings, order))


def _start_poster_warm(key: tuple, refs, settings, order: tuple[str, ...]) -> None:
    # Asked again as it starts: another page of the same month may have begun
    # its warm while this one was being answered.
    if key in _warming or len(_warming) >= _WARM_CEILING:
        return
    _warming.add(key)
    task = asyncio.create_task(_warm_job(key, refs, settings, order))
    _warm_tasks.add(task)
    task.add_done_callback(_warm_tasks.discard)


# TWO CONCURRENT CARD RENDERS, INSTANCE-WIDE, and this is deliberately its own
# semaphore rather than the ranker's. Pillow is synchronous CPU work on the one
# loop that serves every other request, and unfurlers arrive in bursts — but a
# share unfurl must not be able to queue behind two full-size board exports,
# which are an order of magnitude larger and slower, and somebody's export must
# not be blocked by a crawler. Two slots, matching the ranker's count for the
# same reason it picked it.
_card_render_slots = anyio.Semaphore(2)


async def render_card(view: ShareView, share_row, settings) -> bytes:
    """One share card as encoded bytes, rendered only if it is not already on
    disk.

    THE ORDER HERE IS NOT THE OBVIOUS ONE AND IT IS DELIBERATE: the month is read
    and the tiles resolved BEFORE the cache is consulted. The cache is addressed
    by what the picture CONTAINS rather than by the URL that asked for it — which
    is what makes it self-invalidating (share_card_cache.render_key) — and there
    is no way to know the content without doing the read. It is not the wrong way
    round: the read is a local cache read of data the page has usually already
    warmed, while the render is Pillow on a path an anonymous crawler can reach.
    The expensive half is the half being skipped.
    """
    card, complete = await assemble_card(view, share_row, settings,
                                         budget=POSTER_BUDGET_SECONDS)
    key = share_card_cache.render_key(card, owner_id=int(share_row["user_id"]),
                                      month=view.month)
    path = share_card_cache.cache_path(key)

    payload = await anyio.to_thread.run_sync(share_card_cache.cached_render, path)
    if payload is not None:
        logger.info("share card %s: cache hit (%d bytes)", key, len(payload))
        return payload

    # TWO SPANS, BECAUSE THE SEMAPHORE IS INSIDE ONE OF THEM. There are two render
    # slots instance-wide, so a burst of unfurls makes the third card WAIT without
    # doing any work — and a single span covering both would report that wait as
    # render time and send anyone reading it after Pillow.
    with span("card.await_slot"):
        await _card_render_slots.acquire()
    try:
        with span("card.render", tiles=len(card.tiles), complete=complete):
            payload = await anyio.to_thread.run_sync(share_card.build_card, card)
    finally:
        _card_render_slots.release()
    logger.info("share card %s: rendered %d bytes, tiles=%d complete=%s",
                key, len(payload), len(card.tiles), complete)
    if complete:
        # An INCOMPLETE card is served but never kept: some of the artwork it
        # wanted was still on its way, and the next request — by which time the
        # background warm has usually landed — renders and stores the finished
        # one. That is what makes a thin card heal itself rather than being
        # cached half-empty for the whole retention window.
        await anyio.to_thread.run_sync(share_card_cache.store_render, path, payload)
    return payload


async def _warm_job(key: tuple, refs, settings, order: tuple[str, ...]) -> None:
    # This task inherited the spawning request's trace context, and it outlives
    # that request — without detaching, minutes of poster downloads would be
    # attributed to a page that finished long ago.
    perftrace.detach()
    try:
        with span("share.warm_job", refs=len(refs)):
            # Grouped by the per-item order for the same reason the request path
            # groups: the first entry is the item's own source, so a month drawn
            # from two services asks each for its own artwork.
            by_order: dict[tuple[str, ...], list] = {}
            for item, ref in refs:
                by_order.setdefault(_item_order(item, order), []).append(ref)
            for item_order, batch in by_order.items():
                await _warm_posters(settings, batch, item_order, budget=None)
    except Exception:
        # Nobody is waiting on this and there is nothing to tell them. A poster
        # that did not resolve leaves the card a tile short and tries again on
        # the next request.
        logger.debug("Share card poster pre-warm failed", exc_info=True)
    finally:
        _warming.discard(key)


def _too_many_requests() -> Response:
    return PlainTextResponse("Too many requests.", status_code=429)


async def _share_rate_limited(request: Request, settings) -> bool:
    # A read and a WRITE on every public share request, including every crawler
    # hit — which makes this the one thing on these routes that contends for the
    # database rather than just reading it, and worth being able to see on its own
    # when a page is slow before it has done anything.
    with span("share.rate_limit"):
        ip = auth.client_ip(request, settings)
        limited = await auth.rate_limited(
            "share_ip", ip, max_attempts=SHARE_RATE_MAX_ATTEMPTS, window_seconds=SHARE_RATE_WINDOW_SECONDS,
        )
        # Volume-only counter (like registration/invite redemption) — there is no
        # notion of a "failed" share-page request to distinguish.
        await auth.record_attempt("share_ip", ip, True)
    return limited


@guard.get("/s/{token}", AuthLevel.PUBLIC)
async def share_by_token(request: Request, token: str):
    settings = await load_settings()
    if await _share_rate_limited(request, settings):
        return _too_many_requests()
    return await _render(request, await share_links.resolve_by_token(token))


# The picture this feature degrades to, and the picture it replaced: the app's
# own social card, which is also what the register and invite pages advertise
# (app/auth/routes.py builds the same URL). Drawn at the 1200x630 unfurlers
# expect, so a card that falls back to it is still a wide card rather than a
# thumbnail.
STATIC_CARD_URL = "/static/images/distrakkl-social-card-1200x630.png"

# DELIBERATELY NOT THE 90 DAYS THE RENDERED FILE IS KEPT FOR, and the two numbers
# looking like they should match is exactly why this is worth writing down. The
# retention is how long WE keep a file addressed by its CONTENT; this is how long
# a crawler may keep a picture it fetched from a URL addressed by its MONTH. The
# same URL renders a different card the moment that month's line-up changes, so
# telling Discord to hold it for 90 days would pin a stale picture to every post
# already made. Short max-age, long disk retention, no contradiction — and never
# `immutable`, for the same reason.
_CARD_CACHE_HEADERS = {"Cache-Control": "public, max-age=600"}


def _static_card() -> Response:
    """Every unhappy path except a throttle: the static banner, 302.

    NOT a 404 and NOT a 500, for three reasons. A miss stays identical whatever
    caused it — an unknown token, a disabled account, a retired slug — which is
    this module's standing promise that a share link cannot be used to probe
    which. An unfurler that gets a 404 for an og:image renders a preview with a
    broken picture and never retries. And the static banner is the thing this
    feature replaces, so degrading to it is degrading to yesterday's behaviour
    rather than to nothing.
    """
    return RedirectResponse(STATIC_CARD_URL, status_code=302)


@guard.get("/s/{token}/og.jpg", AuthLevel.PUBLIC)
@guard.head("/s/{token}/og.jpg", AuthLevel.PUBLIC)
async def share_card_image(request: Request, token: str):
    """The picture a pasted share link unfurls into, for the month it names.

    ONE ROUTE FOR ALL THREE LINK FORMS, addressed by token, and resolved through
    the resolver that answers whenever the share is published by any of them —
    see share_links.resolve_for_card for both halves of why.

    HEAD IS DECLARED ALONGSIDE because some unfurlers check an image before they
    fetch it, and a 405 there is a preview that never appears. Nothing here reads
    a body, so the same handler answers both.
    """
    settings = await load_settings()
    if await _share_rate_limited(request, settings):
        # THE ONE UNHAPPY PATH THAT IS NOT THE BANNER. Redirecting a throttled
        # request to a static file is a redirect the throttle was trying to avoid
        # serving; an unfurler that gets a 429 shows no picture, which is correct
        # when we are shedding load.
        return _too_many_requests()

    share_row = await share_links.resolve_for_card(token)
    if share_row is None:
        return _static_card()

    view = resolve_view(_effective_params(request), share_row, settings)
    try:
        payload = await render_card(view, share_row, settings)
    except Exception:
        # Broad, and around the one call that draws: a corrupt cached poster or a
        # font that vanished must degrade this request, never 500 into somebody's
        # channel. Logged because unlike a miss it is a fault worth seeing.
        logger.warning("Share card render failed; serving the static banner",
                       exc_info=True)
        return _static_card()
    return Response(payload, media_type="image/jpeg", headers=_CARD_CACHE_HEADERS)


@guard.get("/u/{username}", AuthLevel.PUBLIC)
async def share_by_username(request: Request, username: str):
    settings = await load_settings()
    if await _share_rate_limited(request, settings):
        return _too_many_requests()
    return await _render(request, await share_links.resolve_by_username(username))


@guard.get("/c/{slug}", AuthLevel.PUBLIC)
async def share_by_slug(request: Request, slug: str):
    settings = await load_settings()
    if await _share_rate_limited(request, settings):
        return _too_many_requests()
    return await _render(request, await share_links.resolve_by_slug(slug))


# ---------------------------------------------------------------------------
# details for a card on a public page — same modal content as the calendar
# ---------------------------------------------------------------------------
# A PUBLIC REQUEST MAY SPEND ONE DETAIL LOOKUP, AND ONLY FOR A TITLE THE OWNER'S
# OWN CACHED MONTH ALREADY SHOWS. A per-title detail answer is identical for
# every viewer, so it is cached instance-wide rather than per person (the
# transports pass no `private` for it) — which is what makes spending one here
# worth it at all: a single lookup serves that title to every visitor for the
# cache's whole lifetime, and warms it for the owner too. The alternative, which
# is what this was, is a modal that opens on blanks for everybody until the owner
# happens to click the same card on their own calendar.
#
# THE BINDING IS WHAT MAKES THAT SAFE RATHER THAN AN AMPLIFIER, and it is not
# optional. This endpoint is public and unauthenticated, and it used to read ids
# straight off the query string without ever asking what the month contained — so
# with a fetch behind it, any id on the internet becomes a real call on the
# owner's credential, enumerable at the per-IP limit. Binding converts "anything
# a stranger can name" into "what this link already shows".
#
# THE PROMISE LIVES IN ONE FUNCTION AND NOT IN A FLAG EACH CALLER PASSES.
# `_describe_shared_title` resolves the month through `_read_month` and
# `_visible_items` — the same two functions the page and its preview picture
# resolve through, so the three cannot come to different answers about what this
# link shows — and it does the lookup itself, so there is no way to obtain
# permission and then ask about something else. A boolean parameter would put the
# rule back in every call site, which is the failure the preview picture's own
# outbound warm is shaped to avoid.
#
# WHICH SERVICE ANSWERS IS DECIDED BY THE SAME CODE THE SIGNED-IN ROUTE USES
# (detail_source), and that shared decision is the point: this modal is
# documented as showing the same content as the calendar's, and two copies of
# "who describes this title" would make that stop being true in a way neither
# page could show on its own.

def _season_param(value) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


class _Described(NamedTuple):
    """A public answer about one title: who gave it, what they said, and the
    picture this link is already showing for it.

    THE POSTER COMES FROM THE MONTH, NOT FROM THE REQUEST, and that is the one
    place this differs from the signed-in calendar's panel. There a card's own
    picture rides on the query string, because the page is echoing its own value
    back into its own view; here the caller is a stranger, and an address a
    stranger spelled has no business being rendered into a response served from
    the owner's origin. The month has been read by the time this is decided
    anyway, and it holds exactly the picture the grid drew.
    """
    source: Source
    details: dict
    poster: str
    links: list
    # The airing this names, out of what the view shows, or None. It is what the
    # owner's own watch marks are looked up by — see `_owner_progress`.
    airing: object = None


def _ids_this_view_shows(items: Sequence[Item], media: str,
                         season: int | None) -> dict[str, set[str]]:
    """{id namespace: the values this view's own airings carry}, for the medium
    and season a details request named.

    THE MEDIUM AND THE SEASON ARE PART OF THE BOUND, not decoration. Each is a
    separate lookup upstream — `shows/{id}` and `movies/{id}` are two calls, and
    a season is another one per season — so a bound that checked only the id
    would still let a stranger turn one title the calendar shows into as many
    outbound calls as there are integers.

    A REQUEST NAMING NO SEASON IS NOT NARROWED BY ONE. That is the film case and
    the case of a show placed by its own first-air date, both of which carry no
    season on the card the click came from.
    """
    shown: dict[str, set[str]] = {}
    for item in items:
        if str(item.media) != media:
            continue
        if season is not None and item.season != season:
            continue
        for namespace, value in (item.ids or {}).items():
            if value not in (None, ""):
                shown.setdefault(namespace, set()).add(str(value))
    return shown


async def _describe_shared_title(request: Request, share_row, settings) -> _Described | None:
    """What a public details request may be told about one title, or None when
    nothing may be said.

    THIS IS THE ONLY PLACE A PUBLIC REQUEST IS ALLOWED TO COST AN OUTBOUND CALL,
    and the reason it may is written above this function. Everything that decides
    whether it may is here: the view the request names is resolved exactly as the
    page resolves it, the month is read cache-only through `_read_month`, and the
    ids the request carried are narrowed to the ones that month's visible airings
    actually hold. What survives is by definition a title this link already
    publishes, so the lookup behind it is one the owner's own calendar would have
    made anyway.

    NARROWED RATHER THAN CHECKED, and the difference matters: a request pairs one
    id per service, and `detail_source.choose` picks between them. Dropping the
    unknown ones before that choice is what stops a real id for one service
    smuggling a made-up id for the other past the bound.
    """
    owner_id = int(share_row["user_id"])
    owner_prefs = await auth.get_user_prefs(owner_id)
    # `_effective_params` rather than the raw query string, for the same
    # precedence reason the page and the picture both use it: a link handed out
    # in its short form must resolve to the same view once it is expanded.
    params = _effective_params(request)
    view = resolve_view(params, share_row, settings)
    # Whitelisted rather than trusted, like every other value a stranger can
    # spell in this page's query string: an unusable one falls back instead
    # of raising.
    media = str(parse_media(params.get("media"), Media.SHOW))
    season = _season_param(params.get("season"))

    items, _as_of = await _read_month(view, settings, owner_prefs, owner_id)
    visible = _visible_items(items, view, await calendar_state.not_watching_ids(owner_id))
    shown = _ids_this_view_shows(visible, media, season)
    asked = detail_source.ids_from_query(params)
    bound = {namespace: value for namespace, value in asked.items()
             if value in shown.get(namespace, ())}

    chosen = detail_source.choose(settings, bound)
    if chosen is None:
        return None
    # A season the chosen service has no episodes of is answered by the next
    # service that does — among the BOUND ids only, so the promise above holds
    # for the second question as for the first (detail_source.for_season). THE
    # SAME HOLDS FOR THE THIRD: every service's videos are pooled over `bound`
    # and nothing else, so a service asked for its list is asked about a title
    # this link shows, by the id the link's own airing carries.
    # A PUBLIC PAGE DRAWS NO SEASON STRIP (`_details` says why), so no service is
    # asked for a season list it would not draw.
    chosen, details = await metadata_panel.describe_panel(settings, chosen, bound,
                                                         media, season, with_seasons=False)
    if details is None:
        return None
    # THE BOUND IDS AND WHAT THE ANSWER ADDED, never the raw request: an outbound
    # button is built from an id, and an id a stranger spelled has no more business
    # in a rendered link than in a rendered picture.
    shown = _the_airing_this_names(visible, bound)
    shown_ids = (shown.ids if shown else None) or {}
    return _Described(
        chosen.source, details, str((shown.poster if shown else "") or ""),
        # THE MONTH'S OWN IDS, never the request's. An outbound button is an id
        # rendered into a link, so it follows the picture's rule rather than the
        # lookup's: what a public page publishes about a title comes from what the
        # owner's calendar holds, not from what a stranger spelled. The airing also
        # carries each service's own NAME for the title, which is what lets these
        # links be spelled the way both services ask for. Beneath them, every id the
        # title store knows for that airing's title (identity.every_id) — read from
        # this instance, never asked of anybody.
        detail_source.outbound_links(await title_identity.every_id(media, shown_ids), media),
        shown)


def _the_airing_this_names(items: Sequence[Item], bound: dict[str, str]) -> Item | None:
    """The airing `bound` names, out of the ones this view shows.

    ONE LOOKUP FOR EVERYTHING THE PANEL DRAWS THAT IS NOT THE ANSWER — the
    picture and the outbound links both come from here, so the panel and the card
    behind it cannot disagree about a title. That is the property CLAUDE.md
    already requires of the preview picture, and for the same reason.
    """
    for item in items:
        ids = item.ids or {}
        if any(str(ids.get(namespace) or "") == value for namespace, value in bound.items()):
            return item
    return None


async def _details(request: Request, share_row) -> Response:
    """The details panel for one card on a shared calendar.

    A VIEW, NOT AN API: the same panel the owner's own calendar opens
    (templates/_details.html), so a visitor is shown what the owner is shown and
    there is no second renderer for a public surface to drift away in.

    THE WATCH TICKS ARE THE OWNER'S, AND THE OWNER DECIDES WHETHER THEY APPEAR.
    "Whose progress would they be?" has an answer — the same person whose
    calendar, whose filters and whose not-watching marks this page already
    publishes — and showing how far along they are is most of why somebody shares
    a calendar. THE VISITOR IS NEVER ASKED ABOUT: the read is `owner_id`'s, and a
    visitor has no account here to confuse it with.

    IT IS SWITCHED FROM THE ACCOUNT PAGE, NOT FROM THE LINK, and that is the one
    part worth being careful about. Publishing a viewing history is a different
    KIND of fact from an airing date, so the default being ON only works because
    it can be turned off — and turned off ONCE, for every link already handed out,
    rather than per link. `user_prefs.share_watch_history` (migration 42) is that
    switch; the owner's prefs are already read here for the filters, so consulting
    it costs no second query.

    THE IDENTITY COMES FROM THE MONTH, never from the request — the same rule the
    picture and the outbound links follow. A stranger cannot name a title and be
    told what the owner has watched of it.

    NO SEASON PICKER HERE, AND THAT IS THE POINT RATHER THAN AN OMISSION. A
    shared calendar publishes ONE MONTH. What may be looked up is bound to what
    that view shows — the id, the medium AND the season — so on a link showing
    season 3 of a five-season show, four of the five tabs would refuse. Offering
    a control that mostly says no is worse than not offering it, and the refusal
    has to stay a single unhelpful sentence (telling a stranger which seasons the
    owner's calendar contains is exactly what that one message prevents). So the
    panel is drawn with `picker=False` and shows the season the card is for.
    """
    settings = await load_settings()
    params = _effective_params(request)
    if await _share_rate_limited(request, settings):
        return _too_many_requests()
    if share_row is None:
        return details_panel.gap(request, _NO_DESCRIPTION)
    try:
        described = await _describe_shared_title(request, share_row, settings)
    except SourceUnavailable:
        # A SERVICE THAT COULD NOT BE ASKED IS THE SAME REFUSAL, drawn at 200 like
        # every fragment — a non-200 is not swapped, so an escaped failure left
        # the panel on its loading line. Not "could not load", which every other
        # surface says: only a title this link shows is ever asked about, so a
        # sentence of its own would tell a stranger, for any id tried while a
        # service is down, whether the owner's calendar holds it.
        logger.warning("A shared calendar's details lookup failed", exc_info=True)
        described = None
    if described is None:
        # ONE REFUSAL FOR BOTH CASES, deliberately. "No source can describe this"
        # and "this link does not show that title" are different facts, and a
        # public page that told them apart would answer, for any id a stranger
        # cared to try, whether the owner's calendar contains it. A LINK THAT DOES
        # NOT RESOLVE AT ALL answers the same way for the same reason, and so does
        # a SEASON TAB for a season this view has no airing of.
        return details_panel.gap(request, _NO_DESCRIPTION)
    season = _season_param(params.get("season"))
    progress = await _owner_progress(share_row, described, season)
    return details_panel.panel(
        request, described.details,
        media=str(parse_media(params.get("media"), Media.SHOW)),
        season=season,
        # THE NAME THE PANEL'S POSTER IS DESCRIBED BY comes from the answer here
        # rather than from the request, for the same reason the picture does. It
        # is alt text and nothing else — the heading above it is set from the card
        # the visitor clicked, before this request is even made.
        title=str(described.details.get("title") or ""),
        poster=described.poster, links=described.links, progress=progress,
        # NO STRIP ON A PUBLIC PAGE — see the note above. Nothing is carried
        # forward either, because there is no tab to carry it.
        picker=False)


async def _owner_progress(share_row, described: _Described, season: int | None):
    """The OWNER's watched episodes for this season, or None.

    THREE THINGS HAVE TO BE TRUE and each is a separate refusal. The account must
    have left the switch on; the view must actually be showing this title, so the
    identity comes from the airing rather than from anything the visitor spelled;
    and there must be something recorded — a season the owner has watched none of
    draws no ticks at all, which is the rule the calendar's own panel follows and
    it matters more here, where a column of hollow marks on every card would
    publish "the owner has watched nothing" about their whole month.

    A FILM HAS NO SEASON TO LOOK UP, and neither has a title the waterfall cannot
    key; both are None rather than an error.

    The import is function-local because the tracker reads the calendar back, and
    naming it at module level would close that loop at import time — the same
    reason the calendar's own route defers it. See tests/kernel/test_layering.py,
    where the edge is declared DEFERRED.
    """
    if season is None or described.airing is None:
        return None
    owner_id = int(share_row["user_id"])
    prefs = await auth.get_user_prefs(owner_id)
    if not prefs.get("share_watch_history"):
        return None
    try:
        key = parse_item_key(str(described.airing.mark_key))
    except ValueError:
        return None
    from ..distrakt import episode_progress
    progress = await episode_progress.for_season(
        owner_id, key, season, described.airing.ids or {})
    return progress if progress.count else None


# What a public request is told when it may not be told anything. Named once
# because two callers must not be able to word it differently — see the refusal
# note in `_details`.
_NO_DESCRIPTION = "No source can describe this title"


@guard.get("/s/{token}/details", AuthLevel.PUBLIC)
async def share_details_by_token(request: Request, token: str):
    return await _details(request, await share_links.resolve_by_token(token))


@guard.get("/u/{username}/details", AuthLevel.PUBLIC)
async def share_details_by_username(request: Request, username: str):
    return await _details(request, await share_links.resolve_by_username(username))


@guard.get("/c/{slug}/details", AuthLevel.PUBLIC)
async def share_details_by_slug(request: Request, slug: str):
    return await _details(request, await share_links.resolve_by_slug(slug))


def _public_base(settings) -> str:
    return (settings.public_base_url or "").rstrip("/")


# ---------------------------------------------------------------------------
# the Share panel, drawn by the server
# ---------------------------------------------------------------------------
#
# A FRAGMENT EVERY MUTATION ANSWERS. The panel is fetched when 🔗 Share is
# pressed, and every control in it posts itself: the kind and Rotate are answered
# with the link box, a custom name with its own status line (and the link box out
# of band once it has saved), and the display options with the link box — plus the
# Sources ticks when the link's Calendar option moved. It used to be an empty
# dialog that a script filled from a JSON read and redrew from each JSON answer,
# rebuilding its ticks from a per-calendar map the calendar page shipped for no
# other reason.
#
# A REFUSAL IS DRAWN AT 200, beside the control that asked. A non-200 is never
# swapped (static/js/boost.js's `noSwap`).

SHARE_PANEL_TEMPLATE = "_share_panel.html"
SHARE_LINK_TEMPLATE = "_share_link.html"
SHARE_SLUG_TEMPLATE = "_share_slug_answer.html"
SHARE_VIEW_TEMPLATE = "_share_view_answer.html"


def share_source_choices(endpoint, settings) -> list[dict]:
    """The Share panel's Sources control for a link that opens on `endpoint`: one
    TICK per service the link may be narrowed to, with the field it posts under.
    An empty list means the panel draws nothing, by
    app/calendar/resolve.py's `answering_services` rule rather than by a second
    count of the same thing, so the panel and the toolbar disappear together.

    TICKS RATHER THAN A LIST OF ANSWERS, because a source selection is a SET — the
    shape the 🎚️ Filters panel draws for the stored version of this question, and
    the one that survives a service being registered.

    NOTHING TICKED MEANS "MY SOURCES", and there is no "every service" tick to
    draw: a link narrows the owner's calendar and can never widen it (see
    `_narrowed_prefs`), so "all of them" and "none of them" are one outcome.
    """
    return [{"value": name, "label": label, "name": share_links.source_tick_name(name)}
            for name, label in calendar_resolve.answering_services(endpoint, settings)]


async def _share_context(user, settings, **extra) -> dict:
    """Everything the Share panel draws, from this account's share row.

    THE DISPLAY OPTIONS OPEN ON WHAT THE LINK SAYS, and on the share page's own
    fallbacks where it says nothing — the owner-default columns a bare link
    resolves through — so choosing "custom" starts from the link its owner already
    had rather than from some unrelated default.

    THE YEARS: the one before this and the three after, plus a pinned year outside
    that range, so a link pinned long ago still shows its own year rather than
    snapping to one the owner never picked.
    """
    row = await share_links.get_or_create(user.user_id)
    base = _public_base(settings)
    stored = share_links.link_view(row)
    view = stored or {}
    endpoint = get_endpoint(view.get("endpoint") or row["endpoint"] or settings.endpoint)
    ticked = source_prefs.named_sources(view.get("source") or "") or frozenset()
    today = clock.today()
    pinned = int(view["year"]) if view.get("year") else None
    kind = row["preferred_kind"]
    return {
        "share_kind": kind,
        "share_kinds": list(share_links.KIND_LABELS.items()),
        "share_url": share_links.generated_urls(row, user.username, base)[kind],
        "share_base_missing": not base,
        "share_is_admin": user.is_admin,
        "share_slug": row["custom_slug"] or "",
        "share_custom": stored is not None,
        "share_view": {
            "endpoint": endpoint.key,
            "card": view.get("card") or row["card_style"] or settings.card_style,
            "packing": view.get("packing") or row["day_packing"] or settings.day_packing,
            "hidenw": view.get("hidenw", "1" if row["hide_not_watching"] else "0") == "1",
            "month": view.get("month", ""),
            "year": str(pinned or today.year),
        },
        "share_endpoints": endpoint_choices(),
        "share_timezones": build_timezone_options(
            view.get("tz") or row["timezone"] or user.timezone or settings.timezone),
        "share_sources": [dict(choice, ticked=choice["value"] in ticked)
                          for choice in share_source_choices(endpoint, settings)],
        "share_months": [(number, _calendar.month_name[number]) for number in range(1, 13)],
        "share_years": sorted({*range(today.year - 1, today.year + 4), *([pinned] if pinned else [])}),
        "share_message": "",
        "share_error": "",
        **extra,
    }


async def _share_answer(request: Request, template: str, user, **extra):
    context = await _share_context(user, await load_settings(), **extra)
    return templates.TemplateResponse(request, template, {"request": request, **context})


@guard.get("/calendar/fragments/share", AuthLevel.CALENDAR_APPROVED)
async def share_panel(request: Request):
    """The whole Share overlay, already open, drawn from this account's links."""
    return await _share_answer(request, SHARE_PANEL_TEMPLATE, await auth.current_user(request))


@guard.post("/calendar/share/kind", AuthLevel.CALENDAR_APPROVED)
async def share_kind(request: Request):
    """Choose which of the three links the panel hands out. All three keep
    answering — share_links.set_active_kind says why that is deliberate."""
    user = await auth.current_user(request)
    data = await authz.json_body(request)
    kind = route_params.last_value(data.get("kind"))
    if kind not in share_links.PREFERRED_KINDS:
        return await _share_answer(request, SHARE_LINK_TEMPLATE, user,
                                   share_error="That is not a kind of link.")
    await share_links.set_active_kind(user.user_id, kind)
    return await _share_answer(request, SHARE_LINK_TEMPLATE, user)


@guard.post("/calendar/share/rotate", AuthLevel.CALENDAR_APPROVED)
async def share_rotate(request: Request):
    """Issue a new token link. The old one stops answering at once."""
    user = await auth.current_user(request)
    await share_links.rotate_token(user.user_id)
    return await _share_answer(request, SHARE_LINK_TEMPLATE, user,
                               share_message="A new link is live, and the old one has stopped working.")


@guard.post("/calendar/share/slug", AuthLevel.CALENDAR_APPROVED)
async def share_slug(request: Request):
    """Check a custom name and, if it is free, save it — one request for what used
    to be a check followed by a save. An empty box writes nothing."""
    user = await auth.current_user(request)
    data = await authz.json_body(request)
    slug = str(route_params.last_value(data.get("slug")) or "").strip()
    if not slug:
        return await _share_answer(request, SHARE_SLUG_TEMPLATE, user,
                                   share_slug_status="", share_slug_ok=False)
    error = await share_links.set_custom_slug(user.user_id, slug)
    return await _share_answer(request, SHARE_SLUG_TEMPLATE, user,
                               share_slug_status=error or "Saved.", share_slug_ok=error is None)


@guard.post("/calendar/share/view", AuthLevel.CALENDAR_APPROVED)
async def share_view(request: Request):
    """Set, or clear, the display options the generated link carries.

    This writes the LINK and only the link: the owner's own calendar and the share
    page's fallback defaults are untouched (share_links.set_link_view).

    WHICH SERVICES THE FORM MAY TICK is asked of the calendar the form NAMES, not
    the one stored, because the Calendar option may be the control that just moved
    — and the ticks are redrawn exactly when it did."""
    user = await auth.current_user(request)
    data = await authz.json_body(request)
    settings = await load_settings()
    before = share_links.link_view(await share_links.get_or_create(user.user_id)) or {}
    named = ENDPOINTS.get(str(route_params.last_value(data.get("endpoint")) or ""))
    offered = [choice["value"] for choice in share_source_choices(named, settings)] if named else []
    view = share_links.view_from_form(data, offered)
    error = await share_links.set_link_view(user.user_id, view)
    moved = view is not None and view.get("endpoint") != before.get("endpoint")
    return await _share_answer(request, SHARE_VIEW_TEMPLATE, user,
                               share_view_error=error or "", share_sources_moved=moved and not error)
