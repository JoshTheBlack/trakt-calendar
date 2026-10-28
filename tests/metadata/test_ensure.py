"""app/metadata's one way in: `ensure` registers a title and asks each service it
has no id from, once, by shared id — the one place the app does.

WHAT IS PINNED: a service the title already has an id from is not asked; nothing
is asked without a viewer's settings; an answer that names none of the asked
service's own ids is no answer; a service that could not be asked is reported,
never remembered as "not listed"; asking again within the day costs no request,
because each port keeps its own day-long clock; and an answer that teaches a
higher id re-keys the title, moving the rows already filed under the old key.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import httpx

from app import db, metadata
from app.config import Settings
from app.providers.base import Media, SourceUnavailable
from tests.support import DatabaseTestCase

TMDB_TOKEN = "eyJhbGciOiJIUzI1NiJ9.eyJhdWQiOiJ0ZXN0In0.c2lnbmF0dXJl"
# Every service askable by shared id: Trakt and TMDB on the instance's own
# credentials, Simkl on a viewer's token (providers.for_shared_id_lookup).
EVERY = Settings(trakt_client_id="cid", simkl_client_id="scid",
                 simkl_access_token="viewer-token", tmdb_api_key=TMDB_TOKEN)
TRAKT_ONLY = Settings(trakt_client_id="cid")


def _ports(trakt=None, simkl=None, tmdb=None):
    """Each service's lookup by shared id stood in for — {} ("not listed") unless
    a test says what it answers."""
    return (
        patch("app.providers.trakt.detail.find_by_shared_id", trakt or AsyncMock(return_value={})),
        patch("app.providers.simkl.by_id.find_by_shared_id", simkl or AsyncMock(return_value={})),
        patch("app.providers.tmdb.search.find_by_shared_id", tmdb or AsyncMock(return_value={})),
    )


class EnsureAsksEachMissingServiceOnceTests(DatabaseTestCase):
    async def test_every_service_the_title_has_no_id_from_is_asked_once(self):
        trakt = AsyncMock(return_value={"trakt": 111, "trakt_slug": "a-show", "tmdb": 900})
        simkl = AsyncMock(return_value={"simkl": 222, "simkl_slug": "a-show"})
        tmdb = AsyncMock(return_value={})
        a, b, c = _ports(trakt, simkl, tmdb)
        with a, b, c:
            found = await metadata.ensure(Media.SHOW, {"tmdb": 900}, settings=EVERY)
        self.assertEqual(found.key, "show:tmdb:900")
        self.assertEqual((found.ids["trakt"], found.ids["simkl"]), (111, 222))
        trakt.assert_awaited_once()
        simkl.assert_awaited_once()
        tmdb.assert_not_awaited()  # it has TMDB's id already
        self.assertEqual(trakt.await_args.args[2], {"tmdb": 900})

    async def test_what_was_learned_is_kept_for_the_next_reader(self):
        trakt = AsyncMock(return_value={"trakt": 111})
        a, b, c = _ports(trakt)
        with a, b, c:
            await metadata.ensure(Media.SHOW, {"tmdb": 900}, settings=EVERY)
            again = await metadata.ensure(Media.SHOW, {"tmdb": 900})
        self.assertEqual(again.ids["trakt"], 111)
        trakt.assert_awaited_once()

    async def test_without_a_viewers_settings_nothing_is_asked(self):
        trakt = AsyncMock(return_value={"trakt": 111})
        a, b, c = _ports(trakt)
        with a, b, c:
            found = await metadata.ensure(Media.SHOW, {"tmdb": 900})
        self.assertNotIn("trakt", found.ids)
        trakt.assert_not_awaited()

    async def test_an_answer_naming_none_of_that_services_own_ids_is_no_answer(self):
        """A wrong service id is worse than a missing one: it is never
        overwritten, and it shadows a right one from another service."""
        a, b, c = _ports(trakt=AsyncMock(return_value={"imdb": "tt-other"}))
        with a, b, c:
            found = await metadata.ensure(Media.SHOW, {"tmdb": 900}, settings=EVERY)
        self.assertEqual(found.ids, {"tmdb": 900})

    async def test_a_service_that_could_not_be_asked_is_reported_and_not_remembered(self):
        a, b, c = _ports(trakt=AsyncMock(side_effect=SourceUnavailable("down")))
        with a, b, c:
            answer = await metadata.ask(Media.SHOW, {"tmdb": 900}, settings=EVERY)
        self.assertEqual(answer.unreachable, frozenset({"trakt"}))
        self.assertEqual(answer.identity.ids, {"tmdb": 900})

    async def test_asking_narrows_who_is_asked_to_the_services_it_names(self):
        """A caller spending one service's allowance (app/calendar/completion.py)
        asks that service alone; the others are not spent uncounted."""
        trakt = AsyncMock(return_value={"trakt": 111})
        simkl = AsyncMock(return_value={"simkl": 222})
        a, b, c = _ports(trakt, simkl)
        with a, b, c:
            answer = await metadata.ask(Media.SHOW, {"tmdb": 900}, settings=EVERY,
                                        asking=("trakt",))
        self.assertEqual(answer.identity.ids, {"tmdb": 900, "trakt": 111})
        trakt.assert_awaited_once()
        simkl.assert_not_awaited()


class EachPortKeepsItsOwnDayLongClockTests(DatabaseTestCase):
    async def test_a_not_listed_answer_costs_no_second_request_within_the_day(self):
        """Through Trakt's real lookup, with only its HTTP stood in for: the
        second ensure asks the port again — the title still has no Trakt id —
        and the port answers from its own cache."""
        sent = []

        async def send(client, method, url, **kwargs):
            sent.append(url)
            return httpx.Response(200, json=[])

        with patch("app.providers.trakt.transport.send_or_unavailable", send):
            await metadata.ensure(Media.SHOW, {"tmdb": 900}, settings=TRAKT_ONLY)
            await metadata.ensure(Media.SHOW, {"tmdb": 900}, settings=TRAKT_ONLY)
        self.assertEqual(len(sent), 1)
        self.assertIn("search/tmdb/900", sent[0])


class AnAnswerThatTeachesAHigherIdTests(DatabaseTestCase):
    async def test_the_title_is_re_keyed_and_its_rows_move_with_it(self):
        now = db.now()
        user_id = (await db.execute(
            "INSERT INTO users (username, created_at, updated_at) VALUES ('v', ?, ?)",
            (now, now))).lastrowid
        await db.execute(
            "INSERT INTO distrakt_user_seasons (user_id, media, match_source, match_id, "
            "season, kind, created_at) VALUES (?, 'show', 'tvdb', '4321', 1, 'keepup', ?)",
            (user_id, now))
        tmdb = AsyncMock(return_value={"tmdb": 900, "tvdb": 4321})
        a, b, c = _ports(tmdb=tmdb)
        with a, b, c:
            found = await metadata.ensure(Media.SHOW, {"tvdb": 4321}, settings=EVERY,
                                          filed_under="show:tvdb:4321")
        self.assertEqual(found.key, "show:tmdb:900")
        row = await db.fetch_one("SELECT match_source, match_id, tmdb FROM distrakt_user_seasons")
        self.assertEqual(tuple(row), ("tmdb", "900", 900))
        self.assertEqual(await db.fetch_value(
            "SELECT title_key FROM title_aliases WHERE old_key = 'show:tvdb:4321'"),
            "show:tmdb:900")
