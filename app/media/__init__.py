"""Pictures: where they are fetched from, how they are cached, how they are
drawn. posters.py, logos.py and user_images.py are the callers; artwork.py is
the poster URL registry; imaging.py holds the primitives both ranker's
grid_builder and calendar's share_card draw with; tmdb.py is the API client
network logos and poster art both go through.

THIN ON PURPOSE. Every other feature that reaches into this package imports
the submodule it actually needs (`from .media import posters`), not a name
re-exported here — see the __init__ rule in the packaging notes. Nothing in
this package needs its own package-level surface today.
"""
