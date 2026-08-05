"""Every registered source really does satisfy the Protocol it is registered as.

WHY THIS IS WORTH MORE THAN ITS SIZE. app/providers/base.py declares what a
source must provide and app/providers/trakt/__init__.py provides it, and until
now the only thing keeping the two in step was whoever edited one noticing they
must edit the other. `register()` takes whatever it is handed.

The two directions are not equally dangerous. REMOVING a member from the Protocol
without removing its implementation leaves dead code, which is untidy and
harmless — a `fetch_calendar` sat here exactly like that. ADDING one and
forgetting a source is an AttributeError at run time, raised on whichever source
the user happens to have configured, and the entire suite is blind to it. That is
the direction a second source will travel repeatedly.

WHAT runtime_checkable ACTUALLY VERIFIES, STATED HONESTLY: member PRESENCE, and
nothing else. `isinstance(x, Provider)` asks whether the named attributes and
methods exist on x — it does NOT compare signatures, so a source whose method
takes the wrong arguments passes here and still fails in production. Closing that
gap needs `inspect.signature` over the Protocol's members, which would need no
new dependency; it is not done here because the members are few and the argument
lists are read at every call site anyway, so the cheaper check is the one that
earns its keep. If a source ever gains a method the app calls indirectly, revisit
that trade rather than assuming this file covers it.
"""
from __future__ import annotations

import inspect
import unittest

from app import providers
from app.providers.base import LibraryPort, PlayCountPort, Provider, SyncPort


class RegisteredProvidersConformTests(unittest.TestCase):
    """Walk the registry — not a hand-written list of sources, which would go
    stale the moment a second one is added without anybody updating it."""

    def setUp(self):
        self.registry = providers.registered()

    def test_the_registry_is_not_empty(self):
        """Guards every other test in this file. A registry that failed to load
        would make them all pass by iterating nothing, which is the shape of
        vacuous test this suite has been bitten by before."""
        self.assertTrue(self.registry, "no sources registered; the checks below prove nothing")

    def test_every_registered_source_satisfies_provider(self):
        for source, provider in self.registry.items():
            with self.subTest(source=source):
                self.assertIsInstance(provider, Provider)

    def test_a_source_is_registered_under_the_key_it_claims(self):
        # The registry is a dict keyed by Source, and `provider.source` is the
        # same fact stated on the object. A mismatch means a lookup by key hands
        # back something that disagrees about who it is.
        for source, provider in self.registry.items():
            with self.subTest(source=source):
                self.assertEqual(provider.source, source)

    def test_a_source_claiming_private_reads_carries_a_sync_port(self):
        """`capabilities.private_user_data` is how the tracker decides a source
        can back it; `sync_port` is what it then calls. A source asserting the
        first with None in the second is lying in a way nothing else catches —
        the tracker finds a usable source and then has nothing to call."""
        for source, provider in self.registry.items():
            with self.subTest(source=source):
                if provider.capabilities.private_user_data:
                    self.assertIsNotNone(
                        provider.sync_port,
                        f"{source} claims private user data but carries no sync port")

    def test_every_sync_port_present_satisfies_syncport(self):
        for source, provider in self.registry.items():
            port = provider.sync_port
            if port is None:
                continue
            with self.subTest(source=source):
                self.assertIsInstance(port, SyncPort)


class LibraryPortIsOptionalTests(unittest.TestCase):
    """LibraryPort is deliberately NOT part of SyncPort, and this is what says so.

    A source that can hand over a whole library at once is asked for it and
    matched on the shared identity; one that cannot is asked per title with its
    own id. Both are correct, so the tracker branches on this isinstance and
    nothing else — which makes "does this port claim the library read" a fact
    worth pinning rather than an implementation detail. At least one registered
    source must satisfy it, or the branch is dead and every test of it is
    vacuous.
    """

    def test_at_least_one_registered_source_can_hand_over_a_library(self):
        ports = [provider.sync_port for provider in providers.registered().values()
                 if provider.sync_port is not None]
        self.assertTrue([port for port in ports if isinstance(port, LibraryPort)],
                        "no source implements the library read; the tracker's "
                        "identity-keyed baseline is unreachable")

    def test_a_sync_port_without_the_library_read_is_not_one(self):
        """The negative half, and the one that matters: a port answering per
        title must not be handed a whole-library question it cannot answer."""
        for source, provider in providers.registered().items():
            port = provider.sync_port
            if port is None or hasattr(port, "fetch_library"):
                continue
            with self.subTest(source=source):
                self.assertNotIsInstance(port, LibraryPort)


class PlayCountPortIsOptionalTooTests(unittest.TestCase):
    """The third port protocol, and the same rule: optional, branched on by
    isinstance, and dead the moment no registered source satisfies it.

    IT IS NOT A SMALLER LibraryPort. A source that hands over its whole library
    re-states what it holds per episode, so nothing needs telling which titles
    moved — re-reading the list corrects a removal on its own. This exists for the
    source whose whole-library read carries no episodes at all, where the only
    other way to learn what changed is to ask about every title in turn.
    """

    def test_at_least_one_registered_source_can_sweep_its_play_counts(self):
        ports = [provider.sync_port for provider in providers.registered().values()
                 if provider.sync_port is not None]
        self.assertTrue([port for port in ports if isinstance(port, PlayCountPort)],
                        "no source implements the play-count sweep; the tracker's "
                        "targeted re-baseline is unreachable")

    def test_the_two_optional_ports_are_not_the_same_claim(self):
        """A source is free to satisfy both, neither, or one — but a source that
        satisfies the library read and nothing else must not be swept, and one
        that can only be swept must not be asked for a library."""
        for source, provider in providers.registered().items():
            port = provider.sync_port
            if port is None:
                continue
            with self.subTest(source=source):
                self.assertEqual(isinstance(port, PlayCountPort),
                                 hasattr(port, "fetch_play_counts"))


class TheCheckWouldActuallyFailTests(unittest.TestCase):
    """A conformance test that cannot fail is decoration.

    isinstance against a Protocol is quiet enough that "it passed" is weak
    evidence on its own, so these show the check reacting: an object missing one
    member is rejected, and one that has them all is accepted.
    """

    def _stand_in(self, omit: str | None = None):
        """An object carrying every Provider member except, optionally, one.

        Built from the Protocol's own declared members rather than a hand-written
        list, so adding a member to Provider does not leave this double quietly
        describing the old shape.
        """
        members = [name for name in Provider.__protocol_attrs__ if name != omit]
        namespace = {}
        for name in members:
            declared = getattr(Provider, name, None)
            namespace[name] = (lambda self, *a, **k: None) if inspect.isfunction(declared) else None
        return type("StandIn", (), namespace)()

    def test_an_object_with_every_member_is_accepted(self):
        self.assertIsInstance(self._stand_in(), Provider)

    def test_an_object_missing_one_member_is_rejected(self):
        for name in Provider.__protocol_attrs__:
            with self.subTest(missing=name):
                self.assertNotIsInstance(self._stand_in(omit=name), Provider)

    def test_the_protocol_declares_the_members_the_registry_depends_on(self):
        # Named explicitly because the two tests above are self-referential: they
        # derive the member list from the Protocol, so they would still pass if
        # somebody deleted a member outright. These four are read by
        # app/providers/__init__.py itself.
        for name in ("source", "label", "capabilities", "sync_port", "is_configured"):
            with self.subTest(member=name):
                self.assertIn(name, Provider.__protocol_attrs__)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
