"""The picture a share link unfurls into.

THE POINT OF THIS FILE is that nobody looks at this image until it is already in
somebody else's Discord channel. There is no user standing in front of it who
can retry, and the unfurler that fetched it caches what it got. So the things
worth asserting are the ones that would otherwise be discovered by a stranger:
that it is the size every unfurler expects, that it is a real JPEG, that a
hostile title cannot run ink off the canvas, and that the awkward inputs — an
empty month, a poster that will not decode, an avatar that is not an image —
produce a card rather than an exception.

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

from PIL import Image

from app import share_card

TMP = Path(tempfile.mkdtemp(prefix="tns-card-files-"))

# Longer than any real title and far longer than the column, in scripts the
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


def tiles(count: int, title: str = "A Show") -> tuple[share_card.Tile, ...]:
    return tuple(
        share_card.Tile(title=f"{title} {n}",
                        poster=a_poster(f"poster-{n}.jpg", (40 + n * 30, 40, 90)))
        for n in range(count)
    )


def a_card(**overrides) -> share_card.Card:
    base = dict(month_label="August", year=2026, count=12, tiles=tiles(5),
                avatar=an_avatar())
    base.update(overrides)
    return share_card.Card(**base)


def rendered(card: share_card.Card) -> Image.Image:
    return Image.open(BytesIO(share_card.build_card(card)))


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


class UntrustedTextTests(unittest.TestCase):
    """Every string on this card comes from somewhere this app does not control
    — a provider's title, a month name built from a locale. The box is the
    bound, and these are the cases that would find out if it were not."""

    def test_a_pathological_title_renders_without_raising(self):
        card = a_card(tiles=(share_card.Tile(title=HOSTILE, poster=a_poster()),))
        with rendered(card) as img:
            self.assertEqual(img.size, (share_card.CARD_W, share_card.CARD_H))

    def test_a_pathological_month_label_renders_without_raising(self):
        with rendered(a_card(month_label=HOSTILE)) as img:
            self.assertEqual(img.size, (share_card.CARD_W, share_card.CARD_H))

    def test_no_text_crosses_into_the_poster_strip(self):
        """The text column is bounded away from the artwork. A title long enough
        to reach it would land ON a poster, which is the visible symptom of an
        unbounded draw — so everything right of the column must be pixel-for-
        pixel the same whether the titles are ordinary or absurd."""
        _, right = share_card.text_column()
        strip = (right + 1, share_card.EDGE_WIDTH,
                 share_card.CARD_W - share_card.EDGE_WIDTH,
                 share_card.CARD_H - share_card.EDGE_WIDTH)
        posters = tiles(5)
        plain = rendered(a_card(tiles=posters, month_label="August"))
        hostile = rendered(a_card(
            tiles=tuple(share_card.Tile(title=HOSTILE, poster=t.poster)
                        for t in posters),
            month_label=HOSTILE))
        with plain, hostile:
            self.assertEqual(plain.convert("RGB").crop(strip).tobytes(),
                             hostile.convert("RGB").crop(strip).tobytes())

    def test_a_long_string_costs_a_bounded_amount_of_work(self):
        """The text layer trims one character at a time and measures after each,
        so its cost is linear in the input — and this renderer sits behind a
        route anonymous crawlers reach. Past the cap, nothing a reader can see
        changes, so the extra characters buy only CPU."""
        capped = share_card.Card(month_label="August", year=2026, count=1,
                                 tiles=(share_card.Tile(title=HOSTILE,
                                                        poster=a_poster()),))
        longer = share_card.Card(month_label="August", year=2026, count=1,
                                 tiles=(share_card.Tile(title=HOSTILE + HOSTILE,
                                                        poster=a_poster()),))
        self.assertGreater(len(HOSTILE), share_card.MAX_DRAWN_CHARS)
        self.assertEqual(share_card.build_card(capped), share_card.build_card(longer))


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


class PosterTests(unittest.TestCase):
    def test_fewer_tiles_render_rather_than_leaving_a_gap_at_the_edge(self):
        """The strip is right-aligned however many posters there are, so a thin
        month keeps its artwork against the edge instead of trailing off."""
        for count in range(0, 6):
            with self.subTest(count=count):
                with rendered(a_card(tiles=tiles(count))) as img:
                    self.assertEqual(img.size, (share_card.CARD_W, share_card.CARD_H))

    def test_the_number_of_posters_changes_the_picture(self):
        """Guards against a strip that silently draws nothing — the assertions
        above would all still pass on a card with no artwork on it at all."""
        self.assertNotEqual(share_card.build_card(a_card(tiles=tiles(0))),
                            share_card.build_card(a_card(tiles=tiles(5))))

    def test_a_poster_that_will_not_decode_costs_its_tile_and_not_the_card(self):
        """The caller only passes files it saw on disk, but "exists" and
        "decodes" are different claims — a truncated download satisfies the
        first. An unfurler must never get a 500 out of one."""
        broken = TMP / "not-an-image.jpg"
        broken.write_bytes(b"this is not a JPEG")
        card = a_card(tiles=(share_card.Tile(title="Fine", poster=a_poster()),
                             share_card.Tile(title="Broken", poster=broken),
                             share_card.Tile(title="Gone", poster=TMP / "nope.jpg")))
        with rendered(card) as img:
            self.assertEqual(img.size, (share_card.CARD_W, share_card.CARD_H))

    def test_posters_keep_their_two_to_three_aspect(self):
        """Never stretched: the cache normalizes to 500x750 and a tile that did
        not match would distort every face on the card."""
        self.assertEqual(share_card.TILE_W * 3, share_card.TILE_H * 2)


class AvatarTests(unittest.TestCase):
    def test_the_avatar_changes_the_card_and_its_absence_does_not_break_it(self):
        with_avatar = share_card.build_card(a_card())
        without = share_card.build_card(a_card(avatar=None))
        self.assertNotEqual(with_avatar, without)
        with Image.open(BytesIO(without)) as img:
            self.assertEqual(img.size, (share_card.CARD_W, share_card.CARD_H))

    def test_bytes_that_are_not_an_image_close_the_layout_up(self):
        """A missing or unreadable avatar is the ordinary case, not an error —
        there is no silhouette and no letter tile to fall back to, so the card
        renders exactly as it would for an owner who never uploaded one."""
        self.assertEqual(share_card.build_card(a_card(avatar=b"nope")),
                         share_card.build_card(a_card(avatar=None)))


if __name__ == "__main__":
    unittest.main()
