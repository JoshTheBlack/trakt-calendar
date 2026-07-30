// ---------------------------------------------------------------------------
// boards
// ---------------------------------------------------------------------------

async function newBoard() {
    const name = await ask({ title: 'New board', label: 'Name', maxlength: 60 });
    if (name === null) return;
    try {
        const data = await api('/api/rankings/boards', 'POST',
                               { uid: newUid('board'), name: name.slice(0, 60) });
        window.location.href = '/rankings?board=' + encodeURIComponent(data.board.uid);
    } catch (e) { toast(e.message, false); }
}

async function renameBoard() {
    const name = await ask({ title: 'Rename board', label: 'Name', maxlength: 60,
                             value: state.board.name || '', confirmText: 'Rename' });
    if (name === null) return;
    try {
        await api('/api/rankings/boards/' + encodeURIComponent(boardUid()), 'PATCH',
                  { name: name.slice(0, 60) });
        window.location.reload();
    } catch (e) { toast(e.message, false); }
}

async function cloneBoard() {
    try {
        const data = await api('/api/rankings/boards', 'POST',
                               { clone_of: boardUid(), uid: newUid('board') });
        window.location.href = '/rankings?board=' + encodeURIComponent(data.board.uid);
    } catch (e) { toast(e.message, false); }
}

function deleteBoard(event) {
    confirmInline(event.currentTarget,
        'Delete this board and everything on it? This cannot be undone.', async () => {
        try {
            await api('/api/rankings/boards/' + encodeURIComponent(boardUid()), 'DELETE', {});
        } catch (e) { toast(e.message, false); return; }
        window.location.href = '/rankings';
    }, { danger: true });
}
