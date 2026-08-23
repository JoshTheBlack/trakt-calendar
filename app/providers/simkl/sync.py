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

A FAILURE IS NEVER NORMALIZED INTO AN EMPTY ANSWER. Everything here reads one
person's library, and an empty library is a DESTRUCTIVE answer — the caller
retires the rows a source no longer holds — so "I could not read this" and "there
is nothing here" must not arrive in the same shape. A refused call raises, a
refused CREDENTIAL raises immediately and everywhere (it is a statement about
every request that token will make, not about the list that happened to be asked
for first), and a read that lost some of its buckets says so by coming back
incomplete rather than by coming back smaller.

NOTHING HERE WRITES TO SIMKL. `POST /sync/history` exists and is deliberately
never called: this app reads a person's viewing and never edits it.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from urllib.parse import unquote, urlencode

from ...config import Settings
from ...perftrace import span
from ..base import (LibraryEntry, LibraryRead, Media, UnlistedSeasons, collect_ids,
                    resolve_key)
from . import _ids, _naming, transport

logger = logging.getLogger(__name__)

# The library buckets a watch history can be in. Simkl files an item under
# exactly one, and a season somebody is part-way through sits in `watching`
# while a finished one sits in `completed` — so asking only about `completed`
# would lose every season in progress, which is most of what the tracker counts.
# `plantowatch` is excluded because nothing in it has been watched.
WATCHED_STATUSES = ("watching", "completed", "hold", "dropped")

# The one bucket whose items are NOT itemized, measured against a live account:
# every title in `watching` carries a `seasons[]` block with an entry per episode
# watched, and not one title in `completed` carries the key at all. A completed
# item states itself in counts instead — total_episodes_count,
# watched_episodes_count and a `not_aired_episodes_count`. So the two buckets make
# opposite statements with the same missing block, and reading them the same way
# turns a finished show into a show with nothing watched. See UnlistedSeasons in
# app/providers/base.py, which is how that difference leaves this module.
COMPLETED_STATUS = "completed"

# The catalogues an episode can come from. Simkl keeps anime apart from
# television at every endpoint, and a person's anime is watch history exactly as
# much as their television is — omitting it would silently under-count the half
# of Simkl's library it is best at.
# Named rather than spelled inline because one read asks for more than the
# other: an anime item's season number is Simkl's own, and only the anime buckets
# ask for the mapping that translates it (see ANIME_EXTENDED).
ANIME_TYPE = "anime"
EPISODE_TYPES = ("shows", ANIME_TYPE)

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
# SIMKL'S OWN CAP, NOT A CHOICE. A `POST /sync/watched` asking for episode data
# takes at most 100 items; over that it answers `400 max_items`. (Without episode
# data there is no documented limit, but this read always asks for episodes — the
# counts alone are not what it is for.)
PROGRESS_BATCH = 100

# What every library read asks for, and each one earns its place:
#
#   extended=full            episodes at all. Without it a bucket carries summary
#                            fields only and no `seasons[].episodes[]` anywhere.
#   episode_watched_at=yes   the per-episode timestamp. Simkl documents it as
#                            requiring `extended=full`, and it is what turns a
#                            library into the play events this module publishes.
#   include_all_episodes=yes episodes for the `completed` and `dropped` buckets,
#                            which by DEFAULT carry counts and nothing else. This
#                            app used to reconstruct those episodes from the
#                            counts by hand; the parameter is Simkl doing it
#                            server-side, and doing it better — measured
#                            2026-08-21, a dropped title lists exactly the
#                            episodes actually watched (9 of 47, 15 of 49) rather
#                            than everything up to the count.
LIBRARY_PARAMS = {
    "extended": "full",
    "episode_watched_at": "yes",
    "include_all_episodes": "yes",
}

# What the ANIME buckets ask for instead, and it is a superset of `full`.
#
# WHY ONLY ANIME NEEDS IT. Simkl models each anime season as its own catalogue
# title, numbering its own episodes from 1 — so a viewer's plays on "Beastars
# season 2" arrive as season 1 of a title the tracker has never heard of. This
# parameter is Simkl answering both halves of that itself: `mapped_tvdb_seasons`
# on the entry, a `tvdb: {season, episode}` block on EVERY episode, and — the
# part that removes the most code — the PARENT SERIES' shared ids on the entry.
# Measured 2026-08-21: Beastars season 2 (simkl 1231401) arrives under
# `extended=full` as `{simkl, slug, mal}` and under this one as the same plus
# `imdb`, `tmdb` and `tvdb`. Everything this package used to derive with a
# per-title lookup is in the payload.
ANIME_EXTENDED = "full_anime_seasons"

# What `POST /sync/watched` puts in its `result` field, and the three values are
# three different answers that must not be run together:
#
#   true          the viewer has watched or is watching this title.
#   false         the title is IN SIMKL'S CATALOGUE and this viewer has not
#                 watched it. A real, safe zero — the one answer from here that
#                 may retire what the app had stored.
#   "not_found"   the ids could not be resolved to a title at all. NOT a zero:
#                 Simkl is saying it does not know what was asked about, which is
#                 "I cannot say" and must leave stored counts alone.
#
# The middle one is easy to mistake for the last, and this app did: a `not_found`
# was read as the safe zero and a `false` was not read at all.
UNWATCHED = False
UNRESOLVED = "not_found"


def _api_url(path: str, settings: Settings, params: dict | None = None) -> str:
    """A URL for one of this module's calls, named for the TRACKER.

    Everything here reads one person's watch history with that person's own
    token, which is the half Simkl knows as `distrakkt` — see
    transport.APP_NAME_TRACKER for why the two halves identify themselves
    differently at all.
    """
    return (f"{transport.API_BASE}/{path}?"
            f"{urlencode(transport.api_params(settings, params, app=transport.APP_NAME_TRACKER))}")


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
    blob whatever the size of the library behind it.

    A BEACON THAT COULD NOT BE READ RAISES, and is never answered with an empty
    blob. The caller compares this against what it stored last time to decide
    whether anything has moved, so an empty answer is not "no beacon" — it is the
    claim that every one of the four stamps is absent, which compares EQUAL to a
    stored empty one and gates the sync as unchanged. A source that cannot be
    reached would then report itself up to date for as long as it stayed down,
    and the whole pass would be built on that. The tracker degrades a source that
    raises (it is named in the page's notice and its stored rows are left alone),
    which is the honest version of the same outcome.
    """
    data = await transport.cached_get(
        transport.sync_client(), settings, "sync/activities", {},
        pool=transport.SYNC_POOL, private=True, raise_errors=True,
        app=transport.APP_NAME_TRACKER)
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
    """The id map off a show or movie object, in this app's spelling — the
    `ids` sub-object extracted, and Simkl's own `simkl_id`/`simkl` spelling
    quirk corrected by `_ids.normalize` (shared with search.py, which reads
    the same quirk off a different endpoint's payload)."""
    return _ids.normalize(payload.get("ids") or {})


# Before this, a Simkl watch timestamp is not a date at all. Simkl writes
# `1970-01-01T00:00:01Z` when somebody says they watched a thing but cannot
# remember when, and documents the reading as "any timestamp before 2000-01-01
# signals this placeholder" — so it is a range rather than one literal value.
#
# IT MATTERS BECAUSE THIS APP FILES BY MONTH. A season whose last episode carries
# the placeholder would be recorded as finished in JANUARY 1970 — a real month,
# on a real page, sorted before everything. "I don't remember when" and "the
# first of January 1970" are the same bytes and opposite facts, and only one of
# them belongs in a month.
UNREMEMBERED_BEFORE = "2000-01-01"


def _was_watched(episode: dict) -> bool:
    """Whether this episode was actually WATCHED, across the two shapes Simkl
    answers in.

    THE TWO READS DISAGREE ABOUT WHAT AN EPISODE LIST CONTAINS, and reading one
    like the other is a silent overcount. `/sync/all-items` lists only the
    episodes a viewer has seen, so appearing in it IS the claim. `POST
    /sync/watched` lists EVERY episode of the title and marks each one — measured
    2026-08-21, Beastars season 1 comes back as twelve episodes of which three
    say `watched: true`, beside a `episodes_watched: 3` that agrees. Read as the
    library shape it counted all twelve, and a viewer three episodes into a
    season had it recorded as finished.

    ABSENT MEANS WATCHED, which is the library's shape and the safe reading of
    it: a list that only contains watched episodes cannot mark them, and treating
    an unmarked episode as unwatched would empty every library entry.
    """
    return bool(episode.get("watched", True))


def _watched_at(episode: dict) -> str:
    """When this episode was watched, across the same two shapes — `watched_at`
    on the library read, `last_watched_at` on the per-title one. Two spellings of
    one fact, and taking only the first lost every date the second carried."""
    return str(episode.get("watched_at") or episode.get("last_watched_at") or "")


def _placeholder_date(watched_at: str) -> bool:
    """Whether `watched_at` is Simkl's "very long time ago" placeholder rather
    than a date. See UNREMEMBERED_BEFORE."""
    return bool(watched_at) and watched_at[:10] < UNREMEMBERED_BEFORE


def _coordinates(season: dict, episode: dict) -> tuple[int, int] | None:
    """Where one episode of one library item files IN THE TRACKER'S NUMBERING —
    (season, episode) — or None when the payload does not say.

    SIMKL'S OWN ANSWER IS PREFERRED OVER THE ITEM'S. An anime season-title
    numbers its episodes from 1 whatever season of the show it really is, and
    `extended=full_anime_seasons` puts the real coordinates on every episode as a
    `tvdb` block — Beastars season 2 episode 1 arrives as season 1 episode 1 with
    `tvdb: {season: 2, episode: 1}`. Taking that block is the whole translation
    this package used to spend a per-title lookup deriving, and it is better than
    what the lookup could do: it is per EPISODE, so a series numbered absolutely
    (One Piece, one title with a thousand episodes) maps each episode to the
    season it belongs to rather than being declared untranslatable.

    THE EPISODE NUMBER TRAVELS WITH THE SEASON, NEVER ALONE. Simkl's mapping
    renumbers both — episode 878 of One Piece is season 20 episode 1 — so taking
    the season from the `tvdb` block and the number from the item would produce a
    coordinate that exists in neither numbering.

    IT IS TVDB'S NUMBERING, and this app files records under TMDB ids. Simkl
    publishes no TMDB equivalent, and its own guide says the two "occasionally
    disagree on season numbering for newer or split-cour anime". So a title where
    they diverge is filed under a TMDB id with a TVDB season number — which is
    not new, it is what `_naming` already produced from `mapped_tvdb_seasons`,
    and there is nothing better on offer to replace it with.
    """
    tvdb = episode.get("tvdb")
    if isinstance(tvdb, dict) and tvdb.get("season") is not None and tvdb.get("episode") is not None:
        return int(tvdb["season"]), int(tvdb["episode"])
    if season.get("number") is None or episode.get("number") is None:
        return None
    return int(season["number"]), int(episode["number"])


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
        for episode in season.get("episodes") or []:
            watched_at = str(episode.get("watched_at") or "")
            if not watched_at or _placeholder_date(watched_at):
                continue
            where = _coordinates(season, episode)
            if where is None:
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
                "episode": {"season": where[0], "number": where[1]},
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
                     start_at: str | None) -> dict | None:
    """One /sync/all-items bucket, or None when it could not be read.

    A BUCKET THAT FAILED IS NOT AN EMPTY BUCKET, and the difference between None
    and {} here is the whole reason this function has two ways of answering. A
    person whose "dropped" list 500s should still have their "watching" list
    counted — that much has always been right — but an empty document says
    something far stronger than "this call did not work": it says this person has
    nothing in that list. Composed across every bucket, twelve of those became a
    complete library holding no titles, and the caller concluded, correctly from
    what it was told, that Simkl holds nothing at all and overwrote a viewer's
    entire stored Simkl history with "asked, and it had nothing". So the failure
    travels rather than being flattened, and the caller decides what a partial
    read means.

    A CREDENTIAL FAILURE IS NOT A BUCKET-LEVEL FAILURE AT ALL and is re-raised
    unchanged. It is not a statement about this list; it is a statement about
    every request that will ever be made with this token, so tolerating it once
    per bucket would tolerate it twelve times and call the result a read. See
    transport.is_credential_failure.
    """
    params = dict(LIBRARY_PARAMS)
    if media == ANIME_TYPE:
        params["extended"] = ANIME_EXTENDED
    if start_at:
        params["date_from"] = start_at
    try:
        data = await transport.cached_get(
            transport.sync_client(), settings, f"sync/all-items/{media}/{status}",
            params, pool=transport.SYNC_POOL, private=True, raise_errors=True,
            app=transport.APP_NAME_TRACKER)
    except transport.SimklError as exc:
        if transport.is_credential_failure(exc):
            raise
        logger.warning("simkl all-items/%s/%s could not be read: %s", media, status, exc)
        return None
    # None here is not a failure: `raise_errors=True` means a refusal has already
    # raised, so this is Simkl answering with a body that held nothing to read.
    return data if isinstance(data, dict) else {}


def _refuse_a_read_that_read_nothing(wanted: int, failed: int, what: str) -> None:
    """Raise when every bucket a read meant to fetch failed.

    ONE BUCKET FAILING AND ALL OF THEM FAILING ARE NOT DEGREES OF ONE THING. One
    list refusing leaves the rest of the library readable and the person's other
    counts intact, which is why _all_items tolerates it. Every list refusing at
    once is not a fact about any list — it is a fact about the connection, the
    service, or the credential — and answering it with an empty library would
    hand the caller a destructive conclusion ("this person holds nothing")
    dressed as a successful read. That is precisely what happened: twelve refused
    buckets composed into a read that logged itself complete, holding zero
    titles, and a viewer's stored Simkl seasons were replaced with marks saying
    the service had been asked and had nothing.

    NOTHING WANTED IS NOT NOTHING READ. A read with no buckets to fetch (every
    list unchanged since last time) asked for nothing and got nothing, which is
    an ordinary, successful, empty pass.
    """
    if wanted and failed >= wanted:
        raise transport.SimklError(
            f"Simkl answered none of the {wanted} list(s) {what} asked for.")


async def fetch_history(settings: Settings, start_at: str | None = None) -> list[dict]:
    """This person's watch EVENTS, newest-agnostic, optionally only those on or
    after `start_at` (YYYY-MM-DD).

    SEQUENTIAL, NOT GATHERED, and that is not an oversight. Simkl prohibits
    parallel requests off its cached paths, and SYNC_POOL admits one at a time —
    so issuing these together would only queue them while making a failure harder
    to attribute. There are at most a dozen buckets and each is one call.

    Re-seeing an event already applied is harmless by contract, which is what
    lets `start_at` stay at day granularity.

    A SWEEP IN WHICH NO BUCKET COULD BE READ RAISES rather than answering "you
    watched nothing" — see _refuse_a_read_that_read_nothing. A sweep that lost
    SOME of its buckets still answers, because a play the tracker does not see
    this pass is one it sees on the next: the fold is idempotent and the cursor
    only moves forward a day at a time.
    """
    events: list[dict] = []
    wanted = failed = 0
    with span("simkl.history", start_at=start_at or ""):
        for media in EPISODE_TYPES:
            for status in WATCHED_STATUSES:
                wanted += 1
                document = await _all_items(settings, media, status, start_at)
                if document is None:
                    failed += 1
                    continue
                for item in document.get(media) or document.get("shows") or []:
                    events.extend(_episode_events(item, start_at))
        for status in WATCHED_STATUSES:
            wanted += 1
            document = await _all_items(settings, "movies", status, start_at)
            if document is None:
                failed += 1
                continue
            for item in document.get("movies") or []:
                event = _movie_event(item, start_at)
                if event is not None:
                    events.append(event)
    _refuse_a_read_that_read_nothing(wanted, failed, "fetch_history")
    logger.info("simkl fetch_history(start_at=%s): %d event(s), %d bucket(s) unread",
                start_at, len(events), failed)
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
                    since: dict | None) -> tuple[list[tuple[str, str, str | None]], bool]:
    """Which (catalogue, status) buckets a library read has to fetch, WHAT
    `date_from` each one may be bounded by, and whether fetching only those covers
    the WHOLE library.

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

    A BUCKET THAT DID MOVE IS ASKED ONLY FOR WHAT MOVED, which is the other half
    and the one Simkl asks for outright: pulling a whole list because one episode
    in it changed is the pattern its sync guide says will get a client id
    suspended. The bound is that bucket's OWN previous stamp — per bucket rather
    than one cursor per source, so each list is asked for exactly what it has not
    already been asked for — and it is sent back EXACTLY as Simkl wrote it. The
    guide is explicit that it must not be reformatted locally, so it is carried as
    the opaque string it arrived as and never parsed into a date on the way.

    A BOUNDED READ IS NOT A COMPLETE ONE, and this is where that is decided rather
    than left to the caller to remember. `date_from` bounds which ITEMS come back,
    so a title absent from the answer has not been said to be gone — it has been
    said not to have changed. Those two are indistinguishable downstream, and the
    only safe reading is the second, which `complete=False` already means
    everywhere it is honoured. The cost is that a library read stops being able to
    retire anything once an account is past its first sync; that is deliberate,
    and it is why removals are detected by their own mechanism against their own
    endpoint instead of being inferred from an absence here.
    """
    now = (activities or {}).get("lists") or {}
    before = (since or {}).get("lists") or {}
    wanted: list[tuple[str, str, str | None]] = []
    skipped = bounded = False
    for media in LIBRARY_TYPES:
        for status in WATCHED_STATUSES:
            stamp = _stamp(now, media, status)
            if stamp is None:
                continue
            previous = _stamp(before, media, status)
            if (since is not None and stamp is not _UNSTATED
                    and previous == stamp):
                skipped = True
                continue
            # ONLY WHEN THERE IS A REAL EARLIER STAMP TO CARRY ON FROM. `since`
            # being absent is a first sync (or a pull deliberately reaching
            # further back than the last one), and `_UNSTATED` means this bucket
            # was never recorded — neither is a point in time this list can be
            # asked for changes since, and guessing one would silently skip
            # everything older than the guess.
            date_from = (previous if since is not None
                         and isinstance(previous, str) and previous else None)
            bounded = bounded or date_from is not None
            wanted.append((media, status, date_from))
    return wanted, not (skipped or bounded)


def _unlisted_claim(item: dict, status: str) -> UnlistedSeasons:
    """What this item's silence about a season means, decided by the LIST it came
    out of. Measured against a live account, 2026-08-04.

    `watching`, `hold` and `dropped` itemize: every one of those items carries a
    `seasons[]` block holding the episodes watched, so a season missing from it is
    a season the viewer has seen NONE of.

    `completed` does not itemize at all — 492 items, not one `seasons[]` key
    between them — and says instead that every AIRED episode of the title has been
    watched, with `watched_episodes_count` equal to `total_episodes_count`. So its
    silence means the OPPOSITE, and reading it as a zero would have the app report
    none of a title the service reports as finished.

    THE STATUS COMES FROM THE BUCKET THAT WAS ASKED FOR rather than from the
    item's own `status` field: the request named the list, so that answer cannot be
    wrong about which list this came out of, and a payload whose status field
    drifts or is absent cannot silently flip the meaning of every season it left
    out.

    A COMPLETED ITEM THAT ITEMIZES HAS STOPPED COUNTING AND STARTED LISTING, and
    is read as an itemized one. THAT IS NOW THE ORDINARY CASE rather than the
    exception: `include_all_episodes=yes` (see LIBRARY_PARAMS) has Simkl list the
    episodes of a finished title itself, so the bucket's silence about seasons —
    the thing this claim exists to interpret — is no longer what a completed item
    arrives with. The claim still has to be read, because a title Simkl declines
    to itemize would otherwise fall through as a count of none.

    AN UNFINISHED SEASON IS NOT CLAIMED AT ALL. "Completed" means every episode
    that has AIRED, and `not_aired_episodes_count` is the service saying more are
    coming — while the totals the app renders against are the season's PLANNED
    episode counts (see app/providers/trakt/detail.py, which deliberately reads
    `episode_count` and not `aired_episodes`). Claiming everything watched against
    a planned total would say the viewer has seen episodes that do not exist yet,
    which is the same class of silent wrong answer in the other direction. Such a
    title stays SILENT: its ids and its plays still arrive, and it simply
    contributes no per-season count until it really is finished.
    """
    if status != COMPLETED_STATUS or item.get("seasons"):
        return UnlistedSeasons.ZERO
    try:
        unaired = int(item.get("not_aired_episodes_count") or 0)
    except (TypeError, ValueError):  # a shape we do not understand is not a claim
        return UnlistedSeasons.SILENT
    return UnlistedSeasons.SILENT if unaired > 0 else UnlistedSeasons.WATCHED


def _merged_claim(first: UnlistedSeasons, second: UnlistedSeasons) -> UnlistedSeasons:
    """The claim two items resolving to ONE identity leave behind.

    Agreement keeps the claim; a contradiction drops to SILENT. Simkl files a
    title under exactly one status, so two items making DIFFERENT claims about the
    same title is a payload nothing here can explain — an anime title also filed as
    television, say, with the two halves in different lists. Believing either one
    would be picking, and the two possible mistakes are "reported none of a
    finished show" and "reported a show finished that is not": saying nothing is
    the only answer that cannot be confidently wrong.
    """
    return first if first == second else UnlistedSeasons.SILENT


def _fold_library_item(entries: dict[str, LibraryEntry], item: dict,
                       status: str) -> None:
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

    WHAT A SEASON ABSENT FROM `seasons[]` MEANS IS SCOPED BY THE BUCKET, and
    _unlisted_claim is where that is decided. Only a title absent from the LIBRARY
    ENTIRELY is Simkl saying nothing at all; a title it holds always says
    something, and which something depends on the list it is filed under.

    TWO TITLES OF ONE SEASON UNION THEIR EPISODES, which is a real case and not a
    theoretical one: Simkl files Beastars' third season as two titles (1687953 and
    2831384), both mapping to season 3 with twelve episodes each, and a viewer can
    hold both. Each of them states of its own episodes that they were watched, so
    an episode either of them names IS watched and the union is the honest answer;
    taking the second to fold as the winner would let a shorter listing silently
    retract the longer one's episodes.
    """
    show = item.get("show") or {}
    ids = _entry_ids(show)
    key = resolve_key(Media.SHOW, ids)
    if key is None:
        return
    seasons = _progress_from_seasons(item)
    claim = _unlisted_claim(item, status)
    previous = entries.get(str(key))
    if previous is None:
        entries[str(key)] = LibraryEntry(ids=dict(ids), seasons=seasons,
                                         unlisted_seasons=claim)
        return
    merged = {season: dict(episodes) for season, episodes in previous.seasons.items()}
    for season, episodes in seasons.items():
        slot = dict(merged.get(season, {}))
        for number, watched_at in episodes.items():
            # AN UNDATED PLAY NEVER OVERWRITES A DATED ONE, and the later date
            # wins between two dated ones — the same rule the tracker's own fold
            # takes for a repeated episode. A finished season-title states WHICH
            # episodes it holds without saying when (Simkl hands over counts, not
            # timestamps, for a completed title), so folding it over another
            # title of the same season would otherwise blank the dates that say
            # which month the season was finished in.
            if number not in slot or watched_at > slot[number]:
                slot[number] = watched_at
        merged[season] = slot
    entries[str(key)] = LibraryEntry(
        ids={**previous.ids, **ids}, seasons=merged,
        unlisted_seasons=_merged_claim(previous.unlisted_seasons, claim))


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

    `date_from` IS SENT ONLY WHERE THERE IS AN EARLIER READ TO CARRY ON FROM, per
    bucket, and `_wanted_buckets` owns that decision — including what it costs.
    The FIRST read of a list is unbounded, because a baseline needs every title
    the person holds and an item filtered out of that would read as a title Simkl
    does not have. Every read after it asks only for what has moved, which is what
    Simkl's sync guide requires of a client that wants to keep its id.

    THE TRADE IS THAT A BOUNDED READ CANNOT RETIRE ANYTHING, and the flag carries
    that: a bounded read comes back `complete=False`, so the caller folds in what
    it named and leaves everything else alone. Removals are found by their own
    mechanism rather than inferred from an absence here — see the deletion diff —
    because "did not change" and "is gone" arrive looking identical and only one
    of them is safe to act on.

    `complete` MEANS "EVERY BUCKET I MEANT TO READ, I READ" — never "I finished
    looping". A bucket deliberately skipped because its list has not moved makes
    the read partial, which is what `_wanted_buckets` already decided; a bucket
    that was asked for and REFUSED makes it partial for the same reason and with
    more force. The caller retires a title absent from a COMPLETE read, so a read
    that quietly downgraded a failure to an absence would have it delete watch
    history it merely failed to fetch — which is the defect this rule exists to
    make unreachable. A read in which NOTHING could be read is not a read at all
    and raises (_refuse_a_read_that_read_nothing).
    """
    wanted, complete = _wanted_buckets(activities, since)
    entries: dict[str, LibraryEntry] = {}
    events: list[dict] = []
    failed = 0
    with span("simkl.library", buckets=len(wanted)) as sp:
        for media, status, date_from in wanted:
            document = await _all_items(settings, media, status, date_from)
            if document is None:
                failed += 1
                complete = False
                continue
            if media == "movies":
                for item in document.get("movies") or []:
                    event = _movie_event(item, start_at)
                    if event is not None:
                        events.append(event)
                continue
            items = document.get(media) or document.get("shows") or []
            for item in items:
                events.extend(_episode_events(item, start_at))
                _fold_library_item(entries, item, status)
        sp.set(unread=failed, complete=complete)
    _refuse_a_read_that_read_nothing(len(wanted), failed, "fetch_library")
    logger.info("simkl fetch_library(start_at=%s): %d bucket(s), %d unread, %d title(s), "
                "%d event(s), complete=%s",
                start_at, len(wanted), failed, len(entries), len(events), complete)
    return LibraryRead(entries=entries, events=events, complete=complete)


def _progress_from_seasons(entry: dict) -> dict[int, dict[int, str]]:
    """One /sync/watched answer as {season: {episode: watched_at}}.

    A season with no episode breakdown contributes nothing rather than a guessed
    set: Simkl reports a watched COUNT beside the breakdown, and turning "4
    episodes" into "episodes 1-4" would invent four dates and four episode
    numbers that may not be the ones actually seen. THAT CASE NO LONGER ARISES
    ON THE LIBRARY PATH: `include_all_episodes=yes` has Simkl itemize the
    completed and dropped buckets itself, so an episode list arrives where a bare
    count used to (see LIBRARY_PARAMS). It still arises on the per-title read,
    which is why the rule stays.

    THE SEASON AND EPISODE NUMBERS ARE `_coordinates`', not the payload's, so an
    anime season-title's own numbering is translated here exactly as it is for
    the play events beside it — one rule, read by both, rather than a
    translation applied to one and forgotten for the other.
    """
    out: dict[int, dict[int, str]] = {}
    for season in entry.get("seasons") or []:
        for episode in season.get("episodes") or []:
            if not _was_watched(episode):
                continue
            where = _coordinates(season, episode)
            if where is None:
                continue
            watched_at = _watched_at(episode)
            # THE PLACEHOLDER IS NOT A DATE AND IS STORED AS NO DATE. The episode
            # still counts — the viewer watched it — but "I don't remember when"
            # must not become a play in January 1970. See UNREMEMBERED_BEFORE.
            if _placeholder_date(watched_at):
                watched_at = ""
            out.setdefault(where[0], {})[where[1]] = watched_at
    return {season: dict(sorted(episodes.items()))
            for season, episodes in sorted(out.items()) if episodes}


async def fetch_library_ids(settings: Settings) -> dict[int, str] | None:
    """Every Simkl id this person's library currently holds, mapped to that
    title's slug. None when the answer cannot be trusted to be whole.

    THE CHEAP HALF OF A REMOVAL CHECK. `date_from` deltas never surface removals —
    Simkl says so outright — and the prescribed answer is to re-read the library
    with `extended=simkl_ids_only` and diff it against what is stored. That
    payload carries `ids: {simkl, slug}` per item and nothing else, so it is a
    fraction of the `extended=full` re-pull the removal path would otherwise cost,
    and it is the only thing being asked for: what is still there.

    NO `date_from`, EVER, AND THAT IS THE POINT OF THE CALL. A bounded read
    answers "what changed"; this one has to answer "what remains", and a title
    filtered out for not having moved is exactly the title a diff would then
    report as removed. This is the one read in this module that must stay whole.

    NONE RATHER THAN A SHORT LIST WHEN ANY BUCKET FAILS, and the asymmetry with
    fetch_library is deliberate. There, a bucket that could not be read makes the
    answer PARTIAL and the caller folds in what it got — the missing bucket costs
    freshness. Here, a missing bucket costs every title in it: they are absent
    from the list, and absence is the whole signal. There is no partial version of
    this answer that is safe to diff, so a failure means no check this pass rather
    than a check on less than everything.

    THE SLUGS RIDE ALONG BECAUSE THEY ARE FREE. They are in the payload whether or
    not anything reads them, and a caller filling in the per-service name a record
    was written without would otherwise have to go and ask for what it already has.

    AND THEY ARE DECODED ON THE WAY OUT. Simkl percent-encodes a slug carrying
    anything non-ASCII — `carniv%C3%A0le` is in this account's library — and a
    caller building a URL will encode what it is given. Handing over the encoded
    form produces `carniv%25C3%25A0le`, a link to a title that does not exist,
    from a value that looks right in the database. Decoding here means the stored
    name is the NAME and every consumer can encode it exactly once.
    """
    ids: dict[int, str] = {}
    with span("simkl.library_ids") as sp:
        for media in LIBRARY_TYPES:
            for status in WATCHED_STATUSES:
                params = {"extended": "simkl_ids_only"}
                try:
                    document = await transport.cached_get(
                        transport.sync_client(), settings,
                        f"sync/all-items/{media}/{status}", params,
                        pool=transport.SYNC_POOL, private=True, raise_errors=True,
                        app=transport.APP_NAME_TRACKER)
                except transport.SimklError as exc:
                    if transport.is_credential_failure(exc):
                        raise
                    logger.warning(
                        "simkl library ids: %s/%s could not be read (%s) — no "
                        "removal check this pass", media, status, exc)
                    return None
                if not isinstance(document, dict):
                    continue
                # A bucket with nothing in it is omitted entirely rather than sent
                # as an empty list, so every key is read by name and a missing one
                # is an ordinary empty answer.
                for key in (media, "shows", "movies", "anime"):
                    for item in document.get(key) or []:
                        entry = _entry_ids((item.get("show") or item.get("movie")
                                            or item) if isinstance(item, dict) else {})
                        simkl_id = entry.get("simkl")
                        if simkl_id is None:
                            continue
                        ids[int(simkl_id)] = unquote(str(entry.get("simkl_slug") or ""))
        sp.set(titles=len(ids))
    return ids


async def _speaks_for_one_season(settings: Settings, simkl_ids) -> set[int]:
    """Which of `simkl_ids` name ONE SEASON of a series rather than a whole show.

    THE CALLER TREATS THIS READ AS A SOURCE'S COMPLETE STATEMENT ABOUT A TITLE
    and replaces everything it had stored for that source (see
    watch_history._set_show_baseline). That is sound when the id names the whole
    show, and destructive when it names one season of it: Simkl models each anime
    season as its own title, so asking about Beastars' season-1 title answers
    about season 1 and NOTHING about seasons 2 or 3 — which the caller then reads
    as "this service has seen none of them" and retires. Observed twice on a live
    account: re-adding a season left the other two showing one service's numbers
    and none of Simkl's until a full refresh put them back.

    SO SUCH AN ID IS ANSWERED FOR AT ALL, and the caller reads the library
    instead — the one read that has every title of the series in it and files
    each under the shared identity. `relations` is what says a title is one of a
    family; a show that stands alone has none and takes the cheap per-title path,
    which is every ordinary television title and Simkl's own documented case for
    this endpoint.

    ONE CACHED LOOKUP PER ID, on the edge-cached per-title record and held for a
    day. A lookup that fails answers "not partial", which keeps a transient
    failure from turning every add into a library read.
    """
    namings = await asyncio.gather(
        *(_naming.fetch(settings, simkl_id) for simkl_id in simkl_ids),
        return_exceptions=True)
    partial = set()
    for simkl_id, naming in zip(simkl_ids, namings):
        if isinstance(naming, BaseException):
            logger.warning("simkl could not be asked whether %s is one season of a series: %s",
                           simkl_id, naming)
            continue
        if naming.siblings:
            partial.add(int(simkl_id))
    return partial


async def fetch_progress_details(settings: Settings,
                                 show_ids) -> dict[int, dict[int, dict[int, str]]]:
    """{simkl_id: {season: {episode: watched_at}}} for several shows at once.

    IN PRACTICE THIS ANSWERS ABOUT NOTHING, and that is a fact about the endpoint
    rather than about this account. Measured 2026-08-21: `POST /sync/watched`
    returns `{"simkl": id, "result": true, "list": ..., "last_watched_at": ...}`
    and no `seasons` block for any title — finished or part-way through, anime or
    television, `episode_watched_at=yes` notwithstanding. It says WHETHER a title
    has been watched, never HOW MUCH. So every id comes back absent (see the loop
    below for why absent and not empty), and a caller that needs Simkl's per-title
    progress reads the library instead — which is the one read that has it, and
    the only shape that can carry "finished, episode numbers unknown".

    KEPT RATHER THAN DELETED because SyncPort declares it and because the parsing
    is what would be needed if the endpoint ever did itemize: a title that comes
    back WITH a breakdown is still read and still answered for. What changed is
    only that a title without one is no longer reported as a viewer having seen
    none of it.

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

    AN ID THIS ANSWERED ABOUT IS PRESENT EVEN WHEN THE ANSWER IS "none of it", and
    an id it could NOT answer about is absent — see SyncPort.fetch_progress_details,
    where that distinction is declared. A batch whose request failed leaves every
    id in it absent, which is what stops one refused POST being read as a whole
    chunk of the roster having been watched by nobody.
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
    partial = await _speaks_for_one_season(settings, unique)
    client = transport.sync_client()
    # `episodes` IS THE DOCUMENTED VALUE. This app sent `extended=full`, which
    # Simkl accepts as an alias and answers identically — but `full` is not among
    # the values this endpoint documents (`counters`, `episodes`, `specials`), and
    # an undocumented spelling is one deprecation away from silently returning
    # counts. `specials` is deliberately not asked for: season 0 has no place in a
    # season's episode count and every reader here already drops it.
    url = _api_url("sync/watched", settings, {"extended": "episodes",
                                              "episode_watched_at": "yes"})
    with span("simkl.progress_details", n=len(unique)):
        for offset in range(0, len(unique), PROGRESS_BATCH):
            batch = unique[offset:offset + PROGRESS_BATCH]
            # NEVER CACHED, AND IT COULD NOT BE. This is a POST whose meaning is
            # entirely in its body, and the response cache is keyed by URL — two
            # different rosters would share one key. It is also one person's
            # viewing, which is the other reason.
            resp = await transport.send(
                client, "POST", url, pool=transport.SYNC_POOL,
                headers=transport.api_headers(settings, private=True),
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
                seasons = _progress_from_seasons(entry)
                if int(stated) in partial:
                    # SAYS NOTHING RATHER THAN SAYING PART. See `_speaks_for_one_season`.
                    continue
                if seasons:
                    out[int(stated)] = seasons
                elif entry.get("result") is UNWATCHED:
                    # THE ONE ANSWER THAT REALLY IS A ZERO: Simkl knows the title
                    # and this viewer has not watched it. Retiring what was stored
                    # is exactly right. `not_found` is NOT this — see UNRESOLVED —
                    # and neither is a title that answered with no breakdown.
                    out[int(stated)] = {}
                # OTHERWISE ABSENT, AND THAT IS THE WHOLE FIX. An answer with no
                # episode numbers in it is not an answer of zero: Simkl HOLDS the
                # title and has simply not said how much of it was watched. Absence
                # is what says so — see SyncPort.fetch_progress_details, where it
                # means "I have nothing to tell you about this one" and the caller
                # leaves the stored counts alone, while an empty map means "seen
                # none of it" and retires every season of the title.
                #
                # MEASURED 2026-08-21, AND IT IS EVERY TITLE THE VIEWER HOLDS.
                # `POST /sync/watched` answers `{"simkl": id, "result": true,
                # "list": ..., "last_watched_at": ...}` with no `seasons` block —
                # for a finished title, for one part-way through, for anime and
                # for television alike, `episode_watched_at=yes` notwithstanding.
                # Twin Peaks, itemized down to eight episodes by the library read,
                # comes back from here with nothing. So this endpoint says WHETHER
                # a title has been watched and never HOW MUCH, and every answer it
                # gave was being read as a count of none.
                #
                # NOT SOMETHING THIS SHAPE COULD CARRY ANYWAY, for the finished
                # half of it: "all of it, episode numbers unknown" is a claim
                # rather than a count, and the only shape in this app that holds
                # one is LibraryEntry.unlisted_seasons, which fetch_library
                # produces and this return type has no room for. A caller that
                # needs a real answer reads the library — see
                # watch_history.baseline_show, which falls back to one for a
                # source that has it — and this says honestly that it cannot say.
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
