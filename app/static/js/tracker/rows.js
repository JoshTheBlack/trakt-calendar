// What the tracker's rows still need a browser for, and it is one thing.
//
// EVERYTHING ELSE IN THIS FILE WAS MARKUP IN STRINGS: the bucket order, the
// labels, two comparison functions, and the show, film and question rows
// themselves. All of it is templates/_distrakt_rows.html now, drawn from the
// payload the server was already building — and the vocabulary it re-declared
// (six bucket labels, seven weekday names, a leading-article sort) went with it,
// back to app/distrakt/discord_fmt.py where the announcement posts read the same
// facts. The two SORTS had drifted while they sat side by side in two languages:
// this one matched a leading article followed by any whitespace where Python
// matches a literal space, and ordered with localeCompare where Python orders by
// code point, so a title separated by a non-breaking space and any accented
// title came out in a different order on the page than in the post the page
// exists to produce.

// MEASURING WHAT THE BROWSER HAS ACTUALLY LAID OUT is the one question no server
// can answer: whether a title is wider than the cell drawn for it depends on the
// font, the viewport and the reader's own text size. So a title marquee-scrolls
// on hover only when it genuinely overflows.
//
// DELEGATED, AND ON THE DOCUMENT, because the rows arrive with a fragment and
// are replaced by every mutation — a listener bound per cell would have to be
// re-bound after each of those, which is the lifecycle this app keeps deleting.
// `mouseover`/`mouseout` rather than enter/leave: those two do not bubble.
document.addEventListener('mouseover', (event) => {
    if (!(event.target instanceof Element)) return;
    const cell = event.target.closest('.distrakt-title');
    if (!cell || cell.classList.contains('scrolling')) return;
    const inner = cell.querySelector('.tt');
    if (!inner) return;
    const overflow = inner.scrollWidth - cell.clientWidth;
    if (overflow <= 4) return;
    inner.style.setProperty('--scroll', overflow + 'px');
    // Long titles take proportionally longer, with a floor so a barely-clipped
    // one does not flick back and forth.
    inner.style.setProperty('--dur', Math.max(2.5, overflow / 45) + 's');
    cell.classList.add('scrolling');
});

document.addEventListener('mouseout', (event) => {
    if (!(event.target instanceof Element)) return;
    const cell = event.target.closest('.distrakt-title');
    if (!cell || cell.contains(event.relatedTarget)) return;
    cell.classList.remove('scrolling');
    const inner = cell.querySelector('.tt');
    if (inner) inner.style.removeProperty('--scroll');
});
