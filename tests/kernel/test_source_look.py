"""templating.source_look — how each service is shown, in one table.

templates/_source_logo.html used to answer "Simkl, else Trakt", so a third
service would have been drawn under Trakt's name and logos on every card, flip
control and outbound button. The table is per service, and these hold it to the
registry: every registered service has an entry whose files exist, and a name
with no entry fails rather than borrowing somebody else's face.
"""
from __future__ import annotations

import unittest

from app import providers, templating
from tests.support import STATIC_DIR


class EveryRegisteredServiceHasALookTests(unittest.TestCase):

    def test_every_registered_source_has_an_entry(self):
        missing = {str(s) for s in providers.registered()} - set(templating.SOURCE_IMAGES)
        self.assertEqual(missing, set())

    def test_every_picture_named_exists(self):
        for source, files in templating.SOURCE_IMAGES.items():
            for name in files:
                with self.subTest(source=source, file=name):
                    self.assertTrue((STATIC_DIR / "images" / name).is_file())

    def test_the_label_is_the_providers_own(self):
        for source, provider in providers.registered().items():
            with self.subTest(source=str(source)):
                self.assertEqual(templating.source_look(source).label, provider.label)

    def test_a_name_with_no_entry_is_refused(self):
        with self.assertRaises(KeyError):
            templating.source_look("mercury")


class TheMacrosDrawTheTableTests(unittest.TestCase):

    def _render(self, call: str) -> str:
        template = templating.templates.env.from_string(
            "{% import '_source_logo.html' as src %}" + call)
        return template.render()

    def test_each_service_wears_its_own_name_and_pictures(self):
        for source in providers.registered():
            look = templating.source_look(source)
            with self.subTest(source=str(source)):
                html = self._render(f"{{{{ src.link('{source}', 'https://x.example/') }}}}"
                                    f"{{{{ src.mark('{source}') }}}}")
                self.assertIn(f'title="View on {look.label}"', html)
                self.assertIn(f"/static/images/{look.wordmark}", html)
                self.assertIn(f"/static/images/{look.mark}", html)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
