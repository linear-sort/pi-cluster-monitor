from __future__ import annotations

import os

from pydantic import BaseModel


class AgentSettings(BaseModel):
    token: str = "changeme"
    name: str = ""
    services: list[str] = []


def get_settings() -> AgentSettings:
    services_raw = os.getenv("AGENT_SERVICES", "").strip()
    services = [item.strip() for item in services_raw.split(",") if item.strip()]
    return AgentSettings(
        token=os.getenv("AGENT_TOKEN", "changeme"),
        name=os.getenv("AGENT_NAME", ""),
        services=services,
    )
