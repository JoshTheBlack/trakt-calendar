"""Unit tests for the calendar-source seam (app/providers).

Covers the four things the seam actually promises: that a Record carries its
provenance as source/ids/detail_url rather than one service's ids hoisted to the
top level; that a provider forgetting a field fails at construction rather than
rendering a blank card; that Capabilities answers "can this source do that"; and
that the registry resolves the configured calendar sources without any caller
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
from app.sources import prefs
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


class TestNormalizeProducesARecord:
    def test_provenance_is_source_ids_and_detail_url(self):
        record = trakt_calendar.to_record(ENTRY, SHOWS)
        assert record.source == Source.TRAKT
        assert record.ids == {"slug": "a-show", "trakt": 123, "tvdb": 456,
                              "tmdb": 789, "imdb": "tt42"}
        assert record.detail_url == "https://trakt.tv/shows/a-show"

    def test_a_movie_gets_the_movies_detail_url(self):
        """The two media types live under different paths on Trakt, and a show
        URL for a movie 404s rather than failing visibly here."""
        entry = {"released": "2026-07-15",
                 "movie": {"title": "A Film", "ids": {"slug": "a-film", "trakt": 9}}}
        record = trakt_calendar.to_record(entry, MOVIES)
        assert record.detail_url == "https://trakt.tv/movies/a-film"

    def test_media_is_the_enum_and_still_equals_its_string(self):
        """Templates, DB columns and the response keys all hold the plain
        string; the enum has to stay interchangeable with it or every one of
        those boundaries grows a conversion."""
        record = trakt_calendar.to_record(ENTRY, SHOWS)
        assert record.media is Media.SHOW
        assert record.media == "show"

    def test_a_record_carries_no_viewer_local_spelling_of_its_air_time(self):
        """The whole reason the cache can be shared: a record says WHEN in POSIX
        seconds and nothing else, so one stored copy serves every timezone."""
        record = trakt_calendar.to_record(ENTRY, SHOWS)
        assert record.air_ts == 1784145600.0
        assert not hasattr(record, "air_date")

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

    def test_no_usable_calendar_source_until_one_is_configured(self):
        assert providers.for_calendar_sources(Settings()) == []

    def test_the_configured_source_is_the_usable_one(self):
        configured = Settings(trakt_client_id="id", trakt_access_token="token")
        assert [p.source for p in providers.for_calendar_sources(configured)] == [Source.TRAKT]

    def test_a_source_that_could_answer_is_listed_whether_or_not_it_is_set_up(self):
        """The two questions are different and both are asked. "Who could put
        something on a calendar" is a property of the SOURCE and decides who the
        fill asks; "who can we actually use" adds the credentials and is what the
        page checks before it renders an explanation instead of a month. Simkl's
        calendar needs no credential at all, so it is listed here with NO
        Settings object in play whatsoever — calendar_sources() takes none."""
        assert {p.source for p in providers.calendar_sources()} == {Source.TRAKT, Source.SIMKL}

    def test_a_source_with_no_calendar_port_would_be_in_neither_list(self):
        """The negative half of the rule above, pinned against whichever source
        genuinely carries no calendar_port today — asserted through the
        registry rather than by name, so this does not silently stop meaning
        anything the day every registered source has one."""
        no_calendar = [p for p in providers.registered().values() if p.calendar_port is None]
        for provider in no_calendar:
            assert provider.source not in [p.source for p in providers.calendar_sources()]

    def test_simkl_is_a_usable_calendar_source_once_its_own_credential_is_set(self):
        """`for_calendar_sources` narrows to `is_configured`, which for Simkl
        still asks the TRACKER's credential (client id + access token) even
        though the calendar CDN itself needs neither. `is_configured` answers
        for the whole source rather than per capability, so linking Simkl for
        the tracker is what makes its calendar count as "usable" here too."""
        both = Settings(trakt_client_id="id", trakt_access_token="token",
                        simkl_client_id="id", simkl_access_token="token")
        assert {p.source for p in providers.for_calendar_sources(both)} == {Source.TRAKT, Source.SIMKL}

    def test_an_unconfigured_simkl_is_still_asked_by_the_fill_but_not_usable_yet(self):
        """The fill (`calendar_sources`) does not ask `is_configured` at all —
        so Simkl is admitted to the fill regardless; `for_calendar_sources`
        is the narrower, credential-checked list a route uses to decide whether
        there is anybody to explain the calendar with."""
        trakt_only = Settings(trakt_client_id="id", trakt_access_token="token")
        assert Source.SIMKL in [p.source for p in providers.calendar_sources()]
        assert Source.SIMKL not in [p.source for p in providers.for_calendar_sources(trakt_only)]

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


# A preference that admits everything, the set of every service name, and a
# Settings carrying a usable credential for both — the three arguments the
# selector takes, spelled once because most of these tests vary exactly one of
# them.
_ALL_SOURCES = prefs.SourcePrefs(user_id=1, tracker_source=prefs.BOTH)
_ALL_NAMES = frozenset(str(source) for source in providers.Source)
_CONFIGURED = Settings(trakt_client_id="c", trakt_access_token="t",
                       simkl_client_id="c", simkl_access_token="t")


class TestTrackerPort:
    """The registry answering "who can read one person's own viewing", so the
    tracker never has to name a service."""

    def test_it_finds_every_source_that_reaches_private_data(self):
        ports = providers.for_tracker_ports(_ALL_SOURCES, _ALL_NAMES, _CONFIGURED)
        assert [source for source, _p in ports] == [Source.TRAKT, Source.SIMKL]
        assert ports[0][1] is providers.get(Source.TRAKT).sync_port

    def test_the_primary_source_is_the_first_declared_one(self):
        """The order is the registry's, and the FIRST entry is what a frozen
        month and the announcement post carry when there is room for one number.
        Trakt leads because every existing instance already reads it."""
        ports = providers.for_tracker_ports(_ALL_SOURCES, _ALL_NAMES, _CONFIGURED)
        assert ports[0][0] is Source.TRAKT

    def test_a_preference_naming_one_source_admits_only_that_one(self):
        ports = providers.for_tracker_ports(
            prefs.SourcePrefs(user_id=1, tracker_source=str(Source.SIMKL)),
            _ALL_NAMES, _CONFIGURED)
        assert [source for source, _p in ports] == [Source.SIMKL]

    def test_auto_follows_the_links(self):
        """`auto` is the default and asks whatever the account has connected, so
        an account with one service is on exactly the path it always was."""
        ports = providers.for_tracker_ports(
            prefs.SourcePrefs(user_id=1), {str(Source.TRAKT)}, _CONFIGURED)
        assert [source for source, _p in ports] == [Source.TRAKT]

    def test_an_unconfigured_source_is_never_asked(self):
        """Admitted by the preference and linked, but with no credential on this
        request's settings, is not something to call — see _distrakt_settings,
        which is what puts an account's own tokens there."""
        ports = providers.for_tracker_ports(
            _ALL_SOURCES, _ALL_NAMES, Settings(trakt_client_id="c", trakt_access_token="t"))
        assert [source for source, _p in ports] == [Source.TRAKT]

    def test_tracker_sources_names_who_could_back_it_at_all(self):
        assert providers.tracker_sources() == {str(Source.TRAKT), str(Source.SIMKL)}

    def test_only_a_source_that_declares_private_data_can_back_the_tracker(self):
        for provider in providers.registered().values():
            if provider.sync_port is not None:
                assert provider.capabilities.private_user_data

    def test_the_port_answers_every_question_the_protocol_names(self):
        port = providers.get(Source.TRAKT).sync_port
        for name in ("fetch_last_activities", "fetch_history", "fetch_progress_details",
                     "fetch_watched_progress", "watched_progress_from", "movie_plays_from"):
            assert callable(getattr(port, name))

    def test_the_port_calls_through_the_module_so_a_patch_still_reaches_it(self):
        """The trap this whole branch keeps hitting: a name imported at class
        definition time becomes a second reference that patching the module can no
        longer reach, and the test then exercises the real call. Asserted directly
        because a port that quietly stopped being patchable would show up as live
        provider traffic, not as a failure."""
        port = providers.get(Source.TRAKT).sync_port
        with patch("app.providers.trakt.sync.fetch_last_activities",
                   new=AsyncMock(return_value={"episodes": {"watched_at": "T"}})) as spy:
            answer = asyncio.run(port.fetch_last_activities(Settings()))
        assert answer == {"episodes": {"watched_at": "T"}}
        spy.assert_awaited_once()
