// ---- Adding a title to Sonarr / Radarr / Seerr ----
//
// WHAT A BUTTON SHOWS IS THE SERVER'S. Whether its service can be reached and
// whether the title is already in that library are drawn with the button — on a
// card, on a day that arrives late, and in the details panel's header — from the
// two caches app/integrations/routes.py keeps (its LibraryView). This file used
// to hold a copy of both, poll for them every minute, and mark every button after
// the page had painted, including the buttons on cards the server had just
// rendered. What is left is the press, which only the page can make.

// One add, from what the button carries. On success every button on the page for
// the same title in the same library is marked: the card's and the panel's are
// separate buttons, and one title can air on several days of a month. They are
// found by `data-library-id`, the id that service matches on, which the server
// wrote so this file never has to know which id each service uses.
async function addToArr(el) {
    if (el.disabled || el.classList.contains('busy')) return;
    const original = el.innerHTML;
    el.disabled = true;
    el.classList.add('busy');
    el.innerHTML = '⏳';
    let answer;
    try {
        const res = await fetch('/api/integrations/add', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                target: el.dataset.arr, media: el.dataset.media,
                tvdb: el.dataset.tvdb || null, tmdb: el.dataset.tmdb || null,
                title: el.dataset.title || '',
            }),
        });
        answer = await res.json();
    } catch (e) {
        answer = { ok: false, error: 'Request failed' };
    }
    el.innerHTML = original;
    el.classList.remove('busy');
    el.disabled = false;
    if (!answer.ok) {
        toast(answer.error || 'Could not add', false);
        return;
    }
    if (el.dataset.libraryId) markAdded(el.dataset.arr, [el.dataset.libraryId], answer.message);
    else markAdded(null, [], answer.message, [el]);
    toast(answer.message || 'Added', true);
}

// Every button on the page for the given titles in one library, marked as held.
function markAdded(kind, libraryIds, message, also = []) {
    const buttons = [...also];
    libraryIds.forEach(id => buttons.push(...document.querySelectorAll(
        `.arr-btn[data-arr="${CSS.escape(kind)}"][data-library-id="${CSS.escape(String(id))}"]`)));
    buttons.forEach(btn => {
        btn.classList.add('in-library');
        btn.classList.remove('unreachable');
        btn.title = message || 'Added';
    });
}

// Add every WATCHING title in this MONTH to Sonarr or Radarr. The month is the
// server's to read (calendar/routes.py's add_month_to_library): days fetch
// themselves as they scroll in, so the cards on screen are not the month, and
// only the server applies this viewer's filters to the whole of it. The page's
// own query string names which month, so both read it by the same rules. Asked
// twice — how many, for the confirmation, then do it — and what was added is
// marked on whatever buttons for those titles the page has drawn.
async function addAllToArr(event) {
    const trigger = event.currentTarget;
    const post = (body) => fetch('/calendar/library/add-month' + window.location.search, {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
    }).then(res => res.json()).catch(() => ({ ok: false, error: 'Request failed' }));
    const plan = await post({ dry_run: true });
    if (!plan.ok || !plan.count) {
        toast(plan.message || plan.error || 'Could not read this month', false);
        return;
    }
    confirmInline(trigger, plan.message, async () => {
        toast(`Adding ${plan.count}…`, true);
        const done = await post({});
        markAdded(done.service, done.added || [], 'Added');
        toast(done.message || done.error || 'Could not add', !!done.ok);
    });
}
