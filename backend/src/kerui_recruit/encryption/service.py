from __future__ import annotations

import base64
import os

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

_NONCE_SIZE = 12  # Recommended nonce length for AES-GCM.


class EncryptionService:
    """AES-256-GCM field-level encryption for sensitive data at rest.

    Generates a persistent 256-bit key stored on disk. Used to protect
    sensitive BD lead contact information and stored API keys.
    """

    def __init__(self, key_path: str) -> None:
        self._key_path = key_path
        self._aesgcm = AESGCM(self._load_or_generate_key())

    def encrypt(self, plaintext: str) -> str:
        nonce = os.urandom(_NONCE_SIZE)
        ciphertext = self._aesgcm.encrypt(nonce, plaintext.encode("utf-8"), None)
        return base64.urlsafe_b64encode(nonce + ciphertext).decode("ascii")

    def decrypt(self, ciphertext: str) -> str:
        payload = base64.urlsafe_b64decode(ciphertext.encode("ascii"))
        nonce, data = payload[:_NONCE_SIZE], payload[_NONCE_SIZE:]
        return self._aesgcm.decrypt(nonce, data, None).decode("utf-8")

    def _load_or_generate_key(self) -> bytes:
        if os.path.exists(self._key_path):
            with open(self._key_path, "rb") as f:
                return f.read()
        key = AESGCM.generate_key(bit_length=256)
        os.makedirs(os.path.dirname(self._key_path), exist_ok=True)
        with open(self._key_path, "wb") as f:
            f.write(key)
        return key
