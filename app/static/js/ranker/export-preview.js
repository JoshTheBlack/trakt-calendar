// ---------------------------------------------------------------------------
// the preview, and looking closely at it
// ---------------------------------------------------------------------------
// The preview is the whole grid shrunk into a column beside the options, which
// is the right shape for judging the layout and useless for judging the header —
// the one part of the image made of somebody's own name and picture. Clicking
// the preview shows it at the size it was actually rendered and lets the pointer
// push it around, so the top of the image can be inspected without exporting a
// file to open.
//
// The pointer's position across the frame maps to the same position across the
// image, which is why no dragging is involved: moving to the top-left corner
// shows the top-left of the grid. Fractions are 0..1 by construction, so the
// image can never be pushed off its own edges.

let previewUrl = null;          // the object URL currently in the preview <img>
let previewTimer = null;


// Twice the size the preview was rendered at. The preview is a small render, so
// this magnifies what is there rather than revealing more of it — but the header
// band is a few dozen pixels tall in that render, and at 1:1 inside a narrow
// column it is still too small to tell one picture from another.
const PREVIEW_ZOOM = 2;

function zoomableFrame() { return document.getElementById('exPreview'); }

function togglePreviewZoom() {
    const frame = zoomableFrame();
    const img = frame.querySelector('img');
    if (!img) return;
    const zoomed = frame.classList.toggle('is-zoomed');
    if (zoomed) {
        img.style.width = (img.naturalWidth * PREVIEW_ZOOM) + 'px';
        // Straight to the top, which is what somebody zooming in is nearly
        // always looking for; the pointer takes it anywhere else.
        img.style.transform = 'translate(0px, 0px)';
    } else {
        img.style.width = '';
        img.style.transform = '';
    }
}

// The outer margin of the frame that maps to the ends of the image. Without it
// the last pixel of the image needs the last pixel of the frame, which means
// the edges — the header along the top especially — can only be reached by
// putting the pointer somewhere it is about to leave. Giving the extremes a
// band this wide makes them somewhere you can comfortably sit.
const PREVIEW_PAN_MARGIN = 0.15;

function panFraction(offset, extent) {
    const raw = offset / extent;
    const scaled = (raw - PREVIEW_PAN_MARGIN) / (1 - 2 * PREVIEW_PAN_MARGIN);
    return Math.min(1, Math.max(0, scaled));
}

function panPreview(event) {
    const frame = zoomableFrame();
    if (!frame.classList.contains('is-zoomed')) return;
    const img = frame.querySelector('img');
    if (!img) return;
    const rect = frame.getBoundingClientRect();
    const fx = panFraction(event.clientX - rect.left, rect.width);
    const fy = panFraction(event.clientY - rect.top, rect.height);
    const overflowX = Math.max(0, img.offsetWidth - rect.width);
    const overflowY = Math.max(0, img.offsetHeight - rect.height);
    img.style.transform = 'translate(' + (-overflowX * fx) + 'px, ' + (-overflowY * fy) + 'px)';
}

async function refreshPreview() {
    const frame = document.getElementById('exPreview');
    // A new render is a new image; staying zoomed into where the old one's
    // header was would show whatever now happens to be at those coordinates.
    frame.classList.remove('is-zoomed');
    try {
        const res = await fetch('/api/rankings/boards/' + encodeURIComponent(boardUid()) + '/preview', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(exportOptions()),
        });
        if (!res.ok) {
            const data = await res.json().catch(() => ({}));
            frame.innerHTML = '';
            frame.appendChild(Object.assign(document.createElement('span'),
                { className: 'ranker-preview-hint', textContent: data.error || 'No preview.' }));
            return;
        }
        const blob = await res.blob();
        // Without the revoke the previous preview's bytes stay held for the life
        // of the page, and this route is called on every option change.
        if (previewUrl) URL.revokeObjectURL(previewUrl);
        previewUrl = URL.createObjectURL(blob);
        frame.innerHTML = '';
        const img = document.createElement('img');
        img.alt = 'Preview of the poster grid';
        img.src = previewUrl;
        frame.appendChild(img);
    } catch (e) {
        frame.innerHTML = '';
        frame.appendChild(Object.assign(document.createElement('span'),
            { className: 'ranker-preview-hint', textContent: 'No preview.' }));
    }
}
