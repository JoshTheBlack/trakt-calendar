"""app/youtube.py — which video a trailer value names.

The cases are the shapes measured in a live instance's cached Simkl payloads,
where the field is documented as a bare id and holds something else about 3% of
the time, plus the full URLs Trakt sends. Each side of the line matters: a shape
that names a video must yield it, and one that does not must yield None, or the
details panel offers a trailer button that opens YouTube's error page.
"""
from __future__ import annotations

import unittest

from app import youtube


class VideoIdTests(unittest.TestCase):
    RECOVERABLE = {
        # The documented shape.
        "dQw4w9WgXcQ": "dQw4w9WgXcQ",
        # An id with query junk glued on and cut at twenty characters.
        "CEhHdPcz0u0?si=DTY4z": "CEhHdPcz0u0",
        "OfNzue2Ofms?feature=": "OfNzue2Ofms",
        "jugdz7AakCI?": "jugdz7AakCI",
        # Fragments of a URL.
        "v=uIeUwQ8NEsw": "uIeUwQ8NEsw",
        "watch?v=drnZtufE-JU": "drnZtufE-JU",
        "=AYsEr0vfEtc": "AYsEr0vfEtc",
        "shorts/LVGqQL_nFrY?f": "LVGqQL_nFrY",
        "/R_2BhnUeakU?feature": "R_2BhnUeakU",
        # Whole URLs, which is what Trakt sends.
        "https://www.youtube.com/watch?v=dQw4w9WgXcQ": "dQw4w9WgXcQ",
        "https://youtube.com/watch?feature=share&v=dQw4w9WgXcQ": "dQw4w9WgXcQ",
        "https://youtu.be/dQw4w9WgXcQ?t=4": "dQw4w9WgXcQ",
        "https://www.youtube.com/embed/dQw4w9WgXcQ": "dQw4w9WgXcQ",
        "https://www.youtube.com/shorts/LVGqQL_nFrY": "LVGqQL_nFrY",
    }
    NAMES_NO_VIDEO = (
        # A whole URL the twenty-character cut has already destroyed.
        "https://youtu.be/EhV",
        # Another service, channel handles, a number, free text.
        "https://www.dailymot",
        "@RealTime",
        "/@legendmedia",
        "274574",
        "Lilo and scratch",
        # LONGER than an id: taking its first eleven characters embedded a video
        # that was not this title's.
        "sosaundercover",
        "a7GH44eZKq5shskR",
        # Another host's URL with an eleven-character segment is not mined.
        "https://example.com/video/dQw4w9WgXcQ",
        "", None,
    )

    def test_every_shape_that_names_a_video_yields_it(self):
        for value, expected in self.RECOVERABLE.items():
            with self.subTest(value=value):
                self.assertEqual(youtube.video_id(value), expected)

    def test_every_shape_that_names_none_yields_none(self):
        for value in self.NAMES_NO_VIDEO:
            with self.subTest(value=value):
                self.assertIsNone(youtube.video_id(value))
                self.assertEqual(youtube.watch_url(value), "")

    def test_the_watch_url_is_youtubes_own_page_for_that_id(self):
        self.assertEqual(youtube.watch_url("CEhHdPcz0u0?si=DTY4z"),
                         "https://www.youtube.com/watch?v=CEhHdPcz0u0")

    def test_the_panel_filter_is_the_same_reading(self):
        """One implementation: the template filter IS this function, so the
        provider's link and the panel's embed cannot disagree about a value."""
        from app.templating import templates
        self.assertIs(templates.env.filters["youtube_id"], youtube.video_id)
