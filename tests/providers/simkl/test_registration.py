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
        start answering differently for an instance that changed nothing."""
        configured = Settings(trakt_client_id="id", trakt_access_token="token")
        self.assertEqual([p.source for p in providers.for_calendar_sources(configured)],
                         [Source.TRAKT])

    def test_simkl_credentials_alone_are_not_a_calendar_source(self):
        """It has no calendar module yet, so it answers no endpoint — and a
        source that cannot answer any must not make the instance report that it
        has a calendar source. That would render an empty month, which reads as
        "nothing airs then" instead of "nobody was asked"."""
        simkl_only = Settings(simkl_client_id="id", simkl_access_token="token")
        self.assertTrue(self.simkl.is_configured(simkl_only))
        self.assertEqual(providers.for_calendar_sources(simkl_only), [])
        self.assertFalse(simkl_only.calendar_source_configured)

    def test_it_is_configured_only_with_both_halves_of_the_credential(self):
        self.assertFalse(self.simkl.is_configured(Settings()))
        self.assertFalse(self.simkl.is_configured(Settings(simkl_client_id="cid")))
        self.assertFalse(self.simkl.is_configured(Settings(simkl_access_token="tok")))
        self.assertTrue(self.simkl.is_configured(
            Settings(simkl_client_id="cid", simkl_access_token="tok")))

    def test_it_answers_no_calendar_endpoint_yet(self):
        """An empty endpoint set is how the fill path skips a source without any
        route learning which source it is skipping."""
        for key in ENDPOINTS:
            with self.subTest(endpoint=key):
                self.assertFalse(self.simkl.capabilities.answers(key))

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

    def test_trakt_is_still_first(self):
        """The declared order decides which source is an account's PRIMARY — the
        one number a frozen month keeps — so a second source appearing must not
        displace the one every existing instance already reads."""
        self.assertEqual(list(providers.registered())[0], Source.TRAKT)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
