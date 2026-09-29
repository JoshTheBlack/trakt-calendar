"""Simkl's private, per-person reads: the beacon, the history sweep and the
batched progress record.

THE ONE THING THIS FILE EXISTS TO PIN is that none of it can ever reach the
shared response cache. The cache is keyed by the URL and Simkl carries the token
in a header, so every account's /sync/ request has the identical URL — a single
call made without `private=True` would serve one person's viewing to the next
one who asked. Every call is asserted, by name, rather than spot-checked.

No network: the transport's two entry points are patched and the calls they were
handed are inspected. That is also what makes "this went through SYNC_POOL"
assertable — the pool a call names is an argument, deliberately, so it cannot be
defaulted into the parallel-allowed one.
"""
from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app.providers.base import UnlistedSeasons
from app.providers.simkl import sync, transport

SETTINGS = SimpleNamespace(simkl_client_id="cid", simkl_access_token="tok",
                           cache_ttl_minutes=10)


def _cached_get(*answers):
    """A stand-in for transport.cached_get that serves `answers` in order and
    records every call, so what was asked and how is inspectable.

    AN ANSWER THAT IS AN EXCEPTION IS SERVED THE WAY THE REAL FUNCTION SERVES A
    FAILURE: raised when the caller passed `raise_errors=True`, and handed back as
    None otherwise. That flag is the whole mechanism under test — a double that
    raised whatever it was asked would pass just as happily against the code that
    swallowed every refusal into an empty document.
    """
    served = list(answers)

    async def _answer(*_args, raise_errors=False, **_kwargs):
        value = served.pop(0)
        if isinstance(value, Exception):
            if raise_errors:
                raise value
            return None
        return value

    return AsyncMock(side_effect=_answer)


BUCKETS = [(media, status) for media in sync.LIBRARY_TYPES for status in sync.WATCHED_STATUSES]


def _library_get(*answers):
    """`_cached_get`, for a stand-in whose answers are written PER LIST, in the
    order the per-list reads ask for them — which a whole-library read now asks
    for in one call (`sync/all-items/`). That call takes the next twelve answers
    at once and is served them folded into one document, each item carrying its
    list's `status` as Simkl's own whole answer does (measured: every item's
    status names the list it is filed under). A failure among them fails the one
    call. A per-list call is served one answer, as before."""
    served = list(answers)

    async def _answer(_client, _settings, path, *_args, raise_errors=False, **_kwargs):
        if path != "sync/all-items/":
            value = served.pop(0)
        else:
            parts, served[:len(BUCKETS)] = served[:len(BUCKETS)], []
            failure = next((part for part in parts if isinstance(part, Exception)), None)
            value = failure if failure is not None else {}
            if failure is None:
                for (media, status), part in zip(BUCKETS, parts):
                    for item in (part or {}).get(media) or []:
                        value.setdefault(media, []).append({"status": status, **item})
        if isinstance(value, Exception):
            if raise_errors:
                raise value
            return None
        return value

    return AsyncMock(side_effect=_answer)


class PrivacyTests(unittest.IsolatedAsyncioTestCase):
    """Every read here is one person's. None of it may be cached, and none of it
    may travel on the pool that allows parallel requests."""

    async def test_every_get_is_private_and_on_the_sync_pool(self):
        answers = [
            {"tv_shows": {"all": "T"}},                       # the beacon
            *[{} for _ in range(len(BUCKETS))],               # every history bucket
        ]
        spy = _cached_get(*answers)
        with patch("app.providers.simkl.transport.cached_get", new=spy):
            await sync.fetch_last_activities(SETTINGS)
            await sync.fetch_history(SETTINGS)
        self.assertGreater(spy.await_count, 1)
        for call in spy.await_args_list:
            with self.subTest(path=call.args[2]):
                self.assertIs(call.kwargs["private"], True)
                self.assertIs(call.kwargs["pool"], transport.SYNC_POOL)

    async def test_nothing_here_writes_to_simkl(self):
        """This build reads a person's viewing and never edits it. The write path
        is POST /sync/history, and no call in this module may name it."""
        send = AsyncMock()
        with patch("app.providers.simkl.transport.send", new=send), \
             patch("app.providers.simkl.transport.cached_get",
                   new=_cached_get(*[{}] * (len(BUCKETS) + 1))):
            await sync.fetch_history(SETTINGS)
        for call in send.await_args_list:
            self.assertNotIn("sync/history", call.args[2])


class BeaconTests(unittest.IsolatedAsyncioTestCase):
    """The beacon answers in the shape SyncPort declares, not in Simkl's."""

    async def test_the_newest_of_two_episode_lists_is_the_episode_stamp(self):
        """Simkl files anime apart from television and both move a person's
        episode history, so the beacon is the newer of the two. The tracker gates
        on this blob and must not have to know that."""
        payload = {"tv_shows": {"all": "2026-07-01T00:00:00Z",
                                "removed_from_list": "2026-06-01T00:00:00Z"},
                   "anime": {"all": "2026-08-01T00:00:00Z"},
                   "movies": {"all": "2026-05-05T00:00:00Z",
                              "removed_from_list": "2026-05-06T00:00:00Z"}}
        with patch("app.providers.simkl.transport.cached_get", new=_cached_get(payload)):
            beacon = await sync.fetch_last_activities(SETTINGS)
        self.assertEqual(beacon["episodes"], {"watched_at": "2026-08-01T00:00:00Z",
                                              "removed_at": "2026-06-01T00:00:00Z"})
        self.assertEqual(beacon["movies"], {"watched_at": "2026-05-05T00:00:00Z",
                                            "removed_at": "2026-05-06T00:00:00Z"})

    async def test_the_per_list_stamps_ride_along_beside_the_contract_shape(self):
        """The four values above are the whole of what the beacon CONTRACT asks
        for and they answer "has anything changed". The per-list stamps answer
        "which of the twelve buckets changed", which is the difference between
        re-reading a library and re-reading one list of it — so they travel back
        to this module rather than being thrown away at the boundary.

        A LIST NOBODY HAS EVER USED IS null AND SAYS SO. A status the payload does
        not mention at all is left out, because "Simkl did not say" and "Simkl said
        never" are different answers and only the second is safe to act on.
        """
        payload = {"tv_shows": {"all": "T", "watching": "T1", "completed": "T2",
                                "hold": None, "dropped": None},
                   "anime": {"all": "A", "watching": "A1"}}
        with patch("app.providers.simkl.transport.cached_get", new=_cached_get(payload)):
            beacon = await sync.fetch_last_activities(SETTINGS)
        self.assertEqual(beacon["lists"]["shows"],
                         {"watching": "T1", "completed": "T2",
                          "hold": None, "dropped": None})
        self.assertEqual(beacon["lists"]["anime"], {"watching": "A1"})
        self.assertNotIn("movies", beacon["lists"])

    async def test_a_beacon_that_could_not_be_read_raises(self):
        """IT IS NOT AN UNCHANGED BEACON, and answering an empty blob said it was.
        The caller compares this against what it stored last time; an empty answer
        claims all four stamps are absent, which compares equal to a stored empty
        one and gates the sync as "nothing has moved". A service that was down
        would report itself up to date for as long as it stayed down, and every
        conclusion drawn afterwards would rest on that."""
        with patch("app.providers.simkl.transport.cached_get",
                   new=_cached_get(transport.SimklError("Simkl rejected it", 401))):
            with self.assertRaises(transport.SimklError):
                await sync.fetch_last_activities(SETTINGS)

    async def test_a_body_with_nothing_in_it_is_still_an_empty_blob(self):
        """Which is a different thing entirely: the call SUCCEEDED and Simkl had
        nothing to say. A refusal has already raised by the time this returns."""
        with patch("app.providers.simkl.transport.cached_get", new=_cached_get(None)):
            self.assertEqual(await sync.fetch_last_activities(SETTINGS), {})

    async def test_the_beacon_asks_for_a_refusal_to_be_raised(self):
        """The flag is the mechanism, and it is asserted by name because without
        it every failure above comes back as an empty blob again."""
        spy = _cached_get({"tv_shows": {"all": "T"}})
        with patch("app.providers.simkl.transport.cached_get", new=spy):
            await sync.fetch_last_activities(SETTINGS)
        self.assertIs(spy.await_args.kwargs["raise_errors"], True)


class HistoryTests(unittest.IsolatedAsyncioTestCase):
    """A library, flattened into the plays the tracker's history contract wants."""

    def _library(self):
        return {"shows": [{
            "show": {"title": "Show", "ids": {"simkl_id": 55, "tmdb": "900"}},
            "seasons": [{"number": 1, "episodes": [
                {"number": 1, "watched_at": "2026-07-02T00:00:00Z"},
                {"number": 2, "watched_at": "2026-07-09T00:00:00Z"},
                {"number": 3},                       # watched, but not dated
            ]}],
        }]}

    async def _sweep(self, documents, **kwargs):
        """One bucket per (catalogue, status), in the order the sweep asks for
        them. A document is served for the FIRST status of its catalogue and the
        rest come back empty, because Simkl files an item under exactly one
        status — a fixture that answered every bucket the same way would be
        testing a shape the service cannot produce."""
        answers = []
        for media in (*sync.EPISODE_TYPES, "movies"):
            answers += [documents.get(media) if n == 0 else {}
                        for n, _status in enumerate(sync.WATCHED_STATUSES)]
        with patch("app.providers.simkl.transport.cached_get", new=_cached_get(*answers)):
            return await sync.fetch_history(SETTINGS, **kwargs)

    async def test_a_library_becomes_dated_episode_plays(self):
        events = await self._sweep({"shows": self._library()})
        self.assertEqual(
            [(e["episode"]["season"], e["episode"]["number"]) for e in events],
            [(1, 1), (1, 2)])
        self.assertEqual(events[0]["show"]["ids"], {"simkl": 55, "tmdb": "900"})

    async def test_an_undated_episode_is_not_an_event(self):
        """It cannot be placed in a month, which is the only thing the sweep is
        for. It still reaches the tracker through the progress baseline, where a
        missing date reads as "unknown" rather than as a play on the epoch."""
        events = await self._sweep({"shows": self._library()})
        self.assertNotIn(3, [e["episode"]["number"] for e in events])

    async def test_the_window_bounds_episodes_and_not_only_items(self):
        """`date_from` bounds which ITEMS come back, not which episodes inside
        them, so an item that moved yesterday arrives carrying its whole history.
        Without re-applying the bound here, "what did I watch recently" would
        answer "everything, ever"."""
        events = await self._sweep({"shows": self._library()}, start_at="2026-07-05")
        self.assertEqual([e["episode"]["number"] for e in events], [2])

    async def test_simkls_own_id_is_read_under_this_apps_spelling(self):
        """The payloads say `simkl_id` in places and `simkl` in others. Mapping
        it at the provider boundary is what keeps a second spelling out of the
        rest of the app."""
        events = await self._sweep({"movies": {"movies": [{
            "movie": {"title": "Film", "year": 2026, "ids": {"simkl": 7, "imdb": "tt1"}},
            "last_watched_at": "2026-07-04T00:00:00Z"}]}})
        self.assertEqual(sync.movie_plays_from(events),
                         [{"ids": {"simkl": 7, "imdb": "tt1"}, "title": "Film",
                           "year": 2026, "watched_at": "2026-07-04T00:00:00Z"}])

    async def test_a_bucket_that_cannot_be_read_does_not_sink_the_sweep(self):
        """Somebody whose "dropped" list fails should still have their "watching"
        list counted."""
        events = await self._sweep({"shows": self._library(),
                                    "anime": transport.SimklError("that list 500ed", 500)})
        self.assertTrue(events)

    async def test_a_sweep_that_read_no_bucket_at_all_is_not_an_empty_history(self):
        """"Nothing could be read" and "you watched nothing" are the same shape
        once every bucket is swallowed, and the second is the answer the tracker
        acts on. One bucket failing is tolerated; all of them failing is a fact
        about the connection, the service or the token, and it is not this
        module's to turn into a result."""
        with patch("app.providers.simkl.transport.cached_get",
                   new=_cached_get(*[transport.SimklError("gone", 500)] * len(BUCKETS))):
            with self.assertRaises(transport.SimklError):
                await sync.fetch_history(SETTINGS)

    async def test_a_refused_credential_stops_the_sweep_at_the_first_bucket(self):
        """It is not a property of one list. Tolerating it per bucket would
        tolerate it twelve times over and call the result a history."""
        spy = _cached_get(*[transport.SimklError("user_token_failed", 401)] * 12)
        with patch("app.providers.simkl.transport.cached_get", new=spy):
            with self.assertRaises(transport.SimklError):
                await sync.fetch_history(SETTINGS)
        self.assertEqual(spy.await_count, 1)


class LibraryTests(unittest.IsolatedAsyncioTestCase):
    """The whole library in one read, keyed by the identity the app files its own
    rows under — and only the buckets that can still be holding something new."""

    def _item(self, simkl_id=55, tmdb="900", episodes=(1, 2)):
        return {"show": {"title": "Show", "ids": {"simkl_id": simkl_id, "tmdb": tmdb}},
                "seasons": [{"number": 1, "episodes": [
                    {"number": n, "watched_at": "2026-07-0%d" % n} for n in episodes]}]}

    def _activities(self, **stamps):
        """An activities blob naming every bucket, so a test states which ones
        moved rather than relying on what is missing."""
        lists = {media: {status: stamps.get(f"{media}_{status}", "S")
                         for status in sync.WATCHED_STATUSES}
                 for media in sync.LIBRARY_TYPES}
        return {"lists": lists}

    async def _read(self, answers, **kwargs):
        spy = _library_get(*answers)
        with patch("app.providers.simkl.transport.cached_get", new=spy):
            return await sync.fetch_library(SETTINGS, **kwargs), spy

    async def test_a_library_is_keyed_by_the_shared_identity(self):
        """Not by Simkl's own id, which is the whole point: a roster record that
        names only another service still matches, and Simkl's id comes back on the
        entry rather than being needed to ask."""
        read, _ = await self._read([{"shows": [self._item()]}] + [{}] * 11)
        self.assertEqual(list(read.entries), ["show:tmdb:900"])
        entry = read.entries["show:tmdb:900"]
        self.assertEqual(entry.ids, {"simkl": 55, "tmdb": "900"})
        self.assertEqual(entry.seasons, {1: {1: "2026-07-01", 2: "2026-07-02"}})
        self.assertTrue(read.complete)

    async def test_a_title_no_shared_id_names_is_not_keyed_at_all(self):
        """There is no id in it another service could have named the same title
        by, so nothing could ever be matched to it either way."""
        item = {"show": {"title": "Only Here", "ids": {"simkl_id": 7}}, "seasons": []}
        read, _ = await self._read([{"shows": [item]}] + [{}] * 11)
        self.assertEqual(read.entries, {})

    async def test_a_title_with_nothing_watched_is_still_an_entry(self):
        """"In the library, seen none of it" is a real answer and a different one
        from "not in the library at all"."""
        item = {"show": {"title": "Show", "ids": {"simkl_id": 55, "tmdb": "900"}},
                "seasons": []}
        read, _ = await self._read([{"shows": [item]}] + [{}] * 11)
        self.assertEqual(read.entries["show:tmdb:900"].seasons, {})

    async def test_a_season_the_library_does_not_list_is_a_zero_and_says_so(self):
        """/sync/all-items carries only the seasons a title has WATCHED EPISODES
        in, so a season missing from a title in the `watching` list is one the
        viewer has seen none of — not one nobody asked about. The entry says which
        of those it means, because only this module knows the payload well enough
        to answer.

        Without it a season both services have seen none of renders as a claim
        only the other one made, and a badge appears on an agreement.
        """
        read, _ = await self._read([{"shows": [self._item()]}] + [{}] * 11)
        entry = read.entries["show:tmdb:900"]
        self.assertEqual(entry.unlisted_seasons, UnlistedSeasons.ZERO)
        # And season 1 is the only one it named, so the claim is about every OTHER
        # season of a title it holds rather than about a list it hands over.
        self.assertEqual(list(entry.seasons), [1])

    async def _completed(self, item):
        """One item in the `completed` bucket, which is the SECOND status asked
        for in the first catalogue — the buckets are read in a declared order and
        which one an item came out of is what its silence means."""
        return await self._read([{}, {"shows": [item]}] + [{}] * 10)

    async def test_a_finished_title_carries_no_seasons_and_means_the_opposite(self):
        """THE REGRESSION. Measured on a live account: 492 titles in `completed`,
        not one of them carrying a `seasons[]` key, every one of them reporting
        watched_episodes_count equal to total_episodes_count. Read the way the
        `watching` list is read, the app would report none of a show Simkl reports
        as finished — a silent wrong answer, and worse than the badge the zero
        rule was written to remove."""
        item = {"show": {"title": "Done", "ids": {"simkl_id": 55, "tmdb": "900"}},
                "status": "completed", "total_episodes_count": 91,
                "watched_episodes_count": 91, "not_aired_episodes_count": 0,
                "last_watched_at": "2026-07-04T00:00:00Z"}
        read, _ = await self._completed(item)
        entry = read.entries["show:tmdb:900"]
        self.assertEqual(entry.unlisted_seasons, UnlistedSeasons.WATCHED)
        # No episode is invented for it: the payload named none, and a fabricated
        # set would be a viewing history nobody reported.
        self.assertEqual(entry.seasons, {})
        self.assertEqual(entry.ids, {"simkl": 55, "tmdb": "900"})

    async def test_a_finished_title_with_episodes_still_to_come_claims_nothing(self):
        """"Completed" means every episode that has AIRED. The totals this app
        renders against are the season's PLANNED episode counts, so claiming
        everything watched against one would report episodes that do not exist
        yet. The title is still held — its ids and any plays still arrive — it
        simply contributes no count until it really is finished."""
        item = {"show": {"title": "Airing", "ids": {"simkl_id": 55, "tmdb": "900"}},
                "status": "completed", "total_episodes_count": 12,
                "watched_episodes_count": 8, "not_aired_episodes_count": 4}
        read, _ = await self._completed(item)
        self.assertEqual(read.entries["show:tmdb:900"].unlisted_seasons,
                         UnlistedSeasons.SILENT)

    async def test_two_items_of_one_title_making_different_claims_say_nothing(self):
        """Simkl files a title under exactly one status, so this is a payload
        nothing here can explain — an anime title also filed as television, say.
        Believing either one would be picking, and the two ways of being wrong are
        "reported none of a finished show" and "reported a show finished that is
        not"; silence is the only answer that cannot be confidently wrong."""
        watching = {"show": {"title": "Show", "ids": {"simkl_id": 55, "tmdb": "900"}},
                    "seasons": [{"number": 1, "episodes": [
                        {"number": 1, "watched_at": "2026-07-01"}]}]}
        done = {"show": {"title": "Show", "ids": {"simkl_id": 55, "tmdb": "900"}},
                "status": "completed", "not_aired_episodes_count": 0}
        read, _ = await self._read([{"shows": [watching]}, {"shows": [done]}] + [{}] * 10)
        entry = read.entries["show:tmdb:900"]
        self.assertEqual(entry.unlisted_seasons, UnlistedSeasons.SILENT)
        # And the episodes only one of them carried are still kept.
        self.assertEqual(entry.seasons, {1: {1: "2026-07-01"}})

    async def test_the_same_read_carries_the_plays_inside_it(self):
        """One pull answers both questions. Asking for the library and then for
        the history would read the same buckets twice, and they are the most
        expensive call this module makes."""
        read, spy = await self._read([{"shows": [self._item()]}] + [{}] * 11)
        self.assertEqual([e["episode"]["number"] for e in read.events], [1, 2])
        self.assertEqual(spy.await_count, 1)

    async def test_the_window_bounds_the_plays_and_never_the_library(self):
        """A baseline needs every title the person holds; only the EVENTS are
        about a window. A title filtered out by a date would read as one Simkl
        does not have, which is the answer that gets recorded permanently."""
        read, spy = await self._read([{"shows": [self._item()]}] + [{}] * 11,
                                     start_at="2026-07-02")
        self.assertEqual([e["episode"]["number"] for e in read.events], [2])
        self.assertEqual(read.entries["show:tmdb:900"].seasons,
                         {1: {1: "2026-07-01", 2: "2026-07-02"}})
        for call in spy.await_args_list:
            self.assertNotIn("date_from", call.args[3])

    async def test_a_list_that_has_never_been_used_is_never_fetched(self):
        """Its stamp is null, so it is empty and stays empty until it is not — and
        when it is not, the stamp stops being null. Skipping it costs nothing and
        leaves the read complete."""
        activities = self._activities(shows_hold=None, shows_dropped=None,
                                      anime_hold=None, anime_dropped=None)
        read, spy = await self._read([{}] * 12, activities=activities)
        # Every list it does want is wanted whole, so the whole library is still
        # one call — a never-used list simply holds nothing in that answer.
        # MEASURED: a rebaseline wanting eight lists was eight calls before this.
        self.assertEqual([call.args[2] for call in spy.await_args_list], ["sync/all-items/"])
        self.assertTrue(read.complete)
        # And asked per list — a bounded read — it is never fetched at all.
        before = self._activities(shows_watching="2026-08-01T00:00:00Z",
                                  shows_hold=None, shows_dropped=None)
        now = self._activities(shows_watching="2026-08-20T00:00:00Z",
                               shows_hold=None, shows_dropped=None)
        _read, bounded = await self._read([{}], activities=now, since=before)
        self.assertEqual([call.args[2] for call in bounded.await_args_list],
                         ["sync/all-items/shows/watching"])

    async def test_a_bucket_whose_stamp_has_not_moved_is_not_fetched(self):
        """The private endpoints support no conditional request of any kind — no
        ETag, no Last-Modified — so this per-list comparison is the only thing
        that can say "you already have that one"."""
        before = self._activities()
        now = self._activities(shows_watching="MOVED")
        read, spy = await self._read([{}] * 1, activities=now, since=before)
        self.assertEqual(spy.await_count, 1)
        self.assertEqual(spy.await_args.args[2], "sync/all-items/shows/watching")

    async def test_a_read_that_skipped_a_bucket_says_it_is_not_complete(self):
        """A title missing from a partial read says nothing at all, and its caller
        has to leave what it already knew alone."""
        read, _ = await self._read([{}], activities=self._activities(shows_watching="MOVED"),
                                   since=self._activities())
        self.assertFalse(read.complete)

    async def test_nothing_moving_reads_nothing_and_is_still_not_complete(self):
        read, spy = await self._read([], activities=self._activities(),
                                     since=self._activities())
        spy.assert_not_awaited()
        self.assertFalse(read.complete)

    async def test_a_bucket_the_stamps_do_not_mention_is_read_anyway(self):
        """A shape change at the service must cost traffic rather than
        correctness, so an unstated list is unknown rather than unchanged."""
        read, spy = await self._read([{}] * 12, activities={"lists": {}},
                                     since={"lists": {}})
        self.assertEqual([call.args[2] for call in spy.await_args_list], ["sync/all-items/"])
        self.assertTrue(read.complete)

    async def test_a_bucket_that_moved_is_asked_only_for_what_moved(self):
        """Simkl's sync guide is explicit that pulling a whole list because one
        thing in it changed is what gets a client id suspended. The bound is that
        bucket's OWN previous stamp — not a cursor shared across lists — so each
        one is asked for exactly what it has not already been asked for."""
        before = self._activities(shows_watching="2026-08-01T00:00:00Z")
        now = self._activities(shows_watching="2026-08-20T00:00:00Z")
        _read, spy = await self._read([{}], activities=now, since=before)
        self.assertEqual(spy.await_args.args[3]["date_from"],
                         "2026-08-01T00:00:00Z")

    async def test_the_stamp_is_sent_back_exactly_as_simkl_wrote_it(self):
        """"Don't reformat `date_from` locally" — so it is carried as the opaque
        string it arrived as and never parsed into a date on the way. A value this
        app normalised would silently ask for a different window than the one the
        service last reported."""
        odd = "2026-08-01T00:00:00.000+00:00"
        before = self._activities(shows_watching=odd)
        now = self._activities(shows_watching="later")
        _read, spy = await self._read([{}], activities=now, since=before)
        self.assertEqual(spy.await_args.args[3]["date_from"], odd)

    async def test_a_bounded_read_is_never_complete(self):
        """THE SAFETY PROPERTY OF THE WHOLE DELTA MODEL. `date_from` bounds which
        ITEMS come back, so a title absent from the answer has not been said to be
        gone — it has been said not to have changed. Those two arrive looking
        identical and only one is safe to act on, so a bounded read must never
        license the caller to retire anything."""
        before = self._activities(shows_watching="2026-08-01T00:00:00Z")
        now = self._activities(shows_watching="2026-08-20T00:00:00Z")
        read, _ = await self._read([{}], activities=now, since=before)
        self.assertFalse(read.complete)

    async def test_a_first_read_is_unbounded_and_may_be_complete(self):
        """A baseline needs every title the person holds. An item filtered out of
        the FIRST read would read as a title Simkl does not have, which is the
        answer that gets recorded permanently."""
        read, spy = await self._read([{}] * 12, activities=self._activities())
        self.assertEqual(spy.await_count, 1)
        for call in spy.await_args_list:
            self.assertNotIn("date_from", call.args[3])
        self.assertTrue(read.complete)

    async def test_a_whole_read_is_one_call_and_a_bounded_one_is_per_list(self):
        """Every list wanted whole is what `/sync/all-items/` with no catalogue
        and no status answers — MEASURED on a live account: the same 1,591 items
        the twelve calls return, byte-identical, in a quarter of the time. A
        bounded read asks each moved list for what moved since ITS OWN stamp, which
        one call cannot express, so it stays per list."""
        _read, whole = await self._read([{}] * 12)
        self.assertEqual([call.args[2] for call in whole.await_args_list], ["sync/all-items/"])
        before = self._activities(shows_watching="2026-08-01T00:00:00Z",
                                  movies_completed="2026-08-01T00:00:00Z")
        now = self._activities(shows_watching="2026-08-20T00:00:00Z",
                               movies_completed="2026-08-20T00:00:00Z")
        _read, bounded = await self._read([{}, {}], activities=now, since=before)
        self.assertEqual([call.args[2] for call in bounded.await_args_list],
                         ["sync/all-items/shows/watching", "sync/all-items/movies/completed"])

    async def test_the_whole_read_leaves_the_plan_to_watch_list_out(self):
        """It answers every list at once, `plantowatch` included, and a watch
        record reads only the lists a watch can be in (WATCHED_STATUSES) — a
        title only planned must not become an entry that has seen nothing."""
        planned = {"status": "plantowatch",
                   "show": {"title": "Later", "ids": {"simkl_id": 9, "tmdb": "901"}}}

        async def _whole(*_args, **_kwargs):
            return {"shows": [{"status": "watching", **self._item()}, planned]}

        with patch("app.providers.simkl.transport.cached_get", new=AsyncMock(side_effect=_whole)):
            read = await sync.fetch_library(SETTINGS)
        self.assertEqual(list(read.entries), ["show:tmdb:900"])

    async def test_a_bucket_with_no_earlier_stamp_of_its_own_is_unbounded(self):
        """A list this app has never recorded has no point in time to ask for
        changes since. Borrowing another bucket's stamp would silently skip
        everything in it older than that — a list read once, wrongly, and then
        never re-read because its stamp stops moving."""
        before = {"lists": {"shows": {"watching": "2026-08-01T00:00:00Z"}}}
        now = self._activities(shows_watching="2026-08-20T00:00:00Z")
        _read, spy = await self._read([{}] * len(BUCKETS), activities=now, since=before)
        sent = {call.args[2]: call.args[3].get("date_from")
                for call in spy.await_args_list}
        self.assertEqual(sent["sync/all-items/shows/watching"],
                         "2026-08-01T00:00:00Z")
        self.assertIsNone(sent["sync/all-items/shows/completed"])

    async def test_a_bucket_that_was_asked_for_and_refused_makes_the_read_partial(self):
        """`complete` MEANS "EVERY BUCKET I MEANT TO READ, I READ", never "I
        finished looping". The caller retires a title absent from a complete read,
        so a read that downgraded a refusal to an absence would have it delete
        watch history it merely failed to fetch. A per-list read — every moved
        list, each bounded — is the one that can lose a single list."""
        before = self._activities(shows_watching="2026-08-01T00:00:00Z",
                                  shows_completed="2026-08-01T00:00:00Z")
        now = self._activities(shows_watching="2026-08-20T00:00:00Z",
                               shows_completed="2026-08-20T00:00:00Z")
        answers = [{"shows": [self._item()]}, transport.SimklError("that list 500ed", 500)]
        read, spy = await self._read(answers, activities=now, since=before)
        self.assertEqual(spy.await_count, 2)
        self.assertFalse(read.complete)
        # And what it DID read is still there — a lost bucket costs that bucket.
        self.assertEqual(list(read.entries), ["show:tmdb:900"])

    async def test_a_whole_read_that_was_refused_is_no_read_at_all(self):
        """One call answers every list, so it cannot lose one: refused, nothing
        was read, and that raises rather than answering an empty library."""
        with self.assertRaises(transport.SimklError):
            await self._read([transport.SimklError("gone", 500)] + [{}] * 11)

    async def test_a_read_in_which_no_bucket_could_be_read_raises(self):
        """THE REGRESSION, at the level it starts. Twelve refused buckets used to
        compose into a library that reported itself complete and held zero titles
        — a read in which nothing could be read, claiming to have read
        everything."""
        with self.assertRaises(transport.SimklError):
            await self._read([transport.SimklError("gone", 500)] * 12)

    async def test_a_refused_credential_stops_the_library_read_at_once(self):
        """An authentication failure is not a bucket-level failure and must never
        be treated as one: the same token on the next list gets the same answer,
        so there is nothing to be learned by asking eleven more times."""
        spy = _cached_get(*[transport.SimklError("user_token_failed", 401)] * 12)
        with patch("app.providers.simkl.transport.cached_get", new=spy):
            with self.assertRaises(transport.SimklError):
                await sync.fetch_library(SETTINGS)
        self.assertEqual(spy.await_count, 1)

    async def test_a_read_that_asked_for_no_bucket_is_not_a_failure(self):
        """Nothing wanted is not nothing read. Every list unchanged since last
        time is an ordinary, successful, empty pass — and it is already partial,
        so it says nothing about any title."""
        read, spy = await self._read([], activities=self._activities(),
                                     since=self._activities())
        spy.assert_not_awaited()
        self.assertEqual(read.entries, {})
        self.assertFalse(read.complete)

    async def test_every_bucket_asks_for_a_refusal_to_be_raised(self):
        """The flag is what makes all of the above reachable: without it a 401
        comes back as None and reads as an empty list."""
        _read, spy = await self._read([{}] * 12)
        for call in spy.await_args_list:
            with self.subTest(path=call.args[2]):
                self.assertIs(call.kwargs["raise_errors"], True)


class PlanToWatchTests(unittest.IsolatedAsyncioTestCase):
    """A title on the viewer's Simkl watchlist can carry watched episodes.
    MEASURED on the owner's account: 42 of 47 plan-to-watch titles did — a bulk
    move had put them there — and a read that skipped the list reported Simkl
    holding nothing for them, so two seasons finished that month read as
    verdicts no service backed."""

    WATCHED = {"status": "plantowatch", "watched_episodes_count": 10,
               "last_watched_at": "2026-09-24T23:48:36Z",
               "show": {"title": "Strange New Worlds",
                        "ids": {"simkl_id": 1347534, "tmdb": "103516"}},
               "seasons": [{"number": 4, "episodes": [
                   {"number": n, "watched_at": "2026-09-2%dT00:00:00Z" % min(n, 9)}
                   for n in range(1, 11)]}]}
    LISTED_ONLY = {"status": "plantowatch", "watched_episodes_count": 0,
                   "show": {"title": "Later", "ids": {"simkl_id": 9, "tmdb": "901"}}}

    async def _read(self, *items):
        async def _whole(*_args, **_kwargs):
            return {"shows": list(items)}

        with patch("app.providers.simkl.transport.cached_get", new=AsyncMock(side_effect=_whole)):
            return await sync.fetch_library(SETTINGS)

    async def test_a_watched_title_on_the_watchlist_is_read_as_watched(self):
        read = await self._read(self.WATCHED)
        entry = read.entries["show:tmdb:103516"]
        self.assertEqual(sorted(entry.seasons[4]), list(range(1, 11)))
        self.assertEqual(len([e for e in read.events if e["episode"]["season"] == 4]), 10)

    async def test_a_title_only_on_the_watchlist_says_nothing(self):
        """Simkl stays silent about it, as before — not "seen none of it", which
        would draw a disagreement for every title only watchlisted there."""
        read = await self._read(self.LISTED_ONLY)
        self.assertEqual(read.entries, {})

    async def test_its_stamp_is_one_the_reads_follow(self):
        """So a title moving into the list moves a stamp a bounded read notices."""
        payload = {"tv_shows": {"all": "T", "watching": "T1", "plantowatch": "T2"}}
        with patch("app.providers.simkl.transport.cached_get", new=_cached_get(payload)):
            beacon = await sync.fetch_last_activities(SETTINGS)
        self.assertEqual(beacon["lists"]["shows"]["plantowatch"], "T2")


class AnimeSeasonTitleTests(unittest.IsolatedAsyncioTestCase):
    """Simkl's anime season-titles, and the parameter that resolves them.

    THE PROBLEM, MEASURED 2026-08-18. Simkl models each anime season as its own
    catalogue title: "Beastars" season 2 is simkl 1231401, a different title from
    season 1's 1034467, and it numbers its own episodes from 1. Under
    `extended=full` its library payload arrives as `{simkl, slug, mal}` with no
    tmdb at all — so untranslated it becomes its own `show:mal:` row, numbering
    season 2's episodes as season 1, disconnected from the series the viewer
    tracks under `show:tmdb:90937`.

    THE ANSWER IS `extended=full_anime_seasons`, MEASURED 2026-08-21. Asked that
    way the same entry carries the PARENT SERIES' ids (tmdb and tvdb included)
    and every episode carries a `tvdb: {season, episode}` block giving its real
    coordinates. This package used to derive both with a per-title lookup; the
    payload states them outright. These tests pin the reading of that payload,
    and the parameters that ask for it.
    """

    # Beastars season 2 as `full_anime_seasons` really returns it: the series'
    # ids on a season-title, its own numbering as season 1, and the true
    # coordinates on each episode.
    SEASON_TWO = {"show": {"title": "Beastars",
                           "ids": {"simkl_id": 1231401, "mal": "40935",
                                   "tmdb": "90937", "tvdb": "361013"}},
                  "mapped_tvdb_seasons": [2],
                  "seasons": [{"number": 1, "episodes": [
                      {"number": 1, "watched_at": "2026-08-18T12:00:00Z",
                       "tvdb": {"season": 2, "episode": 1}}]}]}
    SEASON_ONE = {"show": {"title": "Beastars",
                           "ids": {"simkl_id": 1034467, "mal": "39195",
                                   "tmdb": "90937", "tvdb": "361013"}},
                  "mapped_tvdb_seasons": [1],
                  "seasons": [{"number": 1, "episodes": [
                      {"number": 1, "watched_at": "2022-04-25T03:58:43Z",
                       "tvdb": {"season": 1, "episode": 1}}]}]}

    async def _read(self, anime_items=(), finished=(), shows_items=()):
        """A library read whose only non-empty buckets are `shows/watching`,
        `anime/watching` and `anime/completed`. Returns the read, the paths asked
        for and the parameters each was asked with."""
        per_type = len(sync.WATCHED_STATUSES)
        buckets = iter([{"shows": list(shows_items)}] + [{}] * (per_type - 1)
                       + [{"anime": list(anime_items)}, {"anime": list(finished)}]
                       + [{}] * (per_type - 2) + [{}] * per_type)
        paths, params = [], []

        async def _get(client, settings, path, param=None, **kwargs):
            paths.append(path)
            params.append(dict(param or {}))
            if path != "sync/all-items/":
                return next(buckets)
            whole = {}
            for (media, status), part in zip(BUCKETS, buckets):
                for item in (part or {}).get(media) or []:
                    whole.setdefault(media, []).append({"status": status, **item})
            return whole

        with patch("app.providers.simkl.transport.cached_get",
                   new=AsyncMock(side_effect=_get)):
            return await sync.fetch_library(SETTINGS), paths, params

    async def test_a_season_title_files_under_the_series_and_the_real_season(self):
        """Both halves at once: the entry keys by the series' tmdb because the
        payload carries it, and the episode lands on season 2 because its own
        `tvdb` block says so."""
        read, _paths, _params = await self._read([self.SEASON_ONE, self.SEASON_TWO])
        self.assertEqual(list(read.entries), ["show:tmdb:90937"])
        self.assertEqual(read.entries["show:tmdb:90937"].seasons,
                         {1: {1: "2022-04-25T03:58:43Z"}, 2: {1: "2026-08-18T12:00:00Z"}})

    async def test_the_plays_are_translated_too_not_only_the_baseline(self):
        """Both readers take the coordinates from the same place, so a viewer's
        plays can never sit on a different season from their progress."""
        read, _paths, _params = await self._read([self.SEASON_TWO])
        self.assertEqual([(e["episode"]["season"], e["episode"]["number"])
                          for e in read.events], [(2, 1)])

    async def test_an_episode_renumbered_by_simkl_takes_both_coordinates(self):
        """A series numbered absolutely — One Piece is one title with a thousand
        episodes — maps each episode to a season AND a number of its own. Taking
        the season from the mapping and the number from the item would produce a
        coordinate that exists in neither numbering."""
        item = {"show": {"title": "One Piece",
                         "ids": {"simkl_id": 38636, "tmdb": "37854"}},
                "seasons": [{"number": 1, "episodes": [
                    {"number": 878, "watched_at": "2026-01-02T00:00:00Z",
                     "tvdb": {"season": 20, "episode": 1}}]}]}
        read, _paths, _params = await self._read([item])
        self.assertEqual(read.entries["show:tmdb:37854"].seasons,
                         {20: {1: "2026-01-02T00:00:00Z"}})

    async def test_an_item_with_no_mapping_keeps_its_own_numbering(self):
        """Television carries no `tvdb` block and needs none — its seasons are
        already the show's. The fallback is the item's own numbers, which is what
        every non-anime item has always used."""
        plain = {"show": {"title": "Show", "ids": {"simkl_id": 55, "tmdb": "900"}},
                 "seasons": [{"number": 3, "episodes": [
                     {"number": 4, "watched_at": "2026-07-01"}]}]}
        read, _paths, _params = await self._read(shows_items=[plain])
        self.assertEqual(read.entries["show:tmdb:900"].seasons, {3: {4: "2026-07-01"}})

    async def test_the_mapping_is_asked_for_wherever_anime_can_be(self):
        """`full_anime_seasons` is a superset of `full`. A whole read answers the
        anime lists too, so it asks for it; a per-list read asks it of the anime
        lists alone, since television gains nothing from it."""
        _read, paths, params = await self._read()
        self.assertEqual(dict(zip(paths, params))["sync/all-items/"]["extended"],
                         sync.ANIME_EXTENDED)
        asked = {}

        async def _get(client, settings, path, param=None, **kwargs):
            asked[path] = dict(param or {})
            return {}

        stamps = {media: {status: "S" for status in sync.WATCHED_STATUSES}
                  for media in sync.LIBRARY_TYPES}
        moved = {media: dict(block, watching="T") for media, block in stamps.items()}
        with patch("app.providers.simkl.transport.cached_get",
                   new=AsyncMock(side_effect=_get)):
            await sync.fetch_library(SETTINGS, activities={"lists": moved},
                                     since={"lists": stamps})
        self.assertEqual(asked["sync/all-items/anime/watching"]["extended"], sync.ANIME_EXTENDED)
        self.assertEqual(asked["sync/all-items/shows/watching"]["extended"], "full")

    async def test_every_bucket_asks_for_the_episodes_of_a_finished_title(self):
        """`include_all_episodes=yes` is what makes the `completed` and `dropped`
        buckets carry episodes at all — without it they state counts, and this
        app spent a phase reconstructing the episodes from them by hand."""
        _read, _paths, params = await self._read()
        for param in params:
            self.assertEqual(param.get("include_all_episodes"), "yes")
            self.assertEqual(param.get("episode_watched_at"), "yes")

    async def test_a_finished_season_title_needs_no_special_handling_now(self):
        """THE PHASE THIS REPLACES. A completed season-title used to arrive with
        no seasons block at all, and had to be rebuilt from
        `watched_episodes_count` against a per-title season lookup. Asked
        correctly it arrives itemized and translated like any other item."""
        finished = {"show": {"title": "Beastars",
                             "ids": {"simkl_id": 1231401, "mal": "40935",
                                     "tmdb": "90937"}},
                    "status": "completed", "mapped_tvdb_seasons": [2],
                    "watched_episodes_count": 12, "total_episodes_count": 12,
                    "not_aired_episodes_count": 0,
                    "seasons": [{"number": 1, "episodes": [
                        {"number": n, "watched_at": "2026-08-21T15:49:06Z",
                         "tvdb": {"season": 2, "episode": n}}
                        for n in range(1, 13)]}]}
        read, paths, _params = await self._read(finished=[finished])
        entry = read.entries["show:tmdb:90937"]
        self.assertEqual(sorted(entry.seasons), [2])
        self.assertEqual(sorted(entry.seasons[2]), list(range(1, 13)))
        # And it costs no per-title lookup at all: the payload said everything.
        self.assertEqual([p for p in paths if p.startswith("tv/")], [])

    async def test_the_do_not_remember_placeholder_is_not_a_date(self):
        """Simkl writes 1970-01-01T00:00:01Z for "watched it, no idea when".
        Read literally it files a season as finished in January 1970 — a real
        month, on a real page, sorted before everything. The episode still
        counts; the date does not."""
        item = {"show": {"title": "Show", "ids": {"simkl_id": 55, "tmdb": "900"}},
                "seasons": [{"number": 1, "episodes": [
                    {"number": 1, "watched_at": "1970-01-01T00:00:01Z"},
                    {"number": 2, "watched_at": "2026-07-02"}]}]}
        read, _paths, _params = await self._read(shows_items=[item])
        self.assertEqual(read.entries["show:tmdb:900"].seasons[1],
                         {1: "", 2: "2026-07-02"})
        # And it never becomes a play, which is what would carry it into a month.
        self.assertEqual([e["episode"]["number"] for e in read.events], [2])


class ProgressTests(unittest.IsolatedAsyncioTestCase):
    """The per-title progress read never speaks for anything and never asks
    Simkl a question — see sync.fetch_progress_details for the measurement this
    holds: `POST /sync/watched` never itemized a season for any title, so every
    id was always coming back absent, and this now says so with no request."""

    async def test_every_id_comes_back_absent_whatever_was_asked(self):
        for show_ids in ([], [None, "x"], [1, 2, 3], list(range(1, 71))):
            with self.subTest(show_ids=show_ids):
                self.assertEqual(
                    await sync.fetch_progress_details(SETTINGS, show_ids), {})

    async def test_it_never_makes_a_request(self):
        send = AsyncMock()
        cached = AsyncMock()
        with patch("app.providers.simkl.transport.send", new=send), \
             patch("app.providers.simkl.transport.cached_get", new=cached):
            await sync.fetch_progress_details(SETTINGS, [1, 2, 3])
        send.assert_not_awaited()
        cached.assert_not_awaited()


class DerivationTests(unittest.TestCase):
    """The two pure readers, which a caller uses to read one sweep twice."""

    def _events(self):
        return [
            {"type": "episode", "watched_at": "2026-07-02T00:00:00Z",
             "show": {"title": "Show", "ids": {"simkl": 55, "tmdb": "900"}},
             "episode": {"season": 1, "number": 1}},
            {"type": "episode", "watched_at": "2026-07-03T00:00:00Z",
             "show": {"title": "Show", "ids": {"simkl": 55, "tmdb": "900"}},
             "episode": {"season": 1, "number": 2}},
            {"type": "episode", "watched_at": "2026-07-04T00:00:00Z",
             "show": {"title": "Show", "ids": {"simkl": 55}},
             "episode": {"season": 0, "number": 1}},   # specials
        ]

    def test_seasons_are_counted_and_specials_are_not(self):
        self.assertEqual(sync.watched_progress_from(self._events()),
                         [{"ids": {"simkl": 55, "tmdb": "900"}, "season": 1,
                           "watched": 2, "title": "Show", "network": ""}])

    def test_the_broadcaster_comes_back_empty_rather_than_invented(self):
        """Simkl's library payload does not carry it, and every reader already
        treats an empty string as "not stated"."""
        self.assertEqual(sync.watched_progress_from(self._events())[0]["network"], "")


class LibraryIdsTests(unittest.IsolatedAsyncioTestCase):
    """sync.fetch_library_ids — the cheap "what is still there" read a removal
    check diffs against.

    Its answer is used to conclude that a title the viewer HOLDS is gone, so the
    interesting cases are all about when it must refuse to answer at all.

    ONE CALL, EVERY LIST: `/sync/all-items/` unfiltered, measured to list exactly
    the ids the fifteen per-list calls did (see fetch_library_ids).
    """

    async def _ids(self, answer):
        spy = _cached_get(answer)
        with patch("app.providers.simkl.transport.cached_get", new=spy):
            return await sync.fetch_library_ids(SETTINGS), spy

    async def test_it_reads_both_the_show_and_the_movie_shapes(self):
        """Measured live: a show sends `{"show": {"ids": ...}}` and a movie
        `{"movie": {...}}`, under a key named for the catalogue."""
        ids, _ = await self._ids({
            "shows": [{"show": {"ids": {"simkl": 2519, "slug": "chuck"}}}],
            "movies": [{"movie": {"ids": {"simkl": 53084, "slug": "ab"}}}]})
        self.assertEqual(ids, {2519: "chuck", 53084: "ab"})

    async def test_a_percent_encoded_slug_is_decoded_once_here(self):
        """`carniv%C3%A0le` is really in this account's library. A consumer builds
        a URL by encoding what it is given, so handing over the encoded form makes
        `carniv%25C3%25A0le` — a dead link built from a value that looks right in
        the database."""
        ids, _ = await self._ids({"shows": [{"show": {"ids": {"simkl": 514,
                                                              "slug": "carniv%C3%A0le"}}}]})
        self.assertEqual(ids, {514: "carnivàle"})

    async def test_a_listing_that_cannot_be_read_refuses_the_whole_answer(self):
        """THE ASYMMETRY WITH fetch_library, AND THE REASON FOR IT. There, a list
        that failed costs freshness and the caller folds in what it got. Here it
        costs every title in it: they are simply ABSENT, and absence is the entire
        signal. There is no partial version of this answer that is safe to diff,
        so a failure means no check rather than a check on less."""
        ids, _ = await self._ids(transport.SimklError("500 while reading the list"))
        self.assertIsNone(ids)

    async def test_a_credential_failure_is_raised_rather_than_reported_as_empty(self):
        """It is not a statement about this list, it is a statement about every
        request this token will ever make."""
        boom = transport.SimklError("Simkl rejected the credentials (401).")
        with patch.object(transport, "is_credential_failure", return_value=True):
            with self.assertRaises(transport.SimklError):
                await self._ids(boom)

    async def test_an_empty_library_is_an_answer_rather_than_a_refusal(self):
        """Every list read, every one of them empty. That is a viewer who holds
        nothing, and it is a real state the caller is entitled to act on — told
        apart from an unreadable one by being {} rather than None."""
        ids, spy = await self._ids({})
        self.assertEqual(ids, {})
        self.assertEqual(spy.await_count, 1)

    async def test_it_asks_for_every_list_a_library_keeps(self):
        """THE BUG THIS EXISTS FOR, AND IT SHIPPED. This read once asked only the
        lists a WATCH can be found in, which leaves out `plantowatch` — and a
        title absent from this answer is reported to the caller as removed from
        the library. On a live account 39 titles sat in that list, and 29 of the
        33 rows the removal diff called missing were among them.

        One call naming no catalogue and no status asks for every list at once;
        asserted on the address, because a narrower one is how the bug came in."""
        _, spy = await self._ids({})
        self.assertEqual([call.args[2] for call in spy.await_args_list], ["sync/all-items/"])
        self.assertEqual(spy.await_args.args[3], {"extended": "simkl_ids_only"})

    async def test_a_title_only_in_plan_to_watch_is_still_in_the_library(self):
        """The whole claim in one case: the viewer holds it, has watched none of
        it, and it must not read as gone. The listing carries no status here, and
        nothing filters on one."""
        ids, _ = await self._ids({"shows": [
            {"show": {"ids": {"simkl": 4242, "slug": "the-abandons"}}}]})
        self.assertEqual(ids, {4242: "the-abandons"})

    def test_the_listing_covers_every_list_a_watch_can_be_in(self):
        """`plantowatch` is held AND can hold watches (see WATCHED_STATUSES), so
        the listing and the watch reads cover the same lists."""
        self.assertIn("plantowatch", sync.LIBRARY_STATUSES)
        self.assertEqual(set(sync.LIBRARY_STATUSES), set(sync.WATCHED_STATUSES))

    def test_an_unrecognised_bucket_is_never_asked_for(self):
        """Simkl answers a status it does not know with the WHOLE library rather
        than an error — measured: `shows/notinteresting` returned 1,045 distinct
        ids against a library of 1,045 — so a listing that included one would
        report that nothing is ever missing."""
        self.assertNotIn("notinteresting", sync.LIBRARY_STATUSES)

    def test_the_registered_port_actually_offers_it(self):
        """THE CHECK IS GATED ON THIS METHOD BEING ON THE PORT, so a function
        defined on the module and never exposed there is a removal check that
        silently never runs — correct code, wired to nothing, with every one of
        its own tests passing. That is exactly how it was written the first time.

        Asked of the REGISTRY rather than the class, because the registry is what
        the tracker actually reaches for.
        """
        from app import providers
        from app.providers.base import Source
        port = providers.get(Source.SIMKL).sync_port
        self.assertIsNotNone(getattr(port, "fetch_library_ids", None))

    async def test_it_never_sends_date_from(self):
        """A bounded read answers "what changed"; this one has to answer "what
        remains". A title filtered out for not having moved is exactly the title
        the diff would then report as removed."""
        _ids, spy = await self._ids([{}] * 12)
        for call in spy.await_args_list:
            self.assertNotIn("date_from", call.args[3])
            self.assertEqual(call.args[3]["extended"], "simkl_ids_only")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
