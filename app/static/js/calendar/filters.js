// ---- Filters (per viewer, not per instance) ----
// ALMOST NONE OF THIS FEATURE IS IN THIS FILE, WHICH IS THE POINT.
//   The panel arrives rendered, with every chip already carrying its answer, so
//   there is nothing to fetch when it opens and no client copy of the genre,
//   country, rating or release vocabulary (app/calendar/vocab.py holds it).
//   The tabs are radios and the stylesheet draws them, so switching tabs runs no
//   script and needs nothing re-bound.
//   Each chip is three radios, so the browser holds the answer and the
//   stylesheet reads it -- no state is kept here for anything to fall out of
//   step with, and Cancel is the form's own reset.
//   The save serializes the FORM rather than reading fields out by id, so this
//   file knows no field names; and what a filter spec IS -- the leading '-',
//   which dimensions fold case, that networks are a list -- stays entirely in
//   app/calendar/filter.py, reached through the field names the server spelled.
// What is left is the four things a browser cannot do declaratively: cycling a
// chip in one press, adding a token the vocabulary does not name, keeping the
// last service from being switched off, and posting the result.

function openFilters() {
    document.getElementById('filtersModal').classList.add('open');
}

// THE FORM'S OWN RESET, which is exactly right and not a shortcut: `reset()`
// puts every input back to the value the SERVER rendered, so closing the panel
// abandons an edit the same way it always did -- including a chip cycled and a
// service unticked -- without this file listing what those inputs are.
// Chips added by typing are not part of that: reset cannot remove an element, so
// they are dropped here.
function closeFilters() {
    const form = document.getElementById('filtersForm');
    form.querySelectorAll('.fchip.is-added').forEach(chip => chip.remove());
    form.reset();
    document.getElementById('filtersModal').classList.remove('open');
}

// Grey -> only these -> not these -> grey. The three radios are the answer; this
// only advances which of them is checked.
const CHIP_ORDER = ['', 'include', 'exclude'];

function cycleChip(chip) {
    const radios = [...chip.querySelectorAll('input[type="radio"]')];
    const current = radios.find(radio => radio.checked);
    const next = CHIP_ORDER[(CHIP_ORDER.indexOf(current ? current.value : '') + 1) % CHIP_ORDER.length];
    const wanted = radios.find(radio => radio.value === next);
    if (wanted) wanted.checked = true;
}

// A TOKEN THE VOCABULARY DOES NOT NAME, cloned from the <template> the panel
// renders. The name comes from the field's own data-name-prefix, which the
// server spelled through app/calendar/vocab.py, so the only thing composed here
// is prefix + token.
// IT LANDS AS "only this" rather than as "filter out", because somebody who went
// to the trouble of typing a name is naming something they want -- and it is one
// press from the other answer either way.
function addFilterToken(input) {
    const token = input.value.trim();
    if (!token) return;
    const field = input.closest('.ffield');
    // A token already drawn is the same token, so it is lit rather than doubled.
    // WHETHER CASE COUNTS IS THE SERVER'S ANSWER, carried on the field: for
    // networks 'TVN' and 'tvN' are a Polish broadcaster and a Korean one and
    // both must be addable, while for every other dimension they would be one
    // token stored twice. Guessing that here from the field's name would be
    // app/calendar/vocab.py's CASE_SENSITIVE_FIELDS restated in another
    // language.
    const fold = input.dataset.exactCase
        ? (text) => text
        : (text) => text.toLowerCase();
    const existing = [...field.querySelectorAll('.fchip')].find(chip => {
        const face = chip.querySelector('.fchip-face');
        return face && fold(face.textContent.trim()) === fold(token);
    });
    if (existing) {
        const only = existing.querySelector('input[value="include"]');
        if (only) only.checked = true;
        input.value = '';
        return;
    }
    // A colon would produce a name that reads as a different field, which the
    // server refuses outright rather than misfiling -- so it is refused here too,
    // where the person can still see what they typed.
    if (token.includes(':')) {
        toast('A filter cannot contain a colon', false);
        return;
    }
    const chip = document.getElementById('fchipTemplate').content.firstElementChild.cloneNode(true);
    chip.classList.add('is-added');
    chip.setAttribute('aria-label', token);
    chip.querySelectorAll('input[type="radio"]').forEach(radio => {
        radio.name = input.dataset.namePrefix + token;
    });
    chip.querySelector('.fchip-face').textContent = token;
    field.querySelector('.fchips').appendChild(chip);
    input.value = '';
}

// AN EMPTY CALENDAR WITH NO EXPLANATION READS AS A BROKEN APP, so the last
// service showing cannot be switched off. The server refuses it too (see
// post_me_filters), but a control that will not move says so before somebody has
// committed to a save.
// MARKED, NOT DISABLED, and that distinction is load-bearing: a disabled
// checkbox is left out of FormData entirely, so disabling the only ticked
// service would post an EMPTY group and earn exactly the refusal this is meant
// to prevent. The class is what the stylesheet greys, and the change handler
// below is what refuses the press.
function markLastService(form) {
    ['sources_show', 'sources_movie'].forEach(group => {
        const boxes = [...form.querySelectorAll(`input[name="${group}"]`)];
        const on = boxes.filter(box => box.checked);
        boxes.forEach(box => {
            box.closest('.fsource').classList.toggle('is-locked', on.length === 1 && box.checked);
        });
    });
}

function keepOneService(box) {
    const form = box.closest('form');
    const boxes = [...form.querySelectorAll(`input[name="${box.name}"]`)];
    if (!box.checked && !boxes.some(other => other.checked)) {
        box.checked = true;
        toast('At least one service has to stay on', false);
    }
    markLastService(form);
}

// CLEARS THE VISIBLE TAB ONLY, and does not save -- the same bargain every other
// field in these modals makes, so "clear" then close leaves the stored filters
// alone. Services are deliberately untouched: an unticked service is a NARROWING
// and clearing it would mean switching every service back on, which is the one
// state the save refuses and reads as the button working backwards.
function clearFilters(event) {
    const form = event.target.closest('form');
    const panel = form.querySelector('#ftab_movie').checked
        ? form.querySelector('.ftab-panel-movie')
        : form.querySelector('.ftab-panel-show');
    panel.querySelectorAll('.fchip.is-added').forEach(chip => chip.remove());
    panel.querySelectorAll('.fchip input[value=""]').forEach(radio => { radio.checked = true; });
}

async function saveFilters(event) {
    event.preventDefault();
    const form = event.target;
    // THE WHOLE FORM, AS FLAT STRINGS. Nothing here names a field, so a
    // dimension added to the panel needs no change in this file -- and the
    // payload is already the shape htmx's json encoding produces, so adopting it
    // later is deleting this function rather than rewriting the route.
    // Repeated names -- the service boxes -- are joined rather than sent as an
    // array, because an array is the one thing that encoding has no dependable
    // spelling for.
    const payload = {};
    for (const [name, value] of new FormData(form).entries()) {
        payload[name] = name in payload ? `${payload[name]},${value}` : value;
    }
    // An unchecked box sends nothing at all, so the two states of the switch
    // would otherwise be "true" and absent -- and absent means "leave it alone"
    // to the route, which would make it impossible to turn filters back on.
    payload.filters_on = form.querySelector('#f_filters_on').checked;
    // A service group with every box unticked sends nothing either, and that one
    // must reach the server as a refusal rather than as silence.
    ['sources_show', 'sources_movie'].forEach(group => {
        if (form.querySelector(`input[name="${group}"]`) && !(group in payload)) payload[group] = '';
    });
    try {
        const res = await fetch('/api/me/filters', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload)
        });
        const d = await res.json().catch(() => ({}));
        if (!res.ok || !d.ok) {
            toast(d.error || 'Could not save filters', false);
            return false;
        }
        // Filtering happens server-side while the month is assembled, so the
        // page has to be rebuilt to reflect it.
        window.location.reload();
    } catch (e) {
        console.error(e);
        toast('Could not save filters', false);
    }
    return false;
}

// One delegated listener for the whole panel. Bound at the document rather than
// per control so that nothing has to be re-bound when a chip is added, and so a
// boosted navigation cannot leave handlers pointing at a replaced <body>.
document.addEventListener('click', (event) => {
    const face = event.target.closest('.fchip-face');
    if (face) cycleChip(face.closest('.fchip'));
});

document.addEventListener('keydown', (event) => {
    if (event.key !== 'Enter' || !event.target.classList.contains('fchip-add')) return;
    // Enter in a text field inside a form submits it, and here it means "add
    // this one", not "save everything".
    event.preventDefault();
    addFilterToken(event.target);
});

// Typing a comma means the same as pressing Enter, because a list of them is
// what people paste.
document.addEventListener('input', (event) => {
    if (!event.target.classList.contains('fchip-add')) return;
    if (event.target.value.includes(',')) {
        event.target.value = event.target.value.replace(/,/g, '');
        addFilterToken(event.target);
    }
});

document.addEventListener('change', (event) => {
    if (!event.target.closest('#filtersForm')) return;
    if (event.target.name && event.target.name.startsWith('sources_')) keepOneService(event.target);
});

// The lock has to hold from the first paint, not from the first press: a viewer
// who already has one service switched off must not be able to switch off the
// other before touching anything.
document.addEventListener('DOMContentLoaded', () => {
    const form = document.getElementById('filtersForm');
    if (form) markLastService(form);
});
