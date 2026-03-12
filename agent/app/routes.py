from __future__ import annotations

from datetime import datetime, timezone
import socket

from fastapi import APIRouter, Depends, Request

from app.auth import auth_dependency
from app.collectors.system_metrics import collect_metrics, collect_services
from app.models import HealthResponse, ServicesResponse


router = APIRouter()


@router.get("/health", response_model=HealthResponse, dependencies=[Depends(auth_dependency)])
def health(request: Request) -> HealthResponse:
    hostname = request.app.state.settings.name or socket.gethostname()
    return HealthResponse(status="ok", hostname=hostname, timestamp=datetime.now(timezone.utc))


@router.get("/api/v1/metrics", dependencies=[Depends(auth_dependency)])
def metrics(request: Request):
    return collect_metrics(agent_name=request.app.state.settings.name).model_dump()


@router.get("/api/v1/services", response_model=ServicesResponse, dependencies=[Depends(auth_dependency)])
def services(request: Request) -> ServicesResponse:
    settings = request.app.state.settings
    hostname = settings.name or socket.gethostname()
    return ServicesResponse(
        hostname=hostname,
        timestamp=datetime.now(timezone.utc),
        services=collect_services(settings.services),
    )
