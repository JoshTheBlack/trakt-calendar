// The rankings page's shared ground: what the server said this board is, the one
// way this page still talks to the API by hand, and the helpers the other files
// need.
//
// THREE RULES EVERY FILE OF THIS PAGE IS BUILT AROUND. They are here because
// this is the file all of them load first, and because each one is a rule a
// change in any of them can break.
//
// 1. EVERY MUTATION ARRIVES AS application/json. The app-wide request-shape
//    middleware refuses any POST/PUT/PATCH/DELETE that is not exactly
//    application/json, which is deliberate CSRF defence. htmx's hx-boost submits
//    a form with its native urlencoded encoding, so a boosted POST would be
//    refused with 415 — boost is for GET navigation only. What that used to mean
//    was that this page carried no <form> at all and every write went through
//    api() below; it now means that every form here carries `hx-json-enc`, which
//    encodes the same body from the same fields. The two surviving hand-written
//    writes are the layout save and the export download, and neither is a form's
//    own fields.
//
// 2. DRAG BINDING IS IDEMPOTENT AND RE-RUNS AFTER EVERY SWAP. htmx replaces the
//    containers Sortable is bound to — the whole board on a boosted switch, a
//    tier's body when it is first opened. Binding a container twice produces two
//    drop handlers and therefore two saves of the same drag, which is a data bug
//    rather than a visual one. initSortable() may be called on any subtree, any
//    number of times.
//
// 3. THE LAYOUT IS READ AT SAVE TIME, NOT AT DRAG TIME. A save in flight while a
//    fragment swaps must not lose a drag, so nothing is snapshotted early; the
//    board's `version` arbitrates, and a 409 reloads rather than guessing.

// ---------------------------------------------------------------------------
// state
// ---------------------------------------------------------------------------
// `board` mirrors what the server rendered: every tier with its settings and its
// item keys, plus the pool. It exists because a CLOSED tier draws no rows, so
// the DOM alone cannot describe the board — and save_layout refuses a payload
// that omits a tier and would overwrite a label the payload got wrong.
//
// IT IS ALL THAT IS LEFT OF THE PAGE'S DATA. The export limits, the tier
// template, the import years, the pool's page size and the account's own name
// were all sent here too, and every one of them was sent so that the browser
// could build something out of it. The server draws those now.

let state = { board: null };

function readPageData() {
    const node = document.getElementById('rankerData');
    if (!node) { state.board = null; return; }
    try {
        state.board = JSON.parse(node.textContent).board;
    } catch (e) {
        toast('This board could not be read — reload the page.', false);
    }
}

function boardUid() { return state.board && state.board.uid; }

// Every write the data layer accepts bumps the board's version, and the next
// save has to echo the current one. Called ONLY where the server actually
// changed something — adding a title the board already holds is accepted and
// changes nothing, and guessing a bump there would make the next save a 409.
// Every OTHER write on this page answers with the page redrawn, so its new
// version arrives as markup and nothing has to be guessed at all.
function bumpVersion() { state.board.version += 1; }

function tierByUid(uid) { return (state.board.categories || []).find(c => c.uid === uid) || null; }

// ---------------------------------------------------------------------------
// talking to the API
// ---------------------------------------------------------------------------
// TWO CALLERS LEFT, and each is here on its own merits rather than because a
// form could not have been used: the layout save (a nested ordered tree with a
// version token, fired 800ms after a drag) and the tier delete whose undo
// re-saves that tree.

async function api(url, method, body) {
    const opts = { method: method || 'GET' };
    if (method && method !== 'GET') {
        // Including DELETE: the middleware wants a JSON content type on every
        // mutating request, so one that has nothing to say sends {}.
        opts.headers = { 'Content-Type': 'application/json' };
        opts.body = JSON.stringify(body || {});
    }
    const res = await fetch(url, opts);
    let data = {};
    try { data = await res.json(); } catch (e) { data = {}; }
    if (!res.ok || data.ok === false) {
        const err = new Error(data.error || ('Request failed (' + res.status + ')'));
        err.status = res.status;
        err.data = data;
        throw err;
    }
    return data;
}
