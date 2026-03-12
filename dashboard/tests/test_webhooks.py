from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from app.db import ensure_db, get_conn, utc_now_iso
from app.services.alerts import evaluate_metric_thresholds
from app.services.poller import PollingService

pytestmark = pytest.mark.unit


def _seed_node(db_path: Path) -> int:
    with get_conn(db_path) as conn:
        return int(
            conn.execute(
                """
                INSERT INTO nodes (name, hostname, ip_address, token, role, poll_interval_seconds, enabled, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                ("pi-hook", "pi-hook.local", "10.0.0.80", "tok", "worker", 10, 1, utc_now_iso(), utc_now_iso()),
            ).lastrowid
        )


def test_alert_threshold_enqueues_webhook_delivery(tmp_path: Path) -> None:
    db_path = tmp_path / "cluster.db"
    ensure_db(db_path)
    node_id = _seed_node(db_path)

    with get_conn(db_path) as conn:
        evaluate_metric_thresholds(
            conn,
            node_id,
            {
                "cpu_percent": 99.0,
                "memory_percent": 10.0,
                "disk_percent": 10.0,
                "temperature_c": 30.0,
            },
        )
        row = conn.execute(
            "SELECT status, attempt_count FROM webhook_deliveries ORDER BY id DESC LIMIT 1"
        ).fetchone()
        assert row is not None
        assert row["status"] == "pending"
        assert row["attempt_count"] == 0


@pytest.mark.asyncio
async def test_webhook_dispatch_marks_delivery_delivered(monkeypatch, tmp_path: Path) -> None:
    db_path = tmp_path / "cluster.db"
    ensure_db(db_path)
    node_id = _seed_node(db_path)
    now_iso = utc_now_iso()

    with get_conn(db_path) as conn:
        alert_id = conn.execute("SELECT id FROM alerts WHERE key = 'cpu' LIMIT 1").fetchone()["id"]
        event_id = int(
            conn.execute(
                """
                INSERT INTO alert_events (node_id, alert_id, severity, message, metric_value, created_at, resolved_at)
                VALUES (?, ?, 'critical', 'cpu_percent=99', 99.0, ?, NULL)
                """,
                (node_id, alert_id, now_iso),
            ).lastrowid
        )
        conn.execute(
            """
            INSERT INTO webhook_deliveries (
                alert_event_id, status, attempt_count, next_attempt_at, delivered_at, last_error, created_at, updated_at
            ) VALUES (?, 'pending', 0, ?, NULL, NULL, ?, ?)
            """,
            (event_id, now_iso, now_iso, now_iso),
        )

    class FakeResponse:
        status_code = 200

        def raise_for_status(self) -> None:
            return None

    async def fake_post(self, url: str, json: dict):  # noqa: ARG001
        return FakeResponse()

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
    poller = PollingService(
        db_path=db_path,
        webhook_url="http://localhost:9999/webhook",
        webhook_dispatch_interval_seconds=1,
    )
    await poller._dispatch_webhooks_if_due()

    with get_conn(db_path) as conn:
        row = conn.execute(
            "SELECT status, delivered_at, last_error FROM webhook_deliveries ORDER BY id DESC LIMIT 1"
        ).fetchone()
        assert row is not None
        assert row["status"] == "delivered"
        assert row["delivered_at"] is not None
        assert row["last_error"] is None


@pytest.mark.asyncio
async def test_webhook_dispatch_failure_transitions_to_failed(monkeypatch, tmp_path: Path) -> None:
    db_path = tmp_path / "cluster.db"
    ensure_db(db_path)
    node_id = _seed_node(db_path)
    now_iso = utc_now_iso()

    with get_conn(db_path) as conn:
        alert_id = conn.execute("SELECT id FROM alerts WHERE key = 'cpu' LIMIT 1").fetchone()["id"]
        event_id = int(
            conn.execute(
                """
                INSERT INTO alert_events (node_id, alert_id, severity, message, metric_value, created_at, resolved_at)
                VALUES (?, ?, 'critical', 'cpu_percent=99', 99.0, ?, NULL)
                """,
                (node_id, alert_id, now_iso),
            ).lastrowid
        )
        conn.execute(
            """
            INSERT INTO webhook_deliveries (
                alert_event_id, status, attempt_count, next_attempt_at, delivered_at, last_error, created_at, updated_at
            ) VALUES (?, 'pending', 0, ?, NULL, NULL, ?, ?)
            """,
            (event_id, now_iso, now_iso, now_iso),
        )

    async def fake_post(self, url: str, json: dict):  # noqa: ARG001
        req = httpx.Request("POST", "http://localhost:9999/webhook")
        raise httpx.ConnectError("network_down", request=req)

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
    poller = PollingService(
        db_path=db_path,
        webhook_url="http://localhost:9999/webhook",
        webhook_dispatch_interval_seconds=1,
        webhook_max_attempts=3,
        webhook_retry_base_seconds=3,
    )
    await poller._dispatch_webhooks_if_due()

    with get_conn(db_path) as conn:
        row = conn.execute(
            "SELECT status, attempt_count, last_error FROM webhook_deliveries ORDER BY id DESC LIMIT 1"
        ).fetchone()
        assert row is not None
        assert row["status"] == "failed"
        assert row["attempt_count"] == 1
        assert "network_down" in str(row["last_error"])
