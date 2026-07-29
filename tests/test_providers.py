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

from datetime import date
from zoneinfo import ZoneInfo

import pytest

from app import providers
from app.providers.trakt import calendar as trakt_calendar
from app.config import Settings
from app.endpoints import ENDPOINTS, get_endpoint
from app.providers.base import Capabilities, Item, Media, Source, collect_ids

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
