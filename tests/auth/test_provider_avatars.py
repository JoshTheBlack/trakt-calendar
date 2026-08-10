"""app/auth/provider_avatars.py — where a URL out of a third party's JSON is
decided on, and where the app refuses to become somebody's HTTP client.

THE ALLOWLIST IS THE WHOLE SECURITY SURFACE OF THIS FEATURE, so it is tested as
a pure function with no network, no mock and no I/O: `allowed_url` answers "may
the server fetch this", and every way of getting that wrong is enumerated below
rather than left to a fetch test to stumble into.
"""
from __future__ import annotations

import unittest
from unittest.mock import patch

from app.auth import provider_avatars


class AllowedUrlTests(unittest.TestCase):
    TRAKT = "https://media.trakt.tv/images/users/000/022/950/avatars/large/19dd0d4614.png"
    PLEX = "https://plex.tv/users/30019358b2ffe534/avatar?c=1786324158"

    def test_a_real_measured_url_from_each_seeded_provider_is_allowed(self):
        """The two URLs below are the shapes measured live against the author's
        own accounts. A rule that refused these would look like the feature
        simply doing nothing."""
        self.assertEqual(provider_avatars.allowed_url("trakt", self.TRAKT), self.TRAKT)
        self.assertEqual(provider_avatars.allowed_url("plex", self.PLEX), self.PLEX)

    SIMKL_UPLOADED = "https://simkl.in/avatars/88/8814058_100.jpg?1786333234"
    SIMKL_IMPORTED = "https://lh3.googleusercontent.com/a/ACg8ocJY6khlXA4uv6=s200-c"

    def test_a_picture_simkl_itself_hosts_is_allowed(self):
        """Measured after uploading an avatar to Simkl. simkl.in is a host this
        app already talks to — the calendar's posters come from it."""
        self.assertEqual(provider_avatars.allowed_url("simkl", self.SIMKL_UPLOADED),
                         self.SIMKL_UPLOADED)

    def test_a_picture_simkl_merely_imported_is_not_fetched(self):
        """THE SAME ACCOUNT, MEASURED BEFORE IT UPLOADED ONE. Simkl hands back
        whichever identity provider the person signed up with, and that set is
        open-ended — a Facebook or Apple sign-up would give a different host
        again. Covering it would mean listing every social CDN there is, so this
        case is a deliberate no-op and this test is what keeps it one."""
        self.assertIsNone(provider_avatars.allowed_url("simkl", self.SIMKL_IMPORTED))

    def test_no_provider_may_reach_a_social_cdn(self):
        """Stated across all three, because the tempting fix for the case above
        is to add the host 'just for Simkl' — and an allowlist is per provider
        precisely so that a widening has to be argued for one at a time."""
        for provider in provider_avatars.ALLOWED_HOSTS:
            with self.subTest(provider=provider):
                self.assertIsNone(
                    provider_avatars.allowed_url(provider, self.SIMKL_IMPORTED))

    def test_the_internal_addresses_an_ssrf_would_aim_at_are_refused(self):
        """The reason this module exists. Both are reachable from inside the
        container the app runs in."""
        for url in ("http://169.254.169.254/latest/meta-data/",
                    "https://169.254.169.254/latest/meta-data/",
                    "http://localhost/admin",
                    "https://localhost/admin",
                    "http://127.0.0.1:8000/",
                    "http://[::1]/"):
            for provider in ("trakt", "plex"):
                with self.subTest(url=url, provider=provider):
                    self.assertIsNone(provider_avatars.allowed_url(provider, url))

    def test_a_lookalike_host_is_refused_because_the_match_is_whole(self):
        """A suffix test would pass all of these, which is why the comparison is
        against the whole hostname."""
        for url in ("https://evilplex.tv/x.png",
                    "https://plex.tv.attacker.com/x.png",
                    "https://notmedia.trakt.tv/x.png",
                    "https://media.trakt.tv.evil.com/x.png",
                    "https://media-trakt.tv/x.png"):
            with self.subTest(url=url):
                self.assertIsNone(provider_avatars.allowed_url("plex", url))
                self.assertIsNone(provider_avatars.allowed_url("trakt", url))

    def test_userinfo_cannot_smuggle_an_allowed_host_past_the_check(self):
        """`netloc` for this URL contains "media.trakt.tv", but the host the
        request would CONNECT to is evil.example. Comparing hostname rather than
        netloc is what makes the difference."""
        self.assertIsNone(provider_avatars.allowed_url(
            "trakt", "https://media.trakt.tv@evil.example/x.png"))

    def test_only_https_is_accepted(self):
        for url in ("http://media.trakt.tv/x.png",
                    "//media.trakt.tv/x.png",
                    "file:///etc/passwd",
                    "ftp://media.trakt.tv/x.png",
                    "data:image/png;base64,AAAA"):
            with self.subTest(url=url):
                self.assertIsNone(provider_avatars.allowed_url("trakt", url))

    def test_the_host_is_matched_case_insensitively_via_hostname(self):
        """urlparse lower-cases `hostname`, so an upper-cased host is the same
        host and must not be refused — a refusal here would be a silent drop."""
        self.assertIsNotNone(provider_avatars.allowed_url(
            "trakt", "https://MEDIA.TRAKT.TV/images/x.png"))

    def test_an_absent_or_unusable_value_is_simply_no_avatar(self):
        for value in (None, "", "   ", "not a url", 12345, b"bytes"):
            with self.subTest(value=value):
                self.assertIsNone(provider_avatars.allowed_url("trakt", value))

    def test_an_unknown_provider_has_no_allowlist_and_is_refused(self):
        """A provider nobody has measured must never inherit another's hosts."""
        self.assertIsNone(provider_avatars.allowed_url("nope", self.TRAKT))
        self.assertIsNone(provider_avatars.allowed_url("", self.TRAKT))

    def test_one_providers_host_is_not_valid_for_another(self):
        """The allowlist is PER provider, so Plex's own host is still not a place
        the Trakt path may be sent."""
        self.assertIsNone(provider_avatars.allowed_url("trakt", self.PLEX))
        self.assertIsNone(provider_avatars.allowed_url("plex", self.TRAKT))


class FetchRefusesBeforeAnyRequestTests(unittest.IsolatedAsyncioTestCase):
    """A refused URL must cost NO outbound request at all — the check is what
    stops the request being made, not something that inspects it afterwards.
    The pool's client is replaced with one that fails loudly if touched."""

    async def test_a_disallowed_url_never_reaches_the_client(self):
        class ExplodingPool:
            def client(self):
                raise AssertionError(
                    "provider_avatars.fetch opened a client for a refused URL")

        with patch.object(provider_avatars, "POOL", ExplodingPool()):
            for provider, url in (("trakt", "http://169.254.169.254/"),
                                  ("simkl", "https://lh3.googleusercontent.com/a/x"),
                                  ("plex", "https://evilplex.tv/x.png"),
                                  ("trakt", None)):
                with self.subTest(provider=provider, url=url):
                    self.assertIsNone(await provider_avatars.fetch(provider, url))
