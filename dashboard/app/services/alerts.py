from __future__ import annotations

from datetime import datetime, timezone
import sqlite3


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _severity(value: float | None, warning: float, critical: float) -> str:
    if value is None:
        return "info"
    if value >= critical:
        return "critical"
    if value >= warning:
        return "warning"
    return "info"


def _upsert_alert_event(
    conn: sqlite3.Connection,
    node_id: int,
    alert_id: int,
    severity: str,
    message: str,
    metric_value: float | None,
) -> None:
    open_event = conn.execute(
        """
        SELECT id, severity
        FROM alert_events
        WHERE node_id = ? AND alert_id = ? AND resolved_at IS NULL
        ORDER BY created_at DESC
        LIMIT 1
        """,
        (node_id, alert_id),
    ).fetchone()

    if severity == "info":
        conn.execute(
            """
            UPDATE alert_events
            SET resolved_at = ?
            WHERE node_id = ? AND alert_id = ? AND resolved_at IS NULL
            """,
            (_utc_now_iso(), node_id, alert_id),
        )
        return

    if open_event and open_event["severity"] == severity:
        return

    conn.execute(
        """
        INSERT INTO alert_events (node_id, alert_id, severity, message, metric_value, created_at, resolved_at)
        VALUES (?, ?, ?, ?, ?, ?, NULL)
        """,
        (node_id, alert_id, severity, message, metric_value, _utc_now_iso()),
    )


def evaluate_metric_thresholds(conn: sqlite3.Connection, node_id: int, metrics: dict[str, float | int | None]) -> None:
    thresholds = conn.execute(
        """
        SELECT id, key, metric, warning_threshold, critical_threshold
        FROM alerts
        WHERE enabled = 1 AND key != 'offline'
        """
    ).fetchall()

    for item in thresholds:
        metric_name = item["metric"]
        value = metrics.get(metric_name)
        value_float = float(value) if value is not None else None
        sev = _severity(value_float, item["warning_threshold"], item["critical_threshold"])
        msg = f"{metric_name}={value_float if value_float is not None else 'n/a'}"
        _upsert_alert_event(conn, node_id, item["id"], sev, msg, value_float)


def evaluate_offline_alert(conn: sqlite3.Connection, node_id: int, offline_seconds: float) -> None:
    threshold = conn.execute(
        """
        SELECT id, warning_threshold, critical_threshold
        FROM alerts
        WHERE key = 'offline' AND enabled = 1
        LIMIT 1
        """
    ).fetchone()

    if not threshold:
        return

    sev = _severity(offline_seconds, threshold["warning_threshold"], threshold["critical_threshold"])
    msg = f"offline_seconds={offline_seconds:.0f}"
    _upsert_alert_event(conn, node_id, threshold["id"], sev, msg, offline_seconds)
