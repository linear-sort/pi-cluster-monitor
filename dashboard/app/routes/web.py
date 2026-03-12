from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import secrets

from fastapi import APIRouter, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app.db import fetch_all_dict, fetch_one_dict, get_conn, token_expiry_iso, utc_now_iso
from app.models import EnrollmentRequest, EnrollmentResponse, TokenRefreshRequest, TokenRefreshResponse


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
            n.id, n.name, n.hostname, n.ip_address, n.role, n.enabled, n.last_status, n.last_seen_at,
            n.enrollment_status, n.enrolled_at, n.token_expires_at, n.use_tls, n.tls_verify,
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
    with get_conn(_db_path(request)) as conn:
        node = fetch_one_dict(conn, "SELECT * FROM nodes WHERE id = ?", (node_id,))
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
    with get_conn(_db_path(request)) as conn:
        nodes = fetch_all_dict(conn, "SELECT * FROM nodes ORDER BY name")

    return templates.TemplateResponse(
        request,
        "settings.html",
        {"nodes": nodes, "page": "settings", "default_port": request.app.state.settings.agent_default_port},
    )


@router.get("/settings/node-form", response_class=HTMLResponse)
def node_form_partial(request: Request, node_id: int | None = None) -> HTMLResponse:
    with get_conn(_db_path(request)) as conn:
        node = fetch_one_dict(conn, "SELECT * FROM nodes WHERE id = ?", (node_id,)) if node_id else None

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
    token: str = Form(...),
    role: str = Form(default="worker"),
    agent_port: int = Form(default=8001),
    use_tls: str = Form(default="false"),
    tls_verify: str = Form(default="true"),
    poll_interval_seconds: int = Form(default=10),
    enabled: str = Form(default="true"),
) -> RedirectResponse:
    use_tls_bool = str(use_tls).strip().lower() in {"1", "true", "yes", "on"}
    tls_verify_bool = str(tls_verify).strip().lower() in {"1", "true", "yes", "on"}
    enabled_bool = str(enabled).strip().lower() in {"1", "true", "yes", "on"}
    now_iso = utc_now_iso()
    with get_conn(_db_path(request)) as conn:
        if id:
            conn.execute(
                """
                UPDATE nodes
                SET name=?, hostname=?, ip_address=?, token=?, role=?,
                    agent_port=?, use_tls=?, tls_verify=?, poll_interval_seconds=?, enabled=?, updated_at=?
                WHERE id=?
                """,
                (
                    name.strip(),
                    hostname.strip(),
                    ip_address.strip(),
                    token.strip(),
                    role.strip() or "worker",
                    int(agent_port),
                    1 if use_tls_bool else 0,
                    1 if tls_verify_bool else 0,
                    max(3, int(poll_interval_seconds)),
                    1 if enabled_bool else 0,
                    now_iso,
                    id,
                ),
            )
        else:
            conn.execute(
                """
                INSERT INTO nodes (
                    name, hostname, ip_address, token, role,
                    agent_port, use_tls, tls_verify, poll_interval_seconds, enabled,
                    created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    name.strip(),
                    hostname.strip(),
                    ip_address.strip(),
                    token.strip(),
                    role.strip() or "worker",
                    int(agent_port),
                    1 if use_tls_bool else 0,
                    1 if tls_verify_bool else 0,
                    max(3, int(poll_interval_seconds)),
                    1 if enabled_bool else 0,
                    now_iso,
                    now_iso,
                ),
            )

    return RedirectResponse(url="/settings", status_code=303)


@router.post("/settings/nodes/{node_id}/toggle")
def toggle_node(request: Request, node_id: int) -> RedirectResponse:
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
    with get_conn(_db_path(request)) as conn:
        node = fetch_one_dict(conn, "SELECT * FROM nodes WHERE id = ?", (node_id,))
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

    with get_conn(_db_path(request)) as conn:
        existing = fetch_one_dict(
            conn,
            """
            SELECT id
            FROM nodes
            WHERE hostname = ? OR ip_address = ?
            ORDER BY id ASC
            LIMIT 1
            """,
            (payload.hostname.strip(), resolved_ip),
        )
        if existing:
            node_id = int(existing["id"])
            conn.execute(
                """
                UPDATE nodes
                SET name = ?, hostname = ?, ip_address = ?, token = ?, role = ?, agent_port = ?,
                    poll_interval_seconds = ?, enabled = 1, enrollment_status = 'enrolled',
                    enrolled_at = ?, token_issued_at = ?, token_expires_at = ?,
                    previous_token = NULL, previous_token_expires_at = NULL,
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    resolved_name,
                    payload.hostname.strip(),
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
                    name, hostname, ip_address, token, role, agent_port, poll_interval_seconds,
                    enabled, enrollment_status, enrolled_at, token_issued_at, token_expires_at,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 1, 'enrolled', ?, ?, ?, ?, ?)
                """,
                (
                    resolved_name,
                    payload.hostname.strip(),
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

    return EnrollmentResponse(node_id=node_id, token=issued_token, status="enrolled")


@router.post("/api/v1/token/refresh", response_model=TokenRefreshResponse)
def api_token_refresh(request: Request, payload: TokenRefreshRequest) -> TokenRefreshResponse:
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing token")
    presented_token = auth_header.removeprefix("Bearer ").strip()
    if not presented_token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing token")

    with get_conn(_db_path(request)) as conn:
        node = fetch_one_dict(
            conn,
            """
            SELECT id, token, token_expires_at, previous_token, previous_token_expires_at
            FROM nodes
            WHERE hostname = ?
            LIMIT 1
            """,
            (payload.hostname.strip(),),
        )
        if not node:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Node not found")

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
        expires_at=datetime.fromisoformat(expires_at_iso),
        status="refreshed",
    )
