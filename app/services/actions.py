"""Where proposals meet the rules: validate -> policy -> record -> execute or hold.

This is the only layer that calls PayPal, and only after the policy engine
returned AUTO (or the user approved). Every step lands in the audit log.

Why this works (idempotency in practice): the Action row is committed *before*
PayPal is called, and its id deterministically derives the PayPal-Request-Id
for each step. If the process crashes or the network drops mid-call, re-running
the same action sends the same request ids, so PayPal returns the original
invoice instead of creating a second one.
"""

from __future__ import annotations

import datetime as dt
import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from sqlalchemy.orm import Session, sessionmaker

from app.db import session_scope
from app.models import Action, Approval, Invoice, Payout, utcnow
from app.money import fmt
from app.paypal.errors import PayPalError
from app.paypal.invoices import InvoiceDraft, InvoicingAPI, LineItem
from app.paypal.payouts import PayoutsAPI
from app.policy.engine import (
    Level,
    MLSignals,
    evaluate_invoice,
    evaluate_payout,
    evaluate_policy_change,
    evaluate_reminder,
)
from app.schemas import (
    CreateInvoice,
    GetStatus,
    SendPayout,
    SendReminder,
    UpdatePolicy,
)
from app.services import audit
from app.services.followups import ReminderDrafter, TemplateDrafter, reminder_due, tone_for
from app.services.store import (
    days_overdue,
    find_invoices,
    find_payouts,
    get_or_create_client,
    get_or_create_recipient,
    load_policy,
    spend,
)

log = logging.getLogger("paypilot.actions")

PAYPAL_STATUS_TO_LOCAL = {
    "DRAFT": "DRAFT",
    "PAID": "PAID",
    "MARKED_AS_PAID": "PAID",
    "CANCELLED": "CANCELLED",
    "REFUNDED": "CANCELLED",
    "MARKED_AS_REFUNDED": "CANCELLED",
}

# (session, invoice) -> (risk probability, reasons). Supplied by the ML layer.
RiskScorer = Callable[[Session, Invoice], tuple[float, list[str]]]
# (session, payout fields) -> MLSignals. Supplied by the ML layer.
AnomalyScorer = Callable[[Session, dict[str, Any]], MLSignals]


def local_status(paypal_status: str) -> str:
    return PAYPAL_STATUS_TO_LOCAL.get(paypal_status, "SENT")


def request_id(action_id: str, step: str) -> str:
    """Deterministic PayPal-Request-Id per (action, step); a UUID fits PayPal's 38-char limit."""
    return str(uuid.uuid5(uuid.UUID(action_id), step))


@dataclass
class Outcome:
    status: str  # executed/needs_approval/escalated/blocked/invalid/paypal_error/info/failed
    message: str
    action_id: str | None = None
    reasons: list[str] = field(default_factory=list)
    data: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"status": self.status, "message": self.message}
        if self.action_id:
            d["action_id"] = self.action_id
        if self.reasons:
            d["reasons"] = self.reasons
        if self.data:
            d["data"] = self.data
        return d


class ActionService:
    def __init__(
        self,
        sessions: sessionmaker[Session],
        *,
        invoicing: InvoicingAPI | None = None,
        payouts: PayoutsAPI | None = None,
        risk_scorer: RiskScorer | None = None,
        anomaly_scorer: AnomalyScorer | None = None,
        drafter: ReminderDrafter | None = None,
        business_name: str = "",
        clock: Callable[[], dt.datetime] = utcnow,
    ) -> None:
        self.sessions = sessions
        self.invoicing = invoicing
        self.payouts = payouts
        self.risk_scorer = risk_scorer
        self.anomaly_scorer = anomaly_scorer
        self.drafter = drafter or TemplateDrafter()
        self.business_name = business_name
        self.clock = clock

    # ------------------------------------------------------------- dispatch
    def handle(self, action: Any, *, source: str = "chat") -> Outcome:
        handlers = {
            "create_invoice": self.propose_invoice,
            "send_payout": self.propose_payout,
            "send_reminder": self.propose_reminder,
            "get_status": self.get_status,
            "update_policy": self.propose_policy_change,
        }
        handler = handlers.get(action.kind)
        if handler is None:
            return Outcome("failed", f"{action.kind} is not available yet")
        return handler(action, source=source)

    def _new_action(
        self, s: Session, action: Any, source: str, decision, payload: dict | None = None
    ) -> Action:
        act = Action(
            kind=action.kind,
            payload=payload if payload is not None else action.model_dump(mode="json"),
            source=source,
            decision=decision.outcome,
            reasons=list(decision.reasons),
            status="proposed",
        )
        s.add(act)
        s.flush()
        audit.record(
            s,
            actor="agent" if source == "chat" else source,
            event="action_proposed",
            action_id=act.id,
            kind=act.kind,
            payload=act.payload,
        )
        audit.record(
            s, actor="policy", event="policy_decision", action_id=act.id, **decision.to_dict()
        )
        return act

    # -------------------------------------------------------------- invoices
    def propose_invoice(self, a: CreateInvoice, *, source: str = "chat") -> Outcome:
        total = a.total
        with session_scope(self.sessions) as s:
            decision = evaluate_invoice(total=total, currency=a.currency, policy=load_policy(s))
            act = self._new_action(s, a, source, decision)
            if decision.level == Level.BLOCK:
                act.status = "blocked"
                return Outcome("blocked", decision.level.label, act.id, list(decision.reasons))
            client = get_or_create_client(s, name=a.client_name, email=str(a.client_email))
            today = self.clock().date()
            inv = Invoice(
                client_id=client.id,
                action_id=act.id,
                amount=total,
                currency=a.currency,
                description="; ".join(f"{i.quantity} x {i.name}" for i in a.items),
                terms_days=a.due_in_days,
                issued_at=self.clock(),
                due_date=today + dt.timedelta(days=a.due_in_days),
                status="DRAFT",
            )
            s.add(inv)
            s.flush()
            if self.risk_scorer:
                inv.risk_score, inv.risk_reasons = self.risk_scorer(s, inv)
            act.status = "approved"
            action_id = act.id
        # Committed: the record exists before PayPal is called.
        return self.execute_invoice(action_id)

    def execute_invoice(self, action_id: str) -> Outcome:
        with session_scope(self.sessions) as s:
            act = s.get(Action, action_id)
            inv = s.query(Invoice).filter_by(action_id=action_id).one()
            payload = act.payload
            if self.invoicing is None:
                act.status = "failed"
                audit.record(
                    s,
                    actor="system",
                    event="execution_skipped",
                    action_id=action_id,
                    reason="PayPal is not configured",
                )
                return Outcome(
                    "failed", "PayPal is not configured (set PAYPAL_CLIENT_ID/SECRET)", action_id
                )
            draft = InvoiceDraft(
                client_name=payload["client_name"],
                client_email=payload["client_email"],
                currency=payload["currency"],
                items=[
                    LineItem(
                        name=i["name"],
                        quantity=i["quantity"],
                        unit_amount=Decimal(i["unit_amount"]),
                    )
                    for i in payload["items"]
                ],
                invoice_date=inv.issued_at.date(),
                due_date=inv.due_date,
                note=payload.get("note", ""),
            )
            try:
                if not inv.paypal_invoice_id:
                    rid = request_id(action_id, "invoice.create")
                    created = self.invoicing.create_draft(draft, request_id=rid)
                    inv.paypal_invoice_id, inv.number = created.paypal_id, created.number
                    audit.record(
                        s,
                        actor="paypal",
                        event="invoice_created",
                        action_id=action_id,
                        paypal_request_id=rid,
                        paypal_debug_id=created.debug_id,
                        paypal_invoice_id=created.paypal_id,
                    )
                rid = request_id(action_id, "invoice.send")
                sent = self.invoicing.send(inv.paypal_invoice_id, request_id=rid)
            except PayPalError as e:
                act.status = "failed"
                act.result = {"error": str(e), "kind": type(e).__name__}
                audit.record(
                    s,
                    actor="paypal",
                    event="paypal_error",
                    action_id=action_id,
                    paypal_debug_id=e.debug_id,
                    error=str(e),
                    kind=type(e).__name__,
                )
                return Outcome("paypal_error", e.user_message(), action_id)
            inv.status = local_status(sent.status)
            inv.number = sent.number or inv.number
            inv.pay_url = sent.pay_url
            act.status = "executed"
            act.result = {"paypal_invoice_id": sent.paypal_id, "paypal_status": sent.status}
            audit.record(
                s,
                actor="paypal",
                event="invoice_sent",
                action_id=action_id,
                paypal_request_id=rid,
                paypal_debug_id=sent.debug_id,
                paypal_invoice_id=sent.paypal_id,
                paypal_status=sent.status,
            )
            data = {
                "paypal_invoice_id": sent.paypal_id,
                "invoice_number": inv.number,
                "total": f"{inv.amount:.2f}",
                "currency": inv.currency,
                "due_date": inv.due_date.isoformat(),
                "client_email": payload["client_email"],
            }
            if inv.risk_score is not None:
                data["late_payment_risk"] = round(inv.risk_score, 2)
            return Outcome(
                "executed",
                f"Invoice for {fmt(inv.amount, inv.currency)} sent to {payload['client_email']}",
                action_id,
                data=data,
            )

    # ------------------------------------------------- status and reminders
    def refresh_invoice(self, invoice_id: str, *, actor: str = "system") -> str:
        """Poll PayPal for an invoice's status (the fallback when a webhook is missed)."""
        with session_scope(self.sessions) as s:
            inv = s.get(Invoice, invoice_id)
            if inv is None or not inv.paypal_invoice_id or self.invoicing is None:
                return inv.status if inv else "UNKNOWN"
            paypal_id = inv.paypal_invoice_id
        try:
            info = self.invoicing.get(paypal_id)
        except PayPalError as e:
            log.warning("could not refresh %s: %s", paypal_id, e)
            with session_scope(self.sessions) as s:
                return s.get(Invoice, invoice_id).status
        return self.apply_invoice_status(paypal_id, info.status, actor=actor, source="poll")

    def apply_invoice_status(
        self, paypal_invoice_id: str, paypal_status: str, *, actor: str, source: str
    ) -> str:
        """Record a PayPal status for an invoice (from polling or a verified webhook)."""
        new = local_status(paypal_status)
        with session_scope(self.sessions) as s:
            inv = s.query(Invoice).filter_by(paypal_invoice_id=paypal_invoice_id).one_or_none()
            if inv is None:
                return "UNKNOWN"
            if inv.status != new:
                old = inv.status
                inv.status = new
                if new == "PAID" and inv.paid_at is None:
                    inv.paid_at = self.clock()
                audit.record(
                    s,
                    actor=actor,
                    event="invoice_status_changed",
                    action_id=inv.action_id,
                    paypal_invoice_id=paypal_invoice_id,
                    old=old,
                    new=new,
                    paypal_status=paypal_status,
                    source=source,
                )
            return inv.status

    def propose_reminder(self, a: SendReminder, *, source: str = "chat") -> Outcome:
        with session_scope(self.sessions) as s:
            matches = [i for i in find_invoices(s, a.invoice_ref, limit=10) if i.status == "SENT"]
            options = [
                {"invoice_number": i.number, "client": i.client.name, "amount": f"{i.amount:.2f}"}
                for i in matches
            ]
            ids = [i.id for i in matches]
        if not ids:
            return Outcome("info", f"No open (unpaid) invoice matches {a.invoice_ref!r}.")
        if len(ids) > 1:
            return Outcome(
                "info",
                "Several open invoices match; ask the user which one.",
                data={"matches": options},
            )
        return self.remind_invoice(ids[0], source=source)

    def remind_invoice(self, invoice_id: str, *, source: str = "chat") -> Outcome:
        self.refresh_invoice(invoice_id)  # never chase an invoice that was just paid
        with session_scope(self.sessions) as s:
            inv = s.get(Invoice, invoice_id)
            today = self.clock().date()
            d = days_overdue(inv, today)
            tone = tone_for(d, inv.risk_score, inv.reminders_sent)
            decision = evaluate_reminder(
                tone=tone, invoice_open=inv.status == "SENT", policy=load_policy(s)
            )
            payload: dict[str, Any] = {
                "invoice_id": inv.id,
                "paypal_invoice_id": inv.paypal_invoice_id,
                "invoice_number": inv.number,
                "tone": tone,
                "days_overdue": d,
                "risk": inv.risk_score,
            }
            if decision.level != Level.BLOCK:
                ctx = {
                    "client": inv.client.name,
                    "number": f"#{inv.number}" if inv.number else "",
                    "amount": fmt(inv.amount, inv.currency),
                    "due_date": inv.due_date.isoformat(),
                    "days": d,
                    "business": self.business_name,
                }
                payload["subject"], payload["note"] = self.drafter.draft(ctx, tone)
            act = self._new_action(s, SendReminder(invoice_ref=inv.id), source, decision, payload)
            if decision.level == Level.BLOCK:
                act.status = "blocked"
                return Outcome("blocked", decision.level.label, act.id, list(decision.reasons))
            if decision.level != Level.AUTO:
                act.status = "held"
                s.add(Approval(action_id=act.id, level="standard"))
                return Outcome(
                    decision.outcome,
                    f"A {tone} reminder is drafted and waiting in the Approvals inbox.",
                    act.id,
                    list(decision.reasons),
                    data={"subject": payload["subject"], "note": payload["note"]},
                )
            act.status = "approved"
            action_id = act.id
        return self.execute_reminder(action_id)

    def execute_reminder(self, action_id: str) -> Outcome:
        with session_scope(self.sessions) as s:
            act = s.get(Action, action_id)
            p = act.payload
            inv = s.get(Invoice, p["invoice_id"])
            if self.invoicing is None:
                act.status = "failed"
                return Outcome("failed", "PayPal is not configured", action_id)
            rid = request_id(action_id, "invoice.remind")
            try:
                debug_id = self.invoicing.remind(
                    inv.paypal_invoice_id, subject=p["subject"], note=p["note"], request_id=rid
                )
            except PayPalError as e:
                act.status = "failed"
                act.result = {"error": str(e)}
                audit.record(
                    s,
                    actor="paypal",
                    event="paypal_error",
                    action_id=action_id,
                    paypal_debug_id=e.debug_id,
                    error=str(e),
                    kind=type(e).__name__,
                )
                return Outcome("paypal_error", e.user_message(), action_id)
            inv.reminders_sent += 1
            inv.last_reminder_at = self.clock()
            act.status = "executed"
            audit.record(
                s,
                actor="paypal",
                event="reminder_sent",
                action_id=action_id,
                paypal_request_id=rid,
                paypal_debug_id=debug_id,
                paypal_invoice_id=inv.paypal_invoice_id,
                tone=p["tone"],
            )
            return Outcome(
                "executed",
                f"{p['tone'].capitalize()} reminder sent for invoice #{inv.number}",
                action_id,
                data={"subject": p["subject"], "tone": p["tone"]},
            )

    def sweep_followups(self) -> list[Outcome]:
        """Scheduler job: refresh open invoices, then remind the overdue ones that are due."""
        with session_scope(self.sessions) as s:
            open_ids = [
                i.id
                for i in s.query(Invoice).filter(
                    Invoice.status == "SENT", Invoice.is_seeded.is_(False)
                )
            ]
            pending = {
                a.payload.get("invoice_id")
                for a in s.query(Action).filter(
                    Action.kind == "send_reminder", Action.status == "held"
                )
            }
        self.refresh_payouts()
        outcomes = []
        now = self.clock()
        for invoice_id in open_ids:
            if self.refresh_invoice(invoice_id) != "SENT" or invoice_id in pending:
                continue
            with session_scope(self.sessions) as s:
                inv = s.get(Invoice, invoice_id)
                due = reminder_due(
                    days_overdue=days_overdue(inv, now.date()),
                    risk=inv.risk_score,
                    reminders_sent=inv.reminders_sent,
                    last_reminder_at=inv.last_reminder_at,
                    now=now,
                )
            if due:
                outcomes.append(self.remind_invoice(invoice_id, source="scheduler"))
        return outcomes

    # --------------------------------------------------------------- payouts
    def propose_payout(self, a: SendPayout, *, source: str = "chat") -> Outcome:
        with session_scope(self.sessions) as s:
            policy = load_policy(s)
            recipient = get_or_create_recipient(
                s, email=str(a.recipient_email), name=a.recipient_name
            )
            ml = MLSignals()
            if self.anomaly_scorer:
                ml = self.anomaly_scorer(
                    s,
                    {
                        "recipient_id": recipient.id,
                        "amount": a.amount,
                        "currency": a.currency,
                        "at": self.clock(),
                    },
                )
            decision = evaluate_payout(
                amount=a.amount,
                currency=a.currency,
                recipient=recipient.email,
                policy=policy,
                spend=spend(s, a.currency, self.clock()),
                recipient_allowlisted=recipient.allowlisted_at is not None,
                ml=ml,
            )
            act = self._new_action(s, a, source, decision)
            act.signals = {
                "anomaly_flag": ml.anomaly_flag,
                "anomaly_score": ml.anomaly_score,
                "anomaly_reason": ml.anomaly_reason,
            }
            if ml.anomaly_flag:
                audit.record(
                    s, actor="ml", event="anomaly_flagged", action_id=act.id, **act.signals
                )
            if decision.level == Level.BLOCK:
                act.status = "blocked"
                return Outcome("blocked", decision.level.label, act.id, list(decision.reasons))
            if decision.level in (Level.NEEDS_APPROVAL, Level.ESCALATED):
                act.status = "held"
                level = "escalated" if decision.level == Level.ESCALATED else "standard"
                s.add(Approval(action_id=act.id, level=level))
                return Outcome(
                    decision.outcome,
                    "Waiting in the Approvals inbox; nothing has been sent.",
                    act.id,
                    list(decision.reasons),
                )
            s.add(
                Payout(
                    action_id=act.id,
                    recipient_id=recipient.id,
                    amount=a.amount,
                    currency=a.currency,
                    note=a.note,
                    status="approved",
                    created_at=self.clock(),
                    anomaly_score=ml.anomaly_score,
                    anomaly_flag=ml.anomaly_flag,
                    anomaly_reason=ml.anomaly_reason,
                )
            )
            act.status = "approved"
            action_id = act.id
        return self.execute_payout(action_id)

    def execute_payout(self, action_id: str) -> Outcome:
        """Send an approved payout through PayPal Payouts (sender_batch_id = action id)."""
        with session_scope(self.sessions) as s:
            act = s.get(Action, action_id)
            payout = s.query(Payout).filter_by(action_id=action_id).one()
            if self.payouts is None:
                act.status = payout.status = "failed"
                audit.record(
                    s,
                    actor="system",
                    event="execution_skipped",
                    action_id=action_id,
                    reason="PayPal payouts are not configured",
                )
                return Outcome("failed", "PayPal payouts are not configured", action_id)
            rid = request_id(action_id, "payout.create")
            try:
                info = self.payouts.create(
                    receiver_email=payout.recipient.email,
                    amount=payout.amount,
                    currency=payout.currency,
                    note=payout.note,
                    sender_batch_id=action_id,
                    request_id=rid,
                )
            except PayPalError as e:
                act.status = payout.status = "failed"  # failed payouts free up the caps
                act.result = {"error": str(e), "kind": type(e).__name__}
                audit.record(
                    s,
                    actor="paypal",
                    event="paypal_error",
                    action_id=action_id,
                    paypal_request_id=rid,
                    paypal_debug_id=e.debug_id,
                    error=str(e),
                    kind=type(e).__name__,
                )
                return Outcome("paypal_error", e.user_message(), action_id)
            payout.paypal_batch_id = info.batch_id
            payout.paypal_status = info.batch_status
            payout.status = act.status = "executed"
            act.result = {"paypal_batch_id": info.batch_id, "batch_status": info.batch_status}
            audit.record(
                s,
                actor="paypal",
                event="payout_sent",
                action_id=action_id,
                paypal_request_id=rid,
                paypal_debug_id=info.debug_id,
                paypal_batch_id=info.batch_id,
                batch_status=info.batch_status,
            )
            return Outcome(
                "executed",
                f"Sent {fmt(payout.amount, payout.currency)} to {payout.recipient.email} "
                f"(PayPal batch {info.batch_id}, status {info.batch_status})",
                action_id,
                data={"paypal_batch_id": info.batch_id, "batch_status": info.batch_status},
            )

    def refresh_payouts(self) -> int:
        """Poll PayPal for payouts still in flight (webhook fallback). Returns how many changed."""
        if self.payouts is None:
            return 0
        with session_scope(self.sessions) as s:
            pending = [
                (p.id, p.paypal_batch_id)
                for p in s.query(Payout).filter(
                    Payout.status == "executed",
                    Payout.paypal_batch_id.is_not(None),
                    Payout.paypal_status.in_(("PENDING", "PROCESSING")),
                )
            ]
        changed = 0
        for payout_id, batch_id in pending:
            try:
                info = self.payouts.get(batch_id)
            except PayPalError as e:
                log.warning("could not refresh payout %s: %s", batch_id, e)
                continue
            with session_scope(self.sessions) as s:
                p = s.get(Payout, payout_id)
                status = info.item_status or info.batch_status
                if status != p.paypal_status:
                    p.paypal_status = status
                    if status in {"DENIED", "FAILED", "RETURNED", "BLOCKED", "REFUNDED"}:
                        p.status = "failed"
                    audit.record(
                        s,
                        actor="paypal",
                        event="payout_status_changed",
                        action_id=p.action_id,
                        paypal_batch_id=batch_id,
                        paypal_status=status,
                        source="poll",
                    )
                    changed += 1
        return changed

    # ------------------------------------------------------------ read-only
    def get_status(self, a: GetStatus, *, source: str = "chat") -> Outcome:
        with session_scope(self.sessions) as s:
            today = self.clock().date()
            if a.target == "invoice":
                rows = [
                    {
                        "invoice_number": inv.number,
                        "paypal_invoice_id": inv.paypal_invoice_id,
                        "client": {"untrusted_text": inv.client.name},
                        "client_email": inv.client.email,
                        "amount": f"{inv.amount:.2f}",
                        "currency": inv.currency,
                        "status": inv.status,
                        "due_date": inv.due_date.isoformat(),
                        "days_overdue": days_overdue(inv, today),
                        "description": {"untrusted_text": inv.description},
                        "late_payment_risk": inv.risk_score,
                    }
                    for inv in find_invoices(s, a.ref)
                ]
                return Outcome("info", f"{len(rows)} invoice(s) found", data={"invoices": rows})
            if a.target == "payout":
                rows = [
                    {
                        "recipient": p.recipient.email,
                        "amount": f"{p.amount:.2f}",
                        "currency": p.currency,
                        "status": p.status,
                        "paypal_batch_id": p.paypal_batch_id,
                        "created_at": p.created_at.isoformat(),
                        "anomaly_flag": p.anomaly_flag,
                    }
                    for p in find_payouts(s, a.ref)
                ]
                return Outcome("info", f"{len(rows)} payout(s) found", data={"payouts": rows})
            policy = load_policy(s)
            invoices = find_invoices(s, "", limit=500)
            spent = spend(s, "USD", self.clock())
            summary = {
                "outstanding": sum(1 for i in invoices if i.status == "SENT"),
                "overdue": sum(1 for i in invoices if days_overdue(i, today) > 0),
                "paid": sum(1 for i in invoices if i.status == "PAID"),
                "spent_today_usd": f"{spent.today:.2f}",
                "daily_cap_usd": f"{policy.daily_cap:.2f}",
                "spent_month_usd": f"{spent.month:.2f}",
                "monthly_cap_usd": f"{policy.monthly_cap:.2f}",
                "auto_approve_limit_usd": f"{policy.auto_approve_limit:.2f}",
                "pending_approvals": s.query(Approval).filter(Approval.decision.is_(None)).count(),
            }
            return Outcome("info", "summary", data=summary)

    # --------------------------------------------------------- policy change
    def propose_policy_change(self, a: UpdatePolicy, *, source: str = "chat") -> Outcome:
        with session_scope(self.sessions) as s:
            current = load_policy(s)
            decision, proposed = evaluate_policy_change(
                current=current, changes={a.field: a.new_value}
            )
            act = self._new_action(s, a, source, decision)
            if decision.level == Level.BLOCK:
                act.status = "blocked"
                return Outcome("blocked", decision.level.label, act.id, list(decision.reasons))
            act.payload = {**act.payload, "proposed_policy": proposed.to_dict()}
            act.status = "held"
            s.add(Approval(action_id=act.id, level="standard"))
            return Outcome(
                "needs_approval",
                "Rule change queued for your approval in the Approvals inbox.",
                act.id,
                list(decision.reasons),
            )
