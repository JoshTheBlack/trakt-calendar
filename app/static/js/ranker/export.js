// Getting the finished image, and that is all this file is now.
//
// THE OPTIONS ARE A FORM THE SERVER DRAWS, and the size beside them is the
// renderer's own arithmetic asked for as the form changes (ranker.html,
// _ranker_export_readout.html). What used to be here — the column and format
// lists, the scope list, the header picker, the three-shape header value, a
// second implementation of compute_layout fed by constants the page was sent for
// no other reason, and the Markdown copy — is all gone with it.
//
// THE DOWNLOAD IS THE ONE THING LEFT, AND IT IS SCRIPT ON ITS OWN MERITS. It has
// to be a POST: export_grid spends a per-account cooldown, and SameSite=Lax
// cookies ride a top-level GET navigation, so as a GET a link on another site
// could spend it for whoever followed it. A POST whose answer is a file, saved
// under the name the response gives it, is something only a browser can finish.

function exportOptions() {
    // The form's own fields, exactly as the readout and the preview beside it
    // were built from — so what is downloaded cannot be options nobody saw.
    return Object.fromEntries(new FormData(document.getElementById('exportForm')));
}

async function downloadExport() {
    const button = document.getElementById('exportGo');
    // Disabled while rendering: every render takes one of two instance-wide
    // slots, and a burst of clicks would queue full-size renders behind it.
    button.disabled = true;
    button.textContent = 'Rendering…';
    try {
        const board = document.getElementById('rankerBoard').dataset.board;
        const res = await fetch('/api/rankings/boards/' + encodeURIComponent(board) + '/export', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(exportOptions()),
        });
        if (!res.ok) {
            const data = await res.json().catch(() => ({}));
            toast(data.error || 'Could not export.', false);
            return;
        }
        const blob = await res.blob();
        const url = URL.createObjectURL(blob);
        const link = document.createElement('a');
        link.href = url;
        link.download = filenameFrom(res.headers.get('Content-Disposition')) || 'ranking.webp';
        document.body.appendChild(link);
        link.click();
        link.remove();
        // The blob is megabytes; without this it is held until the page goes.
        URL.revokeObjectURL(url);
        const mb = blob.size / (1024 * 1024);
        toast('Exported — ' + mb.toFixed(1) + ' MB.' +
              (mb > 10 ? ' Over 10 MB: try WebP or half size for Discord.' : ''), true);
    } catch (e) {
        toast('Could not export.', false);
    } finally {
        button.textContent = 'Download';
        // THE SERVER DECIDES WHETHER IT MAY BE PRESSED AGAIN, not this: a size the
        // chosen format cannot hold must stay refused rather than being re-armed
        // by the act of having tried it. Asking the readout for itself is what
        // draws the button back in whichever state this board's options leave it.
        htmx.trigger('#exportForm', 'export-changed');
    }
}

// Both Download buttons — the one the dialog opens with and the one every readout
// redraws out of band — carry `data-export-download` rather than an onclick
// attribute, because the app's Content-Security-Policy runs no script written in
// markup (app/csp.py). A disabled button dispatches no click, so the server's
// refusal still holds.
document.addEventListener('click', (e) => {
    if (e.target instanceof Element && e.target.closest('[data-export-download]')) downloadExport();
});

function filenameFrom(disposition) {
    const match = /filename="([^"]+)"/.exec(disposition || '');
    return match ? match[1] : null;
}
