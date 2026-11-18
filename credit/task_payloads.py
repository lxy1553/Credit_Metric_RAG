"""Authenticated encryption for short-lived async task payloads."""

import base64
import binascii
import hashlib
import hmac
import os
import re
from collections.abc import Mapping

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM


class InvalidTaskPayload(ValueError):
    """Raised when an encrypted task payload is malformed or cannot be authenticated."""


class TaskPayloadCipher:
    """AES-256-GCM envelope with request binding and decrypt-only key rotation support."""

    NONCE_BYTES = 12
    MAX_PAYLOAD_BYTES = 64 * 1024
    KEY_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

    def __init__(self, keys: Mapping[str, bytes], *, active_key_id: str):
        if not keys or active_key_id not in keys:
            raise ValueError("active key id must exist in keyring")
        self.keys: dict[str, AESGCM] = {}
        self._key_bytes = dict(keys)
        for key_id, key in keys.items():
            if not self.KEY_ID_RE.fullmatch(key_id):
                raise ValueError("key ids may contain only letters, digits, underscore and hyphen")
            if not isinstance(key, bytes) or len(key) != 32:
                raise ValueError("AES-256-GCM keys must be exactly 32 bytes")
            self.keys[key_id] = AESGCM(key)
        self.active_key_id = active_key_id

    @staticmethod
    def _aad(request_id: str, key_id: str) -> bytes:
        if not isinstance(request_id, str) or not request_id or len(request_id) > 128:
            raise ValueError("request_id must be a non-empty string of at most 128 characters")
        return f"credit-task-payload:v1:{request_id}:{key_id}".encode("utf-8")

    def encrypt(self, plaintext: str, *, request_id: str) -> str:
        if not isinstance(plaintext, str):
            raise TypeError("task payload must be text")
        payload = plaintext.encode("utf-8")
        if len(payload) > self.MAX_PAYLOAD_BYTES:
            raise ValueError("task payload exceeds the 64 KiB limit")
        key_id = self.active_key_id
        iv = os.urandom(self.NONCE_BYTES)
        ciphertext = self.keys[key_id].encrypt(iv, payload, self._aad(request_id, key_id))
        token = base64.urlsafe_b64encode(iv + ciphertext).decode("ascii").rstrip("=")
        return f"{key_id}.{token}"

    def decrypt(self, token: str, *, request_id: str) -> str:
        try:
            key_id, encoded = token.split(".", 1)
            cipher = self.keys[key_id]
            if not self.KEY_ID_RE.fullmatch(key_id) or not encoded:
                raise ValueError
            padded = encoded + "=" * (-len(encoded) % 4)
            blob = base64.b64decode(padded, altchars=b"-_", validate=True)
            if len(blob) < self.NONCE_BYTES + 16:
                raise ValueError
            plaintext = cipher.decrypt(
                blob[:self.NONCE_BYTES], blob[self.NONCE_BYTES:], self._aad(request_id, key_id),
            )
            if len(plaintext) > self.MAX_PAYLOAD_BYTES:
                raise ValueError
            return plaintext.decode("utf-8")
        except (KeyError, ValueError, UnicodeDecodeError, binascii.Error, InvalidTag):
            raise InvalidTaskPayload("task payload authentication failed") from None

    def fingerprint(self, value: str) -> str:
        """Return a deterministic keyed digest for idempotency without exposing text."""
        if not isinstance(value, str):
            raise TypeError("fingerprint input must be text")
        payload = value.encode("utf-8")
        if len(payload) > self.MAX_PAYLOAD_BYTES:
            raise ValueError("fingerprint input exceeds the 64 KiB limit")
        message = b"credit-task-fingerprint:v1:" + payload
        return hmac.new(self._key_bytes[self.active_key_id], message, hashlib.sha256).hexdigest()
