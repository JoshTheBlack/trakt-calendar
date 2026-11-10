"""app/providers/tmdb/watchlist.py and account.py — a linked viewer's own TMDB
watchlist, written under CLAUDE.md's watchlist rule, and their own lists read.

THE ANSWERS HERE ARE TMDB'S OWN, recorded against the live service on 2026-11-08
on the owner's account (each write undone afterwards): an add answers 201 with
TMDB status_code 1, the same add again 12, a removal 13 — also for a title that
was not listed — and an id TMDB has no title for 404 with status_code 34; a list
read pages at 20 with `total_pages`. No network: the transport's private sender
is patched on the module object and answers reads page by page from a list it
holds, which each write changes as TMDB's did.
"""
from __future__ import annotations

import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import httpx

from app.config import Settings
from app.providers.base import (NOT_HELD, ItemKey, Media, WatchlistDrop, WatchlistOutcome,
                                WatchlistTitle, WatchlistUndo)
from app.providers.tmdb import account, transport, watchlist

ACCOUNT = "6a5edaa7df66e2fc7c922663"
SETTINGS = Settings(tmdb_api_key="eyJa.eyJb.c", tmdb_user_token="the-viewers-token",
                    tmdb_account_id=ACCOUNT)
PAGE = 20


def title(tmdb, **more):
    ids = {"tmdb": tmdb, **more} if tmdb is not None else dict(more)
    key = ItemKey("show", "tmdb", str(tmdb)) if tmdb is not None else ItemKey("show", "tvdb", "9")
    return WatchlistTitle(key, ids)


class FakeTmdb:
    """TMDB's account API as measured: `listed` is the TV watchlist (TMDB ids, in
    added order), each write changes it and answers TMDB's body, and a read
    pages it at 20. `unknown` ids answer 34; `ignores` are ids a write answers
    as done and does not do; `fail` maps a call number to an HTTP status."""

    def __init__(self, listed=(), *, unknown=(), ignores=()):
        self.listed = list(listed)
        self.unknown = set(unknown)
        self.ignores = set(ignores)
        self.fail: dict[int, int] = {}
        self.calls: list[dict] = []

    async def send_private(self, method, url, path, *, token, body=None):
        parts = urlsplit(url)
        call = {"method": method, "path": parts.path, "token": token, "body": body,
                "query": {k: v[0] for k, v in parse_qs(parts.query).items()}}
        self.calls.append(call)
        status = self.fail.get(len(self.calls))
        if status:
            return httpx.Response(status, json={"success": False, "status_code": 3})
        if method == "GET":
            page = int(call["query"]["page"])
            pages = -(-len(self.listed) // PAGE)
            items = self.listed[(page - 1) * PAGE:page * PAGE]
            return httpx.Response(200, json={
                "page": page, "results": [{"id": tmdb, "name": f"Show {tmdb}",
                                           "media_type": "tv"} for tmdb in items],
                "total_pages": pages, "total_results": len(self.listed)})
        media = body["media_id"]
        if media in self.unknown:
            return httpx.Response(404, json={"success": False, "status_code": 34})
        if body["watchlist"]:
            code = 12 if media in self.listed else 1
            if media not in self.listed and media not in self.ignores:
                self.listed.append(media)
            return httpx.Response(201, json={"success": True, "status_code": code})
        if media in self.listed and media not in self.ignores:
            self.listed.remove(media)
        return httpx.Response(200, json={"success": True, "status_code": 13})

    def writes(self):
        return [(c["body"]["media_id"], c["body"]["watchlist"]) for c in self.calls
                if c["method"] == "POST"]

    def reads(self):
        return [c for c in self.calls if c["method"] == "GET"]

    def __enter__(self):
        self._patch = patch.object(transport, "send_private", self.send_private)
        self._patch.start()
        return self

    def __exit__(self, *exc):
        self._patch.stop()


class ReadTests(unittest.IsolatedAsyncioTestCase):

    async def test_every_page_is_read(self):
        """A read missing a page would make the shows on it look unlisted."""
        with FakeTmdb(listed=range(1, 46)) as tmdb:
            held = await watchlist.holdings(SETTINGS, [title(45), title(46)])
        self.assertEqual([c["query"]["page"] for c in tmdb.reads()], ["1", "2", "3"])
        self.assertEqual(held["show:tmdb:45"].status, watchlist.LISTED)
        self.assertEqual(held["show:tmdb:46"].status, NOT_HELD)

    async def test_an_empty_list_is_one_read(self):
        with FakeTmdb() as tmdb:
            held = await watchlist.holdings(SETTINGS, [title(1)])
        self.assertEqual(len(tmdb.reads()), 1)
        self.assertEqual(held["show:tmdb:1"].status, NOT_HELD)

    async def test_reads_carry_the_viewers_own_token_and_address_their_account(self):
        with FakeTmdb() as tmdb:
            await watchlist.holdings(SETTINGS, [title(1)])
        call = tmdb.reads()[0]
        self.assertEqual(call["token"], "the-viewers-token")
        self.assertEqual(call["path"], f"/4/account/{ACCOUNT}/tv/watchlist")

    async def test_nothing_is_read_from_or_written_to_the_response_cache(self):
        """Two viewers ask for the same address; a cached answer would be served
        to the wrong one."""
        from app import cache
        with patch.object(cache, "get", side_effect=AssertionError("cache read")), \
             patch.object(cache, "set", side_effect=AssertionError("cache write")), \
             FakeTmdb(listed=[1]):
            await watchlist.put(SETTINGS, [title(2)])

    async def test_a_refused_read_raises_with_the_reason(self):
        with FakeTmdb() as tmdb:
            tmdb.fail = {1: 401}
            with self.assertRaises(transport.TmdbError) as caught:
                await watchlist.holdings(SETTINGS, [title(1)])
        self.assertEqual(caught.exception.status, 401)
        self.assertIn("Connect TMDB again", str(caught.exception))

    async def test_a_list_claiming_more_pages_than_any_is_refused(self):
        async def endless(method, url, path, *, token, body=None):
            return httpx.Response(200, json={"page": 1, "results": [{"id": 1}],
                                             "total_pages": account.MAX_PAGES + 1})

        with patch.object(transport, "send_private", endless):
            with self.assertRaises(transport.TmdbError):
                await watchlist.holdings(SETTINGS, [title(1)])

    async def test_no_linked_account_is_no_read(self):
        with FakeTmdb() as tmdb, self.assertRaises(transport.TmdbError):
            await watchlist.holdings(Settings(), [title(1)])
        self.assertEqual(tmdb.calls, [])


class PutTests(unittest.IsolatedAsyncioTestCase):

    async def test_a_put_is_written_and_confirmed_by_reading_the_list_back(self):
        with FakeTmdb(listed=[7]) as tmdb:
            result = await watchlist.put(SETTINGS, [title(4)])
        self.assertEqual(tmdb.writes(), [(4, True)])
        self.assertEqual(len(tmdb.reads()), 2)                   # before, after
        got = result["show:tmdb:4"]
        self.assertEqual((got.outcome, got.status_before, got.status_after, got.service_id),
                         (WatchlistOutcome.WRITTEN, NOT_HELD, watchlist.LISTED, "4"))
        write = [c for c in tmdb.calls if c["method"] == "POST"][0]
        self.assertEqual(write["path"], f"/3/account/{ACCOUNT}/watchlist")
        self.assertEqual(write["body"], {"media_type": "tv", "media_id": 4, "watchlist": True})
        self.assertEqual(write["token"], "the-viewers-token")

    async def test_a_show_already_listed_is_not_sent(self):
        """The viewer's own: ALREADY, held before as listed — which an undo and
        a drop later read as theirs."""
        with FakeTmdb(listed=[4]) as tmdb:
            result = await watchlist.put(SETTINGS, [title(4)])
        self.assertEqual(tmdb.writes(), [])
        self.assertEqual((result["show:tmdb:4"].outcome, result["show:tmdb:4"].status_before),
                         (WatchlistOutcome.ALREADY, watchlist.LISTED))

    async def test_an_answer_saying_already_listed_counts_as_the_viewers(self):
        """TMDB's 12: listed between the read and the write, by the viewer."""
        with FakeTmdb() as tmdb:
            original = tmdb.send_private

            async def listed_meanwhile(method, url, path, *, token, body=None):
                if method == "POST":
                    tmdb.listed.append(body["media_id"])
                return await original(method, url, path, token=token, body=body)

            with patch.object(transport, "send_private", listed_meanwhile):
                result = await watchlist.put(SETTINGS, [title(4)])
        self.assertEqual((result["show:tmdb:4"].outcome, result["show:tmdb:4"].status_before),
                         (WatchlistOutcome.ALREADY, watchlist.LISTED))

    async def test_a_write_the_list_does_not_show_is_failed(self):
        """A success body proves nothing: TMDB answered a write to the wrong
        account's address as done. The read-back decides."""
        with FakeTmdb(ignores=[4]):
            result = await watchlist.put(SETTINGS, [title(4)])
        self.assertEqual(result["show:tmdb:4"].outcome, WatchlistOutcome.FAILED)

    async def test_an_id_tmdb_does_not_know_is_not_found(self):
        with FakeTmdb(unknown=[999999999]):
            result = await watchlist.put(SETTINGS, [title(999999999), title(5)])
        self.assertEqual(result["show:tmdb:999999999"].outcome, WatchlistOutcome.NOT_FOUND)
        self.assertEqual(result["show:tmdb:5"].outcome, WatchlistOutcome.WRITTEN)

    async def test_a_show_with_no_tmdb_id_is_not_sent(self):
        with FakeTmdb() as tmdb:
            result = await watchlist.put(SETTINGS, [title(None, tvdb=9)])
        self.assertEqual(tmdb.writes(), [])
        self.assertEqual(result["show:tvdb:9"].outcome, WatchlistOutcome.NOT_FOUND)

    async def test_a_refusal_before_any_answer_raises(self):
        """A revoked link: nothing is known to have happened, which is "could
        not ask", never a per-title outcome."""
        with FakeTmdb() as tmdb:
            tmdb.fail = {2: 401}
            with self.assertRaises(transport.TmdbError):
                await watchlist.put(SETTINGS, [title(4), title(5)])

    async def test_a_later_failure_fails_only_its_own_title(self):
        with FakeTmdb() as tmdb:
            tmdb.fail = {3: 500}
            result = await watchlist.put(SETTINGS, [title(4), title(5)])
        self.assertEqual(result["show:tmdb:4"].outcome, WatchlistOutcome.WRITTEN)
        self.assertEqual(result["show:tmdb:5"].outcome, WatchlistOutcome.FAILED)

    async def test_an_unreadable_list_afterwards_confirms_nothing(self):
        with FakeTmdb() as tmdb:
            tmdb.fail = {3: 503}
            result = await watchlist.put(SETTINGS, [title(4)])
        self.assertEqual(result["show:tmdb:4"].outcome, WatchlistOutcome.FAILED)

    async def test_an_empty_batch_asks_nothing(self):
        with FakeTmdb() as tmdb:
            self.assertEqual(await watchlist.put(SETTINGS, []), {})
            self.assertEqual(await watchlist.drop(SETTINGS, []), {})
            self.assertEqual(await watchlist.undo(SETTINGS, []), {})
        self.assertEqual(tmdb.calls, [])


class DropTests(unittest.IsolatedAsyncioTestCase):
    """Abandoning a show on the tracker takes it off the TMDB watchlist ONLY if
    the tracker put it there; a show the viewer listed first stays."""

    async def test_a_show_the_tracker_added_comes_off(self):
        with FakeTmdb(listed=[4]) as tmdb:
            result = await watchlist.drop(SETTINGS, [WatchlistDrop(title(4), NOT_HELD)])
        self.assertEqual(tmdb.writes(), [(4, False)])
        self.assertEqual(tmdb.listed, [])
        self.assertEqual((result["show:tmdb:4"].outcome, result["show:tmdb:4"].status_after),
                         (WatchlistOutcome.WRITTEN, NOT_HELD))

    async def test_a_show_the_viewer_listed_first_stays(self):
        with FakeTmdb(listed=[4]) as tmdb:
            result = await watchlist.drop(SETTINGS, [WatchlistDrop(title(4), watchlist.LISTED)])
        self.assertEqual(tmdb.writes(), [])
        self.assertEqual(tmdb.listed, [4])
        self.assertEqual(result["show:tmdb:4"].outcome, WatchlistOutcome.KEPT)

    async def test_a_show_with_no_confirmed_write_stays(self):
        """No record of the tracker writing it: what is listed is the viewer's."""
        with FakeTmdb(listed=[4]) as tmdb:
            result = await watchlist.drop(SETTINGS, [WatchlistDrop(title(4), None)])
        self.assertEqual(tmdb.writes(), [])
        self.assertEqual(result["show:tmdb:4"].outcome, WatchlistOutcome.KEPT)

    async def test_a_show_no_longer_listed_is_not_sent(self):
        """A removal of an unlisted title answers 13 like a real one, so only the
        read before can say there is nothing to do — and nothing is ever ADDED in
        order to drop it."""
        with FakeTmdb() as tmdb:
            result = await watchlist.drop(SETTINGS, [WatchlistDrop(title(4), NOT_HELD)])
        self.assertEqual(tmdb.writes(), [])
        self.assertEqual(result["show:tmdb:4"].outcome, WatchlistOutcome.ALREADY)


class UndoTests(unittest.IsolatedAsyncioTestCase):

    async def test_the_trackers_own_add_is_taken_back(self):
        with FakeTmdb(listed=[4, 5]) as tmdb:
            result = await watchlist.undo(SETTINGS, [WatchlistUndo(title(4), NOT_HELD)])
        self.assertEqual(tmdb.listed, [5])
        self.assertEqual(result["show:tmdb:4"].outcome, WatchlistOutcome.WRITTEN)

    async def test_a_show_listed_before_is_kept(self):
        with FakeTmdb(listed=[4]) as tmdb:
            result = await watchlist.undo(SETTINGS, [WatchlistUndo(title(4), watchlist.LISTED)])
        self.assertEqual(tmdb.writes(), [])
        self.assertEqual(result["show:tmdb:4"].outcome, WatchlistOutcome.KEPT)

    async def test_a_show_the_viewer_took_off_is_already_undone(self):
        with FakeTmdb() as tmdb:
            result = await watchlist.undo(SETTINGS, [WatchlistUndo(title(4), NOT_HELD)])
        self.assertEqual(tmdb.writes(), [])
        self.assertEqual(result["show:tmdb:4"].outcome, WatchlistOutcome.ALREADY)


class ListUrlTests(unittest.TestCase):

    def test_the_list_is_addressed_by_username(self):
        self.assertEqual(watchlist.list_url(ACCOUNT, "JoshTheBlack"),
                         "https://www.themoviedb.org/u/JoshTheBlack/watchlist/tv")

    def test_no_username_is_no_link(self):
        self.assertIsNone(watchlist.list_url(ACCOUNT, None))
        self.assertIsNone(watchlist.list_url(ACCOUNT, "  "))

    def test_a_username_is_escaped(self):
        self.assertEqual(watchlist.list_url(ACCOUNT, "a/b c"),
                         "https://www.themoviedb.org/u/a%2Fb%20c/watchlist/tv")


class RatingsReadTests(unittest.IsolatedAsyncioTestCase):

    async def test_shows_then_films_every_page(self):
        pages = {("tv", 1): ([{"id": 1, "account_rating": {"value": 8.5}}], 2),
                 ("tv", 2): ([{"id": 2, "account_rating": {"value": 3}}], 2),
                 ("movie", 1): ([{"id": 550, "account_rating": {"value": 10}}], 1)}
        asked = []

        async def answer(method, url, path, *, token, body=None):
            parts = urlsplit(url)
            kind = parts.path.split("/")[4]
            page = int(parse_qs(parts.query)["page"][0])
            asked.append((parts.path, page, token))
            items, total = pages[(kind, page)]
            return httpx.Response(200, json={"page": page, "results": items,
                                             "total_pages": total})

        with patch.object(transport, "send_private", answer):
            rated = await account.ratings(SETTINGS)
        self.assertEqual([(media, item["id"]) for media, item in rated],
                         [(Media.SHOW, 1), (Media.SHOW, 2), (Media.MOVIE, 550)])
        self.assertEqual({path for path, _page, _token in asked},
                         {f"/4/account/{ACCOUNT}/tv/rated", f"/4/account/{ACCOUNT}/movie/rated"})
        self.assertEqual({token for *_rest, token in asked}, {"the-viewers-token"})
