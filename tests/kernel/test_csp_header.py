"""The CSP header lands on every response — app/main.py's
content_security_policy, registered after authz.install so it also wraps
authz's own short-circuit refusals and the themed error pages, not only a
route that reached its handler. What the policy says is app/csp.py's.

Three shapes reach a browser by different paths through the stack, so each gets
its own test: a page response, a JSON refusal, and a static file.
"""
from __future__ import annotations

import re
import unittest

from tests.support import AppTestCase

HASH = re.compile(r"'sha256-[A-Za-z0-9+/]{43}='")


def directives(header: str) -> dict[str, list[str]]:
    return {part.split()[0]: part.split()[1:] for part in header.split("; ")}


class ContentSecurityPolicyHeaderTests(AppTestCase):
    def setUp(self):
        super().setUp()
        self.sign_in_as(self.make_user("viewer", calendar_approved=True))

    def _assert_policy(self, resp):
        policy = directives(resp.headers["Content-Security-Policy"])
        self.assertEqual(policy["frame-ancestors"], ["'none'"])
        self.assertEqual(policy["base-uri"], ["'none'"])
        self.assertEqual(policy["object-src"], ["'none'"])

        # script-src AND style-src ARE COMPARED WHOLE: 'self' and the hashes of
        # the inline blocks app/csp.py lists — two scripts (the tracker reveal,
        # the error page's music) and one stylesheet (the error page's) — and
        # nothing else. Any 'unsafe-…' keyword, a host or a third hash fails
        # here; which blocks the hashes match is tests/kernel/test_inline_script.py's.
        for directive, count in (("script-src", 2), ("style-src", 1)):
            with self.subTest(directive=directive):
                sources = policy[directive]
                self.assertEqual(sources[0], "'self'")
                self.assertEqual(len(sources), 1 + count)
                for source in sources[1:]:
                    self.assertRegex(source, HASH)

        # img-src IS COMPARED WHOLE TOO, so ANY added source fails rather than
        # only the hosts somebody thought to name. Every picture a browser is
        # handed goes through the shared image proxy (app/providers/base.py's
        # proxied_image); an image this blocks is an unproxied read, fixed where
        # it is read, not by admitting its host.
        self.assertEqual(policy["img-src"], ["'self'", "data:", "https://wsrv.nl"])

        # The enforced policy is the strict one, so a report-only copy of it
        # would log every refusal a second time.
        self.assertNotIn("Content-Security-Policy-Report-Only", resp.headers)

    def test_a_page_response_carries_the_policy(self):
        resp = self.client.get("/me")
        self.assertEqual(resp.status_code, 200)
        self._assert_policy(resp)

    def test_a_json_refusal_from_authz_carries_the_policy(self):
        # request_shape_guard runs before routing even looks at the path, so a
        # mutating request with the wrong content type is refused the same way
        # whether or not anything is registered at this address.
        resp = self.client.post("/no-such-mutation", data={"x": "1"})
        self.assertEqual(resp.status_code, 415)
        self._assert_policy(resp)

    def test_a_static_file_carries_the_policy(self):
        resp = self.client.get("/static/css/style.css")
        self.assertEqual(resp.status_code, 200)
        self._assert_policy(resp)


if __name__ == "__main__":
    unittest.main()
