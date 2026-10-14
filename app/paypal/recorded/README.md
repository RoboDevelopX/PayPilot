# Recorded PayPal sandbox responses (DEMO_MODE only)

These JSON files are the response bodies PayPilot answers with in demo mode when the live
PayPal sandbox can't be reached, or when no PayPal keys are set. `app/paypal/recorded.py`
fills in ids, amounts and statuses per request. Every simulated answer gets a PayPal-Debug-Id
starting with `demo-recorded-`, so the audit log shows exactly which steps were simulated.

The files in the repo were written by hand from the response shapes in PayPal's Invoicing v2
and Payouts v1 docs; they are not captures of a real sandbox session. To replace them with
real captures from your own sandbox (emails redacted), run:

    python -m scripts.smoke_test --record
