# Tools used in PayPilot

## AI

**Claude by Anthropic, via the Claude API** (`claude-opus-5-5` by default, set with `ANTHROPIC_MODEL`), called from Python with the official `anthropic` SDK.

Claude is used in two places:

1. **Understanding requests and proposing actions (tool use).** Every chat message goes to Claude with eight tools, all defined with `strict: true` JSON schemas so arguments always match the schema:

   | Tool | Kind | What happens after Claude calls it |
   |---|---|---|
   | `create_invoice` | proposal | validated, checked against the invoice cap and currencies, then created and sent with PayPal Invoicing v2 |
   | `send_payout` | proposal | validated, then the spending rules decide: auto-approve and send with PayPal Payouts, queue for approval, or deny |
   | `send_reminder` | proposal | refused if the invoice is paid or the reminder limit is reached, otherwise sent with the Invoicing v2 remind endpoint |
   | `cancel_invoice` | proposal | always queued for the user's approval |
   | `list_invoices`, `check_invoice`, `list_payouts`, `get_rules` | read-only | answered from the local store or PayPal |

   Claude decides which tool to use, extracts emails, amounts and line items from free text, splits one request into several line items, resolves references like "Alice's invoice" by looking invoices up first, and asks a question when an email or amount is missing. It never sees PayPal credentials. Each tool returns a verdict (`executed`, `pending_approval`, `denied`, `invalid`, `paypal_error`) that Claude explains back to the user. The system prompt tells Claude never to claim money moved unless the verdict was `executed`, and never to work around a rule.

2. **Writing payment reminders (structured output).** When an invoice is due for a follow-up, Claude drafts the subject and message using `output_config.format` with a JSON schema, so the reply is always `{"subject", "note"}`. The tone escalates from friendly to firm with each reminder. If the AI is unavailable, a template is used so reminders still go out.

Other API details: adaptive thinking with `effort: medium` for chat and `low` for reminders, and server-side refusal fallback (`fallbacks: "default"`).

Without an API key, an offline regex parser (`MockAgent`) produces the same tool calls for simple phrasings, so the app can be evaluated with no keys. It is clearly labelled "AI · offline parser" in the UI.

## PayPal (sandbox)

| API | Used for |
|---|---|
| OAuth 2.0 client credentials (`/v1/oauth2/token`) | access tokens, cached and refreshed automatically |
| Invoicing v2: create, send, get, remind, cancel (`/v2/invoicing/invoices…`) | creating and emailing invoices, reading status and the payer link, reminders, cancellation |
| Payouts v1 (`/v1/payments/payouts`) | sending money to an email address |
| Webhooks: verify signature (`/v1/notifications/verify-webhook-signature`) | trusting `INVOICING.INVOICE.*` and `PAYMENT.PAYOUTSBATCH.*` events |

Idempotency: invoices are created with a `PayPal-Request-Id`, and every payout uses a unique `sender_batch_id`, so a retry can't bill or pay twice.

## Everything else

- **Python 3.12, FastAPI, Uvicorn** for the backend and API.
- **httpx** for PayPal REST calls.
- **SQLite** (standard library) for rules, invoices, payouts, approvals and the audit log.
- **Plain HTML, CSS and JavaScript** for the UI, with no build step.
- **pytest** for tests, run by GitHub Actions.
- **Render** for hosting, via the `render.yaml` blueprint.
