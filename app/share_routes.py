"""Public calendar sharing: the read-only /s/, /u/, /c/ pages, and the
owner-facing API the calendar's Share panel calls to manage them.

The three public routes do the SAME read as the authenticated calendar —
calendar_cache.read_month, the per-(endpoint, 7-day window) cache, normalized
into a viewer's timezone the same way — with the fetch branch permanently
switched off: a public request serves whatever is cached — even stale, even
nothing — and never spends the instance's Trakt rate limit. A visitor is never
given a session, and nothing here writes anything on their behalf.

A miss is identical whatever the reason — an unknown token, a disabled
account, or a retired username/slug — so a share link can never be used to
probe which of those three it is.
"""
from __future__ import annotations

import asyncio
import calendar as _calendar
import json
import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from itertools import groupby
from urllib.parse import quote, urlencode
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import anyio
import anyio.to_thread
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, PlainTextResponse, RedirectResponse, Response

from . import (auth, calendar_cache, calendar_state, posters, route_params, share_card,
               share_card_cache, share_code, share_links, user_images)
from .providers.trakt import detail as trakt_detail
from .auth import AuthLevel
from . import authz
from .authz import Guard
from .config import load_settings
from .endpoints import DEFAULT_ENDPOINT, ENDPOINTS, Endpoint, endpoint_choices, get_endpoint
from .providers.base import Item, Media
from .timezones import build_options as build_timezone_options
from .templating import templates

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
_CARRY_PARAMS = ("card", "packing", "hidenw", "tz", "networks", "endpoint")


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


def _resolve_owner_tz(share_row, settings) -> ZoneInfo:
    """The owner's default timezone: their share-specific override if they set
    one, else their account's own saved timezone, else the app-wide default."""
    for name in (share_row["timezone"], share_row["owner_account_timezone"], settings.timezone, "UTC"):
        if not name:
            continue
        try:
            return ZoneInfo(name)
        except (ZoneInfoNotFoundError, ValueError):
            continue
    return ZoneInfo("UTC")


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
    """
    year: int
    month: int
    endpoint: Endpoint
    tz: ZoneInfo
    hide_not_watching: bool
    network_filter: list[str] | None
    card_style: str
    day_packing: str

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
    today = date.today()
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
    )


# ---------------------------------------------------------------------------
# the never-fetch read + render
# ---------------------------------------------------------------------------

def _not_found(request: Request) -> Response:
    return templates.TemplateResponse(request, "share_not_found.html", {"request": request}, status_code=404)


async def _read_month(view: ShareView, settings, owner_prefs) -> tuple[list[Item], int | None]:
    """The month a share request is asking for, out of the local cache and only
    the local cache.

    `allow_fetch=False` is the whole point of every public share surface: a
    visitor is served whatever is already cached — even stale, even nothing —
    and never triggers a Trakt call that would spend the owner's rate-limit
    budget on an anonymous request. An instance with no calendar source has
    nobody to ask at all, and reads as an empty month rather than raising.

    The prefs come from the OWNER's account rather than from the URL, exactly as
    they do for the owner's own calendar: genres, countries and certifications
    are the owner's editorial choices about their calendar, not view options a
    visitor gets to set.
    """
    if not settings.calendar_source_configured:
        return [], None
    return await calendar_cache.read_month(
        view.endpoint, settings, tz=view.tz, year=view.year, month=view.month,
        genres=owner_prefs["genres"], countries=owner_prefs["countries"],
        show_certifications=owner_prefs["show_certifications"],
        movie_certifications=owner_prefs["movie_certifications"],
        network_filter=view.network_filter, allow_fetch=False,
    )


def _visible_items(items: Sequence[Item], view: ShareView, not_watching: set[str]) -> list[Item]:
    """The airings this view actually shows.

    The owner's marks are a SET alongside the items rather than a field copied
    onto each one — one answer to "is this marked" instead of two spellings of
    it, and no per-item copy of a dataclass that is deliberately awkward to copy.
    """
    return [item for item in items if not (view.hide_not_watching and item.id in not_watching)]


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

    settings = load_settings()
    owner_id = int(share_row["user_id"])
    owner_prefs = await auth.get_user_prefs(owner_id)

    view = resolve_view(request.query_params, share_row, settings)

    items, as_of = await _read_month(view, settings, owner_prefs)
    nw_ids = await calendar_state.not_watching_ids(owner_id)
    visible = _visible_items(items, view, nw_ids)

    # Start resolving the artwork this month's preview picture will want, and do
    # not wait for it: a crawler fetches the page first and the picture second,
    # so by the time anyone asks for the card the posters are usually already on
    # disk.
    _spawn_poster_warm(view, owner_id, visible, settings)

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
        "not_watching": nw_ids,
        "total": len(visible),
        "view": {"card_style": view.card_style, "day_packing": view.day_packing},
        "as_of": as_of_label,
        "query_extra": _carry_query(request),
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
    }
    return templates.TemplateResponse(request, "share_calendar.html", context)


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


def _poster_refs(items: Sequence[Item]) -> list[tuple[Item, tuple[str, object]]]:
    """Each item paired with the (media, tmdb) ref its poster is filed under.

    A title carrying no tmdb id is DROPPED rather than looked up. posters.py's
    chain already falls back to a live id-lookup for the ids it does have, and
    going hunting for an id a calendar entry never carried is the one lookup a
    link preview is not worth making.
    """
    return [(item, (item.media, tmdb)) for item in items if (tmdb := item.ids.get("tmdb"))]


async def _warm_posters(settings, refs, *, budget: float | None) -> None:
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
        await posters.ensure_posters(settings, refs)
        return
    # Whatever landed inside the budget stays landed: the tiles are read back off
    # disk afterwards, so cutting this short costs the card a tile, never the
    # work already done.
    with anyio.move_on_after(budget):
        await posters.ensure_posters(settings, refs)


async def _resolve_tiles(settings, visible: Sequence[Item], *,
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

    THE ORDER THE TILES COME BACK IN IS THE DISPLAY ORDER, not the selection
    order — see `arrange_tiles`. The two are sorted separately and the
    arrangement happens LAST, after the titles with no artwork have dropped out,
    so the dates a reader sees run in order across whatever tiles survived
    rather than across the ones that were hoped for.
    """
    pairs = _poster_refs(select_tile_items(visible))
    await _warm_posters(settings, [ref for _item, ref in pairs], budget=budget)

    drawn: list[tuple[share_card.Tile, float | None]] = []
    complete = True
    for item, ref in pairs:
        path = posters.cached_poster(*ref)
        if path is not None:
            drawn.append((share_card.Tile(
                title=item.title, poster=path,
                date_label=_tile_date_label(item),
                is_premiere=tile_tier(item) == _TIER_SERIES_PREMIERE,
            ), _air_moment(item)))
        elif not posters.is_negative(*ref):
            complete = False
    return arrange_tiles(drawn), complete


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
    items, _as_of = await _read_month(view, settings, owner_prefs)
    nw_ids = await calendar_state.not_watching_ids(owner_id)
    visible = _visible_items(items, view, nw_ids)

    tiles, complete = await _resolve_tiles(settings, visible, budget=budget)
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


def _spawn_poster_warm(view: ShareView, owner_id: int, visible: Sequence[Item], settings) -> None:
    """Start resolving the artwork this month's card will want, and return
    immediately.

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
    refs = [ref for _item, ref in _poster_refs(select_tile_items(visible))]
    if not refs:
        return
    _warming.add(key)
    task = asyncio.create_task(_warm_job(key, refs, settings))
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
        return payload

    async with _card_render_slots:
        payload = await anyio.to_thread.run_sync(share_card.build_card, card)
    if complete:
        # An INCOMPLETE card is served but never kept: some of the artwork it
        # wanted was still on its way, and the next request — by which time the
        # background warm has usually landed — renders and stores the finished
        # one. That is what makes a thin card heal itself rather than being
        # cached half-empty for the whole retention window.
        await anyio.to_thread.run_sync(share_card_cache.store_render, path, payload)
    return payload


async def _warm_job(key: tuple, refs, settings) -> None:
    try:
        await _warm_posters(settings, refs, budget=None)
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
    settings = load_settings()
    if await _share_rate_limited(request, settings):
        return _too_many_requests()
    return await _render(request, await share_links.resolve_by_token(token))


# The picture this feature degrades to, and the picture it replaced: the app's
# static banner, which is also what the register and invite pages advertise.
STATIC_CARD_URL = "/static/images/tvbanner.png"

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
    settings = load_settings()
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
    settings = load_settings()
    if await _share_rate_limited(request, settings):
        return _too_many_requests()
    return await _render(request, await share_links.resolve_by_username(username))


@guard.get("/c/{slug}", AuthLevel.PUBLIC)
async def share_by_slug(request: Request, slug: str):
    settings = load_settings()
    if await _share_rate_limited(request, settings):
        return _too_many_requests()
    return await _render(request, await share_links.resolve_by_slug(slug))


# ---------------------------------------------------------------------------
# details for a card on a public page — same modal content as the calendar
# ---------------------------------------------------------------------------
# CACHE-ONLY, same as the calendar view above: this never calls Trakt. The
# owner's own calendar views already fetch and cache each show's detail (cast,
# trailer, episodes);
# this serves that cache back to visitors. A show the owner has not viewed comes
# back with empty fields and the modal renders around them — no public request
# ever spends the owner's rate limit. Rate-limited per IP like every other share
# request; no membership gate is needed because there is no fetch to amplify.

def _season_param(value) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


async def _details(request: Request, share_row) -> Response:
    settings = load_settings()
    if await _share_rate_limited(request, settings):
        return _too_many_requests()
    if share_row is None:
        return JSONResponse({"ok": False, "error": "Not found"}, status_code=404)
    media = request.query_params.get("media", "show")
    trakt_id = (request.query_params.get("id") or "").strip()
    if not trakt_id:
        return JSONResponse({"ok": False, "error": "Missing id"}, status_code=400)
    season = _season_param(request.query_params.get("season"))
    details = await trakt_detail.fetch_details(settings, media, trakt_id, season, cache_only=True)
    return JSONResponse({"ok": True, **details})


@guard.get("/s/{token}/details", AuthLevel.PUBLIC)
async def share_details_by_token(request: Request, token: str):
    return await _details(request, await share_links.resolve_by_token(token))


@guard.get("/u/{username}/details", AuthLevel.PUBLIC)
async def share_details_by_username(request: Request, username: str):
    return await _details(request, await share_links.resolve_by_username(username))


@guard.get("/c/{slug}/details", AuthLevel.PUBLIC)
async def share_details_by_slug(request: Request, slug: str):
    return await _details(request, await share_links.resolve_by_slug(slug))


# ---------------------------------------------------------------------------
# owner-facing API — the Share panel on the logged-in calendar
# ---------------------------------------------------------------------------

def _public_base(settings) -> str:
    return (settings.public_base_url or "").rstrip("/")


def _share_payload(row, username: str | None, settings) -> dict:
    base = _public_base(settings)
    return {
        "ok": True,
        # Every URL below is None without a configured base — there is no
        # request-derived fallback (the request Host isn't trustworthy enough
        # to advertise as the public origin), so the panel needs to say why the
        # link boxes are empty rather than just rendering them blank.
        "base_url_missing": not bool(base),
        "token": row["token"],
        "custom_slug": row["custom_slug"],
        "preferred_kind": row["preferred_kind"],
        "enabled": {
            "token": bool(row["enabled_token"]),
            "username": bool(row["enabled_username"]),
            "slug": bool(row["enabled_slug"]),
        },
        # The links as handed out: carrying the owner's chosen view params, which
        # is the ONLY thing those params affect — not the owner's own calendar,
        # and not the share page's fallback for a link that omits them.
        "urls": share_links.generated_urls(row, username, base),
        # None == "use my current display", i.e. hand out a bare link and let the
        # page resolve the owner's defaults.
        "link_view": share_links.link_view(row),
    }


@guard.get("/api/me/share", AuthLevel.CALENDAR_APPROVED)
async def get_share(request: Request):
    user = await auth.current_user(request)
    row = await share_links.get_or_create(user.user_id)
    return JSONResponse(_share_payload(row, user.username, load_settings()))


@guard.post("/api/me/share/enabled", AuthLevel.CALENDAR_APPROVED)
async def post_share_enabled(request: Request):
    user = await auth.current_user(request)
    data = await authz.json_body(request)
    if data.get("kind") not in share_links.PREFERRED_KINDS:
        return JSONResponse({"ok": False, "error": "Expected {kind, enabled}"}, status_code=400)
    await share_links.set_enabled(user.user_id, data["kind"], bool(data.get("enabled")))
    row = await share_links.get(user.user_id)
    return JSONResponse(_share_payload(row, user.username, load_settings()))


@guard.post("/api/me/share/active", AuthLevel.CALENDAR_APPROVED)
async def post_share_active(request: Request):
    """Publish exactly one of the three link forms and retire the other two.

    What the Share panel's single dropdown writes. The granular
    /enabled + /preferred pair is still there for a caller that wants several
    forms live at once; this is the one-link-at-a-time shape the UI presents.
    """
    user = await auth.current_user(request)
    data = await authz.json_body(request)
    if data.get("kind") not in share_links.PREFERRED_KINDS:
        return JSONResponse({"ok": False, "error": "Expected {kind}"}, status_code=400)
    await share_links.set_active_kind(user.user_id, data["kind"])
    row = await share_links.get(user.user_id)
    return JSONResponse(_share_payload(row, user.username, load_settings()))


@guard.post("/api/me/share/preferred", AuthLevel.CALENDAR_APPROVED)
async def post_share_preferred(request: Request):
    user = await auth.current_user(request)
    data = await authz.json_body(request)
    if data.get("kind") not in share_links.PREFERRED_KINDS:
        return JSONResponse({"ok": False, "error": "Expected {kind}"}, status_code=400)
    await share_links.set_preferred_kind(user.user_id, data["kind"])
    row = await share_links.get(user.user_id)
    return JSONResponse(_share_payload(row, user.username, load_settings()))


@guard.post("/api/me/share/slug", AuthLevel.CALENDAR_APPROVED)
async def post_share_slug(request: Request):
    user = await auth.current_user(request)
    data = await authz.json_body(request)
    if "slug" not in data:
        return JSONResponse({"ok": False, "error": "Expected {slug}"}, status_code=400)
    err = await share_links.set_custom_slug(user.user_id, data.get("slug"))
    if err:
        return JSONResponse({"ok": False, "error": err}, status_code=400)
    row = await share_links.get(user.user_id)
    return JSONResponse(_share_payload(row, user.username, load_settings()))


@guard.get("/api/me/share/slug-check", AuthLevel.CALENDAR_APPROVED)
async def get_share_slug_check(request: Request):
    """Live availability for the slug field, called as the owner types."""
    user = await auth.current_user(request)
    slug = (request.query_params.get("slug") or "").strip()
    if not slug:
        return JSONResponse({"ok": True, "available": False, "error": "Enter a slug."})
    err = await share_links.slug_error(slug, exclude_user_id=user.user_id)
    return JSONResponse({"ok": True, "available": err is None, "error": err})


@guard.post("/api/me/share/view", AuthLevel.CALENDAR_APPROVED)
async def post_share_view(request: Request):
    """Set (or clear) the display options the generated link carries.

    `{"view": null}` hands out a bare link, so whoever opens it sees the owner's
    current display. `{"view": {...}}` pins those options into the URL instead.
    Either way nothing about the owner's own calendar changes — this writes the
    link and only the link.
    """
    user = await auth.current_user(request)
    data = await authz.json_body(request)
    if "view" not in data:
        return JSONResponse({"ok": False, "error": "Expected {view}"}, status_code=400)
    view = data["view"]
    if view is not None and not isinstance(view, dict):
        return JSONResponse({"ok": False, "error": "Expected {view}"}, status_code=400)
    err = await share_links.set_link_view(user.user_id, view)
    if err:
        return JSONResponse({"ok": False, "error": err}, status_code=400)
    row = await share_links.get(user.user_id)
    return JSONResponse(_share_payload(row, user.username, load_settings()))


@guard.post("/api/me/share/rotate", AuthLevel.CALENDAR_APPROVED)
async def post_share_rotate(request: Request):
    user = await auth.current_user(request)
    await share_links.rotate_token(user.user_id)
    row = await share_links.get(user.user_id)
    return JSONResponse(_share_payload(row, user.username, load_settings()))
