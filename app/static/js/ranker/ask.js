// ---------------------------------------------------------------------------
// asking the user something
// ---------------------------------------------------------------------------
// Nothing here calls prompt(), confirm() or alert(). They block the page, they
// cannot be made to look like the rest of it, and a browser configured to
// suppress them turns "name your board" into a dead button. One in-page dialog
// serves both shapes and resolves a promise, so the callers still read as
// straight-line code.

let askResolve = null;

// Resolves to the typed string (asking for text), to true (asking to confirm),
// or to null when it was dismissed — so `=== null` is always "cancelled" and an
// empty string stays a real answer.
function ask(options) {
    const modal = document.getElementById('askModal');
    const field = document.getElementById('askField');
    const input = document.getElementById('askInput');
    const message = document.getElementById('askMessage');
    document.getElementById('askTitle').textContent = options.title || '';
    document.getElementById('askOk').textContent = options.confirmText || 'OK';
    message.textContent = options.message || '';
    message.hidden = !options.message;
    const wantsText = options.input !== false;
    field.hidden = !wantsText;
    if (wantsText) {
        document.getElementById('askLabel').textContent = options.label || 'Name';
        input.maxLength = options.maxlength || 60;
        input.value = options.value || '';
    }
    modal.classList.add('open');
    if (wantsText) { input.focus(); input.select(); }
    return new Promise(resolve => { askResolve = resolve; });
}

function closeAsk(answer) {
    document.getElementById('askModal').classList.remove('open');
    const resolve = askResolve;
    askResolve = null;
    if (resolve) resolve(answer);
}

function submitAsk() {
    const wantsText = !document.getElementById('askField').hidden;
    closeAsk(wantsText ? document.getElementById('askInput').value.trim() : true);
}

// The keys the dialog it replaces answered to, so muscle memory still works.
document.addEventListener('keydown', (event) => {
    if (!askResolve) return;
    if (event.key === 'Enter' && event.target.id === 'askInput') { event.preventDefault(); submitAsk(); }
    else if (event.key === 'Enter' && document.getElementById('askField').hidden) submitAsk();
    else if (event.key === 'Escape') closeAsk(null);
});
