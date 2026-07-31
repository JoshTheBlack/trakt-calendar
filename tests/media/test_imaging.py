"""The shared text and image primitives every renderer in this app draws with.

THE POINT OF THIS FILE is that these functions are the ONLY place a string is
measured, shortened and put on a canvas, so the properties every renderer relies
on are properties of this module and are checked here once rather than being
re-asserted, differently, in each renderer's own tests.

Two of those properties are load-bearing rather than cosmetic. Text is drawn
into a BOX and ellipsized to it, which is what makes an untrusted username or a
provider's title safe to draw at all — the geometry is the bound. And a bad
image costs a render its icon and never the render, because an avatar is
decoration and no picture beats no card.

These tests need no database and no client: pixels in, pixels out.
"""
from __future__ import annotations

import unittest
from io import BytesIO

from PIL import Image, ImageDraw

from app import imaging

LONG_TITLE = "An Extremely Long Title Indeed"


def a_canvas(width: int, height: int) -> tuple[Image.Image, ImageDraw.ImageDraw]:
    canvas = Image.new("RGB", (width, height), imaging.BACKGROUND)
    return canvas, ImageDraw.Draw(canvas)


class FontTests(unittest.TestCase):
    def test_the_same_size_is_loaded_once(self):
        """A render asks for the same two or three sizes once per tile, so the
        cache is the difference between three font loads and three hundred."""
        imaging.font.cache_clear()
        first = imaging.font(42)
        self.assertIs(first, imaging.font(42))
        self.assertEqual(imaging.font.cache_info().misses, 1)

    def test_a_nonsense_size_still_produces_a_usable_face(self):
        """Sizes are derived from scaled layout constants, so a small enough
        scale can arithmetic its way to zero. A zero-size face raises inside
        Pillow, which would turn a legible-but-tiny render into no render."""
        for size in (0, -5):
            with self.subTest(size=size):
                canvas, draw = a_canvas(60, 20)
                imaging.draw_text(draw, (0, 0, 60, 20), "x", imaging.font(size),
                                  imaging.TEXT_COLOUR)


class EllipsisTests(unittest.TestCase):
    def test_text_too_wide_for_its_box_is_ellipsized(self):
        canvas, draw = a_canvas(200, 60)
        font = imaging.font(30)
        shown = imaging.ellipsized(draw, LONG_TITLE, font, 120)
        self.assertTrue(shown.endswith("…"))
        self.assertLess(len(shown), len(LONG_TITLE))
        bbox = draw.textbbox((0, 0), shown, font=font)
        self.assertLessEqual(bbox[2] - bbox[0], 120)

    def test_text_that_fits_is_returned_untouched(self):
        canvas, draw = a_canvas(400, 60)
        self.assertEqual(imaging.ellipsized(draw, "Hi", imaging.font(30), 300), "Hi")

    def test_a_box_too_narrow_for_even_one_glyph_gives_an_empty_string(self):
        """Rather than an ellipsis wider than the space it was trimmed to fit,
        or a loop that never terminates."""
        canvas, draw = a_canvas(400, 60)
        self.assertEqual(imaging.ellipsized(draw, LONG_TITLE, imaging.font(30), 2), "")


class DrawTextTests(unittest.TestCase):
    def test_left_aligned_text_starts_at_its_box_rather_than_floating_in_it(self):
        """A line that sits beside an icon has to start at its box. Centred in
        the space left over it drifts with the length of the name, which stops it
        reading as a label belonging to the picture next to it."""
        canvas, draw = a_canvas(400, 60)
        imaging.draw_text(draw, (0, 0, 400, 60), "Hi", imaging.font(30),
                          imaging.TEXT_COLOUR, align="left")
        ink = [x for x in range(400)
               if any(canvas.getpixel((x, y)) != imaging.BACKGROUND for y in range(60))]
        self.assertLess(min(ink), 4)

    def test_centred_text_sits_in_the_middle_of_its_box(self):
        canvas, draw = a_canvas(400, 60)
        imaging.draw_text(draw, (0, 0, 400, 60), "Hi", imaging.font(30),
                          imaging.TEXT_COLOUR)
        ink = [x for x in range(400)
               if any(canvas.getpixel((x, y)) != imaging.BACKGROUND for y in range(60))]
        # Symmetric about the centre to within a pixel of rounding.
        self.assertLessEqual(abs(min(ink) - (400 - max(ink))), 2)

    def test_no_ink_lands_outside_the_box_however_long_the_string(self):
        """THE SECURITY-SHAPED ONE. Every string these renderers draw is
        untrusted — a username, a provider's title — and the box is the only
        thing stopping one running across the rest of the image."""
        canvas, draw = a_canvas(400, 200)
        box = (100, 60, 300, 140)
        imaging.draw_text(draw, box, "W" * 500, imaging.font(40), imaging.TEXT_COLOUR)
        for x in range(400):
            for y in range(200):
                inside = box[0] <= x < box[2] and box[1] <= y < box[3]
                if not inside and canvas.getpixel((x, y)) != imaging.BACKGROUND:
                    self.fail(f"ink at {(x, y)}, outside {box}")

    def test_an_empty_string_and_an_empty_box_draw_nothing(self):
        for text, box in (("", (0, 0, 100, 40)), ("Hi", (0, 0, 0, 40)),
                          ("Hi", (0, 0, 100, 0))):
            with self.subTest(text=text, box=box):
                canvas, draw = a_canvas(100, 40)
                imaging.draw_text(draw, box, text, imaging.font(20),
                                  imaging.TEXT_COLOUR)
                self.assertEqual(canvas.getcolors(), [(100 * 40, imaging.BACKGROUND)])


class CircularTests(unittest.TestCase):
    def _icon(self, size: int = 64) -> Image.Image | None:
        buffer = BytesIO()
        Image.new("RGB", (512, 512), (200, 50, 50)).save(buffer, format="PNG")
        return imaging.circular(buffer.getvalue(), size)

    def test_an_image_comes_back_masked_to_a_circle_at_the_asked_for_size(self):
        icon = self._icon()
        self.assertIsNotNone(icon)
        self.assertEqual(icon.size, (64, 64))
        self.assertEqual(icon.mode, "RGBA")
        # Opaque in the middle, cut away at the corner — that IS the mask.
        self.assertEqual(icon.getpixel((32, 32))[3], 255)
        self.assertEqual(icon.getpixel((0, 0))[3], 0)

    def test_bytes_that_are_not_an_image_cost_the_icon_and_not_the_render(self):
        for raw in (b"", b"nope", b"\x89PNG\r\n\x1a\n truncated"):
            with self.subTest(raw=raw):
                self.assertIsNone(imaging.circular(raw, 64))


if __name__ == "__main__":
    unittest.main()
