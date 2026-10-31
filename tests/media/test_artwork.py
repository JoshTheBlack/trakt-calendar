"""The poster registry's one rule for a sighting, written as one statement.

A row nobody holds is added; the address already held is seen again and keeps its
failure record; a new address replaces the old one with a fresh one — upstream
re-pointing artwork is normal. A fill files a picture per listing inside a write
transaction, so the rule is one statement however many sightings arrive.
"""
from __future__ import annotations

from app import db
from app.media import artwork
from tests.support import DatabaseTestCase, statements_per_transaction


async def _row(tmdb: int = 1, source: str = "trakt"):
    return await db.fetch_one(
        "SELECT url, first_seen_at, last_seen_at, fail_count, last_failed_at FROM show_posters "
        "WHERE media = 'show' AND tmdb = ? AND source = ?", (tmdb, source))


class ASightingTests(DatabaseTestCase):

    async def test_the_address_already_held_is_seen_again_and_keeps_its_failures(self):
        await artwork.record_poster_urls([("show", 1, "trakt", "https://a/p.jpg")])
        await artwork.record_failure("show", 1, "trakt")
        await db.execute("UPDATE show_posters SET first_seen_at = 5, last_seen_at = 5")
        await artwork.record_poster_urls([("show", 1, "trakt", "https://a/p.jpg")])
        row = await _row()
        self.assertEqual((row["first_seen_at"], row["fail_count"]), (5, 1))
        self.assertIsNotNone(row["last_failed_at"])
        self.assertGreater(row["last_seen_at"], 5)

    async def test_a_new_address_replaces_the_old_with_a_fresh_failure_record(self):
        await artwork.record_poster_urls([("show", 1, "trakt", "https://a/p.jpg")])
        await artwork.record_failure("show", 1, "trakt")
        await artwork.record_poster_urls([("show", 1, "trakt", "https://a/new.jpg")])
        row = await _row()
        self.assertEqual((row["url"], row["fail_count"], row["last_failed_at"]),
                         ("https://a/new.jpg", 0, None))

    async def test_sightings_of_one_title_in_one_batch_land_in_order(self):
        await artwork.record_poster_urls([("show", 1, "trakt", "https://a/one.jpg"),
                                          ("show", 1, "trakt", "https://a/two.jpg")])
        self.assertEqual((await _row())["url"], "https://a/two.jpg")

    async def test_a_recorded_miss_is_capped_and_addresses_nothing(self):
        await artwork.record_miss("show", 1)
        row = await _row(source=artwork.MISSING)
        self.assertEqual((row["url"], row["fail_count"]), ("", artwork.MAX_FAIL_COUNT))

    async def test_thirty_sightings_cost_what_three_do(self):
        def sightings(ids):
            return [("show", n, "trakt", f"https://a/{n}.jpg") for n in ids]

        with statements_per_transaction() as few:
            await artwork.record_poster_urls(sightings(range(1, 4)))
        with statements_per_transaction() as many:
            await artwork.record_poster_urls(sightings(range(4, 34)))
        self.assertEqual(few, many)
        self.assertEqual(await db.fetch_value("SELECT COUNT(*) FROM show_posters"), 33)
