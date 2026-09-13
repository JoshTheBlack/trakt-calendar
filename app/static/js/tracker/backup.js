// Reading the backup file somebody chose into the request body.
//
// THE ONE THING ONLY THE BROWSER CAN DO HERE, and the reason is the shape guard:
// app/authz.py admits `application/json` and nothing else, because a
// cross-origin multipart form post is exactly what that rule exists to refuse.
// So the file's text has to reach the server as a field of a JSON body, and only
// this side can read a local file.
//
// EVERYTHING ELSE WENT TO MARKUP. The typed acknowledgement is the form's own
// `required pattern` (and the route checks it too — a browser validation is a
// courtesy to the person typing, not a gate); the submit is the form's; the
// answer is a fragment the server draws. What is left is the read and the line
// that says what is in the file, which is worth saying BEFORE somebody types a
// confirmation phrase rather than after.
document.addEventListener('change', async (event) => {
    const input = event.target;
    if (!(input instanceof Element) || !input.dataset.fileInto) return;
    const into = document.querySelector(input.dataset.fileInto);
    const status = document.querySelector(input.dataset.fileStatus);
    const file = input.files && input.files[0];
    if (into) into.value = '';
    if (!file) { if (status) status.textContent = ''; return; }
    let text;
    let parsed;
    try {
        text = await file.text();
        parsed = JSON.parse(text);
    } catch (e) {
        // Said here, before anything is asked of the reader: being told the file
        // is unreadable beats typing a confirmation phrase and then finding out.
        if (status) status.textContent = "That file isn't readable JSON.";
        input.value = '';
        return;
    }
    if (into) into.value = text;
    // Counted across BOTH record tables, because a season lives in up to two of
    // three places and a count off one of them would understate the file: a month
    // holds what it announced and what it settled, while what is still in hand
    // belongs to no month at all.
    const months = (parsed.distrakt_months || []).length;
    const rows = (parsed.distrakt_month_records || []).length
        + (parsed.distrakt_user_seasons || []).length;
    if (status) status.textContent = `Ready to restore ${months} month(s) and ${rows} record(s).`;
});
