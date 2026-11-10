"""app/signed_tokens.py — the sealing every token a page hands a browser shares.

THE PROMISES: a token reads back only under the purpose it was sealed for, by
this instance; one bit changed anywhere is refused; one token has one spelling;
the pieces a layout is spelled in read back exactly, and a payload that runs out
or runs over is refused rather than half-read.
"""
from __future__ import annotations

import base64
import unittest

from app import signed_tokens


class SealingTests(unittest.TestCase):

    def test_a_sealed_payload_opens_under_its_own_purpose_only(self):
        text = signed_tokens.seal("one purpose", b"\x01payload")
        self.assertEqual(signed_tokens.opened("one purpose", text), b"\x01payload")
        self.assertIsNone(signed_tokens.opened("another purpose", text))

    def test_one_bit_changed_anywhere_is_refused(self):
        text = signed_tokens.seal("p", b"\x01\x02\x03payload")
        raw = base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
        for at in range(len(raw)):
            with self.subTest(at=at):
                edited = bytearray(raw)
                edited[at] ^= 0x01
                spelled = base64.urlsafe_b64encode(bytes(edited)).rstrip(b"=").decode()
                self.assertIsNone(signed_tokens.opened("p", spelled))

    def test_one_token_has_one_spelling(self):
        text = signed_tokens.seal("p", b"payload")
        for spelled in (text[:4] + "!" + text[4:], text[:4] + "+" + text[4:], text + "=",
                        " " + text, "A" * (signed_tokens.MAX_TEXT + 1), "", "é"):
            with self.subTest(spelled=spelled[:20]):
                self.assertIsNone(signed_tokens.opened("p", spelled))

    def test_the_last_characters_unused_bits_are_part_of_the_spelling(self):
        # A token of 3n+1 bytes ends in a character carrying 4 bits no byte uses,
        # one of 3n+2 bytes in one carrying 2; the decoder ignores them, so every
        # other value of those bits is another spelling of the same bytes.
        alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
        for payload in (b"payload", b"payload!", b"payload!?"):
            text = signed_tokens.seal("p", payload)
            spare = {1: 4, 2: 2}.get((len(payload) + signed_tokens.SIGNATURE_BYTES) % 3, 0)
            with self.subTest(length=len(payload)):
                self.assertEqual(signed_tokens.opened("p", text), payload)
                last = alphabet.index(text[-1])
                others = [alphabet[(last & ~((1 << spare) - 1)) | bits]
                          for bits in range(1 << spare)]
                others.remove(text[-1])
                self.assertEqual(len(others), (1 << spare) - 1)
                for other in others:
                    self.assertIsNone(signed_tokens.opened("p", text[:-1] + other))


class LayoutPiecesTests(unittest.TestCase):

    def test_the_pieces_read_back_exactly(self):
        payload = b"".join((signed_tokens.varint(0), signed_tokens.varint(300),
                            signed_tokens.varint(2 ** 40), signed_tokens.optional_int(None),
                            signed_tokens.optional_int(0), signed_tokens.optional_int(-7),
                            signed_tokens.optional_int(2026), signed_tokens.text("show:tmdb:1"),
                            signed_tokens.text("")))
        reader = signed_tokens.Reader(payload)
        self.assertEqual([reader.varint(), reader.varint(), reader.varint()], [0, 300, 2 ** 40])
        self.assertEqual([reader.optional_int() for _ in range(4)], [None, 0, -7, 2026])
        self.assertEqual([reader.text(64), reader.text(64)], ["show:tmdb:1", ""])
        reader.end()

    def test_a_payload_that_runs_out_or_over_is_refused(self):
        with self.assertRaises(ValueError):
            signed_tokens.Reader(signed_tokens.varint(300)[:1]).varint()
        with self.assertRaises(ValueError):
            signed_tokens.Reader(signed_tokens.text("abcdef")).text(3)
        reader = signed_tokens.Reader(signed_tokens.varint(1) + b"\x00")
        reader.varint()
        with self.assertRaises(ValueError):
            reader.end()

    def test_a_varint_is_never_negative(self):
        with self.assertRaises(ValueError):
            signed_tokens.varint(-1)
