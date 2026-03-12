from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel


class HealthResponse(BaseModel):
    status: str
    hostname: str
    timestamp: datetime


class MetricsResponse(BaseModel):
    hostname: str
    timestamp: datetime
    cpu_percent: float
    memory_percent: float
    disk_percent: float
    temperature_c: float | None
    uptime_seconds: int
    load_1: float
    load_5: float
    load_15: float
    rx_bytes: int
    tx_bytes: int


class ServiceStatus(BaseModel):
    name: str
    status: str


class ServicesResponse(BaseModel):
    hostname: str
    timestamp: datetime
    services: list[ServiceStatus]
