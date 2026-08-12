// ---- Filters (per viewer, not per instance) ----
// Reads and writes /api/me/prefs, not /api/settings: these belong to whoever is
// signed in, and /api/settings is admin-only. The filters are applied at read
// time against one shared calendar cache, so each account can filter the same
// cached month its own way.

async function openFilters() {
    try {
        const res = await fetch('/api/me/prefs', { cache: 'no-store' });
        const d = await res.json();
        const p = d.prefs || {};
        document.getElementById('f_genres').value = p.genres || '';
        document.getElementById('f_countries').value = p.countries || '';
        setCertPicker(document.getElementById('f_show_certifications'), p.show_certifications || '');
        setCertPicker(document.getElementById('f_movie_certifications'), p.movie_certifications || '');
        document.getElementById('f_release_countries').value = p.movie_release_countries || '';
        setCertPicker(document.getElementById('f_release_types'), p.movie_release_types || '');
        document.getElementById('f_networks').value = (p.network_filter || []).join(', ');
    } catch (e) {
        console.error(e);
        toast('Could not load your filters', false);
    }
    document.getElementById('filtersModal').classList.add('open');
}

function closeFilters() {
    document.getElementById('filtersModal').classList.remove('open');
}

// Empties the three inputs WITHOUT saving, so "Clear all" then Cancel leaves the
// stored filters alone — the same bargain every other field in these modals makes.
function clearFilters() {
    ['f_genres', 'f_countries', 'f_release_countries', 'f_networks'].forEach(id => {
        document.getElementById(id).value = '';
    });
    ['f_show_certifications', 'f_movie_certifications', 'f_release_types'].forEach(id => {
        clearCertPicker(document.getElementById(id));
    });
}

async function saveFilters(event) {
    event.preventDefault();
    const payload = {
        genres: document.getElementById('f_genres').value,
        countries: document.getElementById('f_countries').value,
        show_certifications: readCertPicker(document.getElementById('f_show_certifications')),
        movie_certifications: readCertPicker(document.getElementById('f_movie_certifications')),
        movie_release_countries: document.getElementById('f_release_countries').value,
        movie_release_types: readCertPicker(document.getElementById('f_release_types')),
        network_filter: document.getElementById('f_networks').value
    };
    try {
        const res = await fetch('/api/me/prefs', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload)
        });
        const d = await res.json().catch(() => ({}));
        if (!res.ok || !d.ok) {
            toast(d.error || 'Could not save filters', false);
            return false;
        }
        // Filtering happens server-side while the month is assembled, so the
        // page has to be rebuilt to reflect it.
        window.location.reload();
    } catch (e) {
        console.error(e);
        toast('Could not save filters', false);
    }
    return false;
}
