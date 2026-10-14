# PayPilot: Devpost write-up

Paste each section into the matching Devpost field. Everything marked **[TODO: personal story]** is yours to write; nothing personal has been made up here. Other `[TODO: …]` markers are links you'll have after deploying and recording.

**Tagline:** Your AI co-pilot for getting paid and paying smart.

**Links:** demo video [TODO: video link] · live app [TODO: Render URL] · code https://github.com/RoboDevelopX/PayPilot

---

## Inspiration

AI agents are getting good at doing things, not just answering questions. Money is where most people stop: handing an AI your wallet feels reckless, yet chasing invoices and paying people by hand is tedious. PayPilot asks what it would take to let an agent do the work without ever trusting it with the final say.

**[TODO: personal story]** Write a few sentences about the real moment that made you want to build this: an invoice you or someone you know had to chase, a payment that went wrong, or a reason you care about safe AI around money. Only use what actually happened.

## What it does

PayPilot is a chat assistant for a small business's PayPal tasks, with rules the AI can't change.

- **Invoices from a sentence.** "Invoice Acme Corp (ap@acme.example) $450 for logo design, due in 14 days" becomes a PayPal invoice, created and sent through Invoicing v2. If a detail is missing, it asks one question instead of guessing.
- **Payouts with guardrails.** "Pay sam@example.com $40 for the icon set" goes out through PayPal Payouts only if the policy allows it. Small payments to known payees go through on their own. Anything above your limit, or to someone new, waits for your approval with the exact reason. Daily and monthly caps and a currency allowlist are hard blocks that even an approval can't override.
- **A second opinion from ML.** An anomaly detector (Isolation Forest) looks at each payout against that payee's history. When something looks off, the approval card turns red and needs an explicit "I've checked this" tick. ML can only make PayPilot more careful, never less.
- **Follow-ups that fit the client.** A background job checks open invoices with PayPal and sends reminders that go from friendly to firm to final as an invoice ages. A late-payment risk model moves habitual late payers up the ladder sooner and suggests shorter payment terms for them. A "final" reminder waits for you by default.
- **Everything on the record.** Each proposal, decision, approval, PayPal call (with PayPal's debug id) and webhook goes into an append-only audit log that the database itself refuses to edit.
- **A dashboard** for money owed, overdue and paid this month, spend against caps, clients at risk and flagged payouts.

The rule at the center, for a payout of amount *a*:

```
BLOCK            if the currency isn't allowed, or today's or this month's payouts + a would pass the cap
NEEDS_APPROVAL   if the payee is new, or a > L (your auto-approve limit)
ESCALATED        if the anomaly detector flags it
AUTO             otherwise
```

## How I built it

- **The core idea: the AI proposes, the code disposes.** The language model (Google Gemini on the free tier, or Anthropic Claude, switchable with one setting) uses tool calling with strict JSON schemas. It can propose six actions: create an invoice, send a payout, send a reminder, look something up, ask a clarifying question, or propose a rule change. It never sees PayPal credentials and has no way to call PayPal.
- **Validation and policy.** Pydantic v2 turns each proposal into typed data, with amounts as `Decimal` (never floats). A pure Python policy engine with no I/O decides, returning a level and a readable reason for every rule. Its boundary cases (exactly at the limit, one cent over, the cap edge, the month boundary) are unit tested.
- **PayPal sandbox.** OAuth 2.0 client credentials with a cached token; Invoicing v2 (create, send, get, remind); Payouts v1 (create, get); and webhook signature verification. Every create call carries a `PayPal-Request-Id` derived from the saved action, so a retry after a dropped connection can't pay or bill twice. Webhook events are verified with PayPal, deduplicated, and backed up by polling.
- **ML with scikit-learn.** A late-payment risk model (logistic regression with sigmoid calibration, picked over gradient boosting on a calibration window) and an Isolation Forest anomaly detector, both trained on seeded synthetic data with a chronological train, calibration and test split, bootstrap confidence intervals, and baselines reported next to the models.
- **App.** FastAPI, SQLAlchemy on SQLite, Jinja templates with HTMX, deployed to Render from a blueprint. A labeled demo mode seeds sample data and falls back to recorded PayPal responses if the sandbox is unreachable, so the demo still works when the network doesn't.

## Challenges I ran into

**[TODO: personal story]** Describe the challenges you actually hit, in your own words: what was hard, what you tried, what you'd do differently.

Technical problems the build had to solve, if you want to mention any:

- **Keeping the model away from money without making it useless.** Rule changes became proposals that always wait for the user, so text injected into a chat or an invoice description can't raise its own limits.
- **Making retries safe.** `PayPal-Request-Id` has a short length limit, so each step's id is a UUID derived from the action id, and the action is saved before PayPal is called.
- **Honest ML.** Isotonic calibration on a 500-row calibration window collapsed predictions into a few dozen distinct values and lowered ROC-AUC, so the model uses sigmoid calibration, and both are reported. The risk model only slightly beats the client's own past late rate (ROC-AUC 0.756 vs 0.748), and the write-up says so rather than hiding it.
- **A demo that survives a flaky network** without ever passing off simulated results as real: recorded responses are labeled in the UI and the audit log, and a webhook can never be "verified" from a recording.

## Accomplishments that I'm proud of

- The policy engine, not the prompt, decides what happens to money, and the approval re-checks the hard rules, so an approval can't push spending past a cap.
- Idempotent PayPal calls end to end, with a live smoke test that retries a payout and checks that PayPal creates no second batch.
- ML that is honest about itself: real held-out metrics with confidence intervals, baselines next to every model, a model card, and `make train` reproducing the artifacts byte for byte.
- Anyone can try the whole flow in about two minutes with no keys, and everything simulated is labeled.
- About 200 automated tests, plus CI running lint, formatting and a secret check.

## What I learned

**[TODO: personal story]** What did you learn? Some topics this project touches, if they match your experience: how idempotency keys make payment retries safe, why webhooks need signature verification and a polling fallback, OAuth client credentials, and deploying a FastAPI app on Render.

## What's next for PayPilot

- Learn from the user's own approvals and rejections, turning the unsupervised anomaly detector into a supervised one.
- Train the risk model on real invoice histories (with consent) and monitor calibration over time.
- Per-agent budgets, so several assistants can share one wallet with separate limits.
- Approvals from a phone notification instead of the dashboard.
- Postgres and a proper job queue for multi-user use.

## Built with

python, fastapi, pydantic, sqlalchemy, sqlite, httpx, jinja, htmx, scikit-learn, numpy, pandas, paypal-invoicing, paypal-payouts, paypal-webhooks, google-gemini, anthropic-claude, render
