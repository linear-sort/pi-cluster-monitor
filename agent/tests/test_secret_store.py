from __future__ import annotations

import pytest

from app.secret_store import open_secret, seal_secret

pytestmark = pytest.mark.unit


def test_seal_secret_round_trip_with_key() -> None:
    sealed = seal_secret("agent-token", "slice7-key")
    assert sealed.startswith("enc:v2:")
    assert open_secret(sealed, "slice7-key") == "agent-token"


def test_open_secret_supports_legacy_plaintext_values() -> None:
    assert open_secret("legacy-agent-id", "slice7-key") == "legacy-agent-id"


def test_open_secret_fails_closed_with_wrong_key() -> None:
    sealed = seal_secret("agent-token", "slice7-key")
    assert open_secret(sealed, "wrong-key") == ""
