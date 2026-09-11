/* Flipping one field of a card between the services that described it.
 *
 * A CARD CAN ONLY DO THIS BECAUSE IT ALREADY HAS BOTH ANSWERS. The server writes
 * `data-alternatives` on any card whose services filled a field in differently —
 * {field: {service: value}}, including the value currently on screen — so a flip
 * is markup already in the page and never a request. That is what lets it be
 * offered per card at all: a round trip per click would be a call per curious
 * glance, on a page that can hold a thousand cards.
 *
 * IT IS A VIEW FLIP AND IT WRITES NOTHING BACK, which is the rule this file
 * exists to keep. Whose value wins is stated once, for every card, on the
 * Sources screen; a click here that quietly rewrote that preference would change
 * every other card on the page as a side effect of looking at one of them.
 * Nothing below sends anything anywhere.
 *
 * No listener registration, deliberately: the buttons carry their own onclick, so
 * a day block that fetches itself in long after the page loaded works the moment
 * it lands, with nothing to re-arm. The flip is a button inside the card, so the
 * card's own click — opening the details panel — leaves it alone (static/js/ui.js).
 */

// The elements a flip touches for one field: the one showing the value, and the
// button's logos. Kept together because the two must never disagree about which
// service is on screen — that is the whole content of the control.
function swapCardField(button) {
    const card = button.closest('.card');
    const field = button.dataset.field;
    if (!card || !field) return;
    let values;
    try {
        values = JSON.parse(card.dataset.alternatives || '{}')[field];
    } catch (e) {
        // A card whose provenance will not parse simply has no flip. Nothing
        // else on it depends on this attribute, so failing quietly here leaves
        // an ordinary card rather than a broken one.
        return;
    }
    const names = values ? Object.keys(values) : [];
    // The target is the element SHOWING the value: it carries data-source and the
    // button does not, which is what tells the two apart when both name the field.
    const target = card.querySelector('[data-field="' + field + '"][data-source]');
    if (!target || names.length < 2) return;

    const next = names[(names.indexOf(target.dataset.source) + 1) % names.length];
    const value = values[next];
    if (target.tagName === 'IMG') {
        target.src = value;
    } else {
        // textContent, never innerHTML: this is a title or an overview as some
        // service wrote it, and it is not markup.
        target.textContent = value;
    }
    target.dataset.source = next;
    followInDetailsAddress(card, field, value);

    let label = next;
    button.querySelectorAll('.swap-logo').forEach((logo) => {
        logo.hidden = logo.dataset.source !== next;
        if (logo.dataset.source === next) label = logo.dataset.label || next;
    });
    button.title = 'From ' + label + ' — click to see the other';
}

// THE PANEL OPENS ON WHAT IS ON SCREEN. The card's details address was drawn with
// the name, the picture and — where the services disagree about it — the
// description the server picked (_card.html), and those are the things on it a
// flip can change afterwards. So a flip rewrites that value on the address, and a
// flip of the name the heading shown while the panel loads too. Left alone, the
// panel would open on the picture or the words just replaced.
function followInDetailsAddress(card, field, value) {
    if (!['title', 'poster', 'overview'].includes(field)) return;
    const address = new URL(card.dataset.detailsOpen, window.location.href);
    address.searchParams.set(field, value);
    card.dataset.detailsOpen = address.pathname + address.search;
    if (field === 'title') card.dataset.detailsHeading = value;
}
