// ---- Certification chip pickers (Content floor settings + Filters modal) ----
// The two certification dimensions draw from a small, frozen external
// vocabulary (US TV Parental Guidelines and the MPA film ratings), so unlike
// the free-text genre/country fields they get click-to-pick chips. Each chip
// cycles through three states, and the set serializes to the same comma-joined
// "-token" spec app/calendar/filter.py's parse_spec already reads on the server — so the
// backend never has to know a picker produced the string. The same component
// backs both the instance-floor copy and the per-user Filters copy; only the
// vocabulary (declared per-picker in data-vocab) differs.
const CERT_CHIP_STATES = ['', 'include', 'exclude'];

function setChipState(chip, state) {
    chip.dataset.state = state;
    chip.classList.toggle('on-include', state === 'include');
    chip.classList.toggle('on-exclude', state === 'exclude');
}

// Build a picker's chips once from its data-vocab. Idempotent so opening a modal
// repeatedly never re-adds chips; the modal open just resets their states.
function buildCertPicker(picker) {
    if (picker.dataset.built) return;
    (picker.dataset.vocab || '').split(',').map(t => t.trim()).filter(Boolean).forEach(token => {
        const chip = document.createElement('button');
        chip.type = 'button';
        chip.className = 'cert-chip';
        chip.dataset.token = token;
        chip.textContent = token;
        setChipState(chip, '');
        chip.addEventListener('click', () => {
            const next = (CERT_CHIP_STATES.indexOf(chip.dataset.state) + 1) % CERT_CHIP_STATES.length;
            setChipState(chip, CERT_CHIP_STATES[next]);
        });
        picker.appendChild(chip);
    });
    picker.dataset.built = '1';
}

// Serialize chip states into a `TV-14, -TV-MA` spec string.
function readCertPicker(picker) {
    const parts = [];
    picker.querySelectorAll('.cert-chip').forEach(chip => {
        if (chip.dataset.state === 'include') parts.push(chip.dataset.token);
        else if (chip.dataset.state === 'exclude') parts.push('-' + chip.dataset.token);
    });
    return parts.join(', ');
}

// Apply a stored spec back onto the chips. Tokens are matched case-insensitively
// (the server lowercases on parse) and any token outside this picker's fixed
// vocabulary is ignored — the picker only ever surfaces the known ratings.
function setCertPicker(picker, spec) {
    buildCertPicker(picker);
    const include = new Set(), exclude = new Set();
    (spec || '').split(',').forEach(raw => {
        const token = raw.trim().toLowerCase();
        if (!token) return;
        if (token.startsWith('-')) { const bare = token.slice(1).trim(); if (bare) exclude.add(bare); }
        else include.add(token);
    });
    picker.querySelectorAll('.cert-chip').forEach(chip => {
        const t = chip.dataset.token.toLowerCase();
        setChipState(chip, exclude.has(t) ? 'exclude' : include.has(t) ? 'include' : '');
    });
}

function clearCertPicker(picker) {
    picker.querySelectorAll('.cert-chip').forEach(chip => setChipState(chip, ''));
}

// Build every picker up front so a modal opened before its fetch resolves (or
// with no stored value) still shows the full, interactive chip row. buildCertPicker
// is idempotent per element, so re-running it after a boosted swap only builds the
// freshly-inserted pickers.
function initCertPickers() {
    document.querySelectorAll('.chip-picker').forEach(buildCertPicker);
}
