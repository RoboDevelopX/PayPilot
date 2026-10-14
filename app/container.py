"""Wires the app's parts together once, so routers just ask for what they need."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import httpx
from sqlalchemy.orm import Session, sessionmaker

from app.agent.gemini import GeminiCreate
from app.agent.llm import Agent, anthropic_create
from app.config import Settings
from app.paypal import client_from_settings
from app.paypal.client import PayPalClient
from app.paypal.invoices import InvoicingAPI
from app.paypal.payouts import PayoutsAPI
from app.services.actions import ActionService
from app.services.approvals import ApprovalService
from app.services.conversations import ConversationStore
from app.services.followups import LLMDrafter, TemplateDrafter
from app.services.ml import MLScorers
from app.services.ratelimit import RateLimiter


@dataclass
class Container:
    settings: Settings
    sessions: sessionmaker[Session]
    paypal: PayPalClient | None
    actions: ActionService
    approvals: ApprovalService
    agent: Agent | None
    conversations: ConversationStore
    chat_limiter: RateLimiter
    ml: MLScorers


def make_llm(settings: Settings) -> Callable[..., Any] | None:
    """Pick the LLM backend. Both expose the same call signature to the agent."""
    if settings.active_llm == "anthropic":
        return anthropic_create(settings.anthropic_api_key.get_secret_value())
    if settings.active_llm == "gemini":
        return GeminiCreate(settings.gemini_api_key.get_secret_value(), model=settings.gemini_model)
    return None


def build_container(
    settings: Settings,
    sessions: sessionmaker[Session],
    *,
    paypal_transport: httpx.BaseTransport | None = None,
    llm_create: Callable[..., Any] | None = None,
) -> Container:
    paypal = None
    invoicing = None
    payouts = None
    if settings.paypal_configured:
        paypal = client_from_settings(settings, transport=paypal_transport)
        invoicing = InvoicingAPI(
            paypal,
            invoicer_email=settings.paypal_invoicer_email,
            business_name=settings.business_name,
        )
        payouts = PayoutsAPI(paypal)
    if llm_create is None:
        llm_create = make_llm(settings)
    model = settings.llm_model or settings.anthropic_model
    drafter = LLMDrafter(llm_create, model=model) if llm_create else TemplateDrafter()
    ml = MLScorers.load() if settings.ml_enabled else MLScorers()
    actions = ActionService(
        sessions,
        invoicing=invoicing,
        payouts=payouts,
        risk_scorer=ml.score_invoice if ml.risk else None,
        anomaly_scorer=ml.score_payout if ml.anomaly else None,
        drafter=drafter,
        business_name=settings.business_name,
    )
    agent = (
        Agent(
            llm_create,
            handle_action=actions.handle,
            model=model,
            effort=settings.anthropic_effort,
        )
        if llm_create
        else None
    )
    return Container(
        settings=settings,
        sessions=sessions,
        paypal=paypal,
        actions=actions,
        approvals=ApprovalService(actions),
        agent=agent,
        conversations=ConversationStore(),
        chat_limiter=RateLimiter(settings.chat_rate_limit_per_minute),
        ml=ml,
    )
