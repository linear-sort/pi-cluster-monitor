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
