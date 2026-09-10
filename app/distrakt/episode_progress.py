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

Pure transformation: rows in, view records out, no I/O. The route reads the
database and hands the results over.
"""
from __future__ import annotations

from typing import Mapping, NamedTuple, Sequence
from urllib.parse import quote


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
    each service keeps its own tick and the two are named wherever their totals
    disagree. A tick with no attribution turns that disagreement into what looks
    like a bug in the count.

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
        self._services = tuple(services) or tuple(sorted(by_source)) or ("trakt",)
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

    def recorded_by(self, number) -> tuple[str, ...]:
        """Which services recorded this episode, and EMPTY where they agree
        overall — the same rule `counts` follows."""
        if not self._disagree:
            return ()
        n = _as_number(number)
        return tuple(name for name in self._services
                     if n in self._by_source.get(name, ()))

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


def _as_number(value) -> int:
    """An episode number from whatever the payload spelled it as. -1 rather than
    an exception: an unparsable number simply matches nothing, which draws hollow
    ticks on a row rather than taking the whole panel down."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return -1
