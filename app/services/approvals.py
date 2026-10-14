"""The approval inbox: the only way a held action can run.

There is no chat tool for approving anything, so the model can never approve
its own proposal; approvals come only from the UI/API. Each approval is
single-use (an atomic UPDATE ... WHERE decision IS NULL), and approving re-runs
the hard rules, because spend may have changed since the action was proposed.
An escalated (anomaly-flagged) approval also needs an explicit acknowledgement.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from sqlalchemy import update

from app.db import session_scope
from app.models import Action, Approval, Payout, Recipient, utcnow
from app.policy.engine import Level, Policy, PolicyError, evaluate_payout
from app.services import audit
from app.services.actions import ActionService, Outcome
from app.services.store import (
    get_or_create_recipient,
    load_policy,
    save_policy,
    spend,
)


class ApprovalError(Exception):
    def __init__(self, message: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.status_code = status_code


@dataclass
class ApprovalRequest:
    acknowledge_anomaly: bool = False
    allowlist_recipient: bool = False


class ApprovalService:
    def __init__(self, actions: ActionService) -> None:
        self.actions = actions
        self.sessions = actions.sessions

    def pending(self) -> list[dict[str, Any]]:
        with self.sessions() as s:
            rows = (
                s.query(Approval)
                .filter(Approval.decision.is_(None))
                .order_by(Approval.created_at.desc())
                .all()
            )
            return [self._row(a) for a in rows]

    @staticmethod
    def _row(a: Approval) -> dict[str, Any]:
        act = a.action
        return {
            "id": a.id,
            "level": a.level,
            "kind": act.kind,
            "payload": act.payload,
            "reasons": act.reasons or [],
            "signals": act.signals or {},
            "source": act.source,
            "created_at": a.created_at.isoformat(),
        }

    def _claim(self, approval_id: str, decision: str) -> Approval:
        """Atomically mark the approval decided. Fails if it was already used."""
        with session_scope(self.sessions) as s:
            a = s.get(Approval, approval_id)
            if a is None:
                raise ApprovalError("approval not found", 404)
            res = s.execute(
                update(Approval)
                .where(Approval.id == approval_id, Approval.decision.is_(None))
                .values(decision=decision, decided_at=utcnow(), consumed_at=utcnow())
            )
            if res.rowcount != 1:
                raise ApprovalError("this approval was already used", 409)
            s.refresh(a)
            return a

    def reject(self, approval_id: str) -> Outcome:
        a = self._claim(approval_id, "rejected")
        with session_scope(self.sessions) as s:
            act = s.get(Action, a.action_id)
            act.status = "rejected"
            audit.record(s, actor="user", event="approval_rejected", action_id=act.id)
        return Outcome("rejected", "Rejected; nothing was sent.", a.action_id)

    def approve(self, approval_id: str, req: ApprovalRequest | None = None) -> Outcome:
        req = req or ApprovalRequest()
        with self.sessions() as s:
            a = s.get(Approval, approval_id)
            if a is None:
                raise ApprovalError("approval not found", 404)
            if a.level == "escalated" and not req.acknowledge_anomaly:
                raise ApprovalError(
                    "this payout was flagged as unusual; confirm you checked it before approving"
                )
        a = self._claim(approval_id, "approved")
        with session_scope(self.sessions) as s:
            act = s.get(Action, a.action_id)
            audit.record(
                s,
                actor="user",
                event="approval_granted",
                action_id=act.id,
                level=a.level,
                acknowledged_anomaly=req.acknowledge_anomaly,
                allowlist_recipient=req.allowlist_recipient,
            )
            kind = act.kind
        if kind == "send_payout":
            return self._approve_payout(a.action_id, req)
        if kind == "send_reminder":
            return self._approve_reminder(a.action_id)
        if kind == "update_policy":
            return self._approve_policy(a.action_id)
        raise ApprovalError(f"cannot approve {kind}")

    def _approve_payout(self, action_id: str, req: ApprovalRequest) -> Outcome:
        with session_scope(self.sessions) as s:
            act = s.get(Action, action_id)
            p = act.payload
            amount = Decimal(p["amount"])
            recipient = get_or_create_recipient(s, email=p["recipient_email"])
            # Approval covers "above L", "new recipient" and "flagged"; the caps still apply.
            decision = evaluate_payout(
                amount=amount,
                currency=p["currency"],
                recipient=recipient.email,
                policy=load_policy(s),
                spend=spend(s, p["currency"], self.actions.clock()),
                recipient_allowlisted=True,
            )
            audit.record(
                s, actor="policy", event="policy_recheck", action_id=action_id, **decision.to_dict()
            )
            if decision.level == Level.BLOCK:
                act.status = "blocked"
                return Outcome("blocked", decision.level.label, action_id, list(decision.reasons))
            if req.allowlist_recipient and recipient.allowlisted_at is None:
                recipient.allowlisted_at = utcnow()
                audit.record(
                    s,
                    actor="user",
                    event="recipient_allowlisted",
                    action_id=action_id,
                    email=recipient.email,
                )
            signals = act.signals or {}
            s.add(
                Payout(
                    action_id=action_id,
                    recipient_id=recipient.id,
                    amount=amount,
                    currency=p["currency"],
                    note=p.get("note", ""),
                    status="approved",
                    created_at=self.actions.clock(),
                    anomaly_score=signals.get("anomaly_score"),
                    anomaly_flag=bool(signals.get("anomaly_flag")),
                    anomaly_reason=signals.get("anomaly_reason"),
                )
            )
            act.status = "approved"
        return self.actions.execute_payout(action_id)

    def _approve_reminder(self, action_id: str) -> Outcome:
        with self.sessions() as s:
            invoice_id = s.get(Action, action_id).payload["invoice_id"]
        if self.actions.refresh_invoice(invoice_id) != "SENT":
            with session_scope(self.sessions) as s:
                s.get(Action, action_id).status = "blocked"
            return Outcome(
                "blocked", "The invoice is no longer open, so no reminder was sent.", action_id
            )
        with session_scope(self.sessions) as s:
            s.get(Action, action_id).status = "approved"
        return self.actions.execute_reminder(action_id)

    def _approve_policy(self, action_id: str) -> Outcome:
        with session_scope(self.sessions) as s:
            act = s.get(Action, action_id)
            try:
                policy = Policy.from_dict(act.payload["proposed_policy"])
            except PolicyError as e:
                act.status = "blocked"
                return Outcome("blocked", f"invalid policy: {e}", action_id)
            save_policy(s, policy)
            act.status = "executed"
            audit.record(
                s, actor="user", event="policy_updated", action_id=action_id, **policy.to_dict()
            )
        return Outcome("executed", "Your rules were updated.", action_id)


# ---- direct (UI) management of rules and the recipient allowlist ----------------
def update_policy_from_ui(actions: ActionService, data: dict[str, Any]) -> Policy:
    with session_scope(actions.sessions) as s:
        policy = Policy.from_dict({**load_policy(s).to_dict(), **data})
        save_policy(s, policy)
        audit.record(s, actor="user", event="policy_updated", source="ui", **policy.to_dict())
        return policy


def set_allowlisted(actions: ActionService, email: str, name: str, allowed: bool) -> Recipient:
    with session_scope(actions.sessions) as s:
        r = get_or_create_recipient(s, email=email, name=name)
        r.allowlisted_at = utcnow() if allowed else None
        audit.record(
            s,
            actor="user",
            event="recipient_allowlisted" if allowed else "recipient_removed",
            email=r.email,
        )
        s.flush()
        s.expunge(r)
        return r
