"""The ML layer: reproducible synthetic data, leakage-free features, honest
metrics, and advisory-only integration with the app."""

from __future__ import annotations

import datetime as dt
import io
import json
from decimal import Decimal

import numpy as np
import pytest

from app.db import init_db, make_engine, make_session_factory
from app.models import Client, Invoice, Payout, Recipient
from app.paypal.client import PayPalClient
from app.paypal.invoices import InvoicingAPI
from app.paypal.payouts import PayoutsAPI
from app.policy.engine import MLSignals
from app.schemas import parse_action
from app.services.actions import ActionService
from app.services.ml import MLScorers
from ml import evaluate, train_anomaly, train_risk
from ml.features import (
    DAY,
    PriorInvoice,
    anomaly_frame,
    invoice_features,
    payout_features,
    prior_late_count,
    risk_frame,
)
from ml.generate_data import INVOICES_CSV, PAYOUTS_CSV, generate_invoices, generate_payouts
from ml.predict import ANOMALY_ARTIFACT, RISK_ARTIFACT, AnomalyModel, RiskModel

NOW = dt.datetime(2026, 10, 14, 12, 0, tzinfo=dt.UTC)


def days_ago(n: float) -> dt.datetime:
    return NOW - dt.timedelta(days=n)


@pytest.fixture(scope="module")
def scorers() -> MLScorers:
    return MLScorers.load()


# ------------------------------------------------------------------- data
def test_generator_is_seeded_and_deterministic():
    a, b = generate_invoices(300, 10), generate_invoices(300, 10)
    assert a.equals(b)
    p, q = generate_payouts(500, 20), generate_payouts(500, 20)
    assert p.equals(q)
    assert set(p["anomaly_type"]) == {"", "spike", "new_large", "burst"}
    assert 0.01 < p["anomaly"].mean() < 0.03


def test_committed_csvs_are_exactly_what_the_generator_produces():
    for frame, path in ((generate_invoices(), INVOICES_CSV), (generate_payouts(), PAYOUTS_CSV)):
        buf = io.StringIO()
        frame.to_csv(buf, index=False)
        assert buf.getvalue() == path.read_text(), f"{path.name} is stale: run make train"


# --------------------------------------------------------------- features
def test_invoice_features_only_use_what_was_known_at_issue_time():
    t = days_ago(0)
    history = [
        # paid 5 days late, before t: counts as a late payment
        PriorInvoice(days_ago(60), (days_ago(40)).date(), days_ago(35)),
        # paid on time, before t
        PriorInvoice(days_ago(50), (days_ago(30)).date(), days_ago(31)),
        # due 10 days ago, paid only after t: at t it was simply open and overdue
        PriorInvoice(days_ago(30), (days_ago(10)).date(), NOW + dt.timedelta(days=3)),
        # issued after t: does not exist yet
        PriorInvoice(NOW + dt.timedelta(hours=1), NOW.date(), NOW + dt.timedelta(days=1)),
    ]
    f = invoice_features(500.0, 30, t, history)
    assert f["prior_count"] == 2
    assert prior_late_count(f) == 1
    assert f["prior_mean_days_late"] == 2.5  # (5 + 0) / 2
    assert f["prior_open_overdue"] == 1


def test_changing_a_later_outcome_never_changes_earlier_features():
    invoices = generate_invoices(400, 8)
    before = risk_frame(invoices)
    last = invoices.index[-1]
    invoices.loc[last, "paid_at"] = "2030-01-01T00:00:00+00:00"
    invoices.loc[last, "late"] = 1
    after = risk_frame(invoices)
    cols = [c for c in before.columns if c != "late"]
    assert before[cols].equals(after[cols])


def test_payout_features_ignore_later_payouts():
    times = np.array([0.0, DAY, 2 * DAY, 10 * DAY])
    amounts = np.array([40.0, 40.0, 40.0, 4000.0])
    f = payout_features(400.0, 3 * DAY, times, amounts, times, amounts)
    assert f["recipient_ratio"] == pytest.approx(np.log(10))  # vs 40, not the later 4000
    assert f["recipient_24h"] == 1 and f["is_new_recipient"] == 0


def test_training_frames_are_time_ordered():
    frame = anomaly_frame(generate_payouts(300, 15))
    assert frame["at"].is_monotonic_increasing
    assert risk_frame(generate_invoices(200, 5))["issued_at"].is_monotonic_increasing


# ------------------------------------------------------- committed models
def test_artifacts_are_small_and_labeled_synthetic():
    for path in (RISK_ARTIFACT, ANOMALY_ARTIFACT):
        assert path.stat().st_size < 1_000_000
    assert RiskModel.load().artifact["synthetic"] and AnomalyModel.load().artifact["synthetic"]


def test_risk_model_separates_late_payers_from_reliable_ones():
    model = RiskModel.load()

    def client(late: int, n: int = 8, days_late: float = 6.0) -> dict[str, float]:
        history = [
            PriorInvoice(
                days_ago(100 + 10 * i),
                days_ago(70 + 10 * i).date(),
                days_ago(70 + 10 * i - (days_late if i < late else -2)),
            )
            for i in range(n)
        ]
        return invoice_features(800.0, 30, NOW, history)

    p_late, reasons_late = model.predict(client(late=8))
    p_good, reasons_good = model.predict(client(late=0))
    assert 0 <= p_good < p_late <= 1
    assert p_late > 0.6 and p_good < 0.3
    assert any("late" in r for r in reasons_late)
    assert any("on time" in r for r in reasons_good)


def test_anomaly_model_flags_an_obvious_spike_but_not_a_routine_payout():
    model = AnomalyModel.load()
    times = np.arange(30) * DAY  # one $40ish payout a day for a month
    amounts = 40.0 + (np.arange(30) % 5)
    at = 30.5 * DAY

    def check(amount: float):
        f = payout_features(amount, at, times, amounts, times, amounts)
        return model.predict(f, amount)

    flagged, score, reason = check(42.0)
    assert not flagged and reason is None
    flagged, spike_score, reason = check(900.0)
    assert flagged and spike_score > score
    assert "what you usually pay this recipient" in reason


def test_committed_metrics_are_honest_and_beat_baselines():
    m = json.loads(evaluate.METRICS_JSON.read_text())
    assert m["synthetic_data"] is True
    risk, anomaly = m["late_payment_risk"], m["payout_anomaly"]
    assert risk["test"]["brier"] < risk["baselines"]["base_rate"]["brier"]
    assert risk["test"]["roc_auc"] > 0.65
    assert anomaly["test"]["pr_auc"] > 5 * anomaly["baselines"]["random"]["pr_auc"]
    # every candidate is reported, not only the winner
    assert len(risk["all_candidates"]) == 4
    assert "Reading it honestly" in evaluate.REPORT_MD.read_text()


def test_training_pipeline_runs_end_to_end_on_a_small_sample():
    risk = train_risk.train(risk_frame(generate_invoices(800, 15)))
    p = risk["model"].predict_proba(train_risk.matrix(risk_frame(generate_invoices(50, 3))))
    assert ((p >= 0) & (p <= 1)).all()
    anomaly = train_anomaly.train(anomaly_frame(generate_payouts(800, 30)))
    assert 0 < anomaly["threshold"] < 1


# ------------------------------------------------------------ app wiring
@pytest.fixture
def app_env(sandbox, scorers):
    engine = make_engine("sqlite://")
    init_db(engine)
    sessions = make_session_factory(engine)
    pp = PayPalClient("id", "secret", transport=sandbox.transport, sleep=lambda _s: None)
    actions = ActionService(
        sessions,
        invoicing=InvoicingAPI(pp, invoicer_email="", business_name="Studio"),
        payouts=PayoutsAPI(pp),
        risk_scorer=scorers.score_invoice,
        anomaly_scorer=scorers.score_payout,
        clock=lambda: NOW,
    )
    return actions, sandbox


def seed_payouts(actions, email: str, amount: str, n: int = 25) -> None:
    with actions.sessions() as s:
        r = Recipient(email=email, name="Sam", allowlisted_at=days_ago(90))
        s.add(r)
        s.flush()
        for i in range(n):
            s.add(
                Payout(
                    action_id=f"seed-{email}-{i}",
                    recipient_id=r.id,
                    amount=Decimal(amount),
                    currency="USD",
                    status="executed",
                    created_at=days_ago(60 - 2 * i),
                    is_seeded=True,
                )
            )
        s.commit()


def payout(amount: str, email: str = "sam@example.com"):
    return parse_action(
        "send_payout",
        {
            "recipient_email": email,
            "recipient_name": "Sam",
            "amount": amount,
            "currency": "USD",
            "note": "",
        },
    )


def test_ml_escalates_a_small_but_unusual_payout(app_env):
    actions, sandbox = app_env
    seed_payouts(actions, "sam@example.com", "2.50")
    routine = actions.handle(payout("2.75"))
    assert routine.status == "executed"
    # $45 is under the $50 auto-approve limit, but 18x what Sam usually gets.
    out = actions.handle(payout("45.00"))
    assert out.status == "escalated"
    assert any("usually pay this recipient" in r for r in out.reasons)
    assert len(sandbox.payouts) == 1


def test_ml_never_lowers_a_policy_decision(app_env):
    actions, _ = app_env
    seed_payouts(actions, "sam@example.com", "40.00")
    out = actions.handle(payout("80.00"))  # above L: needs approval whatever the model says
    assert out.status in ("needs_approval", "escalated")
    for _ in range(5):
        actions.handle(payout("40.00"))
    assert actions.handle(payout("40.00")).status == "blocked"  # caps still hard-block


def seed_client(actions, *, late: bool, terms: int = 30, n: int = 10) -> str:
    with actions.sessions() as s:
        c = Client(name="Acme Corp", email="ap@acme.example")
        s.add(c)
        s.flush()
        for i in range(n):
            issued = days_ago(400 - 35 * i)
            due = (issued + dt.timedelta(days=terms)).date()
            paid = dt.datetime.combine(due, dt.time(12), tzinfo=dt.UTC) + dt.timedelta(
                days=12 if late else -3
            )
            s.add(
                Invoice(
                    client_id=c.id,
                    amount=Decimal("900.00"),
                    terms_days=terms,
                    issued_at=issued,
                    due_date=due,
                    status="PAID",
                    paid_at=paid,
                    is_seeded=True,
                )
            )
        s.commit()
        return c.id


INVOICE = {
    "client_name": "Acme Corp",
    "client_email": "ap@acme.example",
    "items": [{"name": "Website retainer", "quantity": 1, "unit_amount": "900"}],
    "currency": "USD",
    "due_in_days": 30,
    "note": "",
}


def test_new_invoice_gets_a_risk_score_with_reasons(app_env):
    actions, _ = app_env
    seed_client(actions, late=True)
    assert actions.handle(parse_action("create_invoice", INVOICE)).status == "executed"
    with actions.sessions() as s:
        inv = s.query(Invoice).filter_by(is_seeded=False).one()
        assert inv.risk_score > 0.6
        assert any("late" in r for r in inv.risk_reasons)


def test_terms_advice_for_a_late_paying_client(app_env, scorers):
    actions, _ = app_env
    late_id = seed_client(actions, late=True)
    with actions.sessions() as s:
        advice = scorers.terms_advice(s, late_id, NOW)
        assert advice["risk"] >= 0.5
        assert advice["suggested_terms"] == 7
        assert advice["risk_with_suggestion"] < advice["risk"]
        assert "consider net-7" in advice["text"]
        assert scorers.insights(s, NOW)[0]["client_id"] == late_id


def test_reliable_client_gets_no_advice(app_env, scorers):
    actions, _ = app_env
    good_id = seed_client(actions, late=False)
    with actions.sessions() as s:
        assert scorers.terms_advice(s, good_id, NOW) is None


def test_missing_models_turn_ml_off(tmp_path):
    off = MLScorers.load(tmp_path / "nope.joblib", tmp_path / "nope.joblib")
    assert off.status == "off"
    assert off.score_payout(None, {}) == MLSignals()


def test_a_scoring_error_never_blocks_an_action(app_env, scorers):
    actions, sandbox = app_env

    class Broken:
        def predict(self, *_a, **_k):
            raise RuntimeError("boom")

    broken = MLScorers(risk=scorers.risk, anomaly=Broken())
    actions.anomaly_scorer = broken.score_payout
    seed_payouts(actions, "sam@example.com", "40.00")
    assert actions.handle(payout("40.00")).status == "executed"


def test_health_reports_ml_status(make_app):
    client, _ = make_app(ml_enabled=True)
    assert client.get("/healthz").json()["ml"] == "risk+anomaly"
    client, _ = make_app()
    assert client.get("/healthz").json()["ml"] == "off"
