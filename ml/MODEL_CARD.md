# Model card: PayPilot's two advisory models

> **Synthetic data only.** Both models are trained and evaluated on data invented by
> `ml/generate_data.py` (seed 42). The numbers below show that the pipeline works and
> is evaluated honestly. They say nothing about how the models would do on real
> customers, and they should not be quoted as if they did.

## What the models are for, and what they cannot do

| | Late-payment risk | Payout anomaly detector |
|---|---|---|
| Question | Will this invoice be paid after its due date? | Is this payout unusual for this user? |
| Model | Logistic regression (scaled), sigmoid-calibrated | Isolation Forest, 200 trees |
| Output | Probability plus up to two plain-English reasons | Flag, score in (0, 1), and the most unusual feature as a reason |
| Used for | When the first reminder goes out (day 1 instead of day 3 at 70%+), one step firmer tone, and "consider net-7 terms" advice on the dashboard | Escalating a payout to a red "careful review" card that needs an explicit "I've checked this" tick |
| Cannot | Send, skip or block anything; whether a reminder may go out alone is the policy's call | Lower an approval level, block a payout, or approve one; spending caps and the allowlist are enforced by the policy engine either way |

If the model files are missing or fail to load, or scoring raises an error, the app
logs it and the policy engine decides exactly as it would with `ML_ENABLED=0`.

## Data

`ml/data/synthetic/` (committed, regenerated bit-for-bit by `make train`):

- **invoices.csv**: 5,000 invoices from 60 clients over two years (Oct 2024 to Sep 2026),
  34% paid late. Each client has a hidden tendency to pay late that the model never sees;
  lateness also rises with longer terms, larger amounts, month-end and Friday invoices.
- **payouts.csv**: 6,122 payouts to about 120 recipients who appear over time, with
  2% injected, labeled anomalies: `spike` (5 to 20x the recipient's usual amount),
  `new_large` (a new recipient receiving 4 to 12x the user's usual payout) and `burst`
  (three extra payouts to one recipient within 40 minutes). Labels are used only for
  evaluation; the detector is fitted without them.

Not modeled at all: seasonality, partial payments, disputes and refunds, multiple
currencies, clients changing behavior over time, and real fraud, which adapts to
whatever rules are in place.

## Features (shared by training and the app: `ml/features.py`)

Late-payment risk, using only what was known on the invoice's issue date:
log amount, terms in days, the client's number of past paid invoices, their past late
rate (shrunk toward 30% as if they had two extra invoices, so one late payment does not
read as "always late"), their mean days late, how many of their invoices were unpaid and
past due on that date, the weekday, and a month-end flag. A past invoice paid *after*
the issue date counts as "open", never as "paid late", so outcomes from the future
cannot leak in. A test checks this.

Payout anomaly, using only earlier payouts: log amount, log of amount over the
recipient's median, log of amount over the user's median, a new-recipient flag, payouts
to this recipient in the last 24 hours, and payouts to anyone in the last hour.

## Training and evaluation

Every split is chronological, so each model is judged on data from after everything it
learned from: the first 70% fits the model, the next 10% is a holdout, and the most
recent 20% is the test window, used only by `ml/evaluate.py`.

- **Risk:** both candidates (logistic regression and a small histogram gradient-boosting
  model) are fitted, the one with the better holdout ROC-AUC is kept, and it is calibrated
  on the holdout. Logistic regression won.
- **Anomaly:** the threshold is a review budget, not a label-tuned number: the score that
  flags the top 3% of the holdout window.

Headline test results at seed 42 (full tables, calibration bins and bootstrap intervals in
[`reports/REPORT.md`](reports/REPORT.md), raw numbers in `reports/metrics.json`):

| Risk (1,001 invoices) | ROC-AUC | Brier |
|---|---|---|
| Model | 0.756 (95% CI 0.725 to 0.785) | 0.187 |
| Client's past late rate alone | 0.748 | 0.189 |
| Base rate for everyone | 0.5 | 0.229 |

| Anomaly (1,225 payouts, 27 anomalies) | Precision | Recall | PR-AUC |
|---|---|---|---|
| Isolation Forest | 0.63 | 0.81 | 0.84 (95% CI 0.71 to 0.95) |
| Random | | | 0.02 |

What these say, plainly:

- The risk model is clearly better than guessing the base rate, but most of its signal is
  the client's own history: ranking by past late rate alone is within 0.01 ROC-AUC. Its
  extra value is calibrated probabilities (the calibration table in the report tracks the
  observed rate closely) and the smaller effects that make "consider net-7" advice possible.
- The detector catches every `new_large` and most `spike` anomalies but only 2 of 6
  `burst` payouts. With 27 anomalies in the test window, its numbers move a lot between
  seeds; the bootstrap interval shows how much.
- Its 13 false positives in the test window are ordinary first payouts to a new recipient
  (6) and busy recipients paid several times in one day (7). In PayPilot each one costs
  an extra review click, which is the intended trade.

## Changes from the original plan, and why

- **Sigmoid instead of isotonic calibration.** Isotonic regression tends to overfit with
  fewer than about 1,000 calibration rows, and this holdout has 500. The report lists both
  calibrations for both candidates, so the effect of the choice is visible.
- **70/10/20 split instead of 80/20.** Calibration and the anomaly threshold need data the
  test window must not touch.
- **No hour or weekday features for payouts.** The synthetic payouts have no time-of-day
  pattern, and time features would make the live demo's flags depend on the clock. A
  24-hour per-recipient count and a one-hour count catch bursts instead of a 7-day count.

## Known limitations and risks

- **Synthetic data.** The generator encodes assumptions (for example, longer terms mean
  later payment); a model trained on it learns those assumptions back.
- **Cold start.** With no history, ratio features are 0 and the risk model falls back to
  the 30% prior: new users get fewer flags and less specific reasons.
- **Fairness.** Trained on real data, a late-payment score could encode patterns that
  disadvantage some kinds of clients. PayPilot only uses it for reminder timing and tone,
  and keeps the decision to send, and every reason shown, visible to the user.
- **Drift.** Nothing retrains automatically. With real data this should be monitored
  (calibration by month, flag rate by month) and retrained on a schedule.

## Reproduce

```bash
make train   # generate_data -> train_risk -> train_anomaly -> evaluate
```

Built with scikit-learn 1.9.1, numpy 2.5.4, pandas 3.0.6 (pinned in `requirements.txt`).
Re-running produces byte-identical CSVs, models and reports; a test fails if the committed
CSVs drift from the generator.
