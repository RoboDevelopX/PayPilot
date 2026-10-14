"""Approval inbox, payouts, rules, recipient allowlist and the audit log."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import select

from app.container import Container
from app.models import AuditEvent, Payout, Recipient
from app.policy.engine import PolicyError
from app.services.approvals import (
    ApprovalError,
    ApprovalRequest,
    set_allowlisted,
    update_policy_from_ui,
)
from app.services.store import load_policy, spend

router = APIRouter()


class ApproveIn(BaseModel):
    acknowledge_anomaly: bool = False
    allowlist_recipient: bool = False


class RecipientIn(BaseModel):
    email: EmailStr
    name: str = Field(default="", max_length=100)
    allowlisted: bool = True


def _c(request: Request) -> Container:
    return request.app.state.container


@router.get("/api/approvals")
def list_approvals(request: Request) -> list[dict[str, Any]]:
    return _c(request).approvals.pending()


@router.post("/api/approvals/{approval_id}/approve")
def approve(approval_id: str, request: Request, body: ApproveIn | None = None) -> dict[str, Any]:
    body = body or ApproveIn()
    try:
        out = _c(request).approvals.approve(
            approval_id, ApprovalRequest(body.acknowledge_anomaly, body.allowlist_recipient)
        )
    except ApprovalError as e:
        raise HTTPException(e.status_code, str(e)) from e
    return out.to_dict()


@router.post("/api/approvals/{approval_id}/reject")
def reject(approval_id: str, request: Request) -> dict[str, Any]:
    try:
        return _c(request).approvals.reject(approval_id).to_dict()
    except ApprovalError as e:
        raise HTTPException(e.status_code, str(e)) from e


def payout_row(p: Payout) -> dict[str, Any]:
    return {
        "id": p.id,
        "recipient": p.recipient.email,
        "recipient_name": p.recipient.name,
        "amount": f"{p.amount:.2f}",
        "currency": p.currency,
        "note": p.note,
        "status": p.status,
        "paypal_status": p.paypal_status,
        "paypal_batch_id": p.paypal_batch_id,
        "anomaly_flag": p.anomaly_flag,
        "anomaly_score": p.anomaly_score,
        "anomaly_reason": p.anomaly_reason,
        "created_at": p.created_at.isoformat(),
        "is_seeded": p.is_seeded,
    }


@router.get("/api/payouts")
def list_payouts(request: Request) -> list[dict[str, Any]]:
    with _c(request).sessions() as s:
        rows = s.scalars(select(Payout).order_by(Payout.created_at.desc()).limit(200))
        return [payout_row(p) for p in rows]


@router.get("/api/policy")
def get_policy(request: Request) -> dict[str, Any]:
    c = _c(request)
    with c.sessions() as s:
        policy = load_policy(s)
        spent = spend(s, "USD", c.actions.clock())
        return {
            **policy.to_dict(),
            "spent_today": f"{spent.today:.2f}",
            "spent_month": f"{spent.month:.2f}",
        }


@router.put("/api/policy")
def put_policy(body: dict[str, Any], request: Request) -> dict[str, Any]:
    try:
        return update_policy_from_ui(_c(request).actions, body).to_dict()
    except PolicyError as e:
        raise HTTPException(422, str(e)) from e


@router.get("/api/recipients")
def list_recipients(request: Request) -> list[dict[str, Any]]:
    with _c(request).sessions() as s:
        return [
            {
                "email": r.email,
                "name": r.name,
                "allowlisted": r.allowlisted_at is not None,
            }
            for r in s.scalars(select(Recipient).order_by(Recipient.email))
        ]


@router.post("/api/recipients")
def upsert_recipient(body: RecipientIn, request: Request) -> dict[str, Any]:
    r = set_allowlisted(_c(request).actions, str(body.email), body.name, body.allowlisted)
    return {"email": r.email, "name": r.name, "allowlisted": r.allowlisted_at is not None}


@router.get("/api/audit")
def list_audit(
    request: Request, action_id: str | None = None, limit: int = 100, before_id: int | None = None
) -> list[dict[str, Any]]:
    limit = max(1, min(limit, 500))
    q = select(AuditEvent).order_by(AuditEvent.id.desc()).limit(limit)
    if action_id:
        q = q.where(AuditEvent.action_id == action_id)
    if before_id:
        q = q.where(AuditEvent.id < before_id)
    with _c(request).sessions() as s:
        return [
            {
                "id": e.id,
                "ts": e.ts.isoformat(),
                "actor": e.actor,
                "event": e.event,
                "action_id": e.action_id,
                "paypal_request_id": e.paypal_request_id,
                "paypal_debug_id": e.paypal_debug_id,
                "detail": e.detail,
            }
            for e in s.scalars(q)
        ]
