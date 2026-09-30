"""templates/_details.html's videos: one player, and a menu of links aimed at it.

WHY LINKS AND NOT TABS. A choice is an <a> whose `target` names the one iframe,
so pressing it loads that video into the same frame — the previous one stops
because its document is replaced, and no script or request to this server is
involved. Measured in Chromium and Firefox; these tests hold the markup to the
shape that was measured.
"""
from __future__ import annotations

import re
import unittest

from app.providers.base import Video, VideoKind
from app.templating import templates


def _yt(video_id, kind=VideoKind.TRAILER, title="", sources=("trakt",)):
    return Video(site="youtube", video_id=video_id,
                 url=f"https://www.youtube.com/watch?v={video_id}", title=title,
                 kind=kind, official=True, published=None, sources=sources)


VIMEO = Video(site="vimeo", video_id="", url="https://vimeo.com/123456",
              title="Festival Cut", kind=VideoKind.TRAILER, official=True,
              published=None, sources=("trakt",))

YAGA = [_yt("4PPodH3i_7k", VideoKind.TEASER, "Official Teaser", ("trakt", "simkl")),
        _yt("abP6JNAZjHA", VideoKind.CLIP, "Official Sneak Peek", ("trakt", "simkl"))]


def render(videos, tabs=()):
    return templates.get_template("_details.html").render(
        d={"title": "Yaga", "overview": "o", "videos": videos, "episodes": []},
        media="show", season=None, title="Yaga", poster="", progress=None,
        links=(), note="", actions="", overview="", follow_up="", tabs=tabs)


class OnePlayerTests(unittest.TestCase):
    def test_one_frame_named_for_the_rows_to_aim_at(self):
        html = render(YAGA)
        frames = re.findall(r"<iframe[^>]*>", html)
        self.assertEqual(len(frames), 1)
        self.assertIn('name="detailsPlayer"', frames[0])
        # The default is the first video, loaded without autoplay: opening a
        # panel must not start sound nobody asked for.
        self.assertIn('src="https://www.youtube-nocookie.com/embed/4PPodH3i_7k"', frames[0])

    def test_nothing_starts_playing_on_its_own(self):
        """A chosen video loads paused, like the first: asked to autoplay, one
        could sit on YouTube's loading wheel with no control to stop it."""
        html = render(YAGA)
        self.assertNotIn("autoplay", html)


class TheMenuTests(unittest.TestCase):
    def test_every_choice_targets_the_player(self):
        rows = re.findall(r'<a class="video-pick"[^>]*>', render(YAGA))
        self.assertEqual(len(rows), 2)
        for row, video in zip(rows, YAGA):
            with self.subTest(video=video.video_id):
                self.assertIn('target="detailsPlayer"', row)
                self.assertIn(f'href="https://www.youtube-nocookie.com/embed/'
                              f'{video.video_id}"', row)

    def test_the_choices_are_a_popover_opened_by_one_button(self):
        """A menu rather than a list under the player, so a long list costs the
        panel one line."""
        html = render(YAGA)
        self.assertIn('popovertarget="detailsVideoMenu"', html)
        self.assertIn("All 2 videos", html)
        menu = re.search(r'<ol class="video-menu" id="detailsVideoMenu" popover>', html)
        self.assertIsNotNone(menu)
        # Written after the player, so the menu opens below it.
        self.assertLess(html.index("<iframe"), html.index("detailsVideoMenu"))

    def test_each_row_says_what_it_is_and_who_lists_it(self):
        html = render(YAGA)
        self.assertIn("Official Sneak Peek", html)
        self.assertIn('<span class="v-kind">Clip</span>', html)
        self.assertEqual(html.count('alt="Simkl"'), 2)
        self.assertEqual(html.count("plays first"), 1)
        self.assertIn("▶️ Videos", html)

    def test_a_video_hosted_elsewhere_is_a_link_out(self):
        html = render([YAGA[0], VIMEO])
        [row] = re.findall(r'<a class="video-pick"[^>]*href="https://vimeo[^>]*>', html)
        self.assertIn('target="_blank"', row)
        self.assertIn('rel="noopener"', row)
        self.assertNotIn("detailsPlayer", row)
        self.assertIn("↗", html)
        self.assertNotIn("vimeo.com/123456\"", "".join(re.findall(r"<iframe[^>]*>", html)))

    def test_every_video_is_in_the_menu(self):
        html = render(YAGA * 1 + [_yt(f"film{n:07d}", VideoKind.FEATURETTE) for n in range(12)])
        box = html[html.index('<ol class="video-menu"'):html.index("</ol>")]
        self.assertEqual(box.count('class="video-pick"'), 14)


class HowManyTests(unittest.TestCase):
    def test_one_video_is_a_player_and_no_picker_under_its_own_kind(self):
        html = render([YAGA[0]])
        self.assertEqual(html.count("<iframe"), 1)
        self.assertNotIn("video-menu", html)
        # Named for what it is: calling a teaser "Trailer" would misname it.
        self.assertIn("▶️ Teaser", html)

    def test_no_video_is_no_section(self):
        for videos in ([], None):
            with self.subTest(videos=videos):
                html = render(videos)
                self.assertNotIn("details-videos", html)
                self.assertNotIn("<iframe", html)

    def test_one_video_it_cannot_play_is_a_button_out(self):
        html = render([VIMEO])
        self.assertNotIn("<iframe", html)
        self.assertIn('href="https://vimeo.com/123456" target="_blank" rel="noopener"', html)


if __name__ == "__main__":
    unittest.main()
