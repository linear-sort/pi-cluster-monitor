from __future__ import annotations

import os
from pathlib import Path

from pydantic import BaseModel


class AgentSettings(BaseModel):
    token: str = "changeme"
    name: str = ""
    services: list[str] = []
    dashboard_url: str = ""
    enroll_secret: str = ""
    enroll_enabled: bool = True
    enroll_retry_seconds: int = 10
    token_refresh_enabled: bool = True
    token_refresh_seconds: int = 3600
    push_enabled: bool = False
    push_interval_seconds: int = 10
    token_file: Path = Path("agent_token.txt")
    agent_id: str = ""
    agent_id_file: Path = Path("agent_id.txt")


def get_settings() -> AgentSettings:
    services_raw = os.getenv("AGENT_SERVICES", "").strip()
    services = [item.strip() for item in services_raw.split(",") if item.strip()]
    token_file = Path(os.getenv("AGENT_TOKEN_FILE", "agent_token.txt"))
    agent_id_file = Path(os.getenv("AGENT_ID_FILE", "agent_id.txt"))
    return AgentSettings(
        token=os.getenv("AGENT_TOKEN", "changeme"),
        name=os.getenv("AGENT_NAME", ""),
        services=services,
        dashboard_url=os.getenv("AGENT_DASHBOARD_URL", "").strip().rstrip("/"),
        enroll_secret=os.getenv("AGENT_ENROLL_SECRET", "").strip(),
        enroll_enabled=os.getenv("AGENT_ENROLL_ENABLED", "true").strip().lower() in {"1", "true", "yes", "on"},
        enroll_retry_seconds=max(3, int(os.getenv("AGENT_ENROLL_RETRY_SECONDS", "10"))),
        token_refresh_enabled=os.getenv("AGENT_TOKEN_REFRESH_ENABLED", "true").strip().lower() in {"1", "true", "yes", "on"},
        token_refresh_seconds=max(30, int(os.getenv("AGENT_TOKEN_REFRESH_SECONDS", "3600"))),
        push_enabled=os.getenv("AGENT_PUSH_ENABLED", "false").strip().lower() in {"1", "true", "yes", "on"},
        push_interval_seconds=max(3, int(os.getenv("AGENT_PUSH_INTERVAL_SECONDS", "10"))),
        token_file=token_file,
        agent_id=os.getenv("AGENT_ID", "").strip(),
        agent_id_file=agent_id_file,
    )
