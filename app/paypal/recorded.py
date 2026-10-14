"""Recorded PayPal sandbox responses, used only when DEMO_MODE=1.

In demo mode the PayPal client gets a `RecordedSandbox`. If the live sandbox
can't be reached (network error, timeout, or a 5xx after the normal retries),
the call is answered from the recorded responses in app/paypal/recorded/
instead, so a live demo doesn't die mid-click. With no PayPal keys at all, every
call is answered this way. Outside demo mode none of this is wired in.

Every simulated answer carries a PayPal-Debug-Id starting with "demo-recorded-",
and simulated invoices and batches get "DEMO" ids, so the audit log and the UI
show exactly which steps were simulated. Webhook signatures are never
"verified" from a recording: that call still fails, so PayPal retries later.
"""

from __future__ import annotations

import copy
import datetime as dt
import json
import threading
from decimal import Decimal
from pathlib import Path
from typing import Any

from app.paypal.errors import PayPalNotFound, PayPalUnavailable

FIXTURES = Path(__file__).resolve().parent / "recorded"
INVOICE_PREFIX = "INV2-DEMO-"
BATCH_PREFIX = "DEMO-BATCH-"
DEBUG_PREFIX = "demo-recorded-"


def _fixture(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


class RecordedSandbox:
    def __init__(self, *, always: bool = False) -> None:
        self.always = always  # no PayPal keys: answer everything from recordings
        self.invoices: dict[str, dict[str, Any]] = {}
        self.payouts: dict[str, dict[str, Any]] = {}
        self.reminders: list[dict[str, Any]] = []
        self._by_request_id: dict[str, tuple[int, dict[str, Any]]] = {}
        self._templates = {
            "invoice": _fixture("invoice.json"),
            "payout_create": _fixture("payout_create.json"),
            "payout_get": _fixture("payout_get.json"),
        }
        self._n = 0
        self._lock = threading.Lock()

    def owns(self, path: str) -> bool:
        """Simulated objects stay simulated, even once the live sandbox is back."""
        return self.always or INVOICE_PREFIX in path or f"/payouts/{BATCH_PREFIX}" in path

    def respond(
        self, method: str, path: str, body: Any = None, request_id: str | None = None
    ) -> tuple[int, dict[str, Any], str]:
        """Return (status, json body, debug id) for one PayPal call."""
        with self._lock:
            if request_id and request_id in self._by_request_id:
                status, data = self._by_request_id[request_id]  # idempotent, like PayPal
            else:
                status, data = self._route(method.upper(), path, body or {})
                if request_id:
                    self._by_request_id[request_id] = (status, data)
            self._n += 1
            return status, copy.deepcopy(data), f"{DEBUG_PREFIX}{self._n:05d}"

    # ------------------------------------------------------------------ routes
    def _route(self, method: str, path: str, body: dict[str, Any]) -> tuple[int, dict]:
        parts = path.strip("/").split("/")
        if path == "/v1/notifications/verify-webhook-signature":
            raise PayPalUnavailable("webhook signatures can't be verified from recordings")
        if path == "/v2/invoicing/invoices" and method == "POST":
            return 201, self._create_invoice(body)
        if parts[:3] == ["v2", "invoicing", "invoices"] and len(parts) >= 4:
            inv = self.invoices.get(parts[3]) or self._unknown_invoice(parts[3])
            if method == "GET":
                return 200, inv
            if parts[-1] == "send":
                inv["status"] = "SENT"
                return 200, {"href": f"/v2/invoicing/invoices/{inv['id']}"}
            if parts[-1] == "remind":
                self.reminders.append({"id": inv["id"], **body})
                return 204, {}
        if path == "/v1/payments/payouts" and method == "POST":
            return 201, self._create_payout(body)
        if parts[:3] == ["v1", "payments", "payouts"] and len(parts) == 4 and method == "GET":
            return 200, self._payout_status(parts[3])
        raise PayPalNotFound(f"no recorded response for {method} {path}", status=404)

    def _create_invoice(self, body: dict[str, Any]) -> dict[str, Any]:
        n = len(self.invoices) + 1
        inv = copy.deepcopy(self._templates["invoice"])
        inv_id = f"{INVOICE_PREFIX}{n:04d}"
        detail = body.get("detail", {})
        currency = detail.get("currency_code", "USD")
        total = sum(
            (
                Decimal(i["unit_amount"]["value"]) * Decimal(str(i.get("quantity", "1")))
                for i in body.get("items", [])
            ),
            Decimal("0"),
        )
        inv["id"] = inv_id
        inv["detail"].update({k: v for k, v in detail.items() if k != "metadata"})
        inv["detail"]["invoice_number"] = f"D{n:03d}"
        # no payer link: a simulated invoice has no real page to open
        inv["detail"]["metadata"] = {"create_time": dt.datetime.now(dt.UTC).isoformat()}
        inv["primary_recipients"] = body.get("primary_recipients", [])
        inv["items"] = body.get("items", [])
        inv["amount"] = inv["due_amount"] = {"currency_code": currency, "value": f"{total:.2f}"}
        self.invoices[inv_id] = inv
        return inv

    def _unknown_invoice(self, inv_id: str) -> dict[str, Any]:
        """A demo invoice from before a restart (the seed data): still open."""
        inv = copy.deepcopy(self._templates["invoice"])
        inv.update(id=inv_id, status="SENT")
        inv["detail"]["invoice_number"] = None
        self.invoices[inv_id] = inv
        return inv

    def _create_payout(self, body: dict[str, Any]) -> dict[str, Any]:
        sender_batch_id = body["sender_batch_header"]["sender_batch_id"]
        batch_id = f"{BATCH_PREFIX}{len(self.payouts) + 1:04d}"
        out = copy.deepcopy(self._templates["payout_create"])
        out["batch_header"]["payout_batch_id"] = batch_id
        out["batch_header"]["sender_batch_header"] = body["sender_batch_header"]
        self.payouts[batch_id] = {"sender_batch_id": sender_batch_id, "body": body}
        return out

    def _payout_status(self, batch_id: str) -> dict[str, Any]:
        out = copy.deepcopy(self._templates["payout_get"])
        out["batch_header"]["payout_batch_id"] = batch_id
        item = (self.payouts.get(batch_id, {}).get("body", {}).get("items") or [{}])[0]
        out["items"] = out.get("items") or [{"payout_item": {}}]
        out["items"][0]["payout_batch_id"] = batch_id
        out["items"][0]["payout_item_id"] = batch_id.replace("BATCH", "ITEM")
        out["items"][0]["transaction_status"] = "SUCCESS"
        if item:
            out["items"][0].setdefault("payout_item", {}).update(
                amount=item["amount"], receiver=item["receiver"]
            )
        return out
