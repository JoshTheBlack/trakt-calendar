"""A card's network badge: TMDB's logo through the image proxy, framed by CSS, or
the 📡 with the network's name when the network has none.

RENDERED MARKUP, for the reason the badge tests beside this give: the class that
picks the frame and the address the picture is fetched from are the contract
between the card and the stylesheet and the page's Content-Security-Policy (img-src
admits the one proxy host), and a template that compiles while emitting the wrong
one passes a grep.

No network, no database.
"""
from __future__ import annotations

import re
import unittest

from app.media import logos
from tests.calendar.test_card_filter_badges import _html

DARK = logos.Logo("https://wsrv.nl/?url=amc&w=128&h=128&fit=contain&output=png", False)
LIGHT = logos.Logo("https://wsrv.nl/?url=starz&w=128&h=128&fit=contain&output=png", True)


def _render(logo_networks, can_filter=True, network="tvN") -> str:
    from app.calendar import vocab
    from app.config import Settings
    from app.endpoints import get_endpoint
    from app.templating import templates
    from tests.calendar.test_card_filter_badges import _item
    return templates.env.get_template("_card.html").render(
        item=_item(network=network), not_watching=set(), new_ids=set(), is_admin=False,
        settings=Settings(), can_filter=can_filter, month=1, year=2026,
        endpoint=get_endpoint("shows/new"), badge_modes=vocab.BadgeModes({}),
        logo_networks=logo_networks)


class TheBadgeIsTheProxiedLogoTests(unittest.TestCase):

    def test_a_dark_logo_is_the_proxys_address_on_the_light_frame(self):
        html = _render({"tvN": DARK})
        self.assertIn('<img class="network-logo" src="https://wsrv.nl/?url=amc', html)
        self.assertIn('width="26" height="26"', html)
        self.assertNotIn("is-light", html)
        self.assertNotIn("no-logo", html)

    def test_a_light_logo_carries_the_class_that_picks_the_dark_frame(self):
        self.assertIn('<img class="network-logo is-light" src="https://wsrv.nl/?url=starz',
                      _render({"tvN": LIGHT}))

    def test_a_network_with_no_logo_draws_no_picture_and_the_fallback(self):
        for logo_networks in ({}, {"Other": DARK}, None):
            with self.subTest(logo_networks=logo_networks):
                html = _render(logo_networks)
                self.assertNotIn("<img class=\"network-logo", html)
                self.assertIn("no-logo", html)
                self.assertIn("📡 tvN", html)

    def test_it_is_the_same_badge_with_or_without_the_filter_offer(self):
        for can_filter in (True, False):
            with self.subTest(can_filter=can_filter):
                self.assertIn('class="network-logo" src="https://wsrv.nl/?url=amc',
                              _render({"tvN": DARK}, can_filter=can_filter))

    def test_no_address_on_the_card_reaches_a_logo_route_of_this_app(self):
        html = _render({"tvN": DARK})
        self.assertNotIn("/api/network-logo", html)
        for src in re.findall(r'<img class="network-logo[^"]*" src="([^"]+)"', html):
            self.assertTrue(src.startswith("https://wsrv.nl/"))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
