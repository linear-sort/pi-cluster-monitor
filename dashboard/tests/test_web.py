from __future__ import annotations

import hashlib
import hmac
import os
import json
from pathlib import Path
import time

import pytest
from fastapi.testclient import TestClient

from app.db import ensure_db, get_conn, utc_now_iso
from app.main import app

pytestmark = pytest.mark.integration


def _set_operator_creds() -> None:
    os.environ["DASHBOARD_OPERATOR_CREDENTIALS"] = ",".join(
        [
            "admin-token:alice-admin:admin",
            "operator-token:bob-operator:operator",
            "viewer-token:victor-viewer:viewer",
        ]
    )


def _op_headers(token: str) -> dict[str, str]:
    return {"X-PCM-Operator-Token": token, "X-Request-ID": "req-test-123"}


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


def test_settings_save_tls_flags(tmp_path: Path) -> None:
    db_path = tmp_path / "cluster.db"
    os.environ["DASHBOARD_DB_PATH"] = str(db_path)
    os.environ["DASHBOARD_POLL_BASE_SECONDS"] = "1"

    with TestClient(app) as client:
        response = client.post(
            "/settings/nodes/save",
            data={
                "name": "pi-tls",
                "hostname": "pi-tls.local",
                "ip_address": "10.0.0.12",
                "token": "token-tls",
                "role": "worker",
                "agent_port": 8001,
                "use_tls": "true",
                "tls_verify": "false",
                "tls_ca_path": "/etc/ssl/certs/custom-ca.pem",
                "tls_fingerprint_sha256": "aa:bb:cc",
                "collect_mode": "push",
                "poll_interval_seconds": 10,
                "enabled": "true",
            },
            follow_redirects=False,
        )
        assert response.status_code == 303

    with get_conn(db_path) as conn:
        node = conn.execute(
            "SELECT use_tls, tls_verify, tls_ca_path, tls_fingerprint_sha256, collect_mode FROM nodes WHERE hostname = 'pi-tls.local'"
        ).fetchone()
        assert node is not None
        assert node["use_tls"] == 1
        assert node["tls_verify"] == 0
        assert node["tls_ca_path"] == "/etc/ssl/certs/custom-ca.pem"
        assert node["tls_fingerprint_sha256"] == "aa:bb:cc"
        assert node["collect_mode"] == "push"


def test_agent_enrollment_creates_or_updates_node(tmp_path: Path) -> None:
    db_path = tmp_path / "cluster.db"
    os.environ["DASHBOARD_DB_PATH"] = str(db_path)
    os.environ["DASHBOARD_ENROLL_SECRET"] = "enroll-secret"

    with TestClient(app) as client:
        create_resp = client.post(
            "/api/v1/enroll",
            json={
                "enroll_secret": "enroll-secret",
                "hostname": "pi-auto.local",
                "agent_id": "agent-auto-1",
                "name": "pi-auto",
                "ip_address": "10.0.0.50",
                "agent_port": 8001,
                "role": "worker",
                "poll_interval_seconds": 10,
            },
        )
        assert create_resp.status_code == 200
        create_payload = create_resp.json()
        assert create_payload["status"] == "enrolled"
        assert create_payload["token"]

        update_resp = client.post(
            "/api/v1/enroll",
            json={
                "enroll_secret": "enroll-secret",
                "hostname": "pi-auto.local",
                "agent_id": "agent-auto-1",
                "name": "pi-auto-renamed",
                "ip_address": "10.0.0.50",
                "agent_port": 8001,
                "role": "worker",
                "poll_interval_seconds": 15,
            },
        )
        assert update_resp.status_code == 200
        update_payload = update_resp.json()
        assert update_payload["node_id"] == create_payload["node_id"]
        assert update_payload["token"] != create_payload["token"]
        assert update_payload["agent_id"] == "agent-auto-1"

        bad_resp = client.post(
            "/api/v1/enroll",
            json={"enroll_secret": "bad", "hostname": "pi-bad"},
        )
        assert bad_resp.status_code == 401

    with get_conn(db_path) as conn:
        row = conn.execute(
            "SELECT name, agent_id, enrollment_status, enrolled_at, poll_interval_seconds FROM nodes WHERE hostname = ?",
            ("pi-auto.local",),
        ).fetchone()
        assert row is not None
        assert row["name"] == "pi-auto-renamed"
        assert row["agent_id"] == "agent-auto-1"
        assert row["enrollment_status"] == "enrolled"
        assert row["enrolled_at"] is not None
        assert row["poll_interval_seconds"] == 15


def test_token_refresh_rotates_node_token(tmp_path: Path) -> None:
    db_path = tmp_path / "cluster.db"
    os.environ["DASHBOARD_DB_PATH"] = str(db_path)
    os.environ["DASHBOARD_ENROLL_SECRET"] = "enroll-secret"
    os.environ["DASHBOARD_TOKEN_TTL_SECONDS"] = "3600"
    os.environ["DASHBOARD_TOKEN_GRACE_SECONDS"] = "120"

    with TestClient(app) as client:
        enroll_resp = client.post(
            "/api/v1/enroll",
            json={
                "enroll_secret": "enroll-secret",
                "hostname": "pi-refresh.local",
                "agent_id": "agent-refresh-1",
                "name": "pi-refresh",
                "ip_address": "10.0.0.51",
                "agent_port": 8001,
                "role": "worker",
                "poll_interval_seconds": 10,
            },
        )
        assert enroll_resp.status_code == 200
        old_token = enroll_resp.json()["token"]

        refresh_resp = client.post(
            "/api/v1/token/refresh",
            json={"hostname": "pi-refresh.local", "agent_id": "agent-refresh-1"},
            headers={"Authorization": f"Bearer {old_token}"},
        )
        assert refresh_resp.status_code == 200
        payload = refresh_resp.json()
        assert payload["status"] == "refreshed"
        assert payload["token"] != old_token
        assert int(payload["token_version"]) >= 1
        assert payload["expires_at"]

        # Old token should still be accepted briefly via grace window.
        grace_resp = client.post(
            "/api/v1/token/refresh",
            json={"hostname": "pi-refresh.local", "agent_id": "agent-refresh-1"},
            headers={"Authorization": f"Bearer {old_token}"},
        )
        assert grace_resp.status_code == 200

        bad_resp = client.post(
            "/api/v1/token/refresh",
            json={"hostname": "pi-refresh.local", "agent_id": "agent-refresh-1"},
            headers={"Authorization": "Bearer invalid-token"},
        )
        assert bad_resp.status_code == 401


def test_token_refresh_rejects_version_mismatch(tmp_path: Path) -> None:
    db_path = tmp_path / "cluster.db"
    os.environ["DASHBOARD_DB_PATH"] = str(db_path)
    os.environ["DASHBOARD_ENROLL_SECRET"] = "enroll-secret"

    with TestClient(app) as client:
        enroll_resp = client.post(
            "/api/v1/enroll",
            json={
                "enroll_secret": "enroll-secret",
                "hostname": "pi-version.local",
                "name": "pi-version",
                "ip_address": "10.0.0.60",
            },
        )
        assert enroll_resp.status_code == 200
        token = enroll_resp.json()["token"]
        token_version = int(enroll_resp.json()["token_version"])

        refresh_resp = client.post(
            "/api/v1/token/refresh",
            json={"hostname": "pi-version.local", "token_version": token_version + 1},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert refresh_resp.status_code == 401
        assert refresh_resp.json()["detail"] == "Token version mismatch"


def test_enrollment_rejects_agent_id_rebinding_conflict(tmp_path: Path) -> None:
    db_path = tmp_path / "cluster.db"
    os.environ["DASHBOARD_DB_PATH"] = str(db_path)
    os.environ["DASHBOARD_ENROLL_SECRET"] = "enroll-secret"

    with TestClient(app) as client:
        first = client.post(
            "/api/v1/enroll",
            json={
                "enroll_secret": "enroll-secret",
                "hostname": "pi-ident-a.local",
                "agent_id": "agent-shared",
                "ip_address": "10.0.0.71",
            },
        )
        assert first.status_code == 200

        second = client.post(
            "/api/v1/enroll",
            json={
                "enroll_secret": "enroll-secret",
                "hostname": "pi-ident-a.local",
                "agent_id": "agent-other",
                "ip_address": "10.0.0.71",
            },
        )
        assert second.status_code == 409
        assert second.json()["detail"] == "Agent identity conflict"


def test_token_refresh_rejects_agent_identity_mismatch(tmp_path: Path) -> None:
    db_path = tmp_path / "cluster.db"
    os.environ["DASHBOARD_DB_PATH"] = str(db_path)
    os.environ["DASHBOARD_ENROLL_SECRET"] = "enroll-secret"

    with TestClient(app) as client:
        enroll_resp = client.post(
            "/api/v1/enroll",
            json={
                "enroll_secret": "enroll-secret",
                "hostname": "pi-mismatch.local",
                "agent_id": "agent-mismatch-1",
                "ip_address": "10.0.0.72",
            },
        )
        assert enroll_resp.status_code == 200
        token = enroll_resp.json()["token"]

        refresh_resp = client.post(
            "/api/v1/token/refresh",
            json={"hostname": "pi-other.local", "agent_id": "agent-mismatch-1"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert refresh_resp.status_code == 409
        assert refresh_resp.json()["detail"] == "Identity binding mismatch"


def test_ingest_accepts_signed_payload_and_rejects_replay(tmp_path: Path) -> None:
    db_path = tmp_path / "cluster.db"
    os.environ["DASHBOARD_DB_PATH"] = str(db_path)
    os.environ["DASHBOARD_ENROLL_SECRET"] = "enroll-secret"

    with TestClient(app) as client:
        enroll_resp = client.post(
            "/api/v1/enroll",
            json={
                "enroll_secret": "enroll-secret",
                "hostname": "pi-push.local",
                "agent_id": "agent-push-1",
                "name": "pi-push",
                "ip_address": "10.0.0.52",
                "agent_port": 8001,
                "role": "worker",
                "poll_interval_seconds": 10,
            },
        )
        assert enroll_resp.status_code == 200
        token = enroll_resp.json()["token"]

        payload = {
            "hostname": "pi-push.local",
            "agent_id": "agent-push-1",
            "timestamp": utc_now_iso(),
            "cpu_percent": 10.0,
            "memory_percent": 20.0,
            "disk_percent": 30.0,
            "temperature_c": 40.0,
            "uptime_seconds": 100,
            "load_1": 0.1,
            "load_5": 0.2,
            "load_15": 0.3,
            "rx_bytes": 1000,
            "tx_bytes": 2000,
        }
        payload_json = json.dumps(payload, separators=(",", ":"), sort_keys=True)
        ts = str(int(time.time()))
        nonce = "nonce-1"
        signature = hmac.new(token.encode("utf-8"), f"{ts}.{nonce}.{payload_json}".encode("utf-8"), hashlib.sha256).hexdigest()
        headers = {
            "Authorization": f"Bearer {token}",
            "X-PCM-Timestamp": ts,
            "X-PCM-Nonce": nonce,
            "X-PCM-Signature": signature,
            "Content-Type": "application/json",
        }
        accepted = client.post("/api/v1/ingest", content=payload_json, headers=headers)
        assert accepted.status_code == 200
        assert accepted.json()["status"] == "accepted"

        replay = client.post("/api/v1/ingest", content=payload_json, headers=headers)
        assert replay.status_code == 409


def test_revoke_token_blocks_refresh_and_ingest_and_records_audit(tmp_path: Path) -> None:
    db_path = tmp_path / "cluster.db"
    os.environ["DASHBOARD_DB_PATH"] = str(db_path)
    os.environ["DASHBOARD_ENROLL_SECRET"] = "enroll-secret"
    _set_operator_creds()

    with TestClient(app) as client:
        enroll_resp = client.post(
            "/api/v1/enroll",
            json={
                "enroll_secret": "enroll-secret",
                "hostname": "pi-revoke.local",
                "agent_id": "agent-revoke-1",
                "name": "pi-revoke",
                "ip_address": "10.0.0.61",
            },
        )
        assert enroll_resp.status_code == 200
        token = enroll_resp.json()["token"]
        token_version = int(enroll_resp.json()["token_version"])
        node_id = int(enroll_resp.json()["node_id"])

        revoke_resp = client.post(
            f"/api/v1/nodes/{node_id}/token/revoke",
            json={"actor": "spoofed-user", "reason": "rotation-test"},
            headers=_op_headers("admin-token"),
        )
        assert revoke_resp.status_code == 200
        revoked_payload = revoke_resp.json()
        assert revoked_payload["status"] == "revoked"
        assert int(revoked_payload["token_version"]) == token_version + 1

        refresh_resp = client.post(
            "/api/v1/token/refresh",
            json={"hostname": "pi-revoke.local", "agent_id": "agent-revoke-1", "token_version": token_version},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert refresh_resp.status_code == 401
        assert refresh_resp.json()["detail"] == "Token revoked"

        payload = {
            "hostname": "pi-revoke.local",
            "agent_id": "agent-revoke-1",
            "timestamp": utc_now_iso(),
            "cpu_percent": 10.0,
            "memory_percent": 20.0,
            "disk_percent": 30.0,
            "temperature_c": 40.0,
            "uptime_seconds": 100,
            "load_1": 0.1,
            "load_5": 0.2,
            "load_15": 0.3,
            "rx_bytes": 1000,
            "tx_bytes": 2000,
        }
        payload_json = json.dumps(payload, separators=(",", ":"), sort_keys=True)
        ts = str(int(time.time()))
        nonce = "nonce-revoked"
        signature = hmac.new(token.encode("utf-8"), f"{ts}.{nonce}.{payload_json}".encode("utf-8"), hashlib.sha256).hexdigest()
        headers = {
            "Authorization": f"Bearer {token}",
            "X-PCM-Timestamp": ts,
            "X-PCM-Nonce": nonce,
            "X-PCM-Signature": signature,
            "X-PCM-Token-Version": str(token_version),
            "Content-Type": "application/json",
        }
        ingest_resp = client.post("/api/v1/ingest", content=payload_json, headers=headers)
        assert ingest_resp.status_code == 401

    with get_conn(db_path) as conn:
        node = conn.execute(
            "SELECT token, revoked_at, revoked_reason, revoked_by FROM nodes WHERE id = ?",
            (node_id,),
        ).fetchone()
        assert node is not None
        assert node["token"] == ""
        assert node["revoked_at"] is not None
        assert node["revoked_reason"] == "rotation-test"
        assert node["revoked_by"] == "alice-admin"

        audit = conn.execute(
            "SELECT event_type, actor, reason FROM security_audit_events WHERE node_id = ? ORDER BY id DESC LIMIT 1",
            (node_id,),
        ).fetchone()
        assert audit is not None
        assert audit["event_type"] == "token_revoked"
        assert audit["actor"] == "alice-admin"
        assert audit["reason"] == "rotation-test"


def test_bulk_update_nodes_applies_action_and_writes_audit(tmp_path: Path) -> None:
    db_path = tmp_path / "cluster.db"
    os.environ["DASHBOARD_DB_PATH"] = str(db_path)
    _set_operator_creds()
    ensure_db(db_path)

    now_iso = utc_now_iso()
    with get_conn(db_path) as conn:
        id1 = int(
            conn.execute(
                """
                INSERT INTO nodes (name, hostname, ip_address, token, role, poll_interval_seconds, enabled, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                ("pi-a", "pi-a.local", "10.0.0.21", "tok-a", "worker", 10, 1, now_iso, now_iso),
            ).lastrowid
        )
        id2 = int(
            conn.execute(
                """
                INSERT INTO nodes (name, hostname, ip_address, token, role, poll_interval_seconds, enabled, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                ("pi-b", "pi-b.local", "10.0.0.22", "tok-b", "worker", 10, 1, now_iso, now_iso),
            ).lastrowid
        )

    with TestClient(app) as client:
        resp = client.post(
            "/api/v1/nodes/bulk-update",
            json={
                "node_ids": [id1, id2],
                "action": "set_poll_interval",
                "poll_interval_seconds": 17,
                "actor": "spoofed-actor",
                "reason": "fleet-tune",
            },
            headers=_op_headers("operator-token"),
        )
        assert resp.status_code == 200
        payload = resp.json()
        assert payload["status"] == "updated"
        assert payload["affected_count"] == 2

    with get_conn(db_path) as conn:
        rows = conn.execute(
            "SELECT id, poll_interval_seconds FROM nodes WHERE id IN (?, ?) ORDER BY id",
            (id1, id2),
        ).fetchall()
        assert len(rows) == 2
        assert rows[0]["poll_interval_seconds"] == 17
        assert rows[1]["poll_interval_seconds"] == 17

        audits = conn.execute(
            """
            SELECT event_type, actor, reason
            FROM security_audit_events
            WHERE node_id IN (?, ?)
            ORDER BY id ASC
            """,
            (id1, id2),
        ).fetchall()
        assert len(audits) == 2
        assert all(a["event_type"] == "bulk_set_poll_interval" for a in audits)
        assert all(a["actor"] == "bob-operator" for a in audits)
        assert all(a["reason"] == "fleet-tune" for a in audits)


def test_bulk_update_nodes_rejects_invalid_action(tmp_path: Path) -> None:
    db_path = tmp_path / "cluster.db"
    os.environ["DASHBOARD_DB_PATH"] = str(db_path)
    _set_operator_creds()

    with TestClient(app) as client:
        resp = client.post(
            "/api/v1/nodes/bulk-update",
            json={"node_ids": [1], "action": "invalid-action"},
            headers=_op_headers("operator-token"),
        )
        assert resp.status_code == 400
        assert resp.json()["detail"] == "Unsupported bulk action"


def test_revoke_requires_auth_and_admin_role(tmp_path: Path) -> None:
    db_path = tmp_path / "cluster.db"
    os.environ["DASHBOARD_DB_PATH"] = str(db_path)
    _set_operator_creds()
    ensure_db(db_path)
    now_iso = utc_now_iso()
    with get_conn(db_path) as conn:
        node_id = int(
            conn.execute(
                """
                INSERT INTO nodes (name, hostname, ip_address, token, role, poll_interval_seconds, enabled, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                ("pi-sec", "pi-sec.local", "10.0.0.33", "tok", "worker", 10, 1, now_iso, now_iso),
            ).lastrowid
        )

    with TestClient(app) as client:
        unauth = client.post(f"/api/v1/nodes/{node_id}/token/revoke", json={"reason": "r"})
        assert unauth.status_code == 401

        forbidden = client.post(
            f"/api/v1/nodes/{node_id}/token/revoke",
            json={"reason": "r"},
            headers=_op_headers("operator-token"),
        )
        assert forbidden.status_code == 403


def test_bulk_update_requires_auth_and_allows_operator(tmp_path: Path) -> None:
    db_path = tmp_path / "cluster.db"
    os.environ["DASHBOARD_DB_PATH"] = str(db_path)
    _set_operator_creds()
    ensure_db(db_path)
    now_iso = utc_now_iso()
    with get_conn(db_path) as conn:
        node_id = int(
            conn.execute(
                """
                INSERT INTO nodes (name, hostname, ip_address, token, role, poll_interval_seconds, enabled, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                ("pi-bulk-auth", "pi-bulk-auth.local", "10.0.0.34", "tok", "worker", 10, 1, now_iso, now_iso),
            ).lastrowid
        )

    with TestClient(app) as client:
        unauth = client.post(
            "/api/v1/nodes/bulk-update",
            json={"node_ids": [node_id], "action": "set_enabled", "enabled": False},
        )
        assert unauth.status_code == 401

        forbidden = client.post(
            "/api/v1/nodes/bulk-update",
            json={"node_ids": [node_id], "action": "set_enabled", "enabled": False},
            headers=_op_headers("viewer-token"),
        )
        assert forbidden.status_code == 403

        allowed = client.post(
            "/api/v1/nodes/bulk-update",
            json={"node_ids": [node_id], "action": "set_enabled", "enabled": False},
            headers=_op_headers("operator-token"),
        )
        assert allowed.status_code == 200
        assert allowed.json()["status"] == "updated"


def test_read_only_cluster_summary_remains_unauthenticated(tmp_path: Path) -> None:
    db_path = tmp_path / "cluster.db"
    os.environ["DASHBOARD_DB_PATH"] = str(db_path)
    _set_operator_creds()

    with TestClient(app) as client:
        resp = client.get("/api/v1/cluster/summary")
        assert resp.status_code == 200


def test_webhook_deliveries_api_returns_filtered_rows(tmp_path: Path) -> None:
    db_path = tmp_path / "cluster.db"
    os.environ["DASHBOARD_DB_PATH"] = str(db_path)
    ensure_db(db_path)
    now_iso = utc_now_iso()

    with get_conn(db_path) as conn:
        node_id = int(
            conn.execute(
                """
                INSERT INTO nodes (name, hostname, ip_address, token, role, poll_interval_seconds, enabled, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                ("pi-webhook", "pi-webhook.local", "10.0.0.90", "tok", "worker", 10, 1, now_iso, now_iso),
            ).lastrowid
        )
        alert_id = conn.execute("SELECT id FROM alerts WHERE key = 'cpu' LIMIT 1").fetchone()["id"]
        alert_event_id = int(
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
            ) VALUES (?, 'failed', 1, ?, NULL, 'timeout', ?, ?)
            """,
            (alert_event_id, now_iso, now_iso, now_iso),
        )

    with TestClient(app) as client:
        resp = client.get("/api/v1/webhooks/deliveries?status_filter=failed&limit=10")
        assert resp.status_code == 200
        deliveries = resp.json()["deliveries"]
        assert len(deliveries) == 1
        assert deliveries[0]["status"] == "failed"
        assert deliveries[0]["node_name"] == "pi-webhook"


def test_ingest_rejects_agent_identity_mismatch(tmp_path: Path) -> None:
    db_path = tmp_path / "cluster.db"
    os.environ["DASHBOARD_DB_PATH"] = str(db_path)
    os.environ["DASHBOARD_ENROLL_SECRET"] = "enroll-secret"

    with TestClient(app) as client:
        enroll_resp = client.post(
            "/api/v1/enroll",
            json={
                "enroll_secret": "enroll-secret",
                "hostname": "pi-ingest-id.local",
                "agent_id": "agent-ingest-1",
                "ip_address": "10.0.0.92",
            },
        )
        assert enroll_resp.status_code == 200
        token = enroll_resp.json()["token"]

        payload = {
            "hostname": "pi-different-host.local",
            "agent_id": "agent-ingest-1",
            "timestamp": utc_now_iso(),
            "cpu_percent": 11.0,
            "memory_percent": 22.0,
            "disk_percent": 33.0,
            "temperature_c": 44.0,
            "uptime_seconds": 120,
            "load_1": 0.1,
            "load_5": 0.2,
            "load_15": 0.3,
            "rx_bytes": 111,
            "tx_bytes": 222,
        }
        payload_json = json.dumps(payload, separators=(",", ":"), sort_keys=True)
        ts = str(int(time.time()))
        nonce = "nonce-agent-mismatch"
        signature = hmac.new(token.encode("utf-8"), f"{ts}.{nonce}.{payload_json}".encode("utf-8"), hashlib.sha256).hexdigest()
        headers = {
            "Authorization": f"Bearer {token}",
            "X-PCM-Timestamp": ts,
            "X-PCM-Nonce": nonce,
            "X-PCM-Signature": signature,
            "Content-Type": "application/json",
        }
        ingest_resp = client.post("/api/v1/ingest", content=payload_json, headers=headers)
        assert ingest_resp.status_code == 409
        assert ingest_resp.json()["detail"] == "Identity binding mismatch"
