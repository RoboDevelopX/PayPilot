"""FastAPI application factory.

Why this works (FastAPI structure): `create_app()` builds a fresh app from a
`Settings` object, so tests can spin up an isolated app with an in-memory DB
and a fake PayPal transport, while `uvicorn app.main:create_app --factory` uses the real config.
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import asynccontextmanager
from typing import Any

import httpx
from fastapi import FastAPI
from fastapi.responses import HTMLResponse

from app.config import Settings, get_settings
from app.container import build_container
from app.db import init_db, make_engine, make_session_factory
from app.routers import chat


def create_app(
    settings: Settings | None = None,
    *,
    paypal_transport: httpx.BaseTransport | None = None,
    llm_create: Callable[..., Any] | None = None,
) -> FastAPI:
    settings = settings or get_settings()
    engine = make_engine(settings.database_url)
    init_db(engine)
    sessions = make_session_factory(engine)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        yield
        engine.dispose()

    app = FastAPI(title="PayPilot", version="0.2.0", lifespan=lifespan)
    app.state.settings = settings
    app.state.engine = engine
    app.state.sessions = sessions
    app.state.container = build_container(
        settings, sessions, paypal_transport=paypal_transport, llm_create=llm_create
    )
    app.include_router(chat.router)

    @app.get("/healthz")
    def healthz() -> dict[str, object]:
        return {
            "ok": True,
            "paypal": "sandbox" if settings.paypal_configured else "not configured",
            "llm": settings.anthropic_model if settings.anthropic_configured else "not configured",
            "demo_mode": settings.demo_mode,
        }

    @app.get("/", response_class=HTMLResponse)
    def index() -> str:
        return "<h1>PayPilot (sandbox)</h1><p>UI arrives in M7. See /docs for the API.</p>"

    return app
