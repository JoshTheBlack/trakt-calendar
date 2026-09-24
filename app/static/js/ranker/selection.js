// ---------------------------------------------------------------------------
// selecting titles, anywhere on the board
// ---------------------------------------------------------------------------
// Curating a few hundred candidates one drag at a time is the difference between
// this being pleasant and being a chore.
//
// WHY THIS IS SCRIPT: a selection is made by modifier-clicking rows, and no markup
// reads a modifier key off a click. The stylesheet can draw a selection but not
// make one, and no route hears about it until Move is pressed.
//
// THE DRAG LIBRARY'S SELECTION IS THE SELECTION. SortableJS's MultiDrag, which
// the vendored build bundles and ranker/dnd.js switches on, keeps ONE selection
// across every container on the board, marks each picked row
// `.sortable-selected`, selects a range on shift-click and carries every picked
// row in a drag of any one of them. The stylesheet reads that class for the
// highlight, the count and the bar. The row's hidden field follows it FOR THE
// FORM ALONE — a form can only submit fields — by being disabled whenever its
// row is not picked (_ranker_row.html says why that is not a checkbox), and
// nothing else reads the field. A second statement of which rows are picked is
// the thing this page must never grow again.

function followPick(row, picked) {
    const field = row.querySelector('.ranker-pick');
    if (field) field.disabled = !picked;   // a row still arriving has no field yet
}

function onPicked(evt) { followPick(evt.item, true); }
function onUnpicked(evt) { followPick(evt.item, false); }

// A PLAIN CLICK ON A ROW PICKS NOTHING, and the library has no setting that says
// so. MEASURED in Firefox 155 and Chromium 153: with `multiDragKey` set, a plain
// click replaces the whole selection with the row clicked — one stray click from
// losing a selection built up across a scroll of the pool — and with it unset a
// plain click toggles the row. Putting a toggle back afterwards does not work
// either: the library notes which container a selection lives in only when a
// click ADDS a row, so re-adding one it has just removed, from any other
// container, drops every other pick first.
// SO A PLAIN CLICK NEVER REACHES IT. The library toggles on the release of a
// press on a row that did not become a drag, and reads its `multiDrag` option
// afresh for each event — so it is switched off, through Sortable's own
// `option()`, for the length of that one release. Captured on the window, which
// hears the release before the document the library listens on; put back on the
// next task, once every listener for it has run. `Sortable.active` is set only
// once a press has become a drag, which leaves alone the release that ends one —
// on a touch screen, the release is how a drag of a selection is dropped.
// ONLY A PRESS THAT BEGAN ON A ROW, because that is the only press the library
// toggles for — and while the option is off its own `deselect` does nothing,
// which is what Clear, a plain click on a button, is about to ask of it.
// The key is left unset, which is also what lets Ctrl and ⌘ both select on every
// platform: any modifier held means the click is meant.
let pressedRow = null;
window.addEventListener('pointerdown', (event) => {
    pressedRow = event.target.closest('.ranker-item');
}, true);
window.addEventListener('pointerup', (event) => {
    if (!pressedRow || event.ctrlKey || event.metaKey || event.shiftKey || Sortable.active) return;
    const selecting = Array.from(sortables.values()).filter(s => s.option('multiDrag'));
    selecting.forEach(s => s.option('multiDrag', false));
    setTimeout(() => selecting.forEach(s => s.option('multiDrag', true)));
}, true);

// CLEAR IS THE FORM'S RESET BUTTON, and the reset itself changes nothing — a
// field's disabled state is not something a reset puts back. So the library is
// told to let go of every row, and each row's field follows: the library's own
// `deselect` announces nothing, unlike a click.
document.addEventListener('reset', (event) => {
    if (event.target.id !== 'selectionForm') return;
    document.querySelectorAll('.ranker-item.sortable-selected').forEach(row => {
        Sortable.utils.deselect(row);
        followPick(row, false);
    });
});
