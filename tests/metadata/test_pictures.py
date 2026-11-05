"""app/metadata/pictures.py and its route — a picture that will not load says so,
and its title is described again.

THE PROMISES:
  - A REPORT IS A SIGNED, SHORT-LIVED TOKEN: one this process did not sign, one
    edited, and one past its life are all refused, and refused costs nothing.
  - THE ANSWER IS AT ONCE AND IS THE SERVER'S: the next picture the registry
    holds in the viewer's order, itself reporting, or the placeholder.
  - THE CONFIRMATION READS THE ADDRESS BACK FROM THE REGISTRY and HEADs it: dead
    caps it and heals the whole title at the front of the queue; transient notes
    when and changes nothing else; alive changes nothing; an address replaced
    since the page was drawn is not checked. Once per address per few minutes,
    a bounded number a minute, one heal per title per few hours.
  - THE ROUTE IS PUBLIC, AND ONLY AN IMAGE MAY ASK.
  - A SHARE PAGE NEVER REPORTS; a viewer's own card does.
"""
from __future__ import annotations

import asyncio
import base64
from types import SimpleNamespace
from unittest import mock
from urllib.parse import parse_qs, urlsplit

import os
from contextlib import contextmanager

from cryptography.fernet import Fernet

from app import db, secrets_box, signed_tokens
from app.media import artwork
from app.metadata import pictures, refresh
from app.providers.base import proxied_image, proxied_origin
from tests.support import AppTestCase, DatabaseTestCase, ORIGIN

SETTINGS = SimpleNamespace(public_base_url=ORIGIN, timezone="UTC")
KEY = Fernet.generate_key().decode()


@contextmanager
def _encryption_key(key: str | None):
    """ENCRYPTION_KEY set (or not) for the length of the block, re-read by
    secrets_box, and put back as it was."""
    before = os.environ.get(secrets_box.ENV_VAR)
    if key is None:
        os.environ.pop(secrets_box.ENV_VAR, None)
    else:
        os.environ[secrets_box.ENV_VAR] = key
    secrets_box.reset_cache()
    try:
        yield
    finally:
        if before is None:
            os.environ.pop(secrets_box.ENV_VAR, None)
        else:
            os.environ[secrets_box.ENV_VAR] = before
        secrets_box.reset_cache()
TITLE = pictures.Title("show", 500, "", 2)
ORDER = ("trakt", "simkl")
DEAD = "https://media.trakt.tv/images/shows/500/posters/dead.jpg"
LIVE = "https://simkl.in/posters/live.jpg"


def _report(source="trakt", url=DEAD, *, season=None, at=None, tried=()) -> pictures.Report:
    return pictures.Report(TITLE, source, season, pictures.digest(url), ORDER,
                           db.now() if at is None else at,
                           (*map(pictures.digest, tried), pictures.digest(url)))


def _raw(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _text(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _token_of(src: str) -> str:
    default = parse_qs(urlsplit(src).query).get("default", [""])[0]
    return parse_qs(urlsplit(default).query).get("t", [""])[0]


class _Head:
    """The confirmation's pool, answering every HEAD with one response."""

    def __init__(self, status=404, kind="text/html", raises=None):
        self.status, self.kind, self.raises = status, kind, raises
        self.asked: list[str] = []

    def client(self):
        return self

    async def head(self, url, follow_redirects=False):
        self.asked.append(url)
        if self.raises is not None:
            raise self.raises
        return SimpleNamespace(status_code=self.status, headers={"content-type": self.kind})


class TheTokenTests(DatabaseTestCase):

    def test_a_token_reads_back_as_the_report_it_was(self):
        report = _report()
        self.assertEqual(pictures.read(pictures.token(report), now=report.issued_at), report)

    def test_an_edited_token_is_refused(self):
        """Every byte is signed — the service's code, the digest, the clock, the
        signature itself: one bit changed anywhere is refused."""
        raw = _raw(pictures.token(_report()))
        for at in range(len(raw)):
            with self.subTest(at=at):
                edited = bytearray(raw)
                edited[at] ^= 0x01
                self.assertIsNone(pictures.read(_text(bytes(edited)), now=db.now()))

    def test_a_token_is_a_few_dozen_characters(self):
        """The point of the binary layout: every picture a signed-in page draws
        carries one, and the JSON one spelled out each service, the order and
        every digest — ~190 characters."""
        self.assertLessEqual(len(pictures.token(_report())), 64)
        self.assertRegex(pictures.token(_report()), r"^[A-Za-z0-9_-]+$")

    def test_every_title_shape_reads_back(self):
        for title, season in ((pictures.Title("movie", 603, "", None), None),
                              (pictures.Title("show", None, "show:simkl:12345", 0), 0),
                              (pictures.Title("show", None, "show:tvdb:9", -1), 2_000),
                              (pictures.Title("show", 10 ** 9, "", 2026), None)):
            with self.subTest(title=title):
                report = pictures.Report(title, "simkl", season, pictures.digest(LIVE),
                                         ("tmdb", "trakt", "simkl"), db.now(),
                                         (pictures.digest(DEAD), pictures.digest(LIVE)))
                self.assertEqual(pictures.read(pictures.token(report), now=report.issued_at),
                                 report)

    def test_every_registered_service_has_a_token_code(self):
        from app.providers.base import Source
        self.assertEqual(set(pictures._SOURCE_CODES), {str(source) for source in Source})
        self.assertEqual(len(set(pictures._SOURCE_CODES.values())), len(pictures._SOURCE_CODES))

    def test_a_service_with_no_code_draws_the_static_chain(self):
        choices = [artwork.Choice("trakt", DEAD, None), artwork.Choice("simkl", LIVE, None)]
        with mock.patch.dict(pictures._SOURCE_CODES, clear=True):
            self.assertEqual(pictures.drawn(SETTINGS, TITLE, choices, ORDER),
                             proxied_image(DEAD, fallbacks=[LIVE]))

    def test_a_signed_token_this_layout_does_not_read_is_refused(self):
        """Signed with the right key, and still refused: another layout's
        version, a code no service has, bytes left over, a length past the end.
        A token in the old JSON layout reads as rubbish."""
        payload = _raw(pictures.token(_report()))[:-signed_tokens.SIGNATURE_BYTES]
        for label, bad in (("version", bytes([2]) + payload[1:]),
                           ("trailing byte", payload + b"\x00"),
                           ("cut short", payload[:-3]),
                           ("unknown medium", payload[:1] + payload[1:6] + b"\x09" + payload[7:])):
            with self.subTest(label):
                self.assertIsNone(pictures.read(_text(bad + signed_tokens.sign(pictures._PURPOSE, bad)), now=db.now()))
        self.assertIsNone(pictures.read(
            "WyJzaG93Iiw1MDAsIiIsMixudWxsXQ.c2lnbmF0dXJlc2lnbmF0dQ", now=db.now()))

    def test_with_no_encryption_key_another_processes_token_is_refused(self):
        with _encryption_key(None):
            text = pictures.token(_report())
            with mock.patch.object(signed_tokens, "_PROCESS_KEY", b"another process" * 2):
                self.assertIsNone(pictures.read(text, now=db.now()))

    def test_with_an_encryption_key_a_token_survives_a_restart(self):
        with _encryption_key(KEY):
            text = pictures.token(_report())
            with mock.patch.object(signed_tokens, "_PROCESS_KEY", b"another process" * 2):
                self.assertIsNotNone(pictures.read(text, now=db.now()))

    def test_a_token_signed_under_another_encryption_key_is_refused(self):
        with _encryption_key(KEY):
            text = pictures.token(_report())
        with _encryption_key(Fernet.generate_key().decode()):
            self.assertIsNone(pictures.read(text, now=db.now()))

    def test_a_token_past_its_life_is_refused(self):
        report = _report(at=1000)
        self.assertIsNone(pictures.read(pictures.token(report),
                                        now=1000 + pictures.TOKEN_SECONDS + 1))

    def test_one_token_has_one_spelling(self):
        """A character outside the alphabet is not skipped over: the same bytes
        spelled with a stray character are refused, not read."""
        text = pictures.token(_report())
        for spelled in (text[:5] + "!" + text[5:], text[:5] + "+" + text[5:], text + "="):
            with self.subTest(spelled=spelled):
                self.assertIsNone(pictures.read(spelled, now=db.now()))

    def test_rubbish_is_refused(self):
        for text in ("", "nodot", "a.b", "....", "é.é", "A" * 2000):
            with self.subTest(text=text):
                self.assertIsNone(pictures.read(text, now=db.now()))


class WhatAPageDrawsTests(DatabaseTestCase):

    def test_the_first_picture_with_this_apps_report_address_behind_it(self):
        choices = [artwork.Choice("trakt", DEAD, 2), artwork.Choice("simkl", LIVE, None)]
        src = pictures.drawn(SETTINGS, TITLE, choices, ORDER)
        self.assertEqual(proxied_origin(src), DEAD)
        default = parse_qs(urlsplit(src).query)["default"][0]
        self.assertTrue(default.startswith(f"{ORIGIN}{pictures.ROUTE}?t="))
        report = pictures.read(_token_of(src), now=db.now())
        self.assertEqual((report.title, report.source, report.season, report.order),
                         (TITLE, "trakt", 2, ORDER))

    def test_with_no_public_address_the_static_fallbacks_instead(self):
        choices = [artwork.Choice("trakt", DEAD, None), artwork.Choice("simkl", LIVE, None)]
        self.assertEqual(
            pictures.drawn(SimpleNamespace(public_base_url=""), TITLE, choices, ORDER),
            proxied_image(DEAD, fallbacks=[LIVE]))

    def test_no_picture_is_nothing(self):
        self.assertEqual(pictures.drawn(SETTINGS, TITLE, [], ORDER), "")


class TheAnswerTests(DatabaseTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        await artwork.record_poster_url("show", 500, "trakt", DEAD)
        await artwork.record_poster_url("show", 500, "simkl", LIVE)

    async def test_the_next_picture_in_the_viewers_order_and_it_reports_too(self):
        location, report = await pictures.answer(SETTINGS, pictures.token(_report()))
        self.assertEqual(proxied_origin(location), LIVE)
        self.assertEqual(report.source, "trakt")
        onward = pictures.read(_token_of(location), now=db.now())
        self.assertEqual((onward.source, onward.digest), ("simkl", pictures.digest(LIVE)))

    async def test_nothing_left_is_the_placeholder(self):
        report = _report("simkl", LIVE, tried=[DEAD])
        location, _ = await pictures.answer(SETTINGS, pictures.token(report))
        self.assertEqual(location, pictures.PLACEHOLDER)

    async def test_a_chain_never_goes_back_to_a_picture_it_tried(self):
        """Two dead pictures in a row: the second's report carries the first, so
        neither is offered again, and the browser gets the placeholder rather
        than being sent back and forth between them."""
        location, _ = await pictures.answer(SETTINGS, pictures.token(_report()))
        onward = pictures.read(_token_of(location), now=db.now())
        self.assertEqual(set(onward.tried), {pictures.digest(DEAD), pictures.digest(LIVE)})
        second, _ = await pictures.answer(SETTINGS, _token_of(location))
        self.assertEqual(second, pictures.PLACEHOLDER)

    async def test_a_better_picture_filed_since_the_page_was_drawn_is_offered(self):
        """A description behind the page filed a picture that ranks ahead of the
        one that failed: it is untried, so it comes next."""
        await artwork.record_poster_url("show", 500, "tmdb", "https://image.tmdb.org/p.jpg")
        report = _report()._replace(order=("tmdb", "trakt", "simkl"))
        location, _ = await pictures.answer(SETTINGS, pictures.token(report))
        self.assertEqual(proxied_origin(location), "https://image.tmdb.org/p.jpg")

    async def test_a_refused_token_is_the_placeholder_and_nothing_to_confirm(self):
        self.assertEqual(await pictures.answer(SETTINGS, "forged.token"),
                         (pictures.PLACEHOLDER, None))


class TheConfirmationTests(DatabaseTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        await artwork.record_poster_url("show", 500, "trakt", DEAD)
        await artwork.record_poster_url("show", 500, "simkl", LIVE)
        self.healed: list = []

        async def _heal(settings, report):
            self.healed.append(report.title.key)
            return []
        patch = mock.patch.object(pictures, "heal", _heal)
        patch.start()
        self.addCleanup(patch.stop)

    async def confirm(self, head: _Head, report=None) -> str:
        with mock.patch.object(pictures, "POOL", head):
            return await pictures.confirm(SETTINGS, report or _report())

    async def trakt_row(self):
        return await db.fetch_one(
            "SELECT fail_count, last_failed_at FROM show_posters WHERE source = 'trakt'")

    async def test_a_404_is_dead_capped_and_healed(self):
        head = _Head(404)
        self.assertEqual(await self.confirm(head), "dead")
        self.assertEqual(head.asked, [DEAD], "the registry's address, and only it")
        row = await self.trakt_row()
        self.assertEqual(row["fail_count"], artwork.MAX_FAIL_COUNT)
        self.assertIsNotNone(row["last_failed_at"])
        self.assertEqual(self.healed, ["show:tmdb:500"])
        self.assertEqual(await artwork.urls_for("show", 500), {"simkl": LIVE})

    async def test_a_page_where_a_picture_was_is_dead(self):
        self.assertEqual(await self.confirm(_Head(200, "text/html; charset=utf-8")), "dead")

    async def test_a_server_error_or_a_refusal_is_transient_and_heals_nothing(self):
        for head in (_Head(503), _Head(403), _Head(raises=pictures.httpx.ConnectTimeout("t"))):
            with self.subTest(status=head.status):
                pictures._checked.clear()
                self.assertEqual(await self.confirm(head), "transient")
        row = await self.trakt_row()
        self.assertEqual(row["fail_count"], 0)
        self.assertIsNotNone(row["last_failed_at"])
        self.assertEqual(self.healed, [])

    async def test_a_picture_that_loads_changes_nothing(self):
        self.assertEqual(await self.confirm(_Head(200, "image/jpeg")), "alive")
        self.assertEqual((await self.trakt_row())["fail_count"], 0)

    async def test_an_address_replaced_since_the_page_was_drawn_is_not_checked(self):
        await artwork.record_poster_url("show", 500, "trakt", "https://media.trakt.tv/new.jpg")
        head = _Head(404)
        self.assertEqual(await self.confirm(head), "replaced")
        self.assertEqual(head.asked, [])

    async def test_one_address_is_checked_once_however_many_report_it(self):
        head = _Head(404)
        self.assertEqual(await self.confirm(head), "dead")
        self.assertEqual(await self.confirm(head), "repeat")
        self.assertEqual(len(head.asked), 1)

    async def test_a_minutes_checks_are_bounded(self):
        with mock.patch.object(pictures, "CONFIRMS_PER_MINUTE", 1):
            await self.confirm(_Head(200, "image/jpeg"))
            other = _report("simkl", LIVE)
            self.assertEqual(await self.confirm(_Head(200, "image/jpeg"), other), "limited")


class TheHealTests(DatabaseTestCase):

    async def test_every_service_and_the_season_at_the_front_once_per_title(self):
        queued, ran = [], []

        async def _queue_title(settings, media, ids, seasons=(), *, reason, title_key=""):
            queued.append((media, dict(ids), list(seasons), reason, title_key))
            return [refresh.Entry("tmdb", media, "500", refresh.TITLE)]

        async def _run(settings):
            ran.append(True)
            return 1

        with mock.patch.object(refresh, "queue_title", _queue_title), \
                mock.patch.object(refresh, "run", _run):
            await pictures.heal(SETTINGS, _report())
            await pictures.heal(SETTINGS, _report())
        self.assertEqual(queued, [("show", {"tmdb": 500}, [2], "dead picture", "show:tmdb:500")])
        self.assertEqual(ran, [True])

    async def test_what_it_remembers_shrinks_once_its_clock_has_passed(self):
        """The process runs for weeks between restarts, so a title healed once
        must not be held for all of them: the next heal prunes every entry older
        than HEAL_EVERY_SECONDS — and that title may be healed again."""
        async def _queue_title(*_args, **_kwargs):
            return []

        clock = [1000.0]
        with mock.patch.object(refresh, "queue_title", _queue_title), \
                mock.patch.object(pictures.time, "monotonic", lambda: clock[0]):
            for tmdb in (501, 502, 503):
                await pictures.heal(SETTINGS, _report()._replace(
                    title=pictures.Title("show", tmdb, "", None)))
            self.assertEqual(len(pictures._healed), 3)
            clock[0] += pictures.HEAL_EVERY_SECONDS
            await pictures.heal(SETTINGS, _report())
        self.assertEqual(list(pictures._healed), ["show:tmdb:500"])


class TheRouteTests(AppTestCase):

    def setUp(self):
        super().setUp()
        # An instance that has been set up, and nobody signed in.
        self.make_user("owner", is_admin=True)
        asyncio.run(artwork.record_poster_url("show", 500, "trakt", DEAD))
        asyncio.run(artwork.record_poster_url("show", 500, "simkl", LIVE))

    def get(self, token: str, dest: str | None = "image"):
        headers = {"Sec-Fetch-Dest": dest} if dest else {}
        return self.client.get(f"{pictures.ROUTE}?t={token}", headers=headers,
                               follow_redirects=False)

    def test_it_answers_a_stranger_at_once_with_the_next_picture(self):
        started = []
        with mock.patch.object(refresh, "start", lambda work: started.append(work)):
            response = self.get(pictures.token(_report()))
        for work in started:
            work.close()
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertEqual(proxied_origin(response.headers["location"]), LIVE)
        self.assertEqual(len(started), 1, "the confirmation runs behind the answer")

    def test_only_an_image_may_ask(self):
        for dest in ("document", "empty", None):
            with self.subTest(dest=dest):
                self.assertEqual(self.get(pictures.token(_report()), dest).status_code, 400)

    def test_a_refused_token_is_the_placeholder_and_starts_nothing(self):
        with mock.patch.object(refresh, "start", side_effect=AssertionError("started")):
            response = self.get("forged.token")
        self.assertEqual((response.status_code, response.headers["location"]),
                         (302, pictures.PLACEHOLDER))
