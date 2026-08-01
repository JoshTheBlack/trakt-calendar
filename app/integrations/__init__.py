"""The three services a user points at their OWN self-hosted server and adds
titles to: Sonarr (arr.py), Radarr (arr.py), and Overseerr (seer.py). routes.py
is their admin-only route surface and owns the two in-memory caches (health,
library) that make those routes cheap to poll.

THIN ON PURPOSE. arr.py and seer.py each declare their own client pool and a
LibraryUnavailable exception that routes.py catches by name; both are part of
this package's surface even though neither is re-exported here — a caller that
wants either imports the submodule directly (`from .integrations import arr`),
per the __init__ rule in the packaging notes.
"""
