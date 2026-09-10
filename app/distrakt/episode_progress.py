"""What one viewer's record of a season looks like on an episode row.

THE TICKS ARE THE TRACKER'S AND THE ROW IS EVERYBODY'S. templates/_details.html
draws one details panel for the calendar, the share page and the tracker alike,
and it grows a tick per service only where it is handed a `SeasonProgress`. This
module is what builds one, and it lives here because everything it knows — which
services this account syncs, which of them recorded an episode, and where a
correction would have to be made — is the tracker's own.

NO SERVICE NAMES ARE SPELLED HERE. A tick carries its source and the template
asks templates/_source_logo.html what that service is called, which is the one
place in the app that answers it. This was JavaScript with its own SERVICE_NAMES
table beside that macro's, which is two spellings of one fact.

I/O AT ONE EDGE AND PURE TRANSFORMATION BEHIND IT. `for_season` is the single
read — one query against distrakt_show_progress plus the account's own list of
services — and everything below it takes rows and answers view records. It reads
here rather than in each route because TWO surfaces now draw these ticks: the
tracker's own panel, and the calendar's for a title this viewer tracks. The
identity a calendar card resolves to is the identity that table is keyed on
(app/providers/base.py's MATCH_SOURCES, since the column rekey), so the second
surface is one call rather than a second assembly of the same five parts.
"""
from __future__ import annotations

import json
from typing import Mapping, NamedTuple, Sequence
from urllib.parse import quote

from . import viewer, watch_history
from .. import db
from ..providers.base import ItemKey


class Tick(NamedTuple):
    """One service's answer about one episode, and where to go about it.

    `url` is empty where this title carries no id for that service. A tick is
    what you press to go and correct the count it disagrees with, so a tick with
    nowhere useful to go is drawn as plain text rather than as a link somewhere
    unhelpful — every tick opening Trakt whatever service had recorded the
    episode is the fault this shape replaced.
    """
    source: str
    seen: bool
    url: str


class SourceCount(NamedTuple):
    """How many episodes of the season one service recorded."""
    source: str
    count: int


def _trakt_episode_url(slug: str, trakt_id: str, season: int, number: int) -> str:
    """https://app.trakt.tv/shows/<slug|id>?season=N&view=episode&episode=M

    THE SLUG IS PREFERRED AND THE ID IS THE FALLBACK, the same order the Simkl
    link uses and for the same reason: a service resolves its own numeric id
    perfectly well, but doing so costs it a title lookup and a redirect it would
    not have needed. Sending the readable name when we know it is what both
    services ask for.

    A ROW WITH NEITHER GETS NO LINK. Trakt's slug arrives on history events, so a
    title with no recent play can carry an id for years and still have no name —
    the id alone reaches the right page, which is why it is a fallback rather
    than a second requirement.
    """
    name = slug or str(trakt_id or "")
    if not name:
        return ""
    return (f"https://app.trakt.tv/shows/{quote(name, safe='')}"
            f"?season={season}&view=episode&episode={number}")


def _simkl_url(slug: str, simkl_id: str) -> str:
    """https://simkl.com/tv/<id>[/<slug>]

    THE ID IS THE STABLE IDENTIFIER AND THE SLUG IS DECORATION — Simkl says so
    outright, and asks that the slug be included when it is known. So a title we
    have no Simkl slug for still links, by id alone. Simkl is opened at the
    TITLE, which is as deep as an id from a roster row reaches.
    """
    if not simkl_id:
        return ""
    base = f"https://simkl.com/tv/{quote(str(simkl_id), safe='')}"
    return f"{base}/{quote(slug, safe='')}" if slug else base


class SeasonProgress:
    """One viewer's record of one season, as the episode rows need it.

    A TICK IS PER SERVICE AND THE WATCHED MARK IS THE UNION. Watching happens
    once, so an episode either service saw is an episode this person watched —
    anything narrower under-reports what they actually did. But the two records
    really do differ (one service scrobbles, the other was linked last week), so
    each service keeps its own tick and every row names the services that were
    ASKED. A tick with no attribution turns that disagreement into what looks like
    a bug in the count; the per-service TOTALS above the list are the narrower
    claim and stay behind a real disagreement.

    EACH SERVICE'S OWN SLUG, AND NOTHING ELSE. Trakt and Simkl both call a
    title's readable name `slug` and disagree on what it is — `the-traitors-2023`
    against `the-traitors` — so the shared field holds whichever service wrote
    last, with nothing recording which. Reading it as a fallback for either
    produced live 404s: a Simkl-only title carrying Simkl's name for it was
    handed to the Trakt link builder, which dutifully built a Trakt URL for a
    title Trakt has never heard of.
    """

    def __init__(self, services: Sequence[str], by_source: Mapping[str, Sequence[int]],
                 source_ids: Mapping[str, str], slugs: Mapping[str, str], season: int):
        # EVERY SERVICE THIS ACCOUNT SYNCS, not only the ones with something to
        # say about this season: a hollow tick is the whole answer to "which of
        # them is missing it", and a service dropped for having recorded nothing
        # would leave the reader unable to tell "Simkl has not seen this" from
        # "this account has no Simkl". The two fallbacks behind it are for an
        # account whose linked services cannot be listed at all — a season that
        # recorded something still says who recorded it, and one that recorded
        # nothing still draws the single tick this panel has always drawn.
        self._services = _services_for(services, by_source)
        self._by_source = {name: {_as_number(n) for n in numbers}
                           for name, numbers in by_source.items()}
        self._ids = {name: str(source_ids.get(name) or "") for name in self._services}
        self._slugs = {name: str(slugs.get(name) or "") for name in self._services}
        self._season = season
        self._watched = {n for numbers in self._by_source.values() for n in numbers}
        # A SERVICE THAT ANSWERED FOR FEWER EPISODES THAN THE UNION HOLDS is one
        # the reader has to be told about; agreement needs no caption, and
        # captioning it everywhere would put a service's name on every row of a
        # one-service account's list.
        self._disagree = len(self._services) > 1 and any(
            len(self._by_source.get(name, ())) != len(self._watched)
            for name in self._services)

    @property
    def services(self) -> tuple[str, ...]:
        """Every service this account syncs, which is who the ticks are drawn
        for. Public because tests read it; no route branches on it."""
        return self._services

    @property
    def single(self) -> bool:
        """Whether a tick may be drawn without saying whose it is. With one
        service there is nothing to disambiguate, and a mark on every row of a
        checklist would ask to be recognized for no reason."""
        return len(self._services) < 2

    @property
    def count(self) -> int:
        return len(self._watched)

    @property
    def counts(self) -> tuple[SourceCount, ...]:
        """Per-service totals for the note above the list, and EMPTY where the
        services agree."""
        if not self._disagree:
            return ()
        return tuple(SourceCount(name, len(self._by_source.get(name, ())))
                     for name in self._services)

    def watched(self, number) -> bool:
        return _as_number(number) in self._watched

    @property
    def checked_by(self) -> tuple[str, ...]:
        """Every service that could actually be ASKED about this title, in
        declared order — and empty unless there are at least two of them.

        NAMED ON EVERY ROW RATHER THAN ONLY WHERE THEY DISAGREE, because the two
        facts a reader needs are different: the ticks say who RECORDED an episode,
        and this says who was LOOKED AT. Naming only the recorders made "Simkl has
        this and Trakt has not published it yet" and "only Simkl was ever
        consulted" render identically, which is exactly the question somebody
        opens this list to settle.

        SYNCING A SERVICE IS NOT THE SAME AS BEING ABLE TO ASK IT ABOUT THIS
        TITLE, and reading it as such is what made this claim false. A record
        carries an id per service and frequently carries only one — a season added
        from one service keeps that service's id and no other, for ever, on an
        account that syncs both. The other service was never asked about it and
        has no way to be, so naming it here said something that had not happened.
        THIS IS THE SAME SET WHOSE TICKS ARE LINKS: a tick with nowhere to go is a
        service this title cannot be looked up at, which is the same fact.
        `live.named_sources` is the roster row's own spelling of it — the note
        that reads "(Trakt)" beside the counts — and the two now agree.

        EMPTY BELOW TWO, because there is then nothing to disambiguate — the same
        rule that stops a single tick wearing a mark.
        """
        askable = tuple(name for name in self._services if self._ids[name])
        return askable if len(askable) > 1 else ()

    def ticks(self, number) -> tuple[Tick, ...]:
        n = _as_number(number)
        return tuple(Tick(name, n in self._by_source.get(name, ()), self._url_for(name, n))
                     for name in self._services)

    def _url_for(self, name: str, number: int) -> str:
        if name == "trakt":
            return _trakt_episode_url(self._slugs["trakt"], self._ids["trakt"],
                                      self._season, number)
        if name == "simkl":
            return _simkl_url(self._slugs["simkl"], self._ids["simkl"])
        # A service registered later links nowhere until somebody says where its
        # episodes live; its ticks still draw, as text.
        return ""


def _services_for(services: Sequence[str], by_source: Mapping[str, Sequence[int]]
                  ) -> tuple[str, ...]:
    """Whose ticks a row draws, with the fallbacks applied ONCE.

    EVERY SERVICE THIS ACCOUNT SYNCS, not only the ones with something to say
    about this season: a hollow tick is the whole answer to "which of them is
    missing it", and a service dropped for having recorded nothing would leave the
    reader unable to tell "Simkl has not seen this" from "this account has no
    Simkl". The two fallbacks behind it are for an account whose linked services
    cannot be listed at all — a season that recorded something still says who
    recorded it, and one that recorded nothing still draws the single tick this
    panel has always drawn.

    IT IS A FUNCTION RATHER THAN A LINE IN THE CONSTRUCTOR because the ids a tick
    LINKS to are gathered against this same list, and gathering them against the
    raw one is a bug that shipped: an account syncing nothing fell back to Trakt
    here and to nothing there, so the row drew a tick with no link on the one
    surface where the link is the entire point.
    """
    return tuple(services) or tuple(sorted(by_source)) or ("trakt",)


def _as_number(value) -> int:
    """An episode number from whatever the payload spelled it as. -1 rather than
    an exception: an unparsable number simply matches nothing, which draws hollow
    ticks on a row rather than taking the whole panel down."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return -1


async def for_season(user_id: int, key: ItemKey, season: int,
                     ids: Mapping[str, object]) -> SeasonProgress:
    """One viewer's record of one season of one title, ready for the panel.

    THE IDENTITY IS THE SHARED ONE, which is what lets a second surface ask this
    at all. `distrakt_show_progress` is keyed on (user_id, media, match_source,
    match_id, season, source) — the waterfall in app/providers/base.py, not any
    one service's id — so a calendar card, a roster row and a board item that
    name the same title name the same rows. No provider call, no token and no id
    translation is involved: it is a read of what this app has already stored for
    this person.

    `ids` IS WHERE THE TICKS' LINKS COME FROM and each surface passes its own —
    the roster row's for the tracker, the card's plus the answer's for the
    calendar. Both are the ids that surface would build an outbound button from,
    which is what keeps a service with a working tick and a working button the
    same service.

    ALWAYS A SeasonProgress, NEVER None: a season with nothing recorded still has
    something to say (every synced service drew a hollow tick), and whether that
    is worth drawing is the surface's decision rather than this function's. A
    caller that only wants ticks where there is viewing to show asks `count`.
    """
    rows = await db.fetch_all(
        "SELECT source, watched_episodes_json, trakt_id, simkl_id "
        "FROM distrakt_show_progress "
        "WHERE user_id = ? AND media = ? AND match_source = ? AND match_id = ? "
        "AND season = ? ORDER BY source",
        (user_id, key.media, key.match_source, key.match_id, season),
    )
    # ONE ROW PER SERVICE, AND EVERY ONE OF THEM IS READ. This used to be a
    # fetch_one with no `source` predicate, which on an account syncing two
    # services returned whichever row the database happened to hand back first —
    # so the ticks were one service's, chosen arbitrarily, and the viewer was
    # never told whose. Nothing was corrupted by it, because this only draws;
    # what it produced was a season reading "6 watched" beside a row counting 8,
    # with no way to tell that two answers existed.
    #
    # watch_history owns what that column holds — {episode: watched_at} now, a
    # bare list of numbers before dates were stored — so the shape is read there
    # rather than guessed at again here. Guessing at it here is exactly how this
    # came to answer "nothing watched" for everyone: it read the dated mapping as
    # a list and dropped every entry.
    by_source: dict[str, list[int]] = {}
    for row in rows:
        try:
            stored = json.loads(row["watched_episodes_json"] or "{}")
            episodes = sorted(int(ep) for ep in watch_history.episode_watches(stored))
        except (TypeError, ValueError):
            episodes = []
        if episodes:
            by_source[str(row["source"])] = episodes
    # EVERY SERVICE THIS ACCOUNT SYNCS, not only the ones with something to say
    # about this season. A tick per service is only readable if a service that
    # recorded NOTHING still gets one — that hollow mark is the whole answer to
    # "which of them is missing it", and a service that dropped out of the list
    # for having no episodes would leave the reader unable to tell "Simkl has
    # not seen this" from "this account has no Simkl".
    settings = await viewer.settings_for_user(user_id)
    # RESOLVED HERE, not inside SeasonProgress, because `_source_ids` below has to
    # gather ids for the SAME list the ticks are drawn for — see `_services_for`.
    services = _services_for(
        [str(name) for name, _port in await watch_history.tracker_ports(settings, user_id)],
        by_source)
    return SeasonProgress(
        services, by_source,
        # WHAT EACH SERVICE'S TICK LINKS TO. A tick is where you go to correct a
        # record, so it has to open the service that holds the wrong one — every
        # one of them pointing at Trakt was the reported fault. Only the ids this
        # title actually carries travel; a service with none gets no link and its
        # tick is drawn as plain text.
        source_ids=_source_ids(services, ids, rows),
        # EACH SERVICE'S OWN SLUG, because they do not agree on one. A single
        # `slug` holds whichever service wrote last, which made the Trakt link
        # land nowhere whenever the value had come from Simkl — so it is not read
        # here at all. See store.ID_COLUMNS.
        slugs={"trakt": str(ids.get("trakt_slug") or ""),
               "simkl": str(ids.get("simkl_slug") or "")},
        season=season)


def _source_ids(services: Sequence[str], ids: Mapping[str, object],
                progress_rows: Sequence) -> dict[str, str]:
    """The id each service knows this title by, for the links its ticks carry.

    THE CALLER'S IDS ARE NOT THE ONLY PLACE THEY LIVE, and taking them as such is
    what left half the ticks unlinked. A roster row is filed under the shared
    identity waterfall and keeps whichever service ids it happened to arrive with
    — a season added from Trakt carries a Trakt id and no Simkl one, for ever,
    even on an account that syncs both. But the PROGRESS rows beside it are
    written by each service's own sync, and each one records the id that service
    knows the title by. Measured on a real roster: 41 of 46 seasons had no Simkl
    id on the roster row while their progress rows carried one.

    So the caller's ids are asked first and the progress rows fill the gaps. A
    service still missing after both simply has no link, and its tick is drawn as
    text rather than pointing somewhere unhelpful.
    """
    found = {name: str(ids[name]) for name in services if str(ids.get(name) or "")}
    for row in progress_rows:
        for name, column in (("trakt", "trakt_id"), ("simkl", "simkl_id")):
            if name in services and name not in found and str(row[column] or ""):
                found[name] = str(row[column])
    return found
