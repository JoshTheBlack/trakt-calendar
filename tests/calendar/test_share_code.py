"""The compact `?p=` share code.

Two kinds of test here. The round-trip ones are ordinary. The codebook ones are
guarding a promise that outlives the code: an index published in somebody's link
must still mean the same thing years later, so they fail loudly on a reorder
rather than letting one ship as a "harmless" tidy-up.
"""
from __future__ import annotations

import hashlib
import unittest
from itertools import combinations
from unittest.mock import patch

from app.calendar import share_code
from app.endpoints import ENDPOINTS
from app.calendar.share_links import CARD_STYLES, DAY_PACKINGS
from app.sources import prefs as source_prefs
from app.timezones import CANONICAL


class ShareCodeRoundTripTests(unittest.TestCase):
    def test_a_full_view_survives_the_round_trip(self):
        view = {"endpoint": "shows/premieres", "card": "horizontal", "packing": "stacked",
                "hidenw": "1", "tz": "America/New_York", "year": "2026", "month": "8"}
        code = share_code.encode(view)
        self.assertIsNotNone(code)
        self.assertEqual(share_code.decode(code), view)

    def test_the_code_is_shorter_than_the_query_string_it_replaces(self):
        """The entire point. Twelve characters against a hundred."""
        code = share_code.encode({
            "endpoint": "shows/premieres", "card": "horizontal", "packing": "stacked",
            "hidenw": "1", "tz": "America/New_York", "year": "2026", "month": "8"})
        self.assertEqual(len(code), 12)

    def test_a_view_naming_a_source_survives_the_round_trip(self):
        view = {"endpoint": "shows", "card": "vertical", "packing": "packed",
                "hidenw": "0", "tz": "Europe/London", "year": "2026", "month": "9",
                "source": "simkl"}
        self.assertEqual(share_code.decode(share_code.encode(view)), view)

    def test_the_same_view_without_a_source_carries_none(self):
        """Presence is the whole of what "my sources" means here: a link that
        names no service must not come back naming one, or every link that never
        asked would start overriding the owner's preference."""
        view = {"endpoint": "shows", "card": "vertical", "packing": "packed",
                "hidenw": "0", "tz": "Europe/London", "year": "2026", "month": "9"}
        decoded = share_code.decode(share_code.encode(view))
        self.assertEqual(decoded, view)
        self.assertNotIn("source", decoded)

    def test_every_combination_of_the_registered_services_round_trips(self):
        """THE REASON THE FIELD IS A BITMASK. A selection is a SET, and every set
        of the services this app knows has to survive the short form — including
        the ones nobody thought to write down, because the next service to be
        registered doubles how many there are."""
        names = sorted(source_prefs.SOURCE_NAMES)
        for size in range(1, len(names) + 1):
            for combination in combinations(names, size):
                selection = source_prefs.SEPARATOR.join(combination)
                with self.subTest(selection=selection):
                    decoded = share_code.decode(share_code.encode({"source": selection}))
                    self.assertEqual(source_prefs.named_sources(decoded["source"]),
                                     frozenset(combination))

    def test_a_service_with_no_bit_yet_sends_the_link_out_verbose(self):
        """A service registered but never appended to the codebook. Rule 2: no
        code, so the caller writes the long query string, which always works and
        says exactly what it means. The codebook growth test below is what stops
        this being the quiet permanent state."""
        with patch.object(share_code, "SOURCE_CODES", ("trakt",)):
            self.assertIsNone(share_code.encode({"source": "trakt+simkl"}))
            self.assertIsNone(share_code.encode({"source": "simkl"}))

    def test_a_bit_this_version_has_no_service_for_drops_the_field(self):
        """A code from a build that knows more services than this one. Decoding
        it to the services we DO recognize would narrow the link in a way its
        author never asked for; dropping the field falls back to the owner's own
        preference, which renders wider rather than wrong."""
        code = share_code.encode({"source": "trakt+simkl", "card": "poster"})
        with patch.object(share_code, "SOURCE_CODES", ("trakt",)):
            decoded = share_code.decode(code)
        self.assertNotIn("source", decoded)
        # And only that field: the rest of the code is still read.
        self.assertEqual(decoded["card"], "poster")

    def test_the_legacy_two_service_spelling_codes_as_the_set_it_names(self):
        """"both" is a spelling of exactly {trakt, simkl} and has no entry of its
        own — it packs as those two bits and comes back as the set, which is what
        the Sources screen rewrites it to on the next save anyway."""
        code = share_code.encode({"source": source_prefs.BOTH})
        self.assertEqual(share_code.decode(code)["source"], "trakt+simkl")
        self.assertEqual(code, share_code.encode({"source": "trakt+simkl"}))

    def test_only_the_options_that_were_set_come_back(self):
        """Presence is meaningful: "use my current display" writes no params, so
        a code must not invent defaults for the ones it wasn't given."""
        code = share_code.encode({"year": "2026", "month": "8"})
        self.assertEqual(share_code.decode(code), {"year": "2026", "month": "8"})

    def test_every_single_option_round_trips_alone(self):
        for view in ({"endpoint": "movies"}, {"card": "poster"}, {"packing": "packed"},
                     {"hidenw": "0"}, {"hidenw": "1"}, {"tz": "Pacific/Chatham"},
                     {"year": "1970", "month": "1"}, {"year": "2100", "month": "12"},
                     {"source": "trakt"}, {"source": "simkl"}, {"source": "auto"},
                     {"source": "trakt+simkl"}):
            with self.subTest(view=view):
                self.assertEqual(share_code.decode(share_code.encode(view)), view)

    def test_every_month_of_a_year_round_trips(self):
        for month in range(1, 13):
            view = {"year": "2026", "month": str(month)}
            with self.subTest(month=month):
                self.assertEqual(share_code.decode(share_code.encode(view)), view)

    def test_an_empty_view_has_no_code(self):
        """A link with no options carries no query string at all, not an empty
        code that says nothing."""
        self.assertIsNone(share_code.encode({}))

    def test_something_unencodable_falls_back_rather_than_lying(self):
        """A value outside the codebook must yield no code — the caller then
        writes the long query string, which always works."""
        for view in ({"tz": "Mars/Olympus_Mons"}, {"endpoint": "shows/imaginary"},
                     {"card": "hologram"}, {"year": "1500", "month": "3"}):
            with self.subTest(view=view):
                self.assertIsNone(share_code.encode(view))

    def test_a_mangled_code_decodes_to_nothing_rather_than_raising(self):
        """This parses a string a stranger can type into the address bar."""
        for code in (None, "", "x", "9FF", "1", "1ZZ", "1FF", "13F", "!!!!", "1" * 400):
            with self.subTest(code=code):
                self.assertIsInstance(share_code.decode(code), dict)

    def test_an_index_naming_nothing_is_dropped_not_guessed(self):
        # Endpoint present in the mask, index F: nothing is filed under F.
        self.assertEqual(share_code.decode("101F"), {})

    def test_a_code_is_case_insensitive(self):
        code = share_code.encode({"tz": "Europe/London", "card": "poster"})
        self.assertEqual(share_code.decode(code.lower()), share_code.decode(code))


class PublishedCodeTests(unittest.TestCase):
    """Codes that were handed out before a layout grew, decoded literally.

    A ROUND TRIP CANNOT CATCH WHAT THIS CATCHES. Encoding and decoding move
    together, so a change that repositioned a field or repointed an index would
    round-trip perfectly and still resolve every link already pasted into
    somebody's channel to a different month, a different calendar or a different
    timezone. The only test that sees that is a literal string somebody's link
    actually carries, written down here and compared against what it meant on
    the day it was copied. Every string below came off a real instance.

    A code added here is never edited afterwards. If one of these fails, the
    layout it was written under has been changed rather than added to.
    """

    def test_a_code_written_under_the_first_layout_still_means_what_it_meant(self):
        self.assertEqual(share_code.decode("13F11010E2A7"), {
            "endpoint": "shows/premieres", "card": "horizontal", "packing": "stacked",
            "hidenw": "1", "tz": "America/New_York", "year": "2026", "month": "8",
        })


class ShareCodebookStabilityTests(unittest.TestCase):
    """A codebook index is a promise about what that number means, kept by every
    link already in the wild. These tests fail on the change that would break it.
    """

    # Update ONLY by appending to a codebook, and only alongside the new digest.
    # If one of these fails without you having appended, something was reordered
    # or removed and every link holding that index now points somewhere else.
    DIGESTS = {
        "ENDPOINT_CODES": "38014921cbc7ef6ab13cf4f2210ffde13129b9c230534725874693dc02de8136",
        "CARD_CODES": "00da7eb36be7dd5c30773b9870d9badf30dc6ea3d218117c241f668d1fcaa321",
        "PACKING_CODES": "e4d06ca823d3c3b0f6d14bb61c00520cf778bb6a5441ce99a205e0856b205f87",
        "TZ_CODES": "72f4bf6274384a16ce9325a6457dcc2b427b143a9d3508c02176408eb97539e2",
        "SOURCE_CODES": "85ab2bb9982a90bf204486dfb45d6342e8e96abaac3f8929515c45f336a41b00",
    }

    def _digest(self, book) -> str:
        return hashlib.sha256("\n".join(book).encode()).hexdigest()

    def test_no_codebook_has_been_reordered_or_shortened(self):
        for name, expected in self.DIGESTS.items():
            with self.subTest(codebook=name):
                book = getattr(share_code, name)
                self.assertEqual(self._digest(book[:self.LENGTHS[name]]), expected)

    # The length each codebook had when its digest was taken. Appending past this
    # is fine and needs no new digest; the digest covers only the frozen prefix.
    LENGTHS = {"ENDPOINT_CODES": 5, "CARD_CODES": 3, "PACKING_CODES": 2, "TZ_CODES": 101,
               "SOURCE_CODES": 2}

    def test_no_codebook_has_lost_entries(self):
        for name, length in self.LENGTHS.items():
            with self.subTest(codebook=name):
                self.assertGreaterEqual(len(getattr(share_code, name)), length)

    def test_every_option_the_app_offers_can_be_coded(self):
        """A vocabulary that grows elsewhere without being appended here would
        quietly send every link carrying it out in the long form instead."""
        self.assertEqual(set(ENDPOINTS) - set(share_code.ENDPOINT_CODES), set())
        self.assertEqual(set(CARD_STYLES) - set(share_code.CARD_CODES), set())
        self.assertEqual(set(DAY_PACKINGS) - set(share_code.PACKING_CODES), set())
        zones = {tz for group in CANONICAL.values() for tz in group}
        self.assertEqual(zones - set(share_code.TZ_CODES), set())
        # SERVICE NAMES, because the source field is a bitmask over them: a
        # service registered without a bit appended here has no bit to be named
        # by, so every link naming it — alone or in any combination — would
        # silently go out in the long form.
        self.assertEqual(set(source_prefs.SOURCE_NAMES) - set(share_code.SOURCE_CODES), set())

    def test_no_codebook_has_duplicates(self):
        """Two indexes for one value is a link that decodes to the right place
        and then re-encodes to a different code."""
        for name in self.LENGTHS:
            book = getattr(share_code, name)
            with self.subTest(codebook=name):
                self.assertEqual(len(book), len(set(book)))


if __name__ == "__main__":
    unittest.main()
