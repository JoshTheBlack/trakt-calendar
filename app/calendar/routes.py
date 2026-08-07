"""The calendar: the month picker, the shell, one day's cards, and the small
JSON endpoints the page drives itself from.

The shell (`calendar_page`) is assembled out of five separable answers rather
than one long context build, because they are asked at different scopes and one
of them is asked twice. `assemble_month` produces the month's cards and every
number stated ABOUT the month; `_apply_day_layout` decides how one day is drawn
and is shared with /calendar/day so a day fetched late is laid out identically to
one shipped inline; `_view_preferences`, `_view_data` and `_day_chips` are three
different views of the same assembly for three different consumers — the
template, the client's own bookkeeping, and the jump-to strip.

Everything deciding WHAT a viewer may see is read from their session, never from
the query string: the per-user filters and the not-watching marks are theirs, so
no request can ask for somebody else's view or for an unfiltered one.
"""
from __future__ import annotations

# The STDLIB calendar, not the package around this module. Absolute imports mean
# the bare name resolves to the stdlib either way — this package is app.calendar,
# not calendar — but standing in app/calendar/ it reads as though it might not.
import calendar as _calendar
import dataclasses
import re
from collections import Counter
from datetime import date, datetime
from math import ceil
from urllib.parse import quote
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response

from . import (cache as calendar_cache, resolve as calendar_resolve, share_links,
               state as calendar_state)
from .. import auth, authz, chrome, clock, route_params
from ..auth import AuthLevel
from ..config import load_settings
from ..endpoints import DEFAULT_ENDPOINT, endpoint_choices, get_endpoint
from ..integrations import routes as integrations_routes
from ..media import logos
from ..perftrace import span
from ..providers.trakt import TraktError
from ..providers.trakt.detail import fetch_details, fetch_tile_info
from ..sources import prefs as source_prefs
from ..timezones import build_options as build_timezone_options
from ..templating import templates

router = APIRouter()
guard = authz.Guard(router)

# How many of a month's day blocks the calendar page renders inline. The rest are
# fetched in one request once the page has painted, so a busy month's first
# response is a few dozen cards instead of a thousand — the document the browser
# has to parse before it can show anything is what made the page slow, not the
# cards further down it that nobody has scrolled to yet.
INITIAL_DAY_BLOCKS = 5

NOT_CONFIGURED = (
    "Trakt API credentials aren't set yet. Open ⚙️ Settings to add your Client ID and Access Token."
)


def _picker_context(request: Request, settings, year: int, endpoint, user=None):
    today = clock.today()
    return {
        "request": request,
        "year": year,
        "endpoint": endpoint,
        # The picker carries the same navigation the calendar does — it is a
        # landing page people arrive on directly, and having no way from here to
        # the account or admin screens made those reachable only by typing a URL.
        "endpoints": endpoint_choices(),
        **chrome.page_context(user),
        "months": [{"num": m, "name": _calendar.month_name[m]} for m in range(1, 13)],
        "current_month": today.month if year == today.year else None,
        "today_month": today.month,
        "today_year": today.year,
    }


def _resolve_viewer_tz(user, settings) -> ZoneInfo:
    """The viewer's saved timezone, falling back to the app-wide default and then
    UTC if either name turns out to be unusable (e.g. a stale settings.json value
    predating a tzdata rename)."""
    for name in (user.timezone, settings.timezone, "UTC"):
        if not name:
            continue
        try:
            return ZoneInfo(name)
        except (ZoneInfoNotFoundError, ValueError):
            continue
    return ZoneInfo("UTC")


# How many columns a packed day's grid may grow to, per card style: the poster
# wall is compact, "poster beside" cards are wide. Mirrored by updateCols() in
# calendar/layout.js, which re-runs this per day after a toggle changes what is
# visible.
_COLUMN_CAPS = {"poster": 6, "horizontal": 2}
_COLUMN_CAP_DEFAULT = 5


def _apply_day_layout(grouped: list[dict], *, not_watching: set[str],
                      hide_not_watching: bool, card_style: str) -> None:
    """Annotate each day with the two presentation facts the client used to work
    out only AFTER the page had painted: how many columns that day's grid needs,
    and whether the day collapses entirely because everything on it is hidden.

    Both are decided from the cards the day contains, which the server knows
    before it writes them. Left to the client they land a frame late, and a day
    header that paints and then vanishes takes the rest of the month up the page
    with it — the "1 July, then suddenly 2 July" flicker on a hide-mode load.

    `rows` and `visible` are the same answer put to a different use: a day that
    has not been fetched yet is drawn as a placeholder, and it can only reserve
    the right amount of vertical space (so the scrollbar and the jump-to strip
    land where the day really is) if it knows how many rows of cards are coming."""
    cap = _COLUMN_CAPS.get(card_style, _COLUMN_CAP_DEFAULT)
    for group in grouped:
        visible = sum(1 for item in group["items"] if item.id not in not_watching)
        shown = visible if hide_not_watching else len(group["items"])
        group["cols"] = max(1, min(shown, cap))
        group["collapsed"] = hide_not_watching and visible == 0
        group["visible"] = visible
        group["rows"] = ceil(shown / group["cols"]) if shown else 1


def _filters_active(prefs: dict) -> bool:
    return bool(
        prefs["genres"] or prefs["countries"] or prefs["network_filter"]
        or prefs["show_certifications"] or prefs["movie_certifications"]
    )


def _filters_summary(prefs: dict) -> str:
    """"genre, country" — which dimensions are narrowing this calendar, for the
    header button's tooltip. Names the dimensions rather than the values, which
    can run to dozens of networks and would not fit. Show and movie
    certifications share one "certification" label — the endpoint decides which
    of the two specs is actually in play, but the tooltip is naming a dimension,
    not a value, so it does not need to distinguish them."""
    named = [
        label for label, value in (
            ("genre", prefs["genres"]),
            ("country", prefs["countries"]),
            ("certification", prefs["show_certifications"] or prefs["movie_certifications"]),
            ("network", prefs["network_filter"]),
        ) if value
    ]
    return ", ".join(named)


def _requested_endpoint(request: Request, prefs: dict, settings):
    """The calendar this request is about. Always resolved through get_endpoint,
    which falls back to the default for anything not in the calendar set, so a
    made-up key can never reach a cache row or a Trakt URL."""
    return get_endpoint(
        request.query_params.get("endpoint") or prefs["endpoint"] or settings.endpoint or DEFAULT_ENDPOINT
    )


async def _viewer_source_selection(request: Request, user) -> source_prefs.SourcePrefs:
    """This viewer's saved source preferences, with `calendar_source` swapped
    for a `?source=` override when the query names one of the app's own
    selections.

    THE OVERRIDE IS NEVER PERSISTED. It exists so a coverage-gap message (see
    _coverage_gap below) can offer "show Trakt instead" as a link for THIS
    view only, without silently rewriting the account's saved preference — the
    same reason a share link's own view parameters never write back to the
    owner's row. An unrecognized value is ignored rather than refused, since
    this is a read of the calendar and not a form that owes the visitor an
    error for a stray query string.
    """
    saved = await source_prefs.load(user.user_id)
    requested = request.query_params.get("source")
    if requested and source_prefs.is_selection(requested):
        # The override replaces the account-wide value AND clears the per-
        # endpoint ones: a link that says "show me Trakt instead" has to mean
        # that on the calendar the reader is looking at, and a stored override
        # for that endpoint would quietly win over the thing they just clicked.
        return dataclasses.replace(saved, calendar_source=requested,
                                   endpoint_sources={})
    return saved


def _source_choices(endpoint, requested, settings) -> list[dict]:
    """The toolbar's source control: what it offers, which option is on, and
    whether it is drawn at all. An empty list means the toolbar draws nothing.

    A VIEW CONTROL AND NOT A PREFERENCE, which is the whole of why it is built
    here out of the query string rather than out of the stored row. `?source=`
    has always been a transient override (see `_viewer_source_selection`); this
    is the way to reach it without typing one. Choosing something re-reads THIS
    page and writes nothing — the account's answer is stated on /sources, in one
    place, and a control on the calendar that quietly rewrote it would change
    every other view the account has as a side effect of a look.

    THE FIRST OPTION IS THE STORED ANSWER, unnamed, because it is whatever the
    account said and this control is not the place that says it. It is also the
    way back: without it, overriding once would leave no way to stop overriding
    short of editing the address.

    IT OFFERS EXACTLY THE SERVICES THAT COULD ACTUALLY FILL THIS CALENDAR, which
    is two questions and neither of them is asked here. Whether a service is on
    this INSTANCE's calendar at all belongs to `providers.calendar_sources`, and
    it is asked through `resolve.instance_sources` so that an operator switching
    a service off empties this control by the same rule that empties the
    calendar underneath it — a second spelling of that question living here is a
    copy that answers differently the first time either is edited. Whether it
    publishes THIS calendar is `capabilities.answers`. No service is named in
    either check, so a third one is offered the day it is registered.

    AND WITH FEWER THAN TWO IT IS NOT DRAWN. One service that can answer means
    "my sources", "every service" and "that one only" are three labels for one
    outcome, and offering them makes the page look as though a choice is
    available when none is. Nothing else on the toolbar is drawn to be inert
    either.

    THE COST OF NOT DRAWING IT, taken deliberately: a `?source=` in the address
    on a single-service calendar has no control to clear it with. That override
    only arrives by following a link, switching calendars drops it (the picker
    beside this one carries no source), and the alternative was to keep drawing
    a control whose whole purpose on that page would be to undo something the
    reader did not do.
    """
    from .. import providers  # deferred: see _coverage_gap, same reason

    admitted = calendar_resolve.instance_sources(settings)
    chosen = requested if requested and source_prefs.is_selection(requested) else ""
    services = [(str(source), provider.label)
                for source, provider in providers.registered().items()
                if provider.capabilities.answers(endpoint.key)
                and (admitted is None or str(source) in admitted)]
    if len(services) < 2:
        return []
    choices = [
        {"value": "", "label": "My sources"},
        {"value": source_prefs.AUTO, "label": "Every service"},
    ]
    choices += [{"value": value, "label": f"{label} only"} for value, label in services]
    # An override this calendar does not offer — a named pair, or a service that
    # answers a different endpoint — is still in force, so it is shown rather
    # than silently reading as the stored answer it is currently overriding.
    offered = {source_prefs.AUTO, *(value for value, _ in services)}
    if chosen and chosen not in offered:
        choices.append({"value": chosen, "label": chosen.replace(
            source_prefs.SEPARATOR, " and ").title()})
    for choice in choices:
        choice["selected"] = choice["value"] == chosen
    return choices


def _coverage_gap(prefs: source_prefs.SourcePrefs,
                  year: int, month: int, settings, *, endpoint=None,
                  ) -> tuple[str | None, str | None]:
    """(message, switch_url) when this viewer has named ONE calendar source and
    that source's declared reach does not cover {year, month} at all — the
    explicit "Simkl doesn't reach this month" state. (None, None) otherwise,
    including for 'auto' and 'both', which are never a single source's
    promise to keep.

    ROUTE-LEVEL BY DESIGN. The fill itself (app/providers/__init__.py's
    calendar_sources, app/calendar/cache.py's _covers) already skips a source
    outside its window with no route learning a date range belonging to a
    particular service — so a month simply renders short when several sources
    are admitted, which is correct and needs no message. Naming ONE source and
    getting nothing back is different: an empty calendar reads as "nothing airs
    then" when the honest answer is "this source does not reach that far", and
    only the route knows which of those happened.

    IT ASKS THE ENDPOINT'S OWN SELECTION, not the account-wide one, because a
    per-calendar override is what actually governs what this page will show. A
    viewer whose movies calendar alone names one service would otherwise get an
    unexplained empty month while the account-wide value said several services
    were answering.
    """
    from .. import providers  # deferred: see the DECLARED_EDGES note for CALENDAR -> SOURCES

    selection = prefs.calendar_selection(endpoint.key if endpoint is not None else None)
    named_sources = source_prefs.named_sources(selection)
    if named_sources is None or len(named_sources) != 1:
        return None, None
    admitted = providers.calendar_sources(prefs=prefs, settings=settings, endpoint=endpoint)
    if not admitted or any(calendar_cache.month_covered(p, year, month) for p in admitted):
        return None, None
    named = admitted[0].label
    message = f"{named}'s calendar doesn't reach {_calendar.month_name[month]} {year}."
    alternatives = [p for p in providers.registered().values()
                   if str(p.source) not in named_sources]
    switch_url = None
    if alternatives:
        switch_url = f"?year={year}&month={month}&source={alternatives[0].source}"
    return message, switch_url


@dataclasses.dataclass
class MonthAssembly:
    """A month's cards plus every number the shell states ABOUT that month.

    One record rather than the nine loose locals this used to be, because the
    stats tiles, the "since last run" line, the is-new marks and the day chips
    are all claims about the SAME assembly and must agree. On a failure they must
    all describe an empty month, and the defaults here are what guarantees that —
    nine separately-initialized names is a shape where one of them can be left
    describing the month before last.

    `error` and a non-empty `grouped` can both be set: a month that assembled and
    then failed while working out what changed since the last visit still has real
    cards to show, and saying so beats throwing them away.
    """
    grouped: list[dict] = dataclasses.field(default_factory=list)
    total: int = 0
    watching: int = 0
    not_watching_count: int = 0
    # The month rendered, but at least one window's data couldn't be loaded, so it
    # may be missing days. A warning, distinct from `error`.
    partial: bool = False
    new_ids: set[str] = dataclasses.field(default_factory=set)
    delta: dict = dataclasses.field(default_factory=lambda: {"text": "", "kind": "none"})
    history: list[dict] = dataclasses.field(default_factory=list)
    # How many cards each show has this month. The stats tiles need it to keep
    # counting correctly when one toggle flips a show that airs on a dozen days —
    # without asking the DOM, which only ever knows about the cards it holds.
    show_counts: dict[str, int] = dataclasses.field(default_factory=dict)
    error: str | None = None
    # A Simkl-only month outside the declared coverage window's explicit
    # empty state: set together, and only together, by _coverage_gap.
    # `error` still carries the message a viewer reads — this
    # pair is what the template uses to also offer the switch-source link,
    # which a plain "not configured" error has none of.
    coverage_gap: bool = False
    switch_url: str | None = None
    # How many of THIS month's cards are still waiting for a source's catalogue
    # to be read — see app/calendar/enrich.py. They are on the page rather than
    # filtered out, because the per-viewer genre/country filter exempts a record
    # nobody has looked up yet rather than judging it on values it cannot answer
    # for. So a viewer whose filters are narrow can be looking at titles their
    # own filters would remove, and the honest thing is to say so and let the
    # count fall to zero on its own as the background drain catches up.
    unenriched: int = 0


async def assemble_month(user, settings, prefs: dict, endpoint, tz: ZoneInfo,
                         year: int, month: int, not_watching: set[str],
                         source_selection: source_prefs.SourcePrefs,
                         ) -> MonthAssembly:
    """Fetch, filter and group a WHOLE month, and resolve what changed since this
    viewer last looked at it.

    The whole month is assembled even though the shell only paints the first few
    days of it, because the numbers the shell states — the tiles, the per-day chip
    counts, which shows are new — are claims about the whole month and would be
    wrong if they described only the days that happen to be rendered.
    """
    assembly = MonthAssembly()
    if not settings.calendar_source_configured:
        assembly.error = NOT_CONFIGURED
        return assembly

    message, switch_url = _coverage_gap(source_selection, year, month, settings,
                                        endpoint=endpoint)
    if message is not None:
        assembly.coverage_gap = True
        assembly.switch_url = switch_url
        assembly.error = message
        return assembly

    days = _calendar.monthrange(year, month)[1]
    try:
        # The whole month is fetched, filtered, and grouped before any HTML is
        # sent, so this span is the server-side "time to first byte" for the
        # calendar — dominated by the per-window Trakt fetch on a cold cache
        # (now concurrent across the windows, not one await at a time).
        with span("calendar.read_month", endpoint=endpoint.key, ym=f"{year}-{month:02d}") as sp:
            assembly.grouped, meta = await calendar_cache.assemble_range(
                endpoint, settings, tz=tz,
                start_date=date(year, month, 1), end_date=date(year, month, days),
                genres=prefs["genres"], countries=prefs["countries"],
                show_certifications=prefs["show_certifications"],
                movie_certifications=prefs["movie_certifications"],
                network_filter=prefs["network_filter"] or None,
                not_watching_ids=not_watching,
                prefs=source_selection,
            )
            sp.set(items=meta["total"])
        assembly.total = meta["total"]
        assembly.watching = meta["watching"]
        assembly.not_watching_count = meta["not_watching"]
        # A window Trakt couldn't supply is skipped rather than failing the
        # whole month; flag it so the page can say the month is incomplete
        # instead of silently showing a short one.
        assembly.partial = meta["partial"]
        assembly.unenriched = meta["unenriched"]
        assembly.show_counts = Counter(
            item.id for group in assembly.grouped for item in group["items"])
        # The is-new diff and its baseline commit belong to whoever produced
        # the cards, over the SERVER's full id list. Skipped on the error
        # paths: committing an empty month as the baseline would make the whole
        # month look new the next time it loads properly.
        view_state = await calendar_state.resolve_view(
            user.user_id, endpoint.key, year, month,
            show_ids=meta["show_ids"], total=assembly.total, now=datetime.now(tz),
        )
        assembly.new_ids = view_state["new_ids"]
        assembly.delta = view_state["delta"]
        assembly.history = view_state["history"]
    except TraktError as exc:
        assembly.error = str(exc)
    return assembly


def _view_preferences(prefs: dict, settings) -> dict:
    """Per-user view preferences (card style, day packing, hide-not-watching) —
    distinct from `settings`, which stays the app-wide defaults new accounts are
    seeded from and the admin Settings screen's own values."""
    return {
        "card_style": prefs["card_style"] or settings.card_style,
        "day_packing": prefs["day_packing"] or settings.day_packing,
        "hide_not_watching": prefs["hide_not_watching"],
        # Whether this viewer's month has been narrowed, and by what. The header
        # button reads both: a filter's only other evidence is the shows that
        # aren't there, which is indistinguishable from Trakt not listing them.
        "filters_active": _filters_active(prefs),
        "filters_summary": _filters_summary(prefs),
    }


def _view_data(assembly: MonthAssembly, not_watching: set[str]) -> dict:
    """The shell's numbers again as DATA rather than markup, so the client can keep
    the stats tiles honest through a toggle without asking the DOM — and so a day
    that arrives later can mark is-new from the whole month's answer instead of
    recomputing a diff it cannot see all of."""
    return {
        "newIds": sorted(assembly.new_ids),
        "showCounts": dict(assembly.show_counts),
        "notWatching": sorted(nw for nw in not_watching if nw in assembly.show_counts),
        "watching": assembly.watching,
        "notWatchingCount": assembly.not_watching_count,
    }


def _day_chips(assembly: MonthAssembly, year: int, month: int, days: int,
               not_watching: set[str], hide_not_watching: bool) -> list[dict]:
    """One chip per day of the month for the jump-to strip.

    `count` is what the day holds; `shown` is what THIS viewer will see of it, and
    a day showing nothing has no section to scroll to, so its chip renders inert.
    With hide-not-watching on, a day whose every item is marked renders nothing at
    all, so its chip must not offer to scroll somewhere blank. calendar/layout.js
    keeps this in step when the viewer toggles hiding or marks a show without
    reloading.
    """
    counts = {group["date"]: len(group["items"]) for group in assembly.grouped}
    shown = {
        group["date"]: sum(1 for item in group["items"] if item.id not in not_watching)
        for group in assembly.grouped
    } if hide_not_watching else counts
    chips = []
    for day in range(1, days + 1):
        iso = f"{year}-{month:02d}-{day:02d}"
        chips.append({"day": day, "date": iso,
                      "count": counts.get(iso, 0), "shown": shown.get(iso, 0)})
    return chips


@guard.get("/", AuthLevel.CALENDAR_APPROVED)
async def home(request: Request):
    """The month/year picker landing page, and its ONLY address.

    It answered at /pick as well for a while, rendering this same template from
    this same context — two URLs for one page, which meant a link, a bookmark or
    a browser history entry could disagree about where the picker is. Everything
    that pointed at /pick now points here.

    A `month` in the query is an old calendar link — a bookmark, a shared URL, or
    a Discord post from when this one route served both the picker and the
    calendar. Forward it rather than asking someone to choose the month they
    already named."""
    settings = load_settings()
    # Already resolved and cached by the dependency that let this request in.
    user = await auth.current_user(request)
    prefs = await auth.get_user_prefs(user.user_id)
    year = route_params.valid_year(request.query_params.get("year"), clock.today().year)
    endpoint = _requested_endpoint(request, prefs, settings)
    if route_params.month_given(request.query_params.get("month")):
        month = route_params.valid_month(request.query_params.get("month"), clock.today().month)
        return RedirectResponse(
            f"/calendar?month={month}&year={year}&endpoint={endpoint.key}", status_code=302)
    return templates.TemplateResponse(
        request, "pick.html", _picker_context(request, settings, year, endpoint, user))


@guard.get("/calendar", AuthLevel.CALENDAR_APPROVED)
async def calendar_page(request: Request):
    """The calendar SHELL: the header, the stats bar, the jump-to strip, the
    modals — and the first few days of cards inline. Every day after those is
    announced as a placeholder that fetches itself from /calendar/day when the
    viewer reaches it, so the browser has a usable page long before a whole month
    of cards exists, and a day nobody scrolls to is never built at all."""
    settings = load_settings()
    user = await auth.current_user(request)
    is_admin = bool(user and user.is_admin)
    prefs = await auth.get_user_prefs(user.user_id)
    today = clock.today()
    year = route_params.valid_year(request.query_params.get("year"), today.year)
    endpoint = _requested_endpoint(request, prefs, settings)
    month = route_params.valid_month(request.query_params.get("month"), today.month)
    tz = _resolve_viewer_tz(user, settings)
    days = _calendar.monthrange(year, month)[1]

    # Which calendar source(s) this VIEWER reads from — the calendar half of the
    # same source-preference seam the tracker already reads
    # (app/sources/prefs.py). What they have LINKED is deliberately not part of
    # it: a calendar needs no viewer credential, so linkage narrows the tracker
    # and nothing here (app/sources/prefs.py's `admits_calendar`).
    source_selection = await _viewer_source_selection(request, user)

    # This viewer's marks, read ONCE and handed to the assembly so the cards come
    # out of the template already carrying the class. The client used to add it
    # after the page had painted, which is what made hidden items visibly pop out.
    not_watching = await calendar_state.not_watching_ids(user.user_id)
    month_view = await assemble_month(
        user, settings, prefs, endpoint, tz, year, month, not_watching,
        source_selection)
    view = _view_preferences(prefs, settings)

    _apply_day_layout(month_view.grouped, not_watching=not_watching,
                      hide_not_watching=view["hide_not_watching"],
                      card_style=view["card_style"])

    # Only the first few days go out with the shell; every day after them is a
    # placeholder that fetches its own cards when it is scrolled to. So first
    # paint costs a handful of cards instead of a month of them, and a day nobody
    # ever scrolls to is never assembled, rendered, or shipped at all.
    #
    # The split is by DAY BLOCK rather than by date, because a month can open with
    # a run of empty days and "the first five dates" would then ship nothing.
    inline_groups = month_view.grouped[:INITIAL_DAY_BLOCKS]
    skeleton_groups = month_view.grouped[INITIAL_DAY_BLOCKS:]
    for group in skeleton_groups:
        group["url"] = _day_url(endpoint.key, date.fromisoformat(group["date"]),
                                source=request.query_params.get("source"))

    context = {
        "request": request,
        "settings": settings,
        "view": view,
        "endpoint": endpoint,
        "endpoints": endpoint_choices(),
        # The toolbar's source control. Built from the query string, applying to
        # this view alone, and storing nothing — see _source_choices. Empty when
        # fewer than two services could fill this calendar, and the template
        # draws nothing at all rather than an inert control.
        "source_choices": _source_choices(
            endpoint, request.query_params.get("source"), settings),
        "timezone_groups": build_timezone_options(settings.timezone),
        "viewer_timezone_groups": build_timezone_options(tz.key),
        "year": year,
        "month": month,
        "month_label": _calendar.month_name[month],
        # For the Share panel's "opens on" month picker, which names months
        # rather than numbering them.
        "month_names": [_calendar.month_name[m] for m in range(1, 13)],
        "nav": route_params.adjacent_months(year, month),
        # The days rendered INLINE. The whole month is what every number on the
        # page is computed from; this is only what is painted now.
        "grouped": inline_groups,
        # The days that are announced but not yet fetched: header, chip target and
        # reserved height now, cards when the viewer reaches them.
        "skeletons": skeleton_groups,
        "total": month_view.total,
        # The stats tiles, the is-new marks, the "since last run" line and the
        # history log are all computed above and rendered with the page, so they
        # are right at first paint and stay right when only part of a month is on
        # screen. The card partial reads these two sets by membership.
        "not_watching": not_watching,
        "new_ids": month_view.new_ids,
        "stats": {"total": month_view.total, "watching": month_view.watching,
                  "not_watching": month_view.not_watching_count},
        "delta": month_view.delta,
        "history": month_view.history,
        "view_data": _view_data(month_view, not_watching),
        "day_chips": _day_chips(month_view, year, month, days,
                                not_watching, view["hide_not_watching"]),
        "error": month_view.error,
        "partial": month_view.partial,
        "unenriched": month_view.unenriched,
        # A Simkl-only month outside its declared coverage window renders
        # this explicit state rather than a blank calendar. `switch_url` is
        # the query string to append to THIS page's own URL to preview the
        # other source without saving anything.
        "coverage_gap": month_view.coverage_gap,
        "switch_url": month_view.switch_url,
        "generated": datetime.now().strftime("%H:%M"),
        # Sonarr/Radarr/Seerr writes land in the operator's own shared libraries
        # and Seerr's requests all carry one app-wide API key, so they are an
        # administrator's affordance. The buttons and health state are left out
        # of the page entirely for everyone else rather than rendered into a
        # guaranteed 403.
        # is_admin, calendar_available, ranker_available, version, build
        # for the shared header.
        **chrome.page_context(user),
        # The same two conditions the tracker's own access level enforces, asked
        # here so the easter egg knows whether it has anywhere to send this
        # person. Resolved from the session rather than probed over HTTP: an
        # endpoint answering "may I?" is itself a disclosure that there is
        # something to be allowed into. Note this gates the REVEAL, not the menu
        # item — see _nav.html.
        "distrakt_available": bool(user and user.distrakt_approved and user.has_tracker_identity),
        "integrations": integrations_routes.INTEGRATION_HEALTH if is_admin else {},
    }
    # Jinja renders eagerly when the response is built, so this span is the cost of
    # turning the shell's cards into HTML — the other half of the server's blocking
    # time before the browser gets anything. It is now bounded by the inline day
    # count rather than growing with the whole month.
    with span("calendar.render", cards=sum(len(g["items"]) for g in inline_groups)):
        response = templates.TemplateResponse(request, "index.html", context)
    return response


_YMD = re.compile(r"\d{4}-\d{2}-\d{2}")


def _month_date(value, year: int, month: int) -> date | None:
    """A YYYY-MM-DD query parameter, accepted ONLY if it is exactly that shape and
    falls inside {year, month}. Anything else is None, and the caller refuses the
    request: this value reaches date arithmetic and a cache read, so it is checked
    against the month being viewed rather than merely parsed."""
    # Matched before parsing rather than left to fromisoformat, which also accepts
    # ISO week dates ("2026-W28-1") — a second spelling of the same day is one more
    # shape of input reaching a cache key for no benefit.
    if not isinstance(value, str) or not _YMD.fullmatch(value):
        return None
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        return None
    if (parsed.year, parsed.month) != (year, month):
        return None
    return parsed


def _day_url(endpoint_key: str, day: date, *, source: str | None = None) -> str:
    """The content request for one day. Built in one place because the shell's
    placeholder and the retry button on a day that failed must ask for exactly the
    same thing.

    `source` carries the shell's own `?source=` override forward, when it has
    one, so a day fetched after the fact reads from the same source selection
    the shell resolved the month with — the shell never puts one in a
    placeholder's URL for a viewer who never overrode anything, so the common
    case is unchanged."""
    url = (f"/calendar/day?endpoint={quote(endpoint_key)}"
          f"&year={day.year}&month={day.month}&date={day.isoformat()}")
    if source:
        url += f"&source={quote(source)}"
    return url


@guard.get("/calendar/day", AuthLevel.CALENDAR_APPROVED)
async def calendar_day(request: Request):
    """ONE day's block — the content half of the calendar, rendered through the
    same day-block and card partials the shell uses, so a day that arrives this
    way is byte-identical to one the shell rendered inline.

    Only that day is assembled: the covering window(s) are read and only their
    entries are normalized, so a month the viewer never scrolls through is never
    built. A viewer-local day can straddle two UTC windows, which assemble_range
    already accounts for.

    Everything that decides WHAT this viewer may see comes from their session: the
    per-user filters and their not-watching marks are read here, never taken from
    the query, so this cannot be asked for someone else's view or for an
    unfiltered day. The query only says WHICH day of WHICH calendar, and both are
    validated before they reach a cache key.

    is-new is deliberately NOT computed here. The diff and its baseline commit are
    a whole-month decision the shell already made and wrote; re-reading the
    baseline from a fragment would see the shell's own commit and conclude nothing
    is new. The shell embeds the ids it decided were new and the page marks these
    cards from that one answer."""
    settings = load_settings()
    user = await auth.current_user(request)
    prefs = await auth.get_user_prefs(user.user_id)
    today = clock.today()
    year = route_params.valid_year(request.query_params.get("year"), today.year)
    month = route_params.valid_month(request.query_params.get("month"), today.month)
    endpoint = _requested_endpoint(request, prefs, settings)
    day = _month_date(request.query_params.get("date"), year, month)
    if day is None:
        return JSONResponse({"ok": False, "error": "Invalid date"}, status_code=400)
    if not settings.calendar_source_configured:
        return JSONResponse({"ok": False, "error": "Not configured"}, status_code=400)

    # Same source selection the shell resolved for this month — see _day_url:
    # the placeholder it built for this day already carries the same `source`
    # override, so this read asks the same source(s) the shell's own numbers
    # for the month were computed from.
    source_selection = await _viewer_source_selection(request, user)

    tz = _resolve_viewer_tz(user, settings)
    not_watching = await calendar_state.not_watching_ids(user.user_id)
    context = {
        "request": request, "not_watching": not_watching,
        # Empty on purpose: see the docstring — the shell owns the is-new answer.
        "new_ids": set(),
        "settings": settings, "is_admin": bool(user and user.is_admin),
        "date": day.isoformat(), "label": calendar_cache.day_label(day),
        "retry_url": _day_url(endpoint.key, day, source=request.query_params.get("source")),
        "partial": False,
    }
    try:
        with span("calendar.day", endpoint=endpoint.key, day=day.isoformat()) as sp:
            grouped, meta = await calendar_cache.assemble_range(
                endpoint, settings, tz=tz, start_date=day, end_date=day,
                genres=prefs["genres"], countries=prefs["countries"],
                show_certifications=prefs["show_certifications"],
                movie_certifications=prefs["movie_certifications"],
                network_filter=prefs["network_filter"] or None,
                not_watching_ids=not_watching,
                prefs=source_selection,
            )
            sp.set(items=meta["total"])
        # Same per-day presentation the shell's own blocks were rendered with, so a
        # day that arrives late is laid out correctly on arrival rather than being
        # re-packed (and, in hide mode, collapsed) a frame after it appears.
        _apply_day_layout(grouped, not_watching=not_watching,
                          hide_not_watching=prefs["hide_not_watching"],
                          card_style=prefs["card_style"] or settings.card_style)
    except TraktError as exc:
        # The shell is already on screen with the month's real numbers, so one day
        # failing is a gap in the month, not a broken page. It replaces itself with
        # a block that says so and offers to try again, rather than sitting there
        # as a placeholder that looks like it is still loading.
        return templates.TemplateResponse(
            request, "_day_fragment.html", {**context, "group": None, "error": str(exc)})

    return templates.TemplateResponse(
        request, "_day_fragment.html",
        # A day with nothing on it groups to nothing; the fragment then renders
        # empty and the placeholder it replaces simply disappears, which is what an
        # empty day should look like.
        {**context, "group": grouped[0] if grouped else None,
         "error": None, "partial": meta["partial"]},
    )


@guard.get("/api/tile", AuthLevel.CALENDAR_APPROVED)
async def api_tile(request: Request):
    """Compact season info for a tile.

    Gated on the CATALOGUE credential, not on the instance's access token: a
    season's episode list is public, globally cached and the same for everybody
    (app/providers/trakt/detail.py), so it must not stop working because a token
    lapsed.
    """
    settings = load_settings()
    if not settings.trakt_catalogue_configured:
        return JSONResponse({"ok": False, "error": "Not configured"}, status_code=400)
    media = request.query_params.get("media", "show")
    trakt_id = request.query_params.get("id")
    if not trakt_id:
        return JSONResponse({"ok": False, "error": "Missing id"}, status_code=400)
    try:
        info = await fetch_tile_info(
            settings, media, trakt_id, route_params.season(request.query_params.get("season")))
    except TraktError as exc:
        # A transport failure (rate-limit or unreachable) now raises rather than
        # returning a benign empty tile, so a 429 can't render as "no episodes".
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=exc.status or 502)
    return JSONResponse({"ok": True, **info})


@guard.get("/api/details", AuthLevel.CALENDAR_APPROVED)
async def api_details(request: Request):
    """Full detail payload for the modal.

    Catalogue credential only, for the same reason as /api/tile: everything this
    returns — overview, cast, the episode list — is public and shared.
    """
    settings = load_settings()
    if not settings.trakt_catalogue_configured:
        return JSONResponse({"ok": False, "error": "Not configured"}, status_code=400)
    media = request.query_params.get("media", "show")
    trakt_id = request.query_params.get("id")
    if not trakt_id:
        return JSONResponse({"ok": False, "error": "Missing id"}, status_code=400)
    try:
        details = await fetch_details(
            settings, media, trakt_id, route_params.season(request.query_params.get("season")))
    except TraktError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=exc.status or 502)
    return JSONResponse({"ok": True, **details})


@guard.get("/api/state", AuthLevel.CALENDAR_APPROVED)
async def get_state(request: Request):
    user = await auth.current_user(request)
    today = clock.today()
    year = route_params.valid_year(request.query_params.get("year"), today.year)
    month = route_params.valid_month(request.query_params.get("month"), today.month)
    endpoint = get_endpoint(request.query_params.get("endpoint"))
    return JSONResponse(await calendar_state.load_state(user.user_id, endpoint.key, year, month))


@guard.post("/api/state", AuthLevel.CALENDAR_APPROVED)
async def post_state(request: Request):
    """A DELTA endpoint, not a whole-document replace.

    Two independent payload shapes, dispatched on which keys are present:
    `{item_id, not_watching}` toggles a single SHOW for this user everywhere, and
    `{last_count, last_show_ids, history?}` records this load's change-detection
    baseline for the endpoint/year/month in the query string. Sending only the
    piece that actually changed — instead of the whole notWatching array — is
    what stops one open tab's save from clobbering a mark a second tab just made;
    each write is its own INSERT/DELETE or UPDATE, not a read-modify-write of a
    shared document.

    The endpoint/year/month params are read for the baseline only. A not-watching
    mark has no month in it: it is a statement about the show.
    """
    user = await auth.current_user(request)
    today = clock.today()
    year = route_params.valid_year(request.query_params.get("year"), today.year)
    month = route_params.valid_month(request.query_params.get("month"), today.month)
    endpoint = get_endpoint(request.query_params.get("endpoint"))
    payload = await authz.json_body(request)

    if "item_id" in payload:
        item_id = str(payload.get("item_id") or "")
        if not item_id:
            return JSONResponse({"ok": False, "error": "Missing item_id"}, status_code=400)
        await calendar_state.set_not_watching(
            user.user_id, item_id, bool(payload.get("not_watching")),
        )
        return JSONResponse({"ok": True})

    if "last_count" in payload or "last_show_ids" in payload:
        last_count = payload.get("last_count")
        try:
            last_count = int(last_count) if last_count is not None else None
        except (TypeError, ValueError):
            return JSONResponse({"ok": False, "error": "last_count must be a whole number"}, status_code=400)
        last_show_ids = payload.get("last_show_ids")
        if last_show_ids is not None and not isinstance(last_show_ids, list):
            return JSONResponse({"ok": False, "error": "last_show_ids must be a list"}, status_code=400)
        history = payload.get("history")
        if history is not None and not isinstance(history, list):
            return JSONResponse({"ok": False, "error": "history must be a list"}, status_code=400)
        await calendar_state.set_view_state(
            user.user_id, endpoint.key, year, month,
            last_count=last_count, last_show_ids=last_show_ids, history=history,
        )
        return JSONResponse({"ok": True})

    return JSONResponse(
        {"ok": False, "error": "Expected item_id/not_watching or last_count/last_show_ids"},
        status_code=400,
    )


_CARD_STYLES = ("vertical", "horizontal", "poster")
_DAY_PACKINGS = ("stacked", "packed")


def _filter_spec(value) -> str:
    """Normalize a `-anime, drama` genre/country spec to comma-separated tokens.

    Only tidying — app/calendar/filter.py lowercases and splits on ',' itself, so
    this exists to keep what the user typed from being echoed back with the empty
    tokens and ragged spacing a half-edited list leaves behind.
    """
    return ", ".join(t for t in (s.strip() for s in str(value or "").split(",")) if t)


def _network_list(value) -> list[str]:
    """Networks as a de-duplicated list, from either a JSON array or the comma
    string the textarea produces. Names are matched exactly on the read path, so
    they keep their case; the duplicate check does not."""
    raw = [str(v) for v in value] if isinstance(value, list) else str(value or "").split(",")
    seen: set[str] = set()
    names: list[str] = []
    for name in (item.strip() for item in raw):
        if name and name.lower() not in seen:
            seen.add(name.lower())
            names.append(name)
    return names


@guard.get("/api/me/prefs", AuthLevel.CALENDAR_APPROVED)
async def get_me_prefs(request: Request):
    """The viewer's own view preferences, for the Filters panel to populate from.

    Separate from /api/settings, which is ADMIN-only and describes the instance.
    These are the values that actually filter this person's calendar, so every
    signed-in account has to be able to read them back.
    """
    user = await auth.current_user(request)
    prefs = await auth.get_user_prefs(user.user_id)
    return JSONResponse({"ok": True, "prefs": prefs})


@guard.post("/api/me/prefs", AuthLevel.CALENDAR_APPROVED)
async def post_me_prefs(request: Request):
    """Persist a partial update to the viewer's own calendar view preferences.

    Card style, day packing, and hide-not-watching used to write settings.json —
    an admin-only file — so anyone else's choice applied for one page load and
    was gone on the next. This writes user_prefs instead, so it sticks for every
    account. The genre/country/network filters are here for the same reason: they
    were only editable on the admin Settings screen, which wrote the app-wide
    seed and so changed nothing for the admin's own calendar and offered nobody
    else any way to filter at all.

    Also mirrors into share_links when the user has ever opened the Share panel:
    that table's owner-default columns are seeded from user_prefs at creation and
    otherwise have no editor of their own, so keeping them in sync here is what
    makes a public share page track the owner's own view without a second save
    action (see app/calendar/share_links.py's module docstring).
    """
    user = await auth.current_user(request)
    data = await authz.json_body(request)

    updates: dict = {}
    if "card_style" in data and data["card_style"] in _CARD_STYLES:
        updates["card_style"] = data["card_style"]
    if "day_packing" in data and data["day_packing"] in _DAY_PACKINGS:
        updates["day_packing"] = data["day_packing"]
    if "hide_not_watching" in data:
        updates["hide_not_watching"] = bool(data["hide_not_watching"])
    # Present-but-empty is a real value here — it is how a filter is CLEARED —
    # so these key off presence rather than truthiness.
    if "genres" in data:
        updates["genres"] = _filter_spec(data["genres"])
    if "countries" in data:
        updates["countries"] = _filter_spec(data["countries"])
    if "show_certifications" in data:
        updates["show_certifications"] = _filter_spec(data["show_certifications"])
    if "movie_certifications" in data:
        updates["movie_certifications"] = _filter_spec(data["movie_certifications"])
    if "network_filter" in data:
        updates["network_filter"] = _network_list(data["network_filter"])
    if not updates:
        return JSONResponse({"ok": False, "error": "Nothing to update"}, status_code=400)
    await auth.update_user_prefs(user.user_id, **updates)
    if await share_links.get(user.user_id) is not None:
        await share_links.update_owner_defaults(user.user_id, **updates)
    return JSONResponse({"ok": True})


@guard.post("/api/me/timezone", AuthLevel.CALENDAR_APPROVED)
async def post_me_timezone(request: Request):
    """Persist the viewer's calendar timezone.

    No automatic browser detection — this is reached either by picking a
    zone from the header's dropdown, or by the "use my device timezone" button
    filling in Intl's resolved zone name before the same request fires.
    """
    user = await auth.current_user(request)
    data = await authz.json_body(request)
    tz_name = str((data or {}).get("timezone") or "").strip()
    if not tz_name:
        return JSONResponse({"ok": False, "error": "Missing timezone"}, status_code=400)
    try:
        ZoneInfo(tz_name)
    except (ZoneInfoNotFoundError, ValueError):
        return JSONResponse({"ok": False, "error": "Unknown timezone"}, status_code=400)
    await auth.set_user_timezone(user.user_id, tz_name)
    # Mirrored into share_links the same way post_me_prefs does, once the user
    # has a share row at all — see that route's docstring.
    if await share_links.get(user.user_id) is not None:
        await share_links.update_owner_defaults(user.user_id, timezone=tz_name)
    return JSONResponse({"ok": True})


# `private` rather than `public`: this response now requires a session, so a
# shared cache in front of the app has no business holding a copy.
_LOGO_CACHE_HEADERS = {"Cache-Control": "private, max-age=86400"}


@guard.get("/api/network-logo", AuthLevel.CALENDAR_APPROVED)
async def api_network_logo(request: Request):
    """A processed network-logo PNG tile for a network name (calendar + distrakt).

    Calendar-level, which is where these are overwhelmingly requested from. The
    distrakt page shows them too, so a user approved for distrakt but NOT for the
    calendar sees the emoji fallback there instead of logos — the same thing that
    happens for any network without a logo, and not worth a second access level.

    Generates it from TMDB on first request when `tmdb` is supplied and a TMDB key
    is set; serves the disk cache thereafter. 404 -> the caller falls back to the
    emoji/text tag."""
    name = (request.query_params.get("name") or "").strip()
    tmdb = request.query_params.get("tmdb")
    if not name:
        return Response(status_code=404)
    path = logos.cached_tile(name)
    if path is None and not logos.is_negative(name):
        path = await logos.ensure_logo(load_settings(), name, tmdb)
    if path is None or not path.exists():
        return Response(status_code=404, headers=_LOGO_CACHE_HEADERS)
    # ?download=1 -> attachment (for the emoji-map "download logo" button).
    filename = f"{logos.slug(name)}.png" if request.query_params.get("download") else None
    return FileResponse(path, media_type="image/png", filename=filename, headers=_LOGO_CACHE_HEADERS)


@guard.post("/api/network-logo/regenerate", AuthLevel.ADMIN)
async def api_network_logo_regenerate(request: Request):
    """Drop a single network's cached logo and re-resolve it from TMDB."""
    data = await authz.json_body(request)
    name = (data.get("name") or "").strip()
    tmdb = data.get("tmdb")
    if not name:
        return JSONResponse({"ok": False, "error": "Missing network name"}, status_code=400)
    logos.delete(name)
    path = await logos.ensure_logo(load_settings(), name, tmdb)
    return JSONResponse({"ok": True, "network": name, "generated": bool(path and path.exists())})
