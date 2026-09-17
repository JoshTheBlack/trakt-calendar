"""A boosted navigation that fails is answered with HX-Redirect to the same address
(app/main.py's boosted_failure_becomes_a_navigation), and nothing else is.

The rule replaced a listener in every page's <head>. What it has to keep true:
the browser ends up on a real page for the failing address, without htmx parsing
the error page it walked away from; a fragment that fails is left alone, so it
cannot take its whole page down; and the navigation it causes is not boosted, so it
cannot loop.
"""
from __future__ import annotations

import unittest

from tests.support import AppTestCase

BOOSTED = {"HX-Request": "true", "HX-Boosted": "true", "Accept": "text/html"}
FRAGMENT = {"HX-Request": "true", "Accept": "text/html"}


class BoostedFailureTests(AppTestCase):
    def setUp(self):
        super().setUp()
        self.sign_in_as(self.make_user("viewer", calendar_approved=True))

    def test_a_boosted_request_that_fails_is_sent_there_for_real(self):
        resp = self.client.get("/no-such-page?x=1", headers=BOOSTED)
        self.assertEqual(resp.status_code, 204)
        self.assertEqual(resp.headers["HX-Redirect"], "https://testserver/no-such-page?x=1")
        # Nothing to parse: the error page is not in the answer.
        self.assertEqual(resp.content, b"")

    def test_a_page_that_answers_its_own_failure_is_sent_there_too(self):
        """A route that draws a failure page of its own — a dead share link — is a
        failure the same way an unknown address is."""
        resp = self.client.get("/s/not-a-real-token", headers=BOOSTED)
        self.assertEqual(resp.status_code, 204)
        self.assertEqual(resp.headers["HX-Redirect"], "https://testserver/s/not-a-real-token")

    def test_a_redirect_is_not_a_failure(self):
        """An account without a grant is redirected to its account page, which the
        request follows — that is an arrival, not a failure."""
        resp = self.client.get("/admin", headers=BOOSTED, follow_redirects=False)
        self.assertEqual(resp.status_code, 303)
        self.assertNotIn("HX-Redirect", resp.headers)

    def test_the_navigation_it_causes_gets_the_page(self):
        resp = self.client.get("/no-such-page", headers={"Accept": "text/html"})
        self.assertEqual(resp.status_code, 404)
        self.assertIn("lobby", resp.text)

    def test_a_fragment_that_fails_is_left_alone(self):
        resp = self.client.get("/no-such-page", headers=FRAGMENT)
        self.assertEqual(resp.status_code, 404)
        self.assertNotIn("HX-Redirect", resp.headers)

    def test_a_boosted_request_that_works_is_untouched(self):
        resp = self.client.get("/me", headers=BOOSTED)
        self.assertEqual(resp.status_code, 200)
        self.assertNotIn("HX-Redirect", resp.headers)

    def test_no_page_head_listens_for_it_any_more(self):
        head = self.client.get("/me").text.split("</head>", 1)[0]
        self.assertNotIn("htmx:response:error", head)
        self.assertNotIn("ctx.status", head)


class ThePickerIsAFormTests(AppTestCase):
    """The month picker's calendar switch asks for its own page again — a GET form
    — rather than a script building the address."""

    def setUp(self):
        super().setUp()
        self.sign_in_as(self.make_user("picker", calendar_approved=True))

    def test_the_switch_is_a_get_form_that_carries_no_month(self):
        page = self.client.get("/?year=2025").text
        form = re.search(r'<form class="control" action="/" method="get"[^>]*>(.*?)</form>',
                         page, re.S)
        self.assertIsNotNone(form, "the calendar switch is not a form")
        tag = form.group(0)
        self.assertIn('hx-get="/" hx-trigger="change" hx-target="body"', tag)
        self.assertIn('hx-push-url="true"', tag)
        self.assertIn('name="year" value="2025"', form.group(1))
        self.assertIn('name="endpoint"', form.group(1))
        self.assertNotIn('name="month"', form.group(1))
        self.assertNotIn("switchPickEndpoint", page)

    def test_what_the_form_asks_for_is_the_picker_not_a_month(self):
        resp = self.client.get("/", params={"year": "2025", "endpoint": "movies/premieres"},
                               follow_redirects=False)
        self.assertEqual(resp.status_code, 200)
        self.assertIn("Pick a Month", resp.text)


import re  # noqa: E402  (kept beside the only tests that read markup)

if __name__ == "__main__":
    unittest.main()
