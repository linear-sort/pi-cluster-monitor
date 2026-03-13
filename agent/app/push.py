from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import hashlib
import hmac
import json
import secrets
import socket
import time

import httpx

from app.collectors.system_metrics import collect_metrics
from app.config import AgentSettings


def build_ingest_signature(token: str, timestamp: str, nonce: str, payload_json: str) -> str:
    message = f"{timestamp}.{nonce}.{payload_json}"
    return hmac.new(token.encode("utf-8"), message.encode("utf-8"), hashlib.sha256).hexdigest()


async def push_once(settings: AgentSettings, token: str, agent_id: str, token_version: int = 1) -> dict:
    metrics = collect_metrics(agent_name=settings.name)
    payload = metrics.model_dump(mode="json")
    payload["hostname"] = settings.name or socket.gethostname()
    payload["agent_id"] = agent_id
    payload_json = json.dumps(payload, separators=(",", ":"), sort_keys=True)
    timestamp = str(int(time.time()))
    nonce = secrets.token_urlsafe(12)
    signature = build_ingest_signature(token=token, timestamp=timestamp, nonce=nonce, payload_json=payload_json)
    headers = {
        "Authorization": f"Bearer {token}",
        "X-PCM-Timestamp": timestamp,
        "X-PCM-Nonce": nonce,
        "X-PCM-Signature": signature,
        "X-PCM-Token-Version": str(max(1, int(token_version))),
    }
    timeout = httpx.Timeout(5.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await client.post(
            f"{settings.dashboard_url}/api/v1/ingest",
            content=payload_json,
            headers={**headers, "Content-Type": "application/json"},
        )
        response.raise_for_status()
        return response.json()


async def push_loop(app) -> None:
    settings: AgentSettings = app.state.settings
    while True:
        try:
            token = str(getattr(app.state, "auth_token", "") or "").strip()
            agent_id = str(getattr(app.state, "agent_id", "") or "").strip()
            token_version = int(getattr(app.state, "token_version", 1) or 1)
            if settings.push_enabled and settings.dashboard_url and token and agent_id:
                await push_once(settings=settings, token=token, agent_id=agent_id, token_version=token_version)
        except asyncio.CancelledError:
            raise
        except Exception:
            pass
        await asyncio.sleep(max(3, settings.push_interval_seconds))
