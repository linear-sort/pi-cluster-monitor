from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import httpx

from app.db import ensure_db, get_conn, utc_now_iso
from app.models import AgentMetrics
from app.services.poller import PollingService

pytestmark = pytest.mark.unit


def _seed_node(db_path: Path, last_seen_at: str | None = None) -> int:
    with get_conn(db_path) as conn:
        cursor = conn.execute(
            """
            INSERT INTO nodes (
                name, hostname, ip_address, token, role,
                agent_port, poll_interval_seconds, enabled, last_seen_at, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "pi-1",
                "pi-1.local",
                "10.0.0.10",
                "token",
                "worker",
                8001,
                10,
                1,
                last_seen_at,
                utc_now_iso(),
                utc_now_iso(),
            ),
        )
        return int(cursor.lastrowid)


@pytest.mark.asyncio
async def test_record_success_persists_metrics_and_services(tmp_path: Path) -> None:
    db_path = tmp_path / "cluster.db"
    ensure_db(db_path)
    node_id = _seed_node(db_path)

    poller = PollingService(db_path=db_path)
    metrics = AgentMetrics(
        hostname="pi-1",
        timestamp=datetime.now(timezone.utc),
        cpu_percent=22.0,
        memory_percent=45.0,
        disk_percent=31.0,
        temperature_c=48.0,
        uptime_seconds=123,
        load_1=0.1,
        load_5=0.2,
        load_15=0.3,
        rx_bytes=1000,
        tx_bytes=2000,
    )

    await poller._record_success(
        node_id=node_id,
        metrics=metrics,
        raw_payload=metrics.model_dump(mode="json"),
        services_payload=[{"name": "ssh", "status": "active"}],
    )

    with get_conn(db_path) as conn:
        node = conn.execute("SELECT last_status FROM nodes WHERE id = ?", (node_id,)).fetchone()
        assert node is not None
        assert node["last_status"] == "online"

        metric_count = conn.execute("SELECT COUNT(*) AS c FROM metric_samples WHERE node_id = ?", (node_id,)).fetchone()
        assert metric_count is not None
        assert metric_count["c"] == 1

        service_row = conn.execute("SELECT name, status FROM services WHERE node_id = ?", (node_id,)).fetchone()
        assert service_row is not None
        assert service_row["name"] == "ssh"
        assert service_row["status"] == "active"


@pytest.mark.asyncio
async def test_record_failure_marks_offline_and_creates_alert(tmp_path: Path) -> None:
    db_path = tmp_path / "cluster.db"
    ensure_db(db_path)
    stale_seen = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
    node_id = _seed_node(db_path, last_seen_at=stale_seen)
    poller = PollingService(db_path=db_path)

    await poller._record_failure(node_id=node_id, category="offline", message="connect failed", reachable=False)

    with get_conn(db_path) as conn:
        node = conn.execute("SELECT last_status FROM nodes WHERE id = ?", (node_id,)).fetchone()
        assert node is not None
        assert node["last_status"] == "offline"

        offline_event = conn.execute(
            """
            SELECT ae.severity
            FROM alert_events ae
            JOIN alerts a ON a.id = ae.alert_id
            WHERE ae.node_id = ? AND a.key = 'offline' AND ae.resolved_at IS NULL
            ORDER BY ae.created_at DESC
            LIMIT 1
            """,
            (node_id,),
        ).fetchone()
        assert offline_event is not None
        assert offline_event["severity"] in {"warning", "critical"}


@pytest.mark.asyncio
async def test_record_failure_auth_keeps_node_online_and_sets_error(tmp_path: Path) -> None:
    db_path = tmp_path / "cluster.db"
    ensure_db(db_path)
    node_id = _seed_node(db_path, last_seen_at=datetime.now(timezone.utc).isoformat())
    poller = PollingService(db_path=db_path)

    await poller._record_failure(node_id=node_id, category="auth_failure", message="unauthorized", reachable=True)

    with get_conn(db_path) as conn:
        node = conn.execute(
            "SELECT last_status, last_error_category, last_heartbeat_at, consecutive_failures FROM nodes WHERE id = ?",
            (node_id,),
        ).fetchone()
        assert node is not None
        assert node["last_status"] == "online"
        assert node["last_error_category"] == "auth_failure"
        assert node["last_heartbeat_at"] is not None
        assert node["consecutive_failures"] >= 1


@pytest.mark.asyncio
async def test_retention_cleanup_deletes_old_rows(tmp_path: Path) -> None:
    db_path = tmp_path / "cluster.db"
    ensure_db(db_path)
    node_id = _seed_node(db_path)
    poller = PollingService(
        db_path=db_path,
        metric_retention_hours=1,
        alert_event_retention_days=1,
        service_retention_days=1,
        cleanup_interval_seconds=1,
    )

    old = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
    with get_conn(db_path) as conn:
        conn.execute(
            """
            INSERT INTO metric_samples (
                node_id, collected_at, cpu_percent, memory_percent, disk_percent, temperature_c,
                uptime_seconds, load_1, load_5, load_15, rx_bytes, tx_bytes, raw_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (node_id, old, 1, 1, 1, 1, 1, 0, 0, 0, 1, 1, "{}"),
        )
        alert_id = conn.execute("SELECT id FROM alerts WHERE key = 'cpu'").fetchone()["id"]
        conn.execute(
            """
            INSERT INTO alert_events (node_id, alert_id, severity, message, metric_value, created_at, resolved_at)
            VALUES (?, ?, 'warning', 'old', 80, ?, ?)
            """,
            (node_id, alert_id, old, old),
        )
        conn.execute(
            "INSERT INTO services (node_id, name, status, checked_at) VALUES (?, ?, ?, ?)",
            (node_id, "ssh", "active", old),
        )

    await poller._run_retention_cleanup_if_due()

    with get_conn(db_path) as conn:
        assert conn.execute("SELECT COUNT(*) AS c FROM metric_samples").fetchone()["c"] == 0
        assert conn.execute("SELECT COUNT(*) AS c FROM alert_events").fetchone()["c"] == 0
        assert conn.execute("SELECT COUNT(*) AS c FROM services").fetchone()["c"] == 0


@pytest.mark.asyncio
async def test_circuit_breaker_skips_polling_while_open(monkeypatch, tmp_path: Path) -> None:
    db_path = tmp_path / "cluster.db"
    ensure_db(db_path)
    node_id = _seed_node(db_path)
    poller = PollingService(db_path=db_path, poll_failure_threshold=1, poll_circuit_cooldown_seconds=60)
    poller._node_runtime[node_id] = {"failures": 1, "circuit_until": datetime.now(timezone.utc).timestamp() + 60}

    called = {"count": 0}

    async def fake_poll_node(client: httpx.AsyncClient, node: dict):  # noqa: ARG001
        called["count"] += 1

    monkeypatch.setattr(poller, "_poll_node", fake_poll_node)

    async with httpx.AsyncClient(timeout=httpx.Timeout(1)) as client:
        await poller._poll_due_nodes(client)

    assert called["count"] == 0


@pytest.mark.asyncio
async def test_poll_node_uses_https_when_tls_enabled(monkeypatch, tmp_path: Path) -> None:
    db_path = tmp_path / "cluster.db"
    ensure_db(db_path)
    node_id = _seed_node(db_path)
    poller = PollingService(db_path=db_path)

    calls: list[str] = []

    class FakeResp:
        def __init__(self, status_code: int, payload: dict | None = None) -> None:
            self.status_code = status_code
            self._payload = payload or {}

        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return self._payload

    async def fake_get(self, url: str, headers: dict[str, str] | None = None, **kwargs):  # noqa: ARG001
        calls.append(url)
        if url.endswith("/health"):
            return FakeResp(200, {"status": "ok"})
        if url.endswith("/api/v1/metrics"):
            return FakeResp(
                200,
                {
                    "hostname": "pi-1",
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "cpu_percent": 1.0,
                    "memory_percent": 2.0,
                    "disk_percent": 3.0,
                    "temperature_c": 30.0,
                    "uptime_seconds": 10,
                    "load_1": 0.1,
                    "load_5": 0.2,
                    "load_15": 0.3,
                    "rx_bytes": 1,
                    "tx_bytes": 2,
                },
            )
        return FakeResp(404, {})

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)

    node = {
        "id": node_id,
        "ip_address": "10.0.0.10",
        "token": "token",
        "agent_port": 8001,
        "use_tls": 1,
        "tls_verify": 1,
        "tls_ca_path": "",
    }
    async with httpx.AsyncClient(timeout=httpx.Timeout(1)) as client:
        await poller._poll_node(client, node)

    assert any(url.startswith("https://10.0.0.10:8001/health") for url in calls)


@pytest.mark.asyncio
async def test_poll_node_missing_tls_ca_path_sets_config_error(tmp_path: Path) -> None:
    db_path = tmp_path / "cluster.db"
    ensure_db(db_path)
    node_id = _seed_node(db_path)
    poller = PollingService(db_path=db_path)

    node = {
        "id": node_id,
        "ip_address": "10.0.0.10",
        "token": "token",
        "agent_port": 8001,
        "use_tls": 1,
        "tls_verify": 1,
        "tls_ca_path": str(tmp_path / "missing-ca.pem"),
    }
    async with httpx.AsyncClient(timeout=httpx.Timeout(1)) as client:
        await poller._poll_node(client, node)

    with get_conn(db_path) as conn:
        row = conn.execute(
            "SELECT last_error_category, last_error_message FROM nodes WHERE id = ?",
            (node_id,),
        ).fetchone()
        assert row is not None
        assert row["last_error_category"] == "tls_config_error"
        assert "tls_ca_path_not_found" in row["last_error_message"]


def test_normalize_fingerprint() -> None:
    assert PollingService._normalize_fingerprint("AA:bb:CC") == "aabbcc"


@pytest.mark.asyncio
async def test_poll_node_fingerprint_mismatch_sets_tls_verify_error(monkeypatch, tmp_path: Path) -> None:
    db_path = tmp_path / "cluster.db"
    ensure_db(db_path)
    node_id = _seed_node(db_path)
    poller = PollingService(db_path=db_path)
    monkeypatch.setattr(poller, "_fetch_tls_fingerprint", lambda host, port: asyncio.sleep(0, result="deadbeef"))

    node = {
        "id": node_id,
        "ip_address": "10.0.0.10",
        "token": "token",
        "agent_port": 8001,
        "use_tls": 1,
        "tls_verify": 1,
        "tls_ca_path": "",
        "tls_fingerprint_sha256": "ab:cd",
    }
    async with httpx.AsyncClient(timeout=httpx.Timeout(1)) as client:
        await poller._poll_node(client, node)

    with get_conn(db_path) as conn:
        row = conn.execute(
            "SELECT last_error_category, last_error_message FROM nodes WHERE id = ?",
            (node_id,),
        ).fetchone()
        assert row is not None
        assert row["last_error_category"] == "tls_verify_error"
        assert "fingerprint_mismatch" in row["last_error_message"]
