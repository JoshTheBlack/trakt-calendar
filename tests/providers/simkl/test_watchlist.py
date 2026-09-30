"""app/providers/simkl/watchlist.py — writing the tracker's shows to a person's
Simkl Plan to Watch list, and taking those writes back.

THE ANSWER BODIES HERE ARE SIMKL'S OWN, recorded against the live service on
2026-09-30 on titles the account's owner set aside for it; only the ids inside
an echo differ where this module sends different ids than the recording did.
No network: the library read and the POST are the transport's two entry points,
patched on the module object.
"""
from __future__ import annotations

import unittest
from unittest.mock import patch

import httpx

from app.config import Settings
from app.providers.base import (NOT_HELD, ItemKey, WatchlistOutcome, WatchlistTitle,
                                WatchlistUndo)
from app.providers.simkl import transport, watchlist

SETTINGS = Settings(simkl_client_id="an-id", simkl_access_token="a-token")

# The measured answers.
REFUSED_LEGACY_SHAPE = {"error": "empty_field", "code": 400,
                        "message": "Missed \"to\" parameter"}
REMOVED = {"deleted": {"movies": 0, "shows": 1, "episodes": 0, "rewatches": 0,
                       "rewatch_episodes": 0},
           "not_found": {"movies": [], "shows": [], "episodes": []}}


def added(to, *ids, anime=False, not_found=()):
    """An add-to-list answer as Simkl writes it: every item echoes the ids it
    was sent, anime normalised into `shows`."""
    return {"added": {"movies": [], "shows": [
                {"to": to, "ids": dict(each), "type": "show",
                 "anime_type": "tv" if anime else None} for each in ids]},
            "not_found": {"movies": [], "shows": [
                {"to": to, "ids": dict(each), "type": "show"} for each in not_found]}}


def item(simkl, status, *, tmdb=None, watched=0, last=None):
    ids = {"simkl": simkl, "slug": f"title-{simkl}"}
    if tmdb is not None:
        ids["tmdb"] = str(tmdb)
    return {"show": {"title": f"Title {simkl}", "ids": ids}, "status": status,
            "watched_episodes_count": watched, "last_watched_at": last,
            "total_episodes_count": 10}


def library(*items, anime=()):
    doc = {}
    if items:
        doc["shows"] = list(items)
    if anime:
        doc["anime"] = list(anime)
    return doc


def title(tmdb, simkl=None, **more):
    ids = {"tmdb": tmdb, **more}
    if simkl is not None:
        ids["simkl"] = simkl
    return WatchlistTitle(ItemKey("show", "tmdb", str(tmdb)), ids)


class _Simkl:
    """The two transport entry points: library reads answered in order, POSTs
    recorded and answered in order."""

    def __init__(self, reads, answers=()):
        self.reads = list(reads)
        self.answers = list(answers)
        self.read_calls = []
        self.posts = []

    async def cached_get(self, client, settings, path, params=None, **kw):
        self.read_calls.append((path, kw))
        answer = self.reads.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    async def send_or_unavailable(self, client, method, url, *, pool, headers=None,
                                  json=None, timeout=None):
        self.posts.append({"method": method, "path": url.split("?")[0].replace(
            transport.API_BASE + "/", ""), "body": json, "pool": pool, "headers": headers})
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        status, body = answer
        return httpx.Response(status, json=body)

    def __enter__(self):
        self._patches = [patch.object(transport, "cached_get", new=self.cached_get),
                         patch.object(transport, "send_or_unavailable",
                                      new=self.send_or_unavailable)]
        for p in self._patches:
            p.start()
        return self

    def __exit__(self, *exc):
        for p in self._patches:
            p.stop()


class PutTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_title_held_nowhere_is_written_and_confirmed(self):
        fake = _Simkl(reads=[library(), library(item(3152, "plantowatch", tmdb=4238))],
                      answers=[(201, added("plantowatch", {"simkl": 3152}))])
        with fake:
            result = await watchlist.put(SETTINGS, [title(4238, simkl=3152)])
        outcome = result["show:tmdb:4238"]
        self.assertEqual(outcome.outcome, WatchlistOutcome.WRITTEN)
        self.assertEqual((outcome.status_before, outcome.status_after),
                         (NOT_HELD, "plantowatch"))
        self.assertEqual(outcome.service_id, "3152")

    async def test_the_write_is_the_measured_shape(self):
        """`to` on each item — a top-level `to` is refused — every title in one
        request under `shows`, named by Simkl's own id alone when it is known,
        on the sequential pool with the viewer's own bearer."""
        fake = _Simkl(reads=[library(), library(item(3152, "plantowatch"),
                                                item(41487, "plantowatch"))],
                      answers=[(201, added("plantowatch", {"simkl": 3152}, {"simkl": 41487}))])
        with fake:
            await watchlist.put(SETTINGS, [title(4238, simkl=3152, tvdb=73641),
                                           title(12971, simkl=41487)])
        post, = fake.posts
        self.assertEqual(post["path"], "sync/add-to-list")
        self.assertEqual(post["body"], {"shows": [
            {"to": "plantowatch", "ids": {"simkl": 3152}},
            {"to": "plantowatch", "ids": {"simkl": 41487}}]})
        self.assertIs(post["pool"], transport.SYNC_POOL)
        self.assertEqual(post["headers"]["Authorization"], "Bearer a-token")

    async def test_every_read_of_the_library_is_private(self):
        """The answer is one person's library; the response cache is shared by
        the whole instance and keyed on a URL every account's read shares."""
        fake = _Simkl(reads=[library(), library(item(3152, "plantowatch"))],
                      answers=[(201, added("plantowatch", {"simkl": 3152}))])
        with fake:
            await watchlist.put(SETTINGS, [title(4238, simkl=3152)])
        self.assertEqual(len(fake.read_calls), 2)
        for path, kw in fake.read_calls:
            self.assertEqual(path, "sync/all-items/")
            self.assertTrue(kw["private"])

    async def test_a_title_already_there_is_not_sent(self):
        """Measured: Simkl answers a put of a title already in Plan to Watch with
        the SAME `added` body as a real move, so only the read before can say it
        was already there."""
        fake = _Simkl(reads=[library(item(3152, "plantowatch"))])
        with fake:
            result = await watchlist.put(SETTINGS, [title(4238, simkl=3152)])
        self.assertEqual(result["show:tmdb:4238"].outcome, WatchlistOutcome.ALREADY)
        self.assertEqual(fake.posts, [])

    async def test_whatever_the_viewer_had_it_as_moves(self):
        fake = _Simkl(reads=[library(item(3152, "watching", watched=4)),
                             library(item(3152, "plantowatch", watched=4))],
                      answers=[(201, added("plantowatch", {"simkl": 3152}))])
        with fake:
            result = await watchlist.put(SETTINGS, [title(4238, simkl=3152)])
        self.assertEqual(result["show:tmdb:4238"].outcome, WatchlistOutcome.WRITTEN)
        self.assertEqual(result["show:tmdb:4238"].status_before, "watching")

    async def test_an_answer_the_list_does_not_bear_out_is_a_failure(self):
        """THE 200 THAT DID NOTHING. The body said added; the list says it is not
        there. That is not success."""
        fake = _Simkl(reads=[library(), library()],
                      answers=[(201, added("plantowatch", {"simkl": 3152}))])
        with fake:
            result = await watchlist.put(SETTINGS, [title(4238, simkl=3152)])
        self.assertEqual(result["show:tmdb:4238"].outcome, WatchlistOutcome.FAILED)

    async def test_an_answer_with_no_usable_body_is_a_failure(self):
        for body in (None, [], "ok"):
            with self.subTest(body=body):
                fake = _Simkl(reads=[library()], answers=[(201, body)])
                with fake:
                    result = await watchlist.put(SETTINGS, [title(4238, simkl=3152)])
                self.assertEqual(result["show:tmdb:4238"].outcome, WatchlistOutcome.FAILED)

    async def test_a_title_the_answer_does_not_mention_is_a_failure(self):
        fake = _Simkl(reads=[library(), library(item(3152, "plantowatch"))],
                      answers=[(201, added("plantowatch", {"simkl": 3152}))])
        with fake:
            result = await watchlist.put(SETTINGS, [title(4238, simkl=3152),
                                                    title(1, simkl=77)])
        self.assertEqual(result["show:tmdb:1"].outcome, WatchlistOutcome.FAILED)
        self.assertEqual(result["show:tmdb:4238"].outcome, WatchlistOutcome.WRITTEN)

    async def test_a_title_simkl_cannot_match_is_not_found(self):
        """The measured answer to an id Simkl does not know: echoed back under
        `not_found`, in the same 201."""
        fake = _Simkl(reads=[library()],
                      answers=[(201, added("plantowatch", not_found=[{"simkl": 999999999}]))])
        with fake:
            result = await watchlist.put(SETTINGS, [title(4238, simkl=999999999)])
        self.assertEqual(result["show:tmdb:4238"].outcome, WatchlistOutcome.NOT_FOUND)

    async def test_a_refused_write_raises_rather_than_answering(self):
        """Measured: the legacy body shape is refused with 400 `empty_field`. Any
        refusal of the whole write means Simkl could not be asked, which is not a
        per-title answer."""
        fake = _Simkl(reads=[library()], answers=[(400, REFUSED_LEGACY_SHAPE)])
        with fake, self.assertRaises(transport.SimklError) as caught:
            await watchlist.put(SETTINGS, [title(4238, simkl=3152)])
        self.assertEqual(caught.exception.status, 400)

    async def test_a_library_that_cannot_be_read_writes_nothing(self):
        fake = _Simkl(reads=[transport.SimklError("down", 503)])
        with fake, self.assertRaises(transport.SimklError):
            await watchlist.put(SETTINGS, [title(4238, simkl=3152)])
        self.assertEqual(fake.posts, [])

    async def test_a_read_back_that_fails_confirms_nothing(self):
        fake = _Simkl(reads=[library(), transport.SimklError("down", 503)],
                      answers=[(201, added("plantowatch", {"simkl": 3152}))])
        with fake:
            result = await watchlist.put(SETTINGS, [title(4238, simkl=3152)])
        self.assertEqual(result["show:tmdb:4238"].outcome, WatchlistOutcome.FAILED)

    async def test_a_title_with_no_simkl_id_is_sent_by_its_shared_ids(self):
        """And the Simkl id the read-back finds it under is learned."""
        fake = _Simkl(reads=[library(), library(item(3152, "plantowatch", tmdb=4238))],
                      answers=[(201, added("plantowatch",
                                           {"tmdb": 4238, "imdb": "tt0165581"}))])
        with fake:
            result = await watchlist.put(SETTINGS, [title(4238, imdb="tt0165581")])
        self.assertEqual(fake.posts[0]["body"]["shows"][0]["ids"],
                         {"tmdb": 4238, "imdb": "tt0165581"})
        self.assertEqual(result["show:tmdb:4238"].outcome, WatchlistOutcome.WRITTEN)
        self.assertEqual(result["show:tmdb:4238"].service_id, "3152")

    async def test_a_shared_id_naming_several_simkl_titles_is_not_guessed_at(self):
        """Simkl files each anime season as its own title, and several share one
        TMDB id. With no Simkl id of its own, a write could move one of the
        viewer's titles that is not this one."""
        fake = _Simkl(reads=[library(anime=[item(39687, "watching", tmdb=1429),
                                            item(439744, "hold", tmdb=1429)])])
        with fake:
            result = await watchlist.put(SETTINGS, [title(1429)])
        self.assertEqual(result["show:tmdb:1429"].outcome, WatchlistOutcome.NOT_FOUND)
        self.assertEqual(fake.posts, [])

    async def test_an_anime_title_is_found_in_the_anime_list_and_sent_as_a_show(self):
        """Measured: the library files it under `anime`; add-to-list accepts it
        under `shows` and answers it there."""
        fake = _Simkl(reads=[library(), library(anime=[item(41487, "plantowatch")])],
                      answers=[(201, added("plantowatch", {"simkl": 41487}, anime=True))])
        with fake:
            result = await watchlist.put(SETTINGS, [title(12971, simkl=41487)])
        self.assertIn("shows", fake.posts[0]["body"])
        self.assertEqual(result["show:tmdb:12971"].outcome, WatchlistOutcome.WRITTEN)

    async def test_an_empty_batch_costs_nothing(self):
        fake = _Simkl(reads=[])
        with fake:
            self.assertEqual(await watchlist.put(SETTINGS, []), {})
        self.assertEqual((fake.read_calls, fake.posts), ([], []))


class ChunkTests(unittest.IsolatedAsyncioTestCase):
    """MEASURED: one write of 100 was accepted whole, in 6 s against the
    transport's 30 s budget. Larger batches go out 100 at a time, and are still
    read before once and back once."""

    def _titles(self, n):
        return [title(1000 + i, simkl=5000 + i) for i in range(n)]

    async def test_a_large_batch_is_written_a_hundred_at_a_time(self):
        titles = self._titles(150)
        after = library(*(item(5000 + i, "plantowatch") for i in range(150)))
        fake = _Simkl(reads=[library(), after],
                      answers=[(201, added("plantowatch", *({"simkl": 5000 + i} for i in range(100)))),
                               (201, added("plantowatch", *({"simkl": 5000 + i} for i in range(100, 150))))])
        with fake:
            result = await watchlist.put(SETTINGS, titles)
        self.assertEqual([len(post["body"]["shows"]) for post in fake.posts], [100, 50])
        self.assertEqual(len(fake.read_calls), 2, "one read before, one back, whatever the size")
        self.assertEqual({r.outcome for r in result.values()}, {WatchlistOutcome.WRITTEN})

    async def test_a_later_chunk_failing_fails_only_its_own_titles(self):
        titles = self._titles(150)
        after = library(*(item(5000 + i, "plantowatch") for i in range(100)))
        fake = _Simkl(reads=[library(), after],
                      answers=[(201, added("plantowatch", *({"simkl": 5000 + i} for i in range(100)))),
                               transport.SimklError("write lock", 400)])
        with fake:
            result = await watchlist.put(SETTINGS, titles)
        outcomes = [result[f"show:tmdb:{1000 + i}"].outcome for i in range(150)]
        self.assertEqual(set(outcomes[:100]), {WatchlistOutcome.WRITTEN})
        self.assertEqual(set(outcomes[100:]), {WatchlistOutcome.FAILED})

    async def test_the_first_chunk_failing_raises(self):
        fake = _Simkl(reads=[library()], answers=[transport.SimklError("down", 503)])
        with fake, self.assertRaises(transport.SimklError):
            await watchlist.put(SETTINGS, self._titles(150))


class DropTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_held_title_moves_to_dropped_whatever_it_was(self):
        fake = _Simkl(reads=[library(item(3152, "watching", watched=2)),
                             library(item(3152, "dropped", watched=2))],
                      answers=[(201, added("dropped", {"simkl": 3152}))])
        with fake:
            result = await watchlist.drop(SETTINGS, [title(4238, simkl=3152)])
        self.assertEqual(fake.posts[0]["body"],
                         {"shows": [{"to": "dropped", "ids": {"simkl": 3152}}]})
        self.assertEqual(result["show:tmdb:4238"].outcome, WatchlistOutcome.WRITTEN)
        self.assertEqual(result["show:tmdb:4238"].status_after, "dropped")

    async def test_dropping_never_adds_a_title(self):
        fake = _Simkl(reads=[library()])
        with fake:
            result = await watchlist.drop(SETTINGS, [title(4238, simkl=3152)])
        self.assertEqual(result["show:tmdb:4238"].outcome, WatchlistOutcome.ALREADY)
        self.assertEqual(fake.posts, [])

    async def test_a_title_already_dropped_is_not_sent(self):
        fake = _Simkl(reads=[library(item(3152, "dropped"))])
        with fake:
            result = await watchlist.drop(SETTINGS, [title(4238, simkl=3152)])
        self.assertEqual(result["show:tmdb:4238"].outcome, WatchlistOutcome.ALREADY)
        self.assertEqual(fake.posts, [])


class UndoTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_title_held_nowhere_before_and_unwatched_now_is_removed(self):
        """Measured: history/remove on a whole show takes it out of every list.
        Sent under `shows` — Simkl documents a top-level `anime` array there as
        silently ignored."""
        fake = _Simkl(reads=[library(item(3152, "plantowatch")), library()],
                      answers=[(201, REMOVED)])
        with fake:
            result = await watchlist.undo(SETTINGS, [WatchlistUndo(title(4238, simkl=3152),
                                                                   NOT_HELD)])
        post, = fake.posts
        self.assertEqual(post["path"], "sync/history/remove")
        self.assertEqual(post["body"], {"shows": [{"ids": {"simkl": 3152}}]})
        self.assertEqual(result["show:tmdb:4238"].outcome, WatchlistOutcome.WRITTEN)
        self.assertEqual(result["show:tmdb:4238"].status_after, NOT_HELD)

    async def test_anything_watched_now_keeps_it(self):
        """Removing would take the viewing with it. Read NOW, at the undo."""
        for watched, last in ((1, None), (0, "2026-09-30T01:00:00Z")):
            with self.subTest(watched=watched, last=last):
                fake = _Simkl(reads=[library(item(3152, "watching", watched=watched, last=last))])
                with fake:
                    result = await watchlist.undo(
                        SETTINGS, [WatchlistUndo(title(4238, simkl=3152), NOT_HELD)])
                self.assertEqual(result["show:tmdb:4238"].outcome, WatchlistOutcome.KEPT)
                self.assertEqual(fake.posts, [])

    async def test_a_count_it_cannot_read_is_not_taken_as_nothing_watched(self):
        broken = item(3152, "plantowatch")
        broken["watched_episodes_count"] = "lots"
        fake = _Simkl(reads=[library(broken)])
        with fake:
            result = await watchlist.undo(SETTINGS, [WatchlistUndo(title(4238, simkl=3152),
                                                                   NOT_HELD)])
        self.assertEqual(result["show:tmdb:4238"].outcome, WatchlistOutcome.KEPT)

    async def test_a_title_held_before_goes_back_to_what_it_was(self):
        fake = _Simkl(reads=[library(item(3152, "plantowatch", watched=3)),
                             library(item(3152, "hold", watched=3))],
                      answers=[(201, added("hold", {"simkl": 3152}))])
        with fake:
            result = await watchlist.undo(SETTINGS, [WatchlistUndo(title(4238, simkl=3152),
                                                                   "hold")])
        self.assertEqual(fake.posts[0]["body"],
                         {"shows": [{"to": "hold", "ids": {"simkl": 3152}}]})
        self.assertEqual(result["show:tmdb:4238"].outcome, WatchlistOutcome.WRITTEN)

    async def test_a_finished_show_goes_back_to_watching_never_to_completed(self):
        """MEASURED: a move to completed marked every unwatched episode of a
        three-episode history watched, dated now. A finished show can have gained
        a season since, so completed would invent viewing; watching keeps every
        real play and invents none."""
        fake = _Simkl(reads=[library(item(3152, "plantowatch", watched=5)),
                             library(item(3152, "watching", watched=5))],
                      answers=[(201, added("watching", {"simkl": 3152}))])
        with fake:
            result = await watchlist.undo(SETTINGS, [WatchlistUndo(title(4238, simkl=3152),
                                                                   "completed")])
        self.assertEqual(fake.posts[0]["body"],
                         {"shows": [{"to": "watching", "ids": {"simkl": 3152}}]})
        self.assertEqual(result["show:tmdb:4238"].outcome, WatchlistOutcome.WRITTEN)
        self.assertEqual(result["show:tmdb:4238"].status_before, "plantowatch")

    async def test_a_finished_show_already_watching_or_completed_is_not_sent(self):
        for now in ("watching", "completed"):
            with self.subTest(now=now):
                fake = _Simkl(reads=[library(item(3152, now, watched=5))])
                with fake:
                    result = await watchlist.undo(
                        SETTINGS, [WatchlistUndo(title(4238, simkl=3152), "completed")])
                self.assertEqual(result["show:tmdb:4238"].outcome, WatchlistOutcome.ALREADY)
                self.assertEqual(fake.posts, [])

    async def test_no_undo_ever_asks_for_completed(self):
        """Whatever it was before, nothing an undo sends names completed."""
        statuses = ("watching", "plantowatch", "hold", "completed", "dropped")
        rows = [WatchlistUndo(title(100 + n, simkl=900 + n), status)
                for n, status in enumerate(statuses)]
        held = library(*(item(900 + n, "plantowatch", watched=1) for n in range(len(statuses))))
        fake = _Simkl(reads=[held, held], answers=[(201, added("x"))])
        with fake:
            await watchlist.undo(SETTINGS, rows)
        sent = [entry["to"] for post in fake.posts for entry in post["body"]["shows"]]
        self.assertNotIn("completed", sent)
        self.assertIn("watching", sent)

    async def test_a_title_the_viewer_took_out_themselves_is_left_out(self):
        fake = _Simkl(reads=[library()])
        with fake:
            result = await watchlist.undo(SETTINGS, [WatchlistUndo(title(4238, simkl=3152),
                                                                   "watching")])
        self.assertEqual(result["show:tmdb:4238"].outcome, WatchlistOutcome.KEPT)
        self.assertEqual(fake.posts, [])

    async def test_a_title_already_as_it_was_is_not_sent(self):
        for before, lib in ((NOT_HELD, library()), ("hold", library(item(3152, "hold")))):
            with self.subTest(before=before):
                fake = _Simkl(reads=[lib])
                with fake:
                    result = await watchlist.undo(
                        SETTINGS, [WatchlistUndo(title(4238, simkl=3152), before)])
                self.assertEqual(result["show:tmdb:4238"].outcome, WatchlistOutcome.ALREADY)
                self.assertEqual(fake.posts, [])

    async def test_a_removal_the_list_does_not_bear_out_is_a_failure(self):
        fake = _Simkl(reads=[library(item(3152, "plantowatch")),
                             library(item(3152, "plantowatch"))],
                      answers=[(201, REMOVED)])
        with fake:
            result = await watchlist.undo(SETTINGS, [WatchlistUndo(title(4238, simkl=3152),
                                                                   NOT_HELD)])
        self.assertEqual(result["show:tmdb:4238"].outcome, WatchlistOutcome.FAILED)

    async def test_a_second_write_failing_keeps_the_answer_about_the_first(self):
        """Moves and removals are two writes. Once the first has been answered,
        what it did is kept, and only the second's titles fail."""
        fake = _Simkl(
            reads=[library(item(3152, "plantowatch", watched=2), item(41487, "plantowatch")),
                   library(item(3152, "hold", watched=2), item(41487, "plantowatch"))],
            answers=[(201, added("hold", {"simkl": 3152})),
                     transport.SimklError("write lock", 400)])
        with fake:
            result = await watchlist.undo(SETTINGS, [
                WatchlistUndo(title(4238, simkl=3152), "hold"),
                WatchlistUndo(title(12971, simkl=41487), NOT_HELD)])
        self.assertEqual(result["show:tmdb:4238"].outcome, WatchlistOutcome.WRITTEN)
        self.assertEqual(result["show:tmdb:12971"].outcome, WatchlistOutcome.FAILED)

    async def test_the_first_write_failing_raises(self):
        fake = _Simkl(reads=[library(item(3152, "plantowatch"))],
                      answers=[transport.SimklError("down", 503)])
        with fake, self.assertRaises(transport.SimklError):
            await watchlist.undo(SETTINGS, [WatchlistUndo(title(4238, simkl=3152), NOT_HELD)])


class HoldingsTests(unittest.IsolatedAsyncioTestCase):
    async def test_every_title_comes_back(self):
        fake = _Simkl(reads=[library(item(3152, "watching", watched=4, tmdb=4238))])
        with fake:
            held = await watchlist.holdings(SETTINGS, [title(4238, simkl=3152),
                                                       title(1, simkl=77)])
        self.assertEqual((held["show:tmdb:4238"].status, held["show:tmdb:4238"].watched),
                         ("watching", True))
        self.assertEqual(held["show:tmdb:1"].status, NOT_HELD)
        self.assertEqual(held["show:tmdb:1"].service_id, "77")

    async def test_a_body_that_is_not_a_library_reads_as_holding_nothing(self):
        """Safe in every direction: it can only make a title look not held, and
        nothing is ever removed on the strength of that."""
        fake = _Simkl(reads=[None])
        with fake:
            held = await watchlist.holdings(SETTINGS, [title(4238, simkl=3152)])
        self.assertEqual(held["show:tmdb:4238"].status, NOT_HELD)


class ListUrlTests(unittest.TestCase):
    def test_the_accounts_plan_to_watch_page(self):
        self.assertEqual(watchlist.list_url("8814058"),
                         "https://simkl.com/8814058/tv/plantowatch/")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
