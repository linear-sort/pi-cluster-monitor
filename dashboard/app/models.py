from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field


class NodeForm(BaseModel):
    id: Optional[int] = None
    name: str = Field(min_length=1, max_length=100)
    hostname: str = Field(min_length=1, max_length=255)
    ip_address: str = Field(min_length=1, max_length=255)
    token: str = Field(min_length=1, max_length=255)
    role: str = Field(default="worker", max_length=50)
    agent_port: int = 8001
    poll_interval_seconds: int = 10
    enabled: bool = True


class AgentMetrics(BaseModel):
    hostname: str
    timestamp: datetime
    cpu_percent: float
    memory_percent: float
    disk_percent: float
    temperature_c: Optional[float] = None
    uptime_seconds: int
    load_1: float
    load_5: float
    load_15: float
    rx_bytes: int
    tx_bytes: int


class ClusterStats(BaseModel):
    total_nodes: int
    online_nodes: int
    offline_nodes: int
    warning_nodes: int
    critical_nodes: int
    avg_cpu_percent: float
    hottest_node_name: Optional[str] = None
    hottest_node_temp: Optional[float] = None


class EnrollmentRequest(BaseModel):
    enroll_secret: str
    hostname: str = Field(min_length=1, max_length=255)
    name: str | None = None
    ip_address: str | None = None
    agent_port: int = 8001
    role: str = Field(default="worker", max_length=50)
    poll_interval_seconds: int = 10


class EnrollmentResponse(BaseModel):
    node_id: int
    token: str
    token_version: int
    status: str


class TokenRefreshRequest(BaseModel):
    hostname: str = Field(min_length=1, max_length=255)
    token_version: int | None = None


class TokenRefreshResponse(BaseModel):
    node_id: int
    token: str
    token_version: int
    expires_at: datetime
    status: str


class IngestMetricsRequest(BaseModel):
    hostname: str
    timestamp: datetime
    cpu_percent: float
    memory_percent: float
    disk_percent: float
    temperature_c: float | None = None
    uptime_seconds: int
    load_1: float
    load_5: float
    load_15: float
    rx_bytes: int
    tx_bytes: int


class RevokeTokenRequest(BaseModel):
    actor: str = Field(default="dashboard-operator", min_length=1, max_length=120)
    reason: str = Field(default="manual_revoke", min_length=1, max_length=200)


class RevokeTokenResponse(BaseModel):
    node_id: int
    revoked_at: datetime
    token_version: int
    status: str


class BulkNodeUpdateRequest(BaseModel):
    node_ids: list[int] = Field(min_length=1)
    action: str = Field(min_length=1, max_length=40)
    actor: str = Field(default="dashboard-operator", min_length=1, max_length=120)
    reason: str = Field(default="bulk_update", min_length=1, max_length=200)
    enabled: bool | None = None
    poll_interval_seconds: int | None = None
    role: str | None = Field(default=None, max_length=50)


class BulkNodeUpdateResponse(BaseModel):
    affected_count: int
    action: str
    status: str