"""The picture a share link unfurls into.

THE POINT OF THIS FILE is that nobody looks at this image until it is already in
somebody else's Discord channel. There is no user standing in front of it who
can retry, and the unfurler that fetched it caches what it got. So the things
worth asserting are the ones that would otherwise be discovered by a stranger:
that it is the size every unfurler expects, that it is a real JPEG, that a
hostile title cannot run ink off the canvas or into the tile next door, and that
the awkward inputs — an empty month, a poster that will not decode, an avatar
that is not an image — produce a card rather than an exception.

What a unit test CANNOT see is whether it looks right. That is what rendering
one to a file and looking at it is for.

NO DATABASE AND NO CLIENT: this renderer takes a value object and returns bytes,
which is the whole reason it was built as its own module.
"""
from __future__ import annotations

import tempfile
import unittest
from io import BytesIO
from pathlib import Path

from PIL import Image, ImageDraw

from app import imaging, share_card

TMP = Path(tempfile.mkdtemp(prefix="tns-card-files-"))

# Longer than any real title and far longer than a caption, in scripts the
# bundled Latin face has no glyphs for. Nothing here should raise, and nothing
# should escape its box.
HOSTILE = "𝕋" + "غاية في الطول " * 20 + "🎬🎬🎬" + "A" * 300


def a_poster(name: str = "poster.jpg", colour: tuple[int, int, int] = (90, 40, 40)) -> Path:
    """A 500x750 JPEG — the size the poster cache normalizes to, so these tests
    exercise the same decode path production does."""
    path = TMP / name
    if not path.exists():
        Image.new("RGB", (500, 750), colour).save(path, format="JPEG", quality=85)
    return path


def an_avatar() -> bytes:
    buffer = BytesIO()
    Image.new("RGB", (256, 256), (60, 120, 200)).save(buffer, format="PNG")
    return buffer.getvalue()


def a_tile(title: str = "A Show", poster: Path | None = None, *,
           date_label: str = "Fri 14 Aug", is_premiere: bool = False) -> share_card.Tile:
    return share_card.Tile(title=title, poster=poster or a_poster(),
                           date_label=date_label, is_premiere=is_premiere)


def tiles(count: int, title: str = "A Show") -> tuple[share_card.Tile, ...]:
    return tuple(
        a_tile(f"{title} {n}", a_poster(f"poster-{n}.jpg", (40 + n * 20, 40, 90)),
               date_label=f"Fri {n + 1} Aug")
        for n in range(count)
    )


def a_card(**overrides) -> share_card.Card:
    base = dict(month_label="August", year=2026, count=12,
                tiles=tiles(share_card.MAX_TILES), avatar=an_avatar())
    base.update(overrides)
    return share_card.Card(**base)


def rendered(card: share_card.Card) -> Image.Image:
    return Image.open(BytesIO(share_card.build_card(card)))


def column_band(index: int) -> tuple[int, int]:
    """The left and right edge of one column of the ten-tile grid.

    A full grid is five columns wide and centred, so this is where a tile's
    poster and its caption both live — and therefore the only part of the card a
    tile is allowed to change.
    """
    grid_left = (share_card.CARD_W - share_card.TILE_COLUMNS * share_card.COLUMN_PITCH) // 2
    left = grid_left + index * share_card.COLUMN_PITCH
    return left, left + share_card.COLUMN_PITCH


class ShapeTests(unittest.TestCase):
    def test_it_is_a_1200x630_jpeg(self):
        """The size every unfurler reads as `summary_large_image`. A card that
        is not this shape gets letterboxed or demoted to a thumbnail."""
        payload = share_card.build_card(a_card())
        self.assertTrue(payload.startswith(b"\xff\xd8\xff"), "not JPEG magic bytes")
        with Image.open(BytesIO(payload)) as img:
            self.assertEqual(img.format, "JPEG")
            self.assertEqual(img.size, (share_card.CARD_W, share_card.CARD_H))

    def test_the_canvas_is_opaque(self):
        """An og:image is composited onto whatever background the unfurler uses,
        so there is no transparency to preserve and JPEG could not carry it."""
        with rendered(a_card()) as img:
            self.assertEqual(img.mode, "RGB")

    def test_identical_inputs_produce_identical_bytes(self):
        """The render cache is addressed by a hash of these inputs. A renderer
        that is not reproducible cannot be cached that way at all."""
        self.assertEqual(share_card.build_card(a_card()),
                         share_card.build_card(a_card()))

    def test_the_card_has_an_accent_edge_all_the_way_round(self):
        """A defined boundary is the only thing separating a dark card from a
        dark chat surface whose colour this app does not control."""
        with rendered(a_card()) as img:
            pixels = img.convert("RGB")
            for point in ((0, 0), (share_card.CARD_W - 1, 0),
                          (0, share_card.CARD_H - 1),
                          (share_card.CARD_W - 1, share_card.CARD_H - 1),
                          (share_card.CARD_W // 2, 0)):
                with self.subTest(point=point):
                    r, g, b = pixels.getpixel(point)
                    self.assertGreater(r, g)
                    self.assertGreater(g, b)

    def test_the_grid_fits_under_the_header_and_inside_the_margins(self):
        """The tile size is derived from what the header and the captions leave
        behind, and that arithmetic is the first thing a layout change breaks:
        two rows that no longer fit would silently overrun the bottom edge."""
        cell_h = share_card.TILE_H + share_card.CAPTION_H
        block_h = share_card.TILE_ROWS * cell_h + share_card.ROW_GAP
        self.assertLessEqual(share_card.grid_top() + block_h,
                             share_card.CARD_H - share_card.MARGIN_Y)
        self.assertLessEqual(share_card.TILE_COLUMNS * share_card.COLUMN_PITCH,
                             share_card.CARD_W - 2 * share_card.MARGIN_X)


class UntrustedTextTests(unittest.TestCase):
    """Every string on this card comes from somewhere this app does not control
    — a provider's title, a month name built from a locale. The box is the
    bound, and these are the cases that would find out if it were not."""

    def test_a_pathological_title_renders_without_raising(self):
        card = a_card(tiles=(a_tile(HOSTILE),))
        with rendered(card) as img:
            self.assertEqual(img.size, (share_card.CARD_W, share_card.CARD_H))

    def test_a_pathological_month_label_renders_without_raising(self):
        with rendered(a_card(month_label=HOSTILE)) as img:
            self.assertEqual(img.size, (share_card.CARD_W, share_card.CARD_H))

    def test_a_pathological_date_renders_without_raising(self):
        with rendered(a_card(tiles=(a_tile(date_label=HOSTILE),))) as img:
            self.assertEqual(img.size, (share_card.CARD_W, share_card.CARD_H))

    def test_a_title_stays_inside_its_own_column(self):
        """A caption sits under a tile in a grid of ten. A title long enough to
        reach its neighbour would land on somebody else's poster, which is the
        visible symptom of an unbounded draw — so every column but the one that
        changed must be pixel-for-pixel identical."""
        plain = tiles(share_card.MAX_TILES)
        loud = (share_card.Tile(title=HOSTILE, poster=plain[0].poster,
                                date_label=HOSTILE),) + plain[1:]
        before, after = rendered(a_card(tiles=plain)), rendered(a_card(tiles=loud))
        with before, after:
            for index in range(1, share_card.TILE_COLUMNS):
                left, right = column_band(index)
                box = (left, share_card.EDGE_WIDTH, right,
                       share_card.CARD_H - share_card.EDGE_WIDTH)
                with self.subTest(column=index):
                    self.assertEqual(before.convert("RGB").crop(box).tobytes(),
                                     after.convert("RGB").crop(box).tobytes())

    def test_a_long_string_costs_a_bounded_amount_of_work(self):
        """The text layer trims one character at a time and measures after each,
        so its cost is linear in the input — and this renderer sits behind a
        route anonymous crawlers reach. Past the cap, nothing a reader can see
        changes, so the extra characters buy only CPU."""
        capped = a_card(tiles=(a_tile(HOSTILE),))
        longer = a_card(tiles=(a_tile(HOSTILE + HOSTILE),))
        self.assertGreater(len(HOSTILE), share_card.MAX_DRAWN_CHARS)
        self.assertEqual(share_card.build_card(capped), share_card.build_card(longer))

    def test_the_cap_is_wide_enough_for_the_box_it_was_derived_from(self):
        """The cap is an UPPER bound on what a caption could show, so it has to
        be derived from the narrowest glyph in the face. Derived from a wide one
        it would be a small number that quietly truncated narrow titles, which is
        the one failure a character cap must not have."""
        self.assertGreaterEqual(
            share_card.MAX_DRAWN_CHARS * share_card.NARROWEST_INKED_ADVANCE,
            share_card.CAPTION_W)

    def test_the_cheap_cuts_never_change_the_text_that_is_drawn(self):
        """The whole bargain: the cap and the estimate bound the WORK, and the
        reader is supposed to be unable to tell. The case that would break it is
        a string whose narrow characters come first and whose wide ones follow —
        its average advance understates how much of it fits — so those are what
        this walks, alongside the scripts that make the loop expensive in the
        first place."""
        draw = ImageDraw.Draw(Image.new("RGB", (10, 10)))
        font = imaging.font(share_card.TITLE_TYPE)
        cap = share_card.MAX_DRAWN_CHARS
        cases = ["日本語のタイトル" * 3, "🎬" * 20, "مسلسل عربي طويل جدا " * 3, HOSTILE]
        # Every third split rather than every one: the property is continuous in
        # the split point and this file runs on every suite.
        for split in range(1, cap, 3):
            cases += ["i" * split + "W" * (cap - split),
                      "l" * split + "M" * (cap - split),
                      "W" * split + "." * (cap - split)]
        for case in cases:
            with self.subTest(case=case[:20]):
                self.assertEqual(
                    imaging.ellipsized(draw, case[:cap], font, share_card.CAPTION_W),
                    imaging.ellipsized(draw, share_card._drawn(case, share_card.CAPTION_W, font),
                                       font, share_card.CAPTION_W))

    def test_a_capped_title_is_still_ellipsized_to_the_box(self):
        """The cap bounds the WORK; the text layer still bounds the INK. A title
        cut to the cap and drawn raw would run out of its column."""
        wide = a_card(tiles=(a_tile("W" * share_card.MAX_DRAWN_CHARS),))
        narrow = a_card(tiles=(a_tile("W" * (share_card.MAX_DRAWN_CHARS * 2)),))
        self.assertEqual(share_card.build_card(wide), share_card.build_card(narrow))


class EmptyMonthTests(unittest.TestCase):
    """Nobody asked for this render — a crawler did — so an empty month is an
    answer to give, not an error to raise."""

    def test_a_month_with_nothing_in_it_still_renders(self):
        with rendered(a_card(count=0, tiles=())) as img:
            self.assertEqual(img.size, (share_card.CARD_W, share_card.CARD_H))

    def test_the_count_reads_as_a_sentence_rather_than_a_number(self):
        self.assertEqual(share_card._count_label(0), share_card.EMPTY_LABEL)
        self.assertEqual(share_card._count_label(-1), share_card.EMPTY_LABEL)
        self.assertEqual(share_card._count_label(1), "1 airing")
        self.assertEqual(share_card._count_label(12), "12 airings")

    def test_the_count_is_on_the_card(self):
        """It is the fact the card exists to state, so it is content rather than
        decoration: a card that dropped it would still pass every other
        assertion here."""
        self.assertNotEqual(share_card.build_card(a_card(count=12)),
                            share_card.build_card(a_card(count=13)))


class PosterTests(unittest.TestCase):
    def test_every_tile_count_up_to_a_full_grid_renders(self):
        """A month thinner than the grid is the ordinary case, not an edge one —
        a title whose artwork never resolved is simply not passed, and there is
        no placeholder to fill its cell with."""
        for count in range(0, share_card.MAX_TILES + 1):
            with self.subTest(count=count):
                with rendered(a_card(tiles=tiles(count))) as img:
                    self.assertEqual(img.size, (share_card.CARD_W, share_card.CARD_H))

    def test_a_partial_grid_is_balanced_rather_than_left_over(self):
        """Seven posters as five-then-two looks like a full design that ran out
        of material; four-then-three looks like a design for seven. Rows never
        differ by more than one, and the total is never more than the grid."""
        self.assertEqual(share_card.tile_rows(0), [])
        self.assertEqual(share_card.tile_rows(1), [1])
        self.assertEqual(share_card.tile_rows(7), [4, 3])
        self.assertEqual(share_card.tile_rows(share_card.MAX_TILES),
                         [share_card.TILE_COLUMNS, share_card.TILE_COLUMNS])
        for count in range(0, share_card.MAX_TILES + 2):
            with self.subTest(count=count):
                rows = share_card.tile_rows(count)
                self.assertLessEqual(sum(rows), share_card.MAX_TILES)
                self.assertLessEqual(len(rows), share_card.TILE_ROWS)
                self.assertTrue(all(n <= share_card.TILE_COLUMNS for n in rows))
                if rows:
                    self.assertLessEqual(max(rows) - min(rows), 1)

    def test_the_number_of_posters_changes_the_picture(self):
        """Guards against a strip that silently draws nothing — the assertions
        above would all still pass on a card with no artwork on it at all."""
        self.assertNotEqual(share_card.build_card(a_card(tiles=tiles(0))),
                            share_card.build_card(a_card(tiles=tiles(share_card.MAX_TILES))))

    def test_a_poster_that_will_not_decode_costs_its_tile_and_not_the_card(self):
        """The caller only passes files it saw on disk, but "exists" and
        "decodes" are different claims — a truncated download satisfies the
        first. An unfurler must never get a 500 out of one."""
        broken = TMP / "not-an-image.jpg"
        broken.write_bytes(b"this is not a JPEG")
        card = a_card(tiles=(a_tile("Fine"),
                             a_tile("Broken", broken),
                             a_tile("Gone", TMP / "nope.jpg")))
        with rendered(card) as img:
            self.assertEqual(img.size, (share_card.CARD_W, share_card.CARD_H))

    def test_posters_keep_their_two_to_three_aspect(self):
        """Never stretched: the cache normalizes to 500x750 and a tile that did
        not match would distort every face on the card."""
        self.assertEqual(share_card.TILE_W * 3, share_card.TILE_H * 2)


class CaptionTests(unittest.TestCase):
    """Each tile names itself and says when it airs. Neither line can be checked
    by eye at the size an embed renders, so the properties are asserted here."""

    def test_every_tile_gets_its_own_title(self):
        """Renaming the LAST tile has to change the picture: if the captions ran
        out of room before it, the tenth show would have a poster and no name."""
        full = tiles(share_card.MAX_TILES)
        renamed = full[:-1] + (a_tile("A Completely Different Name", full[-1].poster),)
        self.assertNotEqual(share_card.build_card(a_card(tiles=full)),
                            share_card.build_card(a_card(tiles=renamed)))

    def test_every_tile_gets_its_own_date(self):
        full = tiles(share_card.MAX_TILES)
        redated = full[:-1] + (a_tile(full[-1].title, full[-1].poster,
                                      date_label="Tue 30 Jun"),)
        self.assertNotEqual(share_card.build_card(a_card(tiles=full)),
                            share_card.build_card(a_card(tiles=redated)))

    def test_a_tile_with_no_date_still_renders(self):
        """An air date this app could not parse arrives as an empty string, and
        an empty caption line is a gap rather than an exception."""
        with rendered(a_card(tiles=(a_tile(date_label=""),))) as img:
            self.assertEqual(img.size, (share_card.CARD_W, share_card.CARD_H))


class PremiereTests(unittest.TestCase):
    """A series premiere is the strongest thing a month can hold, and the card
    says so with a glow. It has to survive two hostile environments: the JPEG
    encoder, which rings around hard edges on flat colour, and Discord's embed,
    which renders this card at about a third of its size."""

    def _accent_pixels(self, img: Image.Image, box: tuple[int, int, int, int]) -> int:
        raw = img.convert("RGB").crop(box).tobytes()
        # JPEG moves every value a little, so this is "close to the accent"
        # rather than an equality test.
        return sum(1 for offset in range(0, len(raw), 3)
                   if all(abs(a - b) < 40
                          for a, b in zip(raw[offset:offset + 3], share_card.ACCENT)))

    def _marked(self, premiere: bool) -> share_card.Card:
        """A full grid whose FIRST tile is or is not a premiere, on black
        artwork — so the only accent-coloured pixels that move are the mark's."""
        black = a_poster("black.jpg", (0, 0, 0))
        rest = tuple(a_tile(f"Show {n}", black) for n in range(share_card.MAX_TILES - 1))
        return a_card(tiles=(a_tile("Lead", black, is_premiere=premiere),) + rest)

    def test_a_premiere_is_marked_and_an_ordinary_title_is_not(self):
        whole = (0, 0, share_card.CARD_W, share_card.CARD_H)
        with rendered(self._marked(False)) as plain, rendered(self._marked(True)) as marked:
            self.assertGreater(self._accent_pixels(marked, whole),
                               self._accent_pixels(plain, whole) + 200)

    def test_the_mark_survives_the_downscale_an_embed_renders_at(self):
        """A soft glow alone is a smudge at a third of the size, which is why
        there is a hairline hugging the poster as well. Scaled to the width a
        Discord embed uses, the marked card must still be visibly different.

        COUNTED AS WARM PIXELS, not as accent-coloured ones, and the difference
        is the finding: at a third of the size every gold pixel has been averaged
        with the near-black around it, so almost nothing is still CLOSE to the
        accent — what survives is that the tile's surround is warm where an
        unmarked tile's is neutral. That is what a reader actually sees in an
        embed, so it is what this asserts."""
        small = (share_card.CARD_W // 3, share_card.CARD_H // 3)
        counts = []
        for premiere in (False, True):
            with rendered(self._marked(premiere)) as img, \
                    img.resize(small, Image.LANCZOS) as thumb:
                raw = thumb.convert("RGB").tobytes()
                counts.append(sum(1 for offset in range(0, len(raw), 3)
                                  if raw[offset] > raw[offset + 2] + 40))
        self.assertGreater(counts[1], counts[0] + 100)

    def test_the_glow_stays_inside_its_own_column(self):
        """The gutters are wider than twice the glow's reach, which is the only
        reason a marked tile cannot bleed onto its neighbour's poster."""
        self.assertGreater(share_card.TILE_GAP, 2 * share_card.GLOW_PAD)
        self.assertGreater(share_card.ROW_GAP, share_card.GLOW_PAD)
        plain = tiles(share_card.MAX_TILES)
        marked = plain[:1] + (share_card.Tile(title=plain[1].title, poster=plain[1].poster,
                                              date_label=plain[1].date_label,
                                              is_premiere=True),) + plain[2:]
        before, after = rendered(a_card(tiles=plain)), rendered(a_card(tiles=marked))
        with before, after:
            left, right = column_band(0)
            box = (left, share_card.EDGE_WIDTH, right,
                   share_card.CARD_H - share_card.EDGE_WIDTH)
            self.assertEqual(before.convert("RGB").crop(box).tobytes(),
                             after.convert("RGB").crop(box).tobytes())


class AvatarTests(unittest.TestCase):
    def test_the_avatar_wears_an_accent_ring(self):
        """A photograph of unknown colour on a near-black field either floats or
        disappears; the ring is what gives it an edge. Counted along the top of
        the header row, which nothing else reaches: the month's own gold is
        centred in the row below this band."""
        # Inside the card's own accent hairline, which is the other gold thing
        # this band would otherwise run through.
        band = (share_card.EDGE_WIDTH, share_card.MARGIN_Y,
                share_card.CARD_W - share_card.EDGE_WIDTH,
                share_card.MARGIN_Y + share_card.AVATAR_RING_WIDTH + 1)

        def accent_pixels(card: share_card.Card) -> int:
            with rendered(card) as img:
                raw = img.convert("RGB").crop(band).tobytes()
                return sum(1 for offset in range(0, len(raw), 3)
                           if all(abs(a - b) < 40
                                  for a, b in zip(raw[offset:offset + 3],
                                                  share_card.ACCENT)))

        self.assertGreater(accent_pixels(a_card()), 10)
        self.assertEqual(accent_pixels(a_card(avatar=None)), 0)

    def test_the_avatar_changes_the_card_and_its_absence_does_not_break_it(self):
        with_avatar = share_card.build_card(a_card())
        without = share_card.build_card(a_card(avatar=None))
        self.assertNotEqual(with_avatar, without)
        with Image.open(BytesIO(without)) as img:
            self.assertEqual(img.size, (share_card.CARD_W, share_card.CARD_H))

    def test_bytes_that_are_not_an_image_close_the_header_up(self):
        """A missing or unreadable avatar is the ordinary case, not an error —
        there is no silhouette and no letter tile to fall back to, so the card
        renders exactly as it would for an owner who never uploaded one."""
        self.assertEqual(share_card.build_card(a_card(avatar=b"nope")),
                         share_card.build_card(a_card(avatar=None)))


if __name__ == "__main__":
    unittest.main()
