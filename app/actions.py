"""Executes agent proposals. This is the only module that calls PayPal.

Every tool call the model makes lands in `Service.handle_tool`, which
normalizes the input, asks the rules engine for a verdict, and only then
touches PayPal (or parks the action in the approval queue).
"""
from __future__ import annotations

import datetime as dt
import logging
import threading
import time
from decimal import Decimal
from typing import Any, Callable

from . import rules as R
from .paypal import MockPayPal, PayPal, PayPalError
from .store import Store, new_id

log = logging.getLogger("paypilot.actions")

OPEN_INVOICE_STATUSES = {"SENT", "UNPAID", "PARTIALLY_PAID", "SCHEDULED"}

# (invoice, reminder_number) -> (subject, note). Supplied by the AI layer.
ReminderDrafter = Callable[[dict[str, Any], int], tuple[str, str]]


def default_drafter(invoice: dict[str, Any], n: int) -> tuple[str, str]:
    name = invoice.get("recipient_name") or "there"
    amount = f"{invoice['currency']} {invoice['total']}"
    if n <= 1:
        return (f"Friendly reminder: invoice for {amount}",
                f"Hi {name}, just a friendly nudge that the invoice for {amount} is still open. "
                "You can pay it securely through PayPal. Thank you!")
    if n == 2:
        return (f"Second reminder: {amount} outstanding",
                f"Hi {name}, following up on the invoice for {amount}, which is still unpaid. "
                "Could you take a look when you get a moment? Thanks.")
    return (f"Final reminder: {amount} overdue",
            f"Hi {name}, this is a final reminder that the invoice for {amount} remains unpaid. "
            "Please settle it at your earliest convenience or reply if anything is wrong.")


class Service:
    def __init__(self, store: Store, paypal: PayPal, drafter: ReminderDrafter = default_drafter) -> None:
        self.store = store
        self.paypal = paypal
        self.drafter = drafter
        self._payout_lock = threading.Lock()

    # ------------------------------------------------------------------ rules
    def get_rules(self) -> R.Rules:
        saved = self.store.get("settings", "rules")
        return R.Rules.from_dict(saved["rules"]) if saved else R.Rules()

    def set_rules(self, data: dict[str, Any]) -> R.Rules:
        rules = R.Rules.from_dict({**self.get_rules().to_dict(), **data})
        self.store.put("settings", "rules", {"rules": rules.to_dict()})
        self.store.log("rules_updated", {"rules": rules.to_dict(), "by": "user"})
        return rules

    # ------------------------------------------------------------ tool router
    def handle_tool(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        """Entry point for every model tool call. Always returns a JSON-able result."""
        handlers = {
            "create_invoice": self.propose_invoice,
            "send_payout": self.propose_payout,
            "send_reminder": self.propose_reminder,
            "cancel_invoice": self.propose_cancel,
            "list_invoices": self.list_invoices,
            "list_payouts": lambda a: {"payouts": self.store.list("payout", 25)},
            "check_invoice": self.check_invoice,
            "get_rules": lambda a: {"rules": self.get_rules().to_dict(), "spent_today": str(self.spent_today())},
        }
        if name not in handlers:
            return {"status": "error", "error": f"unknown tool {name}"}
        try:
            return handlers[name](args or {})
        except R.ValidationError as e:
            self.store.log("rejected_invalid", {"tool": name, "input": args, "error": str(e)})
            return {"status": "invalid", "error": str(e)}
        except PayPalError as e:
            self.store.log("paypal_error", {"tool": name, "error": str(e)})
            return {"status": "paypal_error", "error": str(e)}
        except Exception as e:  # never let one bad call crash the conversation
            log.exception("tool %s failed", name)
            return {"status": "error", "error": f"internal error: {type(e).__name__}"}

    # --------------------------------------------------------------- invoices
    def propose_invoice(self, args: dict[str, Any]) -> dict[str, Any]:
        action = R.normalize_invoice(args)
        verdict = R.evaluate_invoice(action, self.get_rules())
        self.store.log("proposed", {"verdict": verdict.to_dict(), "by": "agent"})
        if verdict.decision == "deny":
            return {"status": "denied", "reasons": verdict.reasons}
        return self._execute_invoice(action, verdict.reasons)

    def _execute_invoice(self, action: dict[str, Any], reasons: list[str]) -> dict[str, Any]:
        local_id = new_id("inv")
        created = self.paypal.create_invoice(action, request_id=local_id)
        if action["send"]:
            created = self.paypal.send_invoice(created["paypal_id"])
        inv = self.store.put("invoice", local_id, {
            **{k: action[k] for k in ("recipient_email", "recipient_name", "currency", "items", "total", "note", "due_in_days")},
            "paypal_id": created["paypal_id"],
            "number": created.get("number"),
            "status": created["status"],
            "pay_url": created.get("pay_url"),
            "sent_at": time.time() if action["send"] else None,
            "reminders_sent": 0,
            "last_reminder_at": None,
        })
        self.store.log("executed", {"type": "create_invoice", "invoice_id": local_id, "paypal_id": inv["paypal_id"], "status": inv["status"]})
        return {"status": "executed", "decision": "auto_approve", "reasons": reasons, "invoice": inv}

    def find_invoice(self, ref: str) -> dict[str, Any]:
        ref = str(ref or "").strip()
        inv = self.store.get("invoice", ref)
        if inv:
            return inv
        for cand in self.store.list("invoice", 500):
            if ref and ref in {cand.get("paypal_id"), cand.get("number")}:
                return cand
        raise R.ValidationError(f"no invoice matches '{ref}'. Use list_invoices to find the id.")

    def list_invoices(self, args: dict[str, Any]) -> dict[str, Any]:
        status = str(args.get("status") or "").upper()
        email = str(args.get("recipient_email") or "").lower()
        out = []
        for inv in self.store.list("invoice", 100):
            if status and inv["status"] != status:
                continue
            if email and inv["recipient_email"] != email:
                continue
            out.append({k: inv.get(k) for k in ("id", "paypal_id", "number", "recipient_email", "recipient_name", "total", "currency", "status", "reminders_sent", "sent_at")})
        return {"invoices": out[:25]}

    def check_invoice(self, args: dict[str, Any]) -> dict[str, Any]:
        inv = self.refresh_invoice(self.find_invoice(args.get("invoice_id")))
        return {"invoice": inv}

    def refresh_invoice(self, inv: dict[str, Any]) -> dict[str, Any]:
        latest = self.paypal.get_invoice(inv["paypal_id"])
        if latest["status"] != inv["status"] or latest.get("pay_url") != inv.get("pay_url"):
            inv = self.store.update("invoice", inv["id"], status=latest["status"], pay_url=latest.get("pay_url") or inv.get("pay_url"))
            if latest["status"] == "PAID":
                self.store.log("invoice_paid", {"invoice_id": inv["id"], "paypal_id": inv["paypal_id"]})
        return inv

    def propose_reminder(self, args: dict[str, Any], *, by: str = "agent") -> dict[str, Any]:
        inv = self.refresh_invoice(self.find_invoice(args.get("invoice_id")))
        rules = self.get_rules()
        if inv["status"] not in OPEN_INVOICE_STATUSES:
            return {"status": "denied", "reasons": [f"invoice is {inv['status']}, so there is nothing to remind about"]}
        if inv["reminders_sent"] >= rules.max_reminders:
            return {"status": "denied", "reasons": [f"already sent {inv['reminders_sent']} reminders (your max is {rules.max_reminders})"]}
        n = inv["reminders_sent"] + 1
        subject, note = self.drafter(inv, n)
        if args.get("note"):  # the agent may supply its own wording for a manual reminder
            note = str(args["note"])[:2000]
        self.paypal.remind_invoice(inv["paypal_id"], subject, note)
        inv = self.store.update("invoice", inv["id"], reminders_sent=n, last_reminder_at=time.time())
        self.store.log("executed", {"type": "send_reminder", "invoice_id": inv["id"], "reminder": n, "subject": subject, "note": note, "by": by})
        return {"status": "executed", "invoice_id": inv["id"], "reminder_number": n, "subject": subject, "note": note}

    def propose_cancel(self, args: dict[str, Any]) -> dict[str, Any]:
        inv = self.find_invoice(args.get("invoice_id"))
        if inv["status"] in {"PAID", "CANCELLED", "REFUNDED"}:
            return {"status": "denied", "reasons": [f"invoice is already {inv['status']}"]}
        action = {"type": "cancel_invoice", "invoice_id": inv["id"], "reason": str(args.get("reason") or "")[:500],
                  "recipient_email": inv["recipient_email"], "amount": inv["total"], "currency": inv["currency"]}
        # Cancelling notifies a client and cannot be undone, so it always needs a human.
        return self._queue(action, ["cancelling an invoice notifies the client and cannot be undone"])

    def _execute_cancel(self, action: dict[str, Any]) -> dict[str, Any]:
        inv = self.store.get("invoice", action["invoice_id"])
        self.paypal.cancel_invoice(inv["paypal_id"], action.get("reason") or "This invoice has been cancelled.")
        inv = self.store.update("invoice", inv["id"], status="CANCELLED")
        self.store.log("executed", {"type": "cancel_invoice", "invoice_id": inv["id"]})
        return {"status": "executed", "invoice": inv}

    # ---------------------------------------------------------------- payouts
    def spent_today(self) -> Decimal:
        start = dt.datetime.combine(dt.date.today(), dt.time.min).timestamp()
        return sum(
            (Decimal(p["amount"]) for p in self.store.list("payout", 1000)
             if p.get("approved_by") == "rules" and p["created_at"] >= start and p["status"] not in {"FAILED", "DENIED"}),
            Decimal("0.00"),
        )

    def known_recipients(self) -> set[str]:
        return {p["recipient_email"] for p in self.store.list("payout", 1000) if p["status"] not in {"FAILED", "DENIED"}}

    def propose_payout(self, args: dict[str, Any]) -> dict[str, Any]:
        action = R.normalize_payout(args)
        with self._payout_lock:  # evaluate and record atomically so two requests cannot both squeeze under the daily limit
            verdict = R.evaluate_payout(action, self.get_rules(), spent_today=self.spent_today(), known_recipients=self.known_recipients())
            self.store.log("proposed", {"verdict": verdict.to_dict(), "by": "agent"})
            if verdict.decision == "deny":
                return {"status": "denied", "reasons": verdict.reasons}
            if verdict.decision == "needs_approval":
                return self._queue(action, verdict.reasons)
            return self._execute_payout(action, approved_by="rules", reasons=verdict.reasons)

    def _execute_payout(self, action: dict[str, Any], approved_by: str, reasons: list[str]) -> dict[str, Any]:
        local_id = new_id("pay")
        record = self.store.put("payout", local_id, {**action, "status": "PROCESSING", "approved_by": approved_by, "paypal_id": None})
        try:
            res = self.paypal.create_payout(action, batch_id=local_id)
        except PayPalError:
            self.store.update("payout", local_id, status="FAILED")
            raise
        record = self.store.update("payout", local_id, paypal_id=res["paypal_id"], status=res["status"])
        self.store.log("executed", {"type": "send_payout", "payout_id": local_id, "paypal_id": res["paypal_id"], "amount": action["amount"], "approved_by": approved_by})
        return {"status": "executed", "decision": "auto_approve" if approved_by == "rules" else "approved", "reasons": reasons, "payout": record}

    # -------------------------------------------------------------- approvals
    def _queue(self, action: dict[str, Any], reasons: list[str]) -> dict[str, Any]:
        appr = self.store.put("approval", new_id("apr"), {"action": action, "reasons": reasons, "status": "pending"})
        self.store.log("queued_for_approval", {"approval_id": appr["id"], "action": action, "reasons": reasons})
        return {"status": "pending_approval", "approval_id": appr["id"], "reasons": reasons,
                "message": "Nothing was sent. The user must approve this in the Approvals panel."}

    def decide(self, approval_id: str, approve: bool) -> dict[str, Any]:
        appr = self.store.get("approval", approval_id)
        if not appr:
            raise KeyError("approval not found")
        if appr["status"] != "pending":
            raise ValueError(f"approval is already {appr['status']}")
        action = appr["action"]
        if not approve:
            self.store.update("approval", approval_id, status="rejected")
            self.store.log("rejected_by_user", {"approval_id": approval_id, "action": action})
            return {"status": "rejected"}
        # Re-check the hard rules at approval time: a human can lift the soft limit, not the hard cap.
        if action["type"] == "send_payout":
            v = R.evaluate_payout(action, self.get_rules(), spent_today=Decimal("0"), known_recipients={action["recipient_email"]})
            if v.decision == "deny":
                self.store.update("approval", approval_id, status="denied")
                self.store.log("denied", {"approval_id": approval_id, "reasons": v.reasons})
                return {"status": "denied", "reasons": v.reasons}
            self.store.update("approval", approval_id, status="approved")
            self.store.log("approved_by_user", {"approval_id": approval_id, "action": action})
            try:
                return self._execute_payout(action, approved_by="user", reasons=["approved by you"])
            except PayPalError as e:
                self.store.update("approval", approval_id, status="failed")
                return {"status": "paypal_error", "error": str(e)}
        if action["type"] == "cancel_invoice":
            self.store.update("approval", approval_id, status="approved")
            self.store.log("approved_by_user", {"approval_id": approval_id, "action": action})
            return self._execute_cancel(action)
        raise ValueError(f"unknown action type {action['type']}")

    # -------------------------------------------------------------- reminders
    def reminder_sweep(self, force: bool = False) -> list[dict[str, Any]]:
        """Sends due reminders for unpaid invoices. Runs on a timer and from the UI."""
        rules = self.get_rules()
        if not rules.auto_reminders and not force:
            return []
        results, t = [], time.time()
        for inv in self.store.list("invoice", 500):
            if inv["status"] not in OPEN_INVOICE_STATUSES or not inv.get("sent_at"):
                continue
            if inv["reminders_sent"] >= rules.max_reminders:
                continue
            since = inv["last_reminder_at"] or inv["sent_at"]
            wait = rules.reminder_every_hours if inv["last_reminder_at"] else rules.reminder_after_hours
            if not force and t - since < wait * 3600:
                continue
            try:
                res = self.propose_reminder({"invoice_id": inv["id"]}, by="scheduler")
            except PayPalError as e:
                res = {"status": "paypal_error", "error": str(e)}
            results.append({"invoice_id": inv["id"], **res})
        return results

    # ------------------------------------------------------------------- demo
    def simulate_paid(self, invoice_id: str) -> dict[str, Any]:
        if not isinstance(self.paypal, MockPayPal):
            raise ValueError("simulated payments are only available in mock mode; pay the invoice from its PayPal link instead")
        inv = self.find_invoice(invoice_id)
        self.paypal.mark_paid(inv["paypal_id"])
        return self.refresh_invoice(inv)
