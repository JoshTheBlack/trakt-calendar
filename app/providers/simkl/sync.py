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
from urllib.parse import urlencode

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
# Named rather than spelled inline because one read treats it differently from
# the other: an anime item's season number is Simkl's own and has to be
# translated before anything files it (see _as_the_tracker_keys_them).
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
        pool=transport.SYNC_POOL, private=True, raise_errors=True)
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
    params = {"episode_watched_at": "yes", "extended": "full"}
    if start_at:
        params["date_from"] = start_at
    try:
        data = await transport.cached_get(
            transport.sync_client(), settings, f"sync/all-items/{media}/{status}",
            params, pool=transport.SYNC_POOL, private=True, raise_errors=True)
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
    is read as an itemized one. A finished anime SEASON-title arrives from this
    bucket stating no seasons at all and is rewritten with the one season it is
    (see `_as_the_tracker_keys_it`), which turns its whole-title "everything
    here is watched" into the ordinary statement every other translated
    season-title makes: these episodes of this season, and nothing said about the
    others beyond what the rest of the library says. Keeping WATCHED on such an
    item would carry that claim onto the SERIES' other seasons, which is the one
    outcome the rewrite exists to avoid.

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


def _own_seasons(item: dict) -> list[int]:
    """The season numbers THIS library item uses for its own episodes, in order.

    Empty for an item with no `seasons[]` block at all, which is every item in
    the `completed` bucket — see `_unlisted_claim` for why that bucket states
    itself in counts instead.
    """
    return sorted({int(season["number"]) for season in item.get("seasons") or []
                   if season.get("number") is not None})


def _retitled(item: dict, naming: _naming.Naming, seasons: list[dict]) -> dict:
    """`item` rewritten as the SHOW the tracker knows, carrying `seasons`.

    Two edits, and they are one fact: this title's ids become the ones its own
    per-title record knows the parent series by (a season-title's library payload
    carries only `mal` — measured 2026-08-18, Sousou no Frieren's season-2 title
    arrives with mal/anilist/kitsu/anidb and no tmdb at all), and its locally
    numbered season becomes the season of that series it actually is. Doing one
    without the other would be worse than doing neither: the ids alone would fold
    a second season's episodes onto the first's numbers, and the season alone
    would leave a correctly numbered season on a key nothing else uses.

    WHAT THE SEASONS BLOCK SHOULD BE IS THE CALLER'S QUESTION, because there are
    two answers to it and they come from opposite directions: a part-watched title
    already lists its episodes and only needs them renumbered (`_renumbered`),
    while a finished one lists nothing and states a count instead
    (`_all_of_season`). Both produce the same shape, and everything downstream of
    here reads that shape and never asks which of the two made it.

    The item is COPIED rather than edited in place. It came out of the response
    cache, which hands back a parsed document that other reads may still be
    holding, and a translation written into it would leak into them.
    """
    show = item.get("show") or {}
    return {**item, "show": {**show, "ids": {**_entry_ids(show), **naming.ids}},
            "seasons": seasons}


def _renumbered(item: dict, swap: tuple[int, int]) -> list[dict]:
    """`item`'s own `seasons[]` block with the season it numbers locally rewritten
    as the season of the series it really is."""
    local, named = swap
    return [{**season, "number": named} if int(season.get("number")) == local else season
            for season in item.get("seasons") or []]


def _finished_episode_count(item: dict) -> int | None:
    """How many episodes a `completed` item states it has watched IN FULL, or None
    when it states no such thing.

    THE "DO NOT INVENT EPISODE NUMBERS" RULE (see `_progress_from_seasons`) IS AN
    ARGUMENT ABOUT A PARTIAL COUNT AND DOES NOT REACH THIS ONE. Turning "4
    episodes watched" into "episodes 1-4" picks four of the season's numbers out
    of the air. `watched == total` with nothing left to air picks nothing: it is
    every episode the title has, and which ones they are is not in question. The
    DATES remain unknown and stay unknown — this answers how many episodes there
    are, never when any of them was seen.

    None for every shape that falls short of that: a count short of the total, a
    title still airing (`not_aired_episodes_count`), a total of zero, and a
    payload whose numbers will not parse. Each of them leaves the item alone,
    which is the answer `_unlisted_claim` already gives for the same shapes.
    """
    try:
        watched = int(item.get("watched_episodes_count") or 0)
        total = int(item.get("total_episodes_count") or 0)
        unaired = int(item.get("not_aired_episodes_count") or 0)
    except (TypeError, ValueError):  # a shape we do not understand is not a count
        return None
    return total if total > 0 and watched == total and unaired == 0 else None


def _all_of_season(season: int, total: int) -> list[dict]:
    """A `seasons[]` block saying every episode of `season` was watched, undated.

    THE NUMBERS ARE 1..total BECAUSE THAT IS THE SEASON-TITLE'S OWN NUMBERING —
    Simkl gives each anime season-title its own episode list starting at 1, which
    is precisely why `_renumbered` rewrites the SEASON and leaves the episode
    numbers alone. A finished title's numbering is the same numbering; the only
    difference is that it was never itemized.

    NO `watched_at` ANYWHERE, deliberately. Simkl hands over counts and not
    timestamps for a completed title, so there is no date to carry and inventing
    one would place a play in a month nobody watched it in. An undated play is a
    shape the tracker already has an answer for: it counts, it does not date the
    season's completion, and it never overwrites a dated play of the same episode
    (see `_fold_library_item`).
    """
    return [{"number": season,
             "episodes": [{"number": number} for number in range(1, total + 1)]}]


def _needs_naming(item: dict, status: str) -> bool:
    """Whether asking Simkl what season a title is could change how it files.

    Two shapes can be answered and everything else cannot. A title stating exactly
    ONE season of its own is a candidate for renumbering — a title spanning several
    is already numbering them the way the show does. A title from the `completed`
    bucket stating a whole-title count is a candidate for scoping, and it is the
    one place a title's season has to come from the naming record alone, because
    the payload itemizes nothing to read it off.
    """
    if len(_own_seasons(item)) == 1:
        return True
    return status == COMPLETED_STATUS and _finished_episode_count(item) is not None


def _as_the_tracker_keys_it(item: dict, naming: _naming.Naming, status: str) -> dict:
    """One anime library item as the tracker's show and season, or unchanged.

    THE SEASON IT LISTS AND THE SEASON IT IS ARE THE SAME QUESTION ASKED OF
    DIFFERENT PAYLOADS, which is why one function answers both: an itemized title
    is renumbered from what it lists, and a finished one — which lists nothing —
    is given the season the naming record names outright. Neither computes a
    second translation; `_naming` is asked once and both readings come off the one
    `Naming`.

    UNCHANGED IS ALWAYS AN AVAILABLE ANSWER and is the one every unrecognised
    shape gets: no season named, an ambiguous mapping, a season the title already
    uses, a completed title still airing. `_naming.translation` states why that is
    the right fallback rather than a guess.
    """
    own = _own_seasons(item)
    if own:
        swap = _naming.translation(naming, own)
        return item if swap is None else _retitled(item, naming, _renumbered(item, swap))
    if status != COMPLETED_STATUS or naming.season is None:
        return item
    total = _finished_episode_count(item)
    return item if total is None else _retitled(item, naming, _all_of_season(naming.season, total))


async def _as_the_tracker_keys_them(settings: Settings, items: list[dict],
                                    status: str) -> list[dict]:
    """`items` from one ANIME bucket, each translated out of Simkl's season-title
    numbering and into the show-and-season the tracker files records under.

    WHY ONLY ANIME. Simkl models each anime SEASON as its own catalogue title,
    numbering its episodes from 1 and carrying the parent series' shared ids only
    on its per-title record — `_naming` holds the measurement and the rule. The
    television catalogue models seasons as seasons, so asking the same question
    of 1034 `shows` items would spend a lookup each to learn nothing.

    `status` IS THE BUCKET THAT WAS ASKED FOR, not the item's own status field,
    for the reason `_unlisted_claim` gives: the request named the list, so a
    payload whose status drifts cannot flip what a whole bucket means.

    A FINISHED SEASON-TITLE IS TRANSLATED TOO, AND IT COSTS THE SAME LOOKUP. It
    states no seasons to renumber, so what it gets instead is the season its
    naming record names and every episode of it (`_all_of_season`) — the two
    halves of the same rewrite that a part-watched title gets. Left alone, such a
    title stays on its own bare `mal` key, permanently apart from the series it
    belongs to, holding the viewer's real progress where nothing renders it: a
    season finished on Simkl read as none of it watched. Rewriting it WITHOUT
    stating its episodes would be worse still — the bucket's "everything here is
    watched" would land on the series and claim its OTHER seasons finished.

    MEASURED COST, dev account 2026-08-18: 9 anime titles in a 1043-item library,
    so 9 cached GETs on a day-long TTL against 12 `/sync/all-items` reads that
    are themselves the most expensive thing this module does. Reading the
    `completed` bucket the same way adds one per finished anime title, once a day
    and only in a pass that reads that bucket at all — `_wanted_buckets` skips a
    list Simkl says has not moved.

    A LOOKUP THAT FAILS LEAVES ITS ITEM ALONE rather than failing the library
    read. A library read that raised here would cost a viewer their whole Simkl
    history for a refinement to one title's season number — and `fetch_library`'s
    own contract is that a partial answer says so rather than shrinking.
    """
    namings = await asyncio.gather(
        *(_naming.fetch(settings, _entry_ids(item.get("show") or {}).get("simkl"))
          if _needs_naming(item, status) else _nothing()
          for item in items),
        return_exceptions=True,
    )
    translated = []
    for item, naming in zip(items, namings):
        if isinstance(naming, BaseException):
            logger.warning("simkl could not be asked which season a library title names: %s",
                           naming)
            translated.append(item)
            continue
        translated.append(_as_the_tracker_keys_it(item, naming, status))
    return translated


async def _nothing() -> _naming.Naming:
    """An awaitable EMPTY, so the gather above stays one expression rather than
    two loops that have to be kept in step by position."""
    return _naming.EMPTY


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
        for media, status in wanted:
            document = await _all_items(settings, media, status, None)
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
            if media == ANIME_TYPE:
                # BEFORE EITHER READER, because both of them read the season
                # number: the plays go out as events and the per-season baseline
                # is folded, and a translation applied to one alone would put a
                # viewer's plays on a different season from their progress.
                items = await _as_the_tracker_keys_them(settings, items, status)
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
    numbers that may not be the ones actually seen. That is an argument about a
    PARTIAL count and it is not one this function has to distinguish: a count
    equal to the title's total names no episodes to choose between, and where
    that case is acted on the block is written out first, before anything reaches
    here (see `_finished_episode_count` and `_all_of_season`).
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
                out[int(stated)] = _progress_from_seasons(entry)
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
