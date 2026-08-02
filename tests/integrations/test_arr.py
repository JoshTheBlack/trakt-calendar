"""app/integrations/arr.py — the Sonarr / Radarr client, which had no tests.

WHAT THIS FILE IS. Characterisation of an OUTBOUND SURFACE. The module takes a
base URL and an API key an administrator typed into Settings and issues
authenticated requests to whatever they name, then parses whatever comes back.
Nothing stated what URLs it builds, nothing stated that the key travels in a
header rather than a query string, and nothing stated what it does with a body it
cannot read. Those are properties worth pinning down, and pinning them down is
all this is.

BE CLEAR ABOUT THE THREAT MODEL, BECAUSE IT IS EASY TO OVERSTATE. `credentials()`
does `.strip().rstrip("/")` on the URL and nothing else — no scheme check, no host
check — and every call string-concatenates onto the result. That is not privilege
escalation: the value is admin-configured, and an administrator of this instance
can already do worse than point it at a URL. What was missing is not a control,
it is an assertion. These tests are the assertion.

Ordered cheapest first: the pure parsers (which need no HTTP at all and carry
most of the risk), then what each call actually sends, then how it fails.
"""
from __future__ import annotations

import json
import unittest

import httpx

from app.config import Settings
from app.integrations import arr

from ._fake_http import RecordingClient, pooled, response


def settings(**overrides) -> Settings:
    """Settings with both services configured, unless a test says otherwise."""
    base = dict(
        sonarr_url="http://sonarr.local:8989", sonarr_api_key="sonarr-key",
        sonarr_quality_profile_id=3, sonarr_root_folder="/tv",
        radarr_url="http://radarr.local:7878", radarr_api_key="radarr-key",
        radarr_quality_profile_id=5, radarr_root_folder="/movies",
    )
    base.update(overrides)
    return Settings(**base)


def sonarr(client: RecordingClient):
    return pooled(arr, "_POOLS", client, key="sonarr")


def radarr(client: RecordingClient):
    return pooled(arr, "_POOLS", client, key="radarr")


# --------------------------------------------------------------------------
# 1. The pure functions. No HTTP, and most of the risk.
# --------------------------------------------------------------------------

class CredentialsTests(unittest.TestCase):
    """What `credentials` does to an operator-supplied URL, stated exactly."""

    def test_it_reads_the_pair_for_the_service_asked_for(self):
        url, key = arr.credentials("sonarr", settings())
        self.assertEqual((url, key), ("http://sonarr.local:8989", "sonarr-key"))

    def test_the_two_services_do_not_share_credentials(self):
        # One dict maps kind -> (url attr, key attr); a transposition there would
        # send Sonarr's key to Radarr, which is the kind of mistake that still
        # "works" against two boxes on one LAN until it does not.
        self.assertEqual(arr.credentials("radarr", settings())[1], "radarr-key")

    def test_a_trailing_slash_is_removed_so_the_join_does_not_double_it(self):
        # Every call site concatenates "/api/v3/..." onto this, so a kept slash
        # would produce "//api/v3" — which some servers route and some 404.
        url, _ = arr.credentials("sonarr", settings(sonarr_url="http://host:8989/"))
        self.assertEqual(url, "http://host:8989")

    def test_several_trailing_slashes_all_go(self):
        url, _ = arr.credentials("sonarr", settings(sonarr_url="http://host:8989///"))
        self.assertEqual(url, "http://host:8989")

    def test_surrounding_whitespace_is_removed_from_both(self):
        # A pasted value routinely carries a trailing space or newline, and an API
        # key with one on the end fails authentication in a way that looks like a
        # wrong key rather than a stray character.
        url, key = arr.credentials(
            "sonarr", settings(sonarr_url="  http://host:8989 \n", sonarr_api_key=" k \t"))
        self.assertEqual((url, key), ("http://host:8989", "k"))

    def test_the_url_is_otherwise_taken_exactly_as_given(self):
        """NO scheme check and NO host check — recorded here as the fact it is.

        This is the accurate statement of what the module does: an administrator
        can point it anywhere and the app will issue an authenticated request
        there. It is admin-configured so that is a choice rather than a hole, but
        it should be a choice somebody made on purpose, and a test that changes
        when the behaviour changes is how it stays one.
        """
        for given in ("https://arr.example.com", "http://10.0.0.4:8989",
                      "http://localhost:8989/sonarr"):
            with self.subTest(url=given):
                self.assertEqual(arr.credentials("sonarr", settings(sonarr_url=given))[0], given)

    def test_an_unknown_service_is_a_key_error_rather_than_a_guess(self):
        with self.assertRaises(KeyError):
            arr.credentials("plexarr", settings())


class IsConfiguredTests(unittest.TestCase):
    """Both halves are required — a URL with no key is not a usable service."""

    def test_both_present_is_configured(self):
        self.assertTrue(arr.is_configured("sonarr", settings()))

    def test_a_missing_key_is_not_configured(self):
        self.assertFalse(arr.is_configured("sonarr", settings(sonarr_api_key="")))

    def test_a_missing_url_is_not_configured(self):
        self.assertFalse(arr.is_configured("sonarr", settings(sonarr_url="")))

    def test_whitespace_only_values_are_not_configured(self):
        # They survive `bool()` before stripping, so this is the case that would
        # send an authenticated request to "" with an empty key.
        self.assertFalse(arr.is_configured("sonarr", settings(sonarr_url="   ")))
        self.assertFalse(arr.is_configured("sonarr", settings(sonarr_api_key="  \n")))

    def test_one_service_being_configured_says_nothing_about_the_other(self):
        only_sonarr = settings(radarr_url="", radarr_api_key="")
        self.assertTrue(arr.is_configured("sonarr", only_sonarr))
        self.assertFalse(arr.is_configured("radarr", only_sonarr))


class IdsFromTests(unittest.TestCase):
    """The library parse — somebody else's JSON, on a worker thread."""

    def test_it_takes_the_named_field_from_every_record(self):
        raw = json.dumps([{"tvdbId": 1}, {"tvdbId": 2}, {"tvdbId": 3}]).encode()
        self.assertEqual(arr._ids_from(raw, "tvdbId"), [1, 2, 3])

    def test_the_field_name_is_what_selects_between_shows_and_films(self):
        # Sonarr answers with tvdbId and Radarr with tmdbId out of the same
        # function, so reading the wrong field would return an empty library
        # rather than an error — which is exactly the failure LibraryUnavailable
        # exists to prevent elsewhere.
        raw = json.dumps([{"tvdbId": 1, "tmdbId": 99}]).encode()
        self.assertEqual(arr._ids_from(raw, "tmdbId"), [99])

    def test_a_record_missing_the_field_is_skipped_not_crashed(self):
        raw = json.dumps([{"tvdbId": 1}, {"title": "no id here"}, {"tvdbId": 3}]).encode()
        self.assertEqual(arr._ids_from(raw, "tvdbId"), [1, 3])

    def test_a_record_whose_id_is_zero_or_null_is_skipped(self):
        # `if item.get(field)` is a truthiness test, so 0 and None both drop out.
        # Recorded because 0 is a plausible "unknown id" from a real server and
        # marking a title as already-added on id 0 would mark everything.
        raw = json.dumps([{"tvdbId": 0}, {"tvdbId": None}, {"tvdbId": 7}]).encode()
        self.assertEqual(arr._ids_from(raw, "tvdbId"), [7])

    def test_an_empty_library_is_an_empty_list(self):
        self.assertEqual(arr._ids_from(b"[]", "tvdbId"), [])

    def test_malformed_json_raises_valueerror(self):
        # The caller catches ValueError and turns it into LibraryUnavailable; that
        # only works if this raises it, so the type is part of the contract.
        with self.assertRaises(ValueError):
            arr._ids_from(b"<html>not json</html>", "tvdbId")

    def test_a_body_that_is_not_a_list_raises_rather_than_returning_nothing(self):
        # A JSON object iterates as its keys, so a str.get would fail — the point
        # is that it does NOT quietly answer "the library is empty".
        with self.assertRaises((TypeError, AttributeError)):
            arr._ids_from(b'{"error": "unauthorized"}', "tvdbId")


class ErrorTextTests(unittest.TestCase):
    """What a failed call tells the user, out of a body the remote controls."""

    def test_a_list_body_uses_the_first_error_message(self):
        resp = response(400, json=[{"errorMessage": "Folder is not writable"}])
        self.assertEqual(arr._error_text(resp), "Folder is not writable")

    def test_a_list_body_falls_back_to_message(self):
        resp = response(400, json=[{"message": "Series already exists"}])
        self.assertEqual(arr._error_text(resp), "Series already exists")

    def test_a_dict_body_uses_its_message(self):
        resp = response(401, json={"message": "Unauthorized"})
        self.assertEqual(arr._error_text(resp), "Unauthorized")

    def test_an_empty_list_falls_back_to_the_status_code(self):
        self.assertEqual(arr._error_text(response(500, json=[])), "HTTP 500")

    def test_a_body_with_neither_field_falls_back_to_the_status_code(self):
        self.assertEqual(arr._error_text(response(500, json=[{"other": 1}])), "HTTP 500")

    def test_an_unreadable_body_falls_back_to_the_status_code(self):
        resp = response(502, text="<html><body>Bad Gateway</body></html>")
        self.assertEqual(arr._error_text(resp), "HTTP 502")

    def test_the_html_body_itself_is_not_surfaced(self):
        # Whatever answered may not be Sonarr at all — a proxy, a login page, an
        # unrelated service on that port. Its body reaches an admin-facing UI, so
        # what matters is that the raw text is not what gets shown.
        resp = response(502, text="<html>nginx</html>")
        self.assertNotIn("nginx", arr._error_text(resp))

    def test_it_never_echoes_the_api_key_back(self):
        """A remote-controlled string reaching the UI must not be a way to get a
        credential rendered — and the only defence here is that this function
        never looks at the credentials at all. Asserted rather than reasoned:
        _error_text takes a response and nothing else, and this is what says so.
        """
        resp = response(401, json={"message": "key sonarr-key rejected"})
        # The message IS passed through — that is the documented behaviour, and a
        # server that echoes a key back has already leaked it to itself. What is
        # pinned is that nothing here READS the configured key to build the text.
        self.assertEqual(arr._error_text(resp), "key sonarr-key rejected")
        self.assertNotIn("settings", arr._error_text.__code__.co_names)


# --------------------------------------------------------------------------
# 2. URL and header construction — what actually goes on the wire.
# --------------------------------------------------------------------------

class HealthCheckRequestTests(unittest.IsolatedAsyncioTestCase):

    async def test_it_builds_the_status_url_from_the_configured_base(self):
        client = RecordingClient(response(200))
        with sonarr(client):
            await arr.check_health("sonarr", settings())
        self.assertEqual(client.only.url, "http://sonarr.local:8989/api/v3/system/status")

    async def test_the_api_key_travels_in_a_header(self):
        client = RecordingClient(response(200))
        with sonarr(client):
            await arr.check_health("sonarr", settings())
        self.assertEqual(client.only.headers, {"X-Api-Key": "sonarr-key"})

    async def test_the_api_key_is_not_in_the_query_string(self):
        """THE POINT OF THE PREVIOUS TEST, STATED AS THE PROPERTY IT PROTECTS. A
        key in a query string lands in the target's access log, in any proxy's
        log between here and there, and in a Referer if the URL is ever followed.
        A header does not. Nothing enforced this; now something does."""
        client = RecordingClient(response(200))
        with sonarr(client):
            await arr.check_health("sonarr", settings())
        self.assertNotIn("sonarr-key", client.only.url)
        self.assertEqual(client.only.query, {})

    async def test_an_unconfigured_service_makes_no_call_at_all(self):
        client = RecordingClient(response(200))
        with sonarr(client):
            out = await arr.check_health("sonarr", settings(sonarr_api_key=""))
        self.assertEqual(out, {"configured": False, "reachable": False})
        self.assertEqual(client.calls, [])

    async def test_a_200_is_reachable_and_anything_else_is_not(self):
        for status, reachable in ((200, True), (401, False), (404, False), (500, False)):
            with self.subTest(status=status):
                client = RecordingClient(response(status))
                with sonarr(client):
                    out = await arr.check_health("sonarr", settings())
                self.assertEqual(out, {"configured": True, "reachable": reachable})


class LibraryRequestTests(unittest.IsolatedAsyncioTestCase):

    async def test_sonarr_reads_series_by_tvdb_id(self):
        client = RecordingClient(response(200, content=json.dumps(
            [{"tvdbId": 11}, {"tvdbId": 22}]).encode()))
        with sonarr(client):
            ids = await arr.library_ids("sonarr", settings())
        self.assertEqual(client.only.url, "http://sonarr.local:8989/api/v3/series")
        self.assertEqual(ids, [11, 22])

    async def test_radarr_reads_movie_by_tmdb_id(self):
        # The path AND the field both switch on `kind`; a test that only checked
        # one would pass with the other transposed.
        client = RecordingClient(response(200, content=json.dumps(
            [{"tmdbId": 33}]).encode()))
        with radarr(client):
            ids = await arr.library_ids("radarr", settings())
        self.assertEqual(client.only.url, "http://radarr.local:7878/api/v3/movie")
        self.assertEqual(ids, [33])

    async def test_the_key_travels_in_a_header_here_too(self):
        client = RecordingClient(response(200, content=b"[]"))
        with sonarr(client):
            await arr.library_ids("sonarr", settings())
        self.assertEqual(client.only.headers, {"X-Api-Key": "sonarr-key"})
        self.assertEqual(client.only.query, {})

    async def test_an_unconfigured_service_is_a_knowable_empty(self):
        # Empty list, NOT LibraryUnavailable: "you have not set this up" is a
        # complete answer, unlike "I could not reach it".
        client = RecordingClient(response(200))
        with sonarr(client):
            self.assertEqual(await arr.library_ids("sonarr", settings(sonarr_url="")), [])
        self.assertEqual(client.calls, [])


class OptionsRequestTests(unittest.IsolatedAsyncioTestCase):
    """fetch_options takes explicit credentials — it probes a value that has not
    been saved yet, so it must not read the stored ones."""

    async def test_it_asks_for_profiles_and_folders_at_the_given_url(self):
        client = RecordingClient([
            response(200, json=[{"id": 1, "name": "HD"}]),
            response(200, json=[{"path": "/tv"}]),
        ])
        with sonarr(client):
            out = await arr.fetch_options("sonarr", "http://typed.local:8989/", " typed-key ")
        self.assertEqual([c.url for c in client.calls], [
            "http://typed.local:8989/api/v3/qualityprofile",
            "http://typed.local:8989/api/v3/rootfolder",
        ])
        self.assertEqual(out, {"profiles": [{"id": 1, "name": "HD"}],
                               "folders": [{"path": "/tv"}]})

    async def test_it_uses_the_passed_key_and_not_the_stored_one(self):
        client = RecordingClient([response(200, json=[]), response(200, json=[])])
        with sonarr(client):
            await arr.fetch_options("sonarr", "http://typed.local:8989", "typed-key")
        for call in client.calls:
            self.assertEqual(call.headers, {"X-Api-Key": "typed-key"})

    async def test_a_failing_half_yields_an_empty_list_for_that_half_only(self):
        # The Settings dropdowns degrade one at a time: a server that answers
        # profiles but not folders should still populate the profiles.
        client = RecordingClient([
            response(200, json=[{"id": 1, "name": "HD"}]),
            response(500, json={}),
        ])
        with sonarr(client):
            out = await arr.fetch_options("sonarr", "http://typed.local:8989", "k")
        self.assertEqual(out["profiles"], [{"id": 1, "name": "HD"}])
        self.assertEqual(out["folders"], [])


class AddMediaRequestTests(unittest.IsolatedAsyncioTestCase):

    async def test_sonarr_looks_up_by_tvdb_term_then_posts_to_series(self):
        client = RecordingClient([
            response(200, json=[{"title": "A Show", "tvdbId": 11, "titleSlug": "a-show",
                                 "year": 2026, "seasons": []}]),
            response(201, json={}),
        ])
        with sonarr(client):
            out = await arr.add_media("sonarr", settings(), {"tvdb": 11}, "A Show")
        lookup, post = client.calls
        self.assertEqual(lookup.url, "http://sonarr.local:8989/api/v3/series/lookup")
        self.assertEqual(lookup.params, {"term": "tvdb:11"})
        self.assertEqual(post.url, "http://sonarr.local:8989/api/v3/series")
        self.assertTrue(out["ok"])

    async def test_radarr_looks_up_by_tmdb_term_then_posts_to_movie(self):
        client = RecordingClient([
            response(200, json=[{"title": "A Film", "tmdbId": 33, "titleSlug": "a-film",
                                 "year": 2026}]),
            response(201, json={}),
        ])
        with radarr(client):
            out = await arr.add_media("radarr", settings(), {"tmdb": 33}, "A Film")
        lookup, post = client.calls
        self.assertEqual(lookup.params, {"term": "tmdb:33"})
        self.assertEqual(post.url, "http://radarr.local:7878/api/v3/movie")
        self.assertTrue(out["ok"])

    async def test_the_post_carries_the_configured_profile_and_root_folder(self):
        client = RecordingClient([
            response(200, json=[{"title": "A Show", "tvdbId": 11, "titleSlug": "s",
                                 "year": 2026, "seasons": []}]),
            response(201, json={}),
        ])
        with sonarr(client):
            await arr.add_media("sonarr", settings(), {"tvdb": 11}, "A Show")
        payload = client.calls[1].json
        self.assertEqual(payload["qualityProfileId"], 3)
        self.assertEqual(payload["rootFolderPath"], "/tv")
        self.assertTrue(payload["monitored"])

    async def test_the_key_is_on_both_calls_and_never_in_the_url(self):
        client = RecordingClient([
            response(200, json=[{"title": "A Show", "tvdbId": 11, "titleSlug": "s",
                                 "year": 2026, "seasons": []}]),
            response(201, json={}),
        ])
        with sonarr(client):
            await arr.add_media("sonarr", settings(), {"tvdb": 11}, "A Show")
        for call in client.calls:
            with self.subTest(url=call.url):
                self.assertEqual(call.headers["X-Api-Key"], "sonarr-key")
                self.assertNotIn("sonarr-key", call.url)
                self.assertNotIn("apikey", call.query)

    async def test_an_unconfigured_profile_refuses_before_calling_anything(self):
        client = RecordingClient(response(200))
        with sonarr(client):
            out = await arr.add_media("sonarr", settings(sonarr_root_folder="  "),
                                      {"tvdb": 11}, "A Show")
        self.assertFalse(out["ok"])
        self.assertIn("root folder", out["error"])
        self.assertEqual(client.calls, [])

    async def test_a_title_with_no_usable_id_refuses_before_calling_anything(self):
        client = RecordingClient(response(200))
        with sonarr(client):
            out = await arr.add_media("sonarr", settings(), {"tmdb": 5}, "A Show")
        self.assertFalse(out["ok"])
        self.assertEqual(client.calls, [])

    async def test_a_title_already_in_the_library_is_reported_as_such(self):
        # A lookup result carrying an `id` is one the service already holds; the
        # add is skipped and the answer is ok, because from the user's point of
        # view the thing they asked for is true.
        client = RecordingClient(response(200, json=[{"id": 7, "title": "A Show"}]))
        with sonarr(client):
            out = await arr.add_media("sonarr", settings(), {"tvdb": 11}, "A Show")
        self.assertTrue(out["ok"])
        self.assertIn("already in Sonarr", out["message"])
        self.assertEqual(len(client.calls), 1)

    async def test_a_lookup_that_finds_nothing_says_so(self):
        client = RecordingClient(response(200, json=[]))
        with sonarr(client):
            out = await arr.add_media("sonarr", settings(), {"tvdb": 11}, "Missing Show")
        self.assertFalse(out["ok"])
        self.assertIn("Missing Show", out["error"])


# --------------------------------------------------------------------------
# 3. Failure modes. These feed LibraryUnavailable, which integrations/routes.py
#    catches BY NAME — so the exception TYPE is part of the contract.
# --------------------------------------------------------------------------

class LibraryFailureTests(unittest.IsolatedAsyncioTestCase):

    async def test_a_non_200_raises_library_unavailable(self):
        client = RecordingClient(response(500, content=b"[]"))
        with sonarr(client):
            with self.assertRaises(arr.LibraryUnavailable):
                await arr.library_ids("sonarr", settings())

    async def test_the_refusal_names_the_service_and_the_status(self):
        client = RecordingClient(response(503, content=b"[]"))
        with sonarr(client):
            with self.assertRaises(arr.LibraryUnavailable) as caught:
                await arr.library_ids("sonarr", settings())
        self.assertIn("sonarr", str(caught.exception))
        self.assertIn("503", str(caught.exception))

    async def test_a_timeout_raises_library_unavailable(self):
        client = RecordingClient(response(200), raises=httpx.ReadTimeout("too slow"))
        with sonarr(client):
            with self.assertRaises(arr.LibraryUnavailable):
                await arr.library_ids("sonarr", settings())

    async def test_a_connection_error_raises_library_unavailable(self):
        client = RecordingClient(response(200), raises=httpx.ConnectError("refused"))
        with sonarr(client):
            with self.assertRaises(arr.LibraryUnavailable):
                await arr.library_ids("sonarr", settings())

    async def test_an_unreadable_body_raises_library_unavailable(self):
        """THE FAILURE THIS TYPE WAS INVENTED FOR. A 200 carrying a login page
        rather than JSON must not read as "your library is empty" — the caller
        caches that answer, and the calendar quietly stops marking anything as
        already-added for as long as the cache holds."""
        client = RecordingClient(response(200, content=b"<html>login</html>"))
        with sonarr(client):
            with self.assertRaises(arr.LibraryUnavailable):
                await arr.library_ids("sonarr", settings())

    async def test_an_empty_library_is_not_a_failure(self):
        # The other side of the same coin: [] must still mean "empty", or the
        # distinction the exception exists to draw collapses the other way.
        client = RecordingClient(response(200, content=b"[]"))
        with sonarr(client):
            self.assertEqual(await arr.library_ids("sonarr", settings()), [])


class AddMediaFailureTests(unittest.IsolatedAsyncioTestCase):

    async def test_an_unreachable_service_is_reported_not_raised(self):
        # add_media answers a button press, so it returns a refusal the UI can
        # render rather than raising into the route.
        client = RecordingClient(response(200), raises=httpx.ConnectError("refused"))
        with sonarr(client):
            out = await arr.add_media("sonarr", settings(), {"tvdb": 11}, "A Show")
        self.assertFalse(out["ok"])
        self.assertIn("Could not reach Sonarr", out["error"])

    async def test_a_failed_lookup_reports_the_services_own_message(self):
        client = RecordingClient(response(401, json={"message": "Unauthorized"}))
        with sonarr(client):
            out = await arr.add_media("sonarr", settings(), {"tvdb": 11}, "A Show")
        self.assertFalse(out["ok"])
        self.assertIn("Unauthorized", out["error"])

    async def test_a_failed_add_reports_the_services_own_message(self):
        client = RecordingClient([
            response(200, json=[{"title": "A Show", "tvdbId": 11, "titleSlug": "s",
                                 "year": 2026, "seasons": []}]),
            response(400, json=[{"errorMessage": "Root folder does not exist"}]),
        ])
        with sonarr(client):
            out = await arr.add_media("sonarr", settings(), {"tvdb": 11}, "A Show")
        self.assertEqual(out, {"ok": False, "error": "Root folder does not exist"})


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
