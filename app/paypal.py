"""PayPal REST clients.

`SandboxPayPal` talks to the real PayPal sandbox (Invoicing v2, Payouts v1,
webhook signature verification). `MockPayPal` mirrors the same interface in
memory so the app runs with no credentials. Only `actions.py` calls these, and
only after the rules engine has approved an action. The model never does.
"""
from __future__ import annotations

import datetime as dt
import threading
import time
import uuid
from typing import Any, Protocol

import httpx


class PayPalError(RuntimeError):
    def __init__(self, message: str, status: int | None = None, body: Any = None) -> None:
        super().__init__(message)
        self.status = status
        self.body = body


class PayPal(Protocol):
    mode: str

    def create_invoice(self, action: dict[str, Any], request_id: str) -> dict[str, Any]: ...
    def send_invoice(self, invoice_id: str) -> dict[str, Any]: ...
    def get_invoice(self, invoice_id: str) -> dict[str, Any]: ...
    def remind_invoice(self, invoice_id: str, subject: str, note: str) -> None: ...
    def cancel_invoice(self, invoice_id: str, note: str) -> None: ...
    def create_payout(self, action: dict[str, Any], batch_id: str) -> dict[str, Any]: ...
    def get_payout(self, payout_batch_id: str) -> dict[str, Any]: ...


def _split_name(full: str) -> dict[str, str]:
    parts = full.strip().split()
    if not parts:
        return {}
    if len(parts) == 1:
        return {"given_name": parts[0]}
    return {"given_name": " ".join(parts[:-1]), "surname": parts[-1]}


def invoice_payload(action: dict[str, Any], invoicer_email: str, business_name: str) -> dict[str, Any]:
    """Build the Invoicing v2 create-invoice body from a validated action."""
    today = dt.date.today()
    due = today + dt.timedelta(days=action["due_in_days"])
    recipient: dict[str, Any] = {"email_address": action["recipient_email"]}
    if action.get("recipient_name"):
        recipient["name"] = _split_name(action["recipient_name"])
    invoicer: dict[str, Any] = {"business_name": business_name}
    if invoicer_email:
        invoicer["email_address"] = invoicer_email
    body: dict[str, Any] = {
        "detail": {
            "currency_code": action["currency"],
            "invoice_date": today.isoformat(),
            "payment_term": {"due_date": due.isoformat()},
            "note": action.get("note") or "Thank you for your business!",
        },
        "invoicer": invoicer,
        "primary_recipients": [{"billing_info": recipient}],
        "items": [
            {
                "name": it["description"],
                "quantity": str(it["quantity"]),
                "unit_amount": {"currency_code": action["currency"], "value": it["unit_amount"]},
                "unit_of_measure": "QUANTITY",
            }
            for it in action["items"]
        ],
    }
    return body


class SandboxPayPal:
    mode = "sandbox"

    def __init__(self, client_id: str, secret: str, base_url: str, invoicer_email: str, business_name: str) -> None:
        self._auth = (client_id, secret)
        self._base = base_url.rstrip("/")
        self._invoicer_email = invoicer_email
        self._business_name = business_name
        self._token: str | None = None
        self._token_expiry = 0.0
        self._lock = threading.Lock()
        self._http = httpx.Client(timeout=30)

    # -- auth ---------------------------------------------------------------
    def _access_token(self) -> str:
        with self._lock:
            if self._token and time.time() < self._token_expiry - 60:
                return self._token
            try:
                r = self._http.post(
                    f"{self._base}/v1/oauth2/token",
                    auth=self._auth,
                    data={"grant_type": "client_credentials"},
                    headers={"Accept": "application/json"},
                )
            except httpx.HTTPError as e:
                raise PayPalError(f"Could not reach PayPal: {e}") from e
            if r.status_code != 200:
                raise PayPalError("PayPal authentication failed. Check PAYPAL_CLIENT_ID and PAYPAL_CLIENT_SECRET.", r.status_code, r.text)
            data = r.json()
            self._token = data["access_token"]
            self._token_expiry = time.time() + int(data.get("expires_in", 3000))
            return self._token

    def _request(self, method: str, path: str, *, json: Any = None, headers: dict[str, str] | None = None) -> Any:
        h = {
            "Authorization": f"Bearer {self._access_token()}",
            "Content-Type": "application/json",
            "Prefer": "return=representation",
        }
        h.update(headers or {})
        try:
            r = self._http.request(method, f"{self._base}{path}", json=json, headers=h)
            if r.status_code == 401:  # token revoked early: refresh once
                self._token = None
                h["Authorization"] = f"Bearer {self._access_token()}"
                r = self._http.request(method, f"{self._base}{path}", json=json, headers=h)
        except httpx.HTTPError as e:
            raise PayPalError(f"Could not reach PayPal: {e}") from e
        if r.status_code >= 400:
            try:
                body = r.json()
                msg = body.get("message") or body.get("name") or r.text
                details = body.get("details") or []
                if details:
                    msg += ": " + "; ".join(d.get("description") or d.get("issue", "") for d in details)
            except ValueError:
                body, msg = r.text, r.text
            raise PayPalError(f"PayPal {method} {path} failed ({r.status_code}): {msg}", r.status_code, body)
        if not r.content:
            return {}
        try:
            return r.json()
        except ValueError:
            return {}

    # -- Invoicing v2 ---------------------------------------------------------
    def create_invoice(self, action: dict[str, Any], request_id: str) -> dict[str, Any]:
        body = invoice_payload(action, self._invoicer_email, self._business_name)
        data = self._request("POST", "/v2/invoicing/invoices", json=body, headers={"PayPal-Request-Id": request_id})
        invoice_id = data.get("id")
        if not invoice_id:  # some responses only carry a self link
            for link in data.get("links", []):
                if link.get("rel") == "self":
                    invoice_id = link["href"].rstrip("/").split("/")[-1]
        if not invoice_id:
            raise PayPalError("PayPal did not return an invoice id", body=data)
        return self.get_invoice(invoice_id)

    def send_invoice(self, invoice_id: str) -> dict[str, Any]:
        self._request(
            "POST",
            f"/v2/invoicing/invoices/{invoice_id}/send",
            json={"send_to_invoicer": False, "send_to_recipient": True},
            headers={"PayPal-Request-Id": f"send-{invoice_id}"},
        )
        return self.get_invoice(invoice_id)

    def get_invoice(self, invoice_id: str) -> dict[str, Any]:
        data = self._request("GET", f"/v2/invoicing/invoices/{invoice_id}")
        return {
            "paypal_id": data.get("id", invoice_id),
            "status": data.get("status", "UNKNOWN"),
            "number": data.get("detail", {}).get("invoice_number"),
            "pay_url": data.get("detail", {}).get("metadata", {}).get("recipient_view_url"),
            "amount_due": data.get("due_amount", {}).get("value"),
        }

    def remind_invoice(self, invoice_id: str, subject: str, note: str) -> None:
        self._request(
            "POST",
            f"/v2/invoicing/invoices/{invoice_id}/remind",
            json={"subject": subject[:100], "note": note[:4000], "send_to_invoicer": False, "send_to_recipient": True},
        )

    def cancel_invoice(self, invoice_id: str, note: str) -> None:
        self._request(
            "POST",
            f"/v2/invoicing/invoices/{invoice_id}/cancel",
            json={"subject": "Invoice cancelled", "note": note[:4000], "send_to_invoicer": False, "send_to_recipient": True},
        )

    # -- Payouts v1 -----------------------------------------------------------
    def create_payout(self, action: dict[str, Any], batch_id: str) -> dict[str, Any]:
        body = {
            "sender_batch_header": {
                "sender_batch_id": batch_id,  # PayPal rejects duplicates, so retries cannot double-pay
                "email_subject": "You have a payment",
                "email_message": action.get("note") or "You received a payment via PayPilot.",
            },
            "items": [
                {
                    "recipient_type": "EMAIL",
                    "receiver": action["recipient_email"],
                    "amount": {"value": action["amount"], "currency": action["currency"]},
                    "note": action.get("note") or "Payment via PayPilot",
                    "sender_item_id": batch_id,
                }
            ],
        }
        data = self._request("POST", "/v1/payments/payouts", json=body)
        header = data.get("batch_header", {})
        return {"paypal_id": header.get("payout_batch_id"), "status": header.get("batch_status", "PENDING")}

    def get_payout(self, payout_batch_id: str) -> dict[str, Any]:
        data = self._request("GET", f"/v1/payments/payouts/{payout_batch_id}")
        return {"paypal_id": payout_batch_id, "status": data.get("batch_header", {}).get("batch_status", "UNKNOWN")}

    # -- Webhooks ---------------------------------------------------------------
    def verify_webhook(self, headers: dict[str, str], event: dict[str, Any], webhook_id: str) -> bool:
        body = {
            "auth_algo": headers.get("paypal-auth-algo"),
            "cert_url": headers.get("paypal-cert-url"),
            "transmission_id": headers.get("paypal-transmission-id"),
            "transmission_sig": headers.get("paypal-transmission-sig"),
            "transmission_time": headers.get("paypal-transmission-time"),
            "webhook_id": webhook_id,
            "webhook_event": event,
        }
        data = self._request("POST", "/v1/notifications/verify-webhook-signature", json=body)
        return data.get("verification_status") == "SUCCESS"


class MockPayPal:
    """An in-memory stand-in with the same interface. Used when no PayPal keys are set."""

    mode = "mock"

    def __init__(self) -> None:
        self.invoices: dict[str, dict[str, Any]] = {}
        self.payouts: dict[str, dict[str, Any]] = {}
        self.reminders: list[dict[str, Any]] = []
        self._seen_requests: dict[str, str] = {}

    def create_invoice(self, action: dict[str, Any], request_id: str) -> dict[str, Any]:
        if request_id in self._seen_requests:  # same idempotency as PayPal-Request-Id
            return self.get_invoice(self._seen_requests[request_id])
        pid = f"INV2-MOCK-{uuid.uuid4().hex[:4].upper()}-{uuid.uuid4().hex[:4].upper()}"
        self.invoices[pid] = {"status": "DRAFT", "amount": action["total"], "number": f"{len(self.invoices) + 1:04d}"}
        self._seen_requests[request_id] = pid
        return self.get_invoice(pid)

    def send_invoice(self, invoice_id: str) -> dict[str, Any]:
        self._get(invoice_id)["status"] = "SENT"
        return self.get_invoice(invoice_id)

    def get_invoice(self, invoice_id: str) -> dict[str, Any]:
        inv = self._get(invoice_id)
        return {
            "paypal_id": invoice_id,
            "status": inv["status"],
            "number": inv["number"],
            "pay_url": None,
            "amount_due": "0.00" if inv["status"] == "PAID" else inv["amount"],
        }

    def remind_invoice(self, invoice_id: str, subject: str, note: str) -> None:
        if self._get(invoice_id)["status"] not in {"SENT", "UNPAID", "PARTIALLY_PAID"}:
            raise PayPalError("Only sent, unpaid invoices can be reminded", 422)
        self.reminders.append({"invoice_id": invoice_id, "subject": subject, "note": note})

    def cancel_invoice(self, invoice_id: str, note: str) -> None:
        self._get(invoice_id)["status"] = "CANCELLED"

    def mark_paid(self, invoice_id: str) -> None:
        self._get(invoice_id)["status"] = "PAID"

    def create_payout(self, action: dict[str, Any], batch_id: str) -> dict[str, Any]:
        if batch_id in self.payouts:
            raise PayPalError("Batch with given sender_batch_id already exists", 400)
        pid = f"MOCKPAYOUT{uuid.uuid4().hex[:8].upper()}"
        self.payouts[batch_id] = {"paypal_id": pid, "status": "SUCCESS"}
        return {"paypal_id": pid, "status": "SUCCESS"}

    def get_payout(self, payout_batch_id: str) -> dict[str, Any]:
        return {"paypal_id": payout_batch_id, "status": "SUCCESS"}

    def _get(self, invoice_id: str) -> dict[str, Any]:
        if invoice_id not in self.invoices:
            raise PayPalError(f"Invoice {invoice_id} not found", 404)
        return self.invoices[invoice_id]
