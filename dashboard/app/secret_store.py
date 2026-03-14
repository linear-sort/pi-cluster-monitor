from __future__ import annotations

import base64
import hashlib
import os

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

TOKEN_PREFIX_V1 = "enc:v1:"
TOKEN_PREFIX_V2 = "enc:v2:"


def _xor_bytes(left: bytes, right: bytes) -> bytes:
    return bytes(a ^ b for a, b in zip(left, right))


def _keystream(key: bytes, nonce: bytes, length: int) -> bytes:
    out = bytearray()
    counter = 0
    while len(out) < length:
        block = hashlib.sha256(key + nonce + counter.to_bytes(8, "big")).digest()
        out.extend(block)
        counter += 1
    return bytes(out[:length])


def _derive_v2_key(key: str) -> bytes:
    return hashlib.sha256((key or "").encode("utf-8")).digest()


def _seal_v1(plain: str, key_material: bytes) -> str:
    nonce = os.urandom(16)
    plain_bytes = plain.encode("utf-8")
    cipher = _xor_bytes(plain_bytes, _keystream(key_material, nonce, len(plain_bytes)))
    payload = base64.urlsafe_b64encode(nonce + cipher).decode("ascii")
    return f"{TOKEN_PREFIX_V1}{payload}"


def _open_v1(raw: str, key_material: bytes) -> str:
    payload = raw.removeprefix(TOKEN_PREFIX_V1)
    blob = base64.urlsafe_b64decode(payload.encode("ascii"))
    if len(blob) < 17:
        return ""
    nonce, cipher = blob[:16], blob[16:]
    plain_bytes = _xor_bytes(cipher, _keystream(key_material, nonce, len(cipher)))
    return plain_bytes.decode("utf-8")


def _seal_v2(plain: str, key: str) -> str:
    nonce = os.urandom(12)
    aes = AESGCM(_derive_v2_key(key))
    cipher = aes.encrypt(nonce, plain.encode("utf-8"), None)
    payload = base64.urlsafe_b64encode(nonce + cipher).decode("ascii")
    return f"{TOKEN_PREFIX_V2}{payload}"


def _open_v2(raw: str, key: str) -> str:
    payload = raw.removeprefix(TOKEN_PREFIX_V2)
    blob = base64.urlsafe_b64decode(payload.encode("ascii"))
    if len(blob) < 13:
        return ""
    nonce, cipher = blob[:12], blob[12:]
    aes = AESGCM(_derive_v2_key(key))
    plain = aes.decrypt(nonce, cipher, None)
    return plain.decode("utf-8")


def seal_secret(value: str, key: str) -> str:
    plain = (value or "").strip()
    if not plain:
        return ""
    key_material = (key or "").encode("utf-8")
    if not key_material:
        return plain
    if plain.startswith(TOKEN_PREFIX_V2) or plain.startswith(TOKEN_PREFIX_V1):
        return plain
    return _seal_v2(plain, key)


def open_secret(value: str | None, key: str) -> str:
    raw = (value or "").strip()
    if not raw:
        return ""
    if not raw.startswith(TOKEN_PREFIX_V2) and not raw.startswith(TOKEN_PREFIX_V1):
        return raw
    if not (key or "").strip():
        return ""
    try:
        if raw.startswith(TOKEN_PREFIX_V2):
            return _open_v2(raw, key)
        key_material = key.encode("utf-8")
        return _open_v1(raw, key_material)
    except Exception:
        return ""
