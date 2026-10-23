"""Every registered source really does satisfy the Protocol it is registered as.

WHY THIS IS WORTH MORE THAN ITS SIZE. app/providers/base.py declares what a
source must provide and app/providers/trakt/__init__.py provides it, and until
now the only thing keeping the two in step was whoever edited one noticing they
must edit the other. `register()` takes whatever it is handed.

The two directions are not equally dangerous. REMOVING a member from the Protocol
without removing its implementation leaves dead code, which is untidy and
harmless — a `fetch_calendar` sat here exactly like that. ADDING one and
forgetting a source is an AttributeError at run time, raised on whichever source
the user happens to have configured, and the entire suite is blind to it. That is
the direction a second source will travel repeatedly.

WHAT runtime_checkable ACTUALLY VERIFIES, STATED HONESTLY: member PRESENCE, and
nothing else. `isinstance(x, Provider)` asks whether the named attributes and
methods exist on x — it does NOT compare signatures, so a source whose method
takes the wrong arguments passes here and still fails in production. Closing that
gap needs `inspect.signature` over the Protocol's members, which would need no
new dependency; it is not done here because the members are few and the argument
lists are read at every call site anyway, so the cheaper check is the one that
earns its keep. If a source ever gains a method the app calls indirectly, revisit
that trade rather than assuming this file covers it.
"""
from __future__ import annotations

import inspect
import unittest

from app import providers
from app.config import Settings
from app.providers.base import (CalendarPort, DetailPort, LibraryPort, Media,
                                PlayCountPort, Provider, SearchPort, Source, SyncPort,
                                WatchlistPort)
from app.providers.trakt import detail as trakt_detail
from tests.support import DatabaseTestCase


# EACH SERVICE'S CATALOGUE CREDENTIAL, AND ONLY THAT — the instance-wide value
# its public reads authenticate with. A TABLE WITH AN ENTRY PER SERVICE rather
# than a field name spelled from the service's own name, because the services
# do not share one shape: Trakt and Simkl take a client id, TMDB a read token. A
# registered service missing here fails `test_every_registered_source_has_a_
# credential_entry` rather than being quietly skipped.
TMDB_TOKEN = "eyJhbGciOiJIUzI1NiJ9.eyJhdWQiOiJ0ZXN0In0.c2lnbmF0dXJl"
CATALOGUE_CREDENTIALS = {
    "tmdb": {"tmdb_api_key": TMDB_TOKEN},
    "trakt": {"trakt_client_id": "an-id"},
    "simkl": {"simkl_client_id": "an-id"},
}


# And each service's PRIVATE credential — one person's own token — where it has
# one. TMDB holds none: nobody here links a TMDB account, so its entry is empty
# and says so rather than being absent.
PRIVATE_CREDENTIALS = {
    "tmdb": {},
    "trakt": {"trakt_access_token": "a-token"},
    "simkl": {"simkl_access_token": "a-token"},
}

# Whether each service carries a search port at all, and whether every box asks
# it without anybody choosing it (Capabilities.searched_by_default) — one entry
# per service, so a service registered later is a decision written here.
SEARCHABLE = {"tmdb": True, "trakt": True, "simkl": True}
SEARCHED_BY_DEFAULT = {"tmdb": True, "trakt": False, "simkl": False}


def catalogue_settings(*sources) -> Settings:
    """A Settings carrying these services' catalogue credentials and nothing
    private — the live instance's own shape."""
    fields = {}
    for source in sources:
        fields.update(CATALOGUE_CREDENTIALS[str(source)])
    return Settings(**fields)


class EveryServiceHasACredentialEntryTests(unittest.TestCase):

    def test_every_registered_source_has_a_credential_entry(self):
        registered = {str(s) for s in providers.registered()}
        for table in (CATALOGUE_CREDENTIALS, PRIVATE_CREDENTIALS, SEARCHABLE,
                      SEARCHED_BY_DEFAULT):
            self.assertEqual(set(table), registered)


class RegisteredProvidersConformTests(unittest.TestCase):
    """Walk the registry — not a hand-written list of sources, which would go
    stale the moment a second one is added without anybody updating it."""

    def setUp(self):
        self.registry = providers.registered()

    def test_the_registry_is_not_empty(self):
        """Guards every other test in this file. A registry that failed to load
        would make them all pass by iterating nothing, which is the shape of
        vacuous test this suite has been bitten by before."""
        self.assertTrue(self.registry, "no sources registered; the checks below prove nothing")

    def test_every_registered_source_satisfies_provider(self):
        for source, provider in self.registry.items():
            with self.subTest(source=source):
                self.assertIsInstance(provider, Provider)

    def test_a_source_is_registered_under_the_key_it_claims(self):
        # The registry is a dict keyed by Source, and `provider.source` is the
        # same fact stated on the object. A mismatch means a lookup by key hands
        # back something that disagrees about who it is.
        for source, provider in self.registry.items():
            with self.subTest(source=source):
                self.assertEqual(provider.source, source)

    def test_a_source_claiming_private_reads_carries_a_sync_port(self):
        """`capabilities.private_user_data` is how the tracker decides a source
        can back it; `sync_port` is what it then calls. A source asserting the
        first with None in the second is lying in a way nothing else catches —
        the tracker finds a usable source and then has nothing to call."""
        for source, provider in self.registry.items():
            with self.subTest(source=source):
                if provider.capabilities.private_user_data:
                    self.assertIsNotNone(
                        provider.sync_port,
                        f"{source} claims private user data but carries no sync port")

    def test_every_sync_port_present_satisfies_syncport(self):
        for source, provider in self.registry.items():
            port = provider.sync_port
            if port is None:
                continue
            with self.subTest(source=source):
                self.assertIsInstance(port, SyncPort)


class CalendarPortIsDeclaredAlongsideTheEndpointsTests(unittest.TestCase):
    """`capabilities.endpoints` is how the cache decides a source has a calendar;
    `calendar_port` is what it then calls. The pairing is the same claim
    `private_user_data`/`sync_port` makes about the tracker, and it goes wrong the
    same way: a source declaring endpoints with None in the port is found usable
    and then has nothing to ask.
    """

    def test_a_source_declaring_calendar_endpoints_carries_a_calendar_port(self):
        for source, provider in providers.registered().items():
            with self.subTest(source=source):
                if provider.capabilities.endpoints:
                    self.assertIsNotNone(
                        provider.calendar_port,
                        f"{source} declares calendar endpoints but carries no calendar port")

    def test_every_calendar_port_present_satisfies_calendarport(self):
        for source, provider in providers.registered().items():
            port = provider.calendar_port
            if port is None:
                continue
            with self.subTest(source=source):
                self.assertIsInstance(port, CalendarPort)

    def test_at_least_one_registered_source_can_fill_a_window(self):
        """Or the whole calendar is unreachable and every test of it is
        vacuous."""
        self.assertTrue(
            [p for p in providers.registered().values() if p.calendar_port is not None],
            "no source implements the window fetch; the calendar cannot be filled")

    def test_every_calendar_port_answers_whether_it_can_be_read_at_all(self):
        """`for_calendar_sources` asks the PORT rather than the provider, because
        what a calendar costs to read differs per source: one wants the instance's
        client id, another is a public feed wanting nothing. A port without this
        would fail as an AttributeError on whichever source an operator happens to
        have — and it has to answer without a call, since it gates the call."""
        for source, provider in providers.registered().items():
            port = provider.calendar_port
            if port is None:
                continue
            with self.subTest(source=source):
                self.assertIsInstance(port.calendar_configured(Settings()), bool)

    def test_no_calendar_port_asks_the_private_question(self):
        """The fault this predicate exists to remove: a calendar read never uses
        a viewer's token, so no source may require one to be readable. Asserted
        against a Settings carrying each source's client id and nothing else —
        every calendar that can be read at all must be readable there."""
        for source, provider in providers.registered().items():
            port = provider.calendar_port
            if port is None:
                continue
            with self.subTest(source=source):
                settings = catalogue_settings(source)
                self.assertFalse(provider.is_configured(settings))
                self.assertTrue(port.calendar_configured(settings))


class LibraryPortIsOptionalTests(unittest.TestCase):
    """LibraryPort is deliberately NOT part of SyncPort, and this is what says so.

    A source that can hand over a whole library at once is asked for it and
    matched on the shared identity; one that cannot is asked per title with its
    own id. Both are correct, so the tracker branches on this isinstance and
    nothing else — which makes "does this port claim the library read" a fact
    worth pinning rather than an implementation detail. At least one registered
    source must satisfy it, or the branch is dead and every test of it is
    vacuous.
    """

    def test_at_least_one_registered_source_can_hand_over_a_library(self):
        ports = [provider.sync_port for provider in providers.registered().values()
                 if provider.sync_port is not None]
        self.assertTrue([port for port in ports if isinstance(port, LibraryPort)],
                        "no source implements the library read; the tracker's "
                        "identity-keyed baseline is unreachable")

    def test_a_sync_port_without_the_library_read_is_not_one(self):
        """The negative half, and the one that matters: a port answering per
        title must not be handed a whole-library question it cannot answer."""
        for source, provider in providers.registered().items():
            port = provider.sync_port
            if port is None or hasattr(port, "fetch_library"):
                continue
            with self.subTest(source=source):
                self.assertNotIsInstance(port, LibraryPort)


class PlayCountPortIsOptionalTooTests(unittest.TestCase):
    """The third port protocol, and the same rule: optional, branched on by
    isinstance, and dead the moment no registered source satisfies it.

    IT IS NOT A SMALLER LibraryPort. A source that hands over its whole library
    re-states what it holds per episode, so nothing needs telling which titles
    moved — re-reading the list corrects a removal on its own. This exists for the
    source whose whole-library read carries no episodes at all, where the only
    other way to learn what changed is to ask about every title in turn.
    """

    def test_at_least_one_registered_source_can_sweep_its_play_counts(self):
        ports = [provider.sync_port for provider in providers.registered().values()
                 if provider.sync_port is not None]
        self.assertTrue([port for port in ports if isinstance(port, PlayCountPort)],
                        "no source implements the play-count sweep; the tracker's "
                        "targeted re-baseline is unreachable")

    def test_the_two_optional_ports_are_not_the_same_claim(self):
        """A source is free to satisfy both, neither, or one — but a source that
        satisfies the library read and nothing else must not be swept, and one
        that can only be swept must not be asked for a library."""
        for source, provider in providers.registered().items():
            port = provider.sync_port
            if port is None:
                continue
            with self.subTest(source=source):
                self.assertEqual(isinstance(port, PlayCountPort),
                                 hasattr(port, "fetch_play_counts"))


class EverySourceCanDescribeATitleTests(unittest.TestCase):
    """DetailPort, and the reason it is not optional in practice.

    The detail modal asks the registry which source can describe the card in
    front of it and never names a service. A registered source with no detail
    port is therefore one whose cards open on a refusal — which is exactly the
    state a Simkl-only card was in — and the refusal looks like a bug in the
    modal rather than like a source that was never given an answer.
    """

    def test_every_registered_source_carries_a_detail_port(self):
        for source, provider in providers.registered().items():
            with self.subTest(source=source):
                self.assertIsNotNone(
                    provider.detail_port,
                    f"{source} can list a title but cannot describe one")

    def test_every_detail_port_present_satisfies_detailport(self):
        for source, provider in providers.registered().items():
            port = provider.detail_port
            if port is None:
                continue
            with self.subTest(source=source):
                self.assertIsInstance(port, DetailPort)

    def test_the_catalogue_question_is_not_the_private_one(self):
        """A public per-title lookup must not be gated on a token it never sends.
        Asserted against a Settings carrying each source's ID and nothing else,
        which is the live instance's own shape for Simkl.
        """
        for source, provider in providers.registered().items():
            with self.subTest(source=source):
                settings = catalogue_settings(source)
                self.assertTrue(provider.detail_port.catalogue_configured(settings))
                self.assertFalse(provider.is_configured(settings))


def keys_only_one_source_has(answers: dict[str, frozenset[str]]) -> dict[str, set[str]]:
    """{source: the keys ONLY that source answered}, empty when they all agree.

    A plain symmetric difference against the shared core, so a failure names the
    source that grew a key alone rather than saying two sets differ — which is
    the whole question when a modal field lights on one service and not the
    other.
    """
    shared = frozenset.intersection(*answers.values())
    return {source: set(extra) for source, keys in answers.items()
            if (extra := keys - shared)}


class EveryDetailPortAnswersTheSameKeysTests(DatabaseTestCase):
    """DetailPort's key-set promise, asserted rather than stated.

    The protocol's own docstring says the answer's key set is the same whoever
    gives it, and gives the reason: ONE renderer draws the modal, so a key only
    one source answers reads as a template bug rather than as a lookup that
    source cannot make. Nothing enforced it, and it had already gone wrong three
    keys deep — `anime_type`, `imdb_rating` and `release_types_by_country` were
    on one package's answer and absent from the other's, so a chip could never
    light on a title the other service described whatever the data said.

    WHY THIS IS A CONFORMANCE TEST AND NOT A UNIT ONE, and it is the same reason
    `EveryIdReadingNamespacesTheSlugTests` below is: the rule is about the
    RELATIONSHIP between two packages that never import each other, so neither
    one's own tests can see it. A key added to one side alone is invisible on
    that side — its own modal renders perfectly.

    ASKED cache_only AGAINST AN EMPTY CACHE, which is deliberate on two counts.
    It is the contract's own hardest case — every key present, every value empty
    — and it needs no per-source stubbing, so a third registered source is
    covered by this test the day it is registered rather than the day somebody
    remembers to add a double for it. conftest's outbound guard is what says the
    reads really cost nothing.
    """

    # A title id nothing has ever cached, so every port takes its miss path.
    UNKNOWN_ID = "1"

    # One cell per shape a caller can ask for: a season of a show, a show with no
    # season named, and a film. A key that appears only when a season was given
    # is the same drift one page-render away.
    SHAPES = ((Media.SHOW, 1), (Media.SHOW, None), (Media.MOVIE, None))

    def _settings(self):
        """Every source's CATALOGUE credential and no private one — the live
        instance's own shape, and what makes each port take its real path rather
        than an unconfigured shortcut."""
        return catalogue_settings(*(source for source, _port in self._ports()))

    def _ports(self):
        return [(str(source), provider.detail_port)
                for source, provider in providers.registered().items()
                if provider.detail_port is not None]

    async def _answers(self, media, season) -> dict[str, frozenset[str]]:
        settings = self._settings()
        return {source: frozenset(await port.fetch_details(
                    settings, media, self.UNKNOWN_ID, season, cache_only=True))
                for source, port in self._ports()}

    async def test_more_than_one_source_answers_or_this_file_proves_nothing(self):
        """Two sets agree trivially when there is one of them."""
        self.assertGreater(len(self._ports()), 1,
                           "only one source can describe a title; the key-set "
                           "comparison below is vacuous")

    async def test_every_port_answers_the_same_keys(self):
        for media, season in self.SHAPES:
            with self.subTest(media=str(media), season=season):
                answers = await self._answers(media, season)
                self.assertEqual(
                    keys_only_one_source_has(answers), {},
                    "a detail key one source answers alone renders as a template "
                    "bug on every other source's title")

    async def test_a_port_answers_the_same_keys_whatever_it_is_asked_about(self):
        """The other axis. A film's answer and a show's come from the same
        projection, so a key that appears only for one of them would light a chip
        on half a calendar and not the other half."""
        for source, port in self._ports():
            settings = self._settings()
            shapes = {}
            for media, season in self.SHAPES:
                shapes[f"{media}/{season}"] = frozenset(await port.fetch_details(
                    settings, media, self.UNKNOWN_ID, season, cache_only=True))
            with self.subTest(source=source):
                self.assertEqual(keys_only_one_source_has(shapes), {})

    async def test_the_answer_is_the_modal_field_set_and_not_an_empty_dict(self):
        """Guards every comparison above: two ports that both answered nothing
        would agree perfectly. These are the keys the modal is FOR."""
        answers = await self._answers(Media.SHOW, 1)
        for source, keys in answers.items():
            with self.subTest(source=source):
                self.assertLessEqual(
                    {"title", "overview", "episodes", "genres", "ids", "imdb_rating",
                     "videos"},
                    set(keys))
                # ONE KEY FOR A TITLE'S VIDEOS, not a list beside a single
                # trailer: two keys for one fact is two answers to keep in step.
                self.assertNotIn("trailer", keys)

    async def test_every_port_answers_its_own_video_list_as_videos(self):
        """`fetch_videos` is how the panel asks the service that is NOT
        describing a title, so every port must have it and answer the shape the
        description carries — a list of `Video`, empty on a cold cache."""
        settings = self._settings()
        for source, port in self._ports():
            for media in (Media.SHOW, Media.MOVIE):
                with self.subTest(source=source, media=str(media)):
                    self.assertEqual(await port.fetch_videos(
                        settings, media, self.UNKNOWN_ID, cache_only=True), [])

    def test_the_comparison_would_actually_report_a_one_sided_key(self):
        """The check reacting, because "the sets were equal" is weak evidence on
        its own — see this file's closing tests for the same habit."""
        self.assertEqual(
            keys_only_one_source_has({"one": frozenset({"title"}),
                                      "two": frozenset({"title", "imdb_rating"})}),
            {"two": {"imdb_rating"}})
        self.assertEqual(
            keys_only_one_source_has({"one": frozenset({"title"}),
                                      "two": frozenset({"title"})}), {})


class EverySourceCanBeSearchedTests(unittest.TestCase):
    """SearchPort, and the reason it is not optional in practice.

    The tracker's manual add flow asks the registry which sources can be
    searched and never names one. A registered source with no search port is
    therefore one an instance with no OTHER catalogue configured cannot add
    anything through at all — the state a Trakt-only build left a Simkl-only
    instance in before this port existed.
    """

    def test_a_search_port_is_carried_exactly_where_the_table_says(self):
        for source, provider in providers.registered().items():
            with self.subTest(source=source):
                self.assertEqual(
                    provider.search_port is not None, SEARCHABLE[str(source)],
                    f"{source}'s search port does not match SEARCHABLE")

    def test_which_services_every_box_asks_is_exactly_the_table(self):
        for source, provider in providers.registered().items():
            with self.subTest(source=source):
                self.assertEqual(provider.capabilities.searched_by_default,
                                 SEARCHED_BY_DEFAULT[str(source)])

    def test_a_service_searched_by_default_carries_a_search_port(self):
        """Declaring the default without the port would leave every box saying
        it searched a service it never asked."""
        for source, provider in providers.registered().items():
            if provider.capabilities.searched_by_default:
                with self.subTest(source=source):
                    self.assertIsNotNone(provider.search_port)

    def test_every_search_port_present_satisfies_searchport(self):
        for source, provider in providers.registered().items():
            port = provider.search_port
            if port is None:
                continue
            with self.subTest(source=source):
                self.assertIsInstance(port, SearchPort)

    def test_the_catalogue_question_is_not_the_private_one(self):
        """A public search must not be gated on a token it never sends —
        true of every registered source EXCEPT Simkl, whose v2 search and
        /redirect genuinely DO require a user token (401 user_token_required
        otherwise), where its per-title DetailPort lookups stay anonymous —
        see app/providers/simkl/__init__.py._SimklProvider.catalogue_is_configured.
        Asserted against a Settings carrying each source's ID and nothing
        else, which is the live instance's own shape for a Simkl catalogue
        that answers detail lookups but not search — the same check
        `EverySourceCanDescribeATitleTests` makes for DetailPort, aimed at
        `Provider.catalogue_is_configured` rather than the detail port's own
        predicate.
        """
        for source, provider in providers.registered().items():
            with self.subTest(source=source):
                settings = catalogue_settings(source)
                self.assertFalse(provider.is_configured(settings))
                if source is Source.SIMKL:
                    self.assertFalse(provider.catalogue_is_configured(settings))
                else:
                    self.assertTrue(provider.catalogue_is_configured(settings))


class EveryWatchlistPortWritesTheSameWayTests(DatabaseTestCase):
    """WatchlistPort, the one port that writes to somebody's own list.

    What the Protocol can check is that the verbs exist. What it cannot check is
    the shape of what comes back, and one caller records every service's answer
    through one path — so the shape is declared as types (WatchlistResult,
    WatchlistHolding) and what is asserted here is what every implementation
    must do with them whichever service it speaks for.
    """

    def _ports(self):
        return [(str(source), provider.watchlist_port)
                for source, provider in providers.registered().items()
                if provider.watchlist_port is not None]

    def test_at_least_one_registered_source_writes_a_list(self):
        """Or every other test in this class passes by iterating nothing."""
        self.assertTrue(self._ports(), "no source can write a list; the checks below "
                                       "prove nothing")

    def test_every_watchlist_port_present_satisfies_watchlistport(self):
        for source, port in self._ports():
            with self.subTest(source=source):
                self.assertIsInstance(port, WatchlistPort)

    async def test_an_empty_batch_answers_nothing_and_calls_nothing(self):
        """Every verb, every port. conftest's outbound guard is what says no
        request left: a write step that finds nothing to do must cost nothing,
        and must still get a mapping back rather than None."""
        settings = Settings(**{f"{source}_client_id": "an-id" for source, _ in self._ports()})
        for source, port in self._ports():
            with self.subTest(source=source):
                self.assertEqual(await port.holdings(settings, []), {})
                self.assertEqual(await port.put(settings, []), {})
                self.assertEqual(await port.drop(settings, []), {})
                self.assertEqual(await port.undo(settings, []), {})

    def test_every_port_links_out_without_a_call(self):
        for source, port in self._ports():
            with self.subTest(source=source):
                self.assertTrue(port.list_url("12345").startswith("https://"))

    def test_every_port_links_to_its_own_service(self):
        """The link sits beside that service's switch; a copied port that kept
        another service's address would send the viewer to the wrong site."""
        from urllib.parse import urlparse
        for source, port in self._ports():
            with self.subTest(source=source):
                host = urlparse(port.list_url("12345")).hostname or ""
                self.assertTrue(host == f"{source}.com" or host.endswith(f".{source}.com")
                                or host == f"{source}.tv" or host.endswith(f".{source}.tv"),
                                f"{source} links to {host}")

    def test_the_registry_offers_a_write_only_with_the_viewers_token(self):
        """A write to a person's list is the most private thing a token does, so
        a source is offered only where `is_configured` — the private question —
        holds. A client id alone offers nothing."""
        for source, _port in self._ports():
            with self.subTest(source=source):
                bare = Settings(**{f"{source}_client_id": "an-id"})
                self.assertNotIn(source, [str(s) for s, _ in providers.for_watchlist(bare)])
                linked = Settings(**{f"{source}_client_id": "an-id",
                                     f"{source}_access_token": "a-token"})
                self.assertIn(source, [str(s) for s, _ in providers.for_watchlist(linked)])

    def test_a_source_with_no_port_is_never_offered(self):
        linked = Settings(**{field: value for source in providers.registered()
                             for table in (CATALOGUE_CREDENTIALS, PRIVATE_CREDENTIALS)
                             for field, value in table[str(source)].items()})
        offered = {str(source) for source, _ in providers.for_watchlist(linked)}
        self.assertEqual(offered, {source for source, _ in self._ports()})


# Where each service's own pages live. A TABLE WITH AN ENTRY PER SERVICE, and
# the test below fails for a registered source missing from it: the point of a
# per-service table is that a new service is a visible decision here rather
# than a silent pass.
SERVICE_HOSTS = {"tmdb": "themoviedb.org", "trakt": "trakt.tv", "simkl": "simkl.com"}


class EveryProviderLinksToItsOwnPagesTests(unittest.TestCase):
    """`Provider.title_url` — the outbound button on every card and panel.

    Asked through the registry by detail_source.outbound_links, which used to
    hold a table of two packages' builders and so would have given a third
    service no button at all, with nothing failing.
    """

    def test_every_registered_source_has_a_host_entry(self):
        self.assertEqual(set(SERVICE_HOSTS), {str(s) for s in providers.registered()})

    def test_a_title_this_service_knows_links_to_this_service(self):
        from urllib.parse import urlparse
        for source, provider in providers.registered().items():
            for media in Media:
                with self.subTest(source=str(source), media=str(media)):
                    url = provider.title_url(media, {str(source): "12345"})
                    host = urlparse(url).hostname or ""
                    self.assertTrue(host == SERVICE_HOSTS[str(source)]
                                    or host.endswith("." + SERVICE_HOSTS[str(source)]), url)
                    self.assertIn("12345", url)

    def test_a_title_this_service_does_not_know_has_no_link(self):
        """Not the service's homepage: a button that goes nowhere near the title
        is worse than no button."""
        for source, provider in providers.registered().items():
            with self.subTest(source=str(source)):
                self.assertEqual(provider.title_url(Media.SHOW, {"imdb": "tt1"}), "")
                self.assertEqual(provider.episode_url({"imdb": "tt1"}, 1, 1), "")

    def test_an_episode_this_service_knows_links_to_this_service(self):
        """`Provider.episode_url` — what an episode tick in the tracker's panel
        opens."""
        from urllib.parse import urlparse
        for source, provider in providers.registered().items():
            with self.subTest(source=str(source)):
                url = provider.episode_url({str(source): "12345"}, 2, 3)
                host = urlparse(url).hostname or ""
                self.assertTrue(host == SERVICE_HOSTS[str(source)]
                                or host.endswith("." + SERVICE_HOSTS[str(source)]), url)


class TheCheckWouldActuallyFailTests(unittest.TestCase):
    """A conformance test that cannot fail is decoration.

    isinstance against a Protocol is quiet enough that "it passed" is weak
    evidence on its own, so these show the check reacting: an object missing one
    member is rejected, and one that has them all is accepted.
    """

    def _stand_in(self, omit: str | None = None):
        """An object carrying every Provider member except, optionally, one.

        Built from the Protocol's own declared members rather than a hand-written
        list, so adding a member to Provider does not leave this double quietly
        describing the old shape.
        """
        members = [name for name in Provider.__protocol_attrs__ if name != omit]
        namespace = {}
        for name in members:
            declared = getattr(Provider, name, None)
            namespace[name] = (lambda self, *a, **k: None) if inspect.isfunction(declared) else None
        return type("StandIn", (), namespace)()

    def test_an_object_with_every_member_is_accepted(self):
        self.assertIsInstance(self._stand_in(), Provider)

    def test_an_object_missing_one_member_is_rejected(self):
        for name in Provider.__protocol_attrs__:
            with self.subTest(missing=name):
                self.assertNotIsInstance(self._stand_in(omit=name), Provider)

    def test_the_protocol_declares_the_members_the_registry_depends_on(self):
        # Named explicitly because the two tests above are self-referential: they
        # derive the member list from the Protocol, so they would still pass if
        # somebody deleted a member outright. These are read by
        # app/providers/__init__.py itself.
        for name in ("source", "label", "capabilities", "sync_port", "calendar_port",
                     "detail_port", "search_port", "watchlist_port", "is_configured",
                     "catalogue_is_configured", "title_url", "episode_url"):
            with self.subTest(member=name):
                self.assertIn(name, Provider.__protocol_attrs__)


class EveryIdReadingNamespacesTheSlugTests(unittest.TestCase):
    """A source's readable NAME for a title is namespaced wherever that source's
    ids are read, in every path, in every package.

    WHY THIS IS A CONFORMANCE TEST AND NOT A UNIT ONE. Both services call a
    title's readable name `slug` and disagree about it — Trakt writes
    `the-traitors-2023` where Simkl writes `the-traitors` — so a map carrying only
    the bare key is a name nothing downstream can attribute. Each package answers
    that once, in its own `_ids.normalize`, and every reading of that source's ids
    is supposed to go through it.

    ONE READING DID NOT, AND NOTHING SAID SO. `trakt.detail.ids_map` returned the
    raw block, so titles added to the tracker by hand stored the shared name and
    no `trakt_slug`, while the same title arriving from a calendar window carried
    both. It broke nothing visible — a link falls back to the numeric id — and was
    found by counting stored rows weeks later.

    So the rule is asserted over the PUBLIC id readings rather than trusted to
    each one's own docstring, which is what the module claiming it could not do
    for its neighbour.
    """

    # The functions any code outside a provider package may use to read that
    # source's ids off a payload. A source's `_ids` module is package-internal
    # (the underscore names the MODULE), so these are what the ranker, the
    # tracker and the calendar actually call.
    PUBLIC_ID_READINGS = (
        ("trakt", trakt_detail.ids_map, "slug", "trakt_slug"),
    )

    def test_a_public_id_reading_namespaces_the_service_name(self):
        for source, read, bare, namespaced in self.PUBLIC_ID_READINGS:
            with self.subTest(source=source):
                out = read({"ids": {"trakt": 1, "tmdb": 2, bare: "a-title-2018"}})
                self.assertEqual(
                    out.get(namespaced), "a-title-2018",
                    f"{source}'s public id reading dropped {namespaced}; a stored "
                    f"record built from it can never link by name")

    def test_it_carries_the_bare_key_through_as_well(self):
        """The calendar builds a record's own id from the bare `slug` (see each
        package's calendar.py), so namespacing may not replace it."""
        for source, read, bare, _namespaced in self.PUBLIC_ID_READINGS:
            with self.subTest(source=source):
                out = read({"ids": {"trakt": 1, bare: "a-title-2018"}})
                self.assertEqual(out.get(bare), "a-title-2018")

    def test_it_drops_namespaces_this_app_does_not_name(self):
        """`collect_ids` filters to ID_KEYS, so a namespace nothing can store
        does not travel as though it could."""
        out = trakt_detail.ids_map({"ids": {"trakt": 1, "tvrage": 99}})
        self.assertNotIn("tvrage", out)

    def test_an_empty_block_is_survivable(self):
        for source, read, _bare, _namespaced in self.PUBLIC_ID_READINGS:
            with self.subTest(source=source):
                self.assertEqual(read({}), {})

if __name__ == "__main__":  # pragma: no cover
    unittest.main()
