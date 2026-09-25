"""The calendar: the month picker, the shell, one day's cards, and the small
JSON endpoints the page drives itself from.

The shell (`calendar_page`) is assembled out of five separable answers rather
than one long context build, because they are asked at different scopes and one
of them is asked twice. `assemble_month` produces the month's cards and every
number stated ABOUT the month; `_count_visible` tells a day that has not been
fetched yet how much space to hold for the cards that are coming;
`_view_preferences` and `_day_chips` are two different views of the same
assembly for two different consumers — the template and the jump-to strip.

Everything deciding WHAT a viewer may see is read from their session, never from
the query string: the per-user filters and the not-watching marks are theirs, so
no request can ask for somebody else's view or for an unfiltered one.
"""
from __future__ import annotations

# The STDLIB calendar, not the package around this module. Absolute imports mean
# the bare name resolves to the stdlib either way — this package is app.calendar,
# not calendar — but standing in app/calendar/ it reads as though it might not.
import asyncio
import calendar as _calendar
import dataclasses
import logging
import re
import secrets
from collections import Counter
from datetime import date, datetime
from urllib.parse import quote
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from markupsafe import Markup

from . import (cache as calendar_cache, enrich as calendar_enrich,
               entries as calendar_entries, filter as calendar_filter,
               resolve as calendar_resolve, search as calendar_search,
               share_links, state as calendar_state, vocab)
from .. import (auth, authz, cache, chrome, clock, db, details_panel,
                route_params)
from ..auth import AuthLevel
from ..config import load_settings
from ..endpoints import DEFAULT_ENDPOINT, ENDPOINTS, endpoint_choices, get_endpoint
from ..integrations import routes as integrations_routes
from ..media import logos
from ..perftrace import span
from ..providers import detail_source
from ..providers.base import Media, Source, SourceUnavailable, parse_item_key
from ..providers.trakt import TraktError
from ..sources import prefs as source_prefs
from ..timezones import build_options as build_timezone_options
from ..templating import details_query_names, templates

logger = logging.getLogger(__name__)

router = APIRouter()
guard = authz.Guard(router)

# How many of a month's day blocks the calendar page renders inline, AT MOST. The
# rest are fetched as they scroll into view, so a busy month's first response is a
# few dozen cards instead of a thousand — the document the browser has to parse
# before it can show anything is what made the page slow, not the cards further
# down it that nobody has scrolled to yet.
INITIAL_DAY_BLOCKS = 5
# AND THE REAL BOUND, BECAUSE A DAY IS NOT A UNIT OF WORK. Five days is a few
# dozen cards on Series Premieres and 464 on All Episodes, where a calendar full
# of daily serials puts ninety titles on a single Tuesday — measured at 717ms of
# template rendering for one month, which is pure Python and therefore holds the
# event loop for its whole duration (a worker thread does not help: the GIL means
# the render still runs on the one core the loop needs). Every day block and every
# season line that same page then asks for queues behind it.
#
# So the budget is CARDS, which is what the paragraph above was always really
# about, and the day count is left as a ceiling for the calendars where cards are
# scarce. THE FIRST DAY IS ALWAYS DRAWN however big it is: a page that opened on
# nothing but placeholders would have nothing to show while the first fetch ran.
INITIAL_DAY_CARDS = 120

# WHAT THE PAGE SAYS WHEN NOBODY CAN SUPPLY A CALENDAR, and it deliberately
# names no service. The gate in front of it (`calendar_source_configured` ->
# providers.for_calendar_sources) asks the registry whether ANY source can be
# read, and each source answers for its own transport: one wants the instance's
# client id, another is a public feed that wants nothing. A sentence naming Trakt
# and its Access Token was wrong twice over on an instance reading months from
# somewhere else — it named a service that was not the reason, and a credential
# the calendar never sends. Reaching this state now means every source is either
# missing its own credential or switched off, and both are settings.
NOT_CONFIGURED = (
    "No calendar source is available right now. Open ⚙️ Settings to add a "
    "service's credentials, or to switch a source back on."
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
    return clock.first_zone(user.timezone, settings.timezone)


def _count_visible(grouped: list[dict], not_watching: set[str]) -> None:
    """Tell each day how many of its cards this viewer would see with not-watching
    titles hidden.

    IT IS THE ONE PRESENTATION FACT A DAY THAT HAS NOT BEEN FETCHED YET CANNOT
    WORK OUT. A day block that HAS its cards is asked about them by the
    stylesheet — how many columns it needs, and whether hide mode empties it
    entirely are both `:has()` and `:nth-child(k of …)` over the cards themselves,
    so they stay right through a mark made without a reload. A PLACEHOLDER has no
    cards to be asked about and still has to reserve the right width and height,
    or the scrollbar and the jump-to strip land somewhere other than where the day
    really is. So the shell, which assembled the whole month anyway, writes the
    number onto the placeholder and the stylesheet does the rest of the arithmetic
    from it (_day_skeleton.html, style.css's `--shown`).

    The column CAPS are not here and never come back: they are a fact about how
    each card style looks, they live beside the styles in style.css, and they used
    to be spelled a third time in calendar/layout.js."""
    for group in grouped:
        group["visible"] = sum(1 for item in group["items"]
                               if item.mark_key not in not_watching)


def _filters_active(prefs: dict) -> bool:
    """Whether anything is set, PAUSED OR NOT.

    Deliberately blind to the pause, because the toolbar button has three states
    and this is the one that separates the outer two from the middle. "Nothing
    set" and "set but switched off" must not look alike: the first is a calendar
    that shows everything because nobody narrowed it, the second is a calendar
    that shows everything because somebody chose to see it that way, and reading
    the second as the first is how a viewer forgets their filters exist. The
    template asks this AND `filters_paused` and draws one of three looks.
    """
    return vocab.any_set(prefs, (*vocab.TV_FIELDS, *vocab.MOVIE_FIELDS))


def _filters_summary(prefs: dict) -> str:
    """"genre, country" — which dimensions are narrowing this calendar, for the
    header button's tooltip. Names the dimensions rather than the values, which
    can run to dozens of networks and would not fit. Show and movie
    certifications share one "certification" label — the endpoint decides which
    of the two specs is actually in play, but the tooltip is naming a dimension,
    not a value, so it does not need to distinguish them."""
    named = [
        label for label, value in (
            # ONE LABEL FOR BOTH MEDIA'S COPY OF A DIMENSION, the same way the
            # two certification specs already shared one: a tooltip naming
            # dimensions has nothing to say about which tab an answer was typed
            # on, and "genre, genre" would be the alternative.
            ("genre", prefs["tv_genres"] or prefs["movie_genres"]),
            ("country", prefs["tv_countries"] or prefs["movie_countries"]),
            ("certification", prefs["show_certifications"] or prefs["movie_certifications"]),
            ("network", prefs["network_filter"]),
            # One label for the two release specs, the same way the two
            # certification specs share one: the tooltip names a DIMENSION, and
            # "where and how a film is released" is one dimension asked in two
            # halves.
            ("film release", prefs["movie_release_countries"] or prefs["movie_release_types"]),
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


async def _viewer_source_selection(request: Request, user, media=None) -> source_prefs.SourcePrefs:
    """This viewer's saved source preferences for `media`, with `calendar_source`
    swapped for a `?source=` override when the query names one of the app's own
    selections.

    `media` NARROWS FIRST AND THE OVERRIDE STILL WINS, which is the order the two
    want: the medium picks which of the account's two saved answers is in play,
    and `?source=` is a transient "show me this one instead" that was never about
    either. Passing None means the caller is not asking about one calendar — the
    search does that, because it spans both media and narrows per endpoint
    itself.

    THE OVERRIDE IS NEVER PERSISTED. It exists so a coverage-gap message (see
    _coverage_gap below) can offer "show Trakt instead" as a link for THIS
    view only, without silently rewriting the account's saved preference — the
    same reason a share link's own view parameters never write back to the
    owner's row. An unrecognized value is ignored rather than refused, since
    this is a read of the calendar and not a form that owes the visitor an
    error for a stray query string.
    """
    saved = await source_prefs.load(user.user_id)
    if media is not None:
        saved = saved.for_media(media)
    requested = request.query_params.get("source")
    if requested and source_prefs.is_selection(requested):
        return dataclasses.replace(saved, calendar_source=requested)
    return saved


def _answering_services_for_media(media, settings) -> list[tuple[str, str]]:
    """(name, label) for every service that could fill ANY calendar of `media`.

    THE FILTERS PANEL ASKS PER MEDIUM WHERE THE TOOLBAR ASKS PER CALENDAR, and
    the union is what makes the two agree. A service that publishes All Episodes
    but not Season Finales still belongs on the TV tab: the answer stored there
    governs four endpoints, and a service missing from the tab could never be
    switched on for the one it does answer. The per-endpoint question is still
    asked where it is genuinely per-endpoint -- `_answering_services` and the
    toolbar control it feeds.

    The fewer-than-two rule comes back with it, unchanged and for the same
    reason: one service answering a whole medium is not a choice.
    """
    seen: dict[str, str] = {}
    for endpoint in ENDPOINTS.values():
        if endpoint.media is not media:
            continue
        for name, label in calendar_resolve.answering_services(endpoint, settings):
            seen.setdefault(name, label)
    return list(seen.items()) if len(seen) > 1 else []


def _source_toggles(media, settings, saved: source_prefs.SourcePrefs,
                    showing: set[str] | None = None) -> list[dict]:
    """One tab's service switches, each independently on or off.

    INDEPENDENT BOXES RATHER THAN A "BOTH / TRAKT / SIMKL" PICKER, and the
    difference only shows itself later: a picker enumerates the services that
    exist today, so registering a third makes it a menu somebody has to be
    re-offered, while a box per service simply grows one more. It is also what
    keeps `auto` expressible -- every box ticked is "whatever there is, now and
    later" rather than a named set of the two that happened to exist.

    `showing` IS WHAT THE PANEL IS CURRENTLY HOLDING, when the panel is being
    drawn again mid-edit. It is read as a plain set rather than through
    SourcePrefs because one state the panel can be in cannot be stored at all:
    every box unticked. Only the SAVE refuses that, so a redraw has to be able to
    show it -- otherwise pressing Clear would silently tick a service back on.
    """
    for_media = saved.for_media(media)
    # THE FIELD NAME COMES WITH THE SWITCH, spelled by app/calendar/vocab.py like
    # every other name this panel posts under, so the template never composes one.
    return [{"source": name, "label": label,
             "on": (name in showing if showing is not None
                    else for_media.admits_calendar(name)),
             "name": vocab.source_field_name(media, name)}
            for name, label in _answering_services_for_media(media, settings)]


def _filter_panel(prefs: dict, saved: source_prefs.SourcePrefs, settings, endpoint,
                  showing_sources: dict | None = None) -> dict:
    """Everything the filters panel draws, resolved server-side.

    THE CHIPS ARE MARKUP THE SERVER BUILDS, not a vocabulary string a script
    inflates after the fact. Every chip is a real named input the browser holds
    the state of, so the panel needs no hydration request when it opens, no
    client copy of the vocabulary, and no knowledge anywhere in JavaScript of
    what a filter spec looks like -- app/calendar/vocab.py's docstring is where
    that argument is made in full.

    THE TAB THAT OPENS IS THE MEDIUM IN FRONT OF THE VIEWER, which is a Jinja
    conditional on this value rather than a boot script, because it is decided
    once when the panel is drawn and never changes while it is open.

    `prefs` IS NOT ALWAYS THE STORED ROW. Adding a typed chip and pressing Clear
    both draw the panel again from what it is CURRENTLY holding, which is the
    stored row with the unsaved edits laid over it (`_prefs_showing`) -- so there
    is one renderer for a panel rather than one for its first draw and another
    for every redraw.

    WHAT IS NOT HERE IS THE PROSE. Labels, hints and the per-field notes are the
    template's, so a wording change is a template change; this answers only what
    the server knows -- which chips exist, how each is currently set, how many
    answers a tab is carrying, and which services could fill it.
    """
    return {
        "media": str(endpoint.media),
        "paused": bool(prefs["filters_paused"]),
        "chips": {field: vocab.chips_for(field, prefs.get(field))
                  for field in vocab.FIELDS},
        # "THIS DIMENSION WAS ON THE PANEL", one hidden field per dimension.
        # Without it a dimension that draws no chip -- networks, for an account
        # that has never named one -- would be missing from the payload, and the
        # save could not tell it from a dimension the panel never showed. See
        # app/calendar/vocab.py's `presence_name`.
        "presence": {field: vocab.presence_name(field) for field in vocab.FIELDS},
        # PER TAB, so the tab a viewer is NOT on can say what it is holding.
        # Counted in tokens rather than dimensions: "3" means three things are
        # being filtered, where a dimension count would say "1" for somebody who
        # had excluded nine genres.
        "counts": {
            "show": vocab.count_set(prefs, vocab.TV_FIELDS),
            "movie": vocab.count_set(prefs, vocab.MOVIE_FIELDS),
        },
        "sources": {
            "show": _source_toggles(Media.SHOW, settings, saved,
                                    (showing_sources or {}).get(Media.SHOW)),
            "movie": _source_toggles(Media.MOVIE, settings, saved,
                                     (showing_sources or {}).get(Media.MOVIE)),
        },
    }


def _source_choices(endpoint, requested, settings) -> list[dict]:
    """The toolbar's source control: what it offers, which option is on, and
    whether it is drawn at all. An empty list means the toolbar draws nothing.

    A VIEW CONTROL AND NOT A PREFERENCE, which is the whole of why it is built
    here out of the query string rather than out of the stored row. `?source=`
    has always been a transient override (see `_viewer_source_selection`); this
    is the way to reach it without typing one. Choosing something re-reads THIS
    page and writes nothing — the account's answer is stated once, in the 🎚️
    Filters panel beside the rest of its per-viewer narrowing, and a control on
    the toolbar that quietly rewrote it would change every other view the account
    has as a side effect of a look. The two are deliberately not merged: this one
    is "show me that for a moment", the panel is "this is what my calendar is".

    THE FIRST OPTION IS THE STORED ANSWER, unnamed, because it is whatever the
    account said and this control is not the place that says it. It is also the
    way back: without it, overriding once would leave no way to stop overriding
    short of editing the address.

    IT OFFERS EXACTLY THE SERVICES THAT COULD ACTUALLY FILL THIS CALENDAR, and
    which those are is `_answering_services`' question rather than this
    function's — including the rule that fewer than two of them is no control at
    all.

    THE COST OF NOT DRAWING IT, taken deliberately: a `?source=` in the address
    on a single-service calendar has no control to clear it with. That override
    only arrives by following a link, switching calendars drops it (the picker
    beside this one carries no source), and the alternative was to keep drawing
    a control whose whole purpose on that page would be to undo something the
    reader did not do.
    """
    chosen = requested if requested and source_prefs.is_selection(requested) else ""
    services = calendar_resolve.answering_services(endpoint, settings)
    if not services:
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

    """
    from .. import providers  # deferred: see the DECLARED_EDGES note for CALENDAR -> SOURCES

    named_sources = source_prefs.named_sources(prefs.calendar_source)
    if named_sources is None or len(named_sources) != 1:
        return None, None
    admitted = providers.calendar_sources(prefs=prefs, settings=settings)
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
    # This viewer's marks as MARK KEYS, expanded once by the assembler.
    not_watching_keys: set[str] = dataclasses.field(default_factory=set)
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
    # How many titles this viewer's release filter removed from this month. A
    # release-country rule removes films rather than moving them — a film out
    # only in Brazil does not match a US filter at all — so it can empty a month
    # outright, and an empty month with nothing on the page to explain it reads
    # as a broken calendar rather than as a filter doing its job.
    filtered: int = 0


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
                **vocab.active_specs(prefs, endpoint.media, honour_pause=True),
                not_watching_ids=not_watching,
                prefs=source_selection,
            )
            sp.set(items=meta["total"])
        assembly.total = meta["total"]
        assembly.watching = meta["watching"]
        assembly.not_watching_count = meta["not_watching"]
        # THE EXPANDED MARKS, as mark keys — see cache.assemble_range, which is
        # the one place a stored mark's spelling is translated. Everything the
        # shell hands the template asks the plain membership question against
        # this, so the page and the counts above cannot disagree about which
        # cards are marked.
        assembly.not_watching_keys = meta["not_watching_keys"]
        # A window Trakt couldn't supply is skipped rather than failing the
        # whole month; flag it so the page can say the month is incomplete
        # instead of silently showing a short one.
        assembly.partial = meta["partial"]
        assembly.unenriched = meta["unenriched"]
        assembly.filtered = meta["filtered"]
        assembly.show_counts = Counter(
            item.mark_key for group in assembly.grouped for item in group["items"])
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
        # THE THIRD STATE. "Nothing set" and "set but switched off" both draw a
        # calendar with everything on it, and if they looked alike a viewer would
        # forget their filters existed — so the button asks this as well as
        # `filters_active` and draws one of three looks. It used to come from the
        # filters panel's own context, which the page no longer carries: the
        # panel is a fragment now and the toolbar has to know this without it.
        "filters_paused": bool(prefs["filters_paused"]),
    }


def _day_chip(day: date, items: list, not_watching: set[str],
              hide_not_watching: bool, anchor=None) -> dict:
    """One day's chip for the jump-to strip.

    `count` is what the day holds; `shown` is what THIS viewer will see of it, and
    a day showing nothing has no section to scroll to, so its chip renders inert.
    With hide-not-watching on, a day whose every item is marked renders nothing at
    all, so its chip must not offer to scroll somewhere blank.

    THE CARD A SEARCH JUMP NAMED (`anchor`) IS SHOWN WHATEVER ITS MARK, because
    the stylesheet exempts it from hiding so the jump has somewhere to land — and
    its day with it. Only the page's own draw knows the anchor; a chip redrawn by
    a later mark or toggle is asked without one, and by then the reader has moved
    on from the jump.

    ONE RULE, TWO DRAWERS. The shell draws every chip from it, and a day that
    arrives late draws its own chip again from it (_day_fragment.html), having read
    the marks after the shell did — which is how a mark saved since reaches the
    chip. calendar/layout.js keeps chips in step with toggles made without a
    reload.
    """
    count = len(items)
    shown = (sum(1 for item in items if item.mark_key not in not_watching or item is anchor)
             if hide_not_watching else count)
    return {"day": day.day, "date": day.isoformat(), "count": count, "shown": shown}


def _day_chips(assembly: MonthAssembly, year: int, month: int, days: int,
               not_watching: set[str], hide_not_watching: bool,
               anchor=None) -> list[dict]:
    """One chip per day of the month for the jump-to strip — see `_day_chip`."""
    items_on = {group["date"]: group["items"] for group in assembly.grouped}
    return [_day_chip(on, items_on.get(on.isoformat(), []), not_watching,
                      hide_not_watching, anchor)
            for on in (date(year, month, day) for day in range(1, days + 1))]


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


def _calendar_query(request: Request) -> str:
    """The calendar's own query string, ready to append to another address.

    THREE PLACES CARRY IT AND THEY ARE ONE FACT: the toolbar button that asks for
    the filters panel, the panel's save, and the Add-all control beside them. The
    save ANSWERS WITH THE MONTH REDRAWN, so without it a filter saved while
    looking at October came back holding whichever month today falls in — the
    route would have had nothing to read the month off but the clock.
    """
    return f"?{request.url.query}" if request.url.query else ""


def _jump_anchor(grouped: list[dict], highlight: str, day: str) -> tuple[int, object] | None:
    """(index of its day, the card) a jump is aimed at, or None when the month
    draws no card for `highlight`.

    ONE CARD, AND THE ONE THE ROW NAMED. `highlight` is a TITLE's identity, and a
    title airs on as many days of a month as it airs on — so the search row sends
    its own DAY beside it, both references the server drew itself, and that
    day's airing is the anchor. Without the day every row for a weekly show was
    one destination: four August results all landed on the 6th.
    A LINK THAT NAMES NO DAY, or a day the month no longer draws the title on (an
    old bookmark, a jump whose fill landed elsewhere), falls back to the FIRST
    airing, which is where every jump used to land.
    The card itself is returned rather than a position, because the template asks
    whether the card it is drawing IS this one (`sameas`) — the only test that
    can tell two airings of one show on one day apart without the route and the
    template agreeing on a second way to name them.
    """
    if not highlight:
        return None
    first = None
    for index, group in enumerate(grouped):
        found = next((item for item in group["items"] if item.mark_key == highlight), None)
        if found is None:
            continue
        if group["date"] == day:
            return index, found
        first = first or (index, found)
    return first


async def _month_context(request: Request, user, settings, prefs: dict) -> dict:
    """Everything the calendar BODY is drawn from: the month's cards, every number
    stated about them, and the marks and buttons each card carries.

    ONE ASSEMBLY, TWO DRAWERS. The page renders it inside the shell, and a filters
    save renders it again on its own and swaps it in — so a calendar a save
    answers with cannot come from a different reading of the month than the page
    would have shown. That is also what lets the filters panel stop reloading:
    what a filter changes IS this region, plus the two marks the toolbar carries,
    and nothing else on the page.
    """
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
    source_selection = await _viewer_source_selection(request, user, endpoint.media)

    # This viewer's marks, read ONCE and handed to the assembly so the cards come
    # out of the template already carrying the class. The client used to add it
    # after the page had painted, which is what made hidden items visibly pop out.
    not_watching = await calendar_state.not_watching_ids(user.user_id)
    month_view = await assemble_month(
        user, settings, prefs, endpoint, tz, year, month, not_watching,
        source_selection)
    view = _view_preferences(prefs, settings)

    # Measured at 4.7ms against 31,358 stored airings, which is why it rides the
    # page render rather than a poll: a number a reader can refresh to is worth
    # more than one that ticks, and it costs the page less than asking for it
    # would.
    backlog_titles, backlog_seasons = await calendar_enrich.backlog()

    _count_visible(month_view.grouped, month_view.not_watching_keys)

    # Only the first few days go out with the shell; every day after them is a
    # placeholder that fetches its own cards when it is scrolled to. So first
    # paint costs a handful of cards instead of a month of them, and a day nobody
    # ever scrolls to is never assembled, rendered, or shipped at all.
    #
    # THE MONTH IS ONE ORDERED LIST AND EVERY DAY STAYS IN IT, which is the whole
    # reason this is a flag rather than two lists. <main> IS the month, in date
    # order, and a day lifted out of the sequence to be rendered early lands
    # between the 5th and the 6th however far through the month it belongs — which
    # is exactly what a jump into the 28th used to do.
    #
    # The first few are chosen by DAY BLOCK rather than by date, because a month
    # can open with a run of empty days and "the first five dates" would then ship
    # nothing.
    inline = set()
    budget = INITIAL_DAY_CARDS
    for index, group in enumerate(month_view.grouped[:INITIAL_DAY_BLOCKS]):
        # The first day goes in whatever it costs; every day after it has to fit
        # what is left, so one enormous Tuesday cannot drag four more in with it.
        if index and budget <= 0:
            break
        inline.add(index)
        budget -= len(group["items"])
    # THE DAY A JUMP IS AIMED AT SHIPS WITH THE SHELL TOO, wherever in the month it
    # falls. A search result links to one card, and a browser can only scroll to
    # an element that EXISTS when the page is parsed — a card still inside an
    # unfetched placeholder is not one. That is what made landing on a result
    # need script: something had to wait for the day to arrive, and then keep
    # correcting as the days above it grew. Shipping one extra day costs a
    # handful of cards and lets an anchor behave like an anchor.
    #
    # AND IT IS ONE CARD — see _jump_anchor for which one.
    anchor = None
    aimed = _jump_anchor(month_view.grouped, request.query_params.get("highlight") or "",
                        request.query_params.get("day") or "")
    if aimed is not None:
        index, anchor = aimed
        inline.add(index)
    # THE SHELL'S IS-NEW ANSWER IS FILED ONCE AND ITS PLACEHOLDERS CARRY THE
    # TOKEN. The diff above has already committed this load as the baseline, so a
    # day fetched afterwards could never work it out again; pointed at the answer,
    # the day route draws the marks itself. Written once per load rather than once
    # per day because it IS one answer about the month.
    new_token = await _remember_new_marks(
        user.user_id, endpoint.key, year, month, month_view.new_ids)
    for index, group in enumerate(month_view.grouped):
        group["inline"] = index in inline
        if not group["inline"]:
            group["url"] = _day_url(
                endpoint.key, date.fromisoformat(group["date"]),
                source=request.query_params.get("source"), new=new_token)

    logo_networks = await _logo_networks(
        settings, [item for group in month_view.grouped for item in group["items"]])
    return {
        "request": request,
        "settings": settings,
        "view": view,
        "endpoint": endpoint,
        "tz": tz,
        "year": year,
        "month": month,
        "calendar_query": _calendar_query(request),
        # WHICH CARD A JUMP IS AIMED AT, so that card can carry the anchor rather
        # than a script hunting for it after the fact. The template tests identity
        # against it rather than comparing keys over again — see above.
        "anchor": anchor,
        # EVERY DAY OF THE MONTH THAT HOLDS ANYTHING, IN ORDER, each saying
        # whether it is drawn with its cards or as a placeholder that will fetch
        # them (`inline`). One list rather than two, because <main> is the month
        # and the two are interleaved the moment a jump pulls a later day forward.
        "days": month_view.grouped,
        "total": month_view.total,
        # The stats tiles, the is-new marks, the "since last run" line and the
        # history log are all computed above and rendered with the page, so they
        # are right at first paint and stay right when only part of a month is on
        # screen. The card partial reads these two sets by membership.
        # THE EXPANDED SET, so the template's membership tests agree with the
        # counts beside them — see cache.assemble_range.
        "not_watching": month_view.not_watching_keys,
        # WHETHER THIS PAGE MAY OFFER TO FILTER FROM A CARD'S OWN BADGES. True
        # wherever a signed-in viewer has filters of their own to add to, and
        # False on the public share page, which renders the same card for
        # somebody who has none — a chip that looks pressable and does nothing is
        # worse than one that never offered.
        "can_filter": True,
        # WHAT EACH BADGE IS ALREADY SET TO, parsed once for the whole render
        # rather than per card. Drawn from storage so a reload and the answer to
        # a press agree — the badge used to be drawn blank whatever was stored,
        # so it claimed a filter until the next load and then stopped claiming it
        # while the filter went on applying.
        "badge_modes": vocab.BadgeModes(prefs),
        "new_ids": month_view.new_ids,
        "stats": {"total": month_view.total, "watching": month_view.watching,
                  "not_watching": month_view.not_watching_count},
        "delta": month_view.delta,
        "history": month_view.history,
        # EVERY CONSUMER GETS THE EXPANDED SET, not the raw stored one. Mixing
        # the two is how a mark hides a card but not its day's chip: the card
        # asks about `mark_key` and the chip would be counting raw ids.
        "day_chips": _day_chips(month_view, year, month, days,
                                month_view.not_watching_keys,
                                view["hide_not_watching"], anchor),
        "error": month_view.error,
        "partial": month_view.partial,
        "unenriched": month_view.unenriched,
        # THE TWO DRAIN BACKLOGS, INSTANCE-WIDE, and a different fact from
        # `unenriched` above: that one counts titles ON THIS MONTH whose filters
        # have not been applied yet, which is an apology for what this page is
        # showing. These count what the background drains still owe everywhere,
        # which is the only way to tell a drain working through a queue from one
        # looping on work it has already done. Rendered only while non-zero, so
        # the lines disappearing IS the signal that the drains have settled.
        "backlog_titles": backlog_titles,
        "backlog_seasons": backlog_seasons,
        # How many films this viewer's own release filter took off this month,
        # so a page it emptied can name the filter that emptied it instead of
        # showing a month that looks like it has nothing in it.
        "filtered": month_view.filtered,
        # A Simkl-only month outside its declared coverage window renders
        # this explicit state rather than a blank calendar. `switch_url` is
        # the query string to append to THIS page's own URL to preview the
        # other source without saving anything.
        "coverage_gap": month_view.coverage_gap,
        "switch_url": month_view.switch_url,
        "generated": datetime.now().strftime("%H:%M"),
        # THE ADD-TO-LIBRARY BUTTONS' STATE, drawn onto every card — see
        # `_library`.
        "library": _library(user, settings),
        # WHICH NETWORKS HAVE A LOGO, asked once here rather than once per
        # card by the browser — see `_logo_networks`.
        "logo_networks": logo_networks,
        # How many cards the shell is actually turning into HTML, for the render
        # span the caller opens around it.
        "inline_cards": sum(len(group["items"]) for group in month_view.grouped
                            if group["inline"]),
    }


@guard.get("/calendar", AuthLevel.CALENDAR_APPROVED)
async def calendar_page(request: Request):
    """The calendar SHELL: the header, the stats bar, the jump-to strip, the
    modals — and the first few days of cards inline. Every day after those is
    announced as a placeholder that fetches itself from /calendar/day when the
    viewer reaches it, so the browser has a usable page long before a whole month
    of cards exists, and a day nobody scrolls to is never built at all."""
    settings = load_settings()
    user = await auth.current_user(request)
    prefs = await auth.get_user_prefs(user.user_id)
    month = await _month_context(request, user, settings, prefs)
    endpoint, tz = month["endpoint"], month["tz"]
    context = {
        **month,
        "endpoints": endpoint_choices(),
        # The toolbar's source control. Built from the query string, applying to
        # this view alone, and storing nothing — see _source_choices. Empty when
        # fewer than two services could fill this calendar, and the template
        # draws nothing at all rather than an inert control.
        "source_choices": _source_choices(
            endpoint, request.query_params.get("source"), settings),
        "viewer_timezone_groups": build_timezone_options(tz.key),
        "month_label": _calendar.month_name[month["month"]],
        "nav": route_params.adjacent_months(month["year"], month["month"]),
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
    }
    # Jinja renders eagerly when the response is built, so this span is the cost of
    # turning the shell's cards into HTML — the other half of the server's blocking
    # time before the browser gets anything. It is now bounded by the inline day
    # count rather than growing with the whole month.
    with span("calendar.render", cards=month["inline_cards"]):
        return templates.TemplateResponse(request, "index.html", context)


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


# AT MOST ONE LOGO WARM-UP IN FLIGHT FOR THE WHOLE PROCESS. Every calendar
# render can find missing tiles, and a busy instance would otherwise start a task
# per request, all resolving the same networks against the same rate-limited API.
# One at a time is enough: the work is idempotent and whatever this pass misses
# the next render asks for again.
_logo_warm_running = False


def _warm_logos_later(settings, roster) -> None:
    """Build the missing network tiles off the back of this request."""
    global _logo_warm_running
    if _logo_warm_running:
        return
    _logo_warm_running = True

    async def _run():
        global _logo_warm_running
        try:
            await logos.ensure_logos(settings, roster)
        except Exception:
            # Best-effort by design: a network whose tile cannot be built shows
            # the emoji, which is exactly what it showed before. Never let it
            # reach the request that scheduled it — that request has long since
            # been answered.
            logger.exception("network logo warm-up failed")
        finally:
            _logo_warm_running = False

    asyncio.create_task(_run())


async def _logo_networks(settings, items) -> set[str]:
    """Which of these cards' networks this instance actually holds a tile for.

    ONE QUESTION ANSWERED PER RESPONSE INSTEAD OF ONE REQUEST PER CARD. A network
    badge is its logo where there is one and the viewer's emoji where there is
    not, and the calendar used to settle that by pointing every badge at
    /api/network-logo and handling the 404 in an `onerror`. Measured on a real
    All Episodes September: 99 requests per load, every one of them a 404, all
    competing with the day blocks and season lines for the browser's six
    connections — to learn something the filesystem answers here for nothing.

    THE TRACKER ALREADY DID IT THIS WAY and `logos.tiled` was written for it; the
    calendar simply never adopted it. This is that function, with the warm-up the
    same page needs in front of it.

    WARMED FIRST, BECAUSE OTHERWISE NOTHING WOULD EVER GENERATE ONE. The 404 was
    not only a fallback, it was also the trigger: /api/network-logo builds a
    missing tile on demand. Asking `tiled` without warming would mean a network
    the calendar alone shows never getting a logo at all. `ensure_logos` is a
    path check per already-known network and does real work only for the
    genuinely missing, which is what makes it safe on every load.
    """
    roster = {(item.network, item.ids.get("tmdb")) for item in items if item.network}
    known = logos.tiled(network for network, _tmdb in roster)
    # GENERATING THE MISSING ONES DOES NOT HAPPEN ON THIS REQUEST. Warming inline
    # was the first shape and it cost the render 190ms warm and seven seconds
    # cold, because a network nobody has resolved yet means a TMDB lookup and
    # there can be a hundred of them on one month. The page draws the tiles that
    # exist and asks for the rest to be built behind it, so a new network's logo
    # appears on the next load rather than delaying this one — the same bargain
    # the enrichment drain makes, and for the same reason.
    missing = [(network, tmdb) for network, tmdb in roster
               if network not in known and not logos.is_negative(network)]
    if missing:
        _warm_logos_later(settings, missing)
    return known


def _library(user, settings) -> integrations_routes.LibraryView | None:
    """The add-to-library buttons' state for this viewer, or None, and None means
    no buttons at all rather than buttons that would be refused.

    ADMIN ONLY because Sonarr/Radarr/Seerr writes land in the operator's own shared
    libraries and Seerr's requests all carry one app-wide API key. ONE FUNCTION for
    that rule, because the shell, a day that arrives late and the details panel
    all draw these buttons, and three copies of "who gets them" would be three
    places for somebody else to start seeing them."""
    return integrations_routes.LibraryView(settings) if user and user.is_admin else None


# HOW LONG THE SHELL'S IS-NEW ANSWER OUTLIVES THE LOAD THAT MADE IT. It is read
# by the days of ONE page as the reader scrolls through them, so its life is a
# page's life and not a session's: half an hour is far past the longest anybody
# spends scrolling a month, and short enough that an abandoned tab's answer is
# gone rather than sitting in the store indefinitely.
_NEW_MARKS_TTL_SECONDS = 30 * 60


def _new_marks_key(user_id: int, endpoint_key: str, year: int, month: int,
                   token: str) -> str:
    """Where one page load's is-new answer is filed in the shared cache.

    THE USER IS IN THE KEY, not only the token, so a token guessed or copied from
    somebody else's page reads nothing — this names which titles were new to ONE
    account, and the day route looks it up as the account making the request. The
    month and calendar are in it for the ordinary reason: one reader can have two
    tabs open on two months.
    """
    return f"calendar:new:{user_id}:{endpoint_key}:{year}-{month:02d}:{token}"


async def _remember_new_marks(user_id: int, endpoint_key: str, year: int,
                              month: int, new_ids: set[str]) -> str:
    """File this load's is-new answer and return the token naming it.

    THE ANSWER IS THE SERVER'S, SO THE SERVER KEEPS IT. A day fetched after the
    page has painted cannot work out what was new: the shell's diff has already
    committed this load as the next baseline, so re-reading it would see its own
    commit and conclude nothing is new. The answer used to ride on each
    placeholder's own request, one `new=` parameter per key — a payload in a GET,
    bounded and shape-checked because it was written back into the page by a retry
    button. A token is a REFERENCE: the day route resolves it against this
    account's own cache entry, and the only thing an edited address can do with it
    is name an entry that is not there.

    NO ANSWER IS NOT WORTH A KEY. A month with nothing new on it — much the
    commonest case, since every load commits the baseline the next one is diffed
    against — writes nothing and hands back no token.
    """
    if not new_ids:
        return ""
    token = secrets.token_urlsafe(9)
    await cache.set(_new_marks_key(user_id, endpoint_key, year, month, token),
                    sorted(new_ids), retain_seconds=_NEW_MARKS_TTL_SECONDS)
    return token


async def _new_marks(user_id: int, endpoint_key: str, year: int, month: int,
                     token: str) -> set[str]:
    """The is-new keys a day may mark, read back from what the shell filed.

    A TOKEN THAT NAMES NOTHING MEANS NO MARKS, and that is honest rather than
    wrong: it is what a late day did before the answer was carried at all. The
    entry expires with the page it belongs to, and a reader who leaves a month
    open for an afternoon gets a day with no ✨ on it rather than a day claiming
    something is new that stopped being new hours ago.
    """
    if not token:
        return set()
    stored = await cache.get(
        _new_marks_key(user_id, endpoint_key, year, month, token),
        _NEW_MARKS_TTL_SECONDS)
    return {str(key) for key in stored} if isinstance(stored, list) else set()


def _day_url(endpoint_key: str, day: date, *, source: str | None = None,
             new: str = "") -> str:
    """The content request for one day. Built in one place because the shell's
    placeholder and the retry button on a day that failed must ask for exactly the
    same thing.

    `source` carries the shell's own `?source=` override forward, when it has
    one, so a day fetched after the fact reads from the same source selection
    the shell resolved the month with — the shell never puts one in a
    placeholder's URL for a viewer who never overrode anything, so the common
    case is unchanged.

    `new` is the token naming where this load's is-new answer is filed — see
    `_remember_new_marks`. One short opaque word, the same one on every
    placeholder in the page, and absent for a month with nothing new on it."""
    url = (f"/calendar/day?endpoint={quote(endpoint_key)}"
          f"&year={day.year}&month={day.month}&date={day.isoformat()}")
    if source:
        url += f"&source={quote(source)}"
    if new:
        url += f"&new={quote(new, safe='')}"
    return url


@guard.get("/calendar/search", AuthLevel.CALENDAR_APPROVED)
async def search_the_calendar(request: Request):
    """Where a title is on this viewer's calendar, and where it should be if it
    is not on it yet.

    TWO ANSWERS, TWO PROMISES, AND THE ROUTE KEEPS THEM APART — see
    app/calendar/search.py, which is where the whole argument lives. The stored
    half is confirmed by the real read path so a jump lands on a card that is
    actually there; the catalogue half links to a MONTH, which fills on arrival.

    `live=1` IS THE ONLY THING THAT SPENDS A REQUEST, and the client sends it on
    Enter or the button rather than on every keystroke. Typing therefore costs an
    index read and nothing else, which is what makes it safe to run as somebody
    types.

    A FRAGMENT, NOT A PAGE. It renders the same results partial the shell embeds,
    so the typing path and a full submit produce identical markup.
    """
    user = await auth.current_user(request)
    settings = load_settings()
    query = (request.query_params.get("q") or "").strip()
    live = request.query_params.get("live") in ("1", "true", "yes")
    everywhere = request.query_params.get("all") in ("1", "true", "yes")
    endpoint = get_endpoint(request.query_params.get("endpoint"))
    prefs = await auth.get_user_prefs(user.user_id)
    tz = _resolve_viewer_tz(user, settings)
    source_selection = await _viewer_source_selection(request, user)
    marks = await calendar_state.not_watching_ids(user.user_id)

    # THE CALENDAR THE VIEWER IS STANDING ON, unless they ask for all of them.
    # A search is nearly always about the page in front of somebody — "when is
    # this on" asked of the premieres calendar means the premieres calendar —
    # and the five endpoints are five different questions, so answering all of
    # them by default buries the one that was asked. It is also five times the
    # work: `shows` alone held 12,880 airings in one real month.
    scope = list(ENDPOINTS.values()) if everywhere else [endpoint]

    found = await calendar_search.stored(
        query, settings=settings, prefs=prefs, tz=tz,
        source_selection=source_selection, marks=marks, endpoints=scope)
    elsewhere = calendar_search.Results()
    if live and query:
        # THE STORED ANSWERS ARE HANDED OVER so the catalogue half does not offer
        # somewhere the calendar already draws: naming the day beats naming the
        # month, and offering both would be two rows for one answer.
        #
        # KEYED ON THE SEASON AS WELL AS THE TITLE, because a catalogue row is a
        # season premiere rather than a show. `mark_key` is deliberately the
        # TITLE's identity and carries no season (see Item.mark_key), so keying
        # the skip on it alone would drop every season of a title the calendar
        # happens to hold one airing of — hiding exactly the season somebody is
        # searching for because a different one is on screen.
        known = frozenset((a.item.mark_key, a.item.season) for a in found.airings)
        try:
            elsewhere = await calendar_search.catalogue(
                query, settings=settings, prefs=prefs, tz=tz, known=known)
        except SourceUnavailable as exc:
            # The stored half already has an answer worth drawing; a catalogue
            # that could not be reached is a missing addition to it, not a failed
            # search.
            logger.warning("Catalogue search failed: %s", exc)

    return templates.TemplateResponse(request, "_search_results.html", {
        "request": request, "query": query, "live": live,
        "airings": found.airings, "elsewhere": elsewhere.elsewhere,
        "truncated": found.truncated, "failed": sorted(str(s) for s in elsewhere.failed),
        "everywhere": everywhere, "endpoint": endpoint,
        "not_watching": set(), "new_ids": set(), "can_filter": False,
        "settings": settings, "is_admin": bool(user and user.is_admin),
    })


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

    is-new is NOT COMPUTED HERE, BUT IT IS DRAWN HERE. The diff and its baseline
    commit are a whole-month decision the shell already made and wrote; re-reading
    the baseline from a fragment would see the shell's own commit and conclude
    nothing is new. So the shell files its answer and puts the TOKEN naming it on
    the day's own request (`_day_url`'s `new`); this reads the answer back as the
    account making the request and marks the cards from it. A token naming nothing
    — expired, or invented — marks nothing, which is what a late day did before
    the answer was carried at all."""
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
    source_selection = await _viewer_source_selection(request, user, endpoint.media)

    tz = _resolve_viewer_tz(user, settings)
    not_watching = await calendar_state.not_watching_ids(user.user_id)
    new = await _new_marks(user.user_id, endpoint.key, year, month,
                           request.query_params.get("new") or "")
    context = {
        "request": request, "not_watching": not_watching,
        # WHICH MONTH THIS DAY IS ON, for the watch toggle on each of its cards:
        # a mark's answer is every card for that title on the month, so the
        # button has to say which month it is asking about (_card.html).
        "month": month, "year": year, "endpoint": endpoint,
        # Same as the shell's — a day that arrives late offers the same badges,
        # carrying the same stored answers (vocab.BadgeModes).
        "can_filter": True,
        "badge_modes": vocab.BadgeModes(prefs),
        # The shell's is-new answer, for the cards on this day — see the docstring.
        "new_ids": new,
        "settings": settings, "is_admin": bool(user and user.is_admin),
        # And the same add-to-library buttons, in the state they are in.
        "library": _library(user, settings),
        "date": day.isoformat(), "label": calendar_cache.day_label(day),
        # The retry asks for exactly what the placeholder did, token included —
        # which is the whole of what a retry now has to carry forward.
        "retry_url": _day_url(endpoint.key, day, source=request.query_params.get("source"),
                              new=request.query_params.get("new") or ""),
        "partial": False,
        # Replaced below once this day's items are known; the error path renders
        # no cards, so an empty set is the right answer for it.
        "logo_networks": set(),
    }
    try:
        with span("calendar.day", endpoint=endpoint.key, day=day.isoformat()) as sp:
            grouped, meta = await calendar_cache.assemble_range(
                endpoint, settings, tz=tz, start_date=day, end_date=day,
                **vocab.active_specs(prefs, endpoint.media, honour_pause=True),
                not_watching_ids=not_watching,
                prefs=source_selection,
            )
            sp.set(items=meta["total"])
        # The expanded marks for THIS day's items, from the same assembler the
        # shell used — a day fetched late must decide "is this marked" by exactly
        # the rule the shell's own blocks were drawn with.
        context["not_watching"] = meta["not_watching_keys"]
        context["logo_networks"] = await _logo_networks(
            settings, [item for group in grouped for item in group["items"]])
        # NOTHING LAYS THIS DAY OUT: it arrives with its cards, and how wide it is
        # and whether hide mode empties it are the stylesheet's questions about
        # those cards (style.css's `--cols`), asked the moment it lands and asked
        # again every time a mark changes the answer.
        # Its chip in the jump-to strip is still the server's, drawn by the rule
        # the shell's came from, so a day that arrives collapsed stops being a
        # destination there.
        if grouped:
            context["chip"] = _day_chip(day, grouped[0]["items"], meta["not_watching_keys"],
                                        prefs["hide_not_watching"])
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


@guard.get("/calendar/jump", AuthLevel.CALENDAR_APPROVED)
async def jump_to_a_searched_title(request: Request):
    """Follow a search result that this instance's calendar does not hold: write
    the one airing, then send the reader to the month it lands on.

    A REDIRECT AND NOT A PAGE, so the address bar ends up on the calendar itself.
    Back then returns to wherever the reader was rather than replaying a write,
    and a reload of the month is an ordinary month load.

    IT WRITES ONE THING BECAUSE ONE THING WAS ASKED FOR. The search offers
    everything that matched — sixteen rows for a franchise — and writing all of
    them would turn one act of curiosity into sixteen decisions about a calendar
    everybody on this instance shares. Following a result is the act that names
    exactly one.

    THE QUERY NAMES A SERVICE AND THAT SERVICE'S OWN ID, AND NOTHING ELSE IS
    BELIEVED. Title, date, network, poster and the shared ids all come back from
    the service, so this link cannot be hand-edited into a calendar row of
    somebody's choosing — see calendar/search.py's `fill_one_gap`.

    A WRITE THAT COULD NOT HAPPEN STILL REDIRECTS. The month is worth opening
    either way: it may hold the title from an ordinary fill, and if it does not
    the reader sees the month they asked for rather than an error about
    machinery.
    """
    settings = load_settings()
    user = await auth.current_user(request)
    today = clock.today()
    params = request.query_params
    year = route_params.valid_year(params.get("year"), today.year)
    month = route_params.valid_month(params.get("month"), today.month)
    endpoint = get_endpoint(params.get("endpoint"))
    destination = (f"/calendar?year={year}&month={month}"
                   f"&endpoint={quote(endpoint.key, safe='')}")

    # A NAME THIS APP DOES NOT KNOW IS NOT AN ERROR, it is a link from an older
    # version or a hand-edited one; the reader still gets the month.
    try:
        source = Source(str(params.get("source") or ""))
    except ValueError:
        source = None
    source_id = str(params.get("id") or "").strip()
    if source is None or not source_id:
        return RedirectResponse(destination, status_code=303)
    try:
        media = Media(params.get("media") or "show")
    except ValueError:
        media = Media.SHOW
    season = route_params.season(params.get("season"))

    mark = await calendar_search.fill_one_gap(
        settings, source=source, source_id=source_id, media=media,
        season=season, endpoint_key=endpoint.key,
        tz=_resolve_viewer_tz(user, settings), now=db.now())
    if mark:
        destination += f"&highlight={quote(mark, safe='')}#jump-target"
    # 303, so following this is a GET however the browser got here, and so a
    # reload of where it lands does not re-run the write.
    return RedirectResponse(destination, status_code=303)


def _library_answer(request: Request, *, marks=(), message: str = "",
                    ok: bool = True) -> Response:
    """What an add answers with: every button on the page for that title, redrawn,
    and a line saying what happened.

    ONE RESPONSE REACHES EVERY COPY OF A BUTTON, and that is what takes the whole
    of the page's add script away. A title can air on a dozen days of a month and
    its panel carries a button of its own, so an add has always had to mark
    buttons the response was not aimed at. htmx's out-of-band swap is addressed by
    SELECTOR rather than by id — `outerHTML:.arr-btn[data-library-id='555']` — so
    one redrawn button in the answer replaces every button on the page for that
    title in that library, wherever they were drawn and whenever they arrived.
    Measured on this app's own vendored htmx before any of this was written.

    THE SELECTOR MUST CONTAIN NO SPACE. htmx splits an out-of-band value at the
    first colon into a swap style and a selector, and only when the value has no
    space in it — one with a space is read as a swap style with modifiers
    instead, and the swap silently becomes an ordinary one. So the attribute
    selector is written tight.

    THE TOAST RIDES THE SAME ANSWER, appended to the shell's `#toastHost` the way
    the rankings board's own flash does, so nothing has to compose a sentence in
    the browser from a JSON refusal.
    """
    return templates.TemplateResponse(request, "_library_answer.html", {
        "request": request, "marks": marks, "message": message, "ok": ok})


def _marks_for(library, target: str, titles) -> list[dict]:
    """One redraw per title added, for `_library_answer` — the buttons for that
    service alone, since an add to Sonarr says nothing about Seerr."""
    out = []
    for key, media, ids in titles:
        buttons = [b for b in library.buttons(media, ids) if b.kind == target]
        if buttons:
            out.append({"key": key, "buttons": buttons})
    return out


@guard.post("/calendar/library/add", AuthLevel.ADMIN)
async def add_to_library(request: Request):
    """Add ONE title to Sonarr, Radarr or Seerr — what a card's own button posts.

    IT NAMES THE CARD AND NOT THE TITLE'S IDS. The body is `{key, target}`: the
    shared identity the card already carries and which service to add to. The ids
    are read back off the rows this instance drew that card from
    (`entries.group_identity`), so a page-supplied id can never reach a write into
    the operator's own library — the rule the rankings board already follows for
    the same reason, and the one that makes an edited attribute worth nothing.

    IT ANSWERS MARKUP, not JSON: the buttons redrawn and a toast, both out of band
    — see `_library_answer`."""
    data = await authz.json_body(request)
    settings = load_settings()
    target = str(data.get("target") or "")
    if target not in integrations_routes.LIBRARY_ID_KEYS:
        return _library_answer(request, message="Unknown service.", ok=False)
    if not integrations_routes.configured(target, settings):
        return _library_answer(
            request, ok=False,
            message=f"{integrations_routes.LIBRARY_NAMES[target]} isn't configured.")
    held = await calendar_entries.group_identity(str(data.get("key") or ""))
    if held is None:
        return _library_answer(request, message="This calendar no longer holds that title.",
                               ok=False)
    media, ids, title = held
    key = str(data.get("key"))
    result = await integrations_routes.add_title(settings, target, media, ids, title)
    # THE BUTTONS ARE DRAWN AFTER THE ADD, so a successful one comes back marked
    # in-library without anything here restating what "added" looks like: the add
    # remembers the id and LibraryView reads the same memory.
    return _library_answer(
        request, ok=bool(result.get("ok")),
        message=result.get("message") or result.get("error") or "Could not add",
        marks=_marks_for(integrations_routes.LibraryView(settings), target,
                         [(key, media, ids)]))


def _add_all_answer(request: Request, endpoint, *, confirm: str = "",
                    message: str = "", ok: bool = True, marks=()) -> Response:
    """Every answer Add all gives, drawn through one partial.

    IT ALWAYS REDRAWS THE CONTROL, because the answer REPLACES the element that
    was pressed: either with the confirmation, or with the button back where it
    was. One renderer for both means there is no state in which the page is left
    holding neither, which is what a refusal answered as a bare sentence would
    have done."""
    return templates.TemplateResponse(request, "_add_all.html", {
        "request": request, "endpoint": endpoint, "confirm": confirm,
        "message": message, "ok": ok, "marks": marks,
        # The page's own query string, so the month this acts on is the month
        # being looked at — the button, the confirmation and the commit all name
        # it the same way.
        "query": f"?{request.url.query}" if request.url.query else "",
    })


@guard.get("/calendar/fragments/add-all", AuthLevel.ADMIN)
async def add_all_fragment(request: Request):
    """The Add all button, drawn again — what Cancel asks for.

    A READ THAT CHANGES NOTHING, so it is a GET like every other fragment here.
    Cancelling could not be markup alone: the confirmation replaced the button, so
    putting the button back means drawing it, and the one place it is drawn is
    this partial."""
    settings = load_settings()
    prefs = await auth.get_user_prefs((await auth.current_user(request)).user_id)
    return _add_all_answer(request, _requested_endpoint(request, prefs, settings))


@guard.post("/calendar/library/add-month", AuthLevel.ADMIN)
async def add_month_to_library(request: Request):
    """The calendar's Add all: every title this month shows and this viewer is
    watching, added to Sonarr (a TV calendar) or Radarr (a film one).

    THE MONTH, NOT THE PAGE. Days fetch themselves as they are scrolled to, so the
    cards on screen are an accident of how far somebody has scrolled; the button
    says "this month" and this is what makes that true. The month is read by the
    page's own rules from the page's own query string — its year, month, calendar
    and `?source=` — through the same assembly, so this viewer's filters apply and
    not-watching titles are left out, exactly as they are on screen.

    ONE ADD PER TITLE: a show airing on a dozen days is one Sonarr series. A title
    already held, or carrying no id that service matches on, is skipped rather
    than sent to be refused.

    IT IS ASKED TWICE, AND BOTH ANSWERS ARE MARKUP. The first press counts and
    adds nothing, and is answered with a confirmation the server DREW — the
    sentence and the button that commits, in place of the Add all button. The
    second press carries `{"commit": true}`, does the adds, and is answered with
    the control back plus one redrawn button per title added and a line saying
    what happened (`_library_answer`). Neither step needs the page to compose a
    sentence or to know what an added button looks like.

    NEITHER STEP COMMITS THE IS-NEW BASELINE: that is the page load's decision
    alone (`assemble_month`), and a button press is not a visit."""
    data = await authz.json_body(request)
    settings = load_settings()
    user = await auth.current_user(request)
    prefs = await auth.get_user_prefs(user.user_id)
    today = clock.today()
    year = route_params.valid_year(request.query_params.get("year"), today.year)
    month = route_params.valid_month(request.query_params.get("month"), today.month)
    endpoint = _requested_endpoint(request, prefs, settings)
    target = "radarr" if endpoint.media == Media.MOVIE else "sonarr"
    service = integrations_routes.LIBRARY_NAMES[target]
    # A REFUSAL PUTS THE CONTROL BACK, because the answer replaces the button that
    # was pressed: left as a bare sentence the button would be gone and there
    # would be nothing to press again once the service came back.
    def refuse(message: str) -> Response:
        return _add_all_answer(request, endpoint, message=message, ok=False)

    if not integrations_routes.configured(target, settings):
        return refuse(f"{service} isn't configured.")
    if not integrations_routes.INTEGRATION_HEALTH[target].get("reachable"):
        return refuse(f"{service} can't be reached right now.")

    not_watching = await calendar_state.not_watching_ids(user.user_id)
    try:
        grouped, meta = await calendar_cache.assemble_range(
            endpoint, settings, tz=_resolve_viewer_tz(user, settings),
            start_date=date(year, month, 1),
            end_date=date(year, month, _calendar.monthrange(year, month)[1]),
            **vocab.active_specs(prefs, endpoint.media, honour_pause=True),
            not_watching_ids=not_watching,
            prefs=await _viewer_source_selection(request, user, endpoint.media),
        )
    except TraktError as exc:
        return refuse(str(exc))

    library = integrations_routes.LibraryView(settings)
    # KEYED ON THE LIBRARY ID rather than on the card's key, because ONE ADD PER
    # TITLE means one add per thing the service will store: a show airing on a
    # dozen days is one Sonarr series.
    wanted: dict[str, tuple] = {}
    held = 0
    for item in (item for group in grouped for item in group["items"]):
        if item.mark_key in meta["not_watching_keys"]:
            continue
        for button in library.buttons(item.media, item.ids):
            if button.kind != target or not button.library_id:
                continue
            if button.in_library:
                held += 1
            else:
                wanted.setdefault(button.library_id,
                                  (item.mark_key, item.media, item.ids, item.title))
    already = f" {held} already there." if held else ""

    if not data.get("commit"):
        if not wanted:
            return refuse(f"Nothing to add — every watching title this month is already "
                          f"in {service} or has no id {service} can use.")
        return _add_all_answer(
            request, endpoint,
            confirm=f"Add {len(wanted)} watching title{'' if len(wanted) == 1 else 's'} "
                    f"from this month to {service}?{already}")

    # Three at a time, as the page's own button-by-button add always was: these
    # are writes into a service this app does not control and cannot hurry.
    gate = asyncio.Semaphore(3)

    async def add(library_id: str, key, media, ids, title) -> tuple[tuple, dict]:
        async with gate:
            return ((key, media, ids),
                    await integrations_routes.add_title(settings, target, media, ids, title))

    results = await asyncio.gather(*(add(library_id, *rest)
                                     for library_id, rest in wanted.items()))
    added = [title for title, result in results if result.get("ok")]
    failed = len(results) - len(added)
    return _add_all_answer(
        request, endpoint, ok=not failed or bool(added),
        message=(f"Added {len(added)} to {service}."
                 + (f" {failed} could not be added." if failed else "") + already),
        # THE MARKS ARE READ AFTER THE ADDS, from a LibraryView built now: every
        # successful add remembered its id, so the buttons come back marked
        # without this restating what "added" looks like.
        marks=_marks_for(integrations_routes.LibraryView(settings), target, added))


@guard.get("/calendar/fragments/season-line", AuthLevel.CALENDAR_APPROVED)
async def season_line_fragment(request: Request):
    """The line under a card — how many episodes, what aired last, what airs
    next — drawn for the card that asked.

    THE CARD FETCHES IT FOR ITSELF as it scrolls into view: _card.html draws a
    slot carrying `hx-trigger="intersect once"`, the house pattern
    _day_skeleton.html already uses, and this answer replaces the slot. It used to
    be JSON that a script turned into markup behind a hand-held
    IntersectionObserver — one that had to be disconnected and rebuilt on every
    boosted navigation and never saw the cards on a day that arrived late. htmx
    processes a late day's slots as they land, so there is nothing to re-arm.

    THE CALLER HANDS OVER IDS, NOT A SERVICE, exactly as the details fragment
    below does. `detail_source.choose` picks who can answer, so a Simkl-only card
    gets its line as readily as one Trakt listed, and
    `DetailPort.fetch_season_summary` is what makes asking either the same act.

    NOTHING TO SAY IS AN EMPTY ANSWER AT 200, which removes the slot the way an
    empty day removes its placeholder. That covers a title nobody can describe,
    a request naming no season, and a service that could not be reached — caught
    as the shared degradation contract rather than one service's error type, so a
    rate limit never renders as "no episodes": a line that is not drawn states
    nothing. A non-200 would not be swapped at all (static/js/boost.js's
    `noSwap`), leaving an empty slot behind for a trigger that fires once.

    Gated on the CATALOGUE credential per source, never on an access token: a
    season's episode list is public, globally cached and the same for everybody,
    so it must not stop working because one person's token lapsed.
    """
    from .. import providers  # deferred: see _coverage_gap, same reason
    from ..providers import season as season_rules

    settings = load_settings()
    params = request.query_params
    season = route_params.season(params.get("season"))
    chosen = detail_source.choose(settings, detail_source.ids_from_query(params))
    provider = providers.get(chosen.source) if chosen is not None else None
    line = None
    if season is not None and provider is not None and provider.detail_port is not None:
        try:
            detail = await provider.detail_port.fetch_season_summary(
                settings, chosen.source_id, season, Media(params.get("media", "show")))
        except SourceUnavailable as exc:
            logger.info("No season line from %s: %s", chosen.source, exc)
        except ValueError:
            # A medium this app does not have, which only a hand-built address
            # can name. Nothing to describe, and nothing worth a 500.
            pass
        else:
            line = season_rules.tile_summary(detail, clock.today())
    return templates.TemplateResponse(request, "_season_line.html", {
        "request": request, "line": line, "season": season})


@guard.get("/calendar/fragments/details", AuthLevel.CALENDAR_APPROVED)
async def details_fragment(request: Request):
    """The details panel's contents for one card."""
    return await _details_view(request, episodes_only=False)


@guard.get("/calendar/fragments/details/season", AuthLevel.CALENDAR_APPROVED)
async def details_season_fragment(request: Request):
    """One season's episode rows for a card's panel — what a season tab fetches.

    THE SAME QUESTION AS THE PANEL, ASKED ABOUT A DIFFERENT SEASON, so it is the
    same body with a different amount of it drawn: resolve the ids, choose who
    answers, describe that season, read this viewer's own record of it. A route
    of its own rather than a mode flag on the one above keeps the address honest
    about what comes back — and the panel's tab strip is built by putting
    `/season` on the end of whatever address the panel itself was asked at
    (app/details_panel.py's `season_tabs`), so the two cannot drift apart in what
    a request has to carry.
    """
    return await _details_view(request, episodes_only=True)


async def _details_view(request: Request, *, episodes_only: bool) -> Response:
    """The details panel for one card, or one season of its episode rows.

    A VIEW, NOT AN API. It answers with the panel itself — templates/_details.html,
    the one this app has — so the calendar, the tracker and the public share page
    show the same fields without three renderers being kept in step. This used to
    answer JSON that a JavaScript builder turned into markup, and the two other
    builders beside it had drifted twice over.

    THE CALLER HANDS OVER IDS, NOT A SERVICE. The query carries one parameter per
    id namespace the card was drawn with — `trakt=`, `simkl=` — and
    detail_source.choose picks who to ask. That keeps "which service answers" on
    the side that can see whether a service's credentials are filled in, and it
    is why this is a branch on ONE route rather than a second route per source: a
    Simkl-only title asks the same question at the same level about the same
    thing, and a second route would be a second gate, a second refusal shape and
    a second place the client decides who to ask.

    THE NAME, THE PICTURE AND THE DESCRIPTION COME FROM THE CARD, not from the
    answer: those three are what this viewer's own field order picked, and
    the card's own radio group can flip any of them after it was rendered, so
    a panel drawn from the answer alone would open on words the reader was not
    just looking at.

    THE REQUEST NAMES A SERVICE FOR EACH, NOT THE VALUE. `?title=simkl` is read
    back out of the row this card was drawn from (`entries.stored_values`), which
    is one indexed lookup on the `key` the card already sends, and it means
    nothing a caller WROTE is rendered into a response served from the owner's
    origin — the rule share_routes._Described has always held the public panel to,
    now holding on the signed-in one as well. A parameter absent, naming a service
    with no row for this title, or naming nothing this app has registered leaves
    the field unanswered, and the panel draws the describing service's own, which
    is what it drew before any of this existed.

    Catalogue credentials only, for the same reason as the season line: everything this
    returns — overview, cast, the episode list — is public and shared, and
    "catalogue" is a question per SOURCE rather than Trakt's alone. THE WATCHED
    TICKS ARE NOT PART OF THAT and are fetched from nowhere: they are read out of
    this app's own distrakt_show_progress for the signed-in viewer, which costs no
    call, no token and no id translation.
    """
    params = request.query_params
    settings = load_settings()
    media = params.get("media", "show")
    season = route_params.season(params.get("season"))
    asked = detail_source.ids_from_query(params)
    chosen = detail_source.choose(settings, asked)
    if chosen is None:
        return details_panel.gap(request, "Nothing here can describe this item.")
    try:
        # NO SOURCE REACHABLE MEANS DRAW WHAT IS ALREADY HELD, which `describe`
        # decides: reading the store back needs no credential and makes no
        # request, and refusing instead threw away a description this instance
        # had already fetched once. None is the genuinely empty case.
        details = await detail_source.describe(settings, chosen, media, season)
    except SourceUnavailable:
        # The shared degradation contract rather than one service's error type:
        # this route can be answered by either of two sources and catching only
        # Trakt's would let Simkl's escape as a 500. A failure is something the
        # server DRAWS — a non-200 is not swapped (see boost.js's noSwap), so a
        # status code here would leave the panel on its loading line for ever.
        # The service's own words are logged rather than shown: a rate-limit
        # message from an upstream is not something the reader can act on, and
        # this is the sentence the panel has always shown for a failed lookup.
        logger.warning("Detail lookup failed for %s", chosen.source, exc_info=True)
        return details_panel.gap(request, "⚠️ Could not load details for this item.")
    if details is None:
        return details_panel.gap(request, "Nothing here can describe this item.")
    # THE IDS AND THE NAMES THE CARD CARRIED, plus whatever the answer added.
    # A Trakt-described title's payload carries no Simkl id, so building links
    # from `details` alone would offer one button for a card that legitimately has
    # two. A name only decides how a link is SPELLED, so it is read separately
    # from the ids and never reaches the who-answers decision.
    #
    # ONE MAP FOR THE BUTTONS AND THE TICKS, which is the rule the tracker's own
    # panel states: a service with a working tick has a working button, and two
    # maps would let the same panel spell the same service's address two ways.
    ids = {**asked, **detail_source.names_from_query(params),
           **(details.get("ids") or {})}
    progress = await _viewer_progress(request, params.get("key"), season, ids)
    if episodes_only:
        return details_panel.season_pane(request, details, media=media,
                                         season=season, progress=progress)
    # WHAT THE CARD IS SHOWING, read back from the row it was drawn from — one
    # lookup for all three, and the only thing the request itself supplied is a
    # service's name per field. See the docstring, and `entries.stored_values`.
    shown = await calendar_entries.stored_values(
        params.get("key", ""),
        {name: params.get(name, "") for name in calendar_entries.SHOWN_FIELDS})
    return details_panel.panel(
        request, details, media=media, season=season,
        title=shown.get("title", ""), poster=shown.get("poster", ""),
        overview=shown.get("overview", ""),
        links=detail_source.outbound_links(ids, media),
        progress=progress,
        actions=await _library_actions(request, settings, media, params),
        # THE CARD'S OWN SEASON IS THE COUNT, because a card for season 4 is a
        # card for a title with three earlier ones behind it. Nothing has to be
        # looked up to know that, which is what lets the picker be offered here at
        # all — and a card naming no season gets no strip rather than a guess.
        season_count=season or 0,
        # WHAT A TAB CARRIES, NAMED. The id parameters come from `details_url`'s
        # own walk over `Source`, so a third service registers once rather than
        # being added here too; `key` rides beside them because the ticks are
        # addressed by it. Nothing else does — a parameter this route does not
        # read has no business being rendered back into the page.
        carry=details_query_names() + ("key",))


async def _library_actions(request: Request, settings, media: str, params) -> str:
    """The panel header's add-to-library buttons for this card, as markup, or ""
    for anybody but an administrator (see `_library`) and for a title no
    configured service would take.

    FROM THE CARD'S OWN IDS, which its details address carries for an
    administrator (_card.html), rather than from the describing service's answer:
    the card's buttons behind the panel were drawn from those ids, and a button
    here marked from a different answer could disagree with them about the same
    title. The panel delivers what this returns out-of-band into its header.

    THE IDS DECIDE WHAT A BUTTON SAYS AND THE KEY IS WHAT IT POSTS. The ids
    answer "is this title in that library"; the add itself names the card and
    lets the route read the ids back, so what reaches the template is the key."""
    library = _library(await auth.current_user(request), settings)
    if library is None:
        return ""
    buttons = library.buttons(
        media, {"tvdb": params.get("tvdb", ""), "tmdb": params.get("tmdb", "")})
    if not buttons:
        return ""
    return Markup(templates.get_template("_library_actions.html").render(
        buttons=buttons, key=params.get("key", "")))


async def _viewer_progress(request: Request, key: str | None, season: int | None,
                           ids: dict):
    """This viewer's own watched episodes for the card's season, or None.

    THE CALENDAR OPTS IN AND THE TEMPLATE DRAWS WHAT IT IS GIVEN, which is why
    this is a read in a route rather than anything the panel knows about. The
    tracker files a person's watched episodes under the SHARED identity waterfall
    (app/providers/base.py's MATCH_SOURCES), which is exactly what a calendar card
    resolves to and carries as its `key` — so this is a plain per-user database
    read: no provider call, no token, no id translation.

    A SEASON WITH NOTHING RECORDED STILL DRAWS ITS TICKS, hollow, and that is a
    correction to what this first shipped as. The reasoning then was that a
    calendar card is only something on television, so hollow ticks would claim a
    knowledge of the viewer's watching that the calendar does not have. What that
    missed is what a tick IS: it is the link to that episode at that service, and
    withholding it withheld the one control on the row that works whether or not
    anything has been recorded. An unticked box saying "not recorded, and here is
    where to go about it" is the honest reading, and it is the same one the
    tracker's own panel has always taken.

    AN ACCOUNT THAT SYNCS NOTHING GETS THEM TOO, which is the second half of the
    same correction. The worry was that a tick names a service the viewer does not
    use; the answer is that it does not claim they use it — it links to the
    episode's page at that service, which is a useful place to go whether or not
    anybody has an account there. Nothing about a tick is a statement about the
    reader.

    A HAND-EDITED `key` READS THE CALLER'S OWN ROWS AND NOBODY ELSE'S — the read
    is scoped to the session's user id, so the worst it can do is show somebody
    their own viewing of a different title, which the tracker shows them anyway.

    `ids` IS THE SAME MAP THE OUTBOUND BUTTONS ARE BUILT FROM, so a tick and a
    button for the same service on the same panel are spelled the same way — the
    rule the tracker's own panel already states. Where the card carries no id for
    a service, the progress rows fill it in: each one records the id the service
    that wrote it knows the title by.

    The import is function-local because the tracker reads the calendar back, and
    naming it at module level would close that loop at import time; see
    tests/kernel/test_layering.py, where the edge is declared DEFERRED for exactly
    that reason.
    """
    if not key or season is None:
        return None
    try:
        item_key = parse_item_key(key)
    except ValueError:
        return None
    user = await auth.current_user(request)
    if user is None:
        return None
    from ..distrakt import episode_progress
    progress = await episode_progress.for_season(user.user_id, item_key, season, ids)
    return progress


@guard.get("/api/state", AuthLevel.CALENDAR_APPROVED)
async def get_state(request: Request):
    user = await auth.current_user(request)
    today = clock.today()
    year = route_params.valid_year(request.query_params.get("year"), today.year)
    month = route_params.valid_month(request.query_params.get("month"), today.month)
    endpoint = get_endpoint(request.query_params.get("endpoint"))
    return JSONResponse(await calendar_state.load_state(user.user_id, endpoint.key, year, month))


async def _marked_answer(request: Request, user, endpoint, year: int, month: int,
                         item_id: str) -> Response:
    """What a mark looks like on screen, drawn by the side that decided it.

    EVERY CARD FOR THIS TITLE ON THIS MONTH, plus the two counts a mark moves,
    all out of band (_marked_cards.html). The page used to work this out for
    itself: it wrote the class onto each card it was holding, moved the tiles by
    this show's own airing count taken from a per-show map shipped with the page,
    remembered the mark until its save landed so that a day arriving in the
    meantime could be corrected against it, and put everything back if the save
    failed. All of that was the browser keeping a second copy of what the server
    already knew, and the copy could only ever describe the days the page
    happened to be holding.

    IT IS THE SAME ASSEMBLY THE CARDS COME FROM, read AFTER the write, so the
    numbers in this answer and the cards in this answer cannot disagree.

    A TITLE THAT IS NOT ON THIS MONTH ANSWERS WITH THE COUNTS ALONE. That is not
    an error: the mark is a statement about the show and it has been stored
    whatever month is on screen, and a page whose month does not draw the title
    has nothing to redraw.
    """
    settings = load_settings()
    prefs = await auth.get_user_prefs(user.user_id)
    tz = _resolve_viewer_tz(user, settings)
    not_watching = await calendar_state.not_watching_ids(user.user_id)
    try:
        grouped, meta = await calendar_cache.assemble_range(
            endpoint, settings, tz=tz,
            start_date=date(year, month, 1),
            end_date=date(year, month, _calendar.monthrange(year, month)[1]),
            **vocab.active_specs(prefs, endpoint.media, honour_pause=True),
            not_watching_ids=not_watching,
            prefs=await _viewer_source_selection(request, user, endpoint.media),
            # `allow_fetch=False`, WHICH IS THE WHOLE DIFFERENCE BETWEEN THIS READ
            # AND THE PAGE'S. The page asked for this month and filled whatever it
            # had to; this is redrawing cards that are ALREADY ON SCREEN, so
            # whatever is stored is by definition what drew them. Letting a mark
            # fill a window would make pressing an eye spend a sequence of paced
            # upstream calls — for a month the reader is already looking at — the
            # first time a cached window aged out mid-session.
            allow_fetch=False,
        )
    except TraktError as exc:
        # The mark IS stored — that happened before this read. A month that
        # cannot be re-read leaves the page showing what it showed, which is one
        # card out of date until the next load rather than a lost mark.
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=200)

    # `not_watching_keys` rather than the raw stored ids, for the reason the page
    # render gives: a card asks about `mark_key`, which the assembler expands.
    marked = meta["not_watching_keys"]
    # THE CARDS THIS MARK COVERS, found by the mark rule itself rather than by
    # key equality. The key the page posted is the one it DREW; a Simkl title's
    # key can move under it when enrichment adds a tmdb id (state.key_spellings),
    # and matching on equality then found no card and redrew nothing — the button
    # did not flip until a reload.
    covers = {item_id}
    items = [item for group in grouped for item in group["items"]
             if calendar_state.marked(covers, item)]
    # AND THE CHIP FOR EVERY DAY THIS TITLE AIRS ON, because with hide mode on a
    # mark can empty a day outright and a chip offering to scroll somewhere blank
    # is the thing `_day_chip` exists to avoid. Only those days: a mark cannot
    # change a day the title does not air on, and the whole strip would be
    # thirty-one answers to a question about one or two of them. Drawn by the
    # same rule the shell's chips came from, which is why it is that function
    # being called rather than this one working the condition out again.
    days = {group["date"] for group in grouped
            if any(calendar_state.marked(covers, item) for item in group["items"])}
    chips = [_day_chip(date.fromisoformat(group["date"]), group["items"], marked,
                       prefs["hide_not_watching"])
             for group in grouped if group["date"] in days]
    logo_networks = await _logo_networks(settings, items)
    return templates.TemplateResponse(request, "_marked_cards.html", {
        "chips": chips,
        "request": request, "items": items,
        "not_watching": marked, "new_ids": meta.get("new_ids") or set(),
        "can_filter": True, "badge_modes": vocab.BadgeModes(prefs),
        "settings": settings, "is_admin": bool(user and user.is_admin),
        "library": _library(user, settings),
        # WHICH NETWORKS HAVE A LOGO, asked once here rather than once per
        # card by the browser — see `_logo_networks`.
        "logo_networks": logo_networks,
        "month": month, "year": year, "endpoint": endpoint,
        # THE ASSEMBLER'S OWN COUNTS, not a second tally of the same items: it
        # already translated this viewer's stored marks into mark keys to build
        # `grouped`, and counting them again here would be a second answer to a
        # question that has one.
        "stats": {"watching": meta["watching"], "not_watching": meta["not_watching"]},
    })


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
            user.user_id, item_id, route_params.ticked(payload.get("not_watching")),
        )
        return await _marked_answer(request, user, endpoint, year, month, item_id)

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


def _release_type_spec(value) -> str:
    """Normalize a release-type spec to comma-separated numbers, keeping the
    same leading-'-' exclude convention every other spec here uses.

    The vocabulary is TMDB's release-type numbering, which is what the service
    publishes (see app/providers/simkl/titles.py's RELEASE_TYPE_LABELS). A token
    that is not a number is DROPPED rather than stored: the read path ignores
    one it cannot parse, so keeping it would store a preference that can never
    do anything and would show up on the filters screen as if it could.
    """
    kept: list[str] = []
    for raw in str(value or "").split(","):
        token = raw.strip()
        bare = token[1:].strip() if token.startswith("-") else token
        if not bare.isdigit():
            continue
        spelled = f"-{int(bare)}" if token.startswith("-") else str(int(bare))
        if spelled not in kept:
            kept.append(spelled)
    return ", ".join(kept)


def _network_list(value) -> list[str]:
    """Networks as a de-duplicated list, from either a JSON array or the comma
    string a text field produces.

    THE DUPLICATE CHECK IS EXACT, LIKE THE MATCHING. It folded case once, on the
    reasoning that somebody typing "HBO, hbo" meant one network — which is true
    of a typo and false of the vocabulary: `parse_network_spec` matches names
    exactly BECAUSE one week of this calendar carried both 'TVN' and 'tvN', a
    Polish broadcaster and a Korean one. Folding here made those two one entry
    and silently dropped whichever was named second, so a viewer could add both
    in the filters panel and find only one stored. Two spellings the read path
    treats as two networks have to survive as two.
    """
    raw = [str(v) for v in value] if isinstance(value, list) else str(value or "").split(",")
    seen: set[str] = set()
    names: list[str] = []
    for name in (item.strip() for item in raw):
        if name and name not in seen:
            seen.add(name)
            names.append(name)
    return names


@guard.get("/api/me/prefs", AuthLevel.CALENDAR_APPROVED)
async def get_me_prefs(request: Request):
    """The viewer's own view preferences, for the Filters panel to populate from.

    Separate from the Settings panel (app/settings_routes.py), which is
    ADMIN-only and describes the instance.
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
    # WHICH MONTH IS ON SCREEN, for the answer rather than for the write: a view
    # preference has no month in it, but hiding not-watching can empty a DAY, and
    # the chip that day owns is a fact about the month being looked at.
    today = clock.today()
    year = route_params.valid_year(request.query_params.get("year"), today.year)
    month = route_params.valid_month(request.query_params.get("month"), today.month)
    endpoint = get_endpoint(request.query_params.get("endpoint"))

    updates: dict = {}
    if "card_style" in data and data["card_style"] in _CARD_STYLES:
        updates["card_style"] = data["card_style"]
    if "day_packing" in data and data["day_packing"] in _DAY_PACKINGS:
        updates["day_packing"] = data["day_packing"]
    # `ticked` RATHER THAN `bool`, and that distinction is the whole of it: this
    # arrives as a checkbox's own "0"/"1" now (templates/index.html posts the box
    # with a hidden companion, because an unticked box sends nothing and absent
    # means leave alone here), and `bool("0")` is True. route_params.ticked is the
    # one place that knows which spellings mean on.
    if "hide_not_watching" in data:
        updates["hide_not_watching"] = route_params.ticked(data["hide_not_watching"])
    # THE FILTERS ARE NOT HERE ANY MORE. They are one panel's worth of chips,
    # submitted whole, and they now have a route of their own that speaks that
    # shape — see post_me_filters. This one keeps the view preferences: what a
    # card looks like, how days pack, whether marked titles hide. Both are
    # "things the viewer chose", which is why they shared a route for a while,
    # but they are not the same verb: these arrive one field at a time as a
    # control is touched, and a filter save replaces every answer on a tab at
    # once.
    if not updates:
        return JSONResponse({"ok": False, "error": "Nothing to update"}, status_code=400)
    await auth.update_user_prefs(user.user_id, **updates)
    if await share_links.get(user.user_id) is not None:
        await share_links.update_owner_defaults(user.user_id, **updates)
    return await _view_answer(request, user, endpoint, year, month,
                              chips="hide_not_watching" in updates)


async def _view_answer(request: Request, user, endpoint, year: int, month: int,
                       *, chips: bool) -> Response:
    """What a changed view preference looks like on screen, drawn by the server.

    THE CLASSES ARE ON <body> AND <body> IS THE ONE ELEMENT A SWAP NEVER
    REPLACES, so the answer is `#pageMeta` out of band: it sits inside the body,
    it carries what the classes should be, and static/js/boost.js already wears
    whatever it says on arrival (templates/_calendar_meta.html). Nothing in the
    browser decides which class a card style means.

    THE DAY CHIPS COME TOO WHEN HIDING CHANGED, and only then. With hiding on, a
    day whose every title is marked draws nothing at all, and a chip that went on
    offering to scroll to it would be pointing at nothing — which is the state
    `_day_chip` already knows how to draw. Card style and day packing cannot
    empty a day, so they answer with the classes alone.

    IT IS A CACHE-ONLY READ (`allow_fetch=False`) for the reason the watch
    toggle's is: this is re-describing a month ALREADY ON SCREEN, so whatever is
    stored is by definition what drew it, and letting a preference toggle spend a
    sequence of paced upstream calls would be the wrong thing to do with a press.
    """
    settings = load_settings()
    prefs = await auth.get_user_prefs(user.user_id)
    view = _view_preferences(prefs, settings)
    context = {
        "request": request,
        "view": view,
        # The same answer the page renders it from, so the reveal cannot
        # appear or vanish as a side effect of changing a card style.
        "distrakt_available": bool(
            user and user.distrakt_approved and user.has_tracker_identity),
        "chips": [],
    }
    if chips:
        try:
            grouped, meta = await calendar_cache.assemble_range(
                endpoint, settings, tz=_resolve_viewer_tz(user, settings),
                start_date=date(year, month, 1),
                end_date=date(year, month, _calendar.monthrange(year, month)[1]),
                **vocab.active_specs(prefs, endpoint.media, honour_pause=True),
                not_watching_ids=await calendar_state.not_watching_ids(user.user_id),
                prefs=await _viewer_source_selection(request, user, endpoint.media),
                allow_fetch=False,
            )
        except TraktError:
            # The preference IS saved; a month that cannot be re-read leaves the
            # chips describing the previous answer until the next load, which is
            # a stale grey rather than a lost setting.
            grouped, meta = [], {"not_watching_keys": set()}
        context["chips"] = _day_chips(
            MonthAssembly(grouped=grouped), year, month,
            _calendar.monthrange(year, month)[1],
            meta["not_watching_keys"], view["hide_not_watching"])
    return templates.TemplateResponse(request, "_view_answer.html", context)


def _source_selection_from_form(data: dict, settings,
                                saved: source_prefs.SourcePrefs
                                ) -> tuple[source_prefs.SourcePrefs, str | None]:
    """The source preference this panel payload states, and the refusal if it
    states one that cannot be honoured.

    NOTHING IS WRITTEN HERE. The save needs the answer BEFORE it writes anything
    -- unticking every service is the one thing the panel can ask for and not
    get, and refusing it first leaves the panel exactly as the viewer left it
    rather than half-saved -- and the Clear button needs the same answer only to
    draw the switches back. One function, two readers.

    AN ACCOUNT WITH NO ROW HAS NO OPINION, and saving one it did not ask for
    would undo that: `load` answers with the defaults rather than creating
    anything, so the caller writes only when the selection actually moved.
    """
    chosen_prefs = saved
    for media in (Media.SHOW, Media.MOVIE):
        chosen = vocab.sources_from_form(data, media)
        # AN INSTANCE WITH NOTHING TO CHOOSE BETWEEN DRAWS NO CONTROL, so a
        # payload naming services on such a calendar is not a viewer's doing and
        # is ignored rather than refused.
        available = {name for name, _ in _answering_services_for_media(media, settings)}
        if chosen is None or not available:
            continue
        if unknown := chosen - available:
            return saved, f"This app has no calendar source called {sorted(unknown)[0]!r}."
        if not chosen:
            return saved, "At least one service has to be showing."
        # EVERY SERVICE TICKED IS `auto`, NOT A NAMED SET, and the difference
        # outlives this request: `auto` means "whatever there is, now and later",
        # so an instance that registers a third service starts showing it, where
        # a named set is a choice made from the menu that existed at the time.
        selection = (source_prefs.AUTO if chosen == available
                     else source_prefs.SEPARATOR.join(sorted(chosen)))
        field = ("movie_calendar_source" if media is Media.MOVIE else "calendar_source")
        if getattr(chosen_prefs, field) != selection:
            chosen_prefs = dataclasses.replace(chosen_prefs, **{field: selection})
    return chosen_prefs, None


def _normalized_specs(data: dict) -> dict:
    """The chip answers in this payload, as the shapes the columns store.

    THROUGH THE SAME NORMALIZERS EVERY OTHER WRITER USES, so a spec assembled
    from chips is stored in the shape a badge press and the old text fields both
    produced. `movie_release_types` is the one with a vocabulary narrow enough to
    validate, and it is dropped here rather than at read for the reason its own
    normalizer states.
    """
    specs = dict(vocab.specs_from_form(data))
    for field, value in list(specs.items()):
        if field == vocab.NETWORK_FILTER:
            specs[field] = _network_list(value)
        elif field == vocab.MOVIE_RELEASE_TYPES:
            specs[field] = _release_type_spec(value)
        else:
            specs[field] = _filter_spec(value)
    return specs


def _prefs_showing(data: dict, prefs: dict, *, cleared: tuple[str, ...] = ()) -> dict:
    """This viewer's stored preferences with what the PANEL is currently holding
    laid over them.

    WHAT MAKES THE PANEL ONE RENDERER RATHER THAN TWO. `_filter_panel` draws from
    a preferences dict; opening the panel hands it the stored one, and adding a
    typed token or pressing Clear hands it this -- the stored one with the
    unsaved edits on top. So a chip the server appends and a chip the panel
    opened with are the same markup from the same code, and nothing has to
    describe a half-edited panel in a second language.

    NOTHING IS SAVED BY EITHER. `cleared` zeroes the dimensions of one medium,
    which is what Clear does and why it does not write: closing the panel after
    pressing it leaves the stored filters alone, the same bargain every other
    field in these modals makes.
    """
    showing = {**prefs, **_normalized_specs(data)}
    for field in cleared:
        showing[field] = [] if field in vocab.LIST_FIELDS else ""
    if "filters_on" in data:
        showing["filters_paused"] = not route_params.ticked(data["filters_on"])
    return showing


def _panel_context(request: Request, settings, prefs: dict,
                   saved: source_prefs.SourcePrefs, showing: dict | None = None) -> dict:
    """The context the filters panel template is rendered from.

    `showing` carries the service ticks the panel is holding, when it is being
    drawn again mid-edit; None means "draw the stored ones", which is what
    opening it does.
    """
    endpoint = _requested_endpoint(request, prefs, settings)
    return {"request": request, "settings": settings, "endpoint": endpoint,
            # Handed on to the save, which answers with the month redrawn — see
            # `_calendar_query`. The panel is asked for with the calendar's own
            # query string, so this is that string travelling one more hop.
            "calendar_query": _calendar_query(request),
            "filter_panel": _filter_panel(prefs, saved, settings, endpoint, showing)}


def _sources_showing(data: dict) -> dict:
    """The service ticks a mid-edit payload states, per medium — see
    `_source_toggles`. A medium the payload does not mention comes back None,
    which means "draw what is stored"."""
    return {media: vocab.sources_from_form(data, media)
            for media in (Media.SHOW, Media.MOVIE)}


@guard.get("/calendar/fragments/filters", AuthLevel.CALENDAR_APPROVED)
async def filters_panel_fragment(request: Request):
    """The filters panel, drawn open, with every chip already carrying its answer.

    A FRAGMENT RATHER THAN MARKUP THE PAGE CARRIES, which is a reversal worth
    stating: the panel used to ship with the calendar so that opening it cost
    nothing. What that bought was one request; what it cost was every way the
    panel could be left describing something other than what is stored -- a chip
    added by typing and abandoned, a cycle nobody saved, the counts on the tab
    beside the one just written. Asked for on every opening, the panel IS the
    server's answer about the stored state and there is no second copy to keep in
    step. It is discarded when it closes (`data-modal-discard` in static/js/ui.js),
    so Cancel is the element leaving the page rather than a form being reset.
    """
    user = await auth.current_user(request)
    prefs = await auth.get_user_prefs(user.user_id)
    settings = load_settings()
    # THE SAVED PREFERENCE, not the `?source=` override the toolbar may be
    # showing: the panel offers to make permanent what this account actually
    # stated rather than what it happens to be looking at right now.
    saved = await source_prefs.load(user.user_id)
    return templates.TemplateResponse(request, "_filters_panel.html",
                                      _panel_context(request, settings, prefs, saved))


@guard.post("/calendar/filters/chip", AuthLevel.CALENDAR_APPROVED)
async def add_filter_chip(request: Request):
    """Draw a chip for a token the vocabulary does not name.

    THE SERVER SPELLS IT BECAUSE THE SERVER OWNS EVERY RULE IN IT. The field
    name, the positional ids three labels point `for` at, whether two spellings
    are one token (networks keep their case, everything else folds), that a colon
    would produce a name reading as a different field, and that a comma-separated
    paste is several tokens -- all of that was written a second time in
    JavaScript, and all of it is app/calendar/vocab.py's.

    IT STORES NOTHING. The chip joins the panel set to "only this", and Save is
    what writes it -- somebody who went to the trouble of typing a name is naming
    something they want, and it is one press from the other answer either way.

    THE WHOLE PANEL IS THE ANSWER and the browser takes the one field out of it
    (`hx-select`). That is what keeps each field's prose in the template beside
    the field it explains rather than in a table here: there is one renderer for
    a field, and it is the one the panel itself uses.
    """
    user = await auth.current_user(request)
    prefs = await auth.get_user_prefs(user.user_id)
    settings = load_settings()
    saved = await source_prefs.load(user.user_id)
    data = await authz.json_body(request)
    field = str(route_params.last_value(data.get("field")) or "")
    if field not in vocab.FIELDS:
        return authz.error("Not a filter this panel offers")

    showing = _prefs_showing(data, prefs)
    is_list = field in vocab.LIST_FIELDS
    spec = showing.get(field)
    for raw in str(route_params.last_value(data.get("token")) or "").split(","):
        token = raw.strip()
        # A colon would produce a field name that reads as a different field, so
        # it is dropped rather than misfiled. Silently, because the panel comes
        # back drawn: a chip that did not appear is the answer, and the box still
        # holds nothing anybody has lost.
        if not vocab.addable(token):
            continue
        spec = calendar_filter.merge_token(spec, token, vocab.INCLUDE, is_list=is_list)
    showing[field] = _network_list(spec) if is_list else _filter_spec(spec)
    return templates.TemplateResponse(request, "_filters_panel.html",
                                      _panel_context(request, settings, showing, saved,
                                                     _sources_showing(data)))


@guard.post("/calendar/filters/clear", AuthLevel.CALENDAR_APPROVED)
async def clear_filters(request: Request):
    """Set one medium's chips back to grey, WITHOUT saving.

    ONE TAB ONLY, and the same bargain every other field in these modals makes:
    clear then close and the stored filters are untouched. Services are
    deliberately left alone -- an unticked service is a NARROWING, and clearing
    it would mean switching every service back on, which is the one state the
    save refuses and reads as the button working backwards.

    IT IS A REQUEST BECAUSE THE PANEL IS THE SERVER'S. Doing it in the browser
    meant knowing which inputs a chip is made of and which of the two tabs is
    showing, and both of those are this module's answers. The medium comes from
    the button pressed rather than from reading the tab, because the panel draws
    one button per medium and the stylesheet shows whichever matches the open tab
    -- so there is nothing to read.
    """
    user = await auth.current_user(request)
    prefs = await auth.get_user_prefs(user.user_id)
    settings = load_settings()
    saved = await source_prefs.load(user.user_id)
    data = await authz.json_body(request)
    medium = str(route_params.last_value(data.get("medium")) or "")
    if medium not in ("show", "movie"):
        return authz.error("Not a medium this panel has a tab for")
    cleared = vocab.MOVIE_FIELDS if medium == "movie" else vocab.TV_FIELDS
    showing = _prefs_showing(data, prefs, cleared=cleared)
    return templates.TemplateResponse(request, "_filters_panel.html",
                                      _panel_context(request, settings, showing, saved,
                                                     _sources_showing(data)))


@guard.post("/calendar/filters", AuthLevel.CALENDAR_APPROVED)
async def post_filters(request: Request):
    """Save the filters panel -- its chips, its services and the master switch --
    and answer with the calendar redrawn.

    ONE ROUTE FOR A PANEL THAT WAS TWO REQUESTS. The narrowing and the service
    selection live in different tables and used to be posted separately, in a
    fixed order so they could not land half-applied. They still write to two
    tables, and the order still matters for exactly the same reason -- the
    services save is the one that can be REFUSED -- but a viewer pressed one
    button, so it is one request that either takes both or takes neither.

    THE CHIPS ARRIVE ONE FIELD PER TOKEN and app/calendar/vocab.py turns them
    back into specs, using app/calendar/filter.py's own merge to do it. Nothing
    in the browser knows what a spec looks like, and a spec written here is
    byte-identical to one a card badge writes.

    ONLY THE DIMENSIONS THE PAYLOAD MENTIONS ARE WRITTEN, which is what makes a
    per-tab save safe: the panel posts every chip of both tabs, including the
    ones set to nothing, so clearing works. A dimension it draws with no chips at
    all -- networks, for an account that has never named one -- says so with its
    own marker (vocab.presence_name), so "shown and empty" and "never shown" stay
    different statements rather than one silence.

    THE PAUSE IS A FIELD LIKE ANY OTHER on the same form, so switching filters
    off and editing them in the same visit is one save. It is stored rather than
    held for the session because "show me everything for a bit" is a state
    somebody wants to still be in tomorrow, and every read path asks it before
    applying anything (app/calendar/vocab.py's `active_specs`).

    IT ANSWERS WITH THE MONTH REDRAWN rather than telling the page to reload.
    Filtering is applied while a month is assembled, so the calendar behind the
    panel is genuinely stale the moment this returns -- but the old answer was
    `window.location.reload()`, which is this same rebuild plus a round trip for
    the shell, the stylesheet, the fonts and eight scripts. The two marks that
    depend on the filters and live OUTSIDE that region -- the toolbar's Filters
    button and the month's total beside it -- ride along out of band, and the
    panel closes by having its slot emptied the same way.
    """
    user = await auth.current_user(request)
    data = await authz.json_body(request)
    settings = load_settings()

    # SERVICES FIRST, BECAUSE THIS IS THE HALF THAT CAN REFUSE. Refusing before
    # anything is written leaves the panel exactly as the viewer left it rather
    # than half-saved -- and the refusal is retargeted at the panel's own notice
    # line, so a sentence never replaces the calendar behind it.
    saved_sources = await source_prefs.load(user.user_id)
    chosen_sources, refusal = _source_selection_from_form(data, settings, saved_sources)
    if refusal is None and chosen_sources != saved_sources:
        try:
            await source_prefs.save(chosen_sources)
        except ValueError as exc:
            refusal = str(exc)
    if refusal is not None:
        return templates.TemplateResponse(
            request, "_filters_notice.html", {"request": request, "error": refusal},
            headers={"HX-Retarget": "#filtersNotice", "HX-Reswap": "innerHTML"})

    updates: dict = _normalized_specs(data)
    # THE SWITCH IS SENT THE WAY IT READS AND STORED THE WAY IT DEFAULTS, and
    # this line is the whole of the difference. On the panel it says "Filters
    # are on", because a switch that lights up when the thing it names is
    # happening is the only polarity anybody reads correctly; in the column it
    # is `filters_paused`, because every account starts having paused nothing
    # and a boolean whose default is false needs no migration to mean that.
    # Inverted once, here, rather than drawing the control backwards to match a
    # column name.
    if "filters_on" in data:
        updates["filters_paused"] = not route_params.ticked(data["filters_on"])
    if updates:
        await auth.update_user_prefs(user.user_id, **updates)
        # Mirrored into share_links exactly as the badge route does -- that
        # table's owner-default columns are seeded from user_prefs and have no
        # editor of their own. The PAUSE is deliberately not among them: it is a
        # private look at your own calendar, and a share link keeps filtering
        # whatever the owner is currently looking at.
        if await share_links.get(user.user_id) is not None:
            await share_links.update_owner_defaults(
                user.user_id, **{k: v for k, v in updates.items() if k != "filters_paused"})

    prefs = await auth.get_user_prefs(user.user_id)
    month = await _month_context(request, user, settings, prefs)
    with span("calendar.render", cards=month["inline_cards"]):
        return templates.TemplateResponse(request, "_filters_answer.html", month)


@guard.post("/calendar/filters/badge", AuthLevel.CALENDAR_APPROVED)
async def post_filter_badge(request: Request):
    """Add one token to one of this viewer's filter dimensions -- the offer a
    card's own badge makes.

    ITS OWN ROUTE RATHER THAN THE PANEL'S SAVE, because it is a different verb.
    That one REPLACES whichever dimensions it is handed, which is what a panel
    holding every value wants; this one changes a single token inside one field
    and must leave the rest of that field alone. Sending the whole spec from a
    badge would mean the browser reading the current one first and merging it,
    which is a race and, worse, a second statement of the spec format in another
    language.

    THE MERGE HAPPENS HERE FOR THAT REASON. app/calendar/filter.py owns what a
    spec IS -- the leading '-', which dimensions fold case, that networks are a
    list -- and `merge_token` is written beside the parsers that read it. The
    browser sends a dimension, a token and a verdict; it knows nothing about
    specs and cannot drift from them.

    IT ANSWERS WITH THE OFFER REDRAWN FROM STORAGE, for every card showing that
    token at once, plus the sentence. So a refusal comes back drawn as stored
    rather than as pressed -- the radio had already recorded the press by the
    time this ran, and a badge left claiming a filter nobody stored is exactly
    the failure this shape removes.

    IT DOES NOT RE-RENDER THE MONTH. Filtering is applied while a month is
    assembled, so a change lands on the next read -- the next month, or a
    refresh. Rebuilding a month on every badge press would spend a whole
    assembly on a one-token edit somebody is likely to follow with another, and
    the sentence says so rather than leaving anybody to wonder why the card they
    just filtered is still in front of them.
    """
    user = await auth.current_user(request)
    data = await authz.json_body(request)
    dimension = str(route_params.last_value(data.get("dimension")) or "")
    media = str(route_params.last_value(data.get("media")) or "")
    token = str(route_params.last_value(data.get("token")) or "").strip()
    mode = str(route_params.last_value(data.get("mode")) or "")
    field = vocab.badge_field(dimension, media)
    prefs = await auth.get_user_prefs(user.user_id)

    def answer(message: str, ok: bool, status: int = 200):
        """The offer as STORED, whatever happened -- see the docstring."""
        return templates.TemplateResponse(
            request, "_filter_badge_answer.html",
            {"request": request, "dimension": dimension, "token": token, "media": media,
             "badge_modes": vocab.BadgeModes(prefs), "message": message, "ok": ok},
            status_code=status)

    if field is None:
        return answer("That isn't something this calendar filters on.", False, 400)
    if not token:
        return answer("There is nothing on that badge to filter on.", False, 400)
    if mode not in vocab.MODES:
        return answer("That isn't one of the three answers a badge offers.", False, 400)

    is_list = field in vocab.LIST_FIELDS
    merged = calendar_filter.merge_token(prefs.get(field), token, mode, is_list=is_list)
    # THROUGH THE SAME NORMALIZERS THE PANEL'S OWN SAVE USES, so a spec written by
    # a badge and one saved from the panel cannot come out in different shapes.
    merged = _network_list(merged) if is_list else _filter_spec(merged)
    await auth.update_user_prefs(user.user_id, **{field: merged})
    # Mirrored into share_links exactly as post_me_prefs does -- that table's
    # owner-default columns are seeded from user_prefs and have no editor of
    # their own, and a badge press is as much an edit as the panel's save.
    if await share_links.get(user.user_id) is not None:
        await share_links.update_owner_defaults(user.user_id, **{field: merged})
    prefs = {**prefs, field: merged}
    said = {vocab.EXCLUDE: f"Filtering out {token}",
            vocab.INCLUDE: f"Showing only {token}"}
    return answer(f"{said.get(mode, f'{token} filter removed')} — applies on next load", True)


@guard.post("/api/me/timezone", AuthLevel.CALENDAR_APPROVED)
async def post_me_timezone(request: Request):
    """Persist the viewer's calendar timezone, and ask for the page again.

    No automatic browser detection — this is reached either by picking a
    zone from the header's dropdown, or by the "use my device timezone" button
    filling in Intl's resolved zone name before the same request fires.

    ANSWERED WITH `HX-Refresh` RATHER THAN NOTHING, because every day header and
    air time on the page was rendered server-side for the OLD zone: nothing the
    browser could do to the page it is holding would make it right. htmx turns
    that header into a reload, which is what the caller used to do for itself.
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
    return Response(status_code=204, headers={"HX-Refresh": "true"})


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
