"""Tier boards: ranking titles into ordered tiers, and taking the result away.

core.py is the data layer — it reads and writes `tier_boards`,
`tier_categories` and `tier_items` and decides nothing about HTTP. routes.py
parses requests and delegates to it. sources.py is where titles come from,
behind Protocols that keep a provider's name out of everything downstream, and
imports.py is the one optional source that knows the tracker exists — delete it
and the feature loses a convenience button and nothing else. exports.py decides
which titles an export is of and in what order; grid_builder.py turns that list
into pixels.

THIN ON PURPOSE. Everything outside this package imports the submodule it
actually wants (`from .ranker import routes`), so there is no second name here
to keep in step, and no re-export that would make this __init__ import a
submodule which imports back into the package — a cycle that only shows up at
load time, and one this app has already had to work around elsewhere by
deferring imports into a function body.

The plural in imports.py and exports.py is not a style choice: `import` is a
keyword, so `app/ranker/import.py` cannot be reached by an import statement at
all, and exports.py is spelled to match its pair.
"""
