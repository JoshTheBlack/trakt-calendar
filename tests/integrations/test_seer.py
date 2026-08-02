"""app/integrations/seer.py — the Overseerr / Jellyseerr client, which had no tests.

Same shape and the same framing as tests/integrations/test_arr.py: this is
characterisation of an outbound surface an administrator points at a host of
their choosing, not a vulnerability fix. What was missing was any statement of
what URL is built, where the API key travels, and what a partial read does.

Seerr's distinguishing feature is PAGINATION, and it is where the interesting
failure lives: a library read that gives up half way used to return what it had,
which downstream is indistinguishable from a complete but shorter library. The
cursor arithmetic is therefore tested in its own right.

Ordered cheapest first: the pure parse, then what goes on the wire, then failure.
"""
from __future__ import annotations

import json
import unittest

import httpx

from app.config import Settings
from app.integrations import seer

from ._fake_http import RecordingClient, pooled, response


def settings(**overrides) -> Settings:
    base = dict(seer_url="http://seerr.local:5055", seer_api_key="seer-key")
    base.update(overrides)
    return Settings(**base)


def seerr(client: RecordingClient):
    return pooled(seer, "POOL", client)


def page(ids: list[int], *, total: int, extra_records: int = 0) -> httpx.Response:
    """One /api/v1/media page. `extra_records` adds records carrying no tmdbId,
    which advance the cursor without contributing an id."""
    results = [{"tmdbId": i} for i in ids] + [{"status": 1} for _ in range(extra_records)]
    return response(200, content=json.dumps(
        {"results": results, "pageInfo": {"results": total}}).encode())


# --------------------------------------------------------------------------
# 1. The pure functions.
# --------------------------------------------------------------------------

class BaseTests(unittest.TestCase):
    """`_base` — the same `.strip().rstrip("/")` treatment arr.py gives a URL."""

    def test_it_returns_the_configured_pair(self):
        self.assertEqual(seer._base(settings()),
                         ("http://seerr.local:5055", "seer-key"))

    def test_a_trailing_slash_is_removed(self):
        url, _ = seer._base(settings(seer_url="http://seerr.local:5055/"))
        self.assertEqual(url, "http://seerr.local:5055")

    def test_whitespace_is_removed_from_both_halves(self):
        url, key = seer._base(settings(seer_url="  http://h:5055 ", seer_api_key=" k \n"))
        self.assertEqual((url, key), ("http://h:5055", "k"))

    def test_the_url_is_otherwise_taken_exactly_as_given(self):
        # Recorded as fact, as in test_arr.py: no scheme check, no host check.
        # Admin-configured, so a choice — but a choice somebody should have made.
        for given in ("https://requests.example.com", "http://10.0.0.9:5055"):
            with self.subTest(url=given):
                self.assertEqual(seer._base(settings(seer_url=given))[0], given)


class IsConfiguredTests(unittest.TestCase):

    def test_both_halves_are_required(self):
        self.assertTrue(seer.is_configured(settings()))
        self.assertFalse(seer.is_configured(settings(seer_api_key="")))
        self.assertFalse(seer.is_configured(settings(seer_url="")))

    def test_whitespace_only_is_not_configured(self):
        self.assertFalse(seer.is_configured(settings(seer_url="  ")))
        self.assertFalse(seer.is_configured(settings(seer_api_key="\t")))


class PageParseTests(unittest.TestCase):
    """`_page` — one page as (ids, record count, library total).

    THE RECORD COUNT IS THE POINT. It is returned rather than derived from the
    ids because a record carrying no tmdbId still advances the cursor; counting
    ids instead would leave `skip` short and re-request the same page forever.
    """

    def test_it_reads_the_ids_the_count_and_the_total(self):
        raw = json.dumps({"results": [{"tmdbId": 1}, {"tmdbId": 2}],
                          "pageInfo": {"results": 57}}).encode()
        self.assertEqual(seer._page(raw), ([1, 2], 2, 57))

    def test_a_record_with_no_tmdb_id_still_counts_toward_the_cursor(self):
        # The whole reason the count is separate from len(ids). If these two ever
        # collapse into one number, the read loops forever on a library that has
        # any un-matched record in it.
        raw = json.dumps({"results": [{"tmdbId": 1}, {"status": 3}, {"tmdbId": 2}],
                          "pageInfo": {"results": 3}}).encode()
        ids, count, total = seer._page(raw)
        self.assertEqual(ids, [1, 2])
        self.assertEqual(count, 3)
        self.assertEqual(total, 3)

    def test_a_zero_or_null_id_is_dropped_from_the_ids(self):
        raw = json.dumps({"results": [{"tmdbId": 0}, {"tmdbId": None}, {"tmdbId": 9}],
                          "pageInfo": {"results": 3}}).encode()
        ids, count, _ = seer._page(raw)
        self.assertEqual(ids, [9])
        self.assertEqual(count, 3)

    def test_an_empty_page_is_empty_rather_than_an_error(self):
        raw = json.dumps({"results": [], "pageInfo": {"results": 0}}).encode()
        self.assertEqual(seer._page(raw), ([], 0, 0))

    def test_a_body_with_no_results_key_reads_as_an_empty_page(self):
        self.assertEqual(seer._page(b"{}"), ([], 0, 0))

    def test_a_null_page_info_does_not_crash(self):
        # `(data.get("pageInfo") or {})` — a real server has sent null here.
        raw = json.dumps({"results": [{"tmdbId": 4}], "pageInfo": None}).encode()
        self.assertEqual(seer._page(raw), ([4], 1, 0))

    def test_malformed_json_raises_valueerror(self):
        # library_ids catches ValueError and turns it into LibraryUnavailable, so
        # the type is part of the contract between them.
        with self.assertRaises(ValueError):
            seer._page(b"<html>login</html>")


# --------------------------------------------------------------------------
# 2. URL and header construction.
# --------------------------------------------------------------------------

class HealthCheckRequestTests(unittest.IsolatedAsyncioTestCase):

    async def test_it_builds_the_status_url_from_the_configured_base(self):
        client = RecordingClient(response(200))
        with seerr(client):
            await seer.check_health(settings())
        self.assertEqual(client.only.url, "http://seerr.local:5055/api/v1/status")

    async def test_the_api_key_travels_in_a_header_and_not_the_query(self):
        client = RecordingClient(response(200))
        with seerr(client):
            await seer.check_health(settings())
        self.assertEqual(client.only.headers, {"X-Api-Key": "seer-key"})
        self.assertNotIn("seer-key", client.only.url)
        self.assertEqual(client.only.query, {})

    async def test_an_unconfigured_service_makes_no_call(self):
        client = RecordingClient(response(200))
        with seerr(client):
            out = await seer.check_health(settings(seer_url=""))
        self.assertEqual(out, {"configured": False, "reachable": False})
        self.assertEqual(client.calls, [])

    async def test_only_a_200_is_reachable(self):
        for status, reachable in ((200, True), (403, False), (500, False)):
            with self.subTest(status=status):
                client = RecordingClient(response(status))
                with seerr(client):
                    out = await seer.check_health(settings())
                self.assertEqual(out["reachable"], reachable)


class LibraryPaginationTests(unittest.IsolatedAsyncioTestCase):

    async def test_a_single_page_library_is_read_in_one_call(self):
        client = RecordingClient([page([1, 2, 3], total=3)])
        with seerr(client):
            ids = await seer.library_ids(settings())
        self.assertEqual(ids, [1, 2, 3])
        self.assertEqual(client.only.url, "http://seerr.local:5055/api/v1/media")
        self.assertEqual(client.only.params, {"take": seer.PAGE_SIZE, "skip": 0})

    async def test_the_key_travels_in_a_header_here_too(self):
        client = RecordingClient([page([1], total=1)])
        with seerr(client):
            await seer.library_ids(settings())
        self.assertEqual(client.only.headers, {"X-Api-Key": "seer-key"})
        self.assertNotIn("seer-key", client.only.url)

    async def test_the_cursor_advances_by_records_returned(self):
        client = RecordingClient([
            page([1, 2], total=4),
            page([3, 4], total=4),
        ])
        with seerr(client):
            ids = await seer.library_ids(settings())
        self.assertEqual(ids, [1, 2, 3, 4])
        self.assertEqual([c.params["skip"] for c in client.calls], [0, 2])

    async def test_records_with_no_id_still_move_the_cursor_forward(self):
        """The failure this guards is an INFINITE LOOP, not a wrong answer: with
        the cursor advanced by ids rather than records, a page whose records are
        partly un-matched re-requests itself for as long as the safety cap allows.
        """
        client = RecordingClient([
            page([1], total=4, extra_records=1),   # 2 records, 1 id
            page([2], total=4, extra_records=1),   # 2 records, 1 id
        ])
        with seerr(client):
            ids = await seer.library_ids(settings())
        self.assertEqual(ids, [1, 2])
        self.assertEqual([c.params["skip"] for c in client.calls], [0, 2])

    async def test_it_stops_when_a_page_comes_back_empty(self):
        # A server whose reported total exceeds what it will actually hand over
        # must still terminate the loop.
        client = RecordingClient([
            page([1, 2], total=99),
            page([], total=99),
        ])
        with seerr(client):
            ids = await seer.library_ids(settings())
        self.assertEqual(ids, [1, 2])
        self.assertEqual(len(client.calls), 2)

    async def test_an_unconfigured_service_is_a_knowable_empty(self):
        client = RecordingClient(response(200))
        with seerr(client):
            self.assertEqual(await seer.library_ids(settings(seer_api_key="")), [])
        self.assertEqual(client.calls, [])


class AddMediaRequestTests(unittest.IsolatedAsyncioTestCase):

    async def test_a_show_requests_every_season(self):
        client = RecordingClient(response(201, json={}))
        with seerr(client):
            out = await seer.add_media(settings(), "show", 42, "A Show")
        self.assertEqual(client.only.url, "http://seerr.local:5055/api/v1/request")
        self.assertEqual(client.only.json, {"mediaType": "tv", "mediaId": 42, "seasons": "all"})
        self.assertTrue(out["ok"])

    async def test_a_movie_carries_no_seasons(self):
        client = RecordingClient(response(201, json={}))
        with seerr(client):
            await seer.add_media(settings(), "movie", 42, "A Film")
        self.assertEqual(client.only.json, {"mediaType": "movie", "mediaId": 42})

    async def test_the_tmdb_id_is_coerced_to_an_int(self):
        # It arrives from a JSON body, where it may be a string.
        client = RecordingClient(response(201, json={}))
        with seerr(client):
            await seer.add_media(settings(), "movie", "42", "A Film")
        self.assertEqual(client.only.json["mediaId"], 42)

    async def test_the_key_is_in_a_header_and_not_the_url(self):
        client = RecordingClient(response(201, json={}))
        with seerr(client):
            await seer.add_media(settings(), "movie", 42, "A Film")
        self.assertEqual(client.only.headers["X-Api-Key"], "seer-key")
        self.assertNotIn("seer-key", client.only.url)

    async def test_a_title_with_no_tmdb_id_refuses_before_calling(self):
        client = RecordingClient(response(201))
        with seerr(client):
            out = await seer.add_media(settings(), "movie", None, "A Film")
        self.assertFalse(out["ok"])
        self.assertIn("movie", out["error"])
        self.assertEqual(client.calls, [])

    async def test_an_already_requested_title_is_reported_as_success(self):
        # 409 means it is already there, which from the user's point of view is
        # the thing they asked for being true.
        client = RecordingClient(response(409, json={}))
        with seerr(client):
            out = await seer.add_media(settings(), "movie", 42, "A Film")
        self.assertTrue(out["ok"])
        self.assertIn("already on Seerr", out["message"])


# --------------------------------------------------------------------------
# 3. Failure modes — LibraryUnavailable is caught by name in
#    app/integrations/routes.py, so the type is part of the contract.
# --------------------------------------------------------------------------

class LibraryFailureTests(unittest.IsolatedAsyncioTestCase):

    async def test_a_non_200_on_the_first_page_raises(self):
        client = RecordingClient([response(401, json={})])
        with seerr(client):
            with self.assertRaises(seer.LibraryUnavailable):
                await seer.library_ids(settings())

    async def test_a_non_200_on_a_LATER_page_also_raises(self):
        """PARTIAL IS ALSO UNKNOWN. This used to return the ids gathered so far,
        which reads downstream as a complete, shorter library — the caller caches
        it and quietly stops marking the missing titles as already-requested."""
        client = RecordingClient([
            page([1, 2], total=10),
            response(500, json={}),
        ])
        with seerr(client):
            with self.assertRaises(seer.LibraryUnavailable):
                await seer.library_ids(settings())

    async def test_the_refusal_names_the_status(self):
        client = RecordingClient([response(503, json={})])
        with seerr(client):
            with self.assertRaises(seer.LibraryUnavailable) as caught:
                await seer.library_ids(settings())
        self.assertIn("503", str(caught.exception))

    async def test_a_timeout_raises(self):
        client = RecordingClient(response(200), raises=httpx.ReadTimeout("slow"))
        with seerr(client):
            with self.assertRaises(seer.LibraryUnavailable):
                await seer.library_ids(settings())

    async def test_an_unreadable_body_raises_rather_than_reading_as_empty(self):
        client = RecordingClient([response(200, content=b"<html>login</html>")])
        with seerr(client):
            with self.assertRaises(seer.LibraryUnavailable):
                await seer.library_ids(settings())

    async def test_a_genuinely_empty_library_is_still_empty(self):
        client = RecordingClient([page([], total=0)])
        with seerr(client):
            self.assertEqual(await seer.library_ids(settings()), [])


class AddMediaFailureTests(unittest.IsolatedAsyncioTestCase):

    async def test_an_unreachable_service_is_reported_not_raised(self):
        client = RecordingClient(response(201), raises=httpx.ConnectError("refused"))
        with seerr(client):
            out = await seer.add_media(settings(), "movie", 42, "A Film")
        self.assertFalse(out["ok"])
        self.assertIn("Could not reach Seerr", out["error"])

    async def test_a_refusal_reports_the_services_own_message(self):
        client = RecordingClient(response(400, json={"message": "No permission"}))
        with seerr(client):
            out = await seer.add_media(settings(), "movie", 42, "A Film")
        self.assertEqual(out, {"ok": False, "error": "No permission"})

    async def test_an_unreadable_refusal_falls_back_to_the_status_code(self):
        client = RecordingClient(response(502, text="<html>nginx</html>"))
        with seerr(client):
            out = await seer.add_media(settings(), "movie", 42, "A Film")
        self.assertEqual(out["error"], "HTTP 502")
        self.assertNotIn("nginx", out["error"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
