"""The hidden Discord tracker: its page shell and the whole /api/distrakt/* API.

Every route here is DISTRAKT_APPROVED, and every one of them reads ONE person's
private Trakt history — their progress, their plays, their films. That is why
`_distrakt_settings` exists and why nothing here uses the app-wide token: the
operator's token would hand every user the operator's viewing instead of their
own.

Most of the mutating routes end by returning the whole recomputed month, so the
page never has to guess what its change did. That shared body is
`_distrakt_month_payload`, and it comes in three shapes depending on what can be
reached: a frozen past month rendered from its own snapshot, an open month
computed live against Trakt, and — when a shared Trakt prerequisite fails — the
last-known stored totals plus a notice, at HTTP 200. The third one is why the
tracker stays usable through a rate-limit window instead of showing 0/0.
"""
from __future__ import annotations

import asyncio
import calendar as _calendar
import dataclasses
import json
import logging
from datetime import date, datetime, timezone

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from . import backfill, counts, discord_fmt, lifecycle, live, unsettled, watch_history
# The data layer is reached through this package's own public surface, the same
# names an outside caller uses, rather than through the six modules those names
# are defined in: a route handler has no business knowing which half of the
# tracker `load_month` or `compute_live_shows` lives in.
from .. import distrakt as distrakt_store
from .. import auth, authz, chrome, clock, db, route_params
from ..auth import simkl_routes, trakt_routes
from ..auth import AuthLevel
from ..calendar import share_links
# The turn-away marks themselves, written where a tracker verdict has to reach the
# main calendar. Read back through lifecycle.reconcile_turn_aways rather than here.
from ..calendar import state as calendar_state
from ..config import load_settings
from ..endpoints import endpoint_choices
from ..media import logos
from ..perftrace import span
from ..providers.base import ID_KEYS, ItemKey, Media, collect_ids, parse_item_key
from ..providers.trakt import TraktError, TraktRateLimitError
# Reached through the MODULE rather than by importing the functions off it. A name
# bound at import time is a second reference to the same function that patching
# the owning module cannot reach, so a test double installed on
# app.providers.trakt.detail would leave this file calling the real one — a live
# network call in a test that then passes for the wrong reason.
from ..providers.trakt import detail as trakt_detail, sync as trakt_sync
from ..templating import templates

logger = logging.getLogger(__name__)

router = APIRouter()
guard = authz.Guard(router)

# Which ids arriving in a request body are read as integers. imdb ids are not
# numbers and slugs are words, so those stay text; everything else is a numeric id
# in its own namespace and is coerced rather than stored as whatever the client
# happened to send, or the same title would key differently on two adds.
_NUMERIC_ID_KEYS = frozenset(ID_KEYS) - {"imdb", "slug"}

# What an empty month that has not begun says for itself. There is only ever one
# sentence for it now, at any distance ahead: every future month is waiting to be
# asked, and the ask is a button on the page saying this.
MONTH_AWAITS_IMPORT = ("This month hasn't begun yet, so nothing has been gathered for it. "
                       "Press ⤓ Import to build it from what premieres in it.")


class RequestError(ValueError):
    """A request body the caller has to fix. Its message is what the client is
    told, so it says what is wrong rather than naming a field type."""


def _client_ids(raw) -> dict:
    """An id map out of a request body: known namespaces only, numbers as numbers.

    The client supplies these on the add flows, which it always has — what is new
    is that they decide which row the record becomes, so they are narrowed and
    coerced here at the boundary rather than trusted into a key.
    """
    if not isinstance(raw, dict):
        return {}
    cleaned = {}
    for key, value in raw.items():
        if key not in ID_KEYS or value in (None, ""):
            continue
        if key in _NUMERIC_ID_KEYS:
            try:
                cleaned[key] = int(value)
            except (TypeError, ValueError):
                continue  # a numeric namespace with a non-number in it names nothing
        else:
            cleaned[key] = str(value)
    return collect_ids(cleaned)


def _row_target(data: dict) -> tuple[ItemKey, int]:
    """The (identity, season) a mutating request names, or a RequestError.

    ONE parser for every route that changes a roster row, because they all address
    a row the same way — the flat item key the month payload handed the client,
    plus the season. The key is validated against the same closed sets the ranker's
    is (see providers.base.parse_item_key), so a malformed one is refused here
    rather than reaching a query.
    """
    try:
        key = parse_item_key(data.get("key"))
    except ValueError as exc:
        raise RequestError(str(exc)) from None
    try:
        season = int(data["season"])
    except (KeyError, TypeError, ValueError):
        raise RequestError("Missing or invalid season.") from None
    return key, season


async def _distrakt_user_id(request: Request) -> int:
    """The signed-in user whose tracker this request is for. Every distrakt route
    is gated DISTRAKT_APPROVED, so a user is always present by the time a handler
    runs; current_user is cached on the request by the dependency that gated it."""
    user = await auth.current_user(request)
    return user.user_id


async def _distrakt_settings(user_id: int):
    """The app-wide settings with EVERY source's credential swapped for
    `user_id`'s own.

    The tracker reads one person's private watch history — their progress, their
    plays, their movies — so every call it makes has to authenticate as THEM. The
    tokens in settings.json belong to the operator and would hand every user the
    operator's viewing instead of their own. Everything else on the object (the
    network emoji map, the TMDB key, the genre/country strings) is genuinely
    app-wide and is carried through untouched.

    BOTH TOKENS RIDE ON THE ONE SETTINGS OBJECT rather than each source being
    handed a credential of its own. Each port already reads its own field off
    `settings` — that is what `is_configured` asks about — so a per-source
    credential object would be a second mechanism for something this one already
    expresses, and the SyncPort protocol would have to grow an argument for it.
    A user with no Simkl identity gets an empty Simkl token, `simkl_configured`
    goes false, and that port is simply never asked; the same has always been
    true of Trakt.

    The refresh token is cleared as well: nothing downstream refreshes, and
    leaving the operator's beside somebody else's access token would be a pairing
    that means nothing. (There is no Simkl counterpart to clear — Simkl issues no
    refresh token.) The access level guarantees SOME linked identity that can
    read a history, but a row can still hold an empty token, in which case that
    source's `*_configured` goes false and the handlers take their existing "not
    configured" path.

    The network->emoji map is per-user too, but it is NOT on this object: it was
    removed from Settings entirely when it stopped being app-wide, so the
    renderers take it as an argument (see _distrakt_month_payload). Keeping it off
    `settings` is deliberate — there is no longer an app-wide value for a caller
    to reach for by mistake.
    """
    trakt_token, simkl_token = await asyncio.gather(
        trakt_routes.access_token_for_user(user_id),
        simkl_routes.access_token_for_user(user_id),
    )
    return dataclasses.replace(
        load_settings(), trakt_access_token=trakt_token or "", trakt_refresh_token="",
        simkl_access_token=simkl_token or "",
    )


async def _distrakt_post_link(user_id: int, settings, year: int, month: int) -> str | None:
    """The public calendar link this user's announcement post embeds, or None when
    they have nothing publishable — no configured public base URL, or every link
    form switched off. Omitted rather than rendered empty in that case.

    Pinned to the month the post announces, so a post read in September still
    opens on the August it is about."""
    row = await share_links.get_or_create(user_id)
    user = await auth.get_user(user_id)
    return share_links.post_link_with_view(
        row, user["username"] if user else None, settings.public_base_url,
        year=year, month=month,
    )


def _chooser_months(year: int, tracked: set[str]) -> list[dict]:
    """The twelve tiles the chooser draws for `year`: which month each one is, and
    whether it is one the user already has.

    EVERY month is offered, in every year. Opening one costs nothing on its own —
    a month behind the calendar that was never tracked renders empty and
    read-only, and one ahead renders empty with the ⤓ Import control that is the
    only thing that builds it — so there is no month the grid can advertise and
    the month view then refuse to show. The tiles used to be bounded by how far
    ahead the tracker would build, which meant a month the user could legitimately
    ask for was drawn as unavailable.
    """
    return [
        {
            "num": month,
            "name": _calendar.month_name[month],
            "tracked": distrakt_store.month_key(year, month) in tracked,
        }
        for month in range(1, 13)
    ]


@guard.get("/distrakt", AuthLevel.DISTRAKT_APPROVED)
async def distrakt(request: Request):
    """Hidden Discord-tracker page, reached through an easter egg rather than any
    link in the UI.

    WITH NO MONTH IN THE QUERY THIS IS A CHOOSER, and that is the whole point of
    it. Opening a month is expensive — it can pull a month of premieres, sweep
    recent viewing and write a roster's worth of rows — and the page used to
    inherit its month from whichever view the easter egg was triggered on, so
    which month paid that cost was an accident of where somebody happened to be.
    Naming a month is now a deliberate act, exactly as it is on the calendar's own
    landing page (see calendar.routes.home, whose picker shape this follows).
    Nothing here loads or builds a month: the chooser reads which months this user
    already has and renders a grid.

    WITH a month it renders the shell for the requested {year, month}; the page's
    JS then fetches the computed month via /api/distrakt/month (which lazily rolls
    the month over). Month-nav prev/next mirror the main calendar's nav."""
    today = clock.today()
    user = await auth.current_user(request)
    year = route_params.valid_year(request.query_params.get("year"), today.year)
    if not route_params.month_given(request.query_params.get("month")):
        tracked = set(await distrakt_store.list_months(user.user_id))
        return templates.TemplateResponse(request, "distrakt_pick.html", {
            "request": request,
            "year": year,
            "months": _chooser_months(year, tracked),
            "current_month": today.month if year == today.year else None,
            "today_month": today.month,
            "today_year": today.year,
            **chrome.page_context(user),
        })
    month = route_params.valid_month(request.query_params.get("month"), today.month)
    network_emojis, default_network_emoji = await distrakt_store.get_emoji_prefs(user.user_id)
    context = {
        "request": request,
        "year": year,
        "month": month,
        "nav": route_params.adjacent_months(year, month),
        # For the announcement post's "which calendar view does the embedded link
        # open on" selector; the same list the calendar's endpoint picker uses.
        "endpoints": endpoint_choices(),
        **chrome.page_context(user),
        # This user's OWN map — it renders into their Discord posts and nobody
        # else's. Rendered in rather than fetched because the roster rows fall
        # back to these emoji whenever a network has no logo.
        "network_emojis": network_emojis,
        "default_network_emoji": default_network_emoji,
    }
    return templates.TemplateResponse(request, "distrakt.html", context)


@guard.get("/api/distrakt/list", AuthLevel.DISTRAKT_APPROVED)
async def api_distrakt_list(request: Request):
    """Raw (unbucketed) shows stored for a month — the plain management list."""
    user_id = await _distrakt_user_id(request)
    today = clock.today()
    year = route_params.valid_year(request.query_params.get("year"), today.year)
    month = route_params.valid_month(request.query_params.get("month"), today.month)
    doc = await distrakt_store.load_month(user_id, distrakt_store.month_key(year, month))
    return JSONResponse({"ok": True, "month": distrakt_store.month_key(year, month), "shows": (doc or {}).get("shows", [])})


def _empty_month_payload(month_key: str, emojis: dict, default_emoji: str,
                         standing: distrakt_store.MonthStanding,
                         readonly: bool = False, link_url: str | None = None,
                         empty_note: str = "") -> dict:
    """Headers-only render for a month with no roster + no Trakt call: an
    unconfigured/uninitialized month (readonly=False) or a never-tracked past
    month reached by navigating backward (readonly=True). The tracker only
    ever rolls a month's snapshot forward, never backfills one after the fact,
    so an old month nobody was tracking at the time stays permanently empty
    and read-only rather than retroactively populating from Trakt.

    `empty_note` replaces the page's generic "nothing here yet" line. An empty
    month is not self-explanatory — the same blank list means "waiting for you to
    ask", "the calendar isn't there yet" and "nobody was tracking that far back" —
    so when the reason is known it is said, in the sentence the state owns."""
    return {
        "ok": True, "month": month_key, "closed": False, "readonly": readonly, "shows": [],
        "movies": [],
        "empty_note": empty_note,
        "post1": discord_fmt.render_post1([], [], emojis, default_emoji, link_url=link_url),
        "post2": discord_fmt.render_post2([], emojis, default_emoji, standing=standing),
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


def _rows_for(shape: lifecycle.MonthShape,
              standing: distrakt_store.MonthStanding) -> list[dict]:
    """The rows the page's list draws from, in this month's standing.

    THREE COLLECTIONS, JOINED HERE AND NOWHERE EARLIER. What began in the month,
    what the month settled, and what the viewer has in hand are separate all the
    way through the layer below this one, because the first notice answers only
    the first of those questions and joining them earlier is how one filtered list
    came to be shared between readers who wanted different things from it.

    THIS IS THE PAGE'S OWN LIST, NOT A SHARED ONE. The second notice reads from
    it too (routes.py hands it the very same rows), but it decides what to keep
    from them through its own half of discord_fmt.READER_BUCKETS rather than
    through the filtering this function already did — the page and the second
    notice legitimately disagree about premieres, and the one declaration in
    READER_BUCKETS is what lets them without a second table restating it.

    A PREMIERE THAT HAS BEGUN AIRING IS NOT LISTED HERE. It is already on the
    viewer's list, and showing both copies would put the same season on the page
    twice under two different headings. Its premiere record is untouched and still
    carries the month's announcement — that is what the first notice reads.

    NOR IS ONE THE MONTH HAS ALREADY SETTLED. A season that premiered and was
    settled in the same month genuinely holds TWO records on it — the
    announcement and the verdict — and that is the shape working as intended
    rather than a duplicate. But they are two statements about the same season
    made at different times, and the verdict is the later one: a title turned away
    before it ever aired was showing up on the page both as something still to
    come and as something given up on. Only the page is filtered this way. The
    first notice announces what began in the month whatever became of it since,
    which is a different question and is answered from the premiere records
    directly.

    Which buckets the page may present at all is discord_fmt.READER_BUCKETS,
    read rather than restated: a month that is over settled its verdicts and has
    no work in hand to report, and one that has not begun has nothing but its
    premieres.
    """
    allowed = discord_fmt.READER_BUCKETS[standing].page
    decided = {(s["key"], int(s["season"])) for s in shape.settled}
    unaired = [s for s in shape.premieres()
               if not s.get("started_airing")
               and (s["key"], int(s["season"])) not in decided]
    rows = []
    for show in (*unaired, *shape.listed, *shape.settled):
        if show["bucket"] not in allowed:
            continue
        # The stored flag under the name the browser draws the marker from, and
        # the counts string the row draws — which a live show already carries and
        # a row read straight out of storage does not. Built here rather than in
        # the browser because a frozen month keeps what each service said and has
        # to be able to render that disagreement years later, from the same rule
        # that wrote it (app/distrakt/counts.py).
        rows.append({**show, "returned": bool(show.get("came_back")),
                     "counts": show.get("counts") or counts.counts_label(
                         show.get("watched_by_source") or show.get("watched"),
                         show.get("total"), live.source_labels(), live.source_order())})
    return rows


async def _stale_month_payload(user_id: int, month_key: str, emojis: dict, default_emoji: str,
                               link_url: str | None, rate_limited: bool,
                               standing: distrakt_store.MonthStanding) -> dict:
    """Render a month WITHOUT any Trakt call, from whatever is last persisted — the
    top-level fallback when a shared refresh prerequisite hit Trakt's rate limit or
    was unreachable. Stored records already carry each show's last-known
    watched/total/cadence/dates, so this projects them offline (frozen_shows),
    attaching a visible notice so stale-but-real beats a false 0/0 or a 500.
    `rate_limited` only chooses the notice wording; both cases degrade identically
    and return HTTP 200.

    The viewer's own list is left out on purpose: reading it is cheap, but every
    row on it would need the season lookup that has just failed, so it could only
    be rendered from counts nothing has refreshed."""
    doc = await distrakt_store.load_month(user_id, month_key)
    shape = lifecycle.shape_of(distrakt_store.frozen_shows(doc) if doc else [])
    shows = _rows_for(shape, standing)
    notice = (
        "Trakt is rate-limiting us right now — showing last-known totals. Refresh again in a moment."
        if rate_limited else
        "Couldn't reach Trakt just now — showing last-known totals. Try refreshing again shortly."
    )
    return {
        "ok": True,
        "month": month_key,
        "closed": bool(doc and doc.get("closed")),
        "readonly": False,
        "shows": shows,
        # The films watched that month travel WITH the month, not just inside the
        # POST 2 text: they were being recorded, counted and imported while never
        # appearing anywhere on the page, which reads as them not being there.
        "movies": (doc or {}).get("movies") or [],
        # The first notice gets the month's PREMIERE records and nothing else: it
        # announces what began in the month, which is true of a month in any
        # standing and whatever each of those titles has since become.
        "post1": discord_fmt.render_post1(shape.series_premieres, shape.season_premieres,
                                          emojis, default_emoji, link_url=link_url),
        "post2": discord_fmt.render_post2(shows, emojis, default_emoji,
                                          movies=(doc or {}).get("movies"), standing=standing),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        # Reports the ACTUAL cause: True only when Trakt rate-limited us, False when
        # it was simply unreachable. The client shows the banner off `notice`
        # (present only on this degraded payload), not off this flag, so both cases
        # still surface — this stays accurate metadata rather than a banner trigger.
        "rate_limited": rate_limited,
        "notice": notice,
    }


def _closed_month_payload(doc: dict, month_key: str, emojis: dict, default_emoji: str,
                          link_url: str | None,
                          standing: distrakt_store.MonthStanding) -> dict:
    """A frozen past month, rendered straight from its own snapshot with NO Trakt
    calls. The snapshot is the record of what that month WAS — recomputing it
    against today's watch history would rewrite history every time it was opened."""
    shape = lifecycle.shape_of(distrakt_store.frozen_shows(doc))
    shows = _rows_for(shape, standing)
    return {
        "ok": True, "month": month_key, "closed": True, "readonly": False, "shows": shows,
        "movies": doc.get("movies") or [],
        # A closed month's announcement is still every premiere it had, whatever
        # each one has since become.
        "post1": discord_fmt.render_post1(shape.series_premieres, shape.season_premieres,
                                          emojis, default_emoji, link_url=link_url),
        "post2": discord_fmt.render_post2(shows, emojis, default_emoji,
                                          movies=doc.get("movies"), standing=standing),
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


async def _sync_watch_history(settings, user_id: int, records: list[dict],
                              month_key: str, force_fresh: bool,
                              today: date) -> tuple[dict, dict, list[dict], list, list[str]]:
    """Bring this user's incremental watch-history cache up to date ONCE, and take
    the four answers the month needs out of it: how much of each season they have
    watched, when each season was finished, which films they watched in the month,
    and which episode plays the pull actually reported.

    One sync for all four because they all read the same history: doing it per
    answer would re-baseline the whole history four times. `force_fresh` is a full
    re-baseline and belongs ONLY to an explicit Refresh — a routine load relies on
    the /sync/last_activities gate plus deltas.

    THE PLAYS ARE THE ONE ANSWER THAT IS USUALLY EMPTY, and that is what makes the
    reconciliation they drive free: when the activity beacon has not moved no
    history is fetched at all, so there is nothing to reconcile and nothing gets
    written on an ordinary load.
    """
    with span("payload.watch_history_sync", roster=len(records), force=force_fresh) as sp:
        state = await watch_history.sync_and_baseline(
            settings, user_id, records, force=force_fresh, today=today,
        )
        watched_lookup = watch_history.watched_map(state)
        # When each season was finished, for the "Completed means completed THIS
        # month" rule compute_live_shows applies.
        completed_lookup = watch_history.season_completed_map(state)
        mstart, mend = watch_history.month_bounds(month_key)
        movies = watch_history.movies_in_range(state, mstart, mend)
        plays = watch_history.episode_plays(state)
        # Which sources this pass could not read, so the page can say a number is
        # one service's alone rather than presenting it as what everybody agrees.
        unreadable = watch_history.unreadable_sources(state)
        sp.set(watched_keys=len(watched_lookup), movies=len(movies), plays=len(plays))
    return watched_lookup, completed_lookup, movies, plays, unreadable


def _season_lookup(settings) -> lifecycle.SeasonLookup:
    """How a reopening asks what a season looks like NOW, as a callable the
    lifecycle can use without knowing a provider exists.

    Handed in rather than reached for, because the transitions are otherwise
    derived from facts already in hand and this is the single exception — see
    lifecycle.SeasonLookup. It is paid once per season that reopened, never once
    per episode seen.

    A record with no source id, or a lookup that fails, answers {}: the season
    still comes back onto the viewer's list, on the counts the withdrawn verdict
    had, and the next load re-derives them anyway. Failing the whole month over a
    season that grew would be the worse trade.
    """
    async def look_up(record: dict, season: int) -> dict:
        source_id = (record.get("ids") or {}).get("trakt")
        if source_id is None:
            return {}
        try:
            return await trakt_detail.fetch_season_detail(settings, source_id, season)
        except TraktError:
            return {}
    return look_up


def _unknown_episode_rows(plays) -> list[dict]:
    """The plays that matched no record anywhere, in the flat shape the page
    reads: the title and the episode the history event named, and the identity to
    address them by. Nothing is looked up to build these — see
    lifecycle.reconcile_history for why that is the whole point of them.

    The service ids ride along because saying yes to one of these rows means
    LOOKING THE SEASON UP, and a lookup needs the id of the service being asked.
    The plays themselves live only as long as the request that produced them, so
    by the time the answer comes back there is nothing left here to read them
    off — the row has to carry them or the answer cannot be acted on without
    fetching a whole history again to find them a second time.
    """
    return [{"key": str(play.key), "season": play.season, "number": play.number,
             "title": play.title, "ids": dict(play.ids or {})} for play in plays]


async def _live_month_payload(user_id: int, doc: dict, month_key: str, settings,
                              emojis: dict, default_emoji: str, link_url: str | None,
                              force_fresh: bool, today: date) -> tuple[dict, int]:
    """Compute an OPEN month against Trakt: read what the month announced and what
    the viewer has in hand, let the season's life move on where the facts say it
    has, and render the page's rows and the two notices.

    TWO DIFFERENT KINDS OF FACT COME OUT OF THIS, and they are read from two
    different places because they are two different things. The MONTH's — what
    premiered in it, what it settled, what films were watched during it — are its
    own records. The VIEWER's — what they are keeping up with and what they are
    behind on — belong to no month at all and are their own list, which only the
    month actually in progress shows: a month that is over settled its verdicts,
    and one that has not begun has nothing in hand yet.

    Everything here may reach Trakt, and the caller is what turns a failure into
    the stale-but-real fallback — this function does not degrade, so that decision
    lives in exactly one place.
    """
    committed = distrakt_store.month_committed(month_key, today)
    standing = distrakt_store.month_standing(month_key, today)
    # A PREVIEW month (before the 1st) keeps auto-populating from premieres so it
    # tracks the calendar (and un-turning-away re-adds a previously excluded
    # premiere). A COMMITTED month is stable — premieres only re-import on demand.
    if not committed and settings.trakt_configured:
        await distrakt_store.import_premieres(user_id, month_key, settings)
        doc = await distrakt_store.load_month(user_id, month_key) or doc

    # BEFORE the records are read, because it changes them: a title turned away on
    # the main calendar becomes a verdict here, and one whose mark has been taken
    # back comes back. The calendar cannot call in — it does not know the tracker
    # exists — so the mirror is closed by reading the marks on the way past.
    # Reading the records first and reconciling afterwards would render the load
    # that made the change against the state before it.
    await lifecycle.reconcile_turn_aways(user_id, month_key, standing=standing)

    premieres = await distrakt_store.month_records(
        user_id, month_key, distrakt_store.PREMIERE_KINDS)
    under_way = standing is distrakt_store.MonthStanding.CURRENT
    listed = await distrakt_store.user_records(user_id) if under_way else []
    # One live pass over both, because they share every provider read: the
    # watch-history sync baselines against the whole set, and splitting them would
    # fetch each season twice and baseline the history twice for no gain. The
    # month's SETTLED records are deliberately not in it — a verdict carries the
    # counts it was reached on, and re-deriving them from today's history would let
    # a rewatch months later rewrite what a month recorded.
    everything = premieres + listed
    # WHETHER ANY SOURCE CAN BE ASKED AT ALL, rather than whether one named
    # service is configured. An account signed in with Simkl alone has a perfectly
    # readable history and used to be refused here for not having Trakt.
    ports = await watch_history.tracker_ports(settings, user_id)
    if everything and not ports:
        return {"ok": False, "error": "Not configured"}, 400

    # Two INDEPENDENT freshness knobs (they were wrongly coupled, which made every
    # stale load re-baseline the whole watch history):
    #   season_fresh -> bypass the 24h season cache for `y`. Only on explicit
    #                   Refresh; routine loads let the 24h TTL refresh `y` daily.
    #   force        -> full watch-history re-baseline. ONLY on explicit Refresh;
    #                   normal loads rely on the last_activities gate + deltas.
    season_fresh = force_fresh

    watched_lookup: dict = {}
    completed_lookup: dict = {}
    movies: list[dict] = []
    questions = lifecycle.HistoryQuestions([], [])
    unreadable: list[str] = []
    if ports:
        watched_lookup, completed_lookup, movies, plays, unreadable = await _sync_watch_history(
            settings, user_id, everything, month_key, force_fresh, today)
        if under_way and plays:
            # The history has moved, so what it reported is folded back into the
            # records: a season the viewer had finished and has now watched more
            # of comes back onto their list, and anything it could not place
            # anywhere is handed on to be asked about. Only on the month under
            # way — a season coming back is a statement about what the viewer has
            # in hand, and only that month has a list to put it on.
            questions = await lifecycle.reconcile_history(
                user_id, plays, month=month_key, look_up=_season_lookup(settings))
            # The viewer's list was read BEFORE the history was pulled, so a
            # season that has just come back onto it is not in it. Read again
            # rather than render this load against the state before its own
            # change — the row would otherwise be missing from the page entirely
            # until somebody loaded it a second time.
            listed = await distrakt_store.user_records(user_id)
            everything = premieres + listed

    with span("payload.compute_live_shows", n=len(everything), fresh=season_fresh):
        # allow_degrade: a per-show season 429 marks THAT show unavailable and
        # renders the rest, instead of failing every title at once.
        computed = await distrakt_store.compute_live_shows(
            user_id, everything, settings, fresh=season_fresh, watched_lookup=watched_lookup,
            allow_degrade=True, completed_lookup=completed_lookup,
            # The services this pass actually read, which is what tells a season
            # only one of them knows about from one they agree on.
            sources_read=[source for source, _port in ports]) if everything else []
    # compute_live_shows answers in the order it was asked, so the split is where
    # the two inputs were joined.
    live_premieres, live_listed = computed[:len(premieres)], computed[len(premieres):]

    # The transitions, all of them, off the facts just gathered and with no further
    # provider call — see lifecycle.advance. A month that is not the one under way
    # has no viewer list to move anything on or off, so it only has its premiere
    # records corrected.
    with span("payload.lifecycle", n=len(computed)):
        if under_way:
            shape = await lifecycle.advance(
                user_id, month_key, premieres=live_premieres, listed=live_listed,
                completed_on=completed_lookup)
        else:
            shape = await lifecycle.announce(user_id, month_key, live_premieres)

    shows = _rows_for(shape, standing)
    if premieres and season_fresh:
        await distrakt_store.stamp_refreshed(user_id, month_key)

    # Pre-warm the network-logo cache for everything on the page, so a show added
    # before logos existed doesn't depend on some OTHER show requesting its
    # network's logo first (see logos.ensure_logos). Best-effort and
    # self-limiting: a no-op once each network's tile is on disk.
    if ports and shows:
        with span("payload.ensure_logos"):
            await logos.ensure_logos(settings, [
                (s.get("network"), (s.get("ids") or {}).get("tmdb")) for s in shows
            ])

    with span("payload.render"):
        # The first notice takes the PREMIERE records alone. It announces what
        # began in the month and nothing else, in every standing, which is a
        # different question from the one the page and the second notice ask.
        post1 = discord_fmt.render_post1(shape.series_premieres, shape.season_premieres,
                                         emojis, default_emoji, link_url=link_url)
        post2 = discord_fmt.render_post2(shows, emojis, default_emoji, movies=movies,
                                         standing=standing)
    return {
        "ok": True,
        "month": month_key,
        "closed": False,
        "readonly": False,
        "shows": shows,
        "movies": movies,
        # The two things a history pull can turn up that only the viewer can
        # settle: something nothing here has heard of, and something they gave up
        # on and have gone back to. Both cost nothing to carry — they are what the
        # event already said — and both are only ever present on the load whose
        # history pull produced them.
        "unknown_episodes": _unknown_episode_rows(questions.unknown),
        "given_up_episodes": _unknown_episode_rows(questions.given_up),
        # The services that could not be read on this pass, under the names they
        # are shown by. Present so a season showing one number says WHY that is
        # all there is, instead of reading as two services agreeing.
        "sources_unreadable": [live.source_labels().get(name, name) for name in unreadable],
        "post1": post1,
        "post2": post2,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }, 200


async def _distrakt_month_payload(user_id: int, year: int, month: int, settings,
                                  force_fresh: bool = False) -> tuple[dict, int]:
    """Shared body for GET /api/distrakt/month + POST /api/distrakt/refresh, for
    ONE user's tracker.

    Lazily rolls the month over (ensure_month), then either renders a CLOSED
    month from its frozen snapshot (no Trakt) or computes the OPEN month live
    (or always when force_fresh). A never-tracked PAST/gap month (backward nav)
    is rendered empty + read-only and never created; a month that has not BEGUN is
    rendered empty and not created either, because building that one is something
    the user asks for with ⤓ Import — see the branch below. Returns
    (json_payload, http_status).

    This is also where Trakt failing is turned into an answer. A 429 on a SHARED
    prerequisite (anything but the per-show season fan-out, which degrades itself)
    can't be attributed to one show, so rather than a false 0/0 or a 500 the whole
    month falls back to its last-known stored totals plus a notice at HTTP 200 —
    the user refreshes again once the window clears. A plain reachability failure
    degrades the same way; only the notice wording differs.
    """
    today = clock.today()
    # BEFORE ANYTHING IS READ, and before ensure_month's freeze and turn-away
    # reconciliation in particular. The roster split leaves the rows of a month
    # that never froze held rather than guessed at (app/distrakt/unsettled.py), and
    # until they are settled the viewer's list is missing seasons and a month is
    # missing its announcements. Reconciling turn-aways against that half-built
    # list is what would read a held announcement as a season the viewer gave up
    # on. Costs one indexed lookup once the rows are drained, which is every call
    # but the first.
    await unsettled.settle(user_id, settings)
    month_key = distrakt_store.month_key(year, month)
    # Where this month stands relative to the calendar decides which sections it
    # may show at all (discord_fmt.READER_BUCKETS), so every shape below is
    # handed the same answer rather than working one out for itself.
    standing = distrakt_store.month_standing(month_key, today)
    link_url = await _distrakt_post_link(user_id, settings, year, month)
    # This user's own map, fetched once and handed to every render below. It is
    # not on `settings` any more — see _distrakt_settings.
    emojis, default_emoji = await distrakt_store.get_emoji_prefs(user_id)
    existing = await distrakt_store.load_month(user_id, month_key)
    if existing is None:
        blocked = await distrakt_store.is_backfill_blocked(user_id, month_key, today)
        if blocked or not settings.trakt_configured:
            # Backward/gap past month (blocked) OR no Trakt yet: empty, NOT
            # persisted, no Trakt call. `readonly` hides the add/edit affordances.
            return _empty_month_payload(
                month_key, emojis, default_emoji, standing,
                readonly=blocked, link_url=link_url,
            ), 200
        if standing is distrakt_store.MonthStanding.FUTURE:
            # A MONTH THAT HAS NOT BEGUN IS NOT BUILT BY BEING LOOKED AT. Building
            # one is the expensive act — a month of premieres read, a roster's
            # worth of rows written — and out here it is also a guess: nothing has
            # aired, so there is no state to catch up with. Opening it therefore
            # shows an empty month that says what it is waiting for, and ⤓ Import
            # is what builds it (api_distrakt_import). The month UNDER WAY is the
            # opposite case and still fills itself in on sight: it is the month
            # being asked about, and an empty one there would be wrong rather than
            # merely early. Not persisted and no Trakt call either way.
            return _empty_month_payload(
                month_key, emojis, default_emoji, standing, link_url=link_url,
                empty_note=MONTH_AWAITS_IMPORT,
            ), 200

    try:
        with span("payload.ensure_month", month=month_key, force=force_fresh):
            doc = await distrakt_store.ensure_month(user_id, year, month, settings, today=today)
        month_key = doc["month"]
        if doc.get("closed"):
            return _closed_month_payload(doc, month_key, emojis, default_emoji,
                                         link_url, standing), 200
        return await _live_month_payload(
            user_id, doc, month_key, settings, emojis, default_emoji, link_url,
            force_fresh, today)
    except TraktRateLimitError as exc:
        logger.warning("distrakt month %s degraded to stale (Trakt rate-limited): %s", month_key, exc)
        return await _stale_month_payload(user_id, month_key, emojis, default_emoji, link_url,
                                          rate_limited=True, standing=standing), 200
    except TraktError as exc:
        logger.warning("distrakt month %s degraded to stale (Trakt unreachable): %s", month_key, exc)
        return await _stale_month_payload(user_id, month_key, emojis, default_emoji, link_url,
                                          rate_limited=False, standing=standing), 200


@guard.get("/api/distrakt/month", AuthLevel.DISTRAKT_APPROVED)
async def api_distrakt_month(request: Request):
    """Computed buckets + the two copy-paste POST 1/POST 2 markdown blocks.

    OPEN month: live x/y + cadence/dates recomputed (1x /sync/watched/shows + 1x
    season call per show), auto-refreshed if totals are stale >24h. CLOSED /
    past month: rendered from the frozen snapshot with NO Trakt calls. Opening an
    uninitialized month lazily rolls it over first (see ensure_month)."""
    user_id = await _distrakt_user_id(request)
    today = clock.today()
    year = route_params.valid_year(request.query_params.get("year"), today.year)
    month = route_params.valid_month(request.query_params.get("month"), today.month)
    with span("GET /api/distrakt/month", ym=f"{year}-{month:02d}"):
        payload, status = await _distrakt_month_payload(user_id, year, month, await _distrakt_settings(user_id))
    return JSONResponse(payload, status_code=status)


@guard.post("/api/distrakt/refresh", AuthLevel.DISTRAKT_APPROVED)
async def api_distrakt_refresh(request: Request):
    """Force a fresh totals refresh: bypass the 24h season cache + re-stamp
    totals_refreshed_at for the OPEN month, then return the same shape as GET
    /api/distrakt/month. CLOSED months are frozen (nothing to refresh)."""
    user_id = await _distrakt_user_id(request)
    data = await authz.json_body(request)
    today = clock.today()
    year = route_params.valid_year(data.get("year"), today.year)
    month = route_params.valid_month(data.get("month"), today.month)
    payload, status = await _distrakt_month_payload(user_id, year, month, await _distrakt_settings(user_id),
                                                    force_fresh=True)
    return JSONResponse(payload, status_code=status)


@guard.get("/api/distrakt/months", AuthLevel.DISTRAKT_APPROVED)
async def api_distrakt_months(request: Request):
    """This user's tracked YYYY-MM months for the history nav, plus the real
    current month (always navigable even before it has been initialized)."""
    user_id = await _distrakt_user_id(request)
    today = clock.today()
    current = distrakt_store.month_key(today.year, today.month)
    months = sorted(set(await distrakt_store.list_months(user_id)) | {current})
    return JSONResponse({"ok": True, "months": months, "current": current})


@guard.post("/api/distrakt/import", AuthLevel.DISTRAKT_APPROVED)
async def api_distrakt_import(request: Request):
    """Pull this month's calendar premieres into the OPEN month (shows/new -> New,
    shows/premieres minus new -> Returning; skips existing + not-watching). The
    manual "Import from calendar" action — e.g. to seed the current month when its
    doc already exists (so lazy-init's one-shot premiere seeding was skipped).
    Returns the same shape as GET /api/distrakt/month.

    THIS IS ALSO HOW A MONTH THAT HAS NOT BEGUN GETS BUILT AT ALL. Opening one no
    longer builds it (see _distrakt_month_payload), so for the month ahead this
    button is the ask: it creates the month, which takes that month's premieres
    and nothing carried in from before it (see rollover._initialize_month).

    ANY month ahead, however far, and it does not matter whether the months
    between have been built: this is somebody pressing a button on the month they
    are looking at, which is the deliberate act a bound on how far ahead it may
    point was standing in for. What it gathers is only what this instance already
    holds for that month's calendar, so a month nothing is known about yet builds
    empty rather than expensively. A PAST month never tracked is the one refusal
    left — see rollover.can_initialize for why that one stays."""
    user_id = await _distrakt_user_id(request)
    settings = await _distrakt_settings(user_id)
    if not settings.trakt_configured:
        return JSONResponse({"ok": False, "error": "Not configured"}, status_code=400)
    data = await authz.json_body(request)
    today = clock.today()
    year = route_params.valid_year(data.get("year"), today.year)
    month = route_params.valid_month(data.get("month"), today.month)
    month_key = distrakt_store.month_key(year, month)
    if await distrakt_store.is_backfill_blocked(user_id, month_key, today):
        return JSONResponse({"ok": False, "error": "Can't import into a past month that was never tracked."}, status_code=400)
    doc = await distrakt_store.ensure_month(user_id, year, month, settings, today=today)
    if doc.get("closed"):
        return JSONResponse({"ok": False, "error": "Past month is frozen (read-only)."}, status_code=400)
    doc = await distrakt_store.import_premieres(user_id, month_key, settings)
    await _register_networks(user_id, [s.get("network") for s in (doc or {}).get("shows", [])])
    payload, status = await _distrakt_month_payload(user_id, year, month, settings)
    return JSONResponse(payload, status_code=status)


@guard.post("/api/distrakt/backfill-networks", AuthLevel.DISTRAKT_APPROVED)
async def api_distrakt_backfill_networks(request: Request):
    """Register every network used by this month's roster into the emoji map
    (with the default emoji) so they all show up in the editor. Returns the map."""
    user_id = await _distrakt_user_id(request)
    data = await authz.json_body(request)
    today = clock.today()
    year = route_params.valid_year(data.get("year"), today.year)
    month = route_params.valid_month(data.get("month"), today.month)
    doc = await distrakt_store.load_month(user_id, distrakt_store.month_key(year, month))
    emojis = await _register_networks(
        user_id, [s.get("network") for s in (doc or {}).get("shows", [])],
    )
    _, default_emoji = await distrakt_store.get_emoji_prefs(user_id)
    return JSONResponse({
        "ok": True,
        "network_emojis": emojis,
        "default_network_emoji": default_emoji,
    })


@guard.get("/api/distrakt/details", AuthLevel.DISTRAKT_APPROVED)
async def api_distrakt_details(request: Request):
    """The calendar's detail payload for one roster show, plus what THIS user has
    watched of that season.

    A separate route from /api/details rather than a parameter on it: that one is
    CALENDAR_APPROVED, and a distrakt-approved account need not be calendar
    approved. It also answers a different question — which episodes this
    particular person has seen — that the calendar has no business knowing.

    THE SOURCE ID AND THE SLUG BOTH COME FROM THE USER'S OWN ROSTER ROW, not from
    the query string: the caller names a row it can already see, and everything
    this looks up follows from that row. So the Trakt links it builds — and the
    title it fetches — cannot be pointed somewhere else by the caller.
    """
    user_id = await _distrakt_user_id(request)
    settings = await _distrakt_settings(user_id)
    if not settings.trakt_configured:
        return JSONResponse({"ok": False, "error": "Not configured"}, status_code=400)
    season = route_params.season(request.query_params.get("season"))
    try:
        key = parse_item_key(request.query_params.get("key"))
    except ValueError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    if season is None:
        return JSONResponse({"ok": False, "error": "Missing season"}, status_code=400)

    # Searched the way a reconciliation searches — the viewer's own list, then
    # this month's premieres, then back over what earlier months settled — so a
    # row the page can draw is a row this route can answer about, wherever the
    # tracker happens to be holding it. See lifecycle.find_season.
    today = clock.today()
    placed = await lifecycle.find_season(
        user_id, key, season, month=distrakt_store.month_key(today.year, today.month))
    ids = (placed.record.get("ids") or {}) if placed else {}
    if not ids.get("trakt"):
        return JSONResponse({"ok": False, "error": "Not on your roster"}, status_code=404)
    try:
        details = await trakt_detail.fetch_details(settings, Media.SHOW, ids["trakt"], season)
    except TraktError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=exc.status or 502)
    progress = await db.fetch_one(
        "SELECT watched_episodes_json FROM distrakt_show_progress WHERE user_id = ? "
        "AND media = ? AND match_source = ? AND match_id = ? AND season = ?",
        (user_id, key.media, key.match_source, key.match_id, season),
    )
    # watch_history owns what that column holds — {episode: watched_at} now, a
    # bare list of numbers before dates were stored — so the shape is read there
    # rather than guessed at again here. Guessing at it here is exactly how this
    # route came to answer "nothing watched" for everyone: it read the dated
    # mapping as a list and dropped every entry.
    watched: list[int] = []
    if progress is not None:
        try:
            stored = json.loads(progress["watched_episodes_json"] or "{}")
            watched = sorted(int(ep) for ep in watch_history.episode_watches(stored))
        except (TypeError, ValueError):
            watched = []
    return JSONResponse({
        "ok": True,
        **details,
        "slug": str(ids.get("slug") or ""),
        "season": season,
        "watched_episodes": watched,
    })


@guard.get("/api/distrakt/emojis", AuthLevel.DISTRAKT_APPROVED)
async def api_distrakt_emojis(request: Request):
    """This user's network->emoji map."""
    user_id = await _distrakt_user_id(request)
    emojis, default_emoji = await distrakt_store.get_emoji_prefs(user_id)
    return JSONResponse({
        "ok": True, "network_emojis": emojis, "default_network_emoji": default_emoji,
    })


@guard.post("/api/distrakt/emojis", AuthLevel.DISTRAKT_APPROVED)
async def api_distrakt_set_emojis(request: Request):
    """Replace this user's network->emoji map.

    DISTRAKT_APPROVED rather than ADMIN: the map is this account's own now, and
    it decides how this account's Discord posts render. It used to be saved
    through the admin-only settings endpoint, which is why only an administrator
    could edit what every user's posts looked like.
    """
    user_id = await _distrakt_user_id(request)
    data = await authz.json_body(request)
    emojis = data.get("network_emojis")
    if not isinstance(emojis, dict):
        return JSONResponse(
            {"ok": False, "error": "network_emojis must be an object"}, status_code=400,
        )
    default_emoji = str(data.get("default_network_emoji") or "").strip() or distrakt_store.DEFAULT_EMOJI
    clean = {
        str(k).strip(): str(v).strip()
        for k, v in emojis.items() if str(k).strip()
    }
    await distrakt_store.set_emoji_prefs(user_id, clean, default_emoji)
    return JSONResponse({
        "ok": True, "network_emojis": clean, "default_network_emoji": default_emoji,
    })


async def _tell_the_calendar(user_id: int, record: dict | None, *,
                             turned_away: bool) -> bool:
    """Mirror a tracker verdict onto the viewer's main-calendar turn-away marks.
    True when a mark was actually written or cleared.

    ONE ANSWER FOR BOTH CONTROLS that end a title's run — the ✕ and Abandon.
    They ask the identical question ("does this reach the calendar, and under which
    id?"), and a second copy of the rule beside the second control would drift out
    of step with the first in silence, because the two are only ever read one at a
    time and nothing ever puts them side by side.

    NEITHER A MONTH NOR A SEASON IS PART OF THE QUESTION, which is why neither is
    a parameter. A mark is a statement about the SHOW: giving up in September
    marks it turned away full stop, whatever month the record sits on and whatever
    month is on screen. The rule this replaces asked whether the title was one of
    the VIEWED month's premieres, so a show that premiered in July and was given
    up on in September wrote nothing at all.

    AND NOTHING IS ASKED ABOUT WHAT PUT THE ROW THERE. A mark on a show that never
    appears on the calendar is inert — it hides nothing and re-adds nothing — so
    there was never anything for a provenance guard to protect, and the guard cost
    the marks that mattered.

    THE ID IS THE CALENDAR'S OWN, not the one the tracker files the record under;
    see calendar_import.calendar_mark_id. A record naming neither a slug nor a
    source id cannot be pointed at a card at all, so nothing is written for it.
    """
    item_id = distrakt_store.calendar_mark_id(record or {})
    if not item_id:
        return False
    # Read first so the answer is honest: pressing ✕ on something already turned
    # away changes nothing, and the page's toast says so rather than claiming a
    # mark it did not make.
    if turned_away == (item_id in await calendar_state.not_watching_ids(user_id)):
        return False
    await calendar_state.set_not_watching(user_id, item_id, turned_away)
    return True


@guard.post("/api/distrakt/remove", AuthLevel.DISTRAKT_APPROVED)
async def api_distrakt_remove(request: Request):
    """Take a show+season off the tracker entirely — the ✕ on a row, and the only
    thing that ever removes a record.

    DELIBERATELY BLUNT, AND IT HAS TO BE. A season can hold a premiere record on
    one month, a verdict on another and a row on the viewer's own list all at once,
    so anything narrower leaves a copy behind and the row comes straight back on
    the next load with the ✕ looking broken.
    """
    user_id = await _distrakt_user_id(request)
    data = await authz.json_body(request)
    today = clock.today()
    year = route_params.valid_year(data.get("year"), today.year)
    month = route_params.valid_month(data.get("month"), today.month)
    try:
        key, season = _row_target(data)
    except RequestError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    month_key = distrakt_store.month_key(year, month)
    # Read before removing: the ids a calendar mark would be written under live on
    # the record that is about to go.
    record = await lifecycle.find_season(user_id, key, season, month=month_key)
    # The months that actually held a record of it. Read together with the search
    # above because neither alone is the whole answer: a season on the viewer's
    # list belongs to no month, and a season this month settled is on a month the
    # search deliberately steps over.
    cleared = await distrakt_store.remove_season_everywhere(user_id, key, season)
    if record is None and not cleared:
        return JSONResponse({"ok": False, "error": "Show/season not found in that month"},
                            status_code=404)

    settings = await _distrakt_settings(user_id)
    hidden = await _tell_the_calendar(
        user_id, record.record if record else None, turned_away=True)
    payload, status = await _distrakt_month_payload(user_id, year, month, settings)
    # So the toast can say what actually happened rather than guessing.
    if isinstance(payload, dict):
        payload["hidden_on_calendar"] = hidden
    return JSONResponse(payload, status_code=status)


@guard.get("/api/distrakt/search", AuthLevel.DISTRAKT_APPROVED)
async def api_distrakt_search(request: Request):
    settings = await _distrakt_settings(await _distrakt_user_id(request))
    if not settings.trakt_configured:
        return JSONResponse({"ok": False, "error": "Not configured"}, status_code=400)
    q = request.query_params.get("q", "")
    try:
        results = await trakt_detail.search_shows(settings, q)
    except TraktError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=exc.status or 502)
    return JSONResponse({"ok": True, "results": results})


@guard.get("/api/distrakt/search-movie", AuthLevel.DISTRAKT_APPROVED)
async def api_distrakt_search_movie(request: Request):
    """Film search for the add-a-film flow. Its own route rather than a media
    flag on the show search, because what comes back is a different shape with
    no seasons in it and the caller does something else entirely with it."""
    settings = await _distrakt_settings(await _distrakt_user_id(request))
    if not settings.trakt_configured:
        return JSONResponse({"ok": False, "error": "Not configured"}, status_code=400)
    q = request.query_params.get("q", "")
    try:
        found = await trakt_detail.search_titles(settings, Media.MOVIE, q)
    except TraktError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=exc.status or 502)
    results = [
        {"ids": entry["ids"], "title": entry["title"],
         "year": entry["year"], "runtime": entry.get("runtime")}
        for entry in found if (entry.get("ids") or {}).get("trakt")
    ]
    return JSONResponse({"ok": True, "results": results})


@guard.post("/api/distrakt/add-movie", AuthLevel.DISTRAKT_APPROVED)
async def api_distrakt_add_movie(request: Request):
    """Record a film as watched on a day, by hand.

    Films are not a roster the way shows are — there is nothing to bucket, no
    progress to follow and no season to finish. One is simply a play that
    happened on a date, so this writes the same watch-history record the sweep
    writes and nothing else. The month it shows up in follows from the date,
    exactly as it does for a film Trakt reported itself.

    The date is required rather than defaulted to today: a film added while
    looking at March is a film watched in March, and quietly stamping it with
    today's date would file it under a month the user is not even looking at.
    """
    user_id = await _distrakt_user_id(request)
    data = await authz.json_body(request)
    ids = _client_ids(data.get("ids"))
    if not ids:
        return JSONResponse({"ok": False, "error": "Missing or invalid film ids"}, status_code=400)
    day = str(data.get("watched_on") or "").strip()
    try:
        watched_on = date.fromisoformat(day)
    except ValueError:
        return JSONResponse({"ok": False, "error": "Pick the day you watched it."}, status_code=400)
    if watched_on > clock.today():
        return JSONResponse({"ok": False, "error": "That day hasn't happened yet."}, status_code=400)

    recorded = await watch_history.record_movie_watches(user_id, [{
        "ids": ids,
        "title": data.get("title") or "",
        "year": data.get("year"),
        "watched_at": f"{watched_on.isoformat()}T12:00:00Z",
    }])
    if not recorded:
        # The film named no shared id, so there is no identity to file the play
        # under — see app/providers/base.py's MATCH_SOURCES.
        return JSONResponse(
            {"ok": False, "error": "That film has no id the tracker can file it under."},
            status_code=400)
    await _resnapshot_if_closed(user_id, distrakt_store.month_key(watched_on.year, watched_on.month))

    today = clock.today()
    year = route_params.valid_year(data.get("year_view"), today.year)
    month = route_params.valid_month(data.get("month_view"), today.month)
    payload, status = await _distrakt_month_payload(
        user_id, year, month, await _distrakt_settings(user_id))
    return JSONResponse(payload, status_code=status)


async def _resnapshot_if_closed(user_id: int, month_key: str) -> None:
    """Rebuild a CLOSED month's stored film list from watch history.

    A closed month renders its films from its own snapshot and is never
    recomputed, so a film added to or removed from that month has to be written
    into the snapshot or it simply will not appear there. An OPEN month recomputes
    its films on every load and needs nothing.
    """
    doc = await distrakt_store.load_month(user_id, month_key) if month_key else None
    if doc is None or not doc.get("closed"):
        return
    state = await watch_history.load_state(user_id)
    mstart, mend = watch_history.month_bounds(month_key)
    await distrakt_store.set_month_movies(
        user_id, month_key, watch_history.movies_in_range(state, mstart, mend))


@guard.post("/api/distrakt/remove-movie", AuthLevel.DISTRAKT_APPROVED)
async def api_distrakt_remove_movie(request: Request):
    """Forget a watched film — from a past month as readily as the current one.

    Trakt's history is not always right about what was watched, and a film it
    reports wrongly has nowhere else to be corrected: unlike a show there is no
    roster row to take off, only the watch record itself. So this removes that,
    and re-snapshots whichever CLOSED month had been carrying it.

    A film is held once per id with its latest play, so this forgets the watch
    rather than one month's share of it. It does not remember the decision: a
    later backfill sweep over the same range will see Trakt still reporting the
    film and offer it back — in a plan that has to be confirmed, not silently.
    """
    user_id = await _distrakt_user_id(request)
    data = await authz.json_body(request)
    try:
        key = parse_item_key(data.get("key"))
    except ValueError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)

    watched_at = await watch_history.forget_movie_watch(user_id, key)
    if watched_at is None:
        return JSONResponse({"ok": False, "error": "No such film on record."}, status_code=404)

    # The month it was filed under is the one whose snapshot has to be rebuilt —
    # not necessarily the month being looked at.
    await _resnapshot_if_closed(user_id, watched_at[:7])

    today = clock.today()
    year = route_params.valid_year(data.get("year"), today.year)
    month = route_params.valid_month(data.get("month"), today.month)
    payload, status = await _distrakt_month_payload(
        user_id, year, month, await _distrakt_settings(user_id))
    return JSONResponse(payload, status_code=status)


@guard.get("/api/distrakt/seasons", AuthLevel.DISTRAKT_APPROVED)
async def api_distrakt_seasons(request: Request):
    """Aired seasons for a show (add-flow season picker) — required so the
    browser can call fetch_show_seasons()."""
    settings = await _distrakt_settings(await _distrakt_user_id(request))
    if not settings.trakt_configured:
        return JSONResponse({"ok": False, "error": "Not configured"}, status_code=400)
    trakt_id = request.query_params.get("id")
    if not trakt_id:
        return JSONResponse({"ok": False, "error": "Missing id"}, status_code=400)
    try:
        seasons = await trakt_detail.fetch_show_seasons(settings, trakt_id)
    except TraktError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=exc.status or 502)
    return JSONResponse({"ok": True, "seasons": seasons})


async def _register_networks(user_id: int, networks) -> dict:
    """Auto-populate THIS user's network->emoji map: any network not yet mapped
    gets the default emoji as a placeholder so it appears in their editor, ready
    to customize. No-op for blank / already-mapped networks.

    Per-user because it used to write settings.json, so importing a roster on any
    tracker account registered its networks into the operator's map.
    """
    return await distrakt_store.register_networks(user_id, networks)


@guard.post("/api/distrakt/add", AuthLevel.DISTRAKT_APPROVED)
async def api_distrakt_add(request: Request):
    """Add a show+season by hand, baseline the viewer's watch history for it, and
    register its network in the emoji map.

    WHICH RECORD IT BECOMES FOLLOWS FROM WHETHER IT HAS AIRED, and the season
    lookup that answers that is made here rather than guessed at. A season that
    has not started is something that BEGINS in the month being looked at, so it
    becomes that month's premiere record and the month's first notice announces
    it. One that is already airing is not an announcement about any month — it is
    something the viewer has in hand — so it goes onto their own list, where it
    would have arrived by itself the moment it premiered.

    A lookup that fails puts it on the list anyway: the next load re-derives every
    listed season's standing from its air dates, so the wrong guess in that one
    direction corrects itself, while a premiere record filed for a season that
    began two months ago would announce it for ever.
    """
    user_id = await _distrakt_user_id(request)
    settings = await _distrakt_settings(user_id)
    if not settings.trakt_configured:
        return JSONResponse({"ok": False, "error": "Not configured"}, status_code=400)
    data = await authz.json_body(request)
    today = clock.today()
    year = route_params.valid_year(data.get("year"), today.year)
    month = route_params.valid_month(data.get("month"), today.month)
    try:
        show = {
            "media": Media.SHOW,
            "ids": _client_ids(data.get("ids")),
            "season": int(data["season"]),
            "title": data.get("title") or "",
            "network": data.get("network") or "",
            # Added by hand, so removing it later says nothing about the
            # calendar — see api_distrakt_remove.
            "added_by": distrakt_store.ADDED_BY_MANUAL,
        }
        # Resolved before anything is written, so a title with no shared id is
        # refused here rather than at the insert, where the answer would be a 500.
        key = distrakt_store.record_key(show)
    except distrakt_store.UnkeyableRecord as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    except (KeyError, TypeError, ValueError):
        return JSONResponse({"ok": False, "error": "Missing or invalid ids/season"}, status_code=400)
    month_key = distrakt_store.month_key(year, month)
    if await distrakt_store.is_backfill_blocked(user_id, month_key, today):
        # No backfill: refuse to create a never-tracked PAST month even via a
        # manual add, consistent with the read path's read-only rendering of such
        # months. api_distrakt_add_completed is the way to state one outright.
        return JSONResponse(
            {"ok": False, "error": "Can't add shows to a past month that was never tracked."},
            status_code=400,
        )
    try:
        detail = await trakt_detail.fetch_season_detail(settings, (show["ids"]).get("trakt"),
                                                        show["season"])
    except TraktError:
        detail = {}
    if detail and not detail.get("started_airing"):
        await distrakt_store.add_month_record(user_id, month_key, {
            **show,
            "kind": distrakt_store.premiere_kind(show["season"]),
            **{field: detail.get(field) for field in lifecycle.CATALOGUE_FIELDS},
        })
    else:
        await lifecycle.follow(user_id, {**show, **detail, "season": show["season"]})
    await _register_networks(user_id, [show["network"]])
    try:  # baseline the show's watch history now so its counts are correct immediately
        await watch_history.baseline_show(settings, user_id, show)
    # Deliberately broad: this is a nicety, not the add. Whatever went wrong —
    # Trakt refusing, a malformed history row, a database hiccup — the row is
    # already stored and the next month load re-baselines it, so failing the
    # user's add over it would be the worse outcome.
    except Exception:
        logger.warning("baseline_show failed for %s", key, exc_info=True)
    payload, status = await _distrakt_month_payload(user_id, year, month, settings)  # recomputed month (1d)
    return JSONResponse(payload, status_code=status)


@guard.post("/api/distrakt/add-completed", AuthLevel.DISTRAKT_APPROVED)
async def api_distrakt_add_completed(request: Request):
    """Record a show+season as FINISHED in a past month, whatever watch history says.

    The manual counterpart to the history backfill, for what a sweep cannot know:
    a season watched somewhere that never reached Trakt, a play logged against
    the wrong date, a show the history call simply did not return. It states
    outright that it is not consulting history — the caller is the authority
    here, which is why this is its own route rather than a flag on the ordinary
    add, whose whole job is to let the buckets work the answer out.

    Deliberately allowed to create a month that `can_initialize` would refuse.
    That guard exists to stop month-nav from silently inventing history; this is a
    person saying "January had this in it", which is the case it was never meant
    to cover. Past months only: the current month is the tracker's own to bucket.

    The episode total comes from Trakt's season detail, not from the caller — a
    frozen month's counts are never recomputed, so a wrong one is wrong forever
    and would reach the ranker import as a wrong episode count.
    """
    user_id = await _distrakt_user_id(request)
    settings = await _distrakt_settings(user_id)
    if not settings.trakt_configured:
        return JSONResponse({"ok": False, "error": "Not configured"}, status_code=400)
    data = await authz.json_body(request)
    today = clock.today()
    year = route_params.valid_year(data.get("year"), today.year)
    month = route_params.valid_month(data.get("month"), today.month)
    month_key = distrakt_store.month_key(year, month)
    if month_key >= distrakt_store.month_key(today.year, today.month):
        return JSONResponse(
            {"ok": False, "error": "Only a past month can be filled in by hand."},
            status_code=400)
    ids = _client_ids(data.get("ids"))
    try:
        season = int(data["season"])
    except (KeyError, TypeError, ValueError):
        return JSONResponse({"ok": False, "error": "Missing or invalid season"}, status_code=400)
    if not ids.get("trakt"):
        return JSONResponse({"ok": False, "error": "Missing or invalid ids"}, status_code=400)

    try:
        detail = await trakt_detail.fetch_season_detail(settings, ids["trakt"], season)
    except TraktError as exc:
        return JSONResponse({"ok": False, "error": f"Trakt could not be read: {exc}"}, status_code=502)
    total = int((detail or {}).get("total") or 0)
    if not total:
        return JSONResponse(
            {"ok": False, "error": "Trakt lists no episodes for that season, so it cannot be recorded as finished."},
            status_code=400)

    try:
        await distrakt_store.add_month_record(user_id, month_key, {
            "media": Media.SHOW,
            "ids": ids,
            "title": data.get("title") or "",
            "season": season,
            "network": data.get("network") or "",
            # Stated outright, which is the whole point of this route: the caller
            # is the authority on what that month held, and nothing here is going
            # to work the answer out from counts.
            "kind": distrakt_store.RecordKind.COMPLETED,
            "watched": total,
            "total": total,
            "cadence": (detail or {}).get("cadence"),
            "premiere": (detail or {}).get("premiere"),
            "finale": (detail or {}).get("finale"),
            "started_airing": True,
            "finished_airing": True,
            "added_by": distrakt_store.ADDED_BY_MANUAL,
        })
    except distrakt_store.UnkeyableRecord as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    # Writing the record leaves an existing month's `closed` alone and creates a
    # new one open; either way a hand-filled past month ends up closed, like every
    # other past month, so it renders and imports with no further Trakt calls.
    doc = await distrakt_store.load_month(user_id, month_key)
    if doc is not None and not doc.get("closed"):
        doc["closed"] = True
        doc["totals_refreshed_at"] = db.now()
        await distrakt_store.save_month(user_id, doc)
    await _register_networks(user_id, [data.get("network") or ""])
    payload, status = await _distrakt_month_payload(user_id, year, month, settings)
    return JSONResponse(payload, status_code=status)


@guard.get("/api/distrakt/backfill", AuthLevel.DISTRAKT_APPROVED)
async def api_distrakt_backfill_range(request: Request):
    """The range the backfill dialog opens on, and what is already tracked."""
    user_id = await _distrakt_user_id(request)
    tracked = await distrakt_store.list_months(user_id)
    start, end = backfill.default_range()
    return JSONResponse({"ok": True, "start": start, "end": end, "tracked": tracked})


def _requested_month(value, fallback: str) -> str | None:
    """A "YYYY-MM" out of the backfill form: `fallback` when nothing was sent,
    the value when it is a real month key, and None when it is neither.

    ABSENT AND MALFORMED ARE DIFFERENT ANSWERS, and collapsing them is the bug
    this exists to stop. Substituting the default for a value somebody actually
    typed means a request for two months quietly runs as eight — the user sees a
    plausible result and no sign that what they asked for was discarded. Nothing
    typed at all is the only case a default belongs in.
    """
    if value is None or not str(value).strip():
        return fallback
    return backfill.valid_month(value)


@guard.post("/api/distrakt/backfill/survey", AuthLevel.DISTRAKT_APPROVED)
async def api_distrakt_backfill_survey(request: Request):
    """Work out what a backfill of {start, end} would write, and write nothing.

    The expensive half: one history sweep plus a lookup per show and per season.
    The plan it produces is kept server-side for /apply, so the summary the user
    confirms is the same set of records that gets written, and the client never
    hands records back to be stored.
    """
    user_id = await _distrakt_user_id(request)
    settings = await _distrakt_settings(user_id)
    if not settings.trakt_configured:
        return JSONResponse({"ok": False, "error": "Not configured"}, status_code=400)
    data = await authz.json_body(request)
    default_start, default_end = backfill.default_range()
    start = _requested_month(data.get("start"), default_start)
    end = _requested_month(data.get("end"), default_end)
    if start is None or end is None:
        return JSONResponse(
            {"ok": False, "error": "Months go in as YYYY-MM, with the month padded — 2026-07, not 2026-7."},
            status_code=400)
    if not backfill.month_range(start, end):
        return JSONResponse(
            {"ok": False, "error": "That range covers no months — check the order."},
            status_code=400)
    try:
        plan = await backfill.survey(user_id, settings, start, end)
    except TraktError as exc:
        return JSONResponse({"ok": False, "error": f"Trakt could not be read: {exc}"}, status_code=502)
    return JSONResponse({"ok": True, **backfill.summarize(plan)})


@guard.post("/api/distrakt/backfill/apply", AuthLevel.DISTRAKT_APPROVED)
async def api_distrakt_backfill_apply(request: Request):
    """Write the surveyed plan. Refuses if there isn't one — the survey is the
    only thing that can produce records, and it expires."""
    user_id = await _distrakt_user_id(request)
    try:
        written = await backfill.apply(user_id)
    except backfill.BackfillExpired as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=409)
    months = await distrakt_store.list_months(user_id)
    return JSONResponse({"ok": True, **written, "tracked": months})


@guard.post("/api/distrakt/abandon", AuthLevel.DISTRAKT_APPROVED)
async def api_distrakt_abandon(request: Request):
    """Give up on a show+season, or take that back.

    GIVING UP MOVES THE SEASON OFF THE VIEWER'S LIST AND ONTO A MONTH, and the
    month is the one the CLOCK is standing in — not the one being looked at.
    Giving up is an act, and it happens today: somebody browsing March who decides
    they are done with something decided that in September, and recording it
    against March would put a verdict into a month that had already settled.

    TAKING IT BACK MIGRATES THE RECORD THE OTHER WAY, and the month row does not
    survive it. A month records what it settled; this one no longer settled
    anything, so leaving the row behind would have the month go on announcing a
    verdict just withdrawn.

    `abandoned_form` is frozen at the moment of giving up — the inline form the
    notice renders, minus the premiere/finale dates — so that line stays stable
    afterwards however the season's counts move. It falls back to None when there
    is nothing to freeze from, and the renderer then rebuilds a form from the
    stored record.
    """
    user_id = await _distrakt_user_id(request)
    data = await authz.json_body(request)
    today = clock.today()
    year = route_params.valid_year(data.get("year"), today.year)
    month = route_params.valid_month(data.get("month"), today.month)
    try:
        key, season = _row_target(data)
    except RequestError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    abandoned = bool(data.get("abandoned"))
    viewed_month = distrakt_store.month_key(year, month)
    # An abandon is dated by the clock; see the docstring. The viewed month is
    # still what the response re-renders, and it is what an un-abandon addresses,
    # because that one has to find the record it is undoing.
    acted_month = distrakt_store.month_key(today.year, today.month)

    settings = await _distrakt_settings(user_id)
    if abandoned:
        placed = await lifecycle.find_season(user_id, key, season, month=viewed_month)
        if placed is None:
            return JSONResponse({"ok": False, "error": "Show/season not found in that month"},
                                status_code=404)
        show = await _live_form_source(settings, placed.record, season)
        rec = await lifecycle.give_up(
            user_id, show, month=acted_month,
            abandoned_form=discord_fmt.freeze_form(show))
    else:
        # Undone on whichever of the two months holds the record: the one it was
        # given up on in, which is normally today's, but a verdict reached in a
        # month now behind us is still the one being withdrawn.
        rec = await lifecycle.take_back(user_id, key, season, month=acted_month)
        if rec is None and viewed_month != acted_month:
            rec = await lifecycle.take_back(user_id, key, season, month=viewed_month)
        if rec is None:
            return JSONResponse({"ok": False, "error": "Show/season not found in that month"},
                                status_code=404)

    await _tell_the_calendar(user_id, rec, turned_away=abandoned)
    payload, status = await _distrakt_month_payload(user_id, year, month, settings)
    return JSONResponse(payload, status_code=status)


@guard.post("/api/distrakt/acknowledge-return", AuthLevel.DISTRAKT_APPROVED)
async def api_distrakt_acknowledge_return(request: Request):
    """Clear the marker on a season that had been finished and grew.

    THE VIEWER DISMISSES IT AND NOTHING ELSE DOES — not time, not the next load.
    The marker exists to say something they would otherwise have to notice on
    their own (a title they remember finishing, sitting back on the list), so
    anything that cleared it without being read would defeat the point of it. That
    is why the flag is stored and why clearing it is a request of its own.

    IT ANSWERS WITH NOTHING BUT THE ACKNOWLEDGEMENT, unlike the row's other
    controls. The ✕ and Abandon change what the month HOLDS, so they hand the
    recomputed month back and the page redraws from it; this changes one flag on
    one row and recomputing a month over it would cost a season lookup per listed
    title to redraw markup the browser can amend in place.
    """
    user_id = await _distrakt_user_id(request)
    data = await authz.json_body(request)
    try:
        key, season = _row_target(data)
    except RequestError as exc:
        return authz.error(str(exc))
    if not await distrakt_store.set_came_back(user_id, key, season, False):
        return authz.error("That season isn't on your list.", 404)
    return JSONResponse({"ok": True})


@guard.post("/api/distrakt/unknown-add", AuthLevel.DISTRAKT_APPROVED)
async def api_distrakt_unknown_add(request: Request):
    """Say yes to an episode nothing knew about: look the season up NOW, and put
    it on the viewer's list under whichever kind its air dates call for.

    THIS IS WHERE THE COST IS PAID, and it is the only place it may be. The row
    that offered this was rendered from what the history event already said, with
    nothing fetched, precisely so that a viewing life's worth of unmatched
    episodes costs nothing to be asked about. One lookup happens here, for one
    season, because somebody pressed a button.

    THE IDS COME FROM THE ROW THE PAGE WAS SHOWING, narrowed at the boundary the
    same way the ordinary add narrows them. The plays that produced the row did
    not outlive the request that reported them, so there is nothing left on this
    side to read them back off; and a caller naming a different show here could
    equally have named it through the ordinary add, so nothing is reachable this
    way that was not already.
    """
    user_id = await _distrakt_user_id(request)
    settings = await _distrakt_settings(user_id)
    if not settings.trakt_configured:
        return authz.error("Not configured")
    data = await authz.json_body(request)
    try:
        key, season = _row_target(data)
    except RequestError as exc:
        return authz.error(str(exc))
    record = {
        "media": key.media, "match_source": key.match_source, "match_id": key.match_id,
        "season": season,
        "ids": _client_ids(data.get("ids")),
        "title": str(data.get("title") or ""),
        "network": str(data.get("network") or ""),
        # Nothing announced this and no month built it in: the viewer's own
        # viewing is what put it here, which is what this records.
        "added_by": distrakt_store.ADDED_BY_HISTORY,
    }
    detail = await _season_lookup(settings)(record, season)
    await lifecycle.follow(user_id, {**record, **detail, "season": season})
    today = clock.today()
    year = route_params.valid_year(data.get("year"), today.year)
    month = route_params.valid_month(data.get("month"), today.month)
    payload, status = await _distrakt_month_payload(user_id, year, month, settings)
    return JSONResponse(payload, status_code=status)


@guard.post("/api/distrakt/unknown-resume", AuthLevel.DISTRAKT_APPROVED)
async def api_distrakt_unknown_resume(request: Request):
    """Take back a season the viewer gave up on and has gone back to.

    NOT THE SAME ACT AS ADDING SOMETHING NEW, which is why it is not the same
    route. THE MOVE ITSELF ASKS NOTHING: the record already exists, with the
    counts and dates it was given up on, so it simply moves off the month that
    recorded the verdict and back onto the viewer's list. The month does not keep
    the row — a month records what it settled, and it no longer settled this.

    The RESPONSE is a recomputed month, and that will look the season up like any
    other row it draws, because a season nothing has asked about recently is not
    in the cache. That call belongs to rendering the month, not to taking the
    title back, and it is the same one any load of that month would make.

    THE CALENDAR MARK GOES WITH IT, and that is not a nicety. Giving up turns the
    title away on the main calendar, and that mark is still standing; left there,
    the next load's turn-away reconciliation would read it and give the season up
    all over again. The row would come back given-up with nothing to say why —
    the same failure the un-abandon control was fixed for.
    """
    user_id = await _distrakt_user_id(request)
    data = await authz.json_body(request)
    try:
        key, season = _row_target(data)
    except RequestError as exc:
        return authz.error(str(exc))
    today = clock.today()
    year = route_params.valid_year(data.get("year"), today.year)
    month = route_params.valid_month(data.get("month"), today.month)
    month_key = distrakt_store.month_key(year, month)
    placed = await lifecycle.find_season(user_id, key, season, month=month_key)
    if placed is None or placed.month is None or \
            placed.record["kind"] != distrakt_store.RecordKind.ABANDONED:
        return authz.error("That season isn't recorded as given up on.", 404)
    await lifecycle.take_back(user_id, key, season, month=placed.month)
    await _tell_the_calendar(user_id, placed.record, turned_away=False)
    settings = await _distrakt_settings(user_id)
    payload, status = await _distrakt_month_payload(user_id, year, month, settings)
    return JSONResponse(payload, status_code=status)


@guard.post("/api/distrakt/unknown-dismiss", AuthLevel.DISTRAKT_APPROVED)
async def api_distrakt_unknown_dismiss(request: Request):
    """Say no to an episode nothing knew about, and mean it next time too.

    RECORDED PER SEASON RATHER THAN PER EPISODE. The next episode of the same
    season is the same question, and a refusal that only covered the one play
    would ask again on the very next sitting.

    ONE REFUSAL FOR BOTH QUESTIONS the page can ask. "Nothing here knows about
    this" and "you gave this up and have gone back to it" are asked for different
    reasons, but the answer no means the same thing to both — do not put this on
    my list, stop asking — so it is written down once and read once.

    THE REFUSAL HAS TO BE STORED OR IT IS NOT A REFUSAL. The row is derived from
    the watch history every time the history is read, so with nothing written down
    it would come straight back on the next load and the ✗ would look broken —
    the same reason taking a calendar row off the tracker has to say so to the
    calendar.

    IT ANSWERS WITH THE ACKNOWLEDGEMENT ALONE. Nothing else on the page changed:
    no record was written, no bucket gained or lost a row, and recomputing a month
    to drop one line would cost a season lookup per listed title.
    """
    user_id = await _distrakt_user_id(request)
    data = await authz.json_body(request)
    try:
        key, season = _row_target(data)
    except RequestError as exc:
        return authz.error(str(exc))
    await distrakt_store.dismiss_prompt(user_id, key, season)
    return JSONResponse({"ok": True})


async def _live_form_source(settings, record: dict, season: int) -> dict:
    """`record` merged with what the provider says about it right now, for the
    abandoned form that is about to be frozen.

    Degrades to the stored record rather than failing. Giving up is a user ACTION
    and must still succeed — it is not a read to fail on — so an unreachable
    provider costs the line its live counts and nothing else.
    """
    source_id = (record.get("ids") or {}).get("trakt")
    if not (settings and getattr(settings, "trakt_configured", False)) or source_id is None:
        return dict(record)
    try:
        watched, detail = await asyncio.gather(
            trakt_sync.fetch_watched_map(settings, [source_id]),
            trakt_detail.fetch_season_detail(settings, source_id, season),
        )
    except TraktError:
        return dict(record)
    # fetch_watched_map answers in Trakt's own (id, season) terms, so this one
    # count is re-filed under the shared identity the record is keyed by.
    return {**record, "watched": watched.get((int(source_id), season), 0), **detail,
            "season": season}


@guard.get("/api/distrakt/export", AuthLevel.DISTRAKT_APPROVED)
async def api_distrakt_export(request: Request):
    """Download the REQUESTING user's complete distrakt dataset as one JSON
    document — every month, show row, watch state, per-season progress, and movie
    watch. Contains no tokens and no other user's data, and doubles as the input
    POST /api/distrakt/restore takes back."""
    user_id = await _distrakt_user_id(request)
    doc = await distrakt_store.export_user_data(user_id)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d")
    return JSONResponse(doc, headers={
        "Content-Disposition": f'attachment; filename="distrakt-export-{stamp}.json"',
    })


@guard.post("/api/distrakt/restore", AuthLevel.DISTRAKT_APPROVED)
async def api_distrakt_restore(request: Request):
    """Replace the requesting user's distrakt data with an exported document.

    REPLACE, not merge, in one transaction: a merge would need conflict rules for
    every field and has no clear use case. The restoring user comes from the
    session — any user_id in the file is ignored, so a document can never write
    into someone else's tracker. This is deliberately NOT the same thing as
    POST /api/distrakt/import, which pulls premieres in from the calendar."""
    user_id = await _distrakt_user_id(request)
    data = await authz.json_body(request)
    try:
        await distrakt_store.restore_user_data(user_id, data)
    except distrakt_store.RestoreError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    except db.DatabaseError as exc:
        logger.warning("distrakt restore failed for user %s", user_id, exc_info=True)
        return JSONResponse({"ok": False, "error": f"Could not restore this file: {exc}"}, status_code=400)
    months = await distrakt_store.list_months(user_id)
    return JSONResponse({"ok": True, "months": months})


async def _share_link_payload(user_id: int) -> dict:
    """What the announcement post's link controls need: the resolved URL plus
    which of the three forms are actually publishable right now, so the selector
    can offer only the ones that would produce a working link."""
    settings = load_settings()
    row = await share_links.get_or_create(user_id)
    user = await auth.get_user(user_id)
    username = user["username"] if user else None
    urls = share_links.share_urls(row, username, settings.public_base_url)
    return {
        "ok": True,
        "base_url_missing": not bool(settings.public_base_url),
        "url": share_links.post_link_with_view(row, username, settings.public_base_url),
        # None means "whatever the share panel prefers" rather than a fixed form,
        # which is a different state from having picked that same form outright.
        "kind": row["post_link_kind"],
        "preferred_kind": row["preferred_kind"],
        "endpoint": row["post_link_endpoint"],
        "available": {kind: bool(url) for kind, url in urls.items()},
    }


@guard.get("/api/distrakt/share-link", AuthLevel.DISTRAKT_APPROVED)
async def api_distrakt_share_link(request: Request):
    """This user's current announcement-post link settings."""
    return JSONResponse(await _share_link_payload(await _distrakt_user_id(request)))


@guard.post("/api/distrakt/share-link", AuthLevel.DISTRAKT_APPROVED)
async def api_distrakt_set_share_link(request: Request):
    """Choose which share-link form the announcement post embeds and which
    calendar view it opens on.

    Both fields are optional and each is only written when present, so the two
    controls save independently. An empty string clears the choice: the link form
    goes back to following the share panel's preferred kind, and the view goes
    back to whatever the owner's share defaults already resolve to.
    """
    user_id = await _distrakt_user_id(request)
    data = await authz.json_body(request)
    kind = data["kind"] or None if "kind" in data else ...
    endpoint = data["endpoint"] or None if "endpoint" in data else ...
    if kind is ... and endpoint is ...:
        return JSONResponse({"ok": False, "error": "Nothing to update"}, status_code=400)
    try:
        await share_links.set_post_link(user_id, kind=kind, endpoint=endpoint)
    except ValueError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    return JSONResponse(await _share_link_payload(user_id))
