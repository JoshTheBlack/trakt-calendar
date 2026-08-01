"""The poster grid renderer and the export helpers underneath it.

THE POINT OF THIS FILE is the class of bug that only appears at full size. A
grid that looks right at twenty titles can be a canvas no encoder will accept at
a hundred, and the way that failure arrives — an exception out of the encoder,
after the render — is expensive and unhelpful. So the size check is tested
against the real measured ceilings, and the geometry is tested as arithmetic
rather than by looking at pixels.

What a unit test CANNOT see is whether the result looks right: whether the
header sits well, whether the numbers are where the eye expects them. That is
what the render-to-file check at the end of the session is for.
"""
from __future__ import annotations

import tempfile
import unittest
from io import BytesIO
from pathlib import Path
from unittest import mock

from PIL import Image

from app import grid_builder, ranker_export
from app.media import imaging
from tests.support import TMP

TMP = Path(tempfile.mkdtemp(prefix="tns-grid-files-"))


def entries(count: int, *, poster: Path | None = None, colour: str | None = None,
            title: str = "A Title") -> list[grid_builder.GridEntry]:
    return [
        grid_builder.GridEntry(rank=n, title=f"{title} {n}", poster=poster, colour=colour)
        for n in range(1, count + 1)
    ]


def a_poster(name: str = "poster.jpg") -> Path:
    """A real 500x750 JPEG, the size the poster cache normalizes to — so the
    common path in these tests is the same no-resampling path production uses."""
    path = TMP / name
    if not path.exists():
        Image.new("RGB", (500, 750), (90, 40, 40)).save(path, format="JPEG", quality=85)
    return path


class LayoutTests(unittest.TestCase):
    """Geometry is arithmetic, and it is the arithmetic the size guard refuses
    on — so it is checked directly rather than inferred from a finished image."""

    def test_a_full_grid_measures_as_the_formula_says(self):
        layout = grid_builder.compute_layout(25, columns=5)
        self.assertEqual(layout.width, 5 * grid_builder.TILE_W)
        self.assertEqual(
            layout.height,
            grid_builder.HEADER_H + 5 * (grid_builder.TILE_H + grid_builder.LABEL_H),
        )
        self.assertEqual(layout.rows, 5)
        self.assertEqual(len(layout.cells), 25)

    def test_an_exact_fill_and_a_short_final_row_agree_on_row_count(self):
        exact = grid_builder.compute_layout(30, columns=5)
        short = grid_builder.compute_layout(26, columns=5)
        self.assertEqual(exact.rows, 6)
        self.assertEqual(short.rows, 6)
        self.assertEqual(exact.height, short.height)

    def test_a_short_final_row_is_centred(self):
        """23 titles in 5 columns ends on three. Left-aligned they sit under the
        first three columns with a gap beside them, which reads as a broken
        render rather than as the end of a list."""
        layout = grid_builder.compute_layout(23, columns=5)
        last_row = layout.cells[-3:]
        self.assertEqual(last_row[0].tile[0], (layout.width - 3 * layout.tile_w) // 2)
        # Centred means the space left of the row equals the space right of it.
        self.assertEqual(last_row[0].tile[0], layout.width - last_row[-1].tile[2])

    def test_half_scale_scales_the_constants_rather_than_the_canvas(self):
        full = grid_builder.compute_layout(25, columns=5)
        half = grid_builder.compute_layout(25, columns=5, scale=0.5)
        self.assertEqual(half.width, full.width // 2)
        self.assertEqual(half.tile_w, grid_builder.TILE_W // 2)
        self.assertEqual(half.label_h, grid_builder.LABEL_H // 2)
        self.assertEqual(half.header_h, grid_builder.HEADER_H // 2)
        self.assertEqual(
            half.height,
            half.header_h + 5 * (half.tile_h + half.label_h),
        )

    def test_captions_add_their_strip_and_nothing_else(self):
        plain = grid_builder.compute_layout(10, columns=5)
        captioned = grid_builder.compute_layout(10, columns=5, show_titles=True)
        self.assertEqual(captioned.width, plain.width)
        self.assertEqual(captioned.height, plain.height + 2 * grid_builder.CAPTION_H)
        self.assertIsNone(plain.cells[0].caption)
        self.assertIsNotNone(captioned.cells[0].caption)

    def test_the_podium_row_spans_the_width_and_the_rest_flows_normally(self):
        layout = grid_builder.compute_layout(13, columns=5, podium=True)
        self.assertEqual(layout.podium, 3)
        top = layout.cells[:3]
        # Spans the width bar the pixel or two integer division leaves over,
        # which the centring splits between the two ends.
        self.assertLessEqual(top[0].tile[0], 1)
        self.assertGreaterEqual(top[-1].tile[2], layout.width - 2)
        self.assertGreater(top[0].tile[2] - top[0].tile[0], layout.tile_w)
        # The remaining ten fall into two ordinary rows of five.
        self.assertEqual(layout.rows, 2)
        self.assertEqual(layout.cells[3].tile[2] - layout.cells[3].tile[0], layout.tile_w)

    def test_every_cell_is_inside_the_canvas(self):
        for count, columns, scale in ((23, 5, 1.0), (100, 6, 0.5), (7, 3, 1.0)):
            with self.subTest(count=count, columns=columns, scale=scale):
                layout = grid_builder.compute_layout(
                    count, columns=columns, scale=scale, show_titles=True)
                for cell in layout.cells:
                    box = cell.caption or cell.label
                    self.assertGreaterEqual(cell.tile[0], 0)
                    self.assertLessEqual(cell.tile[2], layout.width)
                    self.assertLessEqual(box[3], layout.height)


class FormatLimitTests(unittest.TestCase):
    """The hard block, and the reason it exists: WebP's ceiling is real, it is
    reached by a request the caps otherwise allow, and the encoder's own answer
    to it costs a full render first."""

    def test_a_hundred_titles_in_three_columns_exceeds_webp_and_not_jpeg(self):
        layout = grid_builder.compute_layout(100, columns=3)
        self.assertGreater(layout.height, grid_builder.MAX_DIMENSION["webp"])
        self.assertLess(layout.height, grid_builder.MAX_DIMENSION["jpeg"])
        self.assertFalse(layout.fits("webp"))
        self.assertTrue(layout.fits("jpeg"))
        self.assertTrue(layout.fits("png"))

    def test_the_refusal_carries_the_numbers_a_user_needs(self):
        with self.assertRaises(grid_builder.CanvasTooLarge) as caught:
            grid_builder.build_grid(entries(100), columns=3, fmt="webp")
        refusal = caught.exception
        self.assertEqual(refusal.limit, 16383)
        self.assertEqual(refusal.fmt, "webp")
        self.assertEqual((refusal.width, refusal.height), (1500, 27748))
        self.assertIn("27748", str(refusal))
        self.assertIn("16383", str(refusal))

    def test_the_same_hundred_titles_render_as_jpeg(self):
        """The other half of the guard: the block is about the FORMAT's ceiling,
        not about the request being unreasonable. Refusing this too would make
        the remedy the error message offers a lie."""
        payload = grid_builder.build_grid(entries(100), columns=3, fmt="jpeg")
        with Image.open(BytesIO(payload)) as img:
            self.assertEqual(img.size, (1500, 27748))

    def test_five_and_six_columns_fit_webp_at_the_item_ceiling(self):
        for columns in (5, 6):
            with self.subTest(columns=columns):
                self.assertTrue(grid_builder.compute_layout(100, columns=columns).fits("webp"))

    def test_half_scale_brings_a_refused_grid_under_the_limit(self):
        """One of the three remedies the message names, so it had better work."""
        self.assertTrue(
            grid_builder.compute_layout(100, columns=3, scale=0.5).fits("webp"))

    def test_an_unknown_format_is_refused(self):
        with self.assertRaises(grid_builder.GridError):
            grid_builder.build_grid(entries(4), columns=3, fmt="gif")


class RenderTests(unittest.TestCase):
    def test_all_three_formats_decode_to_the_expected_image(self):
        expected = grid_builder.compute_layout(9, columns=3)
        for fmt, pillow_name in (("webp", "WEBP"), ("jpeg", "JPEG"), ("png", "PNG")):
            with self.subTest(fmt=fmt):
                payload = grid_builder.build_grid(
                    entries(9, poster=a_poster()), columns=3, fmt=fmt,
                    title="Top Shows", username="someone")
                with Image.open(BytesIO(payload)) as img:
                    self.assertEqual(img.format, pillow_name)
                    self.assertEqual(img.size, (expected.width, expected.height))
                    self.assertEqual(img.convert("RGB").mode, "RGB")

    def test_identical_inputs_produce_identical_bytes(self):
        """An export that is not reproducible cannot be cached on a hash of its
        inputs, and re-downloading an unchanged ranking would hand back a
        different file each time."""
        first = grid_builder.build_grid(
            entries(6, poster=a_poster()), columns=3, title="T", username="u")
        second = grid_builder.build_grid(
            entries(6, poster=a_poster()), columns=3, title="T", username="u")
        self.assertEqual(first, second)

    def test_an_empty_ranking_is_refused_rather_than_rendered(self):
        with self.assertRaises(grid_builder.GridError):
            grid_builder.build_grid([], columns=5)

    def test_the_canvas_has_no_alpha_channel(self):
        """RGB, not RGBA: there is no transparency in the output and the extra
        channel would cost a third more memory for nothing."""
        payload = grid_builder.build_grid(entries(3), columns=3, fmt="png")
        with Image.open(BytesIO(payload)) as img:
            self.assertEqual(img.mode, "RGB")


class PlaceholderTests(unittest.TestCase):
    """A missing poster costs one tile. It must never cost the export."""

    def _placeholder_boxes(self, items) -> list:
        boxes = []
        real = grid_builder._paste_placeholder

        def record(canvas, box):
            boxes.append(box)
            real(canvas, box)

        with mock.patch.object(grid_builder, "_paste_placeholder", record):
            grid_builder.build_grid(items, columns=3, fmt="jpeg")
        return boxes

    def test_a_missing_poster_gets_the_placeholder(self):
        items = [
            grid_builder.GridEntry(rank=1, poster=a_poster()),
            grid_builder.GridEntry(rank=2, poster=None),
            grid_builder.GridEntry(rank=3, poster=a_poster()),
        ]
        self.assertEqual(len(self._placeholder_boxes(items)), 1)

    def test_an_unreadable_poster_falls_back_instead_of_raising(self):
        broken = TMP / "not-an-image.jpg"
        broken.write_bytes(b"this is not a JPEG")
        missing = TMP / "does-not-exist.jpg"
        items = [
            grid_builder.GridEntry(rank=1, poster=broken),
            grid_builder.GridEntry(rank=2, poster=missing),
            grid_builder.GridEntry(rank=3, poster=a_poster()),
        ]
        self.assertEqual(len(self._placeholder_boxes(items)), 2)

    def test_the_placeholder_fills_its_whole_tile(self):
        """A smaller image floating in a larger tile reads as artwork that
        failed to load. Filling the tile reads as a deliberate stand-in and
        keeps the grid's rhythm, which is worth the slight stretch — the source
        is 321x431, not 2:3."""
        payload = grid_builder.build_grid(
            [grid_builder.GridEntry(rank=1)], columns=3, fmt="png")
        tile = grid_builder.compute_layout(1, columns=3).cells[0].tile
        with Image.open(BytesIO(payload)) as img:
            pixels = img.convert("RGB")
            corners = (
                (tile[0] + 2, tile[1] + 2), (tile[2] - 3, tile[1] + 2),
                (tile[0] + 2, tile[3] - 3), (tile[2] - 3, tile[3] - 3),
            )
            for corner in corners:
                with self.subTest(corner=corner):
                    self.assertNotEqual(pixels.getpixel(corner), imaging.BACKGROUND)

    def test_the_placeholder_tile_is_built_once_per_size(self):
        grid_builder._placeholder_tile.cache_clear()
        grid_builder.build_grid(entries(6), columns=3, fmt="jpeg")
        self.assertEqual(grid_builder._placeholder_tile.cache_info().currsize, 1)
        self.assertEqual(grid_builder._placeholder_tile.cache_info().misses, 1)


class TextTests(unittest.TestCase):
    """Every string in the image goes through one function — `imaging.draw_text`
    — which is what makes these assertions possible at all, and what makes adding
    a font fallback later a change in one place for every renderer at once."""

    def _drawn(self, items, **kwargs) -> list[str]:
        drawn = []
        real = imaging.draw_text

        def record(draw, box, text, font, fill, **kwargs):
            drawn.append(text)
            real(draw, box, text, font, fill, **kwargs)

        with mock.patch.object(imaging, "draw_text", record):
            grid_builder.build_grid(items, columns=5, fmt="jpeg", **kwargs)
        return drawn

    def test_ranks_below_a_hundred_are_padded_to_two_digits(self):
        drawn = self._drawn(entries(99), title="Top 99", username="someone")
        self.assertIn("01", drawn)
        self.assertIn("09", drawn)
        self.assertIn("99", drawn)
        self.assertNotIn("1", drawn)

    def test_a_hundred_ranks_are_padded_to_three(self):
        """Padding follows the largest rank PRESENT. Fixed two digits breaks at
        exactly the maximum this feature allows, which is the one size somebody
        is certain to try."""
        drawn = self._drawn(entries(100))
        self.assertIn("001", drawn)
        self.assertIn("099", drawn)
        self.assertIn("100", drawn)

    def test_the_header_names_whose_list_it_is(self):
        drawn = self._drawn(entries(3), title="Top Shows", username="josh")
        self.assertIn("josh's Top Shows", drawn)

    def test_the_header_falls_back_to_whichever_was_given(self):
        self.assertIn("Top Shows", self._drawn(entries(3), title="Top Shows"))
        self.assertIn("josh", self._drawn(entries(3), username="josh"))

    def test_captions_are_drawn_only_when_asked_for(self):
        self.assertNotIn("A Title 1", self._drawn(entries(3)))
        self.assertIn("A Title 1", self._drawn(entries(3), show_titles=True))

    def test_a_tier_colour_tints_the_rank_and_a_bad_one_does_not_break_it(self):
        self.assertEqual(grid_builder._rank_colour("#FF7F7F"), (255, 127, 127))
        for bad in (None, "", "red", "#GGGGGG", "#12345"):
            with self.subTest(bad=bad):
                self.assertEqual(grid_builder._rank_colour(bad), imaging.TEXT_COLOUR)


class HeaderImageTests(unittest.TestCase):
    def test_a_header_image_is_placed_without_changing_the_canvas(self):
        icon = BytesIO()
        Image.new("RGB", (512, 512), (200, 50, 50)).save(icon, format="PNG")
        plain = grid_builder.build_grid(entries(3), columns=3, fmt="png", title="T")
        with_icon = grid_builder.build_grid(
            entries(3), columns=3, fmt="png", title="T", header_image=icon.getvalue())
        self.assertNotEqual(plain, with_icon)
        for payload in (plain, with_icon):
            with Image.open(BytesIO(payload)) as img:
                self.assertEqual(img.size,
                                 (grid_builder.compute_layout(3, columns=3).width,
                                  grid_builder.compute_layout(3, columns=3).height))

    def test_bytes_that_are_not_an_image_cost_the_icon_and_not_the_export(self):
        payload = grid_builder.build_grid(
            entries(3), columns=3, fmt="png", title="T", header_image=b"nope")
        with Image.open(BytesIO(payload)) as img:
            self.assertEqual(img.size[0], grid_builder.compute_layout(3, columns=3).width)


class RenderKeyTests(unittest.TestCase):
    """The rendered-grid cache is only correct if the key covers everything that
    can change a
    pixel. Each of these is one input somebody can change and expect to see."""

    def _ranked(self, **overrides) -> list[ranker_export.RankedTitle]:
        base = dict(media="show", match_source="tmdb", match_id="1", tmdb=1,
                    title="A Show", year=2026, network="HBO", tier_uid="tier-s",
                    tier_label="S", colour="#FF7F7F")
        base.update(overrides)
        return [ranker_export.RankedTitle(rank=1, **base)]

    def _key(self, ranked=None, **overrides) -> str:
        options = dict(renderer_version=1, columns=5, scale=1.0, fmt="webp",
                       show_titles=False, podium=False, title="Top", username="u",
                       header_image=None)
        options.update(overrides)
        return ranker_export.render_key(ranked or self._ranked(), **options)

    def test_identical_inputs_give_the_same_key(self):
        self.assertEqual(self._key(), self._key())

    def test_every_option_that_moves_a_pixel_changes_the_key(self):
        baseline = self._key()
        for change in ({"columns": 4}, {"scale": 0.5}, {"fmt": "png"},
                       {"show_titles": True}, {"podium": True}, {"title": "Other"},
                       {"username": "someone else"}, {"renderer_version": 2},
                       {"header_image": b"some bytes"}):
            with self.subTest(change=change):
                self.assertNotEqual(baseline, self._key(**change))

    def test_the_ranking_itself_changes_the_key(self):
        baseline = self._key()
        self.assertNotEqual(baseline, self._key(self._ranked(match_id="2", tmdb=2)))
        self.assertNotEqual(baseline, self._key(self._ranked(colour="#00FF00")))
        self.assertNotEqual(baseline, self._key(self._ranked(title="Renamed")))


class FilenameTests(unittest.TestCase):
    """A download filename is user text arriving in a response header."""

    def test_a_newline_never_survives_into_a_filename(self):
        name = ranker_export.download_name(("Top\r\nSet-Cookie: x=1", "2026"), "webp")
        self.assertNotIn("\r", name)
        self.assertNotIn("\n", name)
        self.assertTrue(name.endswith(".webp"))

    def test_path_separators_and_dot_segments_are_dropped(self):
        name = ranker_export.download_name(("../../etc/passwd",), "png")
        self.assertNotIn("/", name)
        self.assertNotIn("\\", name)
        self.assertEqual(name, "etc passwd.png")

    def test_a_name_that_survives_nothing_falls_back_to_a_constant(self):
        self.assertEqual(ranker_export.download_name(("進撃の巨人",), "jpeg"),
                         f"{ranker_export.FALLBACK_FILENAME}.jpeg")
        self.assertEqual(ranker_export.download_name((), "webp"),
                         f"{ranker_export.FALLBACK_FILENAME}.webp")

    def test_the_stem_is_capped(self):
        name = ranker_export.download_name(("x" * 200,), "webp")
        self.assertEqual(len(name.removesuffix(".webp")), ranker_export.MAX_FILENAME_STEM)


class MarkdownTests(unittest.TestCase):
    def _ranked(self, tiers: list[tuple[str, list[str]]]) -> list[ranker_export.RankedTitle]:
        out, rank = [], 0
        for label, titles in tiers:
            for title in titles:
                rank += 1
                out.append(ranker_export.RankedTitle(
                    rank=rank, media="show", match_source="tmdb", match_id=str(rank),
                    tmdb=rank, title=title, year=2026, network="HBO",
                    tier_uid=f"tier-{label.lower()}", tier_label=label, colour=None))
        return out

    def test_one_tier_is_a_plain_ordered_list(self):
        text = ranker_export.to_markdown(
            self._ranked([("S", ["Alpha", "Beta"])]), title="Top Shows")
        self.assertIn("# Top Shows", text)
        self.assertIn("1. **Alpha** (2026)", text)
        self.assertIn("2. **Beta** (2026)", text)
        self.assertNotIn("## S", text)

    def test_several_tiers_get_headings_and_keep_the_global_numbering(self):
        """A Markdown ordered list is renumbered from 1 by every renderer, so a
        section starting at 3 would display as 1. Grouped output writes the rank
        into the text instead."""
        text = ranker_export.to_markdown(
            self._ranked([("S", ["Alpha", "Beta"]), ("A", ["Gamma"])]), title="Top")
        self.assertIn("## S", text)
        self.assertIn("## A", text)
        self.assertIn("- **3.** **Gamma** (2026)", text)

    def test_emoji_come_from_the_supplied_map_and_are_absent_without_one(self):
        ranked = self._ranked([("S", ["Alpha"])])
        with_emoji = ranker_export.to_markdown(ranked, emojis={"HBO": "📺"})
        self.assertIn("📺 **Alpha**", with_emoji)
        plain = ranker_export.to_markdown(ranked, emojis={})
        self.assertIn("**Alpha**", plain)
        self.assertNotIn("📺", plain)

    def test_the_default_emoji_covers_a_network_with_no_entry(self):
        ranked = self._ranked([("S", ["Alpha"])])
        text = ranker_export.to_markdown(ranked, emojis={"Netflix": "🅽"}, default_emoji="🎬")
        self.assertIn("🎬 **Alpha**", text)


if __name__ == "__main__":
    unittest.main()
