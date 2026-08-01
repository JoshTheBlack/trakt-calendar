"""What airs when, and who is allowed to see it.

routes.py serves the signed-in calendar; cache.py holds the unfiltered window
Trakt returned and filter.py narrows it per viewer at READ time; state.py
remembers what a viewer is not watching and what has changed since they last
looked. The share_* half is the public face: share_links.py mints and resolves
the tokens, share_code.py encodes the view they carry, share_routes.py serves
the public page, and share_card.py plus share_card_cache.py draw and store the
picture a link unfurls into.

THIN ON PURPOSE. Everything outside this package imports the submodule it
actually wants (`from .calendar import cache`), so there is no second name here
to keep in step, and no re-export that would make this __init__ import a
submodule which imports back into the package — a cycle that only shows up at
load time.

The stdlib also has a `calendar` module, and four modules in here use it for
monthrange and month_name. Absolute imports mean a bare `import calendar`
inside this package still resolves to the stdlib — this package is
`app.calendar` — but the bare name reads ambiguously to a human standing in
this directory, so those modules spell it `import calendar as _calendar`.
"""
