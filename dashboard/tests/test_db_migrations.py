from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from app.db import ensure_db

pytestmark = pytest.mark.unit


def test_ensure_db_creates_security_and_nonce_tables(tmp_path: Path) -> None:
    db_path = tmp_path / "cluster.db"
    ensure_db(db_path)

    with sqlite3.connect(db_path) as conn:
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert "ingest_nonces" in tables
        assert "security_audit_events" in tables


def test_ensure_db_migrates_legacy_nodes_columns(tmp_path: Path) -> None:
    db_path = tmp_path / "legacy.db"
    with sqlite3.connect(db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE nodes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                hostname TEXT NOT NULL,
                ip_address TEXT NOT NULL,
                token TEXT NOT NULL,
                role TEXT NOT NULL DEFAULT 'worker',
                poll_interval_seconds INTEGER NOT NULL DEFAULT 10,
                enabled INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            """
        )

    ensure_db(db_path)

    with sqlite3.connect(db_path) as conn:
        columns = {
            row[1]
            for row in conn.execute("PRAGMA table_info(nodes)").fetchall()
        }
        assert "token_version" in columns
        assert "revoked_at" in columns
        assert "revoked_reason" in columns
        assert "revoked_by" in columns
        assert "collect_mode" in columns
        assert "agent_id" in columns

        indexes = {
            row[1]
            for row in conn.execute("PRAGMA index_list(nodes)").fetchall()
        }
        assert "idx_nodes_agent_id_unique" in indexes

