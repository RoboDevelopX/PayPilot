"""Live check against the real Claude API. Skipped unless ANTHROPIC_API_KEY is set."""

from __future__ import annotations

import pytest

from app.agent.llm import Agent, anthropic_create
from app.config import get_settings
from app.db import init_db, make_engine, make_session_factory
from app.services.actions import ActionService

settings = get_settings()
pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(not settings.anthropic_configured, reason="ANTHROPIC_API_KEY not set"),
]


def test_missing_email_triggers_clarification_not_an_action():
    engine = make_engine("sqlite://")
    init_db(engine)
    service = ActionService(make_session_factory(engine))  # no PayPal: nothing can execute
    agent = Agent(
        anthropic_create(settings.anthropic_api_key.get_secret_value()),
        handle_action=service.handle,
        model=settings.anthropic_model,
        effort="low",
    )
    reply = agent.respond([], "Invoice Acme Corp $450 for logo design, due in 14 days")
    assert reply.clarification, reply.text
    assert "email" in reply.clarification.lower()
    assert reply.outcomes == []
