# PayPilot demo video: shot list (2:30)

## Before you record

- **Best setup: demo mode with real keys.** Set your PayPal sandbox keys and an AI key in `.env`, then start a clean run with `rm -f demo.db && make demo`. You get the sample clients and payees, while new invoices and payouts go to the real PayPal sandbox and replies come from the real model. The banner then only says the data is sample data.
- **No keys?** `make demo` works as is, but the banner and the replies say PayPal and the assistant are simulated. Keep that visible; don't present it as live.
- In shot 3, answer the email question with your **sandbox Personal account email**, so you can show the invoice arriving at sandbox.paypal.com (logged in as that account, in a second tab).
- Browser at 1280 px wide, zoom 110 to 125%, light theme, notifications off. Close other tabs and bookmarks bars.
- No music you don't own (silence or a royalty-free track whose license you keep), and no logos or brands on screen other than PayPal's own sandbox pages.
- The rules require a video **under 3 minutes**, showing the app running, uploaded to **YouTube as Public**, with the link on the Devpost form.
- Before recording, rehearse once: every reply in the script has been run end to end in demo mode, but a real model words its replies its own way.

## Shots

| Time | On screen | Do | Say (voice-over) |
|---|---|---|---|
| 0:00–0:12 | Dashboard | Slow scroll: money owed, overdue, spend vs caps | "Would you let an AI agent touch your PayPal account? I wouldn't, unless something other than the AI had the final say." Then: "PayPilot is an AI co-pilot for PayPal: it bills, pays and chases, inside rules the AI can't change." |
| 0:12–0:25 | Rules page | Point at the limit ($50), daily cap ($200), allowlist | "Under fifty dollars, to people I already pay, it can act alone. Above that it asks me. The caps are hard limits, even for me." |
| 0:25–0:45 | Chat | Type `Invoice Pine Street Bakery $600 for menu photos` (a client PayPilot hasn't seen, so there's no email on file), then answer the email question | "I describe the job. It asks for the one thing it's missing, then creates and sends a real PayPal sandbox invoice." Cut to the invoice in the sandbox tab for two seconds. |
| 0:45–1:00 | Chat | `Pay sam@example.com $40 for the icon set`, then `Pay sam@example.com $120 for the brand guide` | "Forty dollars to a regular payee goes straight out through PayPal Payouts. A hundred and twenty is over my limit, so nothing moves: it waits for me, with the reason." |
| 1:00–1:20 | Chat, then Approvals | `Pay jo@example.com $160 for event photos`, open Approvals, hover the red card | "Jo is new and the amount is unusual, so the anomaly model escalates it. The model can only make PayPilot more careful, never less, and this card can't be approved without a deliberate tick." |
| 1:20–1:38 | Approvals | Approve Sam's $120 (it executes). Tick Jo's box and approve: **blocked** | "Approving Sam sends it. Now Jo: even with my approval, it's blocked, because approvals re-check the hard rules and this would pass today's two-hundred-dollar cap." |
| 1:38–1:52 | Chat | `Raise my auto-approve limit to $75` | "What if someone tricks the assistant into raising its own limit? It can only propose it. Rule changes always wait for me." |
| 1:52–2:10 | Dashboard, then Chat | Point at Acme Corp in "Open invoices most likely to be paid late" and the net-7 advice. Then `Send a reminder to Acme Corp` | "A risk model trained on synthetic payment histories says Acme usually pays late, and suggests shorter terms. Their reminder jumps to a final tone, which I review before it goes out." |
| 2:10–2:22 | Audit log | Scroll the newest events: proposal, decision, PayPal call with its debug id | "Every proposal, decision and PayPal call is logged, and the log can't be edited." |
| 2:22–2:30 | Dashboard | Hold | "The AI proposes, the code disposes. PayPilot." |

## If something goes wrong while recording

- **The sandbox is slow or down:** in demo mode PayPilot answers from labeled recordings, and the reply says so. Either re-record later or keep it and let the label show.
- **The model words a reply differently:** that's fine; the actions and their statuses are what matter. If it doesn't propose an action at all, rephrase exactly as in the table.
- **You need a clean slate:** stop the app, `rm -f demo.db`, `make demo`.
