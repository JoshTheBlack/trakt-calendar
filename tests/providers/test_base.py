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
import dataclasses
import unittest
from datetime import date, datetime, timezone
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
    epoch_moment,
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
        record = trakt_calendar.to_record(ENTRY, SHOWS.media)
        assert record.source == Source.TRAKT
        # `trakt_slug` beside `slug`: both services call a title's readable
        # name `slug` and disagree on it, so each one's is namespaced at the
        # boundary and the shared key stays for rows written before the two
        # were told apart.
        assert record.ids == {"slug": "a-show", "trakt_slug": "a-show",
                              "trakt": 123, "tvdb": 456,
                              "tmdb": 789, "imdb": "tt42"}
        assert record.detail_url == "https://trakt.tv/shows/a-show"

    def test_a_movie_gets_the_movies_detail_url(self):
        """The two media types live under different paths on Trakt, and a show
        URL for a movie 404s rather than failing visibly here."""
        entry = {"released": "2026-07-15",
                 "movie": {"title": "A Film", "ids": {"slug": "a-film", "trakt": 9}}}
        record = trakt_calendar.to_record(entry, MOVIES.media)
        assert record.detail_url == "https://trakt.tv/movies/a-film"

    def test_a_slugless_entry_links_by_id_rather_than_to_the_homepage(self):
        """Trakt resolves `/shows/<id>` to the same page `/shows/<slug>` reaches,
        so an entry Trakt named no slug for still has somewhere correct to go.
        What this replaced sent it to `https://trakt.tv` — a link that looks like
        it worked, so nobody reports it, and lands nowhere near the title."""
        entry = {**ENTRY, "show": {**ENTRY["show"],
                                   "ids": {"trakt": 123, "tvdb": 456}}}
        record = trakt_calendar.to_record(entry, SHOWS.media)
        assert record.detail_url == "https://trakt.tv/shows/123"

    def test_the_slug_still_wins_when_trakt_named_one(self):
        """The id is the FALLBACK, not the preference. Both services ask that the
        readable name be sent when it is known, because resolving the numeric id
        costs them a title lookup and a redirect they need not have done."""
        record = trakt_calendar.to_record(ENTRY, SHOWS.media)
        assert record.detail_url == "https://trakt.tv/shows/a-show"

    def test_an_entry_naming_no_trakt_id_at_all_still_has_a_url(self):
        """Neither name available is the one case the homepage is the honest
        answer: there is no title to point at. It must not render `None` into
        the path, which would 404 instead."""
        entry = {**ENTRY, "show": {**ENTRY["show"], "ids": {"tvdb": 456}}}
        record = trakt_calendar.to_record(entry, SHOWS.media)
        assert record.detail_url == "https://trakt.tv"

    def test_media_is_the_enum_and_still_equals_its_string(self):
        """Templates, DB columns and the response keys all hold the plain
        string; the enum has to stay interchangeable with it or every one of
        those boundaries grows a conversion."""
        record = trakt_calendar.to_record(ENTRY, SHOWS.media)
        assert record.media is Media.SHOW
        assert record.media == "show"

    def test_a_record_carries_no_viewer_local_spelling_of_its_air_time(self):
        """The whole reason the cache can be shared: a record says WHEN in POSIX
        seconds and nothing else, so one stored copy serves every timezone."""
        record = trakt_calendar.to_record(ENTRY, SHOWS.media)
        assert record.air_ts == 1784145600.0
        assert not hasattr(record, "air_date")

    def test_a_date_renders_with_no_time(self):
        """A date-only record is pinned at UTC midnight so it lands on its day
        everywhere; printing that midnight made every film and day-only
        premiere claim to air at 00:00. An instant keeps its local time."""
        from app.providers.base import render
        record = trakt_calendar.to_record(ENTRY, SHOWS.media)
        dated = dataclasses.replace(record, date_only=True, air_ts=1784073600.0)  # 2026-07-15
        assert render(dated, ZoneInfo("America/New_York")).air_time == ""
        assert render(dated, ZoneInfo("America/New_York")).air_date == "2026-07-15"
        assert render(record, ZoneInfo("UTC")).air_time == "20:00"

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
        retain_seconds=None,
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
                            days_after=None, private_user_data=True,
                            retain_seconds=None)
        assert caps.covers(date(1999, 1, 1), today=date(2026, 7, 15))
        assert caps.covers(date(2099, 1, 1), today=date(2026, 7, 15))


class TestRegistry:
    def test_trakt_is_registered_and_declares_every_endpoint(self):
        provider = providers.get(Source.TRAKT)
        assert provider.capabilities.endpoints == frozenset(ENDPOINTS)
        assert provider.capabilities.private_user_data

    def test_an_instance_with_no_credentials_at_all_still_has_a_calendar(self):
        """A source whose calendar needs no credential is usable on an instance
        that has filled nothing in, which is what a public feed means. This used
        to answer [] — `for_calendar_sources` narrowed on `is_configured`, the
        PRIVATE question — and a calendar page therefore explained itself away on
        an instance that could have drawn a month."""
        assert [p.source for p in providers.for_calendar_sources(Settings())] == [Source.SIMKL]

    def test_there_is_still_a_way_to_have_nobody_to_ask(self):
        """The negative half, and it has to exist or the gate above stops
        meaning anything: with the one credential-free calendar switched off and
        no client id for the other, the list is empty and the page says so."""
        nobody = Settings(simkl_public_calendar_enabled=False)
        assert providers.for_calendar_sources(nobody) == []

    def test_the_configured_source_is_the_usable_one(self):
        configured = Settings(trakt_client_id="id", simkl_public_calendar_enabled=False)
        assert [p.source for p in providers.for_calendar_sources(configured)] == [Source.TRAKT]

    def test_trakts_calendar_asks_for_the_client_id_and_not_the_token(self):
        """The catalogue question, not the private one: /calendars/all/
        authenticates with the `trakt-api-key` header and sends no bearer, so an
        instance that has never issued a token still has Trakt's calendar."""
        port = providers.get(Source.TRAKT).calendar_port
        assert port.calendar_configured(Settings(trakt_client_id="id"))
        assert not port.calendar_configured(Settings(trakt_access_token="token"))

    def test_simkls_calendar_needs_no_credential_of_any_kind(self):
        """Its months are public CDN files (app/providers/simkl/calendar.py):
        there is no credential whose absence could make them unreadable, which
        is why the predicate is on the PORT — `is_configured` and
        `catalogue_is_configured` are both false here and both irrelevant."""
        port = providers.get(Source.SIMKL).calendar_port
        assert port.calendar_configured(Settings())

    def test_a_source_that_could_answer_is_listed_whether_or_not_it_is_set_up(self):
        """The two questions are different and both are asked. "Who could put
        something on a calendar" is a property of the SOURCE and decides who the
        fill asks; "who can we actually use" adds the credentials and is what the
        page checks before it renders an explanation instead of a month. Simkl's
        calendar needs no credential at all, so it is listed here with NO
        Settings object in play whatsoever — calendar_sources() takes none."""
        assert {p.source for p in providers.calendar_sources()} == {
            Source.TMDB, Source.TRAKT, Source.SIMKL}

    def test_a_source_with_no_calendar_port_would_be_in_neither_list(self):
        """The negative half of the rule above, pinned against whichever source
        genuinely carries no calendar_port today — asserted through the
        registry rather than by name, so this does not silently stop meaning
        anything the day every registered source has one."""
        no_calendar = [p for p in providers.registered().values() if p.calendar_port is None]
        for provider in no_calendar:
            assert provider.source not in [p.source for p in providers.calendar_sources()]

    def test_both_are_usable_when_both_are_set_up(self):
        both = Settings(trakt_client_id="id", trakt_access_token="token",
                        simkl_client_id="id", simkl_access_token="token")
        assert {p.source for p in providers.for_calendar_sources(both)} == {Source.TRAKT, Source.SIMKL}

    def test_the_tracker_credential_has_no_say_over_simkls_calendar(self):
        """`is_configured` for Simkl asks the TRACKER's credential (client id
        plus access token), and the calendar CDN needs neither — so an instance
        that has linked nothing gets Simkl's calendar anyway. Narrowing on the
        provider-level predicate is what made a working public feed read as no
        source at all."""
        trakt_only = Settings(trakt_client_id="id", trakt_access_token="token")
        assert not providers.get(Source.SIMKL).is_configured(trakt_only)
        assert Source.SIMKL in [p.source for p in providers.calendar_sources()]
        assert Source.SIMKL in [p.source for p in providers.for_calendar_sources(trakt_only)]

    def test_an_accounts_auto_asks_every_source_the_instance_can_fill_from(self):
        """The fill is instance-credentialed, so an account's links have no say
        in it: `calendar_sources` takes no linked set, and `auto` therefore
        cannot come out narrower for somebody who linked one service than for a
        share-link visitor who is nobody at all."""
        auto = prefs.SourcePrefs(user_id=1, calendar_source=prefs.AUTO)
        assert ({p.source for p in providers.calendar_sources(prefs=auto)}
                == {p.source for p in providers.calendar_sources()})

    def test_a_stated_selection_still_narrows_the_fill(self):
        named = prefs.SourcePrefs(user_id=1, calendar_source="simkl")
        assert [p.source for p in providers.calendar_sources(prefs=named)] == [Source.SIMKL]

    def test_default_settings_still_admits_unconfigured_simkl(self):
        """simkl_public_calendar_enabled defaults True, so passing a Settings
        object at all — which no caller did before this switch existed — must
        not itself change who is asked. An instance that has touched nothing
        about Simkl keeps reading its calendar exactly as before."""
        assert Source.SIMKL in [
            p.source for p in providers.calendar_sources(settings=Settings())]

    def test_switching_it_off_drops_unconfigured_simkl_from_the_fill(self):
        off = Settings(simkl_public_calendar_enabled=False)
        sources = {p.source for p in providers.calendar_sources(settings=off)}
        assert sources == {Source.TMDB, Source.TRAKT}

    def test_switching_it_off_drops_a_configured_simkl_too(self):
        """The switch answers exactly one question — does Simkl contribute to
        this instance's calendar — and it is not conditioned on credentials.
        It used to apply only while Simkl was unconfigured, which was very
        nearly always the case (nothing about the calendar needs an
        instance-level Simkl token) and left the rare operator who HAD set one
        with a switch they could not use."""
        configured_off = Settings(
            simkl_client_id="id", simkl_access_token="token",
            simkl_public_calendar_enabled=False)
        assert Source.SIMKL not in [
            p.source for p in providers.calendar_sources(settings=configured_off)]

    def test_a_configured_simkl_is_still_admitted_with_the_switch_on(self):
        """Default-on is what every existing instance has, configured or not."""
        configured_on = Settings(simkl_client_id="id", simkl_access_token="token")
        assert Source.SIMKL in [
            p.source for p in providers.calendar_sources(settings=configured_on)]

    def test_no_settings_at_all_behaves_like_the_switch_being_on(self):
        """The pre-existing, no-argument call every untouched caller still
        makes must keep admitting Simkl — the switch can only ever narrow a
        caller that was updated to pass a Settings object."""
        assert Source.SIMKL in [p.source for p in providers.calendar_sources()]

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


# A preference, the set of every service name, and a Settings carrying a usable
# credential for both — the three arguments the selector takes, spelled once
# because most of these tests vary exactly one of them.
#
# THE PREFERENCE NO LONGER NARROWS THE TRACKER AT ALL, which is why this one is
# the bare default: reading somebody's history needs their token, so what they
# have LINKED is the whole statement, and a second one beside it could only
# agree with the links or contradict them.
_ALL_SOURCES = prefs.SourcePrefs(user_id=1)
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

    def test_the_links_are_the_whole_of_what_is_asked(self):
        """A service this account has not connected has nothing to answer with,
        so it is not asked. This used to be a preference AND the links; the
        preference is gone and the links are what remain, which is what they
        always actually meant."""
        ports = providers.for_tracker_ports(
            _ALL_SOURCES, {str(Source.SIMKL)}, _CONFIGURED)
        assert [source for source, _p in ports] == [Source.SIMKL]

    def test_an_account_with_one_link_is_on_the_path_it_always_was(self):
        ports = providers.for_tracker_ports(
            prefs.SourcePrefs(user_id=1), {str(Source.TRAKT)}, _CONFIGURED)
        assert [source for source, _p in ports] == [Source.TRAKT]

    def test_an_account_with_no_links_is_asked_nothing(self):
        assert providers.for_tracker_ports(_ALL_SOURCES, set(), _CONFIGURED) == []

    def test_an_unconfigured_source_is_never_asked(self):
        """Admitted by the preference and linked, but with no credential on this
        request's settings, is not something to call — see app/auth/viewer.py's settings_for_user,
        which is what puts an account's own tokens there."""
        ports = providers.for_tracker_ports(
            _ALL_SOURCES, _ALL_NAMES, Settings(trakt_client_id="c", trakt_access_token="t"))
        assert [source for source, _p in ports] == [Source.TRAKT]

    def test_the_calendar_switch_is_not_a_gate_on_the_tracker(self):
        """simkl_public_calendar_enabled governs exactly one thing — whether
        Simkl contributes to the CALENDAR. Reading somebody's own Simkl viewing
        is a different question answered by their own credential, and turning
        the calendar off must not take their watch history with it."""
        for enabled in (True, False):
            settings = dataclasses.replace(
                _CONFIGURED, simkl_public_calendar_enabled=enabled)
            ports = providers.for_tracker_ports(_ALL_SOURCES, _ALL_NAMES, settings)
            assert [source for source, _p in ports] == [Source.TRAKT, Source.SIMKL]

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


TMDB_TOKEN = "eyJhbGciOiJIUzI1NiJ9.eyJhdWQiOiJ0ZXN0In0.c2lnbmF0dXJl"


class TestSharedIdLookup:
    """The registry answering "who can be asked which of its titles a shared id
    names" — every service that answers the exact lookup, whichever box found the
    title, and NOT the search boxes' list (see `for_shared_id_lookup`).

    THE SERVICES ASK GENUINELY DIFFERENT QUESTIONS HERE, and that is not an
    inconsistency to paper over: Trakt's lookup authenticates with the
    instance's own client id and TMDB's with its read token, so either alone is
    real askable state. Simkl's v2 /redirect requires a USER token (401
    user_token_required otherwise), so Simkl needs `simkl_configured` (client
    id AND a token) — which only reads true on a per-viewer Settings object. See
    app/providers/simkl/__init__.py._SimklProvider.catalogue_is_configured.
    """

    def test_no_source_is_offered_until_one_has_a_catalogue_credential(self):
        assert providers.for_shared_id_lookup(Settings()) == []

    def test_a_trakt_only_instance_gets_exactly_trakt(self):
        trakt_only = Settings(trakt_client_id="id")
        assert [s for s, _p in providers.for_shared_id_lookup(trakt_only)] == [Source.TRAKT]

    def test_a_simkl_client_id_alone_is_not_enough_to_ask_it(self):
        simkl_only = Settings(simkl_client_id="id")
        assert providers.for_shared_id_lookup(simkl_only) == []

    def test_a_simkl_linked_viewer_gets_simkl(self):
        simkl_linked = Settings(simkl_client_id="id", simkl_access_token="viewer-tok")
        assert [s for s, _p in providers.for_shared_id_lookup(simkl_linked)] == [Source.SIMKL]

    def test_every_service_is_asked_in_registry_order_whatever_a_box_searches(self):
        """TMDB being every box's default does not stop Trakt and Simkl being
        asked by id: that lookup is how a title TMDB found learns their ids."""
        every = Settings(tmdb_api_key=TMDB_TOKEN, trakt_client_id="id",
                         simkl_client_id="id", simkl_access_token="viewer-tok")
        assert [s for s, _p in providers.for_shared_id_lookup(every)] == [
            Source.TMDB, Source.TRAKT, Source.SIMKL]

    def test_trakt_asks_the_catalogue_question_not_the_private_one(self):
        client_id_only = Settings(trakt_client_id="id", trakt_access_token="")
        assert not Settings.trakt_configured.fget(client_id_only)
        assert {s for s, _p in providers.for_shared_id_lookup(client_id_only)} == {Source.TRAKT}

    def test_the_ports_returned_are_the_registered_search_ports(self):
        both = Settings(trakt_client_id="id",
                        simkl_client_id="id", simkl_access_token="viewer-tok")
        pairs = dict(providers.for_shared_id_lookup(both))
        assert pairs[Source.TRAKT] is providers.get(Source.TRAKT).search_port
        assert pairs[Source.SIMKL] is providers.get(Source.SIMKL).search_port


class TestForSearch:
    """Who a search box asks: the service searched by default, and a service the
    viewer has LINKED only when the request adds it by name — see `for_search`.
    No service stands in for another."""

    def asked(self, settings, also=()):
        return [entry.catalogue.key for entry in providers.for_search(settings, also).asked]

    @staticmethod
    def keys(catalogues):
        return tuple(c.key for c in catalogues)

    def test_the_default_is_tmdb_for_everyone_when_its_token_is_set(self):
        settings = Settings(tmdb_api_key=TMDB_TOKEN, trakt_client_id="id")
        assert self.asked(settings) == ["tmdb"]

    def test_no_tmdb_token_is_said_and_nothing_is_asked_in_its_place(self):
        """A Trakt client id is there, and Trakt is still not searched: the
        box says TMDB is not configured instead."""
        plan = providers.for_search(Settings(trakt_client_id="id"))
        assert plan.asked == []
        assert plan.unconfigured == ("tmdb",)

    def test_a_linked_service_is_offered_and_not_asked(self):
        linked = Settings(tmdb_api_key=TMDB_TOKEN, trakt_client_id="id",
                          trakt_access_token="viewer-tok",
                          simkl_client_id="id", simkl_access_token="viewer-tok")
        plan = providers.for_search(linked)
        assert self.asked(linked) == ["tmdb"]
        # Simkl offers its two catalogues, each added on its own.
        assert self.keys(plan.offered) == ("trakt", "simkl", "simkl-anime")
        assert self.keys(plan.order) == ("tmdb", "trakt", "simkl", "simkl-anime")

    def test_an_unlinked_service_is_not_offered(self):
        """A Trakt client id is the instance's; "also search Trakt" is offered
        to a viewer who linked Trakt and nobody else."""
        plan = providers.for_search(Settings(tmdb_api_key=TMDB_TOKEN, trakt_client_id="id"))
        assert plan.offered == ()

    def test_an_added_service_is_asked_in_registry_order(self):
        linked = Settings(tmdb_api_key=TMDB_TOKEN, trakt_client_id="id",
                          trakt_access_token="viewer-tok",
                          simkl_client_id="id", simkl_access_token="viewer-tok")
        plan = providers.for_search(linked, ("simkl", "trakt"))
        assert self.asked(linked, ("simkl", "trakt")) == ["tmdb", "trakt", "simkl"]
        assert self.keys(plan.offered) == ("simkl-anime",)

    def test_a_linked_service_still_works_with_no_tmdb_token(self):
        linked = Settings(simkl_client_id="id", simkl_access_token="viewer-tok")
        plan = providers.for_search(linked, ("simkl-anime",))
        assert self.asked(linked, ("simkl-anime",)) == ["simkl-anime"]
        assert plan.unconfigured == ("tmdb",)

    @pytest.mark.parametrize("name", ["nope", "tmdb", "trakt"])
    def test_a_name_the_viewer_may_not_add_is_refused(self, name):
        """Unknown, already the default, or not linked: each is a refusal, never
        quietly read as the default search."""
        with pytest.raises(providers.UnknownSearchService):
            providers.for_search(Settings(tmdb_api_key=TMDB_TOKEN, trakt_client_id="id"),
                                 (name,))



class TheEpochConversionIsPortableTests(unittest.TestCase):
    """`epoch_moment` — a record's air time as an instant, for ANY value.

    IT EXISTS BECAUSE `datetime.fromtimestamp` IS NOT PORTABLE. On Windows it
    hands the value to the platform C library, which refuses anything before
    1970 with OSError [Errno 22]. That reached production through the calendar
    search: a catalogue lookup answers for whatever a service knows, services
    know about television older than the epoch, and one 1969 premiere took the
    whole request down.
    """

    def test_a_time_before_the_epoch_converts_instead_of_raising(self):
        ts = datetime(1969, 6, 1, tzinfo=timezone.utc).timestamp()
        self.assertEqual(epoch_moment(ts).date(), date(1969, 6, 1))

    def test_it_reaches_well_back_past_the_platform_limit(self):
        ts = datetime(1930, 1, 1, tzinfo=timezone.utc).timestamp()
        self.assertEqual(epoch_moment(ts).date(), date(1930, 1, 1))

    def test_it_agrees_with_the_platform_wherever_the_platform_answers(self):
        """A WIDENING, NOT A CHANGE. Every date this app already displayed has
        to keep converting identically, or swapping the call everywhere would
        have moved dates nobody asked to move."""
        for moment in (datetime(1971, 1, 1, tzinfo=timezone.utc),
                       datetime(2000, 2, 29, 12, 30, tzinfo=timezone.utc),
                       datetime(2026, 9, 3, 23, 59, 59, tzinfo=timezone.utc)):
            with self.subTest(moment=moment):
                ts = moment.timestamp()
                self.assertEqual(epoch_moment(ts),
                                 datetime.fromtimestamp(ts, timezone.utc))

    def test_it_answers_in_utc(self):
        """The callers all go on to ask for a UTC date or convert to a viewer's
        zone themselves; a naive answer would silently be read as local."""
        self.assertEqual(epoch_moment(0).tzinfo, timezone.utc)
        self.assertEqual(epoch_moment(0).date(), date(1970, 1, 1))


class APictureCarriesTheOnesBehindItTests(unittest.TestCase):
    """providers.base.proxied_image's `fallbacks`: what the image proxy tries
    when the first picture will not load. Measured against wsrv.nl: a proxied
    `default` is answered with a 302 to it on the same host; a raw one would be
    redirected to a host the page's img-src refuses."""

    def test_no_fallbacks_is_the_address_it_always_was(self):
        from urllib.parse import quote

        from app.providers.base import proxied_image
        self.assertEqual(proxied_image("https://a/1.jpg"),
                         f"https://wsrv.nl/?url={quote('https://a/1.jpg', safe='')}&q=90&w=440&we")
        self.assertEqual(proxied_image("https://a/1.jpg", fallbacks=[]),
                         proxied_image("https://a/1.jpg"))

    def test_each_fallback_is_proxied_with_the_rest_behind_it(self):
        from urllib.parse import parse_qs, urlsplit

        from app.providers.base import proxied_image
        address = proxied_image("https://a/1.jpg", fallbacks=["https://b/2.jpg", "https://c/3.jpg"])
        first = parse_qs(urlsplit(address).query)
        self.assertEqual(first["url"], ["https://a/1.jpg"])
        second = first["default"][0]
        self.assertTrue(second.startswith("https://wsrv.nl/"), "a raw default leaves the host")
        self.assertEqual(second, proxied_image("https://b/2.jpg", fallbacks=["https://c/3.jpg"]))
        third = parse_qs(urlsplit(second).query)["default"][0]
        self.assertEqual(third, proxied_image("https://c/3.jpg"))

    def test_the_picture_itself_and_blanks_are_not_its_own_fallback(self):
        from app.providers.base import proxied_image
        self.assertEqual(proxied_image("https://a/1.jpg", fallbacks=["", "https://a/1.jpg"]),
                         proxied_image("https://a/1.jpg"))
        self.assertEqual(proxied_image("", fallbacks=["https://b/2.jpg"]), "")
