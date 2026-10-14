# Recorded PayPal sandbox responses (DEMO_MODE only)

These JSON files are the response bodies PayPilot answers with in demo mode when the live
PayPal sandbox can't be reached, or when no PayPal keys are set. `app/paypal/recorded.py`
fills in ids, amounts and statuses per request. Every simulated answer gets a PayPal-Debug-Id
starting with `demo-recorded-`, so the audit log shows exactly which steps were simulated.

`invoice.json` and `payout_get.json` are real captures from a US sandbox run on 2026-10-14
(emails redacted). `payout_create.json` is still written by hand from the Payouts v1 docs,
because the create answer is only a batch header. To re-capture from your own sandbox, run:

    python -m scripts.smoke_test --record
