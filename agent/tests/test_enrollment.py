from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.config import AgentSettings
from app.enrollment import enrollment_loop, load_token_from_file, save_token_to_file

pytestmark = pytest.mark.unit


def test_load_and_save_token_file(tmp_path: Path) -> None:
    token_file = tmp_path / "agent_token.txt"
    assert load_token_from_file(token_file) is None
    save_token_to_file(token_file, "abc123")
    assert load_token_from_file(token_file) == "abc123"


@pytest.mark.asyncio
async def test_enrollment_loop_sets_runtime_and_persisted_token(monkeypatch, tmp_path: Path) -> None:
    token_file = tmp_path / "agent_token.txt"
    settings = AgentSettings(
        token="fallback-token",
        name="pi-agent",
        services=[],
        dashboard_url="http://dashboard.local:8000",
        enroll_secret="secret",
        enroll_enabled=True,
        enroll_retry_seconds=1,
        token_refresh_enabled=False,
        token_refresh_seconds=60,
        token_file=token_file,
    )
    app = SimpleNamespace(state=SimpleNamespace(settings=settings, auth_token="fallback-token"))

    attempts = {"count": 0}
    saved = {"token": None}
    token_saved_event = asyncio.Event()

    async def fake_enroll_once(settings: AgentSettings, ip_address: str | None = None) -> tuple[str | None, int | None]:  # noqa: ARG001
        attempts["count"] += 1
        if attempts["count"] == 1:
            raise RuntimeError("temporary")
        return "issued-token", 3

    original_sleep = asyncio.sleep
    monkeypatch.setattr("app.enrollment.enroll_once", fake_enroll_once)
    monkeypatch.setattr("app.enrollment.asyncio.sleep", lambda _: original_sleep(0))
    monkeypatch.setattr(
        "app.enrollment.save_token_to_file",
        lambda _path, token: (saved.__setitem__("token", token), token_saved_event.set()),
    )

    task = asyncio.create_task(enrollment_loop(app))
    await asyncio.wait_for(token_saved_event.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert app.state.auth_token == "issued-token"
    assert app.state.token_version == 3
    assert saved["token"] == "issued-token"
    assert attempts["count"] >= 2


@pytest.mark.asyncio
async def test_enrollment_loop_refreshes_existing_token(monkeypatch, tmp_path: Path) -> None:
    token_file = tmp_path / "agent_token.txt"
    settings = AgentSettings(
        token="static-token",
        name="pi-agent",
        services=[],
        dashboard_url="http://dashboard.local:8000",
        enroll_secret="",
        enroll_enabled=False,
        enroll_retry_seconds=10,
        token_refresh_enabled=True,
        token_refresh_seconds=1,
        token_file=token_file,
    )
    app = SimpleNamespace(state=SimpleNamespace(settings=settings, auth_token="old-token"))

    async def fake_refresh_once(
        settings: AgentSettings, token: str, token_version: int | None = None
    ) -> tuple[str | None, int | None]:  # noqa: ARG001
        if token == "old-token":
            return "new-token", 4
        return token, token_version

    original_sleep = asyncio.sleep
    monkeypatch.setattr("app.enrollment.refresh_once", fake_refresh_once)
    monkeypatch.setattr("app.enrollment.asyncio.sleep", lambda _: original_sleep(0))

    task = asyncio.create_task(enrollment_loop(app))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert app.state.auth_token == "new-token"
    assert app.state.token_version == 4
    assert load_token_from_file(token_file) == "new-token"
