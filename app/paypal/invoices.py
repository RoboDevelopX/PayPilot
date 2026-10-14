"""PayPal Invoicing API v2: create a draft, send it, read it, remind the payer.

Endpoints (checked against developer.paypal.com, Oct 2026):
  POST /v2/invoicing/invoices                       create draft
  POST /v2/invoicing/invoices/{id}/send             email it to the recipient
  GET  /v2/invoicing/invoices/{id}                  status, amounts, payer link
  POST /v2/invoicing/invoices/{id}/remind           reminder email
Amounts and quantities are strings in the payload, never floats.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from decimal import Decimal

from pydantic import BaseModel, EmailStr, Field

from app.paypal.client import PayPalClient, PayPalResponse
from app.paypal.errors import PayPalError

OPEN_STATUSES = {"SENT", "UNPAID", "PARTIALLY_PAID", "SCHEDULED"}


class LineItem(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    quantity: int = Field(ge=1, le=10_000)
    unit_amount: Decimal = Field(gt=0, decimal_places=2)


class InvoiceDraft(BaseModel):
    client_name: str = Field(min_length=1, max_length=100)
    client_email: EmailStr
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    items: list[LineItem] = Field(min_length=1, max_length=25)
    invoice_date: dt.date
    due_date: dt.date
    note: str = Field(default="", max_length=1000)

    @property
    def total(self) -> Decimal:
        return sum((i.unit_amount * i.quantity for i in self.items), Decimal("0.00"))


@dataclass(frozen=True)
class InvoiceInfo:
    paypal_id: str
    status: str
    number: str | None
    pay_url: str | None
    total: Decimal | None
    due: Decimal | None
    debug_id: str | None = None


def build_create_payload(draft: InvoiceDraft, *, invoicer_email: str, business_name: str) -> dict:
    invoicer: dict = {"business_name": business_name}
    if invoicer_email:
        invoicer["email_address"] = invoicer_email
    return {
        "detail": {
            "currency_code": draft.currency,
            "invoice_date": draft.invoice_date.isoformat(),
            "payment_term": {"due_date": draft.due_date.isoformat()},
            "note": draft.note or "Thank you for your business!",
            "memo": "Created by PayPilot (sandbox)",
        },
        "invoicer": invoicer,
        "primary_recipients": [
            {
                "billing_info": {
                    "business_name": draft.client_name,
                    "email_address": str(draft.client_email),
                }
            }
        ],
        "items": [
            {
                "name": item.name,
                "quantity": str(item.quantity),
                "unit_amount": {
                    "currency_code": draft.currency,
                    "value": f"{item.unit_amount:.2f}",
                },
                "unit_of_measure": "QUANTITY",
            }
            for item in draft.items
        ],
    }


def _invoice_id_from(resp: PayPalResponse) -> str:
    if resp.data.get("id"):
        return resp.data["id"]
    # With Prefer: return=minimal PayPal answers with only a link to the new invoice.
    href = resp.data.get("href") or next(
        (lk["href"] for lk in resp.data.get("links", []) if lk.get("rel") == "self"), ""
    )
    if href:
        return href.rstrip("/").split("/")[-1]
    raise PayPalError("PayPal did not return an invoice id", debug_id=resp.debug_id)


def _info(data: dict, debug_id: str | None = None) -> InvoiceInfo:
    detail = data.get("detail", {})
    total = data.get("amount", {}).get("value")
    due = data.get("due_amount", {}).get("value")
    return InvoiceInfo(
        paypal_id=data["id"],
        status=data.get("status", "UNKNOWN"),
        number=detail.get("invoice_number"),
        pay_url=detail.get("metadata", {}).get("recipient_view_url"),
        total=Decimal(total) if total else None,
        due=Decimal(due) if due else None,
        debug_id=debug_id,
    )


class InvoicingAPI:
    def __init__(self, client: PayPalClient, *, invoicer_email: str, business_name: str) -> None:
        self.client = client
        self.invoicer_email = invoicer_email
        self.business_name = business_name

    def create_draft(self, draft: InvoiceDraft, *, request_id: str) -> InvoiceInfo:
        body = build_create_payload(
            draft, invoicer_email=self.invoicer_email, business_name=self.business_name
        )
        resp = self.client.request(
            "POST", "/v2/invoicing/invoices", json=body, request_id=request_id
        )
        invoice_id = _invoice_id_from(resp)
        if resp.data.get("id"):
            return _info(resp.data, resp.debug_id)
        return self.get(invoice_id)

    def send(self, invoice_id: str, *, request_id: str) -> InvoiceInfo:
        self.client.request(
            "POST",
            f"/v2/invoicing/invoices/{invoice_id}/send",
            json={"send_to_recipient": True, "send_to_invoicer": False},
            request_id=request_id,
        )
        return self.get(invoice_id)

    def get(self, invoice_id: str) -> InvoiceInfo:
        resp = self.client.request("GET", f"/v2/invoicing/invoices/{invoice_id}")
        return _info(resp.data, resp.debug_id)

    def remind(self, invoice_id: str, *, subject: str, note: str, request_id: str) -> str | None:
        resp = self.client.request(
            "POST",
            f"/v2/invoicing/invoices/{invoice_id}/remind",
            json={
                "subject": subject[:100],
                "note": note[:4000],
                "send_to_recipient": True,
                "send_to_invoicer": False,
            },
            request_id=request_id,
            prefer_representation=False,
        )
        return resp.debug_id
