from app.paypal import invoice_payload


def test_invoice_payload_matches_invoicing_v2_shape():
    body = invoice_payload({
        "recipient_email": "dana@example.com", "recipient_name": "Dana Lee", "currency": "USD",
        "items": [{"description": "Consulting", "quantity": 3, "unit_amount": "120.00"}],
        "note": "Thanks!", "due_in_days": 30,
    }, invoicer_email="biz@example.com", business_name="Studio")
    assert body["detail"]["currency_code"] == "USD"
    assert "due_date" in body["detail"]["payment_term"]
    assert body["invoicer"] == {"business_name": "Studio", "email_address": "biz@example.com"}
    billing = body["primary_recipients"][0]["billing_info"]
    assert billing["email_address"] == "dana@example.com"
    assert billing["name"] == {"given_name": "Dana", "surname": "Lee"}
    item = body["items"][0]
    assert item["quantity"] == "3" and item["unit_amount"] == {"currency_code": "USD", "value": "120.00"}
