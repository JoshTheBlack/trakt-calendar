// ---- Backup: download an export, restore one back ----
// The export is a plain authenticated GET, so the download attribute on the link
// does the whole job — no fetch, no blob juggling, and the browser gets the
// filename from the response.
//
// Restore is the destructive direction: it REPLACES this account's tracked data
// rather than merging into it. Hence the typed acknowledgement — a phrase that
// has to be read and copied cannot be cleared by muscle memory the way a
// confirm() can.
const TRACKER_RESTORE_ACK = 'REPLACE MY DATA';
let trackerRestorePayload = null;

function setRestoreStatus(message, ok) {
    const el = document.getElementById('restoreStatus');
    el.textContent = message || '';
    el.className = 'distrakt-note' + (message ? (ok ? ' ok' : ' distrakt-warn') : '');
}

function resetRestore() {
    trackerRestorePayload = null;
    document.getElementById('restoreConfirm').hidden = true;
    document.getElementById('restoreAck').value = '';
    document.getElementById('restoreBtn').disabled = true;
}

async function onRestoreFileChosen() {
    const input = document.getElementById('restoreFile');
    const file = input.files && input.files[0];
    resetRestore();
    if (!file) { setRestoreStatus('', true); return; }
    // Parsed here, before anything is asked of the user: being told the file is
    // unreadable is better than typing a confirmation phrase and then finding out.
    try {
        trackerRestorePayload = JSON.parse(await file.text());
    } catch (e) {
        setRestoreStatus("That file isn't readable JSON.", false);
        input.value = '';
        return;
    }
    const months = (trackerRestorePayload.distrakt_months || []).length;
    const shows = (trackerRestorePayload.distrakt_shows || []).length;
    setRestoreStatus(`Ready to restore ${months} month(s) and ${shows} show row(s).`, true);
    document.getElementById('restoreConfirm').hidden = false;
}

function onRestoreAckInput() {
    const typed = document.getElementById('restoreAck').value.trim();
    document.getElementById('restoreBtn').disabled = !trackerRestorePayload || typed !== TRACKER_RESTORE_ACK;
}

async function restoreTrackerBackup() {
    if (!trackerRestorePayload) return;
    if (document.getElementById('restoreAck').value.trim() !== TRACKER_RESTORE_ACK) return;
    const btn = document.getElementById('restoreBtn');
    btn.disabled = true;
    setRestoreStatus('Restoring…', true);
    try {
        const res = await fetch('/api/distrakt/restore', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(trackerRestorePayload),
        });
        const d = await res.json();
        if (!d.ok) throw new Error(d.error || 'failed');
        setRestoreStatus(`Restored ${(d.months || []).length} month(s).`, true);
        document.getElementById('restoreFile').value = '';
        resetRestore();
        loadMonthData();
        toast('Backup restored', true);
    } catch (e) {
        setRestoreStatus(e.message || 'Could not restore that file.', false);
        toast('Could not restore that file', false);
        onRestoreAckInput();
    }
}
