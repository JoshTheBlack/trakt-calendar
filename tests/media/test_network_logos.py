"""Which logo a network has, recorded in `network_logos`, and the badge drawn from it.

What is held here: a badge is read from the table in ONE statement for a whole
page; a look-up that could not get an answer is never recorded as "no logo"; a
row is rewritten whole so it has one clock, drawn only inside TMDB's six-month
ceiling and swept by it; a warm resolves a bounded number at a time and one warm
at a time; the old disk tiles are removed; and only the Discord download
composites bytes.

TMDB and the image proxy are answered by stubs — nothing here touches the network.
"""
from __future__ import annotations

import asyncio
import tempfile
import threading
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from PIL import Image

from app import db
from app.media import logos
from app.providers.base import proxied_origin
from app.providers.tmdb import transport as tmdb_transport
from tests.support import DatabaseTestCase

CFG = SimpleNamespace(tmdb_configured=True)
DAY = 24 * 60 * 60


def _png(colour, size=(200, 80)) -> bytes:
    """A logo-shaped PNG: an opaque block of `colour` inside transparent margins,
    which is what a real network logo looks like to the trim step."""
    img = Image.new("RGBA", size, (0, 0, 0, 0))
    img.paste(Image.new("RGBA", (size[0] - 40, size[1] - 20), colour), (20, 10))
    out = BytesIO()
    img.save(out, format="PNG")
    return out.getvalue()


DARK, LIGHT = _png((20, 20, 20, 255)), _png((250, 250, 250, 255))


class TMDBStub:
    """TMDB's `/tv/{id}` networks, a network's image list and the image proxy,
    recording what was fetched."""

    def __init__(self, test, *, networks=None, images=None, proxied=None, down=False):
        self.fetched: list[str] = []
        self.asked: list = []
        self.networks, self.images, self.proxied, self.down = networks, images, proxied, down

        async def title_payload(settings, tmdb_id, media, **kw):
            self.asked.append(tmdb_id)
            if self.down:
                raise tmdb_transport.TmdbError("unreachable")
            return None if self.networks is None else {"networks": self.networks}

        async def cached_get(settings, path, params=None, **kw):
            return self.images

        async def download(url):
            self.fetched.append(url)
            return self.proxied

        for target, name, stub in ((logos.tmdb_detail, "title_payload", title_payload),
                                   (logos.tmdb_transport, "cached_get", cached_get),
                                   (logos.tmdb_transport, "download", download)):
            patcher = patch.object(target, name, stub)
            patcher.start()
            test.addCleanup(patcher.stop)


async def _put(network, *, logo_path="/amc.png", light=0, missing=0, checked_at=None,
               fetched_at=None, fail_count=0, last_failed_at=None, tmdb_network_id=1):
    now = db.now()
    await db.execute(
        "INSERT INTO network_logos (network, tmdb_network_id, logo_path, light, missing, "
        "checked_at, fetched_at, fail_count, last_failed_at) VALUES (?,?,?,?,?,?,?,?,?)",
        (network, tmdb_network_id, logo_path, light, missing,
         now if checked_at is None else checked_at,
         now if fetched_at is None else fetched_at, fail_count, last_failed_at))


async def _row(network):
    row = await db.fetch_one("SELECT * FROM network_logos WHERE network = ?", (network,))
    return dict(row) if row else None


class TheBadgeIsReadFromTheTableTests(DatabaseTestCase):

    async def test_a_logo_is_the_proxys_address_and_whether_it_is_light(self):
        await _put("AMC", logo_path="/amc.png", light=0)
        await _put("Starz", logo_path="/starz.png", light=1)
        found = await logos.known(["AMC", "Starz", "Nowhere", "", None])
        self.assertEqual(set(found), {"AMC", "Starz"})
        self.assertFalse(found["AMC"].light)
        self.assertTrue(found["Starz"].light)
        # TMDB's image, through the one proxy the page's Content-Security-Policy
        # admits, at the size a badge is.
        self.assertTrue(found["AMC"].address.startswith("https://wsrv.nl/?url="))
        self.assertEqual(proxied_origin(found["AMC"].address),
                         "https://image.tmdb.org/t/p/w300/amc.png")
        self.assertIn("w=128&h=128&fit=contain&output=png", found["AMC"].address)

    async def test_a_network_with_no_logo_is_not_drawn_with_one(self):
        await _put("Tiny", logo_path="", missing=1)
        self.assertEqual(await logos.known(["Tiny"]), {})

    async def test_an_svg_is_asked_for_at_the_size_the_cdn_serves_it(self):
        await _put("HBO", logo_path="/hbo.svg")
        self.assertEqual(proxied_origin((await logos.known(["HBO"]))["HBO"].address),
                         "https://image.tmdb.org/t/p/original/hbo.svg")

    async def test_a_row_past_the_ceiling_is_not_drawn_even_before_a_sweep_has_run(self):
        past = db.now() - tmdb_transport.MAX_CACHE_SECONDS - 1
        await _put("Old", fetched_at=past)
        await _put("New")
        self.assertEqual(set(await logos.known(["Old", "New"])), {"New"})

    async def test_a_whole_page_is_one_statement(self):
        for n in range(30):
            await _put(f"Net {n}")
        statements = []

        async def traced(sql, params=()):
            def work(conn):
                conn.set_trace_callback(statements.append)
                try:
                    return conn.execute(sql, params).fetchall()
                finally:
                    conn.set_trace_callback(None)
            return await db.run(work)

        with patch.object(db, "fetch_all", traced):
            found = await logos.known([f"Net {n}" for n in range(30)] + ["Absent"])
        self.assertEqual(len(found), 30)
        self.assertEqual(len([s for s in statements if "network_logos" in s]), 1)


class WhenANetworkIsAskedAboutTests(DatabaseTestCase):

    def due(self, row, now=None):
        return logos.is_due(row, db.now() if now is None else now)

    def row(self, **fields):
        base = {"missing": 0, "checked_at": None, "last_failed_at": None, "fail_count": 0}
        return {**base, **fields}

    async def test_a_network_never_asked_about_is_due(self):
        self.assertTrue(self.due(None))

    async def test_a_logo_is_asked_again_only_after_a_month(self):
        now = db.now()
        self.assertFalse(self.due(self.row(checked_at=now - 29 * DAY), now))
        self.assertTrue(self.due(self.row(checked_at=now - 31 * DAY), now))

    async def test_a_network_with_no_logo_is_asked_again_sooner(self):
        now = db.now()
        self.assertFalse(self.due(self.row(missing=1, checked_at=now - 6 * DAY), now))
        self.assertTrue(self.due(self.row(missing=1, checked_at=now - 8 * DAY), now))

    async def test_a_failure_waits_before_the_next_attempt(self):
        now = db.now()
        failed = self.row(fail_count=1, last_failed_at=now - 60)
        self.assertFalse(self.due(failed, now))
        self.assertTrue(self.due(self.row(fail_count=1, last_failed_at=now - 11 * 60), now))

    async def test_a_failure_after_a_good_answer_does_not_make_it_due_early(self):
        now = db.now()
        self.assertFalse(self.due(self.row(checked_at=now - DAY, last_failed_at=now - 60,
                                           fail_count=1), now))


class ALookUpIsFoundMissingOrUnansweredTests(DatabaseTestCase):

    async def test_a_dark_logo_is_found_and_not_light(self):
        stub = TMDBStub(self, networks=[{"id": 1, "name": "AMC", "logo_path": "/amc.png"}],
                        proxied=DARK)
        got = await logos.look_up(CFG, "AMC", (99,))
        self.assertEqual(got, logos.Resolution(logos.Kind.FOUND, 1, "/amc.png", False))
        # Judged on the very picture a browser is handed.
        self.assertEqual(proxied_origin(stub.fetched[0]), "https://image.tmdb.org/t/p/w300/amc.png")
        self.assertIn("output=png", stub.fetched[0])

    async def test_a_light_logo_is_light(self):
        TMDBStub(self, networks=[{"id": 1, "name": "Starz", "logo_path": "/s.png"}], proxied=LIGHT)
        self.assertTrue((await logos.look_up(CFG, "Starz", (1,))).light)

    async def test_the_networks_own_image_list_stands_in_for_a_missing_logo(self):
        TMDBStub(self, networks=[{"id": 7, "name": "HBO", "logo_path": None}],
                 images={"logos": [{"file_path": "/hbo.svg"}, {"file_path": "/hbo.png"}]},
                 proxied=DARK)
        got = await logos.look_up(CFG, "HBO", (5,))
        self.assertEqual((got.kind, got.logo_path), (logos.Kind.FOUND, "/hbo.png"))

    async def test_no_logo_anywhere_is_a_recorded_answer(self):
        TMDBStub(self, networks=[{"id": 1, "name": "Tiny", "logo_path": None}],
                 images={"logos": []})
        self.assertEqual((await logos.look_up(CFG, "Tiny", (3,))).kind, logos.Kind.MISSING)

    async def test_a_show_filed_under_another_network_is_not_that_networks_logo(self):
        """MEASURED on a copy of the owner's data: "BBC Two" drew Channel 4's logo,
        because the one show it was resolved through is listed on Channel 4 and the
        old rule took the show's first network when none matched."""
        TMDBStub(self, networks=[{"id": 9, "name": "Channel 4", "logo_path": "/c4.png"}],
                 proxied=LIGHT)
        got = await logos.look_up(CFG, "BBC Two", (7,))
        self.assertEqual((got.kind, got.logo_path), (logos.Kind.MISSING, ""))

    async def test_the_next_show_is_tried_when_the_first_is_on_another_network(self):
        stub = TMDBStub(self, networks=None, proxied=DARK)
        answers = {7: [{"id": 9, "name": "Channel 4", "logo_path": "/c4.png"}],
                   8: [{"id": 4, "name": "BBC Two", "logo_path": "/bbc2.png"}]}

        async def title_payload(settings, tmdb_id, media, **kw):
            stub.asked.append(tmdb_id)
            return {"networks": answers[tmdb_id]}

        with patch.object(logos.tmdb_detail, "title_payload", title_payload):
            got = await logos.look_up(CFG, "BBC Two", (7, 8))
        self.assertEqual((got.kind, got.logo_path), (logos.Kind.FOUND, "/bbc2.png"))
        self.assertEqual(stub.asked, [7, 8])

    async def test_the_shows_after_the_one_that_named_it_are_not_asked(self):
        stub = TMDBStub(self, networks=[{"id": 1, "name": "AMC", "logo_path": "/amc.png"}],
                        proxied=DARK)
        await logos.look_up(CFG, "AMC", (1, 2, 3))
        self.assertEqual(stub.asked, [1])

    async def test_a_show_that_could_not_be_asked_does_not_decide_the_network(self):
        """One show down and one on another network: nothing about THIS network
        was ever said, so it is no answer rather than "no logo"."""
        stub = TMDBStub(self, networks=None)

        async def title_payload(settings, tmdb_id, media, **kw):
            if tmdb_id == 1:
                raise tmdb_transport.TmdbError("unreachable")
            return None

        with patch.object(logos.tmdb_detail, "title_payload", title_payload):
            got = await logos.look_up(CFG, "AMC", (1, 2))
        self.assertEqual(got.kind, logos.Kind.UNANSWERED)
        self.assertEqual(stub.fetched, [])

    async def test_a_title_with_no_networks_is_a_recorded_answer(self):
        TMDBStub(self, networks=[])
        self.assertEqual((await logos.look_up(CFG, "Tiny", (3,))).kind, logos.Kind.MISSING)

    async def test_a_tmdb_that_could_not_be_asked_is_not_an_answer(self):
        TMDBStub(self, down=True)
        self.assertEqual((await logos.look_up(CFG, "AMC", (99,))).kind, logos.Kind.UNANSWERED)

    async def test_a_title_tmdb_does_not_know_is_not_an_answer_about_the_network(self):
        TMDBStub(self, networks=None)
        self.assertEqual((await logos.look_up(CFG, "AMC", (99,))).kind, logos.Kind.UNANSWERED)

    async def test_a_proxy_that_could_not_be_reached_is_not_an_answer(self):
        TMDBStub(self, networks=[{"id": 1, "name": "AMC", "logo_path": "/amc.png"}], proxied=None)
        self.assertEqual((await logos.look_up(CFG, "AMC", (99,))).kind, logos.Kind.UNANSWERED)

    async def test_bytes_that_are_not_a_picture_decide_nothing(self):
        TMDBStub(self, networks=[{"id": 1, "name": "AMC", "logo_path": "/amc.png"}],
                 proxied=b"<html>nope</html>")
        self.assertEqual((await logos.look_up(CFG, "AMC", (99,))).kind, logos.Kind.UNANSWERED)


class RecordingWhatWasFoundTests(DatabaseTestCase):

    async def test_a_found_logo_writes_every_word_and_one_clock(self):
        await logos.record("AMC", logos.Resolution(logos.Kind.FOUND, 4, "/amc.png", True))
        row = await _row("AMC")
        self.assertEqual((row["tmdb_network_id"], row["logo_path"], row["light"], row["missing"]),
                         (4, "/amc.png", 1, 0))
        self.assertEqual(row["checked_at"], row["fetched_at"])
        self.assertEqual((row["fail_count"], row["last_failed_at"]), (0, None))

    async def test_no_logo_is_recorded_as_such(self):
        await logos.record("Tiny", logos.Resolution(logos.Kind.MISSING, 9))
        row = await _row("Tiny")
        self.assertEqual((row["missing"], row["logo_path"]), (1, ""))
        self.assertIsNotNone(row["fetched_at"])

    async def test_a_failure_is_counted_and_leaves_a_held_logo_exactly_as_it_was(self):
        await _put("AMC", logo_path="/amc.png", light=1, fetched_at=1_000, checked_at=1_000)
        await logos.record("AMC", logos.Resolution(logos.Kind.UNANSWERED))
        row = await _row("AMC")
        self.assertEqual((row["logo_path"], row["light"], row["fetched_at"], row["checked_at"]),
                         ("/amc.png", 1, 1_000, 1_000))
        self.assertEqual(row["fail_count"], 1)
        self.assertIsNotNone(row["last_failed_at"])

    async def test_a_failure_for_a_network_never_answered_is_a_row_with_no_ceiling_clock(self):
        await logos.record("Fresh", logos.Resolution(logos.Kind.UNANSWERED))
        row = await _row("Fresh")
        self.assertIsNone(row["fetched_at"])
        self.assertEqual(await logos.known(["Fresh"]), {})

    async def test_a_new_answer_clears_the_failures(self):
        await logos.record("AMC", logos.Resolution(logos.Kind.UNANSWERED))
        await logos.record("AMC", logos.Resolution(logos.Kind.FOUND, 1, "/amc.png", False))
        row = await _row("AMC")
        self.assertEqual((row["fail_count"], row["last_failed_at"]), (0, None))


class AWarmIsBoundedAndOneAtATimeTests(DatabaseTestCase):

    async def test_it_resolves_at_most_eight_at_once(self):
        state = {"now": 0, "max": 0}

        async def look_up(settings, network, tmdb):
            state["now"] += 1
            state["max"] = max(state["max"], state["now"])
            await asyncio.sleep(0.01)
            state["now"] -= 1
            return logos.Resolution(logos.Kind.FOUND, 1, "/x.png", False)

        with patch.object(logos, "look_up", look_up):
            found = await logos.warm(CFG, {f"Net {n}": (n + 1,) for n in range(40)})
        self.assertEqual((state["max"], found), (logos.BUILDS_AT_ONCE, 40))
        self.assertEqual(logos.BUILDS_AT_ONCE, 8)
        self.assertEqual(len(await logos.known([f"Net {n}" for n in range(40)])), 40)

    async def test_one_failure_does_not_sink_the_rest(self):
        async def look_up(settings, network, tmdb):
            if network == "AMC":
                raise RuntimeError("tmdb down")
            return logos.Resolution(logos.Kind.FOUND, 1, "/x.png", False)

        with patch.object(logos, "look_up", look_up):
            found = await logos.warm(CFG, {"AMC": (1,), "Netflix": (2,)})
        self.assertEqual(found, 1)
        self.assertEqual(set(await logos.known(["AMC", "Netflix"])), {"Netflix"})

    async def test_without_a_tmdb_credential_nothing_is_asked(self):
        with patch.object(logos, "look_up", AsyncMock()) as asked:
            self.assertEqual(await logos.warm(SimpleNamespace(tmdb_configured=False), {"AMC": (1,)}), 0)
        asked.assert_not_awaited()

    async def test_a_page_warms_only_what_is_due_and_behind_the_response(self):
        await _put("Held")
        started = []
        with patch.object(logos, "warm_later", lambda settings, roster: started.append(roster)):
            found = await logos.for_page(CFG, [("Held", 1), ("Held", 2), ("New", 3),
                                               ("NoId", None), (None, 4), ("", 5)])
        self.assertEqual(set(found), {"Held"})
        self.assertEqual(started, [{"New": (3,)}])

    async def test_a_page_offers_a_few_shows_per_network_and_no_more(self):
        started = []
        with patch.object(logos, "warm_later", lambda settings, roster: started.append(roster)):
            await logos.for_page(CFG, [("AMC", n) for n in (5, 5, 6, 7, 8, 9)])
        self.assertEqual(started, [{"AMC": (5, 6, 7)}])
        self.assertEqual(logos.TITLES_TRIED, 3)

    async def test_a_page_with_nothing_due_starts_nothing(self):
        await _put("Held")
        with patch.object(logos, "warm_later") as started:
            await logos.for_page(CFG, [("Held", 1)])
        started.assert_not_called()

    async def test_two_pages_share_one_warm_and_the_memo_is_gone_when_it_ends(self):
        """THE ONLY STATE THIS MODULE KEEPS IN PROCESS: the task of the warm in
        flight. Prod runs for weeks, so it must not outlive its work."""
        release = asyncio.Event()
        runs = []

        async def warm(settings, roster):
            runs.append(roster)
            await release.wait()
            return 0

        with patch.object(logos, "warm", warm):
            logos.warm_later(CFG, {"A": (1,)})
            logos.warm_later(CFG, {"B": (2,)})
            await asyncio.sleep(0)
            self.assertEqual(runs, [{"A": (1,)}])
            self.assertIsNotNone(logos._warming)
            release.set()
            await logos._warming
            await asyncio.sleep(0)
        self.assertIsNone(logos._warming)
        with patch.object(logos, "warm", AsyncMock(return_value=0)) as again:
            logos.warm_later(CFG, {"C": (3,)})
            await logos._warming
        again.assert_awaited_once()

    async def test_a_warm_that_raises_still_clears_the_memo(self):
        with patch.object(logos, "warm", AsyncMock(side_effect=RuntimeError("down"))):
            logos.warm_later(CFG, {"A": (1,)})
            await asyncio.wait({logos._warming})
        self.assertIsNone(logos._warming)


class ARecheckKeepsWhatItHasUntilSomethingBetterExistsTests(DatabaseTestCase):

    async def _recheck(self, resolution):
        with patch.object(logos, "look_up", AsyncMock(return_value=resolution)):
            return await logos.recheck(CFG, "AMC", (1,))

    async def test_a_new_logo_replaces_the_old(self):
        await _put("AMC", logo_path="/old.png", fetched_at=1_000, checked_at=1_000)
        got = await self._recheck(logos.Resolution(logos.Kind.FOUND, 2, "/new.png", True))
        self.assertEqual(got, logos.Recheck.FOUND)
        row = await _row("AMC")
        self.assertEqual((row["logo_path"], row["light"]), ("/new.png", 1))
        self.assertGreater(row["fetched_at"], 1_000)

    async def test_no_logo_this_time_keeps_the_one_it_had(self):
        await _put("AMC", logo_path="/old.png")
        got = await self._recheck(logos.Resolution(logos.Kind.MISSING, 2))
        self.assertEqual(got, logos.Recheck.KEPT)
        self.assertEqual((await _row("AMC"))["logo_path"], "/old.png")

    async def test_no_logo_and_none_held_is_recorded(self):
        got = await self._recheck(logos.Resolution(logos.Kind.MISSING, 2))
        self.assertEqual(got, logos.Recheck.MISSING)
        self.assertEqual((await _row("AMC"))["missing"], 1)

    async def test_a_tmdb_that_could_not_be_asked_changes_nothing(self):
        await _put("AMC", logo_path="/old.png", fail_count=0)
        got = await self._recheck(logos.Resolution(logos.Kind.UNANSWERED))
        self.assertEqual(got, logos.Recheck.UNANSWERED)
        row = await _row("AMC")
        self.assertEqual((row["logo_path"], row["fail_count"]), ("/old.png", 0))


class TheCeilingSweepTests(DatabaseTestCase):

    async def test_rows_past_the_ceiling_go_and_the_rest_stay(self):
        past = db.now() - tmdb_transport.MAX_CACHE_SECONDS - 5
        await _put("Old", fetched_at=past)
        await _put("OldMissing", logo_path="", missing=1, fetched_at=past)
        await _put("New")
        await logos.record("OnlyFailed", logos.Resolution(logos.Kind.UNANSWERED))
        self.assertEqual(await logos.sweep(), 2)
        self.assertIsNone(await _row("Old"))
        self.assertIsNone(await _row("OldMissing"))
        self.assertIsNotNone(await _row("New"))
        self.assertIsNotNone(await _row("OnlyFailed"))

    async def test_a_row_is_swept_at_the_ceiling_and_not_before(self):
        now = db.now()
        await _put("AMC", fetched_at=now - tmdb_transport.MAX_CACHE_SECONDS + 10)
        self.assertEqual(await logos.sweep(now), 0)
        self.assertEqual(await logos.sweep(now + 10), 1)


class TheLegacyTilesAreRemovedTests(DatabaseTestCase):

    async def test_the_old_directory_goes_and_a_second_start_is_a_no_op(self):
        directory = Path(tempfile.mkdtemp(prefix="logos-")) / "logos"
        directory.mkdir()
        (directory / "amc.png").write_bytes(b"tile")
        (directory / "tiny.none").write_text("")
        with patch.object(logos, "LEGACY_TILE_DIR", directory):
            logos.discard_legacy_tiles()
            self.assertFalse(directory.exists())
            logos.discard_legacy_tiles()


class OnlyTheDiscordDownloadCompositesTests(DatabaseTestCase):

    def plate(self, tile_bytes):
        tile = Image.open(BytesIO(tile_bytes)).convert("RGBA")
        self.assertEqual(tile.size, (logos.TILE, logos.TILE))
        # A point inside the rounded corner's straight edge is plate.
        return tile.getpixel((logos.TILE // 2, 3))

    async def test_a_dark_logo_sits_on_white_and_a_light_one_on_dark(self):
        for colour, plate in ((DARK, logos._WHITE_PLATE), (LIGHT, logos._DARK_PLATE)):
            with self.subTest(plate=plate):
                self.assertEqual(self.plate(logos.render_tile(colour)), plate)

    async def test_the_logo_is_trimmed_and_fitted_inside_the_padding(self):
        tile = Image.open(BytesIO(logos.render_tile(_png((200, 0, 0, 255), size=(1000, 300)))))
        tile = tile.convert("RGBA")
        drawn = [x for x in range(logos.TILE)
                 if tile.getpixel((x, logos.TILE // 2))[:3] == (200, 0, 0)]
        self.assertEqual((min(drawn), max(drawn) + 1), (logos.PAD, logos.TILE - logos.PAD))

    async def test_bytes_that_are_not_an_image_are_no_tile(self):
        self.assertIsNone(logos.render_tile(b"<svg>not a raster</svg>"))

    async def test_a_download_is_made_from_the_recorded_logo_and_kept_nowhere(self):
        await _put("AMC", logo_path="/amc.png")
        stub = TMDBStub(self, proxied=DARK)
        png = await logos.download_tile("AMC")
        self.assertEqual(self.plate(png), logos._WHITE_PLATE)
        self.assertIn("w=300&h=300", stub.fetched[0])

    async def test_a_network_with_no_logo_has_nothing_to_download(self):
        await _put("Tiny", logo_path="", missing=1)
        TMDBStub(self, proxied=DARK)
        self.assertIsNone(await logos.download_tile("Tiny"))
        self.assertIsNone(await logos.download_tile("Unknown"))

    async def test_the_composite_runs_off_the_loop_thread(self):
        seen = []
        real = logos.render_tile

        def render(raw):
            seen.append(threading.get_ident())
            return real(raw)

        await _put("AMC")
        TMDBStub(self, proxied=DARK)
        with patch.object(logos, "render_tile", render):
            await logos.download_tile("AMC")
        self.assertNotEqual(seen, [threading.get_ident()])
        self.assertEqual(len(seen), 1)
