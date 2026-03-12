from __future__ import annotations

from pathlib import Path

import pytest

from app.db import ensure_db, get_conn, utc_now_iso
from app.services.alerts import evaluate_metric_thresholds

pytestmark = pytest.mark.unit


def _seed_node(db_path: Path) -> int:
    with get_conn(db_path) as conn:
        cursor = conn.execute(
            """
            INSERT INTO nodes (
                name, hostname, ip_address, token, role,
                agent_port, poll_interval_seconds, enabled, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            ("pi-1", "pi-1.local", "10.0.0.10", "token", "worker", 8001, 10, 1, utc_now_iso(), utc_now_iso()),
        )
        return int(cursor.lastrowid)


def test_threshold_alert_creates_and_resolves_events(tmp_path: Path) -> None:
    db_path = tmp_path / "cluster.db"
    ensure_db(db_path)
    node_id = _seed_node(db_path)

    with get_conn(db_path) as conn:
        evaluate_metric_thresholds(
            conn,
            node_id,
            {
                "cpu_percent": 95.0,
                "memory_percent": 40.0,
                "disk_percent": 20.0,
                "temperature_c": 30.0,
            },
        )
        open_cpu = conn.execute(
            """
            SELECT ae.severity, a.key
            FROM alert_events ae
            JOIN alerts a ON a.id = ae.alert_id
            WHERE ae.node_id = ? AND a.key = 'cpu' AND ae.resolved_at IS NULL
            """,
            (node_id,),
        ).fetchone()
        assert open_cpu is not None
        assert open_cpu["severity"] == "critical"

        evaluate_metric_thresholds(
            conn,
            node_id,
            {
                "cpu_percent": 15.0,
                "memory_percent": 35.0,
                "disk_percent": 20.0,
                "temperature_c": 30.0,
            },
        )
        resolved_cpu = conn.execute(
            """
            SELECT ae.resolved_at
            FROM alert_events ae
            JOIN alerts a ON a.id = ae.alert_id
            WHERE ae.node_id = ? AND a.key = 'cpu'
            ORDER BY ae.created_at DESC
            LIMIT 1
            """,
            (node_id,),
        ).fetchone()
        assert resolved_cpu is not None
        assert resolved_cpu["resolved_at"] is not None
