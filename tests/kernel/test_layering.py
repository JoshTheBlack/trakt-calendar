"""The import layering of app/, enforced.

app/ is a shared kernel plus a handful of feature groups. Python enforces none
of that — making a directory does not stop anything importing anything — so the
structure is only real to the extent something checks it. This is that something.

WHAT IS CHECKED, and why each part earns its place:

  1. EVERY CROSS-GROUP IMPORT IS DECLARED in DECLARED_EDGES below. An undeclared
     edge fails and the failure names the file, the line and the target, because
     "layering violation" tells a reader nothing they can act on.
  2. EVERY DECLARATION IS STILL USED. Without this the table rots into a blanket
     permission: edges get added as they are needed and never removed, and after
     a year it says nothing. A declaration nobody exercises is a rule nobody has.
  3. AN EDGE DECLARED AS DEFERRED NEVER APPEARS AT MODULE LEVEL. Two edges here
     exist only inside a function body or behind `if TYPE_CHECKING`, and that
     placement is the entire reason they are tolerable — it is what stops them
     closing an import cycle at load time. Promoting one to the top of its file
     would look like a tidy-up and would be a load-order change.
  4. THE MODULE-LEVEL GRAPH IS ACYCLIC. Deferred edges are excluded from this
     one, and only from this one: a cycle matters because of what it does at
     import time, and an import inside a function has not happened yet.
  5. EVERY MODULE IN app/ BELONGS TO A GROUP. A new file that nobody filed is
     the failure mode that would otherwise make all of the above vacuous.
  6. EVERY DECLARATION CARRIES A REASON. Check 1 fails with a name and a line,
     and the cheapest way to make it stop is to paste the tuple into the table.
     Requiring a sentence is what makes declaring an edge cost more than the
     thirty seconds it takes to think about whether it should exist.

THE RESOLVER IS THE RISK, NOT THE POLICY. `from ..perftrace import span` inside
app/distrakt/live.py means app.perftrace, not app.distrakt.perftrace; `from .
import cache` inside app/calendar/routes.py means app.calendar.cache, not the
kernel's app.cache — and that second one names a module that really exists, so
getting it wrong raises nothing. A resolver that reads only `node.module` maps
both to the wrong group and then reports green while checking nothing.
ResolverTests below exercises it on synthetic sources for exactly that reason,
and it is the part of this file to distrust first if something here surprises
you.

GROUP MEMBERSHIP IS LOGICAL, NOT POSITIONAL. A module belongs to the group whose
feature it implements, whether it currently sits at the root of app/ or inside
that feature's package. That is deliberate: it lets this test hold while modules
are relocated, so a relocation that files something under the wrong feature
fails here instead of silently redrawing the map.

No network, no database — this reads source text.
"""
from __future__ import annotations

import ast
import unittest
from collections import defaultdict
from typing import NamedTuple

from tests.support import APP_DIR

# ---------------------------------------------------------------------------
# The groups.
# ---------------------------------------------------------------------------
KERNEL = "kernel"
PROVIDER_TYPES = "provider_types"
PROVIDERS = "providers"
AUTH = "auth"
MEDIA = "media"
INTEGRATIONS = "integrations"
CALENDAR = "calendar"
DISTRAKT = "distrakt"
RANKER = "ranker"
SETTINGS = "settings"
MAIN = "main"

# Dotted path under app/ -> group, matched longest-prefix-first, so
# "providers.base" wins over "providers" and a package name covers everything
# inside it. Every feature now lives in a package, so one key per package covers
# its whole contents; the three keys naming a single root-level module are the
# declared exceptions to "the root of app/ is the kernel" and each says why below.
GROUPS: dict[str, str] = {
    # The kernel: configuration, storage, HTTP plumbing, rendering. Depends on
    # no feature, which is what makes it safe for every feature to depend on it.
    "assets": KERNEL, "cache": KERNEL, "changelog": KERNEL, "chrome": KERNEL,
    "clock": KERNEL, "config": KERNEL, "db": KERNEL, "endpoints": KERNEL, "http_pool": KERNEL,
    "perftrace": KERNEL, "route_params": KERNEL, "secrets_box": KERNEL,
    "templating": KERNEL, "timezones": KERNEL,

    # The value-type contract a source produces (Media, Item, Source, ItemKey).
    # Its own group because it is the one part of providers/ the kernel is
    # allowed to know: it declares shapes and imports nothing at run time, so
    # depending on it cannot drag an implementation or a socket along with it.
    "providers.base": PROVIDER_TYPES,

    # The sources themselves, and the registry that picks one.
    "providers": PROVIDERS,

    # Identity: who is signed in, what they may do, and the at-rest key that
    # protects their stored credentials. A LAYER, not a peer feature — every
    # feature may depend on it, and it depends on no feature. authz.py sits at
    # the root of app/ but belongs here: it imports auth and exists to attach
    # auth dependencies to routes.
    "auth": AUTH, "authz": AUTH,

    # Pictures: where they are fetched from, how they are cached, how they are
    # drawn. media/tmdb.py is here rather than with the integrations because
    # network logos and poster art are the only things that read it.
    "media": MEDIA,

    # The three services a user points at their OWN server and adds titles to.
    "integrations": INTEGRATIONS,

    "calendar": CALENDAR,

    "distrakt": DISTRAKT,

    "ranker": RANKER,

    # The admin Settings screen's API, plus the app-wide Trakt device-code flow.
    # Feature tier despite sitting at the root: it reaches sideways into two
    # features and up into auth. One module does not earn a package.
    "settings_routes": SETTINGS,

    # The composition root. Exempt as an IMPORTER (see EXEMPT_IMPORTERS); it is
    # still grouped so that anything importing IT would be caught.
    "main": MAIN,
}

# The composition root registers every feature's router. Importing all of them
# is what a composition root is FOR, so its edges are not declared individually —
# declaring them would mean a table that grows with every feature while asserting
# nothing. Nothing in app/ may import it back, which assertion 1 enforces from
# the other side: no group declares an edge to MAIN.
EXEMPT_IMPORTERS = frozenset({"app.main"})


class Edge(NamedTuple):
    """A permitted group -> group dependency and the reason it is permitted.

    `deferred` marks an edge that may ONLY appear inside a function body or
    behind `if TYPE_CHECKING` — see assertion 3 in the module docstring.
    """
    reason: str
    deferred: bool = False


DECLARED_EDGES: dict[tuple[str, str], Edge] = {
    # --- the kernel, downward. Uninteresting and universal. ----------------
    (PROVIDERS, KERNEL): Edge("a source reads settings, caches responses, times itself"),
    (AUTH, KERNEL): Edge("sessions and credentials are stored and configured"),
    (MEDIA, KERNEL): Edge("images are cached on disk and fetched with the pooled client"),
    (INTEGRATIONS, KERNEL): Edge("a service's URL and API key come from settings"),
    (CALENDAR, KERNEL): Edge("the month cache is a database table read through config"),
    (DISTRAKT, KERNEL): Edge("the tracker's tables, settings and templates"),
    (RANKER, KERNEL): Edge("boards are stored, rendered and configured"),
    (SETTINGS, KERNEL): Edge("the Settings screen reads and writes the settings themselves"),

    # --- the value-type contract, which everything may name ----------------
    (KERNEL, PROVIDER_TYPES): Edge(
        "endpoints.py names Media to declare what each calendar endpoint is FOR. "
        "It is a value type that imports nothing at run time, so this does not "
        "make the kernel depend on any source"),
    (PROVIDERS, PROVIDER_TYPES): Edge("a source produces Items and declares Capabilities"),
    (MEDIA, PROVIDER_TYPES): Edge("artwork is keyed by ItemKey"),
    (CALENDAR, PROVIDER_TYPES): Edge("the cache stores and replays Items"),
    (DISTRAKT, PROVIDER_TYPES): Edge("watch state is keyed by ItemKey across sources"),
    (RANKER, PROVIDER_TYPES): Edge("a board entry is a title identified the same way"),
    (PROVIDER_TYPES, KERNEL): Edge(
        "Settings appears in the Protocol signatures only. Deferred behind "
        "TYPE_CHECKING so the value-type module stays importable from the "
        "kernel without closing the loop",
        deferred=True),

    # --- into the sources --------------------------------------------------
    (AUTH, PROVIDERS): Edge(
        "the Trakt login flow exchanges its device code on the same pooled "
        "client and against the same API base the source itself uses, so the "
        "two cannot drift apart"),
    (MEDIA, PROVIDERS): Edge("poster art is looked up per title through the source"),
    (CALENDAR, PROVIDERS): Edge("the cache asks a source what airs in a window"),
    (DISTRAKT, PROVIDERS): Edge("the tracker reads the viewer's own history and progress"),
    (RANKER, PROVIDERS): Edge("ratings are imported from the viewer's source account"),
    (KERNEL, PROVIDERS): Edge(
        "config.py asks the registry which sources are configured when it "
        "validates settings. Function-local because providers/ reads config, "
        "and doing it at the top of the file would close that loop",
        deferred=True),

    # --- into auth. auth is a layer: every feature may depend on it ---------
    (INTEGRATIONS, AUTH): Edge("adding a title to a library is admin-only"),
    (CALENDAR, AUTH): Edge("the calendar is per-viewer, and share links are per-owner"),
    (DISTRAKT, AUTH): Edge("a tracker month belongs to one signed-in person"),
    (RANKER, AUTH): Edge("a board belongs to one signed-in person"),
    (SETTINGS, AUTH): Edge("the Settings screen is admin-only and links the app's own account"),
    (KERNEL, AUTH): Edge(
        "config.py reaches encryption_flow to refuse writing a secret while the "
        "at-rest key is in a bad state. Function-local because encryption_flow "
        "reads config. THE ONE EDGE that stops 'the kernel depends on nothing "
        "above it' being literally true; inverting it is a load-order change",
        deferred=True),

    # --- feature -> feature. Each one earned its place ---------------------
    (AUTH, MEDIA): Edge("deleting an account deletes its profile and header pictures"),
    (CALENDAR, MEDIA): Edge("a day block, and the picture a share link unfurls into, show posters"),
    (DISTRAKT, MEDIA): Edge("a tracked show is shown with its poster"),
    (RANKER, MEDIA): Edge("an exported board is drawn from posters with the shared image primitives"),
    (CALENDAR, INTEGRATIONS): Edge(
        "the calendar page renders the add-to-library buttons, so for an admin it "
        "has to know which of the three services is alive"),
    (SETTINGS, INTEGRATIONS): Edge(
        "saving settings invalidates the cached library so the add-buttons stop "
        "showing marks from the old configuration"),
    (DISTRAKT, CALENDAR): Edge(
        "the tracker imports a month's premieres, honours 'not watching', and "
        "links a Discord post at the viewer's own calendar"),
    (RANKER, DISTRAKT): Edge(
        "one adapter module imports the tracker's public surface so a board can "
        "be seeded from what the viewer finished. THE ONLY ranker module allowed "
        "to know the tracker exists — tests/ranker/test_standalone.py enforces it"),
}


# ---------------------------------------------------------------------------
# The resolver. Distrust this before you distrust the policy above.
# ---------------------------------------------------------------------------
def group_of(dotted: str) -> str | None:
    """The group `dotted` belongs to, or None if it is not part of app/.

    Longest prefix wins, so app.providers.base lands in its own group while
    app.providers.trakt.sync lands in the sources' one.
    """
    if not dotted.startswith("app.") or dotted == "app":
        return None
    parts = dotted[len("app."):].split(".")
    for cut in range(len(parts), 0, -1):
        group = GROUPS.get(".".join(parts[:cut]))
        if group is not None:
            return group
    return None


def resolve_import(node: ast.Import | ast.ImportFrom, package: tuple[str, ...]) -> list[str]:
    """The absolute dotted module names `node` refers to.

    `package` is the package CONTAINING the file, as dotted parts — ("app",) for
    app/config.py, ("app", "distrakt") for app/distrakt/live.py. Relative levels
    are resolved against it, which is the whole point: level 1 means "this
    package", level 2 means "the parent", and reading node.module alone gets both
    wrong.

    A `from X import a, b` yields X, not X.a and X.b — the names may be
    attributes rather than submodules, and X is enough to place the dependency.
    A `from . import a, b` yields each name, because with no module part they can
    only be submodules of the containing package.
    """
    if isinstance(node, ast.Import):
        return [alias.name for alias in node.names]
    if node.level == 0:
        return [node.module] if node.module else []
    # level 1 is the containing package itself; each further level strips one.
    keep = len(package) - (node.level - 1)
    if keep < 1:
        raise ValueError(f"relative import escapes the package: level {node.level} in {package}")
    base = package[:keep]
    if node.module is None:
        return [".".join(base + (alias.name,)) for alias in node.names]
    return [".".join(base + (node.module,))]


def _defers(node: ast.AST) -> bool:
    """Whether imports inside `node` are deferred — not executed when the module
    is first loaded, and therefore unable to close an import cycle."""
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return True
    if isinstance(node, ast.If):
        # `if TYPE_CHECKING:` — written either bare or as `typing.TYPE_CHECKING`.
        # The else branch is swept in with it, which is harmless: nobody writes
        # one, and treating it as deferred would at worst under-report a cycle
        # that assertion 1 would still catch as an undeclared edge.
        test = node.test
        if isinstance(test, ast.Attribute):
            return test.attr == "TYPE_CHECKING"
        return isinstance(test, ast.Name) and test.id == "TYPE_CHECKING"
    return False


def walk_imports(node: ast.AST, deferred: bool = False):
    """Yield (import node, deferred) for every import under `node`.

    Deliberately does NOT skip function-local imports. Hiding them would hide
    real coupling; they are reported and then declared, which is why
    DECLARED_EDGES carries a `deferred` flag instead of a blind spot.
    """
    for child in ast.iter_child_nodes(node):
        if isinstance(child, (ast.Import, ast.ImportFrom)):
            yield child, deferred
        else:
            yield from walk_imports(child, deferred or _defers(child))


def find_cycle(graph: dict[str, set[str]]) -> list[str]:
    """A cycle in `graph` as the path that closes it, or [] if there is none.

    Returns the PATH rather than a bool because "there is a cycle" is not a
    message anybody can act on — the sequence of groups is.
    """
    cycle: list[str] = []
    state: dict[str, int] = {}  # 1 = on the current path, 2 = finished

    def visit(node: str, path: list[str]) -> bool:
        state[node] = 1
        for nxt in sorted(graph.get(node, ())):
            if state.get(nxt) == 1:
                # Report the loop itself, not the walk that wandered into it.
                # `nxt` is absent from `path` only for a self-edge, where the
                # loop is the node twice over.
                start = path.index(nxt) if nxt in path else len(path)
                cycle.extend(path[start:] + [node, nxt])
                return True
            if state.get(nxt) is None and visit(nxt, path + [node]):
                return True
        state[node] = 2
        return False

    for node in sorted(graph):
        if state.get(node) is None and visit(node, []):
            break
    return cycle


class _Observed(NamedTuple):
    src: str
    dst: str
    where: str
    target: str
    deferred: bool


def _module_files() -> list:
    return sorted(APP_DIR.rglob("*.py"))


def _dotted(path) -> str:
    parts = list(path.relative_to(APP_DIR.parent).with_suffix("").parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def observe() -> list[_Observed]:
    """Every cross-group import in app/, as facts rather than verdicts."""
    found: list[_Observed] = []
    for path in _module_files():
        dotted = _dotted(path)
        if dotted in EXEMPT_IMPORTERS:
            continue
        src = group_of(dotted)
        if src is None:
            continue
        package = tuple(path.relative_to(APP_DIR.parent).with_suffix("").parts[:-1])
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node, deferred in walk_imports(tree):
            for target in resolve_import(node, package):
                dst = group_of(target)
                if dst is None or dst == src:
                    continue
                where = f"{path.relative_to(APP_DIR.parent).as_posix()}:{node.lineno}"
                found.append(_Observed(src, dst, where, target, deferred))
    return found


class ResolverTests(unittest.TestCase):
    """The resolver, on synthetic sources.

    A structural test that resolves imports wrongly reports GREEN and buys
    nothing, so this is checked before anything is asserted with it.
    """

    def _resolve(self, source: str, package: tuple[str, ...]) -> list[str]:
        tree = ast.parse(source)
        out: list[str] = []
        for node, _ in walk_imports(tree):
            out.extend(resolve_import(node, package))
        return out

    def test_plain_absolute_import(self):
        self.assertEqual(self._resolve("import app.db", ("app",)), ["app.db"])

    def test_absolute_from_import_names_the_module_not_the_names(self):
        self.assertEqual(
            self._resolve("from app.providers.base import Item, Media", ("app",)),
            ["app.providers.base"])

    def test_single_dot_with_no_module_names_each_sibling(self):
        """`from . import a, b` inside app/ is two dependencies, not one."""
        self.assertEqual(
            self._resolve("from . import auth, db", ("app",)),
            ["app.auth", "app.db"])

    def test_single_dot_resolves_against_the_containing_package(self):
        """Inside app/calendar/, `from . import cache` is app.calendar.cache —
        NOT the kernel's app.cache, which is a different module entirely."""
        self.assertEqual(
            self._resolve("from . import cache", ("app", "calendar")),
            ["app.calendar.cache"])

    def test_single_dot_with_a_module_part(self):
        self.assertEqual(
            self._resolve("from .transport import TraktError", ("app", "providers", "trakt")),
            ["app.providers.trakt.transport"])

    def test_double_dot_climbs_one_package(self):
        self.assertEqual(
            self._resolve("from .. import discord_fmt", ("app", "distrakt")),
            ["app.discord_fmt"])

    def test_triple_dot_two_packages_deep(self):
        self.assertEqual(
            self._resolve("from ... import calendar_filter", ("app", "providers", "trakt")),
            ["app.calendar_filter"])

    def test_double_dot_with_a_module_part(self):
        self.assertEqual(
            self._resolve("from ..base import Item", ("app", "providers", "trakt")),
            ["app.providers.base"])

    def test_a_relative_import_that_escapes_the_package_is_an_error(self):
        with self.assertRaises(ValueError):
            self._resolve("from ... import x", ("app",))

    def test_function_local_imports_are_seen_and_marked_deferred(self):
        tree = ast.parse(
            "def f():\n"
            "    from . import db\n"
        )
        found = [(resolve_import(n, ("app",)), d) for n, d in walk_imports(tree)]
        self.assertEqual(found, [(["app.db"], True)])

    def test_async_function_local_imports_too(self):
        tree = ast.parse(
            "async def f():\n"
            "    from . import providers\n"
        )
        found = [(resolve_import(n, ("app",)), d) for n, d in walk_imports(tree)]
        self.assertEqual(found, [(["app.providers"], True)])

    def test_type_checking_imports_are_seen_and_marked_deferred(self):
        tree = ast.parse(
            "from typing import TYPE_CHECKING\n"
            "if TYPE_CHECKING:\n"
            "    from ..config import Settings\n"
        )
        found = [(resolve_import(n, ("app", "providers")), d) for n, d in walk_imports(tree)]
        self.assertEqual(found[-1], (["app.config"], True))

    def test_a_module_level_import_is_not_deferred(self):
        tree = ast.parse("from . import db\n")
        self.assertEqual([d for _, d in walk_imports(tree)], [False])

    def test_nested_function_imports_stay_deferred(self):
        tree = ast.parse(
            "def outer():\n"
            "    def inner():\n"
            "        from . import db\n"
        )
        self.assertEqual([d for _, d in walk_imports(tree)], [True])


class CycleFinderTests(unittest.TestCase):
    """The cycle finder, on synthetic graphs — same reasoning as ResolverTests:
    a detector that never fires would let the acyclicity assertion below report
    green while checking nothing."""

    def test_a_layered_graph_has_no_cycle(self):
        self.assertEqual(find_cycle({"a": {"b", "c"}, "b": {"c"}, "c": set()}), [])

    def test_a_direct_cycle_is_found_and_named(self):
        self.assertEqual(find_cycle({"a": {"b"}, "b": {"a"}}), ["a", "b", "a"])

    def test_an_indirect_cycle_is_found(self):
        found = find_cycle({"a": {"b"}, "b": {"c"}, "c": {"a"}})
        self.assertEqual(found, ["a", "b", "c", "a"])

    def test_a_cycle_off_the_first_path_is_still_found(self):
        """The walk starts at `a`, which is not itself in the cycle."""
        self.assertEqual(find_cycle({"a": {"b"}, "b": {"c"}, "c": {"b"}}), ["b", "c", "b"])

    def test_a_self_edge_is_a_cycle(self):
        self.assertEqual(find_cycle({"a": {"a"}}), ["a", "a"])

    def test_a_diamond_is_not_a_cycle(self):
        """Two paths to the same node revisit it, which a naive walk mistakes for
        a cycle. It is not one."""
        self.assertEqual(find_cycle({"a": {"b", "c"}, "b": {"d"}, "c": {"d"}, "d": set()}), [])


class GroupingTests(unittest.TestCase):
    def test_every_module_in_app_belongs_to_a_group(self):
        """A file nobody filed would make every other assertion here vacuous for
        that file: it is neither a checked source nor a checked target."""
        unfiled = sorted(
            path.relative_to(APP_DIR).as_posix() for path in _module_files()
            if _dotted(path) != "app" and group_of(_dotted(path)) is None
        )
        self.assertEqual(unfiled, [], (
            "these modules are not assigned to a group in GROUPS — file each one "
            f"with the feature it implements: {unfiled}"))

    def test_every_declared_edge_carries_a_reason(self):
        """The reason is the only part of an entry a human reads, and adding a
        bare one is how this table would be turned into a rubber stamp — it is
        the two-second way to silence a failure. A blank reason fails."""
        unexplained = sorted(
            f"{src} -> {dst}" for (src, dst), edge in DECLARED_EDGES.items()
            if len(edge.reason.split()) < 4
        )
        self.assertEqual(unexplained, [], (
            "these edges are declared without saying why. An edge nobody can "
            f"justify in a sentence is an edge to remove, not to declare: {unexplained}"))

    def test_no_group_declares_a_dependency_on_the_composition_root(self):
        """main.py imports every feature. A feature importing main back would be
        an import cycle through the one module that is exempt from the cycle
        check, which is the worst possible place to hide one."""
        self.assertEqual([edge for edge in DECLARED_EDGES if edge[1] == MAIN], [])


class LayeringTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.observed = observe()

    def test_every_cross_group_import_is_declared(self):
        undeclared = sorted({
            f"{o.where} imports {o.target} ({o.src} -> {o.dst})"
            for o in self.observed if (o.src, o.dst) not in DECLARED_EDGES
        })
        self.assertEqual(undeclared, [], (
            "undeclared cross-group imports. Either the import belongs "
            "somewhere else, or the edge is legitimate and belongs in "
            f"DECLARED_EDGES with the reason written beside it:\n  "
            + "\n  ".join(undeclared)))

    def test_every_declared_edge_is_still_used(self):
        """A permission nobody exercises is a rule nobody has. Without this the
        table only ever grows and stops describing the code."""
        used = {(o.src, o.dst) for o in self.observed}
        stale = sorted(f"{src} -> {dst}" for src, dst in DECLARED_EDGES if (src, dst) not in used)
        self.assertEqual(stale, [], (
            "these edges are declared but no longer exist. Delete them, so the "
            f"table keeps describing the code: {stale}"))

    def test_a_deferred_edge_never_appears_at_module_level(self):
        """The two kernel edges below exist only inside a function body or behind
        TYPE_CHECKING, and that placement is why they are tolerable at all.
        Hoisting one to the top of its file reads like a tidy-up and is a
        load-order change that can fail at import."""
        offenders = sorted({
            f"{o.where} imports {o.target} ({o.src} -> {o.dst})"
            for o in self.observed
            if not o.deferred and DECLARED_EDGES.get((o.src, o.dst), Edge("")).deferred
        })
        self.assertEqual(offenders, [], (
            "these imports must stay inside a function body or behind "
            f"TYPE_CHECKING — moving them to module level closes an import "
            f"cycle:\n  " + "\n  ".join(offenders)))

    def test_the_module_level_graph_is_acyclic(self):
        """Deferred edges are excluded, and only here: a cycle is a problem
        because of what it does at import time, and an import inside a function
        has not happened yet. Assertion 3 above is what keeps that exclusion
        honest."""
        graph: dict[str, set[str]] = defaultdict(set)
        for (src, dst), edge in DECLARED_EDGES.items():
            if not edge.deferred:
                graph[src].add(dst)
        cycle = find_cycle(graph)
        self.assertEqual(cycle, [], f"import cycle in the declared graph: {' -> '.join(cycle)}")


if __name__ == "__main__":
    unittest.main()
