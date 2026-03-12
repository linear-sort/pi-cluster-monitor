from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from dotenv import load_dotenv
from fastapi.staticfiles import StaticFiles

from app.config import get_settings
from app.db import ensure_db
from app.routes.web import router as web_router
from app.services.poller import PollingService

load_dotenv(Path(__file__).resolve().parents[1] / '.env')


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    ensure_db(settings.db_path)

    app.state.settings = settings
    app.state.poller = PollingService(
        db_path=settings.db_path,
        base_tick_seconds=settings.poll_base_seconds,
        timeout_seconds=settings.http_timeout_seconds,
        metric_retention_hours=settings.metric_retention_hours,
        alert_event_retention_days=settings.alert_event_retention_days,
        service_retention_days=settings.service_retention_days,
        cleanup_interval_seconds=settings.cleanup_interval_seconds,
    )
    app.state.poller.start()

    yield

    await app.state.poller.stop()


app = FastAPI(title="Pi Cluster Dashboard", lifespan=lifespan)
app.include_router(web_router)

STATIC_DIR = Path(__file__).resolve().parents[1] / "static"
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
