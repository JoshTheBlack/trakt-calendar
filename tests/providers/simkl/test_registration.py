"""Simkl is in the registry, and what it says about itself is true.

The conformance suite already checks that every registered source satisfies the
Protocol. This file covers the two things that are specific to a source arriving
BESIDE an existing one: that adding it did not change which source an
unconfigured instance reads from, and that a source which cannot yet answer a
calendar question says so through Capabilities rather than by being left out.

No network, no database — every question here is answered off a Settings object.
"""
from __future__ import annotations

import unittest

from app import providers
from app.config import Settings
from app.endpoints import ENDPOINTS
from app.providers.base import Source


class RegistrationTests(unittest.TestCase):
    def setUp(self):
        self.registry = providers.registered()
        self.simkl = self.registry[Source.SIMKL]

    def test_simkl_is_registered(self):
        self.assertIn(Source.SIMKL, self.registry)
        self.assertEqual(self.simkl.label, "Simkl")

    def test_registering_it_did_not_displace_trakt_as_the_calendar_source(self):
        """A second source arriving is exactly the moment the calendar could
        start answering differently for an instance that changed nothing. Trakt
        still leads; Simkl joins it rather than replacing it, and it takes
        switching Simkl's public calendar off to be left with Trakt alone."""
        configured = Settings(trakt_client_id="id", trakt_access_token="token")
        self.assertEqual([p.source for p in providers.for_calendar_sources(configured)],
                         [Source.TRAKT, Source.SIMKL])
        self.assertEqual(
            [p.source for p in providers.for_calendar_sources(
                Settings(trakt_client_id="id", simkl_public_calendar_enabled=False))],
            [Source.TRAKT])

    def test_simkls_calendar_is_a_source_with_or_without_a_credential(self):
        """Its months are unauthenticated CDN files, so nothing about a
        credential decides whether they can be read — `calendar_port
        .calendar_configured` says so and `for_calendar_sources` asks it there,
        rather than asking `is_configured`, which answers for the AUTHENTICATED
        halves of this source and would withhold a public feed over a token the
        fetch never sends."""
        simkl_only = Settings(simkl_client_id="id", simkl_access_token="token")
        self.assertTrue(self.simkl.is_configured(simkl_only))
        self.assertEqual([p.source for p in providers.for_calendar_sources(simkl_only)],
                         [Source.SIMKL])
        self.assertTrue(simkl_only.calendar_source_configured)
        bare = Settings()
        self.assertFalse(self.simkl.is_configured(bare))
        self.assertEqual([p.source for p in providers.for_calendar_sources(bare)],
                         [Source.SIMKL])

    def test_it_is_configured_only_with_both_halves_of_the_credential(self):
        self.assertFalse(self.simkl.is_configured(Settings()))
        self.assertFalse(self.simkl.is_configured(Settings(simkl_client_id="cid")))
        self.assertFalse(self.simkl.is_configured(Settings(simkl_access_token="tok")))
        self.assertTrue(self.simkl.is_configured(
            Settings(simkl_client_id="cid", simkl_access_token="tok")))

    def test_it_answers_every_calendar_endpoint_except_finales(self):
        """Simkl's calendar CDN carries no concept of a finale (there is no
        endpoint for it), so that key is left out of Capabilities.endpoints —
        which is how the fill path skips asking for it without any route
        learning which source it is skipping."""
        for key in ENDPOINTS:
            with self.subTest(endpoint=key):
                self.assertEqual(self.simkl.capabilities.answers(key), key != "shows/finales")

    def test_the_port_and_the_private_data_flag_moved_together(self):
        """The same rule the conformance suite enforces across the registry,
        stated here as this source's own property: claiming the tracker can be
        backed by it while carrying nothing to call — or carrying a port while
        denying it has private data — is the one lie the registry cannot catch,
        which is why the two are asserted in one place."""
        self.assertIsNotNone(self.simkl.sync_port)
        self.assertTrue(self.simkl.capabilities.private_user_data)

    def test_the_declared_window_is_bounded_at_both_ends(self):
        """Simkl's calendar is a set of pre-baked files with a real beginning and
        end, unlike Trakt's endpoints which accept any date. A bound that was
        left as None would let a month be asked for that can only come back
        empty, and an empty month reads as "nothing airs then"."""
        from datetime import date, timedelta

        caps = self.simkl.capabilities
        today = date(2026, 8, 4)
        self.assertTrue(caps.covers(today, today=today))
        self.assertFalse(
            caps.covers(today - timedelta(days=caps.days_before + 1), today=today))
        self.assertFalse(
            caps.covers(today + timedelta(days=caps.days_after + 1), today=today))


class TrackerSelectionTests(unittest.TestCase):
    """The tracker asks the registry which sources can read private data."""

    def test_both_sources_can_now_back_the_tracker(self):
        self.assertEqual(providers.tracker_sources(),
                         {str(Source.TRAKT), str(Source.SIMKL)})

    def test_trakt_is_still_the_first_tracker(self):
        """The declared order decides which source is an account's PRIMARY — the
        one number a frozen month keeps — so a source appearing must not displace
        the one every existing instance already reads. TMDB is first in the whole
        order and is no tracker at all, so the first TRACKER is still Trakt."""
        trackers = [s for s in providers.registered()
                    if str(s) in providers.tracker_sources()]
        self.assertEqual(trackers[0], Source.TRAKT)

    def test_a_service_with_no_history_never_backs_the_tracker(self):
        """Even named as linked, TMDB is not read for anybody's viewing: it has
        no sync port, so no watched count can ever be filed under it and the
        primary count (counts.primary_count) can never be TMDB's."""
        from app.config import Settings
        from app.distrakt import counts, live
        linked = Settings(tmdb_api_key="eyJa.eyJb.c", trakt_client_id="id",
                          trakt_access_token="tok")
        ports = providers.for_tracker_ports(None, {"tmdb", "trakt"}, linked)
        self.assertEqual([str(s) for s, _ in ports], ["trakt"])
        self.assertNotIn("tmdb", providers.tracker_sources())
        self.assertEqual(counts.primary_count({"simkl": 4, "trakt": 6}, live.source_order()), 6)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
