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
backwards: a SEARCH must use the instance credential (this grant implies no
linked account, so reaching for a per-user token would break the very accounts
this file is about), while a RATINGS read must use the caller's own and hide
itself entirely when there is none.

No network. Every provider call is a stand-in, and the tests that matter assert
the real provider functions were never reached.

Run: ./.venv/Scripts/python.exe -m unittest tests.test_ranker_standalone -v
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

os.environ["TRAKT_DATA_DIR"] = tempfile.mkdtemp(prefix="tns-standalone-test-")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient  # noqa: E402

from app import auth, db, ranker_routes, ranker_sources  # noqa: E402
from app import trakt, trakt_routes  # noqa: E402
from app.config import Settings, save_settings  # noqa: E402
from app.main import app  # noqa: E402
from app.ranker_sources import Media, RatedTitle, TitleRef  # noqa: E402

TMP = Path(os.environ["TRAKT_DATA_DIR"])
ORIGIN = "https://testserver"
APP_DIR = Path(__file__).resolve().parent.parent / "app"

OPERATOR_TOKEN = "operator-instance-token"
USER_TOKEN = "this-users-own-token"


class FakeSearchSource:
    """A `TitleSearchSource` that answers from a canned list.

    Its existence is half the point: the route talks to a Protocol, so a source
    that has never heard of any provider drives the whole path.
    """

    def __init__(self, refs: list[TitleRef]):
        self.refs = refs
        self.calls: list[tuple[str, Media]] = []

    async def search(self, query: str, media: Media) -> list[TitleRef]:
        self.calls.append((query, media))
        return [ref for ref in self.refs if ref.media == media]


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
    _counter = 0

    def setUp(self):
        StandaloneTestCase._counter += 1
        db.set_db_path(TMP / f"standalone-{StandaloneTestCase._counter}.db")
        asyncio.run(db.migrate())
        # A configured instance credential, which is what a search is supposed
        # to use. Nothing in this file links an account to anything.
        save_settings(Settings(trakt_client_id="instance-client",
                               trakt_access_token=OPERATOR_TOKEN))
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
            "SELECT COUNT(*) FROM distrakt_shows WHERE user_id = ?", (self.user_id,)), 0)
        self.assertEqual(self.value(
            "SELECT COUNT(*) FROM linked_identities WHERE user_id = ?", (self.user_id,)), 0)


class ExportNameDefaultTests(StandaloneTestCase):
    """The export dialog prefills the name it draws on the image. It should
    offer the account's chosen display name when there is one — that is what
    somebody wants on a poster they are going to share — and fall back to the
    username otherwise."""

    def _prefilled_name(self):
        """What the page hands the export dialog, out of the JSON island the
        ranker page embeds for its own script."""
        body = self.client.get("/rankings").text
        blob = body.split('id="rankerData"', 1)[1].split(">", 1)[1].split("</script>", 1)[0]
        return json.loads(blob)["username"]

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

        source = FakeSearchSource([a_show(1396, "Breaking Bad"), a_movie(550, "Fight Club")])
        with mock.patch.object(ranker_sources, "search_source", lambda: source):
            found = self.client.post("/api/rankings/search",
                                     json={"query": "brea", "media": "show"}).json()
        self.assertEqual([r["title"] for r in found["results"]], ["Breaking Bad"])
        result, = found["results"]
        self.assertEqual(result["key"], "show:tmdb:1396")

        self.client.post("/api/rankings/boards", json={"uid": "b1", "name": "Top 2026"})
        added = self.client.post("/api/rankings/boards/b1/items", json={"refs": [result]})
        self.assertEqual(added.json()["added"], 1)

        saved = self.client.post("/api/rankings/boards/b1/save", json={
            "version": 1,
            "categories": [{"uid": "s", "label": "S", "rank_priority": 60,
                            "items": ["show:tmdb:1396"]}],
            "pool": [],
        })
        self.assertEqual(saved.status_code, 200, saved.text)

        board = self.client.get("/api/rankings/boards/b1").json()["board"]
        tier, = board["categories"]
        self.assertEqual([i["title"] for i in tier["items"]], ["Breaking Bad"])
        self.assertEqual(board["pool"], [])

        # And out the other end, which is the half of "end to end" that matters
        # most here: an account with nothing but this feature can take its
        # ranking away as both an image and a text block.
        image = self.client.post("/api/rankings/boards/b1/export", json={
            "top_x": 1, "columns": 3, "fmt": "jpeg", "title": "Top Shows",
            "username": "ranker_only"})
        self.assertEqual(image.status_code, 200, image.text)
        self.assertEqual(image.headers["content-type"], "image/jpeg")

        text = self.client.post("/api/rankings/boards/b1/export/markdown",
                                json={"top_x": 1, "columns": 3, "title": "Top Shows"})
        self.assertIn("**Breaking Bad**", text.json()["markdown"])
        self.assertHasNothingElse()

    def test_a_movie_only_board_works_the_same_way(self):
        source = FakeSearchSource([a_movie(550, "Fight Club")])
        with mock.patch.object(ranker_sources, "search_source", lambda: source):
            found = self.client.post("/api/rankings/search",
                                     json={"query": "figh", "media": "movie"}).json()
        self.assertEqual([call[1] for call in source.calls], [Media.MOVIE])
        self.client.post("/api/rankings/boards",
                         json={"uid": "m1", "media_scope": "movie"})
        self.client.post("/api/rankings/boards/m1/items", json={"refs": found["results"]})
        self.assertEqual(self.value("SELECT media FROM tier_items"), "movie")
        self.assertEqual(self.value("SELECT match_id FROM tier_items"), "550")

    def test_a_title_with_no_shared_id_is_dropped_rather_than_stored(self):
        """It could not be told apart from another title of the same name, so
        there is nothing to rank — but it must not take the other results with
        it."""
        source = FakeSearchSource([
            TitleRef(media=Media.SHOW, title="Nameless", ids={"slug": "nameless"}),
            a_show(1396, "Breaking Bad"),
        ])
        with mock.patch.object(ranker_sources, "search_source", lambda: source):
            found = self.client.post("/api/rankings/search", json={"query": "na"}).json()
        self.assertEqual([r["title"] for r in found["results"]], ["Breaking Bad"])


class AbsenceTests(StandaloneTestCase):
    def test_neither_optional_source_is_advertised(self):
        sources = self.client.get("/api/rankings/sources").json()["sources"]
        self.assertEqual(sources, {"search": True})

    def test_the_import_route_refuses_and_explains_nothing(self):
        self.client.post("/api/rankings/boards", json={"uid": "b1"})
        resp = self.client.post("/api/rankings/boards/b1/import/tracker", json={"media": "show"})
        self.assertEqual(resp.status_code, 404)
        self.assertEqual(self.value("SELECT COUNT(*) FROM tier_items"), 0)

    def test_the_ratings_seed_refuses_when_there_is_no_linked_account(self):
        self.client.post("/api/rankings/boards", json={"uid": "b1"})
        with mock.patch.object(trakt, "fetch_ratings",
                               exploding("ratings were read without a linked account")):
            resp = self.client.post("/api/rankings/boards/b1/seed/ratings", json={"commit": True})
        self.assertEqual(resp.status_code, 404)
        self.assertEqual(self.value("SELECT COUNT(*) FROM tier_categories"), 0)


class CredentialTests(StandaloneTestCase):
    """The two rules that are easy to get backwards."""

    def test_search_uses_the_instance_credential_and_never_a_per_user_token(self):
        seen = {}

        async def _search(settings, media, query):
            seen["token"] = settings.trakt_access_token
            return []

        # The real Trakt adapter runs here — only the call at its edge is
        # replaced — so this pins what the adapter actually reaches for.
        with mock.patch.object(trakt, "search_titles", _search), \
             mock.patch.object(trakt_routes, "access_token_for_user",
                               exploding("a search reached for the caller's token")):
            resp = self.client.post("/api/rankings/search", json={"query": "test"})
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(seen["token"], OPERATOR_TOKEN)

    def test_a_ratings_read_uses_the_callers_own_token(self):
        seen = {}

        async def _ratings(settings):
            seen["token"] = settings.trakt_access_token
            return []

        self.client.post("/api/rankings/boards", json={"uid": "b1"})
        with mock.patch.object(trakt_routes, "access_token_for_user",
                               _token_for(USER_TOKEN)), \
             mock.patch.object(trakt, "fetch_ratings", _ratings):
            resp = self.client.post("/api/rankings/boards/b1/seed/ratings", json={})
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(seen["token"], USER_TOKEN)
        self.assertNotEqual(seen["token"], OPERATOR_TOKEN)

    def test_the_seed_is_offered_once_an_account_has_something_to_read(self):
        with mock.patch.object(trakt_routes, "access_token_for_user", _token_for(USER_TOKEN)):
            sources = self.client.get("/api/rankings/sources").json()["sources"]
        self.assertEqual(sources, {"search": True, "ratings": True})


def _token_for(token: str):
    async def _call(user_id, settings=None):
        return token
    return _call


class ProtocolTests(StandaloneTestCase):
    """Both paths go through their seam, so a source that is not this app's
    current provider drives them without any provider function running."""

    def test_a_fake_source_drives_search_with_no_provider_call(self):
        source = FakeSearchSource([a_show(1396, "Breaking Bad")])
        with mock.patch.object(ranker_sources, "search_source", lambda: source), \
             mock.patch.object(trakt, "search_titles", exploding("the provider was called")):
            resp = self.client.post("/api/rankings/search", json={"query": "brea"})
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(source.calls, [("brea", Media.SHOW)])

    def test_a_fake_source_drives_the_ratings_seed_with_no_provider_call(self):
        source = FakeRatingsSource([
            RatedTitle(title=a_show(1396, "Breaking Bad"), rating=10),
            RatedTitle(title=a_show(1399, "Game of Thrones"), rating=7),
        ])
        self.client.post("/api/rankings/boards", json={"uid": "b1"})
        with mock.patch.object(ranker_sources, "ratings_source", lambda: source), \
             mock.patch.object(ranker_sources, "ratings_available", _true()), \
             mock.patch.object(trakt, "fetch_ratings", exploding("the provider was called")):
            preview = self.client.post("/api/rankings/boards/b1/seed/ratings", json={}).json()
            committed = self.client.post("/api/rankings/boards/b1/seed/ratings",
                                         json={"commit": True}).json()

        self.assertEqual(source.calls, [self.user_id, self.user_id])
        self.assertEqual(preview["titles_added"], 2)
        self.assertFalse(preview["committed"])
        self.assertEqual(committed["tiers_created"], 2)

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

    # Every module in this feature EXCEPT the one optional adapter. Modules a
    # later session adds are listed here now: a name that does not exist yet is
    # skipped, and starts being checked the moment it lands.
    RANKER_MODULES = (
        "ranker.py", "ranker_routes.py", "ranker_sources.py",
        "grid_builder.py", "ranker_export.py", "posters.py", "artwork.py",
    )
    # The single module allowed to know, which is what keeps the coupling
    # deletable rather than diffuse.
    ADAPTER = "ranker_import.py"

    # ANY mention, not merely an import statement. The adapter reads the other
    # feature's tables through app/db.py rather than importing its module, so a
    # test that only looked for `import distrakt` would pass everywhere and
    # prove nothing — including in a module that had grown a query against
    # those tables. The module name and every table name share this prefix, so
    # one pattern catches both kinds of coupling.
    MENTIONS_THE_TRACKER = re.compile(r"distrakt", re.IGNORECASE)

    def test_no_ranker_module_touches_the_tracker(self):
        offenders = sorted(
            name for name in self.RANKER_MODULES
            if (path := APP_DIR / name).exists()
            and self.MENTIONS_THE_TRACKER.search(path.read_text(encoding="utf-8"))
        )
        self.assertEqual(offenders, [], f"ranker modules coupled to the tracker: {offenders}")

    def test_at_least_one_of_those_modules_actually_exists(self):
        """Guards the test above against passing because it checked nothing."""
        present = [name for name in self.RANKER_MODULES if (APP_DIR / name).exists()]
        self.assertGreaterEqual(len(present), 5, present)

    def test_the_adapter_is_where_the_coupling_lives(self):
        """The inverse assertion, so the pattern above is known to match
        something real rather than nothing at all."""
        text = (APP_DIR / self.ADAPTER).read_text(encoding="utf-8")
        self.assertTrue(self.MENTIONS_THE_TRACKER.search(text))

    def test_the_data_layer_imports_no_provider_either(self):
        """app/ranker.py stores what it is given. A provider import there would
        mean a title's origin had leaked into how it is stored."""
        text = (APP_DIR / "ranker.py").read_text(encoding="utf-8")
        self.assertNotRegex(text, r"^\s*from\s+\.\s+import\s+[^\n]*\btrakt\b", )
        self.assertNotIn("import trakt", text)


if __name__ == "__main__":
    unittest.main()
