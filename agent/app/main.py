from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from dotenv import load_dotenv

from app.config import get_settings
from app.routes import router

load_dotenv(Path(__file__).resolve().parents[1] / '.env')


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.settings = get_settings()
    yield


app = FastAPI(title="Pi Node Agent", lifespan=lifespan)
app.include_router(router)
