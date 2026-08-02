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
from app.providers.base import Provider, SyncPort


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
