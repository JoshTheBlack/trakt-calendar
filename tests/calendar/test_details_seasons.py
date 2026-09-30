"""The season picker on a card's details panel, and this viewer's own ticks on it.

TWO ADDITIONS TO ONE PANEL, and they are here together because they are the two
halves of "what a calendar card can tell you about a season".

  THE PICKER COSTS NOTHING TO OFFER. A card for season 4 is a card for a title
  with three earlier ones behind it, so the strip is drawn from what the card
  already knew and no lookup happens to build it. Each tab fetches its own rows
  on its first press — hx-get plus hx-trigger="click once", the pattern the
  ranker's tiers and the calendar's day blocks already use — so an unopened
  season costs nothing at all.

  THE TICKS ARE A DATABASE READ AND NOT A SERVICE CALL. The tracker files a
  person's watched episodes under the shared identity waterfall
  (app/providers/base.py's MATCH_SOURCES), which is exactly what a calendar card
  resolves to and carries as its `key` — so the calendar showing them costs no
  call, no token and no id translation. A card for a show this viewer has never
  tracked draws none, which is the difference from the tracker's own panel: there
  a row is a statement that somebody is following the show, here a card is just
  something that is on television.

No network: the transport's pooled client is a stub, so a path nobody stubbed
fails loudly rather than reaching out.
"""
from __future__ import annotations

import asyncio
import json
from urllib.parse import parse_qs, urlsplit

from unittest.mock import patch

from app import db
from app.config import Settings
from app.providers.trakt import transport
from tests.support import AppTestCase, DetailsPanel, ORIGIN

SHOW = {"title": "The Agency", "year": 2026, "overview": "Spies.",
        "status": "returning series", "network": "Paramount+", "runtime": 50,
        "genres": ["drama"], "rating": 8.0}
# The TITLE's videos. Neither service lists any per season, so these are the only
# ones there are and the panel draws one player for them.
VIDEOS = [{"title": "Official Trailer", "url": "https://youtube.com/watch?v=dQw4w9WgXcQ",
           "site": "youtube", "type": "trailer", "official": True}]
PEOPLE = {"cast": [{"person": {"name": "Michael"}, "character": "Martian"}]}


def _episodes(season: int):
    return [{"number": n, "title": f"S{season} Ep {n}",
             "first_aired": "2026-08-01T20:00:00.000Z"} for n in range(1, 6)]


class _Response:
    status_code = 200
    text = ""
    headers: dict = {}

    def __init__(self, body):
        self._body = body

    def json(self):
        return self._body


# What `shows/{id}/seasons?extended=full` answers: one entry per season, each
# with its own name and words. Three seasons, so a card for season 3 has two
# earlier ones behind it.
SEASONS = [{"number": n, "title": f"The {['First','Second','Third'][n - 1]} One",
            "overview": f"What season {n} is about.", "episode_count": 5,
            "aired_episodes": 5}
           for n in range(1, 4)]


def _season_cast(season: int):
    return {"cast": [{"person": {"name": f"S{season} Actor"},
                      "character": f"S{season} Character"}]}


class _CatalogueClient:
    """Answers the catalogue paths the panel reads, for any season.

    THE SEASON PATHS ANSWER DIFFERENTLY PER SEASON, which is the whole point of
    the tests below: a stub that answered one cast for every season could not
    tell a season-scoped panel from the show-scoped one it replaced.
    """

    def __init__(self, seasons=None):
        self._seasons = SEASONS if seasons is None else seasons

    async def get(self, url, headers=None, timeout=None):
        path = url.split("?", 1)[0].split("api.trakt.tv/", 1)[-1]
        if path == "shows/7":
            return _Response(SHOW)
        if path == "shows/7/people":
            return _Response(PEOPLE)
        if path == "shows/7/videos":
            return _Response(VIDEOS)
        if path == "shows/7/seasons":
            return _Response(self._seasons)
        if path.endswith("/people") and path.startswith("shows/7/seasons/"):
            return _Response(_season_cast(int(path.split("/")[3])))
        if path.startswith("shows/7/seasons/"):
            return _Response(_episodes(int(path.rsplit("/", 1)[1])))
        return _Response({})


KEY = "show:tmdb:1"


class _CardPanelTestCase(AppTestCase):
    """A signed-in viewer and one card's worth of query string."""

    def make_settings(self):
        # The catalogue half needs a client id and nothing else: everything this
        # panel draws is either public catalogue data or this account's own
        # stored progress.
        return Settings(public_base_url=ORIGIN, trakt_client_id="cid")

    def setUp(self):
        super().setUp()
        self.user_id = self.make_user("viewer", calendar_approved=True,
                                      distrakt_approved=True)
        self.link_identity(self.user_id, "trakt", 900, "user-token")
        self.sign_in_as(self.user_id)

    def _get(self, path="/calendar/fragments/details", seasons=None, **params):
        query = {"media": "show", "trakt": "7", "season": "3", "key": KEY}
        query.update(params)
        with patch.object(transport, "shared_client",
                          return_value=_CatalogueClient(seasons)):
            return self.client.get(
                path + "?" + "&".join(f"{k}={v}" for k, v in query.items()))

    def _panel(self, **params) -> DetailsPanel:
        resp = self._get(**params)
        self.assertEqual(resp.status_code, 200, resp.text)
        return DetailsPanel(resp.text)

    def _progress(self, source: str, episodes, season: int = 3) -> None:
        asyncio.run(db.execute(
            "INSERT OR REPLACE INTO distrakt_show_progress "
            "(user_id, media, match_source, match_id, season, source, "
            "watched_episodes_json, trakt_id) VALUES (?,?,?,?,?,?,?,?)",
            (self.user_id, "show", "tmdb", "1", season, source,
             json.dumps({str(ep): "2026-08-01T00:00:00Z" for ep in episodes}), 7)))


class SeasonTabsOnACalendarCardTests(_CardPanelTestCase):
    """What the strip offers, and what pressing one of its tabs costs."""

    def test_a_returning_season_offers_every_season_before_it(self):
        """The whole point of the picker on this surface: opening a card for a
        returning season and asking what the earlier ones were about."""
        panel = self._panel()
        self.assertEqual(panel.season_tabs, [1, 2, 3])
        self.assertEqual(panel.open_tab, 3)

    def test_the_season_that_is_open_asks_for_nothing(self):
        """Its rows came with the panel, so a request for them would be a second
        copy of what is already on the page — the same reason a tier rendered
        open carries no hx-get."""
        panel = self._panel()
        self.assertNotIn(3, panel.tab_urls)
        self.assertEqual(sorted(panel.tab_urls), [1, 2])
        self.assertIn("S3 Ep 1", panel.html)

    def test_a_show_with_one_season_draws_no_strip_at_all(self):
        """A picker naming the only season there is offers nothing to move
        between, and would take a line of the panel to say so."""
        panel = self._panel(season="1", seasons=SEASONS[:1])
        self.assertEqual(panel.season_tabs, [])
        self.assertIn("S1 Ep 1", panel.html)

    def test_an_early_season_still_offers_the_later_ones(self):
        """THE COUNT IS THE SERVICE'S, NOT THE CARD'S, and that is a correction:
        the strip used to be built from the season being viewed, so opening
        season 1 of a five-season show offered exactly one tab and there was no
        way to reach the rest."""
        panel = self._panel(season="1")
        self.assertEqual(panel.season_tabs, [1, 2, 3])
        self.assertEqual(panel.open_tab, 1)

    def test_a_film_draws_no_strip_and_no_episodes(self):
        panel = self._panel(media="movie")
        self.assertEqual(panel.season_tabs, [])
        self.assertNotIn("Episodes", panel.html)

    def test_a_tab_asks_the_season_endpoint_for_its_own_season(self):
        """The address is this request's own with `/episodes` on the end and the
        season swapped, which is what keeps a tab carrying everything the panel
        had to carry — the card's ids, its medium and its identity."""
        url = self._panel().tab_urls[1]
        parts = urlsplit(url)
        self.assertEqual(parts.path, "/calendar/fragments/details/season")
        self.assertEqual(parse_qs(parts.query), {
            "media": ["show"], "trakt": ["7"], "season": ["1"], "key": [KEY]})

    def test_a_tab_carries_what_this_route_reads_and_nothing_a_stranger_invented(self):
        """FOUND ONCE BY THE TRACKER'S OWN SECURITY GUARD, and it belongs on this
        surface too: the first version of the picker copied the request's whole
        query string onto every tab, so a parameter nobody reads was rendered
        back into the page. Inert inside an attribute, and still the thing the
        poster rule and the outbound-link rule already refuse to do."""
        panel = self._panel(spoof="evil")
        self.assertNotIn("evil", panel.html)
        from urllib.parse import parse_qs, urlsplit
        query = parse_qs(urlsplit(panel.tab_urls[1]).query)
        self.assertNotIn("spoof", query)
        self.assertEqual(query["key"], [KEY])

    def test_pressing_a_tab_answers_with_that_season_and_nothing_else(self):
        """A fragment, not a second panel: the hero, the chips and the cast are
        already on the page and swapping them in again would draw them twice."""
        resp = self._get("/calendar/fragments/details/season", season="1")
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertIn("Season 1 Episodes", resp.text)
        self.assertIn("S1 Ep 1", resp.text)
        self.assertNotIn("Michael", resp.text)
        self.assertNotIn("Spies.", resp.text)


class WatchTicksOnACalendarCardTests(_CardPanelTestCase):
    """Whose viewing the calendar's panel draws, and when it draws none."""

    def test_a_tracked_season_shows_this_viewers_own_ticks(self):
        self._progress("trakt", [1, 2])
        panel = self._panel()
        self.assertEqual(panel.watched, [1, 2])
        self.assertEqual(panel.by_source, {"trakt": [1, 2]})
        self.assertIn("2 watched", panel.html)

    def test_a_show_this_viewer_does_not_track_still_draws_hollow_ticks(self):
        """A TICK IS A LINK BEFORE IT IS A CLAIM. This first shipped drawing none
        where nothing was recorded, on the grounds that hollow ticks would assert
        a knowledge of the viewer's watching the calendar does not have. That
        withheld the one control on the row that works regardless: the link to
        that episode at that service. Unticked plus a working link is the honest
        reading, and it is what the tracker's own panel has always done."""
        panel = self._panel()
        self.assertEqual(panel.services, ["trakt"])
        self.assertEqual(panel.watched, [])
        self.assertIn("trakt", panel.tick_links)
        self.assertNotIn("watched", panel.html)

    def test_an_account_that_syncs_nothing_gets_them_too(self):
        """A TICK DOES NOT CLAIM THE READER USES THAT SERVICE. It links to the
        episode's own page there, which is a useful place to go whether or not
        anybody has an account — so an account with nothing linked gets the same
        row. This was briefly written the other way, on the reasoning that a tick
        would name a service the viewer does not use; that reads a link as an
        assertion about the reader, and it is not one."""
        solo = self.make_user("no-tracker", calendar_approved=True)
        self.sign_in_as(solo)
        panel = self._panel()
        self.assertEqual(panel.services, ["trakt"])
        self.assertIn("trakt", panel.tick_links)

    def test_the_ticks_follow_the_season_a_tab_opened(self):
        """Each season's rows carry that season's own record — the tab fetches
        the episodes and the viewing together, because they are one answer."""
        self._progress("trakt", [1, 2], season=3)
        self._progress("trakt", [4], season=1)
        self.assertEqual(self._panel().watched, [1, 2])
        rows = DetailsPanel(
            self._get("/calendar/fragments/details/season", season="1").text)
        self.assertEqual(rows.watched, [4])

    def test_a_card_that_names_no_identity_draws_no_ticks(self):
        """Nothing is guessed from the service ids. A card without the shared
        identity on it cannot be matched to a roster row, and a panel that tried
        would be matching on whichever service happened to describe it."""
        self._progress("trakt", [1, 2])
        panel = self._panel(key="")
        self.assertEqual(panel.watched, [])

    def test_a_hand_edited_identity_reads_the_callers_own_rows(self):
        """It is scoped to the session's user id, so the worst a rewritten `key`
        can do is show somebody their own viewing of another title — which their
        own tracker shows them anyway. Asserted so that the scoping is a claim
        with a test behind it rather than a remark in a docstring."""
        other = self.make_user("someone-else", calendar_approved=True,
                               distrakt_approved=True)
        asyncio.run(db.execute(
            "INSERT OR REPLACE INTO distrakt_show_progress "
            "(user_id, media, match_source, match_id, season, source, "
            "watched_episodes_json, trakt_id) VALUES (?,?,?,?,?,?,?,?)",
            (other, "show", "tmdb", "2", 3, "trakt",
             json.dumps({"1": "2026-08-01T00:00:00Z"}), 7)))
        panel = self._panel(key="show:tmdb:2")
        self.assertEqual(panel.watched, [])

    def test_a_tick_and_a_button_spell_the_same_service_the_same_way(self):
        """One map behind both, which is the rule the tracker's own panel states:
        a service with a working tick has a working button. Two maps would let
        the same panel address the same service two different ways on the same
        screen."""
        self._progress("trakt", [1])
        panel = self._panel(trakt_slug="the-agency")
        self.assertIn('href="https://trakt.tv/shows/the-agency"', panel.html)
        self.assertEqual(
            panel.tick_links["trakt"],
            "https://app.trakt.tv/shows/the-agency?season=3&view=episode&episode=1")


class ThePendingMarkerIsNeverAtThePanelsTopLevelTests(_CardPanelTestCase):
    """A structural rule the SERVER has to hold up for the browser's sake.

    static/js/ui.js decides "this request ended without putting anything there"
    by asking whether the pending placeholder it wrote is still the swap target's
    OWN immediate child. That question is only answerable if a rendered panel
    never has one at its top level — and the season picker broke exactly that:
    every unopened season pane arrives holding a pending placeholder, so a panel
    with tabs was read as a failed request and replaced with the failure line.
    Anything with more than one season could not be opened at all.

    ASSERTED HERE RATHER THAN LEFT TO THE BROWSER because it is a fact about the
    MARKUP, and it is the half of the contract a template change can break
    without any JavaScript changing. The other half — that the marker survives
    inside a pane whose request never landed — is the browser's, and this cannot
    reach it.
    """

    def _pending_depths(self, html: str) -> list[int]:
        """How deep each pending marker sits, counting the fragment's own
        top-level elements as depth 0."""
        from html.parser import HTMLParser

        VOID = {"img", "input", "br", "hr", "meta", "link", "source"}

        class _Depths(HTMLParser):
            def __init__(self):
                super().__init__()
                self.depth = 0
                self.found: list[int] = []

            def handle_starttag(self, tag, attrs):
                if any(name == "data-details-pending" for name, _ in attrs):
                    self.found.append(self.depth)
                if tag not in VOID:
                    self.depth += 1

            def handle_endtag(self, tag):
                if tag not in VOID:
                    self.depth -= 1

        parser = _Depths()
        parser.feed(html)
        return parser.found

    def test_a_panel_with_tabs_keeps_every_marker_inside_a_pane(self):
        panel = self._panel()
        self.assertEqual(panel.season_tabs, [1, 2, 3])
        depths = self._pending_depths(panel.html)
        # One per unopened season, and not one of them at the top level.
        self.assertEqual(len(depths), 2)
        self.assertNotIn(0, depths)

    def test_a_panel_without_tabs_carries_no_marker_at_all(self):
        self.assertEqual(
            self._pending_depths(self._panel(season="1", seasons=SEASONS[:1]).html), [])

    def test_a_season_fragment_carries_no_marker_either(self):
        """It is swapped into a pane that already holds one, so a marker of its
        own would leave the pane looking unfilled after it had been filled."""
        rows = self._get("/calendar/fragments/details/season", season="1").text
        self.assertEqual(self._pending_depths(rows), [])


class ThePanelDescribesTheSeasonItIsOpenOnTests(_CardPanelTestCase):
    """The description and the cast belong to the SEASON, not to the title.

    THE DEFECT THIS REPLACES WAS MEASURED BEFORE IT WAS FIXED. The panel drew the
    show's cast whatever season it was open on: across 14 seasons of six titles,
    85 of the 224 people it listed were actually in the season being viewed —
    37%. On an anthology it is plainly wrong rather than merely imprecise, and
    visible to anyone: the `true-detective` season 1 panel listed two actors from
    Night Country, four seasons later.

    Trakt states both per season — every one of the 40 seasons probed had its own
    overview, and all 40 differed from the show's — so what is asserted here is
    that the app asks for them and draws them.
    """

    def test_both_descriptions_are_shown_and_they_are_different_ones(self):
        """THE SHOW'S AND THE SEASON'S, not one instead of the other. They answer
        different questions — the premise, and what this particular run of it is
        about — and an anthology wants both: the show blurb sits with the chips,
        the season's under the trailer."""
        panel = self._panel(season="2")
        self.assertIn("Spies.", panel.html)                    # the show's
        self.assertIn("What season 2 is about.", panel.html)   # this season's

    def test_a_season_with_no_words_of_its_own_repeats_nothing(self):
        """No fallback here, deliberately: the title's description is already on
        screen above the picker, and repeating it under every tab would say "this
        season is about the show" once per season."""
        quiet = [{**s, "overview": ""} for s in SEASONS]
        panel = self._panel(season="2", seasons=quiet)
        self.assertIn("Spies.", panel.html)
        self.assertNotIn("details-overview season", panel.html)

    def test_the_cast_is_the_seasons_own(self):
        panel = self._panel(season="2")
        self.assertIn("S2 Actor", panel.html)
        # The show-level cast is what the panel used to draw for every season.
        self.assertNotIn("Michael", panel.html)

    def test_a_tab_swaps_the_description_and_the_cast_with_the_episodes(self):
        """The reason the picker sits above them: all three change together, so
        a tab that swapped only the rows would leave the other two describing a
        season the reader is no longer looking at."""
        rows = self._get("/calendar/fragments/details/season", season="1").text
        self.assertIn("What season 1 is about.", rows)
        self.assertIn("S1 Actor", rows)
        self.assertIn("S1 Ep 1", rows)
        # And NOT the show's blurb or its trailer, which do not move.
        self.assertNotIn("Spies.", rows)
        self.assertNotIn("Trailer", rows)

    def test_what_does_not_vary_by_season_is_not_re_sent(self):
        """The chips, the show's description, the outbound buttons and the
        trailer are facts about the TITLE, so a tab must not carry them —
        swapping them in again would draw them twice."""
        rows = self._get("/calendar/fragments/details/season", season="1").text
        self.assertNotIn("details-hero", rows)
        self.assertNotIn("Paramount+", rows)
        self.assertNotIn("trailer-embed", rows)

    def test_the_trailer_is_drawn_once_however_many_seasons_there_are(self):
        """It is the TITLE's — neither service publishes one per season, measured
        — so one copy sits between the strip and the panes. A copy per pane would
        be an iframe per opened season, all but one hidden and still loading."""
        self.assertEqual(self._panel().html.count("trailer-embed"), 1)

    def test_the_trailer_is_written_before_the_seasons_it_is_drawn_after(self):
        """WHERE IT SITS IN THE DOCUMENT IS NOT WHERE IT IS DRAWN, and the
        document is what matters when the layout rules are not there. Flex
        `order` lifts it between the strip and the panes; a pane has to stay its
        label's adjacent sibling, so nothing can be written in between. Emitted
        LAST — as it first was — a browser holding a stylesheet from before those
        rules existed dropped it to the bottom of the panel, under the episode
        list. Emitted first, the worst case is that it sits above the tabs."""
        html = self._panel().html
        self.assertLess(html.index("details-trailer"), html.index("ep-season-tab"))
        self.assertLess(html.index("details-trailer"), html.index("ep-list"))

    def test_the_seasons_own_name_is_shown_where_it_has_one(self):
        """Anthologies title their seasons properly — Murder House, Asylum,
        Night Country — and that name says something the number cannot."""
        self.assertIn("The Second One", self._panel(season="2").html)

    def test_a_number_restated_is_not_a_name(self):
        """"Season 2" is the number spelled out. Rendering it beside the number
        gives a reader nothing, so the provider drops it."""
        plain = [{**s, "title": f"Season {s['number']}"} for s in SEASONS]
        panel = self._panel(season="2", seasons=plain)
        self.assertNotIn("ep-season-name", panel.html)
