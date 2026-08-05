"""The reads that belong to WHOSE TOKEN ASKED: the change beacon, the watch
history, and the per-season progress record behind a completion count.

Separate from detail.py because these answers are personal. The response cache is
keyed by the URL and shared by the whole instance, and Simkl carries the token in
a HEADER — so every account's /sync/ request has the IDENTICAL URL, and one
written to the cache without `private=True` would be served straight back to the
wrong person. Every call in this module passes it, and everything here goes
through SYNC_POOL, which admits one request at a time because Simkl's docs
prohibit parallel requests off their Cloudflare-cached paths.

THREE THINGS THIS MODULE NORMALIZES AT THE BOUNDARY, so nothing downstream learns
Simkl's payload shapes:

  THE BEACON. `fetch_last_activities` answers in the shape SyncPort declares —
  episode and movie, watched and removed — rather than in Simkl's own per-list
  timestamps. The tracker gates a sync on that blob and must not have to know
  that one service files anime separately from television.

  THE EVENTS. Simkl publishes no per-play event log; it publishes a LIBRARY, and
  `?episode_watched_at=yes` puts a timestamp on each episode inside it. Flattening
  that into the same {type, show|movie, episode, watched_at} events Trakt's
  history returns is this module's job, because the alternative is the tracker
  holding two readers for one idea.

  THE LIBRARY ITSELF, KEYED BY THE SHARED TITLE IDENTITY. That the library is a
  complete watch record and not only a recency signal is what lets a caller
  baseline from it without ever naming a Simkl id — see fetch_library, and see
  app/providers/base.py for the identity waterfall it runs the ids through, which
  is not restated here. What this module contributes is which field carries which
  id and how the lists are spelled; the rule about what makes two titles the same
  title lives in one place and this is not it.

NOTHING HERE WRITES TO SIMKL. `POST /sync/history` exists and is deliberately
never called: this app reads a person's viewing and never edits it.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

from ...config import Settings
from ...perftrace import span
from ..base import LibraryEntry, LibraryRead, Media, collect_ids, resolve_key
from . import transport

logger = logging.getLogger(__name__)

# The library buckets a watch history can be in. Simkl files an item under
# exactly one, and a season somebody is part-way through sits in `watching`
# while a finished one sits in `completed` — so asking only about `completed`
# would lose every season in progress, which is most of what the tracker counts.
# `plantowatch` is excluded because nothing in it has been watched.
WATCHED_STATUSES = ("watching", "completed", "hold", "dropped")

# The catalogues an episode can come from. Simkl keeps anime apart from
# television at every endpoint, and a person's anime is watch history exactly as
# much as their television is — omitting it would silently under-count the half
# of Simkl's library it is best at.
EPISODE_TYPES = ("shows", "anime")

# The catalogues a library read covers, in the order it reads them. Films are
# read here as well as shows, because a play is a play whichever kind of title it
# is on; only the SHOW half becomes a keyed library entry (see fetch_library).
LIBRARY_TYPES = (*EPISODE_TYPES, "movies")

# What /sync/activities calls each catalogue, against what /sync/all-items is
# asked for. Television is `tv_shows` in one and `shows` in the other, and that
# mismatch is exactly the kind of provider-local spelling that must not leak
# upward — the beacon this module returns is already normalized, and this table
# is what keeps the two halves of the same fact in one place.
ACTIVITY_LISTS = {"shows": "tv_shows", "anime": "anime", "movies": "movies"}

# How many POSTs one progress read may cost. `POST /sync/watched` is batched, so
# a whole roster is normally ONE request — but the cap on a POST is one per
# second, so an unbounded body on a very large roster would be one slow request
# instead of several, and a timeout would lose all of it. Chunking bounds the
# blast radius of a single failure without materially changing the cost.
PROGRESS_BATCH = 200


def _api_url(path: str, settings: Settings, params: dict | None = None) -> str:
    return f"{transport.API_BASE}/{path}?{urlencode(transport.api_params(settings, params))}"


def _latest(*values) -> str | None:
    """The newest of several ISO timestamps, ignoring the ones that are absent.

    Simkl reports a per-list timestamp where the beacon contract wants one per
    KIND, and two lists of the same kind (television and anime) both move a
    person's episode history. The newest of them is the one that says "something
    changed", which is the only question the beacon is asked.
    """
    stamps = sorted(str(v) for v in values if v)
    return stamps[-1] if stamps else None


def _list_stamps(data: dict) -> dict[str, dict[str, str | None]]:
    """The per-(catalogue, status) last-modified stamps, in this module's own
    spelling of the catalogue names.

    A STATUS THAT IS ABSENT FROM THE PAYLOAD IS LEFT OUT, and a catalogue block
    that is missing entirely produces no entry at all — deliberately, because
    "Simkl did not say" and "Simkl said never" are different answers and only the
    second one is safe to act on. Everything downstream treats a missing stamp as
    unknown and reads the bucket anyway, so a shape change at the service costs
    traffic rather than correctness.
    """
    stamps: dict[str, dict[str, str | None]] = {}
    for media, key in ACTIVITY_LISTS.items():
        block = data.get(key)
        if isinstance(block, dict):
            stamps[media] = {status: block.get(status)
                             for status in WATCHED_STATUSES if status in block}
    return stamps


async def fetch_last_activities(settings: Settings) -> dict:
    """Simkl's per-list last-modified timestamps, in the beacon shape.

    THE CHEAPEST CALL IN THE API and the gate every sync opens with: a fixed-size
    blob whatever the size of the library behind it. An unreadable or empty
    answer comes back as an empty dict, which reads downstream as "no beacon" and
    costs one history pull rather than a refusal.
    """
    data = await transport.cached_get(
        transport.sync_client(), settings, "sync/activities", {},
        pool=transport.SYNC_POOL, private=True)
    if not isinstance(data, dict):
        return {}
    shows = data.get("tv_shows") or {}
    anime = data.get("anime") or {}
    movies = data.get("movies") or {}
    return {
        # THE PER-LIST STAMPS RIDE ALONG, under a key of this module's own. The
        # four normalized values below are all the beacon CONTRACT asks for, and
        # they answer "has anything changed"; these answer "which of the twelve
        # buckets changed", which is the difference between re-reading a whole
        # library and re-reading one list of it. The caller never reads them — it
        # hands the blob back to fetch_library, which is the only thing here that
        # knows what a bucket is.
        "lists": _list_stamps(data),
        "episodes": {
            "watched_at": _latest(shows.get("all"), anime.get("all")),
            # A REMOVAL IS NOT A PLAY and never appears in the history, so the
            # tracker watches this separately: when it moves, cached progress is
            # re-baselined instead of being folded forward.
            "removed_at": _latest(shows.get("removed_from_list"),
                                  anime.get("removed_from_list")),
        },
        "movies": {
            "watched_at": movies.get("all"),
            "removed_at": movies.get("removed_from_list"),
        },
    }


def _entry_ids(payload: dict) -> dict:
    """The id map off a show or movie object, in this app's spelling.

    Simkl writes its own id as `simkl_id` on some payloads and `simkl` on others;
    `collect_ids` only knows the second. Mapping it HERE, at the provider
    boundary, is what keeps a second spelling out of the rest of the app.
    """
    raw = dict(payload.get("ids") or {})
    if raw.get("simkl") in (None, "") and raw.get("simkl_id") not in (None, ""):
        raw["simkl"] = raw["simkl_id"]
    return collect_ids(raw)


def _episode_events(item: dict, start_at: str | None) -> list[dict]:
    """One library item flattened into the episode plays it records.

    AN EPISODE WITH NO TIMESTAMP IS NOT AN EVENT. Simkl marks an episode watched
    without always saying when, and an undated play cannot be placed in a month —
    which is the only thing the history sweep is for. Those episodes still reach
    the tracker through the progress baseline, where a missing date reads as
    "date unknown" rather than as a play on the epoch.
    """
    show = item.get("show") or {}
    ids = _entry_ids(show)
    if not ids:
        return []
    title = str(show.get("title") or "")
    events = []
    for season in item.get("seasons") or []:
        number = season.get("number")
        if number is None:
            continue
        for episode in season.get("episodes") or []:
            watched_at = str(episode.get("watched_at") or "")
            if not watched_at or episode.get("number") is None:
                continue
            # `date_from` bounds which ITEMS come back, not which episodes inside
            # them — an item that moved yesterday arrives carrying its whole
            # history. Re-applying an old play is harmless, but it would widen
            # every "what did I watch recently" answer to "everything, ever".
            if start_at and watched_at[:10] < start_at:
                continue
            events.append({
                "type": "episode",
                "show": {"ids": ids, "title": title},
                "episode": {"season": int(number), "number": int(episode["number"])},
                "watched_at": watched_at,
            })
    return events


def _movie_event(item: dict, start_at: str | None) -> dict | None:
    movie = item.get("movie") or {}
    ids = _entry_ids(movie)
    watched_at = str(item.get("last_watched_at") or "")
    if not ids or not watched_at:
        return None
    if start_at and watched_at[:10] < start_at:
        return None
    return {
        "type": "movie",
        "movie": {"ids": ids, "title": str(movie.get("title") or ""),
                  "year": movie.get("year")},
        "watched_at": watched_at,
    }


async def _all_items(settings: Settings, media: str, status: str,
                     start_at: str | None) -> dict:
    """One /sync/all-items bucket, or an empty document.

    A bucket that cannot be read is empty rather than fatal: a person whose
    "dropped" list 500s should still have their "watching" list counted, and the
    tracker's contract for an unreadable source is to serve what it has.
    """
    params = {"episode_watched_at": "yes", "extended": "full"}
    if start_at:
        params["date_from"] = start_at
    data = await transport.cached_get(
        transport.sync_client(), settings, f"sync/all-items/{media}/{status}", params,
        pool=transport.SYNC_POOL, private=True)
    return data if isinstance(data, dict) else {}


async def fetch_history(settings: Settings, start_at: str | None = None) -> list[dict]:
    """This person's watch EVENTS, newest-agnostic, optionally only those on or
    after `start_at` (YYYY-MM-DD).

    SEQUENTIAL, NOT GATHERED, and that is not an oversight. Simkl prohibits
    parallel requests off its cached paths, and SYNC_POOL admits one at a time —
    so issuing these together would only queue them while making a failure harder
    to attribute. There are at most a dozen buckets and each is one call.

    Re-seeing an event already applied is harmless by contract, which is what
    lets `start_at` stay at day granularity.
    """
    events: list[dict] = []
    with span("simkl.history", start_at=start_at or ""):
        for media in EPISODE_TYPES:
            for status in WATCHED_STATUSES:
                document = await _all_items(settings, media, status, start_at)
                for item in document.get(media) or document.get("shows") or []:
                    events.extend(_episode_events(item, start_at))
        for status in WATCHED_STATUSES:
            document = await _all_items(settings, "movies", status, start_at)
            for item in document.get("movies") or []:
                event = _movie_event(item, start_at)
                if event is not None:
                    events.append(event)
    logger.info("simkl fetch_history(start_at=%s): %d event(s)", start_at, len(events))
    return events


# What a stamp lookup answers when the payload did not mention that list at all,
# which is neither "it moved" nor "it never has". A sentinel rather than None
# because None is the service's own way of saying a list has never been used, and
# conflating the two would silently stop reading a bucket over a shape change.
_UNSTATED = object()


def _stamp(stamps: dict, media: str, status: str):
    block = (stamps or {}).get(media)
    if not isinstance(block, dict) or status not in block:
        return _UNSTATED
    return block.get(status)


def _wanted_buckets(activities: dict | None,
                    since: dict | None) -> tuple[list[tuple[str, str]], bool]:
    """Which (catalogue, status) buckets a library read has to fetch, and whether
    fetching only those covers the WHOLE library.

    THIS IS THE ONLY CONDITIONAL REQUEST SIMKL'S PRIVATE HALF SUPPORTS. The
    /sync/ endpoints carry no ETag, no Last-Modified and no Cache-Control — every
    response is served as dynamic — so there is nothing to send an If-None-Match
    against and no 304 is reachable. What IS available is the per-list stamp
    block on /sync/activities, which answers a better question than an ETag on a
    whole response could: not "is this response the one I have" but "which of
    these twelve calls do I still need to make".

    Two reasons to skip a bucket, and they are not the same:
      - ITS STAMP IS NULL. That list has never been used, so it is empty and will
        stay empty until it is not — and when it is not, its stamp stops being
        null. Skipping it costs nothing and leaves the read COMPLETE.
      - ITS STAMP HAS NOT MOVED since the read `since` came from. Nothing in it
        has changed, so what the caller already recorded from it still stands —
        but the bucket was not read, so this read is PARTIAL and a title missing
        from it means nothing.
    """
    now = (activities or {}).get("lists") or {}
    before = (since or {}).get("lists") or {}
    wanted: list[tuple[str, str]] = []
    skipped = False
    for media in LIBRARY_TYPES:
        for status in WATCHED_STATUSES:
            stamp = _stamp(now, media, status)
            if stamp is None:
                continue
            if (since is not None and stamp is not _UNSTATED
                    and _stamp(before, media, status) == stamp):
                skipped = True
                continue
            wanted.append((media, status))
    return wanted, not skipped


def _fold_library_item(entries: dict[str, LibraryEntry], item: dict) -> None:
    """One library item filed under the SHARED identity of its title.

    The identity waterfall is app/providers/base.py's and is not restated here;
    all this module contributes is which field of Simkl's payload carries which
    id. A title the waterfall cannot key — known to Simkl and to nobody else — is
    dropped, because there is no id in it that another service could ever have
    named the same title by, so nothing could be matched to it either way.

    Two items resolving to one identity are MERGED rather than one winning. Simkl
    files a title under exactly one status, so this is not the ordinary case; when
    it does happen (a duplicated catalogue entry, an anime title also filed as
    television) dropping one of them would silently lose the episodes only it
    carried.

    A SEASON ABSENT FROM `seasons[]` IS SIMKL SAYING ZERO, WHICH IS WHY EVERY
    ENTRY THIS BUILDS SETS `unlisted_seasons_are_zero`. /sync/all-items lists only
    the seasons a title has WATCHED EPISODES in — a title in the library whose
    season 2 the viewer has seen none of produces no season-2 block at all. Only a
    title absent from the LIBRARY is Simkl saying nothing. The two are different
    answers and a caller that cannot tell them apart renders the first as a claim
    only the other service made, which is a false single-source label on a season
    both services agree is at zero.
    """
    show = item.get("show") or {}
    ids = _entry_ids(show)
    key = resolve_key(Media.SHOW, ids)
    if key is None:
        return
    seasons = _progress_from_seasons(item)
    previous = entries.get(str(key))
    if previous is None:
        entries[str(key)] = LibraryEntry(ids=dict(ids), seasons=seasons,
                                         unlisted_seasons_are_zero=True)
        return
    merged = {season: dict(episodes) for season, episodes in previous.seasons.items()}
    for season, episodes in seasons.items():
        merged[season] = {**merged.get(season, {}), **episodes}
    entries[str(key)] = LibraryEntry(ids={**previous.ids, **ids}, seasons=merged,
                                     unlisted_seasons_are_zero=True)


async def fetch_library(settings: Settings, *, start_at: str | None = None,
                        activities: dict | None = None,
                        since: dict | None = None) -> LibraryRead:
    """This person's Simkl library, keyed by the shared title identity, plus the
    plays inside it on or after `start_at`.

    ONE READ ANSWERS BOTH QUESTIONS, and that is the point of it existing beside
    fetch_history. /sync/all-items carries the whole watch record — every season,
    every episode, each with the date it was watched — so the same buckets that
    produce the play events produce a complete per-title baseline, and asking for
    them twice would double the cost of the most expensive call this module
    makes.

    NO `date_from` IS SENT, deliberately, and it is the one place this is more
    expensive than fetch_history. `date_from` bounds which ITEMS come back, and a
    baseline needs every title the person holds rather than the ones that moved
    recently — an item filtered out here would read as a title Simkl does not
    have. The events are bounded in this process instead, exactly as they already
    are for the episodes inside an item. What buys the cost back is
    `_wanted_buckets`: an unchanged list is not read at all.
    """
    wanted, complete = _wanted_buckets(activities, since)
    entries: dict[str, LibraryEntry] = {}
    events: list[dict] = []
    with span("simkl.library", buckets=len(wanted), complete=complete):
        for media, status in wanted:
            document = await _all_items(settings, media, status, None)
            if media == "movies":
                for item in document.get("movies") or []:
                    event = _movie_event(item, start_at)
                    if event is not None:
                        events.append(event)
                continue
            for item in document.get(media) or document.get("shows") or []:
                events.extend(_episode_events(item, start_at))
                _fold_library_item(entries, item)
    logger.info("simkl fetch_library(start_at=%s): %d bucket(s), %d title(s), "
                "%d event(s), complete=%s",
                start_at, len(wanted), len(entries), len(events), complete)
    return LibraryRead(entries=entries, events=events, complete=complete)


def _progress_from_seasons(entry: dict) -> dict[int, dict[int, str]]:
    """One /sync/watched answer as {season: {episode: watched_at}}.

    A season with no episode breakdown contributes nothing rather than a guessed
    set: Simkl reports a watched COUNT beside the breakdown, and turning "4
    episodes" into "episodes 1-4" would invent four dates and four episode
    numbers that may not be the ones actually seen.
    """
    out: dict[int, dict[int, str]] = {}
    for season in entry.get("seasons") or []:
        number = season.get("number")
        if number is None:
            continue
        episodes: dict[int, str] = {}
        for episode in season.get("episodes") or []:
            if episode.get("number") is None:
                continue
            episodes[int(episode["number"])] = str(episode.get("watched_at") or "")
        if episodes:
            out[int(number)] = dict(sorted(episodes.items()))
    return out


async def fetch_progress_details(settings: Settings,
                                 show_ids) -> dict[int, dict[int, dict[int, str]]]:
    """{simkl_id: {season: {episode: watched_at}}} for several shows at once.

    ONE REQUEST FOR A WHOLE ROSTER. `POST /sync/watched` takes the items you
    already know about and answers about each of them, which makes it the batched
    analogue of a per-show progress record — and the batching is not an
    optimisation here but the only workable shape, because Simkl caps POSTs at
    one per second and a seventy-title roster asked one at a time would take over
    a minute.

    THE ANSWER IS A PARALLEL ARRAY, so an entry is matched to the id that was
    asked about by POSITION. An entry that names its own id is believed over the
    position, because a service that starts filtering its answers would otherwise
    shift every row silently.
    """
    unique = []
    for value in show_ids or []:
        if value in (None, ""):
            continue
        try:
            number = int(value)
        except (TypeError, ValueError):
            continue
        if number not in unique:
            unique.append(number)
    if not unique:
        return {}
    out: dict[int, dict[int, dict[int, str]]] = {}
    client = transport.sync_client()
    url = _api_url("sync/watched", settings, {"episode_watched_at": "yes"})
    with span("simkl.progress_details", n=len(unique)):
        for offset in range(0, len(unique), PROGRESS_BATCH):
            batch = unique[offset:offset + PROGRESS_BATCH]
            # NEVER CACHED, AND IT COULD NOT BE. This is a POST whose meaning is
            # entirely in its body, and the response cache is keyed by URL — two
            # different rosters would share one key. It is also one person's
            # viewing, which is the other reason.
            resp = await transport.send(
                client, "POST", url, pool=transport.SYNC_POOL,
                headers=transport.api_headers(settings),
                json=[{"simkl": show_id} for show_id in batch])
            if resp.status_code != 200:
                logger.warning("simkl fetch_progress_details -> HTTP %s: %s",
                               resp.status_code, resp.text[:200])
                continue
            try:
                answers = resp.json()
            except ValueError:
                logger.warning("simkl fetch_progress_details -> unreadable body")
                continue
            if not isinstance(answers, list):
                continue
            for position, entry in enumerate(answers):
                if not isinstance(entry, dict):
                    continue
                stated = _entry_ids(entry).get("simkl")
                if stated in (None, "") and position < len(batch):
                    stated = batch[position]
                if stated in (None, ""):
                    continue
                progress = _progress_from_seasons(entry)
                if progress:
                    out[int(stated)] = progress
    return out


async def fetch_watched_progress(settings: Settings, since_days: int | None = 60) -> list[dict]:
    """Recently-active seasons, as candidates for "you seem to be watching this".

    A RECENCY SIGNAL, NOT A COMPLETION RECORD: `watched` here counts the distinct
    episodes seen inside the window, and the caller compares it against the
    season's total to decide in-progress from finished.
    """
    start_at = None
    if since_days is not None:
        start_at = (datetime.now(timezone.utc).date() - timedelta(days=since_days)).isoformat()
    out = watched_progress_from(await fetch_history(settings, start_at=start_at))
    logger.info("simkl fetch_watched_progress(since_days=%s) -> %d recent season(s)",
                since_days, len(out))
    return out


def watched_progress_from(events: list[dict]) -> list[dict]:
    """The seasons in a history sweep, as [{ids, season, watched, title, network}].

    Pure, and split from the fetch for the same reason Trakt's is: a caller that
    needs both the seasons and the films out of one window sweeps the history once
    and reads it twice.

    `network` COMES BACK EMPTY, always. Simkl's library payload does not carry the
    broadcaster, and the field is in the shape because the tracker's records have
    one — an empty string is the honest answer and every reader already treats it
    as "not stated" rather than filling it in.
    """
    aggregate: dict[tuple[str, int], dict] = {}
    for event in events:
        if event.get("type") != "episode":
            continue
        show = event.get("show") or {}
        episode = event.get("episode") or {}
        ids = _entry_ids(show) or collect_ids(show.get("ids") or {})
        simkl_id, season, number = ids.get("simkl"), episode.get("season"), episode.get("number")
        if simkl_id is None or season is None or int(season) == 0:  # season 0 is specials
            continue
        record = aggregate.setdefault((str(simkl_id), int(season)), {
            "eps": set(), "ids": ids, "title": str(show.get("title") or ""),
        })
        if number is not None:
            record["eps"].add(int(number))
    return [{
        "ids": record["ids"], "season": season, "watched": len(record["eps"]),
        "title": record["title"], "network": "",
    } for (_simkl_id, season), record in aggregate.items()]


def movie_plays_from(events: list[dict]) -> list[dict]:
    """The film plays in those same events, as [{ids, title, year, watched_at}].
    Pure, for the same reason as watched_progress_from."""
    out: list[dict] = []
    for event in events:
        if event.get("type") != "movie":
            continue
        movie = event.get("movie") or {}
        ids = _entry_ids(movie) or collect_ids(movie.get("ids") or {})
        if not ids:
            continue
        out.append({"ids": ids, "title": str(movie.get("title") or ""),
                    "year": movie.get("year"),
                    "watched_at": str(event.get("watched_at") or "")})
    return out
