"""The decoupling guarantee, and the file is named for it.

THE CLAIM UNDER TEST: an account that has only ever been granted the rankings
feature — no other approval, no linked account, no data anywhere else in this
app — can build and rank a board from nothing but a search. Everything else this
feature can do is a convenience layered on top, and none of it is allowed to
become a prerequisite.

That claim is defended three ways here, because each catches a different way of
breaking it:
  - END TO END. The journey actually runs for such an account.
  - BY ABSENCE. What that account cannot use is missing rather than refused, and
    the route answers 404 rather than explaining what they are missing.
  - BY INSPECTION. The modules that make up this feature do not import the other
    one at all, in the style of the "only db.py imports sqlite3" test. A
    behavioural test can be satisfied by an import that merely happens not to
    run today; this cannot.

The credential rules are here too, because they are the pair most easily got
backwards: a SEARCH asks with the viewer's own settings, which for this account
carry no token at all — Trakt answers a catalogue search on the instance's client
id alone, so the search still works, and the operator's token is never borrowed —
while a RATINGS read must use the caller's own and hide itself entirely when
there is none.

No network. Every provider call is a stand-in, and the tests that matter assert
the real provider functions were never reached.
"""
from __future__ import annotations

import asyncio
import json
import re
import unittest
from html import unescape
from unittest import mock

from fastapi.testclient import TestClient

from app import auth, db
from app.auth import trakt_routes
from app.providers.trakt import detail as trakt_detail, sync as trakt_sync
from app.config import Settings
from tests.support import save_settings
from app.main import app
from app.media import posters
from app.ranker import core as ranker
from app.ranker import routes as ranker_routes, sources as ranker_sources
from app.providers import title_search
from app.providers.base import SearchPage, Source
from app.providers.tmdb import search as tmdb_search
from app.ranker.sources import Media, RatedTitle, TitleRef
from tests.ranker.test_search import reference
from tests.support import APP_DIR, ORIGIN, migrated_db


OPERATOR_TOKEN = "operator-instance-token"
USER_TOKEN = "this-users-own-token"

def result_rows(html: str) -> list[dict]:
    """The results a search fragment drew, each as the reference it posts."""
    return [reference(attrs) for attrs in
            re.findall(r'<(?:div|summary) class="search-row ranker-result"([^>]*)>', html)]


def result_titles(html: str) -> list[str]:
    return [t.strip() for t in re.findall(r'<span class="search-title">([^<]*)', html)]


TMDB_TOKEN = "eyJhbGciOiJIUzI1NiJ9.eyJhdWQiOiJ0ZXN0In0.c2lnbmF0dXJl"


def canned(*hits):
    """The services' fan-out, answering `hits` for a matching medium and
    recording each call — the seam the board's search goes through
    (providers/title_search.py), so no provider function runs."""
    calls = []

    async def _search(asked, settings, media, query, *, pages=1, cached_only=False):
        calls.append((query, media, cached_only))
        return title_search.SearchMergeResult(
            hits=[hit for hit in hits if hit.media == media], failed=frozenset())
    return _search, calls


def a_hit(title_ref: "TitleRef") -> title_search.MergedSearchHit:
    return title_search.MergedSearchHit(
        key=None, season=None, source_ids={Source.TRAKT: "1"}, ids=dict(title_ref.ids),
        title=title_ref.title, year=title_ref.year, network=title_ref.network,
        runtime=title_ref.runtime, overview="", media=title_ref.media)


class FakeRatingsSource:
    def __init__(self, rated: list[RatedTitle]):
        self.rated = rated
        self.calls: list[int] = []

    async def fetch_ratings(self, user_id: int) -> list[RatedTitle]:
        self.calls.append(user_id)
        return self.rated


def a_show(tmdb: int, title: str) -> TitleRef:
    return TitleRef(media=Media.SHOW, title=title, ids={"trakt": tmdb * 2, "tmdb": tmdb},
                    year=2026, network="HBO")


def a_movie(tmdb: int, title: str) -> TitleRef:
    return TitleRef(media=Media.MOVIE, title=title, ids={"tmdb": tmdb}, year=2026, runtime=101)


def exploding(reason: str):
    """A stand-in that fails the test if it is ever called."""
    async def _call(*args, **kwargs):
        raise AssertionError(reason)
    return _call


class StandaloneTestCase(unittest.TestCase):
    """A single account with the rankings grant and nothing else at all."""
    def setUp(self):
        migrated_db("standalone")
        # A configured instance credential, which is what a search is supposed
        # to use. Nothing in this file links an account to anything.
        save_settings(Settings(trakt_client_id="instance-client",
                               trakt_access_token=OPERATOR_TOKEN, tmdb_api_key=TMDB_TOKEN))
        self.client = TestClient(app, base_url=ORIGIN, headers={"Origin": ORIGIN})
        self.admin_id = asyncio.run(auth.create_user(
            username="admin_user", password="hunter2hunter2", settings=Settings(),
            is_admin=True))
        self.user_id = asyncio.run(auth.create_user(
            username="just_the_ranker", password="hunter2hunter2", settings=Settings(),
            ranker_approved=True))
        session_id = asyncio.run(auth.create_session(self.user_id))
        self.client.cookies.set(auth.COOKIE_NAME_SECURE, session_id)

    def tearDown(self):
        self.client.close()
        db.close_thread_connection()

    def value(self, sql: str, params=()):
        return asyncio.run(db.fetch_value(sql, params))

    def assertHasNothingElse(self):
        """The premise of every test in this file: no other approval, and no
        data belonging to any other feature."""
        user = asyncio.run(auth.get_user(self.user_id))
        self.assertTrue(user["ranker_approved"])
        self.assertFalse(user["distrakt_approved"])
        self.assertFalse(user["calendar_approved"])
        self.assertEqual(self.value(
            "SELECT COUNT(*) FROM distrakt_month_records WHERE user_id = ?",
            (self.user_id,)), 0)
        self.assertEqual(self.value(
            "SELECT COUNT(*) FROM linked_identities WHERE user_id = ?", (self.user_id,)), 0)


class ExportNameDefaultTests(StandaloneTestCase):
    """The export dialog prefills the name it draws on the image. It should
    offer the account's chosen display name when there is one — that is what
    somebody wants on a poster they are going to share — and fall back to the
    username otherwise."""

    def _prefilled_name(self):
        """What the export dialog's own field is DRAWN with. It used to be handed
        to a script in the page's JSON island and written into the field when the
        dialog opened; the field carries it now, like every other option there."""
        asyncio.run(ranker.create_board(self.user_id, uid="b1", name="Top"))
        body = self.client.get("/rankings").text
        return re.search(r'<input type="text" id="exUser" name="username"[^>]*?'
                         r'value="([^"]*)"', body, re.S).group(1)

    def test_it_falls_back_to_the_username(self):
        self.assertEqual(self._prefilled_name(), "just_the_ranker")

    def test_the_display_name_is_preferred_once_set(self):
        asyncio.run(auth.set_display_name(self.user_id, "Josh Black"))
        self.assertEqual(self._prefilled_name(), "Josh Black")

    # That this is only a DEFAULT and not a lock — the dialog's field stays
    # editable and a one-off name is accepted — is already covered by
    # JourneyTests, which exports with a username of its own choosing.


class JourneyTests(StandaloneTestCase):
    def test_such_an_account_can_search_add_and_rank_end_to_end(self):
        self.assertHasNothingElse()
        self.assertEqual(self.client.get("/rankings").status_code, 200)

        search, calls = canned(a_hit(a_show(1396, "Breaking Bad")),
                               a_hit(a_movie(550, "Fight Club")))
        made = self.client.post("/rankings/boards/new", json={"name": "Top 2026"})
        board = made.headers["HX-Location"].split("board=")[1]
        with mock.patch.object(title_search, "search_catalogue", search):
            found = self.client.get(
                f"/rankings/fragments/search?board={board}&q=brea&media=show").text
            self.assertEqual(result_titles(found), ["Breaking Bad"])
            named, = result_rows(found)
            added = self.client.post(f"/api/rankings/boards/{board}/items", json=named)
        self.assertEqual(added.json()["added"], 1)
        self.assertEqual(added.json()["key"], "show:tmdb:1396")
        # One real search, and the add read that search back rather than asking.
        self.assertEqual([call[2] for call in calls], [False, True])

        tiered = self.client.post(f"/rankings/boards/{board}/tiers/new", json={"label": "S"})
        tier_uid = re.search(r'<details class="ranker-tier"[^>]*data-uid="([^"]+)"',
                             tiered.text).group(1)
        saved = self.client.post(f"/api/rankings/boards/{board}/save", json={
            "version": 2,
            "categories": [{"uid": tier_uid, "label": "S", "rank_priority": 60,
                            "items": ["show:tmdb:1396"]}],
            "pool": [],
        })
        self.assertEqual(saved.status_code, 200, saved.text)

        stored = self.client.get(f"/api/rankings/boards/{board}").json()["board"]
        one_tier, = stored["categories"]
        self.assertEqual([i["title"] for i in one_tier["items"]], ["Breaking Bad"])
        self.assertEqual(stored["pool"], [])

        # And out the other end, which is the half of "end to end" that matters
        # most here: an account with nothing but this feature can take its
        # ranking away as both an image and a text block.
        # The export fetches the one tile it draws; nothing here has a picture,
        # so the title's lookup is answered with none rather than a network call.
        with mock.patch.object(posters, "_look_up_address", mock.AsyncMock(return_value=None)):
            image = self.client.post(f"/api/rankings/boards/{board}/export", json={
                "top_x": 1, "columns": 3, "fmt": "jpeg", "title": "Top Shows",
                "username": "ranker_only"})
        self.assertEqual(image.status_code, 200, image.text)
        self.assertEqual(image.headers["content-type"], "image/jpeg")

        readout = self.client.get(
            f"/rankings/fragments/export?board={board}&top_x=1&columns=3&title=Top+Shows")
        self.assertIn("**Breaking Bad**", readout.text)
        self.assertHasNothingElse()

    def test_a_movie_only_board_works_the_same_way(self):
        search, calls = canned(a_hit(a_movie(550, "Fight Club")))
        asyncio.run(ranker.create_board(self.user_id, uid="m1", media_scope="movie"))
        with mock.patch.object(title_search, "search_catalogue", search):
            found = self.client.get("/rankings/fragments/search?board=m1&q=figh&media=movie").text
            for named in result_rows(found):
                self.client.post("/api/rankings/boards/m1/items", json=named)
        self.assertEqual({call[1] for call in calls}, {Media.MOVIE})
        self.assertEqual(self.value("SELECT media FROM tier_items"), "movie")
        self.assertEqual(self.value("SELECT match_id FROM tier_items"), "550")

    def test_a_title_with_no_shared_id_is_refused_rather_than_stored(self):
        """It could not be told apart from another title of the same name, so
        there is nothing to rank — but it must not take the other results with
        it. Shown, because a season lookup may yet key it; refused on the add
        when nothing does."""
        search, _ = canned(
            a_hit(TitleRef(media=Media.MOVIE, title="Nameless", ids={"slug": "nameless"})),
            a_hit(a_movie(550, "Fight Club")))
        asyncio.run(ranker.create_board(self.user_id, uid="m1", media_scope="movie"))
        with mock.patch.object(title_search, "search_catalogue", search):
            found = self.client.get("/rankings/fragments/search?board=m1&q=na&media=movie").text
            self.assertEqual(result_titles(found), ["Nameless", "Fight Club"])
            nameless, _ = result_rows(found)
            refused = self.client.post("/api/rankings/boards/m1/items", json=nameless)
        self.assertEqual(refused.status_code, 400)
        self.assertIn("no id a board can hold it by", refused.json()["error"])
        self.assertEqual(self.value("SELECT COUNT(*) FROM tier_items"), 0)


class AbsenceTests(StandaloneTestCase):
    def test_neither_optional_source_is_advertised(self):
        sources = asyncio.run(ranker_routes._sources(self.user_id))
        self.assertEqual(sources, {"search": True})

    def test_the_import_route_refuses_and_explains_nothing(self):
        self.client.post("/api/rankings/boards", json={"uid": "b1"})
        resp = self.client.post("/api/rankings/boards/b1/import/tracker", json={"media": "show"})
        self.assertEqual(resp.status_code, 404)
        self.assertEqual(self.value("SELECT COUNT(*) FROM tier_items"), 0)

    def test_the_ratings_seed_refuses_when_there_is_no_linked_account(self):
        self.client.post("/api/rankings/boards", json={"uid": "b1"})
        with mock.patch.object(trakt_sync, "fetch_ratings",
                               exploding("ratings were read without a linked account")):
            resp = self.client.post("/api/rankings/boards/b1/seed/ratings", json={"commit": True})
        self.assertEqual(resp.status_code, 404)
        self.assertEqual(self.value("SELECT COUNT(*) FROM tier_categories"), 0)


class CredentialTests(StandaloneTestCase):
    """The two rules that are easy to get backwards."""

    def test_search_reaches_tmdb_on_the_instance_token_and_never_the_operators(self):
        """This account has linked nothing, so its settings carry no token of its
        own — and TMDB still answers, because every box asks it with the
        instance's read token. What must never happen is the operator's own
        Trakt token standing in for the viewer's, or Trakt being asked for an
        account that never linked it."""
        seen = {}

        async def _search(settings, media, query, *, page=1, cached_only=False):
            seen["trakt_token"] = settings.trakt_access_token
            seen["tmdb"] = settings.tmdb_configured
            return SearchPage([])

        with mock.patch.object(tmdb_search, "search_titles", _search),              mock.patch.object(trakt_detail, "search_titles",
                               exploding("Trakt was searched for an account that never linked it")):
            resp = self.client.get("/rankings/fragments/search?q=test")
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(seen, {"trakt_token": "", "tmdb": True})

    def test_a_ratings_read_uses_the_callers_own_token(self):
        seen = {}

        async def _ratings(settings):
            seen["token"] = settings.trakt_access_token
            return []

        asyncio.run(ranker.create_board(self.user_id, uid="b1"))
        with mock.patch.object(trakt_routes, "access_token_for_user",
                               _token_for(USER_TOKEN)), \
             mock.patch.object(trakt_sync, "fetch_ratings", _ratings):
            resp = self.client.post("/rankings/boards/b1/seed", json={})
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(seen["token"], USER_TOKEN)
        self.assertNotEqual(seen["token"], OPERATOR_TOKEN)

    def test_the_seed_is_offered_once_an_account_has_something_to_read(self):
        with mock.patch.object(trakt_routes, "access_token_for_user", _token_for(USER_TOKEN)):
            sources = asyncio.run(ranker_routes._sources(self.user_id))
        self.assertEqual(sources, {"search": True, "ratings": True})


def _token_for(token: str):
    async def _call(user_id, settings=None):
        return token
    return _call


class ProtocolTests(StandaloneTestCase):
    """Both paths go through their seam, so a source that is not this app's
    current provider drives them without any provider function running."""

    def test_a_fake_fan_out_drives_search_with_no_provider_call(self):
        search, calls = canned(a_hit(a_show(1396, "Breaking Bad")))
        with mock.patch.object(title_search, "search_catalogue", search), \
             mock.patch.object(trakt_detail, "search_titles", exploding("the provider was called")):
            resp = self.client.get("/rankings/fragments/search?q=brea")
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(calls, [("brea", Media.SHOW, False)])

    def test_a_fake_source_drives_the_ratings_seed_with_no_provider_call(self):
        source = FakeRatingsSource([
            RatedTitle(title=a_show(1396, "Breaking Bad"), rating=10),
            RatedTitle(title=a_show(1399, "Game of Thrones"), rating=7),
        ])
        asyncio.run(ranker.create_board(self.user_id, uid="b1"))
        with mock.patch.object(ranker_sources, "ratings_source", lambda: source), \
             mock.patch.object(ranker_sources, "ratings_available", _true()), \
             mock.patch.object(trakt_sync, "fetch_ratings", exploding("the provider was called")):
            # THE DRY RUN ANSWERS THE CONFIRMATION ITS OWN COUNTS ARE WRITTEN
            # INTO, and pressing that is the same request with `commit` on.
            preview = self.client.post("/rankings/boards/b1/seed", json={}).text
            committed = self.client.post("/rankings/boards/b1/seed",
                                         json={"commit": "1"}).text

        self.assertEqual(source.calls, [self.user_id, self.user_id])
        self.assertIn("would add 2 title(s)", preview)
        self.assertEqual(self.value("SELECT COUNT(*) FROM tier_items"), 2)
        self.assertIn("2 new tier(s)", committed)

        board = self.client.get("/api/rankings/boards/b1").json()["board"]
        placed = {c["uid"]: [i["title"] for i in c["items"]] for c in board["categories"]}
        self.assertEqual(placed["tier-s"], ["Breaking Bad"])
        self.assertEqual(placed["tier-c"], ["Game of Thrones"])


def _true():
    async def _call(*args, **kwargs):
        return True
    return _call


class ModuleIsolationTests(unittest.TestCase):
    """The structural half of the guarantee.

    A behavioural test can be satisfied by a coupling that merely does not
    happen to run today. This asserts the modules that make up the ranker do not
    import the other feature at all, so the ranker cannot start depending on it
    by accident.
    """

    # READ OFF THE PACKAGE, NOT LISTED BY HAND. A hardcoded tuple of filenames
    # empties itself the moment somebody renames a module, and this test skips
    # names that do not exist — so it would go on reporting green while checking
    # nothing. Reading the directory means a module renamed or added tomorrow is
    # covered the moment it lands, which is the property the assertion needs.
    RANKER_PACKAGE = APP_DIR / "ranker"
    # The single module allowed to know, which is what keeps the coupling
    # deletable rather than diffuse. `imports.py` and not `import.py`: `import`
    # is a keyword, so that filename could never be reached by an import
    # statement at all.
    ADAPTER = "imports.py"
    # Not in the package, but the ranker's routes and its export path draw on
    # them, so a tracker import in either would reach the ranker through the
    # back door. Listed by hand because they live in app/media/, which the
    # calendar shares — they cannot be derived from the ranker's own directory,
    # and the `.exists()` guard below is what lets that list survive a move.
    SHARED_MODULES = ("media/posters.py", "media/artwork.py")

    # ANY mention, not merely an import statement. The adapter reads the other
    # feature's tables through app/db.py rather than importing its module, so a
    # test that only looked for `import distrakt` would pass everywhere and
    # prove nothing — including in a module that had grown a query against
    # those tables. The module name and every table name share this prefix, so
    # one pattern catches both kinds of coupling.
    MENTIONS_THE_TRACKER = re.compile(r"distrakt", re.IGNORECASE)

    def _checked_modules(self) -> list:
        """Every module the isolation rule covers: the whole ranker package bar
        its __init__ and the one adapter, plus the shared image modules it
        draws on."""
        found = [
            path for path in sorted(self.RANKER_PACKAGE.glob("*.py"))
            if path.name not in {"__init__.py", self.ADAPTER}
        ]
        found += [
            path for name in self.SHARED_MODULES
            if (path := APP_DIR / name).exists()
        ]
        return found

    def test_no_ranker_module_touches_the_tracker(self):
        offenders = sorted(
            path.relative_to(APP_DIR).as_posix() for path in self._checked_modules()
            if self.MENTIONS_THE_TRACKER.search(path.read_text(encoding="utf-8"))
        )
        self.assertEqual(offenders, [], f"ranker modules coupled to the tracker: {offenders}")

    def test_at_least_one_of_those_modules_actually_exists(self):
        """Guards the test above against passing because it checked nothing.

        Deriving the list from the directory removes the rename hazard, but not
        this one: an empty or missing package directory would still yield an
        empty list and a green run."""
        present = [path.relative_to(APP_DIR).as_posix() for path in self._checked_modules()]
        self.assertGreaterEqual(len(present), 5, present)

    def test_the_adapter_is_where_the_coupling_lives(self):
        """The inverse assertion, so the pattern above is known to match
        something real rather than nothing at all."""
        text = (self.RANKER_PACKAGE / self.ADAPTER).read_text(encoding="utf-8")
        self.assertTrue(self.MENTIONS_THE_TRACKER.search(text))

    def test_the_data_layer_imports_no_provider_either(self):
        """app/ranker/core.py stores what it is given. A provider import there
        would mean a title's origin had leaked into how it is stored."""
        text = (self.RANKER_PACKAGE / "core.py").read_text(encoding="utf-8")
        # `\.+` and not `\.`: core.py sits one level deeper than it used to, so
        # a sibling of app/ is now `from .. import`, and a pattern pinned to a
        # single dot would quietly stop matching the thing it was written for.
        self.assertNotRegex(text, r"^\s*from\s+\.+\s+import\s+[^\n]*\btrakt\b")
        self.assertNotIn("import trakt", text)


if __name__ == "__main__":
    unittest.main()
