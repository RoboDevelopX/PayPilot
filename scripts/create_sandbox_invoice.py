"""M1 proof: create and send one real PayPal sandbox invoice from the command line.

    python -m scripts.create_sandbox_invoice                # uses PAYPAL_PAYER_EMAIL
    python -m scripts.create_sandbox_invoice --to someone@example.com --amount 450
    python -m scripts.create_sandbox_invoice --dry-run      # print the payload only

Then log in to https://www.sandbox.paypal.com as the sandbox Personal account
to see (and pay) the invoice.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
import uuid

from app.config import get_settings
from app.money import parse_amount_string
from app.paypal import client_from_settings
from app.paypal.errors import PayPalError
from app.paypal.invoices import InvoiceDraft, InvoicingAPI, LineItem, build_create_payload
from app.services.actions import request_id


def main(argv: list[str] | None = None) -> int:
    settings = get_settings()
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    p.add_argument("--to", default=settings.paypal_payer_email, help="recipient email")
    p.add_argument("--name", default="Acme Corp")
    p.add_argument("--amount", default="450.00")
    p.add_argument("--item", default="Logo design")
    p.add_argument("--days", type=int, default=14)
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args(argv)

    if not args.to:
        print(
            "Set PAYPAL_PAYER_EMAIL (your sandbox Personal account) or pass --to.", file=sys.stderr
        )
        return 2

    today = dt.date.today()
    draft = InvoiceDraft(
        client_name=args.name,
        client_email=args.to,
        currency="USD",
        items=[LineItem(name=args.item, quantity=1, unit_amount=parse_amount_string(args.amount))],
        invoice_date=today,
        due_date=today + dt.timedelta(days=args.days),
        note="Sandbox test invoice from PayPilot M1.",
    )
    if args.dry_run:
        payload = build_create_payload(
            draft,
            invoicer_email=settings.paypal_invoicer_email,
            business_name=settings.business_name,
        )
        print(json.dumps(payload, indent=2))
        return 0

    if not settings.paypal_configured:
        print("PAYPAL_CLIENT_ID / PAYPAL_CLIENT_SECRET are not set.", file=sys.stderr)
        return 2

    api = InvoicingAPI(
        client_from_settings(settings),
        invoicer_email=settings.paypal_invoicer_email,
        business_name=settings.business_name,
    )
    action_id = str(uuid.uuid4())
    try:
        created = api.create_draft(draft, request_id=request_id(action_id, "invoice.create"))
        print(f"created draft {created.paypal_id} (status {created.status})")
        sent = api.send(created.paypal_id, request_id=request_id(action_id, "invoice.send"))
    except PayPalError as e:
        print(f"PayPal error [{type(e).__name__}]: {e.user_message()}", file=sys.stderr)
        return 1
    print(f"sent invoice {sent.paypal_id} #{sent.number} status={sent.status} total={sent.total}")
    if sent.pay_url:
        print(f"payer link: {sent.pay_url}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
