"""app/providers/trakt/watchlist.py — writing the tracker's shows to a person's
Trakt watchlist, and taking those writes back.

THE ANSWER BODIES HERE ARE TRAKT'S OWN, recorded against the live service on
2026-09-30 on titles the account's owner set aside for it (an add of one real
show beside one id Trakt does not know, the same add again, and the removal);
a watchlist item is the shape a live read of the owner's list returned, `plex`
block and all. No network: the transport's sender is patched on the module
object and answers reads page by page.
"""
from __future__ import annotations

import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

import httpx

from app import cache
from app.config import Settings
from app.providers.base import (NOT_HELD, ItemKey, WatchlistOutcome, WatchlistTitle,
                                WatchlistUndo)
from app.providers.trakt import TraktError, transport, watchlist

SETTINGS = Settings(trakt_client_id="an-id", trakt_access_token="a-token")

_EMPTY = {"movies": 0, "shows": 0, "seasons": 0, "episodes": 0}
_NOBODY = {"movies": [], "shows": [], "seasons": [], "episodes": [], "people": [], "users": []}


def added(*, added=0, existing=0, not_found=(), count=48):
    """POST /sync/watchlist's answer, as recorded: counts, and the unmatched
    items echoed exactly as sent."""
    return {"added": {**_EMPTY, "shows": added},
            "existing": {**_EMPTY, "shows": existing},
            "not_found": {**_NOBODY, "shows": [{"ids": dict(ids)} for ids in not_found]},
            "list": {"updated_at": "2026-09-30T01:44:42.000Z", "item_count": count}}


def removed(*, deleted=0, not_found=(), count=47):
    """POST /sync/watchlist/remove's answer, as recorded."""
    return {"deleted": {**_EMPTY, "shows": deleted},
            "not_found": {**_NOBODY, "shows": [{"ids": dict(ids)} for ids in not_found]},
            "list": {"updated_at": "2026-09-30T01:44:42.000Z", "item_count": count}}


# The recorded exchanges: Trakt 4214 added beside a TMDB id Trakt has no show for.
T1 = added(added=1, not_found=[{"tmdb": 999999999}])
T2 = added(existing=1)
T3 = removed(deleted=1, not_found=[{"tmdb": 999999999}])


def listed(trakt, *, tmdb=None, tvdb=None, imdb=None):
    ids = {"trakt": trakt, "slug": f"show-{trakt}",
           "plex": {"guid": "6151a5f218874911f7dfb84f", "slug": f"show-{trakt}"}}
    for name, value in (("tmdb", tmdb), ("tvdb", tvdb), ("imdb", imdb)):
        if value is not None:
            ids[name] = value
    return {"rank": 1, "id": 1000 + trakt, "listed_at": "2026-09-27T18:13:30.000Z",
            "notes": None, "type": "show",
            "show": {"title": f"Show {trakt}", "year": 2024, "ids": ids}}


def title(tmdb, trakt=None, **more):
    ids = {"tmdb": tmdb, **more}
    if trakt is not None:
        ids["trakt"] = trakt
    return WatchlistTitle(ItemKey("show", "tmdb", str(tmdb)), ids)


class _Trakt:
    """The transport's sender. Each READ is one page (a list of items, or a
    Response to answer with) or a TUPLE of pages; each POST answer is
    (status, body[, headers]) or an exception."""

    def __init__(self, reads, answers=()):
        self.reads = [list(read) if isinstance(read, tuple) else [read] for read in reads]
        self.answers = list(answers)
        self.gets = []
        self.posts = []

    async def send_or_unavailable(self, client, method, url, *, headers=None, json=None,
                                  timeout=None):
        parsed = urlparse(url)
        path = parsed.path.lstrip("/")
        if method == "GET":
            query = {k: v[0] for k, v in parse_qs(parsed.query).items()}
            self.gets.append({"path": path, "query": query, "headers": headers})
            page = int(query.get("page", 1))
            pages = self.reads[0]
            answer = pages[page - 1]
            if page == len(pages):
                self.reads.pop(0)
            if isinstance(answer, httpx.Response):
                return answer
            return httpx.Response(200, json=answer, headers={
                "x-pagination-page": str(page), "x-pagination-page-count": str(len(pages)),
                "x-pagination-limit": "100",
                "x-pagination-item-count": str(sum(len(p) for p in pages
                                                   if isinstance(p, list)))})
        self.posts.append({"path": path, "body": json, "headers": headers})
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        status, body, *extra = answer
        return httpx.Response(status, json=body, headers=extra[0] if extra else {})

    def __enter__(self):
        self._patch = patch.object(transport, "send_or_unavailable", new=self.send_or_unavailable)
        self._patch.start()
        return self

    def __exit__(self, *exc):
        self._patch.stop()


class ReadTests(unittest.IsolatedAsyncioTestCase):
    async def test_every_page_is_read(self):
        """Measured: the watchlist read pages at 100, and a 146-item read that
        stopped at the first page missed 46 shows."""
        first = [listed(n, tmdb=50000 + n) for n in range(1, 101)]
        second = [listed(4214, tmdb=4238)]
        fake = _Trakt(reads=[(first, second)])
        with fake:
            held = await watchlist.holdings(SETTINGS, [title(4238), title(50001), title(1)])
        self.assertEqual(held["show:tmdb:4238"].status, watchlist.LISTED)
        self.assertEqual(held["show:tmdb:4238"].service_id, "4214")
        self.assertEqual(held["show:tmdb:50001"].status, watchlist.LISTED)
        self.assertEqual(held["show:tmdb:1"].status, NOT_HELD)
        self.assertEqual([(g["path"], g["query"]) for g in fake.gets],
                         [("sync/watchlist/shows", {"page": "1", "limit": "100"}),
                          ("sync/watchlist/shows", {"page": "2", "limit": "100"})])

    async def test_a_page_that_cannot_be_read_fails_the_whole_read(self):
        """A read missing a page would make its shows look unlisted, and a drop
        or an undo would then leave them where they are."""
        first = [listed(n) for n in range(1, 101)]
        fake = _Trakt(reads=[(first, httpx.Response(502, text="bad gateway"))])
        with fake, self.assertRaises(TraktError):
            await watchlist.holdings(SETTINGS, [title(4238)])

    async def test_a_refused_token_says_so(self):
        fake = _Trakt(reads=[httpx.Response(401, text="Unauthorized")])
        with fake, self.assertRaises(TraktError) as caught:
            await watchlist.holdings(SETTINGS, [title(4238)])
        self.assertTrue(transport.is_credential_failure(caught.exception))

    async def test_a_page_count_past_any_watchlist_is_not_followed(self):
        page = httpx.Response(200, json=[], headers={"x-pagination-page-count": "5000"})
        fake = _Trakt(reads=[page])
        with fake, self.assertRaises(TraktError):
            await watchlist.holdings(SETTINGS, [title(4238)])
        self.assertEqual(len(fake.gets), 1)

    async def test_nothing_read_or_written_touches_the_shared_cache(self):
        """The watchlist is one person's; the response cache is keyed by URL and
        shared by the instance."""
        async def refuse(*_a, **_k):
            raise AssertionError("the shared cache was touched")

        fake = _Trakt(reads=[[], [listed(4214, tmdb=4238)]],
                      answers=[(201, added(added=1))])
        with fake, patch.object(cache, "get", new=refuse), patch.object(cache, "set", new=refuse):
            await watchlist.put(SETTINGS, [title(4238, trakt=4214)])
        self.assertEqual(len(fake.gets), 2)

    async def test_reads_carry_the_viewers_bearer_and_no_paging_headers(self):
        fake = _Trakt(reads=[[]])
        with fake:
            await watchlist.holdings(SETTINGS, [title(4238)])
        headers = fake.gets[0]["headers"]
        self.assertEqual(headers["Authorization"], "Bearer a-token")
        self.assertNotIn("X-Pagination-Page", headers)


class PutTests(unittest.IsolatedAsyncioTestCase):
    async def test_the_recorded_add_beside_an_unknown_id(self):
        fake = _Trakt(reads=[[], [listed(4214, tmdb=4238)]], answers=[(201, T1)])
        with fake:
            result = await watchlist.put(SETTINGS, [title(4238, trakt=4214),
                                                    title(999999999)])
        post, = fake.posts
        self.assertEqual(post["path"], "sync/watchlist")
        self.assertEqual(post["body"], {"shows": [{"ids": {"trakt": 4214}},
                                                  {"ids": {"tmdb": 999999999}}]})
        self.assertEqual(post["headers"]["Authorization"], "Bearer a-token")
        written = result["show:tmdb:4238"]
        self.assertEqual(written.outcome, WatchlistOutcome.WRITTEN)
        self.assertEqual((written.status_before, written.status_after),
                         (NOT_HELD, watchlist.LISTED))
        self.assertEqual(written.service_id, "4214")
        self.assertEqual(result["show:tmdb:999999999"].outcome, WatchlistOutcome.NOT_FOUND)

    async def test_a_title_already_listed_is_not_sent(self):
        fake = _Trakt(reads=[[listed(4214, tmdb=4238)]])
        with fake:
            result = await watchlist.put(SETTINGS, [title(4238)])
        self.assertEqual(fake.posts, [])
        self.assertEqual(len(fake.gets), 1)
        self.assertEqual(result["show:tmdb:4238"].outcome, WatchlistOutcome.ALREADY)
        self.assertEqual(result["show:tmdb:4238"].status_before, watchlist.LISTED)
        self.assertEqual(result["show:tmdb:4238"].service_id, "4214")

    async def test_the_recorded_add_again_is_already_and_listed_before(self):
        """The viewer listed it between the read and the write. The body counts
        it as existing, so it was theirs, and an undo must never remove it."""
        fake = _Trakt(reads=[[], [listed(4214, tmdb=4238)]], answers=[(201, T2)])
        with fake:
            result = await watchlist.put(SETTINGS, [title(4238, trakt=4214)])
        answer = result["show:tmdb:4238"]
        self.assertEqual(answer.outcome, WatchlistOutcome.ALREADY)
        self.assertEqual(answer.status_before, watchlist.LISTED)

    async def test_an_add_that_counts_some_as_existing_marks_all_of_them_listed_before(self):
        fake = _Trakt(reads=[[], [listed(1, tmdb=11), listed(2, tmdb=22)]],
                      answers=[(201, added(added=1, existing=1))])
        with fake:
            result = await watchlist.put(SETTINGS, [title(11, trakt=1), title(22, trakt=2)])
        for key in ("show:tmdb:11", "show:tmdb:22"):
            self.assertEqual(result[key].outcome, WatchlistOutcome.WRITTEN)
            self.assertEqual(result[key].status_before, watchlist.LISTED)

    async def test_an_add_the_read_back_does_not_show_failed(self):
        fake = _Trakt(reads=[[], []], answers=[(201, added(added=1))])
        with fake:
            result = await watchlist.put(SETTINGS, [title(4238, trakt=4214)])
        self.assertEqual(result["show:tmdb:4238"].outcome, WatchlistOutcome.FAILED)

    async def test_a_full_list_refuses_every_title_in_the_write(self):
        """420: the account's list is at its limit. Answered, not raised, and not
        read back — nothing was written to confirm."""
        fake = _Trakt(reads=[[]], answers=[(420, {}, {"X-Account-Limit": "100",
                                                      "X-VIP-User": "false"})])
        with fake:
            result = await watchlist.put(SETTINGS, [title(11, trakt=1), title(22, trakt=2)])
        self.assertEqual({r.outcome for r in result.values()}, {WatchlistOutcome.REFUSED})
        self.assertEqual(len(fake.gets), 1)
        self.assertEqual(len(fake.posts), 1)

    async def test_a_first_write_refused_outright_raises(self):
        """Nothing is known to have happened: that is "could not ask"."""
        fake = _Trakt(reads=[[]], answers=[(401, {"error": "invalid_token"})])
        with fake, self.assertRaises(TraktError):
            await watchlist.put(SETTINGS, [title(4238, trakt=4214)])

    async def test_writes_go_in_chunks_and_a_late_failure_fails_only_its_own(self):
        titles = [title(10000 + n, trakt=n) for n in range(1, 151)]
        after = ([listed(n, tmdb=10000 + n) for n in range(1, 101)],
                 [listed(n, tmdb=10000 + n) for n in range(101, 151)])
        fake = _Trakt(reads=[[], after],
                      answers=[(201, added(added=100)), TraktError("gone")])
        with fake:
            result = await watchlist.put(SETTINGS, titles)
        self.assertEqual([len(p["body"]["shows"]) for p in fake.posts], [100, 50])
        outcomes = [result[str(t.key)].outcome for t in titles]
        self.assertEqual(outcomes[:100], [WatchlistOutcome.WRITTEN] * 100)
        self.assertEqual(outcomes[100:], [WatchlistOutcome.FAILED] * 50)

    async def test_without_trakts_own_id_every_shared_id_it_takes_is_sent(self):
        fake = _Trakt(reads=[[], [listed(4214, tmdb=4238, tvdb=73641)]],
                      answers=[(201, added(added=1))])
        with fake:
            result = await watchlist.put(SETTINGS, [
                title(4238, tvdb="73641", imdb="tt0115167", simkl=3152, mal=None)])
        self.assertEqual(fake.posts[0]["body"],
                         {"shows": [{"ids": {"tmdb": 4238, "tvdb": 73641, "imdb": "tt0115167"}}]})
        self.assertEqual(result["show:tmdb:4238"].service_id, "4214")

    async def test_a_shared_id_naming_two_listed_shows_is_not_guessed(self):
        fake = _Trakt(reads=[[listed(1, tmdb=4238), listed(2, tmdb=4238)]])
        with fake:
            result = await watchlist.put(SETTINGS, [title(4238)])
        self.assertEqual(result["show:tmdb:4238"].outcome, WatchlistOutcome.NOT_FOUND)
        self.assertEqual(fake.posts, [])


class DropTests(unittest.IsolatedAsyncioTestCase):
    async def test_the_recorded_removal(self):
        fake = _Trakt(reads=[[listed(4214, tmdb=4238)], []], answers=[(200, T3)])
        with fake:
            result = await watchlist.drop(SETTINGS, [title(4238, trakt=4214)])
        post, = fake.posts
        self.assertEqual(post["path"], "sync/watchlist/remove")
        self.assertEqual(post["body"], {"shows": [{"ids": {"trakt": 4214}}]})
        answer = result["show:tmdb:4238"]
        self.assertEqual(answer.outcome, WatchlistOutcome.WRITTEN)
        self.assertEqual((answer.status_before, answer.status_after),
                         (watchlist.LISTED, NOT_HELD))

    async def test_a_title_not_listed_is_left_alone(self):
        """Dropping never adds a show in order to drop it."""
        fake = _Trakt(reads=[[]])
        with fake:
            result = await watchlist.drop(SETTINGS, [title(4238, trakt=4214)])
        self.assertEqual(fake.posts, [])
        self.assertEqual(result["show:tmdb:4238"].outcome, WatchlistOutcome.ALREADY)

    async def test_a_removal_the_read_back_contradicts_failed(self):
        fake = _Trakt(reads=[[listed(4214, tmdb=4238)], [listed(4214, tmdb=4238)]],
                      answers=[(200, removed(deleted=1))])
        with fake:
            result = await watchlist.drop(SETTINGS, [title(4238, trakt=4214)])
        self.assertEqual(result["show:tmdb:4238"].outcome, WatchlistOutcome.FAILED)


class UndoTests(unittest.IsolatedAsyncioTestCase):
    def departed(self, tmdb, trakt):
        """A show that left the tracker, as the write step names it: the shared
        id it was filed under and the service id a write recorded."""
        return WatchlistTitle(ItemKey("show", "tmdb", str(tmdb)),
                              {"tmdb": str(tmdb), "trakt": str(trakt)})

    async def test_the_trackers_own_add_is_removed(self):
        fake = _Trakt(reads=[[listed(4214, tmdb=4238)], []],
                      answers=[(200, removed(deleted=1))])
        with fake:
            result = await watchlist.undo(SETTINGS, [WatchlistUndo(self.departed(4238, 4214),
                                                                   NOT_HELD)])
        self.assertEqual(fake.posts[0]["body"], {"shows": [{"ids": {"trakt": 4214}}]})
        self.assertEqual(result["show:tmdb:4238"].outcome, WatchlistOutcome.WRITTEN)
        self.assertEqual(result["show:tmdb:4238"].status_after, NOT_HELD)

    async def test_a_show_the_viewer_had_listed_stays(self):
        fake = _Trakt(reads=[[listed(4214, tmdb=4238)]])
        with fake:
            result = await watchlist.undo(SETTINGS, [WatchlistUndo(self.departed(4238, 4214),
                                                                   watchlist.LISTED)])
        self.assertEqual(fake.posts, [])
        self.assertEqual(result["show:tmdb:4238"].outcome, WatchlistOutcome.ALREADY)

    async def test_a_show_already_off_the_list_is_kept_off(self):
        """The viewer took it off, or Trakt did at a play. Nothing of the
        tracker's is left to take back, and nothing is put back."""
        fake = _Trakt(reads=[[]])
        with fake:
            result = await watchlist.undo(SETTINGS, [WatchlistUndo(self.departed(4238, 4214),
                                                                   NOT_HELD)])
        self.assertEqual(fake.posts, [])
        self.assertEqual(result["show:tmdb:4238"].outcome, WatchlistOutcome.KEPT)

    async def test_every_row_is_answered(self):
        fake = _Trakt(reads=[[listed(1, tmdb=11), listed(2, tmdb=22)], [listed(2, tmdb=22)]],
                      answers=[(200, removed(deleted=1))])
        with fake:
            result = await watchlist.undo(SETTINGS, [
                WatchlistUndo(self.departed(11, 1), NOT_HELD),
                WatchlistUndo(self.departed(22, 2), watchlist.LISTED),
                WatchlistUndo(self.departed(33, 3), NOT_HELD)])
        self.assertEqual({key: r.outcome for key, r in result.items()},
                         {"show:tmdb:11": WatchlistOutcome.WRITTEN,
                          "show:tmdb:22": WatchlistOutcome.ALREADY,
                          "show:tmdb:33": WatchlistOutcome.KEPT})


class LinkTests(unittest.TestCase):
    def test_the_list_is_whoever_is_signed_in(self):
        self.assertEqual(watchlist.list_url("30ee86b0-uuid"),
                         "https://app.trakt.tv/users/me/watchlist")


if __name__ == "__main__":
    unittest.main()
