from __future__ import annotations

import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.db import get_conn, utc_now_iso
from app.main import app

pytestmark = pytest.mark.integration


def _seed_node_with_data(db_path: Path) -> int:
    now_iso = utc_now_iso()
    with get_conn(db_path) as conn:
        node_id = int(
            conn.execute(
                """
                INSERT INTO nodes (
                    name, hostname, ip_address, token, role,
                    agent_port, poll_interval_seconds, enabled, last_seen_at, last_status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                ("pi-1", "pi-1.local", "10.0.0.10", "token-1", "worker", 8001, 10, 1, now_iso, "online", now_iso, now_iso),
            ).lastrowid
        )

        conn.execute(
            """
            INSERT INTO metric_samples (
                node_id, collected_at, cpu_percent, memory_percent, disk_percent, temperature_c,
                uptime_seconds, load_1, load_5, load_15, rx_bytes, tx_bytes, raw_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (node_id, now_iso, 21.0, 42.0, 31.0, 49.0, 1000, 0.1, 0.2, 0.3, 100, 200, "{}"),
        )

        alert_id = conn.execute("SELECT id FROM alerts WHERE key = 'cpu'").fetchone()["id"]
        conn.execute(
            """
            INSERT INTO alert_events (node_id, alert_id, severity, message, metric_value, created_at, resolved_at)
            VALUES (?, ?, ?, ?, ?, ?, NULL)
            """,
            (node_id, alert_id, "warning", "cpu_percent=85", 85.0, now_iso),
        )

        conn.execute(
            "INSERT INTO services (node_id, name, status, checked_at) VALUES (?, ?, ?, ?)",
            (node_id, "ssh", "active", now_iso),
        )
    return node_id


def test_cluster_summary_and_nodes_api(tmp_path: Path) -> None:
    db_path = tmp_path / "cluster.db"
    os.environ["DASHBOARD_DB_PATH"] = str(db_path)
    os.environ["DASHBOARD_POLL_BASE_SECONDS"] = "1"

    with TestClient(app) as client:
        node_id = _seed_node_with_data(db_path)

        summary = client.get("/api/v1/cluster/summary")
        assert summary.status_code == 200
        summary_payload = summary.json()
        assert summary_payload["total_nodes"] == 1
        assert summary_payload["online_nodes"] == 1
        assert summary_payload["warning_nodes"] >= 1

        nodes = client.get("/api/v1/nodes")
        assert nodes.status_code == 200
        nodes_payload = nodes.json()["nodes"]
        assert len(nodes_payload) == 1
        assert nodes_payload[0]["id"] == node_id
        assert nodes_payload[0]["cpu_percent"] == 21.0


def test_node_detail_metrics_and_alerts_api(tmp_path: Path) -> None:
    db_path = tmp_path / "cluster.db"
    os.environ["DASHBOARD_DB_PATH"] = str(db_path)
    os.environ["DASHBOARD_POLL_BASE_SECONDS"] = "1"

    with TestClient(app) as client:
        node_id = _seed_node_with_data(db_path)

        node_detail = client.get(f"/api/v1/nodes/{node_id}")
        assert node_detail.status_code == 200
        payload = node_detail.json()
        assert payload["node"]["name"] == "pi-1"
        assert payload["latest"]["memory_percent"] == 42.0
        assert len(payload["alerts"]) >= 1
        assert len(payload["services"]) == 1

        metrics = client.get(f"/api/v1/nodes/{node_id}/metrics?minutes=120")
        assert metrics.status_code == 200
        metrics_payload = metrics.json()
        assert len(metrics_payload["labels"]) == 1
        assert metrics_payload["cpu"] == [21.0]

        alerts = client.get("/api/v1/alerts?unresolved_only=true&limit=20")
        assert alerts.status_code == 200
        alert_payload = alerts.json()["alerts"]
        assert len(alert_payload) >= 1
        assert alert_payload[0]["node_id"] == node_id


def test_settings_save_enabled_false_string(tmp_path: Path) -> None:
    db_path = tmp_path / "cluster.db"
    os.environ["DASHBOARD_DB_PATH"] = str(db_path)
    os.environ["DASHBOARD_POLL_BASE_SECONDS"] = "1"

    with TestClient(app) as client:
        response = client.post(
            "/settings/nodes/save",
            data={
                "name": "pi-2",
                "hostname": "pi-2.local",
                "ip_address": "10.0.0.11",
                "token": "token-2",
                "role": "worker",
                "agent_port": 8001,
                "poll_interval_seconds": 10,
                "enabled": "false",
            },
            follow_redirects=False,
        )
        assert response.status_code == 303

    with get_conn(db_path) as conn:
        node = conn.execute("SELECT enabled FROM nodes WHERE name = 'pi-2'").fetchone()
        assert node is not None
        assert node["enabled"] == 0
