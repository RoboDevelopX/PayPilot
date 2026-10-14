"""A scripted stand-in for the LLM, used only when DEMO_MODE=1.

With no AI key set (or when the AI service fails or runs out of free quota),
demo mode answers the demo script's phrasings with the same tool calls a model
would make. Everything after that is real: schema validation, the policy
engine, PayPal (or its recordings), approvals and the audit log. It knows a
handful of patterns, says so when it doesn't understand, and starts every
reply with "Demo script" so nobody mistakes it for the model.
"""

from __future__ import annotations

import json
import logging
import re
from types import SimpleNamespace
from typing import Any

import anthropic

from app.agent.llm import LLMUnavailable

log = logging.getLogger("paypilot.demo")

LABEL = "Demo script (no AI model):"
AMOUNT = r"\$?(?P<amount>\d{1,7}(?:\.\d{1,2})?)"
EMAIL = r"[^\s()<>@,]+@[^\s()<>@,]+\.[A-Za-z]{2,}"
INVOICE = re.compile(
    rf"^(?:please\s+)?(?:invoice|bill)\s+(?P<name>.+?)\s*(?:\(\s*(?P<email>{EMAIL})\s*\))?\s+"
    rf"{AMOUNT}\s+for\s+(?P<item>.+?)(?:[,.]?\s*(?:due\s+)?in\s+(?P<days>\d{{1,3}})\s+days?)?\s*\.?$",
    re.IGNORECASE,
)
PAYOUT = re.compile(
    rf"^(?:please\s+)?(?:pay|send)\s+(?P<email>{EMAIL})\s+{AMOUNT}"
    rf"(?:\s+(?:for|as)\s+(?P<note>.+?))?\s*\.?$",
    re.IGNORECASE,
)
REMIND = re.compile(
    r"(?:send\s+a\s+reminder\s+to|remind|chase|nudge)\s+(?P<ref>[\w .&'#-]+?)\s*[.?!]?$",
    re.IGNORECASE,
)
STATUS = re.compile(r"\b(overdue|late|outstanding|unpaid|owe)\b", re.IGNORECASE)
LIMIT = re.compile(rf"auto[- ]?approv\w*\s+limit\s+to\s+{AMOUNT}", re.IGNORECASE)
HELP = (
    "I only understand the demo script's phrasings, for example: "
    "'Invoice Acme Corp (ap@acme.example) $450 for logo design, due in 14 days', "
    "'Pay sam@example.com $40 for the icon set', 'Which invoices are overdue?', "
    "'Send a reminder to Acme Corp', or 'Raise my auto-approve limit to $75'."
)


def _text(t: str) -> SimpleNamespace:
    return SimpleNamespace(type="text", text=f"{LABEL} {t}")


def _tool(name: str, args: dict[str, Any], n: int) -> SimpleNamespace:
    return SimpleNamespace(type="tool_use", id=f"demo_{n}", name=name, input=args)


def _invoice_args(m: re.Match, email: str) -> dict[str, Any]:
    return {
        "client_name": m["name"].strip(" ,"),
        "client_email": email,
        "items": [{"name": m["item"].strip(" ,."), "quantity": 1, "unit_amount": m["amount"]}],
        "currency": "USD",
        "due_in_days": int(m["days"] or 14),
        "note": "",
    }


class ScriptedLLM:
    """Callable with the same keyword arguments the agent passes to a real model."""

    def __call__(self, **kw: Any) -> SimpleNamespace:
        messages = kw["messages"]
        last = messages[-1]["content"]
        n = len(messages)
        if isinstance(last, list):  # tool results came back: summarize them
            return SimpleNamespace(content=[_text(self._summary(last))], stop_reason="end_turn")
        return self._plan(last.strip(), messages, n)

    def _plan(self, text: str, messages: list[dict], n: int) -> SimpleNamespace:
        def call(name: str, args: dict[str, Any]) -> SimpleNamespace:
            return SimpleNamespace(content=[_tool(name, args, n)], stop_reason="tool_use")

        if m := INVOICE.match(text):
            if m["email"]:
                return call("create_invoice", _invoice_args(m, m["email"]))
            return call(
                "ask_clarification",
                {
                    "question": f"{LABEL} What email address should the invoice to "
                    f"{m['name'].strip()} go to?",
                    "missing_fields": ["client_email"],
                },
            )
        if m := PAYOUT.match(text):
            email = m["email"]
            return call(
                "send_payout",
                {
                    "recipient_email": email,
                    "recipient_name": email.split("@")[0].replace(".", " ").title(),
                    "amount": m["amount"],
                    "currency": "USD",
                    "note": (m["note"] or "").strip(),
                },
            )
        if m := LIMIT.search(text):
            return call("update_policy", {"field": "auto_approve_limit", "new_value": m["amount"]})
        if m := REMIND.search(text):
            return call("send_reminder", {"invoice_ref": m["ref"].strip()})
        if STATUS.search(text):
            return call("get_status", {"target": "invoice", "ref": ""})
        if email := re.fullmatch(rf"\s*(?P<e>{EMAIL})\s*\.?", text):
            pending = self._pending_invoice(messages)
            if pending:
                return call("create_invoice", _invoice_args(pending, email["e"]))
        return SimpleNamespace(content=[_text(HELP)], stop_reason="end_turn")

    @staticmethod
    def _pending_invoice(messages: list[dict]) -> re.Match | None:
        """The earlier 'Invoice X $N for Y' message that was missing an email."""
        for msg in reversed(messages[:-1]):
            if msg["role"] == "user" and isinstance(msg["content"], str):
                m = INVOICE.match(msg["content"].strip())
                return m if m and not m["email"] else None
        return None

    @staticmethod
    def _summary(results: list[dict[str, Any]]) -> str:
        lines = []
        for r in results:
            try:
                out = json.loads(r["content"])
            except (TypeError, ValueError, KeyError):
                continue
            invoices = (out.get("data") or {}).get("invoices")
            if invoices is not None:
                late = [i for i in invoices if i.get("days_overdue")]
                if not late:
                    lines.append("Nothing is overdue right now.")
                for i in late:
                    risk = i.get("late_payment_risk")
                    lines.append(
                        f"{i['client']['untrusted_text']} owes ${i['amount']}, "
                        f"{i['days_overdue']} days overdue"
                        + (f" (late-payment risk {risk:.0%})" if risk is not None else "")
                        + "."
                    )
                continue
            line = out.get("message") or out.get("status", "")
            if out.get("reasons"):
                line += " Why: " + "; ".join(out["reasons"]) + "."
            lines.append(line)
        return " ".join(lines) or "Done."


class DemoFallbackLLM:
    """Use the real model; if it is unavailable, answer with the demo script instead."""

    def __init__(self, primary: Any, scripted: ScriptedLLM | None = None) -> None:
        self.primary = primary
        self.scripted = scripted or ScriptedLLM()

    def __call__(self, **kw: Any) -> Any:
        try:
            return self.primary(**kw)
        except (LLMUnavailable, anthropic.APIError) as e:
            log.warning("AI service unavailable (%s); answering with the demo script", e)
            return self.scripted(**kw)
