"""Unit tests for the calendar-source seam (app/providers).

Covers the four things the seam actually promises: that an Item carries its
provenance as source/ids/detail_url rather than one service's ids hoisted to the
top level; that a provider forgetting a field fails at construction rather than
rendering a blank card; that Capabilities answers "can this source do that"; and
that the registry resolves the configured calendar source without any caller
naming one.

Also pins the endpoint-key -> provider-path translation, which is the boundary
that lets a second source answer the same dropdown.

No network — the one normalizer test runs on a literal Trakt calendar entry.
"""
from __future__ import annotations

import asyncio
from datetime import date
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

import pytest

from app import providers
from app.providers.trakt import calendar as trakt_calendar
from app.config import Settings
from app.endpoints import ENDPOINTS, get_endpoint
from app.providers.base import (
    Capabilities,
    Item,
    Media,
    Source,
    collect_ids,
    parse_item_key,
    resolve_identity,
    resolve_key,
)

SHOWS = get_endpoint("shows")
MOVIES = get_endpoint("movies")

# One real Trakt calendar entry, trimmed to the fields the normalizer reads.
ENTRY = {
    "first_aired": "2026-07-15T20:00:00.000Z",
    "episode": {"season": 2, "number": 5, "title": "The One"},
    "show": {
        "title": "A Show", "year": 2026, "network": "HBO",
        "ids": {"slug": "a-show", "trakt": 123, "tvdb": 456, "tmdb": 789,
                "imdb": "tt42", "unknown_namespace": "x"},
    },
}


class TestCollectIds:
    def test_keeps_only_the_declared_namespaces(self):
        """An id space the Item vocabulary does not name is dropped, so a
        provider cannot smuggle a private key into the shared record."""
        assert "unknown_namespace" not in collect_ids(ENTRY["show"]["ids"])

    def test_omits_absent_ids_rather_than_filling_them_with_none(self):
        """`"tmdb" in item.ids` has to mean "known to TMDB". A None placeholder
        would make every reader check twice for the same answer."""
        ids = collect_ids({"trakt": 1, "tmdb": None, "imdb": ""})
        assert ids == {"trakt": 1}


class TestNormalizeProducesAnItem:
    def test_provenance_is_source_ids_and_detail_url(self):
        item = trakt_calendar.normalize(ENTRY, SHOWS, ZoneInfo("UTC"))
        assert item.source == Source.TRAKT
        assert item.ids == {"slug": "a-show", "trakt": 123, "tvdb": 456,
                            "tmdb": 789, "imdb": "tt42"}
        assert item.detail_url == "https://trakt.tv/shows/a-show"

    def test_a_movie_gets_the_movies_detail_url(self):
        """The two media types live under different paths on Trakt, and a show
        URL for a movie 404s rather than failing visibly here."""
        entry = {"released": "2026-07-15",
                 "movie": {"title": "A Film", "ids": {"slug": "a-film", "trakt": 9}}}
        item = trakt_calendar.normalize(entry, MOVIES, ZoneInfo("UTC"))
        assert item.detail_url == "https://trakt.tv/movies/a-film"

    def test_media_is_the_enum_and_still_equals_its_string(self):
        """Templates, DB columns and the response keys all hold the plain
        string; the enum has to stay interchangeable with it or every one of
        those boundaries grows a conversion."""
        item = trakt_calendar.normalize(ENTRY, SHOWS, ZoneInfo("UTC"))
        assert item.media is Media.SHOW
        assert item.media == "show"

    def test_an_item_missing_a_required_field_raises_at_construction(self):
        """THE REASON THIS IS A DATACLASS. A provider that forgets to say when
        something airs fails here, in its own tests, rather than rendering a
        card with a blank date."""
        with pytest.raises(TypeError):
            Item(source=Source.TRAKT, media=Media.SHOW, id="x", ids={},
                 detail_url="", title="No air date")


class TestCapabilities:
    CAPS = Capabilities(
        endpoints=frozenset({"shows/new"}),
        days_before=30, days_after=90, private_user_data=False,
    )

    def test_answers_only_the_endpoints_it_declares(self):
        assert self.CAPS.answers("shows/new")
        assert not self.CAPS.answers("movies")

    def test_covers_is_bounded_at_both_ends(self):
        today = date(2026, 7, 15)
        assert self.CAPS.covers(date(2026, 7, 1), today=today)
        assert not self.CAPS.covers(date(2026, 5, 1), today=today)
        assert not self.CAPS.covers(date(2027, 1, 1), today=today)

    def test_a_none_bound_means_unbounded(self):
        """Trakt declares no window; an unbounded source must not accidentally
        read as one that covers nothing."""
        caps = Capabilities(endpoints=frozenset(), days_before=None,
                            days_after=None, private_user_data=True)
        assert caps.covers(date(1999, 1, 1), today=date(2026, 7, 15))
        assert caps.covers(date(2099, 1, 1), today=date(2026, 7, 15))


class TestRegistry:
    def test_trakt_is_registered_and_declares_every_endpoint(self):
        provider = providers.get(Source.TRAKT)
        assert provider.capabilities.endpoints == frozenset(ENDPOINTS)
        assert provider.capabilities.private_user_data

    def test_for_calendar_returns_none_until_a_source_is_configured(self):
        assert providers.for_calendar(Settings()) is None

    def test_for_calendar_finds_the_configured_source(self):
        configured = Settings(trakt_client_id="id", trakt_access_token="token")
        assert providers.for_calendar(configured).source == Source.TRAKT

    def test_registered_hands_back_a_copy(self):
        """A caller iterating the registry must not be able to empty it."""
        snapshot = providers.registered()
        snapshot.clear()
        assert providers.get(Source.TRAKT) is not None


class TestEndpointTranslation:
    def test_an_endpoint_carries_no_provider_path(self):
        """The path belongs to whoever is being asked, not to the endpoint —
        that is what lets a second source answer the same dropdown entry."""
        assert not hasattr(SHOWS, "path")

    def test_every_endpoint_translates_to_a_trakt_path(self):
        for key, endpoint in ENDPOINTS.items():
            assert trakt_calendar.calendar_path(endpoint) == key


class TestIdentityWaterfall:
    """The one definition of "the same title", which the tier boards and the
    tracker now both file their rows under."""

    def test_tmdb_wins_when_it_is_there(self):
        assert resolve_identity({"tvdb": 1, "tmdb": 2, "imdb": "tt3"}) == ("tmdb", "2")

    def test_it_falls_down_the_rungs_in_order(self):
        assert resolve_identity({"tvdb": 1, "imdb": "tt3"}) == ("tvdb", "1")
        assert resolve_identity({"imdb": "tt3", "mal": 9}) == ("imdb", "tt3")
        assert resolve_identity({"mal": 9}) == ("mal", "9")

    def test_a_provider_id_is_not_something_to_key_on(self):
        """trakt/slug/simkl are how you CALL a service, not how two services
        agree about a title — keying on one would make the same title arriving
        from somewhere else a second row for ever."""
        assert resolve_identity({"trakt": 5, "slug": "a-show", "simkl": 7}) is None

    def test_an_empty_id_is_not_an_id(self):
        assert resolve_identity({"tmdb": None, "tvdb": 0, "imdb": "", "mal": 4}) == ("mal", "4")

    def test_the_id_is_stringified_because_imdb_ids_are_not_numbers(self):
        assert resolve_identity({"tmdb": 550})[1] == "550"

    def test_a_key_pairs_the_waterfall_with_the_media_kind(self):
        """A TMDB id is namespaced per kind: movie 550 and TV 550 are different
        titles, so the media type is part of the identity."""
        assert str(resolve_key(Media.SHOW, {"tmdb": 550})) == "show:tmdb:550"
        assert str(resolve_key(Media.MOVIE, {"tmdb": 550})) == "movie:tmdb:550"

    def test_a_title_with_nothing_shared_has_no_key(self):
        assert resolve_key(Media.SHOW, {"trakt": 9}) is None

    def test_the_flat_form_round_trips(self):
        key = resolve_key(Media.MOVIE, {"imdb": "tt0137523"})
        assert parse_item_key(str(key)) == key

    def test_an_imdb_id_containing_the_separator_still_parses(self):
        """Split at most twice: media and match_source come from closed sets and
        can never contain a colon, but somebody else's id is opaque to us."""
        assert parse_item_key("movie:imdb:tt:weird").match_id == "tt:weird"

    @pytest.mark.parametrize("bad", [
        None, 7, "", "show:tmdb", "book:tmdb:1", "show:trakt:1", "show:tmdb:",
    ])
    def test_a_malformed_key_raises_rather_than_returning_a_sentinel(self, bad):
        with pytest.raises(ValueError):
            parse_item_key(bad)


class TestTrackerPort:
    """The registry answering "who can read one person's own viewing", so the
    tracker never has to name a service."""

    def test_it_finds_the_source_that_reaches_private_data(self):
        port = providers.for_tracker()
        assert port is not None
        assert port is providers.get(Source.TRAKT).sync_port

    def test_only_a_source_that_declares_private_data_can_back_the_tracker(self):
        for provider in providers.registered().values():
            if provider.sync_port is not None:
                assert provider.capabilities.private_user_data

    def test_the_port_answers_every_question_the_protocol_names(self):
        port = providers.for_tracker()
        for name in ("fetch_last_activities", "fetch_history", "fetch_progress_details",
                     "fetch_watched_progress", "watched_progress_from", "movie_plays_from"):
            assert callable(getattr(port, name))

    def test_the_port_calls_through_the_module_so_a_patch_still_reaches_it(self):
        """The trap this whole branch keeps hitting: a name imported at class
        definition time becomes a second reference that patching the module can no
        longer reach, and the test then exercises the real call. Asserted directly
        because a port that quietly stopped being patchable would show up as live
        provider traffic, not as a failure."""
        port = providers.for_tracker()
        with patch("app.providers.trakt.sync.fetch_last_activities",
                   new=AsyncMock(return_value={"episodes": {"watched_at": "T"}})) as spy:
            answer = asyncio.run(port.fetch_last_activities(Settings()))
        assert answer == {"episodes": {"watched_at": "T"}}
        spy.assert_awaited_once()
