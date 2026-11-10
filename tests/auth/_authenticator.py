"""A software authenticator, so no passkey test needs a browser or a device.

An ES256 key from `cryptography` makes a passkey the way a platform
authenticator does — a "none" attestation over authenticator data carrying the
credential's public key — and signs assertions over the challenge a page's
options carry. Every field a test may want wrong (the origin, the rp id, the
user handle, the sign count, the flags) is a keyword, so a test says only what
it changes.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import struct

import cbor2
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec

UP, UV, BE, BS, AT = 0x01, 0x04, 0x08, 0x10, 0x40


def b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


class Authenticator:
    """One passkey on one device. `synced` sets the backup flags a synced
    passkey reports; `counter` is the sign count the next assertion carries."""

    def __init__(self, *, synced: bool = True, counter: int = 0):
        self.key = ec.generate_private_key(ec.SECP256R1())
        self.credential_id = os.urandom(16)
        self.synced = synced
        self.counter = counter
        self.user_handle: bytes | None = None

    def _cose(self) -> bytes:
        numbers = self.key.public_key().public_numbers()
        return cbor2.dumps({1: 2, 3: -7, -1: 1, -2: numbers.x.to_bytes(32, "big"),
                            -3: numbers.y.to_bytes(32, "big")})

    def _flags(self, extra: int = 0, *, verified: bool = True) -> int:
        flags = UP | extra | (UV if verified else 0)
        return flags | (BE | BS if self.synced else 0)

    @staticmethod
    def _client_data(kind: str, challenge: str, origin: str) -> bytes:
        return json.dumps({"type": kind, "challenge": challenge, "origin": origin,
                           "crossOrigin": False}).encode("utf-8")

    def create(self, options: str | dict, *, origin: str, rp_id: str | None = None,
               verified: bool = True) -> str:
        """The JSON a browser's toJSON() gives for a credential made from
        `options` (a "Create passkey" button's)."""
        options = json.loads(options) if isinstance(options, str) else options
        self.user_handle = unb64(options["user"]["id"])
        rp = rp_id or options["rp"]["id"]
        auth_data = (hashlib.sha256(rp.encode()).digest()
                     + bytes([self._flags(AT, verified=verified)])
                     + struct.pack(">I", self.counter) + bytes(16)
                     + struct.pack(">H", len(self.credential_id)) + self.credential_id
                     + self._cose())
        attestation = cbor2.dumps({"fmt": "none", "attStmt": {}, "authData": auth_data})
        return json.dumps({
            "id": b64(self.credential_id), "rawId": b64(self.credential_id), "type": "public-key",
            "authenticatorAttachment": "platform", "clientExtensionResults": {},
            "response": {"clientDataJSON": b64(self._client_data(
                "webauthn.create", options["challenge"], origin)),
                "attestationObject": b64(attestation), "transports": ["internal", "hybrid"]}})

    def assert_(self, options: str | dict, *, origin: str, rp_id: str | None = None,
                user_handle: bytes | None = None, verified: bool = True,
                counter: int | None = None) -> str:
        """The JSON a browser's toJSON() gives for an assertion answering
        `options` (a "Sign in with a passkey" button's)."""
        options = json.loads(options) if isinstance(options, str) else options
        if counter is not None:
            self.counter = counter
        rp = rp_id or options["rpId"]
        auth_data = (hashlib.sha256(rp.encode()).digest()
                     + bytes([self._flags(verified=verified)]) + struct.pack(">I", self.counter))
        client_data = self._client_data("webauthn.get", options["challenge"], origin)
        signature = self.key.sign(auth_data + hashlib.sha256(client_data).digest(),
                                  ec.ECDSA(hashes.SHA256()))
        handle = user_handle if user_handle is not None else self.user_handle
        return json.dumps({
            "id": b64(self.credential_id), "rawId": b64(self.credential_id), "type": "public-key",
            "authenticatorAttachment": "platform", "clientExtensionResults": {},
            "response": {"clientDataJSON": b64(client_data), "authenticatorData": b64(auth_data),
                         "signature": b64(signature),
                         "userHandle": b64(handle) if handle else None}})
