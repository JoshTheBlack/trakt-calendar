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

THE ROUTE ANSWERS WITH THE PANEL, NOT WITH A PAYLOAD (templates/_details.html),
so what a source said is asserted through what the reader is shown. That is the
stronger claim of the two — a field the provider fills in and the template never
draws is a field nobody sees — and the one place it cannot reach is the KEY SET
itself, which is asserted against the ports directly below.

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
    """One HTTP call each, against the details fragment as a card really asks it."""

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
            return self.client.get(f"/calendar/fragments/details?{query}")

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
        html = self._both().text
        self.assertIn("Trakt&#39;s words.", html)
        self.assertIn("A Voice", html)
        self.assertIn("Longing for Sea Bream!", html)
        self.assertIn("⭐ 8.1", html)

    def test_the_preference_is_trakts_id_and_not_the_cards_attribution(self):
        """The card the author tested against is a Simkl-attributed card for a
        title Trakt also lists. Nothing about which service the CARD wears
        reaches this route — only the ids do — so a flip on the page cannot
        change who answers the modal."""
        html = self._both().text
        self.assertIn("Trakt&#39;s words.", html)
        self.assertNotIn("Simkl&#39;s words.", html)

    # -- a title only Simkl listed -----------------------------------------

    def _simkl_only(self, query="media=show&simkl=3204421&season=1"):
        return self._get(query, simkl={"tv/3204421": SIMKL_TITLE,
                                       "tv/episodes/3204421": SIMKL_EPISODES})

    def test_a_simkl_only_title_opens_on_simkls_answer(self):
        resp = self._simkl_only()
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertIn("Simkl&#39;s words.", resp.text)
        self.assertIn("📡 CBC", resp.text)
        self.assertIn("⭐ 7.2", resp.text)
        self.assertIn("Airing", resp.text)

    def test_it_renders_without_a_cast_and_without_erroring(self):
        """The whole point of the brief. Simkl publishes no cast anywhere this
        app can reach, so the key is present and empty — which is the same thing
        the renderer already handles for a Trakt title served from a cold
        cache — rather than absent, which would read as a template bug."""
        html = self._simkl_only().text
        self.assertNotIn("Cast", html)
        self.assertIn("Simkl&#39;s words.", html)

    def test_every_key_the_renderer_reads_is_present(self):
        """ONE template draws both sources' answers, so a key only one of them
        sends is a field that silently never appears on the other's cards.

        Asked of the PORTS rather than through the route, because this is the one
        claim in this file the rendered panel cannot carry: a key the template
        happens not to draw today would go unmissed, and the point is that the two
        answers have the same shape whatever is drawn from it. It is the same rule
        tests/providers/test_protocol_conformance.py holds for an EMPTY answer;
        what this adds is a pair of answers with real data in them, where a source
        can leave out a key it has nothing to say for."""
        from app.config import load_settings
        from app.providers import detail_source
        from app.providers.base import Source

        trakt_client = _TraktClient({"shows/203330": TRAKT_SHOW,
                                     "shows/203330/people": TRAKT_PEOPLE,
                                     "shows/203330/seasons/2": TRAKT_EPISODES})
        with patch.object(trakt_transport, "shared_client", return_value=trakt_client), \
             patch.object(simkl_transport, "cached_get",
                          new=_simkl_answers({"tv/3204421": SIMKL_TITLE,
                                              "tv/episodes/3204421": SIMKL_EPISODES})):
            settings = load_settings()
            trakt_body = asyncio.run(detail_source.fetch(
                settings, Source.TRAKT, "show", "203330", 2))
            simkl_body = asyncio.run(detail_source.fetch(
                settings, Source.SIMKL, "show", "3204421", 1))
        for key in trakt_body:
            with self.subTest(key=key):
                self.assertIn(key, simkl_body)

    def test_simkls_genres_are_drawn_the_way_trakts_are(self):
        """The stored extraction slugs a genre so one filter spec matches both
        services; the modal draws chips a person reads, so they come back as
        words."""
        html = self._simkl_only().text
        self.assertIn(">Documentary</span>", html)
        self.assertIn(">Game Show</span>", html)

    def test_a_trailer_is_embedded_when_it_names_a_video_and_absent_when_not(self):
        """Trakt sends a finished URL and Simkl sends an id. Reconciling that in
        the provider is what lets a video mean one thing to the panel.

        THE FIXTURE'S VALUE IS NOT A VIDEO ID — eight characters, where YouTube
        issues eleven — and it used to be wrapped in a watch URL anyway and
        offered as a "Watch trailer" link to YouTube's error page. Measured on a
        live instance, 38 titles did that. A value naming no video now offers no
        trailer at all; a real id embeds."""
        html = self._simkl_only().text
        self.assertNotIn("youtube.com/watch", html)
        self.assertNotIn("trailer-embed", html)
        real = {**SIMKL_TITLE,
                "trailers": [{"name": "Trailer", "youtube": "dQw4w9WgXcQ"}]}
        html = self._get("media=show&simkl=3204421&season=1",
                         simkl={"tv/3204421": real,
                                "tv/episodes/3204421": SIMKL_EPISODES}).text
        self.assertIn("https://www.youtube-nocookie.com/embed/dQw4w9WgXcQ", html)

    # -- videos, pooled from both ------------------------------------------

    def _pooled(self, path="/calendar/fragments/details"):
        """A title both services list, each with a video of its own: Trakt a
        trailer and Simkl a teaser. Returns the page and every Simkl path asked."""
        simkl = _simkl_answers({"tv/2601798": {**SIMKL_TITLE, "trailers": [
            {"name": "Official Teaser", "youtube": "4PPodH3i_7k"}]},
            "tv/episodes/2601798": SIMKL_EPISODES})
        trakt_client = _TraktClient({
            "shows/203330": TRAKT_SHOW, "shows/203330/people": TRAKT_PEOPLE,
            "shows/203330/seasons/2": TRAKT_EPISODES,
            "shows/203330/videos": [{"title": "Official Trailer", "site": "youtube",
                                     "url": "https://youtube.com/watch?v=dQw4w9WgXcQ",
                                     "type": "trailer", "official": True}]})
        with patch.object(trakt_transport, "shared_client", return_value=trakt_client), \
             patch.object(simkl_transport, "cached_get", new=simkl):
            resp = self.client.get(f"{path}?media=show&trakt=203330&simkl=2601798&season=2")
        self.assertEqual(resp.status_code, 200, resp.text)
        return resp.text, [call.args[2] for call in simkl.call_args_list]

    def test_a_panel_asks_the_other_service_for_its_videos(self):
        """Trakt describes the title; Simkl is asked for its catalogue record,
        which is where its video list lives, and Trakt's trailer still plays
        first because a trailer outranks a teaser."""
        html, simkl_asked = self._pooled()
        self.assertIn("Trakt&#39;s words.", html)
        self.assertIn("tv/2601798", simkl_asked)
        self.assertIn('src="https://www.youtube-nocookie.com/embed/dQw4w9WgXcQ"', html)

    def test_a_season_tab_asks_nobody_for_videos(self):
        """A tab draws that season's rows and no player, so it pools nothing."""
        html, simkl_asked = self._pooled("/calendar/fragments/details/season")
        self.assertIn("Longing for Sea Bream!", html)
        self.assertEqual(simkl_asked, [])

    def test_the_episode_list_is_this_season_without_its_specials(self):
        html = self._simkl_only().text
        self.assertIn("E01", html)
        self.assertIn("E02", html)
        self.assertNotIn("A Special", html)
        self.assertIn("10 Aug 2026", html)
        # Simkl publishes no per-episode rating, so no row carries a star.
        self.assertNotIn("ep-rating", html)

    def test_a_listing_with_no_season_is_answered_with_the_only_one_there_is(self):
        """Simkl's calendar files omit the season on anime — 69 of 690 Simkl-only
        show entries on a live instance — and a title whose episode list holds one
        season leaves nothing to choose between. The season that was ANSWERED
        comes back, because the modal draws its heading from it."""
        one_season = [ep for ep in SIMKL_EPISODES if ep["season"] == 1]
        html = self._get("media=show&simkl=3204421",
                         simkl={"tv/3204421": SIMKL_TITLE,
                                "tv/episodes/3204421": one_season}).text
        self.assertIn("Season 1 Episodes", html)
        self.assertIn("E01", html)
        self.assertIn("E02", html)

    def test_a_listing_with_no_season_and_several_to_choose_from_picks_none(self):
        """The other half, and the reason the rule above is a reading rather than
        a guess: with two seasons in the list there IS something to choose
        between, so nothing is chosen and the modal draws no episode section."""
        html = self._get("media=show&simkl=3204421",
                         simkl={"tv/3204421": SIMKL_TITLE,
                                "tv/episodes/3204421": SIMKL_EPISODES}).text
        self.assertNotIn("Episodes", html)
        self.assertIn("Simkl&#39;s words.", html)

    def test_a_movie_costs_no_episode_lookup(self):
        html = self._get("media=movie&simkl=3207691",
                         simkl={"movies/3207691": {**SIMKL_TITLE, "type": "movie"}}).text
        self.assertNotIn("Episodes", html)
        self.assertIn("Simkl&#39;s words.", html)

    # -- linking back ------------------------------------------------------

    def test_the_panel_links_back_to_every_service_the_card_named(self):
        """Both services ask that their data link to the page for the specific
        item, and the tracker has no other way to reach one at all. The ids come
        from the CARD, not from the answer: a Trakt-described title's payload
        carries no Simkl id, so a panel working these out for itself could only
        ever offer one of the two buttons."""
        html = self._both().text
        self.assertIn('href="https://trakt.tv/shows/203330"', html)
        self.assertIn('href="https://simkl.com/tv/2601798"', html)

    def test_a_link_is_spelled_with_the_services_own_name_where_the_card_has_it(self):
        """THE NAME TRAVELS ON THE REQUEST, namespaced. Both services would rather
        be sent the readable name than the id, which costs them a lookup and a
        redirect — and the name has to say WHOSE, because the two do not agree
        about what a title is called."""
        html = self._get(
            "media=show&trakt=203330&simkl=2601798&season=2"
            "&trakt_slug=the-elusive-samurai&simkl_slug=elusive-samurai",
            trakt={"shows/203330": TRAKT_SHOW, "shows/203330/people": TRAKT_PEOPLE,
                   "shows/203330/seasons/2": TRAKT_EPISODES}).text
        self.assertIn('href="https://trakt.tv/shows/the-elusive-samurai"', html)
        self.assertIn('href="https://simkl.com/tv/2601798/elusive-samurai"', html)

    def test_a_name_never_decides_who_is_asked(self):
        """A slug is caller-supplied and only spells a link. Sending one for a
        service whose id the card did not carry must not conjure a button for it,
        and must not reach the who-answers decision either."""
        html = self._simkl_only(
            "media=show&simkl=3204421&season=1&trakt_slug=not-a-real-show").text
        self.assertIn("Simkl&#39;s words.", html)
        self.assertNotIn("trakt.tv", html)

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
        html = self._get("media=show&trakt=203330&simkl=3204421&season=1",
                         simkl={"tv/3204421": SIMKL_TITLE,
                                "tv/episodes/3204421": SIMKL_EPISODES}).text
        self.assertIn("Simkl&#39;s words.", html)

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
        self.assertIn("Simkl&#39;s words.", resp.text)

    def test_nothing_configured_and_nothing_stored_is_a_refusal_naming_no_service(self):
        """The other half, and the reason the fallback is not simply "always
        200": a modal opened on empty fields is a blank card the reader cannot
        act on, so an answer with no title, no overview and no episodes behind it
        is still the honest refusal it always was."""
        save_settings(Settings(public_base_url=ORIGIN))
        resp = self._get("media=show&simkl=3204421&season=1", simkl={})
        self.assertIn("Nothing here can describe this item.", resp.text)
        self.assertNotIn("Trakt", resp.text)
        self.assertNotIn("Simkl", resp.text)

    def test_a_card_carrying_no_id_at_all_is_refused(self):
        self.assertIn("Nothing here can describe this item.",
                      self._get("media=show&season=1").text)

    def test_a_namespace_no_source_issues_is_not_looked_up(self):
        """`?tmdb=` is a real id and no registered source can be asked by it.
        Reading only the names the registry knows is what stops a query string
        naming a lookup this app does not have."""
        self.assertIn("Nothing here can describe this item.",
                      self._get("media=show&tmdb=222623").text)


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
        self.assertIn("Simkl&#39;s words.", resp.text)
        self.assertNotIn("Cast", resp.text)

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
        self.assertIn("Simkl&#39;s words.", resp.text)

    def test_a_simkl_title_this_link_does_not_show_is_never_looked_up(self):
        """An id nobody put on the owner's calendar, asked of the other service's
        transport. Nothing is cached behind it either, so an unbound endpoint
        would go and ask — which is exactly what the stand-in fails on."""
        with self._no_network():
            resp = self._details("media=show&simkl=999999&season=1")
        self.assertIn("No source can describe this title", resp.text)
        self.assertNotIn("Simkl&#39;s words.", resp.text)


class OpeningAOneServiceCardAsksTheOtherTests(AppTestCase):
    """A premiere card only Trakt's calendar draws: its panel carries a request to
    ask Simkl about it, fired once the panel is open, and that request's answer
    redraws the card with Simkl's airing joined to it.

    The panel itself is unchanged and answers at once — the ask is after it, so
    opening a card never waits on a lookup."""

    KEY = "show:tmdb:777"
    PANEL = ("media=show&trakt=203330&season=1&key=show%3Atmdb%3A777"
             "&endpoint={endpoint}&year=2026&month=7")

    make_settings = DetailModalSourceTests.make_settings
    _get = DetailModalSourceTests._get

    def setUp(self):
        super().setUp()
        user_id = self.make_user("viewer", calendar_approved=True)
        # Simkl's own search/redirect now require THIS viewer's own token (Simkl
        # AUTH V2 refuses them without one) — an instance client id alone is no longer enough to
        # offer "ask Simkl", so the ask this class is about needs a real link.
        self.link_identity(user_id, "simkl", 5501, "simkl-token")
        self.sign_in_as(user_id)
        from app.calendar import cache as calendar_cache
        from app.providers.base import Record, Source
        # Trakt's premiere of season 1 alone, in a stored week of July 2026.
        asyncio.run(calendar_cache.store_window(
            "shows/premieres", calendar_cache.window_start(date(2026, 7, 8)),
            [Record(source=Source.TRAKT, media=Media.SHOW, id="203330",
                    ids={"trakt": "203330", "tmdb": 777}, detail_url="",
                    title="The Elusive Samurai",
                    air_ts=datetime(2026, 7, 8, 20, tzinfo=timezone.utc).timestamp(),
                    season=1, episode_number=1, episode_label="S01E01", enriched=True)],
            10**9, 10**10, sources=["trakt", "simkl"], asked=["trakt", "simkl"]))

    def _panel(self, endpoint="shows/premieres"):
        return self._get(self.PANEL.format(endpoint=endpoint),
                         trakt={"shows/203330": TRAKT_SHOW,
                                "shows/203330/people": TRAKT_PEOPLE,
                                "shows/203330/seasons/1": TRAKT_EPISODES}).text

    def test_a_one_service_premiere_card_asks_once_the_panel_is_open(self):
        html = self._panel()
        self.assertIn('hx-post="/api/calendar/complete-card?endpoint=shows%2Fpremieres'
                      '&amp;year=2026&amp;month=7"', html)
        self.assertIn('hx-trigger="load"', html)
        self.assertIn("Trakt&#39;s words.", html, "the panel itself was not drawn")

    def test_an_all_episodes_card_does_not_ask(self):
        """Only a premiere can be built from the other service's record."""
        self.assertNotIn("complete-card", self._panel(endpoint="shows"))

    def test_the_answer_redraws_the_card_with_the_other_services_airing(self):
        from types import SimpleNamespace
        from app.providers.base import Record, Source

        async def _find(settings, media, ids):
            return {"simkl": "4242", "tmdb": 777}

        async def _seasons(settings, source_id, media):
            return SimpleNamespace(seasons=[{"season": 1, "first_aired": "2026-07-08"}])

        async def _records(settings, source_id, media, moments):
            return [Record(source=Source.SIMKL, media=media, id=str(source_id),
                           ids={"simkl": str(source_id), "tmdb": 777}, detail_url="",
                           title="The Elusive Samurai",
                           air_ts=datetime(2026, 7, 8, tzinfo=timezone.utc).timestamp(),
                           date_only=True, season=season, episode_number=1,
                           episode_label="S01E01", enriched=True)
                    for season, _when in moments]

        port = SimpleNamespace(fetch_seasons=_seasons, records_for=_records,
                               fetch_details=AsyncMock(return_value={}))
        with patch("app.providers.for_catalogue_search", return_value=[
                    (Source.SIMKL, SimpleNamespace(find_by_shared_id=_find))]), \
             patch("app.providers.get", return_value=SimpleNamespace(detail_port=port)):
            resp = self.client.post(
                "/api/calendar/complete-card?endpoint=shows%2Fpremieres&year=2026&month=7",
                json={"key": self.KEY, "season": 1})
            again = self.client.post(
                "/api/calendar/complete-card?endpoint=shows%2Fpremieres&year=2026&month=7",
                json={"key": self.KEY, "season": 1})
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertIn('hx-swap-oob="outerHTML:.card[data-airing=', resp.text)
        self.assertIn("simkl=4242", resp.text, "the redrawn card does not carry Simkl's id")
        self.assertEqual(again.text, "", "a card two services draw was asked about again")

    def _complete(self, body):
        from types import SimpleNamespace
        from app.providers.base import Record, Source

        async def _find(settings, media, ids):
            return {"simkl": "4242", "tmdb": 777}

        async def _seasons(settings, source_id, media):
            return SimpleNamespace(seasons=[{"season": 1, "first_aired": "2026-07-08"}])

        async def _records(settings, source_id, media, moments):
            return [Record(source=Source.SIMKL, media=media, id=str(source_id),
                           ids={"simkl": str(source_id), "tmdb": 777}, detail_url="",
                           title="The Elusive Samurai",
                           air_ts=datetime(2026, 7, 8, tzinfo=timezone.utc).timestamp(),
                           date_only=True, season=season, episode_number=1,
                           episode_label="S01E01", enriched=True)
                    for season, _when in moments]

        port = SimpleNamespace(fetch_seasons=_seasons, records_for=_records,
                               fetch_details=AsyncMock(return_value={}))
        with patch("app.providers.for_catalogue_search", return_value=[
                    (Source.SIMKL, SimpleNamespace(find_by_shared_id=_find))]), \
             patch("app.providers.get", return_value=SimpleNamespace(detail_port=port)):
            return self.client.post(
                "/api/calendar/complete-card?endpoint=shows%2Fpremieres&year=2026&month=7",
                json={"key": self.KEY, "season": 1, **body}).text

    def test_the_redraw_replaces_the_card_as_the_page_drew_it(self):
        """The joining service can bring the time, and the time is part of a
        card's airing digest — MEASURED on a live copy, Dexter: Original Sin's
        card was redrawn under a new airing, the answer addressed a card the page
        did not have, and the page kept the one-service card until a reload. The
        panel names the airing the card was drawn under and the answer replaces
        that."""
        drawn = "0123456789abcdef"
        self.assertIn(f"hx-swap-oob=\"outerHTML:.card[data-airing='{drawn}']\"",
                      self._complete({"airing": drawn}))

    def test_an_airing_reference_is_only_ever_a_digest(self):
        """It goes into a selector, so anything but the digest's own shape is
        dropped and the card is addressed by its own airing."""
        answer = self._complete({"airing": "x'] , .card[data-id"})
        self.assertNotIn("x']", answer)
        self.assertIn('hx-swap-oob="outerHTML:.card[data-airing=', answer)

    def test_the_panel_passes_the_cards_airing_on(self):
        html = self._get(self.PANEL.format(endpoint="shows/premieres") + "&airing=0123456789abcdef",
                         trakt={"shows/203330": TRAKT_SHOW,
                                "shows/203330/people": TRAKT_PEOPLE,
                                "shows/203330/seasons/1": TRAKT_EPISODES}).text
        self.assertIn('"airing": "0123456789abcdef"', html)
        # And the card itself carries it on its details address, as drawn.
        import re
        with patch("app.calendar.cache.fetch_window_records",
                   AsyncMock(return_value=([], ["trakt", "simkl"], []))):
            page = self.client.get("/calendar?endpoint=shows%2Fpremieres&year=2026&month=7").text
        card = re.search(r'<div class="card[^"]*" data-id="show:tmdb:777" data-airing="([0-9a-f]{16})"'
                         r'[^>]*data-details-open="([^"]+)"', page)
        self.assertIn(f"airing={card.group(1)}", card.group(2))
