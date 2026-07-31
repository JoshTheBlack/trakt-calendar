"""The share link's preview picture: one month of a calendar, as 1200x630 pixels.

WHAT THIS MODULE IS. A value object in, encoded JPEG bytes out. The caller has
already decided whose calendar this is, which month, how many airings are in the
view, which titles lead it, when each one airs, which of them are series
premieres and which poster files back them. This module turns that into an image
and knows nothing else — it does not import providers, does not read the
database, has never seen a request object, cannot go looking for a poster it was
not handed, and does not know what makes an airing a premiere. It is handed a
preformatted date STRING and a bool. That is what makes it testable with nothing
but Pillow, and it is what keeps "which shows" and "what it looks like" as two
units with two reasons to change.

WHO LOOKS AT THE OUTPUT. Not a browser: an unfurler. Discord, Slack, iMessage
and the rest fetch this once, cache it hard, and composite it onto a background
this app does not choose and cannot query. Every decision below that looks
unusual — the near-black field, the hairline edge, the opaque encode — follows
from that. So does the size everything is drawn at: a Discord embed renders
around a third of this card's width, so anything that has to survive being
scaled down by three either carries its own contrast or is not worth drawing.

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

from PIL import Image, ImageDraw, ImageFilter

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
RENDERER_VERSION = 3

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
# ONE TEXT ELEMENT WEARS IT: the month and year, and nothing else that is a
# string. A card that is gold throughout has no hierarchy; one gold line over a
# grey supporting cast is most of the distance between "the app's colours on a
# rectangle" and "designed". The accent's other uses are all STRUCTURE rather
# than type — the edge hairline, the ring round the avatar, the glow marking a
# series premiere — and they read as one family precisely because no second
# colour was invented for any of them.
ACCENT = (232, 181, 69)            # #e8b545

# Everything that is not the headline: the count and the tile captions. Bright
# enough to read at thumbnail size, dim enough that the eye lands on the month
# first.
MUTED = (155, 161, 168)            # #9BA1A8

# The air date under each title, a step dimmer again than the title itself. Three
# steps of grey is what makes a caption read as "name, then when" rather than as
# two competing lines.
DIM = (122, 128, 136)              # #7A8088

# A hairline in the accent all the way round. It costs nothing and it is what
# gives the image a defined boundary on a surface whose colour we do not control
# and cannot predict — without it, a dark card on a dark theme has no edge.
EDGE_WIDTH = 2

# ---------------------------------------------------------------------------
# layout
#
# THE SHAPE OF THE PROBLEM, because every constant below is a consequence of it.
# The card is 1.9:1 and the content is ten posters at 2:3, two rows deep. That
# makes HEIGHT the binding dimension and it is not close: ten posters wide enough
# to span the card (about 208px each) would be 312px tall, and two of those plus
# a header plus captions is 700+ pixels on a canvas 630 tall. So the tile is as
# tall as the vertical budget allows and its width follows from the aspect, and
# the width left over becomes the gutters between columns — spent on the
# captions, which need it, rather than piled up as empty margin at the sides.
#
# THE VERTICAL BUDGET, which is the arithmetic the tile size actually comes from:
#     630                       the canvas
#   -  48                       MARGIN_Y top and bottom
#   -  52                       HEADER_H, set by the ringed avatar
#   -  16                       HEADER_GAP, header to grid
#   =  514                      for the grid
#   -  20                       ROW_GAP, between the two rows
#   - 100                       two captions, CAPTION_H each
#   =  394 for two posters  ->  197 each, rounded DOWN to 195 so that 2:3 comes
#                               out whole and no poster is ever stretched
# Change any of those and TILE_H changes with it; that is why they are named.
# ---------------------------------------------------------------------------

# The inset from the left and right edges. Also the crop-safety margin: unfurlers
# on narrow screens trim the SIDES, which is why this is the generous one.
MARGIN_X = 44

# The inset from the top and bottom. Deliberately tighter than MARGIN_X, and this
# is where the biggest posters on the card come from: vertical space is the
# scarce dimension (see the arithmetic above), and no unfurler crops the top or
# bottom of a 1.91:1 image — that is the aspect they all asked for.
MARGIN_Y = 24

TILE_COLUMNS = 5
TILE_ROWS = 2
MAX_TILES = TILE_COLUMNS * TILE_ROWS

# The header: the avatar and the month, centred, and smaller than a card with a
# text column beside it could afford them. Everything given back here becomes
# poster.
AVATAR_SIZE = 40
# An accent ring around the avatar. The avatar is a photograph of unknown colour
# dropped onto a near-black field, so without a ring it either floats (a light
# picture) or disappears (a dark one).
AVATAR_RING_WIDTH = 3              # thick enough to read at thumbnail size, thin
                                   # enough not to become a second focal point
AVATAR_RING_GAP = 3                # field colour between the ring and the
                                   # picture, so the ring reads as a ring rather
                                   # than as a border printed on the photograph
AVATAR_BLOCK = AVATAR_SIZE + 2 * (AVATAR_RING_WIDTH + AVATAR_RING_GAP)
AVATAR_GAP = 16                    # from the ring's outer edge to the month

HEADING_TYPE = 34                  # the month and year: the one thing a reader
                                   # scanning a channel should get at a glance.
                                   # Down from 48 — at 48 it was competing with
                                   # the artwork instead of introducing it
COUNT_TYPE = 20

# The count rides on the header row rather than getting a line of its own, and
# this is a content decision, not a spacing one. It is the fact the card exists
# to state — "this month has 27 things in it" — so it belongs beside the month it
# counts, where the two are read as one sentence. A line of its own would have
# cost 26px of vertical, and vertical is exactly what the posters are short of.
HEADER_SEPARATOR = "·"
COUNT_GAP = 12                     # either side of the separator

HEADER_H = AVATAR_BLOCK            # the ringed avatar is the tallest thing in it
HEADER_GAP = 16                    # header to the top of the grid

# A ceiling on how wide the month+year may be drawn. A real month name is at most
# a dozen characters, but the label is built from a locale and this app does not
# choose the locale, so the width that bounds it is stated rather than assumed.
HEADING_MAX_W = 560

# Captions: the title under the poster, the air date under the title.
CAPTION_GAP = 4                    # poster to title; tight, because the caption
                                   # belongs to the poster above it and a gap
                                   # here reads as a gap between rows
TITLE_TYPE = 20
TITLE_H = 26
DATE_TYPE = 17
DATE_H = 20
CAPTION_H = CAPTION_GAP + TITLE_H + DATE_H

ROW_GAP = 20                       # between the bottom of one row's date and the
                                   # top of the next row's poster. Wide enough to
                                   # clear a premiere glow (see GLOW_PAD)

TILE_H = 195                       # see the vertical budget above
TILE_W = TILE_H * 2 // 3           # posters.py normalizes to 500x750, so 2:3 is
                                   # the true aspect and a tile is never
                                   # stretched. 195 is divisible by 3, so this is
                                   # exact rather than rounded
TILE_RADIUS = 8                    # softened corners; square ones read as a
                                   # screenshot of a grid rather than as artwork

# WHAT ONE COLUMN OCCUPIES. The content width is divided into five equal
# columns and the poster is centred in its own, so the gutter between two posters
# is DERIVED — it is the width the height-bound tile could not use (222 - 130),
# not a number anybody chose. Dividing by pitch rather than laying the posters
# out and adding gaps is what spends that leftover on the CAPTIONS: a caption may
# use its whole column, which is 70% wider than the poster above it, and a title
# is the thing on this card most starved of room.
GRID_W = CARD_W - 2 * MARGIN_X
COLUMN_PITCH = GRID_W // TILE_COLUMNS
TILE_GAP = COLUMN_PITCH - TILE_W

# A caption may use its whole column, less a hair so two neighbouring captions
# never touch. This is the width the character cap below is derived from.
CAPTION_W = COLUMN_PITCH - 6

# ---------------------------------------------------------------------------
# marking a series premiere
# ---------------------------------------------------------------------------

# A SERIES premiere — the first episode of the first season — is the strongest
# thing a month can contain, and the card already sorts them to the front. This
# is what says so on the picture.
#
# A GLOW, PLUS A HAIRLINE. The glow alone was the brief and it is not enough on
# its own: a Discord embed renders this card around 400px wide, and a soft
# gradient scaled down by three is a faint smudge nobody reads as deliberate. The
# hairline hugging the poster is the half that survives the downscale, because a
# hard edge in a strong colour stays a hard edge; the glow is what makes it look
# like emphasis rather than like a border somebody forgot to remove.
#
# AND IT SURVIVES THE JPEG, which is the other half of the same question. JPEG
# rings around HIGH-frequency edges on flat colour — hard type, thin lines. A
# blurred gradient is the lowest-frequency thing on this card and encodes almost
# for free; the hairline is the part at risk, and at quality 90 with subsampling
# off (see `_encode`) it comes back clean. Both halves were checked on a rendered
# file rather than assumed.
GLOW_PAD = 12                      # how far the glow reaches past the poster.
                                   # ROW_GAP and TILE_GAP are both wider than
                                   # 2 * this, so a glow never touches a
                                   # neighbouring tile or a neighbour's caption
GLOW_BLUR = 7                      # gaussian radius; roughly half the pad, which
                                   # is what puts the softest part of the falloff
                                   # at the edge of the padded box
GLOW_ALPHA = 150                   # of 255, before the blur spreads it. Strong
                                   # enough to survive a 3x downscale, weak
                                   # enough that it does not read as a second
                                   # poster behind the first
GLOW_RING_WIDTH = 2

# ---------------------------------------------------------------------------
# how much of a string is worth measuring
# ---------------------------------------------------------------------------

# THE CAP IS THE GEOMETRY, READ BACKWARDS. `imaging.ellipsized` trims ONE
# CHARACTER AT A TIME and re-measures after each, so its cost is linear both in
# the length of the input and in how expensive the script is to shape — a few
# hundred characters of a complex script is SECONDS of CPU, on a route an
# anonymous crawler can reach. Now that every string on this card sits in a box
# of known width, the number of characters that box could possibly show can be
# computed once, and everything past it thrown away before anything is measured.
#
# THE FONT IS PROPORTIONAL, so "how many characters fit" is not one number: at
# the caption's type size a 'W' is 21px and an 'i' is 5. The cap has to be an
# UPPER bound on what could ever be shown, so it is derived from the NARROWEST
# glyph in the bundled face that carries ink — 5px — and not from an average and
# not from a wide one. Dividing by a wide glyph would produce a small number that
# silently truncated a narrow string, which is the one failure mode a cap must
# not have. CAPTION_W // 5, plus four: one for the ellipsis the text layer adds
# and three of headroom for a face whose glyphs are narrower than this one's.
#
# WHAT IS LEFT FOR `ellipsized` TO DO is the exact fit, which is its job and
# which no character count can do: the cap makes the work BOUNDED, the measuring
# loop still makes the text CORRECT.
NARROWEST_INKED_ADVANCE = 5        # 'i' and 'l' at TITLE_TYPE, measured
MAX_DRAWN_CHARS = CAPTION_W // NARROWEST_INKED_ADVANCE + 4

# The second half of the same bound, and the half that does the work for scripts
# the cap above is generous to. A capped string is measured ONCE, and if it is
# too wide, its own average advance says roughly how many characters could fit;
# slicing to a multiple of that leaves `ellipsized` a handful of steps instead of
# dozens. The multiple is what keeps it honest: a string whose narrow characters
# are all at the END fits more of itself than its average suggests, so the
# estimate has to be scaled up before it can be trusted.
# 2x IS MEASURED, NOT GUESSED. Against a corpus of ~780 strings — random mixes,
# CJK, emoji, Arabic, and every "N narrow characters then wide ones" case up to
# the cap — 2x reproduces exactly what the text layer would have drawn from the
# uncapped string in EVERY one, while 1.5x and 1.25x each cut a few of the
# adversarial cases short. A tighter factor buys a fraction of a second on
# pathological input and pays for it in text that is quietly wrong.
ESTIMATE_SLACK = 2
MIN_ESTIMATE_CHARS = 8             # below this the loop is already trivial and
                                   # estimating costs more than it saves

# What an empty month says. Rendering it is the point (see the module docstring);
# this is the sentence that makes "nothing" look intentional.
EMPTY_LABEL = "Nothing airing"


@dataclass(frozen=True)
class Tile:
    """One title on the card: its poster, its name, when it airs, and whether it
    is a series premiere.

    `poster` is a file that already exists on disk — `Path`, not `Path | None`.
    This renderer has NO placeholder art to fall back on: a card draws fewer
    tiles rather than a row of "no poster" images, because a 1200x630 preview
    has no grid rhythm to keep and a wall of placeholders reads as a broken
    embed where seven tiles and some space reads as a design. So a tile with no
    artwork is a tile the caller drops, and the type says so rather than a
    comment saying so.

    `date_label` ARRIVES PREFORMATTED and `is_premiere` arrives already decided.
    This module has no idea what a season or an episode number is and no business
    parsing a date — the caller owns the calendar vocabulary, and a renderer that
    started classifying airings would be a second place the rule lived.
    """
    title: str
    poster: Path
    date_label: str
    is_premiere: bool = False


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
    card its avatar and nothing else, and the header closes up.
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
        _draw_header(canvas, draw, card)
        _draw_grid(canvas, draw, card.tiles)
        # Last, so nothing can be painted over the card's own boundary.
        draw.rectangle((0, 0, CARD_W - 1, CARD_H - 1), outline=ACCENT,
                       width=EDGE_WIDTH)
        return _encode(canvas)
    finally:
        canvas.close()


def grid_top() -> int:
    """The y the poster grid starts at — the first pixel below the header.

    Public because it is the bound between the two halves of the card, and
    anything checking that the header and the grid do not collide should read it
    rather than re-add the constants above.
    """
    return MARGIN_Y + HEADER_H + HEADER_GAP


# ---------------------------------------------------------------------------
# the header
# ---------------------------------------------------------------------------

def _draw_header(canvas: Image.Image, draw: ImageDraw.ImageDraw, card: Card) -> None:
    """The avatar, the month and year, and the airing count, centred as one row.

    CENTRED AS A WHOLE, which is why the pieces are measured before any of them
    is drawn: an avatar that is sometimes there and a month name of unknown
    length mean the row's width is only known once its contents are. Each piece
    is then drawn into its OWN box, so `imaging.draw_text` still ellipsizes to a
    rectangle and there is no path by which a hostile month name runs off the
    canvas — the measuring decides where the row sits, never how much ink it may
    lay down.
    """
    heading_font = imaging.font(HEADING_TYPE)
    count_font = imaging.font(COUNT_TYPE)

    icon = imaging.circular(card.avatar, AVATAR_SIZE) if card.avatar else None
    avatar_w = AVATAR_BLOCK + AVATAR_GAP if icon is not None else 0

    heading = _drawn(f"{card.month_label} {card.year}", HEADING_MAX_W, heading_font)
    count = f"{HEADER_SEPARATOR} {_count_label(card.count)}"
    heading_w = min(HEADING_MAX_W, int(heading_font.getlength(heading)) + 1)
    count_w = int(count_font.getlength(count)) + 1

    # If the pieces cannot fit between the margins, the month is the piece that
    # gives — the count is short, generated here, and load-bearing.
    room = CARD_W - 2 * MARGIN_X - avatar_w - COUNT_GAP - count_w
    heading_w = max(0, min(heading_w, room))

    left = (CARD_W - (avatar_w + heading_w + COUNT_GAP + count_w)) // 2
    # A month with nothing in it has no grid for the header to introduce, so the
    # header IS the card and sits in the middle of it. Pinned to the top it would
    # read as a card whose pictures failed to load, which is exactly the wrong
    # thing to tell somebody about a month that is genuinely empty.
    top = MARGIN_Y if card.tiles else (CARD_H - HEADER_H) // 2

    if icon is not None:
        # The ring is drawn round the BLOCK, not round the picture, which is what
        # leaves AVATAR_RING_GAP of field visible between the two.
        draw.ellipse((left, top, left + AVATAR_BLOCK - 1, top + AVATAR_BLOCK - 1),
                     outline=ACCENT, width=AVATAR_RING_WIDTH)
        inset = AVATAR_RING_WIDTH + AVATAR_RING_GAP
        with icon:
            canvas.paste(icon, (left + inset, top + inset), icon)
        left += avatar_w

    imaging.draw_text(draw, (left, top, left + heading_w, top + HEADER_H),
                      heading, heading_font, ACCENT, align="left")
    left += heading_w + COUNT_GAP
    imaging.draw_text(draw, (left, top, left + count_w, top + HEADER_H),
                      count, count_font, MUTED, align="left")


def _count_label(count: int) -> str:
    """How many airings the previewed view holds, as a sentence.

    Zero is a real answer and gets its own words rather than "0 airings", which
    reads like a counter that failed to load.
    """
    if count <= 0:
        return EMPTY_LABEL
    return f"{count} airing" if count == 1 else f"{count} airings"


# ---------------------------------------------------------------------------
# the grid
# ---------------------------------------------------------------------------

def tile_rows(count: int) -> list[int]:
    """How many tiles go in each row of the grid, top row first.

    A PARTIAL GRID IS BALANCED, NOT LEFT-OVER. Filling the top row to
    TILE_COLUMNS and dropping the remainder underneath gives seven posters as
    five-then-two, which looks like a full design that ran out of material. The
    same seven as four-then-three, with each row centred in the grid, looks like
    a design for seven. Rows differ by at most one, always.
    The captions do not disturb that: every tile carries its own, so a row of
    four and a row of three are the same shape as each other, just narrower —
    which is the property the balance depends on.
    That is this layout's answer to "fewer than ten must still look deliberate";
    the other half of the answer is that a title with no artwork is dropped by the
    caller rather than drawn as placeholder art, because a 1200x630 preview has no
    grid rhythm to protect and a wall of "no poster" images reads as a broken
    embed.

    Pure and public because it is the piece most likely to be argued with after
    somebody looks at a six-tile card, and it should be arguable without a canvas.
    """
    count = max(0, min(count, MAX_TILES))
    if count == 0:
        return []
    return [n for n in ((count + 1) // 2, count // 2) if n]


def _draw_grid(canvas: Image.Image, draw: ImageDraw.ImageDraw,
               tiles: tuple[Tile, ...]) -> None:
    """The poster grid and its captions, in the caller's order.

    ROW-MAJOR, READING ORDER: the caller's first tile is the top-left one,
    because the caller's order is a ranking (the strongest titles in the month)
    and the eye starts there.

    The block is centred both ways in the space below the header, so a card with
    three posters is a small composition in the middle of the field rather than a
    full design with a hole in it.
    """
    rows = tile_rows(len(tiles))
    if not rows:
        return
    cell_h = TILE_H + CAPTION_H
    block_h = len(rows) * cell_h + (len(rows) - 1) * ROW_GAP
    top = grid_top() + (CARD_H - MARGIN_Y - grid_top() - block_h) // 2

    index = 0
    for row, in_row in enumerate(rows):
        # Each row centred rather than flush to one end: a short bottom row
        # hanging off the left edge would undo the balance above.
        row_w = in_row * COLUMN_PITCH
        row_left = (CARD_W - row_w) // 2
        row_top = top + row * (cell_h + ROW_GAP)
        for column in range(in_row):
            _draw_tile(canvas, draw, tiles[index],
                       row_left + column * COLUMN_PITCH, row_top)
            index += 1


def _draw_tile(canvas: Image.Image, draw: ImageDraw.ImageDraw, tile: Tile,
               cell_left: int, top: int) -> None:
    """One poster, centred in its column, with its title and air date beneath.

    The caption is drawn whether or not the poster decoded: a title with a name
    and a date and a hole where the artwork should be is a worse card than one
    with a gap, but this function is not the place that decides that — the caller
    drops a tile it has no poster for, and a poster that will not DECODE is rare
    enough (a truncated download) that leaving its caption in place keeps the row
    aligned instead of shifting everything after it.
    """
    left = cell_left + (COLUMN_PITCH - TILE_W) // 2
    if tile.is_premiere:
        _draw_premiere_glow(canvas, left, top)
    _paste_poster(canvas, tile.poster, left, top)
    if tile.is_premiere:
        draw.rounded_rectangle((left, top, left + TILE_W - 1, top + TILE_H - 1),
                               radius=TILE_RADIUS, outline=ACCENT,
                               width=GLOW_RING_WIDTH)

    caption_left = cell_left + (COLUMN_PITCH - CAPTION_W) // 2
    caption_right = caption_left + CAPTION_W
    title_top = top + TILE_H + CAPTION_GAP
    title_font = imaging.font(TITLE_TYPE)
    imaging.draw_text(draw, (caption_left, title_top, caption_right, title_top + TITLE_H),
                      _drawn(tile.title, CAPTION_W, title_font), title_font, MUTED)
    date_font = imaging.font(DATE_TYPE)
    imaging.draw_text(draw, (caption_left, title_top + TITLE_H, caption_right,
                             title_top + TITLE_H + DATE_H),
                      _drawn(tile.date_label, CAPTION_W, date_font), date_font, DIM)


def _draw_premiere_glow(canvas: Image.Image, left: int, top: int) -> None:
    """The soft accent halo behind a series premiere's poster.

    Drawn on its own layer and blurred there rather than onto the canvas: a
    gaussian blur applied to the card itself would take the field, the neighbours
    and anything already drawn with it. The layer is exactly the padded box, so
    the cost is a 154x219 blur and not a 1200x630 one.
    """
    pad = GLOW_PAD
    size = (TILE_W + 2 * pad, TILE_H + 2 * pad)
    layer = Image.new("RGBA", size, (0, 0, 0, 0))
    ImageDraw.Draw(layer).rounded_rectangle(
        (pad // 2, pad // 2, size[0] - pad // 2 - 1, size[1] - pad // 2 - 1),
        radius=TILE_RADIUS + pad // 2, fill=(*ACCENT, GLOW_ALPHA))
    with layer.filter(ImageFilter.GaussianBlur(GLOW_BLUR)) as glow:
        canvas.paste(glow, (left - pad, top - pad), glow)
    layer.close()


def _paste_poster(canvas: Image.Image, poster: Path, left: int, top: int) -> None:
    """One poster, rounded, at its place in the grid.

    An unreadable file costs the card that tile's artwork and nothing else. The
    caller only hands over posters it saw on disk, but "on disk" and "decodes"
    are not the same claim — a truncated download satisfies the first and fails
    the second, and an unfurler must never get a 500 out of a half-written cache
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


# ---------------------------------------------------------------------------
# text
# ---------------------------------------------------------------------------

def _drawn(text: str, box_w: int, font) -> str:
    """As much of `text` as could possibly be shown in `box_w`. See
    MAX_DRAWN_CHARS for why anything is thrown away at all.

    Two cuts, both cheap, neither of them the final fit: the character cap, which
    is a property of the box and costs nothing; then ONE measurement, which turns
    the string's own average advance into an estimate of how much of it could
    fit. `imaging.draw_text` still ellipsizes what comes back, so the text a
    reader sees is exactly what it would have been without either cut — these
    only bound what it costs to arrive at it.
    """
    text = text[:MAX_DRAWN_CHARS]
    if len(text) <= MIN_ESTIMATE_CHARS or box_w <= 0:
        return text
    width = font.getlength(text)
    if width <= box_w:
        return text
    fitting = len(text) * box_w / width
    return text[:max(MIN_ESTIMATE_CHARS, int(fitting * ESTIMATE_SLACK))]


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
