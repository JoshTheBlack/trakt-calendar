// What a row's own buttons do: forget a show, forget a film, abandon or
// un-abandon a season, acknowledge a season that came back, and answer the
// questions the page raises above the list.
//
// The destructive-ish ones confirm first; each ends by handing the recomputed
// month back to applyMonthResponse. Acknowledging is the odd one out — it
// destroys nothing, so it neither confirms nor redraws.

// Delete a show from the tracker entirely (cleanup mistakes, incl. abandoned ones).
// A row the calendar put here is also marked not-watching there — otherwise a
// preview month hands it straight back — so the confirm says so BEFORE the click
// for those rows, and stays quiet about the calendar for a row this page owns.
// The server has the last word (`hidden_on_calendar`), since a row predating the
// provenance column only finds out by asking the calendar.
async function deleteShow(key, season, event, addedBy) {
    // A closed month never touches the calendar, whatever the row says — see
    // api_distrakt_remove. Only an open month can, and only for a calendar row.
    const hides = !monthClosed && (addedBy === 'calendar' || !addedBy);
    confirmInline(event.currentTarget,
        hides
            ? 'Remove this show and mark it not-watching on your calendar? This cannot be undone.'
            : (monthClosed
                ? 'Take this off what this month records? Your calendar is not touched. This cannot be undone.'
                : 'Remove this show from the tracker for this month? This cannot be undone.'),
        async () => {
            try {
                const res = await fetch('/api/distrakt/remove', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ year: window.DISTRAKT_YEAR, month: window.DISTRAKT_MONTH, key, season })
                });
                const d = await res.json();
                if (!d.ok) throw new Error(d.error || 'failed');
                toast(d.hidden_on_calendar
                    ? 'Removed — and hidden on your calendar'
                    : (monthClosed ? 'Taken off this month' : 'Removed from tracker'), true);
                applyMonthResponse(d);  // mutation returns the recomputed month (1d)
            } catch (e) {
                toast('Could not remove show', false);
            }
        }, { danger: true });
}

// Forgets the watch itself: a film is held once, with its latest play, so there
// is no month-by-month share of it to remove.
//
// The title comes off the button rather than being interpolated into the onclick
// attribute: a film called "Good Luck, Have Fun, Don't Die" carries both quote
// characters, and either one ends the attribute early and kills the handler.
async function deleteFilm(key, button) {
    const title = (button && button.dataset.title) || '';
    confirmInline(button,
        `Forget watching ${title || 'this film'}? It comes off every month and out of Rankings imports.`,
        async () => {
            try {
                const res = await fetch('/api/distrakt/remove-movie', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        key,
                        year: window.DISTRAKT_YEAR, month: window.DISTRAKT_MONTH,
                    }),
                });
                const d = await res.json();
                if (!d.ok) throw new Error(d.error || 'failed');
                toast('Film removed', true);
                applyMonthResponse(d);
            } catch (e) {
                toast(e.message || 'Could not remove that film', false);
            }
        }, { danger: true });
}

// "I've seen that this one came back." No confirm — nothing is lost by pressing
// it, and a confirm on a thing whose whole job is to be read once would be more
// in the way than the mark is.
//
// The mark is removed from the page rather than the month redrawn: the server
// answers with the acknowledgement alone (see api_distrakt_acknowledge_return),
// because recomputing a month to drop one word would cost a season lookup per
// listed title. Left in place if the request fails, so it is still there to press
// again rather than silently gone while the flag is still set.
async function acknowledgeReturn(key, season, button) {
    try {
        const res = await fetch('/api/distrakt/acknowledge-return', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ key, season })
        });
        const d = await res.json();
        if (!d.ok) throw new Error(d.error || 'failed');
        button.remove();
    } catch (e) {
        toast(e.message || 'Could not dismiss that mark', false);
    }
}

// "Yes, add that." The expensive half of the untracked-episode question: the row
// itself cost nothing to draw, and this is where the season finally gets looked
// up. The month comes back recomputed because a new row has appeared in one of
// the buckets and the page has to redraw to show it.
//
// No confirm — it is an add, and the ✕ undoes it.
async function addUnknownSeason(key, season, button) {
    const row = button.closest('.distrakt-unknown-row');
    let ids = {};
    try { ids = JSON.parse((row && row.dataset.ids) || '{}'); } catch (e) { ids = {}; }
    try {
        const res = await fetch('/api/distrakt/unknown-add', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                key, season, ids, title: (row && row.dataset.title) || '',
                year: window.DISTRAKT_YEAR, month: window.DISTRAKT_MONTH,
            }),
        });
        const d = await res.json();
        if (!d.ok) throw new Error(d.error || 'failed');
        toast('Added', true);
        applyMonthResponse(d);  // a new row exists now, so the month is redrawn
    } catch (e) {
        toast(e.message || 'Could not add that season', false);
    }
}

// "Yes, put that back." Cheaper than adding something new and not the same act:
// the record already exists with the counts it was given up on, so nothing is
// looked up — it just moves off the month that recorded the verdict and back onto
// the list, and the calendar turn-away that came with it is undone at the same
// time. The month is redrawn because a row has moved between sections.
async function resumeGivenUpSeason(key, season, button) {
    try {
        const res = await fetch('/api/distrakt/unknown-resume', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                key, season,
                year: window.DISTRAKT_YEAR, month: window.DISTRAKT_MONTH,
            }),
        });
        const d = await res.json();
        if (!d.ok) throw new Error(d.error || 'failed');
        toast('Added back', true);
        applyMonthResponse(d);
    } catch (e) {
        toast(e.message || 'Could not add that back', false);
    }
}

// "Yes, work it out again." The month's verdict that this season was finished is
// withdrawn and the season goes back onto the list, derived from what the services
// say now — the same move a season that turned out to have grown makes, because it
// is the same thing happening: a completed record that has stopped being true.
// It confirms first, unlike the other two ✓ buttons: those ADD something and the
// ✕ undoes them, while this one takes a verdict apart, and the month's record of
// having finished that season does not survive it.
async function readdSettledSeason(key, season, button) {
    confirmInline(button,
        'Work this season out again from what your accounts say now? This month stops recording that you finished it.',
        async () => {
            try {
                const res = await fetch('/api/distrakt/verdict-readd', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        key, season,
                        year: window.DISTRAKT_YEAR, month: window.DISTRAKT_MONTH,
                    }),
                });
                const d = await res.json();
                if (!d.ok) throw new Error(d.error || 'failed');
                toast('Worked out again', true);
                applyMonthResponse(d);
            } catch (e) {
                toast(e.message || 'Could not re-add that season', false);
            }
        }, { danger: true });
}

// "No, and stop asking." The row is derived from viewing every time viewing is
// read, so the refusal is recorded server-side or it comes straight back on the
// next load. Nothing else on the page changes, so the row is taken out in place
// rather than the whole month redrawn — and it is left standing if the request
// failed, so it can be pressed again instead of vanishing while nothing was
// written down.
async function dismissUnknownSeason(key, season, button) {
    const row = button.closest('.distrakt-unknown-row');
    try {
        const res = await fetch('/api/distrakt/unknown-dismiss', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ key, season }),
        });
        const d = await res.json();
        if (!d.ok) throw new Error(d.error || 'failed');
        if (row) row.remove();
    } catch (e) {
        toast(e.message || 'Could not dismiss that', false);
    }
}

async function toggleAbandon(key, season, abandoned) {
    try {
        const res = await fetch('/api/distrakt/abandon', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ year: window.DISTRAKT_YEAR, month: window.DISTRAKT_MONTH, key, season, abandoned })
        });
        const d = await res.json();
        if (!d.ok) throw new Error(d.error || 'failed');
        toast(abandoned ? 'Marked abandoned' : 'Un-abandoned', true);
        applyMonthResponse(d);  // mutation returns the recomputed month (1d)
    } catch (e) {
        toast('Could not update abandon status', false);
    }
}
