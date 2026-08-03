"""The reads that belong to WHOSE TOKEN ASKED: watch history, per-show
progress, ratings, and the activity beacon that gates a sync.

Separate from detail.py because these answers are personal. None of them may be
written to the URL-keyed shared cache — two accounts asking the same question
send the identical URL — which is why every call here passes `private=True` or
goes through send directly. That is also the whole of what a second provider
has to supply before the tracker can be backed by it, so it is one module
rather than a handful of functions scattered through the client.
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
from ..base import collect_ids
from . import transport
from .transport import TraktError

logger = logging.getLogger(__name__)
_perf = logging.getLogger("app.perf")


async def fetch_watched_map(settings: Settings, trakt_ids) -> dict[tuple[int, int], int]:
    """Per-season watched-episode counts (the live `x`), keyed {(trakt_id, season):
    completed} — via ONE /shows/{id}/progress/watched call per UNIQUE show.

    Why not the aggregate /sync/watched/shows? An audit showed it returning
    show-level rows (plays + show, capped ~100/page) WITHOUT the seasons[]/
    episodes[] breakdown — paginated or not — so every count came back 0. The
    per-show progress endpoint is the authoritative source of a user's season
    completion and always includes `seasons[].completed`.

    Never cached — both because `x` is live and because a progress record belongs
    to whoever's token asked for it, and the cache is keyed by URL alone. One
    shared httpx client pools the fan-out. Errored/absent shows just contribute
    no keys (that show renders 0)."""
    unique = sorted({int(t) for t in trakt_ids if t is not None})
    if not unique:
        return {}
    params = {"hidden": "false", "specials": "false", "count_specials": "false"}
    client = transport.shared_client()
    results = await asyncio.gather(*(
        transport.cached_get(client, settings, f"shows/{tid}/progress/watched", params, private=True)
        for tid in unique
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
    (fixed size, independent of library size). Used to gate the history sync."""
    res = await transport.cached_get(
        transport.shared_client(), settings, "sync/last_activities", {}, private=True)
    return res if isinstance(res, dict) else {}


async def fetch_show_progress_detail(settings: Settings, trakt_id,
                                     client: httpx.AsyncClient | None = None) -> dict[int, dict[int, str]]:
    """/shows/{id}/progress/watched -> {season_number: {episode_number: watched_at}}.
    The per-show baseline: authoritative, deduped completion straight from Trakt.
    Never cached — this is one person's viewing, and the cache key is the URL.
    Pass a shared `client` when batching.

    `last_watched_at` is carried per episode rather than discarded because WHEN a
    season was finished is what decides which month records it as finished: the
    date of its last episode names that month, and a season finished in July is
    July's whatever month the reader happens to be looking at. It is "" when Trakt
    reports an episode as completed without a timestamp, which reads as "date
    unknown" everywhere downstream and never as a date.
    """
    params = {"hidden": "false", "specials": "false", "count_specials": "false"}
    c = client or transport.shared_client()
    res = await transport.cached_get(c, settings, f"shows/{trakt_id}/progress/watched", params, private=True)
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


async def fetch_progress_details(settings: Settings,
                                 show_ids) -> dict[int, dict[int, dict[int, str]]]:
    """fetch_show_progress_detail for several shows at once, as
    {trakt_id: {season: {episode: watched_at}}}.

    The fan-out lives here rather than in the caller because the pooled client is
    this package's business: the tracker baselines a whole roster in one go and
    has no reason to hold an httpx client to do it. Ids are de-duplicated, so a
    roster carrying two seasons of one show costs one call.
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
    return dict(zip(unique, details))


async def fetch_history(settings: Settings, start_at: str | None = None,
                        limit: int = 100, max_pages: int = 50) -> list[dict]:
    """/users/me/history (ALL types) -> chronological watch EVENTS, newest first,
    optionally since `start_at` (YYYY-MM-DD). Pages via ?page/?limit, following the
    X-Pagination-Page-Count header. Each event is an episode or movie play; the
    caller dedupes. `start_at` at day granularity means each sync may re-see the
    day's earlier events — harmless, since applying them is idempotent."""
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
            break
        _perf.debug("netGET    users/me/history?page=%s -> %s  %.0fms", page,
                    resp.status_code, (_time.perf_counter() - t0) * 1000.0)
        if resp.status_code != 200:
            logger.warning("fetch_history: HTTP %s: %s", resp.status_code, resp.text[:200])
            break
        try:
            batch = resp.json()
        except ValueError:
            break
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
