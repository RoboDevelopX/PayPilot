"""Test doubles: a scripted Claude and a mocked PayPal sandbox."""

from __future__ import annotations

import copy
from types import SimpleNamespace
from typing import Any


def text(t: str) -> SimpleNamespace:
    return SimpleNamespace(type="text", text=t)


def tool(name: str, args: dict[str, Any], id_: str = "tu_1") -> SimpleNamespace:
    return SimpleNamespace(type="tool_use", id=id_, name=name, input=args)


def message(*blocks, stop_reason: str | None = None) -> SimpleNamespace:
    if stop_reason is None:
        stop_reason = "tool_use" if any(b.type == "tool_use" for b in blocks) else "end_turn"
    return SimpleNamespace(content=list(blocks), stop_reason=stop_reason)


class ScriptedLLM:
    """Returns pre-baked responses in order and records what it was sent."""

    def __init__(self, *responses) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    def __call__(self, **kwargs):
        self.calls.append(copy.copy(kwargs) | {"messages": list(kwargs["messages"])})
        if not self.responses:
            raise AssertionError("ScriptedLLM ran out of responses")
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r

    def tool_results(self, call_index: int) -> list[dict[str, Any]]:
        """tool_result blocks in the last user message of a given call."""
        last = self.calls[call_index]["messages"][-1]
        return [
            b for b in last["content"] if isinstance(b, dict) and b.get("type") == "tool_result"
        ]


INVOICE_ID = "INV2-TEST-AAAA-BBBB-CCCC"


def paypal_invoice(status: str = "DRAFT", value: str = "450.00") -> dict[str, Any]:
    return {
        "id": INVOICE_ID,
        "status": status,
        "detail": {
            "invoice_number": "0042",
            "metadata": {"recipient_view_url": "https://www.sandbox.paypal.com/invoice/p/#X"},
        },
        "amount": {"currency_code": "USD", "value": value},
        "due_amount": {"currency_code": "USD", "value": value},
    }


class FakeSandbox:
    """A tiny in-memory PayPal sandbox behind httpx.MockTransport.

    Implements just enough of OAuth, Invoicing v2 and Payouts v1 for tests,
    including PayPal-Request-Id idempotency.
    """

    def __init__(self) -> None:
        import httpx

        self.requests: list[httpx.Request] = []
        self.invoices: dict[str, dict[str, Any]] = {}
        self.payouts: dict[str, dict[str, Any]] = {}
        self.reminders: list[dict[str, Any]] = []
        self.by_request_id: dict[str, Any] = {}
        self.fail_next: int | None = None  # status code to return once
        self.transport = httpx.MockTransport(self.handle)

    def handle(self, request):
        import json

        import httpx

        self.requests.append(request)
        path = request.url.path
        if path == "/v1/oauth2/token":
            return httpx.Response(200, json={"access_token": "A21AA-fake", "expires_in": 32400})
        if self.fail_next:
            code, self.fail_next = self.fail_next, None
            return httpx.Response(
                code, json={"name": "ERR", "message": "boom", "debug_id": "dbg-fail"}
            )
        rid = request.headers.get("PayPal-Request-Id")
        if rid and rid in self.by_request_id:
            return self.by_request_id[rid]
        body = json.loads(request.content) if request.content else {}
        resp = self._route(request.method, path, body)
        if rid:
            self.by_request_id[rid] = resp
        return resp

    def _route(self, method, path, body):
        import httpx

        parts = path.strip("/").split("/")
        if path == "/v2/invoicing/invoices" and method == "POST":
            inv_id = f"INV2-TEST-{len(self.invoices) + 1:04d}"
            total = sum(
                float(i["unit_amount"]["value"]) * int(i["quantity"]) for i in body["items"]
            )
            self.invoices[inv_id] = {
                "id": inv_id,
                "status": "DRAFT",
                "detail": {
                    **body["detail"],
                    "invoice_number": f"{len(self.invoices) + 1:04d}",
                    "metadata": {
                        "recipient_view_url": f"https://www.sandbox.paypal.com/invoice/p/#{inv_id}"
                    },
                },
                "amount": {
                    "currency_code": body["detail"]["currency_code"],
                    "value": f"{total:.2f}",
                },
                "due_amount": {
                    "currency_code": body["detail"]["currency_code"],
                    "value": f"{total:.2f}",
                },
                "primary_recipients": body["primary_recipients"],
            }
            return httpx.Response(
                201, json=self.invoices[inv_id], headers={"paypal-debug-id": "dbg-create"}
            )
        if parts[:3] == ["v2", "invoicing", "invoices"] and len(parts) >= 4:
            inv = self.invoices.get(parts[3])
            if inv is None:
                return httpx.Response(
                    404, json={"name": "RESOURCE_NOT_FOUND", "message": "not found"}
                )
            if method == "GET":
                return httpx.Response(200, json=inv)
            if parts[-1] == "send":
                inv["status"] = "UNPAID"
                return httpx.Response(200, json={"href": "x"})
            if parts[-1] == "remind":
                self.reminders.append({"id": parts[3], **body})
                return httpx.Response(204)
        if path == "/v1/payments/payouts" and method == "POST":
            sbid = body["sender_batch_header"]["sender_batch_id"]
            if sbid in self.payouts:
                return httpx.Response(
                    400,
                    json={
                        "name": "USER_BUSINESS_ERROR",
                        "message": "Batch with given sender_batch_id already exists",
                    },
                )
            batch_id = f"BATCH{len(self.payouts) + 1:04d}"
            self.payouts[sbid] = {"batch_id": batch_id, "body": body}
            return httpx.Response(
                201,
                json={
                    "batch_header": {
                        "payout_batch_id": batch_id,
                        "batch_status": "PENDING",
                        "sender_batch_header": body["sender_batch_header"],
                    }
                },
            )
        if path.startswith("/v1/payments/payouts/") and method == "GET":
            return httpx.Response(
                200,
                json={
                    "batch_header": {"payout_batch_id": parts[-1], "batch_status": "SUCCESS"},
                    "items": [{"payout_item_id": "ITEM1", "transaction_status": "SUCCESS"}],
                },
            )
        if path == "/v1/notifications/verify-webhook-signature":
            ok = body.get("transmission_sig") == "valid-sig"
            return httpx.Response(200, json={"verification_status": "SUCCESS" if ok else "FAILURE"})
        return httpx.Response(404, json={"message": f"unmocked {method} {path}"})

    def calls_to(self, path_prefix: str):
        return [r for r in self.requests if r.url.path.startswith(path_prefix)]
