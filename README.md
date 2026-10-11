# ✈ PayPilot

**Your AI co-pilot for getting paid and paying smart.**

PayPilot is an AI agent that handles PayPal payment tasks from plain-language chat: it creates and sends PayPal invoices, pays people through PayPal Payouts, and follows up on unpaid invoices on its own. You stay in control with rules the agent cannot change: payments at or under your limit go through automatically, and anything above it waits for your approval.

Built for the **Build What's Next with PayPal and AI** hackathon.

> **The AI proposes, the code disposes.** Claude never calls PayPal. It returns a structured action (a tool call), and deterministic Python checks it against your rules before anything reaches PayPal.

```
 You: "Send carol@example.com $400 for the photo shoot"
   │
   ▼
 Claude (tool use) ──► send_payout {recipient_email, amount: "400", currency: "USD"}   ← a proposal, nothing more
   │
   ▼
 Validator (app/rules.py)  shape ✓  email ✓  amount ✓  currency ✓  hard cap ✓
   │
   ▼
 Policy    a = $400 > L = $50  ──►  needs_approval  ──►  Approvals panel (nothing sent)
           a ≤ L, within daily budget  ──►  auto_approve  ──►  PayPal Payouts API
           breaks a hard rule  ──►  deny (never reaches PayPal)
```

## Features

- **Invoices from a sentence.** "Bill dana@example.com $300 for 3 hours of consulting, due in 30 days" becomes a PayPal invoice (Invoicing v2) with line items, a due date and a friendly note, sent to the client by PayPal.
- **Payouts with guardrails.** "Pay bob@example.com $25 for lunch" sends money through PayPal Payouts, only when the rules allow it:

  | Rule | Effect |
  |---|---|
  | Per-payment limit **L** | `a ≤ L` auto-approves, `a > L` asks you |
  | Daily auto-approve budget | auto-approvals stop once today's total would pass it |
  | Hard cap | never exceeded, even with your approval |
  | Allowed currencies | anything else is denied |
  | Ask before paying someone new (optional) | first payout to a new email waits for you |
  | Cancelling an invoice | always needs your approval |

- **Approval queue.** Anything above your rules waits in the Approvals panel with the exact reasons. Approving re-checks the hard rules, and an approval can only be used once.
- **Automatic follow-ups.** A scheduler checks unpaid invoices against PayPal and sends reminders on your timetable (first after N hours, then every M hours, up to a maximum). Claude writes each reminder, getting gently firmer each time. Paid invoices are never chased, because status is re-read from PayPal first.
- **Audit log.** Every proposal, verdict, approval and PayPal call is recorded.
- **Webhooks.** `POST /webhooks/paypal` verifies PayPal's signature and marks invoices paid and payouts settled as PayPal reports them.
- **Mock mode.** With no keys, PayPilot runs against an in-memory PayPal and an offline parser, so anyone can try the whole flow in a minute.

## Quick start (no keys, about 1 minute)

Requires Python 3.10 or newer.

```bash
git clone https://github.com/RoboDevelopX/paypilot.git
cd paypilot
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
uvicorn app.main:app --reload
```

Open http://localhost:8000. The header shows **PayPal · mock mode** and **AI · offline parser**. Click the suggestion chips, approve something in the Approvals panel, press **Simulate payment** on an invoice, and press **Run follow-ups now**.

## Run with the real PayPal sandbox and Claude

1. **PayPal sandbox app.** Sign in at [developer.paypal.com](https://developer.paypal.com), open **Apps & Credentials**, stay on **Sandbox**, and create an app (type: Merchant). Copy its **Client ID** and **Secret**. Under the app's features, make sure **Invoicing** and **Payouts** are enabled.
2. **Sandbox test accounts.** Under **Testing Tools > Sandbox Accounts** you have a Business account (the one your app sends from) and a Personal account (use its email as a client or payee). Log in to [sandbox.paypal.com](https://www.sandbox.paypal.com) with the Personal account to pay invoices.
3. **Anthropic API key** from [console.anthropic.com](https://console.anthropic.com).
4. Put the keys in a local `.env` (never commit it, never paste keys into chats or issues):

   ```bash
   cp .env.example .env
   # edit .env: PAYPAL_CLIENT_ID, PAYPAL_CLIENT_SECRET, ANTHROPIC_API_KEY
   uvicorn app.main:app --reload
   ```

   The header now shows **PayPal sandbox · live** and the Claude model in use.

5. **Optional webhooks.** In your sandbox app, add a webhook pointing to `https://<your-host>/webhooks/paypal` with the events `INVOICING.INVOICE.PAID`, `INVOICING.INVOICE.CANCELLED`, `PAYMENT.PAYOUTSBATCH.SUCCESS` and `PAYMENT.PAYOUTSBATCH.DENIED`, and set `PAYPAL_WEBHOOK_ID`. Without webhooks, invoice status is still refreshed from PayPal before every reminder and with the **Refresh status** button.

Each half falls back to mock independently: PayPal keys alone give real PayPal with the offline parser, and an Anthropic key alone gives Claude with mock PayPal. `PAYPILOT_MOCK_PAYPAL=1` or `PAYPILOT_MOCK_AI=1` forces mock mode.

## Deploy on Render

1. Fork or push this repo to GitHub.
2. In Render, choose **New > Blueprint** and select the repo. `render.yaml` defines the web service.
3. Fill in `PAYPAL_CLIENT_ID`, `PAYPAL_CLIENT_SECRET` and `ANTHROPIC_API_KEY` when prompted (leave them blank for a mock-mode demo).
4. Open the service URL. `/healthz` reports which modes are active.

Note: Render's free plan sleeps when idle and has no persistent disk, so the demo database resets on restart and follow-ups only run while the service is awake. Use a paid instance with a disk (and point `PAYPILOT_DB` at it) for anything longer-lived. The chat endpoint is rate-limited per IP so a public demo can't run up the AI bill.

## Try these

| Say | What happens |
|---|---|
| `Invoice alice@example.com $1,200 for the website redesign, due in 30 days` | Invoice created and sent through PayPal |
| `Bill Dana Lee dana@example.com for 3 hours of consulting at $120 and a $40 travel fee` | Claude splits it into two line items |
| `Pay bob@example.com $25 for lunch` | `$25 ≤ L`, auto-approved, Payout sent |
| `Send carol@example.com $400 for the photo shoot` | `$400 > L`, queued for your approval |
| `Pay dan@example.com $5,000` | Above the hard cap, denied |
| `Which invoices are still unpaid?` | Lists open invoices |
| `Remind Alice about her invoice` | Claude finds the invoice, then a reminder goes out through PayPal |
| `Cancel Alice's invoice` | Always queued for approval |
| `Raise my limit to $10,000` | Refused: only you can change rules, in the Rules panel |

(The offline parser understands the simpler phrasings; Claude understands all of them.)

## How it works

| File | Role |
|---|---|
| `app/agent.py` | Claude tool-use loop. Defines strict tool schemas, sends the conversation, hands each tool call to the service, returns the verdict to Claude. Also drafts reminder text with structured output. Includes the offline `MockAgent`. |
| `app/rules.py` | Pure, deterministic validation and policy. No I/O, fully unit tested. |
| `app/actions.py` | The only code that calls PayPal, and only after a verdict allows it. Approval queue, reminders, audit log. |
| `app/paypal.py` | PayPal REST client (OAuth2, Invoicing v2, Payouts v1, webhook verification) plus an in-memory mock with the same interface. |
| `app/main.py` | FastAPI routes, reminder scheduler, webhook endpoint. |
| `static/` | The chat UI with Rules, Approvals, Invoices, Payouts and Audit log panels. |

Safety details worth knowing:

- Claude has no PayPal credentials and no tool that changes rules.
- Amounts are parsed as decimals and rounded to cents. NaN, infinity, zero and negative amounts are rejected.
- Payouts use a unique `sender_batch_id` and invoices use a `PayPal-Request-Id`, so retries cannot pay or bill twice.
- Evaluating and recording a payout happens under a lock, so two simultaneous requests can't both slip under the daily budget.
- An approval re-checks the hard rules and can be used once.

See [docs/TOOLS.md](docs/TOOLS.md) for the tools and AI usage, and [docs/WRITEUP.md](docs/WRITEUP.md) for the project write-up.

## Tests

```bash
pip install -r requirements-dev.txt
pytest -q
```

The suite covers the rules engine, the full HTTP flow in mock mode, and the Claude tool loop with a scripted fake API.

## License

[MIT](LICENSE)
