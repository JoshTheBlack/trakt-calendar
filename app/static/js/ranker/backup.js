// ---------------------------------------------------------------------------
// backup and restore
// ---------------------------------------------------------------------------
// Downloading is a plain link — the response is already a file with a filename
// on it. Restoring is the destructive half: it replaces every board rather than
// merging, so the file is parsed here (a broken one is caught before anything is
// sent) and the button stays disabled until the acknowledgement is typed out.

const RESTORE_ACK = 'REPLACE MY BOARDS';
let restorePayload = null;

function openBackupModal() {
    restorePayload = null;
    document.getElementById('backupFile').value = '';
    document.getElementById('backupAck').value = '';
    document.getElementById('backupConfirm').hidden = true;
    document.getElementById('backupGo').disabled = true;
    document.getElementById('backupStatus').textContent = '';
    document.getElementById('backupModal').classList.add('open');
}

function closeBackupModal() {
    document.getElementById('backupModal').classList.remove('open');
}

async function onBackupFileChosen(input) {
    const status = document.getElementById('backupStatus');
    const file = input.files && input.files[0];
    restorePayload = null;
    document.getElementById('backupConfirm').hidden = true;
    document.getElementById('backupGo').disabled = true;
    if (!file) { status.textContent = ''; return; }
    try {
        restorePayload = JSON.parse(await file.text());
    } catch (e) {
        status.textContent = 'That file is not readable as a backup.';
        return;
    }
    const boards = (restorePayload && restorePayload.boards) || [];
    status.textContent = boards.length + ' board(s) in this file.';
    document.getElementById('backupConfirm').hidden = false;
    onBackupAckInput();
}

function onBackupAckInput() {
    document.getElementById('backupGo').disabled =
        !restorePayload || document.getElementById('backupAck').value.trim() !== RESTORE_ACK;
}

async function restoreBackup() {
    const button = document.getElementById('backupGo');
    button.disabled = true;
    document.getElementById('backupStatus').textContent = 'Restoring…';
    try {
        const data = await api('/api/rankings/restore', 'POST', restorePayload);
        // A full reload rather than a redraw: every board on the page, its
        // version and its tiers have just been replaced wholesale, and the
        // server's idea of them is the only correct one now.
        toast('Restored ' + data.boards + ' board(s).', true);
        window.location.href = '/rankings';
    } catch (e) {
        document.getElementById('backupStatus').textContent = e.message || 'Could not restore.';
        onBackupAckInput();
    }
}
