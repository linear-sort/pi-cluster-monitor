from __future__ import annotations

import asyncio
from pathlib import Path
import socket
from typing import Any

import httpx

from app.config import AgentSettings


def load_token_from_file(token_file: Path) -> str | None:
    if not token_file.exists():
        return None
    token = token_file.read_text(encoding="utf-8").strip()
    return token or None


def save_token_to_file(token_file: Path, token: str) -> None:
    token_file.parent.mkdir(parents=True, exist_ok=True)
    token_file.write_text(token.strip(), encoding="utf-8")


async def enroll_once(settings: AgentSettings, ip_address: str | None = None) -> tuple[str | None, int | None]:
    if not settings.dashboard_url or not settings.enroll_secret:
        return None

    hostname = settings.name or socket.gethostname()
    payload: dict[str, Any] = {
        "enroll_secret": settings.enroll_secret,
        "hostname": hostname,
        "name": settings.name or hostname,
        "ip_address": ip_address,
        "agent_port": 8001,
        "role": "worker",
        "poll_interval_seconds": 10,
    }

    timeout = httpx.Timeout(5.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await client.post(f"{settings.dashboard_url}/api/v1/enroll", json=payload)
        response.raise_for_status()
        data = response.json()
        token = str(data.get("token", "")).strip()
        token_version = int(data.get("token_version", 1))
        return token or None, token_version


async def refresh_once(settings: AgentSettings, token: str, token_version: int | None = None) -> tuple[str | None, int | None]:
    if not settings.dashboard_url or not token:
        return None

    hostname = settings.name or socket.gethostname()
    timeout = httpx.Timeout(5.0)
    headers = {"Authorization": f"Bearer {token}"}
    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await client.post(
            f"{settings.dashboard_url}/api/v1/token/refresh",
            json={"hostname": hostname, "token_version": token_version},
            headers=headers,
        )
        response.raise_for_status()
        data = response.json()
        next_token = str(data.get("token", "")).strip()
        next_version = int(data.get("token_version", token_version or 1))
        return next_token or None, next_version


async def enrollment_loop(app) -> None:
    settings: AgentSettings = app.state.settings
    static_token = settings.token.strip()
    have_static_fallback = bool(static_token)
    uses_enrollment = settings.enroll_enabled and bool(settings.dashboard_url and settings.enroll_secret)

    while True:
        try:
            current_token = str(getattr(app.state, "auth_token", "") or "").strip()
            current_version = int(getattr(app.state, "token_version", 1) or 1)

            if (not current_token or (have_static_fallback and current_token == static_token)) and uses_enrollment:
                token, token_version = await enroll_once(settings=settings)
                if token:
                    app.state.auth_token = token
                    app.state.token_version = int(token_version or 1)
                    save_token_to_file(settings.token_file, token)
            elif settings.token_refresh_enabled and settings.dashboard_url and current_token:
                token, token_version = await refresh_once(
                    settings=settings,
                    token=current_token,
                    token_version=current_version,
                )
                if token:
                    app.state.auth_token = token
                    app.state.token_version = int(token_version or current_version)
                    save_token_to_file(settings.token_file, token)
        except asyncio.CancelledError:
            raise
        except Exception:
            pass
        sleep_seconds = settings.token_refresh_seconds if settings.token_refresh_enabled else settings.enroll_retry_seconds
        await asyncio.sleep(max(3, sleep_seconds))
