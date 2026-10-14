# SYNTHETIC data

Every row in this folder was invented by `ml/generate_data.py` (seed 42). No real person,
business or payment is represented. Regenerate with `make train`; see `ml/MODEL_CARD.md`
for how the data was generated and what it leaves out.

- `invoices.csv`: one row per invoice. `late` is 1 if it was paid after `due_date`.
- `payouts.csv`: one row per payout. `anomaly` is 1 for the 2% of rows that were injected
  as anomalies, and `anomaly_type` says which kind (`spike`, `new_large`, `burst`).
