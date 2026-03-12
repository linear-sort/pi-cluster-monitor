from __future__ import annotations

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.models import MetricsResponse, ServiceStatus

pytestmark = pytest.mark.integration


def _auth_header(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_auth_required_for_health() -> None:
    with TestClient(app) as client:
        app.state.settings.token = "secret"
        app.state.auth_token = "secret"
        response = client.get("/health")
    assert response.status_code == 401
    assert response.json()["detail"] == "Missing token"


def test_invalid_token_rejected() -> None:
    with TestClient(app) as client:
        app.state.settings.token = "secret"
        app.state.auth_token = "secret"
        response = client.get("/health", headers=_auth_header("wrong"))
    assert response.status_code == 401
    assert response.json()["detail"] == "Invalid token"


def test_metrics_endpoint_returns_collected_payload(monkeypatch) -> None:
    expected = MetricsResponse(
        hostname="pi-test",
        timestamp=datetime.now(timezone.utc),
        cpu_percent=25.0,
        memory_percent=33.0,
        disk_percent=45.0,
        temperature_c=50.0,
        uptime_seconds=1200,
        load_1=0.1,
        load_5=0.2,
        load_15=0.3,
        rx_bytes=101,
        tx_bytes=202,
    )

    def fake_collect_metrics(agent_name: str = "") -> MetricsResponse:
        assert agent_name == "pi-test"
        return expected

    monkeypatch.setattr("app.routes.collect_metrics", fake_collect_metrics)

    with TestClient(app) as client:
        app.state.settings.token = "secret"
        app.state.auth_token = "secret"
        app.state.settings.name = "pi-test"
        response = client.get("/api/v1/metrics", headers=_auth_header("secret"))

    assert response.status_code == 200
    payload = response.json()
    assert payload["hostname"] == "pi-test"
    assert payload["cpu_percent"] == 25.0
    assert payload["rx_bytes"] == 101


def test_services_endpoint_returns_service_status(monkeypatch) -> None:
    def fake_collect_services(_: list[str]) -> list[ServiceStatus]:
        return [ServiceStatus(name="ssh", status="active"), ServiceStatus(name="cron", status="inactive")]

    monkeypatch.setattr("app.routes.collect_services", fake_collect_services)

    with TestClient(app) as client:
        app.state.settings.token = "secret"
        app.state.auth_token = "secret"
        app.state.settings.name = "pi-test"
        app.state.settings.services = ["ssh", "cron"]
        response = client.get("/api/v1/services", headers=_auth_header("secret"))

    assert response.status_code == 200
    payload = response.json()
    assert payload["hostname"] == "pi-test"
    assert len(payload["services"]) == 2
    assert payload["services"][0]["name"] == "ssh"
