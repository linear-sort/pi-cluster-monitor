from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import hmac
import json
from pathlib import Path
import secrets
import sqlite3
import time

from fastapi import APIRouter, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app.db import fetch_all_dict, fetch_one_dict, get_conn, token_expiry_iso, utc_now_iso
from app.models import (
    BulkNodeUpdateRequest,
    BulkNodeUpdateResponse,
    EnrollmentRequest,
    EnrollmentResponse,
    IngestMetricsRequest,
    RevokeTokenRequest,
    RevokeTokenResponse,
    TokenRefreshRequest,
    TokenRefreshResponse,
)
from app.security import request_correlation_id, require_operator


router = APIRouter()

TEMPLATES_DIR = Path(__file__).resolve().parents[2] / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


def _db_path(request: Request) -> Path:
    return request.app.state.settings.db_path


def _cluster_stats(conn) -> dict:
    stats = fetch_one_dict(
        conn,
        """
        SELECT
            COUNT(*) AS total_nodes,
            SUM(CASE WHEN last_status = 'online' THEN 1 ELSE 0 END) AS online_nodes,
            SUM(CASE WHEN last_status = 'offline' THEN 1 ELSE 0 END) AS offline_nodes
        FROM nodes
        WHERE enabled = 1
        """,
    ) or {}

    level_counts = fetch_one_dict(
        conn,
        """
        SELECT
            SUM(CASE WHEN severity = 'warning' THEN 1 ELSE 0 END) AS warning_nodes,
            SUM(CASE WHEN severity = 'critical' THEN 1 ELSE 0 END) AS critical_nodes
        FROM (
            SELECT node_id, MAX(CASE severity WHEN 'critical' THEN 2 WHEN 'warning' THEN 1 ELSE 0 END) AS sev_rank,
                   CASE MAX(CASE severity WHEN 'critical' THEN 2 WHEN 'warning' THEN 1 ELSE 0 END)
                        WHEN 2 THEN 'critical'
                        WHEN 1 THEN 'warning'
                        ELSE 'info'
                   END AS severity
            FROM alert_events
            WHERE resolved_at IS NULL
            GROUP BY node_id
        )
        """,
    ) or {"warning_nodes": 0, "critical_nodes": 0}

    avg_cpu = fetch_one_dict(
        conn,
        """
        SELECT ROUND(AVG(ms.cpu_percent), 1) AS avg_cpu_percent
        FROM metric_samples ms
        JOIN (
            SELECT node_id, MAX(collected_at) AS max_collected
            FROM metric_samples
            GROUP BY node_id
        ) latest ON latest.node_id = ms.node_id AND latest.max_collected = ms.collected_at
        """,
    ) or {"avg_cpu_percent": 0}

    hottest = fetch_one_dict(
        conn,
        """
        SELECT n.name AS hottest_node_name, ms.temperature_c AS hottest_node_temp
        FROM metric_samples ms
        JOIN nodes n ON n.id = ms.node_id
        WHERE ms.temperature_c IS NOT NULL
        ORDER BY ms.temperature_c DESC
        LIMIT 1
        """,
    ) or {"hottest_node_name": None, "hottest_node_temp": None}

    return {
        "total_nodes": stats.get("total_nodes") or 0,
        "online_nodes": stats.get("online_nodes") or 0,
        "offline_nodes": stats.get("offline_nodes") or 0,
        "warning_nodes": level_counts.get("warning_nodes") or 0,
        "critical_nodes": level_counts.get("critical_nodes") or 0,
        "avg_cpu_percent": avg_cpu.get("avg_cpu_percent") or 0,
        "hottest_node_name": hottest.get("hottest_node_name"),
        "hottest_node_temp": hottest.get("hottest_node_temp"),
    }


def _cluster_nodes(conn) -> list[dict]:
    return fetch_all_dict(
        conn,
        """
        SELECT
            n.id, n.name, n.hostname, n.agent_id, n.ip_address, n.role, n.enabled, n.last_status, n.last_seen_at,
            n.enrollment_status, n.enrolled_at, n.token_expires_at, n.token_version, n.revoked_at, n.revoked_reason,
            n.use_tls, n.tls_verify, n.tls_ca_path, n.tls_fingerprint_sha256,
            n.collect_mode,
            n.last_heartbeat_at, n.last_error_category, n.last_error_message, n.consecutive_failures,
            ms.cpu_percent, ms.memory_percent, ms.disk_percent, ms.temperature_c,
            COALESCE(ev.severity, 'info') AS alert_severity
        FROM nodes n
        LEFT JOIN metric_samples ms ON ms.id = (
            SELECT m2.id FROM metric_samples m2
            WHERE m2.node_id = n.id
            ORDER BY m2.collected_at DESC
            LIMIT 1
        )
        LEFT JOIN (
            SELECT node_id,
                   CASE MAX(CASE severity WHEN 'critical' THEN 2 WHEN 'warning' THEN 1 ELSE 0 END)
                        WHEN 2 THEN 'critical'
                        WHEN 1 THEN 'warning'
                        ELSE 'info'
                   END AS severity
            FROM alert_events
            WHERE resolved_at IS NULL
            GROUP BY node_id
        ) ev ON ev.node_id = n.id
        ORDER BY n.name
        """,
    )


def _node_services(conn, node_id: int) -> list[dict]:
    return fetch_all_dict(
        conn,
        """
        SELECT s.name, s.status, s.checked_at
        FROM services s
        JOIN (
            SELECT name, MAX(checked_at) AS max_checked
            FROM services
            WHERE node_id = ?
            GROUP BY name
        ) latest ON latest.name = s.name AND latest.max_checked = s.checked_at
        WHERE s.node_id = ?
        ORDER BY s.name
        """,
        (node_id, node_id),
    )


def _is_future_iso(value: str | None) -> bool:
    if not value:
        return False
    try:
        return datetime.fromisoformat(value) > datetime.now(timezone.utc)
    except ValueError:
        return False


def _validate_node_token(node: dict, token: str) -> bool:
    if node.get("revoked_at"):
        return False
    if token == (node.get("token") or "") and _is_future_iso(node.get("token_expires_at")):
        return True
    if token == (node.get("previous_token") or "") and _is_future_iso(node.get("previous_token_expires_at")):
        return True
    return False


def _normalize_agent_id(value: str | None) -> str | None:
    cleaned = (value or "").strip()
    return cleaned or None


def _find_node_by_identity(conn, hostname: str, agent_id: str | None) -> dict | None:
    normalized_agent_id = _normalize_agent_id(agent_id)
    if normalized_agent_id:
        return fetch_one_dict(
            conn,
            """
            SELECT id, hostname, agent_id, token, token_expires_at, previous_token, previous_token_expires_at, token_version, revoked_at
            FROM nodes
            WHERE agent_id = ?
            LIMIT 1
            """,
            (normalized_agent_id,),
        )
    return fetch_one_dict(
        conn,
        """
        SELECT id, hostname, agent_id, token, token_expires_at, previous_token, previous_token_expires_at, token_version, revoked_at
        FROM nodes
        WHERE hostname = ?
        LIMIT 1
        """,
        (hostname.strip(),),
    )


def _fetch_node_read(conn, node_id: int) -> dict | None:
    return fetch_one_dict(
        conn,
        """
        SELECT
            id, name, hostname, agent_id, ip_address, role, agent_port, use_tls, tls_verify,
            tls_ca_path, tls_fingerprint_sha256, collect_mode, poll_interval_seconds, enabled,
            enrollment_status, enrolled_at, token_expires_at, token_version, revoked_at, revoked_reason, revoked_by,
            last_status, last_seen_at, last_heartbeat_at, last_error_category, last_error_message, consecutive_failures,
            created_at, updated_at
        FROM nodes
        WHERE id = ?
        LIMIT 1
        """,
        (node_id,),
    )


@router.get("/", include_in_schema=False)
def root() -> RedirectResponse:
    return RedirectResponse(url="/cluster", status_code=303)


@router.get("/cluster", response_class=HTMLResponse)
def cluster_overview(request: Request) -> HTMLResponse:
    with get_conn(_db_path(request)) as conn:
        stats = _cluster_stats(conn)
        nodes = _cluster_nodes(conn)

    return templates.TemplateResponse(
        request,
        "cluster_overview.html",
        {
            "stats": stats,
            "nodes": nodes,
            "page": "cluster",
        },
    )


@router.get("/partials/cluster-summary", response_class=HTMLResponse)
def partial_cluster_summary(request: Request) -> HTMLResponse:
    with get_conn(_db_path(request)) as conn:
        stats = _cluster_stats(conn)

    return templates.TemplateResponse(request, "partials/cluster_summary.html", {"stats": stats})


@router.get("/partials/cluster-nodes", response_class=HTMLResponse)
def partial_cluster_nodes(request: Request) -> HTMLResponse:
    with get_conn(_db_path(request)) as conn:
        nodes = _cluster_nodes(conn)

    return templates.TemplateResponse(request, "partials/cluster_nodes.html", {"nodes": nodes})


@router.get("/nodes/{node_id}", response_class=HTMLResponse)
def node_detail(request: Request, node_id: int) -> HTMLResponse:
    require_operator(request, min_role="viewer")
    with get_conn(_db_path(request)) as conn:
        node = _fetch_node_read(conn, node_id)
        if not node:
            return templates.TemplateResponse(request, "not_found.html", {"message": "Node not found"}, status_code=404)

        latest = fetch_one_dict(
            conn,
            """
            SELECT * FROM metric_samples
            WHERE node_id = ?
            ORDER BY collected_at DESC
            LIMIT 1
            """,
            (node_id,),
        )
        alerts = fetch_all_dict(
            conn,
            """
            SELECT ae.severity, ae.message, ae.metric_value, ae.created_at, ae.resolved_at, a.key
            FROM alert_events ae
            JOIN alerts a ON a.id = ae.alert_id
            WHERE ae.node_id = ?
            ORDER BY ae.created_at DESC
            LIMIT 20
            """,
            (node_id,),
        )
        services = _node_services(conn, node_id)

    return templates.TemplateResponse(
        request,
        "node_detail.html",
        {"node": node, "latest": latest, "alerts": alerts, "services": services, "page": "node"},
    )


@router.get("/nodes/{node_id}/metrics")
def node_metrics_json(request: Request, node_id: int, minutes: int = 60):
    cutoff = datetime.now(timezone.utc).timestamp() - (max(1, minutes) * 60)
    cutoff_iso = datetime.fromtimestamp(cutoff, tz=timezone.utc).isoformat()

    with get_conn(_db_path(request)) as conn:
        rows = fetch_all_dict(
            conn,
            """
            SELECT collected_at, cpu_percent, memory_percent, disk_percent, temperature_c
            FROM metric_samples
            WHERE node_id = ? AND collected_at >= ?
            ORDER BY collected_at ASC
            """,
            (node_id, cutoff_iso),
        )

    labels = [r["collected_at"] for r in rows]
    return {
        "labels": labels,
        "cpu": [r["cpu_percent"] for r in rows],
        "memory": [r["memory_percent"] for r in rows],
        "disk": [r["disk_percent"] for r in rows],
        "temp": [r["temperature_c"] for r in rows],
    }


@router.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request) -> HTMLResponse:
    require_operator(request, min_role="viewer")
    with get_conn(_db_path(request)) as conn:
        nodes = fetch_all_dict(
            conn,
            """
            SELECT
                id, name, hostname, agent_id, ip_address, role, agent_port, use_tls, tls_verify,
                tls_ca_path, tls_fingerprint_sha256, collect_mode, poll_interval_seconds, enabled,
                enrollment_status, enrolled_at, token_expires_at, token_version, revoked_at, revoked_reason, revoked_by,
                last_status, last_seen_at, last_heartbeat_at, last_error_category, last_error_message, consecutive_failures
            FROM nodes
            ORDER BY name
            """,
        )

    return templates.TemplateResponse(
        request,
        "settings.html",
        {"nodes": nodes, "page": "settings", "default_port": request.app.state.settings.agent_default_port},
    )


@router.get("/settings/node-form", response_class=HTMLResponse)
def node_form_partial(request: Request, node_id: int | None = None) -> HTMLResponse:
    with get_conn(_db_path(request)) as conn:
        node = _fetch_node_read(conn, node_id) if node_id else None

    return templates.TemplateResponse(
        request,
        "partials/node_form.html",
        {"node": node, "default_port": request.app.state.settings.agent_default_port},
    )


@router.post("/settings/nodes/save")
def save_node(
    request: Request,
    id: int | None = Form(default=None),
    name: str = Form(...),
    hostname: str = Form(...),
    ip_address: str = Form(...),
    token: str = Form(default=""),
    role: str = Form(default="worker"),
    agent_port: int = Form(default=8001),
    use_tls: str = Form(default="false"),
    tls_verify: str = Form(default="true"),
    tls_ca_path: str = Form(default=""),
    tls_fingerprint_sha256: str = Form(default=""),
    collect_mode: str = Form(default="pull"),
    poll_interval_seconds: int = Form(default=10),
    enabled: str = Form(default="true"),
) -> RedirectResponse:
    require_operator(request, min_role="operator")
    token_value = token.strip()
    use_tls_bool = str(use_tls).strip().lower() in {"1", "true", "yes", "on"}
    tls_verify_bool = str(tls_verify).strip().lower() in {"1", "true", "yes", "on"}
    collect_mode_value = str(collect_mode).strip().lower()
    if collect_mode_value not in {"pull", "push", "hybrid"}:
        collect_mode_value = "pull"
    enabled_bool = str(enabled).strip().lower() in {"1", "true", "yes", "on"}
    now_iso = utc_now_iso()
    with get_conn(_db_path(request)) as conn:
        if id:
            current = fetch_one_dict(conn, "SELECT token FROM nodes WHERE id = ? LIMIT 1", (id,))
            if not current:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Node not found")
            token_to_store = token_value or str(current.get("token") or "").strip()
            if not token_to_store:
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Token is required")
            conn.execute(
                """
                UPDATE nodes
                SET name=?, hostname=?, ip_address=?, token=?, role=?,
                    agent_port=?, use_tls=?, tls_verify=?, tls_ca_path=?, tls_fingerprint_sha256=?,
                    collect_mode=?, poll_interval_seconds=?, enabled=?, updated_at=?
                WHERE id=?
                """,
                (
                    name.strip(),
                    hostname.strip(),
                    ip_address.strip(),
                    token_to_store,
                    role.strip() or "worker",
                    int(agent_port),
                    1 if use_tls_bool else 0,
                    1 if tls_verify_bool else 0,
                    tls_ca_path.strip() or None,
                    tls_fingerprint_sha256.strip() or None,
                    collect_mode_value,
                    max(3, int(poll_interval_seconds)),
                    1 if enabled_bool else 0,
                    now_iso,
                    id,
                ),
            )
        else:
            if not token_value:
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Token is required")
            conn.execute(
                """
                INSERT INTO nodes (
                    name, hostname, ip_address, token, role,
                    agent_port, use_tls, tls_verify, tls_ca_path, tls_fingerprint_sha256, collect_mode,
                    poll_interval_seconds, enabled,
                    created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    name.strip(),
                    hostname.strip(),
                    ip_address.strip(),
                    token_value,
                    role.strip() or "worker",
                    int(agent_port),
                    1 if use_tls_bool else 0,
                    1 if tls_verify_bool else 0,
                    tls_ca_path.strip() or None,
                    tls_fingerprint_sha256.strip() or None,
                    collect_mode_value,
                    max(3, int(poll_interval_seconds)),
                    1 if enabled_bool else 0,
                    now_iso,
                    now_iso,
                ),
            )

    return RedirectResponse(url="/settings", status_code=303)


@router.post("/settings/nodes/{node_id}/toggle")
def toggle_node(request: Request, node_id: int) -> RedirectResponse:
    require_operator(request, min_role="operator")
    with get_conn(_db_path(request)) as conn:
        row = fetch_one_dict(conn, "SELECT enabled FROM nodes WHERE id = ?", (node_id,))
        if row is not None:
            next_enabled = 0 if int(row["enabled"]) else 1
            conn.execute(
                "UPDATE nodes SET enabled = ?, updated_at = ? WHERE id = ?",
                (next_enabled, utc_now_iso(), node_id),
            )
    return RedirectResponse(url="/settings", status_code=303)


@router.get("/api/v1/cluster/summary")
def api_cluster_summary(request: Request) -> dict:
    with get_conn(_db_path(request)) as conn:
        return _cluster_stats(conn)


@router.get("/api/v1/nodes")
def api_nodes(request: Request) -> dict:
    with get_conn(_db_path(request)) as conn:
        return {"nodes": _cluster_nodes(conn)}


@router.get("/api/v1/nodes/{node_id}")
def api_node_detail(request: Request, node_id: int) -> dict:
    require_operator(request, min_role="viewer")
    with get_conn(_db_path(request)) as conn:
        node = _fetch_node_read(conn, node_id)
        if not node:
            return {"error": "node_not_found"}
        latest = fetch_one_dict(
            conn,
            """
            SELECT * FROM metric_samples
            WHERE node_id = ?
            ORDER BY collected_at DESC
            LIMIT 1
            """,
            (node_id,),
        )
        alerts = fetch_all_dict(
            conn,
            """
            SELECT ae.severity, ae.message, ae.metric_value, ae.created_at, ae.resolved_at, a.key
            FROM alert_events ae
            JOIN alerts a ON a.id = ae.alert_id
            WHERE ae.node_id = ?
            ORDER BY ae.created_at DESC
            LIMIT 20
            """,
            (node_id,),
        )
        services = _node_services(conn, node_id)
    return {"node": node, "latest": latest, "alerts": alerts, "services": services}


@router.get("/api/v1/nodes/{node_id}/metrics")
def api_node_metrics(request: Request, node_id: int, minutes: int = 60) -> dict:
    require_operator(request, min_role="viewer")
    return node_metrics_json(request=request, node_id=node_id, minutes=minutes)


@router.get("/api/v1/alerts")
def api_alerts(request: Request, unresolved_only: bool = False, limit: int = 100) -> dict:
    with get_conn(_db_path(request)) as conn:
        where_clause = "WHERE ae.resolved_at IS NULL" if unresolved_only else ""
        rows = fetch_all_dict(
            conn,
            f"""
            SELECT ae.node_id, n.name AS node_name, a.key AS alert_key, ae.severity,
                   ae.message, ae.metric_value, ae.created_at, ae.resolved_at
            FROM alert_events ae
            JOIN nodes n ON n.id = ae.node_id
            JOIN alerts a ON a.id = ae.alert_id
            {where_clause}
            ORDER BY ae.created_at DESC
            LIMIT ?
            """,
            (max(1, min(limit, 500)),),
        )
    return {"alerts": rows}


@router.get("/api/v1/diagnostics/loops")
def api_loop_diagnostics(request: Request) -> dict:
    require_operator(request, min_role="viewer")
    poller = getattr(request.app.state, "poller", None)
    if poller is None:
        return {"error": "poller_unavailable"}
    return {"diagnostics": poller.get_diagnostics()}


@router.get("/api/v1/webhooks/deliveries")
def api_webhook_deliveries(request: Request, status_filter: str = "failed", limit: int = 100) -> dict:
    require_operator(request, min_role="viewer")
    allowed = {"pending", "failed", "dead", "delivered", "all"}
    resolved_filter = status_filter.strip().lower()
    if resolved_filter not in allowed:
        resolved_filter = "failed"

    with get_conn(_db_path(request)) as conn:
        if resolved_filter == "all":
            rows = fetch_all_dict(
                conn,
                """
                SELECT
                    wd.id, wd.alert_event_id, wd.status, wd.attempt_count, wd.next_attempt_at,
                    wd.delivered_at, wd.last_error, wd.created_at, wd.updated_at,
                    ae.node_id, ae.severity, ae.message, a.key AS alert_key, n.name AS node_name
                FROM webhook_deliveries wd
                JOIN alert_events ae ON ae.id = wd.alert_event_id
                JOIN alerts a ON a.id = ae.alert_id
                JOIN nodes n ON n.id = ae.node_id
                ORDER BY wd.updated_at DESC
                LIMIT ?
                """,
                (max(1, min(limit, 500)),),
            )
        else:
            rows = fetch_all_dict(
                conn,
                """
                SELECT
                    wd.id, wd.alert_event_id, wd.status, wd.attempt_count, wd.next_attempt_at,
                    wd.delivered_at, wd.last_error, wd.created_at, wd.updated_at,
                    ae.node_id, ae.severity, ae.message, a.key AS alert_key, n.name AS node_name
                FROM webhook_deliveries wd
                JOIN alert_events ae ON ae.id = wd.alert_event_id
                JOIN alerts a ON a.id = ae.alert_id
                JOIN nodes n ON n.id = ae.node_id
                WHERE wd.status = ?
                ORDER BY wd.updated_at DESC
                LIMIT ?
                """,
                (resolved_filter, max(1, min(limit, 500))),
            )
    return {"deliveries": rows}


@router.post("/api/v1/ingest")
async def api_ingest_metrics(request: Request, payload: IngestMetricsRequest) -> dict:
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing token")
    token = auth_header.removeprefix("Bearer ").strip()
    if not token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing token")

    timestamp_header = request.headers.get("X-PCM-Timestamp", "").strip()
    nonce_header = request.headers.get("X-PCM-Nonce", "").strip()
    signature_header = request.headers.get("X-PCM-Signature", "").strip().lower()
    token_version_header = request.headers.get("X-PCM-Token-Version", "").strip()
    if not timestamp_header or not nonce_header or not signature_header:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Missing signature headers")

    try:
        ts = int(timestamp_header)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid timestamp header") from exc
    now_ts = int(time.time())
    if abs(now_ts - ts) > 300:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Stale timestamp")

    raw_body = await request.body()
    try:
        body_for_signature = raw_body.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid request encoding") from exc
    canonical_payload = json.dumps(payload.model_dump(mode="json"), separators=(",", ":"), sort_keys=True)
    signed_message = f"{timestamp_header}.{nonce_header}.{body_for_signature}"

    with get_conn(_db_path(request)) as conn:
        node = _find_node_by_identity(conn, hostname=payload.hostname, agent_id=payload.agent_id)
        if not node:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Node not found")
        if _normalize_agent_id(payload.agent_id) and payload.hostname.strip() != str(node.get("hostname") or "").strip():
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Identity binding mismatch")
        if not _validate_node_token(node, token):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or expired token")
        if token_version_header:
            try:
                presented_version = int(token_version_header)
            except ValueError as exc:
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid token version header") from exc
            if int(node.get("token_version") or 1) != presented_version:
                raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Token version mismatch")

        expected_sig = hmac.new(token.encode("utf-8"), signed_message.encode("utf-8"), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature_header, expected_sig):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid signature")

        nonce_cutoff = datetime.fromtimestamp(now_ts - 600, tz=timezone.utc).isoformat()
        conn.execute("DELETE FROM ingest_nonces WHERE node_id = ? AND created_at < ?", (int(node["id"]), nonce_cutoff))
        try:
            conn.execute(
                "INSERT INTO ingest_nonces (node_id, nonce, created_at) VALUES (?, ?, ?)",
                (int(node["id"]), nonce_header, utc_now_iso()),
            )
        except sqlite3.IntegrityError as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Replay nonce detected") from exc

        existing = fetch_one_dict(
            conn,
            "SELECT id FROM metric_samples WHERE node_id = ? AND collected_at = ? LIMIT 1",
            (int(node["id"]), payload.timestamp.isoformat()),
        )
        now_iso = utc_now_iso()
        conn.execute(
            """
            UPDATE nodes
            SET last_seen_at = ?, last_heartbeat_at = ?, last_status = 'online',
                last_error_category = NULL, last_error_message = NULL, last_poll_error_at = NULL,
                consecutive_failures = 0, updated_at = ?
            WHERE id = ?
            """,
            (now_iso, now_iso, now_iso, int(node["id"])),
        )
        if existing:
            return {"status": "accepted", "deduped": True}

        conn.execute(
            """
            INSERT INTO metric_samples (
                node_id, collected_at, source, cpu_percent, memory_percent, disk_percent, temperature_c,
                uptime_seconds, load_1, load_5, load_15, rx_bytes, tx_bytes, raw_json
            ) VALUES (?, ?, 'push', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                int(node["id"]),
                payload.timestamp.isoformat(),
                payload.cpu_percent,
                payload.memory_percent,
                payload.disk_percent,
                payload.temperature_c,
                payload.uptime_seconds,
                payload.load_1,
                payload.load_5,
                payload.load_15,
                payload.rx_bytes,
                payload.tx_bytes,
                canonical_payload,
            ),
        )

        from app.services.alerts import evaluate_metric_thresholds, evaluate_offline_alert

        evaluate_metric_thresholds(
            conn,
            int(node["id"]),
            {
                "cpu_percent": payload.cpu_percent,
                "memory_percent": payload.memory_percent,
                "disk_percent": payload.disk_percent,
                "temperature_c": payload.temperature_c,
            },
        )
        evaluate_offline_alert(conn, int(node["id"]), 0)
    return {"status": "accepted", "deduped": False}


@router.post("/api/v1/enroll", response_model=EnrollmentResponse)
def api_agent_enroll(request: Request, payload: EnrollmentRequest) -> EnrollmentResponse:
    expected_secret = request.app.state.settings.enroll_secret
    if payload.enroll_secret != expected_secret:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid enrollment secret")

    issued_token = secrets.token_urlsafe(32)
    now_iso = utc_now_iso()
    expires_at_iso = token_expiry_iso(request.app.state.settings.token_ttl_seconds)
    resolved_ip = payload.ip_address or (request.client.host if request.client else "unknown")
    resolved_name = (payload.name or payload.hostname).strip()
    normalized_agent_id = _normalize_agent_id(payload.agent_id)

    node_id = 0
    try:
        with get_conn(_db_path(request)) as conn:
            existing = None
            if normalized_agent_id:
                existing = fetch_one_dict(
                    conn,
                    """
                    SELECT id, hostname, agent_id
                    FROM nodes
                    WHERE agent_id = ?
                    LIMIT 1
                    """,
                    (normalized_agent_id,),
                )
            if not existing:
                existing = fetch_one_dict(
                    conn,
                    """
                    SELECT id, hostname, agent_id
                    FROM nodes
                    WHERE hostname = ? OR ip_address = ?
                    ORDER BY id ASC
                    LIMIT 1
                    """,
                    (payload.hostname.strip(), resolved_ip),
                )
            if existing and normalized_agent_id:
                bound_agent_id = _normalize_agent_id(existing.get("agent_id"))
                if bound_agent_id and bound_agent_id != normalized_agent_id:
                    raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Agent identity conflict")
            if existing:
                node_id = int(existing["id"])
                conn.execute(
                    """
                    UPDATE nodes
                    SET name = ?, hostname = ?, agent_id = ?, ip_address = ?, token = ?, role = ?, agent_port = ?,
                        poll_interval_seconds = ?, enabled = 1, enrollment_status = 'enrolled',
                        enrolled_at = ?, token_issued_at = ?, token_expires_at = ?,
                        previous_token = NULL, previous_token_expires_at = NULL,
                        token_version = CASE WHEN token_version IS NULL THEN 1 ELSE token_version + 1 END,
                        revoked_at = NULL, revoked_reason = NULL, revoked_by = NULL,
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        resolved_name,
                        payload.hostname.strip(),
                        normalized_agent_id if normalized_agent_id else _normalize_agent_id(existing.get("agent_id")),
                        resolved_ip,
                        issued_token,
                        payload.role.strip() or "worker",
                        int(payload.agent_port),
                        max(3, int(payload.poll_interval_seconds)),
                        now_iso,
                        now_iso,
                        expires_at_iso,
                        now_iso,
                        node_id,
                    ),
                )
            else:
                cursor = conn.execute(
                    """
                    INSERT INTO nodes (
                        name, hostname, agent_id, ip_address, token, role, agent_port, poll_interval_seconds,
                        enabled, enrollment_status, enrolled_at, token_issued_at, token_expires_at,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, 'enrolled', ?, ?, ?, ?, ?)
                    """,
                    (
                        resolved_name,
                        payload.hostname.strip(),
                        normalized_agent_id,
                        resolved_ip,
                        issued_token,
                        payload.role.strip() or "worker",
                        int(payload.agent_port),
                        max(3, int(payload.poll_interval_seconds)),
                        now_iso,
                        now_iso,
                        expires_at_iso,
                        now_iso,
                        now_iso,
                    ),
                )
                node_id = int(cursor.lastrowid)
    except sqlite3.IntegrityError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Agent identity conflict") from exc

    with get_conn(_db_path(request)) as conn:
        row = fetch_one_dict(conn, "SELECT token_version, agent_id FROM nodes WHERE id = ? LIMIT 1", (node_id,))
    return EnrollmentResponse(
        node_id=node_id,
        agent_id=_normalize_agent_id(row.get("agent_id") if row else None),
        token=issued_token,
        token_version=int((row or {}).get("token_version") or 1),
        status="enrolled",
    )


@router.post("/api/v1/token/refresh", response_model=TokenRefreshResponse)
def api_token_refresh(request: Request, payload: TokenRefreshRequest) -> TokenRefreshResponse:
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing token")
    presented_token = auth_header.removeprefix("Bearer ").strip()
    if not presented_token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing token")

    with get_conn(_db_path(request)) as conn:
        node = _find_node_by_identity(conn, hostname=payload.hostname, agent_id=payload.agent_id)
        if not node:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Node not found")
        if _normalize_agent_id(payload.agent_id) and payload.hostname.strip() != str(node.get("hostname") or "").strip():
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Identity binding mismatch")
        if node.get("revoked_at"):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Token revoked")
        if payload.token_version is not None and int(node.get("token_version") or 1) != int(payload.token_version):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Token version mismatch")

        is_current = presented_token == (node.get("token") or "")
        is_previous = presented_token == (node.get("previous_token") or "") and _is_future_iso(node.get("previous_token_expires_at"))
        current_valid = is_current and _is_future_iso(node.get("token_expires_at"))
        if not (current_valid or is_previous):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or expired token")

        new_token = secrets.token_urlsafe(32)
        now_iso = utc_now_iso()
        expires_at_iso = token_expiry_iso(request.app.state.settings.token_ttl_seconds)
        previous_expires_at_iso = token_expiry_iso(request.app.state.settings.token_grace_seconds)
        conn.execute(
            """
            UPDATE nodes
            SET previous_token = token,
                previous_token_expires_at = ?,
                token = ?,
                token_issued_at = ?,
                token_expires_at = ?,
                updated_at = ?
            WHERE id = ?
            """,
            (previous_expires_at_iso, new_token, now_iso, expires_at_iso, now_iso, int(node["id"])),
        )

    return TokenRefreshResponse(
        node_id=int(node["id"]),
        token=new_token,
        token_version=int(node.get("token_version") or 1),
        expires_at=datetime.fromisoformat(expires_at_iso),
        status="refreshed",
    )


@router.post("/api/v1/nodes/{node_id}/token/revoke", response_model=RevokeTokenResponse)
def api_revoke_node_token(request: Request, node_id: int, payload: RevokeTokenRequest) -> RevokeTokenResponse:
    principal = require_operator(request, min_role="admin")
    corr_id = request_correlation_id(request)
    now_iso = utc_now_iso()
    with get_conn(_db_path(request)) as conn:
        existing = fetch_one_dict(conn, "SELECT id, token_version FROM nodes WHERE id = ? LIMIT 1", (node_id,))
        if not existing:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Node not found")
        next_version = int(existing.get("token_version") or 1) + 1
        conn.execute(
            """
            UPDATE nodes
            SET token = '',
                token_issued_at = NULL,
                token_expires_at = ?,
                previous_token = NULL,
                previous_token_expires_at = ?,
                token_version = ?,
                revoked_at = ?,
                revoked_reason = ?,
                revoked_by = ?,
                updated_at = ?
            WHERE id = ?
            """,
            (now_iso, now_iso, next_version, now_iso, payload.reason.strip(), principal["principal"], now_iso, node_id),
        )
        conn.execute(
            """
            INSERT INTO security_audit_events (node_id, event_type, actor, reason, metadata_json, created_at)
            VALUES (?, 'token_revoked', ?, ?, ?, ?)
            """,
            (
                node_id,
                principal["principal"],
                payload.reason.strip(),
                json.dumps({"token_version": next_version, "correlation_id": corr_id}, separators=(",", ":"), sort_keys=True),
                now_iso,
            ),
        )
    return RevokeTokenResponse(
        node_id=node_id,
        revoked_at=datetime.fromisoformat(now_iso),
        token_version=next_version,
        status="revoked",
    )


@router.post("/api/v1/nodes/bulk-update", response_model=BulkNodeUpdateResponse)
def api_bulk_update_nodes(request: Request, payload: BulkNodeUpdateRequest) -> BulkNodeUpdateResponse:
    principal = require_operator(request, min_role="operator")
    corr_id = request_correlation_id(request)
    action = payload.action.strip().lower()
    valid_actions = {"set_enabled", "set_poll_interval", "set_role"}
    if action not in valid_actions:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Unsupported bulk action")

    node_ids = sorted({int(node_id) for node_id in payload.node_ids if int(node_id) > 0})
    if not node_ids:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="No valid node IDs provided")

    if action == "set_enabled" and payload.enabled is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="enabled is required for set_enabled")
    if action == "set_poll_interval" and payload.poll_interval_seconds is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="poll_interval_seconds is required for set_poll_interval",
        )
    if action == "set_role" and not (payload.role or "").strip():
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="role is required for set_role")

    now_iso = utc_now_iso()
    affected_count = 0
    with get_conn(_db_path(request)) as conn:
        placeholders = ",".join("?" for _ in node_ids)
        rows = fetch_all_dict(conn, f"SELECT id FROM nodes WHERE id IN ({placeholders})", tuple(node_ids))
        target_ids = sorted(int(row["id"]) for row in rows)
        if not target_ids:
            return BulkNodeUpdateResponse(affected_count=0, action=action, status="no_targets")

        for node_id in target_ids:
            if action == "set_enabled":
                conn.execute(
                    "UPDATE nodes SET enabled = ?, updated_at = ? WHERE id = ?",
                    (1 if payload.enabled else 0, now_iso, node_id),
                )
                metadata = {"enabled": bool(payload.enabled)}
            elif action == "set_poll_interval":
                interval = max(3, int(payload.poll_interval_seconds or 10))
                conn.execute(
                    "UPDATE nodes SET poll_interval_seconds = ?, updated_at = ? WHERE id = ?",
                    (interval, now_iso, node_id),
                )
                metadata = {"poll_interval_seconds": interval}
            else:
                role = str(payload.role or "").strip() or "worker"
                conn.execute(
                    "UPDATE nodes SET role = ?, updated_at = ? WHERE id = ?",
                    (role, now_iso, node_id),
                )
                metadata = {"role": role}

            conn.execute(
                """
                INSERT INTO security_audit_events (node_id, event_type, actor, reason, metadata_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    node_id,
                    f"bulk_{action}",
                    principal["principal"],
                    payload.reason.strip(),
                    json.dumps(
                        {**metadata, "correlation_id": corr_id},
                        separators=(",", ":"),
                        sort_keys=True,
                    ),
                    now_iso,
                ),
            )
            affected_count += 1

    return BulkNodeUpdateResponse(affected_count=affected_count, action=action, status="updated")
