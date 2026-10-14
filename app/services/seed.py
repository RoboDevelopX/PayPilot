"""Demo data: a small business with a few months of history (DEMO_MODE only).

Every row is marked `is_seeded` and shown as sample data. Names use reserved
example domains (.example, example.com), so no real person or business is
represented. Seeding runs once per empty database; on Render's free plan the
database resets on each deploy, and the app reseeds on boot.

The story the data tells, for the demo:
- Acme Corp pays late every time and has an invoice 10 days overdue.
- Northwind Traders is a little late on one invoice; Blue Fern Studio always
  pays early; Lumen Labs is a new client; Orbit Cafe paid this month.
- Sam, Maya and Devon are regular, allowlisted payees with steady amounts, so a
  payout far outside their usual range stands out. Jo is not allowlisted yet.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

from sqlalchemy import select

from app.db import session_scope
from app.models import Client, Invoice, Payout, Recipient
from app.paypal.recorded import INVOICE_PREFIX
from app.services import audit
from app.services.actions import ActionService
from app.services.ml import MLScorers

D = Decimal


def _at(now: dt.datetime, days_ago: float, hour: int = 10) -> dt.datetime:
    day = (now - dt.timedelta(days=days_ago)).replace(hour=hour, minute=0, second=0, microsecond=0)
    return min(day, now - dt.timedelta(minutes=5))


def already_seeded(actions: ActionService) -> bool:
    with actions.sessions() as s:
        return (
            s.scalars(select(Invoice.id).where(Invoice.is_seeded.is_(True)).limit(1)).first()
            is not None
        )


def seed_demo(actions: ActionService, ml: MLScorers | None = None) -> bool:
    """Load the demo data into an empty database. Returns False if it was already there."""
    if already_seeded(actions):
        return False
    now = actions.clock()
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    number = iter(range(1001, 2000))
    with session_scope(actions.sessions) as s:
        clients = {
            key: Client(name=name, email=email)
            for key, name, email in [
                ("acme", "Acme Corp", "ap@acme.example"),
                ("fern", "Blue Fern Studio", "billing@bluefern.example"),
                ("north", "Northwind Traders", "accounts@northwind.example"),
                ("lumen", "Lumen Labs", "finance@lumenlabs.example"),
                ("orbit", "Orbit Cafe", "hello@orbitcafe.example"),
            ]
        }
        s.add_all(clients.values())
        s.flush()

        def paid(key: str, amount: str, terms: int, issued_days_ago: float, paid_offset: int):
            """A past invoice, paid `paid_offset` days after its due date (negative = early)."""
            issued = _at(now, issued_days_ago)
            due = (issued + dt.timedelta(days=terms)).date()
            paid_at = dt.datetime.combine(
                due + dt.timedelta(days=paid_offset), dt.time(15), tzinfo=dt.UTC
            )
            s.add(
                Invoice(
                    client_id=clients[key].id,
                    number=f"S-{next(number)}",
                    amount=D(amount),
                    terms_days=terms,
                    description="Design retainer (sample data)",
                    issued_at=issued,
                    due_date=due,
                    status="PAID",
                    paid_at=min(paid_at, now - dt.timedelta(hours=1)),
                    is_seeded=True,
                )
            )

        for i in range(9):  # Acme: always 9-21 days late
            paid("acme", "1200.00", 30, 330 - 33 * i, 9 + (i * 5) % 13)
        for i in range(8):  # Blue Fern: early
            paid("fern", "450.00", 14, 300 - 35 * i, -3 - i % 4)
        for i in range(6):  # Northwind: sometimes a bit late
            paid("north", "800.00", 45, 330 - 45 * i, (4 if i % 3 == 0 else -2))
        for i in range(4):  # Orbit Cafe: on time
            paid("orbit", "300.00", 14, 200 - 45 * i, -1)
        s.flush()
        # Orbit Cafe paid one this month.
        orbit_paid = month_start + (now - month_start) / 2
        s.add(
            Invoice(
                client_id=clients["orbit"].id,
                number=f"S-{next(number)}",
                amount=D("300.00"),
                terms_days=14,
                description="Menu photography (sample data)",
                issued_at=orbit_paid - dt.timedelta(days=10),
                due_date=(orbit_paid + dt.timedelta(days=4)).date(),
                status="PAID",
                paid_at=orbit_paid,
                is_seeded=True,
            )
        )

        open_invoices = []
        for key, amount, terms, issued_days_ago, desc in [
            ("acme", "1200.00", 30, 40, "Website retainer, September"),
            ("north", "800.00", 45, 48, "Catalog layout"),
            ("fern", "450.00", 14, 5, "Logo refresh"),
            ("lumen", "2400.00", 30, 12, "Product launch campaign"),
        ]:
            issued = _at(now, issued_days_ago)
            n = next(number)
            inv = Invoice(
                client_id=clients[key].id,
                number=f"S-{n}",
                paypal_invoice_id=f"{INVOICE_PREFIX}SEED-{n}",
                amount=D(amount),
                terms_days=terms,
                description=f"{desc} (sample data)",
                issued_at=issued,
                due_date=(issued + dt.timedelta(days=terms)).date(),
                status="SENT",
                is_seeded=True,
            )
            s.add(inv)
            open_invoices.append(inv)
        s.flush()
        if ml is not None:
            for inv in open_invoices:
                inv.risk_score, inv.risk_reasons = ml.score_invoice(s, inv)

        # Payees with steady histories, all before this month (so caps start at zero).
        recipients = {}
        for key, name, email, allow in [
            ("sam", "Sam Lee", "sam@example.com", True),
            ("maya", "Maya Chen", "maya@example.com", True),
            ("devon", "Devon Ray", "devon@example.com", True),
            ("jo", "Jo Park", "jo@example.com", False),
        ]:
            recipients[key] = Recipient(
                email=email, name=name, allowlisted_at=_at(now, 120) if allow else None
            )
        s.add_all(recipients.values())
        s.flush()
        history_end = month_start - dt.timedelta(hours=1)
        for key, amounts, every_days in [
            ("sam", ["35.00", "40.00", "38.00", "42.00", "36.00"], 4),
            ("maya", ["18.00", "22.00", "20.00", "19.50"], 6),
            ("devon", ["150.00", "140.00", "160.00"], 9),
        ]:
            for i in range(12):
                created = history_end - dt.timedelta(days=every_days * i, hours=3 * (i % 3))
                s.add(
                    Payout(
                        action_id=f"seed-{key}-{i}",
                        recipient_id=recipients[key].id,
                        amount=D(amounts[i % len(amounts)]),
                        currency="USD",
                        note="sample data",
                        status="executed",
                        paypal_status="SUCCESS",
                        created_at=created,
                        is_seeded=True,
                    )
                )
        audit.record(s, actor="system", event="demo_data_seeded", note="sample data, not real")
    return True
