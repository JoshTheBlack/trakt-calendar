"""The Content-Security-Policy every response carries, and the inline blocks it admits.

Its own module rather than a constant in app/main.py because the policy is no
longer a fixed string: two of its sources are hashes of what two templates
render, so writing it down means rendering those templates, and that is a
reason to change which has nothing to do with composing the app.

THE POLICY IS STRICT ON SCRIPT AND ON STYLE. Neither directive carries
'unsafe-inline' or 'unsafe-eval', so markup that somehow reaches a page — an
escaping mistake, a stored value drawn raw — can neither run script nor restyle
the page. What that asks of the rest of the app, and where each answer lives:

  - NO EVENT-HANDLER ATTRIBUTES. A hash cannot admit one without 'unsafe-hashes',
    which admits that string on any element anywhere, so every control that runs
    script carries a data- attribute instead and the file that owns the function
    listens for it on `document` (a listener bound to an element would be lost to
    the next swap).
  - NO eval. htmx 4 builds an hx-trigger event filter (`click[...]`), an `hx-on`
    expression and a `js:` value with the Function constructor, gated on no
    config flag, so none of them may appear in a template.
  - NO style="" ATTRIBUTES. A value only the server knows per render — a count
    that sizes a placeholder, a tier's colour — travels as a data- attribute,
    and style.css reads it with typed attr(). MEASURED under this policy in
    Chromium 153, Firefox 155 and WebKit 26.6: each resolved a number in calc(),
    an integer into a custom property and a <color>, and each refused a style
    attribute outright.
  - EXACTLY THE INLINE BLOCKS NAMED BELOW, each admitted by the hash of what it
    renders. tests/kernel/test_inline_script.py fails on any other.

Script and style set through the CSSOM — `element.style.x = …`, a constructed
stylesheet like the one htmx installs for its indicator class — are not inline
markup and no directive here governs them.
"""
from __future__ import annotations

import base64
import hashlib
import re

from .templating import templates

# THE TEMPLATES ALLOWED AN INLINE <script> OR <style>, and why each cannot be a
# file. The hash is taken from what the template RENDERS, once per server start —
# the same lifetime as assets.ASSET_VERSION — so editing a block changes its
# hash with it and there is no second copy of the text to keep in step. A block
# must therefore render the same bytes whatever the page around it is given;
# tests/kernel/test_inline_script.py hashes the blocks out of real responses
# and checks them against this header, which is what catches one that does not.
INLINE_BLOCKS = (
    # The tracker reveal. It must run before first paint, and a same-origin file
    # would be one more render-blocking request in a <head> whose every other
    # script is deferred. The template says why the fact is the browser's.
    "_nav_head.html",
    # The error page's styles and its music. The page has to render when the
    # static files are what broke, so it depends on nothing it would fetch from
    # them — the music included, since that is exactly when the page is shown.
    "error_lobby.html",
)

# An inline block is a tag with no attributes at all: `<script src>` is a file,
# and `<script type="application/json">` is data a browser never runs.
_INLINE = re.compile(r"<(script|style)>(.*?)</\1>", re.DOTALL)


def inline_blocks(html: str) -> list[tuple[str, str]]:
    """Every executable inline block in `html`, as (tag, text) in document order."""
    return _INLINE.findall(html)


def source_hash(text: str) -> str:
    """The CSP source expression that admits exactly this block's text."""
    digest = hashlib.sha256(text.encode("utf-8")).digest()
    return f"'sha256-{base64.b64encode(digest).decode('ascii')}'"


def _hashes() -> dict[str, list[str]]:
    # Rendered with no context: neither block interpolates anything, and the
    # test above is what holds them to that.
    found: dict[str, list[str]] = {"script": [], "style": []}
    for name in INLINE_BLOCKS:
        for tag, text in inline_blocks(templates.env.get_template(name).render()):
            found[tag].append(source_hash(text))
    return found


_HASHES = _hashes()

# img-src ADMITS ONE REMOTE HOST, AND IT MUST STAY ONE. Every picture this app
# hands a browser — a calendar card, a ranker row, the details panel's poster and
# cast headshots, the share page — addresses the shared image proxy
# (app/providers/base.py's _IMAGE_PROXY, reached through `proxied_image`;
# app/media/posters.py explains why the server warms that same host rather than
# serving its own copy). So an image this header blocks is an address that
# skipped `proxied_image`: a hotlink to a service's own CDN, which Trakt's terms
# forbid. Fix it where the address is read, never by naming the service's host
# here — a wider img-src would stop reporting exactly the mistake it caught.
#
# frame-src is the trailer, the one external resource the app embeds. Nothing
# frames this app, and nothing it serves needs a <base>, a plugin or a form
# posting off-site.
#
# NO REPORT-ONLY COPY. One shipped while script-src was permissive, carrying the
# strict version so the console could list what enforcing it would break. Now
# the enforced policy IS that version, and an identical report-only header would
# only log every refusal twice. There is no reporting endpoint either: that
# would be a route accepting unauthenticated POSTs from any browser, to collect
# what one operator can read in their own console.
POLICY = "; ".join((
    "default-src 'self'",
    " ".join(("script-src 'self'", *_HASHES["script"])),
    " ".join(("style-src 'self'", *_HASHES["style"])),
    "img-src 'self' data: https://wsrv.nl",
    "font-src 'self'",
    "connect-src 'self'",
    "frame-src https://www.youtube-nocookie.com",
    "frame-ancestors 'none'",
    "base-uri 'none'",
    "form-action 'self'",
    "object-src 'none'",
))
