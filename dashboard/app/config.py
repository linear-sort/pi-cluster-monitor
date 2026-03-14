from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import os

from pydantic import BaseModel


def _parse_expiry_epoch(value: str) -> float | None:
    cleaned = value.strip()
    if not cleaned:
        return None
    try:
        return float(cleaned)
    except ValueError:
        pass
    try:
        dt = datetime.fromisoformat(cleaned.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except ValueError:
        return None


def _parse_operator_credentials(raw: str) -> dict[str, dict[str, str | float | None]]:
    # Format: token:principal:role[:expires_at][:status]
    credentials: dict[str, dict[str, str | float | None]] = {}
    valid_roles = {"viewer", "operator", "admin"}
    valid_status = {"active", "revoked"}
    for part in raw.split(","):
        chunk = part.strip()
        if not chunk:
            continue
        pieces = [item.strip() for item in chunk.split(":")]
        if len(pieces) < 3:
            continue
        token, principal, role = pieces[0], pieces[1], pieces[2]
        if not token or not principal or role not in valid_roles:
            continue
        expires_at_epoch = _parse_expiry_epoch(pieces[3]) if len(pieces) >= 4 else None
        status = pieces[4].strip().lower() if len(pieces) >= 5 else "active"
        if status not in valid_status:
            status = "active"
        credentials[token] = {
            "principal": principal,
            "role": role,
            "expires_at_epoch": expires_at_epoch,
            "status": status,
        }
    return credentials


class Settings(BaseModel):
    db_path: Path
    poll_base_seconds: int = 2
    http_timeout_seconds: int = 4
    agent_default_port: int = 8001
    enroll_secret: str = "changeme-enroll"
    token_ttl_seconds: int = 86400
    token_grace_seconds: int = 300
    poll_failure_threshold: int = 3
    poll_circuit_cooldown_seconds: int = 60
    metric_retention_hours: int = 48
    alert_event_retention_days: int = 14
    service_retention_days: int = 7
    cleanup_interval_seconds: int = 300
    webhook_url: str = ""
    webhook_timeout_seconds: int = 3
    webhook_retry_base_seconds: int = 15
    webhook_max_attempts: int = 5
    webhook_dispatch_interval_seconds: int = 5
    node_token_key: str = ""
    operator_credentials: dict[str, dict[str, str | float | None]] = {}
    operator_auth_window_seconds: int = 60
    operator_auth_max_failures: int = 5
    operator_auth_lockout_seconds: int = 120


def get_settings() -> Settings:
    root = Path(__file__).resolve().parents[2]
    db_default = root / "dashboard" / "data" / "cluster.db"
    db_path = Path(os.getenv("DASHBOARD_DB_PATH", str(db_default)))

    return Settings(
        db_path=db_path,
        poll_base_seconds=int(os.getenv("DASHBOARD_POLL_BASE_SECONDS", "2")),
        http_timeout_seconds=int(os.getenv("DASHBOARD_HTTP_TIMEOUT_SECONDS", "4")),
        agent_default_port=int(os.getenv("DASHBOARD_AGENT_DEFAULT_PORT", "8001")),
        enroll_secret=os.getenv("DASHBOARD_ENROLL_SECRET", "changeme-enroll"),
        token_ttl_seconds=max(60, int(os.getenv("DASHBOARD_TOKEN_TTL_SECONDS", "86400"))),
        token_grace_seconds=max(30, int(os.getenv("DASHBOARD_TOKEN_GRACE_SECONDS", "300"))),
        poll_failure_threshold=max(1, int(os.getenv("DASHBOARD_POLL_FAILURE_THRESHOLD", "3"))),
        poll_circuit_cooldown_seconds=max(5, int(os.getenv("DASHBOARD_POLL_CIRCUIT_COOLDOWN_SECONDS", "60"))),
        metric_retention_hours=int(os.getenv("DASHBOARD_METRIC_RETENTION_HOURS", "48")),
        alert_event_retention_days=int(os.getenv("DASHBOARD_ALERT_RETENTION_DAYS", "14")),
        service_retention_days=int(os.getenv("DASHBOARD_SERVICE_RETENTION_DAYS", "7")),
        cleanup_interval_seconds=int(os.getenv("DASHBOARD_CLEANUP_INTERVAL_SECONDS", "300")),
        webhook_url=os.getenv("DASHBOARD_WEBHOOK_URL", "").strip(),
        webhook_timeout_seconds=max(1, int(os.getenv("DASHBOARD_WEBHOOK_TIMEOUT_SECONDS", "3"))),
        webhook_retry_base_seconds=max(3, int(os.getenv("DASHBOARD_WEBHOOK_RETRY_BASE_SECONDS", "15"))),
        webhook_max_attempts=max(1, int(os.getenv("DASHBOARD_WEBHOOK_MAX_ATTEMPTS", "5"))),
        webhook_dispatch_interval_seconds=max(1, int(os.getenv("DASHBOARD_WEBHOOK_DISPATCH_INTERVAL_SECONDS", "5"))),
        node_token_key=os.getenv("DASHBOARD_NODE_TOKEN_KEY", "").strip(),
        operator_credentials=_parse_operator_credentials(os.getenv("DASHBOARD_OPERATOR_CREDENTIALS", "").strip()),
        operator_auth_window_seconds=max(10, int(os.getenv("DASHBOARD_OPERATOR_AUTH_WINDOW_SECONDS", "60"))),
        operator_auth_max_failures=max(2, int(os.getenv("DASHBOARD_OPERATOR_AUTH_MAX_FAILURES", "5"))),
        operator_auth_lockout_seconds=max(10, int(os.getenv("DASHBOARD_OPERATOR_AUTH_LOCKOUT_SECONDS", "120"))),
    )
