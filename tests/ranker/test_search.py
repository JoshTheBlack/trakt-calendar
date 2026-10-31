"""The board's search: the app's one title search, landing on a board.

WHAT THIS FILE HOLDS, and why each matters:
  - TYPING READS STORED TITLES AND ASKS NOBODY — no service, no viewer
    settings, no budget — and lists every stored title, whoever's calendar
    loaded it, filters or not.
  - ENTER ASKS TMDB, and a service the viewer has linked only when they add it
    (providers.for_search over auth/viewer.py's settings), and is the one thing
    the search budget counts.
  - A RESULT NAMES ITSELF BY REFERENCE — a stored title by its title_key, a
    service's hit by query and place — and every add reads the title back from
    what a service said. A title_key nothing stores is refused; fields the page
    sends are ignored; nothing is asked again.
  - A RESULT'S SEASON CHECKLIST adds several seasons in one post, each an
    arrival through the board's rule, greying what the board holds.
  - A DRAG adds the whole title the same way a click does, for both kinds.

No network: stored titles are written with the calendar's own writer, the
service fan-out is replaced at providers/title_search.py's edge, and the autouse
guard in tests/conftest.py fails anything that reaches past both.
"""
from __future__ import annotations

import asyncio
import json
import re
from html import unescape
from unittest import mock
from urllib.parse import parse_qs, urlsplit

from app import db, providers
from app.auth import prefs as auth_prefs, simkl_routes, viewer
from app.calendar import entries
from app.metadata import typed_search
from app.config import Settings
from tests.support import save_settings
from app.providers import title_search
from app.providers.base import ItemKey, Media, Record, Source, proxied_image
from app.providers.simkl import search as simkl_search
from app.providers.trakt import detail as trakt_detail
from app.ranker import core as ranker, routes as ranker_routes
from tests.ranker.test_boards import RankerTestCase, inside_pool, show_ref

AIR = 1783540800.0  # 2026-07-08 20:00Z
TMDB_TOKEN = "eyJhbGciOiJIUzI1NiJ9.eyJhdWQiOiJ0ZXN0In0.c2lnbmF0dXJl"
_HIT = re.compile(r'<div class="ranker-hit( is-held)?"([^>]*)>')
# A film's row is a <div> that posts the add; a show's is the <summary> that
# asks for its checklist. Both name the result by the same reference.
_ROW = re.compile(r'<(?:div|summary) class="search-row ranker-result"([^>]*)>')
_TITLE = re.compile(r'<span class="search-title">([^<]*)')


def store(*records, endpoint="shows/premieres"):
    now = db.now()
    asyncio.run(entries.store_loose_airings(endpoint, list(records), now=now,
                                            stale_after=now + 86400))


def a_record(title="Severance", tmdb=95396, media=Media.SHOW, **overrides) -> Record:
    values = dict(source=Source.TRAKT, media=media, id=str(tmdb),
                  ids={"trakt": tmdb, "tmdb": tmdb}, detail_url="https://trakt.test/x",
                  title=title, air_ts=AIR, year=2022, network="Apple TV+",
                  season=1 if media == Media.SHOW else None,
                  episode_number=1 if media == Media.SHOW else None, enriched=True)
    values.update(overrides)
    return Record(**values)


def a_hit(n: int, *, media=Media.SHOW, ids=None, **overrides) -> title_search.MergedSearchHit:
    ids = {"tmdb": n, "trakt": n * 10} if ids is None else ids
    values = dict(key=ItemKey(str(media), "tmdb", str(n)) if "tmdb" in ids else None,
                  season=None, source_ids={Source.TRAKT: str(n)}, ids=ids,
                  title=f"Title {n}", year=2020 + n % 5, network="HBO", runtime=None,
                  overview="", media=media, poster="https://media.trakt.tv/p.jpg")
    values.update(overrides)
    return title_search.MergedSearchHit(**values)


class CannedCatalogue:
    """The services' fan-out, replaced at its edge: a page's worth of hits, and
    a record of every call — who was asked, with which token, which pages, and
    whether it was a read-back."""

    def __init__(self, hits=None):
        self.hits = hits if hits is not None else [
            a_hit(n) for n in range(1, 26)]
        self.calls: list[dict] = []

    async def __call__(self, asked, settings, media, query, *, pages=1, cached_only=False):
        self.calls.append({"asked": [entry[0] for entry in asked], "query": query,
                           "media": media, "cached_only": cached_only, "pages": pages,
                           "simkl_token": settings.simkl_access_token})
        return title_search.SearchMergeResult(
            hits=[hit for hit in self.hits if hit.media == media], failed=frozenset())


def result_rows(html: str) -> list[dict]:
    """Every result as what it would SEND: its wrapper's data attributes (what a
    drag posts), the reference its row names (`body` — a film's hx-vals, which
    its click posts; a show's checklist address, less the board, which its
    checklist posts back), its title and whether it is held."""
    titles = [unescape(t).strip() for t in _TITLE.findall(html)]
    wrappers = _HIT.findall(html)
    bodies = [reference(attrs) for attrs in _ROW.findall(html)]
    out = []
    for index, (classes, attrs) in enumerate(wrappers):
        data = dict(re.findall(r'data-([a-z]+)="([^"]*)"', attrs))
        out.append({"title": titles[index], "held": "is-held" in classes,
                    "data": {k: unescape(v) for k, v in data.items()},
                    "body": bodies[index]})
    return out


def reference(attrs: str) -> dict:
    """What one row names its result by, whichever request it makes."""
    vals = re.search(r'hx-vals="([^"]*)"', attrs)
    if vals:
        return json.loads(unescape(vals.group(1)))
    url = unescape(re.search(r'hx-get="([^"]*)"', attrs).group(1))
    return {name: values[0] for name, values in parse_qs(urlsplit(url).query).items()
            if name != "board"}


class _SearchTestCase(RankerTestCase):

    def setUp(self):
        super().setUp()
        save_settings(Settings(tmdb_api_key=TMDB_TOKEN, trakt_client_id="cid",
                               simkl_client_id="scid", simkl_public_calendar_enabled=False))
        self.user_id = self.ranker_user()
        self.sign_in_as(self.user_id)
        asyncio.run(ranker.create_board(self.user_id, uid="b1"))
        self.catalogue = CannedCatalogue()
        patch = mock.patch.object(title_search, "search_catalogue", self.catalogue)
        patch.start()
        self.addCleanup(patch.stop)

    def typed(self, query, media="show"):
        return self.client.get(
            f"/rankings/fragments/search-stored?board=b1&q={query}&media={media}").text

    def enter(self, query, media="show", also=""):
        return self.client.get(f"/rankings/fragments/search?board=b1&q={query}&media={media}"
                               f"&also={also}").text

    def add(self, body: dict):
        return self.client.post("/rankings/boards/b1/items/add", json=body)

    def drag(self, body: dict):
        return self.client.post("/api/rankings/boards/b1/items", json=body)

    def keys(self) -> list[str]:
        return [f"{r['media']}:{r['match_source']}:{r['match_id']}"
                + (f":s{r['season']}" if r["season"] is not None else "")
                for r in self.rows("SELECT * FROM tier_items ORDER BY id")]


class TypingTests(_SearchTestCase):

    def test_typing_lists_stored_titles_and_asks_no_service_at_all(self):
        store(a_record())
        settings = mock.AsyncMock(side_effect=AssertionError("built viewer settings"))
        with mock.patch.object(viewer, "settings_for_user", settings):
            html = self.typed("sever")
        self.assertEqual([row["title"] for row in result_rows(html)], ["Severance"])
        self.assertEqual(self.catalogue.calls, [])
        settings.assert_not_awaited()
        self.assertIn("Titles this app already knows. Press Enter", html)

    def test_a_typed_read_is_not_budgeted(self):
        store(a_record())
        for _ in range(ranker_routes.SEARCH_MAX_PER_WINDOW + 3):
            self.assertTrue(result_rows(self.typed("sever")))
        self.assertEqual(self.value(
            "SELECT COUNT(*) FROM login_attempts WHERE key_type = 'ranker_search'"), 0)

    def test_the_list_is_every_stored_title_not_the_viewers_calendar(self):
        """Unfiltered: a title another viewer's month stored, and one this
        viewer's own filters would hide, are both addable."""
        asyncio.run(auth_prefs.update_user_prefs(self.user_id, endpoint="shows/all",
                                                 tv_genres="-drama"))
        self.assertEqual(self.value("SELECT tv_genres FROM user_prefs WHERE user_id = ?",
                                    (self.user_id,)), "-drama")
        store(a_record(genres=["drama"]), endpoint="shows/all")
        self.assertEqual([row["title"] for row in result_rows(self.typed("sever"))],
                         ["Severance"])

    def test_a_typed_row_names_its_stored_identity_and_draws_its_poster(self):
        store(a_record(poster="https://media.trakt.tv/severance.jpg"))
        row, = result_rows(self.typed("sever"))
        self.assertEqual(set(row["body"]), {"t", "q", "media"})
        self.assertEqual(row["body"]["q"], "sever")
        self.assertEqual(row["data"]["key"], "show:tmdb:95396")
        self.assertEqual(row["data"]["t"], row["body"]["t"])
        src = unescape(re.search(r'<img class="search-poster" src="([^"]*)"',
                                 self.typed("sever")).group(1))
        self.assertEqual(src, proxied_image("https://media.trakt.tv/severance.jpg"))

    def test_too_short_draws_nothing(self):
        store(a_record())
        self.assertEqual(self.typed("s").strip(), "")

    def test_films_are_listed_under_the_film_medium(self):
        store(a_record("Heat", 949, Media.MOVIE, runtime=170), endpoint="movies/releases")
        self.assertEqual([r["title"] for r in result_rows(self.typed("heat", "movie"))], ["Heat"])
        self.assertEqual(result_rows(self.typed("heat")), [])


class EnterTests(_SearchTestCase):

    def test_enter_asks_the_services_and_draws_source_marked_rows(self):
        html = self.enter("title")
        rows = result_rows(html)
        # THE WHOLE ANSWER IS DRAWN: "more" lengthens it a page at a time, and a
        # row is named by its place in it, so a cap would hide rows it named.
        self.assertEqual(len(rows), len(self.catalogue.hits))
        self.assertEqual(rows[0]["body"], {"q": "title", "i": "0", "media": "show"})
        self.assertEqual(rows[3]["data"], {"key": "show:tmdb:4", "q": "title", "i": "3",
                                           "media": "show"})
        self.assertIn('class="search-marks"', html)
        self.assertEqual([call["cached_only"] for call in self.catalogue.calls], [False])

    def test_enter_reaches_tmdb_alone_for_a_viewer_with_nothing_linked(self):
        html = self.enter("title")
        call, = self.catalogue.calls
        self.assertEqual(call["asked"], [Source.TMDB])
        self.assertNotIn("Also search", html)

    def test_a_linked_simkl_is_offered_and_asked_with_the_viewers_own_token(self):
        async def _token(user_id, *args, **kwargs):
            return "viewer-simkl-token" if user_id == self.user_id else None
        with mock.patch.object(simkl_routes, "access_token_for_user", _token):
            offered = self.enter("title")
            added = self.enter("title", also="simkl")
        self.assertIn(">Also search Simkl<", offered)
        self.assertNotIn(">Also search Simkl<", added)
        self.assertEqual([c["asked"] for c in self.catalogue.calls],
                         [[Source.TMDB], [Source.TMDB, Source.SIMKL]])
        self.assertEqual(self.catalogue.calls[1]["simkl_token"], "viewer-simkl-token")
        # Every row of the widened search names it, so its add re-reads it.
        self.assertEqual(result_rows(added)[0]["body"],
                         {"q": "title", "also": "simkl", "i": "0", "media": "show"})

    def test_the_budget_counts_enter_and_refuses_in_words(self):
        for _ in range(ranker_routes.SEARCH_MAX_PER_WINDOW):
            self.assertTrue(result_rows(self.enter("title")))
        refused = self.enter("title")
        self.assertEqual(result_rows(refused), [])
        self.assertIn("Too many searches", refused)

    def test_the_budget_is_per_account_and_survives_a_restart(self):
        self.enter("title")
        self.assertEqual(self.value(
            "SELECT COUNT(*) FROM login_attempts WHERE key_type = 'ranker_search' "
            "AND key_value = ?", (str(self.user_id),)), 1)
        for _ in range(ranker_routes.SEARCH_MAX_PER_WINDOW + 1):
            self.enter("title")
        self.sign_in_as(self.ranker_user("someone_else"))
        asyncio.run(ranker.create_board(self.ranker_user_id(), uid="b1"))
        self.assertTrue(result_rows(self.enter("title")))

    def ranker_user_id(self):
        return self.value("SELECT id FROM users WHERE username = 'someone_else'")

    def test_an_unknown_medium_is_refused_in_words(self):
        html = self.enter("title", media="album")
        self.assertIn("ranker-preview-hint", html)
        self.assertEqual(result_rows(html), [])

    def test_no_tmdb_token_is_said_rather_than_searched_around(self):
        save_settings(Settings(trakt_client_id="cid", simkl_public_calendar_enabled=False),
                      clear_unset_secrets=True)
        html = self.enter("title")
        call, = self.catalogue.calls
        self.assertEqual(call["asked"], [])
        self.assertIn("is not configured", html)
        self.assertNotIn("Nothing found.", html)

    def test_no_tmdb_token_still_offers_the_services_the_viewer_linked(self):
        """Not a fallback — nothing is asked until the viewer presses the
        button that names the service, and the box goes on saying TMDB is not
        configured beside that service's answer."""
        save_settings(Settings(simkl_client_id="scid", simkl_public_calendar_enabled=False),
                      clear_unset_secrets=True)

        async def _token(user_id, *args, **kwargs):
            return "viewer-simkl-token" if user_id == self.user_id else None
        with mock.patch.object(simkl_routes, "access_token_for_user", _token):
            offered = self.enter("title")
            added = self.enter("title", also="simkl")
        self.assertIn(">Also search Simkl<", offered)
        self.assertEqual([c["asked"] for c in self.catalogue.calls], [[], [Source.SIMKL]])
        self.assertTrue(result_rows(added))
        self.assertIn("is not configured", added)

    def test_a_held_whole_title_is_drawn_held_and_a_keyless_hit_is_not_draggable(self):
        asyncio.run(ranker.add_titles(self.user_id, "b1", [show_ref("2")]))
        self.catalogue.hits = [a_hit(1), a_hit(2), a_hit(3, ids={"simkl": 77})]
        rows = result_rows(self.enter("title"))
        self.assertEqual([r["held"] for r in rows], [False, True, False])
        self.assertNotIn("key", rows[2]["data"])


class AddTests(_SearchTestCase):
    """A result clicked, whether Enter or typing found it."""

    def test_a_service_pick_adds_what_the_search_said_and_answers_with_the_page(self):
        self.enter("title")
        page = self.add({"q": "title", "i": 2, "media": "show",
                         "title": "Something Else", "ids": {"trakt": 666}})
        self.assertEqual(page.status_code, 200)
        self.assertIn("<html", page.text)
        self.assertIn("Added the title to the pool.", page.text)
        item = asyncio.run(ranker.fetch_item(self.user_id, "b1", "show:tmdb:3"))
        self.assertEqual((item["title"], item["ids"]), ("Title 3", {"tmdb": 3, "trakt": 30}))
        # The pick and the redraw both read the search back; nothing asked again.
        self.assertEqual([c["cached_only"] for c in self.catalogue.calls], [False, True, True])

    def test_a_title_found_through_tmdb_learns_every_services_id(self):
        """A board title found through TMDB's search held its tmdb id alone, so its
        details panel had no Trakt or Simkl button though both list the title. The
        add asks each service the title has no id from, by shared id, through the
        title store — with the viewer's own settings, which is what lets Simkl be
        asked at all."""
        self.catalogue.hits = [a_hit(5, ids={"tmdb": 5}, source_ids={Source.TMDB: "5"})]

        async def _token(user_id, *args, **kwargs):
            return "viewer-simkl-token" if user_id == self.user_id else None
        trakt = mock.AsyncMock(return_value={"trakt": 50, "trakt_slug": "title-5", "tmdb": 5})
        simkl = mock.AsyncMock(return_value={"simkl": 500, "simkl_slug": "title-5"})
        with mock.patch.object(simkl_routes, "access_token_for_user", _token), \
             mock.patch("app.providers.trakt.detail.find_by_shared_id", trakt), \
             mock.patch("app.providers.simkl.by_id.find_by_shared_id", simkl):
            self.enter("title")
            self.assertEqual(self.add({"q": "title", "i": 0, "media": "show"}).status_code, 200)
        item = asyncio.run(ranker.fetch_item(self.user_id, "b1", "show:tmdb:5"))
        self.assertEqual({name: item["ids"].get(name) for name in ("tmdb", "trakt", "simkl")},
                         {"tmdb": 5, "trakt": 50, "simkl": 500})
        self.assertEqual((item["ids"]["trakt_slug"], item["ids"]["simkl_slug"]),
                         ("title-5", "title-5"))
        trakt.assert_awaited_once()
        simkl.assert_awaited_once()

    def test_a_title_the_store_knows_by_a_higher_id_is_filed_there(self):
        """A result naming a show only by its tvdb id is the title the store
        already knows by its tmdb id; the board files it there, so it is drawn
        as held beside the row already on the board rather than added again."""
        store(a_record("Known", 900, ids={"trakt": 9, "tmdb": 900, "tvdb": 4321}))
        asyncio.run(ranker.add_titles(self.user_id, "b1", [show_ref("900")]))
        self.catalogue.hits = [a_hit(4321, ids={"tvdb": 4321}, key=ItemKey("show", "tvdb", "4321"),
                                     source_ids={Source.SIMKL: "77"})]
        self.enter("title")
        self.assertIn("already on this board", self.add({"q": "title", "i": 0,
                                                         "media": "show"}).text)
        self.assertEqual(self.keys(), ["show:tmdb:900"])

    def test_a_simkl_only_pick_files_the_poster_its_row_was_drawn_with(self):
        """The row's picture is asked for by key afterwards, and a title nothing
        had filed got one lookup — TMDB and Trakt, by tmdb id — so a title only
        Simkl lists was recorded as having no picture and drew the empty frame,
        although its search row had just shown Simkl's poster. The add files
        that address under Simkl, and the row's picture route redirects to it."""
        poster = "https://simkl.in/posters/31/313599abc_m.jpg"
        self.catalogue.hits = [a_hit(313599, ids={"tmdb": 313599, "simkl": 99},
                                     source_ids={Source.SIMKL: "99"}, poster=poster,
                                     poster_source="simkl")]
        self.enter("title")
        self.assertEqual(self.add({"q": "title", "i": 0, "media": "show"}).status_code, 200)
        self.assertEqual([dict(row) for row in self.rows(
            "SELECT source, url FROM show_posters WHERE tmdb = 313599")],
            [{"source": "simkl", "url": poster}])
        with mock.patch("app.media.posters._look_up_address",
                        side_effect=AssertionError("a filed picture was looked up again")):
            picture = self.client.get("/api/rankings/poster?board=b1&key=show:tmdb:313599",
                                      follow_redirects=False)
        self.assertEqual(picture.status_code, 302)
        self.assertEqual(picture.headers["location"], proxied_image(poster))

    def _keyless(self):
        """A result the search left keyless: Simkl's own id and nothing any other
        service shares, as nine results across five live searches were."""
        self.catalogue.hits = [a_hit(1850009, ids={"simkl": 1850009},
                                     source_ids={Source.SIMKL: "1850009"},
                                     title="Attack on Titan / Shingeki No Die")]
        self.enter("title")

    def _pick(self, ids):
        return title_search.Pick(title="Attack on Titan / Shingeki No Die", network="",
                                 year=None, ids=ids, seasons=[], named_season=None,
                                 media=Media.SHOW)

    def test_a_keyless_result_is_looked_up_and_added(self):
        """MEASURED: every keyless result across five live searches got a tvdb or
        mal id from its own per-title lookup — the one "Seasons…" makes. The add
        makes it when nothing had stored it, where it used to refuse."""
        self._keyless()
        calls = []

        async def _pick_from_hit(settings, hit, *, cached_only):
            # With nothing stored the store-only pick STILL answers, carrying the
            # hit's own ids — measured live, and what kept the lookup from being
            # made when this test answered None instead.
            calls.append(cached_only)
            return self._pick({"simkl": 1850009} if cached_only
                              else {"simkl": 1850009, "tvdb": 416458})

        with mock.patch.object(title_search, "pick_from_hit", new=_pick_from_hit):
            page = self.add({"q": "title", "i": 0, "media": "show"})
        self.assertIn("Added the title to the pool.", page.text)
        self.assertIn("show:tvdb:416458", self.keys())
        self.assertIn(False, calls, "the add never made the lookup")

    def test_a_keyless_result_whose_lookup_finds_no_id_is_still_refused(self):
        self._keyless()

        async def _pick_from_hit(settings, hit, *, cached_only):
            return None if cached_only else self._pick({"simkl": 1850009})

        with mock.patch.object(title_search, "pick_from_hit", new=_pick_from_hit):
            page = self.add({"q": "title", "i": 0, "media": "show"})
        self.assertIn("This title has no id a board can hold it by.", page.text)
        self.assertEqual(self.keys(), [])

    def test_a_lookup_that_could_not_be_made_says_so(self):
        """The service, not the title — so not the no-id sentence."""
        from app.providers.base import SourceUnavailable
        self._keyless()

        async def _pick_from_hit(settings, hit, *, cached_only):
            if cached_only:
                return self._pick({"simkl": 1850009})
            raise SourceUnavailable("Simkl is down")

        with mock.patch.object(title_search, "pick_from_hit", new=_pick_from_hit):
            page = self.add({"q": "title", "i": 0, "media": "show"})
        self.assertIn("look this title up just now", page.text)

    def test_the_search_is_still_on_the_page_with_the_result_held(self):
        self.enter("title")
        html = self.add({"q": "title", "i": 0, "media": "show"}).text
        # The BOX keeps its text by surviving the swap (`hx-preserve`, measured
        # in both engines), so what the answer must carry is the search's rows.
        self.assertIn('id="searchBox-b1"', html)
        rows = result_rows(html)
        self.assertEqual(len(rows), len(self.catalogue.hits))
        self.assertEqual([r["held"] for r in rows[:2]], [True, False])

    def test_the_page_carries_the_new_version_and_hoists_the_title(self):
        before = asyncio.run(ranker.fetch_board(self.user_id, "b1"))["version"]
        html = self.add({"q": "title", "i": 4, "media": "show"}).text
        after = asyncio.run(ranker.fetch_board(self.user_id, "b1"))["version"]
        self.assertGreater(after, before)
        self.assertIn(f'"version": {after}', html)

    def test_the_added_title_is_drawn_at_the_head_of_the_pool(self):
        """The pool pages sixty at a time and a new title sorts LAST, so on a
        board with a full page of unranked titles it would land on a page nobody
        is looking at. The pool stores no order, so putting it first disturbs
        nothing."""
        refs = [show_ref(str(n)) for n in range(300, 300 + ranker_routes.POOL_PAGE_SIZE + 4)]
        asyncio.run(ranker.add_titles(self.user_id, "b1", refs))
        drawn = re.findall(r'data-key="([^"]+)"', inside_pool(self.client.get("/rankings").text))
        self.assertNotIn("show:tmdb:1", drawn)
        self.assertEqual(len(drawn), ranker_routes.POOL_PAGE_SIZE)
        self.enter("title")
        pool = inside_pool(self.add({"q": "title", "i": 0, "media": "show"}).text)
        self.assertEqual(re.findall(r'data-key="([^"]+)"', pool)[0], "show:tmdb:1")

    def test_a_typed_pick_adds_the_stored_title(self):
        store(a_record())
        row, = result_rows(self.typed("sever"))
        html = self.add(row["body"]).text
        self.assertEqual(self.keys(), ["show:tmdb:95396"])
        item = asyncio.run(ranker.fetch_item(self.user_id, "b1", "show:tmdb:95396"))
        self.assertEqual((item["title"], item["network"]), ("Severance", "Apple TV+"))
        # The redraw is the typed list again, the result now held — no service.
        self.assertEqual([r["held"] for r in result_rows(html)], [True])
        self.assertEqual(self.catalogue.calls, [])

    def test_an_unknown_title_key_is_refused_and_writes_nothing(self):
        html = self.add({"t": "show:tmdb:4242", "q": "x", "media": "show"}).text
        self.assertIn("That title is no longer stored here", html)
        self.assertEqual(self.keys(), [])

    def test_a_search_no_longer_stored_is_refused(self):
        self.catalogue.hits = []
        html = self.add({"q": "gone", "i": 0, "media": "show"}).text
        self.assertIn("That search has expired", html)
        self.assertEqual(self.keys(), [])

    def test_a_negative_or_absent_place_names_nothing(self):
        for body in ({"q": "title", "i": -1}, {"q": "title"}, {"q": "", "i": 0}):
            with self.subTest(body=body):
                self.add({**body, "media": "show"})
                self.assertEqual(self.keys(), [])


class SeasonChecklistTests(_SearchTestCase):

    PICK = title_search.Pick(
        title="Title 1", network="HBO", year=2021, ids={"tmdb": 1, "trakt": 10},
        seasons=[{"season": 1, "episode_count": 8, "first_aired": "2020-01-01T00:00:00Z"},
                 {"season": 2, "episode_count": 9, "first_aired": "2021-01-01T00:00:00Z"},
                 {"season": 3, "episode_count": 10, "first_aired": "2099-01-01T00:00:00Z"}],
        named_season=None, media=Media.SHOW)

    def setUp(self):
        super().setUp()
        self.picks = []

        async def _pick(settings, hit, *, cached_only):
            self.picks.append((hit.title, cached_only))
            return self.PICK
        patch = mock.patch.object(title_search, "pick_from_hit", _pick)
        patch.start()
        self.addCleanup(patch.stop)

    def checklist(self, **ref) -> str:
        from urllib.parse import urlencode
        return self.client.get("/rankings/fragments/search-seasons?"
                               + urlencode({"board": "b1", "media": "show", **ref})).text

    def boxes(self, html) -> list[tuple[str, bool, bool]]:
        return [(name, "disabled" in rest, "checked" in rest)
                for name, rest in re.findall(r'<input type="checkbox" name="season:(\d+)"'
                                             r' value="1"([^>]*)>', html)]

    def test_the_checklist_lists_seasons_greying_what_the_board_holds(self):
        asyncio.run(ranker.add_titles(self.user_id, "b1", [show_ref("1", season=1)]))
        self.enter("title")
        html = self.checklist(q="title", i="0")
        self.assertEqual(self.boxes(html), [("1", True, False), ("2", False, False),
                                            ("3", False, False)])
        self.assertIn("suggested", html)
        self.assertIn("airs 1 Jan 2099", html)
        self.assertEqual(self.picks, [("Title 1", False)])
        form = re.search(r'<form class="season-checklist"[^>]*>', html).group(0)
        self.assertIn('hx-post="/rankings/boards/b1/items/add"', form)
        self.assertIn("hx-json-enc", form)

    def test_the_checklist_leads_with_the_whole_show_greyed_once_held(self):
        """Pressing a show's row opens this checklist rather than adding, so the
        whole show is one of its choices — and never "this row", since a result
        is not a row yet."""
        self.enter("title")
        whole = re.compile(r'<input type="checkbox" name="season:" value="1"([^>]*)>')
        html = self.checklist(q="title", i="0")
        self.assertNotIn("disabled", whole.search(html).group(1))
        self.assertLess(html.index("Whole show"), html.index("Season 1"))
        self.assertNotIn("this row", html)
        asyncio.run(ranker.add_titles(self.user_id, "b1", [show_ref("1")]))
        self.assertIn("disabled", whole.search(self.checklist(q="title", i="0")).group(1))

    def test_the_whole_show_and_a_season_ticked_together_are_two_rows(self):
        self.enter("title")
        html = self.add({"q": "title", "i": "0", "media": "show", "want": "seasons",
                         "season:": "1", "season:2": "1"}).text
        self.assertIn("Added the whole show and 1 season to the pool.", html)
        self.assertEqual(self.keys(), ["show:tmdb:1", "show:tmdb:1:s2"])

    def test_the_whole_show_ticked_alone_adds_the_title(self):
        self.enter("title")
        html = self.add({"q": "title", "i": "0", "media": "show", "want": "seasons",
                         "season:": "1"}).text
        self.assertIn("Added the title to the pool.", html)
        self.assertEqual(self.keys(), ["show:tmdb:1"])

    def test_a_multi_season_add_is_one_post_and_n_arrivals(self):
        self.enter("title")
        html = self.add({"q": "title", "i": "0", "media": "show", "want": "seasons",
                         "season:1": "1", "season:3": "1"}).text
        self.assertIn("Added 2 seasons to the pool.", html)
        self.assertEqual(self.keys(), ["show:tmdb:1:s1", "show:tmdb:1:s3"])

    def test_each_ticked_season_counts_its_own_episodes_from_the_stored_list(self):
        """The list the checklist was drawn from, read back cache-only — each
        season row counts its own episodes and no seasons, and a named season
        arrives called by its name."""
        named = self.PICK._replace(seasons=[*self.PICK.seasons[:2], {
            **self.PICK.seasons[2], "name": "The Return"}])
        self.enter("title")
        with mock.patch.object(self, "PICK", named):
            self.add({"q": "title", "i": "0", "media": "show", "want": "seasons",
                      "season:1": "1", "season:3": "1"})
        board = asyncio.run(ranker.fetch_board(self.user_id, "b1"))
        drawn = sorted([*board["pool"], *(i for c in board["categories"] for i in c["items"])],
                       key=lambda item: item["season"])
        self.assertEqual(
            [(r["season"], r["season_count"], r["episode_count"], r["season_name"])
             for r in drawn],
            [(1, None, 8, None), (3, None, 10, "The Return")])
        self.assertIn(("Title 1", True), self.picks, "the list must be read from the cache")

    def test_seasons_over_a_whole_show_row_convert_it_and_add_the_rest(self):
        asyncio.run(ranker.add_titles(self.user_id, "b1", [show_ref("1")]))
        whole = self.value("SELECT id FROM tier_items")
        self.enter("title")
        html = self.add({"q": "title", "i": "0", "media": "show", "want": "seasons",
                         "season:1": "1", "season:2": "1"}).text
        self.assertIn("Added 1 season to the pool, and made 1 whole-show row into the season.",
                      html)
        self.assertEqual(self.value("SELECT season FROM tier_items WHERE id = ?", (whole,)), 2)

    def test_a_season_checklist_past_the_cap_is_refused_whole(self):
        self.enter("title")
        with mock.patch.object(ranker, "MAX_ITEMS_PER_BOARD", 1):
            html = self.add({"q": "title", "i": "0", "media": "show", "want": "seasons",
                             "season:1": "1", "season:2": "1"}).text
        self.assertIn("This would add 2, and 1 more will fit.", html)
        self.assertEqual(self.keys(), [])

    def test_nothing_ticked_is_said(self):
        self.enter("title")
        html = self.add({"q": "title", "i": "0", "media": "show", "want": "seasons"}).text
        self.assertIn("Tick at least one season to add.", html)

    def test_a_typed_result_has_a_checklist_too(self):
        store(a_record("Title 1", 1))
        row, = result_rows(self.typed("title 1"))
        html = self.checklist(**{k: str(v) for k, v in row["body"].items()
                                 if k != "media"})
        self.assertEqual([name for name, _, _ in self.boxes(html)], ["1", "2", "3"])
        self.assertEqual(self.catalogue.calls, [])

    def test_a_season_title_offers_only_its_own_season_ticked(self):
        self.enter("title")
        with mock.patch.object(self, "PICK", self.PICK._replace(named_season=2)):
            html = self.checklist(q="title", i="0")
        self.assertEqual(self.boxes(html), [("2", False, True)])

    def test_a_keyless_hit_is_keyed_by_its_season_lookup(self):
        """A service's season-title can arrive with no shared id; opening its
        checklist is the lookup that finds one, and the add reads that answer
        back from the cache rather than asking again."""
        self.catalogue.hits = [a_hit(1, ids={"simkl": 77})]
        self.enter("title")
        self.assertIn("season:2", self.checklist(q="title", i="0"))
        self.add({"q": "title", "i": "0", "media": "show", "want": "seasons", "season:2": "1"})
        self.assertEqual(self.keys(), ["show:tmdb:1:s2"])
        self.assertEqual(self.picks[-1], ("Title 1", True))

    def test_every_control_joins_the_box_s_queue(self):
        """Server tests cannot run htmx; this pins what the browser measurement
        (_title_search.html) was made with."""
        html = self.enter("title")
        summary = re.search(r"<summary [^>]*>", html).group(0)
        self.assertIn('hx-sync="#searchResults:queue last"', summary)
        self.assertIn('hx-json-enc="false"', summary)
        self.assertIn('hx-trigger="click once"', summary)
        self.assertIn('hx-sync="#searchResults:queue last"', self.checklist(q="title", i="0"))
        self.catalogue.hits.append(a_hit(9, media=Media.MOVIE))
        film = re.search(r'<div class="search-row ranker-result"[^>]*>',
                         self.enter("title", media="movie"))
        self.assertIn('hx-sync="#searchResults:queue last"', film.group(0))


class DragTests(_SearchTestCase):

    def test_a_dragged_service_result_adds_the_whole_title_and_answers_its_key(self):
        self.enter("title")
        row = result_rows(self.enter("title"))[2]
        answer = self.drag(row["data"])
        self.assertEqual(answer.json(), {"ok": True, "added": 1, "key": "show:tmdb:3"})
        self.assertEqual(self.keys(), ["show:tmdb:3"])

    def test_a_dragged_typed_result_adds_the_stored_title(self):
        store(a_record())
        row, = result_rows(self.typed("sever"))
        self.assertEqual(self.drag(row["data"]).json()["key"], "show:tmdb:95396")
        self.assertEqual(self.keys(), ["show:tmdb:95396"])

    def test_a_drag_ignores_a_want_it_sends(self):
        self.enter("title")
        self.drag({"q": "title", "i": "0", "media": "show", "want": "seasons", "season:2": "1"})
        self.assertEqual(self.keys(), ["show:tmdb:1"])

    def test_a_refused_drag_is_a_400_with_the_sentence(self):
        answer = self.drag({"t": "show:tmdb:4242", "media": "show"})
        self.assertEqual(answer.status_code, 400)
        self.assertIn("no longer stored", answer.json()["error"])


class TheRankerDefinesNoSearchOfItsOwnTests(_SearchTestCase):

    def test_no_ranker_module_defines_a_search_or_calls_a_providers_search(self):
        import ast
        from tests.support import APP_DIR
        retired = {"TraktSearchSource", "TitleSearchSource", "search_source",
                   "title_from_search", "_ref_from_search", "search_titles"}
        for path in sorted((APP_DIR / "ranker").glob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            named = {node.name for node in ast.walk(tree)
                     if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))}
            called = {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}
            with self.subTest(module=path.name):
                self.assertFalse(named & retired)
                self.assertNotIn("search_titles", called)

    def test_the_typed_half_is_the_title_stores_verb_and_enter_is_the_shared_fan_out(self):
        """Pinned by behaviour as well as by name: typing reads through
        typed_search.titles_matching, and Enter through title_search.search_catalogue."""
        seen = []

        async def _typed(query, media, limit):
            seen.append(("typed", query))
            return []
        with mock.patch.object(typed_search, "titles_matching", _typed), \
             mock.patch.object(trakt_detail, "search_titles",
                               mock.AsyncMock(side_effect=AssertionError("provider"))), \
             mock.patch.object(simkl_search, "search_titles",
                               mock.AsyncMock(side_effect=AssertionError("provider"))):
            self.typed("anything")
            self.enter("anything")
        self.assertEqual(seen, [("typed", "anything")])
        self.assertEqual([c["query"] for c in self.catalogue.calls], ["anything"])
