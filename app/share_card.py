"""The share link's preview picture: one month of a calendar, as 1200x630 pixels.

WHAT THIS MODULE IS. A value object in, encoded JPEG bytes out. The caller has
already decided whose calendar this is, which month, how many airings are in the
view, which titles lead it and which poster files back them. This module turns
that into an image and knows nothing else — it does not import providers, does
not read the database, has never seen a request object, and cannot go looking
for a poster it was not handed. That is what makes it testable with nothing but
Pillow, and it is what keeps "which shows" and "what it looks like" as two units
with two reasons to change.

WHO LOOKS AT THE OUTPUT. Not a browser: an unfurler. Discord, Slack, iMessage
and the rest fetch this once, cache it hard, and composite it onto a background
this app does not choose and cannot query. Every decision below that looks
unusual — the near-black field, the hairline edge, the opaque encode, the
refusal to put anything load-bearing near the right edge — follows from that.

THE EMPTY MONTH IS NOT AN ERROR. `grid_builder` raises on an empty grid because
a person asked it for an export of nothing and deserves to be told. Here nobody
asked; a crawler did, on behalf of a link somebody pasted. The honest answer to
"what is on this calendar in March" is sometimes "nothing", and a card saying so
is a better preview than a broken image icon.

BLOCKING. Synchronous Pillow work on a path an anonymous request can reach.
Callers on the event loop run this in a worker thread and bound how many run at
once.
"""
from __future__ import annotations

from dataclasses import dataclass
from io import BytesIO
from pathlib import Path

from PIL import Image, ImageDraw

from . import imaging

# ---------------------------------------------------------------------------
# the canvas
# ---------------------------------------------------------------------------

# The size every unfurler agrees on for a large summary card. Twitter/X, Discord
# and Slack all read 1.91:1 as "show this wide", and a card that is not this
# shape gets letterboxed or demoted to a thumbnail by at least one of them.
CARD_W, CARD_H = 1200, 630

# Bumped whenever anything below changes where a pixel lands. Cards are cached
# on disk under a key that includes this number, and unfurlers cache what they
# fetched even harder — without the bump, a layout change keeps being invisible
# to everyone who already unfurled a link. THIS IS THIS MODULE'S OWN VERSION and
# has nothing to do with grid_builder's: the two renderers change independently.
RENDERER_VERSION = 1

# ---------------------------------------------------------------------------
# palette — tuned for the surface this lands on, not for the app's own chrome
# ---------------------------------------------------------------------------

# Near-black, and this is the load-bearing colour choice. Discord renders embeds
# on ~#313338 in its dark theme and ~#F2F3F5 in its light one, and the SAME JPEG
# serves both — there is no media query for an image. A near-black card is
# clearly darker than the dark surface and clearly distinct from the light one,
# so it reads as an object in either. A light card would dissolve into light
# theme. Slack and iMessage split the same way, so this generalizes.
# Deliberately a couple of points ABOVE imaging.BACKGROUND (#111315): that value
# was picked to sit behind poster art inside the app's own dark chrome, and
# against Discord's grey a near-pure black reads as a hole punched in the
# channel rather than as a card.
FIELD = (22, 24, 28)               # #16181C

# The app's own warm gold, kept rather than swapped for something Discord-ish.
# Two reasons: Discord already draws its own coloured furniture around an embed,
# so a blurple card stops reading as content and starts reading as system UI;
# and the dominant colour mass here is poster artwork whose palette is whatever
# the shows happen to be — a saturated blue competes with that, a warm gold used
# sparingly coexists with it.
# SPENT ON EXACTLY ONE ELEMENT, the month and year. A card that is gold
# throughout has no hierarchy; one gold line over a grey supporting cast is most
# of the distance between "the app's colours on a rectangle" and "designed".
ACCENT = (232, 181, 69)            # #e8b545

# Everything that is not the headline: the count and the title list. Bright
# enough to read at thumbnail size, dim enough that the eye lands on the month
# first.
MUTED = (155, 161, 168)            # #9BA1A8

# A hairline in the accent all the way round. It costs nothing and it is what
# gives the image a defined boundary on a surface whose colour we do not control
# and cannot predict — without it, a dark card on a dark theme has no edge.
EDGE_WIDTH = 2

# ---------------------------------------------------------------------------
# layout — every dimension below is derived from these, so the review that
# follows the first real render is a set of one-line changes rather than a hunt
# ---------------------------------------------------------------------------

# The inset from every side. Also the crop-safety margin: unfurlers on narrow
# screens trim the edges, and nothing inside this is at risk.
MARGIN = 44

# POSTERS RIGHT, TEXT LEFT. That is the crop-safe order — unfurlers narrow from
# the right far more often than from the left, and the text is the half that
# survives a crop usefully.
# The strip is a cascade rather than a row: five tiles side by side inside the
# space a text column can spare would be about 100px wide each, which is too
# small for artwork to be recognisable, and recognisable artwork is the entire
# reason the tiles are here. Overlapping them keeps each one poster-sized.
TILE_H = 420                       # tall enough to read; short enough to sit
                                   # inside the margins with room to breathe
TILE_W = TILE_H * 2 // 3           # posters.py normalizes to 500x750, so 2:3 is
                                   # the true aspect and a tile is never stretched
TILE_STEP = 78                     # how much of each covered tile stays visible
TILE_RADIUS = 10                   # softened corners; square ones read as a
                                   # screenshot of a grid rather than as artwork
TILE_SEPARATOR = 3                 # a field-coloured rule around each tile, so
                                   # two overlapping posters do not blend into
                                   # one another when their artwork is similar

# The most tiles the strip is laid out for. The text column's right edge is
# derived from THIS rather than from how many tiles a given card actually got,
# so a thin month and a full one put their text in the same place — a column
# that changed width with the number of posters would make every card a
# different design.
MAX_TILES = 5
STRIP_W = TILE_W + (MAX_TILES - 1) * TILE_STEP

# The gutter between the text column and the nearest poster.
COLUMN_GAP = 36

AVATAR_SIZE = 72                   # matched to the headline's cap height, so the
                                   # two read as one line rather than as a
                                   # picture with a caption beside it
AVATAR_GAP = 20

HEADING_TYPE = 54                  # the month and year: the one thing a reader
                                   # scanning a channel should get at a glance
COUNT_TYPE = 26
TITLE_TYPE = 30
WORDMARK_TYPE = 22

HEADER_H = AVATAR_SIZE + 4         # the headline row; the avatar sets its height
COUNT_GAP = 10                     # the count belongs TO the heading above it
COUNT_H = 36
TITLES_GAP = 26                    # the list is a separate block, so it gets
                                   # more air above it than the count did
TITLE_LINE_MAX = 58                # a ceiling, not a fixed step: a card with one
                                   # title should show a list at the top, not a
                                   # single line floating in the middle of one
WORDMARK_H = 32
WORDMARK_GAP = 16

# No logo and no provider wordmark, deliberately — the same rule the share
# page's meta description already follows. The app's own name is fine.
WORDMARK = "New Shows"

# A hard cap on how much of any string is handed to the text layer, applied
# before anything is measured. `imaging.ellipsized` trims ONE CHARACTER AT A
# TIME and measures after each, so its cost is linear in the length of the input
# — for a few hundred characters of a complex script that is seconds of CPU, and
# this renderer sits behind a route an anonymous crawler can reach. The cap
# cannot change what any reader sees: the column fits a few dozen Latin
# characters at most, so everything past this was going to be replaced by an
# ellipsis whatever happened. It exists purely so a long string costs a bounded
# amount of work. 96 is roughly three times the widest thing the column can
# actually show, so there is headroom for a script whose glyphs are narrower
# than Latin without there being headroom for an attack.
MAX_DRAWN_CHARS = 96

# What an empty month says. Rendering it is the point (see the module docstring);
# this is the sentence that makes "nothing" look intentional.
EMPTY_LABEL = "Nothing airing"


@dataclass(frozen=True)
class Tile:
    """One title's poster on the card.

    `poster` is a file that already exists on disk — `Path`, not `Path | None`.
    This renderer has NO placeholder art to fall back on: a card draws fewer
    tiles rather than a row of "no poster" images, because a 1200x630 preview
    has no grid rhythm to keep and a wall of placeholders reads as a broken
    embed where four tiles and some space reads as a design. So a tile with no
    artwork is a tile the caller drops, and the type says so rather than a
    comment saying so.
    """
    title: str
    poster: Path


@dataclass(frozen=True)
class Card:
    """Everything drawn on one card, already chosen and already resolved.

    Deliberately NOT a list of calendar items: this module does not know what an
    airing is, does not pick which titles appear, and never goes looking for a
    poster file. The route does that mapping, which is what keeps this testable
    with no database and no client.

    NO OWNER NAME. The card carries the owner's avatar and no name at all — a
    share link is anonymous-ish by design, and the card is the one artefact that
    gets pasted into other people's channels, so printing a username on it would
    undo that in exactly the wrong place. `avatar` is raw image bytes because
    that is what the caller has after reading the file; unreadable bytes cost the
    card its avatar and nothing else, and the layout closes up.
    """
    month_label: str                    # "August"
    year: int
    count: int                          # airings in the view being previewed
    tiles: tuple[Tile, ...] = ()
    avatar: bytes | None = None


def build_card(card: Card) -> bytes:
    """Render one card and return the encoded JPEG bytes."""
    canvas = Image.new("RGB", (CARD_W, CARD_H), FIELD)
    try:
        draw = ImageDraw.Draw(canvas)
        # Posters first: the text column is bounded away from the strip, so
        # nothing can land on top of a tile, but drawing the artwork first means
        # the edge hairline is the last thing down and is never overpainted.
        _draw_posters(canvas, card.tiles)
        _draw_text_column(canvas, draw, card)
        draw.rectangle((0, 0, CARD_W - 1, CARD_H - 1), outline=ACCENT,
                       width=EDGE_WIDTH)
        return _encode(canvas)
    finally:
        canvas.close()


def text_column() -> tuple[int, int]:
    """The left and right edges every string on the card is drawn between.

    Public because it is the bound, and a caller sizing anything against this
    layout should read it from here rather than re-deriving it from constants
    that may move at the next design pass.
    """
    return MARGIN, CARD_W - MARGIN - STRIP_W - COLUMN_GAP


def _draw_text_column(canvas: Image.Image, draw: ImageDraw.ImageDraw,
                      card: Card) -> None:
    """The heading, the count and the title list, stacked down the left.

    EVERY STRING HERE IS UNTRUSTED — the titles come from a provider — so every
    one of them goes through `imaging.draw_text`, which ellipsizes to the box it
    is given, and through `_bounded` first, which caps what it costs to do that.
    The geometry bounds what is SEEN; the cap bounds what is SPENT.
    """
    left, right = text_column()

    heading_left = left
    top = MARGIN
    if card.avatar:
        icon = imaging.circular(card.avatar, AVATAR_SIZE)
        if icon is not None:
            with icon:
                # Centred in the header row rather than pinned to its top, so it
                # sits on the heading's optical centre line.
                canvas.paste(icon, (left, top + (HEADER_H - AVATAR_SIZE) // 2), icon)
            heading_left = left + AVATAR_SIZE + AVATAR_GAP

    imaging.draw_text(
        draw, (heading_left, top, right, top + HEADER_H),
        _bounded(f"{card.month_label} {card.year}"), imaging.font(HEADING_TYPE), ACCENT,
        align="left",
    )

    count_top = top + HEADER_H + COUNT_GAP
    imaging.draw_text(
        draw, (left, count_top, right, count_top + COUNT_H), _count_label(card.count),
        imaging.font(COUNT_TYPE), MUTED, align="left",
    )

    titles_top = count_top + COUNT_H + TITLES_GAP
    titles_bottom = CARD_H - MARGIN - WORDMARK_H - WORDMARK_GAP
    if card.tiles:
        # Capped rather than stretched to fill: a two-title month should read as
        # a short list at the top of the column, not as two lines adrift in the
        # middle of one.
        line_h = min(TITLE_LINE_MAX,
                     (titles_bottom - titles_top) // len(card.tiles))
        title_font = imaging.font(TITLE_TYPE)
        for index, tile in enumerate(card.tiles):
            line_top = titles_top + index * line_h
            imaging.draw_text(draw, (left, line_top, right, line_top + line_h),
                              _bounded(tile.title), title_font, MUTED, align="left")

    imaging.draw_text(
        draw, (left, titles_bottom + WORDMARK_GAP, right, CARD_H - MARGIN),
        WORDMARK, imaging.font(WORDMARK_TYPE), MUTED, align="left",
    )


def _bounded(text: str) -> str:
    """As much of `text` as is worth measuring. See MAX_DRAWN_CHARS."""
    return text[:MAX_DRAWN_CHARS]


def _count_label(count: int) -> str:
    """How many airings the previewed view holds, as a sentence.

    Zero is a real answer and gets its own words rather than "0 airings", which
    reads like a counter that failed to load.
    """
    if count <= 0:
        return EMPTY_LABEL
    return f"{count} airing" if count == 1 else f"{count} airings"


def _draw_posters(canvas: Image.Image, tiles: tuple[Tile, ...]) -> None:
    """The cascade down the right-hand side.

    RIGHT-ALIGNED TO THE MARGIN whatever the tile count, so a card with three
    posters still has its artwork against the edge rather than a gap there. The
    text column's own right edge does not move with it (see `text_column`).

    DRAWN BACK TO FRONT — the last tile first, the first tile last — so the tile
    the caller put first is fully visible and the rest peek out behind it. The
    order the caller chose is the order that matters, and the one that gets the
    whole poster should be the one at the head of it.
    """
    if not tiles:
        return
    span = TILE_W + (len(tiles) - 1) * TILE_STEP
    strip_left = CARD_W - MARGIN - span
    top = (CARD_H - TILE_H) // 2

    for index in reversed(range(len(tiles))):
        _paste_tile(canvas, tiles[index].poster,
                    strip_left + index * TILE_STEP, top)


def _paste_tile(canvas: Image.Image, poster: Path, left: int, top: int) -> None:
    """One poster, rounded and ruled, at its place in the cascade.

    An unreadable file costs the card that tile and nothing else. The caller
    only hands over posters it saw on disk, but "on disk" and "decodes" are not
    the same claim — a truncated download satisfies the first and fails the
    second, and an unfurler must never get a 500 out of a half-written cache
    file.
    """
    try:
        with Image.open(poster) as source:
            # Lets the JPEG decoder skip straight to a smaller size, which is
            # every time here: the cache holds 500x750 and a tile is smaller.
            source.draft("RGB", (TILE_W, TILE_H))
            tile = source.convert("RGB").resize((TILE_W, TILE_H), Image.LANCZOS)
    except Exception:
        return

    with tile:
        mask = Image.new("L", (TILE_W, TILE_H), 0)
        ImageDraw.Draw(mask).rounded_rectangle(
            (0, 0, TILE_W - 1, TILE_H - 1), radius=TILE_RADIUS, fill=255)
        with mask:
            canvas.paste(tile, (left, top), mask)

    # The rule goes round the tile in the field colour, so two overlapping
    # posters with similar artwork still read as two objects.
    ImageDraw.Draw(canvas).rounded_rectangle(
        (left, top, left + TILE_W - 1, top + TILE_H - 1),
        radius=TILE_RADIUS, outline=FIELD, width=TILE_SEPARATOR)


def _encode(canvas: Image.Image) -> bytes:
    """JPEG, quality 90, no chroma subsampling.

    JPEG RATHER THAN PNG because the dominant content is photographic poster
    artwork: the same card as PNG runs three to five times larger, and some
    unfurlers cap the file size they will fetch, so the smaller file is the one
    that always renders. PNG's one real advantage, alpha, is worthless here —
    an og:image is composited onto the unfurler's own background and must be
    opaque anyway.
    Quality 90 with `subsampling=0` (4:4:4, no chroma subsampling at all) is the
    combination that keeps small text off flat colour crisp, which is where JPEG
    normally rings. These are the same numbers grid_builder._encode records as
    the measured sweet spot for this app's content; they were not re-derived.
    """
    buffer = BytesIO()
    canvas.save(buffer, format="JPEG", quality=90, subsampling=0)
    return buffer.getvalue()
