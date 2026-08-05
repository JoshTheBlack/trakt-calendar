"""The reads that belong to WHOSE TOKEN ASKED: watch history, per-show
progress, ratings, and the activity beacon that gates a sync.

Separate from detail.py because these answers are personal. None of them may be
written to the URL-keyed shared cache — two accounts asking the same question
send the identical URL — which is why every call here passes `private=True` or
goes through send directly. That is also the whole of what a second provider
has to supply before the tracker can be backed by it, so it is one module
rather than a handful of functions scattered through the client.

A FAILURE IS NEVER NORMALIZED INTO AN EMPTY ANSWER. Everything here reads one
person's viewing, and an empty answer is a DESTRUCTIVE one — the tracker retires
the seasons a source no longer reports — so "I could not read this" and "there is
nothing here" must not arrive in the same shape. Three rules follow, and they are
the same three the other source's port already keeps:

  A REFUSED CREDENTIAL RAISES, IMMEDIATELY AND EVERYWHERE. It is a statement
  about every request that token will make, not about the call that happened to
  be placed first, so a fan-out that tolerates one show failing must not tolerate
  this once per show. See transport.is_credential_failure.

  A READ THAT READ NOTHING IS NOT AN EMPTY READ. A beacon that could not be
  fetched is not four absent timestamps — an empty blob compares EQUAL to a
  stored empty one and gates the next sync as unchanged, so a source that went
  down would report itself up to date for as long as it stayed down. A history
  sweep that lost a page is not a sweep that found no plays, because the cursor
  moves past whatever it did not see and those plays are never asked for again.

  A SHOW THE READ COULD NOT REACH IS ABSENT FROM THE ANSWER, not present with
  nothing watched. The two are indistinguishable to the caller once flattened,
  and the second retires that show's stored seasons.

The whole point of the three is that the tracker degrades a source that raises —
it names the service in the page's notice and leaves its stored rows exactly as
they were — which is the honest version of the outcome a swallowed failure fakes.
"""
from __future__ import annotations

import asyncio
import logging
import time as _time
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

import httpx

from ...config import Settings
from ...perftrace import span
from ..base import PlayCounts, collect_ids
from . import transport
from .transport import TraktError

logger = logging.getLogger(__name__)
_perf = logging.getLogger("app.perf")


async def fetch_watched_map(settings: Settings, trakt_ids) -> dict[tuple[int, int], int]:
    """Per-season watched-episode counts (the live `x`), keyed {(trakt_id, season):
    completed} — via ONE /shows/{id}/progress/watched call per UNIQUE show.

    Why not the aggregate /sync/watched/shows? Measured against the live API, it
    returns show-level rows (plays + show, capped at 250 a page) WITHOUT the
    seasons[]/episodes[] breakdown — with pagination headers and without them,
    and with every `extended` variant — so every count came back 0. The per-show
    progress endpoint is the authoritative source of a user's season completion
    and always includes `seasons[].completed`. What the show-level rows ARE good
    for is `plays`, which is fetch_play_counts's business.

    Never cached — both because `x` is live and because a progress record belongs
    to whoever's token asked for it, and the cache is keyed by URL alone. One
    shared httpx client pools the fan-out. A show that could not be read
    contributes no keys and its caller falls back to the record it already had; a
    refused CREDENTIAL raises, because that is not one show failing."""
    unique = sorted({int(t) for t in trakt_ids if t is not None})
    if not unique:
        return {}
    params = {"hidden": "false", "specials": "false", "count_specials": "false"}
    client = transport.shared_client()
    results = await asyncio.gather(*(
        _progress_record(settings, tid, client=client) for tid in unique
    ))
    lookup: dict[tuple[int, int], int] = {}
    for tid, res in zip(unique, results):
        if not isinstance(res, dict):
            continue
        for season in res.get("seasons") or []:
            num = season.get("number")
            if num is None:
                continue
            lookup[(tid, int(num))] = int(season.get("completed") or 0)
    logger.info("fetch_watched_map: %d show(s) -> %d (trakt_id,season) key(s)", len(unique), len(lookup))
    return lookup


# ---------------------------------------------------------------------------
# Incremental watch-history cache primitives (app/distrakt/watch_history.py).
# The cache baselines a show once (progress -> completed episode numbers), then applies
# only NEW plays from /users/me/history since the last sync, gated by
# /sync/last_activities. Movies come through the same history sweep.
# ---------------------------------------------------------------------------

async def fetch_last_activities(settings: Settings) -> dict:
    """/sync/last_activities -> the small per-type "last changed at" beacon blob
    (fixed size, independent of library size). Used to gate the history sync.

    A BEACON THAT COULD NOT BE READ RAISES, and is never answered with an empty
    blob. The caller compares this against what it stored last time to decide
    whether anything has moved, and an empty answer is not "no beacon" — it is the
    claim that all four stamps are absent, which compares EQUAL to a stored empty
    one and gates the sync as unchanged. A refused token would then have this
    source report itself up to date for as long as it stayed refused, and the
    whole pass would be built on that: the history pull would come back empty for
    the same reason, no re-baseline would run, and the page would render stored
    numbers as though they had just been confirmed.
    """
    res = await transport.cached_get(
        transport.shared_client(), settings, "sync/last_activities", {},
        private=True, raise_errors=True)
    # None here is not a failure: raise_errors=True means a refusal has already
    # raised, so this is Trakt answering with a body that held nothing to read.
    return res if isinstance(res, dict) else {}


async def _progress_record(settings: Settings, trakt_id,
                           client: httpx.AsyncClient | None = None):
    """One /shows/{id}/progress/watched body, or None when THIS SHOW could not be
    read.

    None is deliberately not `{}`. An empty progress record is a real answer —
    this person has watched none of this show — and the tracker acts on it by
    retiring the seasons it had stored. A show that 500s or vanishes has said
    nothing at all, and flattening the two would delete counts over a transient
    failure, one show at a time and with nothing on the page to say so.

    A REFUSED CREDENTIAL IS NOT A PER-SHOW FAILURE AND IS RE-RAISED. It is a
    statement about every request this token will make, so tolerating it here
    would tolerate it once for each show in the roster and compose a hundred and
    forty-six refusals into "you have watched nothing".
    """
    params = {"hidden": "false", "specials": "false", "count_specials": "false"}
    c = client or transport.shared_client()
    try:
        return await transport.cached_get(
            c, settings, f"shows/{trakt_id}/progress/watched", params,
            private=True, raise_errors=True)
    except TraktError as exc:
        if transport.is_credential_failure(exc):
            raise
        # transport has already logged the status; this says what was lost.
        logger.warning("progress record for show %s could not be read: %s", trakt_id, exc)
        return None


def _seasons_from_progress(res) -> dict[int, dict[int, str]]:
    """A progress body as {season: {episode: watched_at}}.

    `last_watched_at` is carried per episode rather than discarded because WHEN a
    season was finished is what decides which month records it as finished: the
    date of its last episode names that month, and a season finished in July is
    July's whatever month the reader happens to be looking at. It is "" when Trakt
    reports an episode as completed without a timestamp, which reads as "date
    unknown" everywhere downstream and never as a date.
    """
    out: dict[int, dict[int, str]] = {}
    if isinstance(res, dict):
        for season in res.get("seasons") or []:
            num = season.get("number")
            if num is None:
                continue
            eps: dict[int, str] = {}
            for e in (season.get("episodes") or []):
                if not e.get("completed") or e.get("number") is None:
                    continue
                eps[int(e["number"])] = str(e.get("last_watched_at") or "")
            out[int(num)] = dict(sorted(eps.items()))
    return out


async def fetch_show_progress_detail(settings: Settings, trakt_id,
                                     client: httpx.AsyncClient | None = None):
    """/shows/{id}/progress/watched -> {season_number: {episode_number: watched_at}},
    or None when this show could not be read.
    The per-show baseline: authoritative, deduped completion straight from Trakt.
    Never cached — this is one person's viewing, and the cache key is the URL.
    Pass a shared `client` when batching.
    """
    res = await _progress_record(settings, trakt_id, client=client)
    return None if res is None else _seasons_from_progress(res)


async def fetch_progress_details(settings: Settings,
                                 show_ids) -> dict[int, dict[int, dict[int, str]]]:
    """fetch_show_progress_detail for several shows at once, as
    {trakt_id: {season: {episode: watched_at}}}.

    The fan-out lives here rather than in the caller because the pooled client is
    this package's business: the tracker baselines a whole roster in one go and
    has no reason to hold an httpx client to do it. Ids are de-duplicated, so a
    roster carrying two seasons of one show costs one call.

    A SHOW THAT COULD NOT BE READ IS ABSENT FROM THE ANSWER rather than present
    with an empty record, which is the protocol's way of saying "I have nothing to
    tell you about this one". Its caller leaves what it already knew alone. A
    refused credential is not one show failing and propagates, which the tracker
    degrades per source: this service is named on the page and every one of its
    stored rows is left standing.
    """
    unique = list(dict.fromkeys(int(t) for t in show_ids if t is not None))
    if not unique:
        return {}
    client = transport.shared_client()
    # ONE CALL PER SHOW, and the count is on the line because that is the number
    # that explains the duration: the fan-out is issued all at once but paced by
    # the outbound rate gate, so this scales with the roster divided by that
    # concurrency, not with the network. A caller wondering why a re-baseline took
    # four seconds wants to see how many shows it asked about.
    with span("trakt.progress_details", n=len(unique)):
        details = await asyncio.gather(*(
            fetch_show_progress_detail(settings, tid, client=client) for tid in unique
        ))
    return {tid: seasons for tid, seasons in zip(unique, details) if seasons is not None}


async def fetch_play_counts(settings: Settings, limit: int = 250,
                            max_pages: int = 40) -> PlayCounts:
    """/sync/watched/shows -> {trakt id: plays}, and whether the whole listing was
    read (app/providers/base.py's PlayCounts).

    WHAT THIS ENDPOINT IS ACTUALLY GOOD FOR. It carries no seasons[] and no
    episodes[] in any variant — see api_headers, where the measurement that
    settled that is written out — so it cannot answer what anyone has watched.
    What each row DOES carry is `plays`, and `plays` tracks the watched set in
    BOTH directions: measured live, removing a season's plays took a show from 20
    to 10 and re-marking them took it back to 20. So a sweep of this is a change
    detector for the whole library at five calls, against one call per title for
    asking properly.

    `last_updated_at` IS ON EVERY ROW AND MUST NOT BE USED FOR THIS. In the same
    measurement it stayed put through the removal and moved only on the addition,
    so a detector keyed on it misses every unwatch — which is the defect this is
    here to catch, one layer down and harder to see.

    A SHOW WITH NO PLAYS LEFT VANISHES FROM THE LISTING ENTIRELY rather than
    appearing with zero, which is why the caller compares against what it stored
    rather than reading this map alone.

    Paginated by query parameter, following x-pagination-page-count. A page that
    could not be read raises if it is the first — a sweep that read nothing is not
    a library that holds nothing — and otherwise makes the sweep incomplete, which
    the caller reads as "may not conclude anything from an absence".
    """
    counts: dict[str, int] = {}
    page = 1
    complete = True
    client = transport.shared_client()
    with span("trakt.play_counts"):
        while page <= max_pages:
            params = {"limit": str(limit), "page": str(page)}
            url = f"{transport.API_BASE}/sync/watched/shows?{urlencode(params)}"
            t0 = _time.perf_counter()
            try:
                resp = await transport.send(
                    client, "GET", url, headers=transport.api_headers(settings, paginate=False))
            except httpx.HTTPError as exc:
                logger.warning("fetch_play_counts: request failed: %s", exc)
                raise TraktError(f"Could not reach Trakt: {exc}") from exc
            _perf.debug("netGET    sync/watched/shows?page=%s -> %s  %.0fms", page,
                        resp.status_code, (_time.perf_counter() - t0) * 1000.0)
            if resp.status_code != 200:
                logger.warning("fetch_play_counts: HTTP %s: %s", resp.status_code, resp.text[:200])
                if page == 1 or resp.status_code in transport.CREDENTIAL_STATUSES:
                    raise TraktError(
                        f"Trakt API returned HTTP {resp.status_code}.", resp.status_code)
                # A LATER PAGE IS SURVIVABLE AND THE FIRST ONE IS NOT. Losing a
                # page costs the titles on it a needless re-read next time, which
                # is a cost; losing the whole listing would say every title has
                # lost its plays, which is a wrong answer.
                complete = False
                break
            try:
                batch = resp.json()
            except ValueError:
                raise TraktError("Trakt API returned an unreadable response.") from None
            if not isinstance(batch, list) or not batch:
                break
            for entry in batch:
                if not isinstance(entry, dict):
                    continue
                trakt_id = ((entry.get("show") or {}).get("ids") or {}).get("trakt")
                if trakt_id is None:
                    continue
                counts[str(trakt_id)] = int(entry.get("plays") or 0)
            try:
                page_count = int(resp.headers.get("x-pagination-page-count") or 1)
            except (TypeError, ValueError):
                page_count = 1
            if page >= page_count:
                break
            page += 1
        else:
            # Ran out of pages to fetch rather than out of pages to read. Saying so
            # is what keeps the cap from quietly becoming "the rest of the library
            # has no plays".
            complete = False
    logger.info("fetch_play_counts: %d show(s) over %d page(s), complete=%s",
                len(counts), page, complete)
    return PlayCounts(counts=counts, complete=complete)


async def fetch_history(settings: Settings, start_at: str | None = None,
                        limit: int = 100, max_pages: int = 50) -> list[dict]:
    """/users/me/history (ALL types) -> chronological watch EVENTS, newest first,
    optionally since `start_at` (YYYY-MM-DD). Pages via ?page/?limit, following the
    X-Pagination-Page-Count header. Each event is an episode or movie play; the
    caller dedupes. `start_at` at day granularity means each sync may re-see the
    day's earlier events — harmless, since applying them is idempotent.

    A PAGE THAT COULD NOT BE READ RAISES RATHER THAN ENDING THE SWEEP EARLY, and
    that is not caution for its own sake: the caller advances its cursor past the
    window this call covered, so plays on a page that was never fetched are never
    asked for again. The old behaviour — log, stop, and return whatever had
    arrived — reported a refused read as "nothing happened", which on a refused
    token meant a page that looked perfectly healthy while no history was being
    read at all. A source that raises is named on the page and its cursor stays
    where it was, so the next load asks for the same window again.

    A 200 CARRYING NO EVENTS IS A DIFFERENT THING AND STILL ENDS THE SWEEP
    NORMALLY. That is Trakt saying there is nothing more, and it is the ordinary
    answer for an account that has watched nothing since the cursor.
    """
    events: list[dict] = []
    page = 1
    client = transport.shared_client()
    while page <= max_pages:
        params = {"limit": str(limit), "page": str(page)}
        if start_at:
            params["start_at"] = start_at
        url = f"{transport.API_BASE}/users/me/history?{urlencode(params)}"
        t0 = _time.perf_counter()
        try:
            resp = await transport.send(client, "GET", url, headers=transport.api_headers(settings, paginate=False))
        except httpx.HTTPError as exc:
            logger.warning("fetch_history: request failed: %s", exc)
            raise TraktError(f"Could not reach Trakt: {exc}") from exc
        _perf.debug("netGET    users/me/history?page=%s -> %s  %.0fms", page,
                    resp.status_code, (_time.perf_counter() - t0) * 1000.0)
        if resp.status_code != 200:
            logger.warning("fetch_history: HTTP %s: %s", resp.status_code, resp.text[:200])
            if resp.status_code in transport.CREDENTIAL_STATUSES:
                raise TraktError(
                    "Trakt rejected the credentials (%s). The link has to be made "
                    "again." % resp.status_code, resp.status_code)
            raise TraktError(f"Trakt API returned HTTP {resp.status_code}.", resp.status_code)
        try:
            batch = resp.json()
        except ValueError:
            raise TraktError("Trakt API returned an unreadable response.") from None
        if not isinstance(batch, list) or not batch:
            break
        events.extend(batch)
        try:
            page_count = int(resp.headers.get("x-pagination-page-count") or 1)
        except (TypeError, ValueError):
            page_count = 1
        if page >= page_count:
            break
        page += 1
    logger.info("fetch_history(start_at=%s): %d event(s) over %d page(s)", start_at, len(events), page)
    return events


async def fetch_watched_progress(settings: Settings, since_days: int | None = 60) -> list[dict]:
    """Recently-active seasons from watch HISTORY (/users/me/history), as
    [{ids, season, watched, title, network}].

    Uses the history event log rather than /sync/watched/shows — that aggregate
    returns show-level rows WITHOUT seasons for some accounts (the same bug that
    zeroed the watch counts), so it surfaced no candidates. `start_at` bounds it
    to the last `since_days` (a date). `watched` here is the count of DISTINCT
    episodes seen in that window per (show, season) — a recency signal; the
    caller checks it against the season total to decide in-progress vs completed.
    """
    start_at = None
    if since_days is not None:
        start_at = (datetime.now(timezone.utc).date() - timedelta(days=since_days)).isoformat()
    out = watched_progress_from(await fetch_history(settings, start_at=start_at))
    logger.info("fetch_watched_progress(since_days=%s) -> %d recent season(s) from history", since_days, len(out))
    return out


def watched_progress_from(events: list[dict]) -> list[dict]:
    """The aggregation half of fetch_watched_progress, over events already in
    hand: [{ids, season, watched, title, network}].

    Split out so a caller that needs BOTH the seasons and the movies from one
    window (app/distrakt/backfill.py) can sweep the history once and read it twice,
    rather than paying for the same paged sweep twice over.

    THE WHOLE ID MAP TRAVELS, not the two ids the first caller happened to need:
    the tracker files a row under whichever shared id it can, so dropping the rest
    here would decide that question on its behalf, and they cost nothing — Trakt
    has already sent them.
    """
    agg: dict[tuple[int, int], dict] = {}
    for ev in events:
        if ev.get("type") != "episode":
            continue
        show = ev.get("show") or {}
        ep = ev.get("episode") or {}
        ids = show.get("ids") or {}
        tid, season, num = ids.get("trakt"), ep.get("season"), ep.get("number")
        if tid is None or season is None or int(season) == 0:  # skip specials
            continue
        rec = agg.setdefault((int(tid), int(season)), {
            "eps": set(), "ids": collect_ids(ids),
            "title": show.get("title") or "", "network": show.get("network") or "",
        })
        if num is not None:
            rec["eps"].add(int(num))
    return [{
        "ids": rec["ids"], "season": season, "watched": len(rec["eps"]),
        "title": rec["title"], "network": rec["network"],
    } for (_tid, season), rec in agg.items()]


def movie_plays_from(events: list[dict]) -> list[dict]:
    """The film plays in a history sweep, as [{ids, title, year, watched_at}].

    The film counterpart to watched_progress_from, and here for the same reason:
    knowing which key of an event holds a film and where its ids sit is knowledge
    about Trakt's payload, and the tracker should not have to carry it.
    """
    out: list[dict] = []
    for event in events:
        if event.get("type") != "movie":
            continue
        movie = event.get("movie") or {}
        ids = collect_ids(movie.get("ids") or {})
        if not ids:
            continue
        out.append({"ids": ids, "title": movie.get("title") or "",
                    "year": movie.get("year"), "watched_at": str(event.get("watched_at") or "")})
    return out


async def fetch_ratings(settings: Settings) -> list[dict]:
    """/sync/ratings -> everything the token's owner has rated, shows and movies
    together, as Trakt's own entries ({type, rating, rated_at, show|movie}).

    PRIVATE TO WHOEVER'S TOKEN ASKED, so it is never written to the shared
    URL-keyed cache — two accounts asking send the identical URL and would
    otherwise be served each other's ratings.

    Pagination headers are deliberately NOT sent: this endpoint returns the
    whole set in one response, and asking for a page would silently cap a large
    library at the pagination limit with nothing in the response to say so.
    """
    url = f"{transport.API_BASE}/sync/ratings?{urlencode({'extended': 'full'})}"
    resp = await transport.send(transport.shared_client(), "GET", url,
                                 headers=transport.api_headers(settings, paginate=False))
    if resp.status_code == 401:
        raise TraktError("Trakt rejected the credentials (401).", 401)
    if resp.status_code != 200:
        raise TraktError(f"Trakt API returned HTTP {resp.status_code}.", resp.status_code)
    try:
        data = resp.json()
    except ValueError:
        raise TraktError("Trakt API returned an unreadable response.") from None
    entries = data if isinstance(data, list) else []
    logger.info("fetch_ratings -> %d rated item(s)", len(entries))
    return entries
