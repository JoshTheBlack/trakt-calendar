// The picture across the top of an exported grid: choosing one, seeing which is
// chosen, and adding one.
//
// The images themselves belong to the ACCOUNT, not to a board — they are the same
// store the account page manages — so everything here talks to /api/me/images and
// the export only ever names one.

async function loadHeaderImageChoices() {
    const select = document.getElementById('exHeader');
    const chosen = select.value;
    select.innerHTML = '<option value="">None</option><option value="avatar">My avatar</option>';
    try {
        const data = await api('/api/me/images');
        // A CONNECTED SERVICE'S PICTURE IS A THIRD KIND OF CHOICE, listed ahead
        // of the saved images because there are at most three of them and they
        // need no naming — the service IS the name. They are not saved images
        // and do not count against that quota, which is why they arrive from a
        // different key rather than being folded into the list above.
        (data.provider_avatars || []).forEach(name => select.add(
            new Option(name.charAt(0).toUpperCase() + name.slice(1), 'provider:' + name)));
        // The account's own name for each, so this is a choice between pictures
        // rather than between positions in a list.
        data.images.forEach(image => select.add(new Option(image.name, 'img:' + image.uid)));
    } catch (e) { /* the avatar and "none" are always available */ }
    select.value = chosen;
    // An image removed from the account page is gone from here too; falling back
    // to "None" beats leaving a selection that would export as nothing.
    if (select.value !== chosen) select.value = '';
    showHeaderThumb();
}

// The picture currently chosen, beside its name. The avatar route resizes the
// master on demand, so this asks for a thumbnail rather than 512px of it.
function showHeaderThumb() {
    const value = document.getElementById('exHeader').value;
    const thumb = document.getElementById('exHeaderThumb');
    if (!value) { thumb.hidden = true; thumb.removeAttribute('src'); return; }
    if (value === 'avatar') {
        thumb.src = '/api/me/avatar?size=96';
    } else if (value.startsWith('provider:')) {
        thumb.src = '/api/me/avatar/source/'
            + encodeURIComponent(value.slice(9)) + '?size=96';
    } else {
        thumb.src = '/api/me/images/' + encodeURIComponent(value.slice(4));
    }
    thumb.hidden = false;
}

// An upload goes to the account's own image store first and is then NAMED by the
// export — so the bytes are validated and normalized in exactly one place rather
// than a second time here. The name is asked for up front: an image saved with
// no name is one the picker can only call "Image 3".
async function uploadHeaderImage(input) {
    const file = input.files && input.files[0];
    if (!file) return;
    const suggested = file.name.replace(/\.[^.]+$/, '').slice(0, 40);
    const name = await ask({
        title: 'Name this image', label: 'Name', value: suggested,
        maxlength: 40, confirmText: 'Upload',
    });
    input.value = '';
    if (name === null) return;
    const reader = new FileReader();
    reader.onload = async () => {
        try {
            const data = await api('/api/me/images', 'POST',
                                   { image_b64: String(reader.result).split(',').pop(), name });
            await loadHeaderImageChoices();
            document.getElementById('exHeader').value = 'img:' + data.uid;
            onExportChange();
        } catch (e) { toast(e.message, false); }
    };
    reader.readAsDataURL(file);
}
