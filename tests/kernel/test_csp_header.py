"""Both CSP headers land on every response — app/main.py's
content_security_policy, registered after authz.install so it also wraps
authz's own short-circuit refusals and the themed error pages, not only a
route that reached its handler.

D6 in the owner's planning notes named three shapes this has to cover: a page
response, a JSON refusal, and a static file. Each gets its own test here.
"""
from __future__ import annotations

import unittest

from tests.support import AppTestCase


class ContentSecurityPolicyHeaderTests(AppTestCase):
    def setUp(self):
        super().setUp()
        self.sign_in_as(self.make_user("viewer", calendar_approved=True))

    def _assert_both_headers(self, resp):
        self.assertIn("frame-ancestors 'none'", resp.headers["Content-Security-Policy"])
        self.assertIn("base-uri 'none'", resp.headers["Content-Security-Policy"])
        self.assertIn("object-src 'none'", resp.headers["Content-Security-Policy"])
        report_only = resp.headers["Content-Security-Policy-Report-Only"]
        self.assertIn("script-src 'self'", report_only)
        self.assertNotIn("unsafe-inline", report_only.split("script-src", 1)[1].split(";", 1)[0])
        # Every poster in this app is addressed straight at the image proxy
        # (app/providers/base.py's _IMAGE_PROXY), so img-src has to admit it or
        # a browser pass logs a violation on every card.
        self.assertIn("https://wsrv.nl", report_only.split("img-src", 1)[1].split(";", 1)[0])

    def test_a_page_response_carries_both_headers(self):
        resp = self.client.get("/me")
        self.assertEqual(resp.status_code, 200)
        self._assert_both_headers(resp)

    def test_a_json_refusal_from_authz_carries_both_headers(self):
        # request_shape_guard runs before routing even looks at the path, so a
        # mutating request with the wrong content type is refused the same way
        # whether or not anything is registered at this address.
        resp = self.client.post("/no-such-mutation", data={"x": "1"})
        self.assertEqual(resp.status_code, 415)
        self._assert_both_headers(resp)

    def test_a_static_file_carries_both_headers(self):
        resp = self.client.get("/static/css/style.css")
        self.assertEqual(resp.status_code, 200)
        self._assert_both_headers(resp)


if __name__ == "__main__":
    unittest.main()
