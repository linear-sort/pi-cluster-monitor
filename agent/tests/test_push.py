from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path

import pytest

from app.config import AgentSettings
from app.models import MetricsResponse
from app.push import build_ingest_signature, push_once

pytestmark = pytest.mark.unit


def test_build_ingest_signature_stable() -> None:
    sig = build_ingest_signature("tok", "1", "abc", '{"k":1}')
    assert len(sig) == 64
    assert sig == build_ingest_signature("tok", "1", "abc", '{"k":1}')


@pytest.mark.asyncio
async def test_push_once_posts_signed_payload(monkeypatch) -> None:
    settings = AgentSettings(
        token="fallback",
        name="pi-agent",
        services=[],
        dashboard_url="http://dashboard.local:8000",
        enroll_secret="",
        enroll_enabled=False,
        enroll_retry_seconds=10,
        token_refresh_enabled=False,
        token_refresh_seconds=3600,
        push_enabled=True,
        push_interval_seconds=5,
        token_file=Path("agent_token.txt"),
    )

    fake_metrics = MetricsResponse(
        hostname="pi-agent",
        timestamp=datetime.now(timezone.utc),
        cpu_percent=1.0,
        memory_percent=2.0,
        disk_percent=3.0,
        temperature_c=40.0,
        uptime_seconds=100,
        load_1=0.1,
        load_5=0.2,
        load_15=0.3,
        rx_bytes=11,
        tx_bytes=22,
    )
    monkeypatch.setattr("app.push.collect_metrics", lambda agent_name="": fake_metrics)

    captured: dict = {}

    class FakeResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return {"status": "accepted"}

    async def fake_post(self, url: str, content: str, headers: dict):  # noqa: ARG001
        captured["url"] = url
        captured["content"] = content
        captured["headers"] = headers
        return FakeResponse()

    monkeypatch.setattr("httpx.AsyncClient.post", fake_post)

    resp = await push_once(settings=settings, token="tok-123", token_version=7)
    assert resp["status"] == "accepted"
    assert captured["url"].endswith("/api/v1/ingest")
    assert "X-PCM-Signature" in captured["headers"]
    assert captured["headers"]["X-PCM-Token-Version"] == "7"
    payload = json.loads(captured["content"])
    assert payload["hostname"] == "pi-agent"
