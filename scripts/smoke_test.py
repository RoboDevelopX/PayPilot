"""Live end-to-end check against the PayPal sandbox (and, optionally, the AI model).

    python -m scripts.smoke_test            # or: make smoke
    python -m scripts.smoke_test --llm      # also run one real chat turn
    python -m scripts.smoke_test --record   # also save responses as demo recordings

Needs PAYPAL_CLIENT_ID, PAYPAL_CLIENT_SECRET and PAYPAL_PAYER_EMAIL (the sandbox
Personal account). It moves $1.00 of sandbox money: one invoice to the payer
and one payout to the payer. Each step prints PASS or FAIL; the exit code is
the number of failures.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import uuid
from decimal import Decimal
from typing import Any

from app.config import get_settings
from app.paypal import client_from_settings
from app.paypal.errors import PayPalError
from app.paypal.invoices import InvoiceDraft, InvoicingAPI, LineItem
from app.paypal.payouts import PayoutsAPI
from app.paypal.recorded import FIXTURES
from app.services.actions import request_id

EMAIL = re.compile(r"[\w.+-]+@[\w-]+(\.[\w-]+)+")
failures = 0


def step(name: str, fn):
    global failures
    try:
        detail = fn()
    except PayPalError as e:
        failures += 1
        print(f"FAIL  {name}: [{type(e).__name__}] {e.user_message()}")
        return None
    except Exception as e:  # noqa: BLE001 - report anything, keep going
        failures += 1
        print(f"FAIL  {name}: {type(e).__name__}: {e}")
        return None
    print(f"PASS  {name}" + (f": {detail}" if isinstance(detail, str) else ""))
    return detail


def redact(data: Any) -> Any:
    """Recordings are committed, so emails are replaced with example addresses."""
    return json.loads(EMAIL.sub("someone@example.com", json.dumps(data)))


def main() -> int:
    global failures
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--llm", action="store_true", help="also run one real chat turn")
    parser.add_argument("--record", action="store_true", help="save responses as recordings")
    args = parser.parse_args()

    settings = get_settings()
    if not (settings.paypal_configured and settings.paypal_payer_email):
        print("Set PAYPAL_CLIENT_ID, PAYPAL_CLIENT_SECRET and PAYPAL_PAYER_EMAIL first.")
        return 2
    pp = client_from_settings(settings)
    invoicing = InvoicingAPI(
        pp, invoicer_email=settings.paypal_invoicer_email, business_name=settings.business_name
    )
    payouts = PayoutsAPI(pp)
    run = str(uuid.uuid4())
    recordings: dict[str, Any] = {}

    step("OAuth token", lambda: f"token cached ({len(pp.access_token())} chars)")

    today = dt.datetime.now(dt.UTC).date()
    draft = InvoiceDraft(
        client_name="PayPilot smoke test",
        client_email=settings.paypal_payer_email,
        currency="USD",
        items=[LineItem(name="Smoke test", quantity=1, unit_amount=Decimal("1.00"))],
        invoice_date=today,
        due_date=today + dt.timedelta(days=7),
        note="Automated PayPilot smoke test (sandbox).",
    )
    created = step(
        "create invoice",
        lambda: invoicing.create_draft(draft, request_id=request_id(run, "invoice.create")),
    )
    if created:
        sent = step(
            "send invoice",
            lambda: invoicing.send(created.paypal_id, request_id=request_id(run, "invoice.send")),
        )
        if sent:
            print(f"      invoice {sent.paypal_id} #{sent.number} status {sent.status}")
            if args.record:
                recordings["invoice.json"] = pp.request(
                    "GET", f"/v2/invoicing/invoices/{sent.paypal_id}"
                ).data
        step(
            "remind invoice",
            lambda: invoicing.remind(
                created.paypal_id,
                subject="Smoke test reminder",
                note="Automated PayPilot smoke test (sandbox).",
                request_id=request_id(run, "invoice.remind"),
            )
            and "reminder accepted",
        )

    batch = step(
        "create payout",
        lambda: payouts.create(
            receiver_email=settings.paypal_payer_email,
            amount=Decimal("1.00"),
            currency="USD",
            note="PayPilot smoke test",
            sender_batch_id=run,
            request_id=request_id(run, "payout.create"),
        ),
    )
    if batch:
        print(f"      batch {batch.batch_id} status {batch.batch_status}")
        again = step(
            "payout retry is idempotent",
            lambda: payouts.create(
                receiver_email=settings.paypal_payer_email,
                amount=Decimal("1.00"),
                currency="USD",
                note="PayPilot smoke test",
                sender_batch_id=run,
                request_id=request_id(run, "payout.create"),
            ),
        )
        if again and again.batch_id != batch.batch_id:
            failures += 1
            print(f"FAIL  idempotency: got a second batch {again.batch_id}")
        status = step("get payout", lambda: payouts.get(batch.batch_id))
        if status:
            print(f"      batch status {status.batch_status}, item {status.item_status}")
        if args.record:
            recordings["payout_get.json"] = pp.request(
                "GET", f"/v1/payments/payouts/{batch.batch_id}"
            ).data

    if settings.paypal_webhook_id:
        from app.paypal.webhooks import verify_signature

        headers = {
            "paypal-auth-algo": "SHA256withRSA",
            "paypal-cert-url": "https://api.sandbox.paypal.com/v1/notifications/certs/CERT-test",
            "paypal-transmission-id": run,
            "paypal-transmission-sig": "not-a-real-signature",
            "paypal-transmission-time": dt.datetime.now(dt.UTC).isoformat(),
        }

        def forged_rejected() -> str:
            if verify_signature(pp, headers, {"id": run}, settings.paypal_webhook_id):
                raise RuntimeError("a forged signature was accepted")
            return "rejected as expected"

        step("webhook check rejects a forged signature", forged_rejected)

    if args.llm:
        from app.container import make_llm
        from app.services.actions import Outcome

        def chat_turn() -> str:
            from app.agent.llm import Agent

            llm = make_llm(settings)
            if llm is None:
                raise RuntimeError("no AI key set (GEMINI_API_KEY or ANTHROPIC_API_KEY)")
            agent = Agent(
                llm,
                handle_action=lambda a: Outcome("info", "smoke test: nothing executed"),
                model=settings.llm_model,
            )
            reply = agent.respond([], "Invoice Acme Corp (ap@acme.example) $450 for a logo")
            tools = [o["tool"] for o in reply.outcomes]
            return f"{settings.assistant_label} proposed {tools or 'no tool'}: {reply.text[:80]!r}"

        step("AI chat turn (nothing executed)", chat_turn)

    if args.record and recordings:
        for name, data in recordings.items():
            (FIXTURES / name).write_text(json.dumps(redact(data), indent=2) + "\n")
        print(f"saved {', '.join(recordings)} to {FIXTURES} (emails redacted)")

    print("all good" if failures == 0 else f"{failures} step(s) failed")
    return failures


if __name__ == "__main__":
    raise SystemExit(main())
