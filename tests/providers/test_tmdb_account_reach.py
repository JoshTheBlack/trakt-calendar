"""What a viewer linking their TMDB account changes in the registry — and what it
must not.

TMDB's `is_configured` (the private question) is true once a viewer's Settings
carries their TMDB grant (auth.viewer.settings_for_user). Every registry
function asking that question was audited; this file holds each answer:

  for_watchlist         CHANGES: a linked viewer's tracker can write their TMDB
                        watchlist. The point of the link.
  for_tracker_ports,    UNCHANGED: TMDB keeps no watch history — no sync port,
  tracker_sources       `private_user_data` False — so the tracker's sources and
                        its access gate never include it.
  for_search            UNCHANGED: TMDB is searched by default with the
                        instance's token, and the private question is asked only
                        of a service that is not — no "Also search TMDB", no
                        second TMDB catalogue.
  calendar_sources,     UNCHANGED: a calendar is read with the instance's
  for_calendar_sources  credentials; neither asks the private question.
  for_shared_id_lookup  UNCHANGED: asks `catalogue_is_configured` only.
"""
from __future__ import annotations

import dataclasses
import unittest

from app import providers
from app.config import Settings
from app.providers.base import Source
from app.sources import prefs

INSTANCE = Settings(tmdb_api_key="eyJa.eyJb.c", trakt_client_id="c", trakt_access_token="t",
                    simkl_client_id="c", simkl_access_token="t")
LINKED = dataclasses.replace(INSTANCE, tmdb_user_token="the-viewers-token",
                             tmdb_account_id="6a5edaa7df66e2fc7c922663")
EVERY_NAME = frozenset(str(source) for source in Source)


class ATmdbLinkTests(unittest.TestCase):

    def test_it_offers_the_tmdb_watchlist(self):
        self.assertNotIn(Source.TMDB, dict(providers.for_watchlist(INSTANCE)))
        self.assertIn(Source.TMDB, dict(providers.for_watchlist(LINKED)))

    def test_it_does_not_make_tmdb_a_tracker_source(self):
        account = prefs.SourcePrefs(user_id=1)
        for settings in (INSTANCE, LINKED):
            with self.subTest(linked=settings is LINKED):
                ports = providers.for_tracker_ports(account, EVERY_NAME, settings)
                self.assertNotIn(Source.TMDB, [source for source, _port in ports])
        self.assertEqual(
            [source for source, _ in providers.for_tracker_ports(account, EVERY_NAME, LINKED)],
            [source for source, _ in providers.for_tracker_ports(account, EVERY_NAME, INSTANCE)])
        self.assertNotIn("tmdb", providers.tracker_sources())

    def test_it_does_not_change_who_a_search_asks(self):
        before, after = providers.for_search(INSTANCE), providers.for_search(LINKED)
        self.assertEqual([(a.source, a.catalogue.key) for a in after.asked],
                         [(a.source, a.catalogue.key) for a in before.asked])
        self.assertEqual(after.offered, before.offered)
        self.assertEqual(after.order, before.order)
        self.assertEqual([a.catalogue.key for a in after.asked].count("tmdb"), 1)
        self.assertNotIn("tmdb", {c.key for c in after.offered})
        with self.assertRaises(providers.UnknownSearchService):
            providers.for_search(LINKED, also=["tmdb"])

    def test_it_does_not_change_the_calendar_sources(self):
        account = prefs.SourcePrefs(user_id=1)
        self.assertEqual(
            [p.source for p in providers.calendar_sources(prefs=account, settings=LINKED)],
            [p.source for p in providers.calendar_sources(prefs=account, settings=INSTANCE)])
        self.assertEqual(
            [p.source for p in providers.for_calendar_sources(LINKED, prefs=account)],
            [p.source for p in providers.for_calendar_sources(INSTANCE, prefs=account)])

    def test_it_does_not_change_the_lookup_by_shared_id(self):
        self.assertEqual([s for s, _ in providers.for_shared_id_lookup(LINKED)],
                         [s for s, _ in providers.for_shared_id_lookup(INSTANCE)])
