# PayPilot: Devpost write-up

Paste each section into the matching Devpost field. The personal parts of Inspiration, Challenges and What I learned are a first draft written only from what actually happened during this build (the sandbox country block, the cached token, the retry surprise, the Gemini limits, the Render deploy). Read every sentence before pasting and change anything that isn't true for you. The one `[TODO: …]` marker left is the video link, which you'll have after uploading.

## Filling in the Devpost form

1. Upload the video to YouTube first, with **Visibility: Public**, and copy its link. Recording notes are in [DEMO_SCRIPT.md](DEMO_SCRIPT.md).
2. **Project name:** PayPilot. **Elevator pitch:** the tagline below. **Thumbnail:** a 3:2 screenshot of the dashboard.
3. **About the project:** paste everything from Inspiration through What's next for PayPilot, below.
4. **Built with:** the tags at the end of this file.
5. **Try it out:** the live app and the code links below.
6. **Video demo link:** the YouTube link.
7. **Image gallery:** 3:2 screenshots of the dashboard, the chat with payouts, the red approval card, the payout blocked by the cap, the rules and the audit log.
8. Paste the testing instructions below into any testing or judging-instructions field, then check the preview and submit before **Nov 12, 12:00 pm Pacific** (1:30 am Nov 13 in India). You can keep editing until the deadline.

**Tagline:** Your AI co-pilot for getting paid and paying smart.

**Links:** demo video [TODO: video link] · live app https://paypilot-ytst.onrender.com · code https://github.com/RoboDevelopX/PayPilot

---

## Inspiration

AI agents are getting good at doing things, not just answering questions. Money is where most people stop: handing an AI your wallet feels reckless, yet chasing invoices and paying people by hand is tedious. PayPilot asks what it would take to let an agent do the work without ever trusting it with the final say.

I'm an early-career ML engineer in India, and before this project I had never worked with payments, webhooks or deploying an app. The question I kept coming back to was simple: would I let an AI agent touch my own PayPal account? My honest answer was no, not unless something other than the model had the final say. PayPilot is my attempt to build that something, and to learn how payments really work along the way.

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

Most of my hard moments came from the real PayPal sandbox rather than from the code.

- **My sandbox accounts were in India.** The first live run failed: PayPal refused to send invoices to customers within India, and refused payouts with "You live in a country that is not allowed to send this payout." I created US sandbox Business and Personal accounts and a new app linked to the US Business account.
- **A permission that wouldn't switch on.** Creating invoices kept returning 403 even after I ticked Invoicing on the new app. PayPal hands back the same access token for hours, with the permissions it had when it was first issued, so I added `make paypal-token-reset` to revoke it.
- **Retries didn't behave the way I expected.** I expected a retried payout with the same `PayPal-Request-Id` to return the first answer. The live sandbox said instead that the batch already exists. Marking that as "failed" would have freed the spending caps for money that had already left, so PayPilot now follows PayPal's link to the original batch and reports it as sent.
- **Running the AI for free.** The Claude API needs paid credit, so I added Google Gemini's free tier as a second provider. It allows only 20 requests a day on the main model and sometimes returns 503 under load, so PayPilot falls back to a lighter model and then to a clearly labeled demo script.
- **Deploying.** My first Render deploy failed with the same country error as before, because I had pasted the old India app's Client ID. Comparing the first characters of the Client ID on Render with the one in the PayPal dashboard found it.

If I started again, I'd create US sandbox accounts on day one and run one real payment end to end before writing much code.

Technical problems the build had to solve:

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

Almost everything about payments was new to me. I learned:

- why payment APIs need idempotency keys, and that "already sent" must never be treated like "failed";
- that a webhook is just a public URL anyone can call, so each delivery has to be verified with PayPal and backed up by polling;
- how OAuth client credentials work, including that a token keeps the permissions it was issued with;
- that sandbox accounts follow real country rules;
- how to deploy a FastAPI app on Render from a Blueprint, with secrets kept in environment variables;
- and, from the ML side, how humbling an honest baseline is: my risk model only slightly beats each client's own past late rate, and I'd rather report that than hide it.

## What's next for PayPilot

- Learn from the user's own approvals and rejections, turning the unsupervised anomaly detector into a supervised one.
- Train the risk model on real invoice histories (with consent) and monitor calibration over time.
- Per-agent budgets, so several assistants can share one wallet with separate limits.
- Approvals from a phone notification instead of the dashboard.
- Postgres and a proper job queue for multi-user use.

## Testing instructions (for the Devpost testing field)

- **Hosted app:** https://paypilot-ytst.onrender.com. It runs in labeled demo mode on the PayPal **sandbox**, with sample clients and payees, so no keys or logins are needed. The free Render instance sleeps when idle; the first page load can take about a minute.
- **Try this in Chat:** `Pay sam@example.com $40 for the icon set` (paid automatically), `Pay sam@example.com $120 for the brand guide` (held for approval), `Pay jo@example.com $160 for event photos` (escalated by the anomaly model), then approve them on the Approvals page. Every step appears in the Audit log. The hosted app is shared, so if earlier visitors used up today's $200 payout cap, a payout is blocked with that reason; the cap can be raised on the Rules page.
- **Run it yourself in two minutes, no keys:** `git clone https://github.com/RoboDevelopX/PayPilot.git && cd PayPilot && make setup && make demo`, then open http://localhost:8000 (Python 3.11+).
- **Against your own PayPal sandbox:** the README's "Setup with the real PayPal sandbox" section takes under 10 minutes, and `make smoke` checks every PayPal call live.

## Built with

python, fastapi, pydantic, sqlalchemy, sqlite, httpx, jinja, htmx, scikit-learn, numpy, pandas, paypal-invoicing, paypal-payouts, paypal-webhooks, google-gemini, anthropic-claude, render
