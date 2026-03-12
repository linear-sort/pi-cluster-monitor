from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sqlite3
from typing import Any, Iterator


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def ensure_db(db_path: Path) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as conn:
        conn.execute("PRAGMA foreign_keys = ON;")
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS nodes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                hostname TEXT NOT NULL,
                ip_address TEXT NOT NULL,
                token TEXT NOT NULL,
                token_issued_at TEXT NULL,
                token_expires_at TEXT NULL,
                previous_token TEXT NULL,
                previous_token_expires_at TEXT NULL,
                role TEXT NOT NULL DEFAULT 'worker',
                agent_port INTEGER NOT NULL DEFAULT 8001,
                use_tls INTEGER NOT NULL DEFAULT 0,
                tls_verify INTEGER NOT NULL DEFAULT 1,
                tls_ca_path TEXT NULL,
                tls_fingerprint_sha256 TEXT NULL,
                poll_interval_seconds INTEGER NOT NULL DEFAULT 10,
                enabled INTEGER NOT NULL DEFAULT 1,
                enrollment_status TEXT NOT NULL DEFAULT 'manual',
                enrolled_at TEXT NULL,
                last_heartbeat_at TEXT NULL,
                last_error_category TEXT NULL,
                last_error_message TEXT NULL,
                last_poll_error_at TEXT NULL,
                consecutive_failures INTEGER NOT NULL DEFAULT 0,
                last_seen_at TEXT NULL,
                last_status TEXT NOT NULL DEFAULT 'unknown',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS metric_samples (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                node_id INTEGER NOT NULL,
                collected_at TEXT NOT NULL,
                cpu_percent REAL NOT NULL,
                memory_percent REAL NOT NULL,
                disk_percent REAL NOT NULL,
                temperature_c REAL NULL,
                uptime_seconds INTEGER NOT NULL,
                load_1 REAL NOT NULL,
                load_5 REAL NOT NULL,
                load_15 REAL NOT NULL,
                rx_bytes INTEGER NOT NULL,
                tx_bytes INTEGER NOT NULL,
                raw_json TEXT NULL,
                FOREIGN KEY(node_id) REFERENCES nodes(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS alerts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                key TEXT UNIQUE NOT NULL,
                metric TEXT NOT NULL,
                warning_threshold REAL NOT NULL,
                critical_threshold REAL NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 1
            );

            CREATE TABLE IF NOT EXISTS alert_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                node_id INTEGER NOT NULL,
                alert_id INTEGER NOT NULL,
                severity TEXT NOT NULL,
                message TEXT NOT NULL,
                metric_value REAL NULL,
                created_at TEXT NOT NULL,
                resolved_at TEXT NULL,
                FOREIGN KEY(node_id) REFERENCES nodes(id) ON DELETE CASCADE,
                FOREIGN KEY(alert_id) REFERENCES alerts(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS services (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                node_id INTEGER NOT NULL,
                name TEXT NOT NULL,
                status TEXT NOT NULL,
                checked_at TEXT NOT NULL,
                FOREIGN KEY(node_id) REFERENCES nodes(id) ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_metric_samples_node_time ON metric_samples(node_id, collected_at);
            CREATE INDEX IF NOT EXISTS idx_alert_events_node_time ON alert_events(node_id, created_at);
            """
        )
        _ensure_nodes_columns(conn)

        defaults = [
            ("cpu", "cpu_percent", 75, 90),
            ("memory", "memory_percent", 80, 92),
            ("disk", "disk_percent", 80, 95),
            ("temperature", "temperature_c", 65, 80),
            ("offline", "offline_seconds", 30, 90),
        ]
        for key, metric, warn, crit in defaults:
            conn.execute(
                """
                INSERT OR IGNORE INTO alerts (key, metric, warning_threshold, critical_threshold, enabled)
                VALUES (?, ?, ?, ?, 1)
                """,
                (key, metric, warn, crit),
            )


def _ensure_nodes_columns(conn: sqlite3.Connection) -> None:
    columns = {
        row[1]
        for row in conn.execute("PRAGMA table_info(nodes)").fetchall()
    }
    if "enrollment_status" not in columns:
        conn.execute("ALTER TABLE nodes ADD COLUMN enrollment_status TEXT NOT NULL DEFAULT 'manual'")
    if "enrolled_at" not in columns:
        conn.execute("ALTER TABLE nodes ADD COLUMN enrolled_at TEXT NULL")
    if "token_issued_at" not in columns:
        conn.execute("ALTER TABLE nodes ADD COLUMN token_issued_at TEXT NULL")
    if "token_expires_at" not in columns:
        conn.execute("ALTER TABLE nodes ADD COLUMN token_expires_at TEXT NULL")
    if "previous_token" not in columns:
        conn.execute("ALTER TABLE nodes ADD COLUMN previous_token TEXT NULL")
    if "previous_token_expires_at" not in columns:
        conn.execute("ALTER TABLE nodes ADD COLUMN previous_token_expires_at TEXT NULL")
    if "last_heartbeat_at" not in columns:
        conn.execute("ALTER TABLE nodes ADD COLUMN last_heartbeat_at TEXT NULL")
    if "last_error_category" not in columns:
        conn.execute("ALTER TABLE nodes ADD COLUMN last_error_category TEXT NULL")
    if "last_error_message" not in columns:
        conn.execute("ALTER TABLE nodes ADD COLUMN last_error_message TEXT NULL")
    if "last_poll_error_at" not in columns:
        conn.execute("ALTER TABLE nodes ADD COLUMN last_poll_error_at TEXT NULL")
    if "consecutive_failures" not in columns:
        conn.execute("ALTER TABLE nodes ADD COLUMN consecutive_failures INTEGER NOT NULL DEFAULT 0")
    if "use_tls" not in columns:
        conn.execute("ALTER TABLE nodes ADD COLUMN use_tls INTEGER NOT NULL DEFAULT 0")
    if "tls_verify" not in columns:
        conn.execute("ALTER TABLE nodes ADD COLUMN tls_verify INTEGER NOT NULL DEFAULT 1")
    if "tls_ca_path" not in columns:
        conn.execute("ALTER TABLE nodes ADD COLUMN tls_ca_path TEXT NULL")
    if "tls_fingerprint_sha256" not in columns:
        conn.execute("ALTER TABLE nodes ADD COLUMN tls_fingerprint_sha256 TEXT NULL")


def token_expiry_iso(ttl_seconds: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=max(60, ttl_seconds))).isoformat()


@contextmanager
def get_conn(db_path: Path) -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def fetch_all_dict(conn: sqlite3.Connection, query: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    rows = conn.execute(query, params).fetchall()
    return [dict(row) for row in rows]


def fetch_one_dict(conn: sqlite3.Connection, query: str, params: tuple[Any, ...] = ()) -> dict[str, Any] | None:
    row = conn.execute(query, params).fetchone()
    return dict(row) if row else None
