"""PayPal client tests against a mocked sandbox (respx intercepts httpx)."""

from __future__ import annotations

import datetime as dt
import json
from decimal import Decimal

import httpx
import pytest

from app.paypal.errors import (
    PayPalAuthError,
    PayPalServerError,
    PayPalUnavailable,
    PayPalValidationError,
)
from app.paypal.invoices import InvoiceDraft, InvoicingAPI, LineItem, build_create_payload

INVOICE = {
    "id": "INV2-AAAA-BBBB-CCCC-DDDD",
    "status": "DRAFT",
    "detail": {
        "invoice_number": "0001",
        "metadata": {"recipient_view_url": "https://www.sandbox.paypal.com/invoice/p/#X"},
    },
    "amount": {"currency_code": "USD", "value": "450.00"},
    "due_amount": {"currency_code": "USD", "value": "450.00"},
}


def draft(**kw) -> InvoiceDraft:
    base = dict(
        client_name="Acme Corp",
        client_email="ap@acme.example",
        currency="USD",
        items=[LineItem(name="Logo design", quantity=1, unit_amount=Decimal("450.00"))],
        invoice_date=dt.date(2026, 10, 14),
        due_date=dt.date(2026, 10, 28),
    )
    base.update(kw)
    return InvoiceDraft(**base)


def test_token_is_cached_across_calls(paypal_mock, pp_client):
    paypal_mock.get("/v2/invoicing/invoices/X").respond(200, json={**INVOICE, "id": "X"})
    pp_client.request("GET", "/v2/invoicing/invoices/X")
    pp_client.request("GET", "/v2/invoicing/invoices/X")
    assert paypal_mock.routes[0].call_count == 1  # one OAuth call for two API calls


def test_token_refreshes_once_on_401(paypal_mock, pp_client):
    route = paypal_mock.get("/v2/invoicing/invoices/X")
    route.side_effect = [httpx.Response(401), httpx.Response(200, json={**INVOICE, "id": "X"})]
    resp = pp_client.request("GET", "/v2/invoicing/invoices/X")
    assert resp.status == 200
    assert paypal_mock.routes[0].call_count == 2


def test_bad_credentials_map_to_auth_error(pp_client):
    import respx

    with respx.mock(base_url="https://api-m.sandbox.paypal.com") as router:
        router.post("/v1/oauth2/token").respond(401, json={"error": "invalid_client"})
        with pytest.raises(PayPalAuthError):
            pp_client.request("GET", "/v2/invoicing/invoices/X")


def test_validation_error_carries_details_and_debug_id(paypal_mock, pp_client):
    paypal_mock.post("/v2/invoicing/invoices").respond(
        422,
        json={
            "name": "UNPROCESSABLE_ENTITY",
            "message": "The requested action could not be performed.",
            "debug_id": "dbg123",
            "details": [{"field": "/detail/currency_code", "issue": "INVALID_CURRENCY_CODE"}],
        },
    )
    with pytest.raises(PayPalValidationError) as ei:
        pp_client.request("POST", "/v2/invoicing/invoices", json={}, request_id="r1")
    assert ei.value.debug_id == "dbg123"
    assert "INVALID_CURRENCY_CODE" in str(ei.value)


def test_5xx_is_retried_with_same_request_id(paypal_mock, pp_client):
    route = paypal_mock.post("/v2/invoicing/invoices")
    route.side_effect = [httpx.Response(503), httpx.Response(201, json=INVOICE)]
    resp = pp_client.request("POST", "/v2/invoicing/invoices", json={}, request_id="same-id")
    assert resp.status == 201
    ids = {c.request.headers["PayPal-Request-Id"] for c in route.calls}
    assert ids == {"same-id"}


def test_post_without_request_id_is_never_retried(paypal_mock, pp_client):
    route = paypal_mock.post("/v1/notifications/verify-webhook-signature").respond(503)
    with pytest.raises(PayPalServerError):
        pp_client.request("POST", "/v1/notifications/verify-webhook-signature", json={})
    assert route.call_count == 1


def test_network_failure_maps_to_unavailable(paypal_mock, pp_client):
    paypal_mock.get("/v2/invoicing/invoices/X").mock(side_effect=httpx.ConnectTimeout("boom"))
    with pytest.raises(PayPalUnavailable):
        pp_client.request("GET", "/v2/invoicing/invoices/X")


def test_create_payload_uses_strings_for_money():
    body = build_create_payload(draft(), invoicer_email="m@example.com", business_name="Studio")
    item = body["items"][0]
    assert item["quantity"] == "1"
    assert item["unit_amount"] == {"currency_code": "USD", "value": "450.00"}
    assert body["detail"]["payment_term"]["due_date"] == "2026-10-28"
    assert body["primary_recipients"][0]["billing_info"]["email_address"] == "ap@acme.example"


def test_create_and_send_invoice_flow(paypal_mock, pp_client):
    create = paypal_mock.post("/v2/invoicing/invoices").respond(201, json=INVOICE)
    send = paypal_mock.post(f"/v2/invoicing/invoices/{INVOICE['id']}/send").respond(
        200, json={"href": "x"}
    )
    paypal_mock.get(f"/v2/invoicing/invoices/{INVOICE['id']}").respond(
        200, json={**INVOICE, "status": "UNPAID"}
    )
    api = InvoicingAPI(pp_client, invoicer_email="m@example.com", business_name="Studio")

    created = api.create_draft(draft(), request_id="act-1-create")
    assert created.paypal_id == INVOICE["id"] and created.status == "DRAFT"
    assert create.calls[0].request.headers["PayPal-Request-Id"] == "act-1-create"
    assert json.loads(create.calls[0].request.content)["detail"]["currency_code"] == "USD"

    sent = api.send(created.paypal_id, request_id="act-1-send")
    assert sent.status == "UNPAID" and sent.total == Decimal("450.00")
    assert json.loads(send.calls[0].request.content)["send_to_recipient"] is True


def test_create_handles_minimal_response_with_href(paypal_mock, pp_client):
    paypal_mock.post("/v2/invoicing/invoices").respond(
        201,
        json={
            "rel": "self",
            "href": "https://api-m.sandbox.paypal.com/v2/invoicing/invoices/INV2-ZZ",
            "method": "GET",
        },
    )
    paypal_mock.get("/v2/invoicing/invoices/INV2-ZZ").respond(
        200, json={**INVOICE, "id": "INV2-ZZ"}
    )
    api = InvoicingAPI(pp_client, invoicer_email="", business_name="Studio")
    assert api.create_draft(draft(), request_id="r").paypal_id == "INV2-ZZ"


def test_client_refuses_live_url():
    from app.paypal.client import PayPalClient

    with pytest.raises(ValueError):
        PayPalClient("a", "b", base_url="https://api-m.paypal.com")
