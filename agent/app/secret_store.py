from __future__ import annotations

import base64
import hashlib
import os

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

SECRET_PREFIX_V2 = "enc:v2:"


def _derive_key(key: str) -> bytes:
    return hashlib.sha256((key or "").encode("utf-8")).digest()


def seal_secret(value: str, key: str) -> str:
    plain = (value or "").strip()
    if not plain:
        return ""
    if not (key or "").strip():
        return plain
    if plain.startswith(SECRET_PREFIX_V2):
        return plain
    nonce = os.urandom(12)
    aes = AESGCM(_derive_key(key))
    cipher = aes.encrypt(nonce, plain.encode("utf-8"), None)
    payload = base64.urlsafe_b64encode(nonce + cipher).decode("ascii")
    return f"{SECRET_PREFIX_V2}{payload}"


def open_secret(value: str | None, key: str) -> str:
    raw = (value or "").strip()
    if not raw:
        return ""
    if not raw.startswith(SECRET_PREFIX_V2):
        # Backward compatibility for legacy plaintext files.
        return raw
    if not (key or "").strip():
        return ""
    try:
        payload = raw.removeprefix(SECRET_PREFIX_V2)
        blob = base64.urlsafe_b64decode(payload.encode("ascii"))
        if len(blob) < 13:
            return ""
        nonce, cipher = blob[:12], blob[12:]
        aes = AESGCM(_derive_key(key))
        plain = aes.decrypt(nonce, cipher, None)
        return plain.decode("utf-8")
    except Exception:
        return ""
