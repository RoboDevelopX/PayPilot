"""FastAPI application factory.

Why this works (FastAPI structure): `create_app()` builds a fresh app from a
`Settings` object, so tests can spin up an isolated app with an in-memory DB
and a fake PayPal transport, while `uvicorn app.main:create_app --factory` uses the real config.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI
from fastapi.concurrency import run_in_threadpool
from fastapi.staticfiles import StaticFiles

from app.auth import BasicAuthMiddleware
from app.config import Settings, get_settings
from app.container import build_container
from app.db import init_db, make_engine, make_session_factory
from app.routers import approvals, chat, invoices, ui
from app.services.seed import seed_demo

log = logging.getLogger("paypilot")
STATIC_DIR = Path(__file__).resolve().parent / "static"


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

    async def followup_loop(app: FastAPI) -> None:
        # Polling fallback + automatic reminders, every FOLLOWUP_INTERVAL_SECONDS.
        while True:
            await asyncio.sleep(settings.followup_interval_seconds)
            try:
                await run_in_threadpool(app.state.container.actions.sweep_followups)
            except Exception:  # keep the loop alive; the next tick retries
                log.exception("follow-up sweep failed")

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        task = asyncio.create_task(followup_loop(app)) if settings.followups_enabled else None
        yield
        if task:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        engine.dispose()

    app = FastAPI(title="PayPilot", version="0.2.0", lifespan=lifespan)
    if settings.app_password.get_secret_value():
        app.add_middleware(
            BasicAuthMiddleware,
            username=settings.app_username,
            password=settings.app_password.get_secret_value(),
        )
    app.state.settings = settings
    app.state.engine = engine
    app.state.sessions = sessions
    app.state.container = build_container(
        settings, sessions, paypal_transport=paypal_transport, llm_create=llm_create
    )
    if settings.demo_mode and seed_demo(app.state.container.actions, app.state.container.ml):
        log.info("DEMO_MODE: loaded sample data into an empty database")
    app.include_router(chat.router)
    app.include_router(invoices.router)
    app.include_router(approvals.router)
    app.include_router(ui.router)
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.get("/healthz")
    def healthz() -> dict[str, object]:
        return {
            "ok": True,
            "paypal": (
                "sandbox"
                if settings.paypal_configured
                else (
                    "simulated (demo recordings)" if settings.paypal_simulated else "not configured"
                )
            ),
            "llm": settings.assistant_label,
            "ml": app.state.container.ml.status,
            "demo_mode": settings.demo_mode,
        }

    return app
