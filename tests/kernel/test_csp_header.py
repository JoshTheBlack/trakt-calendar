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
        enforced = resp.headers["Content-Security-Policy"]
        self.assertIn("frame-ancestors 'none'", enforced)
        self.assertIn("base-uri 'none'", enforced)
        self.assertIn("object-src 'none'", enforced)
        # script-src must be spelled out explicitly in the ENFORCED header, even
        # though it stays permissive: default-src 'self' with script-src simply
        # absent would fall back to default-src, which is strict and breaks the
        # inline handlers and the hx-trigger event filter this phase does not
        # touch. A missing script-src here is the regression this guards.
        enforced_script_src = enforced.split("script-src", 1)[1].split(";", 1)[0]
        self.assertIn("'self'", enforced_script_src)
        self.assertIn("unsafe-inline", enforced_script_src)
        self.assertIn("unsafe-eval", enforced_script_src)
        # Every poster this app draws — the calendar card, the ranker row, the
        # share page, and the details panel's own poster and cast headshots —
        # addresses the image proxy (app/providers/base.py's _IMAGE_PROXY); the
        # detail modules proxy their own reads before handing them back, so
        # nothing needs a second host here.
        enforced_img_src = enforced.split("img-src", 1)[1].split(";", 1)[0]
        self.assertIn("https://wsrv.nl", enforced_img_src)
        self.assertNotIn("media.trakt.tv", enforced_img_src)
        self.assertNotIn("simkl.in", enforced_img_src)

        report_only = resp.headers["Content-Security-Policy-Report-Only"]
        self.assertIn("script-src 'self'", report_only)
        self.assertNotIn("unsafe-inline", report_only.split("script-src", 1)[1].split(";", 1)[0])
        self.assertNotIn("unsafe-eval", report_only.split("script-src", 1)[1].split(";", 1)[0])
        report_only_img_src = report_only.split("img-src", 1)[1].split(";", 1)[0]
        self.assertIn("https://wsrv.nl", report_only_img_src)

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
