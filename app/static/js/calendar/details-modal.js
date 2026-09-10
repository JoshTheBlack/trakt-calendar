// ---- Details modal ----
//
// WHAT IS LEFT HERE AFTER THE PANEL BECAME A TEMPLATE. Its contents are a
// fragment the server renders (templates/_details.html, opened through ui.js's
// openDetailsPanel), and the query the card asks with is markup the server built
// — see `details_url` in app/templating.py. So this file is down to the two
// things the browser is genuinely the only side that can do: deciding that a
// click on a card was not a click on one of its controls, and the actions bar,
// which is built out of state that lives nowhere but this page.

function openDetails(card, event) {
    if (event) {
        const interactive = event.target.closest('.watch-toggle, .trakt-btn, a, button');
        if (interactive) return;
    }
    const title = card.dataset.title || card.querySelector('.show-title')?.textContent || 'Details';
    buildDetailsActions(card, card.dataset.media, title);
    // THE NAME AND THE PICTURE TRAVEL FROM THE LIVE CARD, and they are the only
    // two values this side adds to an address the server built. Everything else
    // about the request was decided when the card was rendered; these two were
    // not, because the source-flip control (calendar/source-swap.js) can change
    // either of them after that, from values already in the page. Baking them in
    // would open the panel on the picture you had just replaced.
    //
    // They are the card's rather than the answer's for the same reason the
    // heading is: which service answers decides what is in the payload, and one
    // of them stores its catalogue records without a title and with artwork this
    // viewer's own field order did not pick.
    //
    // A card carrying no id any registered source recognises still opens: the
    // server answers with the panel's own "nothing here can describe this item"
    // rather than this side deciding it in advance, because which ids count is
    // the registry's answer and not the browser's.
    const url = `${card.dataset.detailsUrl}&title=${encodeURIComponent(title)}`
        + `&poster=${encodeURIComponent(card.dataset.poster || '')}`;
    openDetailsPanel(url, title);
}

// Add-to-library buttons in the details modal's top bar (arr + Seerr, if configured).
//
// THE ONE PART OF THIS PANEL THAT IS STILL BUILT HERE, and it is client state:
// which of the three services is reachable and what is already in the library are
// answers this page holds and the fragment route has never been told. Built with
// createElement/textContent rather than from strings, which is why it was never
// part of the drift the template fixed.
function buildDetailsActions(card, media, title) {
    const actions = document.getElementById('detailsActions');
    actions.innerHTML = '';
    if (!window.IS_ADMIN) return;
    const labels = {
        sonarr: 'Add to Sonarr', radarr: 'Add to Radarr', seer: 'Request on Seerr'
    };
    const targets = [media === 'movie' ? 'radarr' : 'sonarr', 'seer'];
    targets.forEach(kind => {
        const st = arrStatus[kind] || {};
        if (!st.configured) return;
        const btn = document.createElement('button');
        btn.type = 'button';
        btn.className = 'arr-btn ' + kind;
        btn.dataset.arr = kind;
        btn.dataset.media = media;
        btn.dataset.tvdb = card.dataset.tvdb || '';
        btn.dataset.tmdb = card.dataset.tmdb || '';
        btn.dataset.title = title;
        btn.innerHTML = `<img src="/static/icons/${kind}.png" alt=""> ${labels[kind]}`;
        btn.disabled = !st.reachable;
        if (!st.reachable) { btn.classList.add('unreachable'); btn.title = kind.charAt(0).toUpperCase() + kind.slice(1) + ' is unreachable'; }
        const id = libIdFor(kind, btn.dataset);
        if (id && libraryIds[kind] && libraryIds[kind].has(String(id))) {
            markInLibrary(btn, 'Already in ' + kind.charAt(0).toUpperCase() + kind.slice(1));
        }
        btn.addEventListener('click', (e) => addToArr(btn, e));
        actions.appendChild(btn);
    });
}
