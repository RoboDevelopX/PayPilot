"""The AI layer: Claude interprets the user's message and *proposes* actions as tool calls.

The model never sees PayPal credentials and never calls PayPal. Each tool call
is handed to `Service.handle_tool`, which validates it and applies the user's
rules; the model only sees the verdict ("executed", "pending_approval",
"denied", "invalid") and explains it back to the user.

When no ANTHROPIC_API_KEY is set, `MockAgent` turns common phrasings into the
same tool calls with regular expressions, so the full flow can be demoed offline.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import re
import threading
from typing import Any

from .actions import Service, default_drafter

log = logging.getLogger("paypilot.agent")

MAX_TOOL_ROUNDS = 8
MAX_HISTORY_MESSAGES = 60

SYSTEM_PROMPT = """You are PayPilot, an AI co-pilot that handles PayPal payment tasks for a small business owner or freelancer.

You act through tools. The tools are proposals: deterministic code checks every call against the user's rules before anything reaches PayPal, and replies with a status:
- "executed": it happened in PayPal. Report the key facts (amount, recipient, invoice or payout id).
- "pending_approval": nothing was sent. Tell the user it is waiting in the Approvals panel and give the reasons.
- "denied": a hard rule blocked it. Explain which rule, and do not try to get around it (for example by splitting a payment into smaller ones).
- "invalid" or "paypal_error": explain the problem plainly and ask for what is missing.

Guidelines:
- Never say money moved or an invoice was sent unless the tool returned "executed".
- If the recipient email or the amount is missing or ambiguous, ask instead of guessing. Never invent an email address.
- Default currency is USD. Default invoice due date is 14 days. Split invoices into clear line items when the user lists several things.
- Write invoice notes and reminder notes in a warm, professional tone, in the user's voice.
- You cannot change the user's rules or limits. If asked, point them to the Rules panel.
- Use list_invoices to resolve references like "Alice's invoice" to an invoice id before reminding or cancelling.
- Keep replies short: one to three sentences, plain text, no markdown headings."""


def _strict(name: str, description: str, properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {
        "name": name,
        "description": description,
        "strict": True,
        "input_schema": {"type": "object", "properties": properties, "required": required, "additionalProperties": False},
    }


TOOLS: list[dict[str, Any]] = [
    _strict(
        "create_invoice",
        "Propose creating a PayPal invoice (a request for money owed to the user) and emailing it to the recipient. "
        "Invoices bring money in, so they do not use the spending limit.",
        {
            "recipient_email": {"type": "string", "description": "Email of the person or business being billed."},
            "recipient_name": {"type": "string", "description": "Full name of the recipient, if known."},
            "currency": {"type": "string", "description": "ISO currency code, e.g. USD."},
            "items": {
                "type": "array",
                "description": "Line items on the invoice.",
                "items": {
                    "type": "object",
                    "properties": {
                        "description": {"type": "string"},
                        "quantity": {"type": "integer"},
                        "unit_amount": {"type": "string", "description": "Price per unit as a decimal string, e.g. \"125.00\"."},
                    },
                    "required": ["description", "quantity", "unit_amount"],
                    "additionalProperties": False,
                },
            },
            "note": {"type": "string", "description": "A short, friendly note to the recipient."},
            "due_in_days": {"type": "integer", "description": "Days until payment is due. Default 14."},
        },
        ["recipient_email", "currency", "items"],
    ),
    _strict(
        "send_payout",
        "Propose sending money from the user's PayPal balance to someone by email (PayPal Payouts). "
        "Code auto-approves it only if it fits the user's spending rules; otherwise it is queued for the user's approval.",
        {
            "recipient_email": {"type": "string", "description": "PayPal email of the person being paid."},
            "amount": {"type": "string", "description": "Amount as a decimal string, e.g. \"42.50\"."},
            "currency": {"type": "string", "description": "ISO currency code, e.g. USD."},
            "note": {"type": "string", "description": "What the payment is for."},
        },
        ["recipient_email", "amount", "currency"],
    ),
    _strict(
        "send_reminder",
        "Propose sending a payment reminder for an unpaid invoice. Code refuses if it is already paid or the reminder limit is reached.",
        {
            "invoice_id": {"type": "string", "description": "PayPilot invoice id (inv_...) or PayPal invoice id."},
            "note": {"type": "string", "description": "Optional custom reminder message. Omit to let PayPilot draft one."},
        },
        ["invoice_id"],
    ),
    _strict(
        "cancel_invoice",
        "Propose cancelling an unpaid invoice. This always goes to the user for approval.",
        {
            "invoice_id": {"type": "string"},
            "reason": {"type": "string", "description": "Short reason shown to the recipient."},
        },
        ["invoice_id"],
    ),
    _strict(
        "list_invoices",
        "List the user's recent invoices, optionally filtered by status (DRAFT, SENT, PAID, CANCELLED) or recipient email.",
        {"status": {"type": "string"}, "recipient_email": {"type": "string"}},
        [],
    ),
    _strict("check_invoice", "Fetch the latest status of one invoice from PayPal.", {"invoice_id": {"type": "string"}}, ["invoice_id"]),
    _strict("list_payouts", "List recent payouts the user has sent.", {}, []),
    _strict("get_rules", "Read the user's current rules: spending limit, daily limit, currencies, reminder settings.", {}, []),
]


class ClaudeAgent:
    mode = "claude"

    def __init__(self, service: Service, api_key: str, model: str, effort: str) -> None:
        import anthropic

        self._anthropic = anthropic
        self.client = anthropic.Anthropic(api_key=api_key)
        self.model = model
        self.effort = effort
        self.service = service
        self._sessions: dict[str, list[Any]] = {}
        self._locks: dict[str, threading.Lock] = {}

    def _create(self, **kw: Any) -> Any:
        # Server-side fallback: if a safety classifier declines, the API retries on a recommended model.
        return self.client.beta.messages.create(
            model=self.model,
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            **kw,
        )

    def chat(self, session_id: str, text: str) -> dict[str, Any]:
        lock = self._locks.setdefault(session_id, threading.Lock())
        with lock:
            history = self._sessions.setdefault(session_id, [])
            if len(history) > MAX_HISTORY_MESSAGES:
                history.clear()  # start fresh rather than editing earlier turns
            start_len = len(history)
            today = dt.date.today().isoformat()
            history.append({"role": "user", "content": f"[today is {today}]\n{text}"})
            events: list[dict[str, Any]] = []
            try:
                for _ in range(MAX_TOOL_ROUNDS):
                    resp = self._create(
                        max_tokens=16000,
                        system=SYSTEM_PROMPT,
                        tools=TOOLS,
                        output_config={"effort": self.effort},
                        messages=history,
                    )
                    if resp.stop_reason == "refusal":
                        del history[start_len:]
                        return {"reply": "I can't help with that request.", "events": events}
                    history.append({"role": "assistant", "content": resp.content})
                    tool_uses = [b for b in resp.content if b.type == "tool_use"]
                    if resp.stop_reason != "tool_use" or not tool_uses:
                        reply = "".join(b.text for b in resp.content if b.type == "text").strip()
                        return {"reply": reply or "Done.", "events": events}
                    results = []
                    for tu in tool_uses:
                        args = tu.input if isinstance(tu.input, dict) else {}
                        result = self.service.handle_tool(tu.name, args)
                        events.append({"tool": tu.name, "input": args, "result": result})
                        results.append({"type": "tool_result", "tool_use_id": tu.id, "content": json.dumps(result, default=str)})
                    history.append({"role": "user", "content": results})
                return {"reply": "I stopped after several steps. Please check the activity log and try again.", "events": events}
            except self._anthropic.APIError as e:
                del history[start_len:]  # keep the transcript valid for the next turn
                log.exception("Claude API error")
                return {"reply": f"The AI service returned an error: {getattr(e, 'message', str(e))}", "events": events, "error": True}

    def draft_reminder(self, invoice: dict[str, Any], n: int) -> tuple[str, str]:
        """Claude writes the reminder text, escalating tone with each reminder."""
        tone = ["", "friendly and light", "polite but a little more direct", "firm and clear, still courteous"][min(n, 3)]
        facts = {k: invoice.get(k) for k in ("recipient_name", "recipient_email", "total", "currency", "items", "note", "due_in_days", "reminders_sent")}
        try:
            resp = self._create(
                max_tokens=2000,
                output_config={
                    "effort": "low",
                    "format": {
                        "type": "json_schema",
                        "schema": {
                            "type": "object",
                            "properties": {"subject": {"type": "string"}, "note": {"type": "string"}},
                            "required": ["subject", "note"],
                            "additionalProperties": False,
                        },
                    },
                },
                messages=[{
                    "role": "user",
                    "content": (
                        f"Write payment reminder #{n} for this unpaid PayPal invoice. Tone: {tone}. "
                        "Subject under 80 characters. Note under 90 words, signed off as the business, "
                        "mentioning the amount and what it was for. No placeholders.\n\n"
                        f"Invoice: {json.dumps(facts, default=str)}"
                    ),
                }],
            )
            if resp.stop_reason == "refusal":
                return default_drafter(invoice, n)
            data = json.loads(next(b.text for b in resp.content if b.type == "text"))
            return str(data["subject"])[:100], str(data["note"])[:2000]
        except Exception:  # reminders must still go out if the AI is unavailable
            log.exception("reminder drafting failed; using template")
            return default_drafter(invoice, n)


# ---------------------------------------------------------------------------
# Mock agent: deterministic parsing for keyless demos and tests.
# ---------------------------------------------------------------------------

EMAIL = r"[\w.+-]+@[\w-]+\.[\w.-]+"
AMOUNT = r"(?:\$|usd\s*)?(\d[\d,]*(?:\.\d{1,2})?)\s*(?:usd|dollars|bucks)?"


class MockAgent:
    mode = "mock"

    def __init__(self, service: Service) -> None:
        self.service = service

    def draft_reminder(self, invoice: dict[str, Any], n: int) -> tuple[str, str]:
        return default_drafter(invoice, n)

    def chat(self, session_id: str, text: str) -> dict[str, Any]:
        t = text.strip()
        low = t.lower()
        email = re.search(EMAIL, t)
        events: list[dict[str, Any]] = []

        def call(tool: str, args: dict[str, Any]) -> dict[str, Any]:
            res = self.service.handle_tool(tool, args)
            events.append({"tool": tool, "input": args, "result": res})
            return res

        if re.search(r"\b(rules?|limits?)\b", low) and not email:
            r = call("get_rules", {})["rules"]
            return {"reply": f"Your auto-approve limit is ${r['spending_limit']} per payment and ${r['daily_limit']} per day. Change them in the Rules panel.", "events": events}

        if re.search(r"\bremind", low):
            ref = re.search(r"(inv_\w+|INV2-[\w-]+)", t)
            inv_id = ref.group(1) if ref else self._latest_open(email.group(0) if email else None)
            if not inv_id:
                return {"reply": "Which invoice should I remind? Give me the client's email or the invoice id.", "events": events}
            res = call("send_reminder", {"invoice_id": inv_id})
            return {"reply": self._say(res, f"Reminder #{res.get('reminder_number')} sent for {inv_id}."), "events": events}

        if re.search(r"\bcancel", low):
            ref = re.search(r"(inv_\w+|INV2-[\w-]+)", t)
            inv_id = ref.group(1) if ref else self._latest_open(email.group(0) if email else None)
            if not inv_id:
                return {"reply": "Which invoice should I cancel?", "events": events}
            res = call("cancel_invoice", {"invoice_id": inv_id, "reason": "Cancelled at the sender's request."})
            return {"reply": self._say(res, "Cancelled."), "events": events}

        if re.search(r"\b(list|show|status|unpaid|outstanding)\b", low) and not re.search(r"\b(pay|invoice|bill)\b.*" + EMAIL, low):
            if "payout" in low or "paid out" in low or "sent" in low and "invoice" not in low:
                res = call("list_payouts", {})
                lines = [f"{p['recipient_email']} {p['currency']} {p['amount']} ({p['status']})" for p in res["payouts"][:5]]
                return {"reply": "Recent payouts: " + ("; ".join(lines) or "none yet."), "events": events}
            status = "SENT" if re.search(r"unpaid|outstanding|open", low) else ""
            res = call("list_invoices", {"status": status} if status else {})
            lines = [f"{i['id']} to {i['recipient_email']} for {i['currency']} {i['total']} ({i['status']})" for i in res["invoices"][:5]]
            return {"reply": "Your invoices: " + ("; ".join(lines) or "none yet."), "events": events}

        amount = None
        for m in re.finditer(AMOUNT, low):
            if m.group(0).strip() and ("$" in m.group(0) or "usd" in m.group(0) or "dollar" in m.group(0) or "buck" in m.group(0) or "." in m.group(1)):
                amount = m.group(1)
                break
        if amount is None:
            nums = [m.group(1) for m in re.finditer(r"(?<![\w.@])(\d[\d,]*(?:\.\d{1,2})?)(?![\w@])", low)]
            amount = nums[0] if nums else None
        what = re.search(r"\bfor (.+?)(?:[.,;]|\bdue\b|$)", t, re.I)
        what_text = what.group(1).strip() if what else None
        if what_text and re.fullmatch(r"\$?[\d,.]+", what_text):
            what_text = None

        if re.search(r"\b(invoice|bill|charge|request)\b", low):
            if not email or not amount:
                return {"reply": "Who should I invoice, and for how much? For example: invoice alice@example.com $250 for logo design.", "events": events}
            due = re.search(r"due in (\d+) days?", low)
            res = call("create_invoice", {
                "recipient_email": email.group(0), "currency": "USD",
                "items": [{"description": (what_text or "Services").capitalize(), "quantity": 1, "unit_amount": amount}],
                "note": "Thanks for working with me!", "due_in_days": int(due.group(1)) if due else 14,
            })
            inv = res.get("invoice", {})
            return {"reply": self._say(res, f"Invoice {inv.get('id')} for ${inv.get('total')} sent to {inv.get('recipient_email')} through PayPal."), "events": events}

        if re.search(r"\b(pay|send|transfer|reimburse|tip)\b", low):
            if not email or not amount:
                return {"reply": "Who should I pay, and how much? For example: pay bob@example.com $40 for lunch.", "events": events}
            res = call("send_payout", {"recipient_email": email.group(0), "amount": amount, "currency": "USD", "note": what_text or "Payment via PayPilot"})
            p = res.get("payout", {})
            return {"reply": self._say(res, f"Sent ${p.get('amount')} to {p.get('recipient_email')} (auto-approved: within your limit)."), "events": events}

        return {"reply": "I can create invoices, send payouts, remind clients and show what's outstanding. Try: invoice alice@example.com $250 for logo design.", "events": events}

    def _latest_open(self, email: str | None) -> str | None:
        for inv in self.service.store.list("invoice", 200):
            if inv["status"] in {"SENT", "UNPAID", "PARTIALLY_PAID"} and (not email or inv["recipient_email"] == email.lower()):
                return inv["id"]
        return None

    @staticmethod
    def _say(res: dict[str, Any], ok: str) -> str:
        s = res.get("status")
        if s == "executed":
            return ok
        if s == "pending_approval":
            return "I've queued this for your approval, because " + "; ".join(res["reasons"]) + ". Nothing has been sent yet."
        if s == "denied":
            return "I can't do that: " + "; ".join(res["reasons"]) + "."
        return "That didn't work: " + str(res.get("error"))
