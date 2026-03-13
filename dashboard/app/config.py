from __future__ import annotations

from pathlib import Path
import os

from pydantic import BaseModel


def _parse_operator_credentials(raw: str) -> dict[str, dict[str, str]]:
    # Format: token:principal:role,token2:principal2:role2
    credentials: dict[str, dict[str, str]] = {}
    valid_roles = {"viewer", "operator", "admin"}
    for part in raw.split(","):
        chunk = part.strip()
        if not chunk:
            continue
        pieces = [item.strip() for item in chunk.split(":", 2)]
        if len(pieces) != 3:
            continue
        token, principal, role = pieces
        if not token or not principal or role not in valid_roles:
            continue
        credentials[token] = {"principal": principal, "role": role}
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
    operator_credentials: dict[str, dict[str, str]] = {}


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
        operator_credentials=_parse_operator_credentials(os.getenv("DASHBOARD_OPERATOR_CREDENTIALS", "").strip()),
    )
