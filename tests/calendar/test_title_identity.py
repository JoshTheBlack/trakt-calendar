"""The calendar's listings filed and read by the title store's identity.

WHAT CHANGED, AND WHAT EACH TEST HOLDS: a service that knows a show only by its
tvdb id used to file its listing under tvdb while another service filed the same
show's listing under tmdb — two keys, two cards for one episode, measured 33 times
on the development copy. A listing is now filed under the key the title store
answers, the read completes each listing's ids from the store, and a title that
learns a higher id moves every listing it holds in the same transaction — so the
two services' listings are one card whichever arrived first.
"""
from __future__ import annotations

from datetime import date

from app import db
from app.calendar import cache as calendar_cache, entries
from app.metadata import rows as title_rows
from app.providers.base import Media, Record, Source
from tests import support
from tests.support import DatabaseTestCase, listings, title_groups

DAY = 1783540800.0  # 2026-07-08 20:00Z


async def _stored_span(endpoint_key, first, after):
    """Every listing of each episode row with one in [first, after), whole —
    the rows a span's reader works from."""
    from app import db
    from app.calendar import query as _query

    return await db.run(lambda conn: support._stored(
        conn, endpoint_key, _query.read_rows(conn, endpoint_key, first=first, after=after)))

WEEK = (date(2026, 7, 6), date(2026, 7, 13))


def listing(source: Source, ids: dict, *, air_ts: float = DAY, source_id: str = "",
            episode: int = 1) -> Record:
    name = str(source)
    return Record(source=source, media=Media.SHOW, id=source_id or str(ids.get(name)),
                  ids={**ids}, detail_url="", title="A Show", air_ts=air_ts,
                  season=1, episode_number=episode, enriched=True)


async def store(*records, span=WEEK):
    now = db.now()
    await entries.store_span("shows", *span, list(records),
                             sources=sorted({str(r.source) for r in records}),
                             asked=sorted({str(r.source) for r in records}),
                             now=now, stale_after=now + 86400)


class TwoServicesOneEpisodeTests(DatabaseTestCase):
    async def test_a_tvdb_only_listing_and_a_tmdb_listing_of_one_episode_are_one_card(self):
        await store(listing(Source.TRAKT, {"trakt": 7, "tmdb": 900, "tvdb": 4321}),
                    listing(Source.SIMKL, {"simkl": 55, "tvdb": 4321}))
        records, *_ = await support.read_span("shows", *WEEK)
        groups = support.grouped(records)
        self.assertEqual(len(groups), 1)
        self.assertEqual(set(groups[0]["by_source"]), {"trakt", "simkl"})
        self.assertTrue(groups[0]["key"].startswith("show:tmdb:900"))

    async def test_the_same_whichever_service_arrived_first(self):
        await store(listing(Source.SIMKL, {"simkl": 55, "tvdb": 4321}))
        await store(listing(Source.TRAKT, {"trakt": 7, "tmdb": 900, "tvdb": 4321}))
        keys = {listed["title_key"] for listed in await listings()}
        self.assertEqual(keys, {"show:tmdb:900"})
        records, *_ = await support.read_span("shows", *WEEK)
        self.assertEqual(len(support.grouped(records)), 1)

    async def test_the_service_row_keeps_what_the_service_said(self):
        """The store knows the tmdb id; Simkl did not state it, and its row says
        what Simkl said."""
        await store(listing(Source.TRAKT, {"trakt": 7, "tmdb": 900, "tvdb": 4321}),
                    listing(Source.SIMKL, {"simkl": 55, "tvdb": 4321}))
        stored = (await title_groups("simkl"))[0]["ids_json"]
        self.assertNotIn("tmdb", stored)

    async def test_a_title_re_keyed_by_a_later_listing_of_the_same_fill_moves_every_listing(self):
        """Simkl's tvdb-only listing registers the title under its tvdb id; Trakt's,
        later in the SAME fill, states the tmdb id and re-keys it. Every listing
        of the title — those before the re-key and those after — is filed under
        the key the title answers to once the fill is done, never the one it
        retired: an airing under a retired key has no identity to join and draws
        as a card of its own."""
        await store(listing(Source.SIMKL, {"simkl": 55, "tvdb": 4321}),
                    listing(Source.TRAKT, {"trakt": 7, "tmdb": 900, "tvdb": 4321}),
                    listing(Source.SIMKL, {"simkl": 55, "tvdb": 4321}, episode=2,
                            air_ts=DAY + 86400))
        airings = {listed["title_key"] for listed in await listings()}
        described = {group["title_key"] for group in await title_groups()}
        self.assertEqual((airings, described), ({"show:tmdb:900"}, {"show:tmdb:900"}))
        records, *_ = await support.read_span("shows", *WEEK)
        self.assertEqual({group["key"].split("|")[0][:13]
                          for group in support.grouped(records)},
                         {"show:tmdb:900"})

    async def test_two_tmdb_titles_sharing_a_tvdb_id_stay_two_cards(self):
        await store(listing(Source.TRAKT, {"trakt": 7, "tmdb": 900, "tvdb": 4321}),
                    listing(Source.SIMKL, {"simkl": 55, "tmdb": 901, "tvdb": 4321}))
        records, *_ = await support.read_span("shows", *WEEK)
        self.assertEqual(len(support.grouped(records)), 2)


class AFillThatReKeysTests(DatabaseTestCase):
    """A fill that re-keys a title files that title's rows INSIDE the write lock,
    once the merge is written, and every other title's before it
    (entries.store_span). Both halves must come out as one fill would have:
    each title's listings the answer's, and what each service stopped listing
    gone."""

    async def test_the_merged_title_and_every_other_title_are_both_filed(self):
        # Before: Trakt lists a title under its tvdb key at one instant, and
        # another title it will stop listing.
        await store(listing(Source.TRAKT, {"trakt": 7, "tvdb": 4321}),
                    listing(Source.TRAKT, {"trakt": 8, "tmdb": 800}, episode=5))
        self.assertEqual({listed["title_key"] for listed in await listings()},
                         {"show:tvdb:4321", "show:tmdb:800"})
        # The refill: the first title now states its tmdb id (a re-key) at
        # another instant, the second is gone, and a third arrives.
        await store(listing(Source.TRAKT, {"trakt": 7, "tmdb": 900, "tvdb": 4321},
                            air_ts=DAY + 3600),
                    listing(Source.TRAKT, {"trakt": 9, "tmdb": 901}, episode=2))
        found = sorted((listed["title_key"], listed["unit"], listed["air_ts"])
                       for listed in await listings())
        self.assertEqual(found, [("show:tmdb:900", "S1E1", DAY + 3600),
                                 ("show:tmdb:901", "S1E2", DAY)])

    async def test_the_merged_title_keeps_what_another_service_lists(self):
        await store(listing(Source.SIMKL, {"simkl": 55, "tvdb": 4321}))
        await store(listing(Source.TRAKT, {"trakt": 7, "tvdb": 4321}))
        # Trakt's refill teaches the tmdb id: Simkl's listing, which this fill
        # did not answer for, moves with the title and stays one card with it.
        await store(listing(Source.TRAKT, {"trakt": 7, "tmdb": 900, "tvdb": 4321}))
        found = sorted((listed["source"], listed["title_key"]) for listed in await listings())
        self.assertEqual(found, [("simkl", "show:tmdb:900"), ("trakt", "show:tmdb:900")])
        records, *_ = await support.read_span("shows", *WEEK)
        self.assertEqual(len(support.grouped(records)), 1)


class AReadHoldsTheWholeEpisodeTests(DatabaseTestCase):
    async def test_another_services_listing_outside_the_range_is_read_with_it(self):
        """The read groups by the ids a listing carries but fetched the episode's
        other listings by the key it was FILED under; a listing whose ids had
        outgrown its key could miss its partner just outside the range and draw
        as a different card on the neighbouring page."""
        await store(listing(Source.SIMKL, {"simkl": 55, "tvdb": 4321}))
        later = (date(2026, 7, 13), date(2026, 7, 20))
        await store(listing(Source.TRAKT, {"trakt": 7, "tmdb": 900, "tvdb": 4321},
                            air_ts=DAY + 6 * 86400), span=later)
        records = await _stored_span("shows", *WEEK)
        self.assertEqual({str(r.source) for r in records}, {"simkl", "trakt"})
        self.assertEqual(len(support.grouped(records)), 1)


class EnrichmentThatTeachesAHigherIdTests(DatabaseTestCase):
    async def test_the_title_and_its_listings_are_re_keyed_when_the_answer_lands(self):
        await store(listing(Source.SIMKL, {"simkl": 55}, source_id="55"))
        self.assertEqual([listed["title_key"] for listed in await listings()], ["simkl:55"])
        await title_rows.apply_enrichment("simkl", 55, "show",
                                          {"ids": {"tvdb": 4321}, "network": "HBO"}, db.now())
        self.assertEqual([listed["title_key"] for listed in await listings()],
                         ["show:tvdb:4321"])
        self.assertEqual([group["title_key"] for group in await title_groups()],
                         ["show:tvdb:4321"])
