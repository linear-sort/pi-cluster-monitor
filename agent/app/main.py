from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from dotenv import load_dotenv

from app.config import get_settings
from app.enrollment import enrollment_loop, load_token_from_file
from app.routes import router

load_dotenv(Path(__file__).resolve().parents[1] / '.env')


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    app.state.settings = settings
    app.state.auth_token = load_token_from_file(settings.token_file) or settings.token
    app.state.enrollment_task = None

    should_start_auth_loop = bool(settings.dashboard_url) and (
        (settings.enroll_enabled and settings.enroll_secret) or settings.token_refresh_enabled
    )
    if should_start_auth_loop:
        app.state.enrollment_task = asyncio.create_task(enrollment_loop(app))
    yield
    task = app.state.enrollment_task
    if task:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


app = FastAPI(title="Pi Node Agent", lifespan=lifespan)
app.include_router(router)
