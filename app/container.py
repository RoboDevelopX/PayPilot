"""Wires the app's parts together once, so routers just ask for what they need."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import httpx
from sqlalchemy.orm import Session, sessionmaker

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
    if llm_create is None and settings.anthropic_configured:
        llm_create = anthropic_create(settings.anthropic_api_key.get_secret_value())
    drafter = (
        LLMDrafter(llm_create, model=settings.anthropic_model) if llm_create else TemplateDrafter()
    )
    actions = ActionService(
        sessions,
        invoicing=invoicing,
        payouts=payouts,
        drafter=drafter,
        business_name=settings.business_name,
    )
    agent = (
        Agent(
            llm_create,
            handle_action=actions.handle,
            model=settings.anthropic_model,
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
    )
