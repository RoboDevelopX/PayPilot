## Inspiration

AI agents are getting good at *doing* things: booking, researching, writing code. But the moment money is involved, most people hesitate. Handing an AI your wallet feels risky, and chasing payments by hand feels tedious. I wanted to see if both problems could be solved together.

PayPilot is my answer: an AI agent that handles payment tasks through PayPal, while the user stays in control with clear rules and approvals. **[Add a personal line here: a real moment where you or someone you know struggled with a payment, an unpaid invoice, or splitting a bill.]**

## What it does

PayPilot lets you describe a payment task in plain language, and the agent takes care of it through PayPal:

- **Creates and sends PayPal invoices from a chat message.** "Bill dana@example.com for 3 hours of consulting at $120 and a $40 travel fee, due in 30 days" becomes a PayPal invoice with two line items, a due date and a friendly note, emailed to the client by PayPal.
- **Follows up automatically on unpaid invoices.** A scheduler re-checks each invoice with PayPal and sends reminders on the user's timetable. The AI writes each reminder, getting a little firmer each time, and stops the moment the invoice is paid.
- **Pays people within the limits you set.** "Pay bob@example.com $25 for lunch" goes out through PayPal Payouts immediately if it fits the rules. Anything above the limit waits in an approval queue with the exact reasons, and nothing is sent until the user approves.

Every action follows rules the user sets. A simple version of the approval logic is:

$$
\text{action} =
\begin{cases}
\text{auto-approve} & \text{if } a \le L \\
\text{ask the user} & \text{if } a > L
\end{cases}
$$

where $a$ is the payment amount and $L$ is the user's spending limit.

On top of $L$ there is a daily auto-approve budget, a hard cap that even an approval cannot exceed, a currency allowlist, an optional "ask before paying someone new" rule, and cancellation always needs a human. Every proposal and decision is written to an audit log the user can read.

## How I built it

- **PayPal:** Invoicing v2 (create, send, status, remind, cancel), Payouts, OAuth 2.0 and webhook signature verification, all running in the PayPal sandbox.
- **AI:** Claude (Claude Opus 5.5 through the Claude API) interprets the user's request, picks the right action, and drafts messages. It works through **tool calling**: eight tools with strict JSON schemas (create invoice, send payout, send reminder, cancel invoice, and read-only lookups). Claude extracts the recipient, amounts and line items, and asks when something is missing. Reminder emails are written with **structured output** (a JSON schema for subject and body).
- **Backend:** Python with FastAPI, which validates every agent action against the user's rules *before* anything reaches PayPal.
- **Frontend:** a simple chat UI with panels for rules, approvals, invoices, payouts and the audit log.
- **Hosting:** Render, from a `render.yaml` blueprint. A mock mode lets anyone run the whole flow without keys.

The key design decision was that the **AI proposes, the code disposes**. The model never calls PayPal directly. It outputs a structured action, and deterministic code checks the limits and approvals first. The model has no PayPal credentials and no tool that can change its own limits, and every result it sees is a verdict from the code (executed, waiting for approval, or denied), so it can't claim a payment happened when it didn't.

## Challenges I ran into

- **Getting reliably structured actions instead of free text.** Strict tool schemas fixed the shape, but amounts still arrive as text like "1,200" or "$40". Every value goes through a deterministic validator (decimal parsing, rounding to cents, rejecting zero, negative, NaN and infinity) before any rule is checked.
- **PayPal sandbox details.** Invoices need a sandbox business account with invoicing enabled, the create call returns a link rather than always the full invoice, and making retries safe meant using `PayPal-Request-Id` for invoices and unique `sender_batch_id`s for payouts.
- **Deciding how much autonomy the agent should have.** Too strict and it's just a form; too loose and it's scary. I settled on autonomy for small, known payments and for invoices (which only bring money in), human approval above the limit and for anything irreversible like cancelling, and hard rules that no one can override from the chat.

## Accomplishments that I'm proud of

- A fully working end-to-end flow from chat message to sandbox invoice, payout, reminder and payment.
- Guardrails that stop the agent from overspending: the per-payment limit, a daily budget that also catches many small payments, a hard cap, and approvals that can't be replayed.
- A mock mode and a test suite, so judges can run it in a minute without any keys.

## What I learned

- How the PayPal developer platform and sandbox work: OAuth, invoicing, payouts and webhooks.
- Why guardrails matter more than prompts when an AI touches money. The prompt asks the model to behave; the code makes sure it does.
- Shipping a small, complete demo beats a big, half-finished one.

## What's next for PayPilot

- Multi-currency support with per-currency limits.
- Smarter reminders that adapt to each client's payment habits.
- Per-agent budgets for teams running several AI agents.
- Approvals from a phone notification instead of the dashboard.
- PayPal Orders and Subscriptions for recurring billing.
