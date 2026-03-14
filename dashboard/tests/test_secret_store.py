from __future__ import annotations

import pytest

from app.secret_store import _seal_v1, open_secret, seal_secret


pytestmark = pytest.mark.unit


def test_seal_secret_uses_v2_prefix_with_key() -> None:
    sealed = seal_secret("node-token-123", "slice7-secret-key")
    assert sealed.startswith("enc:v2:")
    assert sealed != "node-token-123"
    assert open_secret(sealed, "slice7-secret-key") == "node-token-123"


def test_open_secret_supports_legacy_v1_values() -> None:
    key = "slice7-secret-key"
    legacy = _seal_v1("legacy-token", key.encode("utf-8"))
    assert legacy.startswith("enc:v1:")
    assert open_secret(legacy, key) == "legacy-token"


def test_open_secret_fails_closed_with_wrong_key() -> None:
    sealed = seal_secret("node-token-123", "slice7-secret-key")
    assert open_secret(sealed, "wrong-key") == ""
