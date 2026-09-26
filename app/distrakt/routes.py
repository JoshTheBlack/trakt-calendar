"""The hidden Discord tracker: its page shell, its rendered fragments, and the
whole /api/distrakt/* API.

Every route here is DISTRAKT_APPROVED, and every one of them reads ONE person's
private Trakt history — their progress, their plays, their films. That is why
`viewer.settings_for_user` exists and why nothing here uses the app-wide token:
the operator's token would hand every user the operator's viewing instead of
their own.

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
import json
import logging
from datetime import date, datetime, timedelta, timezone
from typing import NamedTuple
from urllib.parse import quote

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response

from . import (arrival, backfill, counts, discord_fmt, episode_progress, lifecycle, live,
               rollover, slug_repair, unsettled, viewer, watch_history)
# The catalogue-search merge (app/distrakt/search.py) — imported as `catalogue_search`
# rather than bare `search` so a call site reads which module it is, the same
# reason the six modules above are imported by name rather than star-imported.
from . import search as catalogue_search
# The data layer is reached through this package's own public surface, the same
# names an outside caller uses, rather than through the six modules those names
# are defined in: a route handler has no business knowing which half of the
# tracker `load_month` or `compute_live_shows` lives in.
from .. import distrakt as distrakt_store
from .. import auth, authz, chrome, clock, db, details_panel, providers, route_params
from ..auth import AuthLevel
from ..calendar import share_links
from ..config import load_settings
from ..endpoints import endpoint_choices
from ..media import logos
from ..perftrace import span
from ..providers import detail_source
from ..providers.base import (ItemKey, Media, Source, SourceUnavailable,
                              parse_item_key, parse_media, resolve_key)
from ..providers.trakt import TraktError
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
# What an empty month that has not begun says for itself. There is only ever one
# sentence for it now, at any distance ahead: every future month is waiting to be
# asked, and the ask is a button on the page saying this.
MONTH_AWAITS_IMPORT = ("This month hasn't begun yet, so nothing has been gathered for it. "
                       "Press ⤓ Import to build it from what premieres in it.")


class RequestError(ValueError):
    """A request body the caller has to fix. Its message is what the client is
    told, so it says what is wrong rather than naming a field type."""


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


def _viewed_month(request: Request) -> tuple[int, int]:
    """The year and month the page is looking at, off the request's own address.

    ONE READER FOR EVERY ROUTE, and it reads the QUERY STRING on a POST as well
    as on a GET. Which month a control acts on is a REFERENCE the server
    resolves — not data the browser hands over — so it rides in the address the
    way templates/_add_all.html puts the calendar's month there, and the body is
    left carrying nothing but the row's own identity. Out-of-range and missing
    both fall back to today, which is route_params' rule everywhere else.
    """
    today = clock.today()
    return (route_params.valid_year(request.query_params.get("year"), today.year),
            route_params.valid_month(request.query_params.get("month"), today.month))


def _this_month() -> str:
    """The month key the clock is standing in."""
    today = clock.today()
    return distrakt_store.month_key(today.year, today.month)


def _import_due(month_key: str, today: date, premieres: list[dict]) -> bool:
    """Whether the Import button asks to be pressed on `month_key`: the month
    under way or the one after it, with nothing taken from its calendar yet.

    THE MONTH AHEAD TOO, because that is when its premieres are worth a look: the
    calendar already lists them, and choosing ahead of the 1st is what somebody
    tidying their month does. Further out the calendar is still thin, and a
    button lit on every month ahead would stop meaning anything.

    One rule for both ways a month is drawn (built, or ahead and not built yet),
    since a month is no longer filled by opening it (rollover._initialize_month)
    and this is the only thing that says its premieres are waiting.
    """
    this_month = distrakt_store.month_key(today.year, today.month)
    if month_key != this_month and rollover.prev_month_key(month_key) != this_month:
        return False
    return not any(record.get("added_by") == distrakt_store.ADDED_BY_CALENDAR
                   for record in premieres)


async def _distrakt_user_id(request: Request) -> int:
    """The signed-in user whose tracker this request is for. Every distrakt route
    is gated DISTRAKT_APPROVED, so a user is always present by the time a handler
    runs; current_user is cached on the request by the dependency that gated it."""
    user = await auth.current_user(request)
    return user.user_id


def _point_at(payload: dict, key, season) -> dict:
    """Name the row a mutation just wrote, so the page can scroll to it and say
    which one it is.

    THE SERVER NAMES IT BECAUSE THE SERVER IS THE ONE THAT KNOWS. A caller posts
    ids and a season number; which roster row or month record that became is
    worked out here, from the title's own ids, and a page re-deriving it from the
    payload would be a second implementation of `resolve_key` written in
    JavaScript — wrong for exactly the titles the first one exists to get right.

    ADVISORY, AND THE PAGE TREATS IT THAT WAY. An add whose season settles
    somewhere the current month does not draw leaves nothing to point at, and a
    highlight that finds no row is simply not drawn. So this makes no promise
    that the row is on the page; it says which row it would be.
    """
    payload["highlight"] = {"key": str(key), "season": int(season)}
    return payload


async def _rewatch_question(user_id, settings, record, key, season: int, total,
                            data, *, title: str) -> tuple[dict | None, str]:
    """`(what to ask before writing anything, whether they answered "count
    everything" about a season the history says is finished)`.

    ONE IMPLEMENTATION BECAUSE THERE IS ONE QUESTION. A season reaches the roster
    two ways — somebody searches for it, or the history reports a play nothing
    could place and they say yes to the row — and "have you already finished
    this, and does the earlier viewing count" is the same question on both. It
    was asked on the first and not the second, which meant re-watching something
    the tracker had never heard of added it silently at its full episode count:
    the case the question exists for, arrived at by the other door.

    THE HISTORY IS FETCHED HERE AND NOT BY THE CALLER, AND THAT PLACEMENT IS THE
    WHOLE POINT OF THE ARGUMENT. This question is asked of STORED watch state, and
    a title entering the roster for the first time has none — so a caller that
    asks before baselining compares a finished season against a count of zero,
    concludes nothing is finished, and adds it in silence. That is not a
    hypothetical: it was fixed in the search route by moving the baseline above
    the question, and then reproduced EXACTLY in the untracked-episode route,
    which had never baselined at all. Twice is a design fault rather than an
    oversight. Fetching inside the question makes the two inseparable, so a third
    caller cannot get the order wrong because there is no order to get wrong.

    A FIRST ADD IS THE ONLY ADD THE QUESTION HAS. A season finished years ago is
    by definition not on the list, so every add of one is a first add; asking
    before fetching meant the question could not see the single case it exists
    for, and appeared to work whenever it was tested twice.

    THIS IS A WRITE BEFORE THE QUESTION AND IT IS ALLOWED. What the callers'
    "write nothing until it is answered" rule protects is the ROSTER ROW and the
    MONTH RECORD — the writes that put a season on the list and let the recompute
    settle it where the answer can no longer reach. Watch state is neither: it
    names no month, puts nothing on any list, and is re-derived from the services
    on any later pass.

    AND IT COSTS THE ADD NOTHING. Both callers baseline this title anyway, one a
    few lines further down and the other by way of the month recompute, so the
    same one call per source is made either way.

    THE ANSWER IS READ BACK FROM THE HISTORY, NEVER TAKEN FROM THE BODY. A floor
    in the request is the viewer saying "this pass starts here"; the absence of
    one is the other answer, and whether that answer MEANS anything is a fact
    about their plays. A request claiming a season is finished cannot make it so.

    `(None, "")` FOR ALMOST EVERY ADD, which is the case that must stay cheap: a
    season with no history at all fails the count test on its first comparison.
    """
    try:
        await watch_history.baseline_show(settings, user_id, record)
    # Deliberately broad: this is what the question is asked OF, not the add
    # itself. Whatever went wrong — a service refusing, a malformed history row,
    # a database hiccup — the caller can still proceed, and the question then
    # sees no history and does not fire, which is the answer that changes
    # nothing.
    except Exception:
        logger.warning("baseline_show failed for %s", key, exc_info=True)
    if not data.get("decided"):
        finished, offered, restart = await _completed_before_add(
            user_id, key, season, total, watch_history.viewer_tz(settings))
        if not finished:
            return None, ""
        return {
            "key": str(key), "season": season, "title": title,
            "completed_on": finished,
            # WHERE A FRESH RUN WOULD START, offered rather than imposed, and
            # worked out rather than assumed — see `_completed_before_add`. The
            # viewer can still move it, which is what keeps this an offer.
            "suggested_from": offered,
            # WHAT THE VIEWER NEEDS TO SEE TO ANSWER, when the play order shows
            # they have already begun again: the day the previous run ended, and
            # the episodes watched since. The question was a date and nothing
            # else, which meant answering it from memory; naming the episodes
            # lets it be answered by looking. Absent when nothing restarted, and
            # the page then asks the plainer version.
            "restart": restart,
        }, ""
    if _valid_day(data.get("history_from")):
        return None, ""   # a floor IS the other answer
    already, _offered, _restart = await _completed_before_add(
        user_id, key, season, total, watch_history.viewer_tz(settings))
    return None, already


async def _file_finished(user_id, month_key: str, record: dict, detail) -> dict:
    """Record a season as FINISHED on the month being looked at.

    THE VIEWER WAS ASKED AND ANSWERED THAT EVERYTHING THEY HAVE WATCHED COUNTS,
    about a season every episode of which has been seen — so it is done, and a
    done season belongs in Completed rather than sitting on the list at 18/18 for
    ever.

    THIS MONTH AND NOT THE MONTH THE HISTORY NAMES, which is the author's
    decision. `lifecycle.finish_if_done` settles onto the month the last play
    dates and REFUSES when that month was never tracked, for a good reason: it is
    reached from an ordinary read of some other month, and settling there would
    conjure an untracked month out of merely looking at one. That refusal is what
    left a season finished in 2023 stuck on the list. A MANUAL ADD IS NOT AN
    ORDINARY READ — somebody is standing on a month saying "this one, now" — so
    nothing is inferred and no month is invented. Filling in a past month
    deliberately is still api_distrakt_add_completed's job, and it still refuses
    to touch the current one.

    NO ROSTER ROW GOES WITH IT. A completed season is not on anybody's list, and
    writing both would draw it twice and leave the roster row for the live pass
    to settle a second time.

    THE TOTAL, NOT THE HISTORY'S COUNT. Getting here means every episode has been
    seen — `_completed_before_add` will not answer otherwise — and a service that
    reports a title finished without itemizing it would otherwise write a
    completed season at 11/13.
    """
    total = int((detail or {}).get("total") or 0)
    return await distrakt_store.add_month_record(user_id, month_key, {
        **record,
        **{field: (detail or {}).get(field) for field in lifecycle.CATALOGUE_FIELDS},
        "kind": distrakt_store.RecordKind.COMPLETED,
        "watched": total,
        "total": total,
    })


async def _add_to_past_month(request: Request, user_id, month_key: str, show: dict,
                             detail, settings, year: int, month: int,
                             *, message: str = "") -> Response:
    """Put one season onto a month that is OVER, as a completion, and answer with
    that month.

    THE WHOLE OF WHAT ADDING TO A PAST MONTH MEANS, in one place, because two
    routes reach it: the ordinary add when the viewer has navigated back, and the
    add-a-finished-show route the page uses on a month it is drawing read-only.
    Written twice they would drift on the only two decisions there are — what the
    record says and whether the month ends up closed — and drift there is
    invisible until somebody compares two months filled in by different buttons.

    NOTHING IS INFERRED AND NOTHING IS RECOMPUTED. A past month draws completions
    and abandons and nothing else, so the record is a completion; the viewer named
    the month, so it goes on that month; and the season is not put on their list
    as well, because a finished season is not something anybody is part-way
    through and both records would draw it twice. There is no re-watch question
    and no snapshot — see api_distrakt_add for why naming a month that has ended
    is itself the answer that question was asking for.

    THE EPISODE TOTAL MUST BE REAL, so a lookup that could not answer REFUSES
    rather than degrading. A settled record's counts are never recomputed, which
    makes a wrong one wrong for ever: it renders as a part-watched season on a
    month nothing will ever revisit, and reaches the ranker import as a wrong
    episode count.

    THE AIR-DATE FLAGS ARE STATED RATHER THAN READ. Every other record takes them
    from the lookup, which is right while a season is still being kept up with;
    here the viewer has said one was finished in a month that has ended, so it had
    started and had finished — and a lookup that happens to disagree (a service
    still listing an unaired episode, a catalogue since reshaped) would render
    their own statement back to them as a season that never began.
    """
    total = int((detail or {}).get("total") or 0)
    if not total:
        return _month_refusal(
            request,
            "No source lists any episodes for that season, so it cannot be recorded as finished.")
    try:
        stored = await distrakt_store.add_month_record(user_id, month_key, {
            **show,
            "kind": distrakt_store.RecordKind.COMPLETED,
            "watched": total,
            "total": total,
            "cadence": (detail or {}).get("cadence"),
            "premiere": (detail or {}).get("premiere"),
            "finale": (detail or {}).get("finale"),
            "started_airing": True,
            "finished_airing": True,
        })
    except distrakt_store.UnkeyableRecord as exc:
        return _month_refusal(request, str(exc))
    # CLOSED THROUGH ITS OWN VERB. A hand-filled past month ends up closed like
    # every other past month, so it renders and imports from its own rows with no
    # further provider calls — but closing is two columns on the month row, and a
    # save_month round trip to set them would delete the month's records and
    # re-insert only the ones the doc handed over.
    await distrakt_store.set_month_closed(user_id, month_key)
    await arrival.register(user_id, [show])
    await arrival.write_missing_premieres(settings, show)
    return await _month_answer(
        request, user_id, year, month, settings, message=message,
        point_at=((stored or {}).get("key") or "", int(show["season"])))


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
    backfill_start, backfill_end = backfill.default_range(today)
    context = {
        "request": request,
        "year": year,
        "month": month,
        "nav": route_params.adjacent_months(year, month),
        # WHICH MONTH EVERY CONTROL ON THIS PAGE ACTS ON, spelled once. It rides
        # in each address rather than in a body, because it is a REFERENCE the
        # server resolves and not data the browser is handing over — the shape
        # templates/_add_all.html gives the calendar's own month action.
        "month_query": f"?year={year}&month={month}",
        # For the announcement post's "which calendar view does the embedded link
        # open on" selector; the same list the calendar's endpoint picker uses.
        "endpoints": endpoint_choices(),
        **chrome.page_context(user),
        # This user's OWN map — it renders into their Discord posts and nobody
        # else's. Drawn with the page rather than fetched: it used to be BOTH,
        # rendered into a global AND fetched again by the page's own init before
        # the month was even asked for.
        "default_network_emoji": default_network_emoji,
        # The editor's rows come from the one function every emoji answer uses,
        # so the first draw and every redraw are the same markup.
        "emoji_rows": _emoji_row_contexts(network_emojis),
        # The range the backfill dialog opens on. Rendered rather than fetched:
        # the server decides it and used to answer a request whose only job was
        # to fill in two inputs.
        "backfill_start": backfill_start,
        "backfill_end": backfill_end,
        "restore_ack": RESTORE_ACK,
        # Which share-link forms would actually resolve, so the selector offers
        # only the ones that produce a working link.
        "post_link": await _post_link_choices(user.user_id),
        **_modal_context(year, month),
    }
    return templates.TemplateResponse(request, "distrakt.html", context)


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

    A PREMIERE THAT HAS BEGUN AIRING AND IS ON THE VIEWER'S LIST IS NOT LISTED
    HERE, since showing both copies would put the same season on the page twice
    under two different headings. Its premiere record is untouched and still
    carries the month's announcement — that is what the first notice reads. One
    the list does not hold is drawn as the premiere, or nothing would.

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
    # ON THE LIST, CHECKED RATHER THAN ASSUMED. A premiere that has begun airing
    # is left out because the list shows it, so it is left out only when the list
    # does. And one that IS drawn is drawn as what it was filed as — New or
    # Returning, its record's kind — not as the Keepup or Cleanup its airing
    # would make a listed season, because a month ahead shows only its premieres.
    # Measured: a premiere filed on the month ahead whose describing service says
    # it has already aired (Gnomes: Simkl's calendar dates S01E01 7 October,
    # Trakt 7 September) was drawn nowhere — only the first notice named it, and
    # it could not be removed.
    held = {(s["key"], int(s["season"])) for s in shape.listed}
    announced = [{**s, "bucket": distrakt_store.bucket_of_kind(s["kind"])}
                 for s in shape.premieres()
                 if (s["key"], int(s["season"])) not in decided
                 and not (s.get("started_airing") and (s["key"], int(s["season"])) in held)]
    rows = []
    for show in (*announced, *shape.listed, *shape.settled):
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
                         show.get("total"), live.source_labels(), live.source_order()),
                     # THE SAME BREAKDOWN IN FULL, for the row's tooltip — a peek
                     # at what this month actually recorded, which is the most
                     # useful thing a frozen row can offer.
                     #
                     # WITH NO FRESHNESS AND NO DATES, deliberately. Nothing was
                     # asked this pass, so "not asked" would be true of every
                     # service and would say nothing; and the dates live on the
                     # watch state, which a frozen month deliberately does not
                     # consult — it renders what it froze.
                     "counts_detail": show.get("counts_detail") or counts.counts_detail(
                         show.get("watched_by_source") or show.get("watched"),
                         show.get("total"), live.source_labels(), live.source_order())})
    return rows


async def _stale_month_payload(user_id: int, month_key: str, emojis: dict, default_emoji: str,
                               link_url: str | None, rate_limited: bool,
                               standing: distrakt_store.MonthStanding,
                               settings) -> dict:
    """Render a month WITHOUT any provider call, from whatever is last persisted —
    the top-level fallback when a shared refresh prerequisite hit a rate limit or
    was unreachable. Stored records already carry each show's last-known
    watched/total/cadence/dates, so this projects them offline, attaching a
    visible notice so stale-but-real beats a false 0/0 or a 500. `rate_limited`
    only chooses the notice wording; both cases degrade identically and return
    HTTP 200.

    THE VIEWER'S OWN LIST IS RENDERED TOO, and it used to be left out. The stated
    reason was that every row on it would need the season lookup that had just
    failed — true, and it does not follow that the rows cannot be drawn: each one
    carries its own last-known counts and dates (live.stored_shows), which is
    what the degraded LIVE path has always drawn for a single failed title. What
    the old shape produced instead was a page whose whole list vanished on a
    refresh and came back on a reload, which reads as data loss rather than as a
    service being briefly unreachable.

    ONLY FOR THE MONTH UNDER WAY, the same rule the live path follows: what
    somebody is keeping up with belongs to no month, so a month that is over or
    has not begun has no list to show.
    """
    doc = await distrakt_store.load_month(user_id, month_key)
    listed = (await distrakt_store.user_records(user_id)
              if standing is distrakt_store.MonthStanding.CURRENT else [])
    shape = lifecycle.shape_of(distrakt_store.frozen_shows(doc) if doc else [],
                               live.stored_shows(listed, settings))
    shows = _rows_for(shape, standing)
    # EVERY SERVICE THE TRACKER READS IS NAMED, because this page is only drawn
    # when none of them answered — naming Trakt alone told a Simkl account the
    # wrong thing.
    labels = live.source_labels()
    named = [labels.get(str(source), str(source))
             for source in await watch_history.tracker_sources(settings, user_id)]
    services = live.and_list(named) or "The services this tracker reads"
    notice = (
        f"{services} {'is' if len(named) == 1 else 'are'} rate-limiting us right now — "
        "showing last-known totals. Refresh again in a moment."
        if rate_limited else
        f"Couldn't reach {services} just now — showing last-known totals. "
        "Try refreshing again shortly."
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
                              today: date) -> tuple[dict, dict, dict, list[dict], list, list[str]]:
    """Bring this user's incremental watch-history cache up to date ONCE, and take
    the four answers the month needs out of it: how much of each season they have
    watched, when each season was finished, which films they watched in the month,
    and which episode plays the pull actually reported.

    THE STATE ITSELF COMES BACK BESIDE THEM, because one reader asks a question the
    four cannot answer. A settled verdict names a season nothing on the page is
    asking about any more, and watched_map is deliberately bounded to the seasons
    that ARE being asked about — so "what does each service say about that season
    now" has to be put to the state directly (watch_history.season_counts). It is
    the same object all four were derived from, already in memory, so handing it
    back costs nothing and re-reading it would be a second load of the same rows.

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
        completed_lookup = watch_history.season_completed_map(
            state, watch_history.viewer_tz(settings))
        # PER SERVICE, beside the one above and answering a different question:
        # that one is "when was this season finished", which has one answer; this
        # is "what does each service say", which is what a tooltip naming
        # services needs. See watch_history.season_dates_by_source.
        dates_lookup = watch_history.season_dates_by_source(state)
        mstart, mend = watch_history.month_bounds(month_key)
        movies = watch_history.movies_in_range(
            state, mstart, mend, watch_history.viewer_tz(settings))
        plays = watch_history.episode_plays(state)
        # Which sources this pass could not read, so the page can say a number is
        # one service's alone rather than presenting it as what everybody agrees.
        unreadable = watch_history.unreadable_sources(state)
        sp.set(watched_keys=len(watched_lookup), movies=len(movies), plays=len(plays))
    return (state, watched_lookup, completed_lookup, dates_lookup, movies, plays,
            unreadable)


def _season_lookup(settings) -> lifecycle.SeasonLookup:
    """How a reopening asks what a season looks like NOW, as a callable the
    lifecycle can use without knowing a provider exists.

    Handed in rather than reached for, because the transitions are otherwise
    derived from facts already in hand and this is the single exception — see
    lifecycle.SeasonLookup. It is paid once per season that reopened, never once
    per episode seen.

    A record no source can be asked about, or a lookup that fails, answers {}:
    the season still comes back onto the viewer's list, on the counts the
    withdrawn verdict had, and the next load re-derives them anyway. Failing the
    whole month over a season that grew would be the worse trade.

    ASKED OF WHOEVER THE RECORD'S OWN IDS NAME, through `live.season_detail`,
    rather than of Trakt off a bare `ids["trakt"]`. "How long is this season" is
    one question with one answer and one place that decides who answers it — a
    premiere correction that only ever asked Trakt simply never happened on an
    instance reading its catalogue from somewhere else.
    """
    async def look_up(record: dict, season: int) -> dict:
        rec = {**record, "season": season}
        # THE EMPTY ANSWER IS CHECKED FOR RATHER THAN FETCHED. `season_detail`
        # answers a zeroed season when nobody can be asked, which is right for a
        # caller that has nothing else to draw; here it would overwrite a real
        # withdrawn verdict's counts with zeros, and {} is what says "keep them".
        if live.detail_source(rec, settings) is None:
            return {}
        try:
            return await live.season_detail(settings, rec)
        except SourceUnavailable:
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


def _month_label(month_key: str) -> str:
    """"2026-02" as "February 2026", for a sentence."""
    year, month = (int(part) for part in str(month_key).split("-")[:2])
    return f"{_calendar.month_name[month]} {year}"


def _rewatch_rows(offers, state: dict, tz) -> dict[str, list[dict]]:
    """The re-watch offers in the flat shape the page's question row reads, under
    the payload field each kind is drawn from.

    THE SENTENCE NAMES EVERY EPISODE WATCHED AGAIN SINCE THE PLAY THAT RAISED IT,
    not only that one. One offer per season is the whole point — four episodes
    watched in an evening are one question — so the row says it covers four,
    read from the watch state already in hand rather than stored beside the
    question as a second copy of the viewing.

    NO IDS TRAVEL, like the unbacked verdict's row: the answer acts on a record
    that exists, and a caller handing its own in could only aim it elsewhere.
    """
    rows: dict[str, list[dict]] = {"rewatch_offers": [], "finale_offers": []}
    for offer in offers:
        play = offer.play
        day = watch_history.local_day(play.watched_at, tz)
        finished = _month_label(offer.month)
        if offer.finale:
            note = (f"finished in {finished}, and you watched its finale again on "
                    f"{watch_history.day_label(day)}. Record it as completed in "
                    f"{_month_label(day[:7])} too?")
        else:
            since = watch_history.episodes_watched_since(
                state, play.key, play.season, day, tz) or [play.number]
            watched = (f"E{since[0]:02d} again on {watch_history.day_label(day)}"
                       if len(since) == 1 else
                       f"{len(since)} episodes of it again since "
                       f"{watch_history.day_label(day)}")
            note = (f"finished in {finished}, and you've watched {watched}. "
                    f"Start a re-watch from that day?")
        rows["finale_offers" if offer.finale else "rewatch_offers"].append({
            "key": str(play.key), "season": int(play.season),
            "title": play.title, "note": note})
    return rows


def _unbacked_note(question: lifecycle.UnbackedVerdict) -> str:
    """The sentence a settled row nothing stands behind any more is offered with.

    IT SAYS BOTH NUMBERS, not just that something changed. "This no longer adds
    up" leaves the viewer to open the row, remember what it used to say and work
    out which service moved; the whole cost of the question is theirs to pay
    otherwise. Both sides are written through counts.counts_label — the same
    function the row itself is drawn with — so the two can never disagree about
    how a per-service reading is spelled.

    TWO SENTENCES, BECAUSE THERE ARE TWO REASONS AND THEY ARE NOT THE SAME NEWS.
    A RETRACTION is a service changing its mind: it said you had finished this and
    now says you have not, and the viewer may well want to know which one. A
    DECIDER CHANGE is nobody changing their mind at all — the verdict stands
    exactly as the service that made it left it, and what moved is which service
    this account counts first. Telling somebody "Trakt no longer reports finishing
    this" when Trakt never reported it in the first place would be a plain
    falsehood, which is why the shape is chosen here rather than the two being
    forced into one wording.

    Written here for the reason MONTH_AWAITS_IMPORT is: it is a sentence this page
    says about its own state, and the page's states are this module's.
    """
    labels, order = live.source_labels(), live.source_order()
    total = question.record.get("total")
    if question.decider and not question.sources:
        was = counts.counts_label(question.record.get("watched_by_source") or {},
                                  total, labels, order)
        now = counts.counts_label(question.now, total, labels, order)
        return (f"{labels.get(question.decider, question.decider)} is the service "
                f"you count first now, and it does not report this finished. "
                f"{question.month} recorded it as {was}, and it now reads {now}.")
    # Named in the registry's declared order, which is the order every other
    # per-service reading on the page is written in (counts.counts_label takes the
    # same one). The rule that produced them answers alphabetically because a set
    # has to be ordered somehow; that is not an order anybody chose.
    withdrew = ([name for name in order if name in question.sources]
                + [name for name in question.sources if name not in order])
    named = " and ".join(labels.get(name, name) for name in withdrew)
    verb = "no longer reports" if len(question.sources) == 1 else "no longer report"
    was = counts.counts_label(question.record.get("watched_by_source") or {},
                              total, labels, order)
    now = counts.counts_label(question.now, total, labels, order)
    return (f"{named} {verb} finishing this. "
            f"{question.month} recorded it as {was}, and it now reads {now}.")


def _unbacked_rows(questions: list[lifecycle.UnbackedVerdict]) -> list[dict]:
    """The settled verdicts nothing backs any more, in the flat shape the page's
    question row reads.

    NO IDS TRAVEL, unlike an unmatched play's row, and the difference is that this
    question is about a record that EXISTS. Saying yes re-derives a season the
    tracker is already holding, so the ids it needs to look one up are on the
    stored record where they have always been; a caller handing its own in could
    only point the answer at a different title.
    """
    return [{"key": question.record["key"], "season": int(question.record["season"]),
             "title": question.record.get("title") or "",
             "note": _unbacked_note(question)} for question in questions]


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
    standing = distrakt_store.month_standing(month_key, today)
    # NOTHING IS IMPORTED BY LOOKING. A month that has not begun is populated when
    # somebody ASKS — the Import control, which is also the only thing that builds
    # such a month in the first place (see rollover.can_initialize, which says the
    # ask is the safeguard). This load used to re-import a preview month's
    # premieres on every visit, on the reasoning that a preview should track the
    # calendar; what that meant in practice is that the calendar's list for a month
    # goes on growing right up to the 1st, so opening next month filed everything
    # the calendar had learned since the last look — 85 titles in one visit on the
    # instance this was found on, none of them chosen. Reading a month must not
    # decide what is in it.
    #
    # AND A TURN-AWAY ON THE CALENDAR NO LONGER REACHES A MONTH THAT EXISTS. It is
    # read where premieres are IMPORTED — app/distrakt/calendar_import.py's
    # premiere_records skips a marked title, so one is never offered — and
    # nowhere else. A record already on a month is the tracker's, and ending its run is
    # what the row's own ✕ and Abandon are for.
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

    state: dict = {}
    watched_lookup: dict = {}
    completed_lookup: dict = {}
    dates_lookup: dict = {}
    movies: list[dict] = []
    questions = lifecycle.HistoryQuestions([], [], [])
    unreadable: list[str] = []
    if ports:
        (state, watched_lookup, completed_lookup, dates_lookup, movies, plays,
         unreadable) = await _sync_watch_history(
            settings, user_id, everything, month_key, force_fresh, today)
        if under_way:
            # The history has moved, so what it reported is folded back into the
            # records: a season the viewer had finished and has now watched more
            # of comes back onto their list, and anything it could not place
            # anywhere is handed on to be asked about. Only on the month under
            # way — a season coming back is a statement about what the viewer has
            # in hand, and only that month has a list to put it on.
            #
            # ASKED EVEN WHEN THE SYNC REPORTED NOTHING, which is the difference
            # between a question that survives and one that does not. The plays
            # arrive once — the beacon moves and the next pull is empty — so a
            # payload built after that (a reload, or the rebuild every answer
            # route ends with) used to find no plays and therefore no questions,
            # and dropped every prompt the viewer had not got to yet.
            # `standing_questions` reads what is still outstanding from the
            # store; `reconcile_history` is what puts it there, and is skipped
            # when there is nothing new to fold in.
            if plays:
                questions = await lifecycle.reconcile_history(
                    user_id, plays, month=month_key, look_up=_season_lookup(settings),
                    tz=watch_history.viewer_tz(settings))
            else:
                questions = await lifecycle.standing_questions(
                    user_id, month=month_key, tz=watch_history.viewer_tz(settings))
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
            # WITHOUT THIS EVERY SERVICE REPORTS "no watch dates". The lookup is
            # only built inside compute_live_shows' own sync branch, and this
            # caller does its own sync and hands the results in — so a caller
            # that passes `watched_lookup` has to pass this too.
            dates_lookup=dates_lookup,
            # The services this pass actually read, which is what tells a season
            # only one of them knows about from one they agree on — and which of
            # them went quiet, which is what stops a row claiming its counts are
            # current when one service's history could not be read at all.
            sources_read=[source for source, _port in ports],
            sources_unread=unreadable) if everything else []
    # compute_live_shows answers in the order it was asked, so the split is where
    # the two inputs were joined.
    live_premieres, live_listed = computed[:len(premieres)], computed[len(premieres):]

    # A SERVICE CAN GO QUIET IN TWO WAYS AND THE BANNER MUST NAME IT EITHER WAY.
    # The sync above reports one whose HISTORY could not be read; the live pass
    # reports one whose EPISODE COUNTS could not be read, which is a public
    # catalogue lookup made per record and so fails on its own schedule (see
    # live.unreadable_detail_sources). Joined here rather than rendered as two
    # notices because they say the same thing to a reader — that service could
    # not be reached — and a row can be short of either number.
    for name in live.unreadable_detail_sources(computed):
        if name not in unreadable:
            unreadable.append(name)

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

    # WHETHER WHAT THIS MONTH SETTLED IS STILL BACKED BY THE SERVICES. A verdict is
    # never recomputed — it keeps the numbers it was reached on, which is why the
    # settled records are kept out of the live pass above — but it can come to
    # assert something no service says any more, and then the row silently claims a
    # viewing nobody reports. So the record is left exactly as it is and the
    # DIFFERENCE is offered to the viewer instead.
    #
    # ONLY THE MONTH UNDER WAY. A month that is over is history and answers "what
    # did that month decide", which today's watch state cannot make wrong; a month
    # that has not begun has settled nothing. `under_way` is the same test the
    # viewer's own list is read under, a few lines up, for the same reason.
    #
    # AND ONLY THE SERVICES THAT ANSWERED THIS PASS. One that could not be read
    # said nothing, and silence is not a retraction — see counts.no_longer_finished.
    unbacked: list[lifecycle.UnbackedVerdict] = []
    if under_way and ports:
        answered = [str(source) for source, _port in ports
                    if str(source) not in unreadable]
        live_counts = (lambda record, season: watch_history.season_counts(
            state, record["key"], season, answered))
        # A SETTLED ROW MAY STILL LEARN A COUNT THAT WENT UP, while its month is
        # open. A season finished at 8/8 by one service and 7/8 by the other goes
        # on reading 7 for the second long after it caught up, because a settled
        # record does not recompute itself — and the panel behind the row, which
        # reads the watch state directly, showed the eight ticks all along. The
        # two were reading different things. Upward only: a count going DOWN is a
        # withdrawal and belongs to the question below, never to a silent rewrite.
        #
        # BEFORE THE WITHDRAWAL CHECK, deliberately. A service that has caught up
        # is one the verdict now credits with finishing, so a later retraction by
        # that service is a real withdrawal and this is what makes it visible.
        if await lifecycle.catch_up_settled(user_id, month_key, shape.settled, live_counts):
            shape = lifecycle.shape_of(
                await distrakt_store.month_records(user_id, month_key), shape.listed)
        unbacked = await lifecycle.unbacked_verdicts(
            user_id, month_key, shape.settled,
            # The record's own flat identity, which is the key the watch state
            # files everything under — not re-resolved from its ids, which would
            # be a second answer to a question the record already carries.
            live_counts,
            # Which services this account trusts, most first — the same list the
            # rows' own numbers were picked with, so a verdict is only ever
            # questioned against the service the page is already showing.
            order=[source for source, _port in ports])

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
        # A season an earlier month finished, watched again: one offer per season
        # however many episodes the pull carried, split by which offer it is.
        **_rewatch_rows(questions.rewatched, state, watch_history.viewer_tz(settings)),
        # The third thing only the viewer can settle, and the only one of the
        # three that is about a row already on the page: a verdict this month
        # reached that the services have stopped backing. Present only on the
        # month under way, and only until it is answered either way.
        "unbacked_verdicts": _unbacked_rows(unbacked),
        "import_due": _import_due(month_key, today, premieres),
        # WHAT THE PAGE SAYS ABOUT WHAT IT COULD NOT REFRESH, as finished
        # sentences rather than as names for the browser to build one from. A
        # season showing one number has to say WHY that is all there is, instead
        # of reading as two services agreeing — and there is more than one why:
        # a service that could not be read, one this instance has no credential
        # for, and a title no registered service can look up. Composed here
        # because the rule for choosing between them is the same rule the row's
        # own tooltip is written from (app/distrakt/live.py), and a second copy
        # in JavaScript could not be tested against it.
        "source_notices": live.unavailable_notices(computed, unreadable=unreadable),
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
    # not on `settings` any more — see app/distrakt/viewer.py's settings_for_user.
    emojis, default_emoji = await distrakt_store.get_emoji_prefs(user_id)
    existing = await distrakt_store.load_month(user_id, month_key)
    if existing is None:
        blocked = await distrakt_store.is_backfill_blocked(user_id, month_key, today)
        # WHETHER A MONTH CAN BE BUILT IS WHETHER THERE IS A CALENDAR TO BUILD IT
        # FROM, the same question rollover.ensure_month asks before creating one —
        # and it has to be the same question, because this decides whether that
        # one is ever reached. Asking `trakt_configured` of the per-account
        # Settings the tracker builds reads as "did this viewer link Trakt", so an
        # account signed in with Simkl alone was handed an unpersisted empty month
        # on every load and everything it then imported or added went into a
        # document nobody kept.
        if blocked or not settings.calendar_source_configured:
            # Backward/gap past month (blocked) OR nothing to build from: empty,
            # NOT persisted, nothing fetched. `readonly` hides the add/edit
            # affordances.
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
            # is what builds it (api_distrakt_import). The month UNDER WAY is
            # built on sight, since what has been watched in it is already there
            # to catch up with, but its premieres wait for Import like these.
            # Not persisted and no Trakt call either way.
            payload = _empty_month_payload(
                month_key, emojis, default_emoji, standing, link_url=link_url,
                empty_note=MONTH_AWAITS_IMPORT,
            )
            payload["import_due"] = _import_due(month_key, today, [])
            return payload, 200

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
    except SourceUnavailable as exc:
        # THE CONTRACT, NOT ONE SERVICE'S ERROR. watch_history.sync raises when
        # NO service it reads could answer, promising that this handler draws
        # last-known totals at 200 — and this caught TraktError alone, so an
        # account whose first-read service is Simkl got a 500 and a page stuck on
        # "Loading…" whenever the network was down (measured with every outbound
        # connection refused). Any source's failure is degraded the same way.
        # A rate limit is told apart by its status, which both transports raise
        # their rate-limit errors with.
        rate_limited = getattr(exc, "status", None) == 429
        logger.warning("distrakt month %s degraded to stale (%s): %s", month_key,
                       "rate-limited" if rate_limited else "unreachable", exc)
        return await _stale_month_payload(user_id, month_key, emojis, default_emoji, link_url,
                                          rate_limited=rate_limited, standing=standing,
                                          settings=settings), 200


# THE QUESTIONS THE PAGE CAN ASK, and what differs between them. They read
# the same way and are answered the same way — so they share one row
# (templates/_distrakt_rows.html's `ask_row`) and one refusal — and only the
# sentence, the tick's title and the route the tick posts to are their own.
#
# THE UNBACKED ONE IS THE ODD ONE OUT TWICE OVER. It is not raised by a play, so
# there is no episode to name and its sentence is composed per row
# (`_unbacked_note`) rather than being a constant here. And it CONFIRMS before
# acting: the other two ADD something and the row's own remove undoes them, while
# this one takes a verdict apart and the month's record of having finished that
# season does not survive it.
# `no_title` IS HOW LONG ✗ LASTS, said on the button. A question a play raised is
# declined for what has been watched so far and asks again when more is (see
# lifecycle._still_declined); a verdict nothing backs any more is a standing
# condition, and declining it is for good.
_ASK_KINDS = {
    "unknown": {
        "no_title": "Not now. Ask again if I watch more of it",
        "sentence": "Should we add this to Distrakt?",
        "yes_title": "Add this season",
        "yes_url": "/api/distrakt/unknown-add",
        "confirm": "",
    },
    "given_up": {
        "no_title": "Not now. Ask again if I watch more of it",
        "sentence": "You'd given this up. Add it back to Distrakt?",
        "yes_title": "Put this season back on your list",
        "yes_url": "/api/distrakt/unknown-resume",
        "confirm": "",
    },
    "unbacked": {
        "no_title": "Don't ask about this again",
        "sentence": "",
        "yes_title": "Work this season out again from what the services say now",
        "yes_url": "/api/distrakt/verdict-readd",
        "confirm": ("Work this season out again from what your accounts say now? "
                    "This month stops recording that you finished it."),
    },
    # THE RE-WATCH PAIR, and the only two that can also be put off for the rest of
    # the month: an episode watched again for old times' sake is the one question
    # somebody may reasonably not want to answer every evening it happens.
    "rewatch": {
        "no_title": "Not now. Ask again if I watch more of it",
        "sentence": "",
        "yes_title": "Put this season back on your list, counting from that day",
        "yes_url": "/api/distrakt/rewatch-start",
        "confirm": "",
        "snooze": True,
    },
    "finale_again": {
        "no_title": "Not now. Ask again if I watch more of it",
        "sentence": "",
        "yes_title": "Record this season as completed in that month too",
        "yes_url": "/api/distrakt/rewatch-complete",
        "confirm": "",
        "snooze": True,
    },
}

# The order the questions are asked in: the ones nothing knows about first — a
# title the tracker has never heard of is a bigger gap than one it holds a
# verdict on, and a verdict that has lost its backing is the smallest of them,
# because the row it is about is on the page just below saying exactly what it
# has always said. A re-watch sits between: its season is known, and the offers
# are about viewing the page does not otherwise show.
_ASK_ORDER = (("unknown", "unknown_episodes"), ("given_up", "given_up_episodes"),
              ("finale_again", "finale_offers"), ("rewatch", "rewatch_offers"),
              ("unbacked", "unbacked_verdicts"))


def _ask_rows(payload: dict) -> list[dict]:
    """Every outstanding question in the payload, as the row template reads them:
    what the history event said, plus this kind's own sentence and verb."""
    return [{"snooze": False, **_ASK_KINDS[kind], "kind": kind, **ask}
            for kind, field in _ASK_ORDER for ask in payload.get(field) or []]


# What a read-only month says for itself, keyed by whether it was NEVER TRACKED
# (as opposed to tracked and since frozen). Neither state is a dead end any more
# — a finished show can be recorded by hand, and Backup then Backfill fills whole
# months in from watch history — so each names the way in that still works.
_BANNERS = {
    True: ("🕗 Past month — never tracked. Nothing is bucketed here, but you can "
           "record a finished show with ➕ Add show, or fill months in from watch history "
           "under Backup."),
    False: ("🔒 Past month — frozen snapshot. ➕ Add show records something "
            "as finished during it."),
}


def _modal_context(year: int, month: int) -> dict:
    """What the add flow's three overlays need drawing: which catalogues the
    search fields may name, whether an add into this month records something as
    FINISHED, and the day a film defaults to.

    THE MONTH BEING PAST IS THE ONE TEST, and it is the clock's alone. A month
    behind the clock is filled in by hand and is what `add-completed` is for; the
    month under way and anything ahead is an ordinary add. Both routes already
    enforce exactly that for themselves, so asking it here cannot disagree with
    them — and it needs no month read, which is what lets the page draw the right
    wording before the month has even been fetched.

    THE FILM'S DEFAULT DAY IS THE SERVER'S because the server knows both the
    clock and the month on screen: today when they are the same month, and
    otherwise the 1st of the month being filled in. It was `new Date()`
    arithmetic against the month the page had put on `window`, which is a
    second calendar.
    """
    today = clock.today()
    viewing_now = (today.year, today.month) == (year, month)
    return {
        "catalogue_sources": [str(source) for source, _
                              in providers.for_catalogue_search(load_settings())],
        "as_finished": distrakt_store.month_key(year, month) < _this_month(),
        "film_day": today.isoformat() if viewing_now else f"{year:04d}-{month:02d}-01",
        "film_max": today.isoformat(),
        # WHICH MONTH THE SEARCH IS FOR, and it has to start in these overlays.
        # Everything the add flow does afterwards resolves the month from the
        # ADDRESS it was reached by (`_viewed_month`), and the search form is the
        # first link in that chain: search -> results -> seasons -> the post. The
        # month page states this for its own controls, but an overlay is drawn
        # from this context and was getting an empty string — so filling in a
        # past month searched, listed and posted against TODAY, which ran the
        # current month's re-watch evaluation and put the row on the viewer's
        # list instead of recording it finished on the month on screen.
        "month_query": f"?year={year}&month={month}",
    }


def _month_context(request: Request, payload: dict, year: int, month: int,
                   emojis: dict, default_emoji: str, *,
                   message: str = "", ok: bool = True) -> dict:
    """The month fragment's whole context, out of the payload and nothing else.

    ONE READER OF THE PAYLOAD. Every mutating route answers through this, so what
    a month LOOKS like is decided once rather than per route — which is what the
    page's own renderer used to be, in a second language, reading the same dict.
    """
    shows = payload.get("shows") or []
    highlight = payload.get("highlight") or {}
    closed = bool(payload.get("closed") or payload.get("readonly"))
    # Which networks this instance actually holds a tile for, asked once for the
    # whole response rather than once per row, and never of the browser.
    with_logos = logos.tiled(show.get("network") for show in shows)
    return {
        "request": request,
        "sections": discord_fmt.page_sections(shows),
        "films": discord_fmt.page_films(payload.get("movies")),
        "asks": _ask_rows(payload),
        "import_due": bool(payload.get("import_due")),
        "empty_note": payload.get("empty_note") or "",
        # The degraded-month sentence and every unreadable source's, in one list:
        # they say the same thing to a reader, which is that something on this
        # page could not be refreshed.
        "notice_lines": [line for line in
                         [payload.get("notice"), *(payload.get("source_notices") or [])]
                         if line],
        "banner": _BANNERS[bool(payload.get("readonly"))] if closed else "",
        "post1": payload.get("post1") or "",
        "post2": payload.get("post2") or "",
        "highlight_key": highlight.get("key"),
        "highlight_season": highlight.get("season"),
        "message": message,
        "message_ok": ok,
        "closed": closed,
        "month_query": f"?year={year}&month={month}",
        "emoji_for": lambda network: emojis.get(network or "", default_emoji),
        "logo_for": lambda network: (
            f"/api/network-logo?name={quote(network or '')}" if network in with_logos else ""),
        **_modal_context(year, month),
    }


async def _month_answer(request: Request, user_id: int, year: int, month: int,
                        settings, *, message: str = "", ok: bool = True,
                        force_fresh: bool = False,
                        point_at: tuple | None = None) -> Response:
    """The month, recomputed and drawn: the one answer every mutating tracker
    route gives.

    A ROUTE THAT CHANGED SOMETHING HANDS BACK THE WHOLE MONTH, because a row that
    moved between buckets, a verdict withdrawn or a film forgotten all change what
    the page says about the rest of it. That was already true when this was JSON;
    what is new is that there is no second representation of it to keep in step.

    ALWAYS 200, WHATEVER THE PAYLOAD SAYS. A non-200 is never swapped
    (static/js/boost.js's `noSwap`), so a status code would leave the page holding
    the month it had and no explanation of why nothing happened.
    """
    payload, _status = await _distrakt_month_payload(user_id, year, month, settings,
                                                     force_fresh=force_fresh)
    if point_at is not None and payload.get("ok"):
        _point_at(payload, *point_at)
    if not payload.get("ok"):
        # The one payload shape that is not a month: no source can be asked at
        # all. There is nothing to draw, so it is said and the list is left alone.
        return _month_refusal(request, payload.get("error") or "Not configured")
    emojis, default_emoji = await distrakt_store.get_emoji_prefs(user_id)
    return templates.TemplateResponse(
        request, "_distrakt_month.html",
        _month_context(request, payload, year, month, emojis, default_emoji,
                       message=message, ok=ok))


def _rewatch_answer(request: Request, ask: dict, resubmit_url: str,
                    vals: dict) -> Response:
    """The re-watch question, drawn open, over a month left exactly as it was.

    NOTHING HAS BEEN WRITTEN and the answer is what performs the add, so the
    request that raised the question travels with the question — `resubmit_url`
    and the body that produced it — and the two buttons re-post it with the
    answer attached. Two bodies rather than one edited in the template, because
    which of them is sent IS the answer: an empty floor counts everything, and
    the field's own value starts a fresh run from the day it names.

    THE DATES ARE FINISHED HERE. `completed_on_label` and each listed episode's
    day are written out for a reader, and the two presets are the days either
    side of the restart — the day the run began and the day after the last play.
    The browser used to compute the second of those, which was `_day_after` in a
    second language.

    THE WHOLE ANSWER IS OUT OF BAND, so the list keeps the month it is showing:
    the question is about an add that has not happened.
    """
    restart = dict(ask.get("restart") or {})
    episodes = [{**play, "label": watch_history.day_label(play.get("day"))}
                for play in restart.get("episodes") or []]
    if episodes:
        after_day = _day_after(restart["episodes"][-1]["day"])
        restart = {**restart, "episodes": episodes,
                   "first_day": restart["episodes"][0]["day"],
                   "first_day_label": episodes[0]["label"],
                   "after_day": after_day,
                   "after_day_label": watch_history.day_label(after_day)}
    return templates.TemplateResponse(request, "_distrakt_rewatch_answer.html", {
        "request": request,
        "prompt": {**ask, "restart": restart,
                   "completed_on_label": watch_history.day_label(ask.get("completed_on")),
                   # EVERY DAY THIS QUESTION CAN SHOW IS SPELLED HERE, including
                   # the one the submit button opens naming. That button used to
                   # be drawn saying "start a fresh run" over a field that
                   # already held a day, correcting itself only once somebody
                   # touched the field — so the prompt arrived naming one answer
                   # and offering another.
                   "suggested_from_label": watch_history.day_label(ask.get("suggested_from")),
                   # The button's sentence, composed by the same function that
                   # answers /distrakt/fragments/day-label as the field is typed
                   # into — so the day it opens naming and the day it renames
                   # itself to are spelled one way.
                   "run_label": _run_label(ask.get("suggested_from"))},
        "resubmit_url": resubmit_url,
        "vals_counting": {**vals, "decided": True, "history_from": ""},
        "vals_fresh": {**vals, "decided": True},
    })


def _run_label(iso) -> str:
    """The submit button's whole sentence for the day a fresh run would start on.

    ONE SPELLING FOR ALL FOUR DAYS THIS QUESTION SHOWS. Three of them were already
    the server's; the fourth — a day somebody TYPES — was `toLocaleDateString` in
    the browser, and the two did not agree. MEASURED in Firefox 155 and Chromium
    153: this returns "5 September 2026" and the browser returned "September 5,
    2026" on a en-US locale, "5. September 2026" on de-DE, and matched only on
    en-GB. So the button was drawn naming a day one way and re-spelled it the
    other the moment the field was touched — the same class of fault watch_history.day_label
    was written to end, left half-done.

    THE EMPTY DAY IS PART OF THE SENTENCE, not a separate attribute on the button:
    "which run" has no answer when the field is empty, and saying that is this
    function's job like any other day.
    """
    day = watch_history.day_label(iso)
    return f"Start the run from {day}" if day else "Start a fresh run"


def _and_the_month(response: Response) -> Response:
    """`response`, plus a nudge that makes the month list fetch itself again.

    FOR AN ANSWER THAT CHANGES WHAT THE LIST LOOKS LIKE WITHOUT BEING ABOUT IT:
    saving the emoji map changes every roster badge, and restoring a backup
    replaces the month outright. Recomputing a month inside those answers would
    spend a season lookup per listed title in a response nobody asked a month of,
    and it would put a second month-drawing path beside the one the page already
    has. THE LIST ALREADY KNOWS HOW TO FETCH ITSELF (distrakt.html's hidden
    loader, `hx-trigger="load, month-changed from:body"`); this only says when.

    The event is aimed at `body` explicitly rather than left to bubble from
    whatever was pressed, because the loader listens there and a control inside a
    swapped-away fragment is not a reliable place to fire from.
    """
    response.headers["HX-Trigger"] = json.dumps({"month-changed": {"target": "body"}})
    return response


def _month_refusal(request: Request, message: str) -> Response:
    """A route declining to act, said in a line and nothing else.

    THE LIST IS NOT REDRAWN AND THAT IS THE POINT. Nothing changed, and
    recomputing a month to report that would spend a season lookup per listed
    title. The controls ask for `swapEmpty:false` so an answer whose only content
    went out of band leaves their target exactly as it was —
    templates/_distrakt_rows.html's `month_post` says why that has to be spelled.

    200, LIKE EVERY OTHER DRAWN REFUSAL IN THIS APP: a status code is never
    swapped, so a 404 here would be a button that silently does nothing.
    """
    return templates.TemplateResponse(request, "_distrakt_refusal.html",
                                      {"request": request, "message": message})


@guard.get("/distrakt/fragments/month", AuthLevel.DISTRAKT_APPROVED)
async def distrakt_month_fragment(request: Request):
    """The month this page is showing, as the page's own list.

    FETCHED ON LOAD RATHER THAN DRAWN WITH THE PAGE. Opening a month can pull a
    month of premieres, sweep recent viewing and write a roster's worth of rows,
    which is seconds — so the shell paints, says it is working, and this
    fills it in, exactly as it did when the page fetched JSON for itself.

    OPEN month: live x/y plus cadence and dates recomputed (one watch-history read
    plus a season call per show), auto-refreshed if totals are stale over 24h.
    CLOSED or past month: rendered from the frozen snapshot with NO provider
    calls. Opening an uninitialized month lazily rolls it over first (see
    ensure_month).
    """
    user_id = await _distrakt_user_id(request)
    today = clock.today()
    year = route_params.valid_year(request.query_params.get("year"), today.year)
    month = route_params.valid_month(request.query_params.get("month"), today.month)
    with span("GET /distrakt/fragments/month", ym=f"{year}-{month:02d}"):
        settings = await viewer.settings_for_user(user_id)
        return await _month_answer(request, user_id, year, month, settings)


@guard.post("/api/distrakt/refresh", AuthLevel.DISTRAKT_APPROVED)
async def api_distrakt_refresh(request: Request):
    """Force a fresh totals refresh: bypass the 24h season cache + re-stamp
    totals_refreshed_at for the OPEN month, then answer with the month drawn.
    CLOSED months are frozen (nothing to refresh)."""
    user_id = await _distrakt_user_id(request)
    await authz.json_body(request)
    year, month = _viewed_month(request)
    settings = await viewer.settings_for_user(user_id)
    frozen = distrakt_store.month_key(year, month) < _this_month()
    return await _month_answer(
        request, user_id, year, month, settings, force_fresh=True,
        message="Past month (read-only)" if frozen else "Refreshed totals")


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
    settings = await viewer.settings_for_user(user_id)
    # WHETHER THERE IS A CALENDAR TO IMPORT FROM, asked of the INSTANCE's own
    # settings rather than of `settings`, which carries this viewer's tokens.
    # The import reads the month's premieres out of the shared calendar cache and
    # this account's not-watching marks, spending no viewer's credential — so
    # asking either "did this viewer link Trakt" or "can this viewer's tokens
    # fill a calendar" refused the button to somebody signed in with Simkl alone
    # over a token the import never uses. calendar_import.premiere_records reads
    # under the same instance settings, for the same reason.
    if not load_settings().calendar_source_configured:
        return _month_refusal(request, "No calendar is configured, so there is nothing to import.")
    data = await authz.json_body(request)
    # WHICH TITLES, BY REFERENCE: one `pick:<key>|<season>` field per ticked row
    # of the dialog (calendar_import.candidate_ref). What they name is checked
    # against the offer the server reads now, so nothing arrives here as data.
    chosen = {name[len(_IMPORT_PICK):] for name, value in data.items()
              if name.startswith(_IMPORT_PICK) and value}
    today = clock.today()
    year, month = _viewed_month(request)
    month_key = distrakt_store.month_key(year, month)
    if await distrakt_store.is_backfill_blocked(user_id, month_key, today):
        return _month_refusal(request, "Can't import into a past month that was never tracked.")
    # BUILT ON THE PRESS, BEFORE THE TICKS ARE COUNTED: building a month takes
    # nothing from its calendar any more, so pressing Import on a month ahead
    # makes it whether or not anything was chosen.
    doc = await distrakt_store.ensure_month(user_id, year, month, settings, today=today)
    if doc.get("closed"):
        return _month_refusal(request, "Past month is frozen (read-only).")
    if not chosen:
        return _month_refusal(request, "Tick at least one title to import.")
    # The instance's settings, for the reason the gate above gives.
    # The networks it files are registered where it files them (arrival.register).
    added = await distrakt_store.import_premieres(user_id, month_key, load_settings(), chosen)
    return await _month_answer(
        request, user_id, year, month, settings,
        message=f"Imported {added} premiere{'' if added == 1 else 's'} from the calendar")


# The dialog's field prefix for one offered premiere — see api_distrakt_import.
_IMPORT_PICK = "pick:"


@guard.get("/distrakt/fragments/import", AuthLevel.DISTRAKT_APPROVED)
async def import_fragment(request: Request):
    """What the Import dialog offers: the premieres this month's calendar shows
    this viewer that the month does not hold yet, each with a box to untick.

    IT LOOKS NOTHING UP AND BUILDS NOTHING. The list is the stored calendar
    through the viewer's own filters and marks (calendar_import.import_candidates)
    — so a month of five hundred premieres costs a read of what is held, and the
    reader can see what they would be taking before a single title is described.
    A month that has not been built is read as an empty one rather than created:
    building it is the import's, on the press.
    """
    user_id = await _distrakt_user_id(request)
    year, month = _viewed_month(request)
    month_key = distrakt_store.month_key(year, month)
    # Every box starts ticked, so importing everything is one press; `ticked=none`
    # is the dialog's Untick all, drawing the same list with every box clear.
    context = {"request": request, "month_query": f"?year={year}&month={month}",
               "rows": [], "undescribed": [], "note": "",
               "ticked": request.query_params.get("ticked") != "none"}
    settings = load_settings()
    if not settings.calendar_source_configured:
        context["note"] = "No calendar is configured, so there is nothing to import."
    elif await distrakt_store.is_backfill_blocked(user_id, month_key, clock.today()):
        context["note"] = "Can't import into a past month that was never tracked."
    else:
        doc = (await distrakt_store.load_month(user_id, month_key)
               or distrakt_store.new_month_doc(month_key))
        if doc.get("closed"):
            context["note"] = "This month is frozen (read-only)."
        else:
            offer = await distrakt_store.import_candidates(user_id, doc, settings)
            context["rows"] = sorted(
                ({"ref": distrakt_store.candidate_ref(rec), "title": rec.get("title") or "",
                  "season": int(rec["season"]), "network": rec.get("network") or "",
                  "kind": "New" if int(rec["season"]) <= 1 else "Returning"}
                 for rec in offer.offered),
                key=lambda row: (row["kind"] != "New", row["title"].casefold()))
            context["undescribed"] = sorted(offer.undescribed, key=str.casefold)
    return templates.TemplateResponse(request, "_distrakt_import_list.html", context)


@guard.post("/api/distrakt/backfill-networks", AuthLevel.DISTRAKT_APPROVED)
async def api_distrakt_backfill_networks(request: Request):
    """Register every network used by this month's roster into the emoji map
    (with the default emoji) so they all show up in the editor. Returns the map."""
    user_id = await _distrakt_user_id(request)
    data = await authz.json_body(request)
    year, month = _viewed_month(request)
    doc = await distrakt_store.load_month(user_id, distrakt_store.month_key(year, month))
    stored = await _register_networks(
        user_id, [show.get("network") for show in (doc or {}).get("shows", [])],
    )
    # WHAT IS ON SCREEN WINS, WHICH IS WHY THE FORM POSTS ITSELF. The rows the
    # editor is holding may carry edits nobody has saved, and answering with the
    # stored map alone would throw them away — so the form sends what it has and
    # the merge happens here, adding only networks it does not already name. It
    # was `_syncEmojiEntriesFromDom` reading the rows back out of the DOM before
    # a redraw could clobber them; a form that posts its own fields needs no such
    # rescue.
    on_screen = dict(_emoji_rows_from_form(data))
    on_screen.pop("", None)
    merged = {**{network: emoji for network, emoji in stored.items()
                 if network not in on_screen}, **on_screen}
    user = await auth.current_user(request)
    return _emoji_answer(request, merged.items(), user.is_admin,
                         message="Backfilled networks from shows")


@guard.post("/distrakt/fragments/emoji-rows", AuthLevel.DISTRAKT_APPROVED)
async def distrakt_emoji_rows_fragment(request: Request):
    """The editor's rows with one more or one fewer, and nothing written.

    A POST THAT STORES NOTHING, and the verb is right rather than a compromise.
    What this needs in order to answer is the form as it stands — every field
    somebody has typed and not saved — and this app's rule is that an address
    carries a reference and never data. The server cannot look up an unsaved
    edit, so the form is the body.

    `remove` NAMES A ROW BY ITS FIELD SUFFIX. Leaving it out is the other action:
    the answer drops the blanks, sorts what is left and draws a fresh blank row
    at the end, which is exactly what "+ Add network" means once the row being
    typed into is committed.
    """
    user = await auth.current_user(request)
    data = await authz.json_body(request)
    rows = _emoji_rows_from_form(data, drop=str(data.get("remove", "")))
    return _emoji_answer(request, rows, user.is_admin)


@guard.get("/distrakt/fragments/day-label", AuthLevel.DISTRAKT_APPROVED)
async def day_label_fragment(request: Request):
    """What the rewatch question's submit button should say for a given day.

    A ROUTE FOR ONE SENTENCE, AND IT IS WORTH ONE. The alternative is the browser
    spelling a date itself, which it did, in a format that disagreed with the
    three the server had already spelled on the same dialog. Asking costs one
    request per date somebody types — a date field fires when the date is
    complete, not per keystroke — and what comes back cannot disagree with what
    the page opened saying, because the same function wrote both.

    IT READS THE FIELD'S OWN NAME. htmx sends a triggering input's name and value
    with the request, so the parameter is `history_from` — the same name the
    answer is posted under later. Inventing a second name for the same day here
    would be a second thing to keep in step.

    The day is whatever the field holds, including nothing at all; _run_label
    answers for the empty case too, so this needs no branch of its own.

    NOTHING OF THE QUERY REACHES THE ANSWER, which is why this returns a bare
    string rather than a template. watch_history.day_label parses the parameter as a date and
    answers "" for anything it cannot, so the sentence is built from a `date`
    object and two literals — a value carrying markup gets "Start a fresh run".
    tests/distrakt/test_rewatch.py holds that.
    """
    return HTMLResponse(_run_label(request.query_params.get("history_from")))


@guard.get("/distrakt/fragments/details", AuthLevel.DISTRAKT_APPROVED)
async def details_fragment(request: Request):
    """The details panel for one roster show, drawn with THIS user's own record of
    that season."""
    return await _details_view(request, episodes_only=False)


@guard.get("/distrakt/fragments/details/season", AuthLevel.DISTRAKT_APPROVED)
async def details_season_fragment(request: Request):
    """One season's episode rows for a roster show — what a season tab fetches.

    THE ROW IT IS AUTHORIZED BY IS NOT THE SEASON IT DESCRIBES, and that is the
    whole reason this needs saying. A roster row is a title AND a season, and the
    seasons worth paging back through are exactly the ones somebody who started
    tracking a show at season 4 does NOT have a row for. So the address a tab
    carries pins the season the panel was opened from (`row`, put there by
    app/details_panel.py's `season_tabs`) and asks about another one: the
    permission stays "this title is on your roster", which is a thing the caller
    could already see, while the question moves.
    """
    return await _details_view(request, episodes_only=True)


async def _details_view(request: Request, *, episodes_only: bool) -> Response:
    """The details panel for one roster show, or one season of its episode rows,
    drawn with THIS user's own record of that season.

    A VIEW, NOT AN API: it answers with the same panel the calendar and the share
    page open — templates/_details.html — plus the per-service ticks only this
    route can supply. What varies is a parameter, not a copy of the markup.

    A separate route from the calendar's rather than a parameter on it: that one
    is CALENDAR_APPROVED, and a distrakt-approved account need not be calendar
    approved. It also answers a different question — which episodes this
    particular person has seen — that the calendar has no business knowing.

    THE SOURCE ID AND THE SLUG BOTH COME FROM THE USER'S OWN ROSTER ROW, not from
    the query string: the caller names a row it can already see, and everything
    this looks up follows from that row. So the Trakt links it builds — and the
    title it fetches — cannot be pointed somewhere else by the caller.

    NEITHER HALF OF THIS NEEDS THE VIEWER'S TRAKT TOKEN, which is why the gate
    asks the catalogue question. The detail half is public catalogue data cached
    for the whole instance (overview, cast, the season's episode list); the
    watched half is read out of this app's own distrakt_show_progress, where it
    was written by whichever services the account actually syncs. An account
    that signs in with Simkl alone therefore opens the modal on any roster row
    that carries a Trakt id, and sees its own watched episodes on it.

    THE WATCHED HALF IS EVERY SERVICE'S ANSWER, NOT ONE OF THEM — see the read
    below for what it used to do and why the union is the honest reply.

    THE DETAIL HALF ASKS WHICHEVER SERVICE THE ROW CARRIES AN ID FOR, through the
    same `detail_source` the calendar's modal uses. It used to ask Trakt and only
    Trakt, and that was recorded here as a known gap until an account hit it.

    A ROSTER ROW DOES NOT NEED A TRAKT ID TO EXIST. The roster keys on the shared
    identity waterfall (app/providers/base.py's MATCH_SOURCES — tmdb, tvdb, imdb,
    mal) and never on a Trakt id, so a season baselined from a Simkl library read
    is filed perfectly well with none: Simkl's id map carries `traktslug` but
    never a numeric `trakt` (0 of 7768 catalogue records measured), and
    `collect_ids` drops the slug. The old refusal therefore caught rows the
    tracker was happy to create and told the viewer they were "Not on your
    roster" about a row that demonstrably was.

    TWO REFUSALS NOW, WHERE THERE WAS ONE, because they are different facts and a
    reader can act on only one of them. A row the page cannot find at all is
    still "Not on your roster". A row that is on the roster and that no
    registered, configured source can describe says so instead — the calendar's
    wording, because it is the calendar's situation.

    THE CHOICE IS NOT GATED ON WHAT THIS VIEWER LINKED, and `detail_source` is
    where that reasoning lives: a season's description is catalogue data, the
    same for everyone, so a Simkl-only title is asked of Simkl whether or not
    this account signed in with it.
    """
    user_id = await _distrakt_user_id(request)
    settings = await viewer.settings_for_user(user_id)
    # NO CONFIGURATION GATE OF ITS OWN. Which services can answer is exactly what
    # `detail_source.choose` decides, and it already treats "no source recognises
    # this title" and "the ones that do are not configured" as one answer — so a
    # gate naming Trakt here would refuse, in Trakt's name, a row Simkl could
    # have described.
    season = route_params.season(request.query_params.get("season"))
    # THE SEASON THE ROSTER ROW IS, which is the panel's own season on the first
    # request and rides on every season tab's address afterwards. It is what the
    # roster is searched by; `season` is what gets described.
    row_season = route_params.season(request.query_params.get("row")) or season
    try:
        key = parse_item_key(request.query_params.get("key"))
    except ValueError as exc:
        return details_panel.gap(request, str(exc))
    if season is None or row_season is None:
        return details_panel.gap(request, "Missing season")

    # Searched over everywhere the page can draw a row from — the viewer's own
    # list, this month's premieres, what this month settled, and back over what
    # earlier months settled — so a row the page drew is a row this route can
    # answer about, wherever the tracker happens to be holding it. It asks
    # lifecycle.find_shown_season rather than find_season because the latter is
    # written for a reconciliation and steps over the month under way's own
    # verdicts, which is exactly the case the modal reported "could not load" for:
    # a season finished this month has left the viewer's list and is not a
    # premiere, so all three of that search's places miss it.
    #
    # FROM THE MONTH ON SCREEN, NOT TODAY'S. The row names that month in its
    # address (templates/_distrakt_rows.html), and the search walks the viewer's
    # list, that month's premieres and what it and every month before it
    # settled. Asked from today's month instead, a premiere on a month still
    # ahead — every row an import files onto next month — was never searched,
    # and each one opened on "Not on your roster". Nothing is widened by it: the
    # search reads only this viewer's own records, whichever month it starts at.
    year, month = _viewed_month(request)
    placed = await lifecycle.find_shown_season(
        user_id, key, row_season, month=distrakt_store.month_key(year, month))
    if placed is None:
        return details_panel.gap(request, "Not on your roster")
    ids = placed.record.get("ids") or {}
    chosen = detail_source.choose(settings, ids)
    if chosen is None:
        return details_panel.gap(request, "Nothing here can describe this item.")
    source = chosen.source
    try:
        # See the calendar's own modal: with nobody reachable, what this
        # instance already holds beats a blank card, and None is the case where
        # it holds nothing either.
        details = await detail_source.describe(settings, chosen, Media.SHOW, season)
        if details is None:
            return details_panel.gap(request, "Nothing here can describe this item.")
    except Exception:
        # A FAILURE IS SOMETHING THE SERVER DRAWS. A non-200 is not swapped
        # (static/js/boost.js's `noSwap`), so a status code here would leave the
        # panel on its loading line; and the service's own words are logged
        # rather than shown, because an upstream rate-limit message is not
        # something the reader can act on. Every source's failure is one case
        # now: this route can be answered by either of two of them, and passing
        # one service's status on meant catching one service's error type.
        logger.warning("Detail lookup failed for %s", source, exc_info=True)
        return details_panel.gap(request, "⚠️ Could not load details for this item.")
    # THE ANSWER CARRIES THE SERVICE'S OWN NAME FOR THE TITLE, and a record short
    # of it is being looked at from a payload that has it. This used to render
    # the stored value and throw the fresh one away, so a row written without a
    # name stayed without one for ever however often it was opened — and its
    # episode ticks kept falling back to the numeric id.
    #
    # NO EXTRA CALL IS MADE. `describe` above already asked, through the pooled,
    # rate-limited client every read in that package shares; this only stops
    # discarding what came back. The write is add-only (store.learn_ids), and it
    # is logged because needing it at all means something stored the record
    # without a name it had been handed.
    fresh_ids = (details.get("ids") or {}) if isinstance(details, dict) else {}
    learned = slug_repair.recoverable_names(ids, fresh_ids)
    if learned:
        await slug_repair.learn_and_log(
            user_id, key, learned, slug_repair.EVIDENCE_DETAIL_LOOKUP,
            str(placed.record.get("title") or ""))
        ids = {**ids, **learned}
    # THIS VIEWER'S OWN RECORD OF THE SEASON, read by identity rather than by
    # roster row: the same call the calendar's panel makes for a title this
    # person tracks, so the two surfaces cannot come to different conclusions
    # about who watched what. The ids handed over are the row's own, learned
    # ones included — the same map the outbound buttons are built from, so a
    # service with a working tick has a working button.
    progress = await episode_progress.for_season(user_id, key, season, ids)
    if episodes_only:
        return details_panel.season_pane(request, details, media=str(Media.SHOW),
                                         season=season, progress=progress)
    # NO POSTER IS PASSED AND ONE IS STILL DRAWN. A roster row identifies a show
    # by its network logo and carries no picture of its own, so there is nothing
    # better to hand over than what the describing service already answered with —
    # which the panel falls back to on its own.
    return details_panel.panel(request, details, media=str(Media.SHOW), season=season,
                               title=str(placed.record.get("title") or ""),
                               # THE ROW'S OWN IDS, learned ones included — the
                               # same map the ticks are addressed by, so a service
                               # with a working tick has a working button.
                               links=detail_source.outbound_links(ids, Media.SHOW),
                               progress=progress,
                               # THE ROW'S SEASON IS THE COUNT: a row for season 4
                               # is a row for a show with three earlier ones, and
                               # nothing has to be looked up to know it.
                               season_count=row_season,
                               # THE ROW, AND THE ROW'S SEASON, AND NOTHING ELSE.
                               # Everything this route looks up follows from the
                               # key, so a tab needs nothing more — and a
                               # parameter the caller invented must not be
                               # rendered back into the panel, which is the rule
                               # the tick links already follow.
                               carry=("key",),
                               # AND THE MONTH IT WAS FOUND FROM, so a tab asks
                               # the same search the panel did.
                               pinned={"row": str(row_season),
                                       "year": str(year), "month": str(month)})


# The emoji editor's rows travel as one flat field per value, named by the row's
# POSITION: `network-0`, `emoji-0`, `network-1`… That is the shape the calendar's
# filter chips already use and it is chosen for the same two reasons. json-enc
# serializes a form as flat name/value pairs with no dependable array convention,
# so a list has to be spelled out; and the row's own network name cannot be the
# field name, because it is free text somebody is in the middle of editing.
#
# THE INDEX MEANS NOTHING BUT "these two belong together". Rows are re-sorted and
# renumbered on the way back out, so a gap left by a removal is not a hole
# anybody has to keep track of.
_EMOJI_ROW_FIELDS = ("network", "emoji")


def _emoji_rows_from_form(data: dict, *, drop: str = "") -> list[tuple[str, str]]:
    """The editor's rows as (network, emoji) pairs, in the order they were drawn,
    minus the one `drop` names.

    PAIRED BY THE SUFFIX AND ORDERED BY THE FORM, not by counting up from zero. A
    removal leaves a gap in the numbering of what is still on screen, and walking
    0, 1, 2… would stop at the hole and silently discard every row after it. The
    body preserves the order the fields were serialized in, which is the order
    they are drawn in, so iterating it IS the page's order.

    A ROW WITH NO NETWORK IS DROPPED rather than refused: the editor always draws
    a blank row for somebody to type into, and one left untouched is not an error
    to report. Values are stripped, because a network name with a trailing space
    keys nothing.
    """
    return [(str(value or "").strip(),
             str(data.get(f"emoji-{key[len('network-'):]}") or "").strip())
            for key, value in data.items()
            if key.startswith("network-") and key[len("network-"):] != drop]


def _emoji_row_contexts(pairs) -> list[tuple[str, str, str]]:
    """(network, emoji, tile URL or "") per row, alphabetically, blanks dropped.

    ONE BUILDER FOR THE FIRST DRAW AND EVERY REDRAW, so the rows the page ships
    with and the rows an answer swaps in cannot differ. Alphabetical by network
    is the order the editor has always shown, and it is the server's now:
    sorting in the browser meant the rows had to be held in an array there, which
    is why an unsaved edit had to be read back out of the DOM before anything
    could redraw them (`_syncEmojiEntriesFromDom`). A form holds its own edits, so
    that whole problem goes away with the array.
    """
    named = sorted(((network, emoji) for network, emoji in
                    (pairs.items() if isinstance(pairs, dict) else pairs) if network),
                   key=lambda pair: pair[0].lower())
    with_logos = logos.tiled(network for network, _emoji in named)
    return [(network, emoji,
             f"/api/network-logo?name={quote(network)}" if network in with_logos else "")
            for network, emoji in named]


async def _post_link_choices(user_id: int) -> dict:
    """What the announcement post's two selectors offer and which entries are
    currently chosen.

    ONLY FORMS THAT WOULD ACTUALLY RESOLVE ARE OFFERED — an entry that silently
    produces no link is worse than not being there — and with nothing publishable
    at all the selectors are not drawn, because the post omits the link. That
    decision, and the human names for the three kinds, used to live in the
    browser beside `share_links.PREFERRED_KINDS`, which names the same three for
    code; this is the one place they are named for people.
    """
    payload = await _share_link_payload(user_id)
    available = payload["available"]
    return {
        "usable": not payload["base_url_missing"] and any(available.values()),
        "kinds": [(kind, label) for kind, label in share_links.KIND_LABELS.items()
                  if available.get(kind)],
        "kind": payload["kind"] or "",
        "endpoint": payload["endpoint"] or "",
        "preferred_label": share_links.KIND_LABELS.get(payload["preferred_kind"],
                                                payload["preferred_kind"]),
    }


def _emoji_answer(request: Request, pairs, is_admin: bool, *,
                  message: str = "", ok: bool = True) -> Response:
    """The editor's rows, redrawn — the one answer every emoji-map route gives.

    ALPHABETICAL BY NETWORK, which is the order the editor has always shown and
    is now the server's to state. Sorting in the browser meant the rows had to be
    held in an array there, which is why an unsaved edit had to be read back out
    of the DOM before anything could redraw them (`_syncEmojiEntriesFromDom`) —
    a form holds its own edits, so that whole problem goes away with the array.

    A ROW WITH NO NETWORK NAME IS DROPPED HERE and a fresh blank one is drawn by
    the template, so the editor always has somewhere to type and never
    accumulates the ones nobody filled in.
    """
    return templates.TemplateResponse(request, "_distrakt_emoji_rows.html", {
        "request": request,
        "rows": _emoji_row_contexts(pairs),
        "is_admin": is_admin,
        "message": message,
        "message_ok": ok,
    })


@guard.post("/api/distrakt/emojis", AuthLevel.DISTRAKT_APPROVED)
async def api_distrakt_set_emojis(request: Request):
    """Replace this user's network->emoji map.

    DISTRAKT_APPROVED rather than ADMIN: the map is this account's own now, and
    it decides how this account's Discord posts render. It used to be saved
    through the admin-only settings endpoint, which is why only an administrator
    could edit what every user's posts looked like.
    """
    user = await auth.current_user(request)
    data = await authz.json_body(request)
    rows = _emoji_rows_from_form(data)
    default_emoji = str(data.get("default_network_emoji") or "").strip() or distrakt_store.DEFAULT_EMOJI
    clean = {network: emoji for network, emoji in rows if network}
    await distrakt_store.set_emoji_prefs(user.user_id, clean, default_emoji)
    # THE ROWS COME BACK SORTED AND RENUMBERED, which is the save's visible
    # effect: a blank row typed in at the bottom takes its place in the list, and
    # a row emptied out is gone.
    #
    # AND THE MONTH IS ASKED TO REDRAW ITSELF, because every roster badge falls
    # back to this map, so saving it changes what the list looks like. One header
    # rather than a second month recompute inside this answer: the list already
    # knows how to fetch itself, and this says when.
    return _and_the_month(_emoji_answer(request, clean.items(), user.is_admin,
                                        message="Emoji map saved"))


@guard.post("/api/distrakt/remove", AuthLevel.DISTRAKT_APPROVED)
async def api_distrakt_remove(request: Request):
    """The ✕ on a row: TAKE IT OFF THE MONTH IT IS ON, and off the viewer's list
    as well when that month is the one under way.

    WHICH IS THE WHOLE RULE, and it falls straight out of where things are stored.
    A month holds premiere records and settled verdicts (store.MONTH_KINDS); the
    viewer's list holds what they are part-way through and belongs to no month at
    all (store.USER_KINDS). A row on screen is therefore one of the month's or one
    of the list's, and the list's only ever appear on the month under way — so
    those two are the only places a ✕ can need to reach.

    IT USED TO REACH INTO EVERY MONTH AT ONCE, and that was the thinking that a
    removal should also withdraw the announcement that you had meant to watch it.
    The cost of that convenience was not local: MEASURED on a live account,
    September was full of wrongly-dated completions, and cleaning them off
    September took August — imported minutes earlier, correct, and never opened —
    from six records to one. Every ✕ on September deleted August's record of the
    same season. Tidying one month could destroy another's, which is the opposite
    of what a record is for. Going back to the premiere month to remove it there
    too is the cheaper price by a wide margin.

    A MONTH THAT IS OVER NEVER TOUCHES THE LIST. Its rows are verdicts, the list is
    a fact about the person rather than about any month, and nothing drawn on a
    past month came from it.
    """
    user_id = await _distrakt_user_id(request)
    data = await authz.json_body(request)
    year, month = _viewed_month(request)
    try:
        key, season = _row_target(data)
    except RequestError as exc:
        return _month_refusal(request, str(exc))
    month_key = distrakt_store.month_key(year, month)
    # The named month's records, whatever kind they are, and no other month's.
    # Asked of the MONTH rather than of whether it was frozen, for the reason
    # api_distrakt_add gives: a past month the rollover never reached is over just
    # the same, and a removal is not the place to discover that.
    gone = await distrakt_store.remove_season_from_month(user_id, month_key, key, season)
    if distrakt_store.month_standing(month_key, clock.today()) is distrakt_store.MonthStanding.CURRENT:
        # AND THE VIEWER'S LIST, because the month under way is the one page that
        # draws it. Not folded into the line above with `or`: the list has to be
        # asked either way, or a row that lives ONLY there — a season being kept up
        # with that this month never announced — would answer 404 and stay put.
        dropped = await distrakt_store.remove_user_record(user_id, key, season)
        gone = gone or dropped
    if not gone:
        return _month_refusal(request, "Show/season not found in that month")

    settings = await viewer.settings_for_user(user_id)
    return await _month_answer(
        request, user_id, year, month, settings,
        message="Taken off this month" if month_key < _this_month() else "Removed")


def _search_hit_payload(hit: catalogue_search.MergedSearchHit) -> dict:
    """One merged search hit as the add flow's results row is built from.

    THE ONE PLACE A `MergedSearchHit` BECOMES THE ROW: the fragment template
    (templates/_distrakt_search_results.html) draws both the row's text and the
    data attributes the pick reads back off it from this same dict, so there is
    no second statement of what a search hit is for the two to drift apart on.

    `key` TRAVELS AS THE SAME FLAT STRING every other route hands the client
    (see app/distrakt/store.py's `normalize_show`) — None when no source could
    name this title in a shared id space, which is the caller's cue that
    clicking it has to resolve one before an add is offered (see
    app/providers/base.py's SeasonsAnswer and this file's api_distrakt_seasons).
    `source_ids` names both which sources found it (the marks) and, per source,
    the id a season lookup calls back with — see MergedSearchHit's own
    docstring for why one field carries both.
    """
    return {
        "key": str(hit.key) if hit.key is not None else None,
        "season": hit.season,
        "source_ids": {str(source): source_id for source, source_id in hit.source_ids.items()},
        "ids": hit.ids,
        "title": hit.title,
        "year": hit.year,
        "network": hit.network,
        "runtime": hit.runtime,
        "overview": hit.overview,
    }


def _unkeyable_reason(media: Media, ids: dict, title: str) -> str | None:
    """`UnkeyableRecord`'s own sentence for a title the tracker cannot file, or
    None when it can.

    ONE SOURCE OF TRUTH FOR THE REFUSAL, asked BEFORE an add is offered as well
    as at the add itself. `record_key` is what decides whether a title has an id
    the tracker can key on (app/providers/base.py's MATCH_SOURCES is why some do
    not), and its message is what every surface says about it — this file's
    season lookup, the film search's disabled row, and api_distrakt_add's 400.
    Editing that sentence changes all three, which is the point of asking the
    question here rather than writing a second version of the answer.
    """
    try:
        distrakt_store.record_key({"media": media, "ids": ids, "title": title})
    except distrakt_store.UnkeyableRecord as exc:
        return str(exc)
    return None


def _search_rows(hits, media: Media) -> list[dict]:
    """The merged hits as the results fragment's row contexts.

    A FILM HIT WITH NO SHARED ID IS REFUSED HERE AND A SHOW HIT IS NOT, and the
    asymmetry is the flows' own rather than an inconsistency. Clicking a show
    hit already pays for a per-title lookup — the season list — and that lookup
    resolves the ids a search left out, so the show path can afford to find out
    on the click (see api_distrakt_seasons). A film has no season to pick and so
    no click that already spends a round trip, which would leave the refusal to
    the add attempt itself; saying it on the row instead costs one key
    resolution per hit and no request at all.
    """
    rows = []
    for hit in hits:
        row = _search_hit_payload(hit)
        row["unkeyable"] = (_unkeyable_reason(media, hit.ids, hit.title)
                            if media is Media.MOVIE and hit.key is None else None)
        rows.append(row)
    return rows


async def _search_fragment(request: Request, media: Media):
    """One submitted catalogue search as the results list the add modal swaps in.

    A VIEW, NOT AN API, following the ranker's own fragments: it answers with
    the rows and nothing else, so the browser has no second copy of what a
    search hit looks like to keep in step with `_search_hit_payload`.

    Gated on WHETHER ANY SOURCE CAN BE SEARCHED — `providers.for_catalogue_search`
    returning something — rather than on Trakt's configuration alone, so an
    instance with only a Simkl client id can still search: each source
    authenticates a catalogue read with the INSTANCE's own credential, never
    this viewer's token, which is the same distinction api_distrakt_add's own
    comment draws for the identical lookup. Adding what it finds is a separate
    act with its own gate. NO CATALOGUE AT ALL IS ITS OWN RENDERED STATE rather
    than an error: nothing was searched, so telling somebody their search failed
    would send them to retype a query that was never the problem.

    ONE SOURCE FAILING IS NOT THE SEARCH FAILING — `search_catalogue` returns
    who could not be asked alongside what the rest found, and the fragment says
    so quietly above results that are still real.
    """
    settings = await viewer.settings_for_user(await _distrakt_user_id(request))
    asked = providers.for_catalogue_search(settings)
    query = request.query_params.get("q", "")
    year, month = _viewed_month(request)
    context = {"request": request, "media": str(media), "hits": [], "failed": [],
               # A MARK DISAMBIGUATES, so with one catalogue there is nothing for
               # it to do — the rule _source_logo.html states and the reason a
               # single-source instance looks like this app always did.
               "marked": len(asked) > 1, "no_catalogue": not asked,
               # Each row carries its own request, so it needs to be able to spell
               # one: the query that found it names the search to read back, and
               # the month is the one an add would land in.
               "query": query, "month_query": f"?year={year}&month={month}"}
    if asked:
        result = await catalogue_search.search_catalogue(
            asked, settings, media, query)
        context["hits"] = _search_rows(result.hits, media)
        context["failed"] = sorted(str(source) for source in result.failed)
    return templates.TemplateResponse(request, "_distrakt_search_results.html", context)


@guard.get("/distrakt/fragments/search", AuthLevel.DISTRAKT_APPROVED)
async def distrakt_search_fragment(request: Request):
    """Show search for the add flow, merged across every catalogue this instance
    can ask (app/distrakt/search.py's `search_catalogue`). See `_search_fragment`
    for what it renders and why it is a fragment rather than JSON."""
    return await _search_fragment(request, Media.SHOW)


@guard.get("/distrakt/fragments/search-movie", AuthLevel.DISTRAKT_APPROVED)
async def distrakt_movie_search_fragment(request: Request):
    """Film search for the add-a-film flow, merged across every catalogue this
    instance can ask (app/distrakt/search.py's `search_catalogue`) — the same
    machinery the show search uses, media-parameterized to MOVIE.

    ITS OWN ROUTE RATHER THAN A MEDIA FLAG ON THE SHOW SEARCH, and the reason
    is not that the payloads differ — it is that a show and a film MEAN
    different things and change for different reasons, which is the test for
    what may not be collapsed into one unit. A SHOW IS ADDED TO BE TRACKED AS
    ITS EPISODES AIR: it goes onto the viewer's own list or becomes a month's
    premiere record, and the point of adding it is everything that happens
    afterwards — which is why it needs a season picker, a show search hit
    being incomplete without one. A FILM IS RECORDED AS ALREADY WATCHED: it
    goes straight into the month as completed, there is nothing to track and
    nothing will happen afterwards, so there is no season to pick and never
    will be. What is shared between the two routes is the port and the merge,
    both media-parameterized; what is not shared is what each route does with
    the answer.

    Gated on WHETHER ANY SOURCE CAN BE SEARCHED, exactly as the show search is
    — see `_search_fragment` for why `providers.for_catalogue_search` is asked
    rather than `trakt_catalogue_configured` alone, and for why a film hit with
    no shared id comes back refused on the row while a show hit does not.
    """
    return await _search_fragment(request, Media.MOVIE)


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
    settings = await viewer.settings_for_user(user_id)
    year, month = _viewed_month(request)
    day = str(data.get("watched_on") or "").strip()
    try:
        watched_on = date.fromisoformat(day)
    except ValueError:
        return _month_refusal(request, "Pick the day you watched it.")
    if watched_on > clock.today():
        return _month_refusal(request, "That day hasn't happened yet.")
    # THE RESULT IS NAMED, NOT HANDED OVER — the ids, the title and the year are
    # read back out of the search that found it (see `_resolve_pick`), so a play
    # is filed under the identity the service issued and never one a page sent.
    pick = await _resolve_pick(settings, Media.MOVIE, data.get("q") or "",
                               data.get("i"), cached_only=True)
    if pick is None:
        return _month_refusal(request, "That search has expired — search for it again.")
    if pick.unkeyable:
        return _month_refusal(request, pick.unkeyable)

    recorded = await watch_history.record_movie_watches(user_id, [{
        "ids": pick.ids,
        "title": pick.title,
        "year": pick.year,
        "watched_at": f"{watched_on.isoformat()}T12:00:00Z",
    }])
    if not recorded:
        # The film named no shared id, so there is no identity to file the play
        # under — see app/providers/base.py's MATCH_SOURCES.
        return _month_refusal(request, "That film has no id the tracker can file it under.")
    # The film this call just recorded, put on that month if it is closed. The
    # entry has the shape a snapshot holds because it comes from the same reader.
    # ONE FILM, NOT THE WHOLE LIST: re-snapshotting a closed month rebuilt every
    # film on it from today's history, so one correction could rewrite rows the
    # viewer had already settled.
    filed = distrakt_store.month_key(watched_on.year, watched_on.month)
    state = await watch_history.load_state(user_id)
    mstart, mend = watch_history.month_bounds(filed)
    filed_key = str(resolve_key(Media.MOVIE, pick.ids) or "")
    added = next((m for m in watch_history.movies_in_range(
                      state, mstart, mend, watch_history.viewer_tz(settings))
                  if str(m.get("key") or "") == filed_key), None)
    if added is not None:
        await _amend_closed_month_films(user_id, filed, add=added)

    # A film has no season, and -1 is what the roster's own schema uses for
    # "this record is not about one" (see db.py's calendar_airings and
    # store._SEASON_WHERE).
    return await _month_answer(
        request, user_id, year, month, settings,
        point_at=(resolve_key(Media.MOVIE, pick.ids) or "", -1),
        message=f"Recorded {pick.title} — watched {day}")


async def _amend_closed_month_films(user_id: int, month_key: str, *,
                                    add: dict | None = None,
                                    drop: object = None) -> bool:
    """Put ONE film into a closed month's stored list, or take one out.

    A closed month renders its films from its own snapshot and never recomputes
    them, so an edit has to be written into that snapshot or it will not appear
    there. An OPEN month recomputes on every load and needs nothing from here.

    IT AMENDS RATHER THAN REBUILDS, and that is a correction. This used to
    re-derive the whole list from today's watch history on every film edit, which
    made one edit replace the month's entire record with whatever history said at
    that moment. MEASURED on a live account: a month holding three films, with
    history reporting two for the same window, lost TWO when one was forgotten.
    _closed_month_payload says a frozen month's snapshot "is the record of what
    that month WAS — recomputing it against today's watch history would rewrite
    history every time it was opened"; this path was the one place doing exactly
    that.

    Returns whether it changed the month's list — what a removal reports when no
    watch record was behind the film.
    """
    if not month_key:
        return False
    doc = await distrakt_store.load_month(user_id, month_key)
    if doc is None or not doc.get("closed"):
        return False
    changed = False
    if add is not None:
        changed = await distrakt_store.add_month_movie(user_id, month_key, add)
    if drop is not None:
        changed = await distrakt_store.remove_month_movie(user_id, month_key, drop) or changed
    return changed


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
        return _month_refusal(request, str(exc))

    watched_at = await watch_history.forget_movie_watch(user_id, key)
    year, month = _viewed_month(request)

    # TWO MONTHS MAY HOLD IT, AND BOTH ARE AMENDED: the one the watch record
    # names, and the one on screen — the film the viewer pressed the X on.
    # A FROZEN MONTH'S FILM LIST IS ITS OWN RECORD, so a film can be on it with no
    # watch record behind it at all: filed before the tracker kept film history,
    # or no longer reported by any service. MEASURED: four February films on the
    # owner's account, removable in the page and refused here with "No such
    # film on record", because the watch was asked about and the month never was.
    # And a play re-dated since leaves the month on screen holding the old copy.
    months = {distrakt_store.month_key(year, month)}
    if watched_at is not None:
        months.add(watched_at[:7])
    dropped = [await _amend_closed_month_films(user_id, month_key, drop=key)
               for month_key in sorted(months)]
    if watched_at is None and not any(dropped):
        return _month_refusal(request, "No such film on record.")

    return await _month_answer(request, user_id, year, month,
                               await viewer.settings_for_user(user_id),
                               message="Film removed")


class Pick(NamedTuple):
    """A search result somebody chose, resolved the only way it may be resolved:
    by re-reading the answers the search and the season lookup already stored.

    NOTHING HERE CAME OFF THE PAGE. The page names a query and a result key; the
    title, the network and every id are the services' own words for it. That
    closes the last route in this app that took a caller's ids into a write —
    and it matters more here than almost anywhere, because a wrong Trakt id
    SHADOWS a correct Simkl one on a roster row for ever (§7's Supernatural
    finding), so an id a browser could influence is an id that can permanently
    mis-file somebody's watch record.

    `unkeyable` IS THE SERVER'S OWN SENTENCE for a title the tracker has no
    shared id to file under, and it is None when there is one — the same
    sentence the add's refusal uses, asked here BEFORE an add is offered.
    """
    title: str
    network: str
    year: int | None
    ids: dict
    seasons: list[dict]
    named_season: int | None
    unkeyable: str | None


async def _resolve_pick(settings, media: Media, query: str, index, *,
                        cached_only: bool) -> Pick | None:
    """The chosen result, or None when nothing stored answers to that name.

    A RESULT IS NAMED BY ITS QUERY AND ITS PLACE IN THE ANSWER, not by an
    identity, and that is not a shortcut. A show hit CAN have no shared id at
    all — that is the ordinary shape of a Simkl season-title before the per-title
    lookup below fills one in, and refusing those is exactly what this flow
    exists not to do — so there is no key to name it by until the pick has
    already happened. The merge is a pure function of the stored answers, so the
    same query lands on the same row.

    TWO CACHED READS, AND BOTH OF THEM ARE ANSWERS THE CLICK ALREADY PAID FOR.
    The merged search names the title, the network and the ids the hit carried;
    the per-title season lookup names the season list, the season the hit IS
    where its source files seasons as titles, and every shared id the search
    left out. Neither is a new question — the box searched a moment ago and the
    picker looked the title up — so re-asking them here is a read of the shared
    response cache and no request at all.

    `cached_only` IS THE CALLER'S BECAUSE THE TWO CALLERS DIFFER. The picker may
    make the per-title lookup, because that lookup is what the click is FOR; the
    add may not make anything, because an add that can search is an add that
    spends a budget nobody is counting. Both re-read the SEARCH from the store
    either way: the box's own request stored it seconds earlier, and a result
    the store no longer holds is one to search for again rather than one to
    guess at.

    THE FIRST SOURCE IN THE HIT'S OWN ORDER IS WHO IS ASKED, and that order is
    the registry's — whoever found a title is who can be asked about it, and a
    Simkl-only instance has no Trakt entry to reach for.
    """
    asked = providers.for_catalogue_search(settings)
    if not asked:
        return None
    result = await catalogue_search.search_catalogue(asked, settings, media, query,
                                                     cached_only=True)
    try:
        hit = result.hits[int(index)]
    except (TypeError, ValueError, IndexError):
        return None
    if media is Media.MOVIE:
        # A film has no season to pick and so no second lookup: the search hit
        # is the whole answer, which is also why an unfileable one is refused on
        # the row it is drawn as rather than on the click.
        return Pick(hit.title, hit.network, hit.year, dict(hit.ids), [], None,
                    _unkeyable_reason(media, hit.ids, hit.title))
    source, source_id = next(iter(hit.source_ids.items()))
    provider = providers.get(source)
    if not provider.detail_port.catalogue_configured(settings):
        return None
    answer = await provider.detail_port.fetch_seasons(settings, source_id, media,
                                                      cached_only=cached_only)
    # THE LOOKUP WINS A CONFLICT and the hit's own ids fill in the rest. This
    # answer's `season` was read off the per-title record `answer.ids` came from,
    # so those two describe the same title by construction; an id that disagrees
    # describes a different one, and keeping it would file the record with one
    # title's season beside another title's id — which is exactly what a source
    # listing one series as several titles produces, and a record nothing can
    # then count. The hit's ids still travel because a lookup that surfaces
    # nothing (Trakt, always — see SeasonsAnswer) must not read as "this hit had
    # no ids after all".
    ids = {**hit.ids, **answer.ids}
    return Pick(
        title=hit.title,
        year=hit.year,
        # ONLY WHERE THE HIT HAD NONE. The hit's network came from whichever
        # source led the merge; this one is the per-title record's, and it fills
        # a gap rather than overriding a source that already answered — a Simkl
        # search hit carries no network, so on a Simkl-only instance this is the
        # only place the roster ever gets one.
        network=hit.network or answer.network or "",
        ids=ids,
        seasons=answer.seasons,
        named_season=answer.named_season,
        unkeyable=_unkeyable_reason(media, ids, hit.title),
    )


@guard.get("/distrakt/fragments/seasons", AuthLevel.DISTRAKT_APPROVED)
async def distrakt_seasons_fragment(request: Request):
    """The add flow's season picker for the result somebody clicked.

    THE ADDRESS NAMES THE RESULT, NOT THE TITLE. It carries the query that found
    it and that result's own key, which is all the server needs to read the hit
    back — see `_resolve_pick`, and the ranker's own add for the rule this
    follows.

    ONE PARTIAL FOR EVERY STATE this can be in — a picker, a refusal, or a
    season that names itself and adds on sight — because each of them replaces
    the same slot, and a state drawn anywhere else is one the next answer cannot
    replace (templates/_add_all.html established the shape).
    """
    user_id = await _distrakt_user_id(request)
    settings = await viewer.settings_for_user(user_id)
    year, month = _viewed_month(request)
    query = request.query_params.get("q", "")
    index = request.query_params.get("i", "")
    # A SERVICE THAT COULD NOT BE ASKED IS ITS OWN STATE, drawn rather than
    # answered with a status: a non-200 is never swapped, so the slot would stay
    # empty with nothing to explain it — and "search for it again" would send
    # somebody to retype a query that was never the problem.
    unavailable = ""
    try:
        pick = await _resolve_pick(settings, Media.SHOW, query, index, cached_only=False)
    except SourceUnavailable as exc:
        pick, unavailable = None, str(exc)
    return templates.TemplateResponse(request, "_distrakt_seasons.html", {
        "request": request,
        "pick": pick,
        "unavailable": unavailable,
        "query": query,
        "index": index,
        "month_query": f"?year={year}&month={month}",
        # WHICH ROUTE A SEASON BUTTON POSTS TO, decided here rather than in the
        # browser. A month behind the clock is filled in by hand and records
        # something as FINISHED during it; the month under way and anything
        # ahead is an ordinary add the buckets work out. That is the same test
        # each of the two routes already enforces for itself
        # (api_distrakt_add_completed refuses anything but a past month), so
        # asking it here cannot disagree with them.
        "as_finished": distrakt_store.month_key(year, month) < _this_month(),
    })


async def _outstanding_play(user_id: int, key: ItemKey, season: int) -> dict | None:
    """The play that raised a still-open question about `(key, season)`, or None.

    THE ROW'S FACTS COME BACK OUT OF THE STORE, which is what lets the answer
    stop accepting them from the page. `lifecycle.reconcile_history` writes the
    play down — its title and every id it carried — before the question is ever
    rendered, precisely so a question survives a reload; reading it back here
    costs one indexed lookup and closes the last door through which a browser
    could name a different title than the one it was asked about.
    """
    return next((row for row in await distrakt_store.open_prompts(user_id)
                 if str(row["key"]) == str(key) and int(row["season"]) == int(season)),
                None)


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
    settings = await viewer.settings_for_user(user_id)
    # NO CATALOGUE GATE. This used to refuse the whole add unless the instance
    # held a TRAKT client id, which was the same shape of mistake `trakt_configured`
    # was here for before it: a season lookup that only Trakt could answer, stated
    # as a precondition on adding anything at all. The lookup below now asks
    # whichever source the title's own ids name (live.season_detail), so a
    # Simkl-only instance adds a Simkl-only title; and a lookup that cannot be
    # made at all is already survivable — see this function's own last paragraph.
    data = await authz.json_body(request)
    today = clock.today()
    year, month = _viewed_month(request)
    try:
        season = int(data["season"])
    except (KeyError, TypeError, ValueError):
        return _month_refusal(request, "Missing or invalid season")
    # THE RESULT IS NAMED, NOT HANDED OVER. The body carries the query that found
    # it and that hit's own key; the title, the network and every id are read back
    # out of the same two cached answers the picker was drawn from. This route
    # used to take `ids` and `title` straight from the page, which was the last
    # reachable way for a browser to decide what identity somebody's watch record
    # is filed under — and a wrong Trakt id shadows a correct Simkl one for ever.
    pick = await _resolve_pick(settings, Media.SHOW, data.get("q") or "",
                               data.get("i"), cached_only=True)
    if pick is None:
        return _month_refusal(request, "That search has expired — search for it again.")
    if pick.unkeyable:
        return _month_refusal(request, pick.unkeyable)
    show = {
        "media": Media.SHOW,
        "ids": pick.ids,
        "season": season,
        "title": pick.title,
        "network": pick.network,
        # Added by hand, so removing it later says nothing about the
        # calendar — see api_distrakt_remove.
        "added_by": distrakt_store.ADDED_BY_MANUAL,
    }
    try:
        # Resolved before anything is written, so a title with no shared id is
        # refused here rather than at the insert, where the answer would be a 500.
        key = distrakt_store.record_key(show)
    except distrakt_store.UnkeyableRecord as exc:
        return _month_refusal(request, str(exc))
    # ADDING A SEASON WITHDRAWS ANY REFUSAL ABOUT IT. A dismissal says "do not put
    # this season back on my list, stop asking", and adding it by hand is the
    # viewer doing exactly the thing that refusal refused — so the refusal has
    # lapsed. Left standing it silences the page's questions about a season the
    # viewer has visibly changed their mind about, which is what happened to a
    # re-added verdict: it could not be questioned again because a refusal made
    # about the row that used to be there still applied to the one replacing it.
    month_key = distrakt_store.month_key(year, month)
    standing = distrakt_store.month_standing(month_key, today)
    if await distrakt_store.is_backfill_blocked(user_id, month_key, today):
        # No backfill: refuse to create a never-tracked PAST month even via a
        # manual add, consistent with the read path's read-only rendering of such
        # months. api_distrakt_add_completed is the way to state one outright.
        return _month_refusal(
            request, "Can't add shows to a past month that was never tracked.")
    # WHAT THE SEASON IS FILED AS is arrival.describe's — the same ids, title,
    # network and season facts any other way onto the tracker would file — and
    # its season lookup is asked of whichever service those ids name, so a
    # Simkl-only title gets a real episode total too.
    #
    # AHEAD OF EVERY WRITE, because the question below needs the season's episode
    # total and has to be answered before anything is stored; after the refusals
    # above, so a refused add spends no lookup.
    arrived, = await arrival.describe(settings, [show])
    show, detail = arrived.record, arrived.detail

    # ALREADY FINISHED, ACCORDING TO THE VIEWER'S OWN HISTORY — asked FIRST, and
    # nothing at all is written until it is answered.
    #
    # THE TWO CASES ARE INDISTINGUISHABLE FROM THE DATA. Somebody starting a
    # re-watch and somebody whose tracker is meeting an old completion for the
    # first time produce the identical signal: a season the history says is
    # finished. The right record differs completely — a current run at zero, or a
    # finished one filed under the month that history dates — so it is asked.
    #
    # AND ASKED BEFORE THE ADD, WHICH IS THE WHOLE CORRECTION. It was asked after
    # for a while, on the reasoning that a viewer ignoring the question would
    # then get the old behaviour. That was wrong, because adding is not inert:
    # the recompute that follows an add runs the live pass, which settles any
    # season the history dates as finished onto the month it names. A season
    # watched in a month the tracker HAD tracked was therefore moved off the list
    # before the question could be answered, and the answer then had no row to
    # write to — the viewer saw "that season is not on your list" about a season
    # they had just added. One watched in an untracked month stayed and showed
    # its full episode count as this run's progress. Writing nothing until the
    # question is answered makes both unreachable rather than compensated for.
    #
    # A MONTH THAT IS OVER IS NOT ASKED ABOUT AT ALL, and that is not an exception
    # to the paragraph above so much as the case it never applied to. The question
    # exists because "the history says this season is finished" has two readings —
    # a re-watch beginning now, or an old completion the tracker is meeting for the
    # first time — and NAMING A PAST MONTH ANSWERS IT. Somebody standing on August
    # adding a show is saying it belongs to August; there is no current run to
    # start, and offering to begin one on a month that ended would file a season at
    # zero on a month nothing will ever advance.
    #
    # WHETHER THE MONTH WAS FROZEN IS THE WRONG QUESTION and used to be the one
    # asked, by the page rather than here: a past month the rollover never reached
    # has not been frozen, so an account that was offline through August ran the
    # whole re-watch flow on it. Where a month STANDS is a fact about the calendar
    # and true whether or not anything has been written down, which is why
    # month_standing is derived rather than stored.
    if standing is distrakt_store.MonthStanding.PAST:
        return await _add_to_past_month(request, user_id, month_key, show, detail,
                                        settings, year, month)
    floor = _valid_day(data.get("history_from"))
    ask, already_finished = await _rewatch_question(
        user_id, settings, show, key, int(show["season"]), detail.get("total"),
        data, title=show["title"])
    if ask:
        return _rewatch_answer(request, ask, f"/api/distrakt/add?year={year}&month={month}",
                               {"q": data.get("q") or "", "i": data.get("i"),
                                "season": season})

    await distrakt_store.clear_prompt_dismissal(user_id, key, int(show["season"]))
    if already_finished:
        await _file_finished(user_id, month_key, show, detail)
    elif detail and not detail.get("started_airing"):
        await distrakt_store.add_month_record(user_id, month_key, {
            **show,
            "kind": distrakt_store.premiere_kind(show["season"]),
            **{field: detail.get(field) for field in lifecycle.CATALOGUE_FIELDS},
        })
    else:
        await lifecycle.follow(user_id, {**show, **detail, "season": show["season"]})
    await arrival.register(user_id, [show])
    await arrival.write_missing_premieres(settings, show)
    # THE DECISION, AS A FLOOR, WRITTEN BEFORE THE MONTH IS RECOMPUTED. A
    # `history_from` in the body says "this pass starts here": the plays before
    # it belong to the previous run and are not counted, which is what makes a
    # re-watch show as a current season at zero. An empty one is the other
    # answer — count everything — and needs no write, because no floor IS
    # counting everything.
    #
    # IT MUST LAND BEFORE THE RECOMPUTE, not after: the recompute is the pass
    # that would otherwise settle this season straight onto the month its history
    # dates it to.
    #
    # AND IT TOUCHES NO MONTH RECORD. A season already settled on an earlier
    # month keeps that record — `lifecycle.follow` writes the roster row and
    # nothing else — so the old viewing stays where it settled and the re-watch
    # exists beside it, which is what a re-watch is.
    if floor:
        await distrakt_store.set_history_from(
            user_id, key, int(show["season"]), floor)
    return await _month_answer(
        request, user_id, year, month, settings, point_at=(key, show["season"]),
        message=f"Added {show['title']} S{int(show['season']):02d}")


def _valid_day(raw) -> str:
    """A viewer-supplied "YYYY-MM-DD", or "" for anything else.

    REFUSED RATHER THAN CORRECTED. This value decides which of somebody's plays
    count, and a date this cannot read is a date nobody meant — treating an
    unreadable one as "no floor" counts everything, which is the answer that
    changes nothing and is safe to be wrong about.
    """
    text = str(raw or "").strip()[:10]
    if not text:
        return ""
    try:
        return date.fromisoformat(text).isoformat()
    except ValueError:
        return ""


def _day_after(day: str) -> str:
    """The day after `day`, or "" if it cannot be read.

    THE DAY AFTER AND NOT THE DAY: the last episode of the previous run was
    watched ON that day, so a floor including it would carry one episode of the
    finished pass into the new one and show a fresh run starting at 1.
    """
    try:
        return (date.fromisoformat(str(day)[:10]) + timedelta(days=1)).isoformat()
    except ValueError:
        return ""


async def _completed_before_add(user_id: int, key, season: int,
                                total, tz) -> tuple[str, str, dict]:
    """`(the day this season was finished, the day to offer a fresh run from,
    what the restart looks like)` — or `("", "", {})`, where "finished" means
    every episode of it and not merely some.

    THE TWO ARE NOT THE SAME DAY WHENEVER SOMEBODY HAS ALREADY RESTARTED, and
    getting that wrong was the worst answer for exactly the case the question
    exists to serve. The offer used to be the day AFTER the last play, which is
    right for a viewer who has not begun again — but a viewer three episodes into
    a second run has their most recent play INSIDE that run, so the offer landed
    after it and reported the new pass as empty. `watch_history.restart_day`
    finds where the run began by reading the play ORDER rather than by deciding
    how long a gap has to be, and that is what is offered when it finds one.

    THE TOTAL IS REQUIRED AND THAT IS THE POINT OF THE ARGUMENT.
    `season_completed_map` answers "when was this season's finale watched", which
    is a different question and says so in its own docstring: it
    dates a season somebody is two episodes into just as readily as a finished
    one. Asking it alone made the re-watch question appear on any season with any
    history at all, which would interrogate a viewer about a run they are in the
    middle of. The count has to be met before the date means anything.

    NO TOTAL MEANS NO QUESTION. A lookup that failed leaves nothing to compare
    against, and guessing would ask about seasons at random; the add proceeds as
    an ordinary one, which is the answer that changes nothing.

    THE MOST ANY ONE SERVICE HAS SEEN, not the sum. Two services reporting the
    same thirteen episodes have both seen thirteen, and adding them would call
    every co-tracked season finished twice over.

    ASKED OF THE HISTORY AND NOT OF THE ROSTER, because a roster row for this
    season either does not exist yet or says nothing about the past.

    THE FLOOR IS HONOURED, so a season somebody has ALREADY declared a fresh run
    on is not asked about again: the plays before that floor are not part of this
    pass and cannot complete it.
    """
    try:
        needed = int(total or 0)
    except (TypeError, ValueError):
        needed = 0
    if needed <= 0:
        return "", "", {}
    row = await distrakt_store.find_user_record(user_id, key, season)
    state = await watch_history.load_state(user_id)
    if row:
        state = watch_history.apply_history_floor(
            state, watch_history.history_floors([row]))
    counts = watch_history.watched_map(state).get((str(key), season)) or {}
    if max(counts.values(), default=0) < needed:
        return "", "", {}
    finished = watch_history.season_completed_map(state, tz).get((str(key), season), "")
    if not finished:
        return "", "", {}
    # THE RESTART ITSELF WHEN THE ORDER SHOWS ONE, and the day after the last
    # play when it does not. A restart day is offered AS IT IS rather than the
    # day after it, because the episodes watched on that day are the first of
    # the new pass and belong to it.
    restart = watch_history.restart_details(state, key, season)
    if not restart:
        return finished, _day_after(finished), {}
    # THE DAY THE PREVIOUS RUN ENDED, WHERE IT IS KNOWN, because `finished` is
    # the finale's LATEST play and for a viewer who has re-watched as far as the
    # finale that is the new run's — so the page would tell them they finished the
    # season on the day they finished it the second time.
    return (restart.get("finished_on") or finished), restart["began"], restart


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

    The episode total comes from a season lookup, not from the caller — a frozen
    month's counts are never recomputed, so a wrong one is wrong forever and
    would reach the ranker import as a wrong episode count.
    """
    user_id = await _distrakt_user_id(request)
    settings = await viewer.settings_for_user(user_id)
    # NO CATALOGUE GATE, for the reason api_distrakt_add gives: which source can
    # answer for a title follows from the title's own ids, so an instance-wide
    # precondition naming ONE service is the wrong question to ask here. This
    # route still refuses when the lookup comes back with no episodes, which is
    # the condition it actually cares about.
    data = await authz.json_body(request)
    year, month = _viewed_month(request)
    month_key = distrakt_store.month_key(year, month)
    if month_key >= _this_month():
        return _month_refusal(request, "Only a past month can be filled in by hand.")
    try:
        season = int(data["season"])
    except (KeyError, TypeError, ValueError):
        return _month_refusal(request, "Missing or invalid season")
    # The result is NAMED here too, for the reason api_distrakt_add gives.
    pick = await _resolve_pick(settings, Media.SHOW, data.get("q") or "",
                               data.get("i"), cached_only=True)
    if pick is None:
        return _month_refusal(request, "That search has expired — search for it again.")
    if pick.unkeyable:
        return _month_refusal(request, pick.unkeyable)
    try:
        # WHAT THE SEASON IS FILED AS is arrival.describe's, as for every way
        # onto the tracker. STRICT: the episode total has to be REAL here — a
        # frozen month's counts are never recomputed — so unlike the ordinary add
        # a lookup that fails refuses rather than degrading. Asked of whichever
        # source the ids name, so a Simkl-only title can be filled in by hand too.
        arrived, = await arrival.describe(settings, [
            {"media": Media.SHOW, "ids": pick.ids, "title": pick.title,
             "season": season, "network": pick.network,
             "added_by": distrakt_store.ADDED_BY_MANUAL}], strict=True)
    except SourceUnavailable as exc:
        return _month_refusal(request, f"That season could not be read: {exc}")
    # A lookup that names no episodes is refused rather than filed at zero, and
    # that refusal lives in _add_to_past_month with the reason for it, because the
    # other door into a past month has to make the same one.
    #
    # Through the one place that knows what adding to a month that is over means
    # — the record it becomes and the month ending up closed — because the
    # ordinary add reaches the same place once the viewer has navigated back.
    # `added_by` is stated here rather than there because this is a person filling
    # a month in, which is a different provenance from anything a sweep wrote.
    return await _add_to_past_month(
        request, user_id, month_key, arrived.record,
        arrived.detail, settings, year, month,
        message=f"Recorded {arrived.record['title']} S{season:02d} as finished")


def _backfill_answer(request: Request, *, plan: dict | None = None,
                     warning: str = "", done: str = "") -> Response:
    """The backfill panel's answer, in whichever of its states this is: a plan to
    confirm, a line saying it wrote them, or a refusal.

    ONE PARTIAL FOR ALL OF THEM because each replaces the same block, and a state
    drawn anywhere else is one the next answer cannot replace — the shape
    _add_all.html established. It also means "Write these months" only exists
    while there is a plan to write, which is what the browser was keeping a
    `backfillReady` flag for.

    THE PLAN IS THE SERVER'S AND STAYS THERE. This route surveys and keeps what
    it found; the page confirms a summary of it and never hands records back to
    be stored.
    """
    return templates.TemplateResponse(request, "_distrakt_backfill.html", {
        "request": request,
        "plan": plan,
        "warning": warning,
        "done": done,
    })


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
    settings = await viewer.settings_for_user(user_id)
    # WHETHER ANY SERVICE CAN BE ASKED FOR A HISTORY, which is the same question
    # the month list asks and for the same reason — a survey with nothing to
    # sweep has nothing to report. Naming one service refused the whole backfill
    # to an account whose history is perfectly readable from the other.
    if not await watch_history.tracker_ports(settings, user_id):
        return _backfill_answer(request, warning="No service here can be asked for a watch history.")
    data = await authz.json_body(request)
    default_start, default_end = backfill.default_range()
    start = _requested_month(data.get("start"), default_start)
    end = _requested_month(data.get("end"), default_end)
    if start is None or end is None:
        return _backfill_answer(
            request,
            warning="Months go in as YYYY-MM, with the month padded — 2026-07, not 2026-7.")
    if not backfill.month_range(start, end):
        return _backfill_answer(request, warning="That range covers no months — check the order.")
    try:
        plan = await backfill.survey(user_id, settings, start, end)
    except SourceUnavailable as exc:
        # WHICHEVER SOURCE THE SURVEY ASKED, not Trakt by name. A backfill reads
        # the account's primary source (see backfill.survey), which for an
        # account that has not linked Trakt is not Trakt at all — and a survey
        # that cannot read the history has to SAY so rather than come back with
        # an empty plan the viewer would confirm as "there was nothing there".
        return _backfill_answer(request,
                                warning=f"Your watch history could not be read: {exc}")
    # THE VIEWER'S OWN TIMEZONE DECIDES WHICH MONTH A PLAY FALLS IN. Summarising
    # in UTC put a film watched late on the 31st on the following month.
    return _backfill_answer(
        request, plan=backfill.summarize(plan, watch_history.viewer_tz(settings)))


@guard.post("/api/distrakt/backfill/apply", AuthLevel.DISTRAKT_APPROVED)
async def api_distrakt_backfill_apply(request: Request):
    """Write the surveyed plan. Refuses if there isn't one — the survey is the
    only thing that can produce records, and it expires."""
    user_id = await _distrakt_user_id(request)
    await authz.json_body(request)
    settings = await viewer.settings_for_user(user_id)
    try:
        written = await backfill.apply(user_id, settings)
    except backfill.BackfillExpired as exc:
        return _backfill_answer(request, warning=str(exc))
    # WHAT WAS ACTUALLY WRITTEN, and only mentioning films when it wrote some.
    # The sentence used to be composed in the browser and named a control on
    # another screen ("switch What to import to Movies") unconditionally,
    # including on runs that found no films at all — the label of another page's
    # field is not a fact a script can keep true.
    films = written.get("movies") or 0
    said = (f"Wrote {len(written.get('months') or [])} month(s), "
            f"{written.get('shows') or 0} finished season(s)"
            + (f", {films} film(s)." if films else ".")
            + (" Films are on your watch record now; bringing them into a board is a"
               " separate import from Rankings." if films else ""))
    return _and_the_month(_backfill_answer(request, done=said))


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
    year, month = _viewed_month(request)
    try:
        key, season = _row_target(data)
    except RequestError as exc:
        return _month_refusal(request, str(exc))
    abandoned = bool(data.get("abandoned"))
    viewed_month = distrakt_store.month_key(year, month)
    # An abandon is dated by the clock; see the docstring. The viewed month is
    # still what the response re-renders, and it is what an un-abandon addresses,
    # because that one has to find the record it is undoing.
    acted_month = distrakt_store.month_key(today.year, today.month)

    settings = await viewer.settings_for_user(user_id)
    if abandoned:
        placed = await lifecycle.find_season(user_id, key, season, month=viewed_month)
        if placed is None:
            return _month_refusal(request, "Show/season not found in that month")
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
            return _month_refusal(request, "Show/season not found in that month")

    return await _month_answer(request, user_id, year, month, settings,
                               message="Marked abandoned" if abandoned else "Un-abandoned")


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
        return _month_refusal(request, str(exc))
    if not await distrakt_store.set_came_back(user_id, key, season, False):
        return _month_refusal(request, "That season isn't on your list.")
    # AN EMPTY ANSWER IS THE WHOLE ANSWER: the button asked to be swapped out of
    # existence (hx-target="this", outerHTML), so having nothing to swap in is
    # exactly right. A refusal draws a line instead, which goes out of band and
    # therefore leaves the mark standing to be pressed again.
    return Response(status_code=200)


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
    settings = await viewer.settings_for_user(user_id)
    # A CATALOGUE LOOKUP, NOT A READ OF ANYBODY'S OWN DATA. What this needs is the
    # instance's client id, exactly as /search and /seasons ask for the same
    # lookup. `viewer.settings_for_user` swaps in the VIEWER's token, so asking
    # `trakt_configured` here asked whether this account had linked Trakt — and
    # refused the whole action to somebody signed in with Simkl alone.
    if not settings.trakt_catalogue_configured:
        return _month_refusal(request, "Not configured")
    data = await authz.json_body(request)
    try:
        key, season = _row_target(data)
    except RequestError as exc:
        return _month_refusal(request, str(exc))
    # THE IDS AND THE TITLE COME OUT OF THE STORE, NOT OUT OF THE REQUEST. The
    # play that raised this question was written down with them
    # (store.open_prompts), so the row the page drew and the record this writes
    # are the same fact read twice from one place — and no id a browser could
    # edit reaches a write, which is the rule the add flow now keeps everywhere.
    asked = await _outstanding_play(user_id, key, season)
    if asked is None:
        return _month_refusal(request, "That question is no longer outstanding.")
    record = {
        "media": key.media, "match_source": key.match_source, "match_id": key.match_id,
        "season": season,
        "ids": dict(asked.get("ids") or {}),
        "title": str(asked.get("title") or ""),
        "network": "",
        # Nothing announced this and no month built it in: the viewer's own
        # viewing is what put it here, which is what this records.
        "added_by": distrakt_store.ADDED_BY_HISTORY,
    }
    # WHAT THE SEASON IS FILED AS is arrival.describe's, paid HERE, on the click:
    # a history prompt is built from a play, which is deliberately thin (see
    # watch_history.EpisodePlay: a lookup per unmatched episode is the cost the
    # ask-the-viewer path exists to avoid) — no network, one service's ids.
    arrived, = await arrival.describe(settings, [record])
    record, detail = arrived.record, arrived.detail
    year, month = _viewed_month(request)

    # THE SAME QUESTION THE ORDINARY ADD ASKS, AND FOR A BETTER REASON HERE. This
    # row exists BECAUSE a play arrived that nothing could place — which for a
    # season already watched right through is precisely what starting it again
    # looks like. Answering yes used to add it at its full episode count with no
    # question, so a re-watch arrived already finished.
    #
    # NOTHING IS WRITTEN WHILE IT IS ASKED, including the prompt's own closure:
    # a question the viewer abandons has to still be there afterwards, or saying
    # nothing would quietly discard the play that raised it.
    ask, already_finished = await _rewatch_question(
        user_id, settings, {**record, **detail, "season": season}, key, season,
        (detail or {}).get("total"), data, title=record["title"])
    if ask:
        year, month = _viewed_month(request)
        return _rewatch_answer(
            request, ask, f"/api/distrakt/unknown-add?year={year}&month={month}",
            {"key": str(key), "season": season})

    if already_finished:
        await _file_finished(
            user_id, distrakt_store.month_key(year, month), record, detail)
    else:
        await lifecycle.follow(user_id, {**record, **detail, "season": season})
        # THE DECISION, AS A FLOOR. A `history_from` says this pass starts here,
        # so the plays before it are the previous run's and are not counted —
        # which is what makes a re-watch show as a current season at zero.
        floor = _valid_day(data.get("history_from"))
        if floor:
            await distrakt_store.set_history_from(user_id, key, season, floor)
    # The viewer's own emoji map is keyed by network name, so a network nothing
    # registered draws nothing — the same call every way onto the tracker makes.
    await arrival.register(user_id, [record])
    await arrival.write_missing_premieres(settings, {**record, "season": season})
    # ANSWERED, so it stops being asked. Closing it here rather than leaving
    # standing_questions to notice the season is now listed keeps the answer and
    # its effect in one place; that check is the safety net, not the mechanism.
    await distrakt_store.close_prompt(user_id, key, season)
    return await _month_answer(request, user_id, year, month, settings,
                               point_at=(key, season), message="Added")


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
        return _month_refusal(request, str(exc))
    year, month = _viewed_month(request)
    month_key = distrakt_store.month_key(year, month)
    # find_shown_season, for the same reason the page's own question now uses it:
    # find_season deliberately stops one month short of the month being read, so a
    # season given up on THIS month is invisible to it. That made the offer and its
    # answer disagree — the row was drawn, and pressing ✓ refused it with "that
    # season isn't recorded as given up on" about a verdict plainly on screen.
    # The kind check below is what keeps the wider search safe: this route acts
    # only on an ABANDONED record, so the completed verdicts the extra step also
    # reaches are refused exactly as they were before.
    placed = await lifecycle.find_shown_season(user_id, key, season, month=month_key)
    if placed is None or placed.month is None or \
            placed.record["kind"] != distrakt_store.RecordKind.ABANDONED:
        return _month_refusal(request, "That season isn't recorded as given up on.")
    await lifecycle.take_back(user_id, key, season, month=placed.month)
    await distrakt_store.close_prompt(user_id, key, season)
    settings = await viewer.settings_for_user(user_id)
    return await _month_answer(request, user_id, year, month, settings,
                               point_at=(key, season), message="Added back")


@guard.post("/api/distrakt/verdict-readd", AuthLevel.DISTRAKT_APPROVED)
async def api_distrakt_verdict_readd(request: Request):
    """Work a season out again from what the services say now, withdrawing the
    completed verdict this month had reached.

    THE SAME MOVE A SEASON THAT GREW MAKES, and deliberately the same code:
    lifecycle.reopen withdraws the month's verdict, puts the season back on the
    viewer's list and marks it as having come back, all in one transaction so
    there is never a moment when nothing remembers it was ever finished. A second
    withdrawal path written beside that one would be a second answer to "what
    happens to a completed record that turned out not to be true", and the two
    would drift where nobody could see them side by side.

    IT IS THE ONLY WAY A SETTLED RECORD EVER RE-DERIVES ITSELF, which is the point
    of the button existing at all. The page can see that the services no longer
    back a verdict; it may not act on that, because the verdict is a record of
    what somebody watched and only they can say it was wrong.

    THE MONTH UNDER WAY AND NO OTHER. A verdict an earlier month reached is
    history — it says what that month decided, and today's watch state is no
    evidence about it — so this refuses one rather than reaching back. That refusal
    is not merely tidy: find_shown_season composes a search that walks back over
    every month that ever settled anything, so without it a key naming a season
    finished three years ago would take that year's record apart.

    THE SERVICES ARE ASKED ABOUT THIS TITLE BEFORE ANYTHING IS DECIDED, and that
    is the difference between the button working and appearing to undo itself.
    Withdrawing the verdict puts the season back on the viewer's list, where the
    very next thing that happens is the month re-deriving it from the CACHED watch
    state — so a cache still holding the pre-removal counts settles it straight
    back to completed and the row never visibly moves. Observed exactly that way,
    and confirmed by the counts coming out right when a full refresh was pressed
    first.

    A CACHED NUMBER IS NOT GOOD ENOUGH HERE WHATEVER ELSE IS FIXED. This is the
    viewer disputing what the app has just said about one season, so it is the
    control where being wrong is most visible — and a cache can go stale for
    reasons nobody has thought of yet. The reconsideration is therefore decided
    from a fresh read on principle, rather than as a workaround for any one way of
    going stale.

    ONE TITLE, ONE CALL PER SERVICE THAT CAN NAME IT. watch_history.baseline_show
    asks each admitted source about this record alone; a service with no id for it
    is not asked, and a service that cannot be read leaves its slot exactly as it
    was rather than sinking the re-add. The whole-roster re-baseline is
    deliberately not reached for — the viewer asked about one season, and asking
    about everything is what the Refresh button is.
    """
    user_id = await _distrakt_user_id(request)
    data = await authz.json_body(request)
    try:
        key, season = _row_target(data)
    except RequestError as exc:
        return _month_refusal(request, str(exc))
    month_key = _this_month()
    placed = await lifecycle.find_shown_season(user_id, key, season, month=month_key)
    if (placed is None or placed.month != month_key
            or placed.record["kind"] != distrakt_store.RecordKind.COMPLETED):
        return _month_refusal(request, "That season isn't recorded as finished this month.")
    settings = await viewer.settings_for_user(user_id)
    await watch_history.baseline_show(settings, user_id, placed.record)
    await lifecycle.reopen(user_id, placed, look_up=_season_lookup(settings))
    # The month the VIEWER is looking at is what comes back, which need not be the
    # month the verdict sat on — the same split every other row control makes.
    year, month = _viewed_month(request)
    return await _month_answer(request, user_id, year, month, settings,
                               point_at=(key, season), message="Worked out again")


@guard.post("/api/distrakt/unknown-dismiss", AuthLevel.DISTRAKT_APPROVED)
async def api_distrakt_unknown_dismiss(request: Request):
    """Say no to an episode nothing knew about, and mean it next time too.

    RECORDED PER SEASON RATHER THAN PER EPISODE. The next episode of the same
    season is the same question, and a refusal that only covered the one play
    would ask again on the very next sitting.

    ONE REFUSAL FOR ALL THREE QUESTIONS the page can ask. "Nothing here knows
    about this", "you gave this up and have gone back to it" and "your services
    have stopped backing the month's verdict that you finished this" are asked for
    different reasons, but the answer no means the same thing to all three — do not
    put this season on my list, stop asking — so it is written down once and read
    once. A separate store per question would let one of them go on asking about a
    season somebody has already refused, in the same words, from a second table.

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
        return _month_refusal(request, str(exc))
    # WHICH QUESTION WAS REFUSED IS READ FROM THE STORE, NOT FROM THE REQUEST, and
    # the discriminator is exact rather than a heuristic: a history question is
    # raised by a play and always has an outstanding row (lifecycle.
    # reconcile_history writes one before the question is ever rendered), while an
    # unbacked verdict is derived from the records on every load and never has
    # one. Asking the store therefore answers it outright — and it keeps a client
    # from being able to choose how long its own refusal lasts, which is the part
    # that should not be up for negotiation.
    asked = await _outstanding_play(user_id, key, season)
    if asked is not None:
        # NOT FOR WHAT HAS BEEN WATCHED SO FAR, and nothing more: the refusal
        # records the plays it answered, so watching more asks again.
        await distrakt_store.dismiss_prompt(
            user_id, key, season, distrakt_store.DISMISSAL_HISTORY,
            covered=await lifecycle.plays_answered(user_id, key, season, asked))
    else:
        await distrakt_store.dismiss_prompt(user_id, key, season,
                                            distrakt_store.DISMISSAL_VERDICT)
    # TWO WRITES BECAUSE THEY SAY DIFFERENT THINGS. The dismissal is the refusal
    # itself — permanent for a verdict, a watermark for a history question.
    # Closing the open prompt retires THIS outstanding question. A decline needs
    # both: without the first the same play asks again, and without the second the
    # row lingers until something else notices it was answered.
    await distrakt_store.close_prompt(user_id, key, season)
    # The ROW asked to be swapped out (outerHTML on the question it sits in), so
    # an empty answer removes it; a refusal is a line out of band and leaves the
    # question standing rather than losing it to a write that never happened.
    return Response(status_code=200)


@guard.post("/api/distrakt/unknown-snooze", AuthLevel.DISTRAKT_APPROVED)
async def api_distrakt_unknown_snooze(request: Request):
    """Put a question raised by a play off until the month turns.

    THE SAME REFUSAL AS ✗ WITH A DIFFERENT END. ✗ covers the plays it was made
    about and asks again about the next one; this covers every play in the
    viewer's month it was made in — see store.DISMISSAL_MONTH — which is what
    somebody means by "not this month" about an episode they keep watching again.

    ONLY A QUESTION A PLAY RAISED, read from the store as unknown-dismiss reads it.
    An unbacked verdict is a standing condition rather than a viewing, so a month
    has nothing to lapse against and it is refused rather than written as some
    other kind.
    """
    user_id = await _distrakt_user_id(request)
    data = await authz.json_body(request)
    try:
        key, season = _row_target(data)
    except RequestError as exc:
        return _month_refusal(request, str(exc))
    if await _outstanding_play(user_id, key, season) is None:
        return _month_refusal(request, "That question is no longer outstanding.")
    await distrakt_store.dismiss_prompt(user_id, key, season,
                                        distrakt_store.DISMISSAL_MONTH)
    await distrakt_store.close_prompt(user_id, key, season)
    return Response(status_code=200)


async def _rewatch_target(user_id: int, key: ItemKey, season: int
                          ) -> tuple[dict, lifecycle.Placement] | str:
    """`(the play that raised a re-watch offer, the finished season it is about)`,
    or the sentence refusing it.

    BOTH HALVES ARE READ BACK, NEITHER IS TAKEN FROM THE REQUEST: the play from
    the open question (so its day is the one the offer named) and the verdict
    from wherever it stands — which has to be an earlier month's COMPLETED record,
    the only thing a re-watch offer is ever raised about.
    """
    asked = await _outstanding_play(user_id, key, season)
    if asked is None:
        return "That question is no longer outstanding."
    month_key = _this_month()
    placed = await lifecycle.find_shown_season(user_id, key, season, month=month_key)
    if (placed is None or placed.month is None or placed.month >= month_key
            or placed.record["kind"] != distrakt_store.RecordKind.COMPLETED):
        return "That season isn't recorded as finished in an earlier month."
    return asked, placed


@guard.post("/api/distrakt/rewatch-start", AuthLevel.DISTRAKT_APPROVED)
async def api_distrakt_rewatch_start(request: Request):
    """Say yes to a re-watch: the season goes on the viewer's list counting from
    the day of the play that raised the offer.

    THE EARLIER MONTH KEEPS ITS RECORD. That viewing happened and was finished;
    this is a second one beside it — the same shape a re-watch added by search
    takes (api_distrakt_add, with a floor), and the same two writes: the roster
    row, then the floor, both before the month is recomputed so the recompute
    counts only this run.

    THE FLOOR IS THE PLAY'S OWN DAY, in the viewer's zone, and not a day the page
    sends. The offer named it, and the question's stored play is where it lives.
    """
    user_id = await _distrakt_user_id(request)
    data = await authz.json_body(request)
    try:
        key, season = _row_target(data)
    except RequestError as exc:
        return _month_refusal(request, str(exc))
    target = await _rewatch_target(user_id, key, season)
    if isinstance(target, str):
        return _month_refusal(request, target)
    asked, placed = target
    settings = await viewer.settings_for_user(user_id)
    floor = watch_history.local_day(asked["watched_at"], watch_history.viewer_tz(settings))
    if not floor:
        return _month_refusal(request, "That play doesn't say when it was watched.")
    await distrakt_store.clear_prompt_dismissal(user_id, key, season)
    await lifecycle.follow(user_id, {**placed.record, "season": season, "watched": 0,
                                     "abandoned_form": None})
    await distrakt_store.set_history_from(user_id, key, season, floor)
    await distrakt_store.close_prompt(user_id, key, season)
    year, month = _viewed_month(request)
    return await _month_answer(request, user_id, year, month, settings,
                               point_at=(key, season), message="Re-watch started")


@guard.post("/api/distrakt/rewatch-complete", AuthLevel.DISTRAKT_APPROVED)
async def api_distrakt_rewatch_complete(request: Request):
    """Say yes to a finale watched again: the season is recorded completed in the
    month that play fell in, beside the earlier month's record.

    ONLY FOR A PLAY OF THE FINALE, checked against the verdict rather than taken
    from which button was pressed — the offer is only drawn for one, and this
    refuses anything else rather than filing a season as finished on the strength
    of one of its middle episodes.
    """
    user_id = await _distrakt_user_id(request)
    data = await authz.json_body(request)
    try:
        key, season = _row_target(data)
    except RequestError as exc:
        return _month_refusal(request, str(exc))
    target = await _rewatch_target(user_id, key, season)
    if isinstance(target, str):
        return _month_refusal(request, target)
    asked, placed = target
    total = int(placed.record.get("total") or 0)
    if not total or int(asked["number"]) != total:
        return _month_refusal(request, "That play wasn't the season's finale.")
    settings = await viewer.settings_for_user(user_id)
    day = watch_history.local_day(asked["watched_at"], watch_history.viewer_tz(settings))
    if not day:
        return _month_refusal(request, "That play doesn't say when it was watched.")
    await _file_finished(user_id, day[:7], {**placed.record, "season": season}, placed.record)
    await distrakt_store.close_prompt(user_id, key, season)
    year, month = _viewed_month(request)
    return await _month_answer(request, user_id, year, month, settings,
                               point_at=(key, season), message="Recorded as completed")


async def _live_form_source(settings, record: dict, season: int) -> dict:
    """`record` merged with what the provider says about it right now, for the
    abandoned form that is about to be frozen.

    Degrades to the stored record rather than failing. Giving up is a user ACTION
    and must still succeed — it is not a read to fail on — so an unreachable
    provider costs the line its live counts and nothing else.

    TWO HALVES, TWO DIFFERENT QUESTIONS, AND ONLY ONE OF THEM IS PUBLIC. How long
    the season is, is catalogue data and is asked of whoever the record's own ids
    name (`live.season_detail`) — a form frozen on an instance that reads its
    catalogue from somewhere other than Trakt used to carry the stored record and
    nothing fresh at all. How much of it THIS VIEWER has watched is their own
    data, and it is still read from Trakt alone, behind the private
    `trakt_configured` gate that is genuinely entitled to guard it: a per-source
    answer to that needs the watch-history state this path does not hold, which
    is a different piece of work from swapping a season lookup. Where it cannot
    be read the record keeps its OWN `watched`, which is the number the row the
    viewer just pressed was drawing.
    """
    rec = {**record, "season": season}
    try:
        detail = await live.season_detail(settings, rec)
    except SourceUnavailable:
        return dict(record)
    form = {**record, **detail, "season": season}
    source_id = (record.get("ids") or {}).get("trakt")
    if not (settings and getattr(settings, "trakt_configured", False)) or source_id is None:
        return form
    try:
        watched = await trakt_sync.fetch_watched_map(settings, [source_id])
    except TraktError:
        return form
    # fetch_watched_map answers in Trakt's own (id, season) terms, so this one
    # count is re-filed under the shared identity the record is keyed by.
    return {**form, "watched": watched.get((int(source_id), season), 0)}


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


# The phrase a restore has to be confirmed with. A phrase that has to be READ
# and copied cannot be cleared by muscle memory the way a confirm dialog can, and
# a restore REPLACES this account's tracked data rather than merging into it.
RESTORE_ACK = "REPLACE MY DATA"


def _restore_answer(request: Request, message: str, ok: bool = False) -> Response:
    """The restore panel's one line, redrawn.

    A REFUSAL IS 200 WITH A SENTENCE, like every other drawn refusal here: a
    status code is never swapped (static/js/boost.js's `noSwap`), so a 400 would
    be a button that silently did nothing.
    """
    return templates.TemplateResponse(request, "_distrakt_restore.html", {
        "request": request, "message": message, "ok": ok,
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
    body = await authz.json_body(request)
    # THE DOCUMENT ARRIVES AS ONE FIELD OF THE FORM, which is what lets the whole
    # restore be a form. The browser reads the chosen file — the one thing only it
    # can do, because app/authz.py's shape guard admits application/json alone and
    # a cross-origin multipart upload is exactly what that rule exists to refuse —
    # and puts the text in a hidden input. The typed phrase, the submit and the
    # answer are all markup.
    try:
        data = json.loads(str(body.get("payload") or ""))
    except ValueError:
        return _restore_answer(request, "That file isn't readable JSON.")
    if not isinstance(data, dict):
        return _restore_answer(request, "That file is not a tracker export.")
    # THE PHRASE IS CHECKED HERE TOO, not only by the form's own `pattern`. A
    # browser validation is a courtesy to the person typing; this is the one thing
    # standing between a mis-aimed request and somebody's whole tracker, and it
    # used to be checked nowhere but in the page.
    if str(body.get("ack") or "").strip() != RESTORE_ACK:
        return _restore_answer(request, f"Type {RESTORE_ACK} to confirm.")
    try:
        await distrakt_store.restore_user_data(user_id, data)
    except distrakt_store.RestoreError as exc:
        return _restore_answer(request, str(exc))
    except db.DatabaseError as exc:
        logger.warning("distrakt restore failed for user %s", user_id, exc_info=True)
        return _restore_answer(request, f"Could not restore this file: {exc}")
    months = await distrakt_store.list_months(user_id)
    return _and_the_month(_restore_answer(
        request, f"Restored {len(months)} month(s).", ok=True))


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
        return _month_refusal(request, "Nothing to update")
    try:
        await share_links.set_post_link(user_id, kind=kind, endpoint=endpoint)
    except ValueError as exc:
        return _month_refusal(request, str(exc))
    # THE MONTH IS THE ANSWER, because the link is rendered INTO the announcement
    # post: which form it takes and which view it opens on are visible only in
    # the copy block, and the copy blocks ride with the month. It used to be two
    # requests — save, then reload the month — with the selectors rebuilt in
    # between from a payload that was "two shapes of the same answer".
    year, month = _viewed_month(request)
    return await _month_answer(request, user_id, year, month,
                               await viewer.settings_for_user(user_id))
