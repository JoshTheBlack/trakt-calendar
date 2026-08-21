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


class _Response:
    """Just enough of an httpx response for fetch_progress_details."""

    def __init__(self, payload, status_code=200, text=""):
        self._payload = payload
        self.status_code = status_code
        self.text = text

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


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


class PrivacyTests(unittest.IsolatedAsyncioTestCase):
    """Every read here is one person's. None of it may be cached, and none of it
    may travel on the pool that allows parallel requests."""

    async def test_every_get_is_private_and_on_the_sync_pool(self):
        answers = [
            {"tv_shows": {"all": "T"}},                       # the beacon
            *[{} for _ in range(12)],                         # every history bucket
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

    async def test_the_batched_progress_read_is_a_post_and_is_never_cached(self):
        """A POST whose meaning is entirely in its body cannot be expressed by a
        URL key, and this one is also one person's viewing. It goes through
        `send`, which does not cache at all — so a test that it never reaches
        cached_get is the whole assertion."""
        send = AsyncMock(return_value=_Response([]))
        cached = _cached_get()
        with patch("app.providers.simkl.transport.send", new=send), \
             patch("app.providers.simkl.transport.cached_get", new=cached):
            await sync.fetch_progress_details(SETTINGS, [1, 2])
        cached.assert_not_awaited()
        self.assertEqual(send.await_args.args[1], "POST")
        self.assertIs(send.await_args.kwargs["pool"], transport.SYNC_POOL)

    async def test_nothing_here_writes_to_simkl(self):
        """This build reads a person's viewing and never edits it. The write path
        is POST /sync/history, and no call in this module may name it."""
        send = AsyncMock(return_value=_Response([]))
        with patch("app.providers.simkl.transport.send", new=send), \
             patch("app.providers.simkl.transport.cached_get", new=_cached_get(*[{}] * 13)):
            await sync.fetch_progress_details(SETTINGS, [1])
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
                   new=_cached_get(*[transport.SimklError("gone", 500)] * 12)):
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
        spy = _cached_get(*answers)
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
        self.assertEqual(spy.await_count, 12)

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
        read, spy = await self._read([{}] * 8, activities=activities)
        self.assertEqual(spy.await_count, 8)
        self.assertTrue(read.complete)

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
        self.assertEqual(spy.await_count, 12)
        self.assertTrue(read.complete)

    async def test_a_bucket_that_was_asked_for_and_refused_makes_the_read_partial(self):
        """`complete` MEANS "EVERY BUCKET I MEANT TO READ, I READ", never "I
        finished looping". The caller retires a title absent from a complete read,
        so a read that downgraded a refusal to an absence would have it delete
        watch history it merely failed to fetch."""
        answers = [{"shows": [self._item()]},
                   transport.SimklError("that list 500ed", 500)] + [{}] * 10
        read, spy = await self._read(answers)
        self.assertEqual(spy.await_count, 12)
        self.assertFalse(read.complete)
        # And what it DID read is still there — a lost bucket costs that bucket.
        self.assertEqual(list(read.entries), ["show:tmdb:900"])

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
        buckets = iter([{"shows": list(shows_items)}] + [{}] * 3
                       + [{"anime": list(anime_items)},
                          {"anime": list(finished)}] + [{}] * 6)
        paths, params = [], []

        async def _get(client, settings, path, param=None, **kwargs):
            paths.append(path)
            params.append(dict(param or {}))
            return next(buckets)

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

    async def test_only_the_anime_buckets_ask_for_the_mapping(self):
        """`full_anime_seasons` is a superset of `full` and costs a larger
        payload; television gains nothing from it, so it is asked for where it
        answers something."""
        _read, paths, params = await self._read()
        asked = dict(zip(paths, params))
        self.assertEqual(asked["sync/all-items/anime/watching"]["extended"],
                         sync.ANIME_EXTENDED)
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
    """One request for a whole roster, and what comes back matched to what was
    asked."""

    async def test_a_whole_roster_is_one_request(self):
        """The 1 POST/second cap is what makes this essential rather than an
        optimisation: seventy titles asked one at a time would take over a
        minute."""
        answers = [{"ids": {"simkl": n}, "seasons": [
            {"number": 1, "episodes": [{"number": 1, "watched_at": "2026-07-01"}]}]}
            for n in range(1, 71)]
        send = AsyncMock(return_value=_Response(answers))
        with patch("app.providers.simkl.transport.send", new=send):
            got = await sync.fetch_progress_details(SETTINGS, list(range(1, 71)))
        self.assertEqual(send.await_count, 1)
        self.assertEqual(len(got), 70)
        self.assertEqual(got[1], {1: {1: "2026-07-01"}})

    async def test_an_answer_is_matched_by_position_when_it_names_no_id(self):
        """The response is a parallel array. An entry that names its own id is
        believed over its position, because a service that started filtering its
        answers would otherwise shift every row silently."""
        answers = [{"seasons": [{"number": 2, "episodes": [{"number": 4}]}]}]
        send = AsyncMock(return_value=_Response(answers))
        with patch("app.providers.simkl.transport.send", new=send):
            got = await sync.fetch_progress_details(SETTINGS, [91])
        self.assertEqual(got, {91: {2: {4: ""}}})

    async def test_a_season_with_no_breakdown_says_nothing_rather_than_zero(self):
        """Turning a watched COUNT into "episodes 1..n" would invent both the
        numbers and the dates — so there is nothing to report. What that means is
        "I cannot say", NOT "none of it was watched", and the title is therefore
        ABSENT: Simkl has just said four episodes of season 1 were seen, and
        answering the caller with a zero would have it retire every season it had
        stored for the title on the strength of a payload that says the opposite.
        """
        send = AsyncMock(return_value=_Response(
            [{"ids": {"simkl": 5}, "seasons": [{"number": 1, "total_episodes_watched": 4}]}]))
        with patch("app.providers.simkl.transport.send", new=send):
            self.assertEqual(await sync.fetch_progress_details(SETTINGS, [5]), {})

    async def test_an_answer_with_no_seasons_at_all_says_nothing(self):
        """THE SHAPE EVERY REAL ANSWER TAKES, measured 2026-08-21: this endpoint
        reports WHETHER a title has been watched and never HOW MUCH, whatever
        `episode_watched_at` asks for. Twin Peaks comes back like this while the
        library read itemizes eight episodes of it."""
        send = AsyncMock(return_value=_Response(
            [{"simkl": 203, "result": True, "list": "watching",
              "last_watched_at": "2020-02-02T19:07:39Z"}]))
        with patch("app.providers.simkl.transport.send", new=send):
            self.assertEqual(await sync.fetch_progress_details(SETTINGS, [203]), {})

    async def test_a_title_the_viewer_does_not_hold_is_a_real_zero(self):
        """The ONE answer from here that means "seen none of it", and it has to
        stay distinguishable from the rest: a title that has left the library is
        how stored counts are correctly retired."""
        send = AsyncMock(return_value=_Response([{"simkl": 9, "result": False}]))
        with patch("app.providers.simkl.transport.send", new=send):
            self.assertEqual(await sync.fetch_progress_details(SETTINGS, [9]), {9: {}})

    async def test_ids_simkl_could_not_resolve_say_nothing_rather_than_zero(self):
        """`not_found` is Simkl saying it does not know what was asked about,
        which is the opposite of "this viewer has watched none of it" — and this
        app had the two the wrong way round."""
        send = AsyncMock(return_value=_Response([{"simkl": 9, "result": "not_found"}]))
        with patch("app.providers.simkl.transport.send", new=send):
            self.assertEqual(await sync.fetch_progress_details(SETTINGS, [9]), {})

    async def test_a_batch_that_failed_names_none_of_its_ids(self):
        """The other half of the same rule, and the one that protects stored
        counts: a POST that came back refused says nothing about any title in it,
        so every id stays absent and the caller leaves what it had alone."""
        send = AsyncMock(return_value=_Response(None, status_code=500, text="boom"))
        with patch("app.providers.simkl.transport.send", new=send):
            self.assertEqual(await sync.fetch_progress_details(SETTINGS, [5, 6]), {})

    async def test_nothing_to_ask_about_costs_no_request(self):
        send = AsyncMock()
        with patch("app.providers.simkl.transport.send", new=send):
            self.assertEqual(await sync.fetch_progress_details(SETTINGS, []), {})
            self.assertEqual(await sync.fetch_progress_details(SETTINGS, [None, "x"]), {})
        send.assert_not_awaited()

    async def test_an_unreadable_answer_is_skipped_rather_than_fatal(self):
        send = AsyncMock(return_value=_Response(ValueError("not json"), text="<html>"))
        with patch("app.providers.simkl.transport.send", new=send):
            self.assertEqual(await sync.fetch_progress_details(SETTINGS, [5]), {})


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


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
