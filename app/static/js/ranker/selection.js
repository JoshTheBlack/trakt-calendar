// ---------------------------------------------------------------------------
// bulk selection in the pool
// ---------------------------------------------------------------------------
// Curating a few hundred candidates one drag at a time is the difference between
// this being pleasant and being a chore.
//
// THE SELECTION IS A CHECKBOX PER ROW AND NOTHING ELSE. It used to be an array of
// keys held here, a class written onto every row whenever it changed, a count
// written into the bar, the bar's own hidden flag, and a list rebuilt for the
// move — five statements of one fact. The box is that fact: the stylesheet draws
// the highlight from `:has(.ranker-pick:checked)`, counts them with a CSS counter
// and shows the bar with `:has()`, the pool's form submits one field per ticked
// box, and Clear is the form's own reset.
//
// WHAT IS LEFT IS THE ONE PART A BROWSER HAS TO DO: reading a modifier key off a
// click, and remembering the row a shift-click should reach back to. No markup
// expresses either.

let lastClickedKey = null;      // anchor for shift-click range selection

function pickBox(row) { return row && row.querySelector('.ranker-pick'); }

// A ROW THAT LEAVES THE POOL LEAVES THE SELECTION. Every row carries the box,
// because the pool and a tier draw the same element and a drag moves the node
// the browser already has — so a title dragged or sent into a tier would keep a
// tick that the stylesheet no longer draws (the highlight is scoped to the pool)
// and the move would still submit. Giving it up on the way out is what keeps
// "picked" meaning one thing.
function unpick(row) {
    const box = pickBox(row);
    if (box) box.checked = false;
}

function onPoolClick(event) {
    const row = event.target.closest('.ranker-item');
    if (!row || event.target.closest('.ranker-act')) return;
    if (!event.shiftKey && !event.ctrlKey && !event.metaKey) return;
    event.preventDefault();
    const rows = Array.from(document.querySelectorAll('#rankerPool > .ranker-item'));
    if (event.shiftKey && lastClickedKey) {
        const from = rows.findIndex(r => r.dataset.key === lastClickedKey);
        const to = rows.indexOf(row);
        if (from >= 0 && to >= 0) {
            rows.slice(Math.min(from, to), Math.max(from, to) + 1)
                .forEach(r => { const box = pickBox(r); if (box) box.checked = true; });
        }
    } else {
        const box = pickBox(row);
        if (box) box.checked = !box.checked;
    }
    lastClickedKey = row.dataset.key;
}
