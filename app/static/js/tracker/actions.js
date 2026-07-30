// What a row's own buttons do: forget a show, forget a film, abandon or
// un-abandon a season.
//
// All three are destructive-ish and all three confirm first; each ends by
// handing the recomputed month back to applyMonthResponse.

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
