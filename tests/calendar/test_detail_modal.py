"""Which service describes a card, and what the modal gets back when it is not
Trakt.

WHAT THIS FILE IS FOR is the card only one service listed. The detail modal used
to take a single `?id=` meaning a Trakt id and gate on a Trakt credential, so a
title Trakt never listed refused to open — 690 of the shows in one live August
month, every one of which Simkl could have described. The refusal was correct
about the data and wrong about the question.

The rules asserted here, each of which can break silently:
  - a title with a Trakt id still gets TRAKT's answer, cast and all, even when
    the card is attributed to the other service;
  - a title only Simkl listed gets Simkl's, WITHOUT a cast and without erroring;
  - the credential gate is a question about whichever source is being asked, not
    about Trakt;
  - a card carrying no id any source recognises is refused, and the refusal names
    no service.

No network: both providers' transports are replaced with stubs that answer from
a dict, so a path nobody stubbed fails loudly rather than reaching out.
"""
from __future__ import annotations

import asyncio
from datetime import date, datetime, timezone
from unittest.mock import AsyncMock, patch

from app import db
from app.config import Settings, save_settings
from app.providers.base import Media
from app.providers.simkl import transport as simkl_transport
from app.providers.trakt import transport as trakt_transport
from tests.support import AppTestCase, ORIGIN

# What Trakt's three catalogue paths answer for the merged title below. The cast
# is the field that only exists on this side, and is what "Trakt still wins" is
# asserted through.
TRAKT_SHOW = {"title": "The Elusive Samurai", "year": 2024, "overview": "Trakt's words.",
              "status": "returning_series", "network": "Tochigi TV", "runtime": 24,
              "genres": ["anime"], "rating": 7.6, "certification": "tv-14",
              "trailer": "https://youtu.be/traktone"}
TRAKT_PEOPLE = {"cast": [{"person": {"name": "A Voice"}, "character": "Tokiyuki"}]}
TRAKT_EPISODES = [{"number": 1, "title": "Longing for Sea Bream!",
                   "first_aired": "2026-07-17T23:30:00.000Z", "rating": 8.1}]

# Simkl's catalogue record, in the shape /tv/{id} really sends — measured live
# 2026-08-07. `trailers` carries a bare YouTube id rather than a URL, and there
# is no cast field anywhere on it.
SIMKL_TITLE = {
    "title": "Somebody Knows Something", "ids": {"simkl": 3204421}, "year": 2026,
    "overview": "Simkl's words.", "status": "airing", "network": "CBC",
    "runtime": 44, "genres": ["Documentary", "Game Show"], "certification": "tv-14",
    "ratings": {"simkl": {"rating": 7.25}}, "language": "EN",
    "trailers": [{"name": "Trailer", "youtube": "simklone", "size": 1080}],
}
SIMKL_EPISODES = [
    {"episode": 1, "season": 1, "type": "episode", "title": "Pilot",
     "date": "2026-08-10T20:00:00-04:00"},
    {"episode": 2, "season": 1, "type": "episode", "title": "The Second",
     "date": "2026-08-17T20:00:00-04:00"},
    {"episode": 0, "season": 1, "type": "special", "title": "A Special",
     "date": "2026-08-01T20:00:00-04:00"},
    {"episode": 1, "season": 2, "type": "episode", "title": "Next Year",
     "date": "2027-08-10T20:00:00-04:00"},
]


class _TraktClient:
    """An httpx.AsyncClient stand-in over api.trakt.tv, answering by path."""

    def __init__(self, bodies: dict):
        self._bodies = bodies

    async def get(self, url, headers=None, timeout=None):
        path = url.split("?", 1)[0].split("api.trakt.tv/", 1)[-1]
        return _Response(self._bodies.get(path))


class _Response:
    status_code = 200
    text = ""
    headers: dict = {}

    def __init__(self, body):
        self._body = body

    def json(self):
        return self._body


def _simkl_answers(bodies: dict):
    """A stand-in for the Simkl package's cached_get, dispatching on path.

    Patched at `transport.cached_get` rather than at the two modules that call it
    so the real fetch_title / fetch_episodes bodies run — including the
    "does this parse as a title" guard, which is the thing that decides whether an
    id Simkl cannot place is an answer or a miss.
    """
    async def _get(client, settings, path, params=None, **kwargs):
        return bodies.get(path)
    return AsyncMock(side_effect=_get)


class DetailModalSourceTests(AppTestCase):
    """One HTTP call each, against /api/details as the modal really calls it."""

    def make_settings(self):
        # BOTH catalogue credentials, and neither private one. That is the live
        # instance's own shape — a Simkl client id with no Simkl token — and it is
        # what makes "the gate asks about the catalogue" a claim with teeth.
        return Settings(public_base_url=ORIGIN, trakt_client_id="tcid",
                        simkl_client_id="scid")

    def setUp(self):
        super().setUp()
        self.user_id = self.make_user("viewer", calendar_approved=True)
        self.sign_in_as(self.user_id)

    def _get(self, query: str, *, trakt=None, simkl=None):
        trakt_client = _TraktClient(trakt if trakt is not None else {})
        with patch.object(trakt_transport, "shared_client", return_value=trakt_client), \
             patch.object(simkl_transport, "cached_get",
                          new=_simkl_answers(simkl if simkl is not None else {})):
            return self.client.get(f"/api/details?{query}")

    # -- a title both services listed --------------------------------------

    def _both(self):
        return self._get(
            "media=show&trakt=203330&simkl=2601798&season=2",
            trakt={"shows/203330": TRAKT_SHOW, "shows/203330/people": TRAKT_PEOPLE,
                   "shows/203330/seasons/2": TRAKT_EPISODES},
            # Deliberately answerable too: if the route ever preferred the other
            # side, this test would still pass on an empty Simkl stub and prove
            # nothing.
            simkl={"tv/2601798": SIMKL_TITLE, "tv/episodes/2601798": SIMKL_EPISODES},
        )

    def test_a_title_with_a_trakt_id_still_gets_trakts_answer(self):
        """Unchanged from before this existed. Trakt's per-title answer is the
        larger one — a cast, a per-episode air date, a per-episode rating — and a
        merged card must not lose those because the other service also had a
        page for it."""
        body = self._both().json()
        self.assertEqual(body["source"], "trakt")
        self.assertEqual(body["overview"], "Trakt's words.")
        self.assertEqual(body["cast"][0]["name"], "A Voice")
        self.assertEqual(body["episodes"][0]["title"], "Longing for Sea Bream!")
        self.assertEqual(body["episodes"][0]["rating"], 8.1)

    def test_the_preference_is_trakts_id_and_not_the_cards_attribution(self):
        """The card the author tested against is a Simkl-attributed card for a
        title Trakt also lists. Nothing about which service the CARD wears
        reaches this route — only the ids do — so a flip on the page cannot
        change who answers the modal."""
        self.assertEqual(self._both().json()["source"], "trakt")

    # -- a title only Simkl listed -----------------------------------------

    def _simkl_only(self, query="media=show&simkl=3204421&season=1"):
        return self._get(query, simkl={"tv/3204421": SIMKL_TITLE,
                                       "tv/episodes/3204421": SIMKL_EPISODES})

    def test_a_simkl_only_title_opens_on_simkls_answer(self):
        resp = self._simkl_only()
        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()
        self.assertEqual(body["source"], "simkl")
        self.assertEqual(body["overview"], "Simkl's words.")
        self.assertEqual(body["network"], "CBC")
        self.assertEqual(body["rating"], 7.2)
        self.assertEqual(body["status"], "Airing")

    def test_it_renders_without_a_cast_and_without_erroring(self):
        """The whole point of the brief. Simkl publishes no cast anywhere this
        app can reach, so the key is present and empty — which is the same thing
        the renderer already handles for a Trakt title served from a cold
        cache — rather than absent, which would read as a template bug."""
        body = self._simkl_only().json()
        self.assertEqual(body["cast"], [])
        self.assertIn("cast", body)

    def test_every_key_the_renderer_reads_is_present(self):
        """One client-side renderer draws both sources' answers. A key only one
        of them sends is the failure this pins, and it is invisible until
        somebody opens the right card."""
        trakt_body = self._both().json()
        simkl_body = self._simkl_only().json()
        for key in trakt_body:
            if key in ("ok", "source"):
                continue
            with self.subTest(key=key):
                self.assertIn(key, simkl_body)

    def test_simkls_genres_are_drawn_the_way_trakts_are(self):
        """The stored extraction slugs a genre so one filter spec matches both
        services; the modal draws chips a person reads, so they come back as
        words."""
        self.assertEqual(self._simkl_only().json()["genres"],
                         ["Documentary", "Game Show"])

    def test_a_bare_youtube_id_arrives_as_a_watchable_url(self):
        """Trakt sends a finished URL and Simkl sends an id. Reconciling that in
        the provider is what lets `trailer` mean one thing to the renderer."""
        self.assertEqual(self._simkl_only().json()["trailer"],
                         "https://www.youtube.com/watch?v=simklone")

    def test_the_episode_list_is_this_season_without_its_specials(self):
        body = self._simkl_only().json()
        self.assertEqual([ep["number"] for ep in body["episodes"]], [1, 2])
        self.assertEqual(body["episodes"][0]["air_display"], "10 Aug 2026")
        # Simkl publishes no per-episode rating; the key is still there.
        self.assertIsNone(body["episodes"][0]["rating"])

    def test_a_listing_with_no_season_is_answered_with_the_only_one_there_is(self):
        """Simkl's calendar files omit the season on anime — 69 of 690 Simkl-only
        show entries on a live instance — and a title whose episode list holds one
        season leaves nothing to choose between. The season that was ANSWERED
        comes back, because the modal draws its heading from it."""
        one_season = [ep for ep in SIMKL_EPISODES if ep["season"] == 1]
        resp = self._get("media=show&simkl=3204421",
                         simkl={"tv/3204421": SIMKL_TITLE,
                                "tv/episodes/3204421": one_season})
        body = resp.json()
        self.assertEqual(body["season"], 1)
        self.assertEqual([ep["number"] for ep in body["episodes"]], [1, 2])

    def test_a_listing_with_no_season_and_several_to_choose_from_picks_none(self):
        """The other half, and the reason the rule above is a reading rather than
        a guess: with two seasons in the list there IS something to choose
        between, so nothing is chosen and the modal draws no episode section."""
        body = self._get("media=show&simkl=3204421",
                         simkl={"tv/3204421": SIMKL_TITLE,
                                "tv/episodes/3204421": SIMKL_EPISODES}).json()
        self.assertIsNone(body["season"])
        self.assertEqual(body["episodes"], [])

    def test_a_movie_costs_no_episode_lookup(self):
        body = self._get("media=movie&simkl=3207691",
                         simkl={"movies/3207691": {**SIMKL_TITLE, "type": "movie"}}).json()
        self.assertEqual(body["episodes"], [])
        self.assertEqual(body["overview"], "Simkl's words.")

    # -- the gate ----------------------------------------------------------

    def test_the_gate_asks_about_the_source_being_asked(self):
        """The old gate was Trakt's, for every request. A title needing no Trakt
        credential must not be refused over one."""
        save_settings(Settings(public_base_url=ORIGIN, simkl_client_id="scid"))
        self.assertEqual(self._simkl_only().status_code, 200)

    def test_a_source_this_instance_cannot_ask_is_stepped_over(self):
        """A merged title on an instance with no Trakt client id falls through to
        the service that CAN answer, rather than refusing on the preferred one."""
        save_settings(Settings(public_base_url=ORIGIN, simkl_client_id="scid"))
        body = self._get("media=show&trakt=203330&simkl=3204421&season=1",
                         simkl={"tv/3204421": SIMKL_TITLE,
                                "tv/episodes/3204421": SIMKL_EPISODES}).json()
        self.assertEqual(body["source"], "simkl")

    def test_nothing_configured_still_draws_what_this_instance_already_holds(self):
        """A CREDENTIAL IS NEEDED TO FETCH A DESCRIPTION, NOT TO READ ONE BACK.
        With no source reachable the modal falls through to a stored-only read,
        which makes no request and needs nothing configured — so a title this
        instance has described before still opens. Refusing instead threw away
        data it was holding, which is the same answer the roster's own rows
        stopped giving.

        (The stub here replaces the transport's `cached_get` outright, so it
        answers whether or not the read was cache-only; what this pins is that
        the route no longer refuses BEFORE looking.)"""
        save_settings(Settings(public_base_url=ORIGIN))
        resp = self._simkl_only()
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(resp.json()["source"], "simkl")

    def test_nothing_configured_and_nothing_stored_is_a_refusal_naming_no_service(self):
        """The other half, and the reason the fallback is not simply "always
        200": a modal opened on empty fields is a blank card the reader cannot
        act on, so an answer with no title, no overview and no episodes behind it
        is still the honest refusal it always was."""
        save_settings(Settings(public_base_url=ORIGIN))
        resp = self._get("media=show&simkl=3204421&season=1", simkl={})
        self.assertEqual(resp.status_code, 404)
        self.assertNotIn("Trakt", resp.json()["error"])
        self.assertNotIn("Simkl", resp.json()["error"])

    def test_a_card_carrying_no_id_at_all_is_refused(self):
        resp = self._get("media=show&season=1")
        self.assertEqual(resp.status_code, 404)

    def test_a_namespace_no_source_issues_is_not_looked_up(self):
        """`?tmdb=` is a real id and no registered source can be asked by it.
        Reading only the names the registry knows is what stops a query string
        naming a lookup this app does not have."""
        self.assertEqual(self._get("media=show&tmdb=222623").status_code, 404)


# The window the public tests below share: one Simkl-only airing, in a month a
# request has to name. Built as app/providers/simkl/calendar.py's own record
# builder produces one, because what the share page will admit is decided from
# the ids a STORED record carries.
SHARE_MONTH = date(2026, 7, 15)
SHARE_VIEW = "year=2026&month=7"
SIMKL_AIR_TS = datetime(2026, 7, 15, 20, 0, tzinfo=timezone.utc).timestamp()


class SimklOnlyOnAPublicShareLinkTests(AppTestCase):
    """The same title through the public page's own details endpoint.

    The two are documented as showing the same content, and they share the code
    that decides who describes a title — this is what keeps that true. The
    difference is what each may ASK ABOUT: the public one describes only a title
    the owner's cached month already shows, which is what stops a public URL
    turning a stranger's id into a call on the owner's credential.

    THIS FILE'S HALF OF THAT IS THE SIMKL TRANSPORT. The rule is asserted end to
    end against Trakt in tests/calendar/test_share_pages.py; a bound that held for
    one service's ids and not the other's would be no bound at all, and the two
    packages share no code below the port.
    """

    def make_settings(self):
        return Settings(public_base_url=ORIGIN, simkl_client_id="scid")

    def setUp(self):
        super().setUp()
        from app.calendar import cache as calendar_cache, share_links
        from app.endpoints import get_endpoint
        from app.providers.base import Record, Source

        self.user_id = self.make_user("shareowner", calendar_approved=True)
        self.token = asyncio.run(share_links.get_or_create(self.user_id))["token"]
        asyncio.run(share_links.set_enabled(self.user_id, "token", True))
        record = Record(
            source=Source.SIMKL, media=Media.SHOW, id="3204421",
            ids={"simkl": 3204421, "slug": "somebody-knows-something"},
            detail_url="https://simkl.com", title="Somebody Knows Something",
            air_ts=SIMKL_AIR_TS, season=1, episode_number=1, episode_label="S01E01",
            enriched=False,
        )
        asyncio.run(calendar_cache.store_window(
            get_endpoint("shows/new").key, calendar_cache.window_start(SHARE_MONTH),
            [record], 600, db.now(), sources=["simkl"], asked=["simkl"]))

    def _no_network(self):
        class _Boom:
            async def get(self, *a, **k):
                raise AssertionError("a public request must not call a source")
        return patch.object(simkl_transport, "catalog_client", return_value=_Boom())

    def _details(self, query):
        return self.client.get(f"/s/{self.token}/details?{query}&{SHARE_VIEW}")

    def test_a_simkl_only_card_on_a_public_page_serves_what_is_cached(self):
        from app import cache
        # Seeded through the transport's own key builder rather than a spelled-out
        # URL: the address a stored answer lives at is that function's to state,
        # and a copy here would go on passing while the real one moved.
        asyncio.run(cache.set(simkl_transport.cache_key("tv/3204421"), SIMKL_TITLE))
        asyncio.run(cache.set(simkl_transport.cache_key("tv/episodes/3204421"),
                              SIMKL_EPISODES))
        with self._no_network():
            resp = self._details("media=show&simkl=3204421&season=1")
        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()
        self.assertEqual(body["source"], "simkl")
        self.assertEqual(body["overview"], "Simkl's words.")
        self.assertEqual(body["cast"], [])

    def test_an_unknown_id_beside_a_known_one_is_dropped_rather_than_preferred(self):
        """WHAT A HAND-EDITED URL ACTUALLY DOES, and it is the case a bound that
        only CHECKED would get wrong.

        A request carries one id per service and `detail_source.choose` picks
        between them, preferring Trakt wherever it has one. Editing the Trakt id
        to something the link does not show must not hand the whole request to
        that id — nor refuse the request outright, because the OTHER id on it is
        a title this link really does publish. The unknown one is dropped before
        the choice, so the answer comes from whoever still recognises the card.

        Both credentials are configured here on purpose: without the narrowing
        the made-up Trakt id would be preferred and looked up, which is what the
        stand-in fails on.
        """
        from app import cache
        save_settings(Settings(public_base_url=ORIGIN, simkl_client_id="scid",
                               trakt_client_id="tcid"))
        asyncio.run(cache.set(simkl_transport.cache_key("tv/3204421"), SIMKL_TITLE))
        asyncio.run(cache.set(simkl_transport.cache_key("tv/episodes/3204421"),
                              SIMKL_EPISODES))

        class _Boom:
            async def get(self, *a, **k):
                raise AssertionError(
                    "an id this link does not show was looked up because it "
                    "rode along with one that it does")

        with self._no_network(), \
             patch.object(trakt_transport, "shared_client", return_value=_Boom()):
            resp = self._details("media=show&trakt=999999&simkl=3204421&season=1")
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(resp.json()["source"], "simkl")
        self.assertEqual(resp.json()["overview"], "Simkl's words.")

    def test_a_simkl_title_this_link_does_not_show_is_never_looked_up(self):
        """An id nobody put on the owner's calendar, asked of the other service's
        transport. Nothing is cached behind it either, so an unbound endpoint
        would go and ask — which is exactly what the stand-in fails on."""
        with self._no_network():
            resp = self._details("media=show&simkl=999999&season=1")
        self.assertEqual(resp.status_code, 404)
        self.assertFalse(resp.json()["ok"])
